"""运行时主体解析台账的双向校验（描述性契约原文不得被悄悄改权）。

`PRODUCTION.GATE.APPROVE` 在冻结契约里没有机器可读的角色名。本测试证明三件事：

1. 台账确实把它解析成最小授权集合（而不是让整条链无人可调用）；
2. 契约原文变了、台账没跟上 → 加载期 fail-fast（不是继续按旧解释放行）；
3. 契约闸门能独立发现"台账与原文不一致"，不依赖运行时恰好被走到。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity import principal_bindings
from packages.identity.action_registry import action_spec


def test_gate_action_resolves_to_minimal_role_set() -> None:
    spec = action_spec("PRODUCTION.GATE.APPROVE")
    assert spec is not None
    assert spec.allowed_principals == frozenset({"导演"})
    assert spec.allowed_principals_text == "导演"
    assert spec.is_critical


def test_unbound_action_keeps_contract_text() -> None:
    """未登记动作的行为完全不变（台账不是全局授权改写器）。"""
    spec = action_spec("DELIVERY.MANIFEST.CREATE")
    assert spec is not None
    assert spec.allowed_principals == frozenset({"交付服务", "授权制片"})


def test_text_drift_is_fatal() -> None:
    with pytest.raises(DomainError) as exc:
        principal_bindings.resolve_principals_text(
            "PRODUCTION.GATE.APPROVE", "某个新角色"
        )
    assert exc.value.error_code is ErrorRegistry.INTERNAL_ERROR
    assert "已变化" in exc.value.detail


def test_ledger_drift_is_detected_by_contract_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """闸门独立校验台账原文，防止有人只改契约不改台账就悄悄换权。"""
    import scripts.contract_check as cc

    ledger = tmp_path / "principal_bindings.yaml"
    original = Path(cc.PRINCIPAL_BINDINGS_PATH).read_text(encoding="utf-8")
    ledger.write_text(
        original.replace(
            "contract_text: '对应 Gate 授权人类职责主体'",
            "contract_text: '导演/制片'",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cc, "PRINCIPAL_BINDINGS_PATH", ledger)
    assert cc.check_principal_bindings() is False


def test_malformed_ledger_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """台账结构损坏时加载期拒绝，不返回半份可用配置。"""
    ledger = tmp_path / "principal_bindings.yaml"
    ledger.write_text(
        "version: 1\nbindings:\n  PRODUCTION.GATE.APPROVE:\n"
        "    contract_text: x\n    roles: []\n    reason: r\n    unblocks_when: u\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(principal_bindings, "_BINDINGS_PATH", ledger)
    principal_bindings.load_principal_bindings.cache_clear()
    with pytest.raises(DomainError) as exc:
        principal_bindings.load_principal_bindings()
    assert exc.value.error_code is ErrorRegistry.INTERNAL_ERROR
    principal_bindings.load_principal_bindings.cache_clear()
