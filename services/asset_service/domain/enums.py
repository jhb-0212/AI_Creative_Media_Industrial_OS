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
    """AssetVersion 生命周期（仅登记有明确来源边的状态）。"""

    GENERATED = "GENERATED"
    TECHNICAL_QC = "TECHNICAL_QC"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    APPROVED = "APPROVED"
    GOLD = "GOLD"


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
