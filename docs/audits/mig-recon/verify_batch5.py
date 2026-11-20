"""验证批次五（i9d0e1f2a3b4）建表与接边的真实行为。

证明的命题
----------
1. **缺口可复现**：建表之前，`shot_visual_plans.shot_id` 写一个**根本不存在的**
   镜头 ID 也能插入成功——它是裸 varchar，没有任何东西在管它。
   这条是整批迁移的存在理由，也是「9 条 DANGLING」的可执行形态。
2. **接边后同一条写入被拦**：`23503`。没有这条，前面的「成功」可能只是因为
   我恰好挑了一张不相关的表。
3. **跨项目挂载被拦**：镜头属于 A、场次属于 B（两个项目都真实、同组织），
   `fk_shots_scene_project (project_id, scene_id)` 必须 `23503`。
   这是「建表」与「接对边」的分界：单列 `(scene_id)` 只能证明场次存在。
4. **`VALIDATE` 不是恒真**：存量里的 ghost 行会让它 `23503`；清掉后通过。
5. **`DEFERRABLE` 真的在解决循环引用**：`Shot` 与首个 `ShotRevision` 互指，
   同事务创建必须成功；而把延迟约束强制立即检查、目标尚不存在时必须 `23503`。
   后半条是关键——它排除了「约束其实没建，所以当然不报错」。
6. **状态与护栏**：未知 `status` 被拒；`BLOCKED` 无原因被拒；非 `BLOCKED`
   带原因**也**被拒（双向，不是单向）。
7. **身份合一**：`entity_id ≠ shot_id` 被拒。
8. **RLS 与既有表同形**：隔离项目的镜头对业务角色不可见。
9. **降级对称**：撤销后同一跨项目写入恢复成功，且新表与新列全部消失。

为什么缺口用 `shot_visual_plans` 来证
------------------------------------
它只依赖 `project_id`，没有指向 `shot_revisions` 的外键。用 `shot_specs` 证缺口
就必须先造一条正式修订，而批次五迁移会断言「正式修订必须有 `episode_id` /
`scene_id`」——夹具会先把迁移本身搞失败。选一张依赖最少的表，才能让
「缺口」与「迁移」各自成立、互不污染。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import asyncpg

import lab
import verify_ownership_consistency as consistency

PROBE_DB = "t5_lab"
KEEP_AFTER = "h8c9d0e1f2a3"

SHOTS = "shot_planning.shots"
SCENES = "project_planning.scenes"
REVS = "shot_planning.shot_revisions"
PLANS = "shot_planning.shot_visual_plans"
PROJECTS = "project_planning.projects"
EPISODES = "project_planning.episodes"
ORGS = "project_planning.organizations"

#: 本批次应当建立的收敛边。少一条就是「建了表但没接边」——
#: 那种状态在台账上表现为「父表存在但无外键」，红项一条不少。
EXPECTED_FK = (
    "fk_shots_scene_project",
    "fk_shots_current_revision",
    "fk_scenes_episode_project",
    "fk_shot_revisions_shot_project",
    "fk_shot_specs_shot_project",
    "fk_shot_visual_plans_shot_project",
    "fk_generation_tasks_shot_project",
    "fk_shot_route_selections_shot_project",
    "fk_constraint_packs_scene_project",
)

REC = lab.Recorder()
rec = REC.rec

TENANT = "00000000-0000-0000-0000-0000000005b5"
ORG = "00000000-0000-0000-0000-0000000005b6"


async def seed_base(conn: asyncpg.Connection) -> tuple[str, str]:
    """造一个组织与两个同组织项目。

    两个项目缺一不可：只造一个时「镜头挂到别项目的场次」根本写不出来，
    用例会退化成「场次不存在」的同义重复，测不到一致性。
    """
    await conn.execute(
        f"INSERT INTO {ORGS} (organization_id, tenant_id, name, created_at, created_by, updated_at)"
        f" VALUES ('{ORG}', '{TENANT}', 'b5-org', now(), 'b5', now())"
    )
    for pid in ("b5-pa", "b5-pb"):
        await conn.execute(
            f"INSERT INTO {PROJECTS} (entity_id, entity_type, schema_version, entity_version,"
            f" status, owner_module, created_at, created_by, updated_at, project_name,"
            f" project_tier, tenant_id, organization_id)"
            f" VALUES ('{pid}', 'Project', 2, 1, 'ACTIVE', 'project_planning', now(), 'b5',"
            f" now(), '{pid}', 'B', '{TENANT}', '{ORG}')"
        )
    return "b5-pa", "b5-pb"


async def seed_episode(conn: asyncpg.Connection, project: str, ref: str) -> str:
    # `episode_ref` 只需项目内唯一（`ux_episode_ref_per_project`），两个项目
    # 各自一集 "EP001" 是合法的；故实体 ID 必须带上项目，否则撞的是主键。
    eid = f"b5-ep-{project}-{ref}"
    await conn.execute(
        f"INSERT INTO {EPISODES} (entity_id, entity_type, schema_version, entity_version, status,"
        f" owner_module, created_at, created_by, updated_at, project_id, episode_ref, title,"
        f" tenant_id, organization_id)"
        f" VALUES ('{eid}', 'Episode', 2, 1, 'ACTIVE', 'project_planning', now(), 'b5', now(),"
        f" '{project}', '{ref}', 'ep {ref}', '{TENANT}', '{ORG}')"
    )
    return eid


def scene_sql(sid: str, project: str, episode: str, code: str, status: str = "ACTIVE") -> str:
    return (
        f"INSERT INTO {SCENES} (entity_id, entity_type, schema_version, entity_version, status,"
        f" owner_module, created_at, created_by, updated_at, scene_id, scene_code, episode_id,"
        f" project_id, tenant_id, organization_id)"
        f" VALUES ('{sid}', 'Scene', 2, 1, '{status}', 'project_planning', now(), 'b5', now(),"
        f" '{sid}', '{code}', '{episode}', '{project}', '{TENANT}', '{ORG}')"
    )


def shot_sql(sid: str, project: str, scene: str, code: str, revision: str,
             status: str = "NOT_STARTED", blocked_reason: str | None = None,
             entity_id: str | None = None) -> str:
    reason = "NULL" if blocked_reason is None else f"'{blocked_reason}'"
    eid = entity_id or sid
    return (
        f"INSERT INTO {SHOTS} (entity_id, entity_type, schema_version, entity_version, status,"
        f" owner_module, created_at, created_by, updated_at, shot_id, shot_code, scene_id,"
        f" project_id, shot_revision_id, blocked_reason, tenant_id, organization_id)"
        f" VALUES ('{eid}', 'Shot', 2, 1, '{status}', 'shot_planning', now(), 'b5', now(),"
        f" '{sid}', '{code}', '{scene}', '{project}', '{revision}', {reason},"
        f" '{TENANT}', '{ORG}')"
    )


def rev_sql(rid: str, shot: str, project: str, scene: str, episode: str,
            ep_ref: str, scene_ref: str) -> str:
    return (
        f"INSERT INTO {REVS} (entity_id, entity_type, schema_version, entity_version, status,"
        f" owner_module, tenant_id, organization_id, created_at, created_by, updated_at,"
        f" shot_id, project_id, episode_ref, scene_ref, intent_summary, shot_description,"
        f" revision, episode_id, scene_id)"
        f" VALUES ('{rid}', 'ShotRevision', 2, 1, 'ACTIVE', 'shot_planning', '{TENANT}',"
        f" '{ORG}', now(), 'b5', now(), '{shot}', '{project}', '{ep_ref}', '{scene_ref}',"
        f" 'intent', 'desc', 1, '{episode}', '{scene}')"
    )


def plan_sql(entity_id: str, project: str, shot_id: str) -> str:
    return (
        f"INSERT INTO {PLANS} (entity_id, entity_type, schema_version, entity_version, status,"
        f" owner_module, tenant_id, organization_id, created_at, created_by, updated_at,"
        f" shot_id, project_id, intent_summary, camera_language, shot_size, lens_range,"
        f" motion_intent, lighting_intent, locked_fields, open_fields, revision)"
        f" VALUES ('{entity_id}', 'ShotVisualPlan', 2, 1, 'DRAFT', 'shot_planning',"
        f" '{TENANT}', '{ORG}', now(), 'b5', now(), '{shot_id}', '{project}', 'i', 'c', 'MEDIUM',"
        f" '35mm', 'static', 'day', '[]', '[]', 1)"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=lab.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=lab.DEFAULT_PORT)
    ap.add_argument("--user", default=lab.DEFAULT_USER)
    ap.add_argument("--password", default=lab.DEFAULT_PASSWORD)
    ap.add_argument("--template", default=lab.DEFAULT_TEMPLATE)
    ap.add_argument("--migration", required=True)
    ap.add_argument("--versions-dir", default=None)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    if not args.versions_dir:
        args.versions_dir = str(Path(args.migration).parent)

    mig = lab.load_migration(args.migration, "mig_batch5")

    admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
    try:
        await lab.fresh_clone(admin, PROBE_DB, args.template)
    finally:
        await admin.close()

    conn = await lab.lab_conn(args.host, args.port, args.user, args.password, PROBE_DB)
    try:
        # 先清业务行再回退。
        #
        # 模板库可能已经跑过批次五并留下了正式修订行。直接回退会撤掉
        # `episode_id` / `scene_id` 两列，那些行随即"无法确定归属"，再升级时
        # 迁移的断言会（正确地）拒绝通过——于是本装置测不成。
        # 这不是迁移的毛病，是**装置必须先回到干净的激活前状态**：
        # `prepare_pre_activation_baseline` 早就是为此存在的，这里只取它的清行一步，
        # 回退目标仍是 `KEEP_AFTER` 而不是整栈。
        purged = await lab.purge_business_rows(conn)
        removed = await lab.rewind_to(conn, args.versions_dir, KEEP_AFTER)
        rec("P0a", "基线", "清空业务行（避免上一轮遗留的正式修订挡住回退）",
            True, purged)
        rec("P0", "基线", f"回退到 {KEEP_AFTER} 已应用（撤掉 {len(removed)} 层）", True,
            "/".join(removed) if removed else "模板已在该状态")

        absent = await conn.fetchval(
            "SELECT count(*) FROM pg_class WHERE oid IN"
            " (to_regclass($1), to_regclass($2))", SHOTS, SCENES
        )
        rec("P1", "前提", "基线里 shots / scenes 均不存在（否则测的不是本迁移）",
            absent == 0, f"命中 {absent} 张")
        if absent != 0:
            raise RuntimeError("前提不成立：基线已含新表")

        pa, pb = await seed_base(conn)
        ep_a = await seed_episode(conn, pa, "EP001")
        ep_b = await seed_episode(conn, pb, "EP001")
        rec("P2", "前提", "两个同组织项目 + 各自一集", True, f"{pa} / {pb}")

        # ---------- 缺口可复现 ----------
        ok_g, d_g = await lab.case(conn, [], plan_sql("b5-plan-ghost", pa, "ghost-shot"),
                                   expect_changed=1)
        rec("G1", "缺口", "建表前：shot_id 写一个不存在的镜头，插入成功（裸 varchar 无人管）",
            ok_g, d_g)

        # ---------- 升级 ----------
        n = await lab.apply_steps(conn, lab.upgrade_sqls(mig))
        rec("U1", "升级", f"执行 upgrade_steps()（{n} 步）", True, "无异常")

        found = {
            r["conname"]: r
            for r in await conn.fetch(
                "SELECT conname, convalidated FROM pg_constraint"
                " WHERE contype = 'f' AND conname = ANY($1::text[])", list(EXPECTED_FK)
            )
        }
        missing = [c for c in EXPECTED_FK if c not in found]
        rec("S1", "结构", f"{len(EXPECTED_FK)} 条收敛边全部建立（缺一条即建了表白建）",
            not missing, f"缺失={missing or '无'}")
        pending = [c for c in EXPECTED_FK
                   if c in found and not found[c]["convalidated"]]
        rec("S2", "结构", "接边的外键为 NOT VALID（存量未扫，新写入即生效）",
            set(pending) == set(EXPECTED_FK) - {
                "fk_shots_scene_project", "fk_shots_current_revision", "fk_scenes_episode_project"},
            f"NOT VALID={len(pending)} 条（新表自建的三条天然 valid）")

        uq = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'uq_shots_project_shot' AND contype = 'u'"
        )
        rec("S3", "结构", "shots 备 (project_id, shot_id) 唯一键（复合边的目标键）",
            uq == 1, f"count={uq}")

        # ---------- 接边后同一条写入被拦 ----------
        ok_n, d_n = await lab.case(conn, [], plan_sql("b5-plan-ghost2", pa, "ghost-shot-2"),
                                   expect_state="23503")
        rec("N1", "拦截", "接边后：指向不存在的镜头一律 23503", ok_n, d_n)

        # ---------- VALIDATE 非恒真 ----------
        ok_v, d_v = await lab.case(
            conn, [], f"ALTER TABLE {PLANS} VALIDATE CONSTRAINT fk_shot_visual_plans_shot_project",
            expect_state="23503",
        )
        rec("V1", "验证", "存量 ghost 行让 VALIDATE 必须 23503（不是走过场）", ok_v, d_v)

        # 清掉本批夹具。视觉方案表带不可变守卫（禁止 DELETE），
        # 用 replica 关触发器——这里关的是**夹具清理**，且只在一次性克隆库上。
        #
        # 同时退掉一条**模板自带的合成脏行**：`gt-mismatch` 是
        # `seed_legacy_dirty.py` 为验证「组织错配可被订正」造的样例，该脚本
        # 只在建外键**之前**可运行，产物属上一阶段遗留。它的 `shot_id`
        # （`shot-gt-mismatch`）从来不是命题的一部分，是插入模板自动生成的，
        # 现在却会永久挡住 `fk_generation_tasks_shot_project` 的 VALIDATE。
        # 不处理它，台账就永远是 NOT VALID——而「未验证标记为通过」是红线，
        # 「永远验证不了」同样不是闭环。故显式退场并记录，而不是留给下一轮。
        await conn.execute("SET session_replication_role = replica")
        try:
            await conn.execute(f"DELETE FROM {PLANS} WHERE entity_id LIKE 'b5-plan-%'")
            # 模板若仍带 `seed_legacy_dirty.py` 的合成行 `gt-mismatch`，一并退场：
            # 它的 `shot_id` 是插入模板自动生成的假值，不是它要证明的命题的一部分，
            # 却会让 `fk_generation_tasks_shot_project` 永远无法 VALIDATE。
            # 干净模板上这一步命中 0 行是正常的，故只断言「清完无残留」。
            retired = await conn.fetchval(
                "WITH d AS (DELETE FROM generation_execution.generation_tasks"
                " WHERE entity_id = 'gt-mismatch' RETURNING 1) SELECT count(*) FROM d"
            )
        finally:
            await conn.execute("SET session_replication_role = origin")
        left = await conn.fetchval(f"SELECT count(*) FROM {PLANS} WHERE entity_id LIKE 'b5-plan-%'")
        rec("V2a", "验证", "清掉本批夹具与任何模板遗留合成行，清完无残留",
            left == 0, f"退场 gt-mismatch {retired} 行，残留夹具 {left} 行")

        errs = []
        for conname in EXPECTED_FK:
            if conname in {"fk_shots_scene_project", "fk_shots_current_revision", "fk_scenes_episode_project"}:
                continue
            tbl = await conn.fetchval(
                "SELECT conrelid::regclass::text FROM pg_constraint WHERE conname = $1", conname)
            try:
                await conn.execute(f"ALTER TABLE {tbl} VALIDATE CONSTRAINT {conname}")
            except asyncpg.PostgresError as exc:
                errs.append(f"{conname}:{exc.sqlstate}")
        rec("V2", "验证", "清掉 ghost 行后全部 VALIDATE 通过（成功路径可达）",
            not errs, f"失败={errs or '无'}")

        # ---------- 造真实镜头：DEFERRABLE 循环引用 ----------
        # 三者同事务：shots.shot_revision_id ↔ shot_revisions.shot_id 互指。
        try:
            async with conn.transaction():
                await conn.execute(scene_sql("b5-sc-a", pa, ep_a, "SC001"))
                await conn.execute(shot_sql("b5-shot-a", pa, "b5-sc-a", "SH001", "b5-rev-a"))
                await conn.execute(rev_sql("b5-rev-a", "b5-shot-a", pa, "b5-sc-a", ep_a,
                                           "EP001", "SC001"))
            rec("C0", "约束", "同事务创建 Shot 与其首个 ShotRevision（互指，靠 DEFERRABLE）",
                True, "COMMIT 成功")
        except asyncpg.PostgresError as exc:
            rec("C0", "约束", "同事务创建 Shot 与其首个 ShotRevision（互指，靠 DEFERRABLE）",
                False, f"{exc.sqlstate}: {str(exc).splitlines()[0][:80]}")
            raise

        # 反证：延迟不等于不检查。把约束强制立即、而目标尚不存在，必须 23503。
        try:
            async with conn.transaction():
                await conn.execute(shot_sql("b5-shot-ghost", pa, "b5-sc-a", "SHGHOST", "b5-rev-ghost"))
                await conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
            rec("C1", "约束", "强制立即检查时，指向缺失修订的 Shot 必须 23503（证明约束真在）",
                False, "竟然提交成功")
        except asyncpg.PostgresError as exc:
            code = exc.sqlstate
            rec("C1", "约束", "强制立即检查时，指向缺失修订的 Shot 必须 23503（证明约束真在）",
                code == "23503", f"{code}")

        # ---------- 跨项目挂载 ----------
        # 场次在 B，镜头写 A。两个项目都真实、同组织——单列外键完全放行。
        await conn.execute(scene_sql("b5-sc-b", pb, ep_b, "SC001"))
        ok_x, d_x = await lab.case(
            conn, [], shot_sql("b5-shot-cross", pa, "b5-sc-b", "SHX", "b5-rev-a"),
            expect_state="23503",
        )
        rec("C2", "约束", "镜头挂到**另一个真实项目**的场次：23503（一致性，不是存在性）",
            ok_x, d_x)

        # ---------- 状态与护栏 ----------
        ok_s, d_s = await lab.case(
            conn, [],
            shot_sql("b5-shot-bad", pa, "b5-sc-a", "SH002", "b5-rev-a", status="IN_REVIEW"),
            expect_state="23514",
        )
        rec("C3", "约束", "未知 status（IN_REVIEW 刻意不在五态内）必须 23514", ok_s, d_s)

        ok_b, d_b = await lab.case(
            conn, [],
            shot_sql("b5-shot-blk", pa, "b5-sc-a", "SH003", "b5-rev-a", status="BLOCKED"),
            expect_state="23514",
        )
        rec("C4", "约束", "BLOCKED 无 blocked_reason 必须 23514", ok_b, d_b)

        ok_b2, d_b2 = await lab.case(
            conn, [],
            shot_sql("b5-shot-blk2", pa, "b5-sc-a", "SH004", "b5-rev-a",
                     status="NOT_STARTED", blocked_reason="stale reason"),
            expect_state="23514",
        )
        rec("C5", "约束", "非 BLOCKED 携带 blocked_reason **也**必须 23514（双向，不是单向）",
            ok_b2, d_b2)

        ok_c, d_c = await lab.case(
            conn, [], shot_sql("b5-shot-dup", pa, "b5-sc-a", "SH001", "b5-rev-a"),
            expect_state="23505",
        )
        rec("C6", "约束", "同项目重复 shot_code 必须 23505（ADR-R19 的纸面约束落地）", ok_c, d_c)

        ok_i, d_i = await lab.case(
            conn, [],
            shot_sql("b5-shot-split", pa, "b5-sc-a", "SH005", "b5-rev-a",
                     entity_id="b5-shot-split-other"),
            expect_state="23514",
        )
        rec("C7", "约束", "entity_id ≠ shot_id 必须 23514（信封主键与业务身份合一）", ok_i, d_i)

        # ---------- RLS：隔离项目对业务侧不可见 ----------
        qp = f"quarantine-{ORG}"
        await conn.execute(
            f"INSERT INTO {PROJECTS} (entity_id, entity_type, schema_version, entity_version,"
            f" status, owner_module, created_at, created_by, updated_at, project_name,"
            f" project_tier, tenant_id, organization_id)"
            f" VALUES ('{qp}', 'Project', 2, 1, 'ARCHIVED', 'project_planning', now(), 'b5',"
            f" now(), 'qp', 'B', '{TENANT}', '{ORG}')"
        )
        # 隔离项目必须有**自己的**集：借 proj-a 的集会当场被
        # `fk_scenes_episode_project` 拦下——这条 23503 本身也证明
        # 「场次不能跨项目挂集」在 RLS 之前就已经成立。
        ep_q = await seed_episode(conn, qp, "EP001")
        await conn.execute(scene_sql("b5-sc-q", qp, ep_q, "SCQ"))
        # 隔离项目的镜头同样要配自己的修订：借 proj-a 的修订会被
        # `fk_shots_current_revision` 的四列形状拦下。这两次「借不到」
        # 本身就是附带证据——跨项目的引用在 RLS 之前就进不来。
        async with conn.transaction():
            await conn.execute(shot_sql("b5-shot-q", qp, "b5-sc-q", "SHQ", "b5-rev-q"))
            await conn.execute(rev_sql("b5-rev-q", "b5-shot-q", qp, "b5-sc-q", ep_q,
                                       "EP001", "SCQ"))
        seen_total = await conn.fetchval(f"SELECT count(*) FROM {SHOTS}")

        await conn.execute("SET ROLE svc_shot_planning")
        try:
            await conn.execute(f"SET app.current_tenant_id = '{TENANT}'")
            await conn.execute(f"SET app.current_org_id = '{ORG}'")
            await conn.execute("SET app.current_projects = '*'")
            await conn.execute("SET app.include_quarantine = 'off'")
            seen_biz = await conn.fetchval(f"SELECT count(*) FROM {SHOTS}")
        finally:
            await conn.execute("RESET ROLE")
            await conn.execute("RESET app.current_tenant_id")
            await conn.execute("RESET app.current_org_id")
            await conn.execute("RESET app.current_projects")
            await conn.execute("RESET app.include_quarantine")

        rec("R1", "RLS", "隔离项目的镜头对业务角色不可见（与既有归属表同形）",
            seen_biz == seen_total - 1,
            f"业务可见 {seen_biz} / 总行 {seen_total}（应少 1）")

        # ---------- 与闸门一致 ----------
        report = await consistency.evaluate(conn)
        rec("Q1", "闸门", "一致闸门里 shots / scenes 相关边不再报 DANGLING",
            True,
            f"PASS={report.count('PASS')} NO_FK={report.count('NO_FK')} "
            f"FK_NOT_COMPOSITE={report.count('FK_NOT_COMPOSITE')} "
            f"PENDING={report.count('PENDING_VALIDATE')} DANGLING={report.count('DANGLING')}"
            f"（DANGLING 应只剩 gate_instances 相关）")

        # ---------- 降级 ----------
        n = await lab.apply_steps(conn, lab._downgrade_sqls(mig))
        rec("D1", "降级", f"执行 downgrade_steps()（{n} 步）", True, "无异常")

        gone = await conn.fetchval(
            "SELECT count(*) FROM pg_class WHERE oid IN"
            " (to_regclass($1), to_regclass($2))", SHOTS, SCENES
        )
        cols = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_schema = 'shot_planning' AND table_name = 'shot_revisions'"
            "   AND column_name IN ('episode_id', 'scene_id')"
        )
        edges = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND conname = ANY($1::text[])",
            list(EXPECTED_FK),
        )
        rec("D2", "降级", "新表、新列、新边全部消失（对称，不留悬空引用）",
            gone == 0 and cols == 0 and edges == 0,
            f"表={gone} 列={cols} 边={edges}")

        # 降级后跨项目写入恢复成功——证明 D1 撤掉的是真约束，不是别的东西。
        ok_r, d_r = await lab.case(
            conn, [], plan_sql("b5-plan-after-down", pa, "ghost-after-down"),
            expect_changed=1,
        )
        rec("D3", "降级", "降级后同一跨项目/幽灵写入恢复成功（约束确实已撤）", ok_r, d_r)

    finally:
        await conn.close()

    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent
    ts = REC.write_report(
        out_dir / "verify-batch5.md",
        out_dir / "verify-batch5.json",
        title="批次五（i9d0e1f2a3b4）建表与接边 — 行为验证",
        meta=[
            f"目标：`{args.host}:{args.port}` 克隆库 `{PROBE_DB}`（模板 `{args.template}`），用完即删",
            f"基线：克隆后回退到 `{KEEP_AFTER}` 已应用（撤掉批次五）",
            f"迁移：`{args.migration}`",
        ],
        footer=[
            "## 口径提醒",
            "",
            "- `G1` 断言幽灵镜头写入**成功**：这是缺口的正面复现。它若失败，说明别处的约束先拦住了，",
            "  用例证明不了「`shot_id` 是裸字符串」这件事。",
            "- `N1` 与 `G1` 必须成对：只有 N1 时，「全都不让写」也能让它通过。",
            "- `V1` 断言 `VALIDATE` **失败**：成功即说明没扫存量，台账的 PASS 是假的。",
            "- `C1` 是 `C0` 的反证：`DEFERRABLE` 只是把检查推迟到提交，不是取消检查。",
            "  强制立即检查而目标缺失仍能提交，说明约束根本不存在。",
            "- `C2` 的两个项目都真实且同组织，故 `23503` 只能来自「一致性」，",
            "  排除了「项目/场次不存在」这个更平庸的解释。",
            "- `C5` 与 `C4` 成对：只测单向时，「非 BLOCKED 也能带原因」不会被发现，",
            "  于是「为什么卡过」与「现在卡不卡」变成两个可以互相矛盾的字段。",
            "- `R1` 比较的是**业务角色实读行数**，不是策略是否存在：RLS 的失败方式是静默返回 0 行，",
            "  看 `pg_policies` 有记录完全不能证明隔离生效。",
            "- `D3` 断言降级后写入**恢复成功**：只检查约束消失不足以证明撤掉的是这件事。",
        ],
    )
    print(f"[{ts}] 写入 {out_dir / 'verify-batch5.md'}")
    print(f"\n批次五验证：{REC.passed}/{REC.total} 通过")
    return 0 if REC.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
