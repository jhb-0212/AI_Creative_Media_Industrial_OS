"""租约字段写入闸门的负向用例（证明新规则会红，而不是「碰巧现在是绿的」）。

为什么必须有这一条
------------------
``data.transition_single_entry`` 这次从「只查 status」扩到
``lease_owner / lease_expires_at / lease_generation``。判据扩了但没人证明它会
拒绝——那样它只是一段「看起来更严」的代码：真有人 ``task.lease_generation = 99``
时仍然全绿，因为判据可能只匹配 ``self.`` 前缀，而服务层写的是 ``task.``。

构造的坏样本刻意用**非 self 的前缀**：这正是这次扩展要覆盖的形态，也是旧规则
（只认 ``self.status``）会漏掉的那一类。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import boundary_assertions as ba


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("services", "packages", "apps"):
        (tmp_path / name).mkdir()
    # 纯函数里 rel 的取法是 relative_to(ROOT)，判据与取数共用一个根，测试改根。
    monkeypatch.setattr(ba, "ROOT", tmp_path)
    return tmp_path


def test_lease_assignment_outside_domain_is_rejected(fake_root: Path) -> None:
    _write(
        fake_root,
        "services/demo/service/demo_service.py",
        "async def renew(task, generation):\n"
        "    task.lease_generation = generation\n",
    )
    problems = ba.evaluate_transition_entries(fake_root)
    assert problems, "服务层直接改写 lease_generation 未被判红"
    assert any("租约字段" in problem for problem in problems)
    assert any("demo_service.py::renew" in problem for problem in problems)


def test_setattr_of_lease_outside_domain_is_rejected(fake_root: Path) -> None:
    _write(
        fake_root,
        "services/demo/service/demo_service.py",
        "def steal(task):\n"
        '    setattr(task, "lease_owner", "someone-else")\n',
    )
    problems = ba.evaluate_transition_entries(fake_root)
    assert any("租约字段" in problem for problem in problems)


def test_lease_assignment_inside_domain_is_allowed(fake_root: Path) -> None:
    """领域方法自己维护租约是唯一合法位置（否则闸门等于禁止 fencing 实现）。"""
    _write(
        fake_root,
        "services/demo/domain/task.py",
        "def claim(self, worker):\n"
        "    self.lease_owner = worker\n"
        "    self.lease_generation += 1\n",
    )
    assert ba.evaluate_transition_entries(fake_root) == []
