"""RSA 密钥对管理（RS256 签名算法的密钥基础设施）。

设计要点（决策 4：RS256 非对称 JWT + 强制 JWKS）：
- 私钥：仅用于**签发**令牌（Authorization Service / 身份服务持有），
  存离线/高安全区；本模块不把私钥暴露给任何验签路径。
- 公钥：用于**验签**，可安全分发给任何网关/路由层，经 JWKS 文档按 kid 索引。
- 密钥轮换：持有一组「当前 + 历史」密钥对（key id → 密钥），历史私钥保留
  用于验证仍在有效期内的旧令牌，但不再用于签发新令牌。

安全红线（6「密钥不落日志」）：
- 私钥以 ``bytes``（PEM DER 编码）持有，本模块任何方法都不 repr/print 私钥。
- 对外只暴露公钥与 JWKS 结构；私钥导出方法命名显式标注 ``_private`` 并加文档，
  禁止在日志/审计事件中出现私钥内容。
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

# 默认密钥强度：2048 位 RSA（RS256 工业标准下限，兼顾安全与验签性能）。
DEFAULT_KEY_SIZE = 2048


@dataclass(frozen=True)
class SigningKey:
    """一对 RSA 密钥（kid + 私钥 + 公钥），用于签发/验签令牌。

    private_pem / public_pem 均为 PEM 字节流（DER 编码的 ASCII PEM）。
    """

    kid: str
    # repr=False：防止日志/调试打印对象时把完整私钥 PEM 落盘（红线「密钥不落日志」）。
    private_pem: bytes = field(repr=False)
    public_pem: bytes

    @property
    def public_jwk(self) -> dict[str, str]:
        """导出公钥为 JWK 结构（RSA，含 n/e/kty/kid/alg/use），供 JWKS 文档使用。"""
        public_key = serialization.load_pem_public_key(self.public_pem)
        if not isinstance(public_key, RSAPublicKey):
            raise TypeError("签名密钥必须是 RSA 公钥")
        numbers = public_key.public_numbers()
        n_bytes = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
        e_bytes = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
        return {
            "kty": "RSA",
            "kid": self.kid,
            "use": "sig",
            "alg": "RS256",
            "n": _b64url(n_bytes),
            "e": _b64url(e_bytes),
        }


def _b64url(data: bytes) -> str:
    """Base64url 无填充编码（JWK 要求的 n/e 编码格式）。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_signing_key(kid: str | None = None, key_size: int = DEFAULT_KEY_SIZE) -> SigningKey:
    """生成一对 RSA 密钥（默认 2048 位），返回带 kid 的 SigningKey。

    用于密钥轮换或测试环境；生产环境应从密钥管理服务加载既有私钥，
    而非每次启动随机生成（随机生成会导致验签方无法获得匹配公钥）。
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return SigningKey(kid=kid or uuid.uuid4().hex, private_pem=private_pem, public_pem=public_pem)


def load_signing_key(private_pem: bytes, public_pem: bytes, kid: str) -> SigningKey:
    """从既有 PEM 字节构造 SigningKey（生产从密钥管理服务加载）。

    校验 private_pem / public_pem 是合法可解析的密钥，避免把损坏/错误
    内容注入签名路径。
    """
    serialization.load_pem_private_key(private_pem, password=None)
    serialization.load_pem_public_key(public_pem)
    return SigningKey(kid=kid, private_pem=private_pem, public_pem=public_pem)
