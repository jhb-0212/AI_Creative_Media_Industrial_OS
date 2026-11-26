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
# 与父表策略同名：策略名按表作用域独立，同名便于排障时一眼认出。
PARTITION_POLICY = "tenant_isolation_outbox_events"

# relay 唯一允许回写的「投递元数据」列（F03 数据所有权 + 审查 P1-01）。
# 事件业务内容（payload / event_type / tenant_id / aggregate_type /
# aggregate_id / occurred_at）是 append-only 审计事实，relay 不得修改——
# 否则被攻破的 relay 进程可篡改已投递审计事件。列级授权把「只能改投递状态」
# 下沉到数据库层（GRANT UPDATE(col1, col2)），而非依赖 Python 代码自觉。
# 与 workers/relay/consumer.py 的三条 UPDATE（_ACK 写 published_at、
# _FAIL 写 attempts/next_attempt_at/dead_lettered_at）一一对应。
RELAY_UPDATE_COLUMNS = ("published_at", "attempts", "next_attempt_at", "dead_lettered_at")

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
    role: str | None = None,
) -> tuple[str, ...]:
    """返回"让某个分区进入受保护状态"所需的全部 SQL（幂等）。

    顺序有意义：先开 RLS 再建策略（策略可以在 RLS 未开时创建，但先开语义清楚），
    最后回收直连授权——保证任何中间失败重跑都能收敛到同一状态。

    ``role`` 缺省时取配置里的应用角色名（``packages/shared/db_roles.py``）。
    此前它是 ``role: str = APP_ROLE`` 的**默认参数**，默认值在 import 时求值：
    改了 ``POSTGRES_APP_USER`` 之后，本模块仍然回收着字面量角色的权限，
    于是「按配置建的角色」与「被收权的角色」不是同一个（评审 T5 的同型缺陷）。
    """
    from packages.shared.db_roles import app_role_name

    qualified = f"{quote_ident(schema)}.{quote_ident(partition)}"
    policy = quote_ident(PARTITION_POLICY)
    target_role = quote_ident(role if role is not None else app_role_name())
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
        f"REVOKE ALL ON {qualified} FROM {target_role}",
    )


def unprotect_partition_statements(
    partition: str,
    *,
    schema: str = OUTBOX_SCHEMA,
    role: str | None = None,
) -> tuple[str, ...]:
    """回滚语句（仅迁移 downgrade 使用）：恢复"分区可直连但不隔离"的旧状态。"""
    from packages.shared.db_roles import app_role_name

    qualified = f"{quote_ident(schema)}.{quote_ident(partition)}"
    policy = quote_ident(PARTITION_POLICY)
    target_role = quote_ident(role if role is not None else app_role_name())
    return (
        f"DROP POLICY IF EXISTS {policy} ON {qualified}",
        f"ALTER TABLE {qualified} DISABLE ROW LEVEL SECURITY",
        f"GRANT SELECT, INSERT, UPDATE ON {qualified} TO {target_role}",
    )


def relay_column_update_statements(
    table: str,
    *,
    schema: str = OUTBOX_SCHEMA,
) -> tuple[str, ...]:
    """把 relay 对某张 outbox 事件表（父表或分区）的 UPDATE 收窄为列级（审查 P1-01）。

    根因：7a8b9c0d1e2f 对父表授了 ``GRANT SELECT, UPDATE``（整表），
    ae8724980e0b 对分区授了 ``GRANT SELECT, UPDATE``（整表）。整表 UPDATE
    意味着 relay 能改写 payload / event_type / tenant_id 等事件业务内容——
    被攻破的 relay 进程即可篡改已投递审计事件，违背「事件不可篡改」红线。

    根治：撤销整表 UPDATE，改授列级 ``UPDATE (RELAY_UPDATE_COLUMNS)``，
    使「改业务内容」在数据库层被拒绝（permission denied for column），
    而非依赖 relay 代码自觉只写 published_at/attempts 等投递元数据列。

    语句幂等：REVOKE UPDATE 与 GRANT UPDATE(cols) 重复执行无害；但必须先
    REVOKE 再 GRANT（列级授权是追加，整表授权若残留仍会覆盖列级限制）。
    """
    from packages.shared.config import get_settings

    relay_role = get_settings().postgres_relay_user
    qualified = f"{quote_ident(schema)}.{quote_ident(table)}"
    columns = ", ".join(quote_ident(c) for c in RELAY_UPDATE_COLUMNS)
    return (
        f"REVOKE UPDATE ON {qualified} FROM {quote_ident(relay_role)}",
        f"GRANT UPDATE ({columns}) ON {qualified} TO {quote_ident(relay_role)}",
    )


def _grant_relay_select_update(qualified: str, relay_role: str) -> tuple[str, str]:
    """给 relay 角色授予「SELECT + 列级 UPDATE」的最小授权（审查 P1-01）。

    整表 ``GRANT SELECT, UPDATE`` 会让 relay 能改写 payload/event_type/
    tenant_id 等事件业务内容，破坏「事件不可篡改」审计红线。列级授权把
    UPDATE 收窄到 RELAY_UPDATE_COLUMNS（投递元数据列），使「改业务内容」
    在数据库层被拒绝（permission denied for column）。

    返回两条语句（SELECT 与列级 UPDATE 分开）：调用方逐条 op.execute /
    conn.execute(text())，asyncpg 的单语句 text() 不支持一次执行多句。
    """
    columns = ", ".join(quote_ident(c) for c in RELAY_UPDATE_COLUMNS)
    return (
        f"GRANT SELECT ON {qualified} TO {quote_ident(relay_role)}",
        f"GRANT UPDATE ({columns}) ON {qualified} TO {quote_ident(relay_role)}",
    )


def grant_outbox_partition_statements(
    partition: str,
    *,
    schema: str = OUTBOX_SCHEMA,
) -> tuple[str, ...]:
    """返回"给一个 outbox 分区按访问类型授予业务角色"所需的全部 SQL（幂等）。

    根因（DATA-1/F03 拆角色后的分区授权缺口）：分区由维护工具/迁移动态创建，
    父表的 ``GRANT SELECT/INSERT/UPDATE ON 父表`` **不会自动传播到分区**——PG
    在通过父表访问时仍会校验目标分区的权限。历史上授权模型只围绕单一
    app_role（依赖 ``ALTER DEFAULT PRIVILEGES`` 自动授给 app_role 再被
    ``protect_partition_statements`` 收口）；拆出 project_planning_role /
    asset_service_role / outbox_relay_role 后，这三个角色只在父表授了权，
    分区从未授权，导致通过父表写/读事件时报 ``permission denied``。

    按访问类型授权（最小权限，不因分区而放大）：
    - project_planning_role / asset_service_role：INSERT + SELECT（写事件、
      读本域事件）。
    - outbox_relay_role：SELECT + 列级 UPDATE（领取待投递事件、回写投递
      元数据列），见 RELAY_UPDATE_COLUMNS——relay 不得修改事件业务内容
      （payload / event_type / tenant_id 等审计事实）。
    - app_role 不在此列：其分区直连权限已由 protect_partition_statements 收口。

    角色名从 config 读取（单一事实源），避免在运行时工具里硬编码角色字符串。
    """
    from packages.shared.config import get_settings

    settings = get_settings()
    project_role = settings.postgres_project_planning_user
    asset_role = settings.postgres_asset_service_user
    relay_role = settings.postgres_relay_user
    qualified = f"{quote_ident(schema)}.{quote_ident(partition)}"
    return (
        f"GRANT SELECT, INSERT ON {qualified} TO {quote_ident(project_role)}",
        f"GRANT SELECT, INSERT ON {qualified} TO {quote_ident(asset_role)}",
        *_grant_relay_select_update(qualified, relay_role),
    )


def revoke_outbox_partition_statements(
    partition: str,
    *,
    schema: str = OUTBOX_SCHEMA,
) -> tuple[str, ...]:
    """回滚 grant_outbox_partition_statements 的授权（迁移 downgrade 用）。"""
    from packages.shared.config import get_settings

    settings = get_settings()
    project_role = settings.postgres_project_planning_user
    asset_role = settings.postgres_asset_service_user
    relay_role = settings.postgres_relay_user
    qualified = f"{quote_ident(schema)}.{quote_ident(partition)}"
    return (
        f"REVOKE ALL ON {qualified} FROM {quote_ident(project_role)}",
        f"REVOKE ALL ON {qualified} FROM {quote_ident(asset_role)}",
        f"REVOKE ALL ON {qualified} FROM {quote_ident(relay_role)}",
    )


def ops_replay_update_columns() -> tuple[str, ...]:
    """运维重放可改的列（从授权矩阵派生，不另存一份清单）。

    为什么从 ``db_grants`` 取而不是在这里写死：列清单是**授权声明**，它的
    事实源在矩阵里；这里只负责把同一份声明渲染成动态分区上的 SQL。两处各写
    一份的结果是「父表的授权按矩阵、分区的授权按本地常量」——新分区多授一列
    （例如 ``published_at``，等于允许伪造「已投递」）不会有任何闸门发现。
    """
    from packages.shared.db_grants import INFRA_ROLE_GRANTS, OUTBOX_OPS

    for grant in INFRA_ROLE_GRANTS[OUTBOX_OPS]:
        if grant.table == OUTBOX_PARENT and grant.columns:
            return grant.columns
    raise LookupError(
        "授权矩阵里找不到 outbox_ops 对 outbox_events 的列级授权："
        "分区授权无所依据（拒绝猜一份列清单）"
    )


def grant_ops_partition_statements(
    partition: str,
    *,
    schema: str = OUTBOX_SCHEMA,
) -> tuple[str, ...]:
    """给 outbox 分区授予运维角色「SELECT + 重放三列 UPDATE」（幂等）。

    为什么单独一个函数、而不是塞进 ``grant_outbox_partition_statements``：
    那个函数被 ``ae8724980e0b`` / ``b9e1c2d3f4a5`` 调用，而它们是**已执行的
    历史迁移**。给它们的行为加「再授一个角色」会让 clean install 在
    ``outbox_ops_role`` 尚不存在的时点 GRANT 到不存在的角色而失败——正是评审
    F1 的时间胶囊问题。新授权面由新函数 + 新迁移表达。

    运维角色拿不到 ``published_at`` 的写权限：投递时间只能由真正投递成功的
    relay 写，运维动作不得伪造「已投递」。
    """
    from packages.shared.config import get_settings

    ops_role = get_settings().postgres_outbox_ops_user
    qualified = f"{quote_ident(schema)}.{quote_ident(partition)}"
    columns = ", ".join(quote_ident(c) for c in ops_replay_update_columns())
    return (
        f"GRANT SELECT ON {qualified} TO {quote_ident(ops_role)}",
        f"GRANT UPDATE ({columns}) ON {qualified} TO {quote_ident(ops_role)}",
    )


def _ops_partition_loop_block(
    *,
    schema: str,
    statements: tuple[str, ...],
) -> str:
    """把「遍历现存 outbox 分区」的骨架与一组 EXECUTE 语句拼成一个 DO 块。

    授予与撤销共用这一处骨架，是返工换来的教训：两侧各写一份时，授予侧遍历
    分区、撤销侧只撤父表——``alembic downgrade -1`` 才因残留的分区授权停在
    ``role ... cannot be dropped because some objects depend on it``，而
    ``upgrade`` / ``alembic check`` 一路绿灯（分区 ACL 是独立条目，不随父表撤销）。

    谓词同样只有这一份。``c.relkind = 'r'`` 不能少：分区上的索引也
    ``relispartition = true``，对着索引 GRANT/REVOKE 会报「… is an index」。
    """
    body = "\n".join(f"        {statement}" for statement in statements)
    return (
        "DO $$\n"
        "DECLARE r record;\n"
        "BEGIN\n"
        "    FOR r IN\n"
        "        SELECT c.relname\n"
        "        FROM pg_class c\n"
        "        JOIN pg_namespace n ON n.oid = c.relnamespace\n"
        f"        WHERE n.nspname = '{schema}'\n"
        "          AND c.relispartition\n"
        "          AND c.relkind = 'r'\n"
        "    LOOP\n"
        f"{body}\n"
        "    END LOOP;\n"
        "END\n"
        "$$"
    )


def ops_partition_grant_do_block(*, schema: str = OUTBOX_SCHEMA) -> str:
    """对**已存在**的 outbox 分区补齐运维授权（一个动态 DO 块）。

    为什么必须是动态 SQL：分区名按月生成（``outbox_events_yYYYYMM``），静态
    迁移无从枚举。同类问题在本仓库已有定论——它的说法是「动态对象不进静态
    矩阵，由 partition_guard 统一生成」（见 db_grants 模块 docstring）；本函数
    就是该口径在运维角色上的具体实现，而不是绕过闸门的新入口：列清单仍从
    矩阵派生（``ops_replay_update_columns``），未来分区仍由维护工具在
    ``create_partition`` 里逐条授予。

    为什么单独成一个函数而不是写进迁移文件：迁移里的 GRANT 字面量被
    ``data.role_grants_least_privilege`` 判为「手写授权」（授权只能经矩阵
    收敛）。把生成器放在 partition_guard 是既有约定——它是动态分区授权的
    单一事实源，且迁移调用它时语句形态可被回放核对。
    """
    from packages.shared.config import get_settings

    role = get_settings().postgres_outbox_ops_user
    columns = ", ".join(ops_replay_update_columns())
    # %I 由 SQL 的 format() 做标识符引用；角色名与列名都走 %I 参数位，
    # 不拼进 format 模板本身（否则分区名里出现引号就会打断语句）。
    return _ops_partition_loop_block(
        schema=schema,
        statements=(
            f"EXECUTE format('GRANT SELECT ON {schema}.%I TO %I', "
            f"r.relname, '{role}');",
            f"EXECUTE format('GRANT UPDATE ({columns}) ON {schema}.%I TO %I', "
            f"r.relname, '{role}');",
        ),
    )


def ops_partition_revoke_do_block(*, schema: str = OUTBOX_SCHEMA) -> str:
    """撤销**所有现存**分区上的运维授权（``DROP ROLE`` 的前置条件）。

    ``ops_partition_grant_do_block`` 的对偶。缺了它的后果不是"多留一点权限"，
    而是整个 downgrade 硬失败：PG 拒绝删除仍持有对象权限的角色，而分区上的
    GRANT 是独立于父表的 ACL 条目——只撤父表永远撤不完。CI 的 downgrade
    roundtrip 就是被这一条挡下来的（``c5d6e7f8a9b0`` 首版）。
    """
    from packages.shared.config import get_settings

    role = get_settings().postgres_outbox_ops_user
    return _ops_partition_loop_block(
        schema=schema,
        statements=(
            f"EXECUTE format('REVOKE ALL ON {schema}.%I FROM %I', "
            f"r.relname, '{role}');",
        ),
    )


__all__ = [
    "OUTBOX_PARENT",
    "OUTBOX_SCHEMA",
    "PARTITION_POLICY",
    "RELAY_UPDATE_COLUMNS",
    "grant_ops_partition_statements",
    "grant_outbox_partition_statements",
    "ops_partition_grant_do_block",
    "ops_partition_revoke_do_block",
    "ops_replay_update_columns",
    "protect_partition_statements",
    "quote_ident",
    "relay_column_update_statements",
    "revoke_outbox_partition_statements",
    "unprotect_partition_statements",
]
