"""新增审计 schema 与状态迁移事实表（ADR-R23 D2-a）

问题（根因，不是「缺一张表」）
------------------------------
一次状态推进此前只留两处痕：实体上会被下一次迁移覆盖的 ``audit_ref``，和一张
按月分区、保留期到就 DETACH 的 ``outbox.outbox_events``。于是「谁在什么证据下
批准了什么」在保留期之后不可查询——审批事实的生命周期被**传输机制**的保留策略
决定了。ADR-R23 的判据是：事实的生命周期必须由事实的用途决定。

本迁移落地该事实表，并把三条结构性不变量交给数据库，而不是交给「记得写对」：

1. **同一版本只推进一次**：``UNIQUE (tenant_id, resource_type, resource_id,
   entity_version)``。乐观锁已经在 ``save(entity, expected)`` 里保证这点，抬到事实表
   后，并发双写不依赖任何应用层 ``if``。同时 ``CHECK (entity_version >= 1)``：
   版本号 0 或负数描述不了任何真实实体版本（对象 Schema 的下界就是 1），
   NOT NULL 只挡住了「没写」，挡不住「写了个不可能的版本」（codex 评审 T4）。
2. **有迁移必有事件**：``event_id NOT NULL`` + 提交时约束触发器。外键在分区表上
   建不出来（``outbox_events`` 主键为 ``(event_id, occurred_at)``，分区键是
   ``occurred_at``，``event_id`` 单独不唯一），所以用
   ``AFTER INSERT ... DEFERRABLE INITIALLY DEFERRED`` 在 COMMIT 时校验。
   附带收益：校验发生在提交时，写入顺序不再需要人记。
3. **append-only 连 TRUNCATE 一起挡**：``TRUNCATE`` 不触发行级触发器，只写
   ``BEFORE UPDATE OR DELETE`` 等于给整表清空留了门。同时补齐既有
   ``asset_service.qc_reports`` / ``asset_versions`` 的同型缺口——那两张表的守卫
   （``d1e2f3a4b5c6``）也只装了行级，实测可被 TRUNCATE 清空。
   不允许「新表新标准、旧表旧标准」。

写法约束
--------
- ``audit`` 与 ``outbox`` 同为「跨域写入、单一所有者」的证据 schema。
- RLS 同时 ``ENABLE`` 与 ``FORCE``：只 enable 不 force 的话，表所有者/迁移角色
  仍然绕过策略（项目里已踩过一次）。
- 组织维度不在此处落策略（ADR-R23 §4 已登记：随 D3 统一接入），本迁移只做租户维度，
  避免写第二套组织判定。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None

SCHEMA = "audit"
TABLE = "status_transitions"
OUTBOX_SCHEMA = "outbox"
OUTBOX_TABLE = "outbox_events"

# 既有 append-only 表：本次一并补齐 TRUNCATE 守卫（同型缺口）。
_EXISTING_APPEND_ONLY: tuple[tuple[str, str], ...] = (
    ("asset_service", "qc_reports"),
    ("asset_service", "asset_versions"),
)


def _tenant_roles() -> tuple[str, ...]:
    """业务域角色名（来自配置，不写字面量）。

    角色名可被 ``POSTGRES_PROJECT_PLANNING_USER`` /
    ``POSTGRES_ASSET_SERVICE_USER`` 改；写成字面量时，自定义角色名的部署会在
    本迁移的 GRANT 处失败（角色不存在），把部署堵在升级路径上（codex 评审 T5）。
    """
    from packages.shared.db_roles import domain_role_names

    return domain_role_names()


def _tenant_predicate(column: str = "tenant_id") -> str:
    """租户谓词：未注入租户上下文时恒为 false（fail-closed）。

    ``NULLIF(current_setting(...), '')`` 把「未定义」与「空串」都归一为 NULL，
    避免 ``''::uuid`` 转换错误；``x = NULL`` 恒为 NULL → 无行可见/不可插入。
    与 ``8f2a1b3c4d5e`` / ``b2c3d4e5f6a7`` 的同一条写法保持一字不差。
    """
    return (
        f"{column} = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
    )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.execute(
        f"""
        CREATE TABLE {SCHEMA}.{TABLE} (
            transition_id   uuid         PRIMARY KEY,
            tenant_id       uuid         NOT NULL,
            organization_id uuid         NULL,
            resource_type   varchar(64)  NOT NULL,
            resource_id     varchar(64)  NOT NULL,
            entity_version  integer      NOT NULL,
            from_status     varchar(64)  NOT NULL,
            to_status       varchar(64)  NOT NULL,
            owner_action    varchar(64)  NOT NULL,
            actor_subject   varchar(128) NOT NULL,
            authority_ref   varchar(128) NULL,
            evidence_refs   varchar(64)[] NOT NULL DEFAULT '{{}}',
            occurred_at     timestamptz  NOT NULL DEFAULT now(),
            event_id        uuid         NOT NULL,

            CONSTRAINT uq_status_transition_version
                UNIQUE (tenant_id, resource_type, resource_id, entity_version),
            CONSTRAINT ck_status_transition_advances
                CHECK (from_status <> to_status),
            CONSTRAINT ck_status_transition_version_positive
                CHECK (entity_version >= 1)
        )
        """
    )

    op.execute(
        f"""
        CREATE INDEX ix_status_transitions_resource_history
        ON {SCHEMA}.{TABLE} (tenant_id, resource_type, resource_id, occurred_at)
        """
    )

    # ---------------- append-only 守卫：行级 + 语句级 ----------------
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                'append-only 表禁止 % (表 %)', TG_OP, TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_truncate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                'append-only 表禁止 TRUNCATE (表 %)', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )

    op.execute(
        f"""
        CREATE TRIGGER trg_status_transitions_guard
            BEFORE UPDATE OR DELETE ON {SCHEMA}.{TABLE}
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_status_transitions_no_truncate
            BEFORE TRUNCATE ON {SCHEMA}.{TABLE}
            FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_truncate()
        """
    )

    # 既有两表的 TRUNCATE 缺口（实测 d1e2f3a4b5c6 只装了 UPDATE/DELETE）。
    for schema, table in _EXISTING_APPEND_ONLY:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_truncate
                BEFORE TRUNCATE ON {schema}.{table}
                FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_truncate()
            """
        )

    # ---------------- 「有迁移必有事件」 ----------------
    # SECURITY DEFINER 的两个理由（都必须写清，否则后人会当多余而删掉）：
    # 1. 校验函数要读 outbox；调用方是业务角色，在 outbox 上的 RLS 可见性会让
    #    「事件确实存在」被误判为不存在，把正确写入拒掉。
    # 2. 函数属 audit schema（证据域），不应要求业务角色持有 outbox 之外的任何权限。
    # 代价：函数绕过调用方的 RLS，所以判定条件必须自己和调用方的可见性口径同宽，
    # 即**同时比 event_id 与 tenant_id**。只比 event_id 时，租户 A 的事实行可以
    # 指向租户 B 的事件——事实与事件都在、提交也成功，但两个租户的审计链被缝在
    # 一起（codex 评审 T2）。SECURITY DEFINER 函数里的每一条谓词都要按「我现在
    # 没有任何 RLS 保护」来写。
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.assert_transition_event_exists() RETURNS trigger
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
                    '状态迁移事实 % 指向的事件 % 不存在于租户 %：迁移与事件必须同事务写入同一租户',
                    NEW.transition_id, NEW.event_id, NEW.tenant_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE CONSTRAINT TRIGGER trg_status_transitions_event_exists
            AFTER INSERT ON {SCHEMA}.{TABLE}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.assert_transition_event_exists()
        """
    )

    # ---------------- 权限与 RLS ----------------
    roles = ", ".join(_tenant_roles())
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {roles}")
    op.execute(f"GRANT INSERT, SELECT ON {SCHEMA}.{TABLE} TO {roles}")
    # 显式收回变更权：即使将来有人误加默认权限，也不至于静默获得改历史的能力。
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {SCHEMA}.{TABLE} FROM {roles}")
    op.execute(f"REVOKE ALL ON {SCHEMA}.{TABLE} FROM PUBLIC")

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
        f"DROP TRIGGER IF EXISTS trg_status_transitions_event_exists "
        f"ON {SCHEMA}.{TABLE}"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.assert_transition_event_exists()")

    for schema, table in _EXISTING_APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON {schema}.{table}")

    op.execute(
        f"DROP TRIGGER IF EXISTS trg_status_transitions_no_truncate ON {SCHEMA}.{TABLE}"
    )
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_status_transitions_guard ON {SCHEMA}.{TABLE}"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_truncate()")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_mutation()")

    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{TABLE}")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")


__all__: Sequence[str] = ("downgrade", "upgrade")
