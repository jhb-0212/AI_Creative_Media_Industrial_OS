"""生成适配器协议与确定性实现。

``GenerationAdapter`` 是 worker 与"实际执行生成"之间的边界：
- 真实实现（model_gateway 域，未来）走供应商调用；
- ``DeterministicGenerationAdapter`` 是 Phase 1 的诚实替身：不调用外部
  供应商，产出**确定性**的引用与摘要（任务 id + attempt 派生），使主链
  生成环可以在无供应商依赖的情况下端到端验证与回归。

协议返回 ``GenerationOutput`` 而不是裸字符串（ADR-R33）：执行环要把产出
登记为资产版本，而登记资产需要内容摘要、定位与类型。让适配器只返回一个
引用、由执行环"编造"摘要，会让摘要不再证明内容——摘要必须由产出者给出。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.domain.output import GenerationOutput

#: 确定性适配器登记的资产类型（单一路线生成产出的是镜头素材）。
_ASSET_TYPE = "VIDEO"


@runtime_checkable
class GenerationAdapter(Protocol):
    """生成执行适配器协议（worker 执行环的唯一外部依赖点）。"""

    async def execute(self, task: GenerationTask) -> GenerationOutput:
        """执行一次生成，返回产出描述。失败抛异常。"""
        ...


@dataclass
class DeterministicGenerationAdapter:
    """确定性适配器：引用与摘要由任务标识派生，不调用外部供应商。

    ``fail_task_ids`` 是显式失败注入清单（测试与故障演练用）：命中的任务
    以 ``GenerationError`` 失败——失败走 RUNNING→FAILED 终态，重试以新任务
    承载（9.4），适配器绝不原地复活任务。

    摘要口径：对确定性产出的**内容标识**（``genresult://<task>/attempt-<n>``）
    取 SHA-256。这在 Phase 1 是可复现的事实（该字符串就是这条确定性产出的
    全部内容），而不是"随便算一个 64 位十六进制"；真实适配器落地后摘要
    来自供应商返回的实际内容，协议不变。
    """

    fail_task_ids: frozenset[str] = field(default_factory=frozenset)

    async def execute(self, task: GenerationTask) -> GenerationOutput:
        if task.entity_id in self.fail_task_ids:
            raise GenerationError(
                f"任务 {task.entity_id} 命中失败注入清单（确定性适配器）"
            )
        # 确定性产出：同一任务/同一 attempt 恒得同一引用与摘要（可复现回归）。
        artifact_ref = f"genresult://{task.entity_id}/attempt-{task.attempt}"
        digest = hashlib.sha256(artifact_ref.encode("utf-8")).hexdigest()
        return GenerationOutput(
            artifact_ref=artifact_ref,
            sha256=digest,
            storage_ref={"uri": artifact_ref, "producer": "deterministic-adapter"},
            asset_type=_ASSET_TYPE,
        )


class GenerationError(RuntimeError):
    """适配器执行失败（worker 捕获后走 RUNNING→FAILED 终态）。"""


__all__ = [
    "DeterministicGenerationAdapter",
    "GenerationAdapter",
    "GenerationError",
]
