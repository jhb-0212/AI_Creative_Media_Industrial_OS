"""集群身份的单一声明（角色名 + 登录性 + 不可提权），供漂移闸门与 bootstrap 共用。

为什么需要这一层
----------------
AUTHZ-7 的解除路径是「角色创建与运行时授权从迁移链剥离，交给 bootstrap」。
那一步的前提不是写一个脚本，而是**角色属性必须先有唯一声明处**：现在属性散在
二十多个历史迁移的 ``_create_role(... NOBYPASSRLS ...)`` 里，谁也无法回答
「生产集群里的角色属性应该是什么」。没有声明，bootstrap 只能把迁移里的写法再抄
一遍——那就是第二份事实源，和 ``db_grants`` 当初要解决的问题一模一样。

本模块只做三件事，且刻意不做第四件（不建角色、不授权限）：

1. 从 ``db_roles`` 已有的**名字来源**派生角色全集，不另立名单：
   ``app_role`` / ``outbox_relay_role`` / 运维角色 / 维护角色（owner、operator、
   login；``worker`` 已退役不在此列）/ 调度身份 / 六个业务域角色。
2. 声明两条策略：全部角色 ``NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS``
   且 ``INHERIT``（继承是成员关系的语义前提，关掉会让 ``GRANT group TO member``
   静默失效）；只有维护 owner / operator 不可登录，其余可登录。
3. 提供一个**纯判定** ``diff_role_attributes``：把「声明」与「活库快照」比较，
   返回差异清单。判定是纯函数，才能被单测用构造数据证明「漂移会被抓到」。

为什么不在这里声明口令：口令来自运行配置（``Settings`` 的 SecretStr），
它不是集群事实而是部署输入；把口令写进代码等于把密钥入库。bootstrap 执行时
按 ``password_setting`` 字段从配置取值，声明里只保留「这个角色需要口令」。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from packages.shared.authorization.registry import load_registry
from packages.shared.db_roles import (
    app_role_name,
    domain_role_names,
    infra_roles,
    maintenance_roles,
    relay_role_name,
    scheduler_role,
)

_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "authorization"
    / "authorization_registry.yaml"
)


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """一个角色的目标态（集群事实，不含口令值）。"""

    name: str
    login: bool
    # 需要口令的角色给出配置字段名（``Settings`` 上的属性名），由执行方取值。
    password_setting: str | None
    purpose: str


# 全部运行时角色都必须「不可提权」。这不是四条独立策略，而是一条：任何一条
# 为真都会让 RLS 或权限矩阵失效——``rolsuper`` / ``rolbypassrls`` 直接绕过策略，
# ``rolcreatedb`` 能另建一个不受迁移约束的库，``rolcreaterole`` 能造出新身份。
_NO_ESCALATION_INVARIANT = (
    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT"
)

# 不可登录的身份只有维护权限组与维护对象属主：它们的存在意义就是「只能被
# 成员关系或属主引用」，一旦可登录，「账号存在」与「能维护」就分不开了。
_NON_LOGIN_KEYS = frozenset({"owner", "operator"})

_PASSWORD_FIELDS: dict[str, str] = {
    "app": "postgres_app_password",
    "relay": "postgres_relay_password",
    "outbox_ops": "postgres_outbox_ops_password",
    "project_planning": "postgres_project_planning_password",
    "asset_service": "postgres_asset_service_password",
    "production_planning": "postgres_production_planning_password",
    "budget_quota": "postgres_budget_quota_password",
    "qc_governance": "postgres_qc_governance_password",
    "maintenance_login": "postgres_qc_maintenance_login_password",
    "scheduler": "postgres_qc_scheduler_password",
}


def role_specs() -> tuple[RoleSpec, ...]:
    """派生全部运行时角色的目标态（顺序稳定，便于 diff 与报错阅读）。"""
    specs: list[RoleSpec] = [
        RoleSpec(
            name=app_role_name(),
            login=True,
            password_setting=_PASSWORD_FIELDS["app"],
            purpose="遗留单应用角色（outbox/auth 只读），保留给测试与工具",
        ),
        RoleSpec(
            name=relay_role_name(),
            login=True,
            password_setting=_PASSWORD_FIELDS["relay"],
            purpose="Outbox Relay：只消费 outbox 并回写投递元数据列",
        ),
    ]
    for infra, name in infra_roles().items():
        specs.append(
            RoleSpec(
                name=name,
                login=True,
                password_setting=_PASSWORD_FIELDS.get(infra),
                purpose=f"运维角色 {infra}：死信重放与投递事实清理",
            )
        )
    for key, name in maintenance_roles().items():
        if key == "worker":
            # 历史键：角色已退役（迁移 f2b3c4d5e6a7），不得再声明为期望存在。
            continue
        specs.append(
            RoleSpec(
                name=name,
                login=key not in _NON_LOGIN_KEYS,
                password_setting=(
                    _PASSWORD_FIELDS["maintenance_login"] if key == "login" else None
                ),
                purpose=f"维护身份 {key}（WP-03 四角色模型）",
            )
        )
    specs.append(
        RoleSpec(
            name=scheduler_role(),
            login=True,
            password_setting=_PASSWORD_FIELDS["scheduler"],
            purpose="调度发现身份：只允许调用零参发现函数",
        )
    )
    for domain in domain_role_names():
        specs.append(
            RoleSpec(
                name=domain,
                login=True,
                password_setting=_domain_password_setting(domain),
                purpose="遗留业务域角色（R16 拆分前的一域一角色）",
            )
        )
    # R16 之后的「当前物理角色」在注册表里（``runtime_role``），不在 db_roles：
    # 它们与 ``legacy_role`` 并存，两个集合在集群里都真实存在且都可登录。
    # 声明必须覆盖两者，否则闸门只盯住一半身份面。
    registry = load_registry(_REGISTRY_PATH)
    for registry_domain in registry.domains:
        specs.append(
            RoleSpec(
                name=registry_domain.runtime_role,
                login=True,
                password_setting=None,
                purpose=f"域 {registry_domain.id} 的运行时角色（R16）",
            )
        )
        if registry_domain.legacy_role:
            specs.append(
                RoleSpec(
                    name=registry_domain.legacy_role,
                    login=True,
                    password_setting=_domain_password_setting(
                        registry_domain.legacy_role
                    ),
                    purpose=f"域 {registry_domain.id} 的遗留角色（与 runtime_role 并存）",
                )
            )
    # 注册表的 ``legacy_role`` 与 ``domain_role_names()`` 有重叠（同一批角色），
    # 按名字去重：重复声明会让差异清单把同一个角色报两遍，读数的人会以为是
    # 两个对象。
    deduped: dict[str, RoleSpec] = {}
    for spec in specs:
        deduped.setdefault(spec.name, spec)
    return tuple(deduped.values())


def _domain_password_setting(role_name: str) -> str | None:
    """把域角色名映射到配置字段；未登记的域返回 None（由调用方报错）。"""
    from packages.shared.db_grants import DOMAINS
    from packages.shared.db_roles import domain_role

    for domain in DOMAINS:
        if domain_role(domain) == role_name:
            return _PASSWORD_FIELDS.get(domain)
    return None


@dataclass(frozen=True, slots=True)
class LiveRole:
    """活库里的角色快照（只取判定需要的列）。"""

    name: str
    login: bool
    superuser: bool
    create_db: bool
    create_role: bool
    bypass_rls: bool
    inherit: bool


def diff_role_attributes(
    specs: tuple[RoleSpec, ...], live: tuple[LiveRole, ...]
) -> list[str]:
    """比较声明与活库，返回人类可读的差异清单（空 = 一致）。

    判据与理由：
    - **缺失**：声明了但库里没有 → 部署没有按声明收敛（bootstrap 没跑或跑漏）；
    - **提权**：任何一条 ``rolsuper / rolcreatedb / rolcreaterole / rolbypassrls``
      为真 → 直接绕过 RLS 或矩阵，属安全缺陷而不是配置偏差；
    - **可登录性不符**：owner/operator 变得可登录，或运行角色变得不可登录
      （后者会让 Worker/Relay 静默连不上，是可用性事故）；
    - **INHERIT 关闭**：成员关系不再传递，``login`` 拿不到 operator 权限，
      而错误现象是「命令被拒」而不是「配置错」，最难定位。

    未声明的额外角色不在本判定里：集群还可能有运维手工建的身份，把它们一律
    报红会让闸门变成噪音。``scripts/verify_role_attributes.py`` 提供
    ``--include-undeclared`` 做一次性盘点，那属于报告而不是闸门。
    """
    problems: list[str] = []
    by_name = {role.name: role for role in live}
    for spec in specs:
        row = by_name.get(spec.name)
        if row is None:
            problems.append(f"{spec.name}：声明存在但集群里没有（{spec.purpose}）")
            continue
        escalations = [
            label
            for label, flag in (
                ("SUPERUSER", row.superuser),
                ("CREATEDB", row.create_db),
                ("CREATEROLE", row.create_role),
                ("BYPASSRLS", row.bypass_rls),
            )
            if flag
        ]
        if escalations:
            problems.append(
                f"{spec.name}：出现提权属性 {escalations}（必须 {_NO_ESCALATION_INVARIANT}）"
            )
        if row.login != spec.login:
            problems.append(
                f"{spec.name}：可登录性不符，声明={spec.login} 实际={row.login}"
            )
        if not row.inherit:
            problems.append(
                f"{spec.name}：INHERIT 被关闭，成员关系不再传递"
                "（login 会拿不到 operator 权限，且现象是命令被拒）"
            )
    return problems
