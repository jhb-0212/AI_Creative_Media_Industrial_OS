"""父对象「冻结子集合」的锁语义实测（docs/adr/ADR-R27 的 Q1 前置证据）。

要证的不是「加个锁看起来行」，而是四件事，每一件都能单独失败：

H1  ``FOR KEY SHARE`` 挡不住归档 —— 这是 ADR-R27 初稿方案的反证。若 H1 得到
    「畅通」，则初稿写的 ``lock_for_child_write(FOR KEY SHARE)`` 等于没加锁；
    本脚本存在的首要理由就是把这个错误钉下来，而不是留着「推理好像对」。
H2  ``FOR SHARE`` 挡得住 ``UPDATE ... SET status``。
H3  真实归档路径走的是**乐观锁条件 UPDATE**（``WHERE entity_id=? AND
    entity_version=?``），不是 ``UPDATE ... WHERE id=?``。它同样是非键列更新，
    但必须单独测：条件里多了版本列，不能想当然认为与 H2 同结论。
H4  **等锁结束后读到的是新值还是旧快照**。这是最容易被忽略的一环：若创建方
    阻塞后拿到的是旧快照（ACTIVE），冻结判定会读到 ACTIVE 并放行 ——
    闸门全绿、数据已错，且以后再查不出来。READ COMMITTED 下应重检并读到
    ARCHIVED；REPEATABLE READ 下应 40001（fail-closed 也合格），绝不能读到 ACTIVE。
H5  两个创建方之间共享锁互不阻塞 —— 否则同项目并发创建被无谓串行化。
H6  ``FOR SHARE`` 不阻塞子表 INSERT —— 外键检查取 KEY SHARE，与 SHARE 不冲突。
H7  REPEATABLE READ 下 H4 的行为（边界记录，但判据是「不得静默放行」）。
H8  真实表 + 真实 RLS 策略 + 真实业务角色下复现 H2，并断言
    ``SELECT ... FOR SHARE`` **返回 1 行**。返回 0 行时锁根本没加上，
    而语句不会报错 —— 这是「静默不锁」，比报错更危险。
H9  错误租户上下文下 ``FOR SHARE`` 返回 0 行（对照项）：说明「父行查不到」
    必须 fail-closed 成拒绝，绝不能写成 ``if parent is not None: 检查冻结``。

判定三态：PASS / FAIL / N-A（N-A 不计入通过率，单独列出）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).parent))
import lab

HOST = lab.DEFAULT_HOST
PORT = lab.DEFAULT_PORT
USER = lab.DEFAULT_USER
PWD = lab.DEFAULT_PASSWORD
TEMPLATE = lab.DEFAULT_TEMPLATE
LAB = "lab_parent_freeze_lock"

LOCK_TIMEOUT_MS = 400
TENANT = "00000000-0000-0000-0000-000000000001"
ORG = "00000000-0000-0000-0000-000000000011"
PROJECT = "proj-alpha"
SCENE_ID = "scene-alpha-001"
SVC = "svc_project_planning"

results: list[dict] = []


def record(cid: str, title: str, status: str, expect: str, observed: str) -> None:
    results.append(
        {"id": cid, "title": title, "status": status, "expect": expect, "observed": observed}
    )
    mark = {"PASS": "[PASS]", "FAIL": "[FAIL]", "N-A ": "[N/A ]"}[status]
    print(f"{mark} {cid} {title}")
    print(f"       期望: {expect}")
    print(f"       实测: {observed}")


async def conn() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=HOST, port=PORT, user=USER, password=PWD, database=LAB
    )


async def probe(c: asyncpg.Connection, sql: str, timeout_ms: int = LOCK_TIMEOUT_MS) -> tuple[str, str]:
    """在保存点内执行 sql，返回 (blocked|ok|error, 说明)。

    用保存点而非子事务：``ROLLBACK TO SAVEPOINT`` 会把 GUC 一并恢复，所以
    ``lock_timeout`` 必须设在保存点**之前**，否则超时设置会随回滚消失，
    第二次探测就变成「等满默认 timeout」——脚本会挂住而不是失败。
    """
    await c.execute(f"SET LOCAL lock_timeout = '{timeout_ms}ms'")
    await c.execute("SAVEPOINT probe")
    try:
        await c.execute(sql)
        await c.execute("ROLLBACK TO SAVEPOINT probe")
        return "ok", "执行成功（未被阻塞）"
    except asyncpg.exceptions.LockNotAvailableError as exc:
        await c.execute("ROLLBACK TO SAVEPOINT probe")
        return "blocked", f"LockNotAvailable: {exc}"
    except Exception as exc:
        await c.execute("ROLLBACK TO SAVEPOINT probe")
        return "error", f"{type(exc).__name__}: {exc}"


async def setup_temp(admin: asyncpg.Connection) -> None:
    async with admin.transaction():
        await admin.execute("DROP TABLE IF EXISTS _pf_child, _pf_lock CASCADE")
        await admin.execute(
            "CREATE TABLE _pf_lock (id int PRIMARY KEY, status text NOT NULL, entity_version int NOT NULL)"
        )
        await admin.execute(
            "CREATE TABLE _pf_child (id int PRIMARY KEY, parent_id int REFERENCES _pf_lock(id))"
        )
        await admin.execute("INSERT INTO _pf_lock VALUES (1, 'ACTIVE', 1)")


async def case_lock_matrix() -> None:
    """H1/H2/H3/H5/H6：锁强度矩阵。每项都开新事务，互不残留。"""
    holder = await conn()
    other = await conn()
    try:
        # H1 FOR KEY SHARE 挡不住改非键列
        await holder.execute("BEGIN")
        await holder.execute("SELECT id FROM _pf_lock WHERE id=1 FOR KEY SHARE")
        await other.execute("BEGIN")
        state, note = await probe(other, "UPDATE _pf_lock SET status='ARCHIVED' WHERE id=1")
        await other.execute("ROLLBACK")
        await holder.execute("ROLLBACK")
        record(
            "H1",
            "FOR KEY SHARE 不阻挡归档 UPDATE（初稿方案的反证）",
            "PASS" if state == "ok" else "FAIL",
            "畅通 —— 若被阻挡，说明初稿的 FOR KEY SHARE 恰好有效，结论需改写",
            note,
        )

        # H2 FOR SHARE 挡得住
        await holder.execute("BEGIN")
        await holder.execute("SELECT id FROM _pf_lock WHERE id=1 FOR SHARE")
        await other.execute("BEGIN")
        state, note = await probe(other, "UPDATE _pf_lock SET status='ARCHIVED' WHERE id=1")
        await other.execute("ROLLBACK")
        await holder.execute("ROLLBACK")
        record(
            "H2",
            "FOR SHARE 阻挡归档 UPDATE",
            "PASS" if state == "blocked" else "FAIL",
            "被阻塞（LOCK_TIMEOUT）",
            note,
        )

        # H3 乐观锁条件 UPDATE（真实归档路径）
        await holder.execute("BEGIN")
        await holder.execute("SELECT id FROM _pf_lock WHERE id=1 FOR SHARE")
        await other.execute("BEGIN")
        state, note = await probe(
            other,
            "UPDATE _pf_lock SET status='ARCHIVED', entity_version=entity_version+1 "
            "WHERE id=1 AND entity_version=1",
        )
        await other.execute("ROLLBACK")
        await holder.execute("ROLLBACK")
        record(
            "H3",
            "FOR SHARE 阻挡乐观锁条件 UPDATE（WHERE entity_version=?）",
            "PASS" if state == "blocked" else "FAIL",
            "被阻塞 —— 条件里多版本列不改变结论",
            note,
        )

        # H5 两个创建方互不阻塞
        await holder.execute("BEGIN")
        await holder.execute("SELECT id FROM _pf_lock WHERE id=1 FOR SHARE")
        await other.execute("BEGIN")
        state, note = await probe(other, "SELECT id FROM _pf_lock WHERE id=1 FOR SHARE")
        await other.execute("ROLLBACK")
        await holder.execute("ROLLBACK")
        record(
            "H5",
            "两个创建方同时 FOR SHARE 互不阻塞",
            "PASS" if state == "ok" else "FAIL",
            "畅通 —— 否则同项目并发创建被无谓串行化",
            note,
        )

        # H6 不阻塞子表 INSERT（外键检查取 KEY SHARE）
        await holder.execute("BEGIN")
        await holder.execute("SELECT id FROM _pf_lock WHERE id=1 FOR SHARE")
        await other.execute("BEGIN")
        state, note = await probe(other, "INSERT INTO _pf_child VALUES (10, 1)")
        await other.execute("ROLLBACK")
        await holder.execute("ROLLBACK")
        record(
            "H6",
            "FOR SHARE 不阻塞子表 INSERT（外键检查不冲突）",
            "PASS" if state == "ok" else "FAIL",
            "畅通 —— SHARE 与 KEY SHARE 不冲突",
            note,
        )
    finally:
        await holder.close()
        await other.close()


async def case_wait_then_read(isolation: str, cid: str, title: str) -> None:
    """H4/H7：等锁结束后读到的快照。

    顺序必须是「归档方先持锁未提交 → 创建方挂起 → 归档方**提交** → 创建方返回」。
    反过来（归档先提交、创建再读）只是普通的读已提交，测不到阻塞后的重检。

    这里必须是 ``COMMIT`` 而不是 ``ROLLBACK``：回滚后读到 ACTIVE 本来就对，
    用它去判「重检是否读到新值」等于把被测命题换成了「回滚是否生效」。
    第一版就写成了 ROLLBACK，报出 H4/H7 双红——那是装置错，不是产品错。
    数据在 finally 里复原，不靠「不提交」来保持干净。
    """
    arch = await conn()
    cre = await conn()
    try:
        await arch.execute(f"BEGIN ISOLATION LEVEL {isolation}")
        await arch.execute("UPDATE _pf_lock SET status='ARCHIVED' WHERE id=1")

        await cre.execute(f"BEGIN ISOLATION LEVEL {isolation}")

        async def waiting_read() -> str:
            return await cre.fetchval("SELECT status FROM _pf_lock WHERE id=1 FOR SHARE")

        task = asyncio.create_task(waiting_read())
        # 让出事件循环，确认它真的挂起在锁上（而不是秒回旧值）
        await asyncio.sleep(0.35)
        hung = not task.done()

        await arch.execute("COMMIT")  # 真提交：测的是「提交后创建方读到什么」
        try:
            seen = await asyncio.wait_for(task, timeout=5)
            outcome = f"读到 {seen}"
            ok = seen == "ARCHIVED" and hung
            expect = "挂起后读到 ARCHIVED（新版本）"
        except Exception as exc:
            name = type(exc).__name__
            outcome = f"{name}: {exc}"
            # 40001 = 序列化失败，方向是 fail-closed，可接受
            ok = "Serialization" in name or "40001" in str(exc)
            expect = "读到 ARCHIVED；或 40001（fail-closed 亦可，但绝不得读到 ACTIVE）"
        await cre.execute("ROLLBACK")

        record(
            cid,
            title,
            "PASS" if ok else "FAIL",
            expect,
            f"挂起={'是' if hung else '否'}；{outcome}",
        )
    finally:
        await arch.close()
        await cre.close()
        # 复原，供后续 case 使用
        fix = await conn()
        try:
            await fix.execute("UPDATE _pf_lock SET status='ACTIVE' WHERE id=1")
        finally:
            await fix.close()


async def case_real_table_rls() -> None:
    """H8/H9：真实表 + 真实策略 + 真实业务角色。"""
    holder = await conn()
    other = await conn()
    try:
        for c in (holder, other):
            await c.execute(f"SET ROLE {SVC}")

        async def set_ctx(c: asyncpg.Connection, tenant: str = TENANT) -> None:
            await c.execute(f"SET LOCAL app.current_tenant_id = '{tenant}'")
            await c.execute(f"SET LOCAL app.current_org_id = '{ORG}'")
            await c.execute(f"SET LOCAL app.current_projects = '{PROJECT}'")

        # H8 正例：可见 -> 能锁 -> 挡住归档
        await holder.execute("BEGIN")
        await set_ctx(holder)
        n = await holder.fetchval(
            "SELECT count(*) FROM ("
            "  SELECT scene_id FROM project_planning.scenes"
            f"  WHERE scene_id = '{SCENE_ID}' FOR SHARE"
            ") t"
        )
        await other.execute("BEGIN")
        await set_ctx(other)
        state, note = await probe(
            other,
            f"UPDATE project_planning.scenes SET status='ARCHIVED' WHERE scene_id = '{SCENE_ID}'",
        )
        await other.execute("ROLLBACK")
        await holder.execute("ROLLBACK")
        record(
            "H8",
            "真实表+RLS+svc 角色：FOR SHARE 锁到 1 行并挡住归档",
            "PASS" if (n == 1 and state == "blocked") else "FAIL",
            "锁定行数=1 且归档被阻塞",
            f"锁定行数={n}；{note}",
        )

        # H9 对照：错误租户下根本锁不到行，且**不报错**
        await holder.execute("BEGIN")
        await set_ctx(holder, tenant="00000000-0000-0000-0000-0000000000ff")
        n_bad = await holder.fetchval(
            "SELECT count(*) FROM ("
            "  SELECT scene_id FROM project_planning.scenes"
            f"  WHERE scene_id = '{SCENE_ID}' FOR SHARE"
            ") t"
        )
        await holder.execute("ROLLBACK")
        record(
            "H9",
            "错误租户上下文：FOR SHARE 返回 0 行且不报错（静默不锁）",
            "PASS" if n_bad == 0 else "FAIL",
            "0 行 —— 故 service 必须把『父行查不到』判为拒绝，不得跳过冻结检查",
            f"锁定行数={n_bad}",
        )
    finally:
        await holder.close()
        await other.close()


async def main() -> int:
    admin = await lab.admin_conn(HOST, PORT, USER, PWD)
    try:
        print(f"重建实验库 {LAB}（TEMPLATE {TEMPLATE}）……")
        await lab.fresh_clone(admin, LAB, TEMPLATE)
        # 临时表必须建在实验库内。admin 连的是维护库 ``postgres``，
        # 在它上面建表会让后续连接报 UndefinedTable —— 这类错会被误读成
        # 「环境没准备好」，实际是装置连错了库。
        seed = await conn()
        try:
            await setup_temp(seed)
        finally:
            await seed.close()

        await case_lock_matrix()
        print()
        await case_wait_then_read("READ COMMITTED", "H4", "READ COMMITTED：等锁后读到 ARCHIVED（重检）")
        print()
        await case_wait_then_read("REPEATABLE READ", "H7", "REPEATABLE READ：等锁后的行为（边界）")
        print()
        await case_real_table_rls()

        passed = sum(1 for r in results if r["status"] == "PASS")
        failed = [r["id"] for r in results if r["status"] == "FAIL"]
        na = [r["id"] for r in results if r["status"] == "N-A "]
        print(f"\n判定：{passed}/{len(results)} PASS" + (f"，FAIL={failed}" if failed else ""))
        print(f"总体：{'PASS' if not failed else 'FAIL'}")

        out = Path(__file__).with_suffix(".json")
        out.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "lab": LAB,
                    "template": TEMPLATE,
                    "verdict": "PASS" if not failed else "FAIL",
                    "passed": passed,
                    "total": len(results),
                    "na": na,
                    "cases": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"证据：{out}")
        return 0 if not failed else 1
    finally:
        await lab.drop_lab(admin, LAB)
        await admin.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
