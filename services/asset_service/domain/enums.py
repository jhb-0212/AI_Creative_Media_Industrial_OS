"""资产域状态与 QC 枚举（纯声明，对齐契约）。

来源：
- contracts/state_machines/state_machines.yaml → AssetVersion（8.5 Asset 生命周期）
  ``allowed_states`` 十项：GENERATED → TECHNICAL_QC → HUMAN_REVIEW → APPROVED → GOLD，
  以及旁支 REJECTED / QUARANTINED / REWORK / SUPERSEDED / ARCHIVED。
- contracts/schemas/object_schemas.schema.json → QCReport.deviation_class /
  intent_match_status 的枚举值。

为什么枚举收全部十态，而转移表只收四条边
--------------------------------------
这是两件事：**允许的状态集合**与**已授权的转移**。8.5 对旁支只写了「可进入
REJECTED / QUARANTINED / REWORK / SUPERSEDED / ARCHIVED」，没有给出从哪些状态
进入——按 31.5「未登记转移一律拒绝」，运行时不能猜边，所以转移表里没有它们
（缺的来源边登记在 contracts/runtime/state_machine_gaps.yaml，闸门双向校验）。

但枚举必须收全：状态集是「这个字段允许出现哪些值」的契约。旧实现只列了有入边的
五态，等于把「合法但暂无授权入口」的旁支状态判成非法值——一旦真实数据里出现
（例如权利复核产生的 QUARANTINED），序列化/反序列化就会炸，而契约其实允许它。
状态集合与转移图的分离，正是为了让这两种语义各自表达清楚。
"""

from __future__ import annotations

from enum import Enum


class AssetStatus(str, Enum):
    """AssetVersion 生命周期（全部允许状态；转移边见 domain/state_machine.py）。"""

    GENERATED = "GENERATED"
    TECHNICAL_QC = "TECHNICAL_QC"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    APPROVED = "APPROVED"
    GOLD = "GOLD"
    # 旁支：契约允许这些状态存在，但来源边尚未登记（31.5：未登记转移一律拒绝）。
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
