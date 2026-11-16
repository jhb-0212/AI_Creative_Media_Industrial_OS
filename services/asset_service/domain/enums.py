"""资产域状态与 QC 枚举（纯声明，对齐契约）。

来源：
- contracts/state_machines/state_machines.yaml → AssetVersion（8.5 Asset 生命周期）
  ``GENERATED→TECHNICAL_QC→HUMAN_REVIEW→APPROVED→GOLD``
  以及「可进入 REJECTED / QUARANTINED / REWORK / SUPERSEDED / ARCHIVED」。
- contracts/schemas/object_schemas.schema.json → QCReport.deviation_class /
  intent_match_status 的枚举值。

为什么只到 GOLD 为止：
- 旁支状态（REJECTED/QUARANTINED/REWORK/SUPERSEDED/ARCHIVED）在契约正文里
  只给了「可进入」，没有给出**从哪些状态进入**。按 31.5「未登记转移一律
  拒绝」，运行时不能猜测来源边；补齐这些边需要契约补登（见
  docs/adr/ADR-R08-asset-lifecycle-side-branches.md）。这里宁可少一个枚举值，
  也不伪造状态转移。
"""

from __future__ import annotations

from enum import Enum


class AssetStatus(str, Enum):
    """AssetVersion 生命周期（与 `contracts/enums/enum_registry.yaml` 逐值对齐）。

    **状态可表示 ≠ 状态可进入**。这里登记契约声明的全部 10 个状态，但运行时可
    转移的只有 `ASSET_ALLOWED_TRANSITIONS` 里的 4 条边——旁支状态（REJECTED /
    QUARANTINED / REWORK / SUPERSEDED / ARCHIVED）在正文只有「可进入」，没有
    来源边，按 31.5「未登记转移一律拒绝」，服务层无法把资产推进到这些状态。

    为什么仍要全部登记（此前只登记了 5 个）：
    - 枚举是**数据可表示的集合**，不是"当前流程能到达的集合"。缺值会让
      `AssetStatus(model.status)` 在读到旁支状态时抛 ValueError，而旁支状态
      是契约允许存在的数据（例如合规流程隔离出来的资产），读一次就 500；
    - 缺值让「契约 ↔ 代码」的比对只能靠人工记忆（实际已漂移），补全后才能
      由 `contract_check` 的枚举一致性闸门机械校验；
    - 不可达性由 `contracts/runtime/state_machine_gaps.yaml` 显式登记并由闸门
      双向校验（登记了却已可达 → 报陈旧债务），不靠"枚举里没有"来暗示。
    """

    GENERATED = "GENERATED"
    TECHNICAL_QC = "TECHNICAL_QC"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    APPROVED = "APPROVED"
    GOLD = "GOLD"
    # 以下五个为契约登记、运行时不可达的旁支状态（见模块 docstring）。
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"
    REWORK = "REWORK"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"


class DeviationClass(str, Enum):
    """QC 偏差分类（契约 QCReport.deviation_class）。"""

    INTENTIONAL = "INTENTIONAL"
    UNINTENTIONAL = "UNINTENTIONAL"
    UNCERTAIN = "UNCERTAIN"


class IntentMatchStatus(str, Enum):
    """意图匹配状态（契约 QCReport.intent_match_status）。"""

    MATCH = "MATCH"
    CONFLICT = "CONFLICT"
    NOT_ASSESSED = "NOT_ASSESSED"
