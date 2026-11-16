"""测试包入口：在任何应用模块被导入之前固定"密钥边界"。

为什么这段代码必须放在 ``__init__.py`` 而不是 ``conftest.py``：

- 包初始化一定早于包内任何模块（含 conftest）的导入，因此这里设置的进程环境
  变量、生成的测试密钥，对**所有** import 顺序都生效。
- 如果放在 conftest 里，就得保证"设置环境变量"的语句排在
  ``from apps.api.main import app`` 之前——一旦有人调整 import 顺序（或工具自动
  排序），设置会晚于应用导入，测试会用上"没有验签公钥"的配置而全量失败，
  且失败原因看起来与 B4 无关。

这里做两件事（对应包 B4 的形态要求）：

1. **API 侧只给公钥**：``IDENTITY_PUBLIC_KEY_PEM`` 写入环境变量，私钥绝不写入。
   ``IDENTITY_PROCESS_ROLE=api`` 时若配置里有私钥，配置校验会直接拒绝启动——
   测试环境也遵守这条，否则测试证明的部署形态与生产不一致。
2. **私钥留在测试进程内存里**：``TEST_SIGNING_KEY`` 由
   ``issue_token(signing_key=...)`` 显式使用。签发能力来自"显式传给签发函数"，
   而不是"进程配置里有"。
"""

from __future__ import annotations

import os

from packages.identity.keys import generate_signing_key

#: 测试签发密钥：只在测试进程内存在，不进入 Settings / 环境变量。
TEST_SIGNING_KEY = generate_signing_key(kid="current")

os.environ["IDENTITY_PUBLIC_KEY_PEM"] = TEST_SIGNING_KEY.public_pem.decode("utf-8")
os.environ.setdefault("IDENTITY_PROCESS_ROLE", "api")
# 防御：外部 shell 若残留私钥变量，会让 API 进程的拒绝启动逻辑误伤测试。
os.environ.pop("IDENTITY_PRIVATE_KEY_PEM", None)
