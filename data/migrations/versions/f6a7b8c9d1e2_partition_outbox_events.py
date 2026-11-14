"""partition outbox_events by occurred_at (monthly range partitions)

根因（R06 保留期缺口）：outbox.outbox_events 是 append-only 审计事件流，没有任何
删除路径。表随业务线性增长，relay 的 `published_at IS NULL` 扫描与索引成本持续
上升；真到要清理时只剩 DELETE 一条路——大事务、锁表、与主库写入互相拖累。
根治方式是把「保留期」变成元数据操作：按月分区，到期 DETACH（保留数据、不重写），
需要释放空间时再显式 DROP。决策与取舍见
docs/adr/ADR-R06-2-outbox-retention-partitioning.md。

执行内容：
1. 旧表改名 outbox_events_legacy（其索引/主键约束同时改名，腾出名字）；
2. 建按月范围分区父表，主键改为 (event_id, occurred_at)（PG 要求唯一约束含分区键）；
3. 按旧数据实际时间跨度补建月份分区（+ 未来 2 个月），并建 DEFAULT 分区兜底；
4. 全量搬迁数据 → 建回原索引名 → 授权（SELECT/INSERT/UPDATE，不给 DELETE）→
   重建 RLS 策略（父表策略自动作用于所有分区）→ 删除旧表。

大表运维提示：第 4 步是单事务全量 INSERT，事件量大时会产生较长 IO 与 WAL 写入，
应在维护窗口执行；数据量上规模后改为「建父表 + 分批 INSERT + 单独补历史分区」的
多步迁移。降级同样是一次全量回搬。

Revision ID: f6a7b8c9d1e2
Revises: e5f6a7b8c9d0
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d1e2"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "outbox"
_TABLE = "outbox_events"
_LEGACY = "outbox_events_legacy"
_DEFAULT_PARTITION = "outbox_events_default"
_APP_ROLE = "app_role"
_POLICY = "tenant_isolation_outbox_events"
_INDEXES = ("ix_outbox_aggregate", "ix_outbox_published", "ix_outbox_tenant")

_COLUMNS = (
    "event_id",
    "aggregate_type",
    "aggregate_id",
    "tenant_id",
    "event_type",
    "payload",
    "occurred_at",
    "published_at",
)


def _month_start(value: datetime) -> datetime:
    """归一到一个月的起点（UTC，秒级精度，含时区）。"""
    return datetime(value.year, value.month, 1, tzinfo=UTC)


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + (value.month - 1) + months
    year, month = divmod(month_index, 12)
    return datetime(year, month + 1, 1, tzinfo=UTC)


def _partition_name(month: datetime) -> str:
    return f"outbox_events_y{month.strftime('%Y%m')}"


def _create_parent() -> None:
    op.execute(
        f"""
        CREATE TABLE {SCHEMA}.{_TABLE} (
            event_id uuid NOT NULL,
            aggregate_type character varying(64) NOT NULL,
            aggregate_id character varying(64) NOT NULL,
            tenant_id uuid NOT NULL,
            event_type character varying(64) NOT NULL,
            payload jsonb NOT NULL,
            occurred_at timestamp with time zone NOT NULL,
            published_at timestamp with time zone,
            CONSTRAINT outbox_events_pkey PRIMARY KEY (event_id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
        """
    )


def _create_partition(name: str, lower: datetime, upper: datetime) -> None:
    op.execute(
        f"CREATE TABLE {SCHEMA}.{name} PARTITION OF {SCHEMA}.{_TABLE} "
        f"FOR VALUES FROM ('{lower.isoformat()}') TO ('{upper.isoformat()}')"
    )


def _create_indexes() -> None:
    op.execute(f"CREATE INDEX ix_outbox_aggregate ON {SCHEMA}.{_TABLE} (aggregate_id)")
    op.execute(f"CREATE INDEX ix_outbox_published ON {SCHEMA}.{_TABLE} (published_at)")
    op.execute(f"CREATE INDEX ix_outbox_tenant ON {SCHEMA}.{_TABLE} (tenant_id)")


def _grant_and_protect() -> None:
    # 父表授权，不单独授权分区子表：应用永远通过父表名访问。
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.{_TABLE} TO {_APP_ROLE}"
    )
    # append-only：DELETE 一律不给（与 d4e5f6a7b8c9 的口径一致）。
    op.execute(f"REVOKE DELETE ON {SCHEMA}.{_TABLE} FROM {_APP_ROLE}")
    op.execute(f"ALTER TABLE {SCHEMA}.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {_POLICY}
        ON {SCHEMA}.{_TABLE}
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        )
        """
    )


def _month_range() -> tuple[datetime, datetime]:
    """按旧数据真实跨度决定要补建的月份分区（空表则从本月起）。"""
    bind = op.get_bind()
    row = bind.execute(
        sa.text(f"SELECT min(occurred_at), max(occurred_at) FROM {SCHEMA}.{_LEGACY}")
    ).one()
    min_occurred, max_occurred = row[0], row[1]
    now = datetime.now(UTC)
    lower = _month_start(min_occurred) if min_occurred else _month_start(now)
    upper = _add_months(_month_start(max_occurred if max_occurred else now), 2)
    return lower, upper


def upgrade() -> None:
    """旧表改名 → 建分区父表与月份分区 → 搬迁 → 索引/授权/RLS → 删旧表。"""
    op.execute(f"ALTER TABLE {SCHEMA}.{_TABLE} RENAME TO {_LEGACY}")
    op.execute(
        f"ALTER TABLE {SCHEMA}.{_LEGACY} "
        f"RENAME CONSTRAINT outbox_events_pkey TO outbox_events_legacy_pkey"
    )
    for index in _INDEXES:
        op.execute(f"ALTER INDEX {SCHEMA}.{index} RENAME TO {index}_legacy")

    _create_parent()

    lower, upper = _month_range()
    month = lower
    while month < upper:
        _create_partition(_partition_name(month), month, _add_months(month, 1))
        month = _add_months(month, 1)

    # DEFAULT 分区兜底：维护任务漏建月份时写入不会失败，代价是行落进 default。
    op.execute(
        f"CREATE TABLE {SCHEMA}.{_DEFAULT_PARTITION} "
        f"PARTITION OF {SCHEMA}.{_TABLE} DEFAULT"
    )

    columns = ", ".join(_COLUMNS)
    op.execute(
        f"INSERT INTO {SCHEMA}.{_TABLE} ({columns}) "
        f"SELECT {columns} FROM {SCHEMA}.{_LEGACY}"
    )

    _create_indexes()
    _grant_and_protect()

    op.execute(f"DROP TABLE {SCHEMA}.{_LEGACY}")


def downgrade() -> None:
    """回滚为普通表（会丢失分区布局，但数据与 RLS/授权口径保持一致）。"""
    legacy_flat = "outbox_events_flat"
    op.execute(
        f"""
        CREATE TABLE {SCHEMA}.{legacy_flat} (
            event_id uuid NOT NULL,
            aggregate_type character varying(64) NOT NULL,
            aggregate_id character varying(64) NOT NULL,
            tenant_id uuid NOT NULL,
            event_type character varying(64) NOT NULL,
            payload jsonb NOT NULL,
            occurred_at timestamp with time zone NOT NULL,
            published_at timestamp with time zone,
            CONSTRAINT {legacy_flat}_pkey PRIMARY KEY (event_id)
        )
        """
    )
    columns = ", ".join(_COLUMNS)
    op.execute(
        f"INSERT INTO {SCHEMA}.{legacy_flat} ({columns}) "
        f"SELECT {columns} FROM {SCHEMA}.{_TABLE}"
    )

    op.execute(f"DROP TABLE {SCHEMA}.{_TABLE} CASCADE")
    op.execute(f"ALTER TABLE {SCHEMA}.{legacy_flat} RENAME TO {_TABLE}")
    op.execute(
        f"ALTER TABLE {SCHEMA}.{_TABLE} "
        f"RENAME CONSTRAINT {legacy_flat}_pkey TO outbox_events_pkey"
    )

    _create_indexes()
    _grant_and_protect()
