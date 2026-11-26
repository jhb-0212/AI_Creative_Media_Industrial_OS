"""新增死信列表查询的部分索引（``ix_outbox_dead_letter``）。

为什么需要
----------
死信查询端点（``GET /outbox/dead-letters``）按
``dead_lettered_at IS NOT NULL ORDER BY dead_lettered_at DESC, event_id DESC``
做键集分页。``outbox_events`` 现有三个索引分别是 aggregate_id / published_at /
tenant_id，没有一个能覆盖这个谓词与排序：查询会落到 IXSCAN(tenant_id) 之后
再过滤、排序，数据量越大越慢——而这是一张**所有事件都写**的热表。

为什么是部分索引
----------------
死信是极少数行（投递重试耗尽后才进入），普通复合索引会让每一次事件写入都为
这张 B-tree 付维护代价，只为服务一个低频运维查询。``WHERE dead_lettered_at
IS NOT NULL`` 把索引规模压到死信数量级，同时恰好命中查询谓词——优化器能证明
「查询条件是索引条件的超集」，因此这个索引对该查询是可用且唯一需要的。

为什么建在父表上
----------------
``outbox_events`` 是按 occurred_at 月分区的表。在父表上 ``CREATE INDEX`` 会让
分区索引**级联到每个已有分区**，且之后 ``CREATE TABLE ... PARTITION OF``
（``partition_guard`` 与维护工具的建分区路径）会自动为新分区建同名索引。
若逐个分区手工建，未来分区就会缺索引——那正是「只在当期有效」的假修复。

Revision ID: f9b0c1d2e3f4
Revises: f8a9b0c1d2e3
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.outbox import SCHEMA_NAME

revision = "f9b0c1d2e3f4"
down_revision: str | Sequence[str] | None = "f8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "outbox_events"
_INDEX = "ix_outbox_dead_letter"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX {_INDEX} ON {SCHEMA_NAME}.{_TABLE} "
        "(dead_lettered_at, event_id) "
        "WHERE dead_lettered_at IS NOT NULL"
    )


def downgrade() -> None:
    # 父表上的 DROP INDEX 会一并删掉各分区上的同名分区索引。
    op.execute(f"DROP INDEX {SCHEMA_NAME}.{_INDEX}")
