"""空壳资产受控退役（CHG-WP04-D4-9 R1 固化，迁移前使用）。

背景
----
``d0e1f2a3b4c5`` 把 ``asset_service.asset_versions.storage_ref`` 从自由 JSONB
收敛为受控定位符 ``local://xx/yy/<sha256>.<ext>``。迁移对「无法确定转换」的
存量一律 fail-closed，把判断权交还给人：这些行到底是「内容存在、只是引用
格式旧」，还是「内容从未落地的空壳资产」。

本脚本只固化**第二种**处置，并且把它做成可回放、可校验、可回滚的动作：

1. 默认 **dry-run**（只读盘点），必须显式 ``--apply`` 才动数据；
2. 归档优先：先把待退役行整行拷进 ``quarantine`` schema，再逐行文本 md5
   与 live 侧比对，一致才允许删除；不一致立即中止；
3. 被 ``qc_reports`` 引用的行会连同证据行一起归档（证据内容不变，只换存放
   位置）；``ADR-R11`` 的追加型触发器只允许在**同一事务内**临时禁用，
   删除后立即恢复；
4. 只删「不可转换」的行，可转换的行一律不动（不替人做业务裁定）。

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
import json
import os
import sys
from dataclasses import dataclass

import asyncpg

TABLE = "asset_service.asset_versions"
QC_TABLE = "asset_service.qc_reports"
ARCHIVE_SCHEMA = "quarantine"

# 与 d0e1f2a3b4c5 的 _CONVERTIBLE 取反，逐项同源：判据必须和迁移说同一件事，
# 否则会出现「本脚本认为该退役、迁移却放行」的两套标准。
NON_CONVERTIBLE = """NOT (
    storage_ref ? 'uri'
    AND jsonb_typeof(storage_ref -> 'uri') = 'string'
    AND (storage_ref ->> 'uri') ~ '^local://[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\\.(bin|jpg|mp4|png|wav|webm|webp)$'
    AND substr(storage_ref ->> 'uri', 9, 2) = substr(storage_ref ->> 'uri', 15, 2)
    AND substr(storage_ref ->> 'uri', 12, 2) = substr(storage_ref ->> 'uri', 17, 2)
    AND substr(storage_ref ->> 'uri', 15, 64) = lower(sha256)
)"""


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
        return self.qc_rows > 0


def build_plan(total_rows: int, bad_rows: int, qc_rows: int, qc_distinct_versions: int) -> Plan:
    """把盘点数字收敛成一个判定对象（纯函数，便于单测）。"""
    if bad_rows > total_rows:
        raise ValueError("bad_rows 不能大于 total_rows")
    if qc_distinct_versions > bad_rows:
        raise ValueError("被引用的版本数不能大于待退役行数")
    return Plan(total_rows, bad_rows, qc_rows, qc_distinct_versions)


def archive_sql(suffix: str) -> str:
    validate_suffix(suffix)
    return f"""
BEGIN;
CREATE SCHEMA IF NOT EXISTS {ARCHIVE_SCHEMA};
CREATE TABLE IF NOT EXISTS {ARCHIVE_SCHEMA}.asset_versions_{suffix} AS
  SELECT * FROM {TABLE} WHERE {NON_CONVERTIBLE};
CREATE TABLE IF NOT EXISTS {ARCHIVE_SCHEMA}.qc_reports_{suffix} AS
  SELECT * FROM {QC_TABLE}
   WHERE asset_version_id IN (SELECT entity_id FROM {ARCHIVE_SCHEMA}.asset_versions_{suffix});
COMMIT;
"""


def retire_sql(suffix: str) -> str:
    validate_suffix(suffix)
    return f"""
BEGIN;
ALTER TABLE {QC_TABLE} DISABLE TRIGGER USER;
DELETE FROM {QC_TABLE}
 WHERE asset_version_id IN (SELECT entity_id FROM {ARCHIVE_SCHEMA}.asset_versions_{suffix});
ALTER TABLE {QC_TABLE} ENABLE TRIGGER USER;
ALTER TABLE {TABLE} DISABLE TRIGGER USER;
DELETE FROM {TABLE} WHERE {NON_CONVERTIBLE};
ALTER TABLE {TABLE} ENABLE TRIGGER USER;
COMMIT;
"""


def md5_sql(table_expr: str, where: str = "") -> str:
    clause = f" WHERE {where}" if where else ""
    return (
        "SELECT coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), 'empty') "
        f"FROM (SELECT * FROM {table_expr}{clause}) t"
    )


async def run(apply: bool, suffix: str) -> dict:
    conn = await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        database=os.environ.get("POSTGRES_DB", "postgres"),
    )
    try:
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
        plan = build_plan(total, bad, qc_rows, qc_versions)
        result = {
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

        await conn.execute(archive_sql(suffix))
        live_md5 = await conn.fetchval(md5_sql(TABLE, NON_CONVERTIBLE))
        arch_md5 = await conn.fetchval(md5_sql(f"{ARCHIVE_SCHEMA}.asset_versions_{suffix}"))
        qc_live_md5 = await conn.fetchval(
            md5_sql(QC_TABLE, f"asset_version_id IN (SELECT entity_id FROM {ARCHIVE_SCHEMA}.asset_versions_{suffix})")
        )
        qc_arch_md5 = await conn.fetchval(md5_sql(f"{ARCHIVE_SCHEMA}.qc_reports_{suffix}"))
        result.update(
            {
                "versions_md5_live": live_md5,
                "versions_md5_archive": arch_md5,
                "qc_md5_live": qc_live_md5,
                "qc_md5_archive": qc_arch_md5,
            }
        )
        if live_md5 != arch_md5 or qc_live_md5 != qc_arch_md5:
            result["decision"] = "ABORT：归档与 live 不等值，未删除任何行"
            return result

        await conn.execute(retire_sql(suffix))
        result["remaining_non_convertible"] = await conn.fetchval(
            f"SELECT count(*) FROM {TABLE} WHERE {NON_CONVERTIBLE}"
        )
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
    result = asyncio.run(run(args.apply, args.suffix))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("decision", "").startswith(("no-op", "would", "retired")) else 1


if __name__ == "__main__":
    sys.exit(main())
