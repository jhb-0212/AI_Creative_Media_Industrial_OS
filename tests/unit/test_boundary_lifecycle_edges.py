"""断言 ``lifecycle.edge_ownership`` 的负向用例（证明它会报红）。

为什么断言也要有负向用例：只证明「真实仓库现在 PASS」无法区分两种情形——
判据真的成立，还是判据写得太松、怎么改都 PASS。本文件对每一类失效各构造一份
输入，断言它**恰好**报出对应的那条问题。

六类失效对应五条判据（见 ``evaluate_lifecycle_edge_ownership`` 文档）：
1. 导出的邻接集与边表派生结果不一致（存在两份真相）；
2. 边的所有者不在动作注册表（拼错的 action 永远拒绝，看起来却像配好了）；
3. 边的所有者不是 ACTIVE（拿未落地动作当准入凭据）；
4. 实现造了一条契约未登记的边（31.5）；
5. 聚合根的 transition_to 不接收 action（所有权只能靠「记得先查」）；
6. 接收了 action 却没用它判所有权（参数存在但只是装饰）。
"""

from __future__ import annotations

from pathlib import Path

from packages.shared.lifecycle import Lifecycle, Transition
from scripts import boundary_assertions as ba

_A = "DRAFT"
_B = "ACTIVE"
_C = "ARCHIVED"

_REGISTRY: dict[str, str] = {
    "FAKE.STATUS.CHANGE": "ACTIVE",
    "FAKE.DEPRECATED": "PHASE_GATED（Phase 3）",
}
_CONTRACT = frozenset({(_A, _B)})


def _lifecycle() -> Lifecycle[str]:
    return Lifecycle(
        object_name="Fake",
        states=(_A, _B, _C),
        edges=(Transition(_A, _B, "FAKE.STATUS.CHANGE"),),
    )


def _spec(
    *,
    lifecycle: Lifecycle[str] | None = None,
    exported: object | None = None,
    aggregate_path: str = "agg.py",
    aggregate_class: str = "Fake",
    registry: dict[str, str] | None = None,
    contract_edges: frozenset[tuple[str, str]] = _CONTRACT,
) -> ba.LifecycleSpec:
    lc = lifecycle or _lifecycle()
    return ba.LifecycleSpec(
        object_name="Fake",
        lifecycle=lc,
        exported_allowed=lc.allowed_transitions() if exported is None else exported,
        aggregate_path=aggregate_path,
        aggregate_class=aggregate_class,
        registry=_REGISTRY if registry is None else registry,
        contract_edges=contract_edges,
    )


_AGG_OK = """
class Fake:
    def transition_to(self, target, audit_ref, action):
        LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
"""

_AGG_NO_ACTION = """
class Fake:
    def transition_to(self, target, audit_ref):
        assert_registered(self.status, target)
        self.status = target
"""

_AGG_UNUSED_ACTION = """
class Fake:
    def transition_to(self, target, audit_ref, action):
        assert_registered(self.status, target)
        self.status = target
"""


def _write(root: Path, source: str) -> None:
    (root / "agg.py").write_text(source, encoding="utf-8")


def test_clean_spec_has_no_problems(tmp_path: Path) -> None:
    """基线：合法输入不报问题（否则下面的负向用例失去意义）。"""
    _write(tmp_path, _AGG_OK)
    assert ba.evaluate_lifecycle_edge_ownership((_spec(),), root=tmp_path) == []


def test_exported_projection_divergence_is_reported(tmp_path: Path) -> None:
    """判据 1：导出邻接集与边表派生不一致 —— 两份真相。"""
    _write(tmp_path, _AGG_OK)
    stale = {_A: frozenset({_B}), _B: frozenset({_C}), _C: frozenset()}
    problems = ba.evaluate_lifecycle_edge_ownership(
        (_spec(exported=stale),), root=tmp_path
    )
    assert any("两份真相" in p for p in problems)


def test_unknown_owner_action_is_reported(tmp_path: Path) -> None:
    """判据 2：所有者不在动作注册表（拼错的 action 也「非空」）。"""
    _write(tmp_path, _AGG_OK)
    lc = Lifecycle(
        object_name="Fake",
        states=(_A, _B, _C),
        edges=(Transition(_A, _B, "FAKE.STATUS.CHGANGE"),),
    )
    problems = ba.evaluate_lifecycle_edge_ownership((_spec(lifecycle=lc),), root=tmp_path)
    assert any("不在动作注册表" in p for p in problems)


def test_non_active_owner_action_is_reported(tmp_path: Path) -> None:
    """判据 2：所有者存在但不是 ACTIVE —— 未落地动作不可作准入凭据。"""
    _write(tmp_path, _AGG_OK)
    lc = Lifecycle(
        object_name="Fake",
        states=(_A, _B, _C),
        edges=(Transition(_A, _B, "FAKE.DEPRECATED"),),
    )
    problems = ba.evaluate_lifecycle_edge_ownership((_spec(lifecycle=lc),), root=tmp_path)
    assert any("不可作为准入凭据" in p for p in problems)


def test_edge_absent_from_contract_is_reported(tmp_path: Path) -> None:
    """判据 3：实现造了一条契约未登记的边（31.5）。"""
    _write(tmp_path, _AGG_OK)
    lc = Lifecycle(
        object_name="Fake",
        states=(_A, _B, _C),
        edges=(
            Transition(_A, _B, "FAKE.STATUS.CHANGE"),
            Transition(_A, _C, "FAKE.STATUS.CHANGE"),
        ),
    )
    problems = ba.evaluate_lifecycle_edge_ownership((_spec(lifecycle=lc),), root=tmp_path)
    assert any("未在 state_machines.yaml 登记" in p for p in problems)


def test_aggregate_without_action_param_is_reported(tmp_path: Path) -> None:
    """判据 4：aggregate 不接收 action —— 所有权只能靠「记得先查」。"""
    _write(tmp_path, _AGG_NO_ACTION)
    problems = ba.evaluate_lifecycle_edge_ownership((_spec(),), root=tmp_path)
    assert any("未接收 action 形参" in p for p in problems)


def test_aggregate_ignoring_action_is_reported(tmp_path: Path) -> None:
    """判据 5：接收 action 却没用它判所有权 —— 参数是装饰。

    这一条是关键分界：只要求「有 action 形参」的断言会被上面的
    ``_AGG_UNUSED_ACTION`` 骗过——参数有了，准入照旧不判。
    """
    _write(tmp_path, _AGG_UNUSED_ACTION)
    problems = ba.evaluate_lifecycle_edge_ownership((_spec(),), root=tmp_path)
    assert any("等于装饰" in p for p in problems)


def test_empty_specs_never_passes() -> None:
    """空登记表必须报红：断言无条件通过等于没有断言。"""
    assert ba.evaluate_lifecycle_edge_ownership((), root=Path(".")) != []


def test_real_repository_satisfies_edge_ownership() -> None:
    """真实仓库：四个状态机全部满足（本断言的正式判定）。"""
    ok, reason = ba.lifecycle_edge_ownership()
    assert ok, reason
