"""PR-G：ArtifactManifest 增加 media_type 落盘事实。

问题
----
T0 的写入意图只固化 locator / sha256，media_type 仅存在于 T1 事件载荷。事件丢失
或被盗改时没有可比的持久事实；崩溃恢复也无法在重新调用模型前判断该落什么类型。

改动
----
给 ``production_planning.artifact_manifests`` 增加 NOT NULL 的 ``media_type`` 列：

- 新行由 T0 直接写入（应用层保证非空，对应领域对象 ``ArtifactManifest.media_type``）；
- 存量行从 ``locator`` 扩展名回填——受控定位符的扩展名与 media_type 是双射
  （见 ``packages/shared/storage_locator.py::EXT_BY_MEDIA_TYPE``），回填无损；
- 回填后 ``SET NOT NULL``：任何回填不出的行都会让迁移显式失败，而不是留下
  「类型未知」的意图供恢复流程误用（fail-closed）。

方言
----
``ALTER TABLE ... ADD COLUMN`` 与 ``ALTER COLUMN ... SET NOT NULL`` 都是 DDL，
不触发 ``artifact_manifests`` 的行级状态守卫（该表不是不可变表，允许 PREPARED→
STORED_VERIFIED/ABANDONED 的正常 UPDATE）。

Revision ID: a7b8c9d0e1f2
Revises: e4f5a6b7c8d9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "production_planning"
_TABLE = "artifact_manifests"
_COLUMN = "media_type"

# 扩展名 → media_type 的回填映射，必须与 storage_locator.EXT_BY_MEDIA_TYPE 等价。
# 写成 SQL 常量而不是运行期 import：迁移是历史快照，不该随代码演进改变语义。
_BACKFILL = """
UPDATE {schema}.{table}
SET media_type = CASE
    WHEN locator LIKE '%.png'  THEN 'image/png'
    WHEN locator LIKE '%.jpg'  THEN 'image/jpeg'
    WHEN locator LIKE '%.webp' THEN 'image/webp'
    WHEN locator LIKE '%.mp4'  THEN 'video/mp4'
    WHEN locator LIKE '%.webm' THEN 'video/webm'
    WHEN locator LIKE '%.wav'  THEN 'audio/wav'
    WHEN locator LIKE '%.bin'  THEN 'application/octet-stream'
END
WHERE media_type IS NULL
"""


def upgrade() -> None:
    # 先加可空列，回填后再收紧为 NOT NULL。表名与 schema 分开传：把
    # "production_planning.artifact_manifests" 作为单字符串会让 alembic 整体加引号。
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(64), nullable=True),
        schema=_SCHEMA,
    )
    op.execute(_BACKFILL.format(schema=_SCHEMA, table=_TABLE))
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(64),
        nullable=False,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN, schema=_SCHEMA)
