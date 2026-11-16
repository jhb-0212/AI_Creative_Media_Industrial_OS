"""质量治理域枚举（Gate 决策链的取值集合）。

为什么单独成文件而不是塞进 asset_service：Gate 决策属于**治理裁决**，
资产域属于**生成与检测**。同域的枚举放在同域，是让「谁能写什么」在目录
结构上就能读出来（与数据库角色边界一致，见 packages.shared.db_roles）。

本文件里的每个枚举类都必须出现在 ``contracts/runtime/enum_bindings.yaml``
的 bindings 或 exemptions 中，否则 ``check_enum_parity`` 报红——新增枚举
不登记等于免检，这正是包 C4 要消灭的形态。
"""

from __future__ import annotations

from enum import StrEnum


class GateKey(StrEnum):
    """工业 Gate 的键（契约 7.5 主表 + 7.3 A2 子 Gate）。

    只收**工业** Gate。``PG0—PG3`` 是社区域独立 Publication Gate（ADR-056，
    「不替代也不套用 Gate0—4」），CreatorGate 是创作者策略层（34.5「只属
    Creator 策略层，不进入工业 Gate 状态机」）——两者都不在本枚举内，
    混进来会让"工业 Gate4 通过率"这个北极星指标被发布审核混淆。

    刻意未收 ``Gate 1-A``：7.1 子轨道表用它指向 Gate 1 的输出物，7.5 的
    权威 Gate 清单只有 Gate 0—4。把它建成独立取值就必须回答"1-A 与 1 的
    决策能否互相替代"，而契约没有给出答案。登记为债务（ADR-R23），
    不猜。
    """

    GATE_0 = "GATE_0"
    GATE_1 = "GATE_1"
    GATE_2 = "GATE_2"
    GATE_3 = "GATE_3"
    GATE_4 = "GATE_4"
    A2_GEOMETRY_GATE = "A2_GEOMETRY_GATE"
    A2_PURITY_GATE = "A2_PURITY_GATE"
    A2_RIGHTS_GATE = "A2_RIGHTS_GATE"


class GateDecisionValue(StrEnum):
    """Gate 裁决结果（契约 7.4：APPROVED / REJECTED / RETURNED / OVERRIDDEN）。

    枚举**必须**包含全部四个契约取值：``enum_bindings`` 的逐值相等校验会
    把"少一个"报成漂移，而读取契约允许、代码不认识的值会表现为 500。

    ``OVERRIDDEN`` 当前**可表示但不可进入**（领域层拒绝，理由见
    ``gate_decision.GateDecision.__post_init__`` 与 ADR-R23）：它缺少
    "覆盖了哪一条决策"的引用字段，接受它会产出无法追溯的裁决。
    这是「可表示 ≠ 可进入」的又一处，与 ``AssetStatus`` 的旁支状态同一口径。
    """

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETURNED = "RETURNED"
    OVERRIDDEN = "OVERRIDDEN"


class GateDecisionStatus(StrEnum):
    """决策记录自身的状态。

    只有一个取值，且**不是**状态机：``GateDecision`` 是"某人在某时对某版本
    做出了某个裁决"这件事的**事实记录**，事实没有生命周期。契约 7.4 那句
    「输入版本变化时，原 Gate 标记 STALE」说的是 **Gate 实例**的有效性，
    不是决策记录的状态——把 STALE 写在这一行上就等于允许 UPDATE，而那会
    让"当时批的是什么"变得不可信。

    "这个 Gate 现在还算不算通过"是派生问题：取同一 ``gate_instance_id``
    下最新的 APPROVED，再比对它引用的 ``constraint_snapshot_id`` 与当前
    快照是否一致。派生逻辑属 Gate 实例解析（尚未实现，ADR-R23 登记）。
    """

    RECORDED = "RECORDED"
