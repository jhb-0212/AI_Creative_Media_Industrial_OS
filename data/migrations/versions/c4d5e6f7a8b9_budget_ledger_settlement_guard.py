"""账本结算不变量：RELEASE 累计 + REFUND 累计 ≤ RESERVE（P1-05）

缺陷是什么
----------
``_settle`` 原本只判「本次金额 ≤ 预留金额」——**逐笔**判定。于是

    RESERVE 1000 → RELEASE 1000 → REFUND 1000

每一笔都合法，合计 2000 却是预留的 2 倍。同一笔钱被退两次，账面上看不出来：
账本是追加式流水，没有任何一行「余额」字段，超支只能靠求和发现，而原来没有
任何地方求和。

为什么不能只在服务层求和
------------------------
服务层「先求和、再写入」在两个并发事务之间必然漏：

    T1: SUM(已结算)=0 → 判定 0+1000 ≤ 1000 成立
    T2: SUM(已结算)=0 → 判定 0+1000 ≤ 1000 成立   ← 各自都看不见对方未提交的行
    T1/T2 各写一笔 → 合计 2000

（部分唯一索引 ``uq_budget_quota_ledger_reservation_kind`` 只挡「同 kind 重复」，
挡不住「一个 RELEASE + 一个 REFUND」，这正是上面的时序。）

所以本迁移把不变量放到库层，且**串行化**：触发器对 ``reservation_ref`` 取

    pg_advisory_xact_lock(hashtextextended(reservation_ref, 0))

事务级咨询锁，取锁后重新求和，超过预留即拒绝。为什么用咨询锁而不是
``SELECT ... FOR UPDATE``：``budget_quota_role`` 对 ``budget_ledger`` 只有
SELECT/INSERT（这是分域的核心收益——「改一笔已落库的金额」在库层不可能）。
行锁需要 UPDATE 权限，为了加一把锁就把账本改成可 UPDATE，等于用不变量换锁。
咨询锁不需要任何表权限，且按预留键取，不会与业务行锁交叉成死锁。

触发器对 INSERT 与 UPDATE 都生效（域角色没有 UPDATE 权限，但超级用户/迁移角色
有；只在 INSERT 上守，等于给「用管理员连接改账」留门）。求和时排除自身
（``entity_id <> NEW.entity_id``），因此 INSERT 与 UPDATE 共用同一段判定。

错误码用 ``23514``（check_violation）而不是默认的 ``P0001``
----------------------------------------------------------
``packages/shared/port_errors.py`` 只把 ``23xxx`` 翻译成端口契约异常；``P0001``
会被当成「未知失败」原样上抛，最终变成 500。而这里的语义恰恰是「取值/引用不
成立」（累计超预留、预留不存在）＝ ``ReferentialIntegrityError``。用 23514 既
符合语义，又让服务层能把它翻译成 4xx，不必新增一条按消息文本匹配的判据。

附带一条 ``amount >= 0``：负金额流水是绕过上述判定的另一条路（写一条
``REFUND -1000`` 等于凭空增加可用额度）。域层已经拒绝非正数，库层再钉一次——
域层是入口，库层是事实。

Revision ID: c4d5e6f7a8b9
Revises: a2b3c4d5e6f7
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from services.budget_quota.domain.enums import SETTLEMENT_KINDS

revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "budget_quota"
_TABLE = "budget_ledger"
_AMOUNT_CHECK = "ck_budget_ledger_amount_nonneg"
_GUARD_CONSTRAINT = "ck_budget_ledger_settlement_within_reserve"
_GUARD_FUNCTION = f"{_SCHEMA}.budget_ledger_settlement_guard"
_GUARD_TRIGGER = "trg_budget_ledger_settlement_guard"

# 结算种类集合从领域枚举派生：写死一份的下场是「加了新结算种类，累计口径静默
# 漏算」——而漏算不报错，它只是让「累计结算」永远小于真实值。
_SETTLEMENT_KIND_SQL = ", ".join(f"'{kind.value}'" for kind in sorted(SETTLEMENT_KINDS))

_GUARD = f"""
CREATE OR REPLACE FUNCTION {_GUARD_FUNCTION}()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    reserved integer;
    settled integer;
BEGIN
    -- 只有结算类流水受「累计不超预留」约束；RESERVE 自身不参与。
    IF NEW.transaction_kind NOT IN ({_SETTLEMENT_KIND_SQL}) THEN
        RETURN NEW;
    END IF;

    IF NEW.reservation_ref = '' THEN
        RAISE EXCEPTION
            '结算流水必须指回被结算的预留（transaction_kind=%）',
            NEW.transaction_kind
            USING ERRCODE = '23514', CONSTRAINT = '{_GUARD_CONSTRAINT}';
    END IF;

    -- 同一笔预留的结算必须串行（理由见模块文档：并发下服务层求和必然同时放过）。
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(NEW.reservation_ref, 0)
    );

    SELECT amount INTO reserved
      FROM {_SCHEMA}.{_TABLE}
     WHERE entity_id = NEW.reservation_ref
       AND transaction_kind = 'RESERVE';

    IF NOT FOUND THEN
        -- fail-closed：读不到预留（不存在，或不在当前租户作用域内）就拒绝记账。
        -- 把「看不见」当成「没有约束」会让跨租户结算静默通过 RLS 只剩行数差异。
        RAISE EXCEPTION
            '结算指向的预留言本 % 不存在或不在当前作用域，拒绝记账',
            NEW.reservation_ref
            USING ERRCODE = '23514', CONSTRAINT = '{_GUARD_CONSTRAINT}';
    END IF;

    SELECT COALESCE(SUM(amount), 0) INTO settled
      FROM {_SCHEMA}.{_TABLE}
     WHERE reservation_ref = NEW.reservation_ref
       AND transaction_kind IN ({_SETTLEMENT_KIND_SQL})
       AND entity_id <> NEW.entity_id;

    IF settled + NEW.amount > reserved THEN
        RAISE EXCEPTION
            '累计结算 % 将超过预留 %（已结算 %，本次 %）：'
            '一笔预留的 RELEASE+REFUND 合计不得超过 RESERVE',
            settled + NEW.amount, reserved, settled, NEW.amount
            USING ERRCODE = '23514', CONSTRAINT = '{_GUARD_CONSTRAINT}';
    END IF;

    RETURN NEW;
END;
$$;
"""

# 为什么不在这里写 GRANT
# ----------------------
# 触发函数的 EXECUTE 只在 ``CREATE TRIGGER`` 时校验（执行者＝迁移角色＝函数属主），
# 触发时**不再校验**。实测确认过：把 EXECUTE 从 PUBLIC 与 budget_quota_role 上
# 一并撤掉后，同一句 INSERT 依然照常触发守卫并按预期拒绝——
#
#   REVOKE EXECUTE ON FUNCTION ... FROM PUBLIC, budget_quota_role;
#   SET ROLE budget_quota_role; INSERT ... → ERROR: 累计结算 1001 将超过预留 1000
#
# 既然不需要，就不该写。授权面的 DDL 归 ``packages/shared/db_grants.py`` 的矩阵 +
# ``converge_statements`` 统一执行（DATA-6：迁移是矩阵的执行器，不得手写 GRANT）；
# 在迁移里手写一条 GRANT 会让「谁被授了什么」出现第二个事实源。

_CREATE_TRIGGER = f"""
CREATE TRIGGER {_GUARD_TRIGGER}
    BEFORE INSERT OR UPDATE ON {_SCHEMA}.{_TABLE}
    FOR EACH ROW EXECUTE FUNCTION {_GUARD_FUNCTION}()
"""

_DROP_ALL = (
    f"DROP TRIGGER IF EXISTS {_GUARD_TRIGGER} ON {_SCHEMA}.{_TABLE}",
    f"DROP FUNCTION IF EXISTS {_GUARD_FUNCTION}()",
)


def upgrade() -> None:
    # NOT VALID + VALIDATE：存量行不阻塞升级，但**必须立刻 VALIDATE**——
    # 「先建后验」留下的永久 NOT VALID 等于一条不生效的约束（台账只认
    # convalidated ∧ 违规行数=0）。
    op.execute(
        f"ALTER TABLE {_SCHEMA}.{_TABLE} "
        f"ADD CONSTRAINT {_AMOUNT_CHECK} CHECK (amount >= 0) NOT VALID"
    )
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} VALIDATE CONSTRAINT {_AMOUNT_CHECK}")

    # 函数体内部有 $ 引号，但整条 CREATE FUNCTION 仍是一条语句，asyncpg 可执行。
    op.execute(_GUARD)
    op.execute(_CREATE_TRIGGER)


def downgrade() -> None:
    for sql in _DROP_ALL:
        op.execute(sql)
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} DROP CONSTRAINT {_AMOUNT_CHECK}")


__all__ = ["down_revision", "downgrade", "revision", "upgrade"]
