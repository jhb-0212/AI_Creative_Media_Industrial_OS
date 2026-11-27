"""维护身份四角色 + 会话身份守卫 + SECDEF 函数收紧（WP-03 的身份层）。

为什么是四个角色而不是「一个维护角色加权限」
------------------------------------------
三件事必须能被分别审计，角色是它们唯一无法伪造的载体：

* **谁是权限组**（``operator``）——「某人能不能维护」由成员关系表达，一条
  ``GRANT <operator> TO <login>`` 就是一份可回查的授权事实；
* **谁能登录**（``login``）——可连接性只能落在角色属性上；把它和权限组合并，
  「这个账号存在」就自动等于「这个账号能改数」；
* **谁拥有对象**（``owner``）——属主决定 DDL 与策略归属；让维护执行者同时是
  属主，等于让「临时修一次」顺手获得永久改结构的能力。

``qc_worker`` 是第四个、也是**刻意不参与**的一方：常规质检 Worker 若能执行维护
命令，自动流水线就成了「任意修数入口」。所以它不是 ``operator`` 的成员，且
对维护 schema 与守卫函数都没有任何授权。

为什么要有 ``assert_session_identity``
------------------------------------
``current_user`` 可以被 ``SET ROLE`` 改，``session_user`` 不能。维护动作必须以
「登录的是谁」判定，而不是「当前会话以谁的身份在跑」——否则任何持有成员关系的
连接都能 ``SET ROLE`` 到权限组，再用 GUC 伪造上下文。守卫是 SECURITY INVOKER
的（它自己不需要额外权限，只做判定），把「调用者的真实身份」钉在 ``session_user``
上；``SET search_path`` 一并固定，避免 plpgsql 在调用方 search_path 下解析
``pg_has_role``。

为什么授权语句不写在这个文件里
----------------------------
授权面只有一个执行器：``packages/shared/db_grants.py``。迁移是它的执行者，
不是第二份事实源。把 ``GRANT`` 写在迁移里，闸门就再也无法逐条比对「矩阵声明
什么」与「迁移实际做了什么」——而这正是 ``data.role_grants_least_privilege``
存在的理由。所以下面所有授权语句都取自
``maintenance_identity_statements()`` / ``maintenance_membership_statements()``。

为什么现在只收紧 ``assert_signature_event_exists`` 的 PUBLIC EXECUTE
------------------------------------------------------------------
函数默认 ACL 是 ``PUBLIC EXECUTE``（PG 的默认，不是我们的声明）。一个
SECURITY DEFINER 函数对 PUBLIC 开放，等于「任何能连库的角色都能以 definer 身份
执行它」。这里先 ``REVOKE``；``GRANT`` 给目标 ``svc_*`` 角色要等它们被创建
（注册表 ``status: target``，见 ``contracts/authorization/authorization_registry.yaml``
的 exceptions 段），提前 GRANT 到不存在的角色会让 clean install 直接失败。

Revision ID: e6a7b8c9d0e1
Revises: a8b9c0d1e2f3
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.config import get_settings
from packages.shared.db_grants import (
    MAINTENANCE_GUARD,
    MAINTENANCE_SCHEMA,
    maintenance_identity_restore_statements,
    maintenance_identity_statements,
    maintenance_membership_statements,
)
from packages.shared.db_roles import maintenance_role

revision: str = "e6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD_SCHEMA, _GUARD_NAME = MAINTENANCE_GUARD.split(".")
_GUARD_NAME = _GUARD_NAME.removesuffix("()")

# 角色属性：四者共同「不可提权」，差别只在能否登录。
_NO_ESCALATION = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"


def _role(key: str) -> str:
    return maintenance_role(key)


def _create_role(role: str, *, login: bool, password: str | None = None) -> None:
    """幂等建/改角色。

    已存在时也要 ``ALTER``：只建不改会让「角色被人手工改过属性」永远停在错误
    状态，而迁移报告是绿的——属性漂移只能靠每次收敛回来。重复执行时
    ``ALTER`` 与 ``CREATE`` 等价（两边写的是同一组属性）。
    """
    attributes = "LOGIN" if login else "NOLOGIN"
    # 口令来自配置，可能含单引号。PG 的 DDL 没有绑定参数，所以按 SQL 字面量
    # 的规则转义（标准 conforming strings 下把 ' 写成 '' 就是正确字面量）。
    # 不转义会让「口令里有引号」变成语法错误，或在最坏情况下拼出一条额外语句。
    password_sql = (
        " PASSWORD '" + password.replace("'", "''") + "'"
        if password is not None
        else ""
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} {attributes} {_NO_ESCALATION}{password_sql};
            ELSE
                ALTER ROLE {role} WITH {attributes} {_NO_ESCALATION}{password_sql};
            END IF;
        END $$;
        """
    )


def _create_guard() -> None:
    """会话身份守卫（SECURITY INVOKER，只判定、不扩权）。"""
    operator = _role("operator")
    op.execute(
        f"""
        CREATE FUNCTION {_GUARD_SCHEMA}.{_GUARD_NAME}() RETURNS void
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            -- 登录身份必须仍是会话身份：SET ROLE 之后 current_user 变了，
            -- 而「谁登录的」没有变——维护判据只认后者。
            IF current_user <> session_user THEN
                RAISE EXCEPTION
                    '维护命令禁止切换角色执行：current_user=% session_user=%',
                    current_user, session_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NOT pg_has_role(session_user, '{operator}', 'MEMBER') THEN
                RAISE EXCEPTION
                    '当前身份 % 不是维护权限组成员，无权执行维护命令', session_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
        END;
        $$
        """
    )


def _transfer_ownership() -> None:
    """维护对象归属 ``owner``：属主 = 结构变更权，不给可登录身份。

    ``FORCE ROW LEVEL SECURITY`` 已在 a8b9c0d1e2f3 打开，所以属主自己也要过策略
    ——转让属主不会顺带获得「看全表」的旁路。
    """
    owner = _role("owner")
    op.execute(f"ALTER SCHEMA {MAINTENANCE_SCHEMA} OWNER TO {owner}")
    op.execute(
        f"ALTER TABLE {MAINTENANCE_SCHEMA}.task_projection OWNER TO {owner}"
    )
    op.execute(
        f"ALTER FUNCTION {MAINTENANCE_SCHEMA}.{_GUARD_NAME}() OWNER TO {owner}"
    )


def upgrade() -> None:
    settings = get_settings()
    _create_role(_role("owner"), login=False)
    _create_role(_role("operator"), login=False)
    _create_role(
        _role("login"),
        login=True,
        password=settings.postgres_qc_maintenance_login_password.get_secret_value(),
    )
    _create_role(
        _role("worker"),
        login=True,
        password=settings.postgres_qc_worker_password.get_secret_value(),
    )
    for statement in maintenance_membership_statements():
        op.execute(statement)
    _create_guard()
    _transfer_ownership()
    for statement in maintenance_identity_statements():
        op.execute(statement)


def downgrade() -> None:
    for statement in maintenance_identity_restore_statements():
        op.execute(statement)
    # WP-04/#59：父 revision 的 qc_maintenance schema 只有属主权限，
    # 没有任何维护角色 USAGE。升级给 operator / login 授的 schema USAGE
    # 不是"角色成员关系"能覆盖的，必须单独收回，否则 D 比 P 多两条授权。
    for key in ("operator", "login"):
        op.execute(f"REVOKE USAGE ON SCHEMA {MAINTENANCE_SCHEMA} FROM {_role(key)}")
    op.execute(f"ALTER TABLE {MAINTENANCE_SCHEMA}.task_projection OWNER TO CURRENT_USER")
    op.execute(f"ALTER SCHEMA {MAINTENANCE_SCHEMA} OWNER TO CURRENT_USER")
    op.execute(f"DROP FUNCTION IF EXISTS {MAINTENANCE_SCHEMA}.{_GUARD_NAME}()")
    # 角色不删：downgrade 是「回到上一版结构」，不是「删掉部署方可能已在用的
    # 登录身份」。删角色会连带删除其成员关系与口令，属于数据丢失。
    # 需要彻底撤销时按正式治理流程单独执行（并先确认无人持有该身份）。
