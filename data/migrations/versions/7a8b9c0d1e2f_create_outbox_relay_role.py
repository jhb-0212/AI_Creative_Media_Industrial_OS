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

from packages.shared.migration_identity import assert_role_present
from packages.shared.partition_guard import RELAY_UPDATE_COLUMNS

# revision identifiers, used by Alembic.
revision: str = "7a8b9c0d1e2f"
down_revision: str | Sequence[str] | None = "9f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTBOX_SCHEMA = "outbox"


def _relay_role() -> str:
    """relay 角色名的唯一来源：settings（与运行时 DSN 构造同源）。

    审查 R6：角色名可配置（``POSTGRES_RELAY_USER``），但迁移曾硬编码
    ``outbox_relay_role``。两处事实源一旦不一致（部署时改了用户名），
    迁移创建的角色与进程实际使用的角色就不是同一个——进程连不上，或更糟：
    连上的是一个没有预期授权的同名残留角色。角色名必须与配置同源。
    """
    from packages.shared.config import get_settings

    return get_settings().postgres_relay_user


def _relay_password() -> str:
    """relay 角色口令的唯一来源：settings（环境变量 / 密钥管理）。

    口令不出现在版本库中；缺失直接失败，避免以空口令或开发口令建角色。
    单引号按 SQL 字面量规则转义（``'`` → ``''``）。

    迁移经裸 ``get_settings()`` 读配置、不声明进程身份，进程层的生产
    校验不会触发——必须在此显式拒绝开发默认口令（Codex P1：否则生产
    迁移未注入口令时会以仓库里可预测的 relay_dev_password 建角色）。
    """
    from packages.shared.config import (
        get_settings,
        reject_development_default_password,
    )

    settings = get_settings()
    password = settings.postgres_relay_password.get_secret_value()
    if not password:
        raise RuntimeError(
            "POSTGRES_RELAY_PASSWORD 未配置：拒绝以空口令创建 relay 角色"
        )
    reject_development_default_password(
        settings, "postgres_relay_password", "POSTGRES_RELAY_PASSWORD"
    )
    return password.replace("'", "''")


def upgrade() -> None:
    """断言 relay 角色存在（由 bootstrap 创建）并授予 outbox 最小权限。"""
    relay_role = _relay_role()

    # 1. 角色归 bootstrap：本迁移只断言存在，不建角色、不改属性、不读口令。
    #    原写法「IF NOT EXISTS CREATE ROLE ... ELSE ALTER ROLE」有两个问题：
    #    一是把角色属性变成迁移的第二份事实源；二是 downgrade 被迫删角色
    #    （集群级对象，真实多库集群删不掉，降级链直接断，见 P4 根因分析）。
    assert_role_present(relay_role, op=op)

    # 2. 授权 outbox schema USAGE（仅此一个 schema，不授任何业务 schema）。
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {relay_role}")

    # 3. outbox_events 表：SELECT（领取）+ 列级 UPDATE（仅回写投递元数据）。
    #    不授 INSERT（事件只能由业务域同事务写入，relay 不得伪造事件）、
    #    不授 DELETE（append-only 审计）、不授**整表** UPDATE（否则可改
    #    payload/event_type/tenant_id 等业务内容，破坏事件不可篡改——
    #    审查 P1-01）。列级授权在迁移 b9e1c2d3f4a5 进一步收口并覆盖分区；
    #    此处即按列授予，避免新库在 b9e1c2d3f4a5 之前存在越权窗口。
    columns = ", ".join(RELAY_UPDATE_COLUMNS)
    op.execute(f"GRANT SELECT ON {OUTBOX_SCHEMA}.outbox_events TO {relay_role}")
    op.execute(
        f"GRANT UPDATE ({columns}) ON {OUTBOX_SCHEMA}.outbox_events TO {relay_role}"
    )

    # 4. 序列权限（最小权限；relay 不插入新行，通常无需序列，但保留 USAGE
    #    以兼容未来可能的事件表结构演进）。
    op.execute(
        f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {OUTBOX_SCHEMA} TO {relay_role}"
    )

    # 5. list_active_tenants() 函数执行权限（relay 领取前的租户枚举依赖它）。
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() "
        f"TO {relay_role}"
    )


def downgrade() -> None:
    """回滚：只撤销本迁移授出的授权，**不删除 relay 角色**（WP-02）。

    角色是集群级对象，`outbox_relay_role` 在真实集群上被 9 个库共 222 条授权
    依赖（见 P4 根因分析）。迁移只撤得掉自己这一份，原来的 ``DROP ROLE``
    因此在任何多库集群上都会失败，并把整条降级链拖死。
    """
    relay_role = _relay_role()
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() "
        f"FROM {relay_role}"
    )
    op.execute(
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {OUTBOX_SCHEMA} FROM {relay_role}"
    )
    op.execute(
        f"REVOKE ALL ON {OUTBOX_SCHEMA}.outbox_events FROM {relay_role}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {OUTBOX_SCHEMA} "
        f"REVOKE ALL ON TABLES FROM {relay_role}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA {OUTBOX_SCHEMA} FROM {relay_role}")
