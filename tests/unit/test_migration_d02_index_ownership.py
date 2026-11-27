"""D-02 回归：`gate_decisions` 的索引必须由拥有它的表负责，不能按名字逐条删。

判据来自真实集群失败：`head → 7a8b9c0d1e2f` 在第 40 位报
``index "ix_project_planning_gate_decisions_gate_key" does not exist``。原因是同一个
索引在不同路径下有不同名字（本迁移自建 / a0b1 cutover 后由 qc_governance 前缀重建 /
a0b1 降级再重建回 project_planning 且多列索引带全部列名），按名字删只能命中一种。

因此本测试只断言"结构性事实"：
1. downgrade 不再出现任何 ``drop_index`` 调用；
2. downgrade 仍然删掉表本身（索引随表消失，不存在"漏删"）；
3. upgrade 仍然建立那三个索引（否则 index 集合会真的少东西）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO / "data/migrations/versions/f9a0b1c2d3e4_gate_decisions_table.py"


class _OpRecorder:
    """记录迁移对 ``op`` 的全部调用（方法名 + 位置参数 + 关键字参数）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def _record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return _record

    def methods(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for method, args, kwargs in self.calls if method == name]


def _replay(direction: str) -> _OpRecorder:
    spec = importlib.util.spec_from_file_location("_d02_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = _OpRecorder()
    module.op = recorder  # type: ignore[attr-defined]
    getattr(module, direction)()
    return recorder


def test_downgrade_does_not_drop_indexes_by_name() -> None:
    recorder = _replay("downgrade")
    assert recorder.methods("drop_index") == [], "索引应随表删除，不能再按名字 DROP INDEX"


def test_downgrade_still_drops_the_owned_table() -> None:
    recorder = _replay("downgrade")
    dropped = [args for args, _kwargs in recorder.methods("drop_table")]
    assert ("gate_decisions",) in dropped


def test_upgrade_still_creates_the_three_indexes() -> None:
    recorder = _replay("upgrade")
    created = recorder.methods("create_index")
    assert len(created) == 3, f"预期 3 个索引，实际 {len(created)}"
    joined = " ".join(str(args) for args, _kwargs in created)
    for column in ("tenant_id", "project_id", "gate_key"):
        assert column in joined, f"索引定义里缺少列 {column}"
