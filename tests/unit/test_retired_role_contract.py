"""WP-03 方案 A：退役角色契约（`RETIRED_ROLES` 五字段 + 恢复五条硬约束）。

判据来自 2026-11-27 裁决，并按 D4 裁决（2026-11-28，f2b3 选 A）修订执行边界：
- 当前 head 执行 Bootstrap 不创建 `qc_worker`；
- 退役职责在身份治理工具里执行，迁移只登记边界、不做身份 DDL；
- 恢复退役身份（受控、显式确认）的状态与历史定义一致；
- 口令不进 Registry 生成物 / 迁移源文件 / 审计报告，缺失时 fail-closed；
- 恢复必须显式确认并产生告警；
- 未声明角色不能通过 Bootstrap 或迁移被隐式创建。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from packages.shared import config as config_module
from packages.shared.db_roles import maintenance_role
from packages.shared.migration_identity import (
    RETIRED_RESTORE_ENV,
    restore_retired_role,
)
from packages.shared.role_specs import (
    RETIRED_RESTORE_POLICIES,
    RETIRED_ROLES,
    retired_registry_payload,
    retired_role_names,
    retired_role_problems,
    role_specs,
    spec_problems,
)

_ROOT = Path(__file__).resolve().parents[2]
_RETIREMENT_MIGRATION = (
    _ROOT / "data" / "migrations" / "versions" / "f2b3c4d5e6a7_qc_worker_identity_retirement.py"
)


class _Recorder:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def test_retired_roles_declare_all_five_fields() -> None:
    assert RETIRED_ROLES, "退役身份必须有声明，否则降级只能凭空推导"
    for item in RETIRED_ROLES:
        assert item.retired_at_revision
        assert item.retirement_reason.strip()
        assert item.historical_attributes
        assert item.downgrade_restore_policy in RETIRED_RESTORE_POLICIES
        assert item.credential_source, "LOGIN 身份必须有可信凭据来源"


def test_retired_role_is_not_in_current_target_state() -> None:
    retired = retired_role_names()
    assert maintenance_role("worker") in retired
    active_names = {spec.name for spec in role_specs()}
    assert not active_names & retired, "退役身份不得同时出现在当前目标态（Bootstrap 会重建它）"


def test_declarations_are_self_consistent() -> None:
    assert spec_problems() == []
    assert retired_role_problems() == []


def test_registry_payload_has_retired_section_without_secret_values() -> None:
    payload = retired_registry_payload()
    assert payload, "生成物必须包含退役段"
    text = str(payload)
    assert "settings." in text
    # 生成物里不能出现口令值形态：只允许配置字段名。
    assert "PASSWORD '" not in text
    assert all("credential_source" in entry for entry in payload)


def _migration_code_only() -> str:
    """迁移文件去掉模块/函数 docstring 后的「会执行的代码」。"""
    source = _RETIREMENT_MIGRATION.read_text(encoding="utf-8")
    parts = source.split('"""')
    # 偶数段是 docstring（0 是 shebang 前的空段），奇数段才是代码
    return "\n".join(parts[0::2])


def test_retirement_migration_does_not_manage_identity_lifecycle() -> None:
    """D4 裁决（Owner 2026-11-28，f2b3 选 A）：退役职责整体移出迁移。

    迁移只登记退役边界与只读提示；不 ``DROP ROLE``、不 ``CREATE ROLE``、
    不 ``ALTER ROLE``、不写口令，也不调用恢复入口（恢复不再是降级路径的一部分）。
    """
    code = _migration_code_only()
    assert "restore_retired_role(" not in code, (
        "降级不得重建已退役身份：恢复只能走受控身份治理流程"
    )
    for forbidden in ("DROP ROLE", "CREATE ROLE", "ALTER ROLE", "PASSWORD '"):
        assert forbidden not in code, f"迁移不得执行身份生命周期 DDL：{forbidden}"
    assert "retired_roles_by_name(" in code, (
        "退役边界必须从声明层取（RETIRED_ROLES），不能凭空假设"
    )


def test_retirement_tool_is_the_execution_entry() -> None:
    """退役的执行载体是身份治理工具，且目标清单来自同一声明源。"""
    tool = _ROOT / "scripts" / "retire_legacy_roles.py"
    source = tool.read_text(encoding="utf-8")
    assert "--retired" in source
    assert "retired_role_names()" in source, "目标必须读 RETIRED_ROLES，不另存清单"
    assert "RETIREMENT_EXECUTED" in source, "执行结果必须与 RETIRED 状态分开记录"



def test_restore_requires_explicit_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.delenv(RETIRED_RESTORE_ENV, raising=False)
    with pytest.raises(RuntimeError, match=RETIRED_RESTORE_ENV):
        restore_retired_role(maintenance_role("worker"), op=recorder)
    assert recorder.statements == [], "未授权时不得执行任何 DDL"


def test_restore_rejects_wrong_authorization_value(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setenv(RETIRED_RESTORE_ENV, "1")
    with pytest.raises(RuntimeError, match="显式确认"):
        restore_retired_role(maintenance_role("worker"), op=recorder)
    assert recorder.statements == []


def test_restore_fails_closed_when_credential_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    revision = RETIRED_ROLES[0].retired_at_revision
    monkeypatch.setenv(RETIRED_RESTORE_ENV, revision)

    class _Settings:
        postgres_qc_worker_password = None

    monkeypatch.setattr(config_module, "get_settings", lambda: _Settings())
    with pytest.raises(RuntimeError, match="口令来源缺失"):
        restore_retired_role(maintenance_role("worker"), op=recorder)
    assert recorder.statements == []


def test_restore_fails_closed_when_credential_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    revision = RETIRED_ROLES[0].retired_at_revision
    monkeypatch.setenv(RETIRED_RESTORE_ENV, revision)

    class _Secret:
        def get_secret_value(self) -> str:
            return ""

    class _Settings:
        postgres_qc_worker_password = _Secret()

    monkeypatch.setattr(config_module, "get_settings", lambda: _Settings())
    with pytest.raises(RuntimeError, match="空口令"):
        restore_retired_role(maintenance_role("worker"), op=recorder)
    assert recorder.statements == []


def test_restore_applies_historical_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    contract = RETIRED_ROLES[0]
    monkeypatch.setenv(RETIRED_RESTORE_ENV, contract.retired_at_revision)

    class _Secret:
        def get_secret_value(self) -> str:
            return "history-pw"

    class _Settings:
        postgres_qc_worker_password = _Secret()

    monkeypatch.setattr(config_module, "get_settings", lambda: _Settings())
    restore_retired_role(maintenance_role("worker"), op=recorder)

    joined = "\n".join(recorder.statements)
    assert "RAISE WARNING" in joined, "恢复必须带告警，不能只依赖日志"
    assert "CREATE ROLE" in joined
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS" in joined
    # 历史定义是零成员：不得因为默认配置里有 operator 就加回权限组。
    assert f"REVOKE {maintenance_role('operator')}" in joined


def test_restore_refuses_undeclared_role() -> None:
    recorder = _Recorder()
    with pytest.raises(RuntimeError, match="不在 RETIRED_ROLES"):
        restore_retired_role("some_random_role", op=recorder)


def test_restore_signature_allows_gate_replay() -> None:
    """门禁回放会替换迁移模块的 ``op``：恢复入口必须接受显式 op。"""
    params = inspect.signature(restore_retired_role).parameters
    assert "op" in params
    assert params["op"].kind is inspect.Parameter.KEYWORD_ONLY
