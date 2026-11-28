"""资产登记 Saga 的库表与约束（ADR-R42 / C1、C2、C4、C5）。

本迁移只做「契约已经声明、且能由数据库独立守住」的部分：

- ``artifact_manifests``：T0 的写入意图。``(generation_task_id, attempt_id)``
  唯一，同一次尝试只能有一条意图；``locator`` 与 ``sha256`` 必须自洽
  （复用受控定位符的同一份 SQL 判据，不在这里重写正则）。
- ``asset_registration_receipts``：T2 的不可变回执。``(tenant_id, generation_task_id)``
  唯一构成业务幂等键；触发器禁止 UPDATE/DELETE/TRUNCATE。尚无回执时发现内容
  冲突写 REJECTED，已有回执则复用原结果并另记冲突审计事实（ADR-R42 §4.3），
  不新建第二个资产，也不能原地改写。
- ``generation_tasks.status`` 的 CHECK 由 4 值扩到 5 值，纳入
  ``AWAITING_ASSET_REGISTRATION``；``production_routes`` 补
  ``quality_profile_version``，使质量策略能在任务创建时冻结。

为什么两张新表都带 ``(project_id, tenant_id)`` 复合外键
--------------------------------------------------------
主链口径是两列（``organization_id`` 只存在于 projects 等少数表，由父项目
派生）。单列 ``project_id`` 外键允许「A 租户的任务挂到 B 租户的项目」；
复合外键把租户一致性变成数据库事实。两张表的 ``generation_task_id``
也通过三列复合外键指向生成任务，拒绝跨任务/跨租户伪造登记依据。

``NOT VALID`` 的使用范围
------------------------
新表在建表语句内直接带外键，PostgreSQL 会立即打上 ``convalidated=true``
（``CREATE TABLE`` 内不能写 NOT VALID），台账须如实反映这一点，不得伪称
「待验证」。存量表 ``generation_tasks`` 的状态 CHECK 是收紧前的旧规则，
扩展后用 ``NOT VALID → VALIDATE`` 两段，避免长时间持有全表锁。

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_grants import (
    ASSET_SERVICE,
    PRODUCTION_PLANNING,
    converge_statements,
)
from packages.shared.db_roles import domain_role
from packages.shared.rls import tenant_scope_predicate
from packages.shared.status_constraint import status_check_sql
from packages.shared.storage_locator import sql_locator_check

revision = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GEN = "production_planning"
_ASSET = "asset_service"
_GEN_TASKS = f"{_GEN}.generation_tasks"
_ROUTES = f"{_GEN}.production_routes"
_MANIFESTS = f"{_GEN}.artifact_manifests"
_RECEIPTS = f"{_ASSET}.asset_registration_receipts"

_TASK_KEY = "uq_generation_tasks_task_project_tenant"
_ASSET_KEY = "uq_asset_versions_project_entity"
_MANIFEST_STATUS_CK = "ck_artifact_manifests_status"
_MANIFEST_LOCATOR_CK = "ck_artifact_manifests_locator_sha256"
_RECEIPT_STATUS_CK = "ck_asset_registration_receipts_status"
_RECEIPT_COMPLETION_CK = "ck_asset_registration_receipts_completion"
_TASKS_STATUS_CK = "ck_generation_tasks_status"

# 与 GenerationTaskStatus（services/project_planning/domain/enums.py）逐值一致；
# 迁移是时间胶囊，不 import 运行时枚举——今天的枚举改了，历史迁移的语义不能跟着变。
_TASK_STATUSES = (
    "AWAITING_ASSET_REGISTRATION",
    "FAILED",
    "REQUESTED",
    "RUNNING",
    "SUCCEEDED",
)
_OLD_TASK_STATUSES = ("FAILED", "REQUESTED", "RUNNING", "SUCCEEDED")
_MANIFEST_STATUSES = ("PREPARED", "STORED_VERIFIED")
_RECEIPT_STATUSES = ("COMPLETED", "REJECTED")

_RECEIPT_GUARD_FN = "asset_service.reject_receipt_mutation"
_RECEIPT_GUARD_TRG = "trg_asset_registration_receipts_immutable"
_RECEIPT_TRUNCATE_TRG = "trg_asset_registration_receipts_no_truncate"


def _envelope_columns() -> list[sa.Column]:
    """C.3.1 通用信封列，与 packages.shared.orm.EntityMixin 同口径。"""
    return [
        sa.Column("entity_id", sa.String(64), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("entity_version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner_module", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_ref", sa.String(128), nullable=True),
    ]


def _visibility(schema: str, table: str) -> str:
    """租户谓词 + 父项目存在性（与 R16 cutover 迁移同一口径）。"""
    return (
        f"USING ({tenant_scope_predicate()} AND EXISTS ("
        f"SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {schema}.{table}.project_id "
        f"AND p.tenant_id = {schema}.{table}.tenant_id))"
    )


def _task_fk(name: str) -> sa.ForeignKeyConstraint:
    """指向生成任务的复合外键：跨任务/跨租户伪造登记依据都必须被拒。"""
    return sa.ForeignKeyConstraint(
        ["generation_task_id", "project_id", "tenant_id"],
        [
            f"{_GEN_TASKS}.generation_task_id",
            f"{_GEN_TASKS}.project_id",
            f"{_GEN_TASKS}.tenant_id",
        ],
        name=name,
    )


def _project_fk(table: str) -> sa.ForeignKeyConstraint:
    """归属复合外键（主链两列口径）。"""
    return sa.ForeignKeyConstraint(
        ["project_id", "tenant_id"],
        ["project_planning.projects.entity_id", "project_planning.projects.tenant_id"],
        name=f"fk_{table}_project_tenant",
    )


def _indexes(schema: str, table: str) -> tuple[sa.Index, ...]:
    # tenant_id 索引名必须与 EntityMixin 的 index=True 自动生成名逐字一致
    # （含 schema 前缀），否则 alembic check 会把同一列报成「缺一个索引」。
    return (
        sa.Index(f"ix_{table}_project_id", "project_id"),
        sa.Index(f"ix_{schema}_{table}_tenant_id", "tenant_id"),
        sa.Index(f"ix_{table}_generation_task_id", "generation_task_id"),
    )


def _enable_rls(schema: str, table: str) -> None:
    op.execute(f"ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation_{table} ON {schema}.{table} {_visibility(schema, table)}"
    )
    op.execute(f"ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY")


def _disable_rls(schema: str, table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {schema}.{table}")
    op.execute(f"ALTER TABLE {schema}.{table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    # 目标键先建：复合外键只有指向唯一/主键才成立，否则 42830。
    op.create_unique_constraint(
        _TASK_KEY,
        "generation_tasks",
        ["generation_task_id", "project_id", "tenant_id"],
        schema=_GEN,
    )
    # 同租户跨项目引用资产 ID 也必须被拒：单列主键只能证明「资产存在」，
    # 复合引用还需要资产侧提供 (project_id, entity_id) 唯一目标键。
    op.create_unique_constraint(
        _ASSET_KEY,
        "asset_versions",
        ["project_id", "entity_id"],
        schema=_ASSET,
    )

    op.create_table(
        "artifact_manifests",
        *_envelope_columns(),
        sa.Column("generation_task_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("locator", sa.String(96), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "generation_task_id",
            "attempt_id",
            name="uq_artifact_manifests_task_attempt",
        ),
        sa.UniqueConstraint("project_id", "entity_id", name="uq_artifact_manifests_entity_project"),
        _project_fk("artifact_manifests"),
        _task_fk("fk_artifact_manifests_generation_task"),
        sa.CheckConstraint(status_check_sql(_MANIFEST_STATUSES), name=_MANIFEST_STATUS_CK),
        sa.CheckConstraint(sql_locator_check("locator"), name=_MANIFEST_LOCATOR_CK),
        *_indexes(_GEN, "artifact_manifests"),
        schema=_GEN,
    )
    _enable_rls(_GEN, "artifact_manifests")

    op.create_table(
        "asset_registration_receipts",
        *_envelope_columns(),
        sa.Column("generation_task_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("asset_version_id", sa.String(64), nullable=True),
        sa.Column("message", sa.String(512), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "generation_task_id",
            name="uq_asset_registration_receipts_tenant_task",
        ),
        sa.UniqueConstraint(
            "project_id",
            "entity_id",
            name="uq_asset_registration_receipts_entity_project",
        ),
        _project_fk("asset_registration_receipts"),
        _task_fk("fk_asset_registration_receipts_generation_task"),
        sa.ForeignKeyConstraint(
            ["project_id", "asset_version_id"],
            [f"{_ASSET}.asset_versions.project_id", f"{_ASSET}.asset_versions.entity_id"],
            name="fk_asset_registration_receipts_asset_version",
        ),
        sa.CheckConstraint(status_check_sql(_RECEIPT_STATUSES), name=_RECEIPT_STATUS_CK),
        sa.CheckConstraint(
            "status <> 'COMPLETED' OR (asset_version_id IS NOT NULL AND asset_version_id <> '')",
            name=_RECEIPT_COMPLETION_CK,
        ),
        *_indexes(_ASSET, "asset_registration_receipts"),
        schema=_ASSET,
    )
    _enable_rls(_ASSET, "asset_registration_receipts")

    # 回执不可原地改写。用 RAISE 直接拒绝，不用 RLS 策略：RLS 只覆盖带上下文的
    # 连接，迁移/超管/备份连接根本不经过它。函数只做拒绝、不返回数据，故 DEFINER
    # 不会放大可见性；这里用默认 INVOKER，触发器执行不额外要求 EXECUTE。
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_RECEIPT_GUARD_FN}()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                'asset_registration_receipts 是追加式登记回执，禁止 %（ADR-R42 D5）',
                TG_OP
                USING ERRCODE = 'restrict_violation';
        END; $$;
        """
    )
    op.execute(
        f"CREATE TRIGGER {_RECEIPT_GUARD_TRG} "
        f"BEFORE UPDATE OR DELETE ON {_RECEIPTS} "
        f"FOR EACH ROW EXECUTE FUNCTION {_RECEIPT_GUARD_FN}()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION asset_service.reject_receipt_truncate()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'asset_registration_receipts 禁止 TRUNCATE（ADR-R42 D5）'
                USING ERRCODE = 'restrict_violation';
        END; $$;
        """
    )
    op.execute(
        f"CREATE TRIGGER {_RECEIPT_TRUNCATE_TRG} BEFORE TRUNCATE ON {_RECEIPTS} "
        "FOR EACH STATEMENT EXECUTE FUNCTION asset_service.reject_receipt_truncate()"
    )

    # 存量表：状态 CHECK 扩为 5 值。旧约束是子集，存量必然满足新约束，
    # NOT VALID → VALIDATE 只用于缩短持锁窗口，不是给脏行开豁免。
    op.execute(f"ALTER TABLE {_GEN_TASKS} DROP CONSTRAINT IF EXISTS {_TASKS_STATUS_CK}")
    op.execute(
        f"ALTER TABLE {_GEN_TASKS} ADD CONSTRAINT {_TASKS_STATUS_CK} "
        f"CHECK ({status_check_sql(_TASK_STATUSES)}) NOT VALID"
    )
    op.execute(f"ALTER TABLE {_GEN_TASKS} VALIDATE CONSTRAINT {_TASKS_STATUS_CK}")

    # 质量策略来源：路线创建时冻结，任务创建时搬过去；空串表示未绑定，
    # 准入校验在运行时层（PR-D）fail-closed，这里只保证列真实存在。
    op.add_column(
        "production_routes",
        sa.Column(
            "quality_profile_version",
            sa.String(64),
            nullable=False,
            server_default="",
        ),
        schema=_GEN,
    )

    # 授权只经矩阵：新表若不进矩阵，表权限对域角色为零，功能整体 42501
    # 而 ACL 对账仍可能因为对象没纳入而全绿。tables 是本迁移的显式快照。
    for statement in converge_statements(
        domain_role(PRODUCTION_PLANNING),
        PRODUCTION_PLANNING,
        tables=frozenset({"production_routes", "generation_tasks", "artifact_manifests"}),
    ):
        op.execute(statement)
    for statement in converge_statements(
        domain_role(ASSET_SERVICE),
        ASSET_SERVICE,
        tables=frozenset({"asset_versions", "qc_reports", "asset_registration_receipts"}),
    ):
        op.execute(statement)


_INFLIGHT_TASK_STATUS = "AWAITING_ASSET_REGISTRATION"


def _assert_no_inflight_tasks() -> None:
    """降级前置守卫（ADR-R46 §8 裁决 3）。

    隐式的 ``VALIDATE`` 失败仍是第二道防线；这里把它提前到任何破坏性 DDL 之前，
    让失败可读、可测试，并且不会降到一半才炸。错误文案含机器可检索前缀
    ``[DOWNGRADE_BLOCKED:e1f2a3b4c5d6]``，供 CI/运维按 revision 定位。
    """
    count = (
        op.get_bind()
        .execute(
            sa.text(f"SELECT count(*) FROM {_GEN_TASKS} WHERE status = :status"),
            {"status": _INFLIGHT_TASK_STATUS},
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            f"[DOWNGRADE_BLOCKED:e1f2a3b4c5d6] 在途任务状态不满足降级要求："
            f"{count} 条 generation_tasks 仍为 {_INFLIGHT_TASK_STATUS}。"
            " 处置：先让这些任务收敛（完成登记或显式置为 FAILED），再重跑 downgrade；"
            " 不得直接改 status 绕过。"
        )


def downgrade() -> None:
    _assert_no_inflight_tasks()
    # 恢复旧状态 CHECK；此时任何 AWAITING_ASSET_REGISTRATION 行都会让
    # VALIDATE 失败——这是正确行为：要回退必须先把在途任务处置完。
    op.execute(f"ALTER TABLE {_GEN_TASKS} DROP CONSTRAINT IF EXISTS {_TASKS_STATUS_CK}")
    op.execute(
        f"ALTER TABLE {_GEN_TASKS} ADD CONSTRAINT {_TASKS_STATUS_CK} "
        f"CHECK ({status_check_sql(_OLD_TASK_STATUSES)})"
    )

    op.drop_column("production_routes", "quality_profile_version", schema=_GEN)

    op.execute(f"DROP TRIGGER IF EXISTS {_RECEIPT_TRUNCATE_TRG} ON {_RECEIPTS}")
    op.execute("DROP FUNCTION IF EXISTS asset_service.reject_receipt_truncate()")
    op.execute(f"DROP TRIGGER IF EXISTS {_RECEIPT_GUARD_TRG} ON {_RECEIPTS}")
    op.execute(f"DROP FUNCTION IF EXISTS {_RECEIPT_GUARD_FN}()")

    _disable_rls(_ASSET, "asset_registration_receipts")
    op.drop_table("asset_registration_receipts", schema=_ASSET)
    op.drop_constraint(_ASSET_KEY, "asset_versions", schema=_ASSET, type_="unique")
    _disable_rls(_GEN, "artifact_manifests")
    op.drop_table("artifact_manifests", schema=_GEN)
    op.drop_constraint(_TASK_KEY, "generation_tasks", schema=_GEN, type_="unique")
