"""ShotRouteSelection 聚合根单测（纯领域层，无 IO）。

这个对象很小，但它挡住的是 Phase 1 验收里唯一以并发为口径的一条（C4）。
小对象最容易在"看起来没什么可测"的地方失守，所以这里把三件事钉住：

1. **字段校验**：project/shot/route 缺一不可，长度上限与 ORM 列一致；
2. **单值状态**：除 ACTIVE 以外的取值一律拒绝（读到非法值必须显式报错，
   不能悄悄当成 ACTIVE——那会让"非法数据"看起来像"正常记录"）；
3. **不可变性**：聚合根不提供任何改写方法。数据库层还有触发器与权限双闸
   （见 tests/integration/test_generation_route_api.py），这里守的是"代码
   根本不去尝试改"。
"""

from __future__ import annotations

import uuid
from dataclasses import fields

import pytest

from packages.errors.error_codes import DomainError
from services.generation_execution.domain.enums import ShotRouteSelectionStatus
from services.generation_execution.domain.shot_route_selection import (
    ShotRouteSelection,
)


def _selection(**overrides: object) -> ShotRouteSelection:
    defaults: dict[str, object] = {
        "entity_id": f"srs_{uuid.uuid4().hex[:8]}",
        "project_id": "proj_001",
        "shot_id": "SHOT_001",
        "route_id": "route_001",
        "created_by": "producer_001",
        "tenant_id": uuid.uuid4(),
    }
    defaults.update(overrides)
    return ShotRouteSelection(**defaults)  # type: ignore[arg-type]


class TestShotRouteSelectionAggregate:
    def test_construct_defaults_to_active(self) -> None:
        selection = _selection()
        assert selection.status is ShotRouteSelectionStatus.ACTIVE
        assert selection.owner_module == "generation-execution"
        assert selection.ENTITY_TYPE == "ShotRouteSelection"
        assert selection.entity_version == 1

    @pytest.mark.parametrize("field", ["project_id", "shot_id", "route_id"])
    def test_business_fields_required(self, field: str) -> None:
        with pytest.raises(DomainError) as exc_info:
            _selection(**{field: "   "})
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"
        assert field in str(exc_info.value)

    def test_field_length_capped_like_orm_column(self) -> None:
        """领域层与 ORM 列长同口径：避免"领域放行、数据库截断"。"""
        with pytest.raises(DomainError) as exc_info:
            _selection(route_id="r" * 65)
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    def test_only_active_status_allowed(self) -> None:
        """单值状态集：其它取值不是契约登记的状态，必须显式拒绝。"""
        with pytest.raises(DomainError) as exc_info:
            _selection(status="SUPERSEDED")
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"
        assert "单值状态集" in str(exc_info.value)

    def test_to_dict_maps_selection_id(self) -> None:
        selection = _selection()
        data = selection.to_dict()
        assert data["selection_id"] == selection.entity_id
        assert data["project_id"] == "proj_001"
        assert data["shot_id"] == "SHOT_001"
        assert data["route_id"] == "route_001"
        assert data["status"] == "ACTIVE"
        assert data["entity_type"] == "ShotRouteSelection"
        # tenant_id 是内部隔离键，不进契约响应（与信封口径一致）。
        assert "tenant_id" not in data

    def test_aggregate_exposes_no_mutators(self) -> None:
        """聚合根不提供改写入口：不可变性首先是不去写，然后才是写不进去。"""
        public_callables = {
            name
            for name in dir(ShotRouteSelection)
            if not name.startswith("_")
            and callable(getattr(ShotRouteSelection, name))
        }
        assert public_callables == {"to_dict"}, (
            "ShotRouteSelection 不得新增公开方法：生效路线的任何改写都必须"
            "走新的治理动作并显式修订契约（V205-R14-A9）"
        )
        # 没有转移表意味着不存在"状态推进"字段：状态是构造时定型的常量。
        assert {f.name for f in fields(ShotRouteSelection)} >= {
            "project_id",
            "shot_id",
            "route_id",
            "status",
        }
