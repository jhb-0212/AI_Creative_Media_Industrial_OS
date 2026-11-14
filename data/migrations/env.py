"""Alembic 迁移环境配置。

关键点：
- DSN 从应用 settings 读取（密钥不落 ini 文件，红线 6）。
- target_metadata 指向 packages.shared.database.Base.metadata，
  并导入各领域 ORM 模型，使 autogenerate 能识别独立 schema 的表。
- 迁移按模块独立 schema 组织（project_planning 等）。
"""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 将项目根加入 sys.path，便于导入 packages / services。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packages.shared.config import get_settings
from packages.shared.database import Base, build_migration_url
from packages.shared.outbox import OutboxEventModel  # noqa: F401
from services.project_planning.repository.orm import (  # noqa: F401
    EpisodeModel,
    ProjectModel,
    ScriptModel,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 用迁移配置构造 DSN（超级用户，需 DDL 权限；不含明文密钥，密码经 URL 编码）。
# ConfigParser 会做 % 插值：URL 里的百分号编码必须转义成 %%，否则口令含
# @ : / 空格 等字符时 alembic 在导入 env.py 阶段就报 invalid interpolation。
settings = get_settings()
config.set_main_option(
    "sqlalchemy.url", build_migration_url().replace("%", "%%")
)

target_metadata = Base.metadata


# 分区子表在 pg_class 里是独立表，但 ORM 元数据里没有它们（分区属物理布局，
# 不属领域模型）。不过滤的话 `alembic check` 会把每个月份分区报成「库里多出来的
# 表」并提议 drop_table，形成永久性假漂移。命名口径见
# docs/adr/ADR-R06-2-outbox-retention-partitioning.md。
_PARTITION_TABLE_PREFIXES = ("outbox_events_y", "outbox_events_default")


def include_object(obj, name, type_, reflected, compare_to):
    """过滤 autogenerate 的比对对象。

    排除：
    1. Alembic 版本追踪表（alembic_version），由 Alembic 自身管理；
    2. outbox 分区子表与 DEFAULT 分区（物理布局，不属于领域模型）。
    """
    if type_ == "table" and name == "alembic_version":
        return False
    return not (type_ == "table" and name.startswith(_PARTITION_TABLE_PREFIXES))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_schemas=True,
        include_object=include_object,
        version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
