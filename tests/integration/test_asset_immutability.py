"""资产内容列不可变的结构性验证（数据库触发器，ADR-R11）。

这些断言不测代码路径，而是测"结构层最后一道墙"：
- 从 app_role、迁移角色到超级用户，改写内容列一律被拒；
- 允许改的只有状态列（status/entity_version/updated_at/audit_ref）；
- 版本与 QC 证据只能新增，不能删除。

为什么必须测这一层：仓储白名单只约束"走仓储的写入"，直连 SQL、运维脚本、
未来新增的仓储实现都可能绕过它。结构不成立，"不可变"就只是文档声明。
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import build_migration_url


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    """以迁移角色（超级用户）连库：证明触发器对超级用户同样生效。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _one_asset(admin_engine: AsyncEngine) -> tuple[str, str]:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT entity_id, sha256 FROM asset_service.asset_versions "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).first()
    if row is None:
        pytest.skip("库中没有 asset_versions 样本，跳过不可变验证")
    return str(row[0]), str(row[1])


async def _one_qc_report(admin_engine: AsyncEngine) -> tuple[str, str]:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT qc_report_id, asset_version_id "
                    "FROM asset_service.qc_reports ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).first()
    if row is None:
        pytest.skip("库中没有 qc_reports 样本，跳过不可变验证")
    return str(row[0]), str(row[1])


async def test_asset_content_column_update_is_rejected(
    admin_engine: AsyncEngine,
) -> None:
    """改写 sha256 必须被拒——即使以超级用户身份直连。

    目标值必须与当前值不同：触发器保护的是"改写"，同值赋值不算改写。
    早先固定写 ``"f"*64``，一旦库里最新样本恰好就是该值，用例会在
    "什么都没改"的前提下变红，看起来像触发器失效（实测踩过）。
    """
    entity_id, current_sha = await _one_asset(admin_engine)
    target_sha = "0" * 64 if current_sha != "0" * 64 else "1" * 64
    async with admin_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                sa.text(
                    "UPDATE asset_service.asset_versions SET sha256 = :sha "
                    "WHERE entity_id = :id"
                ),
                {"sha": target_sha, "id": entity_id},
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
