"""ShotDraft 聚合根（AI 拆镜草案，领域模型，纯 Python，无框架依赖）。

对齐契约：
- object_schemas.schema.json → ShotDraft（A4 兑现 ADR-R07 债务）
- 19.3 AI 拆镜流程：草案优先、人工确认
- 状态机 DRAFT→CONFIRMED/SUPERSEDED（见 state_machine.py）

边界硬约束（ADR-R27）：
- AI 只能产草案（source=agent），不写正式 ShotRevision；
- 草案确认仅表示「被采纳为正式 ShotRevision 的输入」，不是正式镜头；
- 终态（SUPERSEDED）不可原地复活。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.shot_planning.domain.entity import OWNER_MODULE, Entity
from services.shot_planning.domain.enums import ShotDraftSource, ShotDraftStatus
from services.shot_planning.domain.state_machine import assert_transition_draft


@dataclass
class ShotDraft(Entity):
    """AI 剧本拆镜草案聚合根。

    DRAFT 是 AI 拆镜的唯一入口产物；CONFIRMED 表示被人工采纳。
    """

    ENTITY_TYPE = "ShotDraft"

    # 数据所有权：信封 owner_module 覆写为本模块。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    project_id: str = ""
    episode_ref: str = ""
    scene_ref: str = ""
    shot_code: str = ""
    intent_summary: str = ""
    shot_description: str = ""
    source: ShotDraftSource = ShotDraftSource.AGENT
    status: ShotDraftStatus = ShotDraftStatus.DRAFT

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        for name, max_len in (
            ("project_id", 64),
            ("episode_ref", 64),
            ("scene_ref", 64),
            ("shot_code", 64),
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

    def confirm(self, audit_ref: str) -> ShotDraft:
        """人工确认：DRAFT -> CONFIRMED（作为正式 ShotRevision 的输入）。"""
        assert_transition_draft(self.status, ShotDraftStatus.CONFIRMED)
        self.status = ShotDraftStatus.CONFIRMED
        self._advance_version(audit_ref)
        return self

    def supersede(self, audit_ref: str) -> ShotDraft:
        """取代：DRAFT/CONFIRMED -> SUPERSEDED（终态，不可原地复活）。"""
        assert_transition_draft(self.status, ShotDraftStatus.SUPERSEDED)
        self.status = ShotDraftStatus.SUPERSEDED
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。"""
        data = self._envelope_dict()
        data.update(
            {
                "project_id": self.project_id,
                "episode_ref": self.episode_ref,
                "scene_ref": self.scene_ref,
                "shot_code": self.shot_code,
                "intent_summary": self.intent_summary,
                "shot_description": self.shot_description,
                "source": self.source.value,
            }
        )
        return data
