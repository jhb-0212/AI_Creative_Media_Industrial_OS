"""P1-01：真实字符串定位符贯穿到 QC 报告落库的端到端证据。

为什么需要这份集成测试
----------------------
#49 把 ``storage_ref`` 从自由字典收敛为受控定位符字符串后，QC 检测器
``AssetIntegrityInspector._local_path`` 仍按字典调用 ``.get()``——传入字符串
必抛 ``AttributeError``，Worker 捕获后不写报告、不终结任务，租约过期回队，
真实资产 QC 路径整体不可用。单测可以锁住 ``_check`` 的判定，但只有让**真实
资产域数据库 + 真实存储端口 + 真实 inspect() 全流程**跑通，才能证明「类型断链
已根治」而不是「判定函数被单独修好了」。

本测试不验证授权（那由 least_privilege 系列负责）；它验证的是一条类型事实：
资产登记时写入的定位符字符串，能被 QC 检测器原样读回、生成一份 QC 报告。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapters.storage.local_fs import LocalFsObjectStorage
from packages.shared.storage_locator import build_locator
from tests.integration.planning_seed import seed_project
from workers.qc.asset_inspector import AssetIntegrityInspector

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000091")


class _Task:
    def __init__(
        self,
        *,
        entity_id: str,
        tenant_id: uuid.UUID,
        asset_version_id: str,
        project_id: str,
        quality_profile_version: str,
    ) -> None:
        self.entity_id = entity_id
        self.tenant_id = tenant_id
        self.asset_version_id = asset_version_id
        self.project_id = project_id
        self.quality_profile_version = quality_profile_version
        # 领域对象一定有这个属性（租户级项目为 None）；替身也要有，否则测的是
        # 「替身缺字段」而不是检测器行为。
        self.organization_id = None


async def _seed_asset(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    asset_id: str,
    project_id: str,
    sha256: str,
    locator: str,
) -> None:
    """以超级用户播种一条 GENERATED 资产（RLS 不干扰播种本身）。"""
    now = datetime.now(UTC)
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO asset_service.asset_versions "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " project_id, asset_type, sha256, storage_ref, "
                " source_asset_version_ids, version) "
                "VALUES (:id, 'AssetVersion', 1, 1, 'GENERATED', 'asset_service', "
                " :tid, :now, 'e2e-seeder', :now, :pid, 'image', :sha, :locator, "
                " '[]'::jsonb, 'v1')"
            ),
            {
                "id": asset_id,
                "tid": _TENANT,
                "now": now,
                "sha": sha256,
                "locator": locator,
                "pid": project_id,
            },
        )
        await session.commit()


async def _read_report(
    admin_sessionmaker: async_sessionmaker[AsyncSession], asset_id: str
) -> dict[str, Any] | None:
    async with admin_sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT qc_report_id, findings FROM asset_service.qc_reports "
                        "WHERE asset_version_id = :aid"
                    ),
                    {"aid": asset_id},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None


@pytest.mark.asyncio
async def test_string_locator_produces_qc_report(
    asset_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """真实定位符落盘 → 资产登记 → inspect() → QC 报告落库且判定 PASSED。"""
    project_id = "p1-locator-e2e-" + uuid.uuid4().hex[:8]
    asset_id = "a-locator-e2e-" + uuid.uuid4().hex[:8]
    payload = b"qc-e2e-real-content"
    digest = hashlib.sha256(payload).hexdigest()
    locator = build_locator(digest, "png")

    await seed_project(admin_sessionmaker, project_id=project_id, tenant=_TENANT)
    await _seed_asset(
        admin_sessionmaker,
        asset_id=asset_id,
        project_id=project_id,
        sha256=digest,
        locator=locator,
    )

    storage = LocalFsObjectStorage(tmp_path, max_bytes=1024 * 1024)
    stored = await storage.put(data=payload, media_type="image/png")
    assert stored.locator == locator  # 落盘定位符 == 资产登记时写入的定位符

    inspector = AssetIntegrityInspector(asset_sessionmaker, storage=storage)
    task = _Task(
        entity_id="qc-task-" + uuid.uuid4().hex[:8],
        tenant_id=_TENANT,
        asset_version_id=asset_id,
        project_id=project_id,
        quality_profile_version="qp-e2e-v1",
    )
    inspection = await inspector.inspect(task)

    assert inspection.result == "PASSED", inspection.evidence_refs
    assert inspection.qc_report_id

    report = await _read_report(admin_sessionmaker, asset_id)
    assert report is not None, "inspect() 未写出 QC 报告"
    assert any(
        f.get("check") == "content_verifiable" and f.get("passed") is True
        for f in report["findings"]
    ), report["findings"]


@pytest.mark.asyncio
async def test_missing_content_produces_rejected_report(
    asset_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """反向：资产声明的定位符无对应内容 → 报告落库且判定 REJECTED。

    证明「读不到内容」被记进报告（真实的质量结论），不是让租约反复过期。
    """
    project_id = "p1-locator-miss-" + uuid.uuid4().hex[:8]
    asset_id = "a-locator-miss-" + uuid.uuid4().hex[:8]
    digest = hashlib.sha256(b"never-written").hexdigest()
    locator = build_locator(digest, "png")

    await seed_project(admin_sessionmaker, project_id=project_id, tenant=_TENANT)
    await _seed_asset(
        admin_sessionmaker,
        asset_id=asset_id,
        project_id=project_id,
        sha256=digest,
        locator=locator,
    )

    storage = LocalFsObjectStorage(tmp_path, max_bytes=1024 * 1024)
    # 不落盘：定位符合法但内容不存在。
    inspector = AssetIntegrityInspector(asset_sessionmaker, storage=storage)
    task = _Task(
        entity_id="qc-task-" + uuid.uuid4().hex[:8],
        tenant_id=_TENANT,
        asset_version_id=asset_id,
        project_id=project_id,
        quality_profile_version="qp-e2e-v1",
    )
    inspection = await inspector.inspect(task)

    assert inspection.result == "REJECTED", inspection.evidence_refs
    report = await _read_report(admin_sessionmaker, asset_id)
    assert report is not None
    assert any(
        f.get("check") == "content_verifiable" and f.get("passed") is False
        for f in report["findings"]
    ), report["findings"]
