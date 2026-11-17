"""ShotVisualPlan 聚合根（镜头视觉方案，领域模型，纯 Python，无框架依赖）。

对齐契约：
- object_schemas.schema.json → ShotVisualPlan（C.3 新增领域对象）
- 19.2 ShotVisualPlan：专业参数与创意自由边界——锁定字段与开放字段分离；
  未锁定字段是建议或探索空间；锁定字段必须带锁定人、时间、理由、适用版本
  和变更审批策略；ShotVisualPlan 不自动等于 ShotSpec，只有涉及可执行的
  相机/走位/时长/画幅等约束时，才经确认映射到 ShotSpec/ConstraintPack
- C.7：变更创建新 revision；锁定字段逐字段授权

边界硬约束（ADR-R27 / C2）：
- 锁定只发生在 APPROVED/PARTIALLY_LOCKED 态（逐字段授权）；
- AI 不得静默修改锁定字段（19.2）：锁定条目必须携带锁定人/时间/理由/版本；
- 终态（REJECTED / LOCKED）不可原地复活；变更须新建 revision。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.shot_planning.domain.entity import OWNER_MODULE, Entity, utcnow
from services.shot_planning.domain.enums import ShotVisualPlanStatus
from services.shot_planning.domain.state_machine import assert_transition_visual_plan

#: locked_fields 条目的必填键（19.2：锁定人、时间、理由、适用版本）。
_LOCK_ENTRY_REQUIRED_KEYS = frozenset({"field", "locked_by", "locked_at", "reason", "version"})


@dataclass
class ShotVisualPlan(Entity):
    """镜头视觉方案聚合根（结构化表达导演意图 + 创意自由区分离）。"""

    ENTITY_TYPE = "ShotVisualPlan"

    # 数据所有权：信封 owner_module 覆写为本模块。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    shot_id: str = ""
    project_id: str = ""
    intent_summary: str = ""
    camera_language: str = ""
    shot_size: str = ""
    lens_range: str = ""
    motion_intent: str = ""
    lighting_intent: str = ""
    locked_fields: list[dict[str, Any]] = field(default_factory=list)
    open_fields: list[str] = field(default_factory=list)
    revision: int = 1
    status: ShotVisualPlanStatus = ShotVisualPlanStatus.DRAFT

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        for name, max_len in (
            ("shot_id", 64),
            ("project_id", 64),
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
        for name in (
            "intent_summary",
            "camera_language",
            "shot_size",
            "lens_range",
            "motion_intent",
            "lighting_intent",
        ):
            if not getattr(self, name).strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
        if not self.open_fields:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="open_fields 不能为空（锁定与开放字段分离，19.2）",
            )
        if self.revision < 1:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="revision 必须 >= 1"
            )
        for entry in self.locked_fields:
            missing = _LOCK_ENTRY_REQUIRED_KEYS - set(entry)
            if missing:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"locked_fields 条目缺必填键: {sorted(missing)}（19.2）",
                )
        locked_names = {e["field"] for e in self.locked_fields}
        overlap = locked_names & set(self.open_fields)
        if overlap:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"字段不能同时处于锁定与开放集合: {sorted(overlap)}",
            )

    def submit(self, audit_ref: str) -> ShotVisualPlan:
        """送审：DRAFT -> REVIEW。"""
        assert_transition_visual_plan(self.status, ShotVisualPlanStatus.REVIEW)
        self.status = ShotVisualPlanStatus.REVIEW
        self._advance_version(audit_ref)
        return self

    def approve(self, audit_ref: str) -> ShotVisualPlan:
        """审阅通过：REVIEW -> APPROVED（可逐字段授权锁定）。"""
        assert_transition_visual_plan(self.status, ShotVisualPlanStatus.APPROVED)
        self.status = ShotVisualPlanStatus.APPROVED
        self._advance_version(audit_ref)
        return self

    def reject(self, audit_ref: str) -> ShotVisualPlan:
        """审阅拒绝：REVIEW -> REJECTED（终态；变更须新建 revision）。"""
        assert_transition_visual_plan(self.status, ShotVisualPlanStatus.REJECTED)
        self.status = ShotVisualPlanStatus.REJECTED
        self._advance_version(audit_ref)
        return self

    def lock_fields(
        self,
        *,
        field_names: list[str],
        locked_by: str,
        reason: str,
        audit_ref: str,
    ) -> ShotVisualPlan:
        """逐字段授权锁定（19.2：锁定字段带锁定人/时间/理由/版本）。

        锁定只发生在 APPROVED / PARTIALLY_LOCKED 态；锁定后若仍有开放字段
        则转 PARTIALLY_LOCKED，全部锁定则转 LOCKED（终态）。
        """
        if self.status not in (
            ShotVisualPlanStatus.APPROVED,
            ShotVisualPlanStatus.PARTIALLY_LOCKED,
        ):
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"锁定字段仅允许在 APPROVED/PARTIALLY_LOCKED 态执行，"
                    f"当前为 {self.status.value}"
                ),
            )
        if not field_names:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="待锁定字段不能为空"
            )
        if not reason.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="锁定理由不能为空"
            )
        open_set = set(self.open_fields)
        unknown = [f for f in field_names if f not in open_set]
        if unknown:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"待锁定字段不在开放字段集合内: {unknown}",
            )
        for name in field_names:
            self.locked_fields.append(
                {
                    "field": name,
                    "locked_by": locked_by,
                    "locked_at": utcnow().isoformat(),
                    "reason": reason.strip(),
                    "version": self.entity_version,
                }
            )
        self.open_fields = sorted(open_set - set(field_names))
        target = (
            ShotVisualPlanStatus.LOCKED
            if not self.open_fields
            else ShotVisualPlanStatus.PARTIALLY_LOCKED
        )
        assert_transition_visual_plan(self.status, target)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。"""
        data = self._envelope_dict()
        data.update(
            {
                "shot_id": self.shot_id,
                "project_id": self.project_id,
                "intent_summary": self.intent_summary,
                "camera_language": self.camera_language,
                "shot_size": self.shot_size,
                "lens_range": self.lens_range,
                "motion_intent": self.motion_intent,
                "lighting_intent": self.lighting_intent,
                "locked_fields": self.locked_fields,
                "open_fields": self.open_fields,
                "revision": self.revision,
            }
        )
        return data
