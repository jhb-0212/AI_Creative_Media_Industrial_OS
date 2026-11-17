"""租约时间口径（领域层的唯一实现）。

为什么单独一个函数：租约到期时刻既被单任务领取（``GenerationTaskLifecycle``）
使用，也被 worker 的批量领取（仓储层 SQL 参数）使用。如果两处各算一遍
``now + timedelta(seconds=n)``，某天「续租算错 60 倍」这类 bug 只会出现在
其中一处，而两处都能"正常跑"。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from packages.errors.error_codes import DomainError, ErrorRegistry


def lease_deadline(lease_seconds: float, now: datetime) -> datetime:
    """计算租约到期时刻；非正数租约是配置错误，不是"立刻回收"。"""
    if lease_seconds <= 0:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail="lease_seconds 必须 > 0（租约窗口不能为零或负）",
        )
    return now + timedelta(seconds=lease_seconds)
