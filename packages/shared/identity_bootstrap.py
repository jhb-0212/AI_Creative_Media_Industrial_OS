"""身份引导（bootstrap）：按声明建角色、按矩阵授权，供迁移链卸下角色 DDL。

它和「迁移」的分工
------------------
``server_role`` 属性只有超管能写（见 ``test_identity_bootstrap_boundary_pg``），
而部署侧又要求迁移执行者迟早不是超管。两条约束合起来只剩一个解：把身份创建
搬到超管窗口（bootstrap），历史迁移只留结构 DDL。

于是本模块提供两个**阶段**，而不是一个脚本：

* **roles**（迁移前）：建角色、收敛属性、设口令。此时库里还没有任何业务对象，
  所以这一阶段不碰授权——不存在的表不能授权，硬写只会让 clean install 失败。
* **grants**（迁移后）：对象存在了，再把权限面收敛到矩阵：域角色的表权限、
  运维角色的表权限、维护身份的守卫/命令、调度身份的发现函数。

阶段划分本身就是判据：bootstrap 若在 roles 阶段顺手 GRANT，它就成了「只能在
head 之后跑」的脚本；若在 grants 阶段 ALTER ROLE，它又变成了第二个超管需求点。
两阶段各自只做一件事，才能分别在「升级前」与「升级后」被单独调用。

为什么句句幂等
--------------
bootstrap 会被重复执行（每次部署、每次重建库、CI 的往返测试）。幂等不是靠
「先查再改」的聪明判断，而是靠语句本身：

* 建角色：``DO $$ IF NOT EXISTS ... $$``，存在则跳过；
* 收敛属性：``ALTER ROLE ... WITH ...``，重复断言同一组属性是无操作；
* 授权：``GRANT`` / ``REVOKE`` 重复执行在 PG 里是幂等的，且不需要条件分支，
  闸门因此仍能按「逐条语句」比对（这是 ``db_grants`` 里刻意不用 ``DO`` 的原因）。

口令
----
口令值不进代码、不进声明：``RoleSpec.password_setting`` 只记录「这个角色需要
口令」，执行时由调用方从 ``Settings`` 取值。缺口令的可登录角色**直接报错**，
不建无口令的登录角色——那等于一个空口令账号。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from packages.shared.db_grants import (
    DOMAIN_GRANTS,
    INFRA_ROLE_GRANTS,
    converge_statements,
    infra_converge_statements,
    maintenance_command_statements,
    maintenance_identity_statements,
    maintenance_repair_statements,
    referenced_schemas,
    scheduler_statements,
)
from packages.shared.db_roles import domain_role, infra_roles, maintenance_roles
from packages.shared.role_specs import RoleSpec, role_specs

_MAINTENANCE_LOGIN = "login"
_MAINTENANCE_OPERATOR = "operator"


def _ident(name: str) -> str:
    """角色/对象名一律加双引号：它们都是标识符，不该被当成关键字或大小写折叠。"""
    return f'"{name}"'


def _literal(value: str) -> str:
    """SQL 字面量：单引号按 SQL 规则翻倍，避免口令里的 ``'`` 截断语句。"""
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------- roles 阶段


def role_statements(
    specs: Sequence[RoleSpec],
    passwords: Mapping[str, str],
) -> list[str]:
    """建角色 + 收敛属性的语句（``passwords`` 以配置字段名为键）。

    每个角色两条：一条 ``DO`` 守卫创建（``pg_roles`` 里没有才 ``CREATE``），
    一条 ``ALTER ROLE ... WITH ...`` 无条件收敛。为什么不合成一条：``DO`` 块里的
    ``CREATE`` 分支与``ELSE`` 分支对同一组属性要写两遍，而它们必须永远一致；
    拆成「守卫创建」+「无条件收敛」之后，目标态只写一处。
    """
    statements: list[str] = []
    for spec in specs:
        if spec.login and spec.password_setting is None:
            # 可登录但没登记口令来源：声明不完整，不能靠猜。
            raise ValueError(
                f"{spec.name} 声明为可登录，但没有 password_setting："
                "补声明或改成 NOLOGIN，不要建无口令的登录角色"
            )
        effective_password = _resolve_password(spec, passwords)
        options = _attribute_options(spec, effective_password)
        statements.append(_create_role_guard(spec, options))
        statements.append(
            f"ALTER ROLE {_ident(spec.name)} WITH {options}"
        )
    return statements


def _resolve_password(spec: RoleSpec, passwords: Mapping[str, str]) -> str | None:
    if not spec.login:
        return None
    assert spec.password_setting is not None
    try:
        value = passwords[spec.password_setting]
    except KeyError as exc:
        raise ValueError(
            f"{spec.name} 需要口令，但配置字段 {spec.password_setting} 没有取值"
        ) from exc
    if not value:
        raise ValueError(
            f"{spec.name} 的口令取自 {spec.password_setting}，但该字段为空："
            "空口令的登录角色等于把认证关掉"
        )
    return value


def _attribute_options(spec: RoleSpec, password: str | None) -> str:
    # NOSUPERUSER 等四项是「不可提权」不变量：bootstrap 在超管窗口执行，所以这里
    # 写得出这一串；正因为只有超管写得出，才不能留在迁移里。
    parts = ["LOGIN" if spec.login else "NOLOGIN"]
    parts += ["NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOBYPASSRLS", "INHERIT"]
    if password is not None:
        parts.append(f"PASSWORD {_literal(password)}")
    return " ".join(parts)


def _create_role_guard(spec: RoleSpec, options: str) -> str:
    return (
        "DO $$\n"
        "BEGIN\n"
        f"    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_literal(spec.name)}) THEN\n"
        f"        CREATE ROLE {_ident(spec.name)} {options};\n"
        "    END IF;\n"
        "END\n"
        "$$"
    )


def role_passwords_from_settings(settings: object) -> dict[str, str]:
    """从 ``Settings`` 取出全部被声明引用的口令（键 = 配置字段名）。

    只取声明里出现过的字段：把 Settings 上的口令全搬过来，等于让「声明里没有的
    口令」也进入 SQL 生成路径，将来某个字段改名时无人察觉。
    """
    wanted = {
        spec.password_setting for spec in role_specs() if spec.password_setting
    }
    resolved: dict[str, str] = {}
    for field_name in sorted(wanted):
        value = getattr(settings, field_name, None)
        if value is None:
            raise ValueError(f"配置里缺少 {field_name}：声明要求该角色有口令")
        resolved[field_name] = (
            value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
        )
    return resolved


# --------------------------------------------------------------- grants 阶段


def membership_statements() -> list[str]:
    """身份之间的成员关系（只有矩阵能声明）。

    刻意**不**复用 ``db_grants.maintenance_membership_statements``：那份里带
    ``REVOKE operator FROM qc_worker``，是历史迁移的回放内容；``qc_worker`` 已退役，
    bootstrap 执行它只会得到「role does not exist」。历史由迁移负责，现状由声明
    负责，两者不能共用一份语句。
    """
    operator = maintenance_roles()[_MAINTENANCE_OPERATOR]
    login = maintenance_roles()[_MAINTENANCE_LOGIN]
    return [f"GRANT {operator} TO {login}"]


def grant_statements(*, server_version_num: int) -> list[str]:
    """对象级授权收敛：域角色、运维角色、维护身份、调度身份。

    域/运维角色走 ``converge_statements``（整体 ``REVOKE ALL`` 再按矩阵授予），
    维护与调度身份走各自的声明函数——它们的授权面含 ``REVOKE ... FROM PUBLIC``
    这类「反向收紧」，不是单纯的表权限收敛。
    """
    statements = membership_statements()
    for domain in DOMAIN_GRANTS:
        statements += converge_statements(domain_role(domain), domain)
    for infra in INFRA_ROLE_GRANTS:
        statements += infra_converge_statements(
            _infra_role_name(infra), infra
        )
    statements += maintenance_identity_statements()
    statements += maintenance_command_statements()
    statements += maintenance_repair_statements(server_version_num)
    statements += scheduler_statements()
    return statements


def _infra_role_name(infra: str) -> str:
    return infra_roles()[infra]


def required_objects() -> list[tuple[str, str]]:
    """grants 阶段要求「已经在库里」的对象（schema, table）。

    这张清单不是给 ``GRANT`` 用的，而是 preflight：如果它不为空而库里没有，
    说明 schema 还没到 head，此时执行 grants 会得到一串 ``relation does not
    exist``——比一句「先跑 alembic upgrade head」难读得多。
    """
    objects: set[tuple[str, str]] = set()
    for domain, grants in DOMAIN_GRANTS.items():
        for schema in referenced_schemas(domain):
            objects.add((schema, ""))
        for grant in grants:
            objects.add((grant.schema, grant.table))
    for grants in INFRA_ROLE_GRANTS.values():
        for grant in grants:
            objects.add((grant.schema, grant.table))
    return sorted(objects)


def missing_objects(existing: Mapping[str, frozenset[str]]) -> list[str]:
    """``existing`` 是 ``schema -> 该 schema 下的表名集合``；缺 schema 也要报。"""
    missing: list[str] = []
    for schema, table in required_objects():
        tables = existing.get(schema)
        if tables is None:
            missing.append(schema if not table else f"{schema}.{table}（schema 不存在）")
            continue
        if table and table not in tables:
            missing.append(f"{schema}.{table}")
    return missing


# --------------------------------------------------------------- 计划


@dataclass(frozen=True, slots=True)
class Phase:
    """一个 bootstrap 阶段：名字 + 语句序列（语句只按顺序执行，不做判断）。"""

    name: str
    statements: tuple[str, ...]


def roles_phase(settings: object) -> Phase:
    """roles 阶段：口令来自 ``Settings``，调用方只需给环境配置。"""
    return Phase(
        name="roles",
        statements=tuple(role_statements(role_specs(), role_passwords_from_settings(settings))),
    )


def grants_phase(*, server_version_num: int) -> Phase:
    """grants 阶段：要求对象已在库里（preflight 由调用方先做）。"""
    return Phase(
        name="grants",
        statements=tuple(grant_statements(server_version_num=server_version_num)),
    )
