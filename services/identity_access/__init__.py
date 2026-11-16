"""身份服务装配层（services/identity_access）。

职责：把平台级身份基础设施（packages/identity）组装为可注入的运行时组件。
本模块刻意只做"装配"，不做判定——授权判定在 ``packages/identity/enforcement.py``。

**这里有一条硬边界（包 B4）**：

- ``get_public_key_store()``：**只读公钥**，来源是 ``IDENTITY_PUBLIC_KEY_PEM``
  或 ``IDENTITY_JWKS_FILE``。它不会、也不能退回"用签发密钥派生公钥"——
  那条回退正是旧实现里私钥进入 API 进程的入口。
- ``get_issuer()``：**只有签发进程**（``IDENTITY_PROCESS_ROLE=issuer``）调用，
  缺私钥即拒绝构造。API 进程启动时配置了私钥会**直接拒绝启动**
  （见 ``packages/shared/config.py`` 的进程角色校验与
  ``packages/identity/guards.py``）。

为什么公钥缺失时选择"拒绝启动"而不是"生成一把临时的"：临时密钥意味着重启后
所有既有令牌失效、多副本之间互不认可，且验签方拿到的公钥与签发方不匹配时
表现为"令牌被篡改"——一个必然发生、且排查方向完全错误的故障模式。
"""

from __future__ import annotations

from functools import lru_cache

from packages.identity.issuer import TokenIssuer
from packages.identity.jwks import PublicKeyStore
from packages.identity.keys import SigningKey
from packages.shared.config import get_settings


class IdentityConfigurationError(RuntimeError):
    """身份配置不满足最低安全要求（拒绝启动，不带病运行）。"""


@lru_cache
def get_public_key_store() -> PublicKeyStore:
    """获取纯公钥验签仓储（API 进程使用，结构上不含私钥）。

    来源优先级：``IDENTITY_PUBLIC_KEY_PEM`` → ``IDENTITY_JWKS_FILE``。
    两者都没配置时拒绝启动：此时任何"能起来"的实现都只能靠私钥派生公钥，
    而那等于把签发能力搬进验签进程。
    """
    settings = get_settings()
    public_pem = settings.identity_public_key_pem.get_secret_value().strip()
    if public_pem:
        # kid 固定为 current：签发端与验签端共用同一 kid 约定，
        # 轮换时由 JWKS 文档携带多个 kid（签发端追加、验签端整体替换）。
        return PublicKeyStore({"current": public_pem.encode("utf-8")})

    jwks_file = settings.identity_jwks_file.strip()
    if jwks_file:
        return PublicKeyStore.from_jwks_file(jwks_file)

    raise IdentityConfigurationError(
        "验签公钥缺失：必须配置 IDENTITY_PUBLIC_KEY_PEM 或 IDENTITY_JWKS_FILE。"
        "API 进程不允许用签发私钥派生公钥（B4：私钥不进 API 进程）。"
    )


@lru_cache
def get_issuer() -> TokenIssuer:
    """获取签发端（仅 ``IDENTITY_PROCESS_ROLE=issuer`` 的进程调用）。

    公钥由私钥派生，不再要求单独配置——两者不匹配会让验签全量失败，
    而错误现象是"令牌被篡改"，排查方向完全错。
    """
    settings = get_settings()
    private_pem = settings.identity_private_key_pem.get_secret_value().strip()
    if not private_pem:
        raise IdentityConfigurationError(
            "签发私钥未配置：本进程没有签发能力（这是 API 进程的正确状态）。"
        )
    return TokenIssuer.from_private_pem(
        private_pem.encode("utf-8"),
        kid="current",
        issuer=settings.identity_issuer,
        audience=settings.identity_audience,
        ttl_seconds=settings.identity_access_token_ttl_seconds,
    )


def issue_token(
    *,
    subject_id: str,
    tenant_id: str,
    roles: list[str],
    subject_type: str = "human",
    organization_id: str | None = None,
    project_ids: list[str] | None = None,
    signing_key: SigningKey | None = None,
) -> str:
    """签发一条访问令牌。

    ``signing_key`` 显式传入时用该密钥签发（测试注入：测试进程自己保管一把临时
    私钥，API 进程的配置里没有私钥）。不传时走 ``get_issuer()``，即要求本进程
    真的是签发进程——不存在"配置里有私钥就顺手签一下"的隐式路径。

    默认值刻意保守：主体默认人类、组织默认与租户相同、项目范围默认为空
    （一个项目也访问不了）。需要更宽范围必须显式声明，避免"忘了传"变成
    "默认全权"。
    """
    settings = get_settings()
    issuer = (
        TokenIssuer(
            signing_key=signing_key,
            issuer=settings.identity_issuer,
            audience=settings.identity_audience,
            ttl_seconds=settings.identity_access_token_ttl_seconds,
        )
        if signing_key is not None
        else get_issuer()
    )
    return issuer.issue(
        subject_id=subject_id,
        tenant_id=tenant_id,
        roles=roles,
        subject_type=subject_type,
        organization_id=organization_id,
        project_ids=project_ids,
    )
