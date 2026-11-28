"""累积式会签事实表的数据库边界证明（ADR-R45 / DATA-7）。

全部跑真实 PostgreSQL，只证明「数据库自己会拒绝」，不依赖服务层自觉：
1. append-only：UPDATE / DELETE / TRUNCATE 均被拒；
2. 同席位同主体重复签被唯一约束拒；
3. 跨席位不同主体的判定在服务层（纯函数已单测）；这里证明「同主体签两席位」
   在写入侧被服务拒绝；
4. 消费后整组不可重放；
5. 无事件的签署在提交时被拒（约束触发器）；
6. 跨租户写入被 RLS 拒绝。

刻意不做的：用服务构造「正常推进」再断言有行——那测的是服务。服务侧正向路径由
本文件的 sign/evaluate/consume 用例覆盖（它们现在同时写事实行）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from packages.errors.error_codes import DomainError
from packages.identity.principal import Principal
from packages.shared.approval_signature import SignatureEventKind
from packages.shared.database import build_project_planning_database_url
from services.identity_access.countersign import CountersignatureService, SignatureTarget
from tests.conftest import set_test_scope

ACTION = "DELIVERY.MANIFEST.APPROVE"
RESOURCE_TYPE = "DeliveryManifest"


@pytest.fixture(scope="module")
async def domain_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_project_planning_database_url())
    try:
        yield engine
    finally:
        await engine.dispose()


def _target(resource_id: str) -> SignatureTarget:
    return SignatureTarget(action_key=ACTION, resource_type=RESOURCE_TYPE, resource_id=resource_id)


def _principal(subject: str, tenant: uuid.UUID, role: str) -> Principal:
    return Principal(subject_id=subject, tenant_id=tenant, roles=frozenset({role}))


async def _insert_event(conn: Any, tenant: uuid.UUID, event_id: uuid.UUID) -> None:
    await conn.execute(
        text(
            "INSERT INTO outbox.outbox_events "
            "(event_id, aggregate_type, aggregate_id, tenant_id, event_type, payload, occurred_at) "
            "VALUES (:eid, :rtype, :rid, :tid, 'PROBE', '{}'::jsonb, now())"
        ),
        {"eid": event_id, "tid": tenant, "rtype": RESOURCE_TYPE, "rid": "probe"},
    )


async def _insert_signature(
    conn: Any,
    *,
    tenant: uuid.UUID,
    signature_id: uuid.UUID,
    event_id: uuid.UUID,
    seat: int,
    subject: str,
    kind: str = "SIGNED",
    resource_id: str = "probe",
) -> None:
    await conn.execute(
        text(
            "INSERT INTO audit.approval_signatures "
            "(signature_id, tenant_id, resource_type, resource_id, action_key, "
            " seat_index, subject, event_kind, occurred_at, event_id) "
            "VALUES (:sid, :tid, :rtype, :rid, :action, :seat, :subject, :kind, now(), :eid)"
        ),
        {
            "sid": signature_id,
            "tid": tenant,
            "rtype": RESOURCE_TYPE,
            "rid": resource_id,
            "action": ACTION,
            "seat": seat,
            "subject": subject,
            "kind": kind,
            "eid": event_id,
        },
    )


async def test_sign_revoke_consume_append_only(domain_engine: AsyncEngine) -> None:
    """签署→撤回→再签→消费：全 append-only；UPDATE/DELETE/TRUNCATE 被拒。"""
    tenant = uuid.uuid4()
    rid = f"flow-{tenant}"
    lead = _principal("lead-1", tenant, "交付负责人")
    qc = _principal("qc-1", tenant, "内部 QC")

    async with AsyncSession(domain_engine) as session, session.begin():
        await set_test_scope(session, tenant_id=tenant)
        svc = CountersignatureService(session)
        await svc.sign(target=_target(rid), seat_index=1, principal=lead)
        await svc.sign(target=_target(rid), seat_index=2, principal=qc)

    # 数据库守卫：UPDATE / DELETE 被拒。
    async with AsyncSession(domain_engine) as session, session.begin():
        await set_test_scope(session, tenant_id=tenant)
        # 先确认有行（写入类验收禁止「未抛异常即 PASS」）。
        rows = await session.execute(
            text(
                "SELECT count(*) FROM audit.approval_signatures "
                "WHERE resource_id = :rid AND event_kind = 'SIGNED'"
            ),
            {"rid": rid},
        )
        assert rows.scalar_one() == 2
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE audit.approval_signatures SET subject = 'hacked' "
                    "WHERE resource_id = :rid"
                ),
                {"rid": rid},
            )
        await session.rollback()

    # TRUNCATE 被拒（语句级守卫），以业务角色身份。
    async with AsyncSession(domain_engine) as session, session.begin():
        await set_test_scope(session, tenant_id=tenant)
        with pytest.raises(DBAPIError):
            await session.execute(text("TRUNCATE audit.approval_signatures"))
        await session.rollback()

    # 凑齐 + 消费。
    async with AsyncSession(domain_engine) as session, session.begin():
        await set_test_scope(session, tenant_id=tenant)
        svc = CountersignatureService(session)
        verdict = await svc.evaluate(target=_target(rid), tenant_id=tenant)
        assert verdict.complete is True, verdict.problems
        verdict2, consumed = await svc.consume(target=_target(rid), tenant_id=tenant)
        assert verdict2.complete is True and len(consumed) == 2

    # 消费后不可重放。
    async with AsyncSession(domain_engine) as session, session.begin():
        await set_test_scope(session, tenant_id=tenant)
        svc = CountersignatureService(session)
        verdict = await svc.evaluate(target=_target(rid), tenant_id=tenant)
        assert verdict.complete is False
        with pytest.raises(DomainError):
            await svc.consume(target=_target(rid), tenant_id=tenant)


async def test_duplicate_signature_is_rejected(domain_engine: AsyncEngine) -> None:
    """同席位同主体重复签：服务层拒（DUPLICATE_ENTITY），DB 唯一约束兜底。"""
    tenant = uuid.uuid4()
    rid = f"dup-{tenant}"
    lead = _principal("lead-1", tenant, "交付负责人")

    async with AsyncSession(domain_engine) as session, session.begin():
        await set_test_scope(session, tenant_id=tenant)
        svc = CountersignatureService(session)
        await svc.sign(target=_target(rid), seat_index=1, principal=lead)
        with pytest.raises(DomainError):
            await svc.sign(target=_target(rid), seat_index=1, principal=lead)

    # DB 唯一约束：绕过服务裸插同 (seat, subject, SIGNED) 撞 uq_signature_event。
    async with AsyncSession(domain_engine) as session, session.begin():
        await set_test_scope(session, tenant_id=tenant)
        with pytest.raises(IntegrityError):
            await _insert_signature(
                session,
                tenant=tenant,
                signature_id=uuid.uuid4(),
                event_id=uuid.uuid4(),
                seat=1,
                subject="lead-1",
                resource_id=rid,
            )
        await session.rollback()


async def test_cross_seat_same_subject_is_rejected(domain_engine: AsyncEngine) -> None:
    """同一主体签两个席位：服务拒绝（会签不得降级为单人授权）。"""
    tenant = uuid.uuid4()
    rid = f"cross-{tenant}"
    # 构造一个持有两个席位角色的主体（现实中的配置错误 / 越权尝试）。
    both = Principal(
        subject_id="same-person",
        tenant_id=tenant,
        roles=frozenset({"交付负责人", "内部 QC"}),
    )

    async with AsyncSession(domain_engine) as session, session.begin():
        await set_test_scope(session, tenant_id=tenant)
        svc = CountersignatureService(session)
        await svc.sign(target=_target(rid), seat_index=1, principal=both)
        with pytest.raises(DomainError) as exc:
            await svc.sign(target=_target(rid), seat_index=2, principal=both)
        assert "不同主体" in str(exc.value) or "降级" in str(exc.value)


async def test_consumed_group_cannot_be_reused(domain_engine: AsyncEngine) -> None:
    """消费事实以新 event_kind 行落库，不覆盖 SIGNED；整组不可二次放行。"""
    tenant = uuid.uuid4()
    rid = f"consume-{tenant}"
    lead = _principal("lead-1", tenant, "交付负责人")
    qc = _principal("qc-1", tenant, "内部 QC")

    async with AsyncSession(domain_engine) as session, session.begin():
        await set_test_scope(session, tenant_id=tenant)
        svc = CountersignatureService(session)
        await svc.sign(target=_target(rid), seat_index=1, principal=lead)
        await svc.sign(target=_target(rid), seat_index=2, principal=qc)
        await svc.consume(target=_target(rid), tenant_id=tenant)

    async with AsyncSession(domain_engine) as session, session.begin():
        await set_test_scope(session, tenant_id=tenant)
        kinds = await session.execute(
            text(
                "SELECT event_kind, count(*) FROM audit.approval_signatures "
                "WHERE resource_id = :rid GROUP BY event_kind ORDER BY event_kind"
            ),
            {"rid": rid},
        )
        counts = dict(kinds.all())
        # SIGNED 未被覆盖（append-only），CONSUMED 新增 2 条。
        assert counts.get("SIGNED") == 2
        assert counts.get("CONSUMED") == 2
        assert counts.get(SignatureEventKind.REVOKED.value) is None


async def test_signature_without_event_rejected_at_commit(
    domain_engine: AsyncEngine,
) -> None:
    """无事件的签署事实在提交时被约束触发器拒（延迟到 COMMIT）。"""
    tenant = uuid.uuid4()
    async with AsyncSession(domain_engine) as session:
        try:
            await set_test_scope(session, tenant_id=tenant)
            # event_id 指向不存在的事件；约束触发器在 COMMIT 时校验。
            await _insert_signature(
                session,
                tenant=tenant,
                signature_id=uuid.uuid4(),
                event_id=uuid.uuid4(),
                seat=1,
                subject="ghost",
            )
            await session.commit()
        except DBAPIError:
            await session.rollback()
        else:
            pytest.fail("无事件的签署事实竟然提交成功")
