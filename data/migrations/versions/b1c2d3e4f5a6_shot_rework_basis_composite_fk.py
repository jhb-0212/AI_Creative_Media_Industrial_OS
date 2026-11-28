"""Shot 返工依据跨项目归属收口：单列外键升级为复合外键（P1-03）。

为什么单独一个迁移，而不是改 a13b0
------------------------------------------------------------
迁移是时间胶囊：a13b0 已在测试/生产库应用，改它的 CREATE TABLE 只对新库生效，
存量库永远停留在单列外键——闸门全绿而跨项目借用仍然可能。所以：
- a13b0 的 CREATE TABLE 同步修正（新库一次建对）；
- 本迁移在 a13b0 之后**对存量库做等价升级**，两条路径终态一致。

不变量
------------------------------------------------------------
``shots(project_id, rework_basis_ref) → rework_tasks(project_id, entity_id)``：
单列外键只证明返工任务存在，A 项目可拿 B 项目的授权单号回退（同租户同组织下
多个项目时）。复合外键让数据库拒绝，不依赖 Service 记得比 project_id。

幂等与 fail-closed
------------------------------------------------------------
每步先查 pg_constraint：已存在且定义等价则跳过；同名但列集不等价则拒绝
（fail-closed，不静默让一个语义不同的约束冒充本迁移的目标）。复合外键按
「先建目标唯一键 → 删旧单列键 → NOT VALID 挂新键 → VALIDATE」顺序，
NOT VALID 只豁免历史行、新写入立即受检。

Revision ID: b1c2d3e4f5a6
Revises: a13b0c1d2e3f
Create Date: 2026-11-21
"""

from __future__ import annotations

from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "a13b0c1d2e3f"
branch_labels = None
depends_on = None

_SCHEMA = "project_planning"
_SHOTS = "shots"
_REWORK = "rework_tasks"
_UQ = "uq_rework_tasks_entity_project"
_OLD_FK = "fk_shots_rework_basis"  # 同名复用：从单列升级为复合


def _has_constraint(table: str, name: str, contype: str) -> bool:
    from sqlalchemy import text

    row = op.get_bind()
    result = row.execute(
        text(
            """
            SELECT 1 FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = :schema AND t.relname = :table
              AND c.conname = :name AND c.contype::text = :contype
            """
        ),
        {"schema": _SCHEMA, "table": table, "name": name, "contype": contype},
    ).first()
    return result is not None


def _assert_no_conflicting_constraint() -> None:
    """同名约束存在但列集不等价时 fail-closed，不让别名冒充目标。"""
    from sqlalchemy import text

    bind = op.get_bind()
    # (table, name, 类型, 可接受列集列表)。旧外键两种形态都合法：
    # 单列 (rework_basis_ref) 是待升级的存量；复合是升级后或新库的终态。
    # 除此之外任何列集都是同名不同义，fail-closed。
    checks = (
        (_REWORK, _UQ, "u", [("project_id", "entity_id")]),
        (
            _SHOTS,
            _OLD_FK,
            "f",
            [("rework_basis_ref",), ("project_id", "rework_basis_ref")],
        ),
    )
    for table, name, contype, accepted in checks:
        rows = (
            bind.execute(
                text(
                    """
                SELECT a.attname
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                WHERE n.nspname = :schema AND t.relname = :table
                  AND c.conname = :name AND c.contype::text = :contype
                ORDER BY k.ord
                """
                ),
                {"schema": _SCHEMA, "table": table, "name": name, "contype": contype},
            )
            .scalars()
            .all()
        )
        if rows and tuple(rows) not in accepted:
            raise RuntimeError(
                f"{_SCHEMA}.{table} 的约束 {name} 列集为 {tuple(rows)}，"
                f"可接受 {accepted}——同名不同义，拒绝升级以免静默保留错误约束"
            )


def upgrade() -> None:
    # 先 fail-closed 检查同名约束语义，再动手。
    _assert_no_conflicting_constraint()

    # 1. 目标唯一键：复合外键的引用目标。entity_id 是 envelope 主键，
    #    (project_id, entity_id) 唯一恒成立，但 PG 要求显式键。
    if not _has_constraint(_REWORK, _UQ, "u"):
        op.create_unique_constraint(_UQ, _REWORK, ["project_id", "entity_id"], schema=_SCHEMA)

    # 2. 删旧单列外键（若仍是单列）。同名复用是刻意的：约束名不变，
    #    测试/台账按名字找外键时不会因升级多出一个别名。
    bind = op.get_bind()
    from sqlalchemy import text

    old_cols = (
        bind.execute(
            text(
                """
            SELECT a.attname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE n.nspname = :schema AND t.relname = :table
              AND c.conname = :name AND c.contype::text = 'f'
            ORDER BY k.ord
            """
            ),
            {"schema": _SCHEMA, "table": _SHOTS, "name": _OLD_FK},
        )
        .scalars()
        .all()
    )
    if old_cols and tuple(old_cols) != ("project_id", "rework_basis_ref"):
        # 单列旧键：删掉重建。_assert_no_conflicting_constraint 已保证不是
        # 另一个语义的复合键。
        op.drop_constraint(_OLD_FK, _SHOTS, schema=_SCHEMA)

    # 3. 挂复合外键：新库（a13b0 已建复合）此时 old_cols 已是目标列集，跳过；
    #    存量库删旧键后此处新建。NOT VALID 让存量行不阻塞，新写入立即受检。
    if not _has_constraint(_SHOTS, _OLD_FK, "f"):
        op.execute(
            f"ALTER TABLE {_SCHEMA}.{_SHOTS} "
            f"ADD CONSTRAINT {_OLD_FK} FOREIGN KEY (project_id, rework_basis_ref) "
            f"REFERENCES {_SCHEMA}.{_REWORK} (project_id, entity_id) NOT VALID"
        )
    # 4. VALIDATE：证明存量无违例（rework_basis_ref 为 NULL 的行外键不检查，
    #    非空行必须同项目）。省略这步等于「我不知道有没有脏数据」。
    op.execute(f"ALTER TABLE {_SCHEMA}.{_SHOTS} VALIDATE CONSTRAINT {_OLD_FK}")


def downgrade() -> None:
    """回到单列外键；目标唯一键保留（它本身是正确的领域约束，删了是反向破坏）。"""
    from sqlalchemy import text

    bind = op.get_bind()
    cols = (
        bind.execute(
            text(
                """
            SELECT a.attname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE n.nspname = :schema AND t.relname = :table
              AND c.conname = :name AND c.contype::text = 'f'
            ORDER BY k.ord
            """
            ),
            {"schema": _SCHEMA, "table": _SHOTS, "name": _OLD_FK},
        )
        .scalars()
        .all()
    )
    if not cols:
        return
    op.drop_constraint(_OLD_FK, _SHOTS, schema=_SCHEMA)
    op.create_foreign_key(
        _OLD_FK,
        _SHOTS,
        _REWORK,
        ["rework_basis_ref"],
        ["entity_id"],
        source_schema=_SCHEMA,
        referent_schema=_SCHEMA,
    )
