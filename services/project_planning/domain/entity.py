"""project_planning 模块对共享实体信封的兼容再导出。

背景（为什么只剩一个转发文件）：
- C.3.1 信封是跨模块机制，已上提到 ``packages/shared/entity.py``（单一事实源）。
- 本模块的领域类与测试历史上都从 ``services.project_planning.domain.entity``
  导入 Entity / utcnow；保留本文件做一次显式再导出，避免一次性重命名
  制造大面积无意义 diff。新增代码应直接从 ``packages.shared.entity`` 导入。

注意：``OWNER_MODULE`` 保留为本模块常量（project-planning），各聚合根用它
覆写信封默认所有者，数据所有权仍归属本模块。
"""

from __future__ import annotations

from packages.shared.entity import SCHEMA_VERSION, Entity, utcnow

# 本模块的数据所有权标识（覆写到各聚合根的 owner_module 默认值）。
OWNER_MODULE = "project-planning"

__all__ = ["OWNER_MODULE", "SCHEMA_VERSION", "Entity", "utcnow"]
