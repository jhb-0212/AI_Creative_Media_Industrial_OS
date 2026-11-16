"""JWKS（JSON Web Key Set）文档的导出与消费（决策 4 + 包 B4）。

设计要点：
- **本模块只有公钥**。签发端的密钥由 ``packages.identity.issuer.TokenIssuer``
  独占持有；这里既不解私钥，也不提供"由签发密钥派生"的入口。
- 两个方向都要支持，否则"公钥走 JWKS"只是一句口号：
  * 导出：``public_jwk_from_pem`` / ``PublicKeyStore.jwks()``（身份服务发布公钥）；
  * 消费：``PublicKeyStore.from_jwks_document`` / ``from_jwks_file``（验签方按
    kid 取公钥，支持轮换期多 kid 共存）。
- 刻意删除了旧的 ``KeyStore``：它是"进程内持有多个签名密钥"的注册表，
  正是 B4 要消除的形态。多 kid 的合法需求（轮换期验签）由 ``PublicKeyStore``
  承载——它只装公钥，装不下私钥。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from packages.identity.keys import public_jwk_from_pem


def public_pem_from_jwk(jwk: dict[str, Any]) -> bytes:
    """把 RSA 公钥 JWK 还原为 PEM（JWKS 文档 → 验签用公钥）。

    只接受 ``kty=RSA`` + ``use=sig``，且算法为 RS256 或未声明：
    本项目验签算法固定 RS256，接受其它算法等于允许算法降级攻击。
    """
    if jwk.get("kty") != "RSA":
        raise ValueError("JWKS 只接受 RSA 公钥")
    if jwk.get("use") not in (None, "sig"):
        raise ValueError("JWKS 条目 use 必须为 sig")
    if jwk.get("alg") not in (None, "RS256"):
        raise ValueError("JWKS 条目 alg 必须为 RS256")
    n = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
    e = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
    public_key = RSAPublicNumbers(e, n).public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class PublicKeyStore:
    """仅公钥的验签注册表（API/网关验签路径专用）。

    本类只持有 ``kid → 公钥 PEM``，结构上不存在私钥字段——验签进程即使被
    dump 内存也拿不到签发私钥。旧的 ``KeyStore`` 已删除，因此"从签发仓储派生
    公钥视图"这条路径也不存在了（它正是私钥进入 API 进程的入口）。
    """

    def __init__(self, keys: dict[str, bytes]) -> None:
        if not keys:
            raise ValueError("PublicKeyStore 至少需要一个公钥")
        self._by_kid: dict[str, bytes] = dict(keys)

    def public_pem(self, kid: str) -> bytes | None:
        """按 kid 取公钥 PEM（验签）。"""
        return self._by_kid.get(kid)

    def jwks(self) -> dict[str, object]:
        """导出 JWKS 文档（仅公钥；供身份服务对外发布）。"""
        return {
            "keys": [
                public_jwk_from_pem(pem, kid) for kid, pem in self._by_kid.items()
            ]
        }

    @classmethod
    def from_jwks_document(cls, document: dict[str, Any]) -> PublicKeyStore:
        """从 JWKS 文档构建验签仓储（公钥分发路径，B4）。

        验签进程只需要这份文档：它由身份服务发布，可来自挂载的 secret 文件，
        将来也可来自 HTTP 端点。无论哪种来源，本类结构上都不存在私钥字段。
        """
        keys = document.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError("JWKS 文档缺少 keys")
        by_kid: dict[str, bytes] = {}
        for entry in keys:
            if not isinstance(entry, dict):
                raise ValueError("JWKS keys 条目必须是对象")
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid:
                raise ValueError("JWKS 条目缺少 kid")
            by_kid[kid] = public_pem_from_jwk(entry)
        return cls(by_kid)

    @classmethod
    def from_jwks_file(cls, path: str | Path) -> PublicKeyStore:
        """从 JWKS 文件构建验签仓储。"""
        raw = Path(path).read_text(encoding="utf-8")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("JWKS 文件顶层必须是对象")
        return cls.from_jwks_document(document)
