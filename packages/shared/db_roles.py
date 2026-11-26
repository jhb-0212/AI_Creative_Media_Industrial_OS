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


# 平台运维 → 配置字段。与 ``_DOMAIN_ROLE_FIELDS`` 分开的理由不是分类癖：
# ``domain_role_names()`` 是「全部**业务域**角色」的口径（迁移、授权矩阵、
# ACL 对账都按它取），把运维角色混进去会让「业务域角色」这个集合同时表示
# 两种东西——它没有自有 schema，`business_tables()` 对它返回空集，
# 于是「本域每张表都必须声明」这条判据对它变成永真式。
_INFRA_ROLE_FIELDS: dict[str, str] = {
    "outbox_ops": "postgres_outbox_ops_user",
}


def infra_role(infra: str) -> str:
    """平台运维角色名（未知键直接报错，不返回空串）。"""
    try:
        field = _INFRA_ROLE_FIELDS[infra]
    except KeyError:
        raise LookupError(
            f"未知运维角色 {infra!r}；已知：{sorted(_INFRA_ROLE_FIELDS)}"
        ) from None
    value: str = getattr(get_settings(), field)
    return value


def infra_roles() -> dict[str, str]:
    """``运维键 -> 角色名`` 全量映射（授权矩阵与断言按它取用）。"""
    return {infra: infra_role(infra) for infra in _INFRA_ROLE_FIELDS}


# 维护四角色（WP-03）。它们不自带 schema、不进 ``DOMAINS``——把维护身份混进
# 业务域集合会让「一域一角色」的覆盖判据对它们求值成永真式（同 ``INFRA_ROLES``
# 的理由）。但维护身份也不是「运维角色」：运维清理投递事实，维护改的是业务
# 状态且必须留工单，两者的授权面与审计义务都不同，所以单独一张表。
_MAINTENANCE_ROLE_FIELDS: dict[str, str] = {
    "owner": "postgres_qc_maintenance_owner_user",
    "operator": "postgres_qc_maintenance_operator_user",
    "login": "postgres_qc_maintenance_login_user",
    "worker": "postgres_qc_worker_user",
}


def maintenance_role(key: str) -> str:
    """维护角色名（``owner`` / ``operator`` / ``login`` / ``worker``）。"""
    try:
        field = _MAINTENANCE_ROLE_FIELDS[key]
    except KeyError:
        raise LookupError(
            f"未知维护角色 {key!r}；已知：{sorted(_MAINTENANCE_ROLE_FIELDS)}"
        ) from None
    value: str = getattr(get_settings(), field)
    return value


def maintenance_roles() -> dict[str, str]:
    """``键 -> 角色名`` 全量映射（迁移、注册表断言与集成用例按它取用）。"""
    return {key: maintenance_role(key) for key in _MAINTENANCE_ROLE_FIELDS}


# 业务域 → 配置字段。键就是 schema 名（DATA-1：一域一角色一 schema）。
# 这张表存在的理由：调用方按**域**取角色（``domain_role("asset_service")``），
# 而不是靠 ``domain_role_names()`` 的元组下标——下标用法一旦有人调整顺序，
# 就会「把 A 域的权限授给 B 域角色」，而代码看起来毫无问题。
_DOMAIN_ROLE_FIELDS: dict[str, str] = {
    "project_planning": "postgres_project_planning_user",
    "asset_service": "postgres_asset_service_user",
    # R16 三域拆分（迁移 a0b1c2d3e4f5）：生产链 / 预算额度 / 质检治理。
    # 拆分的理由不是「表变多了」：生产路线与 Gate 决策的证据生命周期、
    # 预算账本的追加式不可变语义，都与项目规划的 DML 面不同；同角色同表空间
    # 会让「账本只能 INSERT」这类收窄失去载体。
    "production_planning": "postgres_production_planning_user",
    "budget_quota": "postgres_budget_quota_user",
    "qc_governance": "postgres_qc_governance_user",
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


def domain_role_names() -> tuple[str, ...]:
    """全部业务域角色名（按 ``_DOMAIN_ROLE_FIELDS`` 的插入序）。

    返回的是**当前**域集合，不是历史快照：迁移里需要「当时有几个域」时必须
    自己写快照别名（见 ``d8e9f0a1b2c3`` 的 ``_snapshot_domain_roles``），否则
    新增一个域就会静默改写一条已执行过的迁移的行为。
    """
    roles = domain_roles()
    return tuple(roles[domain] for domain in _DOMAIN_ROLE_FIELDS)


__all__ = [
    "app_role_name",
    "domain_role",
    "domain_role_names",
    "domain_roles",
    "infra_role",
    "infra_roles",
    "maintenance_role",
    "maintenance_roles",
    "relay_role_name",
]
