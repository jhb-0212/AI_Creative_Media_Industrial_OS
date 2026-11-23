"""project-planning 领域枚举。

严格对齐冻结契约：
- enum_registry.yaml 中的枚举定义
- 主文档 8.1 核心实体、14.2 数据所有权
"""

from __future__ import annotations

from enum import Enum


class ProjectStatus(str, Enum):
    """项目状态。

    对齐契约：contracts/state_machines/state_machines.yaml → objects → Project
    （DRAFT→ACTIVE→ON_HOLD；ACTIVE→COMPLETED；ON_HOLD→ACTIVE；
    DRAFT/ON_HOLD/COMPLETED→ARCHIVED）。
    转移表权威定义见 domain/state_machine.py（单一事实源）。
    """

    DRAFT = "DRAFT"            # 草稿：项目已创建，未开始正式生产
    ACTIVE = "ACTIVE"          # 进行中：进入生产流程
    ON_HOLD = "ON_HOLD"        # 暂停
    COMPLETED = "COMPLETED"    # 完成
    ARCHIVED = "ARCHIVED"      # 归档（终态，不可原地复活）


class ProjectTier(str, Enum):
    """项目等级（对齐 17.4 项目等级 × 镜头等级二维矩阵）。

    项目等级与镜头等级解耦（ADR-025），此处仅项目级。
    """

    S = "S"
    A = "A"
    B = "B"


class ScriptStatus(str, Enum):
    """剧本版本状态。

    对齐契约：contracts/state_machines/state_machines.yaml → objects → ScriptRevision。
    两步审批流（身份治理单元）：DRAFT→LOCK_PENDING→LOCKED，其中
    LOCK_PENDING 是「锁定申请已提交、待批准」的中间态，承载申请人→批准人
    的职责分离（申请人与批准人必须是两个不同的真实主体）。
    转移表权威定义见 domain/state_machine.py 的 SCRIPT_ALLOWED_TRANSITIONS。
    """

    DRAFT = "DRAFT"                # 草稿：导入的可编辑剧本版本（ADR-032 默认草案）
    LOCK_PENDING = "LOCK_PENDING"  # 锁定申请中：已提交锁定申请，待人工批准
    LOCKED = "LOCKED"              # 锁定：经 SCRIPT.LOCK.APPROVE 人工确认
    SUPERSEDED = "SUPERSEDED"      # 被取代：新版本导入，旧版本终态（不可原地复活）


class EpisodeStatus(str, Enum):
    """集数状态。

    对齐契约：contracts/state_machines/state_machines.yaml → objects → Episode
    （DRAFT→ACTIVE→ARCHIVED）。
    转移表权威定义见 domain/state_machine.py 的 EPISODE_ALLOWED_TRANSITIONS。
    """

    DRAFT = "DRAFT"            # 草稿：集数已登记，未进入正式生产
    ACTIVE = "ACTIVE"          # 进行中：集数进入生产流程
    ARCHIVED = "ARCHIVED"      # 归档（终态，不可原地复活）


class SceneStatus(str, Enum):
    """场次状态（ADR-R19 / A13）。

    对齐 contracts/state_machines/state_machines.yaml → objects → Scene。
    刻意与 Episode 同形：同一层级的聚合各造一套状态，等于要求实现方
    为每一层单独学习，且跨层校验无法复用同一套判定。
    不设 IN_REVIEW（审核结论归 GateInstance）、不设 LOCKED（场次无锁定对象）、
    不设生产进度态（那是 Shot 的职责）。
    """

    DRAFT = "DRAFT"            # 草稿：场次已登记，未进入正式生产
    ACTIVE = "ACTIVE"          # 进行中
    ARCHIVED = "ARCHIVED"      # 归档（终态，不可原地复活）


class ShotStatus(str, Enum):
    """镜头状态（ADR-R18 / A13）。

    对齐 contracts/state_machines/state_machines.yaml → objects → Shot。
    不设 IN_REVIEW（审核结论归 GateInstance，写进 Shot 就是第二份审核事实）；
    不设 REWORK（返工归 ReworkTask，获准返工后 Shot 回到 IN_PRODUCTION 并保留
    rework_basis_ref）；不设 DELIVERED（交付结论归 Gate 与 DeliveryPackage）。
    CANCELLED 为终态，恢复必须新建 Shot 并重新授权。
    """

    NOT_STARTED = "NOT_STARTED"                    # 未开始
    IN_PRODUCTION = "IN_PRODUCTION"                # 生产中
    BLOCKED = "BLOCKED"                            # 阻塞（必须有 blocked_reason）
    PRODUCTION_COMPLETE = "PRODUCTION_COMPLETE"    # 生产产物已完成
    CANCELLED = "CANCELLED"                        # 取消（终态，必须有授权记录）


class ShotRevisionStatus(str, Enum):
    """镜头修订状态（ADR-R17）。

    ACTIVE → SUPERSEDED：版本不可原地覆盖，新修订使旧修订成为 SUPERSEDED。
    """

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"


class ShotDraftStatus(str, Enum):
    """拆镜草案状态（ADR-R16）。

    PENDING_CONFIRM → IN_REVIEW → CONFIRMED；IN_REVIEW → RETURNED；
    PENDING_CONFIRM / IN_REVIEW → DISCARDED。
    CONFIRMED / RETURNED / DISCARDED 均为**版本终态**：修改走新 draft_version。
    """

    PENDING_CONFIRM = "PENDING_CONFIRM"
    IN_REVIEW = "IN_REVIEW"
    CONFIRMED = "CONFIRMED"
    RETURNED = "RETURNED"
    DISCARDED = "DISCARDED"


class ShotSpecStatus(str, Enum):
    """镜头规格状态（8.12 / A21 裁定 / A22 实现）。

    对齐 contracts/state_machines/state_machines.yaml → objects → ShotSpec：
    只有一条边 DRAFT → LOCKED，归 SHOTSPEC.LOCK。

    为什么只有两态：8.12 给了字段与「绑定精确修订」，没给状态集。不设
    PARTIALLY_LOCKED 是因为该状态依赖「必锁维度全集」，而契约只给
    locked_dimensions 字段、没给全集——没有全集，「部分」到哪为止不可判定。
    不设 SUPERSEDED 是因为换版发生在 ShotRevision 层：新修订配新规格，旧规格
    对其修订仍然有效。LOCKED 为终态：改规格必须新修订 + 新规格。
    """

    DRAFT = "DRAFT"      # 草稿：可编辑（创建后即此态）
    LOCKED = "LOCKED"    # 已锁定：维度/依据/审批事件齐备，此后不可原地修改


class ReworkTaskStatus(str, Enum):
    """返工任务状态（7.6 精准返工 / A13）。

    OPEN → IN_PROGRESS → DONE → VERIFYING → CLOSED；CLOSED 为终态。
    REJECTED 在枚举里合法，但当前**没有任何授权入边**（契约已登记），
    按 31.5「未登记转移一律拒绝」处理——指向它的请求必须被拒绝，
    而不是被当作「还没实现」放行。
    """

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    VERIFYING = "VERIFYING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class ConstraintPackStatus(str, Enum):
    """约束包状态（7.9 隐式连续性依赖登记 / A23 实现）。

    对齐 contracts/state_machines/state_machines.yaml → objects → ConstraintPack：
    ``DRAFT → ACTIVE``；``ACTIVE → STALE / SUPERSEDED / REVOKED``。

    五个状态全部保留，不做「只实现两态」的裁剪——契约给了五态，裁剪会让库里
    出现一个与契约不同的状态集，而闸门按契约比对，裁剪必然报红。

    但**只有 ``DRAFT → ACTIVE`` 这一条边在本批有入口**（``CONSTRAINT.PACK.LOCK``）。
    三条治理边（REVOKED / STALE / SUPERSEDED）的责任动作 ``CONSTRAINT.PACK.STATUS.CHANGE``
    已在契约登记、运行时尚未实现，逐边债务登记在
    ``contracts/runtime/state_machine_gaps.yaml``。它们是「可达但无入口」，
    不是「不可达」：区别在闸门的债务分区不同，不能混为一谈。

    ``ACTIVE`` 是唯一可作生成输入的状态（7.9 原文）。NOT_APPLICABLE 的包同样
    必须经 ACTIVE：否则「已确认无锁定约束」就退化成一个默认值，而契约要的是
    授权人的判断。
    """

    DRAFT = "DRAFT"            # 草稿：可编辑（创建后即此态）
    ACTIVE = "ACTIVE"          # 已审批生效：唯一可作生成输入的状态
    STALE = "STALE"            # 引用的 PrevizVersion 或上游锁定维度失效，须重新确认
    SUPERSEDED = "SUPERSEDED"  # 已被新版本取代
    REVOKED = "REVOKED"        # 授权被撤销，相关路线立即阻断


class ProductionRouteStatus(str, Enum):
    """生产路线选择记录的生命周期（R16-D3）。"""

    VERIFIED = "VERIFIED"  # 选择生效：路线选择已建立（创建即此态）
    REVOKED = "REVOKED"    # 选择被撤销，终态不可原地复活


class GenerationTaskStatus(str, Enum):
    """生成任务记录的生命周期（R16-D3 / Phase 2 租约 / R42 资产登记 Saga）。"""

    REQUESTED = "REQUESTED"  # 已请求：准入校验通过（创建即此态；也是租约到期退回态）
    RUNNING = "RUNNING"      # 执行中：由 GENERATION.PROCESS 的租约持有者推进
    AWAITING_ASSET_REGISTRATION = "AWAITING_ASSET_REGISTRATION"  # T1 已确认存储并发出登记请求；等待 T3 回执
    SUCCEEDED = "SUCCEEDED"  # 登记成功回执驱动的终态（T3）
    FAILED = "FAILED"        # 执行或登记失败的终态


class QCTaskStatus(str, Enum):
    """QC 待检任务的生命周期（Phase 3，生成终态入队）。"""

    QUEUED = "QUEUED"        # 待检：已入队，等待 QC Worker 领取
    RUNNING = "RUNNING"      # 检测中：由 QC.TASK.PROCESS 的租约持有者推进
    PASSED = "PASSED"        # 检测通过（终态）
    REJECTED = "REJECTED"    # 检测不通过（终态）


class ShotBudgetRequestStatus(str, Enum):
    """单镜头预算申请的生命周期（R16-D3，两步审批）。"""

    DRAFT = "DRAFT"          # 草稿：申请人已提交，待审批（创建即此态）
    APPROVED = "APPROVED"    # 已批准：同一事务原子记账（终态）
    REJECTED = "REJECTED"    # 已拒绝（终态）


class NoLifecycleStatus(str, Enum):
    """信封占位哨兵（status_layers.yaml envelope_only_sentinel）。

    用于没有领域生命周期的追加式/不可变记录（如 BudgetLedger / GateDecision），
    status 唯一允许写入 NO_LIFECYCLE，权威事实在别的字段上。
    """

    NO_LIFECYCLE = "NO_LIFECYCLE"
