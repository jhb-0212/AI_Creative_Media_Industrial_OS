"""把「模型网关 + 受控存储」编排成一个 ``GenerationExecutor``。

为什么编排放在 ``workers/`` 而不是 ``adapters/``
------------------------------------------------
``adapters`` 只允许依赖 ``packages``（ADR-R14）。而「这次生成算不算成功」是一个
业务判定：它要求产物已落地、已算摘要、已登记资产版本。把这个判定放进适配器，
等于让「外部系统适配层」决定资产台账里有什么。因此适配器只做翻译
（协议 ↔ 字节），编排与判定留在 Worker 层——它是唯一同时持有租约与终态的组件。

三段式，每一段失败都给出可区分的证据
------------------------------------
    解析提示词 → 调网关 → 落存储 → 登记资产版本

前三段本轮已接线（真网关到位即可用），第四段必须**注入**：资产登记需要系统
主体、按任务租户切换 RLS 上下文、以及跨域事务语义，三者都还没有裁定
（P2-01 范围）。没有注入时不宣称成功——见 ``_finish`` 的注释：
无 ``asset_version_id`` 的 SUCCEEDED 会被降级为 FAILED，这是 P1-02 定下的
fail-closed 行为，不是本模块的缺陷。

为什么``prompt_ref`` 不能被当作提示词正文
------------------------------------------
它是 ≤200 字符的**引用**（``generation_tasks.prompt_ref``），指向提示词库里的
一条记录。仓库里还没有提示词库的解析实现。若直接把 ref 当 prompt 发给网关，
结果是：网关收到一串 id，返回一张与意图无关的图，链路全程无错、落库成功、
状态 SUCCEEDED。**产物看起来合法、内容完全错误**——这是最坏的一种失败，
因为它不产生任何信号。因此解析器必须显式提供，且只承认声明式内联形态。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from adapters.model_gateway.ports import (
    GatewayMisconfiguredError,
    GatewayRejectedError,
    GatewayRetryableError,
    GenerationRequest,
    ModelGatewayPort,
)
from adapters.storage.ports import (
    ObjectStoragePort,
    StorageError,
    StoredObject,
)
from packages.shared.logging import get_logger
from workers.generation.worker import GenerationOutcome

logger = get_logger(__name__)

# 内联提示词的显式前缀：``inline:<正文>``。
#
# 用前缀而不是「看起来像提示词就用」，是为了让「引用」与「正文」在语法上
# 可区分。没有这个区分时，一个有 bug 的调用方传了 id，系统会安静地把 id
# 当提示词生成一堆垃圾。
INLINE_PROMPT_PREFIX = "inline:"

_EVIDENCE_MAX = 200


class PromptUnresolvableError(Exception):
    """``prompt_ref`` 无法解析成提示词正文。"""


class AssetRegistrationUnavailableError(Exception):
    """资产登记端口未接线：生成产物无法进入资产台账。"""


PromptResolver = Callable[[str], Awaitable[str]]


async def resolve_inline_prompt(prompt_ref: str) -> str:
    """只承认 ``inline:<正文>``，其余一律拒绝。

    真实提示词库接入后，应在此之外增加一个「按引用查库」的实现并注入；
    本函数保留给 E2E 与本地联调（``prompt_ref`` 上限 200 字符，够放一条
    短提示词，不够放完整生产提示词）。
    """
    ref = (prompt_ref or "").strip()
    if ref.startswith(INLINE_PROMPT_PREFIX):
        text = ref[len(INLINE_PROMPT_PREFIX) :].strip()
        if text:
            return text
    raise PromptUnresolvableError(
        f"prompt_ref={ref!r} 无法解析为提示词正文："
        f"只支持 {INLINE_PROMPT_PREFIX}<正文> 形态，或由提示词库解析器接管"
    )


@dataclass(frozen=True)
class GeneratedArtifact:
    """已落地的产物：此后所有引用都走 ``locator``。"""

    stored: StoredObject
    upstream_request_id: str = ""


class AssetRegistrar(Protocol):
    """把产物登记成一个资产版本，返回 ``(asset_version_id, quality_profile_version)``。

    这个端口之所以在 Worker 侧而不是适配器侧：登记要经过资产域服务层
    （``AssetVersion`` 的不变量、幂等、归属校验都在那里），而适配器不得
    依赖 services。
    """

    async def register(
        self, task: Any, artifact: GeneratedArtifact
    ) -> tuple[str, str]: ...


class UnwiredAssetRegistrar:
    """未接线时的占位：明确拒绝，而不是返回空 id 让下游「看起来成功」。"""

    async def register(self, task: Any, artifact: GeneratedArtifact) -> tuple[str, str]:
        raise AssetRegistrationUnavailableError(
            f"资产登记端口未接线（task={getattr(task, 'entity_id', '?')}，"
            f"产物={artifact.stored.locator}）："
            "系统主体与跨域事务尚未裁定，见 P2-01"
        )


class StorageBackedGenerationExecutor:
    """解析提示词 → 调网关 → 落受控存储 → 交资产登记。"""

    def __init__(
        self,
        *,
        gateway: ModelGatewayPort,
        storage: ObjectStoragePort,
        resolve_prompt: PromptResolver,
        registrar: AssetRegistrar,
        default_model: str = "",
        media_type: str = "image/png",
        size: str = "",
    ) -> None:
        self._gateway = gateway
        self._storage = storage
        self._resolve_prompt = resolve_prompt
        self._registrar = registrar
        self._default_model = default_model
        # 产物类型理论上应由生产路由（``production_route_ref``）决定，但路由
        # 解析尚未建模，先用配置默认值。这一点写进 external-evidence 交接单的
        # 「已知缺口」，不让它悄悄变成一个恒为 png 的假设。
        self._media_type = media_type
        self._size = size

    async def execute(self, task: Any) -> GenerationOutcome:
        task_id = getattr(task, "entity_id", "?")

        try:
            prompt = await self._resolve_prompt(getattr(task, "prompt_ref", ""))
        except PromptUnresolvableError as exc:
            logger.error("prompt unresolved for task %s: %s", task_id, exc)
            return _failed("prompt-unresolvable", str(exc))

        request = GenerationRequest(
            task_id=task_id,
            prompt=prompt,
            model=getattr(task, "model_adapter_ref", "") or self._default_model,
            media_type=self._media_type,
            size=self._size,
        )

        try:
            artifact_bytes = await self._gateway.generate(request)
        except GatewayRetryableError as exc:
            # 编码成 retryable：调度侧据此判断「值得再跑一次」，而不是把它
            # 与内容拒绝混成同一种终态。
            logger.warning("gateway retryable failure for task %s: %s", task_id, exc)
            return _failed("gateway-retryable", str(exc))
        except GatewayRejectedError as exc:
            logger.error("gateway rejected task %s: %s", task_id, exc)
            return _failed("gateway-rejected", str(exc))
        except GatewayMisconfiguredError as exc:
            # 配置矛盾不是任务的问题：把它记进任务终态会掩盖部署故障。
            logger.error("gateway misconfigured, task %s cannot proceed: %s", task_id, exc)
            return _failed("gateway-misconfigured", str(exc))

        try:
            stored = await self._storage.put(
                data=artifact_bytes.data,
                media_type=artifact_bytes.media_type,
                expected_sha256=artifact_bytes.declared_sha256,
            )
        except StorageError as exc:
            # 存储失败一律不可重试地记为本次失败：产物已在内存里，重试要重新
            # 调用网关（重复计费），因此交给上层决定是否重跑整个任务。
            logger.error("artifact storage failed for task %s: %s", task_id, exc)
            return _failed(f"storage-{type(exc).__name__}", str(exc))

        artifact = GeneratedArtifact(
            stored=stored, upstream_request_id=artifact_bytes.upstream_request_id
        )
        try:
            asset_version_id, quality_profile_version = await self._registrar.register(
                task, artifact
            )
        except AssetRegistrationUnavailableError as exc:
            logger.error("cannot complete task %s: %s", task_id, exc)
            return _failed("asset-registration-unwired", str(exc))

        return GenerationOutcome(
            result="SUCCEEDED",
            evidence_ref=_clip(
                f"{stored.locator}|sha256={stored.sha256}"
                f"|upstream={artifact_bytes.upstream_request_id or '-'}"
            ),
            asset_version_id=asset_version_id,
            quality_profile_version=quality_profile_version,
        )


def _failed(kind: str, detail: str) -> GenerationOutcome:
    # 结构化前缀让「为什么失败」可被聚合统计；纯文本 detail 只用于排障。
    return GenerationOutcome(result="FAILED", evidence_ref=_clip(f"{kind}: {detail}"))


def _clip(text: str) -> str:
    return text if len(text) <= _EVIDENCE_MAX else text[: _EVIDENCE_MAX - 1] + "…"
