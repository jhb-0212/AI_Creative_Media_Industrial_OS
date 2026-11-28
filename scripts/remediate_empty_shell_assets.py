"""空壳资产受控退役（CHG-WP04-D4-9 R1 固化，迁移前使用）。

背景
----
``d0e1f2a3b4c5`` 把 ``asset_service.asset_versions.storage_ref`` 从自由 JSONB
收敛为受控定位符 ``local://xx/yy/<sha256>.<ext>``。迁移对「无法确定转换」的
存量一律 fail-closed，把判断权交还给人：这些行到底是「内容存在、只是引用
格式旧」，还是「内容从未落地的空壳资产」。

本脚本只固化**第二种**处置，并且把它做成一个**原子**动作：

1. 默认 **dry-run**（只读盘点），必须显式 ``--apply`` 才动数据；
2. **单事务**：表锁 → 归档 → 三重等值校验 → 删除 → 恢复触发器 → 提交。
   校验与删除看到的是同一份快照，中途任何失败整体回滚，不存在「删了没归档」
   的中间态；
3. **按归档主键删除**（``entity_id`` / ``qc_report_id``），不是按谓词重扫。
   归档之后新写入的行不可能被误删——它们根本不在归档里；
4. 校验三重：行数相等、双向集合相等（``EXCEPT`` 两侧为空）、SHA-256 与 md5
   双指纹一致。md5 只作与既有 ``d0e1`` 判据兼容的辅助校验，完整性以
   SHA-256 + 集合相等为准；
5. 被 ``qc_reports`` 引用的行连同证据行一起归档（内容不变，只换存放位置）；
   ``ADR-R11`` 的追加型触发器**只在同一事务内**临时禁用，删除后立即恢复；
6. 只删「不可转换」的行，可转换的行一律不动（不替人做业务裁定）；
7. 删除条数与归档条数必须相等，不等则整事务回滚。

禁止事项（与迁移 HINT 一致）
--------------------------
- 禁止按 sha256 批量派生定位符（会造出语法合规、内容不存在的假资产）；
- 禁止在未完成等值归档前禁用触发器；
- 禁止把本脚本用于「已升级到 d0e1 之后」的库（那时列已是 text，形态不同）。

用法
----
    python scripts/remediate_empty_shell_assets.py                 # 盘点
    python scripts/remediate_empty_shell_assets.py --apply --suffix 20261128
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

TABLE = "asset_service.asset_versions"
QC_TABLE = "asset_service.qc_reports"
ARCHIVE_SCHEMA = "quarantine"
ARCHIVE_VERSIONS = f"{ARCHIVE_SCHEMA}.asset_versions_"

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "migrations"
    / "versions"
    / "d0e1f2a3b4c5_storage_ref_controlled_locator.py"
)


def non_convertible_predicate() -> str:
    """从迁移**导入**判据再取反，不复制第二份字面量。

    复制字面量的风险有方向性：迁移将来放宽（例如多认一个扩展名）而脚本没跟，
    脚本的判据就会变**宽**——把迁移本可转换的真资产当成空壳删掉。这里直接读
    ``d0e1`` 的 ``_CONVERTIBLE``，两边只可能同源。
    """
    spec = importlib.util.spec_from_file_location("_d0e1_predicate", _MIGRATION)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件缺失才会到这里
        raise RuntimeError(f"无法加载迁移判据：{_MIGRATION}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    convertible = module._CONVERTIBLE
    # 外层 COALESCE 是必要的：`NOT (a AND b)` 在任一操作数为 NULL 时结果是 NULL，
    # 既不算「可转换」也不算「不可转换」——`WHERE NOT (...)` 与
    # `WHERE NON_CONVERTIBLE` 都会把这种行静默跳过。storage_ref 在真实 schema 里
    # 是 NOT NULL，所以这是**纵深**而不是当下缺口；但一个「两边都看不见」的判据
    # 不该靠另一张表的约束来兜底。
    return f"NOT (COALESCE(({convertible}), false))"


NON_CONVERTIBLE = non_convertible_predicate()


def validate_suffix(suffix: str) -> str:
    """归档表后缀：只允许 8 位日期，防止拼进 SQL 的值带出注入面。"""
    if len(suffix) != 8 or not suffix.isdigit():
        raise ValueError("suffix 必须是 8 位数字日期，例如 20261128")
    return suffix


@dataclass(frozen=True)
class Plan:
    total_rows: int
    bad_rows: int
    qc_rows: int
    qc_distinct_versions: int

    @property
    def nothing_to_do(self) -> bool:
        return self.bad_rows == 0

    @property
    def needs_trigger_disable(self) -> bool:
        """``qc_reports`` 只在确有证据行要删时才需要临时禁用触发器。

        这个判据会被真正用上（见 ``retire_statements``）：没有 QC 行却去禁用
        QC 表的触发器，属于把保护面扩大到不需要的范围。
        """
        return self.qc_rows > 0


def build_plan(total_rows: int, bad_rows: int, qc_rows: int, qc_distinct_versions: int) -> Plan:
    """把盘点数字收敛成一个判定对象（纯函数，便于单测）。"""
    if bad_rows > total_rows:
        raise ValueError("bad_rows 不能大于 total_rows")
    if qc_distinct_versions > bad_rows:
        raise ValueError("被引用的版本数不能大于待退役行数")
    return Plan(total_rows, bad_rows, qc_rows, qc_distinct_versions)


def archive_statements(suffix: str) -> list[str]:
    """归档语句（不删任何行）。刻意不用 ``IF NOT EXISTS``：

    同名归档表已存在说明这个 suffix 被用过（或上一次没回滚干净）。此时继续
    ``IF NOT EXISTS`` 会**跳过归档**却让后面的校验拿旧表去比新 live，误判成
    "不等值"或更糟——误判成"等值"。直接让 CREATE 失败，由调用方给出明确提示。
    """
    validate_suffix(suffix)
    return [
        f"LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE",
        f"LOCK TABLE {QC_TABLE} IN SHARE ROW EXCLUSIVE MODE",
        f"CREATE TABLE {ARCHIVE_SCHEMA}.asset_versions_{suffix} AS "
        f"SELECT * FROM {TABLE} WHERE {NON_CONVERTIBLE}",
        f"CREATE TABLE {ARCHIVE_SCHEMA}.qc_reports_{suffix} AS "
        f"SELECT * FROM {QC_TABLE} "
        f"WHERE asset_version_id IN (SELECT entity_id FROM {ARCHIVE_SCHEMA}.asset_versions_{suffix})",
    ]


def retire_statements(suffix: str, *, qc_rows: int) -> list[str]:
    """删除语句：按归档主键删，禁用触发器与删除在同一事务内成对出现。

    ``qc_rows == 0`` 时不动 QC 表的触发器——保护面不扩大到不必要的对象。
    返回的语句顺序即执行顺序；``DELETE`` 用 ``WITH d AS (… RETURNING 1)``
    形态执行，由调用方取回真实删除行数并断言。
    """
    validate_suffix(suffix)
    versions_archive = f"{ARCHIVE_SCHEMA}.asset_versions_{suffix}"
    qc_archive = f"{ARCHIVE_SCHEMA}.qc_reports_{suffix}"
    statements: list[str] = []
    if qc_rows:
        statements += [
            f"ALTER TABLE {QC_TABLE} DISABLE TRIGGER USER",
            f"DELETE FROM {QC_TABLE} WHERE qc_report_id IN (SELECT qc_report_id FROM {qc_archive})",
            f"ALTER TABLE {QC_TABLE} ENABLE TRIGGER USER",
        ]
    statements += [
        f"ALTER TABLE {TABLE} DISABLE TRIGGER USER",
        f"DELETE FROM {TABLE} WHERE entity_id IN (SELECT entity_id FROM {versions_archive})",
        f"ALTER TABLE {TABLE} ENABLE TRIGGER USER",
    ]
    return statements


def count_deleted(statement: str) -> str:
    """把 ``DELETE …`` 包成「删除并回传真实行数」的单条语句。

    不靠 ``cursor.rowcount``：它随驱动与语句形态变化，而「删了几行」是本次
    动作的核心回执，必须来自数据库自己。
    """
    return f"WITH d AS ({statement} RETURNING 1) SELECT count(*) FROM d"


def fingerprint_sql(table_expr: str, where: str = "") -> str:
    """内容指纹：集合相等（EXCEPT）之外的第二重证明。

    - ``sha256``：完整性主判据；
    - ``md5``：与既有 ``d0e1`` 判据兼容的辅助校验，单独不构成证明。
    """
    clause = f" WHERE {where}" if where else ""
    rows = f"(SELECT * FROM {table_expr}{clause})"
    return (
        "SELECT encode(sha256(convert_to(coalesce(string_agg(t::text, E'\\n' "
        "ORDER BY t::text), 'empty'), 'UTF8')), 'hex'), "
        f"(SELECT count(*) FROM {rows}) "
        f"FROM {rows} t"
    )


def set_difference_sql(
    left: str,
    right: str,
    *,
    left_where: str = "",
    right_where: str = "",
) -> str:
    """``left EXCEPT right`` 的行数：0 才说明两份逐行相同。

    两侧都支持附加过滤：live 侧必须按同一判据/同一引用集收窄，否则
    「live 里还有可转换行」会让差值恒不为 0，校验永远失败。
    """

    def side(expr: str, where: str) -> str:
        clause = f" WHERE {where}" if where else ""
        return f"(SELECT * FROM {expr}{clause})"

    return (
        f"SELECT count(*) FROM ({side(left, left_where)} EXCEPT {side(right, right_where)}) AS diff"
    )


class RemediationAbortedError(Exception):
    """校验不通过：事务已回滚，回执随异常带走（不能在事务块里 return，那会提交）。"""

    def __init__(self, decision: str, result: dict) -> None:
        super().__init__(decision)
        self.decision = decision
        self.result = result


def md5_sql(table_expr: str, where: str = "") -> str:
    clause = f" WHERE {where}" if where else ""
    return (
        "SELECT coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), 'empty') "
        f"FROM (SELECT * FROM {table_expr}{clause}) t"
    )


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        database=os.environ.get("POSTGRES_DB", "postgres"),
    )


async def _survey(conn: asyncpg.Connection) -> tuple[int, int, int, int]:
    total = await conn.fetchval(f"SELECT count(*) FROM {TABLE}")
    bad = await conn.fetchval(f"SELECT count(*) FROM {TABLE} WHERE {NON_CONVERTIBLE}")
    qc_rows = await conn.fetchval(
        f"SELECT count(*) FROM {QC_TABLE} WHERE asset_version_id IN "
        f"(SELECT entity_id FROM {TABLE} WHERE {NON_CONVERTIBLE})"
    )
    qc_versions = await conn.fetchval(
        f"SELECT count(DISTINCT asset_version_id) FROM {QC_TABLE} WHERE asset_version_id IN "
        f"(SELECT entity_id FROM {TABLE} WHERE {NON_CONVERTIBLE})"
    )
    return total, bad, qc_rows, qc_versions


async def run(apply: bool, suffix: str) -> dict:
    conn = await _connect()
    try:
        storage_ref_type = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'asset_service' AND table_name = 'asset_versions' "
            "AND column_name = 'storage_ref'"
        )
        if storage_ref_type != "jsonb":
            # 判据来自 d0e1，而 d0e1 之后 ``storage_ref`` 已是受控定位符（varchar）。
            # 把 jsonb 判据套在 varchar 上，PG 只会给出
            # ``operator does not exist: character varying ? unknown``——一句和
            # 「跑错阶段」毫无关系的话。这里把它翻成可执行的结论。
            return {
                "mode": "apply" if apply else "dry-run",
                "decision": (
                    f"ABORT：storage_ref 的列类型是 {storage_ref_type!r}，本脚本只能运行在 "
                    "d0e1f2a3b4c5（storage_ref 仍为 jsonb）之前；迁移之后每一行都被 CHECK "
                    "约束保证形态，已不存在「不可转换」的行"
                ),
            }
        total, bad, qc_rows, qc_versions = await _survey(conn)
        plan = build_plan(total, bad, qc_rows, qc_versions)
        result: dict[str, object] = {
            "mode": "apply" if apply else "dry-run",
            "total_rows": plan.total_rows,
            "non_convertible_rows": plan.bad_rows,
            "qc_referencing_rows": plan.qc_rows,
            "qc_distinct_versions": plan.qc_distinct_versions,
            "needs_trigger_disable": plan.needs_trigger_disable,
        }
        if plan.nothing_to_do:
            result["decision"] = "no-op（没有不可转换的行）"
            return result
        if not apply:
            result["decision"] = "would-archive-and-retire（加 --apply 执行）"
            return result

        versions_archive = f"{ARCHIVE_SCHEMA}.asset_versions_{suffix}"
        qc_archive = f"{ARCHIVE_SCHEMA}.qc_reports_{suffix}"
        existing = await conn.fetchval("SELECT to_regclass($1)", versions_archive)
        if existing is not None:
            result["decision"] = (
                f"ABORT：归档表 {existing} 已存在，换一个 suffix 或先人工核对旧归档；"
                "不复用旧表（复用会让校验拿旧数据比新 live）"
            )
            return result

        async with conn.transaction():
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {ARCHIVE_SCHEMA}")
            for statement in archive_statements(suffix):
                await conn.execute(statement)

            archived_versions = await conn.fetchval(f"SELECT count(*) FROM {versions_archive}")
            archived_qc = await conn.fetchval(f"SELECT count(*) FROM {qc_archive}")
            qc_live_where = f"asset_version_id IN (SELECT entity_id FROM {versions_archive})"

            # 三重校验：行数 + 双向集合相等 + 双指纹。任何一项不满足都在
            # 删除之前中止（此时尚未禁用触发器）。
            checks = {
                "versions_count_archive": archived_versions,
                "qc_count_archive": archived_qc,
                "versions_except_live_minus_archive": await conn.fetchval(
                    set_difference_sql(TABLE, versions_archive, left_where=NON_CONVERTIBLE)
                ),
                "versions_except_archive_minus_live": await conn.fetchval(
                    set_difference_sql(versions_archive, TABLE)
                ),
                "qc_except_live_minus_archive": await conn.fetchval(
                    set_difference_sql(QC_TABLE, qc_archive, left_where=qc_live_where)
                ),
                "qc_except_archive_minus_live": await conn.fetchval(
                    set_difference_sql(qc_archive, QC_TABLE)
                ),
            }
            versions_sha = (
                (await conn.fetchrow(fingerprint_sql(TABLE, NON_CONVERTIBLE)))[0],
                (await conn.fetchrow(fingerprint_sql(versions_archive)))[0],
            )
            versions_md5 = (
                await conn.fetchval(md5_sql(TABLE, NON_CONVERTIBLE)),
                await conn.fetchval(md5_sql(versions_archive)),
            )
            qc_sha = (
                (await conn.fetchrow(fingerprint_sql(QC_TABLE, qc_live_where)))[0],
                (await conn.fetchrow(fingerprint_sql(qc_archive)))[0],
            )
            result.update(
                {
                    **checks,
                    "versions_sha256_live": versions_sha[0],
                    "versions_sha256_archive": versions_sha[1],
                    "versions_md5_live": versions_md5[0],
                    "versions_md5_archive": versions_md5[1],
                    "qc_sha256_live": qc_sha[0],
                    "qc_sha256_archive": qc_sha[1],
                }
            )

            def abort(decision: str) -> None:
                result["decision"] = decision
                raise RemediationAbortedError(decision, result)

            if archived_versions != plan.bad_rows or archived_qc != plan.qc_rows:
                abort("ABORT（事务已回滚）：归档行数与盘点不一致，未删除任何行")
            if any(
                checks[key]
                for key in (
                    "versions_except_live_minus_archive",
                    "versions_except_archive_minus_live",
                    "qc_except_live_minus_archive",
                    "qc_except_archive_minus_live",
                )
            ):
                abort("ABORT（事务已回滚）：归档与 live 集合不等值，未删除任何行")
            if (
                versions_sha[0] != versions_sha[1]
                or versions_md5[0] != versions_md5[1]
                or qc_sha[0] != qc_sha[1]
            ):
                abort("ABORT（事务已回滚）：归档与 live 指纹不一致，未删除任何行")

            deleted_qc = 0
            deleted_versions = 0
            for statement in retire_statements(suffix, qc_rows=plan.qc_rows):
                if statement.startswith(f"DELETE FROM {QC_TABLE}"):
                    deleted_qc = await conn.fetchval(count_deleted(statement))
                elif statement.startswith(f"DELETE FROM {TABLE}"):
                    deleted_versions = await conn.fetchval(count_deleted(statement))
                else:
                    await conn.execute(statement)
            result["deleted_qc_rows"] = deleted_qc
            result["deleted_version_rows"] = deleted_versions
            if deleted_qc != plan.qc_rows or deleted_versions != archived_versions:
                abort(
                    "ABORT（事务已回滚）：删除数与归档数不等 "
                    f"（qc {deleted_qc}/{plan.qc_rows}，versions "
                    f"{deleted_versions}/{archived_versions}）"
                )
            remaining = await conn.fetchval(f"SELECT count(*) FROM {TABLE} WHERE {NON_CONVERTIBLE}")
            if remaining != 0:
                abort(f"ABORT（事务已回滚）：仍剩 {remaining} 行不可转换")
            result["remaining_non_convertible"] = remaining
            result["decision"] = "retired（已归档并删除不可转换行）"
        return result
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="空壳资产受控退役（迁移前）")
    parser.add_argument("--apply", action="store_true", help="真正执行；缺省为只读盘点")
    parser.add_argument("--suffix", default="", help="归档表后缀（8 位日期），--apply 时必填")
    args = parser.parse_args(argv)
    if args.apply:
        try:
            validate_suffix(args.suffix)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        result = asyncio.run(run(args.apply, args.suffix))
    except RemediationAbortedError as exc:
        print(json.dumps(exc.result, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if str(result.get("decision", "")).startswith(("no-op", "would", "retired")) else 1


if __name__ == "__main__":
    sys.exit(main())
