"""授权 Registry 的加载与语义校验（ADR-R40 附录 D）。

为什么校验写在包里而不是只写在闸门脚本里
------------------------------------------
同一条规则有两个消费方：CI 闸门（静态报红）与 IP-4 的生成器（生成前必须
确认自己读到的不是坏数据）。若规则只存在于脚本里，生成器就必然「自己再判
一次」，两套判定立刻开始漂移。规则放这里，脚本与生成器都调 ``validate``。

校验分两层，故意不合并：
- ``load_registry``：**形状**错误（缺字段、未知键、类型不对）→ 抛
  ``RegistryError``。形状错的文件没有任何语义可讨论，继续校验只会产生噪声。
- ``validate``：**语义**错误（域不足 8 个、策略子句与命令不匹配、引用越界列）
  → 返回问题列表。一次报全，而不是遇到第一个就退出——复审者需要看到全貌。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from packages.shared.authorization.ast import (
    AllOf,
    AnyOf,
    Eq,
    ExpressionError,
    InArray,
    IsNull,
    Literal,
    Node,
    NotExpr,
    compile_sql,
    parse,
)

# D.3：object_type 枚举。policy 不是独立授权对象（由 resources[].rls 派生）。
OBJECT_TYPES: frozenset[str] = frozenset(
    {"table", "partition", "sequence", "function", "view", "matview", "policy", "schema"}
)
# D.4：按对象类型的合法操作子集（本文件当前无 permissions 段，规则先立在此，
# 接管时不必再补一套判定）。
LEGAL_OPERATIONS: Mapping[str, frozenset[str]] = {
    "table": frozenset(
        {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    ),
    "partition": frozenset(
        {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    ),
    "sequence": frozenset({"USAGE", "SELECT", "UPDATE"}),
    "function": frozenset({"EXECUTE"}),
    "schema": frozenset({"USAGE", "CREATE"}),
    "view": frozenset({"SELECT"}),
    "matview": frozenset({"SELECT"}),
    "database": frozenset({"CONNECT", "CREATE", "TEMPORARY"}),
}
COMMANDS: frozenset[str] = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
# 域数量是固定事实（E10.0）。少一项或多项即报红，不允许「示例式」声明。
EXPECTED_DOMAINS = 8
# SECDEF 目标配置（D.5）：路径首尾都定死，中间不得出现用户可写 schema。
SECURE_SEARCH_PATH = "pg_catalog, pg_temp"
OWNER_ROLE = "afs_owner"

_POLICY_NAME_RE = re.compile(r"^pol_(?P<table>[a-z0-9_]+)_(?P<cmd>[a-z]+)_(?P<kind>scope|guard)$")


class RegistryError(ValueError):
    """Registry 形状错误（缺字段 / 未知键 / 类型不符）：无法继续做语义校验。"""


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Domain:
    id: str
    schema: str
    runtime_role: str
    legacy_role: str | None


@dataclass(frozen=True, slots=True)
class InfraRole:
    key: str
    name: str
    login: bool


@dataclass(frozen=True, slots=True)
class Policy:
    id: str
    commands: tuple[str, ...]
    permissive: bool
    roles: tuple[str, ...]
    using: Node | None
    with_check: Node | None


@dataclass(frozen=True, slots=True)
class Rls:
    enabled: bool
    force: bool
    policies: tuple[Policy, ...]


@dataclass(frozen=True, slots=True)
class Resource:
    id: str
    database: str
    schema: str
    object_type: str
    object_name: str
    domain: str
    scope_dimensions: Mapping[str, str]
    partitioned: bool
    rls: Rls | None

    def qualified(self) -> str:
        return f"{self.schema}.{self.object_name}"


@dataclass(frozen=True, slots=True)
class SecdefException:
    object_id: str
    kind: str
    reason: str
    owner: str
    unblocks_when: str
    expires_at: str
    execute_roles: tuple[str, ...]
    effective_owner: str
    observed_owner: str
    search_path: str
    accessed_resources: tuple[str, ...]
    privilege_scope: str
    security_test: str
    public_execute: bool
    observed_search_path: str
    observed_public_execute: bool


@dataclass(frozen=True, slots=True)
class PermissionsSource:
    module: str
    covers: tuple[str, ...]
    phase: str
    authority: str
    unblocks_when: str


@dataclass(frozen=True, slots=True)
class Registry:
    schema_version: str
    status: str
    domains: tuple[Domain, ...]
    infra_roles: tuple[InfraRole, ...]
    resources: tuple[Resource, ...]
    exceptions: tuple[SecdefException, ...]
    permissions_source: PermissionsSource | None
    design_delta_ids: tuple[str, ...]
    debt_ids: tuple[str, ...]

    def runtime_roles(self) -> frozenset[str]:
        return frozenset(domain.runtime_role for domain in self.domains)

    def resource(self, resource_id: str) -> Resource:
        for item in self.resources:
            if item.id == resource_id:
                return item
        raise KeyError(resource_id)

    def policies(self) -> Iterable[tuple[Resource, Policy]]:
        for item in self.resources:
            if item.rls is None:
                continue
            for policy in item.rls.policies:
                yield item, policy


# --------------------------------------------------------------------------- #
# 加载（形状层）
# --------------------------------------------------------------------------- #
def load_registry(path: Path | str) -> Registry:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    root = _mapping(raw, "root")
    _reject_extra(
        root,
        {
            "schema_version",
            "status",
            "design_delta",
            "databases",
            "domains",
            "roles",
            "permissions_source",
            "permissions",
            "resources",
            "exceptions",
            "debt",
        },
        "root",
    )

    # 顶层 permissions 与 permissions_source 互斥（V6）：两者同时存在就是
    # 「同一批操作集有两个声明处」，而 CI 比对只是一个更晚的报错点。
    if "permissions" in root and "permissions_source" in root:
        raise RegistryError(
            "permissions 与 permissions_source 不得并存："
            "要么由本文件声明操作集，要么指向既有事实源，不能两边都写"
        )

    domains = tuple(_domain(item, index) for index, item in enumerate(_list(root, "domains")))
    resources = tuple(_resource(item, index) for index, item in enumerate(_list(root, "resources")))
    exceptions = tuple(
        _exception(item, index) for index, item in enumerate(_opt_list(root, "exceptions"))
    )

    return Registry(
        schema_version=_str(root, "schema_version", "root"),
        status=_str(root, "status", "root"),
        domains=domains,
        infra_roles=_infra_roles(root.get("roles")),
        resources=resources,
        exceptions=exceptions,
        permissions_source=_permissions_source(root.get("permissions_source")),
        design_delta_ids=tuple(
            _str(_mapping(item, f"design_delta[{i}]"), "id", f"design_delta[{i}]")
            for i, item in enumerate(_opt_list(root, "design_delta"))
        ),
        debt_ids=tuple(
            _str(_mapping(item, f"debt[{i}]"), "id", f"debt[{i}]")
            for i, item in enumerate(_opt_list(root, "debt"))
        ),
    )


def _domain(raw: object, index: int) -> Domain:
    where = f"domains[{index}]"
    item = _mapping(raw, where)
    _reject_extra(item, {"id", "schema", "runtime_role", "legacy_role"}, where)
    legacy = item.get("legacy_role")
    if legacy is not None and not isinstance(legacy, str):
        raise RegistryError(f"{where}.legacy_role: 必须是字符串或 null")
    return Domain(
        id=_str(item, "id", where),
        schema=_str(item, "schema", where),
        runtime_role=_str(item, "runtime_role", where),
        legacy_role=legacy,
    )


def _infra_roles(raw: object) -> tuple[InfraRole, ...]:
    roles = _mapping(raw, "roles")
    # D.2.1 刻意删除了 roles.mappings；这里用「未知键即报错」把它钉死，
    # 避免有人「体贴地」把域→角色映射加回来。
    _reject_extra(roles, {"infrastructure_roles"}, "roles")
    declared = _mapping(roles.get("infrastructure_roles"), "roles.infrastructure_roles")
    result: list[InfraRole] = []
    for key, body in declared.items():
        where = f"roles.infrastructure_roles.{key}"
        item = _mapping(body, where)
        _reject_extra(
            item,
            {"name", "login", "attributes", "owner_membership", "granted_by", "capabilities"},
            where,
        )
        result.append(
            InfraRole(key=key, name=_str(item, "name", where), login=_bool(item, "login", where))
        )
    return tuple(result)


def _permissions_source(raw: object) -> PermissionsSource | None:
    if raw is None:
        return None
    where = "permissions_source"
    item = _mapping(raw, where)
    _reject_extra(item, {"module", "covers", "phase", "authority", "unblocks_when"}, where)
    return PermissionsSource(
        module=_str(item, "module", where),
        covers=_str_tuple(item, "covers", where),
        phase=_str(item, "phase", where),
        authority=_str(item, "authority", where),
        unblocks_when=_str(item, "unblocks_when", where),
    )


def _resource(raw: object, index: int) -> Resource:
    where = f"resources[{index}]"
    item = _mapping(raw, where)
    _reject_extra(
        item,
        {
            "id",
            "database",
            "schema",
            "object_type",
            "object_name",
            "domain",
            "partitioned",
            "scope_dimensions",
            "rls",
        },
        where,
    )
    dimensions_raw = _mapping(item.get("scope_dimensions"), f"{where}.scope_dimensions")
    dimensions: dict[str, str] = {}
    for column, sql_type in dimensions_raw.items():
        if not isinstance(sql_type, str):
            raise RegistryError(f"{where}.scope_dimensions.{column}: 期望 SQL 类型字符串")
        dimensions[column] = sql_type
    return Resource(
        id=_str(item, "id", where),
        database=_str(item, "database", where),
        schema=_str(item, "schema", where),
        object_type=_str(item, "object_type", where),
        object_name=_str(item, "object_name", where),
        domain=_str(item, "domain", where),
        scope_dimensions=dimensions,
        partitioned=_opt_bool(item, "partitioned", where, default=False),
        rls=_rls(item.get("rls"), where),
    )


def _rls(raw: object, where: str) -> Rls | None:
    if raw is None:
        return None
    item = _mapping(raw, f"{where}.rls")
    _reject_extra(item, {"enabled", "force", "policies"}, f"{where}.rls")
    policies = tuple(
        _policy(entry, f"{where}.rls.policies[{index}]")
        for index, entry in enumerate(_opt_list(item, "policies"))
    )
    return Rls(
        enabled=_bool(item, "enabled", f"{where}.rls"),
        force=_bool(item, "force", f"{where}.rls"),
        policies=policies,
    )


def _policy(raw: object, where: str) -> Policy:
    item = _mapping(raw, where)
    _reject_extra(item, {"id", "commands", "permissive", "roles", "using", "with_check"}, where)
    using_raw = item.get("using")
    check_raw = item.get("with_check")
    return Policy(
        id=_str(item, "id", where),
        commands=_str_tuple(item, "commands", where),
        permissive=_bool(item, "permissive", where),
        roles=_str_tuple(item, "roles", where),
        using=None if using_raw is None else parse(using_raw, where=f"{where}.using"),
        with_check=None if check_raw is None else parse(check_raw, where=f"{where}.with_check"),
    )


def _exception(raw: object, index: int) -> SecdefException:
    where = f"exceptions[{index}]"
    item = _mapping(raw, where)
    _reject_extra(
        item,
        {
            "object",
            "kind",
            "reason",
            "owner",
            "unblocks_when",
            "expires_at",
            "execute_roles",
            "effective_owner",
            "observed_owner",
            "search_path",
            "accessed_resources",
            "privilege_scope",
            "security_test",
            "public_execute",
            "observed_search_path",
            "observed_public_execute",
        },
        where,
    )
    return SecdefException(
        object_id=_str(item, "object", where),
        kind=_str(item, "kind", where),
        reason=_str(item, "reason", where),
        owner=_str(item, "owner", where),
        unblocks_when=_str(item, "unblocks_when", where),
        expires_at=_str(item, "expires_at", where),
        execute_roles=_str_tuple(item, "execute_roles", where),
        effective_owner=_str(item, "effective_owner", where),
        observed_owner=_str(item, "observed_owner", where),
        search_path=_str(item, "search_path", where),
        accessed_resources=_str_tuple(item, "accessed_resources", where),
        privilege_scope=_str(item, "privilege_scope", where),
        security_test=_str(item, "security_test", where),
        public_execute=_bool(item, "public_execute", where),
        observed_search_path=_str(item, "observed_search_path", where),
        observed_public_execute=_bool(item, "observed_public_execute", where),
    )


# --------------------------------------------------------------------------- #
# 语义校验（一次报全）
# --------------------------------------------------------------------------- #
def validate(registry: Registry, *, cluster_roles: frozenset[str] | None = None) -> list[str]:
    """返回问题列表；空列表表示通过。

    ``cluster_roles`` 为 ``None`` 时跳过「与 pg_roles 双向对齐」这一条——静态
    闸门无数据库可用；给定集合时（IP-4 / 集成测试）才做集合比对。
    """
    problems: list[str] = []
    problems.extend(_check_domains(registry, cluster_roles))
    problems.extend(_check_resources(registry))
    problems.extend(_check_policies(registry))
    problems.extend(_check_exceptions(registry))
    return problems


def _check_domains(registry: Registry, cluster_roles: frozenset[str] | None) -> list[str]:
    problems: list[str] = []
    ids = [domain.id for domain in registry.domains]
    if len(ids) != EXPECTED_DOMAINS:
        problems.append(
            f"domains 必须恰好 {EXPECTED_DOMAINS} 项（E10.0 固定事实），实际 {len(ids)}：{ids}"
            "——只留示例或多出域都必须报红"
        )
    for duplicated in _duplicates(ids):
        problems.append(f"domains 中 id 重复：{duplicated}")
    for duplicated in _duplicates([domain.schema for domain in registry.domains]):
        problems.append(f"domains 中 schema 重复：{duplicated}（一域一 schema）")
    for duplicated in _duplicates([domain.runtime_role for domain in registry.domains]):
        problems.append(f"多个域映射到同一 runtime_role：{duplicated}（一域一角色）")

    if cluster_roles is not None:
        expected = frozenset(role for role in cluster_roles if role.startswith("svc_"))
        declared = registry.runtime_roles()
        missing = expected - declared
        extra = declared - expected
        if missing:
            problems.append(
                f"集群内存在但 Registry 未声明：{sorted(missing)}"
                "（漏登记会让该域「无声明地」拥有角色）"
            )
        if extra:
            problems.append(
                f"Registry 声明但集群内不存在：{sorted(extra)}"
                "（悬空角色会让授权静默失败，且真空真地「查不到违规」）"
            )
    return problems


def _check_resources(registry: Registry) -> list[str]:
    problems: list[str] = []
    domain_ids = {domain.id for domain in registry.domains}
    seen: set[str] = set()
    for item in registry.resources:
        if item.id in seen:
            problems.append(f"资源 id 重复：{item.id}（资源标识必须全局唯一）")
        seen.add(item.id)
        expected = f"{item.database}.{item.schema}.{item.object_type}.{item.object_name}"
        if item.id != expected:
            problems.append(
                f"资源 id 与字段不符：{item.id} ≠ {expected}"
                "（D.3 四段式；规则改了示例没改正是上一版残留的形态）"
            )
        if item.object_type not in OBJECT_TYPES:
            problems.append(f"{item.id}: object_type={item.object_type!r} 不在枚举内")
        if item.domain not in domain_ids:
            problems.append(f"{item.id}: domain={item.domain!r} 在 domains 中不存在（悬空）")
        if not item.scope_dimensions:
            problems.append(f"{item.id}: 未声明 scope_dimensions（策略无从校验列是否真实存在）")
        if item.rls is None:
            continue
        # FORCE RLS 是必备项：只有 ENABLE 时表 OWNER 仍绕过策略，
        # 而业务函数的目标 OWNER 正是 afs_owner。
        if item.rls.enabled and not item.rls.force:
            problems.append(
                f"{item.id}: rls.enabled=true 但 force=false"
                "（表 OWNER 会绕过策略，FORCE 是目标态的硬要求）"
            )
        if item.rls.policies and not item.rls.enabled:
            problems.append(f"{item.id}: 声明了策略却未启用 RLS（策略永不生效）")
    return problems


def _check_policies(registry: Registry) -> list[str]:
    problems: list[str] = []
    runtime_roles = registry.runtime_roles()
    exception_objects = {item.object_id for item in registry.exceptions}
    stems = _policy_name_stems(registry)

    for resource, policy in registry.policies():
        where = f"{resource.id} / {policy.id}"
        problems.extend(_check_policy_shape(resource, policy, stems[resource.id], where))
        problems.extend(_check_policy_roles(policy, runtime_roles, where))
        problems.extend(_check_policy_ast(resource, policy, exception_objects, where))

    for resource in registry.resources:
        if resource.rls is None or not resource.rls.policies:
            continue
        # 命名唯一性 + 每命令至多一条 PERMISSIVE / 一条 RESTRICTIVE
        names = [policy.id for policy in resource.rls.policies]
        for duplicated in _duplicates(names):
            problems.append(f"{resource.id}: 策略名重复 {duplicated}（同表内唯一）")
        for command, permissive in _policy_index(resource):
            if permissive > 1:
                problems.append(
                    f"{resource.id}: 命令 {command} 有 {permissive} 条 PERMISSIVE 策略"
                    "（多条之间是 OR，等于放宽而非收紧；命名规则只允许一条 scope）"
                )
        # 启用 RLS 却没有 SELECT permissive ⇒ 任何行都不可见（除 append-only 场景外都是缺陷）
        if not any(
            "SELECT" in policy.commands and policy.permissive for policy in resource.rls.policies
        ):
            problems.append(
                f"{resource.id}: 启用 RLS 但缺少 SELECT PERMISSIVE 策略"
                "（没有 permissive ⇒ 任何行不可见，是零行而不是隔离）"
            )
    return problems


def _policy_name_stems(registry: Registry) -> dict[str, frozenset[str]]:
    """每个资源允许出现在策略名里的表名。

    C.3.2 明确「父表与分区各自是独立命名空间（同名不冲突），但必须分别存在」，
    因此分区的策略沿用父表名是**合法且有意的**：同一个隔离语义在四个物理对象上
    叫同一个名字，核对时才能一眼看出「它们应当一致」。若强行按分区名加后缀，
    反而制造了「四个名字、四份语义」的错觉。
    """
    partitioned_parents = {
        item.schema: item.object_name
        for item in registry.resources
        if item.partitioned and item.object_type == "table"
    }
    stems: dict[str, frozenset[str]] = {}
    for item in registry.resources:
        allowed = {item.object_name}
        if item.object_type == "partition" and item.schema in partitioned_parents:
            allowed.add(partitioned_parents[item.schema])
        stems[item.id] = frozenset(allowed)
    return stems


def _check_policy_shape(
    resource: Resource, policy: Policy, stems: frozenset[str], where: str
) -> list[str]:
    problems: list[str] = []
    if len(policy.commands) != 1:
        problems.append(
            f"{where}: 必须恰好声明一个命令，实际 {list(policy.commands)}"
            "（FOR ALL 只能给一套表达式，无法表达「DELETE 无策略」）"
        )
        return problems
    command = policy.commands[0]
    if command not in COMMANDS:
        problems.append(f"{where}: 命令 {command!r} 非法（SELECT/INSERT/UPDATE/DELETE）")
        return problems

    # 子句存在性（C.3.2）：PG16 对不存在的子句直接语法报错，不能「空着」也不能「都给」。
    expected: dict[str, tuple[bool, bool]] = {
        "SELECT": (True, False),
        "INSERT": (False, True),
        "UPDATE": (True, True),
        "DELETE": (True, False),
    }
    need_using, need_check = expected[command]
    if need_using and policy.using is None:
        problems.append(f"{where}: {command} 需要 USING，实际缺失")
    if not need_using and policy.using is not None:
        problems.append(f"{where}: {command} 不接受 USING（PG16 语法报错）")
    if need_check and policy.with_check is None:
        problems.append(f"{where}: {command} 需要 WITH CHECK，实际缺失")
    if not need_check and policy.with_check is not None:
        problems.append(f"{where}: {command} 不接受 WITH CHECK（PG16 语法报错）")

    kind = "scope" if policy.permissive else "guard"
    expected_names = sorted(f"pol_{stem}_{command.lower()}_{kind}" for stem in stems)
    if policy.id not in expected_names:
        problems.append(
            f"{where}: 命名不符合 C.3.2（pol_<table>_<cmd>_<kind>），期望 {expected_names}"
        )
    matched = _POLICY_NAME_RE.match(policy.id)
    if matched and matched.group("table") not in stems:
        problems.append(
            f"{where}: 策略名里的表名 {matched.group('table')!r} 既不是本资源名 "
            f"也不是其父表名 {sorted(stems)}"
        )
    return problems


def _check_policy_roles(policy: Policy, runtime_roles: frozenset[str], where: str) -> list[str]:
    problems: list[str] = []
    if not policy.roles:
        problems.append(f"{where}: 未声明 TO 角色（PG 会给 PUBLIC，等于全租户可见）")
    for role in policy.roles:
        if role not in runtime_roles:
            problems.append(f"{where}: 角色 {role!r} 不是已声明的域运行时角色")
    for duplicated in _duplicates(list(policy.roles)):
        problems.append(f"{where}: 角色 {duplicated} 重复声明")
    return problems


def _check_policy_ast(
    resource: Resource,
    policy: Policy,
    exception_objects: set[str],
    where: str,
) -> list[str]:
    problems: list[str] = []
    for clause, node in (("using", policy.using), ("with_check", policy.with_check)):
        if node is None:
            continue
        try:
            compile_sql(node, resource.scope_dimensions)
        except ExpressionError as exc:
            problems.append(f"{where}.{clause}: {exc}")
            continue
        if resource.id not in exception_objects and _contains_literal(node):
            problems.append(
                f"{where}.{clause}: 使用了 literal 值源"
                "（D.2：常量只允许在例外策略中使用，否则隔离边界由文件内容决定）"
            )
    return problems


def _check_exceptions(registry: Registry) -> list[str]:
    problems: list[str] = []
    runtime_roles = registry.runtime_roles()
    debt_ids = set(registry.debt_ids)
    for item in registry.exceptions:
        where = f"exceptions[{item.object_id}]"
        if item.kind != "SECURITY_DEFINER":
            problems.append(f"{where}: 本轮只支持 SECURITY_DEFINER 例外，实际 {item.kind!r}")
        if item.effective_owner != OWNER_ROLE:
            problems.append(
                f"{where}: effective_owner 必须是 {OWNER_ROLE}，实际 {item.effective_owner!r}"
                "（超级用户 OWNER 即使 FORCE RLS 也不受约束，等于作废安全承诺）"
            )
        if item.search_path != SECURE_SEARCH_PATH:
            problems.append(
                f"{where}: search_path 必须是 {SECURE_SEARCH_PATH!r}"
                "（pg_catalog 首位、pg_temp 显式末位；只写 pg_catalog 挡不住临时表劫持）"
            )
        if item.public_execute:
            problems.append(f"{where}: public_execute 必须为 false（目标态）")
        if not item.execute_roles:
            problems.append(f"{where}: 未声明 execute_roles（不得默认对 PUBLIC 开放）")
        for role in item.execute_roles:
            if role not in runtime_roles:
                problems.append(f"{where}: execute_roles 中的 {role!r} 不是已声明的域角色")
        segments = item.object_id.split(".")
        if len(segments) != 4 or not segments[3].endswith("()"):
            problems.append(
                f"{where}: 函数标识必须是四段式且带参数签名（含无参的 ()），"
                f"实际 {item.object_id!r}——否则无法区分重载"
            )
        if not debt_ids:
            problems.append(f"{where}: 没有 debt 段时不得登记例外（例外必须有到期与解除条件）")
    return problems


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _policy_index(resource: Resource) -> list[tuple[str, int]]:
    """每个命令的 PERMISSIVE 条数（用于「至多一条」判定）。"""
    counts: dict[str, int] = {}
    if resource.rls is None:
        return []
    for policy in resource.rls.policies:
        for command in policy.commands:
            if policy.permissive:
                counts[command] = counts.get(command, 0) + 1
    return list(counts.items())


def _contains_literal(node: Node) -> bool:
    match node:
        case AllOf(children=children) | AnyOf(children=children):
            return any(_contains_literal(child) for child in children)
        case NotExpr(child=child):
            return _contains_literal(child)
        case IsNull():
            return False
        case Eq(value=value) | InArray(value=value):
            return isinstance(value, Literal)
        case _:
            # 结构上不可达（Node 是闭合联合）；保留分支使「新增节点忘了处理」
            # 在类型检查阶段暴露，而不是让 literal 检查静默漏过。
            return False


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicated: list[str] = []
    for value in values:
        if value in seen and value not in duplicated:
            duplicated.append(value)
        seen.add(value)
    return duplicated


def _mapping(raw: object, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RegistryError(f"{where}: 期望映射，实际 {type(raw).__name__}")
    return raw


def _reject_extra(mapping: Mapping[str, Any], allowed: set[str], where: str) -> None:
    extra = set(mapping) - allowed
    if extra:
        raise RegistryError(
            f"{where}: 不认识的键 {sorted(extra)}（合法：{sorted(allowed)}）"
            "——拼错的键会让约束静默失效"
        )


def _list(raw: Mapping[str, Any], key: str) -> list[object]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise RegistryError(f"{key}: 需要非空列表")
    return list(value)


def _opt_list(raw: Mapping[str, Any], key: str) -> list[object]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise RegistryError(f"{key}: 需要列表")
    return list(value)


def _str(raw: Mapping[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{where}.{key}: 需要非空字符串")
    return value


def _str_tuple(raw: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise RegistryError(f"{where}.{key}: 需要非空字符串列表")
    if not all(isinstance(item, str) and item for item in value):
        raise RegistryError(f"{where}.{key}: 列表元素必须是非空字符串")
    return tuple(value)


def _bool(raw: Mapping[str, Any], key: str, where: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise RegistryError(f"{where}.{key}: 需要布尔值")
    return value


def _opt_bool(raw: Mapping[str, Any], key: str, where: str, *, default: bool) -> bool:
    if key not in raw:
        return default
    return _bool(raw, key, where)


__all__ = [
    "COMMANDS",
    "EXPECTED_DOMAINS",
    "LEGAL_OPERATIONS",
    "OBJECT_TYPES",
    "OWNER_ROLE",
    "SECURE_SEARCH_PATH",
    "Domain",
    "InfraRole",
    "PermissionsSource",
    "Policy",
    "Registry",
    "RegistryError",
    "Resource",
    "Rls",
    "SecdefException",
    "load_registry",
    "validate",
]
