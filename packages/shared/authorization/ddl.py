"""从 Registry 派生 DDL：策略、RLS 开关、函数加固（ADR-R40 C.3.2 / D.5 / D.6.0c）。

本模块只做「节点 → 语句」的确定性翻译，不判断「该不该建"——
「该不该」属于 Registry 的语义校验（``registry.validate``），两者分开，
才不会出现「生成器有自己的规则、闸门有另一套规则」的双事实源。

三条实现要点都来自真实踩过的坑：
1. **子句按命令派生，不存在的子句完全省略**：PG16 的 ``CREATE POLICY`` 对
   ``SELECT`` 只接受 ``USING``、对 ``INSERT`` 只接受 ``WITH CHECK``；统一输出
   双子句会直接语法报错（v1.4.1 的模板就是这样报废的）。
2. **函数加固先收搜索路径、再收执行权**：``search_path`` 未定时，调用者的临时
   schema 可能排在内建 schema 之前，足以劫持函数内的未限定对象名。所以顺序是
   SET search_path → OWNER → REVOKE PUBLIC → GRANT，而不是先 GRANT 再补配置。
3. **REVOKE PUBLIC 是落地动作，不是检测**：``pg_proc.proacl`` 为 NULL 表示默认
   对 PUBLIC 开放（E10.3：6 个业务函数中 5 个如此）。只在 Registry 里写
   ``public_execute: false`` 不会改变数据库里的任何东西。
"""

from __future__ import annotations

from packages.shared.authorization.ast import compile_sql
from packages.shared.authorization.registry import (
    OWNER_ROLE,
    Policy,
    Resource,
    SecdefException,
)


class DdlError(ValueError):
    """无法从该声明派生合法 DDL（数据与命令不匹配，属于声明缺陷）。"""


def rls_statements(resource: Resource) -> list[str]:
    """启用 + 强制 RLS（FORCE 必须单独发，ENABLE 不会顺带打开它）。"""
    if resource.rls is None:
        return []
    statements: list[str] = []
    if resource.rls.enabled:
        statements.append(f"ALTER TABLE {resource.qualified()} ENABLE ROW LEVEL SECURITY")
        if resource.rls.force:
            statements.append(f"ALTER TABLE {resource.qualified()} FORCE ROW LEVEL SECURITY")
    return statements


def create_policy_sql(resource: Resource, policy: Policy) -> str:
    """单条 ``CREATE POLICY``（含按命令派生的子句）。"""
    command, using_sql, check_sql = _policy_clauses(resource, policy)
    kind = "PERMISSIVE" if policy.permissive else "RESTRICTIVE"
    roles = ", ".join(policy.roles)
    clauses = [f"CREATE POLICY {policy.id} ON {resource.qualified()}"]
    clauses.append(f"AS {kind} FOR {command} TO {roles}")
    if using_sql is not None:
        clauses.append(f"USING ({using_sql})")
    if check_sql is not None:
        clauses.append(f"WITH CHECK ({check_sql})")
    return " ".join(clauses)


def drop_policy_sql(resource: Resource, policy: Policy) -> str:
    return f"DROP POLICY IF EXISTS {policy.id} ON {resource.qualified()}"


def policy_statements(resource: Resource) -> list[str]:
    """该资源全部策略的建库语句（先 DROP 再 CREATE，使重复执行幂等）。"""
    if resource.rls is None:
        return []
    statements: list[str] = []
    for policy in resource.rls.policies:
        statements.append(drop_policy_sql(resource, policy))
        statements.append(create_policy_sql(resource, policy))
    return statements


def function_hardening_sql(exception: SecdefException) -> list[str]:
    """SECDEF 函数加固：搜索路径 → OWNER → 回收 PUBLIC → 按需授权。

    ``execute_roles`` 为空时只回收不授予（等于「谁都执行不了」），这是
    fail-closed 的正确表达，而不是「没写就沿用默认」。
    """
    signature = _function_signature(exception.object_id)
    statements = [
        f"ALTER FUNCTION {signature} SET search_path = '{exception.search_path}'",
        f"ALTER FUNCTION {signature} OWNER TO {OWNER_ROLE}",
        f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC",
    ]
    if exception.execute_roles:
        statements.append(
            f"GRANT EXECUTE ON FUNCTION {signature} TO {', '.join(exception.execute_roles)}"
        )
    return statements


# --------------------------------------------------------------------------- #
# 子句派生（唯一实现：闸门与生成器都经过这里）
# --------------------------------------------------------------------------- #
def _policy_clauses(resource: Resource, policy: Policy) -> tuple[str, str | None, str | None]:
    if len(policy.commands) != 1:
        raise DdlError(
            f"{policy.id}: 必须恰好一个命令，实际 {list(policy.commands)}"
            "（FOR ALL 无法表达「DELETE 无策略」）"
        )
    command = policy.commands[0]
    using_sql = (
        compile_sql(policy.using, resource.scope_dimensions) if policy.using is not None else None
    )
    check_sql = (
        compile_sql(policy.with_check, resource.scope_dimensions)
        if policy.with_check is not None
        else None
    )
    required: dict[str, tuple[bool, bool]] = {
        "SELECT": (True, False),
        "INSERT": (False, True),
        "UPDATE": (True, True),
        "DELETE": (True, False),
    }
    if command not in required:
        raise DdlError(f"{policy.id}: 未知命令 {command!r}")
    need_using, need_check = required[command]
    if need_using != (using_sql is not None):
        raise DdlError(
            f"{policy.id}: {command} "
            f"{'需要' if need_using else '不接受'} USING，实际 "
            f"{'有' if using_sql is not None else '无'}（PG16 语法约束）"
        )
    if need_check != (check_sql is not None):
        raise DdlError(
            f"{policy.id}: {command} "
            f"{'需要' if need_check else '不接受'} WITH CHECK，实际 "
            f"{'有' if check_sql is not None else '无'}（PG16 语法约束）"
        )
    return command, using_sql, check_sql


def _function_signature(object_id: str) -> str:
    """``industrial.outbox.function.list_active_tenants()`` → ``outbox.list_active_tenants()``。"""
    segments = object_id.split(".")
    if len(segments) != 4 or segments[2] != "function":
        raise DdlError(
            f"函数标识必须是 <database>.<schema>.function.<name>(<args>)，实际 {object_id!r}"
        )
    signature = f"{segments[1]}.{segments[3]}"
    if "(" not in signature:
        raise DdlError(f"函数标识缺少参数签名（无参也要写 ()）：{object_id!r}")
    return signature


__all__ = [
    "DdlError",
    "create_policy_sql",
    "drop_policy_sql",
    "function_hardening_sql",
    "policy_statements",
    "rls_statements",
]
