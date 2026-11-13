"""ScriptRevision 聚合根（领域模型，纯 Python，无框架依赖）。

对齐契约：
- 31.6 Action Registry → SCRIPT.IMPORT：resource_type=ScriptRevision，
  scope=project/episode，MEDIUM 风险，Token=Y，导演/编剧/制片，
  检查项目写权限、文件安全与导入来源 → SCRIPT_IMPORTED。
- ADR-032：AI 剧本拆镜输出默认草案，人工确认前不得写入 ScriptLock。
  剧本导入产出 DRAFT 状态的可编辑版本，锁定是独立动作（SCRIPT.LOCK.APPROVE）。
- C.3.1 通用实体信封（收敛到 domain/entity.py 的 Entity 基类）。

设计要点（不屎山代码）：
- 领域模型纯数据 + 行为，不依赖 ORM / FastAPI。
- 状态变更经显式方法，未登记转移一律拒绝（31.5）。
- 终态（SUPERSEDED）不可原地复活（红线）。
- 信封公共字段/校验/序列化继承自 Entity。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.project_planning.domain.entity import Entity
from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.state_machine import assert_transition_script


@dataclass
class ScriptRevision(Entity):
    """剧本版本聚合根。

    一次导入产生一个版本记录；新版本导入使旧版本转 SUPERSEDED（终态）。
    「不可变」语义指**版本内容不可变**（已入库的版本记录不原地覆盖内容，
    状态只能沿状态机向前迁移，终态不可复活），而非 Python frozen dataclass
    的字段级不可变。
    """

    ENTITY_TYPE = "ScriptRevision"

    # 业务字段（信封字段由 Entity 基类提供）。
    project_id: str = ""
    episode_ref: str = ""
    title: str = ""
    status: ScriptStatus = ScriptStatus.DRAFT

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        for name, max_len in (
            ("project_id", 64),
            ("episode_ref", 64),
            ("title", 200),
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} 不能为空")
            if len(value) > max_len:
                raise ValueError(f"{name} 长度超过上限 {max_len}")

    def transition_to(self, target: ScriptStatus, audit_ref: str) -> ScriptRevision:
        """执行状态迁移，未登记转移拒绝（31.5）。"""
        assert_transition_script(self.status, target)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。"""
        data = self._envelope_dict()
        data.update(
            {
                "project_id": self.project_id,
                "episode_ref": self.episode_ref,
                "title": self.title,
            }
        )
        return data
