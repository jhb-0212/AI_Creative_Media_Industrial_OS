"""通用审批引擎 API 端到端测试（真实 PostgreSQL + 真实 JWT + 真实域角色）。

验证 `WORKFLOW.START_APPROVAL` / `WORKFLOW.SUBMIT_VOTE` 的执行闭环：

1. 发起人不等于审批人：发起人（制片）即使持交付负责人角色也不能投自己的票；
2. 分组投票权来自审批绑定：交付负责人/QC 分属两组，两组都赞成才终结；
3. 身份与结论不可伪造：请求体不接受 decision_by / status / 结论字段；
4. 完成事件随票据终结同事务发出，业务侧据此推进自身状态（ADR-R11-G）。
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from tests.conftest import apply_db_context, make_test_headers

_PROJECT = "proj_approval_api"
_OTHER_PROJECT = "proj_approval_other"
_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d5e1")
_TARGET = "DELIVERY.MANIFEST.APPROVE"


def _client(
    subject: str,
    roles: list[str],
    *,
    subject_type: str = "human",
    project_ids: list[str] | None = None,
) -> AsyncClient:
    headers = make_test_headers(
        subject_id=subject,
        roles=roles,
        tenant_id=_TENANT,
        subject_type=subject_type,
        project_ids=project_ids,
    )
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    )


@pytest.fixture
async def initiator_client() -> AsyncClient:
    """发起人：制片（START_APPROVAL 的允许主体）。"""
    async with _client("approval_initiator", ["制片", "交付负责人"]) as c:
        yield c


@pytest.fixture
async def owner_client() -> AsyncClient:
    async with _client("approval_owner", ["交付负责人"]) as c:
        yield c


@pytest.fixture
async def qc_client() -> AsyncClient:
    async with _client("approval_qc", ["QC Worker"]) as c:
        yield c


def _start_body(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "entity_id": f"ticket-api-{uuid.uuid4().hex[:12]}",
        "target_action_key": _TARGET,
        "resource_type": "DeliveryManifest",
        "resource_id": f"manifest-{uuid.uuid4().hex[:12]}",
        "project_id": _PROJECT,
        "ruleset_version": 3,
    }
    values.update(overrides)
    return values


async def _start(client: AsyncClient, **overrides: object) -> dict[str, object]:
    response = await client.post("/approval-tickets", json=_start_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


# --- 发起审批 ----------------------------------------------------------------


async def test_initiator_can_start_ticket(initiator_client: AsyncClient) -> None:
    payload = await _start(initiator_client)
    assert payload["entity_type"] == "ApprovalTicket"
    assert payload["status"] == "PENDING"
    assert payload["schema_version"] == 205
    assert payload["created_by"] == "approval_initiator"
    assert payload["version"] == 1
    assert "tenant_id" not in payload
    assert "flow_id" in payload  # 对外可见，但不暴露 groups 等内部配置


async def test_writer_cannot_start_approval() -> None:
    """越权发起：编剧不在 START_APPROVAL 允许主体内。"""
    async with _client("approval_writer", ["编剧"]) as client:
        response = await client.post("/approval-tickets", json=_start_body())
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_cross_project_scope_is_denied() -> None:
    """令牌只允许另一项目，不能发起本项目的审批。"""
    async with _client(
        "approval_narrow", ["制片"], project_ids=[_OTHER_PROJECT]
    ) as client:
        response = await client.post("/approval-tickets", json=_start_body())
    assert response.status_code == 403, response.text


async def test_unbound_target_action_is_denied(initiator_client: AsyncClient) -> None:
    """目标动作没有审批绑定：fail-closed。"""
    response = await initiator_client.post(
        "/approval-tickets", json=_start_body(target_action_key="SCRIPT.IMPORT")
    )
    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"


# --- 投票闭环 ----------------------------------------------------------------


async def test_two_group_approval_completes_ticket(
    initiator_client: AsyncClient,
    owner_client: AsyncClient,
    qc_client: AsyncClient,
    relay_sm: async_sessionmaker[AsyncSession],
) -> None:
    """交付负责人 + QC 两组都赞成 → APPROVED，完成事件落库。"""
    ticket = await _start(initiator_client)
    ticket_id = ticket["entity_id"]

    first = await owner_client.post(
        f"/approval-tickets/{ticket_id}/votes",
        json={"group_name": "delivery_owner", "decision": "APPROVE", "reason": "同意交付"},
    )
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "PENDING"  # 一组未终结

    second = await qc_client.post(
        f"/approval-tickets/{ticket_id}/votes",
        json={"group_name": "qc_rights", "decision": "APPROVE", "reason": "权利证据齐备"},
    )
    assert second.status_code == 201, second.text
    assert second.json()["status"] == "APPROVED"
    assert second.json()["version"] >= 2

    async with relay_sm() as session:
        await apply_db_context(session, tenant_id=_TENANT, project_ids=(_PROJECT,))
        rows = (
            await session.execute(
                sa.text(
                    """
                    SELECT event_type, payload FROM outbox.outbox_events
                    WHERE aggregate_id = :eid ORDER BY occurred_at
                    """
                ),
                {"eid": ticket_id},
            )
        ).all()
    types = [row.event_type for row in rows]
    assert "APPROVAL_TICKET_CREATED" in types
    assert types.count("APPROVAL_TICKET_COMPLETED") == 1
    completed = next(row for row in rows if row.event_type == "APPROVAL_TICKET_COMPLETED")
    assert completed.payload["decision"] == "APPROVED"
    assert completed.payload["action_key"] == _TARGET


async def test_initiator_cannot_vote_on_own_ticket(
    initiator_client: AsyncClient,
) -> None:
    """ACT-WORKFLOW-NEG：发起人不得投自己发起的票据（职责分离）。"""
    ticket = await _start(initiator_client)
    response = await initiator_client.post(
        f"/approval-tickets/{ticket['entity_id']}/votes",
        json={"group_name": "delivery_owner", "decision": "APPROVE", "reason": "自投"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_wrong_group_cannot_vote(
    initiator_client: AsyncClient, owner_client: AsyncClient
) -> None:
    """分组资格：交付负责人不能投 QC 组。"""
    ticket = await _start(initiator_client)
    response = await owner_client.post(
        f"/approval-tickets/{ticket['entity_id']}/votes",
        json={"group_name": "qc_rights", "decision": "APPROVE", "reason": "越组"},
    )
    assert response.status_code == 403, response.text


async def test_agent_cannot_vote(initiator_client: AsyncClient) -> None:
    """Agent 即使持有分组角色也不得投 CRITICAL 票。"""
    ticket = await _start(initiator_client)
    async with _client("approval_agent", ["交付负责人"], subject_type="agent") as client:
        response = await client.post(
            f"/approval-tickets/{ticket['entity_id']}/votes",
            json={"group_name": "delivery_owner", "decision": "APPROVE", "reason": "agent"},
        )
    assert response.status_code == 403, response.text


async def test_repeat_vote_is_idempotent(initiator_client: AsyncClient, owner_client: AsyncClient) -> None:
    """同内容重复投票幂等：都返回 201，票据不被第二次推进。"""
    ticket = await _start(initiator_client)
    body = {"group_name": "delivery_owner", "decision": "APPROVE", "reason": "同意"}
    url = f"/approval-tickets/{ticket['entity_id']}/votes"
    first = await owner_client.post(url, json=body)
    second = await owner_client.post(url, json=body)
    assert first.status_code == 201 and second.status_code == 201, (first.text, second.text)
    assert second.json()["status"] == "PENDING"
    assert second.json()["version"] == 1  # 未终结，版本不推进


async def test_terminal_ticket_rejects_vote(
    initiator_client: AsyncClient, owner_client: AsyncClient, qc_client: AsyncClient
) -> None:
    """终态票据不可再投票。"""
    ticket = await _start(initiator_client)
    await owner_client.post(
        f"/approval-tickets/{ticket['entity_id']}/votes",
        json={"group_name": "delivery_owner", "decision": "REJECT", "reason": "不合格"},
    )
    response = await qc_client.post(
        f"/approval-tickets/{ticket['entity_id']}/votes",
        json={"group_name": "qc_rights", "decision": "APPROVE", "reason": "同意"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "STATE_TRANSITION_DENIED"


async def test_forged_identity_fields_are_ignored(initiator_client: AsyncClient) -> None:
    """请求体塞 created_by / status / 结论字段不生效。"""
    forged = _start_body(created_by="someone-else", status="APPROVED", version=99)
    payload = await _start(initiator_client, **forged)
    assert payload["created_by"] == "approval_initiator"
    assert payload["status"] == "PENDING"
    assert payload["version"] == 1
