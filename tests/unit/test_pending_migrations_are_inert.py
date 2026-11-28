"""``data/migrations/pending/`` 必须在链外：不许悄悄进 ``versions/``。

为什么用测试钉这条
------------------
pending 目录装的是分支（integration）已激活、main 链尚未合并的迁移源。
它们的危险不是「存在」，而是「被复制进 ``versions/``」：alembic 会把它们
当作 main 链的下一步，而它们的 ``down_revision`` 指向分支链节点。轻则
``Cycle is detected``，重则一个从未在 main 链验收过的 DDL 被自动执行。

已实测到一处真实隐患：pending 里有两个 revision id
（``d2e3f4a5b6c7`` / ``f8a9b0c1d2e3``）与 main 链**已被占用**的 id 撞车。
所以合并时不是「拷进去就行」，而是必须先重编号——本条把它写成显式常量，
让「撞车集合变大」成为一个会红的信号，而不是一次静默的 ``Duplicate revision``。
"""

from __future__ import annotations

import re
from pathlib import Path

_VERSIONS = Path("data/migrations/versions")
_PENDING = Path("data/migrations/pending/feat-ownership")
_REVISION_RE = re.compile(r'^revision(?::\s*str)?\s*=\s*[\'"]([0-9a-z]+)[\'"]', re.M)

# 已知、且已在 main 链落地的 id。合并 pending 时必须重编号，不能原样拷贝。
_KNOWN_COLLISIONS = frozenset({"d2e3f4a5b6c7", "f8a9b0c1d2e3"})


def _revisions(directory: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(directory.rglob("*.py")):
        match = _REVISION_RE.search(path.read_text(encoding="utf-8"))
        if match:
            found.setdefault(match.group(1), str(path))
    return found


def test_pending_migrations_stay_out_of_the_main_chain() -> None:
    assert _PENDING.is_dir(), "pending 目录不存在时本条断言失去意义，应显式报错"
    versions = _revisions(_VERSIONS)
    pending = _revisions(_PENDING)
    assert pending, "pending 目录里应有迁移源；一个都读不到说明路径或格式变了"

    for path in pending.values():
        assert not Path(path).is_relative_to(_VERSIONS), (
            f"{path} 位于 versions/ 下：pending 迁移必须留在链外"
        )

    new_collisions = set(versions) & set(pending) - _KNOWN_COLLISIONS
    assert not new_collisions, (
        "出现了新的 revision id 撞车："
        f"{sorted(new_collisions)}。撞车的 pending 迁移在合并时必须重编号，"
        "否则 alembic 会报 Duplicate revision 或把分支链当成 main 链的下一步"
    )


def test_known_collisions_still_need_renumbering() -> None:
    """把已知撞车也钉住：若哪天它们被「修好」了，这条会红并提醒更新常量。"""
    versions = _revisions(_VERSIONS)
    pending = _revisions(_PENDING)
    actual = set(versions) & set(pending)
    assert actual == _KNOWN_COLLISIONS, (
        f"已知撞车集合变了（实际 {sorted(actual)}）；"
        "请同步更新 _KNOWN_COLLISIONS，避免这条守卫变成永真"
    )
