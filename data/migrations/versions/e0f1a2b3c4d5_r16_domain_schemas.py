"""R16 三域预埋：production_planning / budget_quota / qc_governance 的空 schema + 专用角色

为什么先预埋（而不是等到 cutover ``a0b1c2d3e4f5``）
------------------------------------------------------------
最小权限收敛迁移（``c1a2b3d4e5f6`` 及其后的运行时表迁移）经
``converge_statements`` 为每个域角色生成 ``GRANT USAGE ON SCHEMA`` 与
``ALTER DEFAULT PRIVILEGES IN SCHEMA <domain>``。PG 要求被引用的 schema 与角色
都已存在，clean install 在 cutover 迁移之前就会抛
``InvalidSchemaNameError`` / ``role ... does not exist``。

本迁移只预埋空 schema 与专用角色：建表、索引、RLS 策略与表级授权仍归
``a0b1c2d3e4f5``（时间胶囊职责不变），这里不写任何业务表 GRANT 字面量。
角色口令只从配置上下文取，迁移不写字面量口令。

Revision ID: e0f1a2b3c4d5
Revises: e9f0a1b2c3d4
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.db_grants import (
    BUDGET_QUOTA,
    PRODUCTION_PLANNING,
    QC_GOVERNANCE,
)
from packages.shared.db_roles import domain_role

revision: str = "e0f1a2b3c4d5"
down_revision: str | Sequence[str] | None = "e9f0a1b2c3d4"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SCHEMAS = (PRODUCTION_PLANNING, BUDGET_QUOTA, QC_GOVERNANCE)


def _create_role_if_absent(role: str, password: str) -> None:
    """幂等建角色：已存在则只更新口令（重复执行迁移不报错）。

    PG 没有 ``CREATE ROLE IF NOT EXISTS``（它是 ``CREATE TABLE`` 才有的语法），
    照抄会得到 syntax error，因此用 DO 块做存在性判断。
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    PASSWORD '{password}';
            ELSE
                ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    PASSWORD '{password}';
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    """预埋三个 R16 业务域的空 schema 与专用角色（幂等）。"""
    from packages.shared.config import get_settings

    settings = get_settings()
    role_passwords = {
        PRODUCTION_PLANNING: settings.postgres_production_planning_password.get_secret_value(),
        BUDGET_QUOTA: settings.postgres_budget_quota_password.get_secret_value(),
        QC_GOVERNANCE: settings.postgres_qc_governance_password.get_secret_value(),
    }
    for schema in _SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        _create_role_if_absent(domain_role(schema), role_passwords[schema])


def downgrade() -> None:
    """回滚预埋：仅当 schema 仍为空（后续迁移已先被 downgrade）时删除。

    角色本身保留：可能与其它部署共用，删角色是不可逆的共享状态改动。
    """
    for schema in reversed(_SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} RESTRICT")
