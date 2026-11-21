"""批次十一验收：三条镜头修订引用边的真实跨项目阻断。

V1 目标键（剧本版本/约束包）存在，可作为四列复合外键目标。
V2 三条外键存在且 convalidated；存量 NULL 不违例。
V3 合法引用写入成功（影响行数 + 回读）。
V4 跨项目借用他项目的剧本版本被 23503 拒，回读未变。
V5 不存在的版本被拒，回读未变。
V6 downgrade / upgrade 可逆。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).parent))
import lab  # noqa: E402
from verify_batch6 import HOST, PORT, PWD, TEMPLATE, USER  # noqa: E402
from verify_batch9 import record, results  # noqa: E402

LAB = "lab_shot_revision_edges"
SR = "shot_planning.shot_revisions"
SCRIPT = "project_planning.script_revisions"
ROOT = Path(__file__).resolve().parents[3]
MIGS = ROOT / "data/migrations/pending/feat-ownership"


async def clone_row(c: asyncpg.Connection, table: str, where: str,
                    overrides: dict[str, object]) -> None:
    """整行复制一个夹具行，仅覆盖身份/归属列。

    比 insert_minimal_row 更合适这里：projects 有 varchar(8) 列，通用占位
    `probe-<col>` 会超长，报错看起来像迁移坏了；复制真实行只换 project_id，
    正好把「唯一差异是项目」这一变异条件钉死。
    """
    cols = [r["column_name"] for r in await c.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=split_part($1,'.',1) AND table_name=split_part($1,'.',2) "
        "ORDER BY ordinal_position", table)]
    # 覆盖值是脚本内常量且只含安全字符；用字面量可避免 INSERT…SELECT 中
    # 参数类型无法推断（IndeterminateDatatypeError），不必给每列写 ::cast。
    exprs = [f"'{overrides[col]}'" if col in overrides else f"src.{col}"
             for col in cols]
    await c.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"SELECT {', '.join(exprs)} FROM {table} src WHERE {where}"
    )


def load(rev: str, pattern: str):
    hits = sorted(MIGS.glob(f"{rev}_*.py"))
    assert len(hits) == 1, (rev, hits)
    return lab.load_migration(hits[0], f"mig_{pattern}")


async def main() -> int:
    m10 = load("n4b5c6d7e8f9", "b10")
    m11 = load("o5c6d7e8f9a0", "b11")
    admin = await lab.admin_conn(HOST, PORT, USER, PWD)
    try:
        await lab.fresh_clone(admin, LAB, TEMPLATE)
        c = await lab.lab_conn(HOST, PORT, USER, PWD, LAB)
        try:
            for sql in m10.upgrade_sqls():
                await c.execute(sql)
            for sql in m11.upgrade_sqls():
                await c.execute(sql)

            keys = await c.fetch(
                "SELECT conname FROM pg_constraint WHERE conname = ANY($1)",
                [f"uq_{t}_entity_project_tenant_org" for t in
                 ("script_revisions", "constraint_packs")],
            )
            record("V1", len(keys) == 2, "两个目标四列唯一键存在",
                   f"实测 {len(keys)}/2")

            fks = {
                r["conname"]: r["convalidated"]
                for r in await c.fetch(
                    "SELECT conname, convalidated FROM pg_constraint "
                    "WHERE conname = ANY($1)",
                    [n for n, _, _ in m11.EDGES],
                )
            }
            record("V2", len(fks) == 3 and all(fks.values()),
                   "三条复合外键存在且 convalidated",
                   f"{fks}")

            # 归属锚点从存量夹具实读：script_revisions 自身还有
            # (project_id, episode_ref) → episodes 的外键，占位假 ref 会误报。
            base = await c.fetchrow(
                f"SELECT project_id, tenant_id, organization_id, episode_ref "
                f"FROM {SR} LIMIT 1"
            )
            anchor = dict(base)
            # 合法锚点：同项目同租户同组织的剧本版本
            same = await lab.insert_minimal_row(
                c, SCRIPT, entity_id="sr-b11-ok", entity_type="ScriptRevision",
                status="DRAFT", project_id=anchor["project_id"],
                tenant_id=anchor["tenant_id"],
                organization_id=anchor["organization_id"],
                episode_ref=anchor["episode_ref"],
            )
            tag = await c.execute(
                f"UPDATE {SR} SET basis_script_revision_id=$1 "
                "WHERE entity_id='rev-gt-mismatch'", same["entity_id"]
            )
            readback = await c.fetchval(
                f"SELECT basis_script_revision_id FROM {SR} "
                "WHERE entity_id='rev-gt-mismatch'"
            )
            record("V3", tag.endswith("1") and readback == "sr-b11-ok",
                   "合法引用写入成功，影响行数=1 且回读已变",
                   f"tag={tag}, 回读={readback}")

            # 跨项目借用：造一个同租户同组织、但属于另一项目的真实剧本版本。
            # 只换 project_id 才测得到复合外键；若 tenant/org 也换，拦它的
            # 可能是别的外键，变异就失去了定位能力。
            other_project = "proj-b11-other"
            await clone_row(
                c, "project_planning.projects", "src.entity_id='proj-alpha'",
                {"entity_id": other_project, "project_id": other_project},
            )
            await clone_row(
                c, "project_planning.episodes", "src.entity_id='ep-alpha-001'",
                {"entity_id": "ep-b11-other", "project_id": other_project,
                 "episode_ref": "EP-B11"},
            )
            other = await lab.insert_minimal_row(
                c, SCRIPT, entity_id="sr-b11-other", entity_type="ScriptRevision",
                status="DRAFT", project_id=other_project,
                tenant_id=anchor["tenant_id"],
                organization_id=anchor["organization_id"],
                episode_ref="EP-B11",
            )
            rejected = False
            try:
                await c.execute(
                    f"UPDATE {SR} SET basis_script_revision_id=$1 "
                    "WHERE entity_id='rev-gt-mismatch'", other["entity_id"]
                )
            except asyncpg.ForeignKeyViolationError as exc:
                rejected = "fk_sr_basis_script" in str(exc)
            readback = await c.fetchval(
                f"SELECT basis_script_revision_id FROM {SR} "
                "WHERE entity_id='rev-gt-mismatch'"
            )
            record("V4", rejected and readback == "sr-b11-ok",
                   "跨项目借用被复合外键拒绝，回读未变",
                   f"拒绝={rejected}, 回读={readback}")

            rejected = False
            try:
                await c.execute(
                    f"UPDATE {SR} SET basis_script_revision_id='sr-does-not-exist' "
                    "WHERE entity_id='rev-gt-mismatch'"
                )
            except asyncpg.ForeignKeyViolationError:
                rejected = True
            readback = await c.fetchval(
                f"SELECT basis_script_revision_id FROM {SR} "
                "WHERE entity_id='rev-gt-mismatch'"
            )
            record("V5", rejected and readback == "sr-b11-ok",
                   "不存在的修订被拒，回读未变",
                   f"拒绝={rejected}, 回读={readback}")

            for sql in m11.downgrade_sqls():
                await c.execute(sql)
            gone = await c.fetchval(
                "SELECT count(*) FROM pg_constraint WHERE conname = ANY($1)",
                [n for n, _, _ in m11.EDGES],
            )
            for sql in m11.upgrade_sqls():
                await c.execute(sql)
            back = await c.fetchval(
                "SELECT count(*) FROM pg_constraint WHERE conname = ANY($1)",
                [n for n, _, _ in m11.EDGES],
            )
            record("V6", gone == 0 and back == 3,
                   "downgrade 撤掉三条外键，upgrade 可重放",
                   f"撤后={gone}, 重放后={back}")
        finally:
            await c.close()
    finally:
        await lab.drop_lab(admin, LAB)
        await admin.close()

    print(f"\n批次十一：{len(results)} 项")
    return 0 if all(r["status"] == "PASS" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
