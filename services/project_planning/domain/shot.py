"""Shot 聚合根（领域模型，纯 Python，无框架依赖）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#Shot（五态、守卫字段）；
- contracts/state_machines/state_machines.yaml#Shot（10 边、6 守卫）；
- ADR-R18（Shot 独立建模）、ADR-R27（运行时入口必须 CONFIRM + STATUS.CHANGE 成对）。

设计要点：
- **单一身份**：``shot_id`` 是 ``entity_id`` 的只读投影（数据库有
  ``ck_shots_single_identity``），不是第二份字段。
- **守卫字段强制**：进入 BLOCKED 必带 ``blocked_reason``、进入 CANCELLED 必带
  ``cancellation_ref`` + ``cancelled_at``、返工回退必带 ``rework_basis_ref``，
  全部在 ``transition_to`` 按边校验，DB CHECK 再兜一次。守卫值由服务层传入，
  领域层只认「非空即满足结构」，不假装能解析授权记录（那部分 fail-closed
  在服务层做：cancellation_ref 指向的授权记录连契约都没有）。
- 终态 CANCELLED 无出边，不可原地复活（恢复须新建 Shot）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import ShotStatus
from services.project_planning.domain.state_machine import (
    SHOT_LIFECYCLE,
    SHOT_REVISION_CONFIRM_ACTION,
)


@dataclass
class Shot(Entity):
    """镜头聚合根（稳定逻辑镜头，归属于 Scene，作用域收敛到 Project）。"""

    ENTITY_TYPE = "Shot"
    owner_module: str = OWNER_MODULE

    # 业务字段
    shot_code: str = ""
    scene_id: str = ""
    project_id: str = ""
    shot_revision_id: str = ""
    status: ShotStatus = ShotStatus.NOT_STARTED
    # 守卫字段
    blocked_reason: str | None = None
    rework_basis_ref: str | None = None
    cancellation_ref: str | None = None
    cancelled_at: datetime | None = None
    source_draft_ref: dict[str, Any] | None = None
    # 组织归属键（内部，不进 API 响应；由服务层从父项目拷贝）
    organization_id: UUID | None = None

    @property
    def shot_id(self) -> str:
        """镜头稳定主键（8.2：不得复用），与 entity_id 同值。"""
        return self.entity_id

    def __post_init__(self) -> None:
        super()._validate_envelope()
        for name, max_len in (
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
            if len(str(value)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def apply_status_change(
        self,
        target: ShotStatus,
        audit_ref: str,
        action: str,
        *,
        blocked_reason: str | None = None,
        rework_basis_ref: str | None = None,
        cancellation_ref: str | None = None,
        cancelled_at: datetime | None = None,
    ) -> Shot:
        """给唯一推进入口用的领域委托（守卫仍在 transition_to 里）。

        服务层不得直接调 ``transition_to``：那是 ``data.transition_single_entry``
        明令禁止的旁路。把「带守卫字段的迁移」封成领域方法，服务只把它作为回调
        交给 ``advance_status``，状态、事实行与事件就能保持同一序列。
        """
        return self.transition_to(
            target,
            audit_ref,
            action,
            blocked_reason=blocked_reason,
            rework_basis_ref=rework_basis_ref,
            cancellation_ref=cancellation_ref,
            cancelled_at=cancelled_at,
        )

    def transition_to(
        self,
        target: ShotStatus,
        audit_ref: str,
        action: str,
        *,
        blocked_reason: str | None = None,
        rework_basis_ref: str | None = None,
        cancellation_ref: str | None = None,
        cancelled_at: datetime | None = None,
    ) -> Shot:
        """按边校验守卫后迁移。

        守卫值来自服务层（服务层已对 ``cancellation_ref`` 等做 fail-closed 解析）。
        这里只保证「契约声明必填的字段在迁移那一刻确实带上」，且只在该条边需要时
        才要求——不要求时若带了，会与 DB 的双向 CHECK 冲突，故同步清空。
        """
        SHOT_LIFECYCLE.edge_owned_by(self.status, target, action)

        is_blocked = target == ShotStatus.BLOCKED
        is_cancelled = target == ShotStatus.CANCELLED
        is_rework = (
            self.status == ShotStatus.PRODUCTION_COMPLETE
            and target == ShotStatus.IN_PRODUCTION
        )

        if is_blocked and not (blocked_reason and blocked_reason.strip()):
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="进入 BLOCKED 必须写明可追溯的 blocked_reason",
            )
        if is_cancelled and not (
            cancellation_ref and cancellation_ref.strip() and cancelled_at is not None
        ):
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="取消必须携带 cancellation_ref 与 cancelled_at（授权行为）",
            )
        if is_rework and not (rework_basis_ref and rework_basis_ref.strip()):
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="PRODUCTION_COMPLETE 回退必须有正式返工依据 rework_basis_ref",
            )

        # 双向一致：非目标态不带对应字段（对齐 ck_shots_blocked_reason /
        # ck_shots_cancelled）。否则库会拒绝，错误看起来像 DB 故障。
        self.blocked_reason = blocked_reason if is_blocked else None
        self.cancellation_ref = cancellation_ref if is_cancelled else None
        self.cancelled_at = cancelled_at if is_cancelled else None
        # rework_basis_ref 在返工边写入，之后保持（它是历史事实，不是瞬时守卫）；
        # CANCELLED 终态清掉，避免与取消语义冲突。
        if is_rework:
            self.rework_basis_ref = rework_basis_ref
        if is_cancelled:
            self.rework_basis_ref = None

        self.status = target
        self._advance_version(audit_ref)
        return self

    def point_to_revision(
        self, shot_revision_id: str, audit_ref: str, action: str
    ) -> Shot:
        """把当前正式版本指针前移（只能指向新确认的 ACTIVE 修订）。

        这不是状态转移，但会推进行版本：乐观锁必须能发现「两个人同时确认了
        不同修订」，否则后写会静默覆盖先写，正是 31.6 的版本冲突禁止项。
        动作归属在这里判定——``SHOT.STATUS.CHANGE`` 不得顺手改版本指针。
        """
        if action != SHOT_REVISION_CONFIRM_ACTION:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="版本指针前移只能由 SHOT.REVISION.CONFIRM 执行",
            )
        if not shot_revision_id or not shot_revision_id.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="shot_revision_id 不能为空"
            )
        if self.status is ShotStatus.CANCELLED:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="已取消镜头不得再前移版本指针",
            )
        self.shot_revision_id = shot_revision_id
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "shot_id": self.shot_id,
                "shot_code": self.shot_code,
                "scene_id": self.scene_id,
                "project_id": self.project_id,
                "shot_revision_id": self.shot_revision_id,
            }
        )
        # 守卫字段按 schema 是 ["string","null"]，始终输出（含 None）。
        data["blocked_reason"] = self.blocked_reason
        data["rework_basis_ref"] = self.rework_basis_ref
        data["cancellation_ref"] = self.cancellation_ref
        data["cancelled_at"] = self.cancelled_at.isoformat() if self.cancelled_at else None
        data["source_draft_ref"] = (
            dict(self.source_draft_ref) if self.source_draft_ref is not None else None
        )
        return data
