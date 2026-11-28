"""``scripts/verify_model_gateway.py`` 的行为测试：证据可信、失败可区分、口令不落证。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from adapters.model_gateway.ports import (
    GatewayArtifact,
    GatewayMisconfiguredError,
    GatewayRejectedError,
    GatewayRetryableError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_probe_module() -> object:
    path = REPO_ROOT / "scripts" / "verify_model_gateway.py"
    spec = importlib.util.spec_from_file_location("_verify_model_gateway", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def probe() -> object:
    return _load_probe_module()


class _StubGateway:
    def __init__(self, *, data: bytes = b"PNG-BYTES", media_type: str = "image/png") -> None:
        self._artifact = GatewayArtifact(
            data=data, media_type=media_type, upstream_request_id="req-1"
        )

    async def generate(self, request: Any) -> GatewayArtifact:
        return self._artifact


def test_success_evidence_carries_self_computed_digest(probe: object, tmp_path: Path) -> None:
    outcome = asyncio.run(
        probe.run_probe(
            _StubGateway(),
            prompt="p",
            model="m",
            media_type="image/png",
            save_path=tmp_path / "a.png",
        )
    )
    assert outcome.exit_code == 0
    evidence = outcome.evidence
    assert evidence["verdict"] == "OK"
    assert evidence["sha256"] == hashlib.sha256(b"PNG-BYTES").hexdigest()
    assert evidence["bytes"] == len(b"PNG-BYTES")
    assert evidence["upstream_request_id"] == "req-1"
    assert "api_key" not in json.dumps(evidence), "证据里不得出现凭据字段"
    assert (tmp_path / "a.png").read_bytes() == b"PNG-BYTES"


@pytest.mark.parametrize(
    ("error", "code", "verdict"),
    [
        (GatewayRejectedError("nope"), 3, "REJECTED"),
        (GatewayRetryableError("later"), 4, "RETRYABLE"),
        (GatewayMisconfiguredError("bad"), 2, "MISCONFIGURED"),
    ],
)
def test_failure_classes_map_to_distinct_exit_codes(
    probe: object, error: Exception, code: int, verdict: str
) -> None:
    class Failing:
        async def generate(self, request: Any) -> Any:
            raise error

    outcome = asyncio.run(probe.run_probe(Failing(), prompt="p", model="m", media_type="image/png"))
    assert outcome.exit_code == code
    assert outcome.evidence["verdict"] == verdict
    assert outcome.evidence["error"]


def test_missing_configuration_is_refused_before_any_request(probe: object) -> None:
    settings = SimpleNamespace(
        model_gateway_base_url="https://gw.example",
        model_gateway_api_key=SecretStr(""),
        model_gateway_default_model="m",
    )
    with pytest.raises(GatewayMisconfiguredError, match="MODEL_GATEWAY_API_KEY"):
        probe._require_config(settings)


def test_evidence_is_json_serialisable_and_key_free(tmp_path: Path, probe: object) -> None:
    evidence_path = tmp_path / "e.json"
    outcome = asyncio.run(
        probe.run_probe(_StubGateway(), prompt="p", model="m", media_type="image/png")
    )
    probe._emit(outcome.evidence | {"base_url_host": "gw.example"}, evidence_path)
    payload = evidence_path.read_text(encoding="utf-8")
    parsed = json.loads(payload)
    assert parsed["verdict"] == "OK"
    assert "Authorization" not in payload


def test_model_can_come_from_cli_when_default_model_is_unset(probe: object) -> None:
    """``--model`` 优先于 ``MODEL_GATEWAY_DEFAULT_MODEL``：临时换模型不该被迫改环境。"""
    settings = SimpleNamespace(
        model_gateway_base_url="https://gw.example",
        model_gateway_api_key=SecretStr("k"),
        model_gateway_default_model="",
    )
    base_url, key, model = probe._require_config(settings, model_override="m-cli")
    assert (base_url, key, model) == ("https://gw.example", "k", "m-cli")


def test_model_is_required_when_neither_cli_nor_default_provides_one(probe: object) -> None:
    settings = SimpleNamespace(
        model_gateway_base_url="https://gw.example",
        model_gateway_api_key=SecretStr("k"),
        model_gateway_default_model="",
    )
    with pytest.raises(GatewayMisconfiguredError, match="MODEL_GATEWAY_DEFAULT_MODEL"):
        probe._require_config(settings)
