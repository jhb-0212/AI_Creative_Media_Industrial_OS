"""收窄 outbox.dead_letter_replays.result 的允许集合到单值 SUCCEEDED。

问题
----
``c5d6e7f8a9b0`` 建表时把 ``ck_dead_letter_replays_result`` 定为
``result IN ('SUCCEEDED', 'FAILED')``，理由写在台账 ``V205-R42-D69-IMPL``
的残余风险第 1 条：「CHECK 保留两个值是为了将来出现多步重放时不必改表」。

但这条理由不成立。本实现的重放是**单事务**的「锁行 → 条件清标记 → 写审计」：
任何拒绝都发生在写之前（``DomainError``），事务提交即三件事同时成立，回滚即
一件都没发生。不存在「已受理但失败」的中间态，因此 ``FAILED`` 是一个**任何
代码路径都写不进去的值**。

保留它的代价
------------
不是「多一个值」这么轻。读代码的人看到 CHECK 允许 FAILED，会得出「失败路径
漏写了」的结论，然后去补一条本不该存在的失败分支——或者更糟，在别处 import
这个值做判定，让「可能出现 FAILED」变成代码里的假设。缺一个值会当场报错，
假的可选值会长期误导，后者更贵。

为什么是收窄而不是补一条失败路径
--------------------------------
补路径等于把「同事务全有或全无」改成「先占位再改写」，而 ``dead_letter_replays``
是 append-only（授权层无 UPDATE、触发器拒绝 UPDATE/TRUNCATE），改写终值根本
不可实现。为让一个值可达而破坏 append-only，是把判据迁就枚举，方向反了。

降级
----
降级恢复两值。收窄方向的降级总是安全（放宽约束），因此没有数据冲突风险；
但降级后的库会重新允许写入一个语义上不可达的值——那是历史形态，如实恢复。

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.outbox import (
    DEAD_LETTER_REPLAY_RESULT_CHECK,
    DEAD_LETTER_REPLAY_SUCCEEDED,
    SCHEMA_NAME,
)

revision = "f8a9b0c1d2e3"
down_revision: str | Sequence[str] | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "dead_letter_replays"
_CONSTRAINT = "ck_dead_letter_replays_result"
# 历史形态（c5d6e7f8a9b0）：降级时逐字恢复，不要「顺手」再加注释或调整空格。
_LEGACY_CHECK = "result IN ('SUCCEEDED', 'FAILED')"


def _precondition_do_block() -> str:
    """「库里不得存在非 SUCCEEDED 行」的前置检查，写成 SQL 而不是 Python。

    为什么不在 Python 侧 ``op.get_bind().execute(...).scalar_one()``：
    迁移会被 ``boundary_assertions`` 以 op 替身回放（``_rendered_migration_statements``），
    那种上下文里没有真实连接，任何「先查再判」的 Python 逻辑都会抛
    ``_EmptyResult has no attribute scalar_one``，把一条授权断言整体打成执行异常。
    判据下沉成 SQL 后，回放只看到一条语句文本，真实库上才执行——这也是本仓库
    的既有口径：能由数据库自己回答的问题，不要由迁移的 Python 侧代答。
    """
    return f"""
DO $$
DECLARE
    offending bigint;
BEGIN
    SELECT count(*) INTO offending
    FROM {SCHEMA_NAME}.{_TABLE}
    WHERE result <> '{DEAD_LETTER_REPLAY_SUCCEEDED}';
    IF offending > 0 THEN
        RAISE EXCEPTION
            '无法升级 {revision}：{SCHEMA_NAME}.{_TABLE} 存在 % 条 result 非 '
            '{DEAD_LETTER_REPLAY_SUCCEEDED} 的记录（绕过应用层的手工写入）。'
            '收窄约束会把它们变成非法行：请先查明并决定处置方式，'
            '不要为了让迁移通过而静默改写审计事实。', offending;
    END IF;
END
$$
"""


def upgrade() -> None:
    # 先判再改：若库里真有非 SUCCEEDED 行，直接 ADD CONSTRAINT 只会给出
    # 「some rows violate」而不说几条、为什么。
    op.execute(_precondition_do_block())
    op.drop_constraint(_CONSTRAINT, _TABLE, schema=SCHEMA_NAME, type_="check")
    op.create_check_constraint(
        _CONSTRAINT, _TABLE, DEAD_LETTER_REPLAY_RESULT_CHECK, schema=SCHEMA_NAME
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, schema=SCHEMA_NAME, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _LEGACY_CHECK, schema=SCHEMA_NAME)
