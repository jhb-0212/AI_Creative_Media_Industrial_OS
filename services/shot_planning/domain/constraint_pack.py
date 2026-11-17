"""ConstraintPack 聚合根（隐式连续性依赖约束包，领域模型，纯 Python）。

对齐契约：
- object_schemas.schema.json → ConstraintPack（7.9 字段契约）
- 7.9 权威状态机：DRAFT→ACTIVE；ACTIVE→STALE/SUPERSEDED/REVOKED；
  只有 ACTIVE 可作为生成输入；NOT_APPLICABLE 包同样须经 ACTIVE，
  以证明「无锁定约束」是授权人判断而非默认值（ADR-088）
- 版本不可变（7.9）：变更须新建版本并 supersede 旧版本，不得原地覆盖

边界硬约束（ADR-R27 / C3）：
- Agent 仅能创建 DRAFT（CONSTRAINT.PACK.CREATE）；激活（锁包）只能由
  授权人完成（CONSTRAINT.PACK.LOCK，导演/授权 TD）；
- NOT_APPLICABLE 包不得携带锁定维度与约束条目（无锁定约束语义）；
- APPLICABLE 包的约束条目必须齐备（constraint_id / target / shot_scope /
  rule / source / authority_level / valid_at / approval_ref，7.9 全必填）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.shot_planning.domain.entity import OWNER_MODULE, Entity, utcnow
from services.shot_planning.domain.enums import (
    LOCKABLE_DIMENSIONS,
    ConstraintPackApplicability,
    ConstraintPackStatus,
)
from services.shot_planning.domain.state_machine import (
    assert_transition_constraint_pack,
)

#: 约束条目必填键（7.9：条目内全部必填）。
CONSTRAINT_ENTRY_REQUIRED_KEYS = frozenset(
    {
        "constraint_id",
        "target",
        "shot_scope",
        "rule",
        "source",
        "authority_level",
        "valid_at",
        "approval_ref",
    }
)


@dataclass
class ConstraintPack(Entity):
    """约束包聚合根（跨镜头状态约束的显式登记与授权）。"""

    ENTITY_TYPE = "ConstraintPack"

    # 数据所有权：信封 owner_module 覆写为本模块。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    project_id: str = ""
    scene_id: str | None = None
    shot_ids: list[str] = field(default_factory=list)
    shot_group_ids: list[str] = field(default_factory=list)
    applicability: ConstraintPackApplicability = ConstraintPackApplicability.APPLICABLE
    locked_dimensions: list[str] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    source_previz_revision: str | None = None
    version: int = 1
    approved_by: str | None = None
    approved_at: datetime | None = None
    status: ConstraintPackStatus = ConstraintPackStatus.DRAFT

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        if not self.project_id or not self.project_id.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )
        if len(self.project_id) > 64:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 长度超过上限 64"
            )
        if not self.shot_ids:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="shot_ids 不能为空（7.9 必填）"
            )
        if self.version < 1:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="version 必须 >= 1"
            )
        for dim in self.locked_dimensions:
            if dim not in LOCKABLE_DIMENSIONS:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"locked_dimensions 含未登记维度: {dim!r}",
                )
        if self.applicability is ConstraintPackApplicability.NOT_APPLICABLE:
            # 无锁定约束语义：不得携带锁定维度与约束条目（7.9 / ADR-088）。
            if self.locked_dimensions:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="NOT_APPLICABLE 包不得携带 locked_dimensions",
                )
            if self.constraints:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="NOT_APPLICABLE 包不得携带 constraints 条目",
                )
        else:
            if not self.constraints:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="APPLICABLE 包必须携带约束条目（7.9）",
                )
            for entry in self.constraints:
                missing = CONSTRAINT_ENTRY_REQUIRED_KEYS - set(entry)
                if missing:
                    raise DomainError(
                        ErrorRegistry.VALIDATION_ERROR,
                        detail=f"约束条目缺必填键: {sorted(missing)}（7.9 条目内全部必填）",
                    )

    def activate(
        self, *, approved_by: str, audit_ref: str
    ) -> ConstraintPack:
        """授权激活（CONSTRAINT.PACK.LOCK）：DRAFT -> ACTIVE。

        approved_by 由调用方（服务层）保证为非 Agent 的授权人；审批时间
        由聚合根统一打点（7.9 approved_at 必填）。
        """
        assert_transition_constraint_pack(
            self.status, ConstraintPackStatus.ACTIVE
        )
        if not approved_by.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="approved_by 不能为空"
            )
        self.status = ConstraintPackStatus.ACTIVE
        self.approved_by = approved_by
        self.approved_at = utcnow()
        self._advance_version(audit_ref)
        return self

    def mark_stale(self, audit_ref: str) -> ConstraintPack:
        """上游锁定维度/Previz 失效：ACTIVE -> STALE（须重新确认）。"""
        assert_transition_constraint_pack(self.status, ConstraintPackStatus.STALE)
        self.status = ConstraintPackStatus.STALE
        self._advance_version(audit_ref)
        return self

    def supersede(self, audit_ref: str) -> ConstraintPack:
        """被新版本包取代：ACTIVE -> SUPERSEDED（终态，不得原地覆盖）。"""
        assert_transition_constraint_pack(
            self.status, ConstraintPackStatus.SUPERSEDED
        )
        self.status = ConstraintPackStatus.SUPERSEDED
        self._advance_version(audit_ref)
        return self

    def revoke(self, audit_ref: str) -> ConstraintPack:
        """授权撤销：ACTIVE -> REVOKED（终态，相关路线立即阻断）。"""
        assert_transition_constraint_pack(self.status, ConstraintPackStatus.REVOKED)
        self.status = ConstraintPackStatus.REVOKED
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。

        契约口径（ConstraintPack 冻结定义）：constraint_pack_id 是主标识字段；
        approved_by / approved_at 必填（未审批的 DRAFT 以空串表达「尚未发生」，
        激活后由聚合根固化为授权人/激活时间）。
        """
        data = self._envelope_dict()
        data.update(
            {
                "constraint_pack_id": self.entity_id,
                "project_id": self.project_id,
                "scene_id": self.scene_id or "",
                "shot_ids": self.shot_ids,
                "shot_group_ids": self.shot_group_ids,
                "applicability": self.applicability.value,
                "locked_dimensions": self.locked_dimensions,
                "constraints": self.constraints,
                "source_previz_revision": self.source_previz_revision or "",
                "version": self.version,
                "approved_by": self.approved_by or "",
                "approved_at": (
                    self.approved_at.isoformat() if self.approved_at else ""
                ),
            }
        )
        return data
