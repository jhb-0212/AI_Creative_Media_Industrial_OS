"""generation-execution 领域枚举。

严格对齐冻结契约：
- object_schemas.schema.json → ProductionRoute（V205-R14-A6 追认）
- object_schemas.schema.json → ShotRouteSelection（V205-R14-A9 追认）
- 契约修订 A6 残余风险登记：状态集随本域实现段显式定型（本次定型）
"""

from __future__ import annotations

from enum import Enum


class ProductionRouteStatus(str, Enum):
    """生产路线状态。

    对齐契约依据（V205-R14-A6 / 基线正文既有规则，非新造）：
    - 基线 16.1：未验证能力标记 UNVERIFIED，不得作为生产路由的可用能力；
    - 基线 16.2：仅可将实际通过验证的能力标记为 VERIFIED（不得手工将
      未测试能力标为 VERIFIED，与 EXTERNAL_TOOL.ADAPTER.VERIFY 同源规则）；
    - 动作矩阵 PRODUCTION.ROUTE.SELECT 前置条件：路线须处于 VERIFIED。

    VERIFIED 为终态：路线验证是"对某版本能力提交过证据"的事实，撤销验证
    属于新证据问题，基线未给撤销语义——不猜契约，不造 REVOKED 边。
    标定 VERIFIED 的入口是适配器验证工作流（集成工程负责人执行验证 +
    安全/QC 独立复核），当前无 HTTP action 承载，路线目录由运维播种。
    """

    UNVERIFIED = "UNVERIFIED"  # 未验证：不得被 PRODUCTION.ROUTE.SELECT 选中
    VERIFIED = "VERIFIED"      # 已验证：证据齐备，可进入生产选路（终态）


class GenerationTaskStatus(str, Enum):
    """生成任务状态。

    对齐契约依据（V205-R14-A6 / 基线正文既有规则，非新造）：
    - 动作矩阵 GENERATION.REQUEST 行：准入由预算/配额/依赖/约束快照校验；
    - 基线 9.4：Worker 崩溃 → 租约到期后重新领取（幂等重试）；参数校验
      失败/输入损坏等不重试；自动重试有上限；
    - A6 冻结 description：**失败为终结态**——重试以新任务承载（attempt+1），
      不在原任务上复活（终态不可原地复活红线）。
    """

    PENDING = "PENDING"      # 已受理，待 worker 领取
    RUNNING = "RUNNING"      # worker 已领取，执行中
    SUCCEEDED = "SUCCEEDED"  # 产出就绪（终态）
    FAILED = "FAILED"        # 执行失败（终态；重试以新任务承载）


class ShotRouteSelectionStatus(str, Enum):
    """镜头生效路线的状态（单值状态集，无转移）。

    对齐契约依据（V205-R14-A9 / 基线正文既有规则，非新造）：
    - 31.2 Phase 1 核心生产主链：**单一路线生成**；
    - C.7：事实记录不可原地改写。

    为什么只有一个值：生效路线是「某镜头在某一刻选定某路线」的**受理事实**，
    不是生命周期对象。它不是"会推进"的东西，只有两种结局——登记成功或被
    拒绝（ROUTE_ALREADY_SELECTED）。造一个 SUPERSEDED 值却没有对应 action
    可触发，等于在契约里留一个不可达状态；真正的路线变更（带 STALE 传播与
    审批）应由独立治理动作承载，届时同步扩状态集。
    """

    ACTIVE = "ACTIVE"  # 唯一的生效态（登记后不可改写、不可删除）
