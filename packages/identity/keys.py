"""RSA 密钥对管理（RS256 签名算法的密钥基础设施）。

设计要点（决策 4：RS256 非对称 JWT + 强制 JWKS）：
- 私钥：仅用于**签发**令牌（Authorization Service / 身份服务持有），
  存离线/高安全区；本模块不把私钥暴露给任何验签路径。
- 公钥：用于**验签**，可安全分发给任何网关/路由层，经 JWKS 文档按 kid 索引。
- 密钥轮换：持有一组「当前 + 历史」密钥对（key id → 密钥），历史私钥保留
  用于验证仍在有效期内的旧令牌，但不再用于签发新令牌。
- **kid 是密钥内容的指纹，不是角色标签**（RFC 7638 JWK Thumbprint，详见
  :func:`public_key_thumbprint`）。此前 kid 被硬编码为字面量 ``"current"``，
  而 ``KeyStore`` 以 ``{kid: key}`` 建索引——两把密钥同名会静默互相覆盖，
  使「多 kid 轮换」在实现层不可达（契约声明了能力，代码没有入口）。
  改为内容派生后：签发方与验签方各自从公钥独立算出同一个 kid，无需共享配置；
  不同密钥必然得到不同 kid，轮换才真正可表达。

安全红线（6「密钥不落日志」）：
- 私钥以 ``bytes``（PEM DER 编码）持有，本模块任何方法都不 repr/print 私钥。
- 对外只暴露公钥与 JWKS 结构；私钥导出方法命名显式标注 ``_private`` 并加文档，
  禁止在日志/审计事件中出现私钥内容。
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey, RSAPublicNumbers

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
        return public_jwk_from_pem(self.public_pem, self.kid)


def public_jwk_from_pem(public_pem: bytes, kid: str | None = None) -> dict[str, str]:
    """把公钥 PEM 转为 JWK 结构（签发侧与纯公钥验签侧共用，避免两份编码实现）。

    ``kid`` 缺省时取公钥指纹（:func:`public_key_thumbprint`），保证 JWKS 文档
    里的 kid 与令牌头部的 kid 同源——两者都从同一把公钥算出，不依赖任何
    调用方记得传参。
    """
    numbers = _rsa_public_numbers(public_pem)
    n_bytes = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e_bytes = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {
        "kty": "RSA",
        "kid": kid if kid is not None else _thumbprint_from_numbers(numbers.n, numbers.e),
        "use": "sig",
        "alg": "RS256",
        "n": _b64url(n_bytes),
        "e": _b64url(e_bytes),
    }


def _rsa_public_numbers(public_pem: bytes) -> RSAPublicNumbers:
    """解析 PEM 公钥并断言是 RSA——签名算法是 RS256，非 RSA 公钥属于配置错误。"""
    public_key = serialization.load_pem_public_key(public_pem)
    if not isinstance(public_key, RSAPublicKey):
        raise TypeError("签名密钥必须是 RSA 公钥")
    return public_key.public_numbers()


def _thumbprint_from_numbers(n: int, e: int) -> str:
    """按 RFC 7638 计算 JWK 指纹（kid 的唯一实现，避免多份编码各自为政）。

    规范要求：只取**必需成员** ``{"e","kty","n"}``，键名字典序、无空白、
    不含 ``kid``/``use``/``alg``（含这些会让 kid 无法自证、轮换时永久漂移）。
    对规范 JSON 取 SHA-256 后 base64url 无填充编码。
    """
    n_b64 = _b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))
    e_b64 = _b64url(e.to_bytes((e.bit_length() + 7) // 8, "big"))
    # 用 json.dumps 生成规范 JSON 而不是手拼字符串：RFC 7638 要求「键名字典序、
    # 无多余空白」，交给序列化器比人肉保证少一类出错面（sort_keys=True +
    # separators 去掉分隔空格，正好是规范形态）。
    canonical = json.dumps(
        {"e": e_b64, "kty": "RSA", "n": n_b64},
        sort_keys=True,
        separators=(",", ":"),
    )
    return _b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def public_key_thumbprint(public_pem: bytes) -> str:
    """公钥的 RFC 7638 指纹，用作 kid。

    为什么是内容派生而不是配置项/随机串：
    - **单一事实源**：kid 由公钥唯一决定，签发方（持私钥+公钥）与验签方
      （只持公钥）各自独立计算即得同一值，不存在「两边配置不一致」这类故障面；
    - **轮换可表达**：不同密钥必然得到不同 kid，``KeyStore`` 的多 kid 索引
      才不会被同名覆盖；
    - **可自证**：验签方拿到 JWKS 后可用公钥重算指纹，校验 kid 未被调包。
    """
    numbers = _rsa_public_numbers(public_pem)
    return _thumbprint_from_numbers(numbers.n, numbers.e)


def _b64url(data: bytes) -> str:
    """Base64url 无填充编码（JWK 要求的 n/e 编码格式）。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_signing_key(kid: str | None = None, key_size: int = DEFAULT_KEY_SIZE) -> SigningKey:
    """生成一对 RSA 密钥（默认 2048 位），返回带 kid 的 SigningKey。

    ``kid`` 缺省取公钥指纹（:func:`public_key_thumbprint`），而非随机串：
    随机 kid 让同一把密钥在不同进程/重启后得到不同标识，轮换与排障都失去依据。

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
    return SigningKey(
        kid=kid if kid is not None else public_key_thumbprint(public_pem),
        private_pem=private_pem,
        public_pem=public_pem,
    )


def load_signing_key(
    private_pem: bytes,
    public_pem: bytes,
    kid: str | None = None,
) -> SigningKey:
    """从既有 PEM 字节构造 SigningKey（生产从密钥管理服务加载）。

    校验内容：
    - 两个 PEM 都能解析；
    - 私钥与公钥**确实是同一对**——此前不校验，配置张冠李戴时会签出
      「私钥 A 签、JWKS 播报公钥 B」的令牌，全部验签失败且现象极难定位。
    - ``kid`` 缺省取公钥指纹（内容派生，见 :func:`public_key_thumbprint`）。
    """
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TypeError("签名私钥必须是 RSA 私钥")
    numbers = _rsa_public_numbers(public_pem)
    if private_key.private_numbers().public_numbers != numbers:
        raise ValueError(
            "IDENTITY_PRIVATE_KEY_PEM 与 IDENTITY_PUBLIC_KEY_PEM 不是同一对密钥："
            "签发方会用私钥签名，而 JWKS/验签方按公钥验签，令牌将全部失败。"
        )
    return SigningKey(
        kid=kid if kid is not None else _thumbprint_from_numbers(numbers.n, numbers.e),
        private_pem=private_pem,
        public_pem=public_pem,
    )
