"""生成执行域：generation_tasks 增租约列（Worker 崩溃恢复，ADR-R34）

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-11-17

背景（Phase 1 可靠性证据，ADR-R34）：
契约 9.4「Worker 崩溃 → 租约到期后重新领取 → 幂等重试」与 12.6「Worker
崩溃恢复：任务租约到期后 5 分钟内重新调度」要求任务有**租约**这个事实，
而 b8c9d0e1f2a3 落地的表只有状态与时间戳：领取后进程崩溃，任务会永远停在
RUNNING（``list_pending`` 只看 PENDING），主链静默卡死。本迁移补齐租约：

1. ``lease_owner`` / ``lease_expires_at``（实现扩展列，不出契约 required）：
   租约是**运行态元数据**，不是业务内容——谁在执行、租到什么时候，都不能
   进契约响应（否则调用方会把内部调度细节当 API 语义）。
2. 守卫函数重建：两列必须进可变列白名单。注意这里**不是**放宽不可变面——
   租约台账天然可变；准入事实（project/shot/route/引用/attempt/retry_of）
   仍在白名单之外，事后改写照旧被拒。
3. 存量 RUNNING 行回填 ``now() + 5 分钟``：迁移瞬间在跑的旧 worker 没有续租
   能力，给一个租约窗口而不是立刻判死；窗口过后由新 worker 接管（这正是
   崩溃恢复语义）。不回填会让旧任务在下一次扫描时被立即抢走，造成无谓的
   重复执行。
4. 过期租约扫描的部分索引：``WHERE status = 'RUNNING'``——回收扫描只关心
   在跑的任务，全表索引会把已终态的历史行也塞进索引里。

降级顺序与升级相反：先摘索引，再把守卫函数恢复为 b8c9d0e1f2a3 的白名单，
最后删列（删列时守卫已不认识这两列，不会误判）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0e1f2a3b4c5"
down_revision: str | Sequence[str] | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "generation_execution"
TABLE = "generation_tasks"

#: b8c9d0e1f2a3 的白名单（降级时恢复的历史口径，不得随代码演进）。
_MUTABLE_COLUMNS_V1: tuple[str, ...] = ("result_ref", "started_at", "completed_at")

#: 本迁移的白名单：租约是运行态元数据，必须可改（接管/续租都写它）。
_MUTABLE_COLUMNS_V2: tuple[str, ...] = (
    *_MUTABLE_COLUMNS_V1,
    "lease_owner",
    "lease_expires_at",
)

#: 存量 RUNNING 任务的租约窗口（与 config.generation_lease_seconds 默认值同源；
#: 迁移是历史快照，不 import 代码）。
_LEGACY_LEASE_WINDOW = "5 minutes"


def _create_guard_function(mutable_columns: tuple[str, ...]) -> None:
    """重建守卫函数（ADR-R20 范式；与 b8c9d0e1f2a3 同一判定逻辑，仅白名单不同）。"""
    mutable = ", ".join(repr(c) for c in mutable_columns)
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{TABLE}_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    '{SCHEMA}.{TABLE} 业务对象不可物理删除';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.generation_task_status_transitions t
                    WHERE t.from_status = old_status AND t.to_status = new_status
                ) THEN
                    RAISE EXCEPTION
                        '非法状态转移 % -> %（未登记，ADR-R20）',
                        old_status, new_status;
                END IF;
            END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = '{SCHEMA}.{TABLE}'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ({mutable}) THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        '{SCHEMA}.{TABLE}.% 为不可变内容列，禁止改写', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_guard_trigger "
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{TABLE}_guard()"
    )


def _drop_guard() -> None:
    op.execute(
        f"DROP TRIGGER IF EXISTS {TABLE}_guard_trigger ON {SCHEMA}.{TABLE}"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{TABLE}_guard()")


def upgrade() -> None:
    _drop_guard()
    op.add_column(
        TABLE,
        sa.Column("lease_owner", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        TABLE,
        sa.Column(
            "lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        schema=SCHEMA,
    )
    _create_guard_function(_MUTABLE_COLUMNS_V2)
    # 存量在跑任务给一个租约窗口：旧 worker 不会续租，窗口过后才可被接管。
    op.execute(
        f"""
        UPDATE {SCHEMA}.{TABLE}
        SET lease_expires_at = now() + interval '{_LEGACY_LEASE_WINDOW}'
        WHERE status = 'RUNNING' AND lease_expires_at IS NULL
        """
    )
    op.execute(
        f"""
        CREATE INDEX ix_{TABLE}_lease_expiry
        ON {SCHEMA}.{TABLE} (lease_expires_at)
        WHERE status = 'RUNNING'
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.ix_{TABLE}_lease_expiry")
    _drop_guard()
    _create_guard_function(_MUTABLE_COLUMNS_V1)
    op.drop_column(TABLE, "lease_expires_at", schema=SCHEMA)
    op.drop_column(TABLE, "lease_owner", schema=SCHEMA)
