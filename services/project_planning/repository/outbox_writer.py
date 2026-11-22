"""兼容再导出：OutboxWriter 端口已上提到 ``packages/shared/outbox_writer.py``。

保留本文件只为让历史导入路径继续可用；新增代码请直接导入
``packages.shared.outbox_writer``。
"""

from __future__ import annotations

from packages.shared.outbox_writer import OutboxWriter

__all__ = ["OutboxWriter"]
