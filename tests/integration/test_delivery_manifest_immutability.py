"""交付清单的结构性约束验证（数据库层，内容不可变 + 状态登记 + 最小授权）。

审查口径是「约束不能只活在代码里」。本文件不经过服务层，直接对表动手，
证明四条性质：

1. **内容不可变**：改业务内容列的 `UPDATE`、`DELETE` 被触发器拒绝——
   **以超级用户直连也一样**，证明拦的是结构而不是角色授权（D4 后清单从"全禁
   UPDATE"演进为"内容不可变 + 状态沿边推进"，C1 同款模式）；
2. **状态可表示但不可乱写**：契约外取值被 CHECK 拒绝；合法转移必须命中
   `delivery_manifest_status_transitions` 登记边，DRAFT→RELEASED 这类跳跃被拒；
3. **最小授权**：资产域角色有 SELECT/INSERT + 仅四列（status/entity_version/
   updated_at/audit_ref）的列级 UPDATE，没有 DELETE、没有内容列 UPDATE；
   系统目录可查（授权是数据库状态，测业务路径证明不了"别的路径走不通"）；
4. **RLS 双策略生效**：跨租户读不到，同租户跨项目也读不到（`project_scope_*`
   是限制性策略，缺 `project_id` 列时这条根本没法落）。
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.shared.database import build_migration_url
from packages.shared.db_roles import DatabaseDomain, all_business_roles, role_for
from tests.conftest import apply_db_context

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d3a1")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d3a2")
_PROJECT = "proj-manifest-guard"
_OTHER_PROJECT = "proj-manifest-other"

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    """迁移角色（超级用户）连库：证明触发器/约束对超级用户同样生效。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _new_manifest(
    admin_engine: AsyncEngine,
    *,
    status: str = "DRAFT",
    tenant_id: uuid.UUID = _TENANT,
    project_id: str = _PROJECT,
) -> str:
    """直插一条清单（超级用户，不受 RLS 与角色授权影响）。"""
    entity_id = f"manifest-guard-{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                """
                INSERT INTO asset_service.delivery_manifests (
                    entity_id, entity_type, schema_version, entity_version,
                    status, owner_module, tenant_id, organization_id,
                    created_at, created_by, updated_at, audit_ref,
                    project_id, asset_refs, checksum, picture_lock_ref,
                    qc_final_ref, rebuild_recipe, rights_evidence_refs, spec_ref
                ) VALUES (
                    :eid, 'DeliveryManifest', 205, 1,
                    :status, 'asset_service', :tid, :tid,
                    now(), 'manifest-guard', now(), NULL,
                    :pid, '["asset-a"]'::jsonb, repeat('0', 64), 'picture-lock-1',
                    'qc-final-1', 'recipe://spec-1/v1',
                    '["rights-1"]'::jsonb, 'spec-1'
                )
                """
            ),
            {"eid": entity_id, "status": status, "tid": tenant_id, "pid": project_id},
        )
    return entity_id


async def test_insert_draft_succeeds(admin_engine: AsyncEngine) -> None:
    """基线：DRAFT 可插入（后面几条"拒绝"才有意义，否则可能是列冻结伪造）。"""
    entity_id = await _new_manifest(admin_engine)
    async with admin_engine.connect() as conn:
        status = (
            await conn.execute(
                sa.text(
                    "SELECT status FROM asset_service.delivery_manifests "
                    "WHERE entity_id = :e"
                ),
                {"e": entity_id},
            )
        ).scalar_one()
    assert status == "DRAFT"


async def test_update_is_rejected_even_for_superuser(admin_engine: AsyncEngine) -> None:
    """改清单内容 → 触发器拒绝（超级用户直连也拦，证明拦的是结构）。"""
    entity_id = await _new_manifest(admin_engine)
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "UPDATE asset_service.delivery_manifests "
                    "SET checksum = repeat('1', 64) WHERE entity_id = :e"
                ),
                {"e": entity_id},
            )
    assert "不可变内容列" in str(exc_info.value)


async def test_illegal_status_jump_is_rejected(admin_engine: AsyncEngine) -> None:
    """状态跳跃被拒：DRAFT 不能直接到 RELEASED（未登记的边，31.5）。"""
    entity_id = await _new_manifest(admin_engine)
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "UPDATE asset_service.delivery_manifests "
                    "SET status = 'RELEASED' WHERE entity_id = :e"
                ),
                {"e": entity_id},
            )
    assert "非法状态转移" in str(exc_info.value)


async def test_registered_status_transition_succeeds(
    admin_engine: AsyncEngine,
) -> None:
    """登记边可推进：DRAFT → VALIDATING 必须能走通（否则状态机是死的）。"""
    entity_id = await _new_manifest(admin_engine)
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE asset_service.delivery_manifests "
                "SET status = 'VALIDATING' WHERE entity_id = :e"
            ),
            {"e": entity_id},
        )
        status = (
            await conn.execute(
                sa.text(
                    "SELECT status FROM asset_service.delivery_manifests "
                    "WHERE entity_id = :e"
                ),
                {"e": entity_id},
            )
        ).scalar_one()
    assert status == "VALIDATING"


async def test_delete_is_rejected(admin_engine: AsyncEngine) -> None:
    """删除清单 → 拒绝（交付依据不能被悄悄抹掉）。"""
    entity_id = await _new_manifest(admin_engine)
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "DELETE FROM asset_service.delivery_manifests WHERE entity_id = :e"
                ),
                {"e": entity_id},
            )
    assert "append-only" in str(exc_info.value)


async def test_status_outside_contract_enum_is_rejected(admin_engine: AsyncEngine) -> None:
    """契约外状态值写不进去（CHECK 约束，读不认识的枚举会变成 500）。"""
    with pytest.raises(DBAPIError) as exc_info:
        await _new_manifest(admin_engine, status="BANANA")
    assert "ck_delivery_manifests_status_registered" in str(exc_info.value)


@pytest.mark.parametrize(
    "status",
    ["DRAFT", "VALIDATING", "VALIDATED", "RELEASED", "INVALID", "STALE", "REVOKED", "SUPERSEDED"],
)
async def test_all_contract_status_values_are_representable(
    admin_engine: AsyncEngine, status: str
) -> None:
    """契约 8 个取值都必须可表示：CHECK 是快照，漏一个就是未来的 500。"""
    entity_id = await _new_manifest(admin_engine, status=status)
    assert entity_id


_MUTABLE_COLUMNS = {"status", "entity_version", "updated_at", "audit_ref"}


async def test_asset_role_has_only_column_scoped_update() -> None:
    """资产域角色：SELECT/INSERT + 仅四列 UPDATE，无 DELETE、无内容列 UPDATE。"""
    role = role_for(DatabaseDomain.ASSET)
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            table_privs = (
                await conn.execute(
                    sa.text(
                        """
                        SELECT privilege_type
                        FROM information_schema.table_privileges
                        WHERE grantee = :role
                          AND table_schema = 'asset_service'
                          AND table_name = 'delivery_manifests'
                        """
                    ),
                    {"role": role},
                )
            ).scalars().all()
            column_updates = (
                await conn.execute(
                    sa.text(
                        """
                        SELECT column_name
                        FROM information_schema.column_privileges
                        WHERE grantee = :role
                          AND table_schema = 'asset_service'
                          AND table_name = 'delivery_manifests'
                          AND privilege_type = 'UPDATE'
                        """
                    ),
                    {"role": role},
                )
            ).scalars().all()
    finally:
        await engine.dispose()
    # 表级权限仍是 SELECT/INSERT：列级 UPDATE 只在 column_privileges 出现，
    # 这正是"没有表级 UPDATE、只有四列可改"的数据库证据。
    assert set(table_privs) == {"SELECT", "INSERT"}
    # 真正能改的列只有四列——内容列一个都不在。
    assert set(column_updates) == _MUTABLE_COLUMNS


async def test_transition_table_is_read_only_for_app_roles() -> None:
    """转移表对应用角色只读：不能靠加一条边自我放行。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            for role in all_business_roles():
                rows = (
                    await conn.execute(
                        sa.text(
                            """
                            SELECT privilege_type
                            FROM information_schema.table_privileges
                            WHERE grantee = :role
                              AND table_schema = 'asset_service'
                              AND table_name = 'delivery_manifest_status_transitions'
                            """
                        ),
                        {"role": role},
                    )
                ).scalars().all()
                assert set(rows) <= {"SELECT"}, f"{role} 对转移表有多余权限: {rows}"
    finally:
        await engine.dispose()


async def test_transition_table_seeds_registered_edges_only() -> None:
    """转移表恰好只有三条登记边——异常态不在其中（不可达，登记在 gaps）。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            edges = (
                await conn.execute(
                    sa.text(
                        "SELECT from_status || '->' || to_status "
                        "FROM asset_service.delivery_manifest_status_transitions "
                        "ORDER BY 1"
                    )
                )
            ).scalars().all()
    finally:
        await engine.dispose()
    assert edges == ["DRAFT->VALIDATING", "VALIDATED->RELEASED", "VALIDATING->VALIDATED"]


async def test_other_roles_cannot_insert_manifests() -> None:
    """非资产域角色连 INSERT 都没有（交付清单只归属资产域写）。

    只看**应用角色**：`postgres`（表 owner / 迁移角色）天然拥有全部权限，
    把它算进来会让本断言永远失败，也会掩盖"某个应用角色多拿了权限"。
    """
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        """
                        SELECT grantee, privilege_type
                        FROM information_schema.table_privileges
                        WHERE table_schema = 'asset_service'
                          AND table_name = 'delivery_manifests'
                          AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
                        """
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    app_roles = set(all_business_roles())
    writers = {
        (row.grantee, row.privilege_type)
        for row in rows
        if row.grantee in app_roles
    }
    asset_role = role_for(DatabaseDomain.ASSET)
    # 只允许资产域 INSERT；不允许任何应用角色 UPDATE/DELETE。
    assert {w for w in writers if w[1] == "INSERT"} == {(asset_role, "INSERT")}
    assert not {w for w in writers if w[1] in {"UPDATE", "DELETE"}}


async def test_rls_hides_manifests_of_other_tenant(
    admin_engine: AsyncEngine,
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """跨租户读不到（租户隔离策略）。"""
    entity_id = await _new_manifest(admin_engine, tenant_id=_TENANT)
    async with asset_sm() as session:
        await apply_db_context(session)
        visible = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM asset_service.delivery_manifests "
                    "WHERE entity_id = :e"
                ),
                {"e": entity_id},
            )
        ).scalar_one()
    # conftest 的默认上下文租户是 _TENANT 之外的固定值，因此看不到该行。
    assert visible == 0


async def test_rls_hides_manifests_of_other_project(
    admin_engine: AsyncEngine,
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """同租户跨项目也读不到（`project_scope_*` 限制性策略）。"""
    entity_id = await _new_manifest(
        admin_engine, tenant_id=_TENANT, project_id=_OTHER_PROJECT
    )
    async with asset_sm() as session:
        # 显式声明只允许 _PROJECT 范围。
        await apply_db_context(
            session, tenant_id=_TENANT, project_ids=(_PROJECT,)
        )
        visible = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM asset_service.delivery_manifests "
                    "WHERE entity_id = :e"
                ),
                {"e": entity_id},
            )
        ).scalar_one()
    assert visible == 0
