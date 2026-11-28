"""批次十验收：`shot_planning.shot_revisions` 与契约 schema 的真实对齐。

命题（每条都能单独失败）：

B1  契约 `ShotRevision.required` 的全部字段落表。
    `shot_revision_id` 是 `entity_id` 的只读投影（与 Scene 同一条身份铁律），
    按契约要求由领域对象输出，不落独立列；这是**显式且可机检**的例外。
B2  必填列全部 NOT NULL；契约允许 null 的列保持 nullable。
B3  本批全部 CHECK 存在且 convalidated——不允许「待验证」伪造状态。
B4  存量夹具行按口径订正：三列取真实旧值，哨兵值写明无人类确认证据。
B5  变异：非法 creative_autonomy 被 CHECK 拒绝（23514），回读未变。
B6  变异：必填列写 NULL 被 NOT NULL 拒绝（23502），回读未变。
B7  正向：按契约全字段写入成功，影响行数 1 且回读值已变。
B8  可逆：downgrade 撤掉新列并恢复草案列，再 upgrade 成功。

判定三态：PASS / FAIL，无 N-A。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).parent))
import lab
from verify_batch6 import HOST, PORT, PWD, TEMPLATE, USER
from verify_batch9 import record, results

LAB = "lab_shot_revision_alignment"
TABLE = "shot_planning.shot_revisions"
ROOT = Path(__file__).resolve().parents[3]

# 身份投影：与 `Scene.scene_id = entity_id` 同一条铁律。领域对象以 property
# 输出该字段；数据库只保留 entity_id 一份身份，避免两套可以互相矛盾的 ID。
PROJECTION_FIELDS = frozenset({"shot_revision_id"})


def contract() -> set[str]:
    schema = json.loads(
        (ROOT / "contracts/schemas/object_schemas.schema.json").read_text(encoding="utf-8")
    )
    return set(schema["definitions"]["ShotRevision"]["required"])


def load_migration():
    path = (
        ROOT
        / "data/migrations/pending/feat-ownership"
        / "n4b5c6d7e8f9_shot_revision_schema_alignment.py"
    )
    return lab.load_migration(path, "mig_batch10_under_test")


async def column_info(c: asyncpg.Connection) -> dict[str, dict]:
    rows = await c.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema='shot_planning' AND table_name='shot_revisions'"
    )
    return {r["column_name"]: dict(r) for r in rows}


async def insert_full_row(c: asyncpg.Connection, entity_id: str) -> str:
    """按契约全字段写一行；返回事务标签，用于「影响行数 + 回读」断言。

    归属列刻意从存量夹具行实读，不写死假 UUID：复合外键要求
    (project_id, tenant_id, organization_id) 指向真实项目，夹具填「像 ID
    的假值」会让整组写入静默失败，报出来还像是本迁移的 CHECK 有问题。
    """
    anchor = await c.fetchrow(
        f"SELECT project_id, tenant_id, organization_id, episode_id, "
        f"scene_id, episode_ref, scene_ref, shot_id FROM {TABLE} "
        "WHERE entity_id='rev-gt-mismatch'"
    )
    cols = (
        "entity_id, entity_type, schema_version, entity_version, status, "
        "owner_module, created_at, created_by, updated_at, tenant_id, "
        "organization_id, episode_ref, scene_ref, "
        "shot_id, project_id, episode_id, scene_id, "
        "description, narrative_intent, character_refs, location_ref, prop_refs, "
        "constraint_pack_applicability, creative_autonomy, risk, quality_profile, "
        "intent_ref, basis_script_revision_id, script_anchors, source_candidate_refs, "
        "change_origin, changed_fields, provenance, confirmed_by, confirmed_at, "
        "revision_no"
    )
    vals = (
        f"'{entity_id}', 'ShotRevision', 1, 1, 'ACTIVE', 'shot_planning', now(), "
        f"'batch10', now(), '{anchor['tenant_id']}', '{anchor['organization_id']}', "
        f"'{anchor['episode_ref']}', '{anchor['scene_ref']}', "
        f"'{anchor['shot_id']}', '{anchor['project_id']}', "
        f"'{anchor['episode_id']}', '{anchor['scene_id']}', "
        "'d', 'n', '[]'::jsonb, 'loc', '[]'::jsonb, "
        "'NOT_APPLICABLE', 'L1', '{\"level\": \"LOW\"}'::jsonb, "
        "'UNIFIED_HIGH_QUALITY', "
        '\'{"intent_id": "i", "intent_version": 1}\'::jsonb, '
        "'sr-base', '[\"EP001:1\"]'::jsonb, '[]'::jsonb, 'MANUAL_EDIT', "
        "'[]'::jsonb, '{}'::jsonb, 'director', now(), 1"
    )
    return await c.execute(f"INSERT INTO {TABLE} ({cols}) VALUES ({vals})")


async def main() -> int:
    want = contract() - PROJECTION_FIELDS
    mod = load_migration()
    admin = await lab.admin_conn(HOST, PORT, USER, PWD)
    try:
        await lab.fresh_clone(admin, LAB, TEMPLATE)
        c = await lab.lab_conn(HOST, PORT, USER, PWD, LAB)
        try:
            for sql in mod.upgrade_sqls():
                await c.execute(sql)

            cols = await column_info(c)

            # B1 字段覆盖
            missing = sorted(want - set(cols))
            record(
                "B1",
                not missing,
                "契约 required 字段（身份投影除外）全部落表",
                f"缺: {missing or '无'}；投影例外: {sorted(PROJECTION_FIELDS)}",
            )

            # B2 NOT NULL 与 nullable 双向比对
            nullable = {
                "basis_script_revision_id",
                "constraint_pack_id",
                "source_draft_ref",
                "supersedes_shot_revision_id",
                "change_summary",
                "audit_ref",
            }
            wrong = [
                name for name in want if (cols[name]["is_nullable"] == "YES") != (name in nullable)
            ]
            record(
                "B2",
                not wrong,
                "必填列 NOT NULL、可空列保持 nullable",
                f"不符: {wrong or '无'}",
            )

            # B3 CHECK 全部存在且 validated
            names = [n for n, _ in mod.CHECKS]
            rows = await c.fetch(
                "SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY($1)",
                names,
            )
            got = {r["conname"]: r["convalidated"] for r in rows}
            bad = sorted(set(names) - set(got)) + [n for n, v in got.items() if not v]
            record(
                "B3",
                not bad,
                f"{len(names)} 条 CHECK 存在且 convalidated",
                f"异常: {bad or '无'}",
            )

            # B4 存量订正
            legacy = await c.fetchrow(
                f"SELECT description, narrative_intent, revision_no, confirmed_by, "
                f"change_summary, provenance, basis_script_revision_id "
                f"FROM {TABLE} WHERE entity_id='rev-gt-mismatch'"
            )
            legacy_basis_is_null = legacy is not None and legacy["basis_script_revision_id"] is None
            ok = (
                legacy is not None
                and legacy["description"]
                == "placeholder shot created so that generation_tasks.gt-mismatch has a real referent"
                and legacy["narrative_intent"] == "backfill parent for gt-mismatch"
                and legacy["revision_no"] == 1
                and legacy["confirmed_by"] == "LEGACY_BACKFILL"
                and "legacy backfill" in legacy["change_summary"]
                # asyncpg 默认把 jsonb 解成字符串，需显式解码（同 uuid 返回 UUID 对象）
                and json.loads(legacy["provenance"]).get("legacy_backfill") is True
                # 目标表无真实锚点：存量夹具的引用列为 NULL，不编造 LEGACY_* 外键
                and legacy_basis_is_null
            )
            record(
                "B4",
                bool(ok),
                "存量夹具按真实旧值映射 + 哨兵值留痕",
                f"description={legacy['description'] if legacy else None!r}, "
                f"confirmed_by={legacy['confirmed_by'] if legacy else None!r}",
            )

            # B5 变异：把合法行改成非法枚举，必须被 CHECK 拒绝且回读未变。
            # 判据是「约束名 + 回读值」，不是「有没有抛异常」——RLS 把越权写
            # 静默过滤成 UPDATE 0 时，只看异常会假绿。
            before = await c.fetchval(
                "SELECT count(*) FROM pg_constraint WHERE conname='ck_sr_creative_autonomy'"
            )
            await insert_full_row(c, "shot-b10-bad-enum")
            rejected = False
            try:
                await c.execute(
                    f"UPDATE {TABLE} SET creative_autonomy='L9' WHERE entity_id='shot-b10-bad-enum'"
                )
            except asyncpg.CheckViolationError as exc:
                rejected = "ck_sr_creative_autonomy" in str(exc)
            after = await c.fetchval(
                f"SELECT creative_autonomy FROM {TABLE} WHERE entity_id='shot-b10-bad-enum'"
            )
            record(
                "B5",
                rejected and before == 1 and after == "L1",
                "非法枚举被 CHECK 拒绝，且被测行未被改动",
                f"CHECK 存在={before}, 拦截={rejected}, 回读={after}",
            )

            # B6 变异：必填列 NULL
            try:
                await c.execute(
                    f"UPDATE {TABLE} SET confirmed_by=NULL WHERE entity_id='shot-b10-bad-enum'"
                )
                rejected = False
            except asyncpg.NotNullViolationError:
                rejected = True
            readback = await c.fetchval(
                f"SELECT confirmed_by FROM {TABLE} WHERE entity_id='shot-b10-bad-enum'"
            )
            record(
                "B6",
                rejected and readback == "director",
                "必填列 NULL 被拒，回读未变",
                f"拒绝={rejected}, 回读={readback!r}",
            )

            # B7 正向写入（影响行数 + 回读）
            tag = await insert_full_row(c, "shot-b10-ok")
            rows = await c.fetchval(f"SELECT count(*) FROM {TABLE} WHERE entity_id='shot-b10-ok'")
            value = await c.fetchval(
                f"SELECT description FROM {TABLE} WHERE entity_id='shot-b10-ok'"
            )
            record(
                "B7",
                tag.endswith("1") and rows == 1 and value == "d",
                "按契约全字段写入成功，影响行数=1 且回读生效",
                f"tag={tag}, 行数={rows}, 回读={value!r}",
            )

            # B9 条件 CHECK：legacy 夹具可空，新写入必须有剧本依据。
            # 这一条是「可空」不退化成「随便空」的机检前提。
            await insert_full_row(c, "shot-b10-basis")
            basis_rejected = False
            try:
                await c.execute(
                    f"UPDATE {TABLE} SET basis_script_revision_id=NULL "
                    "WHERE entity_id='shot-b10-basis'"
                )
            except asyncpg.CheckViolationError as exc:
                basis_rejected = "ck_sr_basis_script" in str(exc)
            basis_readback = await c.fetchval(
                f"SELECT basis_script_revision_id FROM {TABLE} WHERE entity_id='shot-b10-basis'"
            )
            record(
                "B9",
                basis_rejected and basis_readback == "sr-base",
                "新写入清空剧本依据被拒；仅 legacy 夹具可空",
                f"拒绝={basis_rejected}, 回读={basis_readback!r}",
            )

            # B8 可逆性
            for sql in mod.downgrade_sqls():
                await c.execute(sql)
            cols_after = await column_info(c)
            reverted = (
                "description" not in cols_after
                and "shot_description" in cols_after
                and "revision" in cols_after
            )
            for sql in mod.upgrade_sqls():
                await c.execute(sql)
            cols_again = await column_info(c)
            reapplied = "description" in cols_again and "revision_no" in cols_again
            record(
                "B8",
                reverted and reapplied,
                "downgrade 恢复草案列、upgrade 可再次应用",
                f"回退={reverted}, 重放={reapplied}",
            )
        finally:
            await c.close()
    finally:
        await lab.drop_lab(admin, LAB)
        await admin.close()

    print(f"\n批次十：{len(results)} 项")
    return 0 if all(r["status"] == "PASS" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
