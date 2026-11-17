"""create outbox_relay_role and grant outbox minimal access (F03)

根因（F03 数据所有权：进程边界隔离，非简单多账号）：
- Relay（workers/relay）只消费 outbox.outbox_events 并回写投递状态
  （published_at），不读写任何业务 schema（project_planning / asset_service）。
- 但此前 Relay 与 API 共用单一 app_role，而 app_role 持有多个业务 schema 的
  写权限——即使 Relay 代码只操作 outbox，数据库层也无法阻止「Relay 越界
  写业务表」（一旦 Relay 代码被攻破或误改，就能直写 project_planning /
  asset_service 的数据）。
- 根治：为 Relay 建独立角色 outbox_relay_role（LOGIN、NOSUPERUSER、NOBYPASSRLS），
  仅授 outbox schema 的 SELECT + UPDATE（published_at 回写）；不授任何业务
  schema 的 USAGE，使越界访问在数据库层被拒绝。

角色设计：
- outbox_relay_role：NOBYPASSRLS（不绕过 RLS）、LOGIN、NOCREATEDB/
  NOCREATEROLE/NOSUPERUSER（最小权限）。
- 授权范围：仅 outbox schema 的 USAGE + outbox_events 的 SELECT/UPDATE +
  outbox 序列 USAGE（最小权限，不授 DDL、不授 INSERT/DELETE）。
- 读侧还需 list_active_tenants() 函数（outbox schema 内的函数，EXECUTE 权限）。

密码：outbox_relay_role 的密码只走 settings（POSTGRES_RELAY_PASSWORD，生产由
密钥管理服务注入），迁移内不保留明文默认值；缺失即拒绝迁移。已存在角色用
同一口令 ALTER ROLE（幂等），避免开发口令长期留存。

Revision ID: 7a8b9c0d1e2f
Revises: 9f1a2b3c4d5e6
Create Date: 2026-11-17
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a8b9c0d1e2f"
down_revision: str | Sequence[str] | None = "9f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RELAY_ROLE = "outbox_relay_role"
OUTBOX_SCHEMA = "outbox"


def _relay_password() -> str:
    """relay 角色口令的唯一来源：settings（环境变量 / 密钥管理）。

    口令不出现在版本库中；缺失直接失败，避免以空口令或开发口令建角色。
    单引号按 SQL 字面量规则转义（``'`` → ``''``）。
    """
    from packages.shared.config import get_settings

    password = get_settings().postgres_relay_password.get_secret_value()
    if not password:
        raise RuntimeError(
            "POSTGRES_RELAY_PASSWORD 未配置：拒绝以空口令创建 relay 角色"
        )
    return password.replace("'", "''")


def upgrade() -> None:
    """创建 outbox_relay_role 并授予 outbox 最小权限（进程边界隔离）。"""
    password = _relay_password()

    # 1. 创建 relay 专用角色：LOGIN、非超级用户、NOBYPASSRLS（不绕过 RLS）。
    #    已存在时同步口令，避免历史开发口令留在角色上。
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RELAY_ROLE}') THEN
                CREATE ROLE {RELAY_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOBYPASSRLS PASSWORD '{password}';
            ELSE
                ALTER ROLE {RELAY_ROLE} WITH PASSWORD '{password}';
            END IF;
        END
        $$
        """
    )

    # 2. 授权 outbox schema USAGE（仅此一个 schema，不授任何业务 schema）。
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {RELAY_ROLE}")

    # 3. outbox_events 表：SELECT（领取）+ UPDATE（回写 published_at）。
    #    不授 INSERT（事件只能由业务域同事务写入，relay 不得伪造事件）、
    #    不授 DELETE（append-only 审计）。
    op.execute(
        f"GRANT SELECT, UPDATE ON {OUTBOX_SCHEMA}.outbox_events TO {RELAY_ROLE}"
    )

    # 4. 序列权限（最小权限；relay 不插入新行，通常无需序列，但保留 USAGE
    #    以兼容未来可能的事件表结构演进）。
    op.execute(
        f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {OUTBOX_SCHEMA} TO {RELAY_ROLE}"
    )

    # 5. list_active_tenants() 函数执行权限（relay 领取前的租户枚举依赖它）。
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() "
        f"TO {RELAY_ROLE}"
    )


def downgrade() -> None:
    """回滚：回收权限 + 删除 relay 角色。"""
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() "
        f"FROM {RELAY_ROLE}"
    )
    op.execute(
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {OUTBOX_SCHEMA} FROM {RELAY_ROLE}"
    )
    op.execute(
        f"REVOKE ALL ON {OUTBOX_SCHEMA}.outbox_events FROM {RELAY_ROLE}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA {OUTBOX_SCHEMA} FROM {RELAY_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {RELAY_ROLE}")
