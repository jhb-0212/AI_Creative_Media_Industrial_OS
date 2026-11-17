"""shot-planning 领域枚举。

严格对齐冻结契约：
- object_schemas.schema.json 中 ShotDraft / ShotRevision 的 x-afs-lifecycle
- 主文档 19.3 AI 拆镜流程、8.1 核心实体
"""

from __future__ import annotations

from enum import Enum


class ShotDraftStatus(str, Enum):
    """AI 拆镜草案状态。

    对齐契约：ShotDraft x-afs-lifecycle
    （DRAFT→CONFIRMED/SUPERSEDED；CONFIRMED 仅表示草案被采纳为正式
    ShotRevision 的输入，不是正式镜头；终态不可原地复活）。
    转移表权威定义见 domain/state_machine.py。
    """

    DRAFT = "DRAFT"            # 草案：AI 拆镜输出，待人工确认
    CONFIRMED = "CONFIRMED"    # 已采纳：被人工确认，作为正式 ShotRevision 输入
    SUPERSEDED = "SUPERSEDED"  # 被取代（终态，不可原地复活）


class ShotRevisionStatus(str, Enum):
    """正式镜头内容修订状态。

    对齐契约：ShotRevision 绑定稳定逻辑镜头 Shot，正式确认产生，
    版本递增不覆盖旧修订（8.2：ShotRevision 保存每次内容修订）。
    修订是 append-only 事实，无原地改写路径、无状态转移——最新版本
    由 revision 号派生，旧修订永远保留在表中。
    """

    ACTIVE = "ACTIVE"  # 落库即生效；无 SUPERSEDED 态（append-only，无需主动标记）


class ShotDraftSource(str, Enum):
    """草案来源主体类型（契约 source 字段取值）。"""

    AGENT = "agent"
    HUMAN = "human"


class ShotSpecStatus(str, Enum):
    """镜头结构化规格状态。

    对齐契约：ShotSpec（8.12）未在 enum_registry / state_machines 登记生命
    周期状态机——规格不是状态驱动对象：它的「锁定」语义由 locked_dimensions
    字段 + 审批事件（SHOTSPEC_LOCKED）表达，不由 status 表达。
    因此状态恒为 ACTIVE（与 ShotRevision 同范式：单值枚举 + 落库即生效）。
    """

    ACTIVE = "ACTIVE"


class ShotVisualPlanStatus(str, Enum):
    """镜头视觉方案状态。

    对齐契约：enum_registry.yaml → ShotVisualPlan（C.7 权威定义）：
    DRAFT→REVIEW→APPROVED；可 PARTIALLY_LOCKED/LOCKED；拒绝为 REJECTED。
    转移表权威定义见 domain/state_machine.py；锁定字段逐字段授权（19.2），
    锁定人/时间/理由/版本记录在 locked_fields 条目内。
    """

    DRAFT = "DRAFT"                    # 表达计划草案
    REVIEW = "REVIEW"                  # 送审（导演审阅中）
    APPROVED = "APPROVED"              # 审阅通过，可逐字段授权锁定
    PARTIALLY_LOCKED = "PARTIALLY_LOCKED"  # 部分字段已锁定，仍有开放字段
    LOCKED = "LOCKED"                  # 全部字段锁定（终态）
    REJECTED = "REJECTED"              # 审阅拒绝（终态；变更须新建 revision）


class ConstraintPackStatus(str, Enum):
    """约束包状态。

    对齐契约：enum_registry.yaml → ConstraintPack（7.9 权威定义）：
    DRAFT→ACTIVE；ACTIVE→STALE/SUPERSEDED/REVOKED。只有 ACTIVE 可作为
    生成输入；NOT_APPLICABLE 包同样须经 ACTIVE（7.9/ADR-088）。
    转移表权威定义见 domain/state_machine.py。
    """

    DRAFT = "DRAFT"          # 约束包草案（Agent 仅能创建 DRAFT）
    ACTIVE = "ACTIVE"        # 经授权人激活，可作为生成输入
    STALE = "STALE"          # 引用的上游锁定维度/Previz 已失效，须重新确认
    SUPERSEDED = "SUPERSEDED"  # 已被更高版本包取代（版本递增不覆盖）
    REVOKED = "REVOKED"      # 授权被撤销，相关路线立即阻断（终态）


class ConstraintPackApplicability(str, Enum):
    """约束包适用性（7.9 字段契约：APPLICABLE / NOT_APPLICABLE）。

    无锁定约束的镜头必须显式标 NOT_APPLICABLE，不得留空；标 NOT_APPLICABLE
    的包同样须经 ACTIVE 才能证明「无锁定约束」是授权人判断而非默认值。
    """

    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


#: 已授权锁定维度全集（7.9 ConstraintPack.locked_dimensions 字段契约）。
#: ShotSpec 锁定与 ConstraintPack 锁定共用同一维度词典，防止两侧各写一份漂移。
LOCKABLE_DIMENSIONS: frozenset[str] = frozenset(
    {
        "camera_intrinsics",
        "camera_extrinsics",
        "camera_path",
        "blocking",
        "shot_size_nodes",
        "cut_points",
        "spatial_relations",
    }
)
