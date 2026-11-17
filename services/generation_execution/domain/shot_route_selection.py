"""ShotRouteSelection 聚合根（镜头生效路线，领域模型，纯 Python）。

对齐契约：
- object_schemas.schema.json → ShotRouteSelection（V205-R14-A9 追认；基线
  31.2 Phase 1 核心生产主链「单一路线生成」）；
- state_machines.yaml → 单值状态集 ACTIVE（无转移边）。

它回答的问题只有一个：**这个镜头现在的生成路线是哪条**。
不是"哪条路线曾经被选过"（那是审计事件 PRODUCTION_ROUTE_SELECTED 的事），
也不是"路线目录里有哪些路线"（那是 ProductionRoute 的事）。三者分别承载
「当前事实」「历史事实」「能力事实」，混用其中任意两个都会让"单一路线"
退化成应用层约定。

边界硬约束（三条，缺一条 C4 就不成立）：
1. **唯一性**：同一 (tenant, project, shot) 只能有一条生效路线——由数据库
   唯一约束强制，不是靠"选路前先查一下"（查一下在并发下必输）；
2. **不可改写**：生效路线是受理事实，登记后字段与状态一律不可变（数据库
   守卫触发器拦截 UPDATE/DELETE）。允许原地改写等于允许绕过"第二条路线
   必须被拒绝"这条规则；
3. **无转移**：没有"从 ACTIVE 到别的状态"的合法路径，因为动作矩阵里没有
   任何能表达"换路线"的 action；造一条不可达的边比没有边更危险。

变更路线的治理流程（带 STALE 传播与审批）落地时，本对象的处置方式必须显式
重定（扩状态集或新增强制新对象），不得在既有记录上原地改写。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.generation_execution.domain.entity import OWNER_MODULE, Entity
from services.generation_execution.domain.enums import ShotRouteSelectionStatus

#: 业务字段长度上限（对齐 ORM 列定义，避免"领域放行、数据库截断/报错"两种口径）。
_MAX_LEN: dict[str, int] = {
    "project_id": 64,
    "shot_id": 64,
    "route_id": 64,
}


@dataclass
class ShotRouteSelection(Entity):
    """镜头生效路线（受理事实，登记后不可改写）。"""

    ENTITY_TYPE = "ShotRouteSelection"

    # 数据所有权：信封 owner_module 覆写为本模块。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    project_id: str = ""
    shot_id: str = ""
    route_id: str = ""
    status: ShotRouteSelectionStatus = ShotRouteSelectionStatus.ACTIVE

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        for name, max_len in _MAX_LEN.items():
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )
        if self.status is not ShotRouteSelectionStatus.ACTIVE:
            # 单值状态集：任何其它取值都不是契约登记的状态（读到即非法数据）。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"ShotRouteSelection.status 只能是 ACTIVE，收到 {self.status!r}"
                    "（单值状态集，无转移，V205-R14-A9）"
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构（selection_id 是主标识字段）。"""
        data = self._envelope_dict()
        data.update(
            {
                "selection_id": self.entity_id,
                "project_id": self.project_id,
                "shot_id": self.shot_id,
                "route_id": self.route_id,
            }
        )
        return data
