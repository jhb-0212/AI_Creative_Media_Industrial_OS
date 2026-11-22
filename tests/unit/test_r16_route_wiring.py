"""R16 路由装配守卫：四个新端点不得退回进程内内存仓储。

回归背景：R16 首批路由用模块级 ``InMemoryRepository`` 单例装配服务，端点能跑通、
单测全绿，但重启即丢数据，且 outbox 事件会指向一个不存在的聚合。持久层落地后
路由改走 SQLAlchemy 仓储 + UoW（``await uow.commit()``），本用例把这条装配关系
钉住：装配函数返回的仓储类型必须是 SQL 实现，模块内不得再出现内存单例。

它测的不是「行为」（行为由 ``scripts/verify_r16_persistence.py`` 在真实库上证明），
而是「装配形状」——形状一旦退回，上面那个脚本仍然会通过，因为它自己装配服务。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from services.budget_quota.repository.sqlalchemy_repos import (
    SqlAlchemyBudgetLedgerRepository,
    SqlAlchemyShotBudgetRequestRepository,
)
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
    SqlAlchemyProductionRouteRepository,
)
from services.qc_governance.repository.sqlalchemy_repo import (
    SqlAlchemyGateDecisionRepository,
)

_ROUTES = {
    "services.production_planning.api.production_routes": (
        "_repository",
        SqlAlchemyProductionRouteRepository,
    ),
    "services.production_planning.api.generation_routes": (
        "_repository",
        SqlAlchemyGenerationTaskRepository,
    ),
    "services.budget_quota.api.budget_routes": (
        "_repository",
        SqlAlchemyShotBudgetRequestRepository,
    ),
    "services.qc_governance.api.gate_routes": (
        "_repository",
        SqlAlchemyGateDecisionRepository,
    ),
}


class _FakeSession:
    """装配函数只把会话存进仓储，不触碰它——空对象足以验证类型。"""


@pytest.mark.parametrize("module_name", sorted(_ROUTES))
def test_route_module_has_no_in_memory_repository(module_name: str) -> None:
    module = importlib.import_module(module_name)
    # 模块级内存单例一旦回来，`_repository` 就会重新出现在模块命名空间里。
    assert not hasattr(module, "_repository")
    assert not hasattr(module, "_outbox")


@pytest.mark.parametrize(("module_name", "case"), sorted(_ROUTES.items()))
def test_service_is_wired_to_sqlalchemy_repository(
    module_name: str, case: tuple[str, type[Any]]
) -> None:
    module = importlib.import_module(module_name)
    attribute, expected_repo = case
    service = module._service(_FakeSession())
    repository = getattr(service, attribute)
    assert isinstance(repository, expected_repo)
    assert isinstance(service._outbox_writer, SqlAlchemyOutboxWriter)


def test_budget_ledger_repository_is_sqlalchemy() -> None:
    """审批路径的账本仓储不能是内存实现——它是「审批即入账」的证据所在。"""
    module = importlib.import_module("services.budget_quota.api.budget_routes")
    service = module._service(_FakeSession())
    assert isinstance(service._ledger_repository, SqlAlchemyBudgetLedgerRepository)
