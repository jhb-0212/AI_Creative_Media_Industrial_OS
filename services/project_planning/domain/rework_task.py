"""ReworkTask 聚合根（精准返工任务）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#ReworkTask。
- contracts/state_machines/state_machines.yaml → objects → ReworkTask：
  OPEN→IN_PROGRESS→DONE→VERIFYING→CLOSED；CLOSED 为终态。
- 7.6 精准返工：VERIFYING 须有复核人且不得为返工执行人本人；REJECTED 合法但
  当前无任何授权入边，按 31.5 未登记转移一律拒绝。

归属 project_planning 的理由（ADR-R40 §3.3）：Shot.rework_basis_ref 的守卫需要
「引用的返工任务真实存在且状态合法」。若本对象在资产域，这条校验就只能跨服务
或退化为字符串非空——守卫名存实亡。同域后它是一次同 schema 查询，外加库层外键。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.separation import require_different_subject
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import ReworkTaskStatus
from services.project_planning.domain.state_machine import REWORK_TASK_LIFECYCLE


@dataclass
class ReworkTask(Entity):
    """返工任务（一次「因缺陷回到上游」的授权依据）。"""

    ENTITY_TYPE = "ReworkTask"

    owner_module: str = OWNER_MODULE

    rework_task_id: str = ""
    project_id: str = ""
    issue_id: str = ""
    target_ref: str = ""
    defect_category: str = ""
    severity: str = ""
    root_cause: str = ""
    owner: str = ""
    return_node: str = ""
    affected_asset_version_ids: list[str] = field(default_factory=list)
    downstream_task_ids: list[str] = field(default_factory=list)
    constraint_pack_ref: str | None = None
    status: ReworkTaskStatus = ReworkTaskStatus.OPEN

    def __post_init__(self) -> None:
        super()._validate_envelope()
        if self.rework_task_id and self.rework_task_id != self.entity_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="rework_task_id 必须与 entity_id 一致",
            )
        self.rework_task_id = self.entity_id
        for name, max_len in (
            ("rework_task_id", 64),
            ("project_id", 64),
            ("issue_id", 64),
            ("target_ref", 64),
            ("defect_category", 64),
            ("severity", 32),
            ("root_cause", 2000),
            ("owner", 64),
            ("return_node", 64),
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

    def transition_to(
        self, target: ReworkTaskStatus, audit_ref: str, action: str
    ) -> ReworkTask:
        """执行状态迁移；REJECTED 无入边，任何指向它的请求在此被拒。"""
        REWORK_TASK_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def assert_reviewer_not_owner(self, reviewer: str) -> None:
        """职责分离：复核人不得是返工执行人本人（7.6 对象自身不变量）。

        判定委托给 identity 的单一权威 ``require_different_subject``——与脚本、
        资产域同一口径，避免每处各写一次字符串比较而语义漂移。owner 由可信
        Principal 派生（服务层创建时认领），因此这里的两个主体都是可信值。
        """
        require_different_subject(
            actor=reviewer,
            counterpart=self.owner,
            action_label="REWORK.TASK 复核",
            actor_label="复核人",
            counterpart_label="返工执行人",
        )

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "rework_task_id": self.rework_task_id,
                "project_id": self.project_id,
                "issue_id": self.issue_id,
                "target_ref": self.target_ref,
                "defect_category": self.defect_category,
                "severity": self.severity,
                "root_cause": self.root_cause,
                "owner": self.owner,
                "return_node": self.return_node,
                "affected_asset_version_ids": self.affected_asset_version_ids,
                "downstream_task_ids": self.downstream_task_ids,
                "constraint_pack_ref": self.constraint_pack_ref,
            }
        )
        return data
