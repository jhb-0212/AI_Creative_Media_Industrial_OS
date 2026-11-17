"""JWKS 热轮换语义单测（ADR-R29）。

覆盖 ``reload_verification_keys_if_file_changed`` 的三类行为，全部通过
注入假依赖完成，**不触碰真实的 lru_cache 全局状态**：

- 守卫条款：PEM 来源 / 未配置 JWKS 文件时直接返回 False；
- 幂等：文档未变化时返回 False，缓存不清；
- 换装：文档变化时返回 True，且两个缓存（公钥仓储 + 认证器）同时失效。

为什么"两个缓存同时失效"必须测：只清公钥仓储、留下捕获了旧仓储的认证器，
轮换等于没发生——这是本函数存在的核心原因，也是最容易被改坏的点。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from services.identity_access import deps


class _FakeSecret:
    """模拟 SecretStr 接口（只暴露 get_secret_value）。"""

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class _FakeSettings:
    """只暴露 reload 函数消费的两个字段（其余字段不应被访问）。"""

    def __init__(self, *, public_key_pem: str, jwks_file: str) -> None:
        self.identity_public_key_pem = _FakeSecret(public_key_pem)
        self.identity_jwks_file = jwks_file


class _FakeStore:
    """以文档区分身份的最小 PublicKeyStore 替身。"""

    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document

    def jwks(self) -> dict[str, Any]:
        return self._document


class _CallableCache:
    """可调用且带 cache_clear 的单例替身（记录失效次数）。"""

    def __init__(self, value: Any) -> None:
        self._value = value
        self.clear_count = 0

    def __call__(self) -> Any:
        return self._value

    def cache_clear(self) -> None:
        self.clear_count += 1


class _FakeBuilder:
    """按安装时给定文档构建的 PublicKeyStore 构建替身。"""

    _document: dict[str, Any] = {}

    @classmethod
    def from_jwks_file(cls, path: str) -> _FakeStore:
        return _FakeStore(cls._document)


@pytest.fixture
def cache_counters(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable[[], int]]:
    """把 reload 依赖的两个全局单据替换为可观测替身，返回失效计数读取器。"""
    key_store_cache = _CallableCache(_FakeStore({"keys": [{"kid": "old"}]}))
    authenticator_cache = _CallableCache(object())
    monkeypatch.setattr(deps, "get_public_key_store", key_store_cache, raising=True)
    monkeypatch.setattr(
        deps, "_principal_authenticator", authenticator_cache, raising=True
    )
    return {
        "key_store": lambda: key_store_cache.clear_count,
        "authenticator": lambda: authenticator_cache.clear_count,
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: _FakeSettings,
    file_document: dict[str, Any],
) -> None:
    """安装假 settings 与假文件构建器（按给定文档模拟 JWKS 文件内容）。"""
    _FakeBuilder._document = file_document
    monkeypatch.setattr(deps, "get_settings", lambda: settings, raising=True)
    monkeypatch.setattr(deps, "PublicKeyStore", _FakeBuilder, raising=True)


def test_reload_skips_when_pem_source(
    monkeypatch: pytest.MonkeyPatch, cache_counters: dict[str, Callable[[], int]]
) -> None:
    """PEM 来源时直接跳过：环境级轮换伴随进程重启，热刷新无意义。"""
    _install(
        monkeypatch,
        settings=_FakeSettings(
            public_key_pem="-----BEGIN PUBLIC KEY-----", jwks_file=""
        ),
        file_document={"keys": [{"kid": "new"}]},
    )
    assert deps.reload_verification_keys_if_file_changed() is False
    assert cache_counters["key_store"]() == 0
    assert cache_counters["authenticator"]() == 0


def test_reload_skips_when_jwks_file_absent(
    monkeypatch: pytest.MonkeyPatch, cache_counters: dict[str, Callable[[], int]]
) -> None:
    """未配置 JWKS 文件时直接跳过：没有可重读的轮换载体。"""
    _install(
        monkeypatch,
        settings=_FakeSettings(public_key_pem="", jwks_file=""),
        file_document={"keys": [{"kid": "new"}]},
    )
    assert deps.reload_verification_keys_if_file_changed() is False
    assert cache_counters["key_store"]() == 0
    assert cache_counters["authenticator"]() == 0


def test_reload_is_noop_when_document_unchanged(
    monkeypatch: pytest.MonkeyPatch, cache_counters: dict[str, Callable[[], int]]
) -> None:
    """文档未变化：返回 False 且两个缓存都不失效（幂等轮询）。"""
    _install(
        monkeypatch,
        settings=_FakeSettings(
            public_key_pem="", jwks_file="/run/secrets/jwks.json"
        ),
        file_document={"keys": [{"kid": "old"}]},
    )
    assert deps.reload_verification_keys_if_file_changed() is False
    assert cache_counters["key_store"]() == 0
    assert cache_counters["authenticator"]() == 0


def test_reload_swaps_caches_when_document_changed(
    monkeypatch: pytest.MonkeyPatch, cache_counters: dict[str, Callable[[], int]]
) -> None:
    """文档变化：返回 True 且两个缓存同时失效——轮换闭环的核心断言。"""
    _install(
        monkeypatch,
        settings=_FakeSettings(
            public_key_pem="", jwks_file="/run/secrets/jwks.json"
        ),
        file_document={"keys": [{"kid": "old"}, {"kid": "new"}]},
    )
    assert deps.reload_verification_keys_if_file_changed() is True
    assert cache_counters["key_store"]() == 1
    assert cache_counters["authenticator"]() == 1
