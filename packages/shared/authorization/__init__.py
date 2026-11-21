"""授权单一事实源的代码侧入口（ADR-R40 附录 D）。

模块分工（单向依赖，无循环）：
- ``ast``      —— 受限表达式 AST：解析 + SQL 编译（不依赖其余两者）；
- ``registry`` —— 加载与语义校验（依赖 ``ast``）；
- ``ddl``      —— 从声明派生 DDL（依赖 ``ast`` 与 ``registry`` 的模型）。

为什么这里是惰性门面而不是 eager import
----------------------------------------
Python 导入 ``packages.shared.authorization.registry`` 时**必然先执行本包的
``__init__``**。若 ``__init__`` 在导入期再 import 子模块，就得到
``包 → ddl → registry → 包`` 的环：导入顺序敏感，且任何子模块都无法独立加载。
R3 规则把「子模块 → 祖先包」算作边（父包初始化确实会执行），因此唯一的
结构性解法是**让包初始化不加载任何子模块**，同时保留原来的门面 API。

PEP 562 的模块级 ``__getattr__`` 正好满足：符号在被真正访问时才从叶子模块解析，
导入期不产生任何包内依赖边。这不是靠 import 顺序救，也不是关闭规则——依赖图
本身变成单向的叶子结构。对外 ``__all__`` 与门面符号保持不变。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

# 符号 → (定义模块, 属性名)。只登记原本由 __init__ 暴露的符号，不扩大 API 面。
_EXPORTS: dict[str, tuple[str, str]] = {
    "DdlError": ("packages.shared.authorization.ddl", "DdlError"),
    "ExpressionError": ("packages.shared.authorization.ast", "ExpressionError"),
    "Node": ("packages.shared.authorization.ast", "Node"),
    "Registry": ("packages.shared.authorization.registry", "Registry"),
    "RegistryError": ("packages.shared.authorization.registry", "RegistryError"),
    "compile_sql": ("packages.shared.authorization.ast", "compile_sql"),
    "create_policy_sql": ("packages.shared.authorization.ddl", "create_policy_sql"),
    "drop_policy_sql": ("packages.shared.authorization.ddl", "drop_policy_sql"),
    "function_hardening_sql": (
        "packages.shared.authorization.ddl",
        "function_hardening_sql",
    ),
    "load_registry": ("packages.shared.authorization.registry", "load_registry"),
    "parse": ("packages.shared.authorization.ast", "parse"),
    "policy_statements": ("packages.shared.authorization.ddl", "policy_statements"),
    "rls_statements": ("packages.shared.authorization.ddl", "rls_statements"),
    "validate": ("packages.shared.authorization.registry", "validate"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """按需解析门面符号；未知名字按模块属性协议抛 AttributeError。"""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    # 解析一次后固化：后续访问不再走 importlib，也不存在「每次拿到不同对象」。
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """保持门面符号可被 discover（``dir()`` 与 ``__all__`` 一致）。"""
    return sorted(set(globals()) | set(_EXPORTS))
