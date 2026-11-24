"""累积式会签签署事实表（ADR-R45 D2b）

问题（根因，不是「缺一张表」）
------------------------------
ADR-R23 的 ``audit.status_transitions`` 关闭了「迁移可追溯」，但 §D2-e 明确判定：
会签需要的是「推进**之前**某席位已签署」这一**另一类事实**——有独立生命周期
（撤回、消费、凑不齐），不能借 `from_status <> to_status` 的迁移表承载。
在 D2b 之前，多席位动作在 rbac.authorize 里一律 fail-closed（不静默降级已达成），
但会签功能本身不可用，#13 因此 blocked。

本迁移落地该事实表，把结构性不变量交给数据库而非「记得写对」：

1. **同席位同主体不可重复签**：``UNIQUE (tenant, resource_type, resource_id,
   action_key, seat_index, subject, event_kind)``。含 event_kind 让 REVOKE/CONSUME
   作为新行存在而不覆盖 SIGNED；含 subject 让「同席位同主体重复签」撞约束。
   刻意**不含时间列**——含时间会让重复提交写出两条 SIGNED，污染凑齐判定。
2. **有签署必有事件**：``event_id NOT NULL`` + 提交时约束触发器（分区表上 event_id
   单独不唯一，外键建不出来，与 D2a 同法）。SECURITY DEFINER 函数同时比
   event_id 与 tenant_id——只比前者会让租户 A 的签署指向租户 B 的事件。
3. **append-only 连 TRUNCATE 一起挡**：行级 UPDATE/DELETE + 语句级 TRUNCATE
   （TRUNCATE 不触发行级触发器）。撤回/消费是写新 event_kind 行，不是原地改。

写法约束
--------
- ``audit`` 与 D2a 同 schema、不同表，两类事实不混。
- RLS 同时 ENABLE 与 FORCE。
- 授权矩阵（packages/shared/db_grants.py）是唯一声明处；本迁移**不手写 GRANT**，
  只调用 ``converge_statements(..., tables={"approval_signatures"})`` 执行矩阵
  （DATA-6：收敛迁移之后的迁移手写 GRANT 一律判绕过唯一声明处）。

Revision ID: e4f5a6b7c8d9
Revises: f1a2b3c4d5e7
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "e4f5a6b7c8d9"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "audit"
TABLE = "approval_signatures"
OUTBOX_SCHEMA = "outbox"
OUTBOX_TABLE = "outbox_events"

def _tenant_predicate(column: str = "tenant_id") -> str:
    """租户谓词：未注入租户上下文时恒为 false（fail-closed），与 D2a 一字不差。"""
    return (
        f"{column} = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
    )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.execute(
        f"""
        CREATE TABLE {SCHEMA}.{TABLE} (
            signature_id    uuid          PRIMARY KEY,
            tenant_id       uuid          NOT NULL,
            organization_id uuid          NULL,
            resource_type   varchar(64)   NOT NULL,
            resource_id     varchar(64)   NOT NULL,
            action_key      varchar(64)   NOT NULL,
            seat_index      integer       NOT NULL,
            subject         varchar(128)  NOT NULL,
            event_kind      varchar(16)   NOT NULL,
            authority_ref   varchar(128)  NULL,
            evidence_refs   varchar(64)[] NOT NULL DEFAULT '{{}}',
            occurred_at     timestamptz   NOT NULL DEFAULT now(),
            event_id        uuid          NOT NULL,

            CONSTRAINT uq_signature_event
                UNIQUE (tenant_id, resource_type, resource_id, action_key, seat_index, subject, event_kind),
            CONSTRAINT ck_signature_kind
                CHECK (event_kind IN ('SIGNED','REVOKED','CONSUMED')),
            CONSTRAINT ck_signature_seat_positive
                CHECK (seat_index >= 1)
        )
        """
    )

    op.execute(
        f"""
        CREATE INDEX ix_approval_signatures_group
        ON {SCHEMA}.{TABLE} (tenant_id, resource_type, resource_id, action_key, occurred_at)
        """
    )

    # ---------------- append-only 守卫：行级 + 语句级 ----------------
    # 复用 D2a 已建的函数（同 schema 同名），不重复 CREATE——重复执行会报
    # 「function already exists」。本迁移在 D2a 之后，函数必然已存在。
    op.execute(
        f"""
        CREATE TRIGGER trg_approval_signatures_guard
            BEFORE UPDATE OR DELETE ON {SCHEMA}.{TABLE}
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_approval_signatures_no_truncate
            BEFORE TRUNCATE ON {SCHEMA}.{TABLE}
            FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_truncate()
        """
    )

    # ---------------- 「有签署必有事件」 ----------------
    # SECURITY DEFINER：读 outbox 需要绕过业务角色在 outbox 的 RLS 可见性；
    # 同时比 event_id 与 tenant_id：没有 RLS 保护时，谓词必须自己同宽，
    # 否则跨租户签署链被缝合（D2a codex 评审 T2 的同一形态）。
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.assert_signature_event_exists() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, {OUTBOX_SCHEMA}
        AS $$
        DECLARE
            found_event uuid;
        BEGIN
            SELECT event_id INTO found_event
            FROM {OUTBOX_SCHEMA}.{OUTBOX_TABLE}
            WHERE event_id = NEW.event_id
              AND tenant_id = NEW.tenant_id;

            IF found_event IS NULL THEN
                RAISE EXCEPTION
                    '签署事实 % 指向的事件 % 不存在于租户 %：签署与事件必须同事务写入同一租户',
                    NEW.signature_id, NEW.event_id, NEW.tenant_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE CONSTRAINT TRIGGER trg_approval_signatures_event_exists
            AFTER INSERT ON {SCHEMA}.{TABLE}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.assert_signature_event_exists()
        """
    )

    # ---------------- 权限与 RLS ----------------
    # 授权只能由矩阵生成（DATA-6）：本迁移只调用 converge_statements 执行，
    # 不手写任何 GRANT。tables 参数把范围限定为本表——schema USAGE 与默认权限
    # 由生成器全量产出（与 c1a2 重复无妨，判据按「角色×对象×权限」折叠终态比对）。
    from packages.shared.db_grants import DOMAINS, converge_statements
    from packages.shared.db_roles import domain_role

    for domain in DOMAINS:
        for statement in converge_statements(
            domain_role(domain), domain, tables=frozenset({TABLE})
        ):
            op.execute(statement)

    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{TABLE}
        ON {SCHEMA}.{TABLE}
        USING ({_tenant_predicate()})
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{TABLE} ON {SCHEMA}.{TABLE}")
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_approval_signatures_event_exists "
        f"ON {SCHEMA}.{TABLE}"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.assert_signature_event_exists()")
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_approval_signatures_no_truncate ON {SCHEMA}.{TABLE}"
    )
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_approval_signatures_guard ON {SCHEMA}.{TABLE}"
    )
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{TABLE}")


__all__: Sequence[str] = ("downgrade", "upgrade")
