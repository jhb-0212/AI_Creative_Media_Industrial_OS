"""通用审批引擎的领域模型（票据是状态机，选票是不可变事实）。

三条结构性决定（ADR-R11-G）：

1. **分组不是角色清单的直接展开**。`交付负责人+内部 QC/权利职责` 中 `+` 是 AND，
   `/` 是 OR。每组持有一组可接受角色，组内任一角色可投；组间按模式汇总。
2. **事实与状态分离**。`ApprovalStep` 一经写入不可改；`ApprovalTicket` 只有状态与
   乐观锁版本可变。这样"谁投了什么"永远可追，而当前进度只需要一行投影。
3. **结论是纯函数**。`evaluate()` 不碰数据库、不碰时钟：同样的模式、分组与选票
   永远得到同样的结论，避免并发路径与快速路径各算一次然后漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.authorization import holds_any_role
from packages.identity.principal import Principal, SubjectType
from packages.shared.entity import SCHEMA_VERSION, utcnow
from services.workflow.domain.enums import ApprovalMode, TicketStatus, VoteDecision

ENTITY_FLOW = "ApprovalFlow"
ENTITY_TICKET = "ApprovalTicket"
ENTITY_STEP = "ApprovalStep"


@dataclass(frozen=True)
class ApprovalGroup:
    """会签中的一个参与分组（组内角色为 OR 语义）。"""

    name: str
    roles: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="审批分组名不能为空"
            )
        if not self.roles or any(not role.strip() for role in self.roles):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"审批分组 {self.name} 必须至少有一个角色",
            )

    def accepts(self, principal: Principal) -> bool:
        """主体是否有资格代表本组投票（角色交集；身份类型另判）。

        角色比对走 identity 包的单一入口（W3）：分组资格是 ``authorize()``
        之后的额外约束，不能在这里另起一套判定。
        """
        return holds_any_role(principal, self.roles)


@dataclass
class ApprovalFlow:
    """审批流程定义（配置事实，append-only）。"""

    entity_id: str
    action_key: str
    resource_type: str
    resource_id: str
    project_id: str
    mode: ApprovalMode
    groups: tuple[ApprovalGroup, ...]
    ruleset_version: int
    tenant_id: UUID
    organization_id: UUID
    created_by: str
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    status: str = "ACTIVE"
    entity_type: str = ENTITY_FLOW
    schema_version: int = SCHEMA_VERSION
    entity_version: int = 1
    owner_module: str = "workflow"
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("entity_id", "action_key", "resource_type", "resource_id", "project_id", "created_by"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
        if self.ruleset_version < 1:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="ruleset_version 必须 >= 1"
            )
        if not self.groups:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="审批流程必须至少有一个参与分组",
            )
        if len({group.name for group in self.groups}) != len(self.groups):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="审批分组名不得重复"
            )

    def group(self, name: str) -> ApprovalGroup | None:
        return next((group for group in self.groups if group.name == name), None)


@dataclass
class ApprovalTicket:
    """审批票据（状态机投影；选票事实在 ApprovalStep）。"""

    entity_id: str
    flow_id: str
    project_id: str
    tenant_id: UUID
    organization_id: UUID
    status: TicketStatus = TicketStatus.PENDING
    version: int = 1
    completed_at: datetime | None = None
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    entity_type: str = ENTITY_TICKET
    schema_version: int = SCHEMA_VERSION
    entity_version: int = 1
    owner_module: str = "workflow"
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.entity_id.strip() or not self.flow_id.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="票据 id 与流程 id 不能为空"
            )
        if self.version < 1:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="version 必须 >= 1"
            )
        if self.status is not TicketStatus.PENDING and self.completed_at is None:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="终态票据必须有 completed_at（结论时间不可缺失）",
            )

    def to_dict(self) -> dict[str, object]:
        """契约 ApprovalTicket 字段集（内部隔离键不外泄）。"""
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "schema_version": self.schema_version,
            "entity_version": self.entity_version,
            "status": self.status.value,
            "owner_module": self.owner_module,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "updated_at": self.updated_at.isoformat(),
            "audit_ref": self.audit_ref,
            "flow_id": self.flow_id,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "version": self.version,
        }


@dataclass
class ApprovalStep:
    """一票（append-only 事实）。"""

    entity_id: str
    ticket_id: str
    group_name: str
    voter_subject_id: str
    voter_subject_type: SubjectType
    decision: VoteDecision
    reason: str
    tenant_id: UUID
    organization_id: UUID
    project_id: str
    decided_at: datetime = field(default_factory=utcnow)
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    entity_type: str = ENTITY_STEP
    schema_version: int = SCHEMA_VERSION
    entity_version: int = 1
    status: str = "RECORDED"
    owner_module: str = "workflow"
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.ticket_id.strip() or not self.group_name.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="票据 id 与分组名不能为空"
            )
        if not self.voter_subject_id.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="投票人不能为空"
            )
        if self.voter_subject_type is SubjectType.AGENT:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="Agent 不得作为审批投票人（红线：Agent 不得自批自用）",
            )
        if self.decision is VoteDecision.REJECT and not self.reason.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="驳回必须写明理由"
            )


def evaluate(
    mode: ApprovalMode,
    groups: tuple[ApprovalGroup, ...],
    steps: tuple[ApprovalStep, ...],
) -> TicketStatus | None:
    """按模式与当前选票推导票据结论；未完成时返回 None（纯函数）。

    ALL：每个分组至少一票 APPROVE 才通过；任一分组出现 REJECT 立即驳回。
    ANY：任一分组 APPROVE 即通过；所有分组都出现 REJECT 才驳回。
    同组多人投票不叠加权重——一组一票权，组内 OR。
    """
    approved_groups = {
        step.group_name for step in steps if step.decision is VoteDecision.APPROVE
    }
    rejected_groups = {
        step.group_name for step in steps if step.decision is VoteDecision.REJECT
    }
    all_names = {group.name for group in groups}

    if mode is ApprovalMode.ALL:
        if rejected_groups:
            return TicketStatus.REJECTED
        return TicketStatus.APPROVED if approved_groups == all_names else None

    if approved_groups:
        return TicketStatus.APPROVED
    return TicketStatus.REJECTED if rejected_groups == all_names else None
