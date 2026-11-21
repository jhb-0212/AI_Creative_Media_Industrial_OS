"""ShotRevision 聚合根（不可变内容版本，纯 Python）。

对齐契约：contracts/schemas/object_schemas.schema.json#ShotRevision。
本对象不是「可编辑的镜头详情」：内容一旦由 SHOT.REVISION.CONFIRM 确认，
业务字段永不原地修改；唯一允许的状态变化是 ACTIVE→SUPERSEDED，且只由同一个
CONFIRM 动作在确认下一版时执行（ADR-R16/R17/R18）。

为什么实体里保留完整契约字段
----------------------------
批次十之前 shot_revisions 只有 21 列，服务层即便写了成功也无法落契约事实。
本轮表已对齐，聚合根按契约 required 建模，避免「服务对象比数据库窄」造成的
假成功。字段级校验在此做一次，数据库 CHECK/外键再做一次，二者不是互相替代：
领域层给出可诊断的 4xx，数据库层拦住绕过服务的写入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import ShotRevisionStatus
from services.project_planning.domain.state_machine import (
    SHOT_REVISION_CONFIRM_ACTION,
    SHOTREVISION_LIFECYCLE,
)

_APPLICABILITY = frozenset({"BOUND", "NOT_APPLICABLE"})
_CREATIVE_AUTONOMY = frozenset({"L1", "L2", "L3", "L4"})
_RISK_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
_CHANGE_ORIGINS = frozenset(
    {"BREAKDOWN_CONFIRM", "PROMOTION", "MANUAL_EDIT", "BRANCH_MERGE"}
)


def _nonempty(value: object, name: str, *, max_len: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
        )
    if max_len is not None and len(value) > max_len:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=f"{name} 长度超过上限 {max_len}",
        )
    return value


def _as_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 必须是对象"
        )
    return value


def _as_string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 必须是字符串数组"
        )
    return value


@dataclass
class ShotRevision(Entity):
    """一次被人工确认的镜头内容修订（不可变版本快照）。"""

    ENTITY_TYPE = "ShotRevision"
    owner_module: str = OWNER_MODULE

    shot_id: str = ""
    project_id: str = ""
    episode_id: str = ""
    scene_id: str = ""
    # 批次五保留的草案期软指针，数据库仍为 NOT NULL；不是契约字段，不进响应。
    # 服务层从父 Episode / Scene 的业务编码填入，避免编造来源。
    episode_ref: str = ""
    scene_ref: str = ""
    revision_no: int = 1
    description: str = ""
    narrative_intent: str = ""
    character_refs: list[str] = field(default_factory=list)
    location_ref: str = ""
    prop_refs: list[str] = field(default_factory=list)
    constraint_pack_applicability: str = "NOT_APPLICABLE"
    constraint_pack_id: str | None = None
    creative_autonomy: str = "L1"
    risk: dict[str, Any] = field(default_factory=lambda: {"level": "LOW"})
    quality_profile: str = "UNIFIED_HIGH_QUALITY"
    intent_ref: dict[str, Any] = field(default_factory=dict)
    basis_script_revision_id: str = ""
    script_anchors: list[str] = field(default_factory=list)
    source_draft_ref: dict[str, Any] | None = None
    source_candidate_refs: list[str] = field(default_factory=list)
    change_origin: str = "BREAKDOWN_CONFIRM"
    supersedes_shot_revision_id: str | None = None
    change_summary: str | None = None
    changed_fields: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    confirmed_by: str = ""
    confirmed_at: datetime | None = None
    status: ShotRevisionStatus = ShotRevisionStatus.ACTIVE
    organization_id: UUID | None = None

    @property
    def shot_revision_id(self) -> str:
        """修订身份是 entity_id 的只读投影，不允许两套标识各自生成。"""
        return self.entity_id

    def __post_init__(self) -> None:
        super()._validate_envelope()
        for name in (
            "shot_id",
            "project_id",
            "episode_id",
            "scene_id",
            "episode_ref",
            "scene_ref",
        ):
            _nonempty(getattr(self, name), name, max_len=64)
        _nonempty(self.description, "description")
        _nonempty(self.narrative_intent, "narrative_intent")
        _nonempty(self.location_ref, "location_ref", max_len=256)
        _nonempty(self.basis_script_revision_id, "basis_script_revision_id", max_len=64)
        self.character_refs = _as_string_list(self.character_refs, "character_refs")
        self.prop_refs = _as_string_list(self.prop_refs, "prop_refs")
        self.source_candidate_refs = _as_string_list(
            self.source_candidate_refs, "source_candidate_refs"
        )
        self.changed_fields = _as_string_list(self.changed_fields, "changed_fields")
        self.script_anchors = _as_string_list(self.script_anchors, "script_anchors")
        if not self.script_anchors:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="script_anchors 至少一条：无来源的镜头就是凭空造剧情",
            )

        if self.constraint_pack_applicability not in _APPLICABILITY:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"constraint_pack_applicability 非法: {self.constraint_pack_applicability}",
            )
        if self.creative_autonomy not in _CREATIVE_AUTONOMY:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"creative_autonomy 非法: {self.creative_autonomy}",
            )
        if self.quality_profile != "UNIFIED_HIGH_QUALITY":
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="quality_profile 只能是 UNIFIED_HIGH_QUALITY",
            )
        if self.change_origin not in _CHANGE_ORIGINS:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"change_origin 非法: {self.change_origin}",
            )

        risk = _as_object(self.risk, "risk")
        if str(risk.get("level", "")) not in _RISK_LEVELS:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="risk.level 非法或缺失"
            )
        intent_ref = _as_object(self.intent_ref, "intent_ref")
        if not intent_ref.get("intent_id") or not intent_ref.get("intent_version"):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="intent_ref 必须固定 intent_id + intent_version",
            )
        self.provenance = _as_object(self.provenance, "provenance")

        if self.revision_no < 1:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="revision_no 必须 >= 1"
            )
        if self.revision_no == 1 and self.supersedes_shot_revision_id is not None:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="首版修订不得有前驱"
            )
        if self.revision_no >= 2 and not (
            self.supersedes_shot_revision_id and self.change_summary
        ):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="非首版修订必须携带 supersedes_shot_revision_id 与 change_summary",
            )
        if self.constraint_pack_applicability == "BOUND":
            _nonempty(self.constraint_pack_id, "constraint_pack_id", max_len=64)
        elif self.constraint_pack_id is not None:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="NOT_APPLICABLE 时 constraint_pack_id 必须为空",
            )
        if self.change_origin == "BREAKDOWN_CONFIRM" and not self.source_draft_ref:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="BREAKDOWN_CONFIRM 必须携带 source_draft_ref（确认即绑定）",
            )
        if self.source_draft_ref is not None:
            draft = _as_object(self.source_draft_ref, "source_draft_ref")
            if not draft.get("draft_id") or not draft.get("draft_version"):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="source_draft_ref 必须固定 draft_id + draft_version",
                )
        _nonempty(self.confirmed_by, "confirmed_by", max_len=64)
        if self.confirmed_at is None:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="confirmed_at 不能为空"
            )

    def transition_to(
        self,
        target: ShotRevisionStatus,
        audit_ref: str,
        action: str = SHOT_REVISION_CONFIRM_ACTION,
    ) -> ShotRevision:
        """唯一允许的迁移：ACTIVE→SUPERSEDED，且归 CONFIRM 动作所有。"""
        SHOTREVISION_LIFECYCLE.edge_owned_by(self.status, target, action)
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
                "character_refs": list(self.character_refs),
                "location_ref": self.location_ref,
                "prop_refs": list(self.prop_refs),
                "constraint_pack_applicability": self.constraint_pack_applicability,
                "constraint_pack_id": self.constraint_pack_id,
                "creative_autonomy": self.creative_autonomy,
                "risk": dict(self.risk),
                "quality_profile": self.quality_profile,
                "intent_ref": dict(self.intent_ref),
                "basis_script_revision_id": self.basis_script_revision_id,
                "script_anchors": list(self.script_anchors),
                "source_draft_ref": dict(self.source_draft_ref)
                if self.source_draft_ref is not None
                else None,
                "source_candidate_refs": list(self.source_candidate_refs),
                "change_origin": self.change_origin,
                "supersedes_shot_revision_id": self.supersedes_shot_revision_id,
                "change_summary": self.change_summary,
                "changed_fields": list(self.changed_fields),
                "provenance": dict(self.provenance),
                "confirmed_by": self.confirmed_by,
                "confirmed_at": self.confirmed_at.isoformat()
                if self.confirmed_at is not None
                else None,
            }
        )
        return data
