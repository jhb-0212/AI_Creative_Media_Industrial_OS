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
# R16 三域拆分（迁移 a0b1c2d3e4f5）：生产链 / 预算额度 / 质检治理。
PRODUCTION_PLANNING = "production_planning"
BUDGET_QUOTA = "budget_quota"
QC_GOVERNANCE = "qc_governance"
DOMAINS: tuple[str, ...] = (
    PROJECT_PLANNING,
    ASSET_SERVICE,
    PRODUCTION_PLANNING,
    BUDGET_QUOTA,
    QC_GOVERNANCE,
)

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
            "scenes",
            "SELECT INSERT UPDATE",
            "SELECT：读取场次 + 归属解析 _RESOLVE_SQL['Scene']；INSERT：SCENE.CREATE；"
            "UPDATE：SCENE.STATUS.CHANGE 的乐观锁条件更新。无 DELETE：场次的终结语义是 "
            "ARCHIVED，契约里没有 SCENE.DELETE；物理删除会打断 shots 的归属链。",
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
            PROJECT_PLANNING,
            "shot_specs",
            "SELECT INSERT UPDATE",
            "SELECT：规格读取 + 幂等回读 + 修订占用回读；"
            "INSERT：SHOTSPEC.CREATE；UPDATE：SHOTSPEC.LOCK 的状态推进与版本递增。"
            "无 DELETE：LOCKED 是终态，改规格走新修订 + 新规格，历史不可物理删除。",
        ),
        _grant(
            PROJECT_PLANNING,
            "constraint_packs",
            "SELECT INSERT UPDATE",
            "SELECT：约束包读取 + 幂等回读 + 作用镜头归属校验；"
            "INSERT：CONSTRAINT.PACK.CREATE；"
            "UPDATE：CONSTRAINT.PACK.LOCK 的 DRAFT→ACTIVE 与版本递增"
            "（三条治理边的动作 CONSTRAINT.PACK.STATUS.CHANGE 尚未实现，"
            "授权面先留在这里——它一旦实现不需要再改矩阵）。"
            "无 DELETE：STALE/SUPERSEDED/REVOKED 都是治理结论，"
            "包历史是「当时凭什么锁」的唯一证据，不可物理删除。",
        ),
        # 生产链与预算对象的五张表**不在**本域：R16 域拆分（a0b1c2d3e4f5）
        # 已把它们移到 production_planning / budget_quota / qc_governance 三个
        # 自有 schema。这里若继续声明，会变成「矩阵声明了本域不存在的表」，
        # 且两个角色对同一张表的 DML 面会产生两份互相矛盾的声明。
        _grant(
            "audit",
            "status_transitions",
            "SELECT INSERT",
            "INSERT：状态推进写迁移事实（与业务写同事务）；SELECT：历史查询。"
            "append-only 由触发器保证，授权层面也不给 UPDATE/DELETE。",
            source="migration",
        ),
        _grant(
            "audit",
            "approval_signatures",
            "SELECT INSERT",
            "INSERT：席位签署写累积会签事实（与放行事件同事务）；SELECT：凑齐判定读取。"
            "append-only 由触发器保证，撤回/消费以新 event_kind 行表达，不给 UPDATE/DELETE。",
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
            "asset_registration_receipts",
            "SELECT INSERT",
            "SELECT：T3 按业务幂等键回读回执、重复投递时复用结果；"
            "INSERT：T2 登记成功或内容冲突时落一条事实。无 UPDATE/DELETE："
            "回执由触发器强制 append-only，冲突只能另建带理由的 REJECTED，不能改写既有事实。",
        ),
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
            "audit",
            "approval_signatures",
            "SELECT INSERT",
            "INSERT：席位签署写累积会签事实（与放行事件同事务）；SELECT：凑齐判定读取。"
            "append-only 由触发器保证，撤回/消费以新 event_kind 行表达，不给 UPDATE/DELETE。",
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


def _platform_grants(*, label: str) -> tuple[TableGrant, ...]:
    """共享基础设施的授权（audit 迁移事实写、outbox 事件写、auth 只读、版本只读）。

    写成函数而不是各域抄一遍：``validate_matrix`` 要求共享表在**所有域**口径
    一致，手抄四份的第一次手误就是「某个域能 UPDATE 事件内容」——而那种差异
    不会在单域测试里暴露。``label`` 只影响 ``write_path`` 的文字（保留「谁在写」
    这个必答项），不改变权限集。
    """
    return (
        _grant(
            "audit",
            "status_transitions",
            "SELECT INSERT",
            f"INSERT：{label}的状态推进写迁移事实（与业务写同事务）；SELECT：历史查询。"
            "append-only 由触发器保证，授权层面也不给 UPDATE/DELETE。",
            source="migration",
        ),
        _grant(
            "audit",
            "approval_signatures",
            "SELECT INSERT",
            f"INSERT：{label}的席位签署写会签事实（与放行事件同事务）；SELECT：凑齐判定读取。"
            "append-only 由触发器保证，授权层面也不给 UPDATE/DELETE（撤回/消费以新 event_kind 行表达）。",
            source="migration",
        ),
        _grant(
            "outbox",
            "outbox_events",
            "SELECT INSERT",
            f"INSERT：{label}的跨域事件与业务写同事务落库；SELECT：同域回读校验。"
            "UPDATE 归 relay（列级），DELETE 无人持有。分区授权见 partition_guard。",
            source="migration",
        ),
        _grant(
            "auth",
            "revoked_tokens",
            "SELECT",
            "SELECT：每个请求的吊销检查（fail-closed）。写入由签发方负责。",
            source="infra",
        ),
        _grant(
            "public",
            "alembic_version",
            "SELECT",
            "SELECT：/readyz 回报 schema revision（识别连到了另一个库实例）。",
            source="infra",
        ),
    )


def _projects_readonly(label: str) -> TableGrant:
    """跨域列级只读 ``project_planning.projects``（归属解析的唯一依赖）。

    新域要知道「这个 project 属于哪个租户/组织」才能做 RLS 与归属校验，但它
    没有任何理由读剧本、镜头或规格。列级而非整表：项目域未来新增的列（例如
    预算总额、客户信息）不会自动对邻域可见——这正是 ``b3c4d5e6f7a8`` 已经
    为 asset_service 立下的范式，新域沿用同一形态而不是各写一种。
    """
    return _grant(
        PROJECT_PLANNING,
        "projects",
        "SELECT",
        f"跨域只读（DATA-2）：{label}校验归属项目是否存在、属于哪个租户/组织，"
        "只读三列即可（entity_id / tenant_id / organization_id）。",
        source="cross_domain",
        columns=("entity_id", "organization_id", "tenant_id"),
    )


# R16 三域（a0b1c2d3e4f5）。每个域只声明**自己** schema 的表 + 平台共享表 +
# 项目域的列级只读；它没有别的域的任何权限，因此「生产域去写账本」这类越界
# 在数据库层就被拒绝，而不是靠代码审查。
DOMAIN_GRANTS[PRODUCTION_PLANNING] = (
    _grant(
        PRODUCTION_PLANNING,
        "artifact_manifests",
        "SELECT INSERT UPDATE",
        "SELECT：按 generation_task_id 回读写入意图，供崩溃恢复重建登记请求；"
        "INSERT：T0 落 PREPARED 意图；UPDATE：T1 复核对象与 SHA256 后推进 STORED_VERIFIED。"
        "无 DELETE：意图是恢复依据，抹掉它等于把崩溃后的任务变成无法对账的黑洞。",
    ),
    _grant(
        PRODUCTION_PLANNING,
        "production_routes",
        "SELECT INSERT UPDATE",
        "SELECT：路线读取/回读；INSERT：PRODUCTION.ROUTE.SELECT 落一条 VERIFIED 记录；"
        "UPDATE：VERIFIED→REVOKED 治理边与版本递增。无 DELETE：选择记录是证据。",
    ),
    _grant(
        PRODUCTION_PLANNING,
        "generation_tasks",
        "SELECT INSERT UPDATE",
        "SELECT：任务读取；INSERT：GENERATION.REQUEST 建 REQUESTED；"
        "UPDATE：推进 SUCCEEDED/FAILED（或未来的 RUNNING 租约续期）与版本递增。"
        "无 DELETE：任务历史是「谁在什么约束下生成了什么」的证据。",
    ),
    _projects_readonly("生产链"),
    *_platform_grants(label="生产链"),
)

DOMAIN_GRANTS[BUDGET_QUOTA] = (
    _grant(
        BUDGET_QUOTA,
        "shot_budget_requests",
        "SELECT INSERT UPDATE",
        "SELECT：申请读取/审批回读；INSERT：BUDGET.SHOT.REQUEST 建 DRAFT；"
        "UPDATE：BUDGET.SHOT.APPROVE 推进 APPROVED/REJECTED。无 DELETE：审批是证据。",
    ),
    _grant(
        BUDGET_QUOTA,
        "budget_ledger",
        "SELECT INSERT",
        "SELECT：余额按流水聚合、幂等键回读；INSERT：审批同事务原子追加一条流水。"
        "无 UPDATE/DELETE：追加式不可变账本（金额事实一旦落库就只能新增冲正流水）。",
    ),
    _projects_readonly("预算额度"),
    *_platform_grants(label="预算额度"),
)

DOMAIN_GRANTS[QC_GOVERNANCE] = (
    _grant(
        QC_GOVERNANCE,
        "gate_decisions",
        "SELECT INSERT",
        "SELECT：Gate 决策回读与审计比对；INSERT：PRODUCTION.GATE.APPROVE 落一条决策。"
        "无 UPDATE/DELETE：决策是追加式不可变证据，撤销语义应记新决策而不是改写历史。",
    ),
    _grant(
        QC_GOVERNANCE,
        "qc_tasks",
        "SELECT INSERT UPDATE",
        "SELECT：待检队列调度（next_claimable_for_claim / list_expired）与回读；"
        "INSERT：QC.TASK.ENQUEUE 入队；"
        "UPDATE：QC.TASK.PROCESS 的领取/续租/判定/租约回收（乐观锁条件更新）。"
        "无 DELETE：队列记录是「这份产物检过没有」的证据，不能物理删除。",
    ),
    _projects_readonly("质检治理"),
    # 调度投影（``qc_maintenance.task_projection``，迁移 ``a8b9c0d1e2f3``）。
    # 它由质检域在同事务里写，因此授权挂在 ``qc_governance_role`` 上；但它
    # **不**计入 ``business_tables``（那是「本域自有 schema 的表」），所以这里
    # 用显式 qualified 名声明。写入路径必须写清，否则「谁能写投影」重新变成
    # 没人回答的问题。
    _grant(
        "qc_maintenance",
        "task_projection",
        "SELECT INSERT UPDATE DELETE",
        "SELECT：调度候选读取（queued/running 两类查询）与对账比对；"
        "INSERT：任务入队时落首行投影；UPDATE：领取/续租/租约回收/进度变化时"
        "同事务覆盖（带 entity_version 单调条件，旧快照不得覆盖新快照）；"
        "DELETE：任务进入终态（PASSED/REJECTED）时删除该行——投影只表达"
        "「活动任务」，留一行终态会让调度看到永不运行的候选。"
        "DELETE 在此只针对**派生数据**，不是业务事实：错删可由 qc_tasks 重建。",
    ),
    *_platform_grants(label="质检治理"),
)


# --------------------------------------------------------------------------- #
# 平台运维角色（不是业务域：不拥有任何 schema，只操作事件总线的投递状态）
# --------------------------------------------------------------------------- #
# 为什么这里必须有第二张矩阵，而不是把运维角色塞进 DOMAINS
# ------------------------------------------------------------
# `DOMAINS` 的语义是「一域一角色一自有 schema」，覆盖判据、`business_tables`、
# 组织 RLS 应检集合全都建立在「域 == schema」上。运维角色没有自己的 schema，
# 塞进去会让「本域 schema 的每张 ORM 表都必须在矩阵里」这条判据对它求值成
# 空集——看起来通过，实际是把判据变成永真式。
#
# 但也不能不声明：`data.role_grants_least_privilege` 禁止**此后**的迁移手写
# GRANT（授权只能经矩阵收敛），没有声明载体时，新角色与新表的授权要么写不出
# 来（只能手写 GRANT 然后被闸门拦下），要么被迫给某个业务域角色扩权——后者是
# 权限倒退，比缺一个角色严重得多。D6-9 需要「读事件 + 清死信标记 + 写运维审计表」
# 三者同时成立，而 outbox_relay_role 刻意没有 INSERT（否则可伪造任意业务事件），
# 业务域角色刻意没有 outbox 的 UPDATE。所以这张表是权限边界的直接产物。
OUTBOX_OPS = "outbox_ops"
INFRA_ROLES: tuple[str, ...] = (OUTBOX_OPS,)

INFRA_ROLE_GRANTS: dict[str, tuple[TableGrant, ...]] = {
    OUTBOX_OPS: (
        _grant(
            "outbox",
            "outbox_events",
            "SELECT",
            "SELECT：按 event_id 定位死信事件并 FOR UPDATE 读取失败事实"
            "（attempts / dead_lettered_at）。RLS 仍按租户生效，跨租户查不到即 404。",
            source="infra",
        ),
        _grant(
            "outbox",
            "outbox_events",
            "UPDATE",
            "UPDATE：重放只清死信标记三列（attempts 归零、next_attempt_at=now()、"
            "dead_lettered_at=NULL），交回正常 claim。刻意不给 published_at："
            "投递时间只能由真正投递成功的 relay 写，运维动作不得伪造「已投递」。"
            "事件内容（payload / event_type / tenant_id）任何角色都不得改写。",
            source="infra",
            columns=("attempts", "dead_lettered_at", "next_attempt_at"),
        ),
        _grant(
            "outbox",
            "dead_letter_replays",
            "SELECT INSERT",
            "INSERT：每次重放尝试落一行 append-only 审计（操作者/理由/重放前失败次数/"
            "重放前死信时刻/结果/失败原因）；SELECT：排障回读。"
            "无 UPDATE/DELETE：审计行一旦写入即定稿，结果以新行表达，"
            "改写历史等于抹掉「当时谁按什么理由放行的」。",
            source="migration",
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


def referenced_schemas(domain: str) -> tuple[str, ...]:
    """该域角色在矩阵里被授过表权限的**全部** schema（去重、稳定排序）。

    为什么必须派生而不是列一份「共享 schema」清单：schema USAGE 是表权限的
    前置条件（只授表不授 USAGE，第一次读写就是 ``permission denied for schema``），
    而它和表权限分属两个 ACL 位置（``pg_namespace.nspacl`` vs ``pg_class.relacl``），
    矩阵里没有任何一条表授权能"顺带"表达它。于是「谁需要 USAGE」只能从
    「他在这张 schema 下有表权限」反推——反推漏掉一个 schema，该域角色就拿不到
    这个共享基础设施，而**表权限还在**，ACL 对账看起来完全正常。

    这正是 R16 三域（production_planning / budget_quota / qc_governance）的历史
    缺陷：它们靠 ``8f3b2c1d4e5a`` 手写的 ``GRANT USAGE`` 覆盖到当时已有的角色，
    而矩阵接管授权面之后，只有 ``project_planning`` 被硬编码补了一句 USAGE，
    新域天生缺 outbox/audit/auth 的 USAGE——写事件即 500。
    """
    return tuple(sorted({grant.schema for grant in DOMAIN_GRANTS[domain]}))


def schema_usage_statements(role: str, domain: str) -> list[str]:
    """该域角色需要的 ``GRANT USAGE ON SCHEMA`` 全集（由 :func:`referenced_schemas` 派生）。

    单独成函数供两处调用：``converge_statements``（迁移与断言）与补 USAGE 的
    修复迁移。两处共用同一份派生，避免「迁移补了 A、断言要求 B」这种第二次分裂。
    """
    return [
        f"GRANT USAGE ON SCHEMA {schema} TO {role}"
        for schema in referenced_schemas(domain)
    ]


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


def _grants_converge(
    role: str,
    grants: tuple[TableGrant, ...],
    *,
    usage_schemas: tuple[str, ...],
    tables: frozenset[str] | None,
    default_privilege_schemas: tuple[str, ...],
) -> list[str]:
    """把一组声明收敛成 SQL 语句（业务域与运维角色共用同一实现）。

    抽出来的唯一理由：两类角色的收敛规则必须**逐字相同**——REVOKE ALL 再按矩阵
    GRANT、默认权限一并撤销、语句顺序稳定。分成两份实现后，「运维角色忘了撤默认
    权限」这种缺口只能靠人去比对两段代码，而它恰好是最难在运行库上发现的一类。

    ``usage_schemas`` 由调用方给出而不是在这里从 ``grants`` 反推：域角色的
    USAGE 口径是 :func:`referenced_schemas`（已被 ACL 对账判据引用），运维角色
    没有域，口径是它被授过表权限的 schema 全集。两套口径各自唯一，不在本函数里
    合成第三套。
    """
    # schema USAGE 是「能访问这些 schema」的前置条件，和表级授权同属授权面：
    # 只授表不授 USAGE，第一次读写就是 permission denied for schema。
    statements: list[str] = [
        f"GRANT USAGE ON SCHEMA {schema} TO {role}" for schema in usage_schemas
    ]
    statements += [
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM {role}"
        for schema in default_privilege_schemas
    ] + [
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
        f"REVOKE ALL ON SEQUENCES FROM {role}"
        for schema in default_privilege_schemas
    ] + [
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM PUBLIC"
        for schema in default_privilege_schemas
    ]
    selected = [
        grant for grant in grants if tables is None or grant.table in tables
    ]
    # 撤销按**表**去重，且全部先于授予。同一张表可能既有整表权限又有列级权限
    # （运维角色的 outbox_events = SELECT 整表 + UPDATE 三列）；若按「每条声明
    # revoke→grant」直译，第二条声明的 ``REVOKE ALL`` 会把第一条刚授的 SELECT
    # 一起抹掉，最终 ACL 与矩阵不符却没人报错——语句逐条看都对。
    for qualified in dict.fromkeys(grant.qualified() for grant in selected):
        statements.append(f"REVOKE ALL ON {qualified} FROM PUBLIC")
        statements.append(f"REVOKE ALL ON {qualified} FROM {role}")
    for grant in selected:
        statements.append(grant.grant_statement(role))
    return statements


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
    return _grants_converge(
        role,
        DOMAIN_GRANTS[domain],
        usage_schemas=referenced_schemas(domain),
        tables=tables,
        default_privilege_schemas=DOMAINS,
    )


def infra_converge_statements(
    role: str, infra: str, *, tables: frozenset[str] | None = None
) -> list[str]:
    """平台运维角色的授权收敛语句（与 :func:`converge_statements` 同规则）。

    与域角色的唯一差别是「默认权限撤销的作用域」：域角色覆盖五个业务 schema，
    运维角色只覆盖它真正被授过表权限的 schema（``outbox``）。多撤不会更安全，
    少撤会让新分区/新表天生带权限——所以按声明派生，而不是抄一份 DOMAINS。
    """
    grants = INFRA_ROLE_GRANTS[infra]
    schemas = tuple(sorted({grant.schema for grant in grants}))
    return _grants_converge(
        role,
        grants,
        usage_schemas=schemas,
        tables=tables,
        default_privilege_schemas=schemas,
    )


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


def legacy_r16_grant_statements(role: str) -> list[str]:
    """仅 downgrade 使用：cutover 之前五张 R16 表的旧授权口径。

    为什么单独一份、而不是让矩阵「再声明一次」：矩阵描述的是**当前**权限面，
    把已迁走的表写回去会让 coverage 判据认为它们还在 project_planning。降级
    需要的是历史口径，两者混在同一个声明里必然有一方是错的——历史口径因此
    显式命名为 legacy，并且只由 ``a0b1c2d3e4f5`` 的 ``downgrade()`` 调用。
    """
    return [
        f"GRANT {privileges} ON {PROJECT_PLANNING}.{table} TO {role}"
        for table, privileges in (
            ("production_routes", "SELECT, INSERT, UPDATE"),
            ("generation_tasks", "SELECT, INSERT, UPDATE"),
            ("shot_budget_requests", "SELECT, INSERT, UPDATE"),
            ("budget_ledger", "SELECT, INSERT"),
            ("gate_decisions", "SELECT, INSERT"),
        )
    ]


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


def validate_infra_matrix() -> list[str]:
    """平台运维角色矩阵的可判定性检查（与 :func:`validate_matrix` 同口径）。

    独立成函数而不是复用 ``validate_matrix``：后者绑定 ``DOMAIN_GRANTS`` 与
    ``DOMAINS``（域 == schema），对没有自有 schema 的角色求值会变成永真式。
    但检查项必须一致——空 write_path、DELETE/TRUNCATE、重复声明、列清单乱序，
    在两类角色上是同一批失效模式，不能因为「这是运维表」就放宽。

    这里额外禁止 ``DELETE``：运维审计表与投递状态列都不允许物理删除，
    一旦有人给它加上 DELETE，审计就从「append-only 事实」退化成「可被清理的日志」。
    """
    problems: list[str] = []
    if set(INFRA_ROLE_GRANTS) != set(INFRA_ROLES):
        problems.append(
            f"运维角色矩阵 {sorted(INFRA_ROLE_GRANTS)} 与 INFRA_ROLES 不一致"
        )
    for infra in INFRA_ROLES:
        seen: set[tuple[str, str, tuple[str, ...] | None]] = set()
        for grant in INFRA_ROLE_GRANTS[infra]:
            key = (grant.schema, grant.table, grant.columns)
            if key in seen:
                problems.append(f"{infra}: {grant.qualified()} 重复声明")
            seen.add(key)
            unknown = grant.privileges - ALL_TABLE_PRIVILEGES
            if unknown:
                problems.append(
                    f"{infra}: {grant.qualified()} 含未知权限 {sorted(unknown)}"
                )
            unusable = grant.privileges - _USABLE_PRIVILEGES
            if unusable:
                problems.append(
                    f"{infra}: {grant.qualified()} 声明了应用用不到的权限 "
                    f"{sorted(unusable)}"
                )
            if "DELETE" in grant.privileges:
                problems.append(
                    f"{infra}: {grant.qualified()} 声明了 DELETE"
                    "（运维角色不得物理删除投递事实或审计行）"
                )
            if not grant.privileges:
                problems.append(f"{infra}: {grant.qualified()} 没有任何权限")
            if grant.source not in {"orm", "migration", "infra", "cross_domain"}:
                problems.append(
                    f"{infra}: {grant.qualified()} 的 source={grant.source!r} 非法"
                )
            if not grant.write_path.strip():
                problems.append(
                    f"{infra}: {grant.qualified()} 未给出真实写入路径"
                    "（说不出谁在写，就不该有写权限）"
                )
            if grant.columns is not None:
                if not grant.columns:
                    problems.append(f"{infra}: {grant.qualified()} 列级授权但没有列清单")
                if list(grant.columns) != sorted(set(grant.columns)):
                    problems.append(
                        f"{infra}: {grant.qualified()} 的列清单未按字典序去重"
                    )
                if len(grant.privileges) > 1:
                    problems.append(
                        f"{infra}: {grant.qualified()} 列级授权声明了多项权限 "
                        f"{sorted(grant.privileges)}"
                    )
    return problems


MAINTENANCE_SCHEMA = "qc_maintenance"
MAINTENANCE_GUARD = f"{MAINTENANCE_SCHEMA}.assert_session_identity()"
MAINTENANCE_SESSION = f"{MAINTENANCE_SCHEMA}.assert_session_member()"
MAINTENANCE_ENUMERATE = f"{MAINTENANCE_SCHEMA}.enumerate_due_tasks()"
MAINTENANCE_DETECT = f"{MAINTENANCE_SCHEMA}.detect_projection_anomalies()"
MAINTENANCE_RECONCILE = f"{MAINTENANCE_SCHEMA}.reconcile_tenant_tasks(text)"
MAINTENANCE_DENIAL = f"{MAINTENANCE_SCHEMA}.record_maintenance_denial(text, text, text)"
MAINTENANCE_HELPER = f"{MAINTENANCE_SCHEMA}._projection_anomalies(uuid)"
SECDEF_SIGNATURE = "audit.assert_signature_event_exists()"

# 白名单：维护身份**只能**经这些函数触达数据库，且函数数量本身就是边界——
# 多一个函数就多一个可被调用的能力，所以这里用元组而不是「按前缀匹配」。
MAINTENANCE_COMMAND_FUNCTIONS: tuple[str, ...] = (
    MAINTENANCE_SESSION,
    MAINTENANCE_ENUMERATE,
    MAINTENANCE_DETECT,
    MAINTENANCE_RECONCILE,
    MAINTENANCE_DENIAL,
)


def maintenance_membership_statements() -> list[str]:
    """维护身份的成员关系（同样只有矩阵能声明）。

    两句都做成**无条件**语句而不是 ``DO $$ IF EXISTS … $$`` 包裹：PG 里重复
    ``GRANT <组> TO <成员>`` 是幂等的，对非成员 ``REVOKE`` 也只发一条 warning，
    所以「重复执行」不需要条件分支；而条件分支会把语句变成一段 plpgsql，
    闸门就无法按「逐条授权语句」比对，只能退化成模糊匹配。
    """
    from packages.shared.db_roles import maintenance_role

    operator = maintenance_role("operator")
    return [
        f"GRANT {operator} TO {maintenance_role('login')}",
        f"REVOKE {operator} FROM {maintenance_role('worker')}",
    ]


def maintenance_identity_statements() -> list[str]:
    """维护身份（``operator`` / ``login``）到达守卫所需的授权，与 SECDEF 函数收紧。

    为什么不在迁移里直接写这几句：授权面只有一个执行器（矩阵），迁移只是它的
    执行者。让维护授权走这里，判据才能把「矩阵声明」与「迁移实际执行」逐条
    比对；写在迁移里就变成第二份事实源，且下一次有人往迁移补一句 GRANT 时
    闸门再也拦不住。

    只给 ``operator`` / ``login``：``qc_worker`` 是常规质检 Worker，它在维护
    schema 里没有任何可达路径——这条边界靠授权面表达，而不是靠调用方自觉。
    """
    from packages.shared.db_roles import maintenance_role

    operator = maintenance_role("operator")
    login = maintenance_role("login")
    return [
        f"GRANT USAGE ON SCHEMA {MAINTENANCE_SCHEMA} TO {operator}",
        f"GRANT USAGE ON SCHEMA {MAINTENANCE_SCHEMA} TO {login}",
        f"REVOKE ALL ON FUNCTION {MAINTENANCE_GUARD} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {MAINTENANCE_GUARD} TO {operator}",
        f"GRANT EXECUTE ON FUNCTION {MAINTENANCE_GUARD} TO {login}",
        f"REVOKE ALL ON FUNCTION {SECDEF_SIGNATURE} FROM PUBLIC",
    ]


def maintenance_command_statements() -> list[str]:
    """三个只读维护命令 + 会话成员判定的授权面（WP-A 第一阶段）。

    只读阶段**刻意不授予任何写权限**：
    * 对 ``qc_governance.qc_tasks`` 只有 ``SELECT``——检测/对账只读源表；
    * 不给 ``UPDATE``/``DELETE``/``TRUNCATE``，也不给 PG17 的 ``MAINTAIN``。
      修复命令（``repair_tenant_task``）落地时才按 PG 版本补锁权限，届时本函数
      会多一段「版本适配」的语句，而不是在今天就提前扩大权限面。

    由于三条命令都是 SECURITY DEFINER（owner 执行），owner 必须能真正读到
    ``qc_tasks`` 与投影：它需要 schema USAGE + 表 SELECT，而 FORCE RLS 让它仍
    被策略约束——所以迁移里另加「仅对维护 owner 开放」的读取策略，而不是
    给它 BYPASSRLS。
    """
    from packages.shared.db_roles import maintenance_role

    owner = maintenance_role("owner")
    operator = maintenance_role("operator")
    login = maintenance_role("login")
    statements = [
        f"GRANT USAGE ON SCHEMA {QC_GOVERNANCE} TO {owner}",
        f"GRANT SELECT ON TABLE {QC_GOVERNANCE}.qc_tasks TO {owner}",
        # 内部取数函数是对外命令的实现细节，不构成能力：REVOKE 而不 GRANT。
        f"REVOKE ALL ON FUNCTION {MAINTENANCE_HELPER} FROM PUBLIC",
    ]
    for function in MAINTENANCE_COMMAND_FUNCTIONS:
        statements.append(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        statements.append(f"GRANT EXECUTE ON FUNCTION {function} TO {operator}")
        statements.append(f"GRANT EXECUTE ON FUNCTION {function} TO {login}")
    return statements


def maintenance_identity_restore_statements() -> list[str]:
    """``downgrade`` 用：把被 WP-03 收紧的 PUBLIC 执行权放回原状。

    ``assert_session_identity`` 不进这里——downgrade 会把它删掉，给一个即将
    消失的函数授 PUBLIC 执行权没有意义。真正需要复原的只有历史函数的默认口径。
    """
    return [f"GRANT EXECUTE ON FUNCTION {SECDEF_SIGNATURE} TO PUBLIC"]


__all__ = [
    "ALL_TABLE_PRIVILEGES",
    "ASSET_SERVICE",
    "BUDGET_QUOTA",
    "DOMAINS",
    "DOMAIN_GRANTS",
    "INFRA_ROLES",
    "INFRA_ROLE_GRANTS",
    "MAINTENANCE_COMMAND_FUNCTIONS",
    "MAINTENANCE_DENIAL",
    "MAINTENANCE_DETECT",
    "MAINTENANCE_ENUMERATE",
    "MAINTENANCE_GUARD",
    "MAINTENANCE_HELPER",
    "MAINTENANCE_RECONCILE",
    "MAINTENANCE_SCHEMA",
    "MAINTENANCE_SESSION",
    "OUTBOX_OPS",
    "PRODUCTION_PLANNING",
    "PROJECT_PLANNING",
    "QC_GOVERNANCE",
    "SECDEF_SIGNATURE",
    "TableGrant",
    "business_tables",
    "converge_statements",
    "declared_column_privileges",
    "declared_privileges",
    "declared_tables",
    "infra_converge_statements",
    "legacy_r16_grant_statements",
    "legacy_statements",
    "maintenance_command_statements",
    "maintenance_identity_restore_statements",
    "maintenance_identity_statements",
    "maintenance_membership_statements",
    "validate_infra_matrix",
    "validate_matrix",
]
