"""令牌签发端（全仓唯一持有私钥的组件）。

为什么单独成模块（包 B4）：签发私钥此前是"全局配置 + 全局 KeyStore"，
于是 API 进程只要配置里有 ``IDENTITY_PRIVATE_KEY_PEM`` 就能签发令牌——而生产
配置校验还把私钥列为**必填**。结果是"签发与验签分离"只写在文档里：

- 任何 dump 到 API 进程内存的人都能签发任意主体的令牌（包括 CRITICAL 动作）；
- 私钥轮换要求重启 API，验签方无法在不停机的情况下跟上；
- 一旦 API 被攻破，攻击面从"读数据"扩大到"伪造身份"。

本模块把签发能力收敛为一个显式对象：**必须传入私钥才能构造**，没有全局单例、
没有"配置里有就用"的隐式路径。API 进程里不构造它，也就没有签发能力。

配套约束：
- ``IDENTITY_PROCESS_ROLE=api``（默认）时，配置出现私钥即拒绝启动。
- ``IDENTITY_PROCESS_ROLE=issuer`` 的进程只签发，不需要公钥（公钥由私钥派生，
  并经 JWKS 文档对外分发）。
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.identity.jwt_core import encode_token
from packages.identity.keys import SigningKey, load_signing_key, public_pem_from_private
from packages.identity.principal import SubjectType


@dataclass(frozen=True)
class TokenIssuer:
    """签发端（持私钥）。字段与 ``encode_token`` 的策略参数一一对应。"""

    signing_key: SigningKey
    issuer: str
    audience: str
    ttl_seconds: int = 900

    @classmethod
    def from_private_pem(
        cls,
        private_pem: bytes,
        *,
        kid: str,
        issuer: str,
        audience: str,
        ttl_seconds: int = 900,
    ) -> TokenIssuer:
        """从私钥 PEM 构造签发端（公钥由私钥派生，不需要单独配置）。

        单独配置公钥的旧做法有个隐蔽缺陷：两者不匹配时验签全量失败，
        而失败原因看起来像"令牌被篡改"。派生公钥让不匹配在结构上不可能发生。
        """
        public_pem = public_pem_from_private(private_pem)
        return cls(
            signing_key=load_signing_key(private_pem, public_pem, kid),
            issuer=issuer,
            audience=audience,
            ttl_seconds=ttl_seconds,
        )

    @property
    def kid(self) -> str:
        return self.signing_key.kid

    @property
    def public_jwks(self) -> dict[str, object]:
        """签发端自己的 JWKS 视图（供身份服务对外发布公钥）。"""
        return {"keys": [self.signing_key.public_jwk]}

    def issue(
        self,
        *,
        subject_id: str,
        tenant_id: str,
        roles: list[str],
        subject_type: str = SubjectType.HUMAN.value,
        organization_id: str | None = None,
        project_ids: list[str] | None = None,
    ) -> str:
        """签发一条访问令牌。"""
        return encode_token(
            self.signing_key,
            subject_id=subject_id,
            tenant_id=tenant_id,
            roles=roles,
            issuer=self.issuer,
            audience=self.audience,
            ttl_seconds=self.ttl_seconds,
            subject_type=subject_type,
            organization_id=organization_id,
            project_ids=project_ids,
        )


__all__ = ["TokenIssuer"]
