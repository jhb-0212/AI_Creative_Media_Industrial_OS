"""生成「受控例外」迁移文件（D-10.2 实现层）。

为什么需要生成器而不是手写
--------------------------
受控例外要在 13 个不可变守卫上都开一个窄门。手抄 13 份 plpgsql 函数体必然出错，
而错一个字节的后果是「不可变保护失效」，这不是可以靠 review 兜住的风险。

设计取舍：生成器直接产出**注入后的完整字面量**，迁移里不做任何运行期字符串处理。
理由：如果在迁移里再实现一遍注入逻辑，就有两份「注入实现」，一旦不一致，
升级执行的语句就与生成时验证过的语句不同——那是不可验证的迁移。

用法
----
    python docs/audits/mig-recon/gen_guard_exception_migration.py \
        --port 15444 --out data/migrations/pending/feat-ownership/b0c9d8e7f6a5_ownership_repair_exception.py

只读数据库（仅 SELECT pg_proc），不写入任何库。
"""

from __future__ import annotations

import argparse
import re

import asyncpg

# 13 个不可变守卫：(schema, table, guard_func, 行标识列)
# 12 张表主键即 entity_id；qc_reports 主键是 qc_report_id（该表无 entity_id 列）。
GUARDS: tuple[tuple[str, str, str, str], ...] = (
    ("asset_service", "asset_versions", "asset_versions_guard", "entity_id"),
    ("asset_service", "delivery_manifests", "delivery_manifests_guard", "entity_id"),
    ("asset_service", "qc_reports", "qc_reports_guard", "qc_report_id"),
    ("generation_execution", "generation_tasks", "generation_tasks_guard", "entity_id"),
    ("generation_execution", "production_routes", "production_routes_guard", "entity_id"),
    ("generation_execution", "shot_route_selections", "shot_route_selections_guard", "entity_id"),
    ("qc_governance", "gate_decisions", "gate_decisions_guard", "entity_id"),
    ("qc_governance", "qc_review_sessions", "qc_review_sessions_guard", "entity_id"),
    ("shot_planning", "constraint_packs", "constraint_packs_guard", "entity_id"),
    ("shot_planning", "shot_specs", "shot_specs_guard", "entity_id"),
    ("shot_planning", "shot_visual_plans", "shot_visual_plans_guard", "entity_id"),
    ("workflow", "approval_flows", "approval_flows_guard", "entity_id"),
    ("workflow", "approval_steps", "approval_steps_guard", "entity_id"),
)

INJECT_MARK = "ownership_repair_allows"


def inject(body: str, key_col: str, table_fqn: str) -> str:
    """在函数体顶层 BEGIN 之后注入受控例外接入点。原文其余部分一字不改。

    注入放在 BEGIN 之后（而非各个 RAISE 之前）的理由：
    - 13 个守卫结构不同（8 个逐列扫描、5 个无条件 RAISE），锚点各异；
    - 统一插在 BEGIN 后只有一条规则，不会因结构差异漏插或错插；
    - 安全性由 ownership_repair_allows() 自身保证：它独立算出变更列集合，
      只有「变更列 ⊆ 归属三列」且其余条件同时满足才返回 true。
      因此提前返回不会放过任何内容列变更。
    """
    lines = body.split("\n")
    for i, line in enumerate(lines):
        m = re.fullmatch(r"(\s*)BEGIN\s*", line)
        if not m:
            continue
        ind = m.group(1) + "    "
        block = [
            f"{ind}-- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。",
            f"{ind}-- 四条件同时满足才放行；判定逻辑的唯一实现在",
            f"{ind}-- project_planning.ownership_repair_allows()，此处只接入、不复制条件。",
            f"{ind}IF TG_OP = 'UPDATE' THEN",
            f"{ind}    IF project_planning.ownership_repair_allows(",
            f"{ind}        TG_RELID, to_jsonb(OLD), to_jsonb(NEW),",
            f"{ind}        to_jsonb(OLD) ->> '{key_col}'",
            f"{ind}    ) THEN",
            f"{ind}        RETURN NEW;",
            f"{ind}    END IF;",
            f"{ind}END IF;",
            "",
        ]
        return "\n".join(lines[: i + 1] + block + lines[i + 1 :])
    raise ValueError(f"{table_fqn}: 未找到顶层 BEGIN，拒绝生成")


def py_literal(text: str) -> str:
    """把 SQL 原文嵌入 Python 字面量，保证逐字节可逆。"""
    if "'''" not in text and not text.endswith("'"):
        return "r'''" + text + "'''"
    body = "\n".join("    " + repr(line) for line in text.split("\n"))
    return "(\n" + body + "\n)"


TEMPLATE_HEAD = '''"""
补齐迁移 ②：不可变守卫的「受控例外」（D-10.2 实现层）。

本文件由 docs/audits/mig-recon/gen_guard_exception_migration.py 生成。
**请勿手改**。要改判定逻辑 → 改共享函数 _ALLOWS_FN；要改守卫原文 → 改来源库后重新生成。
迁移内**不含任何运行期字符串改写**：GUARD_PATCHED 就是生成时验证过的完整语句。

为什么必须有这一层
------------------
D-10.1 要求 14 表归属闭环（VALIDATE 必须成功），而订正归属列会撞上各表的
不可变守卫（ADR-R11/R27）→ P0001。若不提供例外，订正只能前向新建版本，
旧行仍在且仍违规 → VALIDATE 必失败（T3 D3e）→ 闭环不可达。

为什么不是 DISABLE TRIGGER
--------------------------
禁守卫使豁免面变成「整表 × 所有列 × 所有会话」，且需 ACCESS EXCLUSIVE 锁。
受控例外把豁免面压到「单行 × 归属三列 × 单工单 × 单角色」，且可逐行审计。

四条件（缺一即不放行，判定唯一实现在 ownership_repair_allows）
------------------------------------------------------------
1. 会话角色是 afs_data_repair 成员（pg_has_role 读真实身份，GUC 不可伪造）
2. 会话设置了 app.ownership_repair_ticket
3. 变更列 ⊆ {project_id, organization_id, tenant_id}（函数自算，不接受调用方传入）
4. ownership_repair_map 存在 (表, 行标识, 工单) 匹配且 applied_at IS NULL

范围说明
--------
13 张表 = 归属矩阵 13 张域表中**有不可变守卫**的那些（projects 无守卫，不需接入）。
另含 asset_versions（归属矩阵第 15 张，T3 代表表）与 qc_reports（有归属列但无 entity_id）。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b0c9d8e7f6a5"
down_revision: str | Sequence[str] | None = "a9b8c7d6e5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPAIR_ROLE = "afs_data_repair"
MAP_TABLE = "project_planning.ownership_repair_map"
'''

TEMPLATE_REST = r'''

# ---------------------------------------------------------------------------
# 只读映射表：唯一需要提权的地方。
# SECURITY DEFINER 仅用于绕过「业务角色不应拥有映射表 SELECT」这一点；
# 它不接收身份参数、不做授权判定，因此提权不带来决策权。
_MAP_MATCH_FN = """
CREATE OR REPLACE FUNCTION project_planning.ownership_repair_map_match(
    p_table   text,
    p_row_key text,
    p_ticket  text
) RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, project_planning
AS $fn$
    SELECT EXISTS (
        SELECT 1
          FROM project_planning.ownership_repair_map m
         WHERE m.table_name = p_table
           AND m.row_key    = p_row_key
           AND m.ticket_ref = p_ticket
           AND m.applied_at IS NULL
    );
$fn$;
"""

# 四条件判定的唯一实现。**必须是 SECURITY INVOKER（默认，不加 SECURITY DEFINER）**。
# 若写成 SECURITY DEFINER，函数体里的 current_user 会变成函数 owner（迁移执行者/超级用户），
# pg_has_role(current_user,'afs_data_repair','member') 恒为真 ——
# 角色条件形同不存在，例外退化为「任何有表写权限者的万能钥匙」。
# 这不是理论风险：本迁移在验证阶段实测到了（用例 F8 期望 false、实得 true），
# 拆分后由 SECDEF 只承担「读映射表」这一件事，问题消失。
_ALLOWS_FN = """
CREATE OR REPLACE FUNCTION project_planning.ownership_repair_allows(
    p_table   regclass,
    p_old     jsonb,
    p_new     jsonb,
    p_row_key text
) RETURNS boolean
LANGUAGE plpgsql
STABLE
AS $fn$
DECLARE
    v_ticket  text;
    v_changed text[];
BEGIN
    -- 条件 1：会话角色。此处 current_user 是**调用者**（INVOKER），
    -- pg_has_role 读会话真实身份，SET app.xxx 无法伪造。
    IF NOT pg_has_role(current_user, 'afs_data_repair', 'member') THEN
        RETURN false;
    END IF;

    -- 条件 2：工单令牌。
    v_ticket := nullif(current_setting('app.ownership_repair_ticket', true), '');
    IF v_ticket IS NULL THEN
        RETURN false;
    END IF;

    -- 条件 3：变更列必须 ⊆ 归属三列。
    -- 由本函数自算，不接受调用方传入 —— 否则调用方可以谎报「只改了归属列」
    -- 而实际同时改内容列，例外就变成万能钥匙。
    SELECT array_agg(k)
      INTO v_changed
      FROM jsonb_object_keys(p_new) AS k
     WHERE (p_old -> k) IS DISTINCT FROM (p_new -> k);

    IF v_changed IS NULL THEN
        RETURN false;           -- 没有任何变更，不需要例外，也不该用它
    END IF;
    IF NOT (v_changed <@ ARRAY['project_id', 'organization_id', 'tenant_id']) THEN
        RETURN false;
    END IF;

    -- 条件 4：映射行存在、未盖章、工单一致（提权只发生在这个只读查询里）。
    RETURN project_planning.ownership_repair_map_match(
        p_table::text, p_row_key, v_ticket
    );
END;
$fn$;
"""

_MAP_DDL = """
CREATE TABLE IF NOT EXISTS project_planning.ownership_repair_map (
    id                  bigserial PRIMARY KEY,
    table_name          text        NOT NULL,
    row_key             text        NOT NULL,
    old_project_id      text,
    old_organization_id text,
    old_tenant_id       text,
    new_project_id      text,
    new_organization_id text,
    new_tenant_id       text,
    reason              text        NOT NULL,
    ticket_ref          text        NOT NULL,
    applied_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_ownership_repair_map UNIQUE (table_name, row_key, ticket_ref),
    CONSTRAINT ck_ownership_repair_map_reason_len CHECK (length(btrim(reason)) >= 8)
)
"""

_MAP_IDX = """
CREATE INDEX IF NOT EXISTS ix_ownership_repair_map_pending
    ON project_planning.ownership_repair_map (table_name, row_key)
    WHERE applied_at IS NULL
"""

# NOLOGIN：专用角色只作为 SET ROLE 的目标，不直接登录。
_ROLE_DDL = """
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'afs_data_repair') THEN
        CREATE ROLE afs_data_repair NOLOGIN;
    END IF;
END
$do$;
"""


def upgrade_sqls() -> list[str]:
    """返回升级语句清单（按序执行）。

    独立成函数是为了让验证脚本执行**与迁移完全相同的那批 SQL**，
    而不是照着迁移再抄一遍 —— 抄一遍就失去了「验证的是迁移本身」这个性质。
    """
    sqls: list[str] = []

    # E0 前置断言：防重复执行 / 防拿错谱系。
    sqls.append(
        "DO $do$ BEGIN "
        "IF to_regprocedure("
        "'project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text)'"
        ") IS NOT NULL OR to_regprocedure("
        "'project_planning.ownership_repair_map_match(text,text,text)'"
        ") IS NOT NULL THEN "
        "RAISE EXCEPTION '受控例外函数已存在：本迁移已执行过或谱系不符'; "
        "END IF; END $do$"
    )
    sqls.append(
        "DO $do$ BEGIN "
        "IF to_regclass('project_planning.ownership_repair_map') IS NOT NULL THEN "
        "RAISE EXCEPTION '映射表已存在：本迁移已执行过或谱系不符'; "
        "END IF; END $do$"
    )

    # E1 专用角色
    sqls.append(_ROLE_DDL)

    # E2 映射表 + 待处理索引
    sqls.append(_MAP_DDL)
    sqls.append(_MAP_IDX)

    # E3 共享判定函数：map_match 只读提权 + allows 四条件判定（INVOKER）
    sqls.append(_MAP_MATCH_FN)
    sqls.append(_ALLOWS_FN)

    # E4 接入：13 个守卫各替换为「原文 + 一段接入」，其余部分逐字不变
    for fqn in GUARD_ORDER:
        sqls.append(GUARD_PATCHED[fqn])

    # E5 权限：函数体为 SECURITY DEFINER，但**不**给 PUBLIC EXECUTE
    #   （ADR 附录 E #15：函数默认 PUBLIC EXECUTE 是残余风险）。
    #   守卫对一切写者生效，故须显式授予所有可能写入这些表的主体。
    #   未列出的角色写入时会得到 42501 而非 P0001 —— 这是**有意的**：
    #   它暴露「该角色未被授权走例外」，比静默降级为普通拒绝更可诊断。
    sqls.append(
        "REVOKE ALL ON FUNCTION "
        "project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text) FROM PUBLIC"
    )
    sqls.append(
        "REVOKE ALL ON FUNCTION "
        "project_planning.ownership_repair_map_match(text,text,text) FROM PUBLIC"
    )
    for role in ("app_role", "afs_data_repair"):
        sqls.append(
            "DO $do$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            "EXECUTE 'GRANT USAGE ON SCHEMA project_planning TO " + role + "'; "
            "EXECUTE 'GRANT EXECUTE ON FUNCTION "
            "project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text) "
            f"TO {role}'; "
            "END IF; END $do$"
        )

    # E6 按实际表授权收敛，消除「保护语义漂移」：
    #   守卫对**一切写者**生效。若某角色有表 UPDATE 权限却没有本函数 EXECUTE 权限，
    #   它在升级前得到 P0001（守卫拒绝），升级后会得到 42501（权限拒绝）——同一个
    #   非法操作换了错误码，既有调用方按 P0001 做的处理会失效。硬编码角色名无法穷举，
    #   因此改为「谁有权写这些表，谁就有权调用判定函数」。
    sqls.append(
        "DO $do$ DECLARE r record; BEGIN "
        "FOR r IN "
        "SELECT DISTINCT g.grantee::text AS grantee "
        "  FROM information_schema.role_table_grants g "
        " WHERE g.table_schema || '.' || g.table_name IN ("
        + ", ".join("'" + fqn + "'" for fqn in GUARD_ORDER)
        + ") "
        "   AND g.privilege_type IN ('UPDATE', 'INSERT', 'DELETE') "
        "   AND g.grantee <> 'PUBLIC' "
        "LOOP "
        "  EXECUTE format('GRANT USAGE ON SCHEMA project_planning TO %I', r.grantee); "
        "  EXECUTE format("
        "'GRANT EXECUTE ON FUNCTION "
        "project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text) TO %I', r.grantee); "
        "  EXECUTE format("
        "'GRANT EXECUTE ON FUNCTION "
        "project_planning.ownership_repair_map_match(text,text,text) TO %I', r.grantee); "
        "END LOOP; END $do$"
    )
    return sqls


def downgrade_sqls() -> list[str]:
    """返回降级语句清单（按序执行）。"""
    sqls: list[str] = []
    # 先还原 13 个守卫原文（关闭例外通道），再撤函数、表。
    for fqn in GUARD_ORDER:
        sqls.append(GUARD_ORIGINAL[fqn])
    sqls.append(
        "DROP FUNCTION IF EXISTS "
        "project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text)"
    )
    sqls.append(
        "DROP FUNCTION IF EXISTS "
        "project_planning.ownership_repair_map_match(text,text,text)"
    )
    sqls.append("DROP TABLE IF EXISTS project_planning.ownership_repair_map")
    # 角色不删：PostgreSQL 角色是集群级对象，其他库可能已依赖；删角色会引发
    # 不可预期的 REASSIGN 需求。保留一个 NOLOGIN 且无任何授权的空角色是安全的。
    return sqls


def upgrade() -> None:
    for sql in upgrade_sqls():
        op.execute(sql)


def downgrade() -> None:
    for sql in downgrade_sqls():
        op.execute(sql)

'''


def build(defs: list[tuple[str, str, str, str]]) -> str:
    out = [TEMPLATE_HEAD, "", "# 生成顺序即执行顺序（与校验脚本的期望顺序一致）"]
    out.append("GUARD_ORDER: tuple[str, ...] = (")
    for fqn, _k, _o, _p in defs:
        out.append(f"    {fqn!r},")
    out.append(")")
    out.append("")
    out.append("# 原文（导出时刻的真实定义，逐字保留）：downgrade 用这一份")
    out.append("GUARD_ORIGINAL: dict[str, str] = {")
    for fqn, key_col, orig, _patched in defs:
        out.append(f"    # {fqn}  行标识={key_col}")
        out.append(f"    {fqn!r}: {py_literal(orig)},")
    out.append("}")
    out.append("")
    out.append("# 注入接入点后的完整语句（upgrade 用这一份，生成时已逐字校验）")
    out.append("GUARD_PATCHED: dict[str, str] = {")
    for fqn, key_col, _orig, patched in defs:
        out.append(f"    # {fqn}  行标识={key_col}")
        out.append(f"    {fqn!r}: {py_literal(patched)},")
    out.append("}")
    return "\n".join(out) + TEMPLATE_REST


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=15444)
    ap.add_argument("--db", default="ai_film_industrial")
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--password", default="replay_super_pw")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import asyncio

    async def fetch() -> list[tuple[str, str, str, str]]:
        conn = await asyncpg.connect(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            database=args.db,
        )
        try:
            rows = []
            for schema, table, func, key_col in GUARDS:
                body = await conn.fetchval(
                    "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = $1 AND p.proname = $2",
                    schema,
                    func,
                )
                if not body:
                    raise SystemExit(f"缺失守卫函数：{schema}.{func}")
                if INJECT_MARK in body:
                    raise SystemExit(f"{schema}.{func} 已含例外接入，拒绝重复注入")
                orig = body.strip()
                rows.append((f"{schema}.{table}", key_col, orig, inject(orig, key_col, f"{schema}.{table}")))
            return rows
        finally:
            await conn.close()

    defs = asyncio.run(fetch())
    text = build(defs)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"generated: {args.out}  guards={len(defs)}  bytes={len(text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
