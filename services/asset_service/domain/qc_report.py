"""QCReport 对象（不可变检测证据，不改变 Gate、不自动放行资产）。

契约对齐：
- contracts/schemas/object_schemas.schema.json → QCReport：
  required = qc_report_id / asset_version_id / quality_profile_version /
  detector_versions / findings；deviation_class、intent_match_status 为枚举。
- contracts/action_registry.yaml → QC.REPORT.CREATE（MEDIUM，QC Worker /
  受信检测服务，「只写检测结果与证据，不改变 Gate」）。
- 负向红线「缺证据报告不得 PASS」：findings 为空即无证据，构造阶段直接拒绝，
  不允许产生「空报告」再去推动任何状态。

与 AssetVersion 的关系：
- QC 报告是资产的证据附件，不是资产版本本身；它只让资产从 GENERATED 进入
  TECHNICAL_QC，是否 APPROVED 由 ASSET.VERSION.APPROVE 的授权主体决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import utcnow
from services.asset_service.domain.enums import DeviationClass, IntentMatchStatus


@dataclass
class QCReport:
    """一条 QC 检测报告（append-only 证据记录）。"""

    qc_report_id: str
    asset_version_id: str
    tenant_id: UUID
    quality_profile_version: str
    detector_versions: dict[str, Any]
    findings: list[Any]
    created_by: str
    benchmark_version: str | None = None
    deviation_class: DeviationClass | None = None
    intent_match_status: IntentMatchStatus | None = None
    human_decision: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        """校验证据完整性（缺证据的报告不得存在）。"""
        for name in ("qc_report_id", "asset_version_id", "created_by"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )

        if not self.quality_profile_version.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="quality_profile_version 不能为空",
            )
        if not self.detector_versions:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="detector_versions 不能为空"
            )
        if not self.findings:
            # 红线：没有 findings 就没有证据，不能用「空报告」换放行。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="findings 不能为空（缺证据的报告不得 PASS）",
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约结构（枚举转字符串值）。"""
        return {
            "qc_report_id": self.qc_report_id,
            "asset_version_id": self.asset_version_id,
            "tenant_id": str(self.tenant_id),
            "quality_profile_version": self.quality_profile_version,
            "benchmark_version": self.benchmark_version,
            "detector_versions": self.detector_versions,
            "findings": self.findings,
            "deviation_class": (
                self.deviation_class.value if self.deviation_class else None
            ),
            "intent_match_status": (
                self.intent_match_status.value if self.intent_match_status else None
            ),
            "human_decision": self.human_decision,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
