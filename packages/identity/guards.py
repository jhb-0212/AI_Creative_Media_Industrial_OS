"""进程角色守卫（包 B4：签发私钥不得进入 API 进程）。

为什么要一个显式的守卫函数，而不是只靠配置校验：

1. **配置校验回答"配置文件对不对"，守卫回答"这个进程该不该有签发能力"。**
   两者失败时的信息完全不同：前者是"字段填错"，后者是"部署形态错了"。
   API 容器挂错了 secret 时，报错必须指向后者，否则运维会去改字段而不是拔挂载。
2. **它是可测试的。** "API 进程出现私钥即拒绝启动"如果能被一条单测触发，
   这条要求就不会在下次重构里悄悄消失。配置校验类逻辑通常难测（要重建 Settings），
   守卫接受显式参数，测试可以直接喂一个带私钥的配置。

调用点：``apps/api/main.py`` 的 ``lifespan`` 启动段（fail-closed：进程起不来，
不等到第一个请求）。放在 lifespan 而不是模块导入时，是因为导入期检查会误伤
"只需要路由表"的用法（OpenAPI 导出、契约检查、路由单测）——它们因为一个与
自身无关的密钥配置而失败，排查成本高而收益为零。
"""

from __future__ import annotations

from packages.shared.config import Settings


class SigningKeyPresentInApiProcessError(RuntimeError):
    """API 进程检测到签发私钥（红线：私钥不出签发进程）。"""


def assert_no_signing_key_in_api_process(settings: Settings) -> None:
    """API 进程里出现签发私钥 → 拒绝启动。

    错误信息刻意写明"要怎么修"（拔掉 secret 挂载 / 改为 PUBLIC_KEY），
    因为这类故障几乎总是部署配置错误，而不是代码缺陷。
    """
    private_pem = settings.identity_private_key_pem.get_secret_value().strip()
    if not private_pem:
        return
    raise SigningKeyPresentInApiProcessError(
        "API 进程检测到签发私钥（IDENTITY_PRIVATE_KEY_PEM）：拒绝启动。"
        "签发能力只属于 IDENTITY_PROCESS_ROLE=issuer 的进程；"
        "API 进程请改为挂载公钥（IDENTITY_PUBLIC_KEY_PEM 或 IDENTITY_JWKS_FILE），"
        "并移除私钥挂载。"
    )


__all__ = [
    "SigningKeyPresentInApiProcessError",
    "assert_no_signing_key_in_api_process",
]
