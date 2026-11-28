"""ShotRevision 聚合根（镜头内容修订，不可原地覆盖）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#ShotRevision。
- contracts/state_machines/state_machines.yaml → objects → ShotRevision：
  ACTIVE → SUPERSEDED（唯一一条边，归 SHOT.REVISION.CONFIRM）。
- ADR-R17：SHOT.REVISION.CONFIRM 是唯一创建入口；新修订使旧修订 SUPERSEDED。

设计要点：Shot 是稳定逻辑镜头，ShotRevision 保存每次内容修订。修订历史不可
物理删除，也不可原地改写——「改内容」永远是新增一条 revision_no。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import ShotRevisionStatus
from services.project_planning.domain.state_machine import SHOT_REVISION_LIFECYCLE


@dataclass
class ShotRevision(Entity):
    """镜头修订（内容载体）。"""

    ENTITY_TYPE = "ShotRevision"

    owner_module: str = OWNER_MODULE

    shot_revision_id: str = ""
    shot_id: str = ""
    project_id: str = ""
    episode_id: str = ""
    scene_id: str = ""
    revision_no: int = 0
    description: str = ""
    narrative_intent: str = ""
    character_refs: list[str] = field(default_factory=list)
    location_ref: str = ""
    prop_refs: list[str] = field(default_factory=list)
    constraint_pack_applicability: str = ""
    constraint_pack_id: str | None = None
    creative_autonomy: str = ""
    risk: dict[str, Any] = field(default_factory=dict)
    quality_profile: str = ""
    intent_ref: dict[str, Any] = field(default_factory=dict)
    basis_script_revision_id: str = ""
    script_anchors: list[Any] = field(default_factory=list)
    source_draft_ref: dict[str, Any] | None = None
    source_candidate_refs: list[Any] = field(default_factory=list)
    change_origin: str = ""
    supersedes_shot_revision_id: str | None = None
    change_summary: str | None = None
    changed_fields: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    confirmed_by: str = ""
    confirmed_at: datetime | None = None
    status: ShotRevisionStatus = ShotRevisionStatus.ACTIVE

    def __post_init__(self) -> None:
        super()._validate_envelope()
        if self.shot_revision_id and self.shot_revision_id != self.entity_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="shot_revision_id 必须与 entity_id 一致",
            )
        self.shot_revision_id = self.entity_id
        for name, max_len in (
            ("shot_revision_id", 64),
            ("shot_id", 64),
            ("project_id", 64),
            ("episode_id", 64),
            ("scene_id", 64),
            ("description", 4000),
            ("narrative_intent", 2000),
            ("location_ref", 64),
            ("constraint_pack_applicability", 64),
            ("creative_autonomy", 64),
            ("quality_profile", 64),
            ("basis_script_revision_id", 64),
            ("change_origin", 64),
            ("confirmed_by", 64),
        ):
            value = getattr(self, name)
            if not value or not str(value).strip():
                raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空")
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )
        if self.revision_no < 1:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="revision_no 从 1 开始")
        if self.confirmed_at is None:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="confirmed_at 不能为空")

    def transition_to(
        self, target: ShotRevisionStatus, audit_ref: str, action: str
    ) -> ShotRevision:
        """ACTIVE → SUPERSEDED：旧修订成为终态，不可复活。"""
        SHOT_REVISION_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "shot_revision_id": self.shot_revision_id,
                "shot_id": self.shot_id,
                "project_id": self.project_id,
                "episode_id": self.episode_id,
                "scene_id": self.scene_id,
                "revision_no": self.revision_no,
                "description": self.description,
                "narrative_intent": self.narrative_intent,
                "character_refs": self.character_refs,
                "location_ref": self.location_ref,
                "prop_refs": self.prop_refs,
                "constraint_pack_applicability": self.constraint_pack_applicability,
                "constraint_pack_id": self.constraint_pack_id,
                "creative_autonomy": self.creative_autonomy,
                "risk": self.risk,
                "quality_profile": self.quality_profile,
                "intent_ref": self.intent_ref,
                "basis_script_revision_id": self.basis_script_revision_id,
                "script_anchors": self.script_anchors,
                "source_draft_ref": self.source_draft_ref,
                "source_candidate_refs": self.source_candidate_refs,
                "change_origin": self.change_origin,
                "supersedes_shot_revision_id": self.supersedes_shot_revision_id,
                "change_summary": self.change_summary,
                "changed_fields": self.changed_fields,
                "provenance": self.provenance,
                "confirmed_by": self.confirmed_by,
                "confirmed_at": (self.confirmed_at.isoformat() if self.confirmed_at else None),
            }
        )
        return data
