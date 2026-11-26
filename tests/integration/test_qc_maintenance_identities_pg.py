"""维护身份的授权形状验收（真实 PostgreSQL）。

WP-03 的结论不是「写了一个守卫函数」，而是「维护权限只能被一种身份取得」。
因此这里核对的都是**数据库里的既成事实**，不是代码里的意图：

1. 维护三身份（owner / operator / login）都不可提权
   （NOSUPERUSER / NOBYPASSRLS / NOCREATEDB / NOCREATEROLE），且只有 ``login``
   可登录——「账号存在」不等于「能维护」；
2. ``login`` ∈ ``operator``，而 **Worker 的运行身份**（``qc_governance_role``）
   ∉ ``operator``：常规质检 Worker 不能因为「同在质检域」就继承维护权。
   这一条原本打在 ``qc_worker`` 这个专用壳角色上；它已退役（迁移
   ``f2b3c4d5e6a7``），判据改为打在 Worker 真连库用的域角色上——验一个没人
   连接的角色，边界等于没验；
3. 守卫函数只授予 ``operator`` / ``login``，Worker 的运行身份执行即被库拒绝；
4. 守卫认 ``session_user`` 而非 ``current_user``：``login`` 登录后调用通过，
   一旦 ``SET ROLE operator`` 立刻被拒（否则任何有成员关系的连接都能用
   ``SET ROLE`` 伪造「我在维护身份下」）；
5. 维护对象属主是 ``qc_maintenance_owner``（不可登录），策略仍是 FORCE RLS；
6. SECURITY DEFINER 函数 ``audit.assert_signature_event_exists()`` 对 PUBLIC 的
   EXECUTE 已收回（PG 默认 ACL 是 PUBLIC EXECUTE，不显式撤就等于公开）；
7. ``qc_worker`` 本身不得复活：它不是任何进程的连接身份，重建需走治理流程。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from packages.shared.config import get_settings
from packages.shared.database import build_migration_url
from packages.shared.db_roles import domain_role, maintenance_role

_GUARD = "qc_maintenance.assert_session_identity()"
_SIGNATURE_FN = "audit.assert_signature_event_exists()"
_IDENT = "insufficient_privilege"

_ROLE_KEYS = ("owner", "operator", "login")

# Worker 实际连库用的身份（``workers/qc/__main__.py`` 装配的 qc_governance 域池）。
# 退役 qc_worker 之后，「Worker 不能维护」的判据必须打在**真身份**上：验一个
# 不存在、也没人连接的角色，结论再漂亮也证明不了任何边界。
_RUNTIME = domain_role("qc_governance")


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
        retired_exists = await session.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
            {"r": maintenance_role("worker")},
        )

    by_name = {r.rolname: r for r in rows}
    assert set(by_name) == {_role(k) for k in _ROLE_KEYS}, "维护三身份必须都存在"

    for name, row in by_name.items():
        assert not row.rolsuper, f"{name} 不许是超级用户"
        assert not row.rolbypassrls, f"{name} 不许绕过行级安全"
        assert not row.rolcreatedb, f"{name} 不许建库"
        assert not row.rolcreaterole, f"{name} 不许建角色"

    assert not by_name[_role("owner")].rolcanlogin, "属主身份不可登录"
    assert not by_name[_role("operator")].rolcanlogin, "权限组不可登录"
    assert by_name[_role("login")].rolcanlogin, "维护登录身份必须可登录"
    assert retired_exists is None, (
        "qc_worker 已退役（迁移 f2b3c4d5e6a7）：它不是任何进程的连接身份，"
        "Worker 用的是 qc_governance_role"
    )


async def test_login_is_operator_member_but_worker_runtime_is_not(
    admin_sessionmaker,
) -> None:
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
                    "members": [_role("login"), _RUNTIME],
                },
            )
        ).all()

    memberships = {r.member_name: r for r in rows}
    assert _role("login") in memberships, "维护登录身份必须是 operator 成员"
    assert not memberships[_role("login")].admin_option, "成员不得携带 ADMIN OPTION"
    assert _RUNTIME not in memberships, (
        "Worker 的运行身份不得是维护权限组成员——否则自动流水线就是任意修数入口"
    )


async def test_worker_runtime_has_no_execute_on_guard(admin_sessionmaker) -> None:
    operator = _role("operator")
    login = _role("login")
    async with admin_sessionmaker() as session:
        grants = {
            role: await _scalar(
                session,
                "SELECT has_function_privilege(:role, :fn, 'EXECUTE')",
                role=role,
                fn=_GUARD,
            )
            for role in (operator, login, _RUNTIME)
        }
        public_execute = await _has_public_execute(session, _GUARD)
        # 域角色对维护 schema 有 USAGE（历史上按域收敛授过），USAGE 只代表
        # 「能进 schema」，不代表「能调用函数」。真正的边界在 EXECUTE 上：
        # 守卫已从 PUBLIC 收回、只授 operator/login，所以这里逐个命令核 EXECUTE。
        command_grants = {
            fn: await _scalar(
                session,
                "SELECT has_function_privilege(:role, :fn, 'EXECUTE')",
                role=_RUNTIME,
                fn=fn,
            )
            for fn in (
                "qc_maintenance.enumerate_due_tasks()",
                "qc_maintenance.detect_projection_anomalies()",
                "qc_maintenance.reconcile_tenant_tasks(text)",
                "qc_maintenance.repair_tenant_task(text, text, text)",
            )
        }

    assert grants[operator] is True, "权限组必须能执行维护守卫"
    assert grants[login] is True, "登录身份必须能执行维护守卫"
    assert grants[_RUNTIME] is False, "Worker 的运行身份必须无法执行维护守卫"
    assert public_execute is False, "维护守卫不得对 PUBLIC 开放"
    forbidden = sorted(fn for fn, allowed in command_grants.items() if allowed)
    assert not forbidden, f"Worker 的运行身份不得执行维护命令：{forbidden}"


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


async def test_worker_runtime_is_rejected_by_database_not_by_application() -> None:
    """Worker 的运行身份调守卫必须被**库**拒绝，而不是被应用层判断拦下。

    用 ``SET SESSION AUTHORIZATION`` 而不是 ``SET ROLE``：要验的是「以 Worker
    身份连库时能做什么」，不是「超级用户切角色后的权限」——后者的失败恒为
    「超级用户被绕」而非真实结论。
    """
    runtime = _RUNTIME
    engine = create_async_engine(build_migration_url(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'SET SESSION AUTHORIZATION "{runtime}"'))
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


def test_settings_expose_maintenance_identities_and_retired_worker_key() -> None:
    """三个在线身份必须有配置；``postgres_qc_worker_user`` 保留给历史回放。

    退役只删了集群里的角色，没有删配置键：迁移 ``e6a7b8c9d0e1`` 在 clean install
    上创建它、``f2b3c4d5e6a7`` 再删掉，回放仍要读这个键（含口令）。删键会让
    全新库的 ``alembic upgrade head`` 直接 KeyError——历史是不可改的事实。
    """
    settings = get_settings()
    assert settings.postgres_qc_maintenance_owner_user
    assert settings.postgres_qc_maintenance_operator_user
    assert settings.postgres_qc_maintenance_login_user
    assert settings.postgres_qc_worker_user
