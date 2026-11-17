"""ProductionRoute 聚合根（生产路线适配器目录条目，领域模型，纯 Python）。

对齐契约：
- object_schemas.schema.json → ProductionRoute（V205-R14-A6 追认；基线
  9.2.2 路线适配器抽象）；
- 路线是**平台能力目录条目**，不是项目资源：字段集里没有 project_id
  （A6 冻结定义如此），RLS 走租户隔离，选路时按动作 scope 绑定项目上下文；
- 9.2.2：若 ShotSpec/导演锁定要求相机、走位或节奏控制，路由器必须筛选
  覆盖所需控制维度的候选路线——supported_control_signals 是选路依据之一；
- 16.2：VERIFIED 必须有验证证据背书（verified_by/verified_at 记录在
  迁移列与审计引用，不出现在契约响应里，与 ShotSpec 锁定元数据同范式）。

边界硬约束：
- UNVERIFIED 路线不得被 PRODUCTION.ROUTE.SELECT 选中（ACT-ROUTE-NEG-001），
  由 RouteService 在选路入口判定；数据库层转移守卫保证状态只能沿登记边推进。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.generation_execution.domain.entity import OWNER_MODULE, Entity
from services.generation_execution.domain.enums import ProductionRouteStatus

#: 路线类型（9.2.2 路线适配器抽象的实现形态）。
ROUTE_TYPES: frozenset[str] = frozenset(
    {"blender_scripted", "threejs", "game_engine", "external_dcc"}
)


@dataclass
class ProductionRoute(Entity):
    """生产路线聚合根（可校验的路线适配器元数据条目）。"""

    ENTITY_TYPE = "ProductionRoute"

    # 数据所有权：信封 owner_module 覆写为本模块。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    route_type: str = ""
    supported_control_signals: list[str] = field(default_factory=list)
    required_appearance_refs: list[str] = field(default_factory=list)
    qc_dimensions: list[str] = field(default_factory=list)
    capability_refs: list[str] = field(default_factory=list)
    status: ProductionRouteStatus = ProductionRouteStatus.UNVERIFIED

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        if not self.route_type.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="route_type 不能为空"
            )
        if self.route_type not in ROUTE_TYPES:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"route_type 未登记: {self.route_type!r}（9.2.2 路线形态）",
            )
        if not self.qc_dimensions:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="qc_dimensions 不能为空（路线的 QC 覆盖维度是选路与 Gate 依据）",
            )
        if self.status is ProductionRouteStatus.VERIFIED and not self.capability_refs:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="VERIFIED 路线必须引用能力证据（capability_refs，16.2）",
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构（route_id 是主标识字段）。"""
        data = self._envelope_dict()
        data.update(
            {
                "route_id": self.entity_id,
                "route_type": self.route_type,
                "supported_control_signals": self.supported_control_signals,
                "required_appearance_refs": self.required_appearance_refs,
                "qc_dimensions": self.qc_dimensions,
                "capability_refs": self.capability_refs,
            }
        )
        return data
