"""ShotBreakdownDraft 聚合根（AI 拆镜草案，人工确认前不得写正式对象）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#ShotBreakdownDraft。
- contracts/state_machines/state_machines.yaml → objects → ShotBreakdownDraft：
  5 态 5 边，三个动作分领（CONFIRM / RETURN / DISCARD）。
- ADR-R16：一版草案含多个镜头；确认产出 confirmed_shot_revision_refs（复数）。
  CONFIRMED / RETURNED / DISCARDED 都是**版本终态**，修改走新 draft_version。

设计要点：草案是「拟议」，不是正式对象。它的确认动作才是 Shot 与首个
ShotRevision 的诞生点（ADR-R18 §12），因此确认的守卫比其它转移更严：
必须同时给出确认人、确认时间与产出清单，三者缺一即拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import ShotDraftStatus
from services.project_planning.domain.state_machine import SHOT_DRAFT_LIFECYCLE


@dataclass
class ShotBreakdownDraft(Entity):
    """拆镜草案（一版草案 = 一次拆镜产出的完整快照）。"""

    ENTITY_TYPE = "ShotBreakdownDraft"

    owner_module: str = OWNER_MODULE

    shot_breakdown_draft_id: str = ""
    project_id: str = ""
    episode_ref: str | None = None
    basis_script_revision_id: str = ""
    breakdown_scope: dict[str, Any] = field(default_factory=dict)
    plan_name: str = ""
    generation_method: str = ""
    draft_version: int = 1
    root_draft_ref: str = ""
    supersedes_draft_ref: str | None = None
    change_summary: str | None = None
    ai_rationale: str | None = None
    open_questions: list[Any] = field(default_factory=list)
    potential_issues: list[Any] = field(default_factory=list)
    scene_breakdowns: list[Any] = field(default_factory=list)
    review_records: list[Any] = field(default_factory=list)
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    confirmed_shot_revision_refs: list[str] = field(default_factory=list)
    returned_by: str | None = None
    returned_at: datetime | None = None
    return_reason: str | None = None
    discarded_reason: str | None = None
    status: ShotDraftStatus = ShotDraftStatus.PENDING_CONFIRM

    def __post_init__(self) -> None:
        super()._validate_envelope()
        if self.shot_breakdown_draft_id and (self.shot_breakdown_draft_id != self.entity_id):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="shot_breakdown_draft_id 必须与 entity_id 一致",
            )
        self.shot_breakdown_draft_id = self.entity_id
        for name, max_len in (
            ("shot_breakdown_draft_id", 64),
            ("project_id", 64),
            ("basis_script_revision_id", 64),
            ("plan_name", 200),
            ("generation_method", 64),
            ("root_draft_ref", 64),
        ):
            value = getattr(self, name)
            if not value or not str(value).strip():
                raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空")
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )
        if self.draft_version < 1:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="draft_version 从 1 开始")

    def transition_to(
        self, target: ShotDraftStatus, audit_ref: str, action: str
    ) -> ShotBreakdownDraft:
        """执行状态迁移：结构 + 所有权 + 终态证据。"""
        SHOT_DRAFT_LIFECYCLE.edge_owned_by(self.status, target, action)
        self._assert_terminal_evidence(target)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def _assert_terminal_evidence(self, target: ShotDraftStatus) -> None:
        """三个终态各自必须有对应证据，否则「进了终态却说不清为什么」。"""
        if target is ShotDraftStatus.CONFIRMED:
            _require(self.confirmed_by, "确认必须给出 confirmed_by")
            if self.confirmed_at is None:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail="确认必须给出 confirmed_at"
                )
            if not self.confirmed_shot_revision_refs:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="确认必须给出 confirmed_shot_revision_refs（至少一条产出）",
                )
        elif target is ShotDraftStatus.RETURNED:
            _require(self.returned_by, "退回必须给出 returned_by")
            if self.returned_at is None:
                raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="退回必须给出 returned_at")
            _require(self.return_reason, "退回必须给出 return_reason")
        elif target is ShotDraftStatus.DISCARDED:
            _require(self.discarded_reason, "废弃必须给出 discarded_reason")

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "shot_breakdown_draft_id": self.shot_breakdown_draft_id,
                "project_id": self.project_id,
                "episode_ref": self.episode_ref,
                "basis_script_revision_id": self.basis_script_revision_id,
                "breakdown_scope": self.breakdown_scope,
                "plan_name": self.plan_name,
                "generation_method": self.generation_method,
                "draft_version": self.draft_version,
                "root_draft_ref": self.root_draft_ref,
                "supersedes_draft_ref": self.supersedes_draft_ref,
                "change_summary": self.change_summary,
                "ai_rationale": self.ai_rationale,
                "open_questions": self.open_questions,
                "potential_issues": self.potential_issues,
                "scene_breakdowns": self.scene_breakdowns,
                "review_records": self.review_records,
                "confirmed_by": self.confirmed_by,
                "confirmed_at": (self.confirmed_at.isoformat() if self.confirmed_at else None),
                "confirmed_shot_revision_refs": self.confirmed_shot_revision_refs,
                "returned_by": self.returned_by,
                "returned_at": (self.returned_at.isoformat() if self.returned_at else None),
                "return_reason": self.return_reason,
                "discarded_reason": self.discarded_reason,
            }
        )
        return data


def _require(value: object, detail: str) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=detail)
