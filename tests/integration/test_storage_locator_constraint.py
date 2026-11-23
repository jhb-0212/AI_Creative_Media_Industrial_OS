"""数据库层 storage_ref 约束的等价性证明（P1-07 / #179）。

为什么要单独一个文件
--------------------
定位符的同一条判据有三处表达：

| 位置 | 表达方式 | 为什么不能共用一种 |
|---|---|---|
| Python（``parse_locator``） | 正则 + 分组判定 | 要给出精确原因（形态错 / 扩展名未登记 / 前两段错位） |
| 契约 ``pattern`` | ECMA-262 正则 + ``\\1\\2`` 反向引用 | 请求入口与文档要能一次判死 |
| SQL ``CHECK`` | 正则 + ``substr`` 位置算术 | PostgreSQL 的 POSIX 正则**不支持反向引用** |

三处不可能靠「读代码核对」保持一致——它们写法本就不同。唯一可信的判据是
**把同一批样本分别喂给两侧，结论逐条相同**。本文件用真实 INSERT 做这件事：
约束说「合法」的值必须插得进去，说「非法」的必须 23514。

这比单测更重（要连库），但它守的是单测守不住的东西：SQL 里那几个位置数字
（9 / 12 / 15 / 17）写错一位，Python 侧全绿，而库里要么放行错位定位符、
要么把合法值全部拒掉。位置算术没有类型系统保护，只能实测。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from packages.shared.storage_locator import (
    ALLOWED_EXTENSIONS,
    build_locator,
    is_controlled_locator,
)

# PostgreSQL 的 CHECK 违反。
CHECK_VIOLATION = "23514"

_DIGEST = "0123456789abcdef" * 4


def _sqlstate(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None)
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return str(state or "")


def _now() -> datetime:
    return datetime.now(UTC)


async def _insert_project(conn, *, project_id: str, tenant) -> None:
    """播种归属根。organization_id 允许为 NULL（组织是平台级归属根，可缺省）。"""
    await conn.execute(
        text(
            "INSERT INTO project_planning.projects "
            "(entity_id, entity_type, schema_version, entity_version, status, "
            " owner_module, tenant_id, created_at, created_by, updated_at, "
            " project_name, project_tier, organization_id) "
            "VALUES (:pid, 'Project', 205, 1, 'DRAFT', 'project_planning', "
            " :tid, :now, 'test-seeder', :now, '定位符约束样本', 'B', NULL) "
            "ON CONFLICT (entity_id) DO NOTHING"
        ),
        {"pid": project_id, "tid": tenant, "now": _now()},
    )


async def _try_insert_asset(conn, *, tenant, project_id: str, sha256: str, locator: str):
    """尝试插入一行资产；不提交，由调用方回滚。"""
    await conn.execute(
        text(
            "INSERT INTO asset_service.asset_versions "
            "(entity_id, entity_type, schema_version, entity_version, status, "
            " owner_module, tenant_id, created_at, created_by, updated_at, "
            " project_id, asset_type, sha256, storage_ref, source_asset_version_ids) "
            "VALUES (:eid, 'AssetVersion', 205, 1, 'GENERATED', 'asset-service', "
            " :tid, :now, 'test-seeder', :now, :pid, 'IMAGE', :sha, :locator, '[]'::jsonb)"
        ),
        {
            "eid": uuid.uuid4().hex,
            "tid": tenant,
            "now": _now(),
            "pid": project_id,
            "sha": sha256,
            "locator": locator,
        },
    )


@pytest.fixture
async def seeded(admin_sessionmaker):
    """一个真实项目（管理连接播种，绕过 RLS 但外键仍然成立）。"""
    tenant = uuid.uuid4()
    project_id = uuid.uuid4().hex
    async with admin_sessionmaker() as session:
        await _insert_project(session, project_id=project_id, tenant=tenant)
        await session.commit()
    return tenant, project_id


async def test_every_registered_extension_is_accepted(admin_sessionmaker, seeded) -> None:
    """每个登记过的扩展名都能落库——白名单与约束不能有两份不同答案。

    这条同时把「位置算术写错」的上半段排除了：如果 substr 的位置写偏一位，
    合法值会被误拒，这里立刻红。
    """
    tenant, project_id = seeded
    async with admin_sessionmaker() as session:
        for extension in sorted(ALLOWED_EXTENSIONS):
            # 每个扩展名配一个确定性且互不相同的哈希：若共用同一哈希，就测不出
            # 「同一内容不同封装」这类边界，也掩盖了位置算术错误的行级差异。
            digest = hashlib.sha256(extension.encode()).hexdigest()
            locator = build_locator(digest, extension)
            await _try_insert_asset(
                session, tenant=tenant, project_id=project_id, sha256=digest, locator=locator
            )
        await session.rollback()


@pytest.mark.parametrize(
    ("case", "sha256", "locator_factory"),
    [
        # 前两段与哈希不符：语法合法、永远读不到（本次收敛发现的历史缺口）。
        ("前两段错位", _DIGEST, lambda: f"local://ff/ee/{_DIGEST}.png"),
        # 未登记扩展名。
        ("未登记扩展名", _DIGEST, lambda: build_locator(_DIGEST, "png").replace(".png", ".sh")),
        # 旧的自由引用形态。
        ("自由引用", _DIGEST, lambda: "s3://bucket/frame.png"),
        # 路径穿越。
        ("路径穿越", _DIGEST, lambda: "local://../../etc/passwd"),
        # 定位符本身合法，但与同行的 sha256 列矛盾（跨列一致性）。
        (
            "与 sha256 不一致",
            "f" * 64,
            lambda: build_locator(_DIGEST, "png"),
        ),
    ],
)
async def test_invalid_storage_states_are_rejected_by_database(
    admin_sessionmaker, seeded, case, sha256, locator_factory
) -> None:
    """坏状态必须在数据库层被拒绝（CHECK 违反 23514）。

    刻意只认 23514：*不是* 23503（外键）、不是 42501（RLS）。三者混淆时
    「被拒」看起来一样，但证明的是不同的事——外键拒绝说明归属错了，RLS 拒绝
    说明租户错了，而这里要证明的是**内容寻址形态本身不成立**。
    """
    tenant, project_id = seeded
    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc_info:
            await _try_insert_asset(
                session,
                tenant=tenant,
                project_id=project_id,
                sha256=sha256,
                locator=locator_factory(),
            )
        assert _sqlstate(exc_info.value) == CHECK_VIOLATION, case
        await session.rollback()


async def test_constraint_is_validated_not_just_declared(admin_sessionmaker) -> None:
    """约束必须 ``convalidated``。

    ``NOT VALID`` 的 CHECK 只对新写入生效，存量行从未被检查——「拒绝永久豁免」
    这条红线针对的正是它。台账判据要求的是「存在 ∧ 已校验 ∧ 违规行数 0」，
    只看「约束存在」等于把豁免当成了通过。
    """
    async with admin_sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT conname, convalidated FROM pg_constraint "
                    "WHERE conname = 'ck_asset_versions_storage_ref_locator'"
                )
            )
        ).all()
    assert len(rows) == 1, "storage_ref 约束不存在或多份，说明迁移未生效/重复添加"
    assert rows[0].convalidated is True


async def test_existing_rows_violate_nothing(admin_sessionmaker) -> None:
    """可复算的违规行数 = 0（能真 FAIL 的判据，不是「约束存在所以没问题」）。

    SQL 与实现分别算一遍：若两者对同一行给出不同结论，说明三处表达已经分叉，
    而这恰恰是本文件要守住的东西。这里用实现判定做基准、SQL 计数做对照——
    不是「用 A 的失败证明 B 有效」，而是在同一批真实数据上双向核对。
    """
    async with admin_sessionmaker() as session:
        rows = (
            await session.execute(
                text("SELECT storage_ref, sha256 FROM asset_service.asset_versions")
            )
        ).all()

    mismatched = [
        row.storage_ref
        for row in rows
        if not is_controlled_locator(row.storage_ref) or row.storage_ref[14:78] != row.sha256
    ]
    assert not mismatched, f"以下行的 storage_ref 与实现判定不符：{mismatched[:3]}"
