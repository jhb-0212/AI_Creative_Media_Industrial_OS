"""仓储层领域异常（不依赖任何数据库驱动）。

为什么单独成模块，而不是放在 ``packages/shared/persistence.py`` 里：
翻译函数必须 ``import sqlalchemy``（它读驱动异常的 SQLSTATE），但这些异常
本身是**领域语义**——服务层要捕获它们，而服务层不该因为捕获异常就拉进
SQLAlchemy。依赖方向固定为：

```
packages/errors/repository_errors.py   ← 纯领域异常（无驱动依赖）
packages/shared/persistence.py         ← 翻译器（依赖驱动 + 上面的异常）
services/*/repository/*                ← 仓储实现（捕获驱动异常并翻译）
services/*/service/*                   ← 只捕获领域异常
```

语义区分（这是本模块存在的核心理由）：

| 异常 | 含义 | 正确处置 |
|---|---|---|
| ``DuplicateEntityError`` | 同一请求被重复提交（唯一约束兜住竞态） | 幂等：内容一致则返回既有记录 |
| ``ConcurrentModificationError`` | 两个不同请求并发改同一实体（乐观锁） | 重试：必须基于最新版本重算 |
| ``ReferenceViolationError`` | 引用了不存在的对象（外键悬空） | 拒绝：调用方参数错误 |
| ``IntegrityViolationError`` | 其它完整性冲突（CHECK/NOT NULL/触发器） | 报警：实现缺陷，不是用户输入问题 |

把前两者合并成一个 ``IntegrityError`` 的后果不是"少一层抽象"，而是**处置反了**：
并发修改被当成重复提交去幂等返回，等于把别人的修改结果当成本次请求的结果。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry


class RepositoryError(DomainError):
    """仓储层异常的基类（便于调用方按层级捕获）。"""


class DuplicateEntityError(RepositoryError):
    """唯一约束/主键冲突：同一实体被并发插入。"""

    def __init__(self, entity: str) -> None:
        super().__init__(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail=f"实体 {entity} 已存在（并发重复提交）",
        )


class ReferenceViolationError(RepositoryError):
    """外键冲突：引用了不存在的对象（如 QC 报告挂在不存在的资产上）。"""

    def __init__(self, entity: str) -> None:
        super().__init__(
            ErrorRegistry.VALIDATION_ERROR,
            detail=f"实体 {entity} 引用的对象不存在或已被移除",
        )


class IntegrityViolationError(RepositoryError):
    """其它完整性约束冲突（CHECK / NOT NULL / 触发器拒绝）。

    归为内部错误而不是校验错误：走到这里说明**代码写入了自己不该写的值**
    （域枚举与数据库约束不一致、迁移漏更新），属于实现缺陷而非用户输入问题。
    """

    def __init__(self, entity: str) -> None:
        super().__init__(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"实体 {entity} 违反数据库完整性约束（实现缺陷，需排查）",
        )


__all__ = [
    "DuplicateEntityError",
    "IntegrityViolationError",
    "ReferenceViolationError",
    "RepositoryError",
]
