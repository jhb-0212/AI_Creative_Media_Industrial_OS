"""兼容再导出：SQLAlchemy OutboxWriter 已上提到 ``packages/shared/outbox_writer.py``。"""

from __future__ import annotations

from packages.shared.outbox_writer import SqlAlchemyOutboxWriter

__all__ = ["SqlAlchemyOutboxWriter"]
