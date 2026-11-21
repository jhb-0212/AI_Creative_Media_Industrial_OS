"""批次八：把 `scenes` 的表级权限收敛到授权矩阵（DATA-6）。

要被削掉的那条路
---------------
批次五（``i9d0e1f2a3b4``）建 `scenes` 时顺手手写了三条 `GRANT`：

    svc_project_planning  → SELECT, INSERT, UPDATE, DELETE
    afs_audit             → SELECT
    afs_data_repair       → SELECT, UPDATE

其中那条 `DELETE` 与授权矩阵（`packages/shared/db_grants.py::DOMAIN_GRANTS`）
不一致——矩阵对 `episodes` / `script_revisions` / 本表都只声明
`SELECT INSERT UPDATE`，理由是**本域没有任何 `*.DELETE` 动作**：集数/场次/剧本
的终结语义是 `ARCHIVED`，物理删除会打断下游归属链。

为什么不能「改一下批次五」
------------------------
迁移是时间胶囊。批次五已经应用在目标库与所有克隆上，改它的正文会让「源」与
「已执行的库」出现两个版本，而 `alembic` 不会重跑它——于是干净安装得到新结果、
已升级的库保留旧结果，两边都自称「批次五」。矩阵声明的执行职责因此属于
**新迁移**，这也是 `c1a2b3d4e5f6` 文档里写死的口径。

为什么用 `converge_statements` 而不是手写 GRANT
--------------------------------------------
`data.role_grants_least_privilege` 这个断言要求「授权面 == 矩阵声明」，判据的
实现方式是**回放所有授权迁移、取语句并集，与矩阵渲染结果比对**。手写 GRANT
即使这一次刚好与矩阵相等，下一次矩阵调整时它就会静默偏离——而偏离的表现是
「某张表多了一项权限」，没有任何报错。所以这里只调生成器，不写 DDL。

`tables=frozenset({"scenes"})` 是**本迁移的冻结范围**：只处理 scenes。
不传这个参数会让本迁移在未来矩阵新增表时去 `REVOKE ALL` 一张尚未建出来的表。

降级为何不是对称的 REVOKE
------------------------
升级前 scenes 的 ACL 是「批次五手写的结果」，其中包含 DELETE。降级要回到**那个
状态**，不是回到「没有权限」——后者会让降级后的实例上 SCENE.CREATE 直接 42501，
看起来像代码坏了。故取 `legacy_statements`（矩阵的逆）并按表名过滤，只回滚
本迁移改过的那一张表；全域回滚会把 episodes / script_revisions 的权限一并放大，
那是降级路径上的权限扩散，比不降级更危险。
"""

from __future__ import annotations

from alembic import op

from packages.shared.db_grants import converge_statements, legacy_statements
from packages.shared.db_roles import domain_role

revision = "l2a3b4c5d6e7"
down_revision = "k1f2a3b4c5d6"
branch_labels = None
depends_on = None

DOMAIN = "project_planning"
#: 本迁移只处理这一张表。新增表请另开迁移，不要把表名加到这里——
#: 加了就会让「那时该做什么」随今天的需求漂移。
TARGET_TABLE = "scenes"
#: 本迁移的冻结范围（`converge_statements` 的参数形态）。
TABLES = frozenset({TARGET_TABLE})


def upgrade_steps() -> list[str]:
    """升级语句序列（**唯一事实源**）。

    与既有七个批次同一形态：``upgrade()`` 只把本函数的结果逐条交给
    ``op.execute``，验证脚本 import 的也是这一份。若在这里另写一遍 DDL，
    「验证的东西」与「迁移执行的东西」就成了两份，改动时必然漂移。
    """
    role = domain_role(DOMAIN)
    return converge_statements(role, DOMAIN, tables=TABLES)


def downgrade_steps() -> list[str]:
    """降级语句序列：回到批次五手写授权的结果（含 DELETE）。

    按 ``.<表名>`` 过滤 ``legacy_statements``：只回滚本迁移改过的表。全量回滚
    会把同域其它表的权限放大成「全 DML」，那是降级路径上的权限扩散。
    """
    role = domain_role(DOMAIN)
    marker = f".{TARGET_TABLE}"
    return [
        statement
        for statement in legacy_statements(role, DOMAIN)
        if marker in statement
    ]


def upgrade() -> None:
    """把 scenes 的域角色权限收敛到矩阵声明。"""
    for statement in upgrade_steps():
        op.execute(statement)


def downgrade() -> None:
    """回到批次五手写授权的结果（含 DELETE）。"""
    for statement in downgrade_steps():
        op.execute(statement)
