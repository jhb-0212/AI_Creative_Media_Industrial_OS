"""兼容再导出：乐观锁基础设施已上提到 ``packages/shared/versioning.py``。

保留本文件只为让历史导入路径继续可用；新增代码请直接导入
``packages.shared.versioning``。单一事实源在 shared，本文件不含实现。
"""

from __future__ import annotations

from packages.shared.versioning import (
    ConcurrentModificationError,
    _orm_update_values,
    conditional_update,
)

__all__ = [
    "ConcurrentModificationError",
    "_orm_update_values",
    "conditional_update",
]
