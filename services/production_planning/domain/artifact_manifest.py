"""产物写入意图（ADR-R42 T0/T1）。

为什么它必须是持久化对象
------------------------
对象存储写入成功、登记事务提交前进程崩溃，会留下一个无人认领的对象。只在内存里
持有 ``locator`` 时，恢复流程除了重新调用模型（重复计费、可能换内容）别无选择。
把意图先落库，恢复流程就能按 ``PREPARED`` 记录继续，且定位符与摘要从一开始就绑定。
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from services.project_planning.domain.enums import ArtifactManifestStatus
from services.project_planning.domain.state_machine import (
    ARTIFACT_MANIFEST_LIFECYCLE,
    GENERATION_PROCESS_ACTION,
)


def _blank(value: str) -> bool:
    return value is None or not str(value).strip()


@dataclass
class ArtifactManifest(Entity):
    ENTITY_TYPE = "ArtifactManifest"
    owner_module = "production-planning"

    generation_task_id: str = ""
    project_id: str = ""
    locator: str = ""
    sha256: str = ""
    attempt_id: str = ""
    status: ArtifactManifestStatus = ArtifactManifestStatus.PREPARED

    def __post_init__(self) -> None:
        super()._validate_envelope()
        for name, max_len in (
            ("generation_task_id", 64),
            ("project_id", 64),
            ("locator", 96),
            ("sha256", 64),
            ("attempt_id", 64),
        ):
            value = getattr(self, name)
            if _blank(value):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def transition_to(
        self,
        target: ArtifactManifestStatus,
        audit_ref: str,
        action: str = GENERATION_PROCESS_ACTION,
    ) -> ArtifactManifest:
        """状态只能经此处改写：事实行与审计引用同步落库（DATA-4）。"""
        ARTIFACT_MANIFEST_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def mark_stored(self, audit_ref: str) -> ArtifactManifest:
        """T1：对象已落盘并复核哈希，意图可以进入登记阶段。"""
        return self.transition_to(ArtifactManifestStatus.STORED_VERIFIED, audit_ref)


__all__ = ["ArtifactManifest"]
