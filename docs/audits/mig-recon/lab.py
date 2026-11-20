"""mig-recon 实验库公共装置：克隆、连接、执行、记录、出报告。

为什么单独抽出来
----------------
`verify_exception.py`（受控例外行为）与 `verify_guard_coverage.py`（逐表覆盖矩阵）
证明的是**两个不同命题**，判定必须各自写清楚；但「克隆一个用完即删的库」
「失败即失败地执行一条 SQL」「指纹怎么算」是**机械动作**，不该有两份实现——
两份装置一旦漂移，就会出现「A 脚本的库隔离有效、B 脚本其实连到了模板库」这类
无法从报告里看出来的事故。

边界：本模块**不做任何判定**，只返回 (是否符合预期, 说明)。
判定的语义（期望 P0001 还是期望影响 1 行）留在各验证脚本里。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import asyncpg

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 15444
DEFAULT_USER = "postgres"
DEFAULT_PASSWORD = "replay_super_pw"
DEFAULT_TEMPLATE = "ai_film_industrial"


# ---------------------------------------------------------------------------
# 库生命周期
# ---------------------------------------------------------------------------
async def admin_conn(host: str, port: int, user: str, password: str) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=host, port=port, user=user, password=password, database="postgres"
    )


async def fresh_clone(
    admin: asyncpg.Connection, lab: str, template: str
) -> None:
    """重建实验库。克隆而非直连：任何 DDL/数据损坏都只影响一次性副本。"""
    await admin.execute(f'DROP DATABASE IF EXISTS "{lab}" WITH (FORCE)')
    await admin.execute(f'CREATE DATABASE "{lab}" TEMPLATE "{template}"')


async def drop_lab(admin: asyncpg.Connection, lab: str) -> None:
    await admin.execute(f'DROP DATABASE IF EXISTS "{lab}" WITH (FORCE)')


async def lab_conn(
    host: str, port: int, user: str, password: str, lab: str
) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=host, port=port, user=user, password=password, database=lab
    )


async def reset_role(admin: asyncpg.Connection, role: str, *, bypass_rls: bool = True) -> None:
    """重建探针角色。

    ``BYPASSRLS`` 是**测试夹具**属性，不是产品设计：目标表启用 RLS 时，普通角色
    会在 RLS 层被静默过滤成 ``UPDATE 0``，测到的就不是守卫。加 BYPASSRLS 才能让
    「守卫是否拦住」成为唯一变量。
    """
    await admin.execute(f"DROP ROLE IF EXISTS {role}")
    suffix = " BYPASSRLS" if bypass_rls else ""
    await admin.execute(f"CREATE ROLE {role} NOLOGIN{suffix}")


async def drop_role(admin: asyncpg.Connection, role: str) -> bool:
    """删除角色，返回是否真的删掉。

    ``DROP ROLE`` 在角色于**任意**库持有权限时都会失败。归属五层迁移激活后，
    `afs_data_repair` 已在目标库被正式授权（40+ 个依赖），因此它不再可删——
    这不是产品缺陷，而是「角色已投入使用」的正常表现。把它当成错误处理会让
    验证脚本在激活后全面不可用，那才是真问题（证据不可复现）。
    所以这里统一吞掉该错误并**返回删除结果**，由调用方决定是否记录。
    """
    try:
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        return True
    except asyncpg.DependentObjectsStillExistError:
        return False


# ---------------------------------------------------------------------------
# 迁移加载与执行
# ---------------------------------------------------------------------------
def load_migration(path: str | Path, module_name: str = "mig_under_test") -> Any:
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:  # pragma: no cover - 只在路径写错时触发
        raise RuntimeError(f"无法加载迁移：{path}")
    mod = importlib.util.module_from_spec(spec)
    # 必须先登记到 sys.modules 再 exec：`@dataclass` 在 exec 期会通过
    # sys.modules[cls.__module__] 反查模块命名空间，未登记时抛
    # AttributeError: 'NoneType' object has no attribute '__dict__'。
    # 这不是迁移的问题，是动态导入的固有前提。
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


async def apply_steps(conn: asyncpg.Connection, steps: Iterable[Any]) -> int:
    n = 0
    for step in steps:
        await conn.execute(step.sql if hasattr(step, "sql") else str(step))
        n += 1
    return n


# ---------------------------------------------------------------------------
# 回到「激活前基线」
# ---------------------------------------------------------------------------
# 为什么需要显式回退，而不是给夹具补数据
# --------------------------------------
# 两个验证脚本证明的都是「从激活前基线出发，迁移被正确应用」这件事。
# 激活一旦完成，模板库就停在 e3f4，于是同一份脚本会连到一个**已经建好外键、
# 已经注入守卫**的库上：`verify_exception` 的 B1「升级前改归属列一律被拒」不再
# 成立（守卫已被改造），`verify_guard_coverage` 的 U1 重放 upgrade 会被前置断言
# 拒绝。此时若只在夹具里补一行 `projects` 让 23503 消失，脚本会「跑通」但在验证
# 一个与被测迁移无关的库——这正是 `@dataclass` 那次事故的同一类错误：
# 装置在对的地方修好了，被测命题却悄悄换掉了。
#
# 所以基线必须**由脚本自己重建并断言**，而不是假设模板库还停在原处。
OWNERSHIP_STACK: tuple[str, ...] = (
    "a9b8c7d6e5f4",
    "b0c9d8e7f6a5",
    "c1d2e3f4a5b7",
    "d2e3f4a5b6c7",
    "e3f4a5b6c7d8",
    # 批次二：三张「有归属列却无归属外键」的表补三列外键。
    # 它一旦应用，模板库就不再是「激活前基线」，回退必须一并撤掉；
    # 漏掉这一层会让 assert_rewound 仍然全绿（它当时只数前两批），
    # 于是基线静默偏离——这正是本函数存在的理由。
    "f8a9b0c1d2e3",
)


def _resolve_revision(versions_dir: Path, rev: str) -> Path:
    hits = sorted(versions_dir.glob(f"{rev}_*.py"))
    if len(hits) != 1:
        raise RuntimeError(f"revision {rev} 在 {versions_dir} 命中 {len(hits)} 个文件，无法确定迁移体")
    return hits[0]


def _downgrade_sqls(mod: Any) -> list[str]:
    """取迁移的降级语句序列。两种写法（Steps / SQL 字符串）都支持。

    刻意不在这里手写一遍 DROP：手抄的降级脚本会在迁移改动后静默漂移，
    最后证明的是「我抄的那份」而不是「被测迁移」。
    """
    if hasattr(mod, "downgrade_sqls"):
        raw = mod.downgrade_sqls()
    elif hasattr(mod, "downgrade_steps"):
        raw = mod.downgrade_steps()
    else:  # pragma: no cover - 只在迁移缺 downgrade 时触发
        raise RuntimeError(f"{mod.__name__} 没有 downgrade_sqls()/downgrade_steps()")
    return [s.sql if hasattr(s, "sql") else str(s) for s in raw]


def upgrade_sqls(mod: Any) -> list[str]:
    """取迁移的升级语句序列（与 ``_downgrade_sqls`` 对称）。

    归属栈里两种写法是**既成事实**：a9b8/f8a9 用 ``NamedTuple`` 的 steps，
    b0c9/c1d2/d2e3/e3f4 用纯 SQL 字符串列表。调用方不该各写一遍 ``hasattr`` 分支——
    那会把同一个适配器复制成 N 份，改动时漏改一处就静默半失败。
    """
    if hasattr(mod, "upgrade_sqls"):
        raw = mod.upgrade_sqls()
    elif hasattr(mod, "upgrade_steps"):
        raw = mod.upgrade_steps()
    else:  # pragma: no cover
        raise RuntimeError(f"{mod.__name__} 没有 upgrade_sqls()/upgrade_steps()")
    return [s.sql if hasattr(s, "sql") else str(s) for s in raw]


async def rewind_ownership_stack(
    conn: asyncpg.Connection, versions_dir: str | Path, stack: tuple[str, ...] = OWNERSHIP_STACK
) -> list[str]:
    """按逆序回退归属栈（当前六层），使克隆库回到激活前基线。返回实际回退的 revision 顺序。"""
    vd = Path(versions_dir)
    applied: list[str] = []
    for rev in reversed(stack):
        mod = load_migration(_resolve_revision(vd, rev), f"mig_rewind_{rev}")
        for sql in _downgrade_sqls(mod):
            await conn.execute(sql)
        applied.append(rev)
    return applied


async def assert_rewound(conn: asyncpg.Connection) -> list[str]:
    """断言基线真的回去了。

    不做这件事，「回退失败但没报错」会让后续所有用例在一个混合状态的库上运行，
    而且照样能跑出「通过」——静默的假基线比明确的失败危险得多。
    返回 (检查项, 实测值) 列表，由调用方决定记 PASS 还是 FAIL。
    """
    checks = {
        "归属三列外键": "SELECT count(*) FROM pg_constraint WHERE contype='f' "
        "AND conname LIKE 'fk\\_%\\_project\\_tenant\\_org'",
        "两列组织外键": "SELECT count(*) FROM pg_constraint WHERE contype='f' "
        "AND conname = 'fk_production_routes_organization_tenant'",
        "例外映射表": "SELECT (to_regclass('project_planning.ownership_repair_map') IS NOT NULL)::int",
        "例外判定函数": "SELECT (to_regprocedure("
        "'project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text)') IS NOT NULL)::int",
        "RLS 表达式备份表": "SELECT (to_regclass("
        "'project_planning.rls_policy_backup') IS NOT NULL)::int",
        # 批次二的三条单独列一项：聚合数已经能兜住（少了就非 0），
        # 但报告里明确出现「批次二=0」才能让人看出**这一层**也被撤了，
        # 而不是只看到一个总和。这一轮新增的迁移最容易在回退清单里被漏掉。
        "批次二归属外键": "SELECT count(*) FROM pg_constraint WHERE contype='f' "
        "AND conname IN ('fk_qc_reports_project_tenant_org',"
        "'fk_shot_specs_project_tenant_org','fk_approval_steps_project_tenant_org')",
    }
    out: list[str] = []
    for label, sql in checks.items():
        val = await conn.fetchval(sql)
        out.append(f"{label}={val}")
    return out


async def purge_business_rows(conn: asyncpg.Connection) -> str:
    """清空克隆库的业务行，使回退可达。

    为什么实验室里允许清数据，而不是把模板库当成干净的
    --------------------------------------------------
    归属五层迁移的激活演练是在模板库 `ai_film_industrial` 上做的，那一步已经把
    一行 `asset_versions` 订正**指向了隔离项目**。而 `c1d2` 的降级按设计会在
    「隔离项目仍被引用」时被外键拒绝（这是正确的：删掉隔离项目等于抹掉那行的
    归属事实）。于是模板库不再是「激活前基线」。

    实验室是一次性副本，被测命题只关心**结构**基线，因此在此清空业务行并如实
    记录清空量——不这么做，就等于在脚本里悄悄假设「模板还干净」，而那个假设
    已经失效了。`session_replication_role=replica` 是必需的：个别表装了
    禁止 TRUNCATE 的触发器，正常模式下会拦下这次清理。
    """
    before_projects = await conn.fetchval("SELECT count(*) FROM project_planning.projects")
    before_assets = await conn.fetchval("SELECT count(*) FROM asset_service.asset_versions")
    await conn.execute("SET session_replication_role = replica")
    try:
        await conn.execute("TRUNCATE project_planning.organizations CASCADE")
    finally:
        await conn.execute("SET session_replication_role = origin")
    after = await conn.fetchval("SELECT count(*) FROM project_planning.projects")
    return f"projects {before_projects}→{after}，asset_versions {before_assets}→0"


async def prepare_pre_activation_baseline(
    conn: asyncpg.Connection, versions_dir: str | Path, stack: tuple[str, ...] = OWNERSHIP_STACK
) -> tuple[str, list[str], list[str]]:
    """清业务行 → 逆序回退归属栈 → 断言基线。返回 (清理说明, 回退顺序, 断言明细)。

    三步必须绑在一起：只回退不清行会被外键拒绝；只清行不回退就还在激活后状态；
    回退不断言则「回退失败但没报错」会让后续用例跑在混合库上并照样「通过」。
    """
    purge_detail = await purge_business_rows(conn)
    order = await rewind_ownership_stack(conn, versions_dir, stack)
    checks = await assert_rewound(conn)
    return purge_detail, order, checks


async def fn_hash(conn: asyncpg.Connection, schema: str, name: str) -> str:
    body = await conn.fetchval(
        "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = $1 AND p.proname = $2",
        schema,
        name,
    )
    return hashlib.sha256((body or "").encode()).hexdigest()


# ---------------------------------------------------------------------------
# 断言执行
# ---------------------------------------------------------------------------
async def case(
    conn: asyncpg.Connection,
    setup: list[str],
    action: str,
    *,
    expect_state: str | None = None,
    expect_changed: int | None = None,
) -> tuple[bool, str]:
    """在单事务内执行 setup + action，返回 (是否符合预期, 说明)。

    ``expect_state`` 与 ``expect_changed`` 至少给一个，否则「没报错」会被当成通过——
    这正是本项目反复出现的真空真陷阱。
    """
    try:
        async with conn.transaction():
            for s in setup:
                await conn.execute(s)
            status = await conn.execute(action)
    except asyncpg.PostgresError as exc:
        state = exc.sqlstate
        msg = str(exc).split("\n")[0][:110]
        if expect_state is None:
            return False, f"意外报错 {state}: {msg}"
        if state != expect_state:
            return False, f"SQLSTATE 不符：期望 {expect_state} 实得 {state}: {msg}"
        return True, f"{state}: {msg}"
    except Exception as exc:  # noqa: BLE001 - 非 PostgresError 也必须暴露为失败
        return False, f"脚本异常 {type(exc).__name__}: {exc}"

    if expect_state is not None:
        return False, f"期望 {expect_state} 但语句成功（{status}）"
    if expect_changed is not None:
        try:
            n = int(status.split()[-1])
        except (ValueError, IndexError):
            n = -1
        if n != expect_changed:
            return False, f"影响行数 {n}，期望 {expect_changed}（静默 0 行不等于成功）"
        return True, status
    return True, status


# ---------------------------------------------------------------------------
# 记录与报告
# ---------------------------------------------------------------------------
class Recorder:
    """三态记录器：PASS / FAIL / N/A。

    为什么必须有 N/A
    ----------------
    「不适用」与「通过」是两件事。若把「该表按设计不建归属外键、故 N5 无从执行」
    记成 PASS，通过率会虚高；记成 FAIL 又等于把设计决策说成缺陷。
    两者都会让报告失去可读性，所以单列一态，且**不计入通过率**。
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.open_findings: list[dict[str, str]] = []

    def rec(self, cid: str, phase: str, desc: str, ok: bool, detail: str = "") -> None:
        self.rows.append({"id": cid, "phase": phase, "desc": desc, "ok": ok, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {cid:<6} {desc}  {detail}")

    def note(self, cid: str, phase: str, desc: str, detail: str) -> None:
        """不适用：前置条件按设计不存在，用例无从执行。"""
        self.rows.append({"id": cid, "phase": phase, "desc": desc, "ok": None, "detail": detail})
        print(f"[N/A ] {cid:<6} {desc}  {detail}")

    def finding(self, cid: str, desc: str, gap: str) -> None:
        """把 N/A 背后的真实缺口显式登记，避免「不适用」变成「没人管」。"""
        self.open_findings.append({"id": cid, "desc": desc, "gap": gap})
        print(f"[OPEN] {cid:<6} {desc}  {gap}")

    @property
    def passed(self) -> int:
        return sum(1 for r in self.rows if r["ok"] is True)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r["ok"] is False)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.rows if r["ok"] is None)

    @property
    def total(self) -> int:
        return self.passed + self.failed

    def write_report(
        self,
        out_md: Path,
        out_json: Path,
        *,
        title: str,
        meta: list[str],
        footer: list[str] | None = None,
    ) -> str:
        ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        lines = [f"# {title}", "", f"- 执行时间：{ts}"]
        lines += [f"- {m}" for m in meta]
        summary = f"- 结果：**{self.passed}/{self.total}**"
        if self.skipped:
            summary += f"（另有 {self.skipped} 条 N/A，不计入通过率）"
        lines += [summary, ""]
        marks = {True: "PASS", False: "**FAIL**", None: "N/A"}
        lines += ["| # | 阶段 | 用例 | 结果 | 说明 |", "| --- | --- | --- | --- | --- |"]
        for r in self.rows:
            lines.append(
                f"| {r['id']} | {r['phase']} | {r['desc']} | {marks[r['ok']]} | {r['detail']} |"
            )
        if self.open_findings:
            lines += ["", "## 待裁决缺口（N/A 背后的真实问题，不得并入下一轮「已修」）", ""]
            lines += ["| # | 事项 | 缺口 |", "| --- | --- | --- |"]
            for f in self.open_findings:
                lines.append(f"| {f['id']} | {f['desc']} | {f['gap']} |")
        if footer:
            lines += ["", *footer]
        out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out_json.write_text(
            json.dumps(
                {
                    "at": ts,
                    "passed": self.passed,
                    "failed": self.failed,
                    "not_applicable": self.skipped,
                    "open_findings": self.open_findings,
                    "results": self.rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return ts
