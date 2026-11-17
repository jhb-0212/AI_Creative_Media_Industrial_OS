"""ShotSpec 聚合根（镜头结构化规格，领域模型，纯 Python，无框架依赖）。

对齐契约：
- object_schemas.schema.json → ShotSpec（8.12 镜头结构化规格单一数据源）
- 8.12：必须版本化并绑定 ShotRevision；提示词、白模和控制信号均为其派生
  输出，不应分别维护冲突的镜头时长或切点
- 31.5：ShotSpec 未登记生命周期状态机——锁定语义由 locked_dimensions
  字段 + 审批事件（SHOTSPEC_LOCKED）表达，status 恒为 ACTIVE

边界硬约束（ADR-R27 / C2）：
- 绑定的 ShotRevision 必须已由人工确认产生（C1：未确认不得自动推进）；
- 锁定维度只能取 LOCKABLE_DIMENSIONS 词典内的值（7.9），锁定必须携带
  锁定人与依据（19.2：锁定字段带锁定人/时间/理由/版本）；
- 规格内容列不可原地改写（C2：历史版本不可改写）：修改 = 新建一条
  ShotSpec（版本递增），锁定是唯一允许的授权变更。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.shot_planning.domain.entity import OWNER_MODULE, Entity, utcnow
from services.shot_planning.domain.enums import LOCKABLE_DIMENSIONS, ShotSpecStatus


@dataclass
class ShotSpec(Entity):
    """镜头结构化规格聚合根（绑定正式 ShotRevision）。

    时间必须以整数帧或明确的 rational FPS 表示（8.12：不允许混用未经
    说明的秒/帧单位）——fps 以 ``num/den`` 字符串表示，时长以毫秒整数
    表示，切点/景别变化以帧号表示。
    """

    ENTITY_TYPE = "ShotSpec"

    # 数据所有权：信封 owner_module 覆写为本模块。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    shot_revision_id: str = ""
    shot_id: str = ""
    project_id: str = ""
    duration_ms: int = 0
    aspect_ratio: str = ""
    fps: str = ""
    coordinate_system: str = ""
    camera_path: dict[str, Any] = field(default_factory=dict)
    subject_paths: list[dict[str, Any]] = field(default_factory=list)
    shot_size_changes: list[dict[str, Any]] = field(default_factory=list)
    cut_points_frames: list[int] = field(default_factory=list)
    previz_ref: str | None = None
    control_signal_refs: list[str] = field(default_factory=list)
    locked_dimensions: list[str] = field(default_factory=list)
    # 锁定元数据（19.2：锁定字段必须带锁定人、时间、理由；审计走信封 audit_ref）。
    locked_by: str | None = None
    locked_at: datetime | None = None
    lock_rationale: str | None = None
    status: ShotSpecStatus = ShotSpecStatus.ACTIVE

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        for name, max_len in (
            ("shot_revision_id", 64),
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
        if self.duration_ms <= 0:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="duration_ms 必须 > 0"
            )
        for name, max_len in (
            ("aspect_ratio", 32),
            ("fps", 32),
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
        if not self.coordinate_system.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="coordinate_system 不能为空"
            )
        if not self.camera_path:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="camera_path 不能为空"
            )
        for dim in self.locked_dimensions:
            if dim not in LOCKABLE_DIMENSIONS:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"locked_dimensions 含未登记维度: {dim!r}",
                )

    def lock_dimensions(
        self,
        *,
        dimensions: list[str],
        locked_by: str,
        rationale: str,
        audit_ref: str,
    ) -> ShotSpec:
        """授权锁定维度（SHOTSPEC.LOCK）。

        硬约束：
        - 维度必须落在本轮授权词典（LOCKABLE_DIMENSIONS）内，未登记维度拒绝；
        - 锁定必须携带锁定人与依据（19.2），不得静默锁定；
        - 锁定是规格唯一允许的授权变更：内容列不动，版本推进 + 审计引用。
        """
        if not dimensions:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="locked_dimensions 不能为空"
            )
        unknown = [d for d in dimensions if d not in LOCKABLE_DIMENSIONS]
        if unknown:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"未登记锁定维度: {unknown}（7.9 维度词典之外，已拒绝）",
            )
        if not rationale.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="锁定依据（rationale）不能为空"
            )
        self.locked_dimensions = sorted(set(dimensions))
        self.locked_by = locked_by
        self.locked_at = utcnow()
        self.lock_rationale = rationale.strip()
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。

        契约口径（ShotSpec 冻结定义）：结构化字段（camera_path / subject_paths /
        shot_size_changes / cut_points_frames / locked_dimensions）与 duration_ms
        的契约类型是 string——统一以 JSON 文本序列化；previz_ref 必填（未挂
        Previz 时为空串）。
        """
        data = self._envelope_dict()
        data.update(
            {
                "shot_spec_id": self.entity_id,
                "shot_revision_id": self.shot_revision_id,
                "shot_id": self.shot_id,
                "project_id": self.project_id,
                "duration_ms": str(self.duration_ms),
                "aspect_ratio": self.aspect_ratio,
                "fps": self.fps,
                "coordinate_system": self.coordinate_system,
                "camera_path": json.dumps(self.camera_path, ensure_ascii=False),
                "subject_paths": json.dumps(self.subject_paths, ensure_ascii=False),
                "shot_size_changes": json.dumps(
                    self.shot_size_changes, ensure_ascii=False
                ),
                "cut_points_frames": json.dumps(
                    self.cut_points_frames, ensure_ascii=False
                ),
                "previz_ref": self.previz_ref or "",
                "control_signal_refs": self.control_signal_refs,
                "locked_dimensions": json.dumps(
                    self.locked_dimensions, ensure_ascii=False
                ),
            }
        )
        return data
