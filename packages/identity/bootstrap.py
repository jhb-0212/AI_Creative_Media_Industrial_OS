"""开发/测试环境的签名密钥引导（KEYS-1）。

为什么需要它：验签方 ``services.identity_access.get_public_key_store`` 只从
``IDENTITY_PUBLIC_KEY_PEM`` 取公钥（不再从签发私钥派生），因此任何要加载运行时
应用（apps.api.main）的进程——测试（tests/conftest.py）与契约校验
（scripts/contract_check.py）——都必须**在 import app 之前**显式注入一对同源密钥，
使签发方（identity 角色读私钥+公钥）与验签方（读公钥）共享同一对密钥。

生产环境**不得**调用本函数：生产密钥由密钥管理服务注入环境变量，本函数若在
生产随机生成密钥，会导致签发方与验签方公钥失配、令牌全部验签失败，且破坏
JWKS 可信链条。调用方必须自行保证只在本函数用于开发/测试装配。

本函数幂等：仅当环境变量尚未提供公钥时才生成并注入，绝不覆盖外部显式注入的
密钥（例如 CI 里真实密钥注入、或 .env 已配置密钥）。
"""

from __future__ import annotations

import os

from packages.identity.keys import generate_signing_key


def bootstrap_signing_key_if_missing() -> None:
    """若环境未提供签名密钥，则生成一对并注入环境变量（幂等，开发/测试专用）。

    - 只在 ``IDENTITY_PUBLIC_KEY_PEM`` 缺失时生成；公钥已在则视为已注入，不动。
    - 同时写入 ``IDENTITY_PRIVATE_KEY_PEM``（签发方）与 ``IDENTITY_PUBLIC_KEY_PEM``
      （验签方），保证签发与验签同源。
    - 生产调用是错误用法：请在本函数外层以 app_env 守卫（调用方职责），或直接
      由密钥系统注入环境变量，让本函数因「公钥已存在」而空转。
    """
    if os.environ.get("IDENTITY_PUBLIC_KEY_PEM"):
        return
    key = generate_signing_key(kid="current")
    os.environ["IDENTITY_PRIVATE_KEY_PEM"] = key.private_pem.decode("utf-8")
    os.environ["IDENTITY_PUBLIC_KEY_PEM"] = key.public_pem.decode("utf-8")
