"""不变量 SCOPE-4 的断言 ``scope.org_rls_enforced`` 的**负向**测试。

为什么要专门测「断言会不会报红」
------------------------------------------------------------
断言本身也可能是一句空话：判据写成「迁移里出现过 organization 字样」时，
文档字符串就能满足它；判据写成「有没有 CREATE POLICY」时，策略里只有租户谓词
也能满足它。这类断言在 CI 里永远是 PASS，比没有断言更糟——它让人以为守住了。

这里把判定的**取数**与**判据**分开（``evaluate_org_rls`` 收数据，不收仓库根），
于是可以喂构造的迁移语句：先构造一份「合规样本」证明判据不误报，再逐项破坏它，
每项只改一个地方，断言恰好该条报红。

合规样本用 ``organization_scope_predicate()`` 生成，与真实迁移同一来源：
若把谓词手抄进测试，测试就会在「单一事实源改了、迁移没改」时照样通过。
"""

from __future__ import annotations

import pytest

from packages.shared.rls import (
    organization_scope_predicate,
    tenant_scope_predicate,
)
from scripts import boundary_assertions as ba

# 真实迁移里受控的表与组织规则形态（与断言共用同一份登记，避免测试自成一套事实）
_TABLES = ba._ORG_RLS_TABLES
_FORCED = ba._ORG_RLS_FORCED


def _column_clause(schema: str, table: str) -> str:
    """有组织列的表：谓词必须等于单一事实源生成的那一段。"""
    return organization_scope_predicate()


def _parent_clause(schema: str, table: str) -> str:
    """子表：组织规则经父项目存在性传递（不复制组织列）。"""
    return (
        f"EXISTS (SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {schema}.{table}.project_id "
        f"AND p.tenant_id = {schema}.{table}.tenant_id)"
    )


def _clause_for(schema: str, table: str, form: str) -> str:
    """按登记的形态生成该表的组织子句（qc_reports 走两跳链）。"""
    if form == "column":
        return _column_clause(schema, table)
    if table == "qc_reports":
        return (
            "EXISTS (SELECT 1 FROM asset_service.asset_versions a "
            "JOIN project_planning.projects p "
            "ON p.entity_id = a.project_id AND p.tenant_id = a.tenant_id "
            "WHERE a.entity_id = asset_service.qc_reports.asset_version_id "
            "AND a.tenant_id = asset_service.qc_reports.tenant_id)"
        )
    return _parent_clause(schema, table)


def _good_statements() -> list[str]:
    """一份合规的迁移语句集合（全部受控表的策略 + FORCE）。"""
    statements = [
        f"CREATE POLICY tenant_isolation_{table} ON {schema}.{table} "
        f"USING (({tenant_scope_predicate()}) AND "
        f"({_clause_for(schema, table, form)}))"
        for schema, table, form, _correlations in _TABLES
    ]
    statements += [
        f"ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY" for schema, table in _FORCED
    ]
    return statements


def _declared() -> frozenset[tuple[str, str]]:
    """应检的业务表集合（与断言登记的规则形态一致，构成「无问题」的前提）。"""
    return frozenset((schema, table) for schema, table, _, _ in _TABLES)


def _evaluate(
    statements: list[str] | None = None,
    *,
    database_text: str = "verify_rls_enforcement(project)\nverify_rls_enforcement(asset)\n",
    injects_organization: bool = True,
    self_injected_paths: list[str] | None = None,
    tenant_tables: frozenset[tuple[str, str]] | None = None,
) -> list[str]:
    """按真实口径求值：应检集合默认就是登记的全部业务表。

    传 ``tenant_tables`` 而不是留空，是为了让「合规样本必须零问题」也覆盖完备性
    判据——否则完备性只在真实仓库里跑，负向用例证明不了它会报红。
    """
    return ba.evaluate_org_rls(
        _good_statements() if statements is None else statements,
        database_text=database_text,
        injects_organization=injects_organization,
        self_injected_paths=[] if self_injected_paths is None else self_injected_paths,
        tenant_tables=_declared() if tenant_tables is None else tenant_tables,
    )


def _replace_table_policy(
    statements: list[str], schema: str, table: str, replacement: str
) -> list[str]:
    """把某张表的策略换成 ``replacement``（其余不动，保证单变量）。"""
    needle = f" ON {schema}.{table} "
    return [
        replacement if statement.startswith("CREATE POLICY ") and needle in statement else statement
        for statement in statements
    ]


# --------------------------------------------------------------------------- #
# 正样本：判据不能误报
# --------------------------------------------------------------------------- #
def test_good_statements_are_clean() -> None:
    """合规样本无问题项（否则「一切皆红」等于「没有告警」）。"""
    assert _evaluate() == []


def test_real_migration_is_clean() -> None:
    """真实迁移链回放后无问题项——断言不是靠构造样本才成立的。"""
    statements = ba._policy_statements()
    assert statements, "迁移回放没有产出语句：断言会退化成「空集合必过」"
    assert _evaluate(statements) == []


# --------------------------------------------------------------------------- #
# 完备性：应检集合本身必须是派生的，不能是手写清单
# --------------------------------------------------------------------------- #
def test_unregistered_business_table_is_reported() -> None:
    """业务表没登记规则形态 → 判据对它是零求值，必须报红。

    这正是 A13 的失效形态：五张新表建了、矩阵声明了、授权也配了，唯独没人把它们
    加进这份登记。判据于是对它们不闻不问，五张表在库层零策略却报 PASS。
    """
    problems = _evaluate(tenant_tables=_declared() | {("project_planning", "storyboards")})
    assert any("project_planning.storyboards 是业务表但未登记" in p for p in problems), problems


def test_stale_registration_is_reported() -> None:
    """登记了但矩阵里没有这张表 → 判据在验一个不存在的东西。"""
    problems = _evaluate(tenant_tables=_declared() - {("project_planning", "scenes")})
    assert any("project_planning.scenes 登记了" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 终态语义：判据问的是「数据库最后是什么样」
# --------------------------------------------------------------------------- #
def test_later_policy_upgrade_replaces_earlier_one() -> None:
    """先建单维策略、后升级为双维 → 不得因为「第一条缺组织谓词」报红。

    真实链就是这样：``8f2a1b3c4d5e`` 建租户单维策略，``e9f0a1b2c3d4`` 升级为
    「租户 + 组织」。若判据取第一条匹配，每次策略修订都要回来改判据——而改判据
    的人下一步就会把「缺组织谓词」这条检查删掉。
    """
    first = (
        f"CREATE POLICY tenant_isolation_episodes ON project_planning.episodes "
        f"USING ({tenant_scope_predicate()})"
    )
    statements = [first, *_good_statements()]
    assert _evaluate(statements) == []


def _owner_read_policy(schema: str, table: str) -> str:
    """角色限定的额外策略：真实链里 qc_tasks 就多这一条（仅 owner 可读）。"""
    return (
        f"CREATE POLICY maintenance_owner_read_{table} ON {schema}.{table} "
        "TO qc_maintenance_owner FOR SELECT USING (true)"
    )


def test_extra_role_policy_does_not_hide_isolation() -> None:
    """同一张表上加一条「仅 owner 可读」策略 → 隔离策略仍然成立。

    旧判据只取**最后一条** CREATE POLICY，于是这条读策略会把
    ``tenant_isolation_qc_tasks`` 顶掉，闸门报「策略名不对 / 缺租户谓词」。
    PostgreSQL 里 permissive 策略是 OR，多一条读策略不改变隔离策略对其它角色
    的约束——判据算错了，而不是策略坏了。
    """
    statements = [
        *_good_statements(),
        _owner_read_policy("qc_governance", "qc_tasks"),
    ]
    assert _evaluate(statements) == []


def test_policy_order_permutation_keeps_verdict() -> None:
    """策略顺序置换成「额外策略在前、隔离策略在后」→ 结论不变。

    创建顺序不是授权语义的一部分；判据必须对顺序免疫，否则每次修订策略顺序
    都会让闸门翻红，而真正的隔离缺陷反而被噪音淹没。
    """
    good = _good_statements()
    isolation = [
        s for s in good if s.startswith("CREATE POLICY ") and " ON qc_governance.qc_tasks " in s
    ]
    rest = [s for s in good if s not in isolation]
    statements = [
        _owner_read_policy("qc_governance", "qc_tasks"),
        *isolation,
        *rest,
    ]
    assert _evaluate(statements) == []


def test_public_permissive_everything_is_reported() -> None:
    """一条 TO PUBLIC … USING (true) 就把整张表放开：permissive 是 OR，必须报红。"""
    statements = [
        *_good_statements(),
        "CREATE POLICY debug_read_all ON project_planning.episodes USING (true)",
    ]
    problems = _evaluate(statements)
    assert any("对 PUBLIC 完全放行的策略 debug_read_all" in p for p in problems), problems


def test_dropped_policy_is_treated_as_missing() -> None:
    """先建后删 → 终态没有策略，必须报红（后写覆盖先写，两个方向都要成立）。"""
    statements = [
        *_good_statements(),
        "DROP POLICY IF EXISTS tenant_isolation_episodes ON project_planning.episodes",
    ]
    assert any("episodes 缺少策略" in p for p in _evaluate(statements))


def test_prefix_table_name_does_not_satisfy_lookup() -> None:
    """``scenes_archive`` 的策略不得被当成 ``scenes`` 的。

    定位点按 ``ON <schema>.<table>`` 匹配时必须看边界：只做子串匹配，表名互为
    前缀的两张表会互相顶替，一个「有策略」的假象就足以让判据沉默。
    """
    statements = [
        statement
        for statement in _good_statements()
        if " ON project_planning.scenes " not in statement
    ] + [
        f"CREATE POLICY tenant_isolation_scenes ON project_planning.scenes_archive "
        f"USING (({tenant_scope_predicate()}) AND "
        f"({_parent_clause('project_planning', 'scenes_archive')}))"
    ]
    assert any("project_planning.scenes 缺少策略" in p for p in _evaluate(statements))


def test_policy_lookup_is_not_confused_by_parent_reference() -> None:
    """子表策略里出现的 ``project_planning.projects`` 不得被当成 projects 的策略。

    这是判据实现层的坑：按 ``.projects `` 子串找策略会把 episodes 的策略认成
    projects 的。这里删掉 projects 自己的策略、只留子表的，要求报的正是
    「projects 缺少策略」而不是「projects 谓词不一致」。
    """
    statements = [
        statement
        for statement in _good_statements()
        if " ON project_planning.projects " not in statement
    ]
    problems = _evaluate(statements)
    assert any("project_planning.projects 缺少策略" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 负样本：每一格破坏都必须被点名
# --------------------------------------------------------------------------- #
def test_missing_policy_is_reported() -> None:
    """整张表没有策略：组织边界对它是零。"""
    statements = [
        statement
        for statement in _good_statements()
        if " ON project_planning.episodes " not in statement
    ]
    assert any("episodes 缺少策略" in p for p in _evaluate(statements))


def test_hand_copied_predicate_without_null_branch_is_reported() -> None:
    """手抄谓词漏掉 ``IS NULL`` 分支：个人租户的行会整片消失。

    这正是「抄一份副本」的典型后果——副本看起来「也是组织谓词」，但真值表
    已经和应用层不一致，而两层各自都能自圆其说。
    """
    statements = _replace_table_policy(
        _good_statements(),
        "project_planning",
        "projects",
        "CREATE POLICY tenant_isolation_projects ON project_planning.projects "
        f"USING (({tenant_scope_predicate()}) AND (organization_id = "
        "NULLIF(current_setting('app.current_organization_id', true), '')::uuid))",
    )
    problems = _evaluate(statements)
    assert any("organization_scope_predicate() 不一致" in p for p in problems), problems


def test_sub_table_copying_organization_column_is_reported() -> None:
    """子表直接引用 organization_id（自己造一份组织规则）必须报红。"""
    statements = _replace_table_policy(
        _good_statements(),
        "project_planning",
        "episodes",
        "CREATE POLICY tenant_isolation_episodes ON project_planning.episodes "
        f"USING (({tenant_scope_predicate()}) AND "
        f"({organization_scope_predicate()}))",
    )
    problems = _evaluate(statements)
    assert any("episodes 未用父项目存在性" in p for p in problems), problems


def test_uncorrelated_exists_is_reported() -> None:
    """EXISTS 存在但**没与本表行关联** → 他组织的子行整片泄漏（评审 F2）。

    这个形态最危险：``EXISTS (SELECT 1 FROM project_planning.projects p)`` 只要
    租户里有任意一个可见项目就为真，于是任意子行都可见。而只查
    「有没有 EXISTS / 有没有 project_planning.projects」的判据会照样报 PASS。
    """
    statements = _replace_table_policy(
        _good_statements(),
        "project_planning",
        "episodes",
        "CREATE POLICY tenant_isolation_episodes ON project_planning.episodes "
        f"USING (({tenant_scope_predicate()}) AND "
        "(EXISTS (SELECT 1 FROM project_planning.projects p)))",
    )
    problems = _evaluate(statements)
    assert any("未与自身行关联" in p for p in problems), problems


def test_half_correlated_exists_is_reported() -> None:
    """只关联了 project_id、漏了 tenant_id：跨租户父子行可互相满足。

    复合外键保证 (project_id, tenant_id) 一致，但策略不能依赖「另一个约束会兜住」——
    它是在 RLS 求值、比外键更早，且不为其他约束的存废负责。
    """
    statements = _replace_table_policy(
        _good_statements(),
        "asset_service",
        "asset_versions",
        "CREATE POLICY tenant_isolation_asset_versions "
        "ON asset_service.asset_versions "
        f"USING (({tenant_scope_predicate()}) AND "
        "(EXISTS (SELECT 1 FROM project_planning.projects p "
        "WHERE p.entity_id = asset_service.asset_versions.project_id)))",
    )
    problems = _evaluate(statements)
    assert any("p.tenant_id = asset_service.asset_versions.tenant_id" in p for p in problems), (
        problems
    )


def test_two_hop_chain_missing_link_is_reported() -> None:
    """qc_reports 两跳链丢了第二跳的关联：任一跳丢条件即整表放行。"""
    statements = _replace_table_policy(
        _good_statements(),
        "asset_service",
        "qc_reports",
        "CREATE POLICY tenant_isolation_qc_reports ON asset_service.qc_reports "
        f"USING (({tenant_scope_predicate()}) AND "
        "(EXISTS (SELECT 1 FROM asset_service.asset_versions a "
        "JOIN project_planning.projects p ON p.entity_id = a.project_id "
        "WHERE a.entity_id = asset_service.qc_reports.asset_version_id)))",
    )
    problems = _evaluate(statements)
    assert any("p.tenant_id = a.tenant_id" in p or "a.tenant_id = " in p for p in problems), (
        problems
    )


def test_downgrade_does_not_weaken_upstream_forceless_audit_table() -> None:
    """回滚不得摘掉上游迁移建立的 FORCE（评审 F1）。

    ``audit.status_transitions`` 的 FORCE 由 d8e9f0a1b2c3 建立。若某个后续迁移的
    回滚对它 ``NO FORCE``，回滚后的审计表比回滚前更弱——而正向断言只读 upgrade
    的回放文本，看不到这个退化。

    这里对**整条链**求值而不是盯住某一份迁移：写死路径时，新迁移里同样的回滚退化
    不会被检查（本文件此前正是盯住单份迁移）。同时保留反向控制位：确实存在撤销
    自己 FORCE 的回滚，否则「没有任何 NO FORCE」也可能只是回放没产出语句。
    """
    unforced: set[str] = set()
    for _, rel in ba._ordered_migrations():
        text = (ba.ROOT / rel).read_text(encoding="utf-8")
        if "NO FORCE ROW LEVEL SECURITY" not in text:
            continue
        for statement in ba._rendered_migration_statements(rel, direction="downgrade"):
            if statement.startswith("ALTER TABLE ") and "NO FORCE" in statement:
                unforced.add(
                    statement.removeprefix("ALTER TABLE ")
                    .removesuffix(" NO FORCE ROW LEVEL SECURITY")
                    .strip()
                )
    assert "audit.status_transitions" not in unforced, (
        "回滚把审计表的 FORCE 摘掉了：force 来自上游迁移，后续迁移无权撤销"
    )
    assert unforced, "没有任何回滚撤销 FORCE：这条判据退化成空检查"


def test_policy_without_tenant_predicate_is_reported() -> None:
    """只加组织谓词、丢了租户谓词：跨租户可见（比改之前更糟）。"""
    statements = _replace_table_policy(
        _good_statements(),
        "project_planning",
        "projects",
        "CREATE POLICY tenant_isolation_projects ON project_planning.projects "
        f"USING ({organization_scope_predicate()})",
    )
    assert any("projects 策略缺租户谓词" in p for p in _evaluate(statements))


def test_missing_force_is_reported() -> None:
    """缺 FORCE：所有权转给业务角色后策略静默失效。"""
    statements = [
        statement
        for statement in _good_statements()
        if statement != "ALTER TABLE audit.status_transitions FORCE ROW LEVEL SECURITY"
    ]
    assert any("audit.status_transitions 未 FORCE" in p for p in _evaluate(statements))


def test_business_code_injecting_scope_is_reported() -> None:
    """业务代码自己 SET 作用域：多一个注入点就多一条绕过路径。"""
    problems = _evaluate(self_injected_paths=["services/x/service/y.py"])
    assert any("业务代码自行注入 RLS 作用域" in p for p in problems)


def test_missing_organization_injection_is_reported() -> None:
    """注入点只注租户：策略第二维永远为 NULL，组织行全部不可见（功能崩塌）。"""
    problems = _evaluate(injects_organization=False)
    assert any("未把 principal.organization_id 注入会话" in p for p in problems)


def test_startup_verification_not_wired_is_reported() -> None:
    """只定义检查、不接线：等于没有检查（超级用户连库仍会静默失效）。"""
    problems = _evaluate(database_text="verify_rls_enforcement(project)\n")
    assert any("未对每个业务域引擎验证" in p for p in problems)


@pytest.mark.parametrize(
    "marker", ["set_tenant_context(", "set_organization_context(", "set_session_scope("]
)
def test_all_injection_markers_are_scanned(tmp_path, marker: str) -> None:
    """三个注入函数各写一次，都必须被扫到（漏一个，绕过点就隐形）。"""
    service_dir = tmp_path / "services" / "demo"
    service_dir.mkdir(parents=True)
    (service_dir / "leak.py").write_text(
        f"async def handler(session):\n    await {marker}session, x\n", encoding="utf-8"
    )
    assert ba.find_self_injected_scope_paths(tmp_path) == ["services/demo/leak.py"]


def test_clean_service_file_is_not_flagged(tmp_path) -> None:
    """不注入作用域的文件不得误报（否则唯一注入点也会被算成绕过）。"""
    service_dir = tmp_path / "services" / "demo"
    service_dir.mkdir(parents=True)
    (service_dir / "clean.py").write_text(
        "async def handler(session):\n    return session\n", encoding="utf-8"
    )
    assert ba.find_self_injected_scope_paths(tmp_path) == []


def test_empty_tree_is_reported_by_assertion_scan() -> None:
    """空仓库目录扫描结果为空——正因如此，真实断言依赖「迁移回放有语句」自证非空。"""
    assert ba._policy_statements()
