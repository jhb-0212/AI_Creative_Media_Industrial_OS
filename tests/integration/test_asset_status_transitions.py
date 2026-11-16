"""资产状态转移的结构性验证（数据库约束，ADR-R20）。

审查第三轮第 13 条的事实是：内容列不可变，但 `status` 在触发器白名单里，
于是「状态机」只存在于 Python 的转移表，一句
`UPDATE ... SET status = 'GOLD'` 就能把 GENERATED 直接推到 GOLD。

本文件证明三条结构性质（都不经过服务层，刻意的）：
1. 非法跳转被数据库拒绝——包括以**超级用户**直连，证明拦的是结构不是权限；
2. 已登记边仍然走得通——否则"拦住了"可以靠把整列冻结伪造；
3. 契约外的状态值写不进去（CHECK 约束），转移表本身对应用角色不可写
   （否则"加一条边来自我放行"就是绕过路径）。
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

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a5a5")
_PROJECT = "proj-status-guard"


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    """迁移角色（超级用户）连库：证明触发器/约束对超级用户同样生效。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _new_asset(admin_engine: AsyncEngine, status: str = "GENERATED") -> str:
    """插入一条测试资产（超级用户直插，不受 RLS 与角色授权影响）。"""
    entity_id = f"asset-status-{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                """
                INSERT INTO asset_service.asset_versions (
                    entity_id, entity_type, schema_version, entity_version,
                    status, owner_module, tenant_id, organization_id,
                    created_at, created_by, updated_at, audit_ref,
                    asset_type, sha256, storage_ref, source_asset_version_ids,
                    project_id
                ) VALUES (
                    :eid, 'AssetVersion', 205, 1,
                    :status, 'asset_service', :tid, :tid,
                    now(), 'status-guard', now(), NULL,
                    'IMAGE', repeat('a', 64), '{}'::jsonb, '[]'::jsonb,
                    :pid
                )
                """
            ),
            {"eid": entity_id, "status": status, "tid": _TENANT, "pid": _PROJECT},
        )
    return entity_id


async def test_unregistered_transition_is_rejected(
    admin_engine: AsyncEngine,
) -> None:
    """GENERATED → GOLD（跳过 QC 与人工审核）必须被数据库拒绝。

    这是审查第 13 条的直接复现：此前这条 UPDATE 会成功，状态机形同虚设。
    """
    entity_id = await _new_asset(admin_engine)
    async with admin_engine.begin() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                sa.text(
                    "UPDATE asset_service.asset_versions SET status = 'GOLD' "
                    "WHERE entity_id = :id"
                ),
                {"id": entity_id},
            )
    message = str(exc_info.value)
    assert "非法状态转移" in message
    assert "GENERATED -> GOLD" in message


async def test_registered_transition_is_allowed(admin_engine: AsyncEngine) -> None:
    """已登记边（GENERATED → TECHNICAL_QC）必须走得通。

    反向证明：拒绝来自"边未登记"，而不是"状态列被整列冻结"。
    """
    entity_id = await _new_asset(admin_engine)
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE asset_service.asset_versions "
                "SET status = 'TECHNICAL_QC', entity_version = entity_version + 1 "
                "WHERE entity_id = :id"
            ),
            {"id": entity_id},
        )
        status = (
            await conn.execute(
                sa.text(
                    "SELECT status FROM asset_service.asset_versions "
                    "WHERE entity_id = :id"
                ),
                {"id": entity_id},
            )
        ).scalar_one()
    assert status == "TECHNICAL_QC"


async def test_status_outside_contract_enum_is_rejected(
    admin_engine: AsyncEngine,
) -> None:
    """契约外的状态值写不进去（CHECK 约束）：枚举不是"建议"，是约束。

    走 INSERT 而不是 UPDATE：UPDATE 会先被转移判定的触发器拦下（同样拒绝，
    但证明的是另一件事）。这里要证明的是"从没登记过的取值根本进不了库"。
    """
    async with admin_engine.begin() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                sa.text(
                    """
                    INSERT INTO asset_service.asset_versions (
                        entity_id, entity_type, schema_version, entity_version,
                        status, owner_module, tenant_id, organization_id,
                        created_at, created_by, updated_at, audit_ref,
                        asset_type, sha256, storage_ref, source_asset_version_ids,
                        project_id
                    ) VALUES (
                        :eid, 'AssetVersion', 205, 1,
                        'BANANA', 'asset_service', :tid, :tid,
                        now(), 'status-guard', now(), NULL,
                        'IMAGE', repeat('b', 64), '{}'::jsonb, '[]'::jsonb,
                        :pid
                    )
                    """
                ),
                {
                    "eid": f"asset-bogus-{uuid.uuid4().hex[:8]}",
                    "tid": _TENANT,
                    "pid": _PROJECT,
                },
            )
    assert "ck_asset_versions_status_registered" in str(exc_info.value)


async def test_side_branch_status_cannot_be_entered(
    admin_engine: AsyncEngine,
) -> None:
    """旁支状态（QUARANTINED）在契约里允许存在，但**不可进入**。

    QUARANTINED 是契约枚举的合法值（所以 CHECK 放行），但它没有登记来源边
    （见 `contracts/runtime/state_machine_gaps.yaml`），因此从任何状态都进不去。
    这条用例把「可表示」与「可进入」的差别变成可执行的断言。
    """
    entity_id = await _new_asset(admin_engine)
    async with admin_engine.begin() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                sa.text(
                    "UPDATE asset_service.asset_versions "
                    "SET status = 'QUARANTINED' WHERE entity_id = :id"
                ),
                {"id": entity_id},
            )
    assert "非法状态转移" in str(exc_info.value)


async def test_application_role_cannot_edit_transition_table(
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """业务角色不能改转移表——否则它可以先加一条边，再自我放行。

    这条比"不能改资产状态"更根本：转移表就是状态机的定义。
    """
    with pytest.raises(DBAPIError) as exc_info:
        async with asset_sm() as session:
            await session.execute(
                sa.text(
                    "INSERT INTO asset_service.asset_status_transitions "
                    "(from_status, to_status) VALUES ('GENERATED', 'GOLD')"
                )
            )
    assert "permission denied" in str(exc_info.value)


async def test_transition_table_matches_code_state_machine(
    admin_engine: AsyncEngine,
) -> None:
    """库里的边与代码转移表逐条相等（数据库不是"另一份事实"）。"""
    from services.asset_service.domain.state_machine import (
        ASSET_ALLOWED_TRANSITIONS,
    )

    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.text(
                    "SELECT from_status, to_status "
                    "FROM asset_service.asset_status_transitions"
                )
            )
        ).all()
    in_db = {(str(r[0]), str(r[1])) for r in rows}
    in_code = {
        (src.value, tgt.value)
        for src, targets in ASSET_ALLOWED_TRANSITIONS.items()
        for tgt in targets
    }
    assert in_db == in_code


async def test_asset_role_can_read_transition_table(
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """业务角色必须能读转移表：触发器以 INVOKER 身份执行，读不到就会
    把"非法转移"误报成"权限不足"，排障方向完全错。"""
    async with asset_sm() as session:
        count = (
            await session.execute(
                sa.text("SELECT count(*) FROM asset_service.asset_status_transitions")
            )
        ).scalar_one()
    assert count > 0
