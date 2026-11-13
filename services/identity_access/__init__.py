"""身份服务装配层（services/identity_access）。

职责：把平台级身份基础设施（packages/identity）组装为可注入的运行时组件：
- KeyStore 单例（当前 + 历史签名密钥，验签按 kid 取公钥）。
- FastAPI 认证依赖工厂（Bearer → Principal）。
- JWKS 端点（对外暴露公钥，供网关/其它服务验签）。

安全边界（决策 4）：
- 私钥仅用于签发令牌（身份服务持有），API 层只拿公钥验签。
- 本模块不落任何密钥到日志。
"""

from __future__ import annotations

from functools import lru_cache

from packages.identity.jwks import KeyStore
from packages.identity.keys import SigningKey, generate_signing_key, load_signing_key
from packages.shared.config import get_settings


@lru_cache
def get_key_store() -> KeyStore:
    """获取签名密钥注册表单例（验签按 kid 取公钥）。

    密钥来源：
    - 若配置了 identity_private_key_pem / identity_public_key_pem，从环境
      变量加载既有密钥（生产路径，密钥由密钥管理服务注入）。
    - 否则开发环境随机生成一把（仅用于本地/测试，重启后失效，验签方
      需经 JWKS 端点动态获取公钥，天然支持轮换）。

    kid 默认取固定值「current」，便于开发/测试环境稳定引用；生产轮换时
    追加新 kid，旧 kid 保留验签仍在有效期内的令牌。
    """
    settings = get_settings()
    private_pem = settings.identity_private_key_pem.get_secret_value()
    public_pem = settings.identity_public_key_pem.get_secret_value()

    if private_pem and public_pem:
        key = load_signing_key(
            private_pem.encode("utf-8"),
            public_pem.encode("utf-8"),
            kid="current",
        )
        return KeyStore([key])

    # 生产环境绝不随机生成：临时密钥会导致签发方与验签方公钥不匹配、
    # 进程重启即失效，且破坏 JWKS 的可信链条。配置缺失时直接拒绝启动。
    if settings.app_env == "production":
        raise RuntimeError(
            "生产环境（APP_ENV=production）必须配置 IDENTITY_PRIVATE_KEY_PEM 与 "
            "IDENTITY_PUBLIC_KEY_PEM；禁止使用进程内随机生成的临时签名密钥。"
        )
    # 开发/测试：随机生成一把（不落日志，进程内持有）。
    key = generate_signing_key(kid="current")
    return KeyStore([key])


def issue_token(
    *,
    subject_id: str,
    tenant_id: str,
    roles: list[str],
) -> str:
    """签发一条访问令牌（身份服务内部使用，含私钥）。

    生产环境此函数应由独立 Authorization Service 调用，私钥不出该服务；
    此处仅作为身份服务的签发入口，供联调/测试与后续 Token 端点使用。
    """
    from packages.identity.jwt_core import encode_token
    from packages.shared.config import get_settings

    settings = get_settings()
    return encode_token(
        get_key_store().current,
        subject_id=subject_id,
        tenant_id=tenant_id,
        roles=roles,
        issuer=settings.identity_issuer,
        audience=settings.identity_audience,
        ttl_seconds=settings.identity_access_token_ttl_seconds,
    )


def get_signing_key() -> SigningKey:
    """获取当前签发密钥（仅身份服务签发令牌时使用）。"""
    return get_key_store().current
