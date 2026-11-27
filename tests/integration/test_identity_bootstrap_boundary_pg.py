"""bootstrap 边界的实测证据：有 ADMIN 也改不了 ``SUPERUSER`` 属性。

为什么这条要成为回归用例
------------------------
AUTHZ-7 的解除路径很容易被简化成一句「给迁移执行者 CREATEROLE + ADMIN 就行」。
实测否掉了这个方案：历史迁移普遍写

    ALTER ROLE app_role WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE ...

即便执行者持有目标角色的 ADMIN option（非超管能拿到的最高档），``NOSUPERUSER``
这一项仍被判成「只有超管才能改变 SUPERSER 属性」。这不是权限给得不够，而是
**语句本身只有超管能跑**。

于是「把角色 DDL 从迁移里搬走」不是优化项，而是唯一路径：bootstrap 在超管窗口
建身份，历史迁移只留结构 DDL。

本用例同时锁住反面：同一个持有 ADMIN 的非超管改 ``LOGIN`` 与 ``PASSWORD`` 是
**可以**的——否则证据会被误读成「非超管什么都不能改」，把真正的原因（SUPERUSER
属性专属）掩盖掉。
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from packages.shared.config import get_settings

pytestmark = pytest.mark.integration


async def _admin_conn() -> asyncpg.Connection:
    settings = get_settings()
    return await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        database="postgres",
    )


async def test_admin_option_does_not_unlock_superuser_attribute() -> None:
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    executor = f"probe_exec_{suffix}"
    target = f"probe_target_{suffix}"
    password = uuid.uuid4().hex

    admin = await _admin_conn()
    try:
        await admin.execute(
            f'CREATE ROLE "{executor}" LOGIN CREATEROLE NOSUPERUSER NOCREATEDB '
            f"NOBYPASSRLS PASSWORD '{password}'"
        )
        await admin.execute(f'CREATE ROLE "{target}" LOGIN NOSUPERUSER NOCREATEDB')
        # 非超管能拿到的最高档权限：目标角色的 ADMIN option。
        await admin.execute(f'GRANT "{target}" TO "{executor}" WITH ADMIN OPTION')
    finally:
        await admin.close()

    try:
        conn = await asyncpg.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=executor,
            password=password,
            database="postgres",
        )
        try:
            # 反面：LOGIN / PASSWORD 改得动，说明「改角色」本身没有被拒。
            await conn.execute(
                f'ALTER ROLE "{target}" WITH LOGIN PASSWORD \'{password}\''
            )

            # 正面一：语句里只要出现 NOSUPERUSER，哪怕目标本来就不是超管、
            # 哪怕执行者持有 ADMIN，也仍按「改 SUPERSER 属性」拒掉——迁移里
            # 那种「幂等再断言一遍属性」的写法和首次创建同样跑不了。
            with pytest.raises(asyncpg.InsufficientPrivilegeError) as nosuper:
                await conn.execute(f'ALTER ROLE "{target}" WITH NOSUPERUSER')
            assert "SUPERUSER attribute" in str(nosuper.value)

            # 正面二：直接提权同样越不过去。
            with pytest.raises(asyncpg.InsufficientPrivilegeError) as superuser:
                await conn.execute(f'ALTER ROLE "{target}" WITH SUPERUSER')
            assert "SUPERUSER attribute" in str(superuser.value)
        finally:
            await conn.close()
    finally:
        cleanup = await _admin_conn()
        try:
            await cleanup.execute(f'DROP ROLE IF EXISTS "{target}"')
            await cleanup.execute(f'DROP ROLE IF EXISTS "{executor}"')
        finally:
            await cleanup.close()
