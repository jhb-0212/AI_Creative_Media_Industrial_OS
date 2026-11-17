"""身份验签装配层（services/identity_access）。

职责：把平台级身份基础设施（packages/identity）组装为可注入的验签运行时组件：
- 纯公钥验签仓储 PublicKeyStore（kid → 公钥 PEM，结构上无私钥）。
- FastAPI 认证依赖工厂（Bearer → Principal）。
- JWKS 端点（对外暴露公钥，供网关/其它服务验签）。

安全边界（决策 4 / KEYS-1）：
- 本模块是**验签方**装配层，只依赖公钥，结构上不可能持有私钥。
- 签发能力（get_key_store/issue_token/get_signing_key）已迁至
  services/identity_issuance，本模块不 import 私钥路径（唯一例外：
  ``get_public_key_store`` 的开发分支经 ``from_key_store`` 派生公钥，
  该桥在 KEYS-1 S2 废弃，见其 docstring）。
- 本模块不落任何密钥到日志。
"""

from __future__ import annotations

from functools import lru_cache

from packages.identity.jwks import PublicKeyStore
from packages.shared.config import get_settings


@lru_cache
def get_public_key_store() -> PublicKeyStore:
    """获取纯公钥验签仓储（API 进程使用，结构上不含私钥）。

    只从 ``IDENTITY_PUBLIC_KEY_PEM`` 构建，签发私钥不出身份服务。验签方与
    签发方（services/identity_issuance）**结构上零依赖**——本模块不 import
    签发模块，也不从签发密钥仓储派生公钥（KEYS-1：消除「API 经私钥派生公钥」
    的越界，验签方只认显式注入的公钥）。

    无公钥配置时拒绝启动（fail-closed）：验签方绝不允许在无权威公钥来源时
    静默构造一个「本进程随机生成、签发方不知道」的公钥仓储——那会签发方与
    验签方公钥失配，令牌全部验签失败。开发/测试环境的同源密钥由测试装配层
    （tests/conftest.py）在进程启动前显式注入 ``IDENTITY_PUBLIC_KEY_PEM``。
    """
    settings = get_settings()
    public_pem = settings.identity_public_key_pem.get_secret_value()
    if not public_pem:
        raise RuntimeError(
            "验签方缺少公钥：必须配置 IDENTITY_PUBLIC_KEY_PEM（API 进程只持公钥"
            "验签，不得从签发私钥派生）。开发/测试由测试装配层显式注入同源公钥。"
        )
    return PublicKeyStore({"current": public_pem.encode("utf-8")})
