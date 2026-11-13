"""工作单元（Unit of Work）边界——全进程唯一实现（R07）。

为什么要它：
- 事务边界只允许一种语义，API 用例与 Worker 任务共用同一实现，
  不允许「请求层一套提交逻辑、Worker 又抄一套」。
- 提交点前移到**用例返回响应之前**（由路由显式 ``await uow.commit()``），
  不依赖 FastAPI yield 依赖的退出码——退出码在响应发出之后才执行，
  一旦提交失败，客户端已经收到了成功响应。
- 只读单元用 ``SET TRANSACTION READ ONLY`` 在**数据库层**兜底：
  GET 用例即使误写也写不进去（防呆，不靠代码自觉）。

取消语义：``CancelledError`` / ``GeneratorExit`` 属 BaseException，不在此
``await rollback``（取消态里再次 await 会二次取消），由 ``async with`` 退出时
``close()`` 隐式回滚。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SessionMaker = async_sessionmaker[AsyncSession]


class UnitOfWork:
    """一次业务用例的数据库工作单元。

    只暴露 ``session`` 与两个显式动作：
    - ``commit()``：写用例在返回响应前调用，确定性地落库。
    - ``rollback()``：异常路径由上下文管理器调用，业务代码通常不需要。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()


@asynccontextmanager
async def unit_of_work(
    sessionmaker: SessionMaker, *, read_only: bool = False
) -> AsyncIterator[UnitOfWork]:
    """打开一个工作单元：正常退出不自动提交，异常自动回滚。

    - 写单元必须由调用方显式 ``commit()``；忘记提交 = 事务不落库（fail-closed）。
    - 只读单元先声明 ``SET TRANSACTION READ ONLY``，写语句由数据库拒绝。
    """
    async with sessionmaker() as session:
        if read_only:
            await session.execute(text("SET TRANSACTION READ ONLY"))
        uow = UnitOfWork(session)
        try:
            yield uow
        except Exception:
            await session.rollback()
            raise


async def get_uow(request: Request) -> AsyncIterator[UnitOfWork]:
    """写工作单元依赖（会话工厂来自 lifespan 挂在 app.state 的实例）。"""
    async with unit_of_work(request.app.state.sessionmaker) as uow:
        yield uow


async def get_read_uow(request: Request) -> AsyncIterator[UnitOfWork]:
    """只读工作单元依赖（GET/HEAD 用例使用，数据库层拒绝写入）。"""
    async with unit_of_work(request.app.state.sessionmaker, read_only=True) as uow:
        yield uow
