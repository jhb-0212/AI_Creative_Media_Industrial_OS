"""批次十一：为批次十新增的三条真实引用列建归属外键。

为什么不是改判据
----------------
批次十对齐契约 schema 后，归属闸门报 6 条 UNCLASSIFIED。逐条核过之后：
3 条是内嵌对象/外部地点（合法非边）；3 条是真实实体引用——剧本依据、约束包、
上一版修订。把它们登记成 NonRef 就是把「跨项目借用剧本/约束包」从判据里删掉，
属于洗白，故必须建外键。

目标键先行（ADR-R40）
---------------------
script_revisions 与 constraint_packs 只有单列主键，无法证明「被引用的版本
属于同一项目/租户/组织」。先建四列唯一键，再挂 NOT VALID 外键；存量三列均为
NULL（目标表本身 0 行，批次十不编造锚点），故订与不订都没有违例，但
NOT VALID → VALIDATE 两步不省——省了就无法证明存量真的干净。

自引用的 supersedes 边同样四列：只允许同一项目的上一版修订被取代。

Revision ID: o5c6d7e8f9a0
Revises: n4b5c6d7e8f9
"""

from __future__ import annotations

from alembic import op

revision = "o5c6d7e8f9a0"
down_revision = "n4b5c6d7e8f9"
branch_labels = None
depends_on = None

SR = "shot_planning.shot_revisions"
SCRIPT = "project_planning.script_revisions"
PACK = "shot_planning.constraint_packs"

TARGET_KEYS: tuple[tuple[str, str], ...] = (
    ("script_revisions", SCRIPT),
    ("constraint_packs", PACK),
)

# (约束名, 本表引用列, 目标表)
EDGES: tuple[tuple[str, str, str], ...] = (
    ("fk_sr_basis_script", "basis_script_revision_id", SCRIPT),
    ("fk_sr_constraint_pack", "constraint_pack_id", PACK),
    ("fk_sr_supersedes", "supersedes_shot_revision_id", SR),
)

OWNERSHIP_COLS = ("project_id", "tenant_id", "organization_id")


def _key_name(table: str) -> str:
    return f"uq_{table}_entity_project_tenant_org"


def upgrade_sqls() -> list[str]:
    sqls: list[str] = []
    for name, table in TARGET_KEYS:
        cols = ", ".join(("entity_id",) + OWNERSHIP_COLS)
        sqls.append(
            f"ALTER TABLE {table} ADD CONSTRAINT {_key_name(name)} UNIQUE ({cols})"
        )
    for constraint, column, target in EDGES:
        cols = ", ".join((column,) + OWNERSHIP_COLS)
        ref = ", ".join(("entity_id",) + OWNERSHIP_COLS)
        sqls.append(
            f"ALTER TABLE {SR} ADD CONSTRAINT {constraint} FOREIGN KEY ({cols}) "
            f"REFERENCES {target} ({ref}) NOT VALID"
        )
    # 存量三列均 NULL，订正语句是显式「无违例」的可执行声明，不是靠推断。
    sqls.append(
        f"UPDATE {SR} SET basis_script_revision_id = NULL "
        "WHERE basis_script_revision_id IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM {SCRIPT} s WHERE s.entity_id = "
        f"{SR}.basis_script_revision_id AND "
        + " AND ".join(f"s.{c} = {SR}.{c}" for c in OWNERSHIP_COLS)
        + ")"
    )
    for constraint, _, _ in EDGES:
        sqls.append(f"ALTER TABLE {SR} VALIDATE CONSTRAINT {constraint}")
    return sqls


def downgrade_sqls() -> list[str]:
    sqls: list[str] = []
    for constraint, _, _ in EDGES:
        sqls.append(f"ALTER TABLE {SR} DROP CONSTRAINT IF EXISTS {constraint}")
    for name, table in reversed(TARGET_KEYS):
        sqls.append(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {_key_name(name)}")
    return sqls


def upgrade() -> None:
    for sql in upgrade_sqls():
        op.execute(sql)


def downgrade() -> None:
    for sql in downgrade_sqls():
        op.execute(sql)
