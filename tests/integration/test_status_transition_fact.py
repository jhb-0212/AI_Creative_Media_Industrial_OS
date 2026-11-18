"""状态迁移事实表的边界证明（ADR-R23 §D2-a / 不变量 DATA-4、DATA-5）。

这些用例全部跑真实 PostgreSQL，且**只证明数据库自己会拒绝**，不依赖服务层自觉：
1. 事实与事件互相指向，两者在同一事务提交；
2. 无事件的迁移在**提交时**被拒（不是写入时——写入顺序因此不再重要）；
3. append-only：UPDATE / DELETE / **TRUNCATE** 均被拒，后者以**表所有者**身份也拒；
4. 既有 append-only 表（qc_reports）同样挡住 TRUNCATE（同型缺口已补齐）；
5. 同一版本只能有一条事实（并发双写在约束上不可能），no-op 不能冒充迁移；
6. 跨租户写入被 RLS 拒绝；
7. ``audit_ref`` 指针编解码与遗留值三态判定（纯函数，无需数据库）。

刻意不做的：用服务层构造「正常推进」再断言有行——那测的是服务，不是边界。
服务侧的正向路径由各域既有用例覆盖（它们现在同时会写事实行）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from packages.shared.database import (
    build_migration_url,
    build_project_planning_database_url,
)
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.status_transition import (
    AuditRefKind,
    TransitionEvent,
    TransitionFact,
    classify_audit_ref,
    parse_audit_ref,
    transition_audit_ref,
)

_FACT_COLUMNS = (
    "transition_id, tenant_id, resource_type, resource_id, entity_version, "
    "from_status, to_status, owner_action, actor_subject, event_id"
)


@pytest.fixture(scope="module")
async def domain_engine() -> AsyncIterator[AsyncEngine]:
    """业务域角色连接（真实运行时角色，受 RLS 与授权约束）。"""
    engine = create_async_engine(build_project_planning_database_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    """迁移/所有者角色连接（用于证明触发器对所有者同样生效）。"""
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


async def _insert_event(conn: Any, tenant: uuid.UUID, event_id: uuid.UUID) -> None:
    await conn.execute(
        text(
            "INSERT INTO outbox.outbox_events "
            "(event_id, aggregate_type, aggregate_id, tenant_id, event_type, payload, occurred_at) "
            "VALUES (:eid, 'Project', 'p_probe', :tid, 'PROBE', '{}'::jsonb, now())"
        ),
        {"eid": event_id, "tid": tenant},
    )


async def _insert_fact(
    conn: Any,
    *,
    tenant: uuid.UUID,
    transition_id: uuid.UUID,
    event_id: uuid.UUID,
    version: int = 2,
    from_status: str = "DRAFT",
    to_status: str = "ACTIVE",
    resource_id: str = "p_probe",
) -> None:
    await conn.execute(
        text(
            f"INSERT INTO audit.status_transitions ({_FACT_COLUMNS}) "
            "VALUES (:tr, :tid, 'Project', :rid, :ver, :frm, :to, "
            "'PROJECT.STATUS.CHANGE', 'probe', :eid)"
        ),
        {
            "tr": transition_id,
            "tid": tenant,
            "rid": resource_id,
            "ver": version,
            "frm": from_status,
            "to": to_status,
            "eid": event_id,
        },
    )


async def test_transition_row_and_event_mutually_reference(
    domain_engine: AsyncEngine,
) -> None:
    """经真实写入端口落库：事实持 event_id，事件 payload 持 transition_id。

    用 ``SqlAlchemyOutboxWriter`` 而不是裸 SQL——互指是**写入端口**的契约
    （``record_transition`` 一次写两条），裸 SQL 插一条事件当然没有 transition_id。
    """
    tenant = uuid.uuid4()
    transition_id = uuid.uuid4()

    async with AsyncSession(domain_engine) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        event_id = SqlAlchemyOutboxWriter(session).record_transition(
            fact=TransitionFact(
                transition_id=transition_id,
                tenant_id=tenant,
                resource_type="Project",
                resource_id="p_probe_fact",
                entity_version=2,
                from_status="DRAFT",
                to_status="ACTIVE",
                owner_action="PROJECT.STATUS.CHANGE",
                actor_subject="probe",
            ),
            event=TransitionEvent(
                aggregate_type="Project",
                aggregate_id="p_probe_fact",
                tenant_id=tenant,
                event_type="PROJECT_STATUS_CHANGED",
                payload={"project_id": "p_probe_fact"},
            ),
        )

    async with domain_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        fact = (
            await conn.execute(
                text(
                    "SELECT event_id, from_status, to_status, entity_version "
                    "FROM audit.status_transitions WHERE transition_id = :tr"
                ),
                {"tr": transition_id},
            )
        ).one()
        payload = (
            await conn.execute(
                text("SELECT payload FROM outbox.outbox_events WHERE event_id = :eid"),
                {"eid": event_id},
            )
        ).scalar_one()

    assert fact.event_id == event_id
    assert (fact.from_status, fact.to_status) == ("DRAFT", "ACTIVE")
    assert fact.entity_version == 2
    assert payload["transition_id"] == str(transition_id)


async def test_transition_without_event_is_rejected_at_commit(
    domain_engine: AsyncEngine,
) -> None:
    """事件不存在的迁移在 COMMIT 时被拒——写入顺序不再是需要人记的规则。"""
    tenant = uuid.uuid4()
    with pytest.raises(DBAPIError) as excinfo:
        async with domain_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :t, true)"),
                {"t": str(tenant)},
            )
            await _insert_fact(
                conn,
                tenant=tenant,
                transition_id=uuid.uuid4(),
                event_id=uuid.uuid4(),  # 从未写入的事件
                version=3,
            )
    assert "不存在" in str(excinfo.value)


async def test_append_only_blocks_update_and_delete(owner_engine: AsyncEngine) -> None:
    """行级守卫：UPDATE / DELETE 被触发器拒绝。

    用**所有者**身份验证，因为业务角色连 UPDATE 权限都没有（REVOKE 已收口），
    那样测到的是「没权限」而不是「触发器生效」——两者都要有，且要分别证明。
    """
    tenant = uuid.uuid4()
    event_id = uuid.uuid4()
    transition_id = uuid.uuid4()

    async with owner_engine.begin() as conn:
        await _insert_event(conn, tenant, event_id)
        await _insert_fact(
            conn,
            tenant=tenant,
            transition_id=transition_id,
            event_id=event_id,
            version=10,
            resource_id="p_probe_mut",
        )

    for statement in (
        "UPDATE audit.status_transitions SET actor_subject = 'x' WHERE transition_id = :tr",
        "DELETE FROM audit.status_transitions WHERE transition_id = :tr",
    ):
        with pytest.raises(DBAPIError) as excinfo:
            async with owner_engine.begin() as conn:
                await conn.execute(text(statement), {"tr": transition_id})
        assert "append-only" in str(excinfo.value)


async def test_domain_role_has_no_mutation_privilege(
    domain_engine: AsyncEngine,
) -> None:
    """业务角色连 UPDATE/DELETE 权限都没有：守卫之外还有一层授权收口。"""
    tenant = uuid.uuid4()
    with pytest.raises(DBAPIError) as excinfo:
        async with domain_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :t, true)"),
                {"t": str(tenant)},
            )
            await conn.execute(text("DELETE FROM audit.status_transitions"))
    assert "permission denied" in str(excinfo.value)


async def test_owner_cannot_truncate_fact_table(owner_engine: AsyncEngine) -> None:
    """语句级守卫：连表所有者也不能 TRUNCATE——TRUNCATE 不触发行级触发器。"""
    with pytest.raises(DBAPIError) as excinfo:
        async with owner_engine.begin() as conn:
            await conn.execute(text("TRUNCATE audit.status_transitions"))
    assert "append-only" in str(excinfo.value)
    assert "TRUNCATE" in str(excinfo.value)


async def test_owner_cannot_truncate_existing_append_only_tables(
    owner_engine: AsyncEngine,
) -> None:
    """既有 append-only 表的同型缺口：qc_reports 也必须挡住 TRUNCATE。"""
    with pytest.raises(DBAPIError) as excinfo:
        async with owner_engine.begin() as conn:
            await conn.execute(text("TRUNCATE asset_service.qc_reports"))
    assert "append-only" in str(excinfo.value)


async def test_duplicate_entity_version_is_rejected(domain_engine: AsyncEngine) -> None:
    """同一 (tenant, type, id, version) 只能有一条事实：并发双写结构上不可能。"""
    tenant = uuid.uuid4()
    event_id = uuid.uuid4()

    async with domain_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        await _insert_event(conn, tenant, event_id)
        await _insert_fact(
            conn,
            tenant=tenant,
            transition_id=uuid.uuid4(),
            event_id=event_id,
            version=21,
            resource_id="p_probe_dup",
        )

    with pytest.raises(IntegrityError):
        async with domain_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :t, true)"),
                {"t": str(tenant)},
            )
            await _insert_fact(
                conn,
                tenant=tenant,
                transition_id=uuid.uuid4(),
                event_id=event_id,
                version=21,
                resource_id="p_probe_dup",
            )


async def test_noop_transition_is_rejected(domain_engine: AsyncEngine) -> None:
    """from_status == to_status 不是迁移：CHECK 约束直接拒绝（不给 no-op 行留退路）。"""
    tenant = uuid.uuid4()
    event_id = uuid.uuid4()

    with pytest.raises(IntegrityError):
        async with domain_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :t, true)"),
                {"t": str(tenant)},
            )
            await _insert_event(conn, tenant, event_id)
            await _insert_fact(
                conn,
                tenant=tenant,
                transition_id=uuid.uuid4(),
                event_id=event_id,
                version=31,
                from_status="ACTIVE",
                to_status="ACTIVE",
                resource_id="p_probe_noop",
            )


async def test_cross_tenant_insert_rejected_by_rls(domain_engine: AsyncEngine) -> None:
    """租户维度 RLS：上下文是 A 租户时，写 B 租户的事实被拒。"""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    with pytest.raises(DBAPIError) as excinfo:
        async with domain_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :t, true)"),
                {"t": str(tenant_a)},
            )
            await _insert_fact(
                conn,
                tenant=tenant_b,
                transition_id=uuid.uuid4(),
                event_id=uuid.uuid4(),
                version=41,
                resource_id="p_probe_rls",
            )
    assert "row-level security" in str(excinfo.value)


def test_audit_ref_pointer_roundtrip_and_legacy_classification() -> None:
    """指针可编解码；迁移前的自由串被判为 legacy，而不是解析失败。"""
    transition_id = uuid.uuid4()
    pointer = transition_audit_ref(transition_id)

    assert pointer.startswith("tr:")
    assert classify_audit_ref(pointer) is AuditRefKind.POINTER
    assert parse_audit_ref(pointer) == transition_id

    legacy = "ASSET.STATUS.CHANGE:director_1"
    assert classify_audit_ref(legacy) is AuditRefKind.LEGACY
    assert parse_audit_ref(legacy) is None  # 不解析，也不判错

    assert classify_audit_ref(None) is AuditRefKind.ABSENT
    assert classify_audit_ref("   ") is AuditRefKind.ABSENT

    broken = "tr:not-a-uuid"
    assert classify_audit_ref(broken) is AuditRefKind.POINTER
    assert parse_audit_ref(broken) is None  # 前缀对、内容坏：这才是真异常


async def test_event_link_must_be_same_tenant(
    domain_engine: AsyncEngine,
    owner_engine: AsyncEngine,
) -> None:
    """事实指向的事件必须同租户：SECURITY DEFINER 校验不能只看 event_id。

    评审 T2：校验函数以 ``SECURITY DEFINER`` 运行（要读调用方不可见的 outbox），
    于是它**绕过了调用方的 RLS**。只比 ``event_id`` 时，租户 A 的事实行可以指向
    租户 B 的事件——两条记录都在、提交也成功，但两个租户的审计链被缝在一起。
    函数里每一条谓词都要按「我现在没有任何 RLS 保护」来写。
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    foreign_event = uuid.uuid4()

    # 租户 B 的事件（所有者身份写入，绕过 RLS）。
    async with owner_engine.begin() as conn:
        await _insert_event(conn, tenant_b, foreign_event)

    with pytest.raises(DBAPIError) as excinfo:
        async with domain_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :t, true)"),
                {"t": str(tenant_a)},
            )
            await _insert_fact(
                conn,
                tenant=tenant_a,
                transition_id=uuid.uuid4(),
                event_id=foreign_event,
                version=51,
                resource_id="p_probe_xtenant",
            )
    assert "不存在于租户" in str(excinfo.value)


async def test_non_positive_entity_version_is_rejected(
    domain_engine: AsyncEngine,
) -> None:
    """版本号 0 描述不了任何实体版本：CHECK 拒绝，NOT NULL 挡不住它。

    评审 T4：对象 Schema 对 entity_version 的下界是 1，而表上只有 NOT NULL。
    有 INSERT 权限的调用方可以写 0 或负数，造出一条「不可能是真迁移」的事实。
    """
    tenant = uuid.uuid4()
    event_id = uuid.uuid4()

    with pytest.raises(IntegrityError) as excinfo:
        async with domain_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :t, true)"),
                {"t": str(tenant)},
            )
            await _insert_event(conn, tenant, event_id)
            await _insert_fact(
                conn,
                tenant=tenant,
                transition_id=uuid.uuid4(),
                event_id=event_id,
                version=0,
                resource_id="p_probe_zero",
            )
    assert "ck_status_transition_version_positive" in str(excinfo.value)
