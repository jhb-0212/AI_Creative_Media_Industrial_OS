"""``python -m workers.identity`` 进程入口（KEYS-1 S3）。

与 relay 同构：同镜像、不同 entrypoint，独立进程生命周期。区别在于
identity 需要监听 HTTP（JWKS 公钥分发是拉模型，验签方要能随时回源），
因此这里用 uvicorn 起 ``workers.identity.app:app``，并显式绑定
``identity_host`` / ``identity_port``（与 API 的 8000 分开）。

为什么不在模块顶层启动 uvicorn：入口必须可被测试 import（跑 JWKS 契约断言、
做镜像冒烟），进程行为（监听端口）只发生在 ``main()``/``__main__``。
"""

from __future__ import annotations

import sys

import uvicorn

from packages.shared.config import get_process_settings
from packages.shared.logging import get_logger, setup_logging


def main() -> int:
    """启动身份进程（持私钥，提供 JWKS）。"""
    settings = get_process_settings("identity")
    setup_logging(settings.log_level)
    logger = get_logger("workers.identity")
    logger.info(
        "identity_process_starting",
        host=settings.identity_host,
        port=settings.identity_port,
    )
    uvicorn.run(
        "workers.identity.app:app",
        host=settings.identity_host,
        port=settings.identity_port,
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
