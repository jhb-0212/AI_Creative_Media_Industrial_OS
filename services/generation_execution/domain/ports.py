"""generation-execution 域端口协议（依赖倒置：领域只依赖抽象）。

两类端口，方向相同（本域定义协议，外部提供实现）：

1. ``RefOwnershipPort``（读方向）：GENERATION.REQUEST 准入校验需要确认
   shot_spec_ref / visual_plan_ref / constraint_snapshot_ref 指向的对象
   **存在且属于请求声明的项目**。这些对象的所有者是 shot_planning 域——
   本域不 import shot_planning 的任何东西（R2：领域模块之间禁止直连），
   只定义端口；适配器在组合根（apps/api）装配，复用 ADR-R28 的登记式
   解析器（读永远经过所有者域自己的数据库身份）。

2. ``GenerationOutputRegistrar``（写方向）：执行成功的产出必须登记为资产
   版本（ASSET.VERSION.CREATE 的领域侧入口），而资产表的所有者是 asset_service
   域——本域同样不 import 它，只定义"登记产出"这一个动作；适配器在 worker
   进程装配（ADR-R33）。写方向比读方向多一层要求：**实现方必须自己开资产域
   的数据库会话**（域角色 + RLS），不允许借 generation 的连接写别人的表。

``RefOwnership`` 只保留归属判定所需的最小事实（project_id + 所有者域的准入
结论），向消费域泄漏更多业务字段只会制造新的耦合面。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from packages.identity.principal import Principal
from services.generation_execution.domain.output import GenerationOutput

if TYPE_CHECKING:  # 仅类型标注需要，避免领域内模块级循环
    from services.generation_execution.domain.generation_task import GenerationTask


@dataclass(frozen=True)
class RefOwnership:
    """引用对象的归属事实（最小暴露面：project_id + 准入判定）。

    ``eligible`` 是**所有者域**对"该引用能否作为生成输入"的判定：
    - ``True``：所有者确认可用（例如约束包处于 ACTIVE）；
    - ``False``：所有者明确判定不可用 → 受理拒绝；
    - ``None``：契约没有为该类型写明状态准入规则 → **不做判定**。

    为什么不是消费域去读对方的状态词表：状态取值是所有者域的词汇，消费域
    照着对方枚举写 if 就是把两个域焊死（换一次状态名，生成侧静默失效）。
    让所有者回答"能不能用"，消费域只认结论——与 ADR-R28 的"读经过所有者"
    同一条纪律。``None`` 不表示"默认放行"，而表示"没有规则可判"，两者在
    审计上是不同事实，不合并。
    """

    project_id: str
    eligible: bool | None = None
    #: 所有者给出的不可用理由（eligible=False 时用于拒绝文案；可空）。
    ineligible_reason: str = ""


@runtime_checkable
class RefOwnershipPort(Protocol):
    """跨域引用对象归属解析端口（实现由组合根提供）。"""

    def can_resolve(self, resource_type: str) -> bool:
        """资源类型是否有解析器（未登记的类型 fail-closed 拒绝）。"""
        ...

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        resource_type: str,
        resource_id: str,
    ) -> RefOwnership | None:
        """解析引用对象归属；不存在返回 None（区分 404 与跨项目 422）。"""
        ...


@dataclass(frozen=True)
class OutputRegistration:
    """产出登记结果（只有资产版本号——执行环不需要别的）。"""

    asset_version_id: str


@runtime_checkable
class GenerationOutputRegistrar(Protocol):
    """生成产出登记端口（实现由 worker 进程的组合根提供，ADR-R33）。

    契约：**幂等**。同一任务（同一 attempt）重复登记必须返回同一资产版本，
    不得新增版本——执行环"登记成功但终态提交失败 → 下一轮重跑"的恢复路径
    完全依赖这一条（产出对象不可覆盖是资产域红线，重跑不能靠覆盖收敛）。
    """

    async def register(
        self,
        *,
        task: GenerationTask,
        output: GenerationOutput,
        principal: Principal,
    ) -> OutputRegistration:
        """把产出登记为资产版本（GENERATED），返回资产版本号。失败抛异常。"""
        ...

