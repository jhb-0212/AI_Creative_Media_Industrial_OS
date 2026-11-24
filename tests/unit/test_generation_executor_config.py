"""装配口径测试：配置的四种状态必须各自给出明确结果。

这类测试防的不是「功能不对」，而是「功能看起来对」：

- 配了地址但缺 KEY，若静默回落到未配置执行器，运维会去查网关而不是查本机配置；
- ``STORAGE_BACKEND=s3`` 若静默回落本地，产物位置会变成一个谜；
- ``MODEL_GATEWAY_PROTOCOL`` 写了未实现的值若被忽略，日志里看不出协议没生效。

因此每一条都用 ``pytest.raises`` 或类型断言锁定，而不是「跑起来没报错」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from adapters.model_gateway.openai_compatible import OpenAICompatibleGateway
from adapters.model_gateway.ports import GatewayMisconfiguredError
from workers.generation.__main__ import UnconfiguredExecutor, _build_executor
from workers.generation.executor import StorageBackedGenerationExecutor


def _wire_executor(settings: SimpleNamespace):
    """配置测试不连库：登记端口只做装配形状校验，传哨兵会话工厂即可。"""
    return _build_executor(settings, object())


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "model_gateway_base_url": "https://gateway.example.com/v1",
        "model_gateway_api_key": SecretStr("sk-test"),
        "model_gateway_protocol": "openai_compatible",
        "model_gateway_default_model": "image-model-1",
        "model_gateway_timeout_seconds": 30.0,
        "model_gateway_response_format": "b64_json",
        "model_gateway_max_artifact_bytes": 1024,
        "storage_backend": "local",
        "storage_local_root": ".",
        "storage_max_object_bytes": 1024,
        "generation_default_media_type": "image/png",
        "generation_default_size": "1024x1024",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_no_base_url_uses_honest_failure_executor() -> None:
    executor, gateway = _wire_executor(_settings(model_gateway_base_url=""))
    assert isinstance(executor, UnconfiguredExecutor)
    assert gateway is None


def test_full_config_wires_real_executor() -> None:
    executor, gateway = _wire_executor(_settings())
    assert isinstance(executor, StorageBackedGenerationExecutor)
    assert isinstance(gateway, OpenAICompatibleGateway)


def test_missing_api_key_is_a_config_conflict_not_a_silent_fallback() -> None:
    """缺 KEY 不降级为 UnconfiguredExecutor：那会让配置错误伪装成网关故障。"""
    with pytest.raises(GatewayMisconfiguredError):
        _wire_executor(_settings(model_gateway_api_key=SecretStr("")))


def test_invalid_base_url_is_rejected() -> None:
    with pytest.raises(GatewayMisconfiguredError):
        _wire_executor(_settings(model_gateway_base_url="ftp://gateway/v1"))


def test_missing_storage_root_is_rejected() -> None:
    with pytest.raises(ValueError, match="STORAGE_LOCAL_ROOT"):
        _wire_executor(_settings(storage_local_root=""))


def test_unimplemented_protocol_is_rejected() -> None:
    with pytest.raises(ValueError, match="MODEL_GATEWAY_PROTOCOL"):
        _wire_executor(_settings(model_gateway_protocol="bogus_protocol"))


def test_s3_backend_is_rejected_rather_than_silently_local(tmp_path) -> None:
    """S3 配置项存在但适配器未实现：必须报错，不能悄悄存到本地目录。"""
    with pytest.raises(ValueError, match="STORAGE_BACKEND"):
        _wire_executor(_settings(storage_backend="s3", storage_local_root=str(tmp_path)))


async def test_gateway_is_closable(tmp_path) -> None:
    """装配出来的网关必须能被关闭，否则 Worker 退出时连接池泄漏。"""
    _executor, gateway = _wire_executor(
        _settings(storage_local_root=str(tmp_path / "objects"))
    )
    assert gateway is not None
    await gateway.aclose()
