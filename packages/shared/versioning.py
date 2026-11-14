"""乐观锁基础设施（跨模块共享，避免各仓储复制条件更新逻辑）。

设计要点：
- C.3.1 通用实体信封的 ``entity_version`` 是乐观锁的唯一事实源。
- 状态迁移都会自增 entity_version，但仅自增不构成并发安全：若更新时不校验
  读取时的版本，两个并发请求会基于同一旧版本各自 +1，后写覆盖先写
  （lost update）。唯一约束只能兜 INSERT 冲突，兜不住 UPDATE。
- 本模块提供通用条件 UPDATE：``UPDATE ... SET ... WHERE entity_id = :id
  AND entity_version = :expected``；命中 0 行即抛结构化
  ``CONCURRENT_MODIFICATION``（retryable）。
- 校验在数据库层原子完成（行级锁语义下天然串行化），根治 check-then-act。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.database import Base


class ConcurrentModificationError(DomainError):
    """乐观锁冲突：读取的 entity_version 已被并发请求推进。

    与 IDEMPOTENCY_CONFLICT 区分：
    - IDEMPOTENCY_CONFLICT：同一逻辑请求重复提交，可幂等返回原结果。
    - CONCURRENT_MODIFICATION：两个不同请求并发修改同一实体，须基于最新
      版本重试（retryable=True）。
    """

    def __init__(self, entity_id: str, expected_version: int) -> None:
        super().__init__(
            ErrorRegistry.CONCURRENT_MODIFICATION,
            detail=(
                f"并发修改冲突: 实体 {entity_id} 的 entity_version 已非 "
                f"{expected_version}（可能被并发请求更新或已不存在）"
            ),
        )


async def conditional_update(
    session: AsyncSession,
    model_cls: type[Base],
    entity_id: str,
    expected_version: int,
    values: dict[str, object],
) -> None:
    """以乐观锁条件执行 UPDATE，版本不匹配则抛并发冲突。

    参数：
    - session：当前工作单元会话（事务边界由请求层控制，本函数不 commit）。
    - model_cls：ORM 模型类（继承 shared.database.Base，须含 entity_id 主键
      与 entity_version 列）。
    - entity_id：目标实体主键。
    - expected_version：调用方读取时的 entity_version。
    - values：要写入的列值字典（不含主键与 entity_version；后者由本函数统一
      推进为 expected + 1，避免调用方手改版本号导致口径漂移）。

    类型说明：SQLAlchemy 声明式模型上的信封列是运行时挂载的列属性，静态类型
    无法在 ``type[Base]`` 上表达，故配合 ``# type: ignore[attr-defined]``
    收窄 mypy 误报；列存在性由模型定义与迁移契约保证。
    """
    model = update(model_cls)
    result = await session.execute(
        model.where(
            model_cls.entity_id == entity_id,  # type: ignore[attr-defined]
            model_cls.entity_version == expected_version,  # type: ignore[attr-defined]
        ).values(**values, entity_version=expected_version + 1)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise ConcurrentModificationError(entity_id, expected_version)


def _orm_update_values(instance: Base) -> dict[str, object]:
    """从 ORM 实例提取待写列值（排除主键与 entity_version），供 conditional_update。

    - 主键排除：entity_id 是 UPDATE 的定位条件，不参与 SET。
    - entity_version 排除：版本推进统一由 conditional_update 负责。
    - 借助 Mapper 列信息自动提取，模型加列时自动跟进，杜绝手写字段清单漂移。
    """
    mapper: Mapper[Any] = instance.__mapper__
    values: dict[str, object] = {}
    for column in mapper.columns:
        if column.primary_key:
            continue
        if column.name == "entity_version":
            continue
        values[column.name] = getattr(instance, column.name)
    return values
