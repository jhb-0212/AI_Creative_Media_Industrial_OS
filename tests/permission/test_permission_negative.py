"""权限矩阵负向测试（permission-negative-tests CI job）。

定位：对 `contracts/permission/permission_matrix.yaml` 做「负向不变量」校验，
即断言权限契约中不存在违规配置。区别于 contract_check.py 的「结构完整性」
正向校验，本模块聚焦「越权/自批/无令牌」等红线是否被契约明确禁止。

对齐红线（正文 C.5 与 V2.0.5 核心红线）：
- Agent 不得自批自用。
- 终态不可原地复活、资产不可覆盖、质量不可暗降（此处聚焦权限面）。
- Capability Token 强制（token_required）。

本模块仅校验契约本身，不依赖尚未实现的运行时权限代码。
"""

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
PERMISSION_MATRIX = CONTRACTS / "permission" / "permission_matrix.yaml"


def _load_action_matrix() -> list[dict]:
    """加载 permission_matrix.yaml 的 action_matrix.actions。"""
    data = yaml.safe_load(PERMISSION_MATRIX.read_text(encoding="utf-8"))
    return data["action_matrix"]["actions"]


def _actions() -> list[dict]:
    return _load_action_matrix()


def test_every_action_has_unique_negative_test_id() -> None:
    """负向不变量：每个 action 都声明唯一 negative_test_id，便于逐项追踪越权用例。"""
    ids = [a.get("negative_test_id") for a in _actions()]
    assert all(i for i in ids), "存在未声明 negative_test_id 的 action"
    assert len(ids) == len(set(ids)), "negative_test_id 存在重复"


def test_token_required_is_mandatory() -> None:
    """负向不变量：所有 action 必须强制 Capability Token（token_required=true）。"""
    missing = [a["action_key"] for a in _actions() if not a.get("token_required")]
    assert not missing, f"以下 action 未强制 Capability Token: {missing}"


def test_no_action_has_empty_allowed_principals() -> None:
    """负向不变量：任何 action 都不得是「无授权主体」的空授权。"""
    empty = [
        a["action_key"]
        for a in _actions()
        if not (a.get("allowed_principals") or "").strip()
    ]
    assert not empty, f"以下 action 的 allowed_principals 为空: {empty}"


def test_self_approval_red_line_is_declared() -> None:
    """负向不变量：高风险「批准类」动作必须声明职责分离，禁止自批。

    断言每个 HIGH 风险、动作名含 APPROVE 的动作，其 allowed_principals 或
    approval_requirement 中至少一处体现「多主体/职责分离」语义——
    即批准权不能是单一自批主体。

    判定依据（确凿、可维护）：
    - allowed_principals 含「/」或「+」等多主体连接符，或
    - approval_requirement 含「分离」「不得自批」「人工确认」等职责分离关键词。
    """
    high_risk_approve = [
        a
        for a in _actions()
        if a.get("risk_level") == "HIGH" and "APPROVE" in a.get("action_key", "")
    ]
    assert high_risk_approve, "期望存在 HIGH 风险批准类动作（契约缺失）"

    def _has_separation(a: dict) -> bool:
        principals = a.get("allowed_principals") or ""
        requirement = a.get("approval_requirement") or ""
        # 多主体（/ 或 +）或明确的职责分离措辞。
        if "/" in principals or "+" in principals:
            return True
        return any(k in requirement for k in ("分离", "自批", "人工", "确认"))

    violations = [a["action_key"] for a in high_risk_approve if not _has_separation(a)]
    assert not violations, (
        f"以下 HIGH 风险批准动作未声明职责分离（违反「不得自批」红线）: {violations}"
    )


def test_no_action_allows_agent_self_approval_in_role_matrix() -> None:
    """负向不变量：role_matrix 中 Agent/Worker 不得被授予「批准/锁定」权。

    判定依据：Agent/Worker 的授权描述中不得出现「可批准」「可锁定」「批准」
    等授予性措辞；但「不得自批」「只能请求确认」等禁止性描述是正确配置，
    不应被误判为违规。
    """
    data = yaml.safe_load(PERMISSION_MATRIX.read_text(encoding="utf-8"))
    # 授予性（正面授权）关键词——禁止出现。
    granted = ("可批准", "可锁定", "批准权", "自批通过")
    for action in data.get("role_matrix", {}).get("actions", []):
        grants = action.get("grants", {})
        agent_grant = grants.get("Agent/Worker", "")
        assert not any(w in agent_grant for w in granted), (
            f"role_matrix 动作「{action.get('action')}」的 Agent/Worker 授权"
            f"含批准/锁定授权: {agent_grant!r}"
        )
