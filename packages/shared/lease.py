"""租约型 Worker 的短工作单元契约（生成 / QC 共用）。

为什么需要这个契约
------------------
租约要能作为「谁正在跑这个任务」的事实，**必须在领取那一刻就提交**。如果领取、
执行、终结共用一个数据库事务，那么在耗时执行期间：租约对其他会话不可见（别人
仍读到 REQUESTED，看不见任务已被持有），行锁也一直被持有（其他 Worker 领不到
后续任务）。这不是「加把锁」能修的，而是事务边界画错了。

因此 Worker 不在构造时绑死一个 Session，而是按阶段打开短工作单元：

    领取：读候选（SKIP LOCKED）→ claim → commit → 立刻释放锁
    心跳：renew → commit
    终结：finish → commit
    回收：release_expired → commit

本模块只定义这条边界，不含任何仓储或 SQLAlchemy 实现——实现由各 Worker 的入口
进程注入（见 ``workers/*/__main__.py``），内核因此仍可用内存替身做单测。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol


class LeaseScope(Protocol):
    """一个阶段内可用的服务与仓储，外加显式提交。

    ``commit`` 必须由作用域实现暴露，而不是让内核去猜底层是 Session 还是别的东西：
    内核的职责是「什么时候提交」，不是「提交谁」。
    """

    service: Any
    repository: Any

    async def commit(self) -> None: ...


LeaseScopeFactory = Callable[[], AbstractAsyncContextManager[LeaseScope]]

__all__ = ["LeaseScope", "LeaseScopeFactory"]
