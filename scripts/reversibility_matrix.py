"""迁移可逆性矩阵 V2：从"数条数"升级为"比集合"。

为什么升级
----------
V1 只比较 upgrade/downgrade 的动作计数：``GRANT 12 ↔ REVOKE 12`` 会被判对称，
但两个集合可能完全不重叠（不同对象、不同角色、不同权限、不同 grant option）。
数量相等不是可逆性证据，只是"看起来像"。

V2 把每条语句解析成可比较的动作元组::

    (动作方向, 对象范围, 对象名, 角色, 权限集, grant option)

然后做双向匹配：

* upgrade ``GRANT`` ↔ downgrade ``REVOKE``（同一对象/同一角色/同一权限集）
* upgrade ``REVOKE`` ↔ downgrade ``GRANT``（撤掉的既有授权必须按原主体恢复）
  ——这是 R1 的机检版本：只认集合相等，不认"有 GRANT 就算恢复"
* downgrade 多出来的 ``GRANT`` = 权限扩大（比漏恢复更危险，单独标记）
* ``CREATE ROLE`` ↔ ``DROP ROLE``、``CREATE POLICY`` ↔ ``DROP POLICY`` 按名字配对

分类不是自动结论。每个不对称 revision 必须落在三类之一，并带理由与证据：

* ``no_restore_proven``：已证明升级撤的权限本来就不存在（no-op），不需要恢复
* ``intentional_one_way``：已证明是安全收紧，降级不得回退
* ``unproven_blocking``：证据不足，阻断闸门，交给 Owner

默认 ``--check`` 在有 ``unproven_blocking`` 或未分类不对称项时返回非零。
脚本只读 JSON、不连库、不改迁移。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_INVENTORY = _REPO_ROOT / "docs" / "audits" / "2026-11-27-identity-migration-inventory.json"
_DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "audits" / "2026-11-27-wp04-reversibility-matrix.json"
_DEFAULT_SNAPSHOT = _REPO_ROOT / "docs" / "audits" / "2026-11-27-wp04-bidirectional-snapshot.json"

CLASSIFICATIONS = ("no_restore_proven", "restore_required", "intentional_one_way", "unproven_blocking")

#: 允许从"往返快照差异"中排除的语义规则。每一条都必须能说明"为什么与身份语义无关"，
#: 并且有测试证明排除范围恰好只覆盖这一类。禁止用正则兜底吞掉未知差异。
SNAPSHOT_IGNORE_RULES: dict[str, str] = {
    "pg_toast_oid": (
        "pg_toast / pg_toast_<oid> 的 OID 抖动：TOAST 表在表重建/重写时由 PG 内部重新分配，"
        "名称里的 OID 不承载身份语义，且其 ACL 不参与业务授权。"
        "由 test_snapshot_ignore_rules_are_narrow 限定为只匹配 pg_toast 前缀。"
    ),
    "acl_item_order": (
        "同一对象 ACL 的条目顺序：规范化快照按 role=PRIV[,role=PRIV] 排序后比较，"
        "原文本顺序不是语义；只允许该字段级别的差异。"
    ),
}

#: 每条不对称 revision 的人工结论：必须同时给 reason 与 evidence，缺一即视为未分类。
CLASSIFIED: dict[str, dict[str, str]] = {
    "e5f6a7b8c9d0": {
        "class": "no_restore_proven",
        "reason": "upgrade 撤销的是本 revision 自己刚建的 outbox.list_active_tenants() 上的 PUBLIC EXECUTE，降级删函数即消失",
        "evidence": "docs/audits/2026-11-27-wp04-s1-s3-and-d08-evidence.md §4",
    },
    "d8e9f0a1b2c3": {
        "class": "no_restore_proven",
        "reason": "audit.status_transitions 由本 revision 创建，只 GRANT INSERT,SELECT；随后 REVOKE UPDATE/DELETE/TRUNCATE 撤的是角色从未拥有的权限（S1 实测 relacl 无差异）",
        "evidence": "docs/audits/2026-11-27-wp04-s1-s3-and-d08-evidence.md §1",
    },
    "f9a0b1c2d3e4": {
        "class": "no_restore_proven",
        "reason": "父 revision 与升级后 pg_default_acl 均只有 outbox 一条且逐字段相同，10 条 ALTER DEFAULT PRIVILEGES REVOKE 为 no-op（更早 #35/#37/#38 已撤）",
        "evidence": "docs/audits/2026-11-27-wp04-s1-s3-and-d08-evidence.md §2",
    },
    "f8c9d0e1a2b3": {
        "class": "no_restore_proven",
        "reason": (
            "对象与本 revision 同批创建，撤 PUBLIC 撤的是 PG 建函数自动带的默认权限；"
            "另补齐维护 owner 对 qc_governance 的 USAGE 与 qc_tasks 的 SELECT 的降级撤权"
            "（完整 ACL 快照下曾报 restore_required）"
        ),
        "evidence": "docs/audits/2026-11-27-wp04-f3-p41-verification.md §3",
    },
    "b4c5d6e7f8a9": {
        "class": "no_restore_proven",
        "reason": "qc_maintenance.repair_tenant_task 由本 revision 创建，proacl 中 PUBLIC 从不存在（S3 实测），降级删函数天然满足",
        "evidence": "docs/audits/2026-11-27-wp04-s1-s3-and-d08-evidence.md §3",
    },
    # 隔离库往返实验（父 revision 快照 P vs 降级后 D），结果见 2026-11-27-wp04-triage.md
    "d7e8f9a0b1c2": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 父态=降级态（P==D，0 差异），升级撤权为 no-op",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "e8f9a0b1c2d3": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "f1a2b3c4d5e6": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "e1f2a3b4c5d6": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异；数据守卫在降级前置，未触发",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "e4f5a6b7c8d9": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "c5d6e7f8a9b0": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异（角色退役点的撤权为 no-op）",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "a8b9c0d1e2f3": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    # 以下 10 条曾在首轮实测为 restore_required；2026-11-27 整改轮已按父态语义
    # 修复并复跑 P→upgrade→downgrade→D，结果为 0 差异，故降级为 no_restore_proven
    # （保留原差异描述作回归线索：若哪天又出现同类差异，原因可追溯）。
    "a13b0c1d2e3f": {
        "class": "no_restore_proven",
        "reason": "已修复（WP-04 整改轮）：降级后残留 upgrade 创建的唯一索引 uq_episodes_entity_project",
        "evidence": "docs/audits/2026-11-27-wp04-fix-round.json",
    },
    "c3d4e5f6a7b8": {
        "class": "no_restore_proven",
        "reason": "已修复（WP-04 整改轮）：降级后残留唯一索引 uq_shot_revisions_entity_project",
        "evidence": "docs/audits/2026-11-27-wp04-fix-round.json",
    },
    "b7c8d9e0f1a2": {
        "class": "no_restore_proven",
        "reason": "已修复（WP-04 整改轮）：降级多给 public.alembic_version ACL，并把 outbox 默认权限从 arw 扩到 arwd（越权恢复）",
        "evidence": "docs/audits/2026-11-27-wp04-fix-round.json",
    },
    "c6d7e8f9a0b1": {
        "class": "no_restore_proven",
        "reason": "已修复（WP-04 整改轮）：降级后 auth schema 出现 ACL（父态无），多授 USAGE",
        "evidence": "docs/audits/2026-11-27-wp04-fix-round.json",
    },
    "b3c4d5e6f7a8": {
        "class": "no_restore_proven",
        "reason": "已修复（WP-04 整改轮）：降级给 project_planning schema 多授 asset_service_role USAGE（父态无）",
        "evidence": "docs/audits/2026-11-27-wp04-fix-round.json",
    },
    "b8c9d0e1f2a3": {
        "class": "no_restore_proven",
        "reason": "已修复（WP-04 整改轮）：降级后 5 个 schema 的 USAGE 多给 3 个角色（父态仅部分持有）",
        "evidence": "docs/audits/2026-11-27-wp04-fix-round.json",
    },
    "e6a7b8c9d0e1": {
        "class": "no_restore_proven",
        "reason": "已修复（WP-04 整改轮）：降级后 qc_maintenance schema 多出 operator/login USAGE，函数 assert_signature_event_exists 的 ACL 残留",
        "evidence": "docs/audits/2026-11-27-wp04-fix-round.json",
    },
    "a5b6c7d8e9f0": {
        "class": "no_restore_proven",
        "reason": "已修复（WP-04 整改轮）：降级后 qc_maintenance 默认权限与 enforce_work_order_transition 函数残留",
        "evidence": "docs/audits/2026-11-27-wp04-fix-round.json",
    },
    "c1a2b3d4e5f6": {
        "class": "no_restore_proven",
        "reason": "已修复（WP-04 整改轮）：降级后 3 个新 schema 默认权限与 asset_service 默认权限被扩到多角色（19 组差异）",
        "evidence": "docs/audits/2026-11-27-wp04-fix-round.json",
    },
    "a0b1c2d3e4f5": {
        "class": "no_restore_proven",
        "reason": "已修复（WP-04 整改轮）：降级后 gate_decisions 表 ACL 出现（父态无）且父态 ix_..._gate_key 索引消失（结构损失）",
        "evidence": "docs/audits/2026-11-27-wp04-fix-round.json",
    },
    "f6a7b8c9d1e2": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异（清掉 pg_toast OID 抖动后）",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "c1d2e3f4a5b6": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "b5c6d7e8f9a0": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "a1b2c3d4e5f6": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异（PUBLIC 撤权/EXECUTE 授予均无残留）",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "e0f1a2b3c4d5": {
        "class": "no_restore_proven",
        "reason": "隔离库实测 P==D 0 差异（建角色/授 schema 均在降级中被清除）",
        "evidence": "docs/audits/2026-11-27-wp04-triage-results.json",
    },
    "8f3b2c1d4e5a": {
        "class": "no_restore_proven",
        "reason": (
            "F3：升级不再 DROP ROLE；降级按父态逐表还原 app_role 授权，并对称撤销本"
            "migration 授出的 schema/TABLE/SEQUENCE/默认权限（含交叉默认权限与 public USAGE）。"
            "P→upgrade→downgrade→D 快照 0 差异；两域角色降级后仍存在，B 库授权快照不变。"
            "范围仅限本库/本 migration 管理面，cluster_wide_zero_authorization_verified 保持 false"
        ),
        "evidence": (
            "docs/audits/2026-11-27-wp04-f3-p41-verification.md §8；"
            "docs/audits/2026-11-27-wp04-n24-triage.json；"
            "分类变更记录含原 unproven_blocking 依据："
            "docs/audits/2026-11-27-wp04-n24-classification-change.json"
        ),
    },
}

_SCOPE_RE = re.compile(r"\bON\s+(?P<scope>.+?)\s+(?:TO|FROM)\s+(?P<tail>.+)$", re.IGNORECASE)
_TAIL_RE = re.compile(
    r"^(?P<role>[A-Za-z0-9_\"\.]+)"
    r"(?P<gopt>\s+WITH\s+GRANT\s+OPTION)?"
    r"(\s+(?:CASCADE|RESTRICT))?\s*$",
    re.IGNORECASE,
)
_PRIVS_HEAD_RE = re.compile(
    r"^(?:ALTER\s+DEFAULT\s+PRIVILEGES\s+IN\s+SCHEMA\s+(?P<dflt_schema>[A-Za-z0-9_\"]+)\s+)?"
    r"(?P<verb>GRANT|REVOKE)"
    r"(?:\s+GRANT\s+OPTION\s+FOR)?"
    r"\s+(?P<privs>.+?)\s+ON\s+",
    re.IGNORECASE | re.DOTALL,
)


def _norm_name(raw: str) -> str:
    return raw.replace('"', "").replace("'", "").strip().lower()


def _norm_privs(raw: str) -> str:
    # GRANT UPDATE (col_a, col_b) 的列清单不是权限，先剥掉
    scrubbed = re.sub(r"(UPDATE|INSERT|REFERENCES)\s*\([^)]*\)", r"\1", raw, flags=re.I)
    tokens = {_norm_privs_token(part) for part in re.split(r",", scrubbed) if part.strip()}
    if "ALL PRIVILEGES" in tokens:
        tokens = {"ALL"}
    return "|".join(sorted(tokens))


def _norm_privs_token(token: str) -> str:
    value = " ".join(token.split()).upper()
    return "ALL" if value in {"ALL", "ALL PRIVILEGES"} else value


def _split_scope(scope: str) -> tuple[str, str]:
    """把 ``ON ...`` 部分拆成 (范围类型, 对象名)。"""
    text = " ".join(scope.split())
    upper = text.upper()
    if upper in {"TABLES", "SEQUENCES", "FUNCTIONS", "TYPES", "SCHEMAS"}:
        return "default:" + upper.lower(), ""
    for prefix, kind in (
        ("ALL TABLES IN SCHEMA ", "all_tables"),
        ("ALL SEQUENCES IN SCHEMA ", "all_sequences"),
        ("ALL FUNCTIONS IN SCHEMA ", "all_functions"),
        ("TABLE ", "table"),
        ("SEQUENCE ", "sequence"),
        ("FUNCTION ", "function"),
        ("SCHEMA ", "schema"),
    ):
        if upper.startswith(prefix):
            return kind, _norm_name(text[len(prefix) :])
    return "object", _norm_name(text)


def _row_roles(row: dict[str, Any], tail_match: re.Match[str] | None) -> list[str]:
    raw = row.get("roles")
    if isinstance(raw, list) and raw:
        return [_norm_name(str(item)) for item in raw]
    if isinstance(raw, str) and raw:
        return [_norm_name(part) for part in re.split(r",", raw) if part.strip()]
    if tail_match:
        return [_norm_name(part) for part in re.split(r",", tail_match.group("role")) if part.strip()]
    return []



def parse_actions(row: dict[str, Any]) -> list[dict[str, str]]:
    """把一条盘点行解析为一组可比较的动作元组（多角色语句展开为多条）。"""
    sql = " ".join(str(row.get("sql") or "").split())
    if not sql:
        return []
    operation = str(row.get("operation") or "")
    direction = str(row.get("direction") or "")

    if operation in {"create_role", "drop_role"}:
        match = re.search(r"(?:CREATE|DROP)\s+ROLE\s+(?:IF\s+EXISTS\s+)?(?P<role>[A-Za-z0-9_\"\.]+)", sql, re.I)
        if not match:
            return []
        return [
            {
                "direction": direction,
                "kind": operation,
                "scope": "role",
                "object": _norm_name(match.group("role")),
                "role": "",
                "privs": "",
                "grant_option": "",
            }
        ]

    if operation == "policy":
        match = re.search(
            r"(?:CREATE|DROP)\s+POLICY\s+(?P<name>[A-Za-z0-9_\"]+)\s+ON\s+(?P<obj>[A-Za-z0-9_\"\.]+)",
            sql,
            re.I,
        )
        if not match:
            return []
        return [
            {
                "direction": direction,
                "kind": "policy:" + ("create" if sql.upper().startswith("CREATE") else "drop"),
                "scope": "policy",
                "object": f"{_norm_name(match.group('obj'))}:{_norm_name(match.group('name'))}",
                "role": "",
                "privs": "",
                "grant_option": "",
            }
        ]

    if operation not in {"grant", "revoke", "default_privs"}:
        return []

    head = _PRIVS_HEAD_RE.match(sql)
    scope_match = _SCOPE_RE.search(sql)
    if not head or not scope_match:
        return []
    tail = scope_match.group("tail")
    tail_match = _TAIL_RE.match(tail)
    roles = _row_roles(row, tail_match)

    scope_kind, object_name = _split_scope(scope_match.group("scope"))
    verb = head.group("verb").upper()
    is_default = bool(head.group("dflt_schema"))
    if is_default:
        scope_kind = scope_kind if scope_kind.startswith("default:") else "default:object"
        object_name = _norm_name(head.group("dflt_schema"))

    grant_option = "with_grant_option" if (tail_match and tail_match.group("gopt")) else "plain"
    return [
        {
            "direction": direction,
            "kind": verb.lower(),
            "scope": scope_kind,
            "object": object_name,
            "role": role,
            "privs": _norm_privs(head.group("privs")),
            "grant_option": grant_option,
        }
        for role in roles
    ]


def _key(action: dict[str, str]) -> tuple[str, ...]:
    return (
        action["kind"],
        action["scope"],
        action["object"],
        action["role"],
        action["privs"],
        action["grant_option"],
    )


_VERSIONS_DIR = _REPO_ROOT / "data" / "migrations" / "versions"

_DDL_OPS = {
    "create_table": "table",
    "drop_table": "table",
    "create_schema": "schema",
    "drop_schema": "schema",
    "drop_column": "column",
    "create_index": "index",
    "drop_index": "index",
}

_SQL_DDL_RE = re.compile(
    r"\b(?:CREATE|DROP|ALTER)\s+(?:OR\s+REPLACE\s+)?(TABLE|SCHEMA|FUNCTION|SEQUENCE|INDEX|TYPE)"
    r"\s+(?:IF\s+EXISTS\s+)?([A-Za-z0-9_\"\.]+)",
    re.IGNORECASE,
)


def collect_ddl_index(versions_dir: Path = _VERSIONS_DIR) -> dict[str, dict[str, set[str]]]:
    """扫描迁移源码，收集每个 revision 的 DDL 生命周期动作。

    权限矩阵只看 GRANT/REVOKE 会误判：降级若 ``drop_table``，表上 ACL 随对象消失，
    不需要 REVOKE。这里用 AST 抓 ``op.<ddl>(...)`` 的第一个字符串参数，
    用来判定"对象是否在降级中被删除"。
    """
    import ast

    index: dict[str, dict[str, set[str]]] = {}
    if not versions_dir.is_dir():
        return index
    for path in sorted(versions_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        revision = path.name[:12]
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in {"upgrade", "downgrade"}:
                continue
            targets = index.setdefault(revision, {"upgrade": set(), "downgrade": set()})[node.name]
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                if not call.args:
                    continue
                kind = _DDL_OPS.get(call.func.attr)
                if kind is None and call.func.attr != "execute":
                    continue
                first = call.args[0]
                if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                    continue
                if call.func.attr == "execute":
                    # op.execute("DROP FUNCTION x()") 这类字符串 DDL 同样要进索引
                    for match in _SQL_DDL_RE.finditer(first.value):
                        ddl_kind, ddl_name = match.group(1).lower(), _norm_name(match.group(2))
                        targets.add(f"{ddl_kind}:{ddl_name}")
                        if ddl_kind in {"table", "function"} and "." in ddl_name:
                            targets.add(f"schema-table:{ddl_name.split('.')[0]}")
                    continue
                name = _norm_name(first.value)
                targets.add(f"{kind}:{name}")
                if kind == "table" and "." in name:
                    targets.add(f"schema-table:{name.split('.')[0]}")
    return index


def _dropped_in_downgrade(ddl: dict[str, set[str]] | None, action: dict[str, str]) -> bool:
    if not ddl:
        return False
    dropped = ddl.get("downgrade", set())
    scope, obj = action["scope"], action["object"]
    if scope in {"all_tables", "table", "object"}:
        if f"table:{obj}" in dropped:
            return True
        if "." in obj and f"schema-table:{obj.split('.')[0]}" in dropped:
            return True
    if scope in {"schema", "all_sequences", "all_functions"}:
        return f"schema:{obj}" in dropped
    return False



def _describe(action: dict[str, str]) -> str:
    parts = [action["kind"], action["scope"], action["object"]]
    if action["role"]:
        parts.append(f"role={action['role']}")
    if action["privs"]:
        parts.append(f"privs={action['privs']}")
    if action["grant_option"] == "with_grant_option":
        parts.append("grant_option")
    return " ".join(parts)


def collect_actions(rows: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, str]]]]:
    """按 revision 收集 upgrade / downgrade 动作（保持语句顺序）。"""
    collected: dict[str, dict[str, list[dict[str, str]]]] = {}
    for row in rows:
        for action in parse_actions(row):
            bucket = collected.setdefault(str(row["revision"]), {"upgrade": [], "downgrade": []})
            bucket.setdefault(action["direction"], []).append(action)
    return collected


def _matched(index: dict[tuple[str, ...], dict[str, str]], action: dict[str, str]) -> bool:
    """在对面集合里找配对。

    覆盖规则：
    * 权限集必须相等，``ALL`` 视为覆盖任意子集；
    * ``ALL TABLES/SEQUENCES IN SCHEMA s`` 的撤权覆盖该 schema 下逐对象授权；
    * grant option 必须一致（带 grant option 的授权不能被普通 REVOKE 抹掉）。
    """
    inverse = {"grant": "revoke", "revoke": "grant"}[action["kind"]]

    def hit(scope: str, obj: str, privs: str) -> bool:
        key = (inverse, scope, obj, action["role"], privs, action["grant_option"])
        return key in index

    if hit(action["scope"], action["object"], action["privs"]):
        return True
    if hit(action["scope"], action["object"], "ALL"):
        return True
    if action["scope"] in {"object", "table", "sequence"} and "." in action["object"]:
        schema = action["object"].split(".")[0]
        for scope in ("all_tables", "all_sequences"):
            if hit(scope, schema, action["privs"]) or hit(scope, schema, "ALL"):
                return True
    return False


def _priv_set(privs: str) -> set[str]:
    return {"ALL"} if privs == "ALL" else set(privs.split("|"))


def _already_granted(index: dict[tuple[str, ...], dict[str, str]], action: dict[str, str]) -> bool:
    """降级多出来的 GRANT，是否只是把升级已给的权限再写一遍（冗余而非扩权）。"""
    want = _priv_set(action["privs"])
    for key in index:
        (kind, scope, obj, role, privs, gopt) = key
        if kind != "grant" or (scope, obj, role, gopt) != (
            action["scope"],
            action["object"],
            action["role"],
            action["grant_option"],
        ):
            continue
        have = _priv_set(privs)
        if "ALL" in have or want <= have:
            return True
    return False


def _declared_retired() -> set[str]:
    """已登记退役的角色：其重建由受控恢复契约管理，不算矩阵不对称。"""
    try:
        from packages.shared.role_specs import retired_role_names
    except Exception:  # pragma: no cover - 脚本独立运行时允许缺失
        return set()
    return {name.lower() for name in retired_role_names()}


def _aggregate(findings: list[str], cap: int = 25) -> list[str]:
    """同对象同角色的多条权限集差异合并成一条，避免 156 行噪音。"""
    buckets: dict[str, list[str]] = {}
    for finding in findings:
        head, _, rest = finding.partition(" privs=")
        buckets.setdefault(head, []).append(rest)
    merged = [
        f"{head} ×{len(items)}（权限集不同，需父 revision 证据）" if len(items) > 1 else
        f"{head}（privs={items[0]}）"
        for head, items in buckets.items()
    ]
    if len(merged) > cap:
        return [*merged[:cap], f"... 另有 {len(merged) - cap} 条同类差异（详见矩阵 JSON 明细）"]
    return merged


def _compare(
    upgrade: list[dict[str, str]],
    downgrade: list[dict[str, str]],
    ddl: dict[str, set[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """返回 (findings, notes)。findings 决定是否不对称，notes 仅供人工参考。"""
    findings: list[str] = []
    notes: list[str] = []
    up_index = {_key(a): a for a in upgrade}
    down_index = {_key(a): a for a in downgrade}
    dropped_roles = {a["object"] for a in downgrade if a["kind"] == "drop_role"}

    for _k, action in up_index.items():
        if action["kind"] not in {"grant", "revoke"}:
            continue
        if action["role"] in dropped_roles:
            notes.append(f"角色在降级中被删除，授权随之消失 — {_describe(action)}")
            continue
        if _matched(down_index, action):
            continue
        if _dropped_in_downgrade(ddl, action):
            notes.append(f"对象在降级中删除，权限随之消失 — {_describe(action)}")
            continue
        if action["kind"] == "revoke":
            findings.append(f"RISK:missing restore — upgrade {_describe(action)}")
        else:
            findings.append(f"missing revoke — upgrade {_describe(action)}")

    for _k, action in down_index.items():
        if action["kind"] not in {"grant", "revoke"}:
            continue
        if action["role"] in dropped_roles:
            continue
        if _matched(up_index, action):
            continue
        if _dropped_in_downgrade(ddl, action):
            notes.append(f"降级语句作用于已删对象（无效恢复）— {_describe(action)}")
            continue
        if action["kind"] == "grant":
            if _already_granted(up_index, action):
                notes.append(f"重复授权（升级本来就给了）— downgrade {_describe(action)}")
                continue
            findings.append(f"RISK:extra grant — downgrade {_describe(action)}（权限扩大）")
        else:
            notes.append(f"unmatched revoke — downgrade {_describe(action)}（对象可能随 DDL 消失）")

    up_roles = {a["object"] for a in upgrade if a["kind"] == "create_role"}
    down_roles = {a["object"] for a in downgrade if a["kind"] == "drop_role"}
    for role in sorted(up_roles - down_roles):
        findings.append(f"RISK:升级创建角色 {role}，降级不删除（身份泄漏）")
    for role in sorted(down_roles - up_roles):
        findings.append(f"RISK:降级删除非本 revision 创建的角色 {role}（越权删除）")

    retired = {a["object"] for a in upgrade if a["kind"] == "drop_role"}
    rebuilt = {a["object"] for a in downgrade if a["kind"] == "create_role"}
    for role in sorted(retired & rebuilt):
        findings.append(f"RISK:升级退役身份 {role}，降级重建（已退役身份复活）")
    declared = _declared_retired()
    for role in sorted(rebuilt - retired):
        if role in declared:
            notes.append(f"角色 {role} 已登记退役，重建由 RESTORE_POLICY 受控执行")
            continue
        findings.append(f"RISK:降级创建本 revision 未退役的角色 {role}（身份越权创建）")

    # policy：降级常通过 DROP TABLE 连带删除，盘点拿不到 drop_table，
    # 因此只记 note，不参与不对称判定（否则会制造假阳性）。
    up_policies = {a["object"] for a in upgrade if a["kind"] == "policy:create"}
    down_policies = {a["object"] for a in downgrade if a["kind"] == "policy:drop"}
    for name in sorted(up_policies - down_policies):
        notes.append(f"policy 未显式 drop — {name}（可能随表删除，需人工确认）")
    return findings, notes


def build_matrix(
    inventory: dict[str, Any], ddl_index: dict[str, dict[str, set[str]]] | None = None
) -> dict[str, Any]:
    """由盘点 JSON 生成集合级矩阵（纯函数，便于单测喂构造数据）。"""
    rows: list[dict[str, Any]] = list(inventory.get("rows") or [])
    per_rev: dict[str, dict[str, dict[str, int]]] = inventory.get("per_revision") or {}
    actions = collect_actions(rows)
    index_by_rev: dict[str, int] = {}
    for row in rows:
        if row.get("chain_index") is not None:
            index_by_rev.setdefault(str(row["revision"]), int(row["chain_index"]))

    matrix: list[dict[str, Any]] = []
    for revision in per_rev:
        collected = actions.get(revision, {"upgrade": [], "downgrade": []})
        findings, notes = _compare(
            collected["upgrade"],
            collected["downgrade"],
            (ddl_index or {}).get(revision),
        )
        findings = _aggregate(findings)
        classification = CLASSIFIED.get(revision)
        matrix.append(
            {
                "revision": revision,
                "chain_index": index_by_rev.get(revision),
                "upgrade": per_rev[revision].get("upgrade", {}),
                "downgrade": per_rev[revision].get("downgrade", {}),
                "upgrade_actions": len(collected["upgrade"]),
                "downgrade_actions": len(collected["downgrade"]),
                "verdict": "asymmetric" if findings else "symmetric",
                "classification": classification["class"] if classification else None,
                "reason": classification["reason"] if classification else None,
                "evidence": classification["evidence"] if classification else None,
                "findings": findings,
                "notes": notes,
            }
        )

    matrix.sort(
        key=lambda item: (
            item["chain_index"] is None,
            item["chain_index"] if item["chain_index"] is not None else 0,
        )
    )

    asymmetric = [item for item in matrix if item["verdict"] == "asymmetric"]
    unclassified = [item["revision"] for item in asymmetric if not item["classification"]]
    blocking = [
        item["revision"]
        for item in asymmetric
        if item["classification"] in {"unproven_blocking", "restore_required"}
    ]
    defects = [item["revision"] for item in asymmetric if item["classification"] == "restore_required"]
    return {
        "chain_length": inventory.get("chain_length"),
        "head": inventory.get("head"),
        "source": str(_DEFAULT_INVENTORY.relative_to(_REPO_ROOT)).replace("\\", "/"),
        "rule": "集合比对：对象+范围+角色+权限集+grant option；数量相等不算等价",
        "revisions_with_identity_ops": len(matrix),
        "asymmetric_revisions": len(asymmetric),
        "classified": {name: sum(1 for i in matrix if i["classification"] == name) for name in CLASSIFICATIONS},
        "unclassified_revisions": unclassified,
        "restore_defects": defects,
        "blocking_revisions": blocking,
        "matrix": matrix,
    }


def _snapshot_diff_items(entry: dict[str, Any]) -> list[str]:
    """把快照条目里的 diff（按类别分组）摊平成单条差异文本。"""
    diff = entry.get("diff") or {}
    items: list[str] = []
    for group in sorted(diff):
        values = diff[group]
        if isinstance(values, list):
            items.extend(f"[{group}] {value}" for value in values)
        else:
            items.append(f"[{group}] {values}")
    return items


def _is_registered_ignore(item: str, ignored: list[dict[str, str]]) -> bool:
    """差异是否落在**已登记**的排除规则内（逐条登记，不做正则兜底）。"""
    for rule in ignored:
        name = rule.get("rule") or ""
        if name not in SNAPSHOT_IGNORE_RULES:
            return False
        declared = rule.get("item") or ""
        if declared and declared == item:
            return True
        if name == "pg_toast_oid" and "pg_toast" in item:
            return True
        if name == "acl_item_order" and item.startswith("[acl]") and "->" in item:
            return True
    return False


def evaluate_snapshot(
    snapshot: dict[str, Any], matrix_payload: dict[str, Any]
) -> dict[str, Any]:
    """双向语义闸门：把"P → upgrade → downgrade → D"的实测差异作为正式输入。

    检查四个方向，任何一项不满足都阻断：

    1. upgrade 变化：由源码集合比对（``build_matrix``）回答；
    2. downgrade 恢复：源码对称 + 快照 ``no_restore_proven`` 才算证完；
    3. downgrade 额外变化：快照 ``diff`` 非空且未被登记规则排除 → 阻断；
    4. 父态一致性：``no_restore_proven`` 要求 diff 为空（或只剩登记排除项）。

    纯函数，便于单测直接构造快照与矩阵，不依赖数据库。
    """
    entries: dict[str, dict[str, Any]] = {}
    blocks: list[str] = []
    ignored_used: list[str] = []
    for entry in snapshot.get("results") or []:
        revision = str(entry.get("revision") or "")
        if not revision:
            blocks.append("快照条目缺少 revision")
            continue
        entries[revision] = entry
        verdict = entry.get("verdict")
        if verdict not in CLASSIFICATIONS:
            blocks.append(f"{revision}: 快照 verdict 未分类（{verdict!r}）")
            continue
        if not entry.get("parent"):
            blocks.append(f"{revision}: 快照缺少父 revision，无法证明回到哪个状态")
        items = _snapshot_diff_items(entry)
        unexplained = [item for item in items if not _is_registered_ignore(item, entry.get("ignored") or [])]
        ignored_used.extend(item for item in items if item not in unexplained)
        if verdict == "no_restore_proven" and unexplained:
            blocks.append(
                f"{revision}: 标 no_restore_proven 但仍有未解释差异 {unexplained[:3]}"
            )
        if verdict == "restore_required":
            blocks.append(f"{revision}: 快照实测仍需要恢复（restore_required）")
        if verdict == "unproven_blocking":
            blocks.append(f"{revision}: 证据不足（unproven_blocking）")
        if verdict == "intentional_one_way" and not (entry.get("safety_contract") or "").strip():
            blocks.append(f"{revision}: 单向变化缺少 safety_contract，不得登记")

    matrix_rows = {item["revision"]: item for item in matrix_payload.get("matrix") or []}
    for revision, row in matrix_rows.items():
        if row.get("verdict") != "asymmetric":
            continue
        if not row.get("classification"):
            blocks.append(f"{revision}: 源码集合比对未分类")
            continue
        if row["classification"] == "restore_required":
            proven = entries.get(revision, {}).get("verdict")
            if proven not in {"no_restore_proven", "intentional_one_way"}:
                blocks.append(
                    f"{revision}: 源码仍标 restore_required，快照未证明修复（verdict={proven!r}）"
                )
            continue
        if revision not in entries:
            # 覆盖率：断言"已实测"的 revision 必须有 P/D 快照证据；
            # 缺证据不能借静态分类过关（`unproven_blocking` 本身就是阻断项）。
            blocks.append(f"{revision}: 缺少 P/D 往返快照证据（分类={row['classification']}）")
            continue
    for revision in entries:
        if revision not in matrix_rows:
            blocks.append(f"{revision}: 快照中的 revision 不在迁移链内（数据源漂移）")

    verdicts = {revision: entry.get("verdict") for revision, entry in entries.items()}
    return {
        "path": str(snapshot.get("method") or ""),
        "entries": verdicts,
        "counts": {
            name: sum(1 for v in verdicts.values() if v == name) for name in CLASSIFICATIONS
        },
        "ignored": ignored_used,
        "ignore_rules": SNAPSHOT_IGNORE_RULES,
        "blocks": blocks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成迁移可逆性矩阵（集合比对，只读）")
    parser.add_argument("--inventory", type=Path, default=_DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=_DEFAULT_SNAPSHOT,
        help="P→upgrade→downgrade→D 的实测快照（双向语义闸门输入）；传空引用可跳过",
    )
    parser.add_argument("--print", action="store_true", help="打印不对称项与结论")
    parser.add_argument("--check", action="store_true", help="存在未分类或 unproven_blocking 时返回 1")
    args = parser.parse_args(argv)

    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    payload = build_matrix(inventory, collect_ddl_index())
    if args.snapshot and args.snapshot.exists():
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        payload["snapshot"] = evaluate_snapshot(snapshot, payload)
        payload["snapshot"]["source"] = str(args.snapshot.relative_to(_REPO_ROOT)).replace("\\", "/")
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[MATRIX] revisions={payload['revisions_with_identity_ops']} "
        f"asymmetric={payload['asymmetric_revisions']} "
        f"classified={payload['classified']} "
        f"unclassified={len(payload['unclassified_revisions'])} -> {args.output}"
    )
    if "snapshot" in payload:
        print(
            f"[MATRIX][SNAPSHOT] {payload['snapshot']['source']} "
            f"counts={payload['snapshot']['counts']} ignored={len(payload['snapshot']['ignored'])}"
        )
    if args.print:
        for item in payload["matrix"]:
            if item["verdict"] != "asymmetric":
                continue
            print(f"#{item['chain_index']} {item['revision']} [{item['classification'] or 'UNCLASSIFIED'}]")
            for finding in item["findings"]:
                print("   -", finding)
    if args.check:
        if payload["unclassified_revisions"]:
            print("[MATRIX][FAIL] 未分类不对称 revision:", ", ".join(payload["unclassified_revisions"]))
            return 1
        if payload["restore_defects"]:
            print("[MATRIX][FAIL] 实测未恢复（需修复迁移）:", ", ".join(payload["restore_defects"]))
            return 1
        if "snapshot" in payload and payload["snapshot"]["blocks"]:
            print("[MATRIX][FAIL] 双向语义闸门阻断:", "; ".join(payload["snapshot"]["blocks"][:5]))
            return 1
        if payload["blocking_revisions"]:
            print("[MATRIX][BLOCKED] 待补证据:", ", ".join(payload["blocking_revisions"]))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())





