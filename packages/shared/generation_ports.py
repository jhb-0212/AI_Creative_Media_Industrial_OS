"""跨域读取端口的契约：生成任务的权威事实（不属于任何单个领域）。

为什么需要它（与 ``evidence_ports`` 同构，但不是同一件事）
--------------------------------------------------------
人工恢复 QC 入队要回答一个问题：**这条生成任务真的「已成功且携带已登记产物」吗？**
这个事实在 ``production_planning`` 域，而判定发生在 ``qc_governance`` 域。

四种做法只有一种对（理由同 ``evidence_ports``）：

① QC 域 import 生产域仓储 —— 违反 R2；
② 生产域 import QC 域的 DTO —— 同样违反 R2，只是方向反过来；
③ 给 ``qc_governance_role`` 补一条 ``production_planning`` 的表授权 —— 这不是
   「读取一个事实」，而是永久拆掉跨域边界：一旦 QC 角色能读生产表，下一个
   「顺手 JOIN 一下」就没有任何东西拦得住；
④ **契约下沉到中立层**（本文件）：使用方声明需要什么事实，数据属主提供适配，
   组合根把两者接起来。

本条尤其不能用 ③：C7-b 的全部意义是「人工入口不得凭请求体自报」，
而「为了让核验跑得通，先把读取权限放宽」正是把判据换成权限妥协。

端口只带核验需要的最少字段
--------------------------
核验要的是「终态 + 产物引用 + 归属」，不是整条生成任务。返回整行会把
「什么才算可入队」的判断从 QC 域泄漏到数据搬运层——那一层没有上下文，
只能照着字段名猜，猜错就是静默放行。

为什么 ``status`` 是字符串而不是枚举
------------------------------------
枚举定义在 ``services/project_planning/domain/enums.py``，``packages`` 不得
反向依赖 ``services``（分层规则 R1）。因此本层只搬运**原始取值**，
「哪个取值算终态成功」由使用方判定——那是业务判断，不该在适配层做。

失败契约：查不到就是 ``None``，由使用方翻译成「任务不存在或不可见」。
「不存在」与「读取失败」是两件事，后者应上抛成基础设施错误；压平成同一个
结论会让排障无法区分（与 ``evidence_ports`` 同一条纪律）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class GenerationFacts:
    """一条生成任务的权威事实（恢复入队判定的全部输入）。

    ``asset_version_id`` / ``quality_profile_version`` 是 T3 从资产登记回执绑定的
    产物引用（ADR-R42 C1/C3）：它们非空 ⟺ 产物已在资产域登记成功。
    """

    generation_task_id: str
    tenant_id: UUID
    status: str
    project_id: str
    shot_id: str
    asset_version_id: str
    quality_profile_version: str


# ``(generation_task_id, project_id, tenant_id) -> GenerationFacts | None``
#
# 为什么键里必须有 ``project_id``：``generation_tasks`` 的唯一键是
# ``(generation_task_id, project_id, tenant_id)``，**不是** 单个 task id
# （见 ``uq_generation_tasks_task_project_tenant``）。只按 task id 查会得到
# 多行，让「恢复哪一条」变成一个静默的随机选择——那正是人工入口最该避免的
# 不确定性。要求调用方同时给出项目，等于要求它说清「我要恢复哪一个」，
# 而这个说法本身仍要被数据库事实确认（查不到就是 404，不是信任它）。
GenerationFactsLookup = Callable[[str, str, UUID], Awaitable[GenerationFacts | None]]


__all__ = ["GenerationFacts", "GenerationFactsLookup"]
