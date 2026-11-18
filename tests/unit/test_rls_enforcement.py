"""启动期「RLS 真的会生效」验证的单元测试（ADR-R24 §D3a-5）。

为什么这条检查必须存在
------------------------------------------------------------
``FORCE ROW LEVEL SECURITY`` 对**超级用户和 BYPASSRLS 角色无效**（PostgreSQL
语义），而当前表所有者是 ``postgres``。这意味着「DSN 误配成超级用户」会让全部
RLS 静默失效——权限检查看起来一切正常，组织边界却已经不存在。

这类失效无法在运行期被发现（请求在跑，安全属性未知），只能在启动期判定。
因此验证函数必须有两条明确行为：

1. 生产环境发现问题 → **拒绝启动**（RuntimeError）；
2. 非生产环境发现问题 → 告警放行（本地/CI 用 ``postgres`` 连库是既有事实，
   把它变成硬失败会让开发无法启动；这条差异写在代码里，不靠「测试里不跑」保证）。

这里用构造的探测结果打这两条分支：真实引擎的探测由
``tests/integration`` 在真实 PostgreSQL 上覆盖，但「生产必须拒绝」这条**不能**
只靠环境碰巧配置正确来验证。
"""

from __future__ import annotations

import pytest

from packages.shared import rls as rls_module
from packages.shared.rls import RlsEnforcement, verify_rls_enforcement


def _enforcement(**overrides: object) -> RlsEnforcement:
    """构造一次探测结果（默认是「健康」的一次探测）。"""
    defaults: dict[str, object] = {
        "role": "project_planning_role",
        "is_superuser": False,
        "bypass_rls": False,
        "rls_enabled_tables": 3,
        "rls_disabled_tables": (),
        "owned_tables_without_force": (),
    }
    defaults.update(overrides)
    return RlsEnforcement(**defaults)  # type: ignore[arg-type]


def test_clean_connection_has_no_problems() -> None:
    """健康连接无问题项（否则「全部告警」等于「没有告警」）。"""
    assert _enforcement().problems() == []
    assert _enforcement().overrides_rls is False


def test_superuser_is_reported() -> None:
    """超级用户整体绕过 RLS：必须被判定为问题，且原因点名角色。"""
    problems = _enforcement(role="postgres", is_superuser=True).problems()
    assert any("超级用户" in p and "postgres" in p for p in problems)


def test_bypass_rls_role_is_reported() -> None:
    """带 BYPASSRLS 的角色同样整体绕过 RLS。"""
    problems = _enforcement(bypass_rls=True).problems()
    assert any("BYPASSRLS" in p for p in problems)


def test_disabled_tables_are_reported() -> None:
    """未启用 RLS 的表要逐个点名（否则「有没有开」只能靠人看 DDL）。"""
    problems = _enforcement(
        rls_enabled_tables=2, rls_disabled_tables=("audit.status_transitions",)
    ).problems()
    assert any("audit.status_transitions" in p for p in problems)


def test_owned_tables_without_force_are_reported() -> None:
    """当前角色是表所有者且未 FORCE：查询会绕过策略。"""
    problems = _enforcement(
        owned_tables_without_force=("project_planning.projects",)
    ).problems()
    assert any("project_planning.projects" in p for p in problems)


async def test_production_superuser_connection_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 + 超级用户连库 → 抛 RuntimeError（拒绝启动），不是告警。"""
    bad = _enforcement(role="postgres", is_superuser=True)

    async def _fake_probe(_engine: object) -> RlsEnforcement:
        return bad

    monkeypatch.setattr(rls_module, "probe_rls_enforcement", _fake_probe)
    with pytest.raises(RuntimeError) as exc_info:
        await verify_rls_enforcement(None, app_env="production")  # type: ignore[arg-type]
    assert "拒绝" in str(exc_info.value)


async def test_non_production_only_warns(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """非生产环境同样探测，但只告警放行（本地开发用超级用户连库是既有事实）。

    断言走 ``capfd`` 而不是 ``caplog``：项目日志是 structlog ``PrintLogger``
    直接写 stdout 的 JSON，不经过 stdlib logging，``caplog`` 天然看不到——
    用 caplog 写这条断言，无论实现有没有告警都会「失败」，是假阴性陷阱。
    同时断言字段是**平铺**的：写成 ``extra={...}`` 会把 role/problems 压进
    "extra" 键，JSON 检索按字段筛选就失效了，而人眼看日志仍然「有字」。
    """
    bad = _enforcement(role="postgres", is_superuser=True)

    async def _fake_probe(_engine: object) -> RlsEnforcement:
        return bad

    monkeypatch.setattr(rls_module, "probe_rls_enforcement", _fake_probe)
    await verify_rls_enforcement(None, app_env="development")  # type: ignore[arg-type]

    captured = capfd.readouterr()
    emitted = captured.out + captured.err
    assert "rls_enforcement_degraded" in emitted, "非生产环境必须留下可见的降级告警（静默放行等于没人知道 RLS 没生效）"
    assert '"role": "postgres"' in emitted, "降级告警必须平铺携带角色字段，不能塞进 extra 包"
    assert '"app_env": "development"' in emitted
