"""Outbox 分区安全护栏：把"分区也必须受 RLS 保护"落到结构层。

为什么需要这个模块（根因）：
- 迁移 ``f6a7b8c9d1e2`` 只对**父表** ``ENABLE ROW LEVEL SECURITY``。分区由维护
  工具或后续迁移动态创建，PG 的父表策略只在「通过父表访问」时生效；一旦有人
  ``SELECT * FROM outbox.outbox_events_y202611``，分区自身的
  ``relrowsecurity=false`` 就等于没有隔离。
- 更隐蔽的一层：``9e3b4c5d6e7f`` 用 ``ALTER DEFAULT PRIVILEGES`` 给新表自动
  授了 SELECT/INSERT/UPDATE（DELETE 已在 ``d4e5f6a7b8c9`` 撤销），所以每个新建
  分区**天生**可被 app_role 直连——授权不是"忘了给"，而是"默认给了"。
- 结论：保护必须在"创建分区"这同一个动作里完成，且只能有一个实现；分散在
  迁移、维护工具、手工 SQL 的三份拷贝，迟早漂移。

设计：
- 本模块只产出 SQL 语句（不执行），迁移与 ``workers/outbox_maintenance`` 共用，
  避免两套口径；
- 语句幂等：重复执行不报错（``DROP POLICY IF EXISTS`` / ``REVOKE`` 重复无害）；
- 标识符做白名单校验后加引号，杜绝拼接分区名带来的注入面。

对应不变量（由 ``tests/integration/test_tenant_isolation_invariants.py`` 断言）：
1. 每个 outbox 分区 ``relrowsecurity = true`` 且有租户策略；
2. app_role 对任何分区**没有**直连权限（只能通过父表访问）；
3. 通过父表、无租户上下文查询返回 0 行（fail-closed）。
"""

from __future__ import annotations

import re

OUTBOX_SCHEMA = "outbox"
OUTBOX_PARENT = "outbox_events"
APP_ROLE = "app_role"
# 与父表策略同名：策略名按表作用域独立，同名便于排障时一眼认出。
PARTITION_POLICY = "tenant_isolation_outbox_events"

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def quote_ident(name: str) -> str:
    """校验并引用标识符（只接受小写字母/数字/下划线，首字符非数字）。"""
    if not _IDENT_RE.fullmatch(name):
        raise ValueError(f"非法标识符（拒绝拼接）: {name!r}")
    return f'"{name}"'


def protect_partition_statements(
    partition: str,
    *,
    schema: str = OUTBOX_SCHEMA,
    role: str = APP_ROLE,
) -> tuple[str, ...]:
    """返回"让某个分区进入受保护状态"所需的全部 SQL（幂等）。

    顺序有意义：先开 RLS 再建策略（策略可以在 RLS 未开时创建，但先开语义清楚），
    最后回收直连授权——保证任何中间失败重跑都能收敛到同一状态。
    """
    qualified = f"{quote_ident(schema)}.{quote_ident(partition)}"
    policy = quote_ident(PARTITION_POLICY)
    return (
        f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {policy} ON {qualified}",
        f"""
        CREATE POLICY {policy}
        ON {qualified}
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        )
        """.strip(),
        f"REVOKE ALL ON {qualified} FROM {quote_ident(role)}",
    )


def unprotect_partition_statements(
    partition: str,
    *,
    schema: str = OUTBOX_SCHEMA,
    role: str = APP_ROLE,
) -> tuple[str, ...]:
    """回滚语句（仅迁移 downgrade 使用）：恢复"分区可直连但不隔离"的旧状态。"""
    qualified = f"{quote_ident(schema)}.{quote_ident(partition)}"
    policy = quote_ident(PARTITION_POLICY)
    return (
        f"DROP POLICY IF EXISTS {policy} ON {qualified}",
        f"ALTER TABLE {qualified} DISABLE ROW LEVEL SECURITY",
        f"GRANT SELECT, INSERT, UPDATE ON {qualified} TO {quote_ident(role)}",
    )


__all__ = [
    "APP_ROLE",
    "OUTBOX_PARENT",
    "OUTBOX_SCHEMA",
    "PARTITION_POLICY",
    "protect_partition_statements",
    "quote_ident",
    "unprotect_partition_statements",
]
