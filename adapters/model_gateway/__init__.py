"""模型网关适配器包（端口 + 自研兼容协议实现 + 确定性假网关）。

当前只实现 ``openai_compatible``：同步一次请求拿到字节。协议选择仍由
``MODEL_GATEWAY_PROTOCOL`` 控制，未知值在装配期 fail-closed（不静默降级）。

包初始化零加载：见 ``adapters/__init__.py`` 的说明。
"""

__all__: list[str] = []
