"""生成产出 → 资产版本的登记适配器（``GenerationOutputRegistrar`` 的实现）。

为什么适配器在 worker 层而不是 services 层：
- 跨域**写**必须经过所有者域自己的数据库身份（asset_service 角色 + RLS），
  而 assembly 点是唯一同时知道两个域的地方。worker 层允许依赖 services
  （``scripts/architecture_check.py`` 的 LAYER_RULES），generation 域则只
  认识端口——域之间仍然没有 import 关系（R2 不立白名单也一样成立）。

幂等来自**确定性资产版本号**：``av_<generation_task_id>``。
- 执行环的顺序是"先登记产出、后推进任务终态"（终态要登记产出对象引用）。
  若登记成功而终态提交失败，下一轮重跑会再次调用本适配器——同一任务必然
  得到同一资产版本号，资产域按内容幂等返回既有版本，不会新增版本、更不会
  覆盖（产出对象不可覆盖是资产域红线）。重试链（新任务）拿到的是新任务 id，
  因此自然产生新版本，不需要额外去重逻辑。

授权面（为什么这里不调 ``authorize``）：
- 契约把 ASSET.VERSION.CREATE 的合法主体直接写成 ``Worker/Model Adapter``，
  即"服务进程本身就是该动作的主体"。HTTP 侧的 ``require_action`` 依据的是
  JWT 角色 + Capability Token，而 worker 进程没有这两样东西；在进程内伪造
  一份 capability 才是真正的问题（把"有票据"变成"自己开票"）。
- worker 侧的等价约束是三条真实存在的闸门：受信编排上下文（组织/项目范围
  来自配置，不从任务行自称）、资产域数据库角色（写不了别的 schema）、RLS
  （项目级限制性策略）+ ``AssetService`` 的 ``assert_project_scope``。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.identity.principal import Principal
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.rls import set_request_context
from services.asset_service.repository.sqlalchemy_asset_repo import (
    SqlAlchemyAssetRepository,
)
from services.asset_service.repository.sqlalchemy_qc_repo import (
    SqlAlchemyQCReportRepository,
)
from services.asset_service.service.asset_service import AssetService
from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.domain.output import GenerationOutput
from services.generation_execution.domain.ports import OutputRegistration

SessionMaker = async_sessionmaker[AsyncSession]

#: worker 服务身份（审计引用用，与 executor 的 ACTOR 同值）。
ACTOR = "svc_generation_executor"

#: 资产版本号前缀：让"这条资产来自生成任务"在 id 上就能读出来。
_ASSET_ID_PREFIX = "av_"


def derive_asset_version_id(task: GenerationTask) -> str:
    """确定性资产版本号（幂等键；同一任务必然同一 id）。"""
    return f"{_ASSET_ID_PREFIX}{task.entity_id}"


@dataclass(frozen=True)
class AssetVersionRegistrar:
    """把生成产出登记为资产版本（GENERATED 态，不越过任何审批）。

    ``asset_sessionmaker`` 必须是**资产域角色**的会话工厂：登记是资产域的
    写动作，用 generation 的连接写 asset_service 表会被数据库直接拒绝
    （这正是按域拆角色的目的）。
    """

    asset_sessionmaker: SessionMaker

    async def register(
        self,
        *,
        task: GenerationTask,
        output: GenerationOutput,
        principal: Principal,
    ) -> OutputRegistration:
        """登记产出（幂等）；失败抛异常，由执行环把任务推进到 FAILED。"""
        async with self.asset_sessionmaker() as session:
            await set_request_context(session, principal)
            service = AssetService(
                SqlAlchemyAssetRepository(session),
                SqlAlchemyQCReportRepository(session),
                SqlAlchemyOutboxWriter(session),
            )
            asset = await service.create_version(
                project_id=task.project_id,
                asset_type=output.asset_type,
                sha256=output.sha256,
                storage_ref=dict(output.storage_ref),
                # 生成产出没有前序资产版本：它由生成任务派生，来源可经
                # task.result_ref → asset_version_id 双向追溯。
                source_asset_version_ids=[],
                # 权利凭据取受理时定型的准入事实（可空：缺失则不可批准）。
                rights_manifest_ref=task.rights_manifest_ref,
                # adapter_id 记录"哪个路线产出它"——路线即能力条目，
                # 出了质量问题要能回答"是哪条路线做的"。
                adapter_id=task.route_id,
                tool_product=None,
                operator_ref=ACTOR,
                version=None,
                principal=principal,
                entity_id=derive_asset_version_id(task),
            )
            await session.commit()
            # 只取主键：SET LOCAL 的 RLS 上下文随 commit 清除，响应不依赖
            # 会话内对象属性（D3 教训）。
            return OutputRegistration(asset_version_id=asset.entity_id)


__all__ = ["AssetVersionRegistrar", "derive_asset_version_id"]
