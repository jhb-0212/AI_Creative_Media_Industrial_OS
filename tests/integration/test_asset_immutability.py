"""资产内容列不可变的结构性验证（数据库触发器，ADR-R11）。

这些断言不测代码路径，而是测"结构层最后一道墙"：
- 从 app_role、迁移角色到超级用户，改写内容列一律被拒；
- 允许改的只有状态列（status/entity_version/updated_at/audit_ref）；
- 版本与 QC 证据只能新增，不能删除。

为什么必须测这一层：仓储白名单只约束"走仓储的写入"，直连 SQL、运维脚本、
未来新增的仓储实现都可能绕过它。结构不成立，"不可变"就只是文档声明。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import build_migration_url
from packages.shared.storage_locator import build_locator

# 测试自造的样本主键（确定性，避免与真实数据冲突）。
_SAMPLE_ASSET_ID = "immut-sample-0001"
_SAMPLE_PROJECT_ID = "immut-project-0001"
_SAMPLE_QC_ID = "immut-qc-0001"


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    """以迁移角色（超级用户）连库：证明触发器对超级用户同样生效。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
async def _seed_samples(admin_engine: AsyncEngine) -> None:
    """自造样本：触发器只拦 UPDATE/DELETE，合法 INSERT 可写入。

    不依赖外部手动造数——容器重建后测试仍能自证。样本用确定性主键，
    幂等（ON CONFLICT DO NOTHING），跑多轮不重复。
    """
    now = datetime.now(UTC)
    tenant = uuid.UUID("00000000-0000-0000-0000-000000000001")
    async with admin_engine.begin() as conn:
        # 归属前提（SCOPE-3）：资产必须挂到真实项目上，复合外键才放行。
        await conn.execute(
            sa.text(
                "INSERT INTO project_planning.projects "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " project_name, project_tier, organization_id) "
                "VALUES (:pid, 'Project', 205, 1, 'DRAFT', 'project_planning', "
                " :tid, :now, 'test-seeder', :now, '不可变性样本项目', 'B', NULL) "
                "ON CONFLICT (entity_id) DO NOTHING"
            ),
            {"pid": _SAMPLE_PROJECT_ID, "tid": tenant, "now": now},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO asset_service.asset_versions "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " project_id, asset_type, sha256, storage_ref, "
                " source_asset_version_ids, version) "
                "VALUES (:id, 'AssetVersion', 1, 1, 'APPROVED', 'asset_service', "
                " :tid, :now, 'test-seeder', :now, :pid, 'image', :sha, "
                " :locator, '[]'::jsonb, 'v1') "
                "ON CONFLICT (entity_id) DO NOTHING"
            ),
            {
                "id": _SAMPLE_ASSET_ID,
                "tid": tenant,
                "now": now,
                "sha": "a" * 64,
                # storage_ref 必须是受控定位符，且内嵌哈希与 sha256 一致
                # （DB CHECK 同时校验形态、派生关系与跨列一致）。
                "locator": build_locator("a" * 64, "png"),
                "pid": _SAMPLE_PROJECT_ID,
            },
        )
        await conn.execute(
            sa.text(
                "INSERT INTO asset_service.qc_reports "
                "(qc_report_id, asset_version_id, tenant_id, "
                " quality_profile_version, detector_versions, findings, "
                " created_by, created_at, updated_at) "
                "VALUES (:id, :aid, :tid, 'qp-v1', '{}'::jsonb, '[]'::jsonb, "
                " 'test-seeder', :now, :now) "
                "ON CONFLICT (qc_report_id) DO NOTHING"
            ),
            {
                "id": _SAMPLE_QC_ID,
                "aid": _SAMPLE_ASSET_ID,
                "tid": uuid.UUID("00000000-0000-0000-0000-000000000001"),
                "now": now,
            },
        )


async def _one_asset(admin_engine: AsyncEngine) -> tuple[str, str]:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT entity_id, sha256 FROM asset_service.asset_versions "
                    "WHERE entity_id = :id"
                ),
                {"id": _SAMPLE_ASSET_ID},
            )
        ).first()
    assert row is not None, "样本 fixture 未生效：asset_versions 无样本"
    return str(row[0]), str(row[1])


async def _one_qc_report(admin_engine: AsyncEngine) -> tuple[str, str]:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT qc_report_id, asset_version_id "
                    "FROM asset_service.qc_reports WHERE qc_report_id = :id"
                ),
                {"id": _SAMPLE_QC_ID},
            )
        ).first()
    assert row is not None, "样本 fixture 未生效：qc_reports 无样本"
    return str(row[0]), str(row[1])


async def test_asset_content_column_update_is_rejected(
    admin_engine: AsyncEngine,
) -> None:
    """改写 sha256 必须被拒——即使以超级用户身份直连。"""
    entity_id, _ = await _one_asset(admin_engine)
    async with admin_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                sa.text(
                    "UPDATE asset_service.asset_versions SET sha256 = :sha WHERE entity_id = :id"
                ),
                {"sha": "f" * 64, "id": entity_id},
            )
    assert "不可变内容列" in str(exc_info.value)


async def test_asset_version_delete_is_rejected(admin_engine: AsyncEngine) -> None:
    """版本只能新增，整行删除必须被拒（否则"不可变"可以被 DELETE 绕开）。"""
    entity_id, _ = await _one_asset(admin_engine)
    async with admin_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                sa.text("DELETE FROM asset_service.asset_versions WHERE entity_id = :id"),
                {"id": entity_id},
            )
    assert "禁止删除" in str(exc_info.value)


async def test_status_columns_remain_writable(admin_engine: AsyncEngine) -> None:
    """状态列必须仍可写：保护的是内容，不是把整张表冻结。"""
    entity_id, _ = await _one_asset(admin_engine)
    async with admin_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                sa.text(
                    "UPDATE asset_service.asset_versions "
                    "SET updated_at = updated_at WHERE entity_id = :id"
                ),
                {"id": entity_id},
            )
        finally:
            await trans.rollback()


async def test_qc_report_update_and_delete_are_rejected(
    admin_engine: AsyncEngine,
) -> None:
    """QC 证据是追加型：改写结论文档必须被拒，删除证据同样被拒。"""
    report_id, asset_version_id = await _one_qc_report(admin_engine)

    async with admin_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                sa.text(
                    "UPDATE asset_service.qc_reports SET findings = '[]'::jsonb "
                    "WHERE qc_report_id = :id"
                ),
                {"id": report_id},
            )
        assert "追加型检测证据" in str(exc_info.value)

    async with admin_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                sa.text("DELETE FROM asset_service.qc_reports WHERE qc_report_id = :id"),
                {"id": report_id},
            )
        assert "追加型检测证据" in str(exc_info.value)

    # 证据仍可被读取（拒绝的是改写，不是可见性）。
    async with admin_engine.connect() as conn:
        count = (
            await conn.execute(
                sa.text(
                    "SELECT count(*) FROM asset_service.qc_reports "
                    "WHERE qc_report_id = :id AND asset_version_id = :aid"
                ),
                {"id": report_id, "aid": asset_version_id},
            )
        ).scalar_one()
    assert count == 1
