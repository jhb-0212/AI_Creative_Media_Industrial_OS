"""身份签发领域层（services/identity_issuance）。

职责：承载「签发令牌」这一身份领域的核心能力，物理上与验签装配层
（services/identity_access）分离：

- KeyStore 单例（当前 + 历史签名密钥，持私钥）。
- issue_token：签发一条访问令牌（含私钥，签发方语义）。
- get_signing_key：取当前签发密钥（仅签发方使用）。

安全边界（KEYS-1 决策 2 / 决策 3）：
- 私钥只存在于本模块；验签方（services/identity_access）结构上只拿公钥。
- 本模块只被 identity 进程（workers/identity）与测试引用；API 进程禁止
  import 本模块（由 scripts/architecture_check.py 的 LAYER_RULES 强制，
  非靠人工自觉）。
- 本模块不落任何密钥到日志。
"""

from __future__ import annotations

from functools import lru_cache

from packages.identity.jwks import KeyStore
from packages.identity.keys import SigningKey, generate_signing_key, load_signing_key
from packages.shared.config import get_process_settings


@lru_cache
def get_key_store() -> KeyStore:
    """获取签名密钥注册表单例（验签按 kid 取公钥，签发按 kid 取私钥）。

    密钥来源：
    - 若配置了 identity_private_key_pem / identity_public_key_pem，从环境
      变量加载既有密钥（生产路径，密钥由密钥管理服务注入）。
    - 否则开发环境随机生成一把（仅用于本地/测试，重启后失效，验签方
      需经 JWKS 端点动态获取公钥，天然支持轮换）。

    kid 默认取固定值「current」，便于开发/测试环境稳定引用；生产轮换时
    追加新 kid，旧 kid 保留验签仍在有效期内的令牌。

    注意：本函数持私钥（签发方语义），仅身份签发服务被要求持有私钥，
    API/Relay 的进程清单不含该密钥，结构上杜绝私钥外泄到验签进程。
    """
    settings = get_process_settings("identity")
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
    capabilities: list[str] | None = None,
    organization_id: str | None = None,
) -> str:
    """签发一条访问令牌（身份服务内部使用，含私钥）。

    生产环境此函数应由独立 Authorization Service 调用，私钥不出该服务；
    此处仅作为身份服务的签发入口，供联调/测试与后续 Token 端点使用。

    capabilities：能力范围集合（caps claim）。契约中所有动作 ``token_required``
    均为 true，因此签发方必须显式授予能力范围；不传则签发空能力集，任何
    需 Capability 校验的动作都会被拒绝（fail-closed）。

    organization_id：组织归属（可选，org claim）——组织项目的资源级 ABAC
    判定键（基线 27.3 多层级作用域）。
    """
    from packages.identity.jwt_core import encode_token

    # 签发方语义：以 identity 进程角色读取配置（与 get_key_store 一致）。
    settings = get_process_settings("identity")
    return encode_token(
        get_key_store().current,
        subject_id=subject_id,
        tenant_id=tenant_id,
        roles=roles,
        capabilities=capabilities or [],
        organization_id=organization_id,
        issuer=settings.identity_issuer,
        audience=settings.identity_audience,
        ttl_seconds=settings.identity_access_token_ttl_seconds,
    )


def get_signing_key() -> SigningKey:
    """获取当前签发密钥（仅身份服务签发令牌时使用）。"""
    return get_key_store().current
