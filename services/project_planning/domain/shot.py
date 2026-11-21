"""Shot 聚合根（镜头，归属 Scene）。

对齐契约：
- contracts/state_machines/state_machines.yaml → objects → Shot：5 态 10 边，6 条守卫。
- contracts/schemas/object_schemas.schema.json#Shot（required 含 shot_revision_id）。
- ADR-R18：Shot 是稳定逻辑镜头，内容是 ShotRevision；首次确认原子创建两者。

守卫在**领域层**求值（快速失败 + 结构化错误），数据库 CHECK 只是第二道：
两层不是重复——领域层负责「错误可读」，库层负责「绕过服务也写不进去」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import ShotStatus
from services.project_planning.domain.state_machine import SHOT_LIFECYCLE

# 守卫用到的四个证据字段（与 Shot schema 的 properties 同名）。
_EVIDENCE_FIELDS = ("blocked_reason", "cancellation_ref", "cancelled_at")


@dataclass
class Shot(Entity):
    """镜头聚合根（归属于 Scene）。"""

    ENTITY_TYPE = "Shot"

    owner_module: str = OWNER_MODULE

    shot_id: str = ""
    shot_code: str = ""
    scene_id: str = ""
    project_id: str = ""
    # 指向当前生效的 ShotRevision（ADR-R18：Shot 只前移指针，不重建）。
    shot_revision_id: str = ""
    blocked_reason: str | None = None
    rework_basis_ref: str | None = None
    cancellation_ref: str | None = None
    cancelled_at: datetime | None = None
    source_draft_ref: dict[str, Any] | None = None
    status: ShotStatus = ShotStatus.NOT_STARTED

    def __post_init__(self) -> None:
        super()._validate_envelope()
        if self.shot_id and self.shot_id != self.entity_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="shot_id 必须与 entity_id 一致（同一稳定标识不得有两个值）",
            )
        self.shot_id = self.entity_id
        for name, max_len in (
            ("shot_id", 64),
            ("shot_code", 64),
            ("scene_id", 64),
            ("project_id", 64),
            ("shot_revision_id", 64),
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
        self._assert_evidence_consistent()

    def transition_to(self, target: ShotStatus, audit_ref: str, action: str) -> Shot:
        """执行状态迁移：结构 + 所有权 + 守卫（31.5 / N-07）。"""
        SHOT_LIFECYCLE.edge_owned_by(self.status, target, action)
        self._assert_transition_guards(target)
        self.status = target
        self._advance_version(audit_ref)
        self._assert_evidence_consistent()
        return self

    def _assert_transition_guards(self, target: ShotStatus) -> None:
        """按目标状态与来源状态求值守卫。

        守卫是**准入条件**，不是事后校验：不满足时状态不变、版本不增。
        """
        if target is ShotStatus.BLOCKED:
            _require(self.blocked_reason, "进入 BLOCKED 必须给出 blocked_reason")
            _forbid(self.cancellation_ref, "进入 BLOCKED 时 cancellation_ref 必须为空")
            _forbid(self.cancelled_at, "进入 BLOCKED 时 cancelled_at 必须为空")
            return

        if target is ShotStatus.CANCELLED:
            # fail-closed（contract_amendments.yaml V205-R14-A14 残留风险登记）：
            # 可解析的授权记录对象尚未建模，cancellation_ref 是调用方自报字符串，
            # 守卫只能判非空、无法证明授权真实成立。按契约裁定，运行时显式拒绝
            # 一切带 cancellation_ref 的推进——CANCELLED 边暂不可用，好过把
            # 自我声明当成授权依据写进正式对象与审计事实。
            # 注意：这只挡「推进时写入」，__post_init__ 的证据自洽仍允许存量
            # CANCELLED 对象被加载——历史事实可读，只是不再新增。
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    "取消授权记录无法解析（可解析授权对象尚未建模），"
                    "拒绝推进至 CANCELLED"
                ),
            )

        # 返工回退是唯一允许携带 rework_basis_ref 的「非阻塞非取消」边。
        if (
            self.status is ShotStatus.PRODUCTION_COMPLETE
            and target is ShotStatus.IN_PRODUCTION
        ):
            _require(
                self.rework_basis_ref,
                "返工回退必须给出 rework_basis_ref（ReworkTask 授权依据）",
            )
            _forbid(self.blocked_reason, "返工回退时 blocked_reason 必须为空")
            _forbid(self.cancellation_ref, "返工回退时 cancellation_ref 必须为空")
            _forbid(self.cancelled_at, "返工回退时 cancelled_at 必须为空")
            return

    def _assert_evidence_consistent(self) -> None:
        """状态证据自洽（与库层 CHECK 同一条不变量，语义逐字对应）。

        BLOCKED 必须给出原因；CANCELLED 必须给出授权记录与时间；
        其余状态的这三个字段必须为空（rework_basis_ref 例外：返工后保留）。
        """
        if self.status is ShotStatus.BLOCKED:
            _require(self.blocked_reason, "BLOCKED 状态必须携带 blocked_reason")
            _forbid(self.cancellation_ref, "BLOCKED 状态 cancellation_ref 必须为空")
            _forbid(self.cancelled_at, "BLOCKED 状态 cancelled_at 必须为空")
        elif self.status is ShotStatus.CANCELLED:
            _require(self.cancellation_ref, "CANCELLED 状态必须携带 cancellation_ref")
            _require(self.cancelled_at, "CANCELLED 状态必须携带 cancelled_at")
        else:
            for name in _EVIDENCE_FIELDS:
                if getattr(self, name) is not None:
                    raise DomainError(
                        ErrorRegistry.VALIDATION_ERROR,
                        detail=f"状态 {self.status.value} 不得携带证据字段 {name}",
                    )

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "shot_id": self.shot_id,
                "shot_code": self.shot_code,
                "scene_id": self.scene_id,
                "project_id": self.project_id,
                "shot_revision_id": self.shot_revision_id,
                "blocked_reason": self.blocked_reason,
                "rework_basis_ref": self.rework_basis_ref,
                "cancellation_ref": self.cancellation_ref,
                "cancelled_at": (
                    self.cancelled_at.isoformat() if self.cancelled_at else None
                ),
                "source_draft_ref": self.source_draft_ref,
            }
        )
        return data


def _require(value: object, detail: str) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=detail)


def _forbid(value: object, detail: str) -> None:
    if value is not None:
        raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=detail)
