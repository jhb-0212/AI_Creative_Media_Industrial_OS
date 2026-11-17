"""平台阶段门禁（F1）单元测试。

三层断言，缺一层就会变成"看起来有门禁"：

1. **解析层**：契约里 ``PHASE_GATED（Phase N）`` 的各种写法都能取出阶段号，
   且不会把普通动作误判成门禁动作（误判的后果是拒绝一个合法动作）。
2. **契约覆盖层**：把 Action Registry 里**全部** PHASE_GATED 动作逐个过一遍解析。
   写死一两个样例只能证明"这两个能解析"，证明不了"44 条都能解析"——而 F1 的
   拒绝对象是全体，不是样例。
3. **行为层**：真正打 HTTP，断言拒绝码、状态码、details 结构，以及未登记 key
   与非门禁动作**仍然 404**（否则拒绝面会退化成动作发现面）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from apps.api.main import app
from packages.identity.action_registry import action_spec, all_action_specs
from packages.identity.phase_gate import (
    PLATFORM_PHASE,
    PlatformPhase,
    is_blocked,
    parse_required_phase,
)

#: 契约里全部 Creator 动作（F1 的正面验收对象）。
CREATOR_ACTIONS = (
    "CREATOR.WORKSPACE.CREATE",
    "CREATOR.GATE.EVALUATE",
    "CREATOR.MODE.UPGRADE",
    "CREATOR.DELIVERY.MANIFEST.CREATE",
    "CREATOR.RELEASE.SUBMIT",
    "CREATOR.RELEASE.PUBLISH",
    "CREATOR.PUBLICATION.CHECK",
    "CREATOR.RELEASE.REVOKE",
)


@pytest.fixture
def client() -> TestClient:
    """与 ``test_health`` 同款：用 ``with`` 触发 lifespan（生产同路径）。"""
    previous_pools = getattr(app.state, "pools", None)
    with TestClient(app) as c:
        yield c
    app.state.pools = previous_pools


class TestParseRequiredPhase:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("ACTIVE", None),
            ("PHASE_GATED（Phase 2）", PlatformPhase.PHASE_2),
            ("PHASE_GATED（Phase 3）", PlatformPhase.PHASE_3),
            # 括号里的补充说明不参与判定：它描述启用条件，不是启用阶段。
            ("PHASE_GATED（Phase 2+）", PlatformPhase.PHASE_2),
            ("PHASE_GATED（Phase 3 公开）", PlatformPhase.PHASE_3),
            # 半角括号同样要能解析（契约手写时容易混用）。
            ("PHASE_GATED(Phase 4)", PlatformPhase.PHASE_4),
        ],
    )
    def test_parses_phase_number(self, status: str, expected: PlatformPhase | None) -> None:
        assert parse_required_phase(status) == expected

    @pytest.mark.parametrize(
        "status",
        [
            "PHASE_GATED",  # 没有阶段号：契约书写缺陷，不猜
            "PHASE_GATED（Phase 9）",  # 越界阶段：IntEnum 无此项，按非门禁处理
            "active",  # 大小写不是契约写法，但也不能崩
        ],
    )
    def test_undecidable_status_is_not_treated_as_gated(self, status: str) -> None:
        assert parse_required_phase(status) is None

    def test_active_action_never_blocks(self) -> None:
        """``PHASE_GATED`` 前缀匹配不能宽到把 ACTIVE 也吃进来。"""
        assert parse_required_phase("ACTIVE") is None
        assert is_blocked(None, current=PlatformPhase.PHASE_1) is False


class TestContractCoverage:
    def test_current_phase_is_phase_1(self) -> None:
        """阶段是代码常量（F1 禁止配置开关）。改这一行等于改发布事实——

        因此这里钉一根桩：任何把阶段推上去的改动都必须同时改这条断言，
        强迫提交者面对"这一步是阶段跃迁，不是普通重构"。
        """
        assert PLATFORM_PHASE is PlatformPhase.PHASE_1
        assert PLATFORM_PHASE.label == "Phase 1"

    def test_every_phase_gated_action_is_parsable(self) -> None:
        """契约里每一条 PHASE_GATED 动作都必须解析出阶段号。

        这条用 ``all_action_specs()`` 全量扫描而不是列样例：样例只能证明被
        选中的那几条没写错，而拒绝对象是全体。若有动作写成 ``PHASE_GATED``
        但括号里漏了 Phase N，运行时会静默当成"非门禁"放过去——测试在这里红。
        """
        gated = [spec for spec in all_action_specs() if spec.is_phase_gated]
        assert gated, "契约里应当存在 PHASE_GATED 动作；此处为空说明分母不对"

        unparsable = [
            (spec.action_key, spec.status)
            for spec in gated
            if parse_required_phase(spec.status) is None
        ]
        assert unparsable == [], f"以下动作的 status 无法解析出阶段号: {unparsable}"

    def test_creator_actions_are_all_gated_beyond_phase_1(self) -> None:
        """F1 的前提：8 个 Creator 动作在当前阶段必须全部不可用。"""
        for key in CREATOR_ACTIONS:
            spec = action_spec(key)
            assert spec is not None, f"{key} 未在 Action Registry 登记"
            required = parse_required_phase(spec.status)
            assert required is not None, f"{key} 未标为 PHASE_GATED"
            assert is_blocked(required, current=PLATFORM_PHASE), (
                f"{key} 的阶段 {required.label} 不早于当前 {PLATFORM_PHASE.label}，"
                "F1 的前提不成立"
            )


class TestActionSurface:
    def test_creator_action_returns_phase_not_enabled(self, client: TestClient) -> None:
        resp = client.post("/actions/creator.mode.upgrade")
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["error_code"] == "PHASE_NOT_ENABLED"
        assert body["category"] == "PHASE"
        # details 必须能让调用方自己判断"要不要等"，而不只是知道被拒。
        assert body["details"]["action_key"] == "CREATOR.MODE.UPGRADE"
        assert body["details"]["current_phase"] == "Phase 1"
        assert body["details"]["required_phase"] == "Phase 2"

    @pytest.mark.parametrize("action_key", CREATOR_ACTIONS)
    def test_all_creator_actions_are_refused(
        self, client: TestClient, action_key: str
    ) -> None:
        """F1 要的是"调用 Creator 动作返回 PHASE_NOT_ENABLED"，不是"某一个"。

        逐个断言，避免只测最容易的那条（``CREATOR.MODE.UPGRADE``）就宣布通过。
        """
        resp = client.post(f"/actions/{action_key.lower()}")
        assert resp.status_code == 409, f"{action_key}: {resp.text}"
        assert resp.json()["error_code"] == "PHASE_NOT_ENABLED"

    def test_non_creator_gated_action_is_also_refused(self, client: TestClient) -> None:
        """门禁是通用能力，不是给 Creator 开的特例——否则 B1 的 44 条门禁动作
        仍有一条 404 尾巴。"""
        resp = client.post("/actions/community.post.submit")
        assert resp.status_code == 409, resp.text
        assert resp.json()["error_code"] == "PHASE_NOT_ENABLED"

    def test_unknown_action_key_stays_404(self, client: TestClient) -> None:
        """未登记 key 不能因为"猜测是阶段问题"就变成 409——

        否则拒绝面就变成了动作发现面：随便敲一个 key，用 409/404 的差别就能
        枚举出契约里有哪些能力。
        """
        resp = client.post("/actions/does.not.exist")
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "NOT_FOUND"

    def test_active_action_path_stays_404(self, client: TestClient) -> None:
        """已启用动作的契约路径不是它的运行时路径（``/projects`` 才是）。

        这条防止一个常见误实现：把 ``/actions/*`` 做成"所有动作的通用入口"。
        它只负责拒绝，不负责转发。
        """
        resp = client.post("/actions/project.create")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error_code"] == "NOT_FOUND"
        assert body["details"]["status"] == "ACTIVE"

    def test_get_method_is_refused_too(self, client: TestClient) -> None:
        """门禁按路径生效，不按方法生效。"""
        resp = client.get("/actions/creator.workspace.create")
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "PHASE_NOT_ENABLED"

    def test_rejection_is_counted(self, client: TestClient) -> None:
        """指标里必须看得见"有人在敲未开放能力"。

        只靠 404 计数无法与拼错路径区分——这正是加这个计数器的理由，
        所以计数器本身也要有断言（否则它可能从未被调用）。
        """
        before = (
            REGISTRY.get_sample_value(
                "phase_gate_rejections_total",
                {"action_key": "CREATOR.RELEASE.PUBLISH"},
            )
            or 0.0
        )
        resp = client.post("/actions/creator.release.publish")
        assert resp.status_code == 409
        after = REGISTRY.get_sample_value(
            "phase_gate_rejections_total",
            {"action_key": "CREATOR.RELEASE.PUBLISH"},
        )
        assert after is not None
        assert after == before + 1
