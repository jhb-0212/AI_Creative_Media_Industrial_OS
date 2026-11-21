"""批次九验收：`workflow.rework_tasks` 与 `shots.rework_basis_ref` 的真实闭环。

命题（每条都能单独失败）：

S1  表字段覆盖 `ReworkTask` 契约的 required 集合（不是「看着差不多」）。
S2  身份 CHECK 与状态 CHECK 与契约同源：``entity_id = rework_task_id``，
    状态集合恰好是契约六态，不多也不少。
S3  `shots.rework_basis_ref` 的外键是**复合**的，列序为
    ``(project_id, rework_basis_ref)``，且已 VALIDATE。单列外键会放行跨项目借用。
S4  RLS 已启用，五个策略齐全（与同域 approval_tickets 同构）。
S5  行为：合法授权可挂；不存在的授权被拒；**另一个项目的授权也被拒**。
    写入断言用「影响行数 + 回读值」，不以「有没有抛异常」为判据。
S6  变异：撤掉外键后，刚才被拒的跨项目借用变成成功——证明拦截者是这条
    外键，而不是 RLS、CHECK 或夹具碰巧写错。

判定三态：PASS / FAIL，无 N-A。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import asyncpg
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import lab  # noqa: E402
from verify_batch6 import (  # noqa: E402
    HOST,
    ORG,
    PORT,
    PWD,
    PROJECT,
    TEMPLATE,
    TENANT,
    USER,
    seed_scene,
    seed_shot,
    sqlstate_of,
)

LAB = "lab_rework_tasks"
TABLE = "workflow.rework_tasks"
SCENE = "scene-b9"
SHOT = "shot-b9"
RT_OK = "rt-b9-ok"
RT_OTHER = "rt-b9-other-project"
OTHER_PROJECT = "proj-b9-beta"
ROOT = Path(__file__).resolve().parents[3]

results: list[dict] = []


def record(cid: str, ok: bool, expect: str, observed: str) -> None:
    status = "PASS" if ok else "FAIL"
    results.append({"id": cid, "status": status, "expect": expect, "observed": observed})
    print(f"[{status}] {cid}")
    print(f"       期望: {expect}")
    print(f"       实测: {observed}")


def contract() -> dict:
    schema = json.loads(
        (ROOT / "contracts/schemas/object_schemas.schema.json").read_text(encoding="utf-8")
    )
    definition = schema["definitions"]["ReworkTask"]
    states = yaml.safe_load(
        (ROOT / "contracts/state_machines/state_machines.yaml").read_text(encoding="utf-8")
    )
    state = next(item for item in states["objects"] if item["object"] == "ReworkTask")
    return {"required": set(definition["required"]), "states": set(state["allowed_states"])}


async def insert_rework(c: asyncpg.Connection, task_id: str, project: str) -> None:
    await lab.insert_minimal_row(
        c,
        TABLE,
        entity_id=task_id,
        entity_type="ReworkTask",
        schema_version=1,
        entity_version=1,
        status="OPEN",
        owner_module="workflow",
        tenant_id=TENANT,
        organization_id=ORG,
        project_id=project,
        rework_task_id=task_id,
        issue_id="issue-b9",
        defect_category="visual",
        root_cause="验收用",
        severity="major",
        return_node="SHOT",
        target_ref=SHOT,
        owner="tester",
    )


async def point_shot(c: asyncpg.Connection, ref: str) -> tuple[str, str, int]:
    state, message, _ = await sqlstate_of(
        c, f"UPDATE shot_planning.shots SET rework_basis_ref = '{ref}' WHERE entity_id = '{SHOT}'"
    )
    seen = await c.fetchval(
        f"SELECT rework_basis_ref FROM shot_planning.shots WHERE entity_id = '{SHOT}'"
    )
    # 「SQL 成功」不等于「写进去了」：RLS / WHERE 不匹配会静默 UPDATE 0。
    if state == "00000" and seen != ref:
        return "NOOP", f"语句成功但回读值仍为 {seen}", 0
    return state, message, 1 if seen == ref else 0


async def main() -> int:
    want = contract()
    admin = await lab.admin_conn(HOST, PORT, USER, PWD)
    try:
        await lab.fresh_clone(admin, LAB, TEMPLATE)
        c = await lab.lab_conn(HOST, PORT, USER, PWD, LAB)
        try:
            # S1 字段覆盖
            columns = {
                r["column_name"]
                for r in await c.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='workflow' AND table_name='rework_tasks'"
                )
            }
            missing = sorted(want["required"] - columns)
            record("S1", not missing, "契约 required 字段全部落表", f"缺: {missing or '无'}")

            # S2 身份 + 状态 CHECK
            check_sql = await c.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_rework_tasks_identity'"
            )
            status_sql = await c.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_rework_tasks_status'"
            )
            # PG16 会把 CHECK (status IN (...)) 规范化成 = ANY(ARRAY[...]::text[])，
            # 不能按 "IN (" 切分；从约束定义里取全部大写状态字面量才稳。
            got_states = set(re.findall(r"'([A-Z][A-Z_]*)'", status_sql))
            # PG 会把 varchar 比较规范化成 (col)::text，直接比字面串会假阴性。
            normalized = re.sub(r"[\s()]+", "", check_sql).replace("::text", "")
            ok = "entity_id=rework_task_id" in normalized
            record(
                "S2",
                ok and got_states == want["states"],
                "身份 CHECK 成立且状态集合等于契约",
                f"identity={check_sql}, states={sorted(got_states)}",
            )

            # S3 复合外键
            fk_cols = [
                r["n"]
                for r in await c.fetch(
                    "SELECT a.attname n FROM pg_constraint k "
                    "JOIN unnest(k.conkey) WITH ORDINALITY x(attnum, ord) ON true "
                    "JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.attnum "
                    "WHERE k.conname='fk_shots_rework_basis' ORDER BY x.ord"
                )
            ]
            valid = await c.fetchval(
                "SELECT convalidated FROM pg_constraint WHERE conname='fk_shots_rework_basis'"
            )
            record(
                "S3",
                fk_cols == ["project_id", "rework_basis_ref"] and valid is True,
                "复合外键列序正确且 validated",
                f"cols={fk_cols}, convalidated={valid}",
            )

            # S4 RLS
            rls = await c.fetchval(
                "SELECT relrowsecurity FROM pg_class WHERE oid='workflow.rework_tasks'::regclass"
            )
            policies = {
                r["polname"]
                for r in await c.fetch(
                    "SELECT polname FROM pg_policy "
                    "WHERE polrelid='workflow.rework_tasks'::regclass"
                )
            }
            want_policies = {
                "tenant_isolation_rework_tasks",
                "project_scope_rework_tasks",
                "quarantine_restrictive_rework_tasks",
                "repair_row_scope",
                "audit_quarantine_scope",
            }
            record("S4", bool(rls) and policies == want_policies, "RLS 开启且五策略齐全",
                   f"enabled={rls}, 缺={sorted(want_policies - policies)}")

            # S5 行为
            await seed_scene(c, SCENE)
            await seed_shot(c, SHOT, SCENE, status="IN_PRODUCTION")
            await insert_rework(c, RT_OK, PROJECT)
            state_ok, msg_ok, rows_ok = await point_shot(c, RT_OK)
            record("S5a", state_ok == "00000" and rows_ok == 1,
                   "同项目授权可挂且回读生效", f"{state_ok} rows={rows_ok} {msg_ok}")

            state_missing, msg_missing, rows_missing = await point_shot(c, "rt-does-not-exist")
            record("S5b", state_missing == "23503" and rows_missing == 0,
                   "不存在的授权被外键拒绝", f"{state_missing} {msg_missing[:120]}")

            await lab.insert_minimal_row(
                c, "project_planning.projects",
                entity_id=OTHER_PROJECT, entity_type="Project", schema_version=1,
                entity_version=1, status="ACTIVE", owner_module="project_planning",
                tenant_id=TENANT, organization_id=ORG,
                project_name="B9 他项目", project_tier="B",
            )
            await insert_rework(c, RT_OTHER, OTHER_PROJECT)
            state_cross, msg_cross, rows_cross = await point_shot(c, RT_OTHER)
            record("S5c", state_cross == "23503" and rows_cross == 0,
                   "跨项目授权被复合外键拒绝", f"{state_cross} {msg_cross[:120]}")

            # S6 变异：撤掉外键后同一条写必须成功——否则 S5c 的红灯另有原因。
            await c.execute("ALTER TABLE shot_planning.shots DROP CONSTRAINT fk_shots_rework_basis")
            state_mut, msg_mut, rows_mut = await point_shot(c, RT_OTHER)
            record("S6", state_mut == "00000" and rows_mut == 1,
                   "撤掉外键后跨项目写成功（证明拦截者是外键）",
                   f"{state_mut} rows={rows_mut} {msg_mut}")
        finally:
            await c.close()
    finally:
        await admin.close()

    failed = [item for item in results if item["status"] == "FAIL"]
    print(f"\n结果：{len(results) - len(failed)}/{len(results)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
