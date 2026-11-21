"""端到端验证：SQLAlchemy 事务里的 ``FOR SHARE`` 是否真的锁住父行。

为什么在锁语义实测（``verify_parent_freeze_lock.py`` 9/9 PASS）之外还要这一个：

那个脚本用裸 asyncpg 证明「``FOR SHARE`` 与归档 UPDATE 冲突」。它证明的是
**数据库语义**，不是**本仓库的调用路径**。真正上线跑的是 SQLAlchemy
``select(...).with_for_update(read=True)``，中间还隔着会话事务边界：

- 若会话落在 autocommit / AUTOCOMMIT 隔离级别上，``FOR SHARE`` 在语句结束即释放，
  锁形同虚设，而所有代码看起来都对；
- 若调用方忘了开事务，同上。

这类失效**不会报错**，只会让冻结检查在并发下穿透。所以必须端到端测一遍：
用真实 engine + 真实仓储方法 + 两个会话，断言归档方真的被挡在锁外。

R1  带锁读能读到父行，且读到 ACTIVE。
R2  持锁期间，另一会话用乐观锁条件 UPDATE 归档 —— 必须被阻塞（LOCK_TIMEOUT）。
R3  持锁方提交后，归档方能成功；随后新的带锁读读到 ARCHIVED。
    （R3 是 R2 的对照组：若归档一直失败，说明锁根本没释放，是死锁而非正确。）
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from services.project_planning.domain.enums import ProjectStatus
from services.project_planning.repository.sqlalchemy_repo import (
    SqlAlchemyProjectRepository,
)

HOST, PORT, USER, PWD, TEMPLATE = (
    lab.DEFAULT_HOST,
    lab.DEFAULT_PORT,
    lab.DEFAULT_USER,
    lab.DEFAULT_PASSWORD,
    lab.DEFAULT_TEMPLATE,
)
LAB = "lab_parent_freeze_e2e"
TENANT = "00000000-0000-0000-0000-000000000001"
ORG = "00000000-0000-0000-0000-000000000011"
PROJECT = "proj-alpha"
SVC = "svc_project_planning"
LOCK_TIMEOUT_MS = 400

results: list[dict] = []


def record(cid: str, ok: bool, expect: str, observed: str) -> None:
    status = "PASS" if ok else "FAIL"
    results.append({"id": cid, "status": status, "expect": expect, "observed": observed})
    print(f"[{status}] {cid}")
    print(f"       期望: {expect}")
    print(f"       实测: {observed}")


def sessionmaker_for(database: str) -> tuple[async_sessionmaker, any]:
    engine = create_async_engine(
        f"postgresql+asyncpg://{USER}:{PWD}@{HOST}:{PORT}/{database}",
        pool_size=4,
        max_overflow=0,
    )
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def enter_scope(session) -> None:
    """置为业务角色并注入租户上下文，使 RLS 放行（与生产请求作用域等价）。

    ``SET`` 不支持绑定参数，故用常量拼接——这里的值是本文件内的字面量，
    不含外部输入，不构成注入面。
    """
    await session.execute(text(f"SET ROLE {SVC}"))
    await session.execute(text(f"SET app.current_tenant_id = '{TENANT}'"))
    await session.execute(text(f"SET app.current_org_id = '{ORG}'"))
    await session.execute(text(f"SET app.current_projects = '{PROJECT}'"))


def is_lock_timeout(exc: DBAPIError) -> bool:
    """SQLSTATE 55P03 = lock_not_available。

    刻意不按异常类名判：SQLAlchemy 会用方言层包装 asyncpg 异常，类名变成
    ``AsyncAdapt_asyncpg_dbapi.Error``，「看起来不是 LockNotAvailable」；
    也不按「任何数据库错误都算阻塞」判——那会把版本冲突、约束违反一并吞成
    PASS，等于把判定放宽到永不失败。SQLSTATE 是唯一稳定的口径。
    """
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate == "55P03":
        return True
    return "lock timeout" in str(orig).lower()


async def main() -> int:
    admin = await lab.admin_conn(HOST, PORT, USER, PWD)
    try:
        await lab.fresh_clone(admin, LAB, TEMPLATE)
        sm, engine = sessionmaker_for(LAB)

        holder = sm()
        archiver = sm()
        try:
            # ---- R1 带锁读 ----
            await enter_scope(holder)
            repo = SqlAlchemyProjectRepository(holder)
            project = await repo.get_by_id_for_child_write(PROJECT)
            in_tx = holder.in_transaction()
            record(
                "R1",
                project is not None and project.status == ProjectStatus.ACTIVE and in_tx,
                "读到父行且 status=ACTIVE，且会话处于事务中",
                f"project={project.entity_id if project else None} "
                f"status={project.status.value if project else None} in_transaction={in_tx}",
            )

            # ---- R2 持锁期间归档必须被挡 ----
            await enter_scope(archiver)
            arch_repo = SqlAlchemyProjectRepository(archiver)

            async def try_archive() -> tuple[bool, str]:
                try:
                    await archiver.execute(
                        text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
                    )
                    p = await arch_repo.get_by_id(PROJECT)
                    if p is None:
                        return False, "项目不存在（前置失败，非锁问题）"
                    p.status = ProjectStatus.ARCHIVED  # 直接改状态，绕过边校验，只测锁
                    # expected_version 是「读取时的 entity_version」，由
                    # conditional_update 统一推进为 +1。写成 -1 会让条件恒不命中，
                    # 得到 ConcurrentModificationError —— 那是版本冲突，不是锁阻塞，
                    # 两者都会「失败」，但被测命题完全不同。
                    await arch_repo.save(p, expected_version=p.entity_version)
                    await archiver.commit()
                    return True, "归档成功（未被阻塞）"
                except DBAPIError as exc:
                    await archiver.rollback()
                    if is_lock_timeout(exc):
                        return True, f"被阻塞：{exc.orig}"
                    return False, f"其他数据库错误：{exc.orig}"
                except Exception as exc:
                    await archiver.rollback()
                    return False, f"{type(exc).__name__}: {exc}"

            blocked, note = await asyncio.wait_for(try_archive(), timeout=15)
            record(
                "R2",
                blocked and "被阻塞" in note,
                f"被阻塞（LockNotAvailable，{LOCK_TIMEOUT_MS}ms 超时）",
                note,
            )

            # ---- R3 提交后归档成功，新读读到 ARCHIVED ----
            await holder.commit()

            ok_after, note_after = await asyncio.wait_for(try_archive(), timeout=15)
            # 归档后新的带锁读
            await enter_scope(archiver)
            again = await SqlAlchemyProjectRepository(archiver).get_by_id_for_child_write(
                PROJECT
            )
            await archiver.rollback()
            record(
                "R3",
                ok_after and again is not None and again.status == ProjectStatus.ARCHIVED,
                "锁释放后归档成功；新带锁读读到 ARCHIVED",
                f"{note_after}；新读 status={again.status.value if again else None}",
            )
        finally:
            for s in (holder, archiver):
                await s.close()
            await engine.dispose()
    finally:
        await lab.drop_lab(admin, LAB)
        await admin.close()

    failed = [r["id"] for r in results if r["status"] == "FAIL"]
    print(f"\n判定：{len(results) - len(failed)}/{len(results)} PASS"
          + (f"，FAIL={failed}" if failed else ""))
    print(f"总体：{'PASS' if not failed else 'FAIL'}")
    out = Path(__file__).with_suffix(".json")
    out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "lab": LAB,
                "verdict": "PASS" if not failed else "FAIL",
                "cases": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"证据：{out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
