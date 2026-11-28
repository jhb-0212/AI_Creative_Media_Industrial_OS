"""死信查询（``OUTBOX.DEAD_LETTER.READ``）——游标、分页与权限边界的静态测试。

真实验收（RLS 跨租户不可见、游标翻页、重放后从结果集消失）打在
``tests/integration/test_outbox_dead_letter_list_pg.py``；本文件只覆盖
「不需要数据库就能判定」的部分：

* 游标是**全序**的：``(dead_lettered_at, event_id)`` 两列，缺一列就会在
  「同一时刻批量进死信」时跨页丢行；
* 非法游标必须**拒绝**（422），不能被吞成空页——空页等于告诉运维「没有更多
  死信了」，而真实原因是他抄错了一个字符；
* 列表 SQL 不得带出 ``payload``：权限边界不是「别 SELECT 它」，而是「这条
  查询里根本没有这一列」。

为什么这些规则值得单独测
------------------------
它们都不会让「列表能返回数据」这件事变红。翻页丢一条、payload 多带一列、
游标解析宽松一点——三种缺陷在手工点一次接口时都看不出来，只在真实积压与
审计时才暴露。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.outbox_ops.repository.dead_letter_repo import (
    _DEAD_LETTERS_AFTER_CURSOR,
    _DEAD_LETTERS_FIRST_PAGE,
    DeadLetterSummaryRow,
)
from services.outbox_ops.service.dead_letter_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DeadLetterQueryService,
    decode_cursor,
    encode_cursor,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")
_AT = datetime(2026, 11, 25, 6, 0, 0, 123456, tzinfo=UTC)


def _summary(index: int, *, at: datetime = _AT) -> DeadLetterSummaryRow:
    return DeadLetterSummaryRow(
        event_id=uuid.UUID(int=index),
        event_type="QC_TASK_QUEUED",
        tenant_id=TENANT,
        attempts=5,
        dead_lettered_at=at,
        occurred_at=at,
    )


class _FakeRepository:
    """替身仓储：只记录调用参数并回放给定行，不模拟 SQL 语义。"""

    def __init__(self, rows: list[DeadLetterSummaryRow]) -> None:
        self._rows = rows
        self.calls: list[dict[str, Any]] = []

    async def list_dead_letters(
        self,
        *,
        tenant_id: uuid.UUID,
        fetch: int,
        event_type: str | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> list[DeadLetterSummaryRow]:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "fetch": fetch,
                "event_type": event_type,
                "after": after,
            }
        )
        return self._rows[:fetch]


def _service(rows: list[DeadLetterSummaryRow]) -> tuple[DeadLetterQueryService, _FakeRepository]:
    repository = _FakeRepository(rows)
    return DeadLetterQueryService(repository), repository  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 游标：编码往返与非法输入
# --------------------------------------------------------------------------- #
def test_cursor_round_trips_with_microseconds_and_timezone() -> None:
    event_id = uuid.uuid4()
    at, parsed_id = decode_cursor(encode_cursor(_AT, event_id))
    assert at == _AT
    assert parsed_id == event_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("no-separator", "格式非法"),
        ("not-a-time|00000000-0000-0000-0000-000000000001", "不是合法时间"),
        # 无时区：fromisoformat 会接受，但与 timestamptz 比较时按服务器时区解释，
        # 同一份游标在不同部署里指向不同的行 —— 必须拒绝。
        ("2026-11-25T06:00:00|00000000-0000-0000-0000-000000000001", "必须带时区"),
        ("2026-11-25T06:00:00+00:00|not-a-uuid", "不是合法 UUID"),
        ("2026-11-25T06:00:00+00:00|", "不是合法 UUID"),
    ],
)
def test_decode_cursor_rejects_malformed(raw: str, expected: str) -> None:
    with pytest.raises(DomainError) as exc:
        decode_cursor(raw)
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR
    assert expected in str(exc.value)


# --------------------------------------------------------------------------- #
# 分页：探测多取一行，截断后给出 next_cursor
# --------------------------------------------------------------------------- #
async def test_first_page_requests_limit_plus_one_probe() -> None:
    service, repository = _service([_summary(i) for i in range(3)])
    page = await service.list_dead_letters(tenant_id=TENANT, limit=3)

    call = repository.calls[0]
    assert call["after"] is None, "首页不带游标"
    assert call["fetch"] == 4, "多取一行才能判断有没有下一页"
    assert call["tenant_id"] == TENANT
    assert len(page.items) == 3
    assert page.next_cursor is None, "恰好 3 条时后面没有了"


async def test_page_with_extra_row_yields_cursor_pointing_at_last_returned() -> None:
    rows = [_summary(i) for i in range(4)]
    service, _ = _service(rows)
    page = await service.list_dead_letters(tenant_id=TENANT, limit=3)

    assert len(page.items) == 3, "多取的那一行不外泄"
    assert page.next_cursor is not None
    at, event_id = decode_cursor(page.next_cursor)
    assert event_id == rows[2].event_id, "游标锚点是本页最后一条，不是多取的那条"
    assert at == rows[2].dead_lettered_at


async def test_cursor_is_forwarded_as_parsed_tuple() -> None:
    cursor = encode_cursor(_AT, uuid.UUID(int=7))
    service, repository = _service([_summary(1)])
    await service.list_dead_letters(tenant_id=TENANT, limit=10, cursor=cursor)

    assert repository.calls[0]["after"] == (_AT, uuid.UUID(int=7))


async def test_event_type_filter_is_forwarded_verbatim() -> None:
    service, repository = _service([])
    page = await service.list_dead_letters(
        tenant_id=TENANT, limit=10, event_type="GENERATION_FAILED"
    )
    assert repository.calls[0]["event_type"] == "GENERATION_FAILED"
    assert page.items == ()
    assert page.next_cursor is None


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1])
async def test_limit_out_of_range_is_rejected(limit: int) -> None:
    service, _ = _service([])
    with pytest.raises(DomainError) as exc:
        await service.list_dead_letters(tenant_id=TENANT, limit=limit)
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR


def test_default_page_size_within_contract_bounds() -> None:
    assert 1 <= DEFAULT_PAGE_SIZE <= MAX_PAGE_SIZE


# --------------------------------------------------------------------------- #
# SQL 形态：全序排序、无 payload、两条语句同构
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "statement",
    [_DEAD_LETTERS_FIRST_PAGE, _DEAD_LETTERS_AFTER_CURSOR],
)
def test_list_sql_is_total_order_and_tenant_scoped(statement: Any) -> None:
    sql = str(statement)
    assert "dead_lettered_at DESC, event_id DESC" in sql, (
        "只按 dead_lettered_at 排序会在同一时刻多条死信时跨页丢行"
    )
    assert "tenant_id = :tenant_id" in sql
    assert "dead_lettered_at IS NOT NULL" in sql


@pytest.mark.parametrize(
    "statement",
    [_DEAD_LETTERS_FIRST_PAGE, _DEAD_LETTERS_AFTER_CURSOR],
)
def test_list_sql_never_selects_payload(statement: Any) -> None:
    sql = str(statement).lower()
    assert "payload" not in sql, (
        "运维列表只该给元数据：payload 里有业务数据与跨域引用，"
        "把它带出去等于把「可读运维元数据」放大成「可读业务事件内容」"
    )


def test_cursor_page_compares_the_same_two_columns_as_its_order_by() -> None:
    """游标谓词的列必须与 ORDER BY 完全一致，否则分页会在边界处跳行。"""
    sql = str(_DEAD_LETTERS_AFTER_CURSOR)
    assert "(dead_lettered_at, event_id) < (:cursor_at, :cursor_event_id)" in sql
