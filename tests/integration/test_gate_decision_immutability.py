"""Gate 裁决的结构性约束（绕过服务层，直接对数据库动手）。

领域层能拒绝非法构造，但"拒绝"若只活在 Python 里，一次手工补数据、一次迁移
脚本、一次其它服务的直连写入就能留下不可信的裁决。本文件证明五条性质：

1. UPDATE / DELETE 被触发器拒绝（**超级用户直连也一样**）；
2. Agent 决策人、无证据的 APPROVED、契约外取值被 CHECK 拒绝；
3. 幂等唯一索引让"同一输入同一结论"并发下也只能落一条；
4. 应用角色只有 SELECT/INSERT（系统目录可查，不是靠业务路径推测）；
5. RLS 双策略：跨租户与同租户跨项目都读不到。
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

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d4c1")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d4c2")
_PROJECT = "proj-gate-guard"
_OTHER_PROJECT = "proj-gate-other"

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    """迁移角色（超级用户）连库：证明触发器/CHECK 对超级用户同样生效。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _insert(
    admin_engine: AsyncEngine,
    *,
    decision: str = "APPROVED",
    subject_type: str = "human",
    evidence: str = '["evidence-1"]',
    tenant_id: uuid.UUID = _TENANT,
    project_id: str = _PROJECT,
    instance: str | None = None,
    entity_id: str | None = None,
) -> str:
    """直插一条裁决（超级用户，不受 RLS 与角色授权影响）。"""
    entity_id = entity_id or f"gate-guard-{uuid.uuid4().hex[:12]}"
    instance = instance or f"instance-{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                """
                INSERT INTO qc_governance.gate_decisions (
                    entity_id, entity_type, schema_version, entity_version,
                    status, owner_module, tenant_id, organization_id,
                    created_at, created_by, updated_at, audit_ref,
                    project_id, gate_key, gate_instance_id, decision,
                    constraint_snapshot_id, ruleset_version, entry_criteria,
                    required_artifacts, reviewers, reason_code, comment,
                    evidence_refs, scope, decided_by, decided_by_subject_type,
                    decided_at
                ) VALUES (
                    :eid, 'GateDecision', 205, 1,
                    'RECORDED', 'qc_governance', :tid, :tid,
                    now(), 'gate-guard', now(), NULL,
                    :pid, 'GATE_4', :instance, :decision,
                    'snapshot-1', 3, 'entry', 'artifacts',
                    '["gate-guard"]'::jsonb, 'PASS', 'comment',
                    CAST(:evidence AS jsonb), '{"level":"project"}'::jsonb,
                    'gate-guard', :subject_type, now()
                )
                """
            ),
            {
                "eid": entity_id,
                "tid": tenant_id,
                "pid": project_id,
                "instance": instance,
                "decision": decision,
                "evidence": evidence,
                "subject_type": subject_type,
            },
        )
    return entity_id


async def test_approved_decision_can_be_inserted(admin_engine: AsyncEngine) -> None:
    """基线：合法裁决可插入（后续"拒绝"用例才有意义）。"""
    entity_id = await _insert(admin_engine)
    async with admin_engine.connect() as conn:
        value = (
            await conn.execute(
                sa.text(
                    "SELECT decision FROM qc_governance.gate_decisions "
                    "WHERE entity_id = :e"
                ),
                {"e": entity_id},
            )
        ).scalar_one()
    assert value == "APPROVED"


@pytest.mark.parametrize("action", [
    "UPDATE qc_governance.gate_decisions SET reason_code = 'CHANGED' WHERE entity_id = :e",
    "DELETE FROM qc_governance.gate_decisions WHERE entity_id = :e",
])
async def test_update_and_delete_rejected_even_for_superuser(
    admin_engine: AsyncEngine, action: str
) -> None:
    """裁决不可改写：超级用户直连也拦，证明拦的是结构而不是角色。"""
    entity_id = await _insert(admin_engine)
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(sa.text(action), {"e": entity_id})
    assert "append-only" in str(exc_info.value)


async def test_agent_decider_is_rejected_by_check(admin_engine: AsyncEngine) -> None:
    """Agent 不得自批自用：绕过所有应用代码仍被数据库拒绝。"""
    with pytest.raises(DBAPIError) as exc_info:
        await _insert(admin_engine, subject_type="agent")
    assert "no_agent_decider" in str(exc_info.value)


async def test_approved_without_evidence_is_rejected_by_check(
    admin_engine: AsyncEngine,
) -> None:
    """缺证据不得 PASS：数据库 CHECK 与领域校验双份。"""
    with pytest.raises(DBAPIError) as exc_info:
        await _insert(admin_engine, evidence="[]")
    assert "needs_evidence" in str(exc_info.value)


async def test_rejected_decision_without_evidence_is_allowed(
    admin_engine: AsyncEngine,
) -> None:
    """否决不强制证据（理由由 reason_code/comment 承载），否则 CHECK 过严。"""
    entity_id = await _insert(admin_engine, decision="REJECTED", evidence="[]")
    assert entity_id


async def test_decision_outside_contract_is_rejected(admin_engine: AsyncEngine) -> None:
    """契约外取值被 CHECK 拒绝（可表示 ≠ 可进库）。"""
    with pytest.raises(DBAPIError) as exc_info:
        await _insert(admin_engine, decision="BANANA")
    assert "decision_registered" in str(exc_info.value)


@pytest.mark.parametrize("decision", ["APPROVED", "REJECTED", "RETURNED", "OVERRIDDEN"])
async def test_all_contract_values_are_representable(
    admin_engine: AsyncEngine, decision: str
) -> None:
    """契约四值全部可入库：领域层拒绝 OVERRIDDEN 不等于数据库把它当非法值。"""
    entity_id = await _insert(admin_engine, decision=decision)
    assert entity_id


async def test_idempotency_unique_index_rejects_duplicate(
    admin_engine: AsyncEngine,
) -> None:
    """同实例+同快照+同版本+同结论只能一条；并发由约束兜底。"""
    instance = f"instance-{uuid.uuid4().hex[:12]}"
    await _insert(admin_engine, instance=instance)
    with pytest.raises(DBAPIError) as exc_info:
        await _insert(admin_engine, instance=instance)
    assert "idempotency" in str(exc_info.value)


async def test_only_qc_role_has_insert_and_no_one_has_update(
    admin_engine: AsyncEngine,
) -> None:
    """系统目录断言：授权是数据库状态，测业务路径证明不了别的路径走不通。"""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.text(
                    """
                    SELECT grantee, privilege_type
                    FROM information_schema.table_privileges
                    WHERE table_schema = 'qc_governance'
                      AND table_name = 'gate_decisions'
                      AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
                    """
                )
            )
        ).all()
    qc_role = role_for(DatabaseDomain.QC_GOVERNANCE)
    business_roles = set(all_business_roles())
    # postgres 是表 owner，天然有全部权限；把它算进来会永远失败并掩盖真问题。
    writers = {
        (row.grantee, row.privilege_type)
        for row in rows
        if row.grantee in business_roles
    }
    assert {w for w in writers if w[1] == "INSERT"} == {(qc_role, "INSERT")}
    assert not [
        w for w in writers if w[1] in {"UPDATE", "DELETE"} and w[0] in business_roles
    ]


async def test_rls_blocks_other_tenant_and_other_project(
    admin_engine: AsyncEngine,
    qc_sm: async_sessionmaker[AsyncSession],
) -> None:
    """租户 + 项目限制性策略：跨租户、同租户跨项目都读不到。

    必须用**应用角色**会话：超级用户默认不受 RLS 约束，用它测只会得到假绿。
    """
    entity_id = await _insert(admin_engine)

    async def _count(
        *, tenant_id: uuid.UUID, projects: tuple[str, ...]
    ) -> int:
        async with qc_sm() as session:
            await apply_db_context(session, tenant_id=tenant_id, project_ids=projects)
            return int(
                (
                    await session.execute(
                        sa.text(
                            "SELECT count(*) FROM qc_governance.gate_decisions "
                            "WHERE entity_id = :e"
                        ),
                        {"e": entity_id},
                    )
                ).scalar_one()
            )

    visible = await _count(tenant_id=_TENANT, projects=(_PROJECT,))
    other_tenant = await _count(tenant_id=_OTHER_TENANT, projects=(_PROJECT,))
    other_project = await _count(tenant_id=_TENANT, projects=(_OTHER_PROJECT,))
    assert (visible, other_tenant, other_project) == (1, 0, 0)
