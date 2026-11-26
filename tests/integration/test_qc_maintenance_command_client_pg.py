"""维护命令调用适配层（拒绝补记协议）的真库验收。

数据库只能**拒绝**，不能让「拒绝」和「拒绝账」同事务落库：同一语句里先
INSERT 再 RAISE 会整条回滚。因此补记是一条有失败窗口的协议，本文件要证明：

1. 成功路径不产生 DENIED 行；
2. 被拒时**先回滚、再另开连接补记**，且补记的是可信主体（session_user）；
3. 同一 attempt_id 重试幂等，不会把一次拒绝记成两行；
4. 补记本身失败时**告警**且 ``denial_recorded=False``——不假装账已经平了。
"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.shared.database import (
    build_migration_url,
    create_qc_maintenance_engine,
)
from packages.shared.db_roles import maintenance_role
from services.qc_governance.maintenance.command_client import MaintenanceCommandClient
from tests.integration.maintenance_seed import create_work_order

_LOGIN = maintenance_role("login")
_OPERATOR = maintenance_role("operator")


@pytest.fixture()
async def client(caplog):
    engine = create_qc_maintenance_engine()
    logger = logging.getLogger("tests.qc_maintenance.command_client")
    caplog.set_level(logging.ERROR, logger=logger.name)
    try:
        yield MaintenanceCommandClient(engine, logger=logger)
    finally:
        await engine.dispose()


async def test_success_path_records_no_denial(client, admin_sessionmaker) -> None:
    async with admin_sessionmaker() as session:
        wo = await create_work_order(session, tenant=uuid.uuid4())

    result = await client.reconcile(wo, attempt_id=f"ok-{uuid.uuid4().hex}")
    assert result.denial is None
    assert result.denial_recorded is True
    assert result.rows, "对账应返回分类计数"

    async with admin_sessionmaker() as session:
        denied = await session.scalar(
            text(
                "SELECT count(*) FROM qc_maintenance.maintenance_action_log "
                "WHERE work_order_id = :id AND outcome = 'DENIED'"
            ),
            {"id": wo},
        )
    assert denied == 0


async def test_denial_is_rolled_back_then_recorded_by_adapter(
    client, admin_sessionmaker
) -> None:
    """命令失败 → 事务回滚 → 适配层用**独立**连接补记一行 DENIED。"""
    async with admin_sessionmaker() as session:
        wo = await create_work_order(session, tenant=uuid.uuid4(), approve=False)
        other = await create_work_order(
            session, tenant=uuid.uuid4(), kind="DETECT", approve=True
        )

    attempt = f"deny-{uuid.uuid4().hex}"
    pending = await client.reconcile(wo, attempt_id=attempt)
    assert pending.denial == "not_approved"
    assert pending.denial_recorded is True
    assert pending.rows == []

    wrong_kind = await client.reconcile(other, attempt_id=f"deny-{uuid.uuid4().hex}")
    assert wrong_kind.denial == "kind_mismatch"

    # 不存在工单：命令在解析租户前就被拒，补记时 tenant 为空但仍要落账。
    missing = await client.reconcile(
        f"no-such-{uuid.uuid4().hex}", attempt_id=f"deny-{uuid.uuid4().hex}"
    )
    assert missing.denial == "work_order_not_found"
    assert missing.denial_recorded is True

    async with admin_sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT attempt_id, detail->>'reason' AS reason, "
                    "detail->>'actor' AS actor "
                    "FROM qc_maintenance.maintenance_action_log "
                    "WHERE work_order_id = :id ORDER BY id"
                ),
                {"id": wo},
            )
        ).all()
    assert [row.reason for row in rows] == ["not_approved"]
    assert rows[0].attempt_id == attempt
    assert rows[0].actor == _LOGIN, "补记主体必须来自 session_user"


async def test_retry_with_same_attempt_is_idempotent(client, admin_sessionmaker) -> None:
    async with admin_sessionmaker() as session:
        wo = await create_work_order(session, tenant=uuid.uuid4(), approve=False)

    attempt = f"retry-{uuid.uuid4().hex}"
    first = await client.reconcile(wo, attempt_id=attempt)
    second = await client.reconcile(wo, attempt_id=attempt)
    assert first.denial == second.denial == "not_approved"
    assert first.denial_recorded and second.denial_recorded

    async with admin_sessionmaker() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM qc_maintenance.maintenance_action_log "
                "WHERE attempt_id = :attempt"
            ),
            {"attempt": attempt},
        )
    assert count == 1, "重试不得把一次拒绝记成多次"


async def test_record_failure_alarms_instead_of_silence(
    client, admin_sessionmaker, caplog
) -> None:
    """补记本身失败：返回 ``denial_recorded=False`` 并告警，不允许静默。

    真实失败是「login 没有补记函数的 EXECUTE」——入口被权限拒时，被拒的那个
    函数根本没被调用，它自己写不了审计，只能靠适配层补记；补记再失败，这条
    拒绝事实就丢了，必须留下告警而不是吞掉。
    """
    async with admin_sessionmaker() as session:
        wo = await create_work_order(session, tenant=uuid.uuid4(), approve=False)

    admin = create_async_engine(build_migration_url())
    try:
        # login 通过 operator 成员继承 EXECUTE：只撤 login 的直接授权挡不住它。
        for role in (_LOGIN, _OPERATOR):
            async with admin.begin() as conn:
                await conn.execute(
                    text(
                        "REVOKE EXECUTE ON FUNCTION "
                        "qc_maintenance.record_maintenance_denial(text, text, text, text, text) "
                        f"FROM {role}"
                    )
                )
        result = await client.reconcile(wo, attempt_id=f"alarm-{uuid.uuid4().hex}")
        assert result.denial == "not_approved", "原拒绝原因不能被补记失败掩盖"
        assert result.denial_recorded is False
        assert any(
            "补记失败" in record.message for record in caplog.records
        ), "补记失败必须有告警，否则丢的是无声的审计缺口"
    finally:
        for role in (_LOGIN, _OPERATOR):
            async with admin.begin() as conn:
                await conn.execute(
                    text(
                        "GRANT EXECUTE ON FUNCTION "
                        "qc_maintenance.record_maintenance_denial(text, text, text, text, text) "
                        f"TO {role}"
                    )
                )
        await admin.dispose()


async def test_enumerate_denial_without_work_order_is_alarmed(
    client, caplog
) -> None:
    """发现/检测没有工单维度：拒绝无法挂账时必须告警，而不是当作已记录。"""
    admin = create_async_engine(build_migration_url())
    try:
        for role in (_LOGIN, _OPERATOR):
            async with admin.begin() as conn:
                await conn.execute(
                    text(
                        "REVOKE EXECUTE ON FUNCTION "
                        "qc_maintenance.enumerate_due_tasks() "
                        f"FROM {role}"
                    )
                )
        result = await client.enumerate_due_tasks(attempt_id=f"enum-{uuid.uuid4().hex}")
        assert result.denial == "permission_denied"
        assert result.denial_recorded is False
        assert any("没有工单可挂账" in record.message for record in caplog.records)
    finally:
        for role in (_LOGIN, _OPERATOR):
            async with admin.begin() as conn:
                await conn.execute(
                    text(
                        "GRANT EXECUTE ON FUNCTION "
                        "qc_maintenance.enumerate_due_tasks() "
                        f"TO {role}"
                    )
                )
        await admin.dispose()
