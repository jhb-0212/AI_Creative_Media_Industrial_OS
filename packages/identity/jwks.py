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

from packages.identity.keys import SigningKey


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

    def jwks(self) -> dict[str, object]:
        """导出 JWKS 文档（仅公钥，不含任何私钥信息）。"""
        return {"keys": [key.public_jwk for key in self._by_kid.values()]}
