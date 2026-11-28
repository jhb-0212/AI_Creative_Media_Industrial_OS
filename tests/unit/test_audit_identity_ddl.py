"""身份 DDL 盘点的判据用例。

重点不是「函数能跑」，而是三条口径不被改坏：
1. 包在 ``DO $$ ... THEN <DDL>`` 与 ``EXECUTE format('<DDL>')`` 里的语句必须被数到
   （历史迁移大量这么写，按首词匹配会数漏）；
2. 一个片段可以同时命中多个类别（``REVOKE`` 与 ``GRANT`` 挤在一起）；
3. 基线文件必须与当前迁移链一致——否则 ``--check`` 的比较对象就是过期账本。
"""

from __future__ import annotations

import json

from scripts.audit_identity_ddl import (
    _BASELINE_PATH,
    _totals,
    audit_tree,
    classify_sql,
)


def test_literal_statements_are_classified() -> None:
    counts = classify_sql(
        """
        CREATE ROLE app_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
        ALTER ROLE app_role WITH LOGIN NOSUPERUSER PASSWORD 'x';
        GRANT USAGE ON SCHEMA project_planning TO app_role;
        REVOKE ALL ON outbox_events FROM app_role;
        ALTER DEFAULT PRIVILEGES FOR ROLE object_owner GRANT SELECT ON TABLES TO app_role;
        ALTER TABLE project_planning.projects OWNER TO object_owner;
        """
    )
    assert counts["role_create"] == 1
    assert counts["role_alter"] == 1
    assert counts["grant"] == 1
    assert counts["revoke"] == 1
    assert counts["default_privs"] == 1
    assert counts["owner"] == 1


def test_ddl_inside_do_block_is_counted() -> None:
    """``DO $$ BEGIN IF NOT EXISTS ... THEN CREATE ROLE`` 是链里最常见的写法。"""
    counts = classify_sql(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'x') THEN
                CREATE ROLE x LOGIN NOSUPERUSER;
            ELSE
                ALTER ROLE x WITH LOGIN NOSUPERUSER;
            END IF;
        END
        $$;
        """
    )
    assert counts["role_create"] == 1
    assert counts["role_alter"] == 1


def test_ddl_inside_execute_format_is_counted() -> None:
    counts = classify_sql(
        "EXECUTE format('GRANT %I TO %I', 'SELECT', 'app_role');"
    )
    assert counts["grant"] == 1


def test_policy_statements_are_counted_separately() -> None:
    counts = classify_sql(
        """
        CREATE POLICY tenant_isolation_qc_tasks ON qc_governance.qc_tasks
            USING (tenant_id = current_setting('app.tenant_id')::uuid);
        """
    )
    assert counts["policy"] == 1
    assert counts["grant"] == 0


def test_role_drop_is_counted_separately() -> None:
    """销毁角色必须单独计数：缺这一类时基线对最危险的身份动作失明（D4-10）。"""
    counts = classify_sql("DROP ROLE qc_worker;")
    assert counts["role_drop"] == 1
    assert counts["role_create"] == 0


def test_dynamic_drop_role_inside_execute_format_is_counted() -> None:
    """``EXECUTE format('DROP ROLE %I', 'qc_worker')`` 是历史上真实的写法。

    alembic 的 ``as_sql`` 渲染会把 ``%I`` 变成 ``%%I``，两种形态都必须数到。
    """
    for sql in (
        "EXECUTE format('DROP ROLE %I', 'qc_worker');",
        "EXECUTE format('DROP ROLE %%I', 'qc_worker');",
    ):
        counts = classify_sql(sql)
        assert counts["role_drop"] == 1, sql


def test_drop_role_inside_conditional_do_block_is_counted() -> None:
    """条件分支里的 DROP ROLE 也不能漏（f2b3 的历史写法就是 DO + pg_shdepend 守卫）。"""
    counts = classify_sql(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'qc_worker') THEN
                RETURN;
            END IF;
            EXECUTE format('DROP ROLE %I', 'qc_worker');
        END $$;
        """
    )
    assert counts["role_drop"] == 1


def test_grant_inside_policy_body_is_not_double_counted_as_statement() -> None:
    """策略表达式里不会出现 GRANT；出现时也只算策略，不该污染授权计数。"""
    counts = classify_sql(
        "CREATE POLICY p ON t USING (true);"
    )
    assert counts["grant"] == 0


def test_baseline_matches_current_migration_tree() -> None:
    baseline: dict[str, int] = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    assert _totals(audit_tree()) == baseline


def test_known_offender_is_detected() -> None:
    """``8f3b2c1d4e5a`` 是拆域角色那次，必须仍在盘点范围内且是最大单一来源。"""
    audits = {item.filename: item for item in audit_tree()}
    assert "8f3b2c1d4e5a_split_domain_roles.py" in audits
    assert audits["8f3b2c1d4e5a_split_domain_roles.py"].identity_actions >= 40
