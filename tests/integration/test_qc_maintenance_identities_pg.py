"""维护身份四角色的授权形状验收（真实 PostgreSQL）。

WP-03 的结论不是「写了一个守卫函数」，而是「维护权限只能被一种身份取得」。
因此这里核对的都是**数据库里的既成事实**，不是代码里的意图：

1. 四角色都不可提权（NOSUPERUSER / NOBYPASSRLS / NOCREATEDB / NOCREATEROLE），
   且只有 ``login`` / ``worker`` 可登录——「账号存在」不等于「能维护」；
2. ``login`` ∈ ``operator``，``worker`` ∉ ``operator``：常规质检 Worker 不能因为
   「同在质检域」就继承维护权；
3. 守卫函数只授予 ``operator`` / ``login``，``worker`` 执行即被库拒绝；
4. 守卫认 ``session_user`` 而非 ``current_user``：``login`` 登录后调用通过，
   一旦 ``SET ROLE operator`` 立刻被拒（否则任何有成员关系的连接都能用
   ``SET ROLE`` 伪造「我在维护身份下」）；
5. 维护对象属主是 ``qc_maintenance_owner``（不可登录），策略仍是 FORCE RLS；
6. SECURITY DEFINER 函数 ``audit.assert_signature_event_exists()`` 对 PUBLIC 的
   EXECUTE 已收回（PG 默认 ACL 是 PUBLIC EXECUTE，不显式撤就等于公开）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from packages.shared.config import get_settings
from packages.shared.database import build_migration_url
from packages.shared.db_roles import maintenance_role

_GUARD = "qc_maintenance.assert_session_identity()"
_SIGNATURE_FN = "audit.assert_signature_event_exists()"
_IDENT = "insufficient_privilege"

_ROLE_KEYS = ("owner", "operator", "login", "worker")


def _role(key: str) -> str:
    return maintenance_role(key)


async def _scalar(conn, sql: str, **params):
    return await conn.scalar(text(sql), params)


async def _has_public_execute(conn, function: str) -> bool:
    """``proacl`` 里是否存在 grantee = PUBLIC(0) 的 EXECUTE。

    ``aclexplode`` 把 ACL 展开成 (grantor, grantee, privilege_type) 行；PUBLIC
    的 grantee oid 恒为 0。``proacl IS NULL`` 在 PG 里表示「默认 ACL」= PUBLIC
    EXECUTE，所以 NULL 必须按「有」处理，而不是「查不到」。
    """
    row = await _scalar(
        conn,
        """
        SELECT CASE
                 WHEN p.proacl IS NULL THEN true
                 ELSE EXISTS (
                     SELECT 1
                       FROM aclexplode(p.proacl) a
                      WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'
                 )
               END
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = :schema AND p.proname = :name
        """,
        schema=function.split(".")[0],
        name=function.split(".")[1].rstrip("()"),
    )
    return bool(row)


async def test_roles_cannot_escalate_and_only_login_identities_can_connect(
    admin_sessionmaker,
) -> None:
    async with admin_sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT rolname, rolsuper, rolbypassrls, rolcreatedb,
                           rolcreaterole, rolcanlogin
                      FROM pg_roles
                     WHERE rolname = ANY(:names)
                    """
                ),
                {"names": [_role(k) for k in _ROLE_KEYS]},
            )
        ).all()

    by_name = {r.rolname: r for r in rows}
    assert set(by_name) == {_role(k) for k in _ROLE_KEYS}, "四角色必须都存在"

    for name, row in by_name.items():
        assert not row.rolsuper, f"{name} 不许是超级用户"
        assert not row.rolbypassrls, f"{name} 不许绕过行级安全"
        assert not row.rolcreatedb, f"{name} 不许建库"
        assert not row.rolcreaterole, f"{name} 不许建角色"

    assert not by_name[_role("owner")].rolcanlogin, "属主身份不可登录"
    assert not by_name[_role("operator")].rolcanlogin, "权限组不可登录"
    assert by_name[_role("login")].rolcanlogin, "维护登录身份必须可登录"
    assert by_name[_role("worker")].rolcanlogin, "质检 Worker 必须可登录"


async def test_login_is_operator_member_but_worker_is_not(admin_sessionmaker) -> None:
    async with admin_sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT g.rolname AS group_name, r.rolname AS member_name,
                           m.admin_option
                      FROM pg_auth_members m
                      JOIN pg_roles g ON g.oid = m.roleid
                      JOIN pg_roles r ON r.oid = m.member
                     WHERE g.rolname = ANY(:groups) AND r.rolname = ANY(:members)
                    """
                ),
                {
                    "groups": [_role("operator")],
                    "members": [_role("login"), _role("worker")],
                },
            )
        ).all()

    memberships = {r.member_name: r for r in rows}
    assert _role("login") in memberships, "维护登录身份必须是 operator 成员"
    assert not memberships[_role("login")].admin_option, "成员不得携带 ADMIN OPTION"
    assert _role("worker") not in memberships, (
        "质检 Worker 不得是维护权限组成员——否则自动流水线就是任意修数入口"
    )


async def test_worker_has_no_execute_on_guard(admin_sessionmaker) -> None:
    operator = _role("operator")
    login = _role("login")
    worker = _role("worker")
    async with admin_sessionmaker() as session:
        grants = {
            role: await _scalar(
                session,
                "SELECT has_function_privilege(:role, :fn, 'EXECUTE')",
                role=role,
                fn=_GUARD,
            )
            for role in (operator, login, worker)
        }
        public_execute = await _has_public_execute(session, _GUARD)
        worker_schema_usage = await _scalar(
            session,
            "SELECT has_schema_privilege(:role, 'qc_maintenance', 'USAGE')",
            role=worker,
        )

    assert grants[operator] is True, "权限组必须能执行维护守卫"
    assert grants[login] is True, "登录身份必须能执行维护守卫"
    assert grants[worker] is False, "质检 Worker 必须无法执行维护守卫"
    assert public_execute is False, "维护守卫不得对 PUBLIC 开放"
    assert worker_schema_usage is False, "质检 Worker 不得持有维护 schema 的 USAGE"


async def test_guard_binds_to_session_user_not_current_user() -> None:
    """``SET ROLE`` 必须失效，否则「有成员关系」就等于「永远是维护身份」。

    这两条用例改的是**会话级**身份（``SET SESSION AUTHORIZATION``），而
    ``ROLLBACK`` 清不掉它。所以用 NullPool 的独立连接：用完直接物理关闭，
    绝不把「顶着 login 身份」的连接还给共享池——池污染造成的假阳性会在
    毫不相干的用例里冒出来，且极难定位。
    """
    login = _role("login")
    operator = _role("operator")
    engine = create_async_engine(build_migration_url(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'SET SESSION AUTHORIZATION "{login}"'))
            # 正例：以登录身份直连（session_user = current_user），守卫通过。
            await conn.execute(text(f"SELECT {_GUARD}"))

        async with engine.connect() as conn:
            await conn.execute(text(f'SET SESSION AUTHORIZATION "{login}"'))
            await conn.execute(text(f'SET ROLE "{operator}"'))
            with pytest.raises(DBAPIError) as excinfo:
                await conn.execute(text(f"SELECT {_GUARD}"))
            assert "禁止切换角色执行" in str(excinfo.value), "应被守卫的 SET ROLE 判据拒绝"
            assert _role("login") in str(excinfo.value), (
                "错误信息应点明真实登录身份（session_user），而不是 SET ROLE 后的身份"
            )
    finally:
        await engine.dispose()


async def test_worker_is_rejected_by_database_not_by_application() -> None:
    worker = _role("worker")
    engine = create_async_engine(build_migration_url(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'SET SESSION AUTHORIZATION "{worker}"'))
            with pytest.raises(DBAPIError) as excinfo:
                await conn.execute(text(f"SELECT {_GUARD}"))
            assert "permission denied" in str(excinfo.value), (
                "Worker 执行维护守卫必须被数据库拒绝（权限层，而不是应用层判断）"
            )
    finally:
        await engine.dispose()


async def test_maintenance_objects_are_owned_by_non_login_owner(
    admin_sessionmaker,
) -> None:
    owner = _role("owner")
    async with admin_sessionmaker() as session:
        schema_owner = await _scalar(
            session,
            """
            SELECT r.rolname
              FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
             WHERE n.nspname = 'qc_maintenance'
            """,
        )
        table = (
            await session.execute(
                text(
                    """
                    SELECT r.rolname AS owner_name, c.relrowsecurity, c.relforcerowsecurity
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                      JOIN pg_roles r ON r.oid = c.relowner
                     WHERE n.nspname = 'qc_maintenance' AND c.relname = 'task_projection'
                    """
                )
            )
        ).first()

    assert schema_owner == owner, "维护 schema 属主必须是不可登录的 owner"
    assert table is not None
    assert table.owner_name == owner, "投影表属主必须是不可登录的 owner"
    assert table.relrowsecurity is True, "投影表必须开启行级安全"
    assert table.relforcerowsecurity is True, (
        "投影表必须 FORCE RLS——否则属主身份自己就能看全表"
    )


async def test_secdef_signature_function_no_longer_public(admin_sessionmaker) -> None:
    async with admin_sessionmaker() as session:
        assert await _has_public_execute(session, _SIGNATURE_FN) is False, (
            "SECURITY DEFINER 函数不得保留 PUBLIC EXECUTE"
        )


def test_settings_expose_all_four_identities() -> None:
    settings = get_settings()
    assert settings.postgres_qc_maintenance_owner_user
    assert settings.postgres_qc_maintenance_operator_user
    assert settings.postgres_qc_maintenance_login_user
    assert settings.postgres_qc_worker_user
