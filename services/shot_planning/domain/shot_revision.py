"""ShotRevision 聚合根（正式镜头内容修订，领域模型，纯 Python，无框架依赖）。

对齐契约：
- object_schemas.schema.json → ShotRevision（A4 兑现 ADR-R07 债务）
- 8.1 核心实体：Shot 是稳定逻辑镜头，ShotRevision 保存每次内容修订，
  绑定镜头描述、约束、创意意图与版本
- 8.2：ShotRevision 版本递增不覆盖（append-only 事实）

边界硬约束（ADR-R27）：
- 正式镜头修订由授权人类确认（SHOT.REVISION.CONFIRM）产生，AI 不产出；
- 修订是 append-only：新修订即新行，版本递增由 revision 号派生，
  旧修订永远保留，绝不原地改写、不主动标记取代态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.shot_planning.domain.entity import OWNER_MODULE, Entity
from services.shot_planning.domain.enums import ShotRevisionStatus


@dataclass
class ShotRevision(Entity):
    """正式镜头内容修订聚合根（绑定稳定逻辑镜头 Shot）。

    无状态转移方法：append-only 事实，落库即 ACTIVE，最新版本由
    revision 号派生（见 repository.get_active_by_shot）。
    """

    ENTITY_TYPE = "ShotRevision"

    # 数据所有权：信封 owner_module 覆写为本模块。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    shot_id: str = ""
    project_id: str = ""
    episode_ref: str = ""
    scene_ref: str = ""
    intent_summary: str = ""
    shot_description: str = ""
    revision: int = 1
    status: ShotRevisionStatus = ShotRevisionStatus.ACTIVE

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        for name, max_len in (
            ("shot_id", 64),
            ("project_id", 64),
            ("episode_ref", 64),
            ("scene_ref", 64),
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )
        if not self.intent_summary.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="intent_summary 不能为空"
            )
        if not self.shot_description.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="shot_description 不能为空"
            )
        if self.revision < 1:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="revision 必须 >= 1"
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。"""
        data = self._envelope_dict()
        data.update(
            {
                "shot_id": self.shot_id,
                "project_id": self.project_id,
                "episode_ref": self.episode_ref,
                "scene_ref": self.scene_ref,
                "intent_summary": self.intent_summary,
                "shot_description": self.shot_description,
                "revision": self.revision,
            }
        )
        return data
