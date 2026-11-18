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


def domain_role_names() -> tuple[str, str]:
    """两个业务域角色名（``project_planning``、``asset_service``，DATA-1）。"""
    settings = get_settings()
    return (
        settings.postgres_project_planning_user,
        settings.postgres_asset_service_user,
    )


__all__ = ["app_role_name", "domain_role_names", "relay_role_name"]
