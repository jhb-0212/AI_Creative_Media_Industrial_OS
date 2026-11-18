"""仓储端口失败契约的**真实驱动**验证（P2-03 的运行时证据）。

单元测试证明「翻译规则对不对」，本文件证明「翻译真的接在真实 asyncpg 上」：
SQLAlchemy 的包装层会把驱动异常藏在 ``exc.orig`` 里，属性名也随驱动而异
（asyncpg 用 ``sqlstate``）。只测纯函数等于假设这些细节成立。

三个用例对应三种必须区分的失败：

1. 同 id 重复 INSERT → ``DuplicateEntityError``（可幂等回读）；
2. 归属项目不存在（外键 23503）→ ``ReferentialIntegrityError``，**不得**被当成
   重复提交——旧实现把所有 ``IntegrityError`` 一律幂等降级，这条负向断言正是
   防止那种"把错误变成成功"的回归；
3. 同一路径下 session 仍可用（SAVEPOINT 生效）：翻译不能靠把连接搞坏来实现。

依赖真实 PostgreSQL；无 PG 环境时本文件跳过（与 tests/integration 同口径）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.asset_service.domain.asset_version import AssetVersion
from services.asset_service.repository.sqlalchemy_asset_repo import (
    SqlAlchemyAssetRepository,
)


def _now() -> datetime:
    return datetime.now(UTC)


async def _seed_project(db_sessionmaker, tenant: uuid.UUID, project_id: str) -> None:
    """播种归属项目（project_planning 域角色写入，asset_service 角色无写权限）。"""
    async with db_sessionmaker() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        await session.execute(
            text(
                """
                INSERT INTO project_planning.projects
                    (entity_id, entity_type, schema_version, entity_version,
                     status, owner_module, tenant_id, created_at, created_by,
                     updated_at, project_name, project_tier, organization_id)
                VALUES
                    (:eid, 'Project', 205, 1, 'DRAFT', 'project_planning',
                     :tid, :now, 'tester', :now, '端口契约项目', 'B', NULL)
                """
            ),
            {"eid": project_id, "tid": tenant, "now": _now()},
        )
        await session.commit()


def _asset(tenant: uuid.UUID, project_id: str, entity_id: str | None = None) -> AssetVersion:
    return AssetVersion(
        entity_id=entity_id or uuid.uuid4().hex,
        created_by="tester",
        tenant_id=str(tenant),
        project_id=project_id,
        asset_type="video",
        sha256="c" * 64,
        storage_ref={"uri": "s3://bucket/key.mp4"},
    )


async def _asset_session(asset_sessionmaker, tenant: uuid.UUID):
    """打开资产域会话并设置租户上下文（RLS 依赖会话级 GUC）。"""
    session = asset_sessionmaker()
    await session.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"),
        {"t": str(tenant)},
    )
    return session


async def test_duplicate_primary_key_translates_to_duplicate_entity_error(
    db_sessionmaker, asset_sessionmaker
) -> None:
    """真实驱动下的主键冲突 → DuplicateEntityError（携带 entity_id）。"""
    tenant = uuid.uuid4()
    project_id = f"port-{uuid.uuid4().hex[:8]}"
    await _seed_project(db_sessionmaker, tenant, project_id)
    asset = _asset(tenant, project_id)

    session = await _asset_session(asset_sessionmaker, tenant)
    try:
        repo = SqlAlchemyAssetRepository(session)
        await repo.add(asset)
        with pytest.raises(DuplicateEntityError) as exc_info:
            await repo.add(_asset(tenant, project_id, entity_id=asset.entity_id))
        assert exc_info.value.entity_id == asset.entity_id
        # SAVEPOINT 生效：翻译不是靠让 session 进入 aborted 态实现的，仍可查询。
        assert await repo.get_by_id(asset.entity_id) is not None
    finally:
        await session.rollback()
        await session.close()


async def test_missing_project_translates_to_referential_integrity_error(
    db_sessionmaker, asset_sessionmaker
) -> None:
    """归属项目不存在（23503）→ ReferentialIntegrityError，**不得**是重复冲突。

    这条断言守的是"错误不能被降级成成功"：若译成 DuplicateEntityError，服务层
    会回读、找不到既有记录，然后把一个真实的归属失效报成 500 或（更糟）在
    存在同名记录时幂等返回，调用方以为资产已入库。
    """
    tenant = uuid.uuid4()
    session = await _asset_session(asset_sessionmaker, tenant)
    try:
        repo = SqlAlchemyAssetRepository(session)
        with pytest.raises(ReferentialIntegrityError) as exc_info:
            await repo.add(_asset(tenant, f"ghost-{uuid.uuid4().hex[:8]}"))
        assert not isinstance(exc_info.value, DuplicateEntityError)
        assert exc_info.value.sqlstate == "23503"
    finally:
        await session.rollback()
        await session.close()
