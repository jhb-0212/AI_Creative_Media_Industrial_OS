"""进程内仓储与 Outbox（单元测试 / 无数据库 MVP 实现）。

供 R16-D3 新增服务的领域测试与轻量运行时使用；生产持久化走各服务的
SQLAlchemy 实现（与既有 project_planning / asset_service 同构）。本模块不含
任何 ORM 依赖，保证领域测试可离线运行。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.versioning import ConcurrentModificationError


class InMemoryRepository:
    """按 entity_id 索引的进程内仓储（乐观锁语义与 SQLAlchemy 实现对齐）。"""

    def __init__(self) -> None:
        self._rows: dict[str, Any] = {}
        # 乐观锁版本单独登记：实体对象是按引用存储的，transition_to 会原地改
        # entity_version。若 save 直接比对「当前对象版本」，会把自己刚推进过的
        # 版本误判成并发冲突。这里记录「最近一次落库时的版本」，与 SQLAlchemy
        # conditional_update 比对「库里已持久化的版本」同口径。
        self._persisted_versions: dict[str, int] = {}

    async def get_by_id(self, entity_id: str) -> Any | None:
        return self._rows.get(entity_id)

    async def get_route_by_ref(
        self,
        route_ref: str,
        *,
        project_id: str,
        shot_id: str,
        tenant_id: object,
    ) -> Any | None:
        """与 SQL 仓储同语义：按 route_id 找已 VERIFIED 且归属一致的生产路线。

        内存仓储按 entity_id 索引，生产路线的 route_id 可能与 entity_id 不同，
        这里线性扫描并按属性过滤；测试规模小，可读性优先。
        """
        for row in self._rows.values():
            if getattr(row, "ENTITY_TYPE", None) != "ProductionRoute":
                continue
            if getattr(row, "route_id", None) != route_ref:
                continue
            if (
                getattr(row, "project_id", None) == project_id
                and getattr(row, "shot_id", None) == shot_id
                and getattr(row, "tenant_id", None) == tenant_id
                and getattr(getattr(row, "status", None), "value", None) == "VERIFIED"
            ):
                return row
        return None

    async def add(self, entity: Any) -> Any:
        if entity.entity_id in self._rows:
            raise DomainError(
                ErrorRegistry.DUPLICATE_ENTITY,
                detail=f"实体已存在: {entity.entity_id}",
            )
        self._rows[entity.entity_id] = entity
        self._persisted_versions[entity.entity_id] = entity.entity_version
        return entity

    async def save(self, entity: Any, expected_version: int) -> Any:
        persisted = self._persisted_versions.get(entity.entity_id, 0)
        if persisted != expected_version:
            raise ConcurrentModificationError(entity.entity_id, expected_version)
        self._rows[entity.entity_id] = entity
        self._persisted_versions[entity.entity_id] = entity.entity_version
        return entity

    def snapshot(self) -> dict[str, Any]:
        return dict(self._rows)


class InMemoryOutboxWriter:
    """进程内 Outbox（只追加、不投递；断言用 events 列表）。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        tenant_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> UUID:
        event_id = uuid4()
        self.events.append(
            {
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "tenant_id": str(tenant_id),
                "event_type": event_type,
                "payload": dict(payload),
            }
        )
        return event_id

    def record_transition(self, *, fact: Any, event: Any) -> UUID:
        event_id = uuid4()
        self.events.append(
            {
                "aggregate_type": event.aggregate_type,
                "aggregate_id": event.aggregate_id,
                "tenant_id": str(event.tenant_id),
                "event_type": event.event_type,
                "payload": dict(event.payload),
            }
        )
        return event_id


__all__ = ["InMemoryOutboxWriter", "InMemoryRepository"]
