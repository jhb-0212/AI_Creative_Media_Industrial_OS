"""ORM 层公共实体信封 mixin（C.3.1 的持久化单一事实源，跨模块共享）。

为什么上提到 packages/shared：
- 资产域（asset_service）等新模块同样需要 C.3.1 信封列。若继续从
  project_planning 反向导入，模块间依赖方向就被破坏（资产域依赖规划域）。
- 信封列是**同一套映射机制**，只能有一份实现，否则字段长度/类型会漂移。

业务列仍由各模块自己的模型声明；本 mixin 只负责信封与 tenant_id。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKeyConstraint, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

# 契约 C.3.1 通用实体信封的 schema_version 固定值（V2.0.5）。
SCHEMA_VERSION = 205

# 归属根：每个域表的 (project_id, tenant_id) 最终都指向这一张表。
# 写成一处是因为它是**平台级事实**——各域手抄 ``project_planning.projects``
# 时一旦哪处抄错（schema 名或表名），得到的是一个指向不存在表的约束，
# 迁移期报错还好，最怕的是同一事实在两个域里被写成两种目标。
OWNERSHIP_ROOT = "project_planning.projects"


def project_tenant_fk(table: str) -> ForeignKeyConstraint:
    """把「项目真实 ∧ 租户一致」压成一条数据库事实（域表归属外键的统一构造）。

    为什么每个域表都要一条，而不是只靠父链
    -------------------------------------
    ``scenes.project_id`` 经 ``episodes`` 到 ``projects`` 的父链只保证
    「同一个 project_id」，**不保证**「本行的 tenant_id 等于该项目的租户」；
    单列外键 ``project_id -> projects.entity_id`` 同样只证明项目存在。

    于是这一行可以合法存在：

        episodes(project_id=P_A, tenant_id=T_B)   -- P_A 实际属于 T_A

    而 ``tenant_id`` 是 RLS 的作用域键——写错租户不是「数据脏一点」，是这个
    实体出现在它不属于的租户视图里。所以本条约束守的不是引用完整性，是
    **归属真实性**：目标键 ``uq_projects_entity_tenant`` 存在 ⇒
    (project_id, tenant_id) 这一对必须真实指向某个项目行，两列不能各自
    来自不同项目。

    为什么不需要新增列
    ------------------
    ``project_id`` 与 ``tenant_id`` 本来就在每张域表上（信封列）。本函数只是
    把已经存下来的事实约束住，不是引入冗余建模。

    为什么约束名由表名派生
    ----------------------
    历史上三张表的单列外键分别叫 ``fk_episodes_project`` /
    ``fk_rework_tasks_project`` / ``fk_shot_draft_project``——最后一个的表名
    其实是 ``shot_breakdown_drafts``。名字手抄必然漂移，而漂移后的名字会让
    「按名字找约束」的审计脚本静默漏项。派生名消除这种可能。

    复合外键**蕴含**单列外键（前者成立时后者必成立），因此调用方应当用它
    **替换**单列外键而不是并存：两条约束守同一个事实是两份事实源，且每次
    写入多一次索引查找。``asset_versions`` / ``constraint_packs`` 早已是
    「只有复合、没有单列」的形态，本函数把那个范式收敛到一处。
    """
    return ForeignKeyConstraint(
        ["project_id", "tenant_id"],
        [f"{OWNERSHIP_ROOT}.entity_id", f"{OWNERSHIP_ROOT}.tenant_id"],
        name=f"fk_{table}_project_tenant",
    )


class EntityMixin:
    """C.3.1 通用实体信封列 + 平台级 tenant_id（供各领域 ORM 模型继承）。

    注意：本 mixin 不含 __tablename__ / __table_args__，由具体模型声明，
    避免与 SQLAlchemy 声明式基类的 schema 配置冲突。

    tenant_id（决策 5）：多租户行级隔离键，Uuid 类型，配合 PostgreSQL RLS
    策略 ``USING (tenant_id = current_setting('app.current_tenant_id')::uuid)``
    在数据库层按死越权。
    """

    entity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_module: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    audit_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
