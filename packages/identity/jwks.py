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

from collections.abc import Iterable

from packages.identity.keys import (
    SigningKey,
    public_jwk_from_pem,
    public_key_thumbprint,
)


class KeyStore:
    """一组签名密钥的不可变注册表（按 kid 索引，供签发与验签）。

    - ``current``：当前用于签发的密钥（kid 最新）。
    - ``get(kid)``：按 kid 取公钥用于验签，未命中返回 None。
    - ``jwks()``：导出 JWKS 文档（仅公钥，供外部验签方拉取）。
    """

    def __init__(self, keys: list[SigningKey]) -> None:
        if not keys:
            raise ValueError("KeyStore 至少需要一个签名密钥")
        # kid 现在是公钥指纹（RFC 7638），重复即代表同一把密钥被注册两次或
        # 调用方自行指派了相同 kid——两种情况都会让索引静默覆盖，
        # 因此这里显式拒绝，而不是让「后注册的赢」变成一个隐性事实。
        seen: set[str] = set()
        for key in keys:
            if key.kid in seen:
                raise ValueError(f"KeyStore 中存在重复 kid：{key.kid}（同 kid 会静默覆盖前一把密钥）")
            seen.add(key.kid)
        self._by_kid: dict[str, SigningKey] = {k.kid: k for k in keys}
        # 当前密钥取列表最后一个（约定：新轮换的密钥追加在末尾）。
        # kid 是内容指纹，无法按字典序判断新旧，因此「谁是当前密钥」由顺序表达。
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

    def public_pems(self) -> list[bytes]:
        """导出公钥 PEM 列表（供纯公钥验签仓储构建，不带私钥）。

        返回列表而非 ``{kid: pem}``：kid 由公钥派生，让调用方连「提供 kid」
        这个动作都不存在，才不会有贴错标签的余地。
        """
        return [key.public_pem for key in self._by_kid.values()]

    def jwks(self) -> dict[str, object]:
        """导出 JWKS 文档（仅公钥，不含任何私钥信息）。"""
        return {"keys": [key.public_jwk for key in self._by_kid.values()]}


class PublicKeyStore:
    """仅公钥的验签注册表（API/网关验签路径专用）。

    与 :class:`KeyStore` 的关键区别：本类只持有 ``kid → 公钥 PEM``，
    结构上不存在私钥字段——验签进程即使被 dump 内存也拿不到签发私钥。

    构造只接受**公钥 PEM 列表**，索引键由 :func:`public_key_thumbprint` 现场算出。
    接受 ``{kid: pem}`` 会让调用方自己给公钥贴标签——标签一旦与内容不符，
    签发方按 A 签、验签方按内容索引到 B，令牌全部拒收；这类错误必须在
    类型层面无法表达，而不是靠约定「调用方要算对」。
    """

    def __init__(self, public_pems: Iterable[bytes]) -> None:
        by_kid: dict[str, bytes] = {}
        for pem in public_pems:
            kid = public_key_thumbprint(pem)
            if kid in by_kid:
                raise ValueError(f"PublicKeyStore 中存在重复公钥：kid={kid}")
            by_kid[kid] = pem
        if not by_kid:
            raise ValueError("PublicKeyStore 至少需要一个公钥")
        self._by_kid: dict[str, bytes] = by_kid

    def public_pem(self, kid: str) -> bytes | None:
        """按 kid 取公钥 PEM（验签）。"""
        return self._by_kid.get(kid)

    def jwks(self) -> dict[str, object]:
        """导出 JWKS 文档（与 KeyStore 同构，仅公钥）。"""
        return {"keys": [public_jwk_from_pem(pem) for pem in self._by_kid.values()]}
