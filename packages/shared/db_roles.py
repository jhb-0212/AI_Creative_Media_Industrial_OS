"""数据库角色名的唯一读取口径（迁移与 DDL 构造器专用）。

为什么需要这个模块（根因，不是为省几行代码）
--------------------------------------------
角色名是**配置**（``POSTGRES_APP_USER`` / ``POSTGRES_RELAY_USER`` /
``POSTGRES_PROJECT_PLANNING_USER`` / ``POSTGRES_ASSET_SERVICE_USER``），却曾被
当成常量散落在 12 处迁移与 DDL 构造器里。后果不是「不好看」，而是：

- 部署方把角色名改成 ``afs_app`` 后，迁移仍然 ``CREATE ROLE app_role`` 并
  ``GRANT TO app_role``——建出一个没人用的角色，业务进程以 ``afs_app`` 连库时
  权限全无；
- 或者更糟：``app_role`` 在某个环境里不存在，迁移直接 GRANT 失败，升级路径
  被堵死（codex 评审 T5 实测）。

两种失效都不会被「读代码」发现，因为代码看起来完全正常。名字的取法收敛到
本模块后，迁移只描述「授什么权」，不再描述「角色叫什么」；
``scripts/boundary_assertions.py`` 的 ``db.role_names_from_config`` 断言禁止
任何其他文件出现默认角色名字面量（``packages/shared/config.py`` 除外——那里
是默认值的唯一定义处）。

调用约定：**在使用处调用**（放进函数体内），不要提升成模块级常量——模块级
常量在 import 时求值，会让「改了环境变量再跑迁移」这种场景静默用旧值。
"""

from __future__ import annotations

from packages.shared.config import get_settings


def app_role_name() -> str:
    """遗留应用角色名（``POSTGRES_APP_USER``）。"""
    return get_settings().postgres_app_user


def relay_role_name() -> str:
    """Outbox 投递角色名（``POSTGRES_RELAY_USER``）。"""
    return get_settings().postgres_relay_user


# 业务域 → 配置字段。键就是 schema 名（DATA-1：一域一角色一 schema）。
# 这张表存在的理由：调用方按**域**取角色（``domain_role("asset_service")``），
# 而不是靠 ``domain_role_names()`` 的元组下标——下标用法一旦有人调整顺序，
# 就会「把 A 域的权限授给 B 域角色」，而代码看起来毫无问题。
_DOMAIN_ROLE_FIELDS: dict[str, str] = {
    "project_planning": "postgres_project_planning_user",
    "asset_service": "postgres_asset_service_user",
}


def domain_role(domain: str) -> str:
    """业务域对应的数据库角色名（未知域直接报错，不返回空串）。"""
    try:
        field = _DOMAIN_ROLE_FIELDS[domain]
    except KeyError:
        raise LookupError(
            f"未知业务域 {domain!r}；已知域：{sorted(_DOMAIN_ROLE_FIELDS)}"
        ) from None
    # 用带注解的中间变量而不是直接 return getattr(...)：字段名是运行期字符串，
    # getattr 的返回类型是 Any，直接返回会把「角色名是字符串」这个事实丢掉。
    value: str = getattr(get_settings(), field)
    return value


def domain_roles() -> dict[str, str]:
    """``域 -> 角色名`` 全量映射（授权矩阵、集成用例按域取用）。"""
    return {domain: domain_role(domain) for domain in _DOMAIN_ROLE_FIELDS}


def domain_role_names() -> tuple[str, str]:
    """两个业务域角色名（``project_planning``、``asset_service``，DATA-1）。

    保留元组形态供既有调用方（迁移、断言）使用；顺序由 ``_DOMAIN_ROLE_FIELDS``
    的插入序决定，新增域时需同步确认调用方是否依赖位置。
    """
    roles = domain_roles()
    return (roles["project_planning"], roles["asset_service"])


__all__ = [
    "app_role_name",
    "domain_role",
    "domain_role_names",
    "domain_roles",
    "relay_role_name",
]
