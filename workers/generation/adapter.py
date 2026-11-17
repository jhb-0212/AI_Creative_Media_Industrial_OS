"""生成适配器协议与确定性实现。

``GenerationAdapter`` 是 worker 与"实际执行生成"之间的边界：
- 真实实现（model_gateway 域，未来）走供应商调用；
- ``DeterministicGenerationAdapter`` 是 Phase 1 的诚实替身：不调用外部
  供应商，产出**确定性** result_ref（任务 id + attempt 派生），使主链
  生成环可以在无供应商依赖的情况下端到端验证与回归。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from services.generation_execution.domain.generation_task import GenerationTask


@runtime_checkable
class GenerationAdapter(Protocol):
    """生成执行适配器协议（worker 执行环的唯一外部依赖点）。"""

    async def execute(self, task: GenerationTask) -> str:
        """执行一次生成，返回产出引用（result_ref）。失败抛异常。"""
        ...


@dataclass
class DeterministicGenerationAdapter:
    """确定性适配器：result_ref 由任务标识派生，不调用外部供应商。

    ``fail_task_ids`` 是显式失败注入清单（测试与故障演练用）：命中的任务
    以 ``GenerationError`` 失败——失败走 RUNNING→FAILED 终态，重试以新任务
    承载（9.4），适配器绝不原地复活任务。
    """

    fail_task_ids: frozenset[str] = field(default=frozenset())

    async def execute(self, task: GenerationTask) -> str:
        if task.entity_id in self.fail_task_ids:
            raise GenerationError(
                f"任务 {task.entity_id} 命中失败注入清单（确定性适配器）"
            )
        # 确定性产出：同一任务/同一 attempt 恒得同一引用（可复现回归）。
        return f"genresult://{task.entity_id}/attempt-{task.attempt}"


class GenerationError(RuntimeError):
    """适配器执行失败（worker 捕获后走 RUNNING→FAILED 终态）。"""
