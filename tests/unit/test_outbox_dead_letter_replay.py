"""死信人工重放（D6-9）——判定规则与授权边界的静态测试。

真实验收（RLS、并发、append-only 触发器）打在 ``tests/integration/
test_outbox_dead_letter_replay_pg.py``；本文件只覆盖「不需要数据库就能判定」的部分：

* 判定顺序：先取数据库事实，再判归属与生命周期，最后才写；
* 每条拒绝路径都**不产生审计行**（拒绝过的操作不该留痕，也不该写脏数据）；
* 重放只动投递状态三列，绝不碰事件内容与 ``published_at``；
* 授权矩阵里运维角色拿不到「伪造已投递」与「伪造业务事件」的能力。

为什么这些规则值得单独测
------------------------
它们不是「实现细节」而是**权限与语义边界**：把 ``published_at`` 加进 SET 子句、
给运维角色补一条 outbox_events INSERT、或把拒绝路径改成先写审计再判定，
三件事都不会让主流程测试变红，但每一件都让「谁放行了一条本该被丢弃的事件」
重新变成不可审计的动作。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared import db_grants
from packages.shared.partition_guard import ops_replay_update_columns
from services.outbox_ops.repository.dead_letter_repo import (
    _CLEAR_DEAD_LETTER,
    _LOCK_FOR_REPLAY,
    DeadLetterRow,
)
from services.outbox_ops.service.dead_letter_service import (
    DeadLetterReplayService,
    declared_event_types,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d9")
# 已在契约里登记的 event_type（contracts/runtime/event_catalog.yaml）。
_DECLARED_TYPE = "QC_TASK_QUEUED"
_UNDECLARED_TYPE = "NOT_A_DECLARED_EVENT_TYPE"


def _row(
    *,
    tenant_id: uuid.UUID = TENANT,
    event_type: str = _DECLARED_TYPE,
    attempts: int = 5,
    dead_lettered_at: datetime | None = datetime(2026, 11, 24, 3, 0, tzinfo=UTC),
    published_at: datetime | None = None,
) -> DeadLetterRow:
    return DeadLetterRow(
        event_id=uuid.uuid4(),
        event_type=event_type,
        tenant_id=tenant_id,
        attempts=attempts,
        dead_lettered_at=dead_lettered_at,
        published_at=published_at,
    )


class _FakeRepository:
    """替身仓储：只记录「被调用了什么」，不模拟数据库语义。

    刻意不实现 ``rowcount``/SQL 语义——那些必须由真实 PG 证明（集成测试）。
    这里要证明的是**编排顺序**：拒绝路径不得触碰写方法。
    """

    def __init__(self, row: DeadLetterRow | None, *, clears: bool = True) -> None:
        self._row = row
        self._clears = clears
        self.clear_calls: list[uuid.UUID] = []
        self.recorded: list[Any] = []

    async def lock(self, event_id: uuid.UUID) -> DeadLetterRow | None:
        if self._row is None or self._row.event_id != event_id:
            return None
        return self._row

    async def clear_dead_letter(self, event_id: uuid.UUID) -> bool:
        self.clear_calls.append(event_id)
        return self._clears

    async def record(self, replay: Any) -> None:
        self.recorded.append(replay)


def _service(repo: _FakeRepository) -> DeadLetterReplayService:
    return DeadLetterReplayService(repo)  # type: ignore[arg-type]


async def _replay(repo: _FakeRepository) -> Any:
    target = repo._row.event_id if repo._row else uuid.uuid4()
    return await _service(repo).replay(
        event_id=target,
        tenant_id=TENANT,
        operator_id="ops-1",
        reason="relay 侧下游持续 5xx，确认已恢复后重放",
    )


def _assert_rejected_without_trace(repo: _FakeRepository) -> None:
    assert repo.clear_calls == [], "拒绝路径不得改动事件状态"
    assert repo.recorded == [], "拒绝路径不得留下审计行"


# --------------------------------------------------------------------------- #
# 判定规则：每条拒绝路径都必须「只看不写」
# --------------------------------------------------------------------------- #
async def test_missing_event_is_not_found_and_touches_nothing() -> None:
    """不存在 → 404；既不写审计也不改状态。"""
    repo = _FakeRepository(None)
    with pytest.raises(DomainError) as exc:
        await _replay(repo)
    assert exc.value.error_code is ErrorRegistry.NOT_FOUND
    _assert_rejected_without_trace(repo)


async def test_foreign_tenant_is_reported_as_not_found() -> None:
    """跨租户与不存在同形（404）：不给「别处有这条事件」的探测位。"""
    repo = _FakeRepository(_row(tenant_id=OTHER_TENANT))
    with pytest.raises(DomainError) as exc:
        await _replay(repo)
    assert exc.value.error_code is ErrorRegistry.NOT_FOUND
    _assert_rejected_without_trace(repo)


async def test_published_event_cannot_be_replayed() -> None:
    """已投递成功 → 409：重放只用于处置死信，不是「再投一次」。"""
    repo = _FakeRepository(
        _row(published_at=datetime(2026, 11, 24, 4, 0, tzinfo=UTC))
    )
    with pytest.raises(DomainError) as exc:
        await _replay(repo)
    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED
    _assert_rejected_without_trace(repo)


async def test_live_event_is_not_replayable() -> None:
    """仍在正常重试中（未死信）→ 409：清标记会掩盖退避、把失败次数抹掉。"""
    repo = _FakeRepository(_row(dead_lettered_at=None))
    with pytest.raises(DomainError) as exc:
        await _replay(repo)
    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED
    _assert_rejected_without_trace(repo)


async def test_undeclared_event_type_is_rejected() -> None:
    """契约外事件类型 → 409：重放它只会制造永不被消费的积压。

    D6-3 的原始表述是「handler 已注册」，但那份注册表活在 relay 进程里。
    这里判的是契约全集——契约外必然没有消费者，故判据实质成立。
    """
    repo = _FakeRepository(_row(event_type=_UNDECLARED_TYPE))
    with pytest.raises(DomainError) as exc:
        await _replay(repo)
    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED
    _assert_rejected_without_trace(repo)


async def test_lost_race_is_conflict_not_silent_success() -> None:
    """条件更新命中 0 行 → 409：判定已过期，按冲突拒绝而不是当成成功。"""
    repo = _FakeRepository(_row(), clears=False)
    with pytest.raises(DomainError) as exc:
        await _replay(repo)
    assert exc.value.error_code is ErrorRegistry.CONCURRENT_MODIFICATION
    assert repo.recorded == [], "状态没改动就不能写「重放成功」的审计行"


# --------------------------------------------------------------------------- #
# 成功路径：审计行必须带「重放前」的真实失败事实
# --------------------------------------------------------------------------- #
async def test_success_preserves_pre_replay_failure_facts() -> None:
    """审计行记录 attempts/dead_lettered_at 的**重放前**取值。"""
    dead_at = datetime(2026, 11, 24, 2, 30, tzinfo=UTC)
    repo = _FakeRepository(_row(attempts=5, dead_lettered_at=dead_at))
    result = await _replay(repo)

    assert repo.clear_calls == [result.event_id]
    assert len(repo.recorded) == 1
    audit = repo.recorded[0]
    assert audit.source_event_id == result.event_id
    assert audit.tenant_id == TENANT
    assert audit.event_type == _DECLARED_TYPE
    assert audit.operator == "ops-1", "操作者取自 Principal，不取请求体"
    assert audit.reason
    assert audit.attempts_before == 5
    assert audit.dead_lettered_at_before == dead_at
    assert audit.result == "SUCCEEDED"
    assert audit.failure_reason is None
    # replay_id 是操作身份：每次重放都是一条新的操作记录，不是事件身份。
    assert audit.replay_id != audit.source_event_id


# --------------------------------------------------------------------------- #
# SQL 不变量：重放只动投递状态，不动事件身份与投递时间
# --------------------------------------------------------------------------- #
def _assigned_columns(sql: str) -> set[str]:
    """取出 UPDATE 语句 SET 子句里被赋值的列名。"""
    set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    return set(re.findall(r"([a-z_]+)\s*=", set_clause))


def test_clear_statement_only_touches_delivery_state() -> None:
    """SET 子句恰好是三列：多一列就是越权语义（尤其 published_at）。"""
    assert _assigned_columns(_CLEAR_DEAD_LETTER.text) == {
        "dead_lettered_at",
        "next_attempt_at",
        "attempts",
    }


def test_clear_statement_is_conditional_on_current_state() -> None:
    """条件必须同时带「当前是死信」与「尚未投递」，否则会清掉正常重试中的行。"""
    sql = " ".join(_CLEAR_DEAD_LETTER.text.split())
    assert "dead_lettered_at IS NOT NULL" in sql
    assert "published_at IS NULL" in sql


def test_lock_statement_locks_the_row() -> None:
    """``FOR UPDATE`` 是「先取事实再判定」成立的前提（并发下判定才有意义）。"""
    sql = " ".join(_LOCK_FOR_REPLAY.text.split())
    assert "FOR UPDATE" in sql
    for column in ("attempts", "dead_lettered_at", "published_at"):
        assert column in sql


# --------------------------------------------------------------------------- #
# 契约与授权矩阵
# --------------------------------------------------------------------------- #
def test_declared_event_types_reads_the_catalog() -> None:
    """契约全集来自 event_catalog.yaml，且包含已知类型。"""
    declared = declared_event_types()
    assert isinstance(declared, frozenset)
    assert _DECLARED_TYPE in declared
    assert _UNDECLARED_TYPE not in declared


def test_ops_role_cannot_forge_business_events() -> None:
    """运维角色对 outbox_events 无 INSERT：补上它等于允许伪造任意业务事件。"""
    grants = db_grants.INFRA_ROLE_GRANTS[db_grants.OUTBOX_OPS]
    for grant in grants:
        if grant.table != "outbox_events":
            continue
        assert "INSERT" not in grant.privileges
        assert "DELETE" not in grant.privileges


def test_ops_role_cannot_write_published_at() -> None:
    """列级 UPDATE 不含 published_at：投递时间只能由真正投递成功的 relay 写。"""
    columns = set(ops_replay_update_columns())
    assert columns == {"attempts", "dead_lettered_at", "next_attempt_at"}
    assert "published_at" not in columns


def test_partition_grant_columns_come_from_the_matrix() -> None:
    """分区授权与父表授权同源：不允许存在第二份列清单。"""
    matrix_columns = next(
        grant.columns
        for grant in db_grants.INFRA_ROLE_GRANTS[db_grants.OUTBOX_OPS]
        if grant.table == "outbox_events" and grant.columns
    )
    assert ops_replay_update_columns() == matrix_columns


def test_infra_matrix_is_self_consistent() -> None:
    """新增的运维矩阵同样受闸门校验（且被禁止 DELETE）。"""
    assert db_grants.validate_infra_matrix() == []


# --------------------------------------------------------------------------- #
# 请求体：只能带 reason
# --------------------------------------------------------------------------- #
def test_request_body_rejects_smuggled_identity_or_tenant() -> None:
    """``operator`` / ``tenant_id`` 出现在请求体即 422（静默忽略比报错更危险）。"""
    from services.outbox_ops.api.outbox_ops_routes import ReplayDeadLetterRequest

    ReplayDeadLetterRequest(reason="ok")
    for smuggled in (
        {"reason": "ok", "operator": "ceo"},
        {"reason": "ok", "tenant_id": str(OTHER_TENANT)},
        {"reason": "ok", "published_at": "2026-11-24T00:00:00Z"},
    ):
        with pytest.raises(ValidationError):
            ReplayDeadLetterRequest(**smuggled)


def test_request_body_requires_a_reason() -> None:
    """没有理由的重放不可接受：审计行的意义就是记住「为什么」。"""
    from services.outbox_ops.api.outbox_ops_routes import ReplayDeadLetterRequest

    with pytest.raises(ValidationError):
        ReplayDeadLetterRequest(reason="")
