"""生成任务事实的读取适配（数据属主：``production_planning``）。

契约在 ``packages/shared/generation_ports.py``，使用方是 QC 域的人工恢复入队
（``services/qc_governance/service/qc_task_service.py`` 的 ``recover``）。装配在
``apps/api/wiring.py``——三个位置各司其职，谁都不认识谁的内部结构。

为什么适配必须在数据属主这一侧
------------------------------
``qc_governance_role`` 对 ``production_planning`` **零表授权**（实测
``information_schema.role_table_grants``）。这不是配置疏漏，而是 DATA-1 /
R16 分域的本意：越域读不靠代码自觉拦，靠 ``permission denied`` 拦。
所以恢复入队要读生产事实时，只有两条路——由生产域提供适配（本文件），
或者给 QC 角色补一条跨域授权。后者会把「读一个事实」变成「永久开一扇门」，
下一个顺手 JOIN 就没有任何东西能拦住。

为什么用 ORM 而不是裸 SQL
------------------------
本文件在数据的属主域内，模型就是事实的唯一定义。在别处拼 SQL 会随模型演进而
漂移：R16 分域迁移之后，``packages/identity/ownership.py`` 的 ``_RESOLVE_SQL``
仍指向旧 schema，导致审批端点整条 500，而它的服务层单测全绿。教训是
「表名与列名不会自己举手」，所以这里只依赖本域模型的属性，不写字符串 SQL。

为什么查两列（task id + project id）
-----------------------------------
``generation_tasks`` 的唯一键是 ``(generation_task_id, project_id, tenant_id)``。
只按 task id 查可能命中多行，``scalar_one_or_none`` 会抛 ``MultipleResultsFound``，
而把它改成 ``first()`` 就是把「恢复哪一条」变成静默的任意选择。这里要求
调用方同时给出项目：查不到 → ``None`` → 使用方翻译成 404，
既不给跨租户留探测位，也不让歧义被静默消解。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.generation_ports import GenerationFacts, GenerationFactsLookup
from services.production_planning.repository.orm import GenerationTaskModel


def build_generation_facts_lookup(session: AsyncSession) -> GenerationFactsLookup:
    """在给定会话（含 RLS 租户上下文）内构造事实读取函数。"""

    async def lookup(
        generation_task_id: str, project_id: str, tenant_id: UUID
    ) -> GenerationFacts | None:
        model = (
            await session.execute(
                select(GenerationTaskModel).where(
                    GenerationTaskModel.generation_task_id == generation_task_id,
                    GenerationTaskModel.project_id == project_id,
                    GenerationTaskModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if model is None:
            return None
        return GenerationFacts(
            generation_task_id=model.generation_task_id,
            tenant_id=model.tenant_id,
            status=model.status,
            project_id=model.project_id,
            shot_id=model.shot_id,
            asset_version_id=model.asset_version_id,
            quality_profile_version=model.quality_profile_version,
            organization_id=model.organization_id,
        )

    return lookup


__all__ = ["build_generation_facts_lookup"]
