"""账本结算不变量的**库层**验证（P1-05）。

被测命题：「一笔预留的 RELEASE 累计 + REFUND 累计 ≤ RESERVE」在数据库里成立，
而不是只在服务层某段 ``if`` 里成立。

为什么服务层求和不够（三条独立判决）
------------------------------------
1. **绕过服务层的写入者**：未来新增的代码、一次性修数据、迁移脚本都不会经过
   ``BudgetService``。只验服务层等于只验「当前这条调用路径」。
2. **并发**：两个事务各自 ``SUM(已结算)=0`` → 各判「0+满额 ≤ 预留」成立 →
   各写一笔，合计 2 倍。这个序列在单线程测试里永远不出现，必须真的把两个事务
   排在一起：一个持锁未提交，另一个必须**在锁上等**，而不是读到一个陈旧的 0。
3. **fail-closed**：结算指向不存在（或不在当前租户作用域）的预留时，库层必须
   拒绝。「看不见」不能当成「没有约束」。

串行化机制是触发器里对 ``reservation_ref`` 取事务级咨询锁
（``pg_advisory_xact_lock``）。用咨询锁而不是 ``SELECT ... FOR UPDATE``，是因为
``budget_quota_role`` 对账本没有 UPDATE 权限——为了加一把行锁而给账本开 UPDATE，
等于用「账本不可变」换锁。

断言只落在「库层是否拒绝」与「是否真的在锁上相遇」，不断言错误码文案：文案是
服务层的事（``tests/unit/test_budget_ledger_settlement.py`` 覆盖），这一层的价值
是证明那条语句在数据库里真的会被挡住。

所有主键都用随机后缀：这些用例会真提交数据，写死 id 会在第二次运行时撞主键，
于是一个「不变量是否成立」的用例变成「数据是否重复」的用例。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.shared.port_errors import DuplicateEntityError
from services.budget_quota.domain.budget_ledger import BudgetLedger
from services.budget_quota.repository.sqlalchemy_repos import (
    SqlAlchemyBudgetLedgerRepository,
)
from tests.conftest import set_test_scope

# 等锁上限：真卡住时报「等锁超时」，而不是让测试挂死。
_LOCK_TIMEOUT = "10s"
# 轮询 pg_locks 的上限（不是「睡一会儿看协程跑完没跑完」）。
_WAIT_TIMEOUT = 8.0
_WAIT_POLL = 0.02

_INSERT = text(
    """
    INSERT INTO budget_quota.budget_ledger
      (entity_id, entity_type, schema_version, entity_version, status, owner_module,
       tenant_id, created_at, created_by, updated_at, audit_ref, transaction_id,
       account_type, amount, currency, transaction_kind, idempotency_key,
       approval_ref, source, reservation_ref)
    VALUES
      (:entity_id, 'BudgetLedger', 1, 1, 'NO_LIFECYCLE', 'budget_quota',
       :tenant_id, now(), 'producer', now(), NULL, '',
       'SHOT_BUDGET', :amount, 'CNY', :kind, :idempotency_key,
       'ap-1', :source, :reservation_ref)
    """
)

_SETTLED_TOTAL = text(
    "SELECT COALESCE(SUM(amount), 0) FROM budget_quota.budget_ledger "
    "WHERE reservation_ref = :ref AND transaction_kind IN ('RELEASE', 'REFUND')"
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def _insert(
    session,
    *,
    tenant,
    entity_id: str,
    kind: str,
    amount: int,
    reservation_ref: str = "",
    source: str = "req-1",
) -> None:
    await session.execute(
        _INSERT,
        {
            "entity_id": entity_id,
            "tenant_id": tenant,
            "amount": amount,
            "kind": kind,
            "idempotency_key": f"k-{entity_id}",
            "reservation_ref": reservation_ref,
            "source": source,
        },
    )


async def _reserve(budget_sessionmaker, *, tenant, amount: int = 1000) -> str:
    """播一笔 RESERVE 并提交（返回它的 entity_id）。"""
    reserve_id = _uid("res")
    async with budget_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        await _insert(
            session,
            tenant=tenant,
            entity_id=reserve_id,
            kind="RESERVE",
            amount=amount,
        )
        await session.commit()
    return reserve_id


async def _settled_total(budget_sessionmaker, *, tenant, reserve_id: str) -> int:
    async with budget_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        total = await session.scalar(_SETTLED_TOTAL, {"ref": reserve_id})
    return int(total or 0)


# --------------------------------------------------------------------------- #
# 1. 绕过服务层：库层必须自己拒绝
# --------------------------------------------------------------------------- #
async def test_db_rejects_settlement_exceeding_reserve(budget_sessionmaker) -> None:
    tenant = uuid.uuid4()
    reserve_id = await _reserve(budget_sessionmaker, tenant=tenant, amount=1000)

    async with budget_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        await _insert(
            session,
            tenant=tenant,
            entity_id=_uid("rel"),
            kind="RELEASE",
            amount=1000,
            reservation_ref=reserve_id,
        )
        await session.commit()

    async with budget_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        with pytest.raises(DBAPIError) as excinfo:
            await _insert(
                session,
                tenant=tenant,
                entity_id=_uid("ref"),
                kind="REFUND",
                amount=1,
                reservation_ref=reserve_id,
            )
        # 拒绝理由必须能指向「累计 vs 预留」，否则现场只能看到一句 SQL 错误。
        assert "1000" in str(excinfo.value.orig)
        assert "1001" in str(excinfo.value.orig)

    # 失败的那一笔没有落库：账本上仍然只有那笔 1000 的 RELEASE。
    assert await _settled_total(
        budget_sessionmaker, tenant=tenant, reserve_id=reserve_id
    ) == 1000


async def test_db_allows_settlement_within_reserve(budget_sessionmaker) -> None:
    """对照组：合计不超预留时库层必须放行（防止守卫变成「什么都拒绝」）。"""
    tenant = uuid.uuid4()
    reserve_id = await _reserve(budget_sessionmaker, tenant=tenant, amount=1000)

    async with budget_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        await _insert(
            session,
            tenant=tenant,
            entity_id=_uid("rel"),
            kind="RELEASE",
            amount=600,
            reservation_ref=reserve_id,
        )
        await _insert(
            session,
            tenant=tenant,
            entity_id=_uid("ref"),
            kind="REFUND",
            amount=400,
            reservation_ref=reserve_id,
        )
        await session.commit()

    assert await _settled_total(
        budget_sessionmaker, tenant=tenant, reserve_id=reserve_id
    ) == 1000


async def test_db_rejects_settlement_pointing_at_missing_reserve(
    budget_sessionmaker,
) -> None:
    """fail-closed：预留不存在（或不在当前租户作用域）时拒绝记账。

    「看不见」不能被当成「没有约束」：跨租户结算在 RLS 下表现为查不到父行，
    若此时放行，唯一看得见的差异只是行数，而不是一个被拒绝的写入。
    """
    tenant = uuid.uuid4()
    ghost = _uid("ghost")
    async with budget_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        with pytest.raises(DBAPIError) as excinfo:
            await _insert(
                session,
                tenant=tenant,
                entity_id=_uid("rel"),
                kind="RELEASE",
                amount=10,
                reservation_ref=ghost,
            )
        assert "不存在" in str(excinfo.value.orig)


async def test_db_rejects_negative_amount(budget_sessionmaker) -> None:
    """负金额是绕过累计判定的另一条路（``REFUND -1000`` 等于凭空加额度）。"""
    tenant = uuid.uuid4()
    reserve_id = await _reserve(budget_sessionmaker, tenant=tenant, amount=1000)

    async with budget_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        with pytest.raises(DBAPIError) as excinfo:
            await _insert(
                session,
                tenant=tenant,
                entity_id=_uid("neg"),
                kind="REFUND",
                amount=-1000,
                reservation_ref=reserve_id,
            )
        assert "amount_nonneg" in str(excinfo.value.orig)


# --------------------------------------------------------------------------- #
# 2. 并发：两个事务必须在同一笔预留上串行
# --------------------------------------------------------------------------- #
async def _someone_waits_on_advisory_lock(observer) -> bool:
    """是否有后端正在等 advisory 锁。

    仓库里只有账本结算守卫使用咨询锁（``grep -r advisory`` 可复算），因此
    「有后端在等 advisory 锁」等价于「第二个结算事务被串行化了」。

    刻意**不**按 ``pg_stat_activity.datname = current_database()`` 过滤：观察者
    连的是迁移库连接，与业务库可能不是同一个库名，那样过滤会把真正在等锁的后端
    滤掉，用例就退化成「永远看不到等待」的假绿。
    """
    row = await observer.execute(
        text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
    )
    return bool(row.scalar_one())


async def test_concurrent_settlements_cannot_both_take_full_amount(
    budget_sessionmaker, admin_sessionmaker
) -> None:
    """T1 持「RELEASE 满额」未提交时，T2 的「REFUND 满额」必须等锁后失败。

    这正是服务层求和漏掉的时序：若没有串行化，T2 在 T1 提交前求和会读到 0，
    于是两笔都成立，账上出现 2 倍预留。
    """
    tenant = uuid.uuid4()
    reserve_id = await _reserve(budget_sessionmaker, tenant=tenant, amount=1000)

    async with admin_sessionmaker() as observer, budget_sessionmaker() as session1:
        await set_test_scope(session1, tenant_id=tenant)
        await _insert(
            session1,
            tenant=tenant,
            entity_id=_uid("con_rel"),
            kind="RELEASE",
            amount=1000,
            reservation_ref=reserve_id,
        )

        async def _second_settlement() -> str:
            async with budget_sessionmaker() as session2:
                await set_test_scope(session2, tenant_id=tenant)
                await session2.execute(
                    text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
                )
                await _insert(
                    session2,
                    tenant=tenant,
                    entity_id=_uid("con_ref"),
                    kind="REFUND",
                    amount=1000,
                    reservation_ref=reserve_id,
                )
                await session2.commit()
                return "committed"

        task = asyncio.create_task(_second_settlement())

        deadline = asyncio.get_running_loop().time() + _WAIT_TIMEOUT
        blocked = False
        while asyncio.get_running_loop().time() < deadline:
            if await _someone_waits_on_advisory_lock(observer):
                blocked = True
                break
            await asyncio.sleep(_WAIT_POLL)
        assert blocked, (
            "第二个结算事务没有在 advisory 锁上等待："
            "说明它读到了「已结算 0」的陈旧快照并直接写入——串行化没有生效"
        )

        # T1 提交后，T2 必须在同一笔预留上重新求和并拒绝。
        await session1.commit()

        with pytest.raises(DBAPIError):
            await task

    assert await _settled_total(
        budget_sessionmaker, tenant=tenant, reserve_id=reserve_id
    ) == 1000, "两笔满额结算都落库了：累计不变量被并发绕过"


# --------------------------------------------------------------------------- #
# 3. 端口失败契约：同预留同种类的重复写入要翻译成业务语义
# --------------------------------------------------------------------------- #
async def test_repository_translates_duplicate_settlement_kind(
    budget_sessionmaker,
) -> None:
    """同预留同种类写第二笔：端口必须抛端口契约异常，而不是裸 IntegrityError。

    裸驱动异常会一路冒成 500——把「你已经释放过这一笔」报成服务器错误，调用方
    只会一直重试，而重试永远失败。
    """
    tenant = uuid.uuid4()
    reserve_id = await _reserve(budget_sessionmaker, tenant=tenant, amount=1000)

    def _settlement(entity_id: str) -> BudgetLedger:
        return BudgetLedger(
            entity_id=entity_id,
            created_by="producer",
            tenant_id=tenant,
            transaction_id="",
            account_type="SHOT_BUDGET",
            amount=100,
            currency="CNY",
            transaction_kind="RELEASE",
            idempotency_key=f"k2-{entity_id}",
            approval_ref="ap-1",
            source=reserve_id,
            reservation_ref=reserve_id,
        )

    async with budget_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        repo = SqlAlchemyBudgetLedgerRepository(session)
        await repo.add(_settlement(_uid("dup")))
        with pytest.raises(DuplicateEntityError):
            await repo.add(_settlement(_uid("dup")))
        await session.rollback()
