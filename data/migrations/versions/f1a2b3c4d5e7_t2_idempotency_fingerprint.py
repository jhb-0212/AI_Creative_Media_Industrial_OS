"""R42 T2 幂等冲突指纹（PR-F）。

问题
----
回执的幂等键 ``(tenant_id, generation_task_id)`` 只回答「有没有来过」，
不回答「是不是同一个请求」。同一任务收到不同 SHA / 项目 / locator 的请求
时，handler 早返回 ACK，把内容冲突洗成正常重复（根因报告 G-1）。

改动
----
给 ``asset_registration_receipts`` 加 ``registration_fingerprint`` 列：
规范化请求的确定性 SHA-256（见 packages.shared.registration_fingerprint）。

为什么不回填存量行
------------------
回执是不可变事实，行级触发器禁止一切 UPDATE/DELETE；回填 = UPDATE，
会被守卫拒绝。新列 nullable，存量行保持 NULL——它们在指纹机制上线前创建，
没有可比的原始请求语义，这是事实而非缺口。新回执由应用层强制带指纹。
不写 DEFAULT：伪默认值会让「没指纹」看起来像「有指纹」。
冲突证据不新建表：发 ``ASSET_REGISTRATION_CONFLICT`` 事件，同事务写入，
事件永久留在 append-only 的 outbox.outbox_events（无 DELETE 授权），
并由专用 no-op handler 正常 ack（不抬高 backlog、不被 webhook 误发）。

Revision ID: f1a2b3c4d5e7
Revises: e1f2a3b4c5d6
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "asset_service"
_TABLE = "asset_registration_receipts"
_COLUMN = "registration_fingerprint"
_CK = "ck_asset_registration_receipts_fingerprint"


def upgrade() -> None:
    # 表名与 schema 必须分开传：把 "asset_service.asset_registration_receipts"
    # 作为一个字符串传给 op.add_column，alembic 会整体加引号当成单张表，
    # 生成 ALTER TABLE "asset_service.asset_registration_receipts"（不存在）。
    # ALTER TABLE ADD COLUMN 是 DDL，不触发 BEFORE UPDATE/DELETE 的行级不可变守卫。
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(64), nullable=True),
        schema=_SCHEMA,
    )
    # 64 位十六进制或 NULL（存量行无指纹）。用语义化 CHECK 名而非 sa.CheckConstraint
    # 的列绑定形式，表达式直接写列名即可。
    op.execute(
        f"ALTER TABLE {_SCHEMA}.{_TABLE} ADD CONSTRAINT {_CK} "
        f"CHECK ({_COLUMN} IS NULL OR {_COLUMN} ~ '^[0-9a-f]{{64}}$')"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} DROP CONSTRAINT {_CK}")
    op.drop_column(_TABLE, _COLUMN, schema=_SCHEMA)
