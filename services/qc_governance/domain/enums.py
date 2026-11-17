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


class QCReviewSessionStatus(StrEnum):
    """QC 复查会话状态（C.7 权威状态链 + 31.5 裁决 ④）。

    链：OPEN→IN_REVIEW→DECISION_PENDING→RESOLVED→GATE_ELIGIBLE→CLOSED。
    31.5 裁决 ④ 明确禁止 ``RESOLVED→CLOSED`` 跳关：跳过 Gate 资格校验等同
    绕过 Gate 门禁。C.3 写的 ``RESOLVED→GATE_ELIGIBLE/CLOSED`` 是同一冲突的
    另一表述，按权威顺序取 C.7，差异已登记在 state_machines.yaml 的 conflict 字段。

    ``WAIVE_WITH_AUTHORITY`` 为什么在枚举里却没有转移边：enum_registry 把它
    登记为 QCReviewSession 的状态值，而 C.7 状态链与 21.1 都把它当**人工结论
    类型**（21.1 明列的五种 human_decision 之一）。删掉它会触发 enum_parity 缺值，
    给它造边则等于凭空发明一条来源边。因此保留取值、不建边——与
    ``GateDecisionValue.OVERRIDDEN`` 的「可表示 ≠ 可进入」同一口径，差异登记
    于 ADR-R36 §3，待契约裁决。
    """

    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    DECISION_PENDING = "DECISION_PENDING"
    RESOLVED = "RESOLVED"
    GATE_ELIGIBLE = "GATE_ELIGIBLE"
    CLOSED = "CLOSED"
    WAIVE_WITH_AUTHORITY = "WAIVE_WITH_AUTHORITY"


class QCIssueSeverity(StrEnum):
    """缺陷严重级别（4.3 QC 严重级别）。

    刻意不含 ``BLOCKER``：它只在 7.6 的 ReworkTask 字段表里出现，是**返工单**
    的级别；把返工口径混进缺陷口径会让「这条缺陷到底多严重」随返工单变动。
    ``CRITICAL`` 对应 4.3 的红线级（版权/安全/隐私/交付不可用），
    因此不能以任何理由豁免——见 ``qc_issue`` 的豁免校验。
    """

    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    OBSERVATION = "OBSERVATION"


class QCIssueHumanDecision(StrEnum):
    """人工结论类型（21.1 明列五种；``PENDING`` 取自 8.9 QCReport 示例）。

    ``PENDING`` 是**未作结论**的显式取值，不是第五种结论：允许它出现在
    缺陷里，但不允许它作为一条裁决的结论（那样等于用"待定"推进会话）。

    会话可带着 ``NEEDS_MORE_EVIDENCE`` / ``CONFIRM_DEFECT`` 进入
    DECISION_PENDING，但两者都不能进入 RESOLVED——未闭合的问题不是已解决的
    问题：前者缺证据，后者缺返工（21.2 对已确认缺陷创建修复任务并进入 DAG）。
    只有 ``FALSE_POSITIVE`` / ``ARTISTIC_INTENTIONAL`` / ``WAIVE_WITH_AUTHORITY``
    把缺陷从"待处理"里移出去。
    """

    PENDING = "PENDING"
    CONFIRM_DEFECT = "CONFIRM_DEFECT"
    ARTISTIC_INTENTIONAL = "ARTISTIC_INTENTIONAL"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"
    WAIVE_WITH_AUTHORITY = "WAIVE_WITH_AUTHORITY"


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
