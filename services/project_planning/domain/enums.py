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

    对齐契约：contracts/state_machines/state_machines.yaml → objects → ScriptRevision
    （DRAFT→LOCKED；DRAFT/LOCKED→SUPERSEDED）。
    转移表权威定义见 domain/script.py 的 _ALLOWED_TRANSITIONS。
    """

    DRAFT = "DRAFT"            # 草稿：导入的可编辑剧本版本（ADR-032 默认草案）
    LOCKED = "LOCKED"          # 锁定：经 SCRIPT.LOCK.APPROVE 人工确认
    SUPERSEDED = "SUPERSEDED"  # 被取代：新版本导入，旧版本终态（不可原地复活）
