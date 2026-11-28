"""应用层裁决与数据库层裁决的**一致性**用例（ADR-R24 §D3a-2 的核心证据）。

为什么需要这一条（根因）
------------------------------------------------------------
组织边界现在有两处执行位置：

* 应用层 ``packages/identity/ownership.py::assert_ownership_scope``（决定是否 403）；
* 数据库层 RLS 组织谓词（决定行是否可见，不可见在 API 上表现为 404）。

「两处执行同一规则」这件事本身是风险：任何一处改了而另一处没改，就会出现
「应用放行、数据库拒绝」或反过来的两套规则，而且**两边各自看起来都对**。
文本断言救不了它——``pg_policies.qual`` 只是字符串，抄一份一样的字符串很容易。

因此这里用**同一张判定矩阵**分别打两层，断言裁决相同：矩阵是唯一的判据来源，
两层只是这份判据的两个执行者。矩阵增删一行，两层必须同时跟着变，否则本用例报红。

矩阵（资源组织 × 主体组织）
------------------------------------------------------------
| 资源组织 | 主体组织 | 期望裁决 |
| --- | --- | --- |
| 空（个人租户） | 无 | 放行 |
| 空（个人租户） | 组织 A | 放行 |
| 组织 A | 组织 A | 放行 |
| 组织 A | 无 | 拒绝 |
| 组织 A | 组织 B | 拒绝 |
| 组织 B | 组织 A | 拒绝 |

「拒绝」在两层有不同形态（应用层抛 403、数据库层不可见），但**裁决**必须一致：
允许 ⇔ 行可见。这正是本文件断言的东西。

依赖真实 PostgreSQL，无 PG 环境时跳过（与 tests/integration 同口径）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from packages.errors.error_codes import DomainError
from packages.identity.ownership import (
    OwnershipPath,
    ResourceRef,
    assert_ownership_scope,
)
from packages.identity.principal import Principal
from tests.conftest import set_test_scope


# 判定矩阵：(资源组织, 主体组织, 是否允许)。
# 资源组织用 None 表示「个人租户资源」（organization_id 为空）。
# 组织 id 必须**按用例现取**：模块级常量会让第二个用例拿同一个 organization_id
# 去插另一个租户，`ON CONFLICT DO NOTHING` 会静默跳过、随后被复合外键拒绝——
# 报错看起来像「外键坏了」，其实是「同一组织被两个租户共用」这个用例缺陷。
def _matrix(
    org_a: uuid.UUID, org_b: uuid.UUID
) -> tuple[tuple[uuid.UUID | None, uuid.UUID | None, bool], ...]:
    return (
        (None, None, True),
        (None, org_a, True),
        (org_a, org_a, True),
        (org_a, None, False),
        (org_a, org_b, False),
        (org_b, org_a, False),
    )


def _principal(tenant_id: uuid.UUID, organization_id: uuid.UUID | None) -> Principal:
    return Principal(
        subject_id="parity-probe",
        tenant_id=tenant_id,
        roles=frozenset({"导演"}),
        capabilities=frozenset({"*"}),
        organization_id=organization_id,
    )


def _app_verdict(
    tenant_id: uuid.UUID,
    resource_org: uuid.UUID | None,
    principal_org: uuid.UUID | None,
) -> bool:
    """应用层裁决：允许返回 True，拒绝（DomainError）返回 False。"""
    try:
        assert_ownership_scope(
            _principal(tenant_id, principal_org),
            ResourceRef("Project", "parity"),
            OwnershipPath(tenant_id=tenant_id, organization_id=resource_org),
        )
    except DomainError:
        return False
    return True


async def _seed_organization(
    admin_sessionmaker, tenant_id: uuid.UUID, organization_id: uuid.UUID
) -> None:
    """播种组织行（管理连接）。

    DATA-6 之后业务域角色对 ``organizations`` 只有 SELECT——契约里没有
    ``ORGANIZATION.*`` 动作，组织由管理面创建。播种是测试基础设施，用管理连接
    对应尚不存在的管理面路径；放宽授权让业务角色能插，才是方向反了。
    管理连接不受 RLS 约束，因此不需要注入作用域（前提由 fixture 断言）。
    """
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, '一致性用例组织', :now, 'tester', :now)
                """
            ),
            {"org": organization_id, "tid": tenant_id, "now": datetime.now(UTC)},
        )
        await session.commit()


async def _seed_project(
    sessionmaker,
    tenant_id: uuid.UUID,
    organization_id: uuid.UUID | None,
) -> str:
    """播种一个项目，返回 entity_id（写入上下文取项目自己的组织）。"""
    entity_id = uuid.uuid4().hex
    async with sessionmaker() as session:
        await set_test_scope(session, organization_id, tenant_id=tenant_id)
        await session.execute(
            text(
                """
                INSERT INTO project_planning.projects
                    (entity_id, entity_type, schema_version, entity_version, status,
                     owner_module, tenant_id, created_at, created_by, updated_at,
                     project_name, project_tier, organization_id)
                VALUES (:eid, 'Project', 205, 1, 'DRAFT', 'project-planning',
                        :tid, :now, 'tester', :now, '一致性用例项目', 'B', :org)
                """
            ),
            {
                "eid": entity_id,
                "tid": tenant_id,
                "now": datetime.now(UTC),
                "org": organization_id,
            },
        )
        await session.commit()
    return entity_id


async def _db_verdict(
    sessionmaker,
    tenant_id: uuid.UUID,
    entity_id: str,
    principal_org: uuid.UUID | None,
) -> bool:
    """数据库层裁决：该主体作用域下「这一行是否可见」（可见 ⇔ 允许）。"""
    async with sessionmaker() as session:
        await set_test_scope(session, principal_org, tenant_id=tenant_id)
        visible = await session.scalar(
            text("SELECT count(*) FROM project_planning.projects WHERE entity_id = :eid"),
            {"eid": entity_id},
        )
        await session.rollback()
        return bool(visible)


async def test_matrix_verdict_matches_between_layers(db_sessionmaker, admin_sessionmaker) -> None:
    """同一张矩阵打两层：应用层裁决必须与数据库可见性逐一相同。

    只比较 **裁决**（允许/拒绝），不比较响应形态：应用层拒绝是 403，数据库层
    拒绝是「查不到」（API 上表现为 404）。形态不同是刻意的（不可见不泄漏存在性），
    裁决相同才是这条不变量要守的东西。
    """
    tenant_id = uuid.uuid4()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _seed_organization(admin_sessionmaker, tenant_id, org_a)
    await _seed_organization(admin_sessionmaker, tenant_id, org_b)

    project_by_org: dict[uuid.UUID | None, str] = {
        None: await _seed_project(db_sessionmaker, tenant_id, None),
        org_a: await _seed_project(db_sessionmaker, tenant_id, org_a),
        org_b: await _seed_project(db_sessionmaker, tenant_id, org_b),
    }

    mismatches: list[str] = []
    for resource_org, principal_org, expected in _matrix(org_a, org_b):
        app_allows = _app_verdict(tenant_id, resource_org, principal_org)
        db_allows = await _db_verdict(
            db_sessionmaker, tenant_id, project_by_org[resource_org], principal_org
        )
        label = f"资源组织={resource_org} 主体组织={principal_org}"
        if app_allows != expected:
            mismatches.append(f"{label}: 应用层裁决={app_allows}，期望={expected}")
        if db_allows != expected:
            mismatches.append(f"{label}: 数据库层可见性={db_allows}，期望={expected}")
        if app_allows != db_allows:
            mismatches.append(f"{label}: 两层裁决不一致（应用={app_allows} / 数据库={db_allows}）")

    assert not mismatches, "组织范围判定两层不一致：\n" + "\n".join(mismatches)


async def test_other_organization_rows_are_invisible(db_sessionmaker, admin_sessionmaker) -> None:
    """他组织的行不是「可见但被拒」，而是**查不到**（RLS 过滤，ADR-R24 D3a-2）。"""
    tenant_id = uuid.uuid4()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _seed_organization(admin_sessionmaker, tenant_id, org_a)
    project_id = await _seed_project(db_sessionmaker, tenant_id, org_a)

    # 组织 A 自己看得见。
    assert await _db_verdict(db_sessionmaker, tenant_id, project_id, org_a) is True
    # 组织 B（同租户）看不见。
    assert await _db_verdict(db_sessionmaker, tenant_id, project_id, org_b) is False
    # 无组织上下文（个人主体）同样看不见。
    assert await _db_verdict(db_sessionmaker, tenant_id, project_id, None) is False


async def test_child_resource_follows_parent_scope(db_sessionmaker, admin_sessionmaker) -> None:
    """子资源（集数）随父项目的组织一起收敛：组织规则只写一次，靠父表传递。

    这是 ADR-R24 §D3a-3 的行为证据：episodes 没有 organization_id 列，若它的
    可见性与父项目不一致，说明「组织规则在子表处成了另一份副本」。
    """
    tenant_id = uuid.uuid4()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _seed_organization(admin_sessionmaker, tenant_id, org_a)
    project_id = await _seed_project(db_sessionmaker, tenant_id, org_a)
    episode_id = uuid.uuid4().hex

    async with db_sessionmaker() as session:
        await set_test_scope(session, org_a, tenant_id=tenant_id)
        await session.execute(
            text(
                """
                INSERT INTO project_planning.episodes
                    (entity_id, entity_type, schema_version, entity_version, status,
                     owner_module, tenant_id, created_at, created_by, updated_at,
                     project_id, episode_ref, title)
                VALUES (:eid, 'Episode', 205, 1, 'DRAFT', 'project-planning',
                        :tid, :now, 'tester', :now, :pid, 'EP001', '一致性用例集数')
                """
            ),
            {
                "eid": episode_id,
                "tid": tenant_id,
                "now": datetime.now(UTC),
                "pid": project_id,
            },
        )
        await session.commit()

    async def _episode_visible(organization_id: uuid.UUID | None) -> bool:
        async with db_sessionmaker() as session:
            await set_test_scope(session, organization_id, tenant_id=tenant_id)
            count = await session.scalar(
                text("SELECT count(*) FROM project_planning.episodes WHERE entity_id = :eid"),
                {"eid": episode_id},
            )
            await session.rollback()
            return bool(count)

    assert await _episode_visible(org_a) is True
    assert await _episode_visible(org_b) is False
    assert await _episode_visible(None) is False


def test_app_layer_matrix_is_self_consistent() -> None:
    """矩阵自检：应用层判定不依赖数据库，可独立跑（防止漏 PG 时矩阵完全没被执行）。

    没有这条，``test_matrix_verdict_matches_between_layers`` 在无 PG 环境下被跳过，
    矩阵本身（判据来源）就无人校验——被跳过的用例不该连带跳过「判据对不对」。
    """
    tenant_id = uuid.uuid4()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    for resource_org, principal_org, allow in _matrix(org_a, org_b):
        assert _app_verdict(tenant_id, resource_org, principal_org) is allow, (
            f"矩阵行 ({resource_org}, {principal_org}) 期望 {allow}"
        )
