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

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 将项目根加入 sys.path，便于导入 packages / services。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packages.shared.config import get_settings  # noqa: E402
from packages.shared.database import Base  # noqa: E402
from services.project_planning.repository.orm import ProjectModel  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 用应用配置构造 DSN（不含明文密钥）。
settings = get_settings()
_dsn = (
    f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password.get_secret_value()}"
    f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
)
config.set_main_option("sqlalchemy.url", _dsn)

target_metadata = Base.metadata


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
