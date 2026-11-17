"""生成执行域：shot_route_selections.route_id 外键（引用完整性下沉）

Revision ID: a9f1c2e3d4b5
Revises: f1a2b3c4d5e6
Create Date: 2026-11-17

背景（再次审查 P2-3）：
``shot_route_selections.route_id`` 此前是普通字符串列，存在性只靠应用层
"选路前先查路线"兜住。任何绕过服务层的写入（运维脚本、缺陷、未来新增的
批量导入）都能造出"生效路线指向不存在的路线"——而生效路线是**受理事实**，
它指向的对象必须真实存在，否则"该镜头已选路"这个判断本身就是假的。

为什么这一条必须下沉数据库：
- 服务层校验只覆盖经过服务的路径。生效路线不可 UPDATE/DELETE（守卫触发器），
  一旦写进一条幽灵引用就**永久无法修正**（不能改、不能删）——所以事前结构
  约束比事后校验重要得多；
- 同 schema（generation_execution 内 production_routes），成本几乎为零。

迁移策略：
1. 先做**孤儿行预检**，有孤儿就明确失败并给处置提示——不用 ``NOT VALID``
   蒙过去（那会把"约束不存在"伪装成"约束存在"，下一个人以为有保护）。
2. 加 FK，``ON DELETE RESTRICT``：路线不可物理删除（生产路线的授权口径本就
   没有 DELETE），这里把"不可删"从权限层再钉一层。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9f1c2e3d4b5"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "generation_execution"
TABLE = "shot_route_selections"
ROUTE_TABLE = "production_routes"
FK_NAME = "fk_generation_execution_shot_route_selections_route_id"


def _assert_no_orphans() -> None:
    """存在指向不存在路线的生效记录时明确失败。

    这类数据只能来自"绕过服务层的写入"（本表的守卫触发器禁止 UPDATE/DELETE，
    正常路径不可能产生）。修数据必须由人决定，不能由迁移悄悄删——删掉的是
    一条"某镜头走过某路线"的受理历史。
    """
    conn = op.get_bind()
    orphans = conn.execute(
        sa.text(
            f"SELECT count(*) FROM {SCHEMA}.{TABLE} s "
            f"LEFT JOIN {SCHEMA}.{ROUTE_TABLE} r ON r.entity_id = s.route_id "
            "WHERE r.entity_id IS NULL"
        )
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"{SCHEMA}.{TABLE} 有 {orphans} 行 route_id 指向不存在的路线，"
            "外键无法建立。处置：先人工确认这些记录（多为测试/播种残留），"
            "按 runbook 在维护窗口内清理后再执行迁移——迁移不会替你删除受理事实。"
        )


def upgrade() -> None:
    _assert_no_orphans()
    op.create_foreign_key(
        FK_NAME,
        TABLE,
        ROUTE_TABLE,
        ["route_id"],
        ["entity_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(FK_NAME, TABLE, type_="foreignkey", schema=SCHEMA)
