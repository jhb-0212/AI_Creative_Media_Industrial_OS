"""JWKS（JSON Web Key Set）文档导出与按 kid 缓存（决策 4）。

设计要点：
- 验签方（API 网关 / 各路由层）只拿公钥，经 JWKS 文档按 kid 索引取公钥，
  私钥永不进入本路径。
- KeyStore 持有一组 SigningKey（当前 + 历史），支持密钥轮换：新 kid 用于签发，
  旧 kid 仅用于验签仍在有效期内的令牌；轮换对验签方无感（按 kid 命中）。
- 缓存语义：KeyStore 是内存中的单一事实源，构建后不可变追加，
  避免并发下密钥表被篡改。
"""

from __future__ import annotations

from packages.identity.keys import SigningKey, public_jwk_from_pem


class KeyStore:
    """一组签名密钥的不可变注册表（按 kid 索引，供签发与验签）。

    - ``current``：当前用于签发的密钥（kid 最新）。
    - ``get(kid)``：按 kid 取公钥用于验签，未命中返回 None。
    - ``jwks()``：导出 JWKS 文档（仅公钥，供外部验签方拉取）。
    """

    def __init__(self, keys: list[SigningKey]) -> None:
        if not keys:
            raise ValueError("KeyStore 至少需要一个签名密钥")
        self._by_kid: dict[str, SigningKey] = {k.kid: k for k in keys}
        # 当前密钥取列表最后一个（约定：新轮换的密钥追加在末尾）。
        self._current: SigningKey = keys[-1]

    @property
    def current(self) -> SigningKey:
        """当前签发密钥（kid 最新）。"""
        return self._current

    def get(self, kid: str) -> SigningKey | None:
        """按 kid 取密钥（验签方据此取公钥）。"""
        return self._by_kid.get(kid)

    def public_pem(self, kid: str) -> bytes | None:
        """按 kid 取公钥 PEM（验签直接使用）。"""
        key = self._by_kid.get(kid)
        return key.public_pem if key is not None else None

    def public_keys(self) -> dict[str, bytes]:
        """导出 kid → 公钥 PEM 映射（供纯公钥验签仓储构建，不带私钥）。"""
        return {kid: key.public_pem for kid, key in self._by_kid.items()}

    def jwks(self) -> dict[str, object]:
        """导出 JWKS 文档（仅公钥，不含任何私钥信息）。"""
        return {"keys": [key.public_jwk for key in self._by_kid.values()]}


class PublicKeyStore:
    """仅公钥的验签注册表（API/网关验签路径专用）。

    与 :class:`KeyStore` 的关键区别：本类只持有 ``kid → 公钥 PEM``，
    结构上不存在私钥字段——验签进程即使被 dump 内存也拿不到签发私钥。
    """

    def __init__(self, keys: dict[str, bytes]) -> None:
        if not keys:
            raise ValueError("PublicKeyStore 至少需要一个公钥")
        self._by_kid: dict[str, bytes] = dict(keys)

    def public_pem(self, kid: str) -> bytes | None:
        """按 kid 取公钥 PEM（验签）。"""
        return self._by_kid.get(kid)

    def jwks(self) -> dict[str, object]:
        """导出 JWKS 文档（与 KeyStore 同构，仅公钥）。"""
        return {
            "keys": [
                public_jwk_from_pem(pem, kid) for kid, pem in self._by_kid.items()
            ]
        }

    @classmethod
    def from_key_store(cls, store: KeyStore) -> PublicKeyStore:
        """从签发密钥仓储派生纯公钥视图（开发/单进程场景，公钥与私钥同源）。"""
        return cls(store.public_keys())
