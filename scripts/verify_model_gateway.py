"""真实模型网关取证（E5 第 1 段）：一次真实 ``POST {base}/images/generations`` 调用。

与 A1 的边界（必须先说清）
--------------------------
本脚本证明的是「协议 + 凭据 + 模型 id + 产物取回方式」这一侧成立，对应
``docs/runbooks/external-evidence-requirements.md`` 第一节第 1–5 项。它**不**建任务、
不写资产台账、不产生 ``asset_version_id``，因此**不能**代替 A1 全链 E2E——后者要走
generation worker 的 T0/T1 与 relay 的 T2/T3。

为什么要单独一条路径
--------------------
A1 失败时最常见的假象，是把「编排通了、对端不通」与「对端通了、登记不通」混成一个
FAILED。先把网关这一段单独钉住（一次调用、字节、摘要、上游 id、媒体类型），后面的
失败才能落到正确的层：是凭据/协议，还是台账/回执。

三条硬约束
----------
1. **口令不进证据**：只记录 host 与 model；``Authorization`` 与 api_key 任何形态都不落盘。
2. **失败要可区分**：可重试 / 已拒绝 / 配置矛盾走三个退出码，不合并成 1。
3. **产物必须可复核**：证据里带 ``sha256`` 与字节数，``--save`` 时把字节落盘，
   读回再算一遍摘要（只信自己算的）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from adapters.model_gateway.openai_compatible import OpenAICompatibleGateway
from adapters.model_gateway.ports import (
    GatewayArtifact,
    GatewayMisconfiguredError,
    GatewayRejectedError,
    GatewayRetryableError,
    GenerationRequest,
)
from packages.shared.config import get_settings
from packages.shared.storage_locator import extension_for_media_type

DEFAULT_PROMPT = "一只橘猫坐在窗台上，午后侧光，写实摄影风格"
EXIT_OK = 0
EXIT_MISCONFIGURED = 2
EXIT_REJECTED = 3
EXIT_RETRYABLE = 4
EXIT_UNEXPECTED = 5


@dataclass(frozen=True)
class ProbeOutcome:
    """一次取证的结论；``evidence`` 是可直接落盘的事实字典。"""

    exit_code: int
    evidence: dict[str, Any]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _host_of(base_url: str) -> str:
    """只留 host（含端口）：路径可能带租户/项目标识，不必进证据。"""
    parsed = urlparse(base_url)
    return parsed.netloc or ""


async def run_probe(
    gateway: Any,
    *,
    prompt: str,
    model: str,
    media_type: str,
    size: str = "",
    save_path: Path | None = None,
) -> ProbeOutcome:
    """调用一次网关，返回可落盘的证据；失败按三类映射退出码。"""
    request = GenerationRequest(
        task_id="gateway-probe",
        prompt=prompt,
        model=model,
        media_type=media_type,
        size=size,
    )
    started = _now()
    try:
        artifact: GatewayArtifact = await gateway.generate(request)
    except GatewayMisconfiguredError as exc:
        return ProbeOutcome(
            exit_code=EXIT_MISCONFIGURED,
            evidence={"verdict": "MISCONFIGURED", "error": str(exc), "started_at": started},
        )
    except GatewayRejectedError as exc:
        return ProbeOutcome(
            exit_code=EXIT_REJECTED,
            evidence={"verdict": "REJECTED", "error": str(exc), "started_at": started},
        )
    except GatewayRetryableError as exc:
        return ProbeOutcome(
            exit_code=EXIT_RETRYABLE,
            evidence={"verdict": "RETRYABLE", "error": str(exc), "started_at": started},
        )
    except Exception as exc:  # 未映射异常：证据里留类型名，便于定位适配器缺口
        return ProbeOutcome(
            exit_code=EXIT_UNEXPECTED,
            evidence={
                "verdict": "UNEXPECTED",
                "error": f"{type(exc).__name__}: {exc}",
                "started_at": started,
            },
        )

    digest = hashlib.sha256(artifact.data).hexdigest()
    saved: str | None = None
    if save_path is not None:
        # 落盘是阻塞 I/O：放进线程，避免取证脚本在事件循环里卡住其他协程。
        await asyncio.to_thread(save_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(save_path.write_bytes, artifact.data)
        # 读回再算：只信自己算的摘要，不信「刚写进去应该没错」。
        reread = hashlib.sha256(await asyncio.to_thread(save_path.read_bytes)).hexdigest()
        if reread != digest:
            return ProbeOutcome(
                exit_code=EXIT_UNEXPECTED,
                evidence={
                    "verdict": "SAVE_MISMATCH",
                    "error": f"落盘读回摘要 {reread} 与响应字节 {digest} 不一致",
                    "started_at": started,
                },
            )
        saved = str(save_path)

    return ProbeOutcome(
        exit_code=EXIT_OK,
        evidence={
            "verdict": "OK",
            "started_at": started,
            "finished_at": _now(),
            "model": model,
            "media_type": artifact.media_type,
            "bytes": len(artifact.data),
            "sha256": digest,
            "upstream_request_id": artifact.upstream_request_id,
            "declared_sha256": artifact.declared_sha256,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "saved_path": saved,
        },
    )


def _require_config(settings: Any, *, model_override: str = "") -> tuple[str, str, str]:
    """取 base_url / api_key / model；缺任何一项都拒绝发请求（不消耗配额）。

    ``model_override``（``--model``）优先于 ``MODEL_GATEWAY_DEFAULT_MODEL``：临时换一个
    模型做取证时，不必为了一个默认值去改环境变量。
    """
    base_url = (getattr(settings, "model_gateway_base_url", "") or "").strip()
    key = (
        settings.model_gateway_api_key.get_secret_value() if settings.model_gateway_api_key else ""
    )
    model = (
        model_override.strip()
        or (getattr(settings, "model_gateway_default_model", "") or "").strip()
    )
    missing = [
        name
        for name, value in (
            ("MODEL_GATEWAY_BASE_URL", base_url),
            ("MODEL_GATEWAY_API_KEY", key),
            ("MODEL_GATEWAY_DEFAULT_MODEL", model),
        )
        if not value
    ]
    if missing:
        raise GatewayMisconfiguredError(
            "缺少网关配置：" + "、".join(missing) + "（留空等于不接真网关，本脚本不猜默认值）"
        )
    return base_url, key, model


def _build_gateway(settings: Any, base_url: str, key: str) -> Any:
    protocol = getattr(settings, "model_gateway_protocol", "openai_compatible")
    if protocol != "openai_compatible":
        raise ValueError(
            f"MODEL_GATEWAY_PROTOCOL={protocol!r} 无对应适配器（已实现: openai_compatible）"
        )
    return OpenAICompatibleGateway(
        base_url=base_url,
        api_key=key,
        default_model=getattr(settings, "model_gateway_default_model", ""),
        timeout_seconds=getattr(settings, "model_gateway_timeout_seconds", 120.0),
        max_bytes=getattr(settings, "model_gateway_max_artifact_bytes", 256 * 1024 * 1024),
        response_format=getattr(settings, "model_gateway_response_format", "b64_json"),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真实模型网关取证（一次调用）")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--model", default="", help="覆盖 MODEL_GATEWAY_DEFAULT_MODEL")
    parser.add_argument("--size", default="", help="如 1024x1024；留空则不传")
    parser.add_argument("--media-type", default="image/png")
    parser.add_argument("--save", type=Path, default=None, help="产物落盘路径（含扩展名）")
    parser.add_argument("--evidence", type=Path, default=None, help="证据 JSON 落盘路径")
    return parser.parse_args(argv)


async def _run(argv: list[str] | None) -> int:
    args = _parse_args(argv)
    settings = get_settings()

    try:
        base_url, key, model = _require_config(settings, model_override=args.model)
    except GatewayMisconfiguredError as exc:
        evidence = {"verdict": "MISCONFIGURED", "error": str(exc), "started_at": _now()}
        _emit(evidence, args.evidence)
        return EXIT_MISCONFIGURED

    gateway = _build_gateway(settings, base_url, key)
    try:
        outcome = await run_probe(
            gateway,
            prompt=args.prompt,
            model=model,
            media_type=args.media_type,
            size=args.size,
            save_path=args.save or _default_save_path(args.media_type),
        )
    finally:
        close = getattr(gateway, "aclose", None)
        if close is not None:
            await close()

    evidence = outcome.evidence | {"base_url_host": _host_of(base_url)}
    _emit(evidence, args.evidence)
    return outcome.exit_code


def _default_save_path(media_type: str) -> Path | None:
    extension = extension_for_media_type(media_type)
    if extension is None:
        return None
    return Path("gateway-probe") / f"artifact.{extension}"


def _emit(evidence: dict[str, Any], path: Path | None) -> None:
    payload = json.dumps(evidence, ensure_ascii=False, indent=2)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
    print(payload)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(argv))


if __name__ == "__main__":
    sys.exit(main())
