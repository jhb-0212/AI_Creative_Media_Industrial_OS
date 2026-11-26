"""QC 任务的测试播种夹具：按指定租户造一条「已入队 + 已进投影」的任务。

为什么单独成模块：调度、租户池复用、端到端闭环三组用例都需要同一条种子，
各写一份就会出现「一处补了新必填字段、另一处没补」的漂移——测出来的绿灯
不可信。种子必须走**真实服务**（``QCTaskService.enqueue``），这样投影是同事务
写出来的，而不是测试自己往投影表里插一行假装同步。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.shared.database import create_qc_governance_engine, create_sessionmaker
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from services.qc_governance.domain.qc_task import EnqueueOrigin
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.repository.task_projection_repo import (
    SqlAlchemyTaskProjectionRepository,
)
from services.qc_governance.service.qc_task_service import QCTaskService
from tests.conftest import set_test_scope
from tests.integration.planning_seed import seed_project

__all__ = ["qc_sessionmaker_factory", "seed_tenant_task"]


def qc_sessionmaker_factory() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """建一个 qc_governance 角色引擎 + 会话工厂（由各测试模块的 fixture 持有）。

    不把 fixture 直接 export 给别的测试模块 import：同名 fixture 与被测函数
    参数同名时，静态检查会把「导入的 fixture」和「函数参数」看成一次重定义，
    而自动修复给出的建议恰好是**删掉参数**——那会把用例悄悄改成不接种子。
    让每个模块自己持有引擎，冲突从源头消失。
    """
    engine = create_qc_governance_engine()
    return engine, create_sessionmaker(engine)


async def seed_tenant_task(
    qc_sm: async_sessionmaker[AsyncSession],
    admin_sm: async_sessionmaker[AsyncSession],
    *,
    suffix: str | None = None,
    tenant: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    """在 ``tenant``（默认新租户）下播种一条 QUEUED 任务，返回 ``(tenant, task_id)``。"""
    suffix = suffix or uuid.uuid4().hex[:8]
    tenant = tenant or uuid.uuid4()
    project_id = f"proj-qcseed-{suffix}"
    await seed_project(admin_sm, project_id=project_id, tenant=tenant)
    async with qc_sm() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = QCTaskService(
            SqlAlchemyQCTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
            projection=SqlAlchemyTaskProjectionRepository(session),
        )
        task = await service.enqueue(
            project_id=project_id,
            asset_version_id=f"av-qcseed-{suffix}",
            generation_task_id=f"gen-qcseed-{suffix}",
            quality_profile_version=f"qp-{suffix}",
            tenant_id=tenant,
            origin=EnqueueOrigin(source="RELAY", operator_id="outbox-relay"),
            shot_id=f"shot-{suffix}",
            qc_task_id=f"qc-qcseed-{suffix}",
        )
        await session.commit()
        return tenant, task.entity_id
