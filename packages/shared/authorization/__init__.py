"""授权单一事实源的代码侧入口（ADR-R40 附录 D）。

模块分工（单向依赖，无循环）：
- ``ast``      —— 受限表达式 AST：解析 + SQL 编译（不依赖其余两者）；
- ``registry`` —— 加载与语义校验（依赖 ``ast``）；
- ``ddl``      —— 从声明派生 DDL（依赖 ``ast`` 与 ``registry`` 的模型）。

暴露 ``load_registry`` / ``validate`` / ``create_policy_sql`` 等常用符号，
使调用方不必记住文件划分。
"""

from __future__ import annotations

from packages.shared.authorization.ast import (
    ExpressionError,
    Node,
    compile_sql,
    parse,
)
from packages.shared.authorization.ddl import (
    DdlError,
    create_policy_sql,
    drop_policy_sql,
    function_hardening_sql,
    policy_statements,
    rls_statements,
)
from packages.shared.authorization.registry import (
    Registry,
    RegistryError,
    load_registry,
    validate,
)

__all__ = [
    "DdlError",
    "ExpressionError",
    "Node",
    "Registry",
    "RegistryError",
    "compile_sql",
    "create_policy_sql",
    "drop_policy_sql",
    "function_hardening_sql",
    "load_registry",
    "parse",
    "policy_statements",
    "rls_statements",
    "validate",
]
