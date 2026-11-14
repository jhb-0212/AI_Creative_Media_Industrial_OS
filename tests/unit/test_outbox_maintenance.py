"""Outbox 分区维护的纯逻辑单测（不连库）。

覆盖：月份边界换算、分区命名/解析、创建计划、保留期计划（含边界月份）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from workers.outbox_maintenance.__main__ import (
    DEFAULT_PARTITION,
    add_months,
    month_start,
    parse_partition_month,
    partition_name,
    plan_create,
    plan_retention,
)


def test_month_start_normalizes_to_first_day_utc() -> None:
    assert month_start(datetime(2026, 11, 14, 12, 30, tzinfo=UTC)) == datetime(
        2026, 11, 1, tzinfo=UTC
    )


def test_add_months_crosses_year_boundaries() -> None:
    assert add_months(datetime(2026, 12, 1, tzinfo=UTC), 1) == datetime(
        2027, 1, 1, tzinfo=UTC
    )
    assert add_months(datetime(2026, 1, 1, tzinfo=UTC), -1) == datetime(
        2025, 12, 1, tzinfo=UTC
    )
    assert add_months(datetime(2026, 11, 1, tzinfo=UTC), 30) == datetime(
        2029, 5, 1, tzinfo=UTC
    )


def test_partition_name_and_parse_roundtrip() -> None:
    name = partition_name(datetime(2026, 11, 1, tzinfo=UTC))
    assert name == "outbox_events_y202611"
    assert parse_partition_month(name) == datetime(2026, 11, 1, tzinfo=UTC)


def test_parse_partition_month_ignores_default_partition() -> None:
    assert parse_partition_month(DEFAULT_PARTITION) is None
    assert parse_partition_month("outbox_events_archived_y202601") is None


def test_plan_create_skips_existing_and_covers_horizon() -> None:
    today = datetime(2026, 11, 14, tzinfo=UTC)
    planned = plan_create({"outbox_events_y202611"}, today, ahead_months=2)
    assert [p.name for p in planned] == [
        "outbox_events_y202612",
        "outbox_events_y202701",
    ]
    assert planned[0].lower == datetime(2026, 12, 1, tzinfo=UTC)
    assert planned[0].upper == datetime(2027, 1, 1, tzinfo=UTC)


def test_plan_create_everything_exists_is_noop() -> None:
    today = datetime(2026, 11, 14, tzinfo=UTC)
    existing = {"outbox_events_y202611", "outbox_events_y202612"}
    assert plan_create(existing, today, ahead_months=1) == []


def test_plan_retention_only_fully_old_months() -> None:
    """保留 6 个月，2026-11 为当月 → cutoff=2026-05-01。

    - 2026-04（上界 2026-05-01）<= cutoff：可摘除；
    - 2026-05（上界 2026-06-01）> cutoff：保留（该月仍有窗口内的数据）；
    - DEFAULT 与归档表不参与。
    """
    today = datetime(2026, 11, 14, tzinfo=UTC)
    existing = {
        DEFAULT_PARTITION,
        "outbox_events_y202604",
        "outbox_events_y202605",
        "outbox_events_y202611",
        "outbox_events_archived_y202601",
    }
    planned = plan_retention(existing, today, retain_months=6)
    assert [p.name for p in planned] == ["outbox_events_y202604"]
    assert planned[0].upper == datetime(2026, 5, 1, tzinfo=UTC)


def test_plan_retention_keeps_everything_when_horizon_not_reached() -> None:
    today = datetime(2026, 11, 14, tzinfo=UTC)
    existing = {"outbox_events_y202606", "outbox_events_y202607"}
    assert plan_retention(existing, today, retain_months=6) == []


def test_csv_value_serializes_json_payload() -> None:
    """jsonb 落到 CSV 里必须是可复算的 JSON 文本，不能是 Python repr。"""
    from workers.outbox_maintenance.archive import _csv_value

    assert _csv_value(None) == ""
    assert _csv_value({"n": 1}) == '{"n":1}'
    assert _csv_value(["a", "b"]) == '["a","b"]'
    assert _csv_value("x") == "x"


def test_s3_target_is_refused_not_silently_downgraded() -> None:
    """归档目标是 s3:// 时必须报错，禁止静默改存本地。"""
    import asyncio
    from typing import cast

    import pytest
    from sqlalchemy.ext.asyncio import AsyncConnection

    from workers.outbox_maintenance.archive import (
        S3ArchiveUnavailableError,
        export_partition,
    )

    with pytest.raises(S3ArchiveUnavailableError, match="S3 归档尚未接入"):
        asyncio.run(
            export_partition(
                cast(AsyncConnection, None),
                "outbox_events_y202601",
                "s3://demo/outbox",
            )
        )
