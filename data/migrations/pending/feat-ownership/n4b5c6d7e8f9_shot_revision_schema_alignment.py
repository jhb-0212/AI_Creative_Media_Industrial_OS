"""批次十：让 `shot_planning.shot_revisions` 真正对齐契约 schema。

为什么这是 Shot 域的前置而不是可选项
------------------------------------
ADR-R17 §缺口 2 已裁定「没有运行时，也没有修订表」，A5 把它登记为显式债务。
本轮实库核对（`ai_film_industrial` @ m3b4c5d6e7f8）确认差距比台账写的更具体：

* 表只有 21 列，契约 `ShotRevision.required` 有 30+ 字段；
* `description` / `narrative_intent` / `risk` / `intent_ref` / `confirmed_by`
  等 20 个字段完全不存在；
* 旧列 `shot_description` / `intent_summary` / `revision` / `episode_ref` /
  `scene_ref` 是草案期列名，不在契约对象里。

在这种表上写 `SHOT.REVISION.CONFIRM` 只会得到「服务声称成功、库里没有契约要求
的事实」——比不做更坏。所以先补表，再开入口，顺序不可颠倒。

存量行的处置（不洗白）
----------------------
当前唯一存量行 `rev-gt-mismatch` 是为外键验收造的夹具（`created_by='backfill'`），
不是真实的人类确认修订。可从旧列映射的字段逐列映射；契约新增而库里没有语义来源
的字段，用契约允许的**最小合法值**回填，并在 `change_summary` 与 `provenance`
里留下显式痕迹。这不是「补出一段假历史」：该行的 `confirmed_by='LEGACY_BACKFILL'`
明确表明它没有人类确认证据，任何把它当作 Gate 1 准入依据的下游都会读到哨兵值。

为什么直接建 validated 约束
----------------------------
本批新增约束的存量行在回填后已满足全部 CHECK。ADR-R40 的 NOT VALID 流程适用于
「约束建立时存量可能违例」的归属外键；此处没有这种历史，伪造「待验证」状态
反而会让台账说谎。

SQL 单一事实源
--------------
``upgrade_sqls()`` / ``downgrade_sqls()`` 是语句序列的唯一定义处，Alembic 入口与
验收装置（``lab.upgrade_sqls``）都从这里取。装置不手抄一遍 DROP——手抄的脚本
会在迁移改动后静默漂移，最后证明的是副本而不是被测迁移。

Revision ID: n4b5c6d7e8f9
Revises: m3b4c5d6e7f8
"""

from __future__ import annotations

from alembic import op

revision = "n4b5c6d7e8f9"
down_revision = "m3b4c5d6e7f8"
branch_labels = None
depends_on = None

TABLE = "shot_planning.shot_revisions"

# 契约新增列的 DDL 投影。事实源是 object_schemas.schema.json::ShotRevision。
# (name, ddl_type, default_sql)：DEFAULT 只用于让存量行有合法值，随后 DROP。
NEW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("description", "text", "'legacy backfill: no pre-contract description'"),
    ("narrative_intent", "text", "'legacy backfill: no pre-contract intent'"),
    ("character_refs", "jsonb", "'[]'::jsonb"),
    ("location_ref", "varchar(256)", "'UNSPECIFIED'"),
    ("prop_refs", "jsonb", "'[]'::jsonb"),
    ("constraint_pack_applicability", "varchar(32)", "'NOT_APPLICABLE'"),
    ("constraint_pack_id", "varchar(64)", "NULL"),
    ("creative_autonomy", "varchar(8)", "'L1'"),
    ("risk", "jsonb", '\'{"level": "LOW"}\'::jsonb'),
    ("quality_profile", "varchar(32)", "'UNIFIED_HIGH_QUALITY'"),
    (
        "intent_ref",
        "jsonb",
        '\'{"intent_id": "LEGACY_BACKFILL", "intent_version": 0}\'::jsonb',
    ),
    ("basis_script_revision_id", "varchar(64)", "NULL"),
    ("script_anchors", "jsonb", "'[\"LEGACY_BACKFILL\"]'::jsonb"),
    ("source_draft_ref", "jsonb", "NULL"),
    ("source_candidate_refs", "jsonb", "'[]'::jsonb"),
    ("change_origin", "varchar(32)", "'MANUAL_EDIT'"),
    ("supersedes_shot_revision_id", "varchar(64)", "NULL"),
    ("change_summary", "text", "'legacy backfill: fields absent from pre-contract rows'"),
    ("changed_fields", "jsonb", "'[]'::jsonb"),
    ("provenance", "jsonb", "'{\"legacy_backfill\": true}'::jsonb"),
    ("confirmed_by", "varchar(64)", "'LEGACY_BACKFILL'"),
    ("confirmed_at", "timestamptz", "now()"),
    ("revision_no", "integer", "1"),
)

# 可空列分两类，都由**可机检的 CHECK 前提**约束，不是「留空也行」：
# - 真正可空（source_draft_ref / change_summary / supersedes：契约允许首版为空）；
# - 引用列（basis_script_revision_id / constraint_pack_id）：存量夹具行没有真实
#   目标可指（script_revisions / constraint_packs 均 0 行），编造一个 LEGACY_*
#   锚点就是造假的引用事实。故存量为 NULL，由 ck_sr_basis_script /
#   ck_sr_constraint_pack 保证**新写入必须有值或 applicability 一致**。
#   这是带前提的豁免：前提一旦被下游用作真实依据，哨兵值 confirmed_by 可见。
NULLABLE = frozenset(
    {
        "basis_script_revision_id",
        "constraint_pack_id",
        "source_draft_ref",
        "supersedes_shot_revision_id",
        "change_summary",
    }
)

# 枚举与最小结构约束直接来自 schema 的 enum / minItems / required。
CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_sr_constraint_applicability",
        "constraint_pack_applicability IN ('BOUND', 'NOT_APPLICABLE')",
    ),
    ("ck_sr_creative_autonomy", "creative_autonomy IN ('L1', 'L2', 'L3', 'L4')"),
    (
        "ck_sr_risk_level",
        "risk ? 'level' AND risk->>'level' IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')",
    ),
    ("ck_sr_quality_profile", "quality_profile = 'UNIFIED_HIGH_QUALITY'"),
    (
        "ck_sr_intent_ref",
        "intent_ref ? 'intent_id' AND intent_ref ? 'intent_version'",
    ),
    ("ck_sr_script_anchors", "jsonb_array_length(script_anchors) >= 1"),
    (
        "ck_sr_change_origin",
        "change_origin IN ('BREAKDOWN_CONFIRM', 'PROMOTION', 'MANUAL_EDIT', 'BRANCH_MERGE')",
    ),
    ("ck_sr_changed_fields", "jsonb_typeof(changed_fields) = 'array'"),
    ("ck_sr_provenance", "jsonb_typeof(provenance) = 'object'"),
    ("ck_sr_revision_no", "revision_no >= 1"),
    # 新写入必须有真实剧本依据；仅 legacy 夹具行可空，且它的 confirmed_by
    # 哨兵值明确标记了「这不是人类确认的修订」。
    (
        "ck_sr_basis_script",
        "confirmed_by = 'LEGACY_BACKFILL' OR basis_script_revision_id IS NOT NULL",
    ),
    # 契约：BOUND 必填 constraint_pack_id，NOT_APPLICABLE 必须留空。
    # 双向，防止「留空既可能是没约束、也可能是忘了绑」。
    (
        "ck_sr_constraint_pack",
        "(constraint_pack_applicability = 'BOUND' AND constraint_pack_id IS NOT NULL) OR "
        "(constraint_pack_applicability = 'NOT_APPLICABLE' AND constraint_pack_id IS NULL)",
    ),
)

# 草案期列名 → 契约列名。DROP 旧列而不是留双份：留着就成了两个可以同时
# 不一致的「同一件事」，而数据库只拦得住写入时一致。
RENAMED_FROM: tuple[tuple[str, str], ...] = (
    ("shot_description", "description"),
    ("intent_summary", "narrative_intent"),
    ("revision", "revision_no"),
)

LEGACY_SUMMARY = "legacy backfill: fields absent from pre-contract rows"


def upgrade_sqls() -> list[str]:
    sqls: list[str] = []
    for name, ddl_type, default_sql in NEW_COLUMNS:
        sqls.append(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl_type} DEFAULT {default_sql}")

    # 存量订正：能从草案列映射的三列取真实旧值，其余保持最小合法默认；
    # 轨迹落在 change_summary / provenance，可被下游查询识别。
    sqls.append(
        f"UPDATE {TABLE} SET "
        "description = COALESCE(shot_description, description), "
        "narrative_intent = COALESCE(intent_summary, narrative_intent), "
        "revision_no = COALESCE(revision, revision_no), "
        f"change_summary = COALESCE(change_summary, '{LEGACY_SUMMARY}'), "
        "provenance = COALESCE(provenance, '{\"legacy_backfill\": true}'::jsonb)"
    )

    for name, _, _ in NEW_COLUMNS:
        if name not in NULLABLE:
            sqls.append(f"ALTER TABLE {TABLE} ALTER COLUMN {name} SET NOT NULL")
        # DEFAULT 不留在列上：每次写入必须显式提供契约字段，不能靠默认值降级。
        sqls.append(f"ALTER TABLE {TABLE} ALTER COLUMN {name} DROP DEFAULT")

    for constraint, expr in CHECKS:
        sqls.append(f"ALTER TABLE {TABLE} ADD CONSTRAINT {constraint} CHECK ({expr})")

    # episode_ref / scene_ref 也属草案字段，但批次五回填逻辑引用过它们；
    # 本批不删，留待单独迁移，避免在本批制造未经裁定的范围扩张。
    for old, _ in RENAMED_FROM:
        sqls.append(f"ALTER TABLE {TABLE} DROP COLUMN {old}")
    return sqls


def downgrade_sqls() -> list[str]:
    sqls: list[str] = []
    for constraint, _ in CHECKS:
        sqls.append(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {constraint}")

    for old, new in RENAMED_FROM:
        ddl_type = "integer" if old == "revision" else "text"
        sqls.append(f"ALTER TABLE {TABLE} ADD COLUMN {old} {ddl_type}")
        sqls.append(f"UPDATE {TABLE} SET {old} = {new}")

    for name, _, _ in reversed(NEW_COLUMNS):
        sqls.append(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {name}")
    return sqls


def upgrade() -> None:
    for sql in upgrade_sqls():
        op.execute(sql)


def downgrade() -> None:
    for sql in downgrade_sqls():
        op.execute(sql)
