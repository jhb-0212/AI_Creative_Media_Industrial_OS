"""确定性的假模型网关：让「没有真网关」不等于「什么都验不了」。

它解决的具体问题
----------------
真网关未接入时，生成链路的所有下游（执行器编排、产物落盘、QC 入队、结算）
都可以验，唯独缺一份字节。过去的选择是「整个链路等真网关」，于是 P1-01 的
短事务、P1-02 的产物契约、QC 的归属核验全都没有端到端覆盖。

**产物确定性**是这里的关键约束：同一个 prompt/model 必须每次生成同一份字节。
否则同一任务重跑会产生不同 sha256，而「幂等」这件事就无从断言——
内容寻址存储的重放语义也会退化成「每次都新增一份」。

它不能被当成证据
----------------
假网关产出的字节与真实模型无关，因此：

- 可以证明「链路能跑通」（A1 的骨架）；
- **不能**证明「模型能出片」（A1 的实质），也不能用于性能与成本结论。

``evidence_ref`` 里会带 ``fake-gateway`` 前缀，让台账一眼看出这条任务不是真实
生成（见 ``workers/generation/executor.py``）。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Literal

from adapters.model_gateway.ports import (
    GatewayArtifact,
    GatewayMisconfiguredError,
    GatewayRejectedError,
    GatewayRetryableError,
    GenerationRequest,
)

FakeFailureMode = Literal[
    "timeout", "server_error", "rejected", "oversize", "misconfigured"
]

# 合法 PNG 签名 + 固定块：让产物看起来是图片，便于人工排查时直接打开。
_PNG_HEAD = b"\x89PNG\r\n\x1a\n"


def deterministic_png(seed: str, *, size_bytes: int = 4096) -> bytes:
    """由 seed 派生确定性伪 PNG：同 seed 同字节，不同 seed 不同字节。"""
    if size_bytes < len(_PNG_HEAD):
        raise ValueError("size_bytes 至少覆盖 PNG 签名长度")
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    body = bytearray()
    counter = 0
    while len(_PNG_HEAD) + len(body) < size_bytes:
        body.extend(hashlib.sha256(digest + counter.to_bytes(4, "big")).digest())
        counter += 1
    return _PNG_HEAD + bytes(body[: size_bytes - len(_PNG_HEAD)])


class FakeModelGateway:
    """按 seed 确定性产出，可选注入一种故障。"""

    def __init__(
        self,
        *,
        fail_mode: FakeFailureMode | None = None,
        size_bytes: int = 4096,
        delay_seconds: float = 0.0,
    ) -> None:
        self._fail_mode = fail_mode
        self._size_bytes = size_bytes
        self._delay = delay_seconds
        self.calls: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GatewayArtifact:
        # 记录调用：测试要断言「重试了几次」「用了哪个 model」这类事实，
        # 而不是靠日志字符串匹配。
        self.calls.append(request)
        if self._delay:
            await asyncio.sleep(self._delay)

        mode = self._fail_mode
        if mode == "timeout":
            raise GatewayRetryableError(f"假网关：超时（task={request.task_id}）")
        if mode == "server_error":
            raise GatewayRetryableError(f"假网关：503（task={request.task_id}）")
        if mode == "rejected":
            raise GatewayRejectedError(
                f"假网关：内容被拒（task={request.task_id}）"
            )
        if mode == "oversize":
            raise GatewayRejectedError(
                f"假网关：产物超出上限（task={request.task_id}）"
            )
        if mode == "misconfigured":
            raise GatewayMisconfiguredError("假网关：模拟配置矛盾")

        data = deterministic_png(
            f"{request.model}|{request.prompt}|{request.size}", size_bytes=self._size_bytes
        )
        return GatewayArtifact(
            data=data,
            media_type=request.media_type,
            declared_sha256=hashlib.sha256(data).hexdigest(),
            upstream_request_id=f"fake-{hashlib.sha256(request.task_id.encode()).hexdigest()[:12]}",
        )
