"""QCIssue 值对象（21.1 的九项绑定 + 4.3 严重级别）。

为什么缺陷是**值对象**而不是独立聚合：
- 21.1 要求"每条问题必须绑定 shot_id、asset_version、timecode/frame_range、
  QC dimension、severity、evidence、detector_version、human_decision 和
  disposition"——九项缺一，这条问题就不是"可复核的缺陷"，只是一段描述。
- 缺陷的生命周期（派生 ReworkTask、修复后复核关闭）属于返工域，尚未建模
  （ADR-R36 §3 债务 1）。在此之前给它一个独立的表与状态机，会得到一个
  "只能创建不能推进"的骨架——那比"明确地说还没做"更难维护。

为什么保留 unknown 字段（``extra``）：
检测器输出（8.9 的 confidence / evidence_refs / detector_eligibility /
category）比契约要求的九项更多。校验必填项的同时丢掉未知字段，等于让
"证据"在落库时被裁剪——上游再也拿不回自己写进去的数据。因此未知字段
原样保留，只对**必填项**与**取值枚举**做校验。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.qc_governance.domain.enums import (
    QCIssueHumanDecision,
    QCIssueSeverity,
)

#: 21.1 要求每条问题必须绑定的字段（issue_id 由 7.6 的 issue_id 引用要求带出）。
REQUIRED_STRING_FIELDS: tuple[str, ...] = (
    "issue_id",
    "shot_id",
    "timecode",
    "frame_range",
    "qc_dimension",
    "evidence",
    "disposition",
)
REQUIRED_INT_FIELDS: tuple[str, ...] = ("asset_version", "detector_version")


@dataclass(frozen=True)
class QCIssue:
    """一条结构化 QC 缺陷（不可变；结论的变更以"新裁决"承载，不改这一条）。"""

    issue_id: str
    shot_id: str
    asset_version: int
    timecode: str
    frame_range: str
    qc_dimension: str
    severity: QCIssueSeverity
    evidence: str
    detector_version: int
    human_decision: QCIssueHumanDecision
    disposition: str
    #: 检测器输出的其余字段（confidence 等），原样保存不参与校验。
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in REQUIRED_STRING_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"QCIssue.{name} 不能为空（21.1 要求每条缺陷必须绑定）",
                )
        for name in REQUIRED_INT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"QCIssue.{name} 必须是 >= 1 的整数",
                )

    @property
    def is_red_line(self) -> bool:
        """是否为红线级缺陷（4.3：CRITICAL 必须阻断，不得以任何理由豁免）。"""
        return self.severity is QCIssueSeverity.CRITICAL

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, index: int) -> QCIssue:
        """从请求载荷构造一条缺陷（必填项缺一即拒，报错带下标便于定位）。

        错误里带 ``issue_index`` 与 ``missing_fields``：批量提交时"某条缺陷缺
        evidence"必须能定位到是哪一条，否则调用方只能逐条试。
        """
        if not isinstance(raw, Mapping):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"findings[{index}] 必须是对象",
            )

        missing = [
            name
            for name in (*REQUIRED_STRING_FIELDS, *REQUIRED_INT_FIELDS, "severity",
                         "human_decision")
            if _is_missing(raw.get(name))
        ]
        if missing:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"findings[{index}] 缺必填项 {missing}：21.1 要求每条问题必须绑定"
                    "镜头、资产版本、时间码/帧区间、QC 维度、严重级别、证据、"
                    "检测器版本、人工结论与处置"
                ),
                context={"issue_index": index, "missing_fields": missing},
            )

        severity = _parse_enum(QCIssueSeverity, raw["severity"], index, "severity")
        human_decision = _parse_enum(
            QCIssueHumanDecision, raw["human_decision"], index, "human_decision"
        )
        known = set(REQUIRED_STRING_FIELDS) | set(REQUIRED_INT_FIELDS) | {
            "severity",
            "human_decision",
        }
        return cls(
            issue_id=str(raw["issue_id"]),
            shot_id=str(raw["shot_id"]),
            asset_version=raw["asset_version"],
            timecode=str(raw["timecode"]),
            frame_range=str(raw["frame_range"]),
            qc_dimension=str(raw["qc_dimension"]),
            severity=severity,
            evidence=str(raw["evidence"]),
            detector_version=raw["detector_version"],
            human_decision=human_decision,
            disposition=str(raw["disposition"]),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约结构（必填九项 + 检测器其余字段）。"""
        payload: dict[str, Any] = dict(self.extra)
        payload.update(
            {
                "issue_id": self.issue_id,
                "shot_id": self.shot_id,
                "asset_version": self.asset_version,
                "timecode": self.timecode,
                "frame_range": self.frame_range,
                "qc_dimension": self.qc_dimension,
                "severity": self.severity.value,
                "evidence": self.evidence,
                "detector_version": self.detector_version,
                "human_decision": self.human_decision.value,
                "disposition": self.disposition,
            }
        )
        return payload


def _is_missing(value: object) -> bool:
    """必填项"没填"的口径：``None``、空串与纯空白串一视同仁。

    为什么不止判 ``None`` / ``""``：``"   "`` 在 JSON 里是完全合法的字符串，
    但在 21.1 的语义下它和没填等价——放过它等于把"必须绑定证据"降级成
    "必须有一个非 null 的字符序列"，而空字符串的 evidence 是不可复核的。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _parse_enum[E: QCIssueSeverity | QCIssueHumanDecision](
    enum_cls: type[E], value: object, index: int, field_name: str
) -> E:
    """把取值解析成枚举；非法值报出"允许什么"，不静默落到默认值。"""
    try:
        return enum_cls(str(value))
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=(
                f"findings[{index}].{field_name}={value!r} 不在契约取值内："
                f"{sorted(member.value for member in enum_cls)}"
            ),
            context={"issue_index": index, "field": field_name},
        ) from exc


__all__ = ["REQUIRED_INT_FIELDS", "REQUIRED_STRING_FIELDS", "QCIssue"]
