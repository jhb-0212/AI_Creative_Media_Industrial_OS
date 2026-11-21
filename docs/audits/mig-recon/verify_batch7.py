"""批次七验收：阻断集合单源 + `BLOCKED` 不阻断归档的裁定。

命题（每条都能单独失败）：

S1  应用后 `shot_archive_blocking_states()` 返回的集合**等于**迁移常量，
    也等于契约划分里的 `BLOCKING_STATES`——三者一致才算单源。
S2  两个守卫的函数体与两个触发器的定义里**不再出现状态字面量**。
    只测行为不够：行为相同可以来自「函数改了但没人调用」。
S3  `BLOCKED` 镜头不阻断其场次归档（裁定），且归档后该镜头**推不进**
    IN_PRODUCTION（B 侧兜底）。这两条缺一不可——A 侧放行 BLOCKED 的正当性
    完全建立在「复活路径已被关死」上。
S4  `IN_PRODUCTION` 仍然阻断（回归，防止本批次把批次六的能力改没）。
S5  变异：把函数改成包含 `BLOCKED`，含 BLOCKED 镜头的场次**归档被拒**。
    这是「守卫真的从函数取值」的证据——若守卫仍写死字面量，改函数不会有效果。
S6  变异：把函数改成空数组，含 `IN_PRODUCTION` 的场次**归档成功**。
    与 S5 成对：一个证明「能加进去」，一个证明「能拿出去」。只有 S5 时，
    一个「函数存在但守卫另有硬编码 + 恰好包含 IN_PRODUCTION」的实现也能过。
S7  降级：函数消失、守卫恢复字面量写法、触发器 WHEN 回来、归档恢复成功。
S8  台账「跨表不变量」仍全 PASS（本批次没破坏批次六建立的不变量）。
S9  划分完备：阻断 ∪ 非阻断 == 契约全部 Shot 状态，且不交。

判定三态：PASS / FAIL，无 N-A。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import acceptance_ledger as L  # noqa: E402
import lab  # noqa: E402
from verify_batch6 import (  # noqa: E402
    ORG,
    PROJECT,
    SVC,
    TEMPLATE,
    TENANT,
    seed_scene,
    seed_shot,
    sqlstate_of,
    HOST,
    PORT,
    USER,
    PWD,
)

LAB = "lab_shot_archive_blocking"
MIG = (
    Path(__file__).parents[3]
    / "data/migrations/pending/feat-ownership/k1f2a3b4c5d6_shot_archive_blocking_states.py"
)

results: list[dict] = []


def record(cid: str, ok: bool, expect: str, observed: str) -> None:
    status = "PASS" if ok else "FAIL"
    results.append({"id": cid, "status": status, "expect": expect, "observed": observed})
    print(f"[{status}] {cid}")
    print(f"       期望: {expect}")
    print(f"       实测: {observed}")


async def blocking_states(c: asyncpg.Connection, fn: str) -> list[str]:
    """读取阻断集合函数的返回值（``fn`` 允许指向变异体）。"""
    return await c.fetchval(f"SELECT {fn}()")


async def function_body(c: asyncpg.Connection, signature: str) -> str:
    return await c.fetchval(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")


async def trigger_def(c: asyncpg.Connection, name: str) -> str:
    return await c.fetchval(
        "SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t WHERE t.tgname = $1", name
    )


async def main() -> int:
    mod = lab.load_migration(MIG, "mig_batch7")
    contract_states = _contract_shot_states()
    blocking = list(mod.BLOCKING_STATES)
    non_blocking = list(mod.NON_BLOCKING_STATES)

    admin = await lab.admin_conn(HOST, PORT, USER, PWD)
    try:
        await lab.fresh_clone(admin, LAB, TEMPLATE)
        c = await lab.lab_conn(HOST, PORT, USER, PWD, LAB)
        try:
            # ---- 前提：批次六在、批次七不在 ----
            guard_present = await c.fetchval(
                "SELECT to_regprocedure('project_planning.scenes_archive_guard()')"
                " IS NOT NULL AND to_regprocedure("
                "'shot_planning.shots_parent_scene_guard()') IS NOT NULL"
            )
            fn_absent = not await c.fetchval(
                "SELECT to_regprocedure("
                "'shot_planning.shot_archive_blocking_states()') IS NOT NULL"
            )
            if not (guard_present and fn_absent):
                # 前提不成立就停：在错的起点上跑出来的 PASS 比 FAIL 更危险。
                raise SystemExit(
                    f"前提不成立：批次六守卫={guard_present}，批次七函数缺失={fn_absent}"
                )

            # ---- 造数据（装守卫前，供 S3/S4/S5/S6 使用）----
            await seed_scene(c, "b7-sc-blocked")
            await seed_shot(c, "b7-sh-blocked", "b7-sc-blocked", status="BLOCKED")
            await seed_scene(c, "b7-sc-prod")
            await seed_shot(c, "b7-sh-prod", "b7-sc-prod", status="IN_PRODUCTION")
            await seed_scene(c, "b7-sc-mut-blocked")
            await seed_shot(c, "b7-sh-mut-blocked", "b7-sc-mut-blocked", status="BLOCKED")
            await seed_scene(c, "b7-sc-mut-empty")
            await seed_shot(c, "b7-sh-mut-empty", "b7-sc-mut-empty", status="IN_PRODUCTION")

            # ---- 应用本层 ----
            applied = await lab.apply_steps(c, mod.upgrade_steps())
            print(f"\n已应用 {applied} 步升级\n")

            # ---- S1 单源：DB == 迁移常量 ----
            db_states = sorted(await blocking_states(c, "shot_planning.shot_archive_blocking_states"))
            record(
                "S1",
                db_states == sorted(blocking) == sorted(_contract_blocking(contract_states, non_blocking)),
                "数据库函数返回值 == 迁移常量 == 契约划分中的阻断集合",
                f"db={db_states} 迁移={sorted(blocking)} "
                f"契约划分={sorted(_contract_blocking(contract_states, non_blocking))}",
            )

            # ---- S2 守卫/触发器里没有状态字面量 ----
            scenes_body = await function_body(
                c, "project_planning.scenes_archive_guard()"
            )
            shots_body = await function_body(
                c, "shot_planning.shots_parent_scene_guard()"
            )
            trg_scenes = await trigger_def(c, "scenes_archive_guard_trigger")
            trg_shots = await trigger_def(c, "shots_parent_scene_guard_trigger")
            leaks = {
                "A 侧守卫": "IN_PRODUCTION" in scenes_body,
                "B 侧守卫": "IN_PRODUCTION" in shots_body,
                "A 侧触发器": "IN_PRODUCTION" in trg_scenes,
                "B 侧触发器": "IN_PRODUCTION" in trg_shots,
            }
            record(
                "S2",
                not any(leaks.values()),
                "四处都不再出现 'IN_PRODUCTION'，判定只走函数",
                "；".join(f"{k}={'仍有' if v else '无'}" for k, v in leaks.items()),
            )

            # ---- S4 IN_PRODUCTION 仍阻断（先跑回归，再跑会改状态的 S3）----
            st, msg, n = await sqlstate_of(
                c,
                "UPDATE project_planning.scenes SET status='ARCHIVED'"
                " WHERE scene_id='b7-sc-prod'",
            )
            record(
                "S4",
                st == "P0001" and "1 个镜头" in msg,
                "P0001，消息含『1 个镜头处于在制状态』",
                f"sqlstate={st}；{msg[:110]}",
            )

            # ---- S3 BLOCKED 不阻断归档，但归档后不可复活 ----
            st, msg, n = await sqlstate_of(
                c,
                "UPDATE project_planning.scenes SET status='ARCHIVED'"
                " WHERE scene_id='b7-sc-blocked'",
            )
            back = await c.fetchval(
                "SELECT status FROM project_planning.scenes"
                " WHERE scene_id='b7-sc-blocked'"
            )
            archived_ok = st == "00000" and n == 1 and back == "ARCHIVED"

            st2, msg2, _ = await sqlstate_of(
                c,
                "UPDATE shot_planning.shots SET status='IN_PRODUCTION'"
                " WHERE shot_id='b7-sh-blocked'",
            )
            still_blocked = await c.fetchval(
                "SELECT status FROM shot_planning.shots WHERE shot_id='b7-sh-blocked'"
            )
            record(
                "S3",
                archived_ok and st2 == "P0001" and still_blocked == "BLOCKED",
                "归档成功；随后该 BLOCKED 镜头推进 IN_PRODUCTION 被拒（B 侧兜底），"
                "状态仍为 BLOCKED",
                f"归档 sqlstate={st} 行数={n} 回读={back}；"
                f"复活 sqlstate={st2} 回读={still_blocked}",
            )

            # ---- S9 划分完备 ----
            record(
                "S9",
                set(blocking) | set(non_blocking) == contract_states
                and not (set(blocking) & set(non_blocking)),
                "阻断 ∪ 非阻断 == 契约全部 Shot 状态，且两者不交",
                f"契约={sorted(contract_states)} 阻断={sorted(blocking)} "
                f"非阻断={sorted(non_blocking)}",
            )

            # ---- S8 台账不变量：本批次产出状态下必须全 PASS ----
            # 刻意排在任何变异之前：变异会故意制造存量违例（S6 把「有在制镜头的
            # 场次」归档掉），那之后的 FAIL 是装置的产物，不是产品的缺陷。
            inv = await L.collect_invariants(c)
            record(
                "S8",
                all(i["status"] == "PASS" for i in inv),
                "违例行数=0 且守卫函数齐全",
                "；".join(f"{i['title']}={i['status']}/{i['actual']}" for i in inv),
            )

            # ---- S5 变异：函数加入 BLOCKED ⇒ 含 BLOCKED 的场次归档被拒 ----
            await c.execute(_mutate_fn_sql(mod, ("IN_PRODUCTION", "BLOCKED")))
            st, msg, n = await sqlstate_of(
                c,
                "UPDATE project_planning.scenes SET status='ARCHIVED'"
                " WHERE scene_id='b7-sc-mut-blocked'",
            )
            record(
                "S5",
                st == "P0001",
                "P0001 —— 把 BLOCKED 写进函数后，含 BLOCKED 镜头的场次立即被拦",
                f"sqlstate={st}；{msg[:110]}",
            )

            # ---- S6 变异：函数清空 ⇒ 含 IN_PRODUCTION 的场次归档成功 ----
            await c.execute(_mutate_fn_sql(mod, ()))
            st, msg, n = await sqlstate_of(
                c,
                "UPDATE project_planning.scenes SET status='ARCHIVED'"
                " WHERE scene_id='b7-sc-mut-empty'",
            )
            back = await c.fetchval(
                "SELECT status FROM project_planning.scenes"
                " WHERE scene_id='b7-sc-mut-empty'"
            )
            record(
                "S6",
                st == "00000" and n == 1 and back == "ARCHIVED",
                "归档成功且回读 ARCHIVED —— 守卫确实从函数取值，没有第二处硬编码",
                f"sqlstate={st} 影响行数={n} 回读={back}",
            )

            # 还原函数，恢复本层语义（后续 S7 依赖它）
            await c.execute(_mutate_fn_sql(mod, blocking))

            # ---- S10 台账不是恒真：变异留下的违例必须被抓到 ----
            # S6 之后库里确实存在「已归档场次 + 在制镜头」。若台账此时仍 PASS，
            # 它就是在给现状背书——这正是 S8 单独存在无法证明的那一半。
            inv_bad = await L.collect_invariants(c)
            record(
                "S10",
                inv_bad[0]["status"] == "FAIL" and inv_bad[0]["actual"] == 1,
                "违例行数=1 ⇒ FAIL（S6 变异留下的存量违例被台账抓到）",
                f"违例行数={inv_bad[0]['actual']} 判定={inv_bad[0]['status']}",
            )

            # ---- S7 降级 ----
            for s in mod.downgrade_steps():
                await c.execute(s.sql)
            fn_gone = not await c.fetchval(
                "SELECT to_regprocedure("
                "'shot_planning.shot_archive_blocking_states()') IS NOT NULL"
            )
            restored_a = await function_body(
                c, "project_planning.scenes_archive_guard()"
            )
            restored_b = await function_body(
                c, "shot_planning.shots_parent_scene_guard()"
            )
            trg_back = await trigger_def(c, "scenes_archive_guard_trigger")
            st, msg, n = await sqlstate_of(
                c,
                "UPDATE project_planning.scenes SET status='ARCHIVED'"
                " WHERE scene_id='b7-sc-prod'",
            )
            record(
                "S7",
                fn_gone
                and "IN_PRODUCTION" in restored_a
                and "IN_PRODUCTION" in restored_b
                and "WHEN" in trg_back.upper()
                and st == "P0001",
                "函数已撤、两个守卫恢复字面量、触发器 WHEN 回来、IN_PRODUCTION 仍被拦",
                f"函数已撤={fn_gone} A侧字面量={'IN_PRODUCTION' in restored_a} "
                f"B侧字面量={'IN_PRODUCTION' in restored_b} "
                f"触发器WHEN={'WHEN' in trg_back.upper()} 归档={st}",
            )
        finally:
            await c.close()
    finally:
        await lab.drop_lab(admin, LAB)
        await admin.close()

    failed = [r["id"] for r in results if r["status"] == "FAIL"]
    print(
        f"\n批次七验证：{len(results) - len(failed)}/{len(results)} 通过"
        + (f"（FAIL: {failed}）" if failed else "")
    )
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


def _contract_shot_states() -> set[str]:
    data = yaml.safe_load(
        (
            Path(__file__).parents[3]
            / "contracts"
            / "state_machines"
            / "state_machines.yaml"
        ).read_text(encoding="utf-8")
    )
    entry = next(o for o in data["objects"] if o.get("object") == "Shot")
    return set(entry["allowed_states"])


def _contract_blocking(contract: set[str], non_blocking: list[str]) -> set[str]:
    """契约全集减去显式「不算在制」的部分——用于与 DB 函数三方比对。

    刻意用减法而不是直接读常量：等式 `全集 - 非阻断 == 阻断` 才是「划分」的
    定义。若写成 `== set(BLOCKING_STATES)` 就是同义反复，任何常量都自洽。
    """
    return contract - set(non_blocking)


def _mutate_fn_sql(mod, states: tuple[str, ...]) -> str:
    """就地替换阻断集合函数体（变异测试用），返回一条 SQL。

    变异走 ``CREATE OR REPLACE``，撤掉变异就再执行一次原定义——这样 S5/S6
    测的是**线上同一个对象**，而不是另建一个同名替身。
    """
    literal = ", ".join(f"'{state}'" for state in states)
    return (
        "CREATE OR REPLACE FUNCTION shot_planning."
        f"{mod.FUNC_BLOCKING_STATES}() RETURNS text[] LANGUAGE sql IMMUTABLE "
        f"PARALLEL SAFE AS $mutation$ SELECT ARRAY[{literal}]::text[] $mutation$"
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
