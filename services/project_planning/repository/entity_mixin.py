"""兼容再导出：信封 ORM mixin 已上提到 ``packages/shared/orm.py``。

保留本文件只为让历史导入路径继续可用；新增代码请直接导入
``packages.shared.orm``。单一事实源在 shared，本文件不含任何实现。
"""

from __future__ import annotations

from packages.shared.orm import SCHEMA_VERSION, EntityMixin

__all__ = ["SCHEMA_VERSION", "EntityMixin"]
