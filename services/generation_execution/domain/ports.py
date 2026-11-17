"""generation-execution 域端口协议（依赖倒置：领域只依赖抽象）。

两类端口，方向相同（本域定义协议，外部提供实现）：

1. ``RefOwnershipPort``（读方向）：GENERATION.REQUEST 准入校验需要确认
   shot_spec_ref / visual_plan_ref / constraint_snapshot_ref 指向的对象
   **存在、属于请求声明的项目、且绑定在请求的镜头上**。这些对象的所有者是
   shot_planning 域——本域不 import shot_planning 的任何东西（R2：领域模块
   之间禁止直连），只定义端口；适配器在组合根（apps/api）装配，复用 ADR-R28
   的登记式解析器（读永远经过所有者域自己的数据库身份）。

1b. ``ShotOwnershipPort``（读方向）：选路（PRODUCTION.ROUTE.SELECT）需要确认
   镜头真实存在且归属该项目的归属方，否则可以为"幽灵镜头"登记生效路线。

2. ``GenerationOutputRegistrar``（写方向）：执行成功的产出必须登记为资产
   版本（ASSET.VERSION.CREATE 的领域侧入口），而资产表的所有者是 asset_service
   域——本域同样不 import 它，只定义"登记产出"这一个动作；适配器在 worker
   进程装配（ADR-R33）。写方向比读方向多一层要求：**实现方必须自己开资产域
   的数据库会话**（域角色 + RLS），不允许借 generation 的连接写别人的表。

``RefOwnership`` 只保留归属判定所需的最小事实（project_id + 镜头绑定 +
所有者域的准入结论）。"最小"不等于"能过就算"——少一个镜头维度，跨镜头错配
就成了完全合法的路径（P1-1）。
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
    """引用对象的归属事实（project_id + 镜头绑定 + 准入判定）。

    ``eligible`` 是**所有者域**对"该引用能否作为生成输入"的判定：
    - ``True``：所有者确认可用（例如约束包处于 ACTIVE）；
    - ``False``：所有者明确判定不可用 → 受理拒绝；
    - ``None``：契约没有为该类型写明状态准入规则 → **不做判定**。

    为什么不是消费域去读对方的状态词表：状态取值是所有者域的词汇，消费域
    照着对方枚举写 if 就是把两个域焊死（换一次状态名，生成侧静默失效）。
    让所有者回答"能不能用"，消费域只认结论——与 ADR-R28 的"读经过所有者"
    同一条纪律。``None`` 不表示"默认放行"，而表示"没有规则可判"，两者在
    审计上是不同事实，不合并。

    ``shot_ids`` 同理但更硬：它回答"这个引用绑定在哪几个镜头上"。
    - ``None``：该类型不做镜头级绑定（例如 DeliveryManifest）；
    - 集合：引用只能用于集合内的镜头（ShotSpec/ShotVisualPlan 单元素，
      ConstraintPack 多元素）。

    为什么不能只要 project_id：同一项目内把镜头 A 的规格/视觉方案/约束包
    用在镜头 B 的生成上，项目归属校验完全挡不住，而产出会与镜头意图错配——
    这是数据正确性问题，不是权限问题。镜头身份在跨域契约里必须显式传递。
    """

    project_id: str
    eligible: bool | None = None
    #: 所有者给出的不可用理由（eligible=False 时用于拒绝文案；可空）。
    ineligible_reason: str = ""
    #: 该引用绑定的镜头集合；``None`` = 该类型不做镜头级绑定。
    shot_ids: frozenset[str] | None = None


@dataclass(frozen=True)
class ShotFacts:
    """镜头的事实（存在性 + 项目归属）。

    只回答两件事：这个 shot_id 在本租户内是否存在、属于哪个项目。
    镜头的**状态/内容**不在这里——选路只需要"镜头真实存在且归属对"，把剧情
    语义带进生成域只会制造新的耦合面。
    """

    shot_id: str
    project_id: str


@runtime_checkable
class ShotOwnershipPort(Protocol):
    """镜头归属解析端口（实现由组合根提供，读经过 shot_planning 域）。

    用途：``PRODUCTION.ROUTE.SELECT`` 不得为不存在的镜头登记生效路线——
    幽灵 shot 会污染 ``shot_route_selections`` 的唯一键，且让后续"该镜头已
    选路"的判断基于一个从未存在的对象。
    """

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        shot_id: str,
    ) -> ShotFacts | None:
        """解析镜头事实；不存在返回 None（区分 404 与 422）。"""
        ...


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

