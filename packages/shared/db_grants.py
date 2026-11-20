"""域角色的表级授权矩阵 —— 「谁对哪张表有什么权限」的唯一事实源。

为什么需要这个模块（根因，不是为省几条 REVOKE）
------------------------------------------------------------
N-06 的表面现象是「``project_planning_role`` 持有契约里不存在的 DELETE」，
根因是授权**从来没有被声明过**：``8f3b2c1d4e5a`` 用
``GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA`` 表达
「这个域能读写自己的 schema」。于是权限面由**表清单**决定，而不是由
**已实现的动作清单**决定：

- 新增一张表 → 自动多一份全量 DML（不需要任何人决定）；
- 动作注册表里没有任何 ``*.DELETE``（删除语义是 ``ARCHIVED`` / ``SUPERSEDED``）
  → 授权里却有 DELETE，两者从未被要求对齐；
- ``ALTER DEFAULT PRIVILEGES`` 更进一步：**未来**创建的表天生带 DELETE，
  而「验证现有表」永远看不见它（表还不存在）。

所以本模块把矩阵写成声明，并强制每条声明给出 ``write_path``（谁在何时写它）。
没有写入路径就不该有写权限——这是「权限 == 已实现动作所需的最小集合」的
可执行形式，而不是一句注释。

三层证据各管一段（互不替代）
------------------------------------------------------------
1. 静态（``scripts/boundary_assertions.py::data.role_grants_least_privilege``）：
   矩阵自洽、业务 schema 的每张 ORM 表都被声明、收敛迁移的语句**逐条等于**
   本模块生成的结果、其他迁移不得手写业务表 GRANT、默认权限不得授予域角色；
2. 收敛迁移（``c1a2b3d4e5f6``）：把矩阵落到数据库（``REVOKE ALL`` + 声明的
   ``GRANT``，并撤销业务 schema 的默认权限）。迁移里没有任何手写 DDL——
   否则「矩阵」只是文档；
3. 运行时（``tests/integration/test_least_privilege_grants.py``）：在真实 PG 上
   比对 ``information_schema.role_table_grants`` 与矩阵，并证明越权 DML 被拒。

为什么 outbox 只声明父表：分区按月动态创建，静态矩阵无法枚举；分区授权由
``packages/shared/partition_guard.py`` 统一生成（单事实源），并有独立断言
``data.relay_column_grants`` 与分区保护用例覆盖。把动态对象塞进静态矩阵只会
制造第二份清单。
"""

from __future__ import annotations

from dataclasses import dataclass

# 业务域键 == schema 名（DATA-1：一域一角色一 schema）。顺序即 db_roles 的
# domain_roles() 的插入序，不做位置耦合——用键取值，不用下标。
PROJECT_PLANNING = "project_planning"
ASSET_SERVICE = "asset_service"
DOMAINS: tuple[str, ...] = (PROJECT_PLANNING, ASSET_SERVICE)

# PG 的表级权限全集。矩阵只应出现前四种；出现 TRUNCATE/REFERENCES/TRIGGER
# 一律判为错误声明（应用从不 ALTER/DROP 表，也不需要整表清空）。
ALL_TABLE_PRIVILEGES: frozenset[str] = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
)
_USABLE_PRIVILEGES: frozenset[str] = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
# SQL 里权限的书写顺序（稳定输出，避免「顺序不同」被误判成「内容不同」）。
_PRIVILEGE_ORDER: tuple[str, ...] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


@dataclass(frozen=True)
class TableGrant:
    """一条授权声明：某域角色对某张静态表的最小权限集 + 它的真实写入路径。"""

    schema: str
    table: str
    privileges: frozenset[str]
    write_path: str
    # 表由谁定义：``orm``（本域业务表，断言强制覆盖）/ ``migration``（迁移建的表，
    # 如 audit.status_transitions）/ ``infra``（共享只读基础设施）/
    # ``cross_domain``（他域表的**列级**只读，不计入本域表覆盖判据）。
    # 这不是分类癖：ORM 覆盖判据只能对 ``orm`` 生效，否则「新表必须声明」无从执行。
    source: str = "orm"
    # 非 None 时表示**列级**授权（只授这些列）。跨域只读必须列级：授整表
    # SELECT 会把本域的 schema 细节（未来新增的敏感列）自动暴露给邻域。
    columns: tuple[str, ...] | None = None

    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    def grant_statement(self, role: str, privileges: frozenset[str] | None = None) -> str:
        """生成 ``GRANT`` 语句（列级授权的列清单位置在这里被一次定死）。

        PG 语法是 ``GRANT SELECT (col, …) ON tbl TO role``——列清单跟在**权限**
        后面，不是跟在表名后面。写错位置得到的是 ``syntax error at or near "("``，
        看起来像迁移坏了、实际是语法位置错（本地实测踩过）。
        """
        privs = _privileges_sql(self.privileges if privileges is None else privileges)
        if self.columns is not None:
            privs = f"{privs} ({', '.join(self.columns)})"
        return f"GRANT {privs} ON {self.qualified()} TO {role}"


def _grant(
    schema: str,
    table: str,
    privileges: str,
    write_path: str,
    source: str = "orm",
    columns: tuple[str, ...] | None = None,
) -> TableGrant:
    """构造声明（权限写成字符串，避免每处都手打 frozenset 字面量）。"""
    return TableGrant(
        schema=schema,
        table=table,
        privileges=frozenset(privileges.split()),
        write_path=write_path,
        source=source,
        columns=columns,
    )


# --------------------------------------------------------------------------- #
# 矩阵本体
# --------------------------------------------------------------------------- #
# 读这条矩阵的方式：先看 privileges，再看 write_path。若某项权限说不出「谁在
# 写」，它就应当被删掉；若某条写入路径没有对应权限，它会在第一次调用时报
# permission denied（fail-closed，而不是静默降级）。
DOMAIN_GRANTS: dict[str, tuple[TableGrant, ...]] = {
    PROJECT_PLANNING: (
        _grant(
            PROJECT_PLANNING,
            "projects",
            "SELECT INSERT UPDATE",
            "SELECT：find/get/list + 归属解析；INSERT：创建项目（project_routes → service.add）；"
            "UPDATE：状态推进与乐观锁版本递增（save(expected_version)）。"
            "无 DELETE：删除语义是 ARCHIVED，契约里没有 *.DELETE 动作。",
        ),
        _grant(
            PROJECT_PLANNING,
            "episodes",
            "SELECT INSERT UPDATE",
            "SELECT：按项目列举/读取；INSERT：创建集数；UPDATE：状态推进 + 版本递增。无 DELETE（同上）。",
        ),
        _grant(
            PROJECT_PLANNING,
            "script_revisions",
            "SELECT INSERT UPDATE",
            "SELECT：读取剧本版本；INSERT：提交新修订；UPDATE：锁定审批（request_lock/approve_lock）"
            "与状态推进。无 DELETE：修订历史不可物理删除（SUPERSEDED 才是终态）。",
        ),
        _grant(
            PROJECT_PLANNING,
            "organizations",
            "SELECT",
            "SELECT：归属解析 _RESOLVE_SQL['Organization'] 读组织是否存在及其 tenant。"
            "只读是刻意的：契约里没有任何 ORGANIZATION.* 动作，组织是平台级归属根，"
            "由管理面/运维创建（ORGANIZATION.CREATE 落地前不授予写入）。",
        ),
        _grant(
            PROJECT_PLANNING,
            "scenes",
            "SELECT INSERT UPDATE",
            "SELECT：按集数列举/读取 + 归属解析；INSERT：登记场次（SCENE.CREATE）；"
            "UPDATE：状态推进 + 乐观锁版本递增。无 DELETE：删除语义是 ARCHIVED。",
        ),
        _grant(
            PROJECT_PLANNING,
            "shots",
            "SELECT INSERT UPDATE",
            "SELECT：按场次列举/读取 + rework_basis_ref 守卫校验；"
            "INSERT：首次确认原子创建（SHOT.REVISION.CONFIRM）；"
            "UPDATE：状态推进 + shot_revision_id 指针前移 + 版本递增。"
            "无 DELETE：删除语义是 CANCELLED，恢复必须新建 Shot。",
        ),
        _grant(
            PROJECT_PLANNING,
            "shot_revisions",
            "SELECT INSERT UPDATE",
            "SELECT：读取修订与 revision_no 分配（取最大号）；INSERT：确认新修订；"
            "UPDATE：旧修订转 SUPERSEDED。无 DELETE：修订历史不可物理删除。",
        ),
        _grant(
            PROJECT_PLANNING,
            "shot_breakdown_drafts",
            "SELECT INSERT UPDATE",
            "SELECT：按项目读取草案与版本链；INSERT：AI 拆镜产出草案；"
            "UPDATE：认领/确认/退回/废弃。无 DELETE：终态版本必须保留可追溯。",
        ),
        _grant(
            PROJECT_PLANNING,
            "rework_tasks",
            "SELECT INSERT UPDATE",
            "SELECT：返工依据校验 + 任务查询；INSERT：登记返工任务；"
            "UPDATE：状态推进。无 DELETE：CLOSED 是终态，任务不可物理删除。",
        ),
        _grant(
            "audit",
            "status_transitions",
            "SELECT INSERT",
            "INSERT：状态推进写迁移事实（与业务写同事务）；SELECT：历史查询。"
            "append-only 由触发器保证，授权层面也不给 UPDATE/DELETE。",
            source="migration",
        ),
        _grant(
            "outbox",
            "outbox_events",
            "SELECT INSERT",
            "INSERT：跨域事件与业务写同事务落库；SELECT：同域回读校验。"
            "UPDATE 归 relay（列级），DELETE 无人持有。分区授权见 partition_guard。",
            source="migration",
        ),
        _grant(
            "auth",
            "revoked_tokens",
            "SELECT",
            "SELECT：每个请求的吊销检查（fail-closed）。写入（吊销）由签发方负责，"
            "业务域角色只读。",
            source="infra",
        ),
        _grant(
            "public",
            "alembic_version",
            "SELECT",
            "SELECT：/readyz 回报 schema revision（识别连到了另一个库实例）。",
            source="infra",
        ),
    ),
    ASSET_SERVICE: (
        _grant(
            ASSET_SERVICE,
            "asset_versions",
            "SELECT INSERT UPDATE",
            "SELECT：按项目列举/读取；INSERT：登记新版本；UPDATE：状态推进（_advance）+ 版本递增。"
            "无 DELETE：资产不可覆盖（红线），作废走 SUPERSEDED。",
        ),
        _grant(
            PROJECT_PLANNING,
            "projects",
            "SELECT",
            "跨域只读（DATA-2）：资产要校验归属项目是否存在、属于哪个租户/组织，"
            "只读三列即可（entity_id / tenant_id / organization_id）。"
            "列级而非整表：授整表 SELECT 会让项目域未来新增的列自动对资产域可见。",
            source="cross_domain",
            columns=("entity_id", "organization_id", "tenant_id"),
        ),
        _grant(
            ASSET_SERVICE,
            "qc_reports",
            "SELECT INSERT",
            "SELECT：读取质检结论；INSERT：写入质检报告。"
            "无 UPDATE/DELETE：质检结论是证据，写入即定稿（b7c8d9e0f1a2 的 append-only 口径）。",
        ),
        _grant(
            "audit",
            "status_transitions",
            "SELECT INSERT",
            "INSERT：资产状态推进写迁移事实；SELECT：历史查询。",
            source="migration",
        ),
        _grant(
            "outbox",
            "outbox_events",
            "SELECT INSERT",
            "INSERT：资产事件与业务写同事务落库；SELECT：同域回读校验。",
            source="migration",
        ),
        _grant(
            "auth",
            "revoked_tokens",
            "SELECT",
            "SELECT：吊销检查（fail-closed）。",
            source="infra",
        ),
        _grant(
            "public",
            "alembic_version",
            "SELECT",
            "SELECT：/readyz 回报 schema revision。",
            source="infra",
        ),
    ),
}


# --------------------------------------------------------------------------- #
# 派生视图与语句生成
# --------------------------------------------------------------------------- #
def declared_privileges(domain: str) -> dict[tuple[str, str], frozenset[str]]:
    """``(schema, table) -> 声明权限集``（**仅整表**授权；列级见下一个函数）。

    分开取用不是细枝末节：整表授权与列级授权在数据库里是两份 ACL
    （``relacl`` vs ``attacl``），混在一起比对会让「列级只读」永远对不上。
    """
    return {
        (g.schema, g.table): g.privileges
        for g in DOMAIN_GRANTS[domain]
        if g.columns is None
    }


def declared_column_privileges(
    domain: str,
) -> dict[tuple[str, str], tuple[frozenset[str], frozenset[str]]]:
    """``(schema, table) -> (列集合, 权限集)``（列级授权，目前只有跨域只读）。"""
    return {
        (g.schema, g.table): (frozenset(g.columns or ()), g.privileges)
        for g in DOMAIN_GRANTS[domain]
        if g.columns is not None
    }


def declared_tables(domain: str) -> frozenset[tuple[str, str]]:
    """该域声明过的表集合（含共享表与跨域只读表）。"""
    return frozenset((g.schema, g.table) for g in DOMAIN_GRANTS[domain])


def business_tables(domain: str) -> frozenset[tuple[str, str]]:
    """该域**自己的** schema 里声明过的表（不含 audit/outbox/auth/public）。

    跨域列级只读（``source="cross_domain"``）不在此列：它属于**他域**的表，
    计入「本域表覆盖」会让「新增表必须声明」这条判据把邻居的表也算自家人。
    """
    return frozenset(
        (g.schema, g.table)
        for g in DOMAIN_GRANTS[domain]
        if g.schema == domain
    )


def _privileges_sql(privileges: frozenset[str]) -> str:
    """按固定顺序拼权限列表（``SELECT, INSERT, UPDATE``），保证语句可比对。"""
    order = {p: i for i, p in enumerate(_PRIVILEGE_ORDER)}
    return ", ".join(sorted(privileges, key=lambda p: order.get(p, len(order))))


def converge_statements(
    role: str, domain: str, *, tables: frozenset[str] | None = None
) -> list[str]:
    """把该域角色的表级权限收敛到矩阵所需的**全部**语句（迁移逐条执行）。

    先 ``REVOKE ALL`` 再按矩阵 ``GRANT``：只 ``REVOKE`` 越权项也能达到同样效果，
    但那样「矩阵之外还有什么」永远取决于历史；``REVOKE ALL`` 让结果只取决于
    矩阵——这正是「声明式」与「差量式」的区别。域角色是专用角色，整表重置
    不会伤到别人的授权。

    ``REVOKE … FROM PUBLIC`` 是判据的一部分，不是顺手多写一句：``PUBLIC`` 的
    授权对**所有**角色生效，却不出现在「按角色过滤」的 ACL 视图里。只撤具名
    角色的话，一次 ``GRANT UPDATE ON t TO PUBLIC`` 就能让域角色绕过矩阵而不被
    任何按角色的比对照见（评审 F4）。

    ``tables`` 是**历史迁移的冻结范围**（评审 F1）。迁移是时间胶囊：它记录的是
    「那时该做什么」，不能读今天的矩阵去决定「那时有什么表」。不传 ``tables``
    表示按当前矩阵全量收敛（供断言与初始化使用）；迁移必须显式传自己的快照，
    否则未来矩阵新增一张表时，历史迁移会在**建表之前**对它 ``REVOKE ALL``，
    clean install 直接失败，而已升级的库又不会重跑该迁移。

    默认权限的撤销也在本函数里：``ALTER DEFAULT PRIVILEGES`` 让未来表天生带权限，
    不撤销就等于把同一个根因留给「下一次建表」（``8f3b2c1d4e5a`` 曾给域角色
    授了 ``SELECT, INSERT, UPDATE, DELETE`` 的默认权限，而 ``b7c8d9e0f1a2``
    的「默认零授权」只覆盖了 app_role——旧机制在新角色上复发）。
    """
    statements: list[str] = [
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM {role}"
        for schema in DOMAINS
    ] + [
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
        f"REVOKE ALL ON SEQUENCES FROM {role}"
        for schema in DOMAINS
    ] + [
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM PUBLIC"
        for schema in DOMAINS
    ]
    for grant in DOMAIN_GRANTS[domain]:
        if tables is not None and grant.table not in tables:
            continue
        qualified = grant.qualified()
        statements.append(f"REVOKE ALL ON {qualified} FROM PUBLIC")
        statements.append(f"REVOKE ALL ON {qualified} FROM {role}")
        statements.append(grant.grant_statement(role))
    return statements


def legacy_statements(role: str, domain: str) -> list[str]:
    """``converge_statements`` 的逆：回到「全量 DML + 全量默认权限」的旧状态。

    仅用于 ``downgrade()``。旧状态不等同于「矩阵的补集」：它是
    ``8f3b2c1d4e5a`` 的 ``GRANT ... ON ALL TABLES`` 加 ``_revoke_append_only_overreach``
    之后的具体结果——按表写出来，而不是用一个 ``ALL TABLES`` 回滚（那会把
    append-only 的收紧一并抹掉，正是本迁移要根治的形态）。
    """
    append_only_tables = {
        (ASSET_SERVICE, "qc_reports"),
        (ASSET_SERVICE, "asset_versions"),
    }
    statements: list[str] = []
    for grant in DOMAIN_GRANTS[domain]:
        if grant.schema not in DOMAINS:
            # 共享表（audit / outbox / auth / public）**不归本迁移管**（评审 F3）。
            # 本迁移从未改过它们的历史口径，所以「升级前是什么」就是矩阵此刻声明
            # 的权限——不需要也不应该在这里重建。早先版本对它们一律「REVOKE ALL
            # + 全量 DML」，把 ``auth.revoked_tokens`` 从只读放大成可 DELETE，
            # 于是降级一次就能删掉吊销记录、让已吊销令牌复活——降级路径成了
            # 「终态不可复活」的后门。降级只回滚自己改过的东西。
            continue
        if grant.columns is not None:
            # 列级授权：历史口径与矩阵一致（b3c4d5e6f7a8 授的就是这三列只读），
            # 回滚不需要也不允许把它放大成整表 DML——`GRANT DELETE (col)` 是
            # 非法语句（PG：invalid privilege type DELETE for column），
            # 恰好说明「列级」与「整表」不能共用同一套权限字面量。
            statements.append(f"REVOKE ALL ON {grant.qualified()} FROM {role}")
            statements.append(grant.grant_statement(role))
            continue
        privileges = {"SELECT", "INSERT", "UPDATE", "DELETE"}
        if (grant.schema, grant.table) in append_only_tables:
            # 旧口径：qc_reports 无 UPDATE/DELETE；asset_versions 无 DELETE。
            privileges = {"SELECT", "INSERT"}
            if grant.table == "asset_versions":
                privileges = {"SELECT", "INSERT", "UPDATE"}
        qualified = grant.qualified()
        statements.append(f"REVOKE ALL ON {qualified} FROM {role}")
        statements.append(grant.grant_statement(role, frozenset(privileges)))
    statements.extend(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
        for schema in DOMAINS
    )
    statements.extend(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {role}"
        for schema in DOMAINS
    )
    return statements


# --------------------------------------------------------------------------- #
# 自洽校验（由断言调用，返回问题列表而不是抛异常：闸门要能一次报全）
# --------------------------------------------------------------------------- #
def validate_matrix() -> list[str]:
    """矩阵自身的可判定性检查（与数据库、与代码都无关，纯声明层）。

    这些规则看起来「只是一个声明文件能有什么错」，但它们正是矩阵从「文档」
    变成「契约」的地方：``source`` 拼错会让 ORM 覆盖判据静默失效，空
    ``write_path`` 会让「为什么需要这项权限」重新变成没人回答的问题。
    """
    problems: list[str] = []
    if set(DOMAIN_GRANTS) != set(DOMAINS):
        problems.append(f"矩阵的域集合 {sorted(DOMAIN_GRANTS)} 与 DOMAINS 不一致")

    for domain in DOMAINS:
        seen: set[tuple[str, str]] = set()
        for grant in DOMAIN_GRANTS[domain]:
            key = (grant.schema, grant.table)
            if key in seen:
                problems.append(f"{domain}: {grant.qualified()} 重复声明")
            seen.add(key)
            unknown = grant.privileges - ALL_TABLE_PRIVILEGES
            if unknown:
                problems.append(f"{domain}: {grant.qualified()} 含未知权限 {sorted(unknown)}")
            unusable = grant.privileges - _USABLE_PRIVILEGES
            if unusable:
                problems.append(
                    f"{domain}: {grant.qualified()} 声明了应用用不到的权限 "
                    f"{sorted(unusable)}（应用从不 ALTER/TRUNCATE 表）"
                )
            if not grant.privileges:
                problems.append(f"{domain}: {grant.qualified()} 没有任何权限，应从矩阵删除")
            if grant.source not in {"orm", "migration", "infra", "cross_domain"}:
                problems.append(
                    f"{domain}: {grant.qualified()} 的 source={grant.source!r} 非法"
                    "（orm/migration/infra/cross_domain），拼错会让覆盖判据静默失效"
                )
            if not grant.write_path.strip():
                problems.append(
                    f"{domain}: {grant.qualified()} 未给出真实写入路径"
                    "（说不出谁在写，就不该有写权限）"
                )
            if grant.columns is not None:
                if not grant.columns:
                    problems.append(
                        f"{domain}: {grant.qualified()} 列级授权但没有列清单"
                        "（空清单在 SQL 里非法，等于整表语义）"
                    )
                if list(grant.columns) != sorted(set(grant.columns)):
                    problems.append(
                        f"{domain}: {grant.qualified()} 的列清单未按字典序去重"
                        "（否则生成的 GRANT 语句顺序不稳定，无法逐字比对）"
                    )
                if "DELETE" in grant.privileges:
                    problems.append(
                        f"{domain}: {grant.qualified()} 对列授 DELETE 属语义错误"
                        "（列级 DELETE 无意义，PG 也不支持）"
                    )
                if len(grant.privileges) > 1:
                    problems.append(
                        f"{domain}: {grant.qualified()} 列级授权声明了多项权限 "
                        f"{sorted(grant.privileges)}：PG 会把列清单只绑定到最后一个权限，"
                        "生成的语句语义与声明不符"
                    )

    # 共享表（audit/outbox/auth/public）在两个域里必须完全一致：不一致意味着
    # 「同一个平台组件对不同域不同口径」，那是第二套规则，不是最小权限。
    # 注意排除业务 schema：那里两个域的口径本来就**必须**不同（跨域只读是
    # 单向的），把它算进「必须一致」会让矩阵自相矛盾。
    shared: dict[tuple[str, str], set[frozenset[str]]] = {}
    for domain in DOMAINS:
        for grant in DOMAIN_GRANTS[domain]:
            if grant.schema not in DOMAINS:
                shared.setdefault((grant.schema, grant.table), set()).add(grant.privileges)
    for key, variants in shared.items():
        if len(variants) > 1:
            problems.append(
                f"{key[0]}.{key[1]} 在两个域的权限口径不一致："
                f"{[sorted(v) for v in variants]}"
            )

    return problems


__all__ = [
    "ALL_TABLE_PRIVILEGES",
    "ASSET_SERVICE",
    "DOMAINS",
    "DOMAIN_GRANTS",
    "PROJECT_PLANNING",
    "TableGrant",
    "business_tables",
    "converge_statements",
    "declared_column_privileges",
    "declared_privileges",
    "declared_tables",
    "legacy_statements",
    "validate_matrix",
]
