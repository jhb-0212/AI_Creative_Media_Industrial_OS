"""Gate 决策 API 端到端测试（真实 PostgreSQL + 真实 JWT 鉴权 + 真实域角色）。

验证 `PRODUCTION.GATE.APPROVE` 的执行闭环，以及四条结构性承诺：

1. **只有绑定后的授权主体能批**：冻结契约写的是描述性短语，运行时台账解析为
   「导演」；制片/Agent/Worker 都进不来（负向用例 ACT-GATE-NEG-001）。
2. **身份与时间由服务端固定**：请求体里塞 `decided_by` / `decided_at` / `status`
   不生效；决策人来自令牌，时间来自服务端。
3. **缺证据的 APPROVED 被拒**：领域校验与数据库 CHECK 双份。
4. **裁决写入与审计事件同事务**：不落库就不发事件，重放不重复发。
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from tests.conftest import apply_db_context, make_test_token

_PROJECT = "proj_gate_api"
_OTHER_PROJECT = "proj_gate_other"
_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d5")


def _client(
    subject: str,
    roles: list[str],
    *,
    subject_type: str = "human",
    project_ids: list[str] | None = None,
) -> AsyncClient:
    token = make_test_token(
        subject_id=subject,
        roles=roles,
        tenant_id=_TENANT,
        subject_type=subject_type,
        project_ids=project_ids,
    )
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.fixture
async def director_client() -> AsyncClient:
    """运行时主体解析台账把 Gate 审批权绑定给「导演」。"""
    async with _client("gate_director", ["导演"]) as c:
        yield c


@pytest.fixture
async def producer_client() -> AsyncClient:
    async with _client("gate_producer", ["制片"]) as c:
        yield c


@pytest.fixture
async def agent_client() -> AsyncClient:
    async with _client(
        "gate_agent", ["导演"], subject_type="agent"
    ) as c:
        yield c


def _body(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "entity_id": f"gate-{uuid.uuid4().hex[:12]}",
        "project_id": _PROJECT,
        "gate_key": "GATE_4",
        "gate_instance_id": f"instance-{uuid.uuid4().hex[:12]}",
        "decision": "APPROVED",
        "constraint_snapshot_id": "snapshot-1",
        "ruleset_version": 3,
        "entry_criteria": "终检报告齐备",
        "required_artifacts": "PictureLock、版权证据、终检报告",
        "reviewers": ["gate_director"],
        "reason_code": "ALL_CHECKS_PASSED",
        "comment": "全部检查通过",
        "evidence_refs": ["qc-final-1", "rights-1"],
        "scope": {"level": "project", "shot": "SH001"},
    }
    values.update(overrides)
    return values


async def test_director_can_record_gate_decision(
    director_client: AsyncClient,
) -> None:
    response = await director_client.post("/gate-decisions", json=_body())
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["entity_type"] == "GateDecision"
    assert payload["decision"] == "APPROVED"
    assert payload["decided_by"] == "gate_director"
    assert payload["status"] == "RECORDED"
    assert payload["schema_version"] == 205
    assert "tenant_id" not in payload
    assert "decided_by_subject_type" not in payload


async def test_unbound_role_is_denied(producer_client: AsyncClient) -> None:
    """冻结契约的描述性主体只解析出「导演」，制片不得越权（fail-closed）。"""
    response = await producer_client.post("/gate-decisions", json=_body())
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_agent_cannot_approve_even_with_director_role(
    agent_client: AsyncClient,
) -> None:
    """ACT-GATE-NEG-001：Agent/非授权人审批拒绝。"""
    body = _body(reviewers=["gate_agent"])
    response = await agent_client.post("/gate-decisions", json=body)
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "AUTH_SCOPE_DENIED"


@pytest.mark.parametrize("role", ["QC Worker", "Worker", "交付服务"])
async def test_other_roles_are_denied(role: str) -> None:
    async with _client(f"gate_other_{role}", [role]) as client:
        response = await client.post("/gate-decisions", json=_body())
    assert response.status_code == 403, response.text


async def test_cross_project_scope_is_denied() -> None:
    """令牌只允许 proj_gate_other，不能批 proj_gate_api。"""
    async with _client(
        "gate_narrow", ["导演"], project_ids=[_OTHER_PROJECT]
    ) as client:
        response = await client.post("/gate-decisions", json=_body())
    assert response.status_code == 403, response.text


async def test_identity_and_time_cannot_be_forged_in_body(
    director_client: AsyncClient,
) -> None:
    """请求体塞入身份/时间/状态字段也不影响落库结果。"""
    forged_id = f"gate-forged-{uuid.uuid4().hex[:12]}"
    body = _body(
        decided_by="someone-else",
        decided_at="1999-01-01T00:00:00Z",
        status="VALIDATED",
        entity_id=forged_id,
    )
    response = await director_client.post("/gate-decisions", json=body)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["decided_by"] == "gate_director"
    assert payload["status"] == "RECORDED"
    assert payload["decided_at"] != "1999-01-01T00:00:00Z"
    assert payload["entity_id"] == forged_id


async def test_approved_without_evidence_is_rejected(
    director_client: AsyncClient,
) -> None:
    """缺证据不得 PASS（领域层拒绝，数据库 CHECK 再兜一层）。"""
    response = await director_client.post(
        "/gate-decisions", json=_body(evidence_refs=[])
    )
    assert response.status_code == 422, response.text
    assert "evidence_refs" in response.text


async def test_rejected_without_evidence_is_allowed(
    director_client: AsyncClient,
) -> None:
    response = await director_client.post(
        "/gate-decisions",
        json=_body(decision="REJECTED", evidence_refs=[]),
    )
    assert response.status_code == 201, response.text
    assert response.json()["decision"] == "REJECTED"


async def test_replay_is_idempotent_and_does_not_duplicate_event(
    director_client: AsyncClient,
    relay_sm: async_sessionmaker[AsyncSession],
) -> None:
    """同一实体 id 重放：一次写入、一个事件；改写内容则 409。"""
    replay_id = f"gate-replay-{uuid.uuid4().hex[:12]}"
    body = _body(entity_id=replay_id)
    first = await director_client.post("/gate-decisions", json=body)
    assert first.status_code == 201, first.text
    second = await director_client.post("/gate-decisions", json=body)
    assert second.status_code == 201, second.text
    assert first.json()["decided_at"] == second.json()["decided_at"]

    # 改写冲突必须复用**同一幂等键**（gate_instance_id 等），只改内容。
    # 早先版本用 _body() 生成随机 instance，却靠数据库里残留的固定 entity_id
    # 主键冲突假绿——干净库上会误判为 201。这里显式复用幂等键，使断言自包含。
    conflict = await director_client.post(
        "/gate-decisions",
        json=_body(
            entity_id=f"gate-conflict-{uuid.uuid4().hex[:12]}",
            gate_instance_id=body["gate_instance_id"],
            reason_code="OTHER",
        ),
    )
    assert conflict.status_code == 409, conflict.text

    async with relay_sm() as session:
        await apply_db_context(session, tenant_id=_TENANT, project_ids=(_PROJECT,))
        count = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM outbox.outbox_events "
                    "WHERE aggregate_type = 'GateDecision' "
                    "AND aggregate_id = :eid "
                    "AND event_type = 'PRODUCTION_GATE_APPROVED'"
                ),
                {"eid": replay_id},
            )
        ).scalar_one()
    assert count == 1
