"""QC 报告核验器的装配闸门。

被测命题：**「跨域端口确实被接上了」**，而不是「代码里有这么个函数」。

为什么需要单独的装配测试：报告核验器的失效方式是静默的——如果组合根没接上
（或 state 键两边写错），服务层会 fail-closed 拒绝终结，症状是「每次判定都
500」，看起来像业务 bug；若有人把它改成「没接上就放行」，症状会变成
「看起来通过」。两种情况都没有语法/类型错误可捕获，只有把「使用点读的键」
和「装配点写的键」放在一起比一次才能证伪。

同时验证适配器本身：返回归属事实、缺失返回 ``None``（由服务层转成
「报告不存在」），并且**两种会话形状读的是同一口径**（API 侧注入会话、
Worker 侧每调用一次自取会话并设租户上下文）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from apps.api.wiring import QC_REPORT_LOOKUP_FACTORY, register_cross_domain_ports


class _FakeRepo:
    def __init__(self, report: Any) -> None:
        self._report = report

    async def get_by_id(self, qc_report_id: str) -> Any:
        return self._report


@pytest.fixture()
def patched_repo(monkeypatch):
    """替换仓储构造：本测试验装配与口径，不验 SQL。"""

    def _install(report: Any) -> None:
        import services.asset_service.repository.qc_report_lookup as module

        monkeypatch.setattr(module, "SqlAlchemyQCReportRepository", lambda _s: _FakeRepo(report))

    return _install


def test_composition_root_registers_the_lookup_factory() -> None:
    """组合根必须真的把适配器挂上 app.state——否则每次 QC 终结都会 500。"""
    from fastapi import FastAPI

    from services.asset_service.repository.qc_report_lookup import build_report_lookup

    app = FastAPI()
    register_cross_domain_ports(app)
    assert getattr(app.state, QC_REPORT_LOOKUP_FACTORY, None) is build_report_lookup


def test_api_composition_root_actually_ran() -> None:
    """真实应用入口必须调用过装配——只测 register 函数不够。"""
    from apps.api.main import app

    assert getattr(app.state, QC_REPORT_LOOKUP_FACTORY, None) is not None


def test_route_reads_the_same_state_key_the_root_writes() -> None:
    """装配点与使用点的 state 键必须一致（两边各写一个字面量，最易漂移）。"""
    from services.qc_governance.api import qc_task_routes

    assert qc_task_routes._LOOKUP_FACTORY_STATE_KEY == QC_REPORT_LOOKUP_FACTORY


async def test_lookup_returns_ownership_facts(patched_repo) -> None:
    from services.asset_service.repository.qc_report_lookup import build_report_lookup

    tenant = uuid4()
    patched_repo(SimpleNamespace(asset_version_id="asset-1", tenant_id=tenant))

    facts = await build_report_lookup(object())("report-1", tenant)
    assert facts is not None
    assert facts.asset_version_id == "asset-1"
    assert facts.tenant_id == tenant


async def test_lookup_returns_none_for_unknown_report(patched_repo) -> None:
    from services.asset_service.repository.qc_report_lookup import build_report_lookup

    patched_repo(None)
    assert await build_report_lookup(object())("missing", uuid4()) is None


async def test_sessionmaker_variant_sets_tenant_context(patched_repo, monkeypatch) -> None:
    """Worker 侧必须显式设租户上下文：否则 RLS 下读到 0 行，
    「越权」与「报告不存在」会给出同一个结论。

    注入点必须在 **workers** 而非 services：业务代码自行注入 RLS 作用域被
    SCOPE-4 禁止（唯一注入点是 ``build_tenant_uow_dependency``）。
    """
    from workers.qc.__main__ import _make_report_lookup

    tenant = uuid4()
    patched_repo(SimpleNamespace(asset_version_id="asset-1", tenant_id=tenant))

    seen: list[Any] = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    import workers.qc.__main__ as worker_main

    async def _record(session, tenant_id):
        seen.append(tenant_id)

    monkeypatch.setattr(worker_main, "set_tenant_context", _record)

    facts = await _make_report_lookup(lambda: _Session())("report-1", tenant)
    assert facts is not None and facts.asset_version_id == "asset-1"
    assert seen == [tenant]
