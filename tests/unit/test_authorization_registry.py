"""授权 Registry：AST 编译、策略派生与语义校验的回归测试。

这些用例锁的是**判据本身**，不是「文件长得对不对」：
- 派生出来的 SQL 是否只用了原生数组解析通道（E11.4：旧写法 0 行、新写法 684 行）；
- POLICY 子句是否按 PG16 规则与命令一一对应（错一个就是语法报错或假通过）；
- 域/角色/列这些「看起来显然」的约束是否真的会让闸门报红。

若某个用例只能在当前数据上通过、换个域就失效，那它不是判据，是快照。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from packages.shared.authorization.ast import ExpressionError, compile_sql, parse
from packages.shared.authorization.ddl import (
    DdlError,
    create_policy_sql,
    function_hardening_sql,
)
from packages.shared.authorization.registry import (
    Registry,
    RegistryError,
    load_registry,
    validate,
)

REGISTRY_PATH = Path("contracts/authorization/authorization_registry.yaml")
ASSET_RESOURCE = "industrial.asset_service.table.asset_versions"


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry(REGISTRY_PATH)


def _policy_named(registry: Registry, resource_id: str, policy_id: str):
    resource = registry.resource(resource_id)
    assert resource.rls is not None
    for policy in resource.rls.policies:
        if policy.id == policy_id:
            return resource, policy
    raise AssertionError(f"{resource_id} 缺少策略 {policy_id}")


def _with_policy(registry: Registry, resource_id: str, policy_id: str, **changes) -> Registry:
    """返回一个把指定策略替换为修改版的 Registry（用于负向用例）。"""
    target = registry.resource(resource_id)
    assert target.rls is not None
    policies = tuple(
        replace(policy, **changes) if policy.id == policy_id else policy
        for policy in target.rls.policies
    )
    updated = replace(target, rls=replace(target.rls, policies=policies))
    return replace(
        registry,
        resources=tuple(updated if item.id == resource_id else item for item in registry.resources),
    )


# --------------------------------------------------------------------------- #
# 正向：真实文件必须自洽
# --------------------------------------------------------------------------- #
def test_registry_passes_semantic_validation(registry: Registry) -> None:
    assert validate(registry) == []


def test_registry_declares_exactly_eight_domains(registry: Registry) -> None:
    assert len(registry.domains) == 8
    assert len(registry.runtime_roles()) == 8


def test_every_declared_policy_derives_executable_sql(registry: Registry) -> None:
    """每条策略都能派生 SQL，且子句与命令严格对应（C.3.2 的子句存在性表）。"""
    compiled = 0
    for resource, policy in registry.policies():
        sql = create_policy_sql(resource, policy)
        assert sql.startswith(f"CREATE POLICY {policy.id} ON {resource.qualified()} ")
        command = policy.commands[0]
        if command == "SELECT":
            assert " USING (" in sql
            assert "WITH CHECK" not in sql, "SELECT 带 WITH CHECK 在 PG16 是语法错误"
        elif command == "INSERT":
            assert "WITH CHECK (" in sql
            assert " USING (" not in sql, "INSERT 带 USING 在 PG16 是语法错误"
        elif command == "UPDATE":
            assert " USING (" in sql
            assert "WITH CHECK (" in sql
        compiled += 1
    assert compiled == 16


# --------------------------------------------------------------------------- #
# 正向：解析通道与类型（E11.4 回归）
# --------------------------------------------------------------------------- #
def test_project_predicate_uses_native_array_input(registry: Registry) -> None:
    """项目集合必须走 ``::text[]``；旧写法会静默返回 0 行。"""
    resource, policy = _policy_named(registry, ASSET_RESOURCE, "pol_asset_versions_select_scope")
    assert policy.using is not None
    sql = compile_sql(policy.using, resource.scope_dimensions)
    assert "current_setting('app.current_projects', true)::text[]" in sql
    assert "string_to_array" not in sql, "按逗号切分数组文本会残留花括号（E11.1）"
    assert "project_id::uuid" not in sql, "varchar 列上的 ::uuid 在 684 行上直接 22P02（E11.2）"


def test_tenant_predicate_casts_to_column_type(registry: Registry) -> None:
    resource, policy = _policy_named(registry, ASSET_RESOURCE, "pol_asset_versions_select_scope")
    assert policy.using is not None
    sql = compile_sql(policy.using, resource.scope_dimensions)
    assert "current_setting('app.current_tenant_id', true)::uuid" in sql


def test_update_policy_carries_both_clauses(registry: Registry) -> None:
    """UPDATE 必须 USING+WITH CHECK：只有 USING 时可以把行改出 scope。"""
    resource, policy = _policy_named(registry, ASSET_RESOURCE, "pol_asset_versions_update_scope")
    sql = create_policy_sql(resource, policy)
    assert sql.count("tenant_id = current_setting('app.current_tenant_id', true)::uuid") == 2


def test_partition_policies_reuse_parent_name(registry: Registry) -> None:
    """C.3.2：父表与分区是独立命名空间，同名不冲突且**必须**分别存在。"""
    parent, _ = _policy_named(
        registry, "industrial.outbox.table.outbox_events", "pol_outbox_events_select_scope"
    )
    partition, _ = _policy_named(
        registry,
        "industrial.outbox.partition.outbox_events_y202611",
        "pol_outbox_events_select_scope",
    )
    assert parent.qualified() != partition.qualified()
    assert validate(registry) == []


# --------------------------------------------------------------------------- #
# 负向：AST 只接受闭合节点集合
# --------------------------------------------------------------------------- #
def test_unknown_node_is_rejected() -> None:
    with pytest.raises(ExpressionError, match="未知节点"):
        parse({"tenant_id_like": {"column": "tenant_id"}}, where="t")


def test_node_must_have_single_key() -> None:
    with pytest.raises(ExpressionError, match="恰好有一个键"):
        parse({"all": [], "any": []}, where="t")


def test_guc_array_cast_parameter_is_rejected() -> None:
    """v1.4.1 的 ``cast: text`` 会诱导出非数组输入的切分实现，出现即报错。"""
    with pytest.raises(ExpressionError, match="cast"):
        parse(
            {
                "in": {
                    "column": "project_id",
                    "value": {"guc_array": "app.current_projects", "cast": "text"},
                }
            },
            where="t",
        )


def test_array_element_type_must_match_column_family() -> None:
    node = parse(
        {
            "in": {
                "column": "project_id",
                "value": {"guc_array": "app.current_projects", "element_type": "uuid"},
            }
        },
        where="t",
    )
    with pytest.raises(ExpressionError, match="不可比较"):
        compile_sql(node, {"project_id": "text"})


def test_column_outside_scope_dimensions_is_rejected() -> None:
    node = parse(
        {"eq": {"column": "organization_id", "value": {"guc": "app.current_organization_id"}}},
        where="t",
    )
    with pytest.raises(ExpressionError, match="scope_dimensions"):
        compile_sql(node, {"tenant_id": "uuid", "project_id": "text"})


def test_array_source_in_eq_is_rejected() -> None:
    node = parse(
        {
            "eq": {
                "column": "project_id",
                "value": {"guc_array": "app.current_projects", "element_type": "text"},
            }
        },
        where="t",
    )
    with pytest.raises(ExpressionError, match="数组上下文"):
        compile_sql(node, {"project_id": "text"})


def test_literal_array_source_in_in_is_rejected() -> None:
    node = parse({"in": {"column": "project_id", "value": {"literal": "project-1"}}}, where="t")
    with pytest.raises(ExpressionError, match="literal"):
        compile_sql(node, {"project_id": "text"})


# --------------------------------------------------------------------------- #
# 负向：语义校验必须报红（而不是「看起来没问题」）
# --------------------------------------------------------------------------- #
def test_domain_count_shortfall_is_red(registry: Registry) -> None:
    problems = validate(replace(registry, domains=registry.domains[:7]))
    assert any("必须恰好 8 项" in problem for problem in problems)


def test_cluster_role_drift_is_bidirectional(registry: Registry) -> None:
    declared = registry.runtime_roles()
    # 集群多出一个 Registry 没声明的角色（漏登记）
    missing = validate(registry, cluster_roles=declared | {"svc_ghost"})
    assert any("集群内存在但 Registry 未声明" in problem for problem in missing)

    # Registry 声明的角色在集群里不存在（悬空，会让授权静默失败）
    extra = validate(registry, cluster_roles=frozenset({"svc_asset"}))
    assert any("Registry 声明但集群内不存在" in problem for problem in extra)

    # 非 svc_ 角色（过渡角色）不参与比对，不得因此报红
    assert validate(registry, cluster_roles=declared | {"app_role"}) == []


def test_select_policy_with_check_clause_is_red(registry: Registry) -> None:
    node = parse(
        {"eq": {"column": "tenant_id", "value": {"guc": "app.current_tenant_id"}}}, where="t"
    )
    broken = _with_policy(
        registry, ASSET_RESOURCE, "pol_asset_versions_select_scope", with_check=node
    )
    problems = validate(broken)
    assert any("SELECT 不接受 WITH CHECK" in problem for problem in problems)


def test_delete_policy_without_using_is_red(registry: Registry) -> None:
    """DELETE 只接受 USING；缺了它既不能建策略，也不能靠 WITH CHECK 代偿。"""
    resource, policy = _policy_named(registry, ASSET_RESOURCE, "pol_asset_versions_select_scope")
    delete_policy = replace(
        policy,
        id="pol_asset_versions_delete_scope",
        commands=("DELETE",),
        using=None,
        with_check=None,
    )
    with pytest.raises(DdlError, match="需要 USING"):
        create_policy_sql(resource, delete_policy)


def test_policy_name_must_follow_command_and_kind(registry: Registry) -> None:
    broken = _with_policy(
        registry,
        ASSET_RESOURCE,
        "pol_asset_versions_select_scope",
        id="tenant_isolation_asset_versions",
    )
    problems = validate(broken)
    assert any("命名不符合 C.3.2" in problem for problem in problems)


def test_policy_role_must_be_declared_domain_role(registry: Registry) -> None:
    broken = _with_policy(
        registry, ASSET_RESOURCE, "pol_asset_versions_select_scope", roles=("public",)
    )
    problems = validate(broken)
    assert any("不是已声明的域运行时角色" in problem for problem in problems)


def test_force_rls_is_mandatory(registry: Registry) -> None:
    target = registry.resource(ASSET_RESOURCE)
    assert target.rls is not None
    broken_resource = replace(target, rls=replace(target.rls, force=False))
    problems = validate(
        replace(
            registry,
            resources=tuple(
                broken_resource if item.id == ASSET_RESOURCE else item
                for item in registry.resources
            ),
        )
    )
    assert any("force=false" in problem for problem in problems)


def test_secdef_owner_must_be_afs_owner(registry: Registry) -> None:
    broken = replace(
        registry,
        exceptions=(replace(registry.exceptions[0], effective_owner="postgres"),),
    )
    problems = validate(broken)
    assert any("effective_owner 必须是 afs_owner" in problem for problem in problems)


def test_secdef_search_path_must_pin_pg_temp_last(registry: Registry) -> None:
    broken = replace(
        registry,
        exceptions=(replace(registry.exceptions[0], search_path="pg_catalog"),),
    )
    problems = validate(broken)
    assert any("search_path 必须是" in problem for problem in problems)


# --------------------------------------------------------------------------- #
# 负向：形状层拒绝（双事实源的两个入口）
# --------------------------------------------------------------------------- #
_BASE_YAML = """
schema_version: "1.2"
status: target
databases:
  industrial: { physical_name: ai_film_industrial, kind: business }
domains:
  - { id: d, schema: s, runtime_role: svc_d, legacy_role: null }
roles:
  infrastructure_roles:
    object_owner: { name: afs_owner, login: false }
resources:
  - id: industrial.s.table.t
    database: industrial
    schema: s
    object_type: table
    object_name: t
    domain: d
    scope_dimensions: { tenant_id: uuid }
    rls: { enabled: true, force: true, policies: [] }
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_roles_mappings_cannot_come_back(tmp_path: Path) -> None:
    """D.2.1：域→角色映射的唯一权威是 domains[].runtime_role。"""
    text = _BASE_YAML.replace(
        "    object_owner: { name: afs_owner, login: false }",
        "    object_owner: { name: afs_owner, login: false }\n  mappings: []",
    )
    with pytest.raises(RegistryError, match="mappings"):
        load_registry(_write(tmp_path, text))


def test_permissions_and_source_cannot_coexist(tmp_path: Path) -> None:
    text = (
        _BASE_YAML
        + """
permissions_source:
  module: packages/shared/db_grants.py
  covers: [asset_service]
  phase: expand
  authority: legacy_roles_only
  unblocks_when: "IP-4"
permissions:
  - resource: industrial.s.table.t
    role: svc_d
    operations: [SELECT]
"""
    )
    with pytest.raises(RegistryError, match="不得并存"):
        load_registry(_write(tmp_path, text))


# --------------------------------------------------------------------------- #
# 函数加固：顺序即安全性
# --------------------------------------------------------------------------- #
def test_function_hardening_revokes_public_before_granting(registry: Registry) -> None:
    statements = function_hardening_sql(registry.exceptions[1])
    joined = "\n".join(statements)
    assert "SET search_path = 'pg_catalog, pg_temp'" in joined
    assert "OWNER TO afs_owner" in joined
    revoke_at = joined.index("REVOKE ALL ON FUNCTION")
    grant_at = joined.index("GRANT EXECUTE ON FUNCTION")
    assert revoke_at < grant_at, "先 REVOKE PUBLIC 再 GRANT，否则默认 ACL 仍在（E10.3：5/6 个函数）"
    assert "audit.assert_transition_event_exists()" in joined


def test_function_hardening_without_roles_grants_nothing(registry: Registry) -> None:
    statements = function_hardening_sql(replace(registry.exceptions[0], execute_roles=()))
    assert not any(statement.startswith("GRANT EXECUTE") for statement in statements)
