"""批次六验收：归档场次的「不得留下在制品」不变量。

命题（每条都能单独失败）：

G1  迁移前，归档一个仍有 IN_PRODUCTION 子镜头的场次**成功**——缺口可复现，
    而不是「看起来该缺」。必须在装触发器之前造数据才有意义：先装守卫再断言
    被拒，证明的是「守卫存在」，不是「缺口真实」。
N1  迁移后，同一操作被拒（P0001），消息里带**数量**（可用于判断该先处理几个）。
N2  反向：父场次已归档时，把子镜头推进到 IN_PRODUCTION 被拒。
N3  反向 INSERT：直接插入一个 IN_PRODUCTION 镜头到已归档场次被拒。
F1  NOT_STARTED 子镜头不拦归档（砍掉未开拍的场次是合法操作）。
F2  无子镜头的场次归档不受影响。
F3  只拦状态转换：IN_PRODUCTION 镜头改非状态列（shot_code）不受影响。
F4  同事务内新建场次 + 建 IN_PRODUCTION 镜头不误伤（父行此刻可能查不到，
    由外键兜底，不由本触发器判）。
F5  BLOCKED 子镜头**不拦**归档（2026-11-21 裁定）。它的正当性不在本脚本，
    而在批次七的 S3：归档后该镜头推不进 IN_PRODUCTION，复活路径已被关死。
P1  以真实业务角色 `svc_project_planning` 归档，得到的是 P0001（业务拒绝）
    而不是 42501（权限不足）——这是 SECURITY DEFINER 的核心证据：
    该角色对 shot_planning.shots **零权限**，用 INVOKER 会让所有归档全挂。
D1  降级后同一归档恢复成功（约束确实已撤）。

判定三态：PASS / FAIL，无 N-A（每条在本环境都可执行）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).parent))
import acceptance_ledger as L  # noqa: E402
import lab  # noqa: E402

HOST, PORT, USER, PWD, TEMPLATE = (
    lab.DEFAULT_HOST,
    lab.DEFAULT_PORT,
    lab.DEFAULT_USER,
    lab.DEFAULT_PASSWORD,
    lab.DEFAULT_TEMPLATE,
)
LAB = "lab_scene_archive_invariant"
MIG = (
    Path(__file__).parents[3]
    / "data/migrations/pending/feat-ownership/j0e1f2a3b4c5_scene_archive_invariant.py"
)

TENANT = "00000000-0000-0000-0000-000000000001"
ORG = "00000000-0000-0000-0000-000000000011"
PROJECT = "proj-alpha"
EPISODE = "ep-alpha-001"
SVC = "svc_project_planning"

results: list[dict] = []


def record(cid: str, ok: bool, expect: str, observed: str) -> None:
    status = "PASS" if ok else "FAIL"
    results.append({"id": cid, "status": status, "expect": expect, "observed": observed})
    print(f"[{status}] {cid}")
    print(f"       期望: {expect}")
    print(f"       实测: {observed}")


async def sqlstate_of(c: asyncpg.Connection, sql: str) -> tuple[str, str, int]:
    """执行语句，返回 (sqlstate, 说明, 影响行数)；成功时 sqlstate="00000"。

    为什么返回**影响行数**而不只返回「有没有报错」：写入类验收禁止以
    「未抛异常」为判据。RLS 与 `WHERE` 不匹配都会把越权/无效写静默变成
    `UPDATE 0`，语句照样成功——只看异常会把「什么都没改」判成 PASS。

    事务归属：
    - 调用方没有事务时，本函数自开自结（成功 COMMIT、失败 ROLLBACK），
      使「归档成功」是**真的提交**，可被后续语句读到；
    - 调用方已在事务内（如 P1 的 `SET LOCAL ROLE`）时用保存点，
      绝不替调用方提交或回滚整个事务。

    保存点而不是裸 try：一条失败语句会把整个事务置为 aborted，
    后续断言全部连锁失败，看起来像「后面每一条都坏了」。
    """
    own = not c.is_in_transaction()
    sp = "probe_sp"
    if own:
        await c.execute("BEGIN")
    await c.execute(f"SAVEPOINT {sp}")
    try:
        status = await c.execute(sql)
        await c.execute(f"RELEASE SAVEPOINT {sp}")
        if own:
            await c.execute("COMMIT")
        rows = int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0
        return "00000", status, rows
    except asyncpg.PostgresError as exc:
        await c.execute(f"ROLLBACK TO SAVEPOINT {sp}")
        if own:
            await c.execute("ROLLBACK")
        return getattr(exc, "sqlstate", "?????"), str(exc), 0
    except Exception as exc:  # noqa: BLE001
        await c.execute(f"ROLLBACK TO SAVEPOINT {sp}")
        if own:
            await c.execute("ROLLBACK")
        return "?????", f"{type(exc).__name__}: {exc}", 0


async def seed_scene(
    c: asyncpg.Connection, scene_id: str, *, status: str = "ACTIVE"
) -> None:
    await lab.insert_minimal_row(
        c,
        "project_planning.scenes",
        entity_id=scene_id,
        entity_type="Scene",
        schema_version=1,
        entity_version=1,
        owner_module="project_planning",
        status=status,
        scene_id=scene_id,
        scene_code=f"SC-{scene_id}",
        episode_id=EPISODE,
        project_id=PROJECT,
        tenant_id=TENANT,
        organization_id=ORG,
    )


async def seed_shot(
    c: asyncpg.Connection, shot_id: str, scene_id: str, *, status: str
) -> None:
    """同事务建 shot_revisions + shots：两表互指且双边 NOT NULL（延迟约束）。"""
    revision_id = f"rev-{shot_id}"
    # BLOCKED 与 blocked_reason 必须同真同假（ck_shots_blocked_reason）。
    # 夹具若不提供，会撞 CHECK —— 那不是约束太严，是夹具造了一个非法状态。
    shot_extra: dict[str, object] = {}
    if status == "BLOCKED":
        shot_extra["blocked_reason"] = "验收用例：阻塞中"
    if status == "CANCELLED":
        shot_extra["cancellation_ref"] = "approval-b6-cancel"
        shot_extra["cancelled_at"] = datetime(2026, 1, 2, tzinfo=timezone.utc)
    async with c.transaction():
        await lab.insert_minimal_row(
            c,
            "shot_planning.shot_revisions",
            entity_id=revision_id,
            entity_type="ShotRevision",
            schema_version=1,
            entity_version=1,
            owner_module="shot_planning",
            status="CONFIRMED",
            tenant_id=TENANT,
            organization_id=ORG,
            shot_id=shot_id,
            project_id=PROJECT,
            episode_ref="EP001",
            scene_ref="SC001",
            episode_id=EPISODE,
            scene_id=scene_id,
            # 批次十后这些是契约必填列。给合法值而不是让通用占位填
            # varchar(32)（"probe-<col>" 超长会报截断，看起来像迁移坏了）。
            # confirmed_by 用 legacy 哨兵，才能合法地不填剧本依据：夹具
            # 本来就不是人类确认的修订，不能为过 CHECK 编造一条剧本版本。
            description="验收用",
            narrative_intent="验收用",
            character_refs=json.dumps([]),
            location_ref="LOC-B6",
            prop_refs=json.dumps([]),
            constraint_pack_applicability="NOT_APPLICABLE",
            creative_autonomy="L1",
            risk=json.dumps({"level": "LOW"}),
            quality_profile="UNIFIED_HIGH_QUALITY",
            intent_ref=json.dumps({"intent_id": "intent-b6", "intent_version": 1}),
            script_anchors=json.dumps(["EP001:1"]),
            change_origin="BREAKDOWN_CONFIRM",
            changed_fields=json.dumps([]),
            provenance=json.dumps({"legacy_backfill": True}),
            confirmed_by="LEGACY_BACKFILL",
            confirmed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            revision_no=1,
        )
        await lab.insert_minimal_row(
            c,
            "shot_planning.shots",
            entity_id=shot_id,
            entity_type="Shot",
            schema_version=1,
            entity_version=1,
            owner_module="shot_planning",
            status=status,
            shot_id=shot_id,
            shot_code=f"SH-{shot_id}",
            scene_id=scene_id,
            project_id=PROJECT,
            shot_revision_id=revision_id,
            tenant_id=TENANT,
            organization_id=ORG,
            **shot_extra,
        )


async def main() -> int:
    mod = lab.load_migration(MIG, "mig_batch6")
    admin = await lab.admin_conn(HOST, PORT, USER, PWD)
    try:
        await lab.fresh_clone(admin, LAB, TEMPLATE)
        c = await lab.lab_conn(HOST, PORT, USER, PWD, LAB)
        try:
            # 脚本不得假设库停在某版本：若本层已在，先撤掉，才能复现 G1。
            present = await c.fetchval(
                "SELECT to_regprocedure('project_planning.scenes_archive_guard()') IS NOT NULL"
            )
            if present:
                print("检测到本层已应用，先降级以复现缺口……")
                for s in lab._downgrade_sqls(mod):
                    await c.execute(s)

            # ---- 造数据（必须在装触发器之前）----
            await seed_scene(c, "b6-sc-A")                      # 有 IN_PRODUCTION 子镜头
            await seed_scene(c, "b6-sc-B")                      # 有 NOT_STARTED 子镜头
            await seed_scene(c, "b6-sc-C")                      # 无子镜头
            await seed_scene(c, "b6-sc-D")                      # 预先归档，供反向用例
            await seed_scene(c, "b6-sc-E")                      # 有 BLOCKED 子镜头
            await seed_shot(c, "b6-sh-A", "b6-sc-A", status="IN_PRODUCTION")
            await seed_shot(c, "b6-sh-B", "b6-sc-B", status="NOT_STARTED")
            await seed_shot(c, "b6-sh-D", "b6-sc-D", status="NOT_STARTED")
            await seed_shot(c, "b6-sh-E", "b6-sc-E", status="BLOCKED")

            # ---- G1 缺口可复现 ----
            st, msg, n = await sqlstate_of(
                c, "UPDATE project_planning.scenes SET status='ARCHIVED' WHERE scene_id='b6-sc-A'"
            )
            # 不只判「没报错」：必须回读确认真的写进去了，否则 RLS 静默过滤
            # （UPDATE 0）也会被当成缺口成立。
            back = await c.fetchval(
                "SELECT status FROM project_planning.scenes WHERE scene_id='b6-sc-A'"
            )
            ok_g1 = st == "00000" and n == 1 and back == "ARCHIVED"
            if st == "00000":
                await c.execute(
                    "UPDATE project_planning.scenes SET status='ACTIVE' WHERE scene_id='b6-sc-A'"
                )
            record(
                "G1",
                ok_g1,
                "归档成功且回读为 ARCHIVED —— 缺口真实存在（装触发器前必须复现）",
                f"sqlstate={st} 影响行数={n} 回读={back}",
            )

            # 预先把 SC-D 归档（此时无守卫），供反向用例。
            # 这条失败会让 N2/N3 变成「父场次其实还是 ACTIVE」，
            # 于是它们会因错误的原因通过——必须显式断言。
            st_d, msg_d, n_d = await sqlstate_of(
                c, "UPDATE project_planning.scenes SET status='ARCHIVED' WHERE scene_id='b6-sc-D'"
            )
            assert st_d == "00000" and n_d == 1, f"预置 SC-D 归档失败：{st_d} {msg_d}"

            # ---- 应用本层 ----
            applied = await lab.apply_steps(c, mod.upgrade_steps())
            print(f"\n已应用 {applied} 步升级\n")

            # ---- N1 归档被拒 ----
            st, msg, n = await sqlstate_of(
                c, "UPDATE project_planning.scenes SET status='ARCHIVED' WHERE scene_id='b6-sc-A'"
            )
            record(
                "N1",
                st == "P0001" and "1 个镜头" in msg,
                "P0001，消息含『1 个镜头处于 IN_PRODUCTION』",
                f"sqlstate={st}；{msg[:120]}",
            )

            # ---- N2 反向 UPDATE ----
            st, msg, n = await sqlstate_of(
                c, "UPDATE shot_planning.shots SET status='IN_PRODUCTION' WHERE shot_id='b6-sh-D'"
            )
            record(
                "N2",
                st == "P0001" and "已归档" in msg,
                "P0001，消息说明父场次已归档",
                f"sqlstate={st}；{msg[:120]}",
            )

            # ---- N3 反向 INSERT ----
            st, msg, n = await sqlstate_of(
                c,
                "INSERT INTO shot_planning.shots (entity_id, entity_type, schema_version,"
                " entity_version, status, owner_module, created_at, created_by, updated_at,"
                " shot_id, shot_code, scene_id, project_id, shot_revision_id, tenant_id,"
                f" organization_id) VALUES ('b6-sh-D2','Shot',1,1,'IN_PRODUCTION','shot_planning',"
                f" now(),'u',now(),'b6-sh-D2','SH-D2','b6-sc-D','{PROJECT}','rev-b6-sh-D',"
                f"'{TENANT}','{ORG}')",
            )
            record(
                "N3",
                st == "P0001",
                "P0001 —— 直接 INSERT 成 IN_PRODUCTION 同样被挡",
                f"sqlstate={st}；{msg[:100]}",
            )

            # ---- F1 NOT_STARTED 不拦 ----
            st, msg, n = await sqlstate_of(
                c, "UPDATE project_planning.scenes SET status='ARCHIVED' WHERE scene_id='b6-sc-B'"
            )
            record("F1", st == "00000" and n == 1, "归档成功（子镜头 NOT_STARTED）", f"sqlstate={st} {msg}".strip())

            # ---- F2 无子镜头 ----
            st, msg, n = await sqlstate_of(
                c, "UPDATE project_planning.scenes SET status='ARCHIVED' WHERE scene_id='b6-sc-C'"
            )
            record("F2", st == "00000" and n == 1, "归档成功（无子镜头）", f"sqlstate={st} {msg}".strip())

            # ---- F3 只拦状态转换 ----
            st, msg, n = await sqlstate_of(
                c, "UPDATE shot_planning.shots SET shot_code='SH-A2' WHERE shot_id='b6-sh-A'"
            )
            still = await c.fetchval(
                "SELECT status FROM project_planning.scenes WHERE scene_id='b6-sc-A'"
            )
            record(
                "F3",
                st == "00000" and still == "ACTIVE",
                "改非状态列成功，且父场次仍为 ACTIVE",
                f"sqlstate={st}；父场次={still}",
            )

            # ---- F4 同事务建场次 + IN_PRODUCTION 镜头 ----
            st = "00000"
            msg = ""
            try:
                async with c.transaction():
                    await seed_scene(c, "b6-sc-NEW")
                    await seed_shot(c, "b6-sh-NEW", "b6-sc-NEW", status="IN_PRODUCTION")
            except Exception as exc:  # noqa: BLE001
                st, msg = getattr(exc, "sqlstate", "?????"), str(exc)
            cnt = await c.fetchval(
                "SELECT count(*) FROM shot_planning.shots WHERE shot_id='b6-sh-NEW'"
            )
            record(
                "F4",
                st == "00000" and cnt == 1,
                "成功 —— 父行此刻尚未可见，应由外键兜底而非本触发器判",
                f"sqlstate={st}；行数={cnt}；{msg[:80]}",
            )

            # ---- F5 BLOCKED 当前不拦 ----
            st, msg, n = await sqlstate_of(
                c, "UPDATE project_planning.scenes SET status='ARCHIVED' WHERE scene_id='b6-sc-E'"
            )
            record(
                "F5",
                st == "00000" and n == 1,
                "归档成功（BLOCKED 不计在制 —— 2026-11-21 裁定，见批次七；"
                "复活路径由 B 侧守卫关闭）",
                f"sqlstate={st} {msg}".strip(),
            )

            # ---- I1 台账复算：无存量违例时应全 PASS ----
            inv = await L.collect_invariants(c)
            record(
                "I1",
                all(i["status"] == "PASS" for i in inv),
                "违例行数=0 且守卫函数齐全（两条独立检查都是 PASS）",
                "；".join(f"{i['title']}={i['status']}/{i['actual']}" for i in inv),
            )

            # ---- I2 台账复算必须能 FAIL（而不是恒真）----
            # 手法：卸守卫 → 造一条「归档场次 + IN_PRODUCTION 镜头」→ 只重装守卫、
            # 不修数据，模拟「守卫上线前就已存在的存量违例」。
            # 若台账恒真，这一步会仍然 PASS —— 那它就是在给现状背书。
            for s in mod.downgrade_steps():
                await c.execute(s.sql)
            await seed_scene(c, "b6-sc-MUT")
            await seed_shot(c, "b6-sh-MUT", "b6-sc-MUT", status="IN_PRODUCTION")
            st_m, msg_m, n_m = await sqlstate_of(
                c,
                "UPDATE project_planning.scenes SET status='ARCHIVED' WHERE scene_id='b6-sc-MUT'",
            )
            assert st_m == "00000" and n_m == 1, f"造违例失败：{st_m} {msg_m}"
            for s in mod.upgrade_steps():
                await c.execute(s.sql)
            inv2 = await L.collect_invariants(c)
            record(
                "I2",
                inv2[0]["status"] == "FAIL" and inv2[0]["actual"] == 1,
                "违例行数=1 ⇒ FAIL（台账抓到存量违例）",
                f"违例行数={inv2[0]['actual']} 判定={inv2[0]['status']}",
            )

            # ---- P1 真实角色下是业务拒绝而非权限不足 ----
            # 手动事务：SET LOCAL ROLE 只在事务内有效，且测完必须整体回滚
            # （否则 SC-A 会被真的归档，影响后续 D1）。
            await c.execute("BEGIN")
            await c.execute(f"SET LOCAL ROLE {SVC}")
            await c.execute(f"SET LOCAL app.current_tenant_id = '{TENANT}'")
            await c.execute(f"SET LOCAL app.current_org_id = '{ORG}'")
            await c.execute(f"SET LOCAL app.current_projects = '{PROJECT}'")
            st, msg, n = await sqlstate_of(
                c,
                "UPDATE project_planning.scenes SET status='ARCHIVED' WHERE scene_id='b6-sc-A'",
            )
            await c.execute("ROLLBACK")
            record(
                "P1",
                st == "P0001",
                "P0001（业务拒绝），**不是** 42501（权限不足）",
                f"sqlstate={st}；{msg[:120]}",
            )

            # ---- D1 降级后恢复 ----
            for s in mod.downgrade_steps():
                await c.execute(s.sql)
            st, msg, n = await sqlstate_of(
                c, "UPDATE project_planning.scenes SET status='ARCHIVED' WHERE scene_id='b6-sc-A'"
            )
            back = await c.fetchval(
                "SELECT status FROM project_planning.scenes WHERE scene_id='b6-sc-A'"
            )
            record(
                "D1",
                st == "00000" and n == 1 and back == "ARCHIVED",
                "降级后归档成功且回读为 ARCHIVED（约束确实已撤）",
                f"sqlstate={st} 影响行数={n} 回读={back}",
            )
        finally:
            await c.close()
    finally:
        await lab.drop_lab(admin, LAB)
        await admin.close()

    failed = [r["id"] for r in results if r["status"] == "FAIL"]
    print(f"\n批次六验证：{len(results) - len(failed)}/{len(results)} 通过"
          + (f"（FAIL: {failed}）" if failed else ""))
    out = Path(__file__).with_suffix(".json")
    out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "revision": mod.revision,
                "lab": LAB,
                "verdict": "PASS" if not failed else "FAIL",
                "cases": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"证据：{out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
