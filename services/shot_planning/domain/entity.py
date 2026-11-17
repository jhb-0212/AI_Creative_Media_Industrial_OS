"""shot_planning 模块对共享实体信封的兼容再导出。

与 project_planning.domain.entity 同构：信封是跨模块机制，单一事实源在
``packages/shared/entity.py``；本文件只做显式再导出 + 本模块所有权常量。
"""

from __future__ import annotations

from packages.shared.entity import SCHEMA_VERSION, Entity, utcnow

# 本模块的数据所有权标识（覆写到各聚合根的 owner_module 默认值）。
OWNER_MODULE = "shot-planning"

__all__ = ["OWNER_MODULE", "SCHEMA_VERSION", "Entity", "utcnow"]
