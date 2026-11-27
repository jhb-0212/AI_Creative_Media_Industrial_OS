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
    domain_role,
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
    """一个角色的目标态（集群事实，不含口令值）。

    2026-11-27 WP-01：除「属性目标态」外，补齐**生命周期元数据**。原因是同一个
    角色可能在多个迁移里被建、被改、被删——只声明属性无法回答「谁有权删它」。
    生命周期字段的取值受限，由 ``spec_problems`` 做一致性校验：

    - ``lifecycle_owner``：``bootstrap`` / ``migration`` / ``governance``；
    - ``lifecycle_state``：``active`` / ``deprecated`` / ``retired``；
    - ``deletion_policy``：``bootstrap_retire_only`` / ``migration_owned`` /
      ``governance_gate``。
    """

    name: str
    login: bool
    # 需要口令的角色给出配置字段名（``Settings`` 上的属性名），由执行方取值。
    password_setting: str | None
    purpose: str
    lifecycle_owner: str = "bootstrap"
    lifecycle_state: str = "active"
    deletion_policy: str = "bootstrap_retire_only"
    superuser: bool = False
    createdb: bool = False
    createrole: bool = False
    bypassrls: bool = False
    inherit: bool = True
    member_of: tuple[str, ...] = ()
    database_domains: tuple[str, ...] = ()
    adr_refs: tuple[str, ...] = ()
    migration_refs: tuple[str, ...] = ()

    @property
    def role_type(self) -> str:
        """LOGIN / NOLOGIN 由 ``login`` 派生，不单独存一份（避免两份真相）。"""
        return "LOGIN" if self.login else "NOLOGIN"

    @property
    def can_be_dropped_by_migration(self) -> bool:
        """迁移是否有权删除该角色（WP-02/WP-03 的核心判据）。"""
        return self.deletion_policy == "migration_owned"


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
    # R16 之后的「当前物理角色」在注册表里（``runtime_role``），不在 db_roles。
    # 它们与 ``legacy_role`` 并存，但**当前是占位**：注册表自述「8 个 svc_* 表权限
    # 全零」，仓库里没有任何代码/配置用它们连接，也没有任何迁移创建它们（集群里
    # 那 8 个是手工产物）；Settings 上更没有对应的口令字段。
    #
    # 因此声明为 NOLOGIN：一个「零授权 + 无人使用 + 无口令来源」的登录壳身份，
    # 与已退役的 qc_worker 是同一类风险面（账号存在但没人负责）。R16 真正启用时，
    # 改这一处声明（login=True + 补配置字段）再由 bootstrap 收敛即可——声明是
    # 目标态，bootstrap 负责把集群拉过去，而不是反过来。
    registry = load_registry(_REGISTRY_PATH)
    for registry_domain in registry.domains:
        specs.append(
            RoleSpec(
                name=registry_domain.runtime_role,
                login=False,
                password_setting=None,
                purpose=f"域 {registry_domain.id} 的运行时角色（R16 占位，零授权未启用）",
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
    return tuple(_with_lifecycle(spec) for spec in deduped.values())


# 角色的「生命周期事故史」：这些 revision 在 upgrade/downgrade 里直接建/改/删过
# 该角色（来自 P1 全链矩阵，语句级展开，不是字面搜索）。它不是"最后一个修改者"，
# 而是"所有需要一起看的迁移"——WP-02 的三处 DROP ROLE 就是靠这张表先定位到的。
#
# 键是**角色句柄**（``domain:asset_service`` 这类稳定键），不是角色名字面量：
# 角色名可从部署配置改名（CRED-3），把字面量写在这里等于把「名字」钉死成第二份
# 事实源，也会让 ``test_db_role_names`` 闸门（禁写死默认角色名）失效。
_LIFECYCLE_REFS: dict[str, tuple[str, ...]] = {
    "app": ("9e3b4c5d6e7f",),
    "relay": ("7a8b9c0d1e2f",),
    "infra:outbox_ops": ("c5d6e7f8a9b0",),
    "domain:project_planning": ("8f3b2c1d4e5a",),
    "domain:asset_service": ("8f3b2c1d4e5a",),
    "domain:production_planning": ("e0f1a2b3c4d5", "a0b1c2d3e4f5"),
    "domain:budget_quota": ("e0f1a2b3c4d5", "a0b1c2d3e4f5"),
    "domain:qc_governance": ("e0f1a2b3c4d5", "a0b1c2d3e4f5"),
    "maintenance:owner": ("e6a7b8c9d0e1",),
    "maintenance:operator": ("e6a7b8c9d0e1",),
    "maintenance:login": ("e6a7b8c9d0e1",),
    "scheduler": ("a5b6c7d8e9f0",),
}


def _role_handle(name: str) -> str | None:
    """把角色名映射回稳定句柄（配置改名后仍能命中生命周期表）。"""
    if name == app_role_name():
        return "app"
    if name == relay_role_name():
        return "relay"
    for key, value in infra_roles().items():
        if value == name:
            return f"infra:{key}"
    for key, value in maintenance_roles().items():
        if value == name:
            return f"maintenance:{key}"
    if name == scheduler_role():
        return "scheduler"
    for domain in _domain_mappings():
        if domain_role(domain) == name:
            return f"domain:{domain}"
    return None


def _with_lifecycle(spec: RoleSpec) -> RoleSpec:
    """补齐派生型生命周期字段（域归属、成员关系、迁移引用）。

    能算出就不手填：域归属与 ``db_grants`` 的角色映射同源，成员关系与
    ``db_roles.maintenance_roles`` 的模型同源；手填只会多一份漂移面。
    """
    from dataclasses import replace

    domains: tuple[str, ...] = ()
    for domain in _domain_mappings():
        if domain_role(domain) == spec.name:
            domains = (domain,)
            break

    member_of: tuple[str, ...] = ()
    maintenance = maintenance_roles()
    # 实际方向是 ``GRANT qc_maintenance_operator TO qc_maintenance_login``：
    # login 是 operator 组的成员（operator 是 NOLOGIN 权限组）。方向写反会让
    # 对账闸门把一个健康集群报成漂移。
    if spec.name == maintenance.get("login") and maintenance.get("operator"):
        member_of = (maintenance["operator"],)

    handle = _role_handle(spec.name)
    refs = _LIFECYCLE_REFS.get(handle, ()) if handle else ()

    return replace(
        spec,
        database_domains=domains,
        member_of=member_of,
        migration_refs=refs,
        adr_refs=(),  # 空 = 没有已登记的 ADR；不拿未落档的决策编号充数
    )


def _domain_mappings() -> list[str]:
    """域 id 列表（供域归属派生用，不在本模块复制一份域清单）。"""
    from packages.shared.db_grants import DOMAINS

    return list(DOMAINS)


#: 生命周期字段的合法取值。新增取值前先想清楚"谁有权做这个动作"。
LIFECYCLE_OWNERS = frozenset({"bootstrap", "migration", "governance"})
LIFECYCLE_STATES = frozenset({"active", "deprecated", "retired"})
DELETION_POLICIES = frozenset(
    {"bootstrap_retire_only", "migration_owned", "governance_gate"}
)


def spec_problems(specs: tuple[RoleSpec, ...] | None = None) -> list[str]:
    """声明自身的自洽性校验（不连库）：返回问题清单，空 = 通过。

    这不是"再抄一遍 pytest"，而是让**声明写错**在 import 阶段就暴露：
    提权属性为真、NOLOGIN 却配了口令、退役身份仍标 active、迁移拥有删除权
    却又是 bootstrap 管理——这些都是两份事实源开始分叉的第一现场。
    """
    items = specs if specs is not None else role_specs()
    problems: list[str] = []
    for spec in items:
        if any((spec.superuser, spec.createdb, spec.createrole, spec.bypassrls)):
            problems.append(
                f"{spec.name}：声明里出现提权属性（必须 {_NO_ESCALATION_INVARIANT}）"
            )
        if not spec.inherit:
            problems.append(f"{spec.name}：声明关闭了 INHERIT，成员关系会静默失效")
        if spec.lifecycle_owner not in LIFECYCLE_OWNERS:
            problems.append(f"{spec.name}：未知 lifecycle_owner={spec.lifecycle_owner}")
        if spec.lifecycle_state not in LIFECYCLE_STATES:
            problems.append(f"{spec.name}：未知 lifecycle_state={spec.lifecycle_state}")
        if spec.deletion_policy not in DELETION_POLICIES:
            problems.append(f"{spec.name}：未知 deletion_policy={spec.deletion_policy}")
        if spec.lifecycle_state == "retired":
            problems.append(
                f"{spec.name}：已退役身份不得出现在「期望存在」的声明里"
                "（退役要移出 role_specs，再走独立退役流程）"
            )
        if spec.deletion_policy == "migration_owned" and spec.lifecycle_owner != "migration":
            problems.append(
                f"{spec.name}：deletion_policy=migration_owned 但 owner="
                f"{spec.lifecycle_owner}（删除权与所有权必须同源）"
            )
        if not spec.login and spec.password_setting:
            problems.append(
                f"{spec.name}：NOLOGIN 角色不应声明口令字段（{spec.password_setting}）"
            )
    return problems


def registry_payload(specs: tuple[RoleSpec, ...] | None = None) -> list[dict[str, object]]:
    """把声明导出为可序列化结构（**生成物**，不得反向成为第二事实源）。

    仅供审计报告 / CI 产物 / 与 catalog 对账使用；任何"手工改 JSON 再回灌"
    的用法都是把事实源分裂成两份，明确禁止。
    """
    items = specs if specs is not None else role_specs()
    return [
        {
            "name": spec.name,
            "role_type": spec.role_type,
            "lifecycle_owner": spec.lifecycle_owner,
            "lifecycle_state": spec.lifecycle_state,
            "login": spec.login,
            "superuser": spec.superuser,
            "createdb": spec.createdb,
            "createrole": spec.createrole,
            "bypassrls": spec.bypassrls,
            "inherit": spec.inherit,
            "member_of": list(spec.member_of),
            "database_domains": list(spec.database_domains),
            "deletion_policy": spec.deletion_policy,
            "adr_refs": list(spec.adr_refs),
            "migration_refs": list(spec.migration_refs),
            "purpose": spec.purpose,
        }
        for spec in items
    ]


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


# ---------------------------------------------------------------------------
# 退役角色（WP-03 方案 A）：`RETIRED_ROLES` 是 `role_specs.py` 里的**第二段生命周期**，
# 不是第二份身份事实源。
#
# 为什么必须在这里声明，而不是"从 role_specs 里删掉就算退役"
# -------------------------------------------------------
# 删掉只剩一句"现在不存在"，回答不了三个问题：哪个 revision 退的、退的时候
# 是什么属性、降级回到那个 revision 时该恢复成什么。于是角色就"只活在提交信息里"，
# 降级链要么凭空造一个默认属性的角色（比升级前更宽松），要么直接断。
#
# 与 `role_specs()` 的边界（五条硬约束落到代码的位置）
# -------------------------------------------------
# 1. 两段声明同在本模块，Registry 生成物只有一份（§1 单一事实源）；
# 2. `role_specs()`（当前目标态）**不含**退役角色；`spec_problems()` 反向校验
#    两者不重叠，Bootstrap 因此不会重建退役身份（§2）；
# 3. 降级恢复只读 `historical_attributes`，不从 `_LIFECYCLE_REFS` 或当前默认
#    配置推导（§3）；
# 4. `credential_source` 只写**配置字段名**，口令值永不进声明、生成物或报告；
#    字段缺失时 `restore_retired_role` 失败关闭（§4）；
# 5. 恢复必须带显式确认（`AUTHORIZE_RETIRED_ROLE_RESTORE=<revision>`）并
#    `RAISE WARNING`，不能只留一条日志（§5）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetiredRole:
    """一个已退役角色的历史定义 + 降级恢复契约。

    字段刻意只保留"降级时真正要用到"的五个：多写一个就多一个可能与
    `role_specs()` 分叉的口径。
    """

    #: 在哪个 revision 退役（12 位十六进制，必须能在迁移链里定位）
    retired_at_revision: str
    #: 为什么退役——写给降级的人看，不是给审计填空的套话
    retirement_reason: str
    #: 退役前的属性快照（登录性、提权位、成员关系、域归属、口令字段名）
    historical_attributes: dict[str, object]
    #: `restore_on_downgrade`（降级要恢复）/ `never_restore`（明确不恢复）
    downgrade_restore_policy: str
    #: 恢复登录身份时用的可信凭据来源（`settings.<字段名>`，绝不写口令值）
    credential_source: str | None
    #: 本系统管理范围（本库 + 本迁移的授权面）内的授权是否已全部撤销。
    #: 这一位**只**证明"我们自己的账本清了"，不证明集群范围。
    managed_scope_revoked: bool = False
    #: 其他数据库 / 外部系统仍持有该角色的 ACL、属主或成员关系（库名或描述）。
    #: 未做过集群核验时保持空元组，同时 `cluster_wide_zero_authorization_verified=False`。
    external_dependencies: tuple[str, ...] = ()
    #: 是否完成**全集群**零授权核验。只有在真正检查过所有库之后才允许置 True；
    #: 不允许因为"本库撤干净了"就推断集群干净。
    cluster_wide_zero_authorization_verified: bool = False

    @property
    def login(self) -> bool:
        return bool(self.historical_attributes.get("login", False))

    @property
    def member_of(self) -> tuple[str, ...]:
        value = self.historical_attributes.get("member_of", ())
        return tuple(str(item) for item in value) if isinstance(value, (list, tuple)) else ()

    @property
    def database_domains(self) -> tuple[str, ...]:
        value = self.historical_attributes.get("database_domains", ())
        return tuple(str(item) for item in value) if isinstance(value, (list, tuple)) else ()


#: 合法取值：只允许这两种。第三种"看情况恢复"会让降级结果取决于执行人。
RETIRED_RESTORE_POLICIES = frozenset({"restore_on_downgrade", "never_restore"})

#: 退役身份的句柄与名称从 `db_roles` 派生，不在本模块写死默认字面量
#: （`test_db_role_names` / CRED-3 都禁止：名字的唯一来源是 config + db_roles）。
_WORKER_KEY = "worker"
_WORKER_ROLE = maintenance_roles()[_WORKER_KEY]
_WORKER_PASSWORD_SETTING = f"postgres_{_WORKER_ROLE}_password"

RETIRED_ROLES: tuple[RetiredRole, ...] = (
    RetiredRole(
        retired_at_revision="f2b3c4d5e6a7",
        retirement_reason=(
            f"{_WORKER_ROLE} 是空壳登录身份：仓库内没有任何 DSN/池/进程入口使用它，"
            "集群里零依赖、零成员关系；Worker 真正连库用的是 QC 治理域角色"
            "（见 db_grants 的域映射）。给它补授权只会造出无人负责却可登录的"
            "权限面（AUTHZ-5 复核结论）。"
        ),
        historical_attributes={
            "login": True,
            "superuser": False,
            "createdb": False,
            "createrole": False,
            "bypassrls": False,
            "inherit": True,
            "member_of": (),  # 退役时为零成员；恢复也不得自动加回 operator
            "database_domains": (),  # 空壳没有任何库级归属
            "password_setting": _WORKER_PASSWORD_SETTING,
        },
        downgrade_restore_policy="restore_on_downgrade",
        credential_source=f"settings.{_WORKER_PASSWORD_SETTING}",
    ),
)


def retired_role_names() -> frozenset[str]:
    """退役角色的**名字**（按部署配置解析，不写死默认字面量）。"""
    names: set[str] = set()
    for item in RETIRED_ROLES:
        revision = item.retired_at_revision
        # 名称来源于迁移自身的 `maintenance_role("worker")` 口径，本模块不另存一份。
        if revision == "f2b3c4d5e6a7":
            names.add(maintenance_roles()["worker"])
    return frozenset(names)


def retired_roles_by_name() -> dict[str, RetiredRole]:
    """名字 → 退役契约（供迁移降级与 Registry 生成物共用）。"""
    out: dict[str, RetiredRole] = {}
    for name in retired_role_names():
        for item in RETIRED_ROLES:
            if item.retired_at_revision == "f2b3c4d5e6a7":
                out[name] = item
    return out


def retired_role_problems() -> list[str]:
    """退役声明自身的自洽性校验（不连库）：空 = 通过。"""
    problems: list[str] = []
    active = retired_role_names()
    for spec in role_specs():
        if spec.name in active:
            problems.append(
                f"{spec.name}：同时出现在 role_specs 与 RETIRED_ROLES"
                "（同一身份不能既是目标态又是退役态）"
            )
    seen: set[str] = set()
    for item in RETIRED_ROLES:
        revision = item.retired_at_revision
        if not (len(revision) == 12 and all(ch in "0123456789abcdef" for ch in revision)):
            problems.append(f"{revision}：退役点必须是 12 位十六进制 revision id")
        if revision in seen:
            problems.append(f"{revision}：同一 revision 不能退役两个身份（请分开登记）")
        seen.add(revision)
        if item.downgrade_restore_policy not in RETIRED_RESTORE_POLICIES:
            problems.append(
                f"{revision}：未知 downgrade_restore_policy={item.downgrade_restore_policy}"
            )
        if not item.retirement_reason.strip():
            problems.append(f"{revision}：retirement_reason 不能为空")
        attrs = item.historical_attributes
        for flag in ("superuser", "createdb", "createrole", "bypassrls"):
            if attrs.get(flag):
                problems.append(
                    f"{revision}：历史属性里出现提权位 {flag}=True"
                    f"（恢复后必须仍是 {_NO_ESCALATION_INVARIANT}）"
                )
        if item.login and not item.credential_source:
            problems.append(
                f"{revision}：历史定义是 LOGIN，但没有 credential_source"
                "（降级恢复会 fail-closed，等于不可恢复）"
            )
        if not item.login and item.credential_source:
            problems.append(f"{revision}：历史定义是 NOLOGIN，不应声明 credential_source")
        if item.credential_source and not item.credential_source.startswith("settings."):
            problems.append(
                f"{revision}：credential_source 只能指向 `settings.<字段名>`，"
                f"不得内联口令值（当前={item.credential_source[:12]}…）"
            )
        if (
            item.downgrade_restore_policy == "restore_on_downgrade"
            and item.login
            and attrs.get("password_setting") is None
        ):
            problems.append(
                f"{revision}：restore_on_downgrade 的 LOGIN 身份必须写明 "
                "historical_attributes['password_setting']"
            )
        # 四个生命周期状态必须分开表达，禁止用一个布尔值概括：
        # "已退役" ≠ "本系统管理范围撤干净" ≠ "集群范围零授权"。
        if item.cluster_wide_zero_authorization_verified:
            if not item.managed_scope_revoked:
                problems.append(
                    f"{revision}：声称集群零授权，但 managed_scope_revoked=False"
                )
            if item.external_dependencies:
                problems.append(
                    f"{revision}：声称集群零授权，但仍登记 external_dependencies="
                    f"{list(item.external_dependencies)}"
                )
    return problems


def retired_registry_payload() -> list[dict[str, object]]:
    """退役段的可序列化导出（**生成物**，不得反向回灌）。"""
    return [
        {
            "name": name,
            "retired_at_revision": item.retired_at_revision,
            "retirement_reason": item.retirement_reason,
            "historical_attributes": {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in item.historical_attributes.items()
            },
            "downgrade_restore_policy": item.downgrade_restore_policy,
            "credential_source": item.credential_source,
            "lifecycle_state": "retired",
            "managed_scope_revoked": item.managed_scope_revoked,
            "external_dependencies": list(item.external_dependencies),
            "cluster_wide_zero_authorization_verified": (
                item.cluster_wide_zero_authorization_verified
            ),
        }
        for name, item in retired_roles_by_name().items()
    ]

