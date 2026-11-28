"""资产内容列不可变：用数据库触发器兜底（ADR-R11）

Revision ID: d1e2f3a4b5c6
Revises: b7c8d9e0f1a2
Create Date: 2026-11-14

背景（审计 P1-4）：
- 仓储层 `save()` 曾把「除主键与 entity_version 外所有列」整列回写，每次状态
  迁移都会把 sha256/storage_ref 等一起 UPDATE。语句层没有白名单，任何一次
  手误都可能原地改写内容，而"不可变"只是文档里的声明。
- 授权层只约束得到 app_role；迁移角色/运维直连仍能改。声明要成立，必须在
  结构层拦住"谁都能改"的那条路。

本迁移做两件事：
1. `asset_versions`：BEFORE UPDATE/DELETE 逐列比对，除 status/entity_version/
   updated_at/audit_ref 之外的列一旦变化即拒绝；整行 DELETE 拒绝（版本只能新增）。
2. `qc_reports`：追加型证据表，BEFORE UPDATE/DELETE 一律拒绝。

为什么用触发器而不是列级授权：
- 列级 REVOKE 只对特定角色生效，超级用户/迁移角色照样能改；
- 触发器对所有角色生效（含超级用户），且新加内容列自动纳入保护（逐列扫描
  pg_attribute，不维护列清单，不会随模型加列而漏保护）。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_GUARD_ASSET_VERSIONS = """
CREATE OR REPLACE FUNCTION asset_service.asset_versions_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    col text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'asset_versions 是追加型不可变表，禁止删除（ADR-R11）';
    END IF;

    -- 逐列扫描当前表的真实列，新增内容列自动纳入保护，不依赖手写清单。
    FOR col IN
        SELECT attname
        FROM pg_attribute
        WHERE attrelid = 'asset_service.asset_versions'::regclass
          AND attnum > 0
          AND NOT attisdropped
    LOOP
        IF col NOT IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
            IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                RAISE EXCEPTION
                    'asset_versions.% 为不可变内容列，禁止改写（ADR-R11）', col;
            END IF;
        END IF;
    END LOOP;

    RETURN NEW;
END;
$$;
"""

_GUARD_QC_REPORTS = """
CREATE OR REPLACE FUNCTION asset_service.qc_reports_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'qc_reports 是追加型检测证据，禁止 %（ADR-R11）', TG_OP;
END;
$$;
"""

_CREATE_TRIGGERS = (
    "DROP TRIGGER IF EXISTS trg_asset_versions_guard ON asset_service.asset_versions",
    """
CREATE TRIGGER trg_asset_versions_guard
    BEFORE UPDATE OR DELETE ON asset_service.asset_versions
    FOR EACH ROW EXECUTE FUNCTION asset_service.asset_versions_guard()
""",
    "DROP TRIGGER IF EXISTS trg_qc_reports_guard ON asset_service.qc_reports",
    """
CREATE TRIGGER trg_qc_reports_guard
    BEFORE UPDATE OR DELETE ON asset_service.qc_reports
    FOR EACH ROW EXECUTE FUNCTION asset_service.qc_reports_guard()
""",
)

_DROP_ALL = (
    "DROP TRIGGER IF EXISTS trg_asset_versions_guard ON asset_service.asset_versions",
    "DROP TRIGGER IF EXISTS trg_qc_reports_guard ON asset_service.qc_reports",
    "DROP FUNCTION IF EXISTS asset_service.asset_versions_guard()",
    "DROP FUNCTION IF EXISTS asset_service.qc_reports_guard()",
)


def upgrade() -> None:
    # asyncpg 的 prepared statement 一次只接受一条命令，必须逐条执行
    # （函数体内部的 $ 引号不影响：整条 CREATE FUNCTION 仍是一条语句）。
    for sql in (_GUARD_ASSET_VERSIONS, _GUARD_QC_REPORTS):
        op.execute(sql)
    for sql in _CREATE_TRIGGERS:
        op.execute(sql)


def downgrade() -> None:
    for sql in _DROP_ALL:
        op.execute(sql)
