"""归属外键约束负向测试（F04：数据库层锁死归属真实性）。

验证第二阶段落地的数据库级归属约束真实生效（R2 不变量放最可靠层）：

1. SCOPE-1：projects.organization_id 外键 -> organizations(organization_id)
   —— 写入引用不存在组织的项目必须被数据库拒绝。
2. SCOPE-2：episodes.project_id 外键 -> projects(entity_id)
   —— 写入引用不存在项目的集数必须被数据库拒绝，而非仅靠 Service 校验。

依赖真实 PostgreSQL（app_role 连接，NOBYPASSRLS），无 PG 环境时本文件
全部跳过（CI 有 PG 时执行，本地无 PG 时不影响其它闸门）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def _now() -> datetime:
    return datetime.now(UTC)


async def test_project_must_reference_existing_organization(db_sessionmaker) -> None:
    """写引用不存在组织的项目 → 外键约束拒绝（SCOPE-1）。"""
    sm = db_sessionmaker
    nonexistent_org = uuid.uuid4()
    tenant = uuid.uuid4()

    async with sm() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO project_planning.projects
                        (entity_id, entity_type, schema_version, entity_version,
                         status, owner_module, tenant_id, created_at, created_by,
                         updated_at, project_name, project_tier, organization_id)
                    VALUES
                        (:eid, 'Project', 205, 1, 'DRAFT', 'project_planning',
                         :tid, :now, 'tester', :now, :name, 'B', :org)
                    """
                ),
                {
                    "eid": uuid.uuid4().hex,
                    "tid": tenant,
                    "now": _now(),
                    "name": "归属缺失项目",
                    "org": nonexistent_org,
                },
            )
        await session.rollback()


async def test_episode_must_reference_existing_project(db_sessionmaker) -> None:
    """写引用不存在项目的集数 → 外键约束拒绝（SCOPE-2）。"""
    sm = db_sessionmaker
    tenant = uuid.uuid4()
    nonexistent_project_id = uuid.uuid4().hex

    async with sm() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO project_planning.episodes
                        (entity_id, entity_type, schema_version, entity_version,
                         status, owner_module, tenant_id, created_at, created_by,
                         updated_at, project_id, episode_ref, title)
                    VALUES
                        (:eid, 'Episode', 205, 1, 'DRAFT', 'project_planning',
                         :tid, :now, 'tester', :now, :pid, 'EP01', '悬空集数')
                    """
                ),
                {
                    "eid": uuid.uuid4().hex,
                    "tid": tenant,
                    "now": _now(),
                    "pid": nonexistent_project_id,
                },
            )
        await session.rollback()


async def test_project_with_valid_organization_succeeds(db_sessionmaker) -> None:
    """正向：先建组织再建归属项目 → 外键约束放行（SCOPE-1 不误伤合法路径）。"""
    sm = db_sessionmaker
    tenant = uuid.uuid4()
    org_id = uuid.uuid4()

    async with sm() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        # 1. 建组织。
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, '测试组织', :now, 'tester', :now)
                """
            ),
            {"org": org_id, "tid": tenant, "now": _now()},
        )
        # 2. 建归属该组织的项目（不抛异常即通过）。
        await session.execute(
            text(
                """
                INSERT INTO project_planning.projects
                    (entity_id, entity_type, schema_version, entity_version,
                     status, owner_module, tenant_id, created_at, created_by,
                     updated_at, project_name, project_tier, organization_id)
                VALUES
                    (:eid, 'Project', 205, 1, 'DRAFT', 'project_planning',
                     :tid, :now, 'tester', :now, :name, 'B', :org)
                """
            ),
            {
                "eid": uuid.uuid4().hex,
                "tid": tenant,
                "now": _now(),
                "name": "合法归属项目",
                "org": org_id,
            },
        )
        await session.rollback()
