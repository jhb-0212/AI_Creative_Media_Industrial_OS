"""验签侧密钥边界单元测试（决策 4 + 包 B4：签发与验签分离）。

验收点从"约定不打印私钥"升级为"结构上不可能持有私钥"：

- ``PublicKeyStore`` 只有 ``kid → 公钥 PEM``，没有 ``current``（签发用入口）；
- 它由 PEM 或 JWKS 文档构建，**不能**由签发密钥仓储派生
  （``from_key_store`` 已删除——那条路径正是私钥进入 API 进程的入口）；
- 运行时验签仓储来自进程配置（只有公钥），且 ``get_public_key_store()``
  在公钥缺失时拒绝启动而不是"临时生成一把"。
"""

from __future__ import annotations

import pytest

from packages.identity.jwks import PublicKeyStore
from packages.identity.keys import generate_signing_key
from services.identity_access import IdentityConfigurationError, get_public_key_store


def test_public_store_from_jwks_document_round_trips() -> None:
    """JWKS 文档 → 验签仓储：公钥可还原，且不含任何私钥参数（d/p/q…）。"""
    key = generate_signing_key()
    # JWKS 由公钥导出（B4 之后没有"签发密钥仓储"这种东西）。
    document: dict[str, object] = {"keys": [key.public_jwk]}

    public_store = PublicKeyStore.from_jwks_document(document)

    assert public_store.public_pem(key.kid) == key.public_pem
    assert public_store.jwks() == document
    jwk = public_store.jwks()["keys"][0]
    for private_param in ("d", "p", "q", "dp", "dq", "qi"):
        assert private_param not in jwk


def test_public_store_rejects_non_rs256_jwks() -> None:
    """JWKS 里出现非 RSA / 非 RS256 条目 → 拒绝（否则等于允许算法降级）。"""
    key = generate_signing_key()
    document: dict[str, object] = {"keys": [key.public_jwk]}
    keys = document["keys"]
    assert isinstance(keys, list)
    tampered = {"keys": [{**keys[0], "alg": "HS256"}]}

    with pytest.raises(ValueError, match="RS256"):
        PublicKeyStore.from_jwks_document(tampered)


def test_public_store_has_no_signing_entry_point() -> None:
    """验签仓储没有签发入口：没有 ``current``，也没有"从签发仓储派生"的构造器。"""
    store = get_public_key_store()
    assert isinstance(store, PublicKeyStore)
    assert not hasattr(store, "current")
    assert not hasattr(store, "get")
    assert not hasattr(PublicKeyStore, "from_key_store")


def test_runtime_store_refuses_to_start_without_public_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公钥缺失 → 拒绝启动，不做"临时生成"兜底。

    构造"配置为空"的情形必须绕过 lru_cache 并清掉配置字段：这里直接重建仓储，
    等价于新进程启动——正是"API 容器漏挂公钥"时会发生的事。
    """
    from pydantic import SecretStr

    from packages.shared.config import Settings

    # Settings 是 frozen 的（配置不可变是它的一项保证），所以这里不 patch 字段，
    # 而是让装配层拿到一个"公钥为空"的配置对象——等价于漏挂公钥的容器。
    empty = Settings.model_construct(
        identity_public_key_pem=SecretStr(""), identity_jwks_file=""
    )
    monkeypatch.setattr("services.identity_access.get_settings", lambda: empty)
    get_public_key_store.cache_clear()
    try:
        with pytest.raises(IdentityConfigurationError, match="验签公钥缺失"):
            get_public_key_store()
    finally:
        # 恢复缓存，避免影响同进程内其它用例（lru_cache 是全局状态）。
        get_public_key_store.cache_clear()
