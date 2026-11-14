"""Outbox 分区归档后端（本地目录已实现；S3 预留接口，未接入凭证）。

为什么单独成模块：
- 「归档」是本项目里唯一会**跨越存储边界**的动作。把它和分区 DDL 混在一起，
  S3 接入时必然要改主流程；拆出来后，未来只需实现一个新的 backend。
- 接口口径固定为「导出一个可校验副本」：CSV.gz + sha256 + manifest。
  校验值必须能独立复算，否则归档等于没归档。

当前状态（明确）：
- ``local``：已实现并实测（导出 → sha256 → manifest → 摘除/清理）。
- ``s3://bucket/prefix``：**未实现**。``S3_*`` 配置全空、仓库没有任何对象存储
  客户端依赖。这里刻意抛错而不是静默降级到本地——静默降级的后果是运维以为
  数据在对象存储里，实际只在这台机器上。
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from packages.shared.logging import get_logger

logger = get_logger(__name__)

SCHEMA = "outbox"


class S3ArchiveUnavailableError(RuntimeError):
    """S3 归档未接入：缺凭证与客户端依赖（见 ADR 未做项）。"""


@dataclass(frozen=True)
class ArchiveResult:
    """一次归档的可校验结果（路径、行数、内容摘要）。"""

    path: str
    rows: int
    sha256: str


def _csv_value(value: object) -> str:
    """把 jsonb/list 之类的值序列化成 CSV 安全文本（None → 空串）。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _sha256_of(path: Path) -> str:
    """按块计算文件摘要（同步实现，供 asyncio.to_thread 调用）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def export_partition_local(
    conn: AsyncConnection,
    partition: str,
    directory: Path,
) -> ArchiveResult:
    """把分区全部行导出为 CSV.gz + .sha256 + .manifest.json。"""
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    csv_path = directory / f"{partition}.csv.gz"
    rows = 0

    result = await conn.stream(
        text(f"SELECT * FROM {SCHEMA}.{partition} ORDER BY occurred_at, event_id")
    )
    with gzip.open(csv_path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        keys: list[str] | None = None
        async for row in result:
            mapping = row._mapping
            if keys is None:
                keys = list(mapping.keys())
                writer.writerow(keys)
            writer.writerow([_csv_value(mapping[key]) for key in keys])
            rows += 1

    sha = await asyncio.to_thread(_sha256_of, csv_path)
    await asyncio.to_thread(
        (directory / f"{partition}.sha256").write_text,
        f"{sha}  {csv_path.name}\n",
        "utf-8",
    )
    manifest = {
        "partition": partition,
        "rows": rows,
        "sha256": sha,
        "file": csv_path.name,
        "exported_at": datetime.now(UTC).isoformat(),
    }

    (directory / f"{partition}.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("outbox_partition_archived", partition=partition, rows=rows, sha256=sha)
    return ArchiveResult(path=str(csv_path), rows=rows, sha256=sha)


async def export_partition(
    conn: AsyncConnection,
    partition: str,
    target: str,
) -> ArchiveResult:
    """按目标 URI 分派归档后端。

    - ``s3://...`` → 明确报错（未接入凭证与依赖），绝不静默改存本地；
    - 其余按本地目录处理。
    """
    if target.startswith("s3://"):
        raise S3ArchiveUnavailableError(
            "S3 归档尚未接入：需要 S3_ENDPOINT_URL / S3_ACCESS_KEY_ID / "
            "S3_SECRET_ACCESS_KEY / S3_BUCKET 以及对象存储客户端依赖。"
            " 在此之前请使用本地目录归档（--archive-dir <本地路径>）。"
        )
    return await export_partition_local(conn, partition, Path(target))
