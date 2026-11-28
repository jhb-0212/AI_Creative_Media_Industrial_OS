"""集成测试数据库版本闸门（C4）的判定用例。

为什么给「测试环境的闸门」写测试：闸门失效的表现不是红灯，而是**静默放行**。
它不会报错，只会让后面的用例在错误的库上产出绿色结论——这比红灯难发现，
也正是「绿得没道理」的来源。所以判定逻辑单独成纯函数，在这里逐种形态验证。

判定对象是**集合**而不是单值：``alembic_version`` 支持多行（多 head 分叉），
「落后」「超前」「分叉」「空库」都必须落进同一个结论——不放行。
"""

from __future__ import annotations

import pytest

from tests.integration._db_head import DatabaseHeadMismatchError, check_database_at_head

HEAD = ("f9b0c1d2e3f4",)


def test_matching_revision_passes() -> None:
    check_database_at_head(HEAD, HEAD)


def test_head_order_does_not_matter_for_multi_head() -> None:
    """多 head 库只要集合相等就通过，顺序不是事实。"""
    check_database_at_head(("aaa", "bbb"), ("bbb", "aaa"))


def test_stale_database_is_rejected() -> None:
    """落后一个版本：这是本批次要拦住的真实形态（本地库曾停在 b1c2d3e4f5a6）。"""
    with pytest.raises(DatabaseHeadMismatchError) as exc_info:
        check_database_at_head(("b1c2d3e4f5a6",), HEAD)

    message = str(exc_info.value)
    assert "b1c2d3e4f5a6" in message
    assert "f9b0c1d2e3f4" in message
    assert "alembic upgrade head" in message


def test_ahead_of_repository_is_rejected() -> None:
    """库比仓库新同样不可信：跑的是一份仓库里不存在的结构。"""
    with pytest.raises(DatabaseHeadMismatchError):
        check_database_at_head(("ffffffffffff",), HEAD)


def test_diverged_heads_are_rejected() -> None:
    """分叉库（多 head 与仓库不一致）不能当成有效环境。"""
    with pytest.raises(DatabaseHeadMismatchError):
        check_database_at_head(("aaa", "bbb"), HEAD)


def test_empty_database_is_rejected() -> None:
    """空库（没有 version 行）必须给出「未迁移」而不是 IndexError。"""
    with pytest.raises(DatabaseHeadMismatchError) as exc_info:
        check_database_at_head((), HEAD)
    assert "未迁移" in str(exc_info.value) or "为空" in str(exc_info.value)


def test_repository_heads_reads_alembic_script_directory() -> None:
    """仓库 head 必须从 alembic.ini 读，而不是写死一份快照。"""
    from tests.integration._db_head import repository_heads

    heads = repository_heads()
    assert heads, "仓库没有任何迁移 head，alembic.ini 的 script_location 是否正确？"
    # head 会随迁移演进；这里只断言「读得到且是 revision 形态」，
    # 写死具体值会让每次加迁移都要改这条用例（闸门退化成手工台账）。
    assert all(len(head) == 12 and head.isalnum() for head in heads)
