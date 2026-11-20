"""Scene 聚合根（场次，归属 Episode）。

对齐契约：
- contracts/state_machines/state_machines.yaml → objects → Scene：
  DRAFT→ACTIVE、DRAFT→ARCHIVED、ACTIVE→ARCHIVED。
- contracts/schemas/object_schemas.schema.json#Scene（required 含 scene_id / scene_code）。
- ADR-R19：刻意与 Episode 同形，不设 IN_REVIEW / LOCKED / 生产进度态。

设计要点：
- 纯数据 + 行为，不依赖 ORM / FastAPI。
- 未登记转移与越权动作一律拒绝（31.5 / N-07），判定在 Lifecycle.edge_owned_by。
- 终态 ARCHIVED 不可原地复活：边表里没有出边，不是靠 if 拦住。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import SceneStatus
from services.project_planning.domain.state_machine import SCENE_LIFECYCLE


@dataclass
class Scene(Entity):
    """场次聚合根（归属于 Episode）。

    ``scene_id`` 与信封 ``entity_id`` 是同一个稳定标识：契约同时要求两者，
    因此在构造时强制相等。保留两列是为了让外部引用（Shot.scene_id）不必
    依赖信封字段名，也避免两套 ID 各自漂移。
    """

    ENTITY_TYPE = "Scene"

    owner_module: str = OWNER_MODULE

    scene_id: str = ""
    scene_code: str = ""
    project_id: str = ""
    episode_id: str = ""
    status: SceneStatus = SceneStatus.DRAFT

    def __post_init__(self) -> None:
        super()._validate_envelope()
        if self.scene_id and self.scene_id != self.entity_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="scene_id 必须与 entity_id 一致（同一稳定标识不得有两个值）",
            )
        self.scene_id = self.entity_id
        for name, max_len in (
            ("scene_id", 64),
            ("scene_code", 64),
            ("project_id", 64),
            ("episode_id", 64),
        ):
            value = getattr(self, name)
            if not value or not str(value).strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def transition_to(self, target: SceneStatus, audit_ref: str, action: str) -> Scene:
        """执行状态迁移：结构合法性 + 边所有权。"""
        SCENE_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "scene_id": self.scene_id,
                "scene_code": self.scene_code,
                "project_id": self.project_id,
                "episode_id": self.episode_id,
            }
        )
        return data
