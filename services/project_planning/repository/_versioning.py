"""乐观锁基础设施（统一抽象，避免各仓储复制粘贴条件更新逻辑）。

设计要点（不屎山代码 + 高标准）：
- C.3.1 通用实体信封的 ``entity_version`` 是乐观锁的唯一事实源。
- 所有聚合根（Project / ScriptRevision / Episode）的状态迁移都会自增
  ``entity_version``，但仅自增不构成并发安全——若更新时不校验读取时的
  版本，两个并发请求会基于同一旧版本各自 +1，后写覆盖先写，造成
  「丢失更新」（lost update）。部分唯一索引只能兜 INSERT 冲突，兜不住
  UPDATE 的丢失更新。
- 本模块提供一个通用的条件 UPDATE：``UPDATE ... SET ... WHERE
  entity_id = :id AND entity_version = :expected``。命中 0 行即说明行不存在
  或已被并发修改，抛出结构化 ``CONCURRENT_MODIFICATION``。
- 校验在数据库层原子完成（单条 UPDATE 的 WHERE 条件在行级锁语义下天然
  串行化），彻底根治 check-then-act 竞态，而非在应用层补判断。
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
    - CONCURRENT_MODIFICATION：两个不同请求并发修改同一实体，须基于
      最新版本重试（retryable=True）。
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
      与 entity_version 列，即 C.3.1 通用信封的乐观锁字段）。
    - entity_id：目标实体主键。
    - expected_version：调用方读取时的 entity_version（乐观锁期望值）。
    - values：要写入的列值字典（不含主键与 entity_version；后者由本函数
      统一推进为 expected + 1，避免调用方手改版本号导致口径漂移）。

    语义：
    - ``WHERE entity_id = :id AND entity_version = :expected`` 原子匹配，
      命中 0 行即抛 ConcurrentModificationError。
    - 版本自增在本函数内完成（expected + 1），保证所有写路径的版本推进
      口径一致，杜绝「某处 +1、某处忘记 +1」的隐性 bug。

    类型说明：SQLAlchemy 声明式模型上 entity_id / entity_version 是运行时
    才挂载的列属性，静态类型无法在 Base 上声明；此处用 getattr 访问，
    配合 ``# type: ignore[attr-defined]`` 收窄 mypy 误报（列存在性由模型
    定义与迁移契约保证，且集成测试覆盖真实落库）。
    """
    model = update(model_cls)
    result = await session.execute(
        model.where(
            getattr(model_cls, "entity_id") == entity_id,
            getattr(model_cls, "entity_version") == expected_version,
        ).values(**values, entity_version=expected_version + 1)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise ConcurrentModificationError(entity_id, expected_version)


def _orm_update_values(instance: Base) -> dict[str, object]:
    """从 ORM 实例提取待写列值（排除主键与 entity_version），供 conditional_update。

    - 主键列排除：entity_id 是 UPDATE 的定位条件，不参与 SET。
    - entity_version 排除：版本推进统一由 conditional_update 负责（expected+1），
      避免调用方写入值与条件更新版本推进冲突（重复关键字参数 / 口径漂移）。
    - 借助 ORM Mapper 的列信息自动提取，模型加列时本函数自动跟进，杜绝
      手写字段清单与模型定义的漂移。
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
