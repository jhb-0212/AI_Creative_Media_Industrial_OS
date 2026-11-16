"""驱动异常翻译器：把数据库完整性异常翻译成领域异常。

## 为什么需要这一层

审查第 16 条：四个 service 直接 ``from sqlalchemy.exc import IntegrityError``，
于是业务层与具体驱动绑死。由此带来两个具体问题，都不是"风格问题"：

1. **一个异常表达两件事。** ``IntegrityError`` 同时被用来表示"唯一约束冲突
   （并发重复提交）"和"乐观锁冲突（版本被推进）"。前者应幂等返回既有结果，
   后者应让调用方重试——两者处理方式相反，却只能靠"捕获后回查"猜。
   内存实现照抄了这个含混：它对**两种**情况都抛 ``IntegrityError``，而 SQL
   实现在乐观锁冲突时抛 ``ConcurrentModificationError``，两条实现语义并不等价。
2. **语义泄漏到 API。** 驱动异常没有错误码，逃出服务层就变成 500。业务层
   本该用"这是重复提交"或"这是并发冲突"这样的词说话。

## 分层口径

```
数据库驱动异常  →（仓储层：本模块）→ 领域异常  →（服务层判定）→ 结构化错误码
```

异常类型本身定义在 ``packages/errors/repository_errors.py``（无驱动依赖），
本模块只负责"识别与翻译"，因此**只有仓储层需要 import 它**。服务层不得
import SQLAlchemy——由闸门 R6 强制（`scripts/architecture_check.py`）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.exc import IntegrityError

from packages.errors.repository_errors import (
    DuplicateEntityError,
    IntegrityViolationError,
    ReferenceViolationError,
    RepositoryError,
)

#: PostgreSQL SQLSTATE 常量（只登记本项目实际会遇到的几类）。
_UNIQUE_VIOLATION = "23505"
_FOREIGN_KEY_VIOLATION = "23503"


def _sqlstate(exc: IntegrityError) -> str | None:
    """取驱动层 SQLSTATE（asyncpg 用 ``sqlstate``，其余驱动多用 ``pgcode``）。"""
    orig: Any = exc.orig
    for attr in ("sqlstate", "pgcode"):
        value = getattr(orig, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def translate_integrity_error(exc: IntegrityError, *, entity: str) -> RepositoryError:
    """把驱动完整性异常翻译为领域异常。

    ``entity`` 只用于错误详情（例如 ``asset_version_id=abc``），不含列值，
    避免把用户数据写进日志或响应。

    未识别的 SQLSTATE 一律落到 ``IntegrityViolationError``（内部错误）而不是
    静默当成功：约束冲突若被当成成功，数据不变量就失去了最后一道观测点。
    """
    state = _sqlstate(exc)
    if state == _UNIQUE_VIOLATION:
        return DuplicateEntityError(entity)
    if state == _FOREIGN_KEY_VIOLATION:
        return ReferenceViolationError(entity)
    return IntegrityViolationError(entity)


@contextmanager
def translate_integrity_errors(*, entity: str) -> Iterator[None]:
    """仓储层写法糖：``with translate_integrity_errors(entity=...): ...``。

    只翻译 ``IntegrityError``：其它驱动异常（连接断开、超时）不属于完整性
    语义，翻译成领域异常反而会掩盖"基础设施故障"这一事实，交由上层按
    不可用处理。
    """
    try:
        yield
    except IntegrityError as exc:
        raise translate_integrity_error(exc, entity=entity) from exc


__all__ = ["translate_integrity_error", "translate_integrity_errors"]
