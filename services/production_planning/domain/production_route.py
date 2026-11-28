"""ProductionRoute 聚合根（生产路线选择记录，R16-D3 / 9.2.2）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#ProductionRoute；
- contracts/state_machines/state_machines.yaml → objects → ProductionRoute
  （VERIFIED → REVOKED，唯一一条边归 PRODUCTION.ROUTE.SELECT）。

边界（刻意的）：
- 这是**选择记录**，不是路线目录本身。路线能力（ImageToVideo / TextToVideo /
  PrevizToVideo / 2D Layout / Virtual Production）的注册与验证归能力目录
  （正文 §16.1），不在 MVP 六个动作内。
- 契约「路线须处于 VERIFIED」由**创建即 VERIFIED**满足：SELECT 落一条选择记录，
  出生点就是 VERIFIED。能力与权利证据由 capability_ref 承载（指向已验证的能力声明）。
- REVOKED 是终态：重新选择必须新建记录，不原地复活。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from services.project_planning.domain.enums import ProductionRouteStatus
from services.project_planning.domain.state_machine import PRODUCTION_ROUTE_LIFECYCLE

_OWNER_MODULE = "production-planning"


def _blank(value: str) -> bool:
    return value is None or not str(value).strip()


@dataclass
class ProductionRoute(Entity):
    """生产路线选择记录（VERIFIED 出生，REVOKED 终态）。"""

    ENTITY_TYPE = "ProductionRoute"

    owner_module: str = _OWNER_MODULE

    route_id: str = ""
    project_id: str = ""
    shot_id: str = ""
    task_ref: str = ""
    route_type: str = ""
    capability_ref: str = ""
    basis_ref: str = ""
    # 任务创建时从本路线冻结到 GenerationTask 的质量策略来源（C4）。空值表示
    # 该路线尚未绑定质量档案，创建准入须拒绝——这个判定在 PR-C/D 落地，本 PR
    # 只建立契约字段与运行时投影，避免两套事实。
    quality_profile_version: str = ""
    status: ProductionRouteStatus = ProductionRouteStatus.VERIFIED

    def __post_init__(self) -> None:
        super()._validate_envelope()
        self.route_id = self.route_id.strip() or self.entity_id
        for name in (
            "route_id",
            "project_id",
            "shot_id",
            "task_ref",
            "route_type",
            "capability_ref",
            "basis_ref",
        ):
            if _blank(getattr(self, name)):
                raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空")
        for name, max_len in (
            ("route_id", 64),
            ("project_id", 64),
            ("shot_id", 64),
            ("task_ref", 64),
            ("route_type", 32),
            ("capability_ref", 200),
            ("basis_ref", 200),
        ):
            if len(getattr(self, name)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def transition_to(
        self,
        target: ProductionRouteStatus,
        audit_ref: str,
        action: str,
    ) -> ProductionRoute:
        """执行治理边（VERIFIED → REVOKED），终态不可原地复活。"""
        PRODUCTION_ROUTE_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "route_id": self.route_id,
                "project_id": self.project_id,
                "shot_id": self.shot_id,
                "task_ref": self.task_ref,
                "route_type": self.route_type,
                "capability_ref": self.capability_ref,
                "basis_ref": self.basis_ref,
                "quality_profile_version": self.quality_profile_version,
            }
        )
        return data


__all__ = ["ProductionRoute"]
