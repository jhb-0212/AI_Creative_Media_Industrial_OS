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


class ShotStatus(str, Enum):
    """镜头状态（五态，所有者 2026-11-18《Shot 独立建模裁定》）。

    对齐契约：contracts/state_machines/state_machines.yaml → objects → Shot。
    三个刻意的边界：不设 IN_REVIEW（审核结论归 GateInstance）、不设 REWORK
    （返工归 ReworkTask）、不设 DELIVERED（Gate 3/4 结论归 GateInstance 与交付物）。
    PRODUCTION_COMPLETE ≠ Gate 通过。CANCELLED 为终态，恢复须新建 Shot。
    转移表权威定义见 domain/state_machine.py 的 SHOT_ALLOWED_TRANSITIONS。
    """

    NOT_STARTED = "NOT_STARTED"                  # 未启动
    IN_PRODUCTION = "IN_PRODUCTION"              # 生产中
    BLOCKED = "BLOCKED"                          # 阻塞（须写 blocked_reason）
    CANCELLED = "CANCELLED"                      # 取消（终态，须授权记录 + 时间）
    PRODUCTION_COMPLETE = "PRODUCTION_COMPLETE"  # 生产完成（非 Gate 通过）


class ShotRevisionStatus(str, Enum):
    """镜头修订状态（两态，从不可变版本语义推导，ADR-R17）。

    一个修订要么当前有效（ACTIVE），要么被同一 shot_id 的更高 revision_no 取代
    （SUPERSEDED），不存在第三态。草案态属 ShotBreakdownDraft；生产进度属 Shot。
    终态 SUPERSEDED 不可原地复活。
    """

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"


class SceneStatus(str, Enum):
    """场次状态。

    对齐契约：contracts/state_machines/state_machines.yaml → objects → Scene
    （DRAFT→ACTIVE→ARCHIVED，`source_kind: derived_from_parent_lifecycle`）。

    与 Episode 形状完全一致是刻意的：两者在归属语义上没有差别（都是「上级聚合
    下的有序子集合」），各造一套状态名只会让跨层校验无法复用同一套判定。
    刻意不含 IN_REVIEW（审核结论归 GateInstance）、不含 LOCKED（锁定是剧本版本
    与资产的语义）、不含任何生产进度态（那是 Shot 的职责）。

    转移表权威定义见 domain/state_machine.py 的 SCENE_ALLOWED_TRANSITIONS。
    """

    DRAFT = "DRAFT"            # 草稿：场次已登记，尚未启用（可修改/补编号）
    ACTIVE = "ACTIVE"          # 进行中：可挂载镜头
    ARCHIVED = "ARCHIVED"      # 归档（终态，不可原地复活）
