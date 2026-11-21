"""归属外键补齐：organizations 目标键 + projects 复合唯一键 + 14 条归属外键（NOT VALID）

【本文件尚未并入任何 Alembic 链，原因见 README】
放在 ``data/migrations/pending/`` 而非 ``versions/``，是因为本迁移的目标谱系是
**分支链**（``down_revision = c4e5f6a7b8d9``），而当前工作区 checkout 的是 main
（``c1a2b3d4e5f6``）。把 down_revision 指向分支 head 的文件放进 ``versions/``
会立刻在主支上造出第二个 head，使 ``alembic upgrade head`` 变成二义性命令。
因此：**代码已写完并在真实克隆库上跑通 upgrade/downgrade**，但**不激活**。

根因（不是「缺几条外键」，是「没有可引用的目标键」）
----------------------------------------------------
第三轮系统性审查把问题描述为「14 张表零归属约束」，容易误判成「数据脏、加约束失败」。
T3 实验（E13，49 步）证明的顺序恰恰相反：

  A0  在**空表**上建 ``(project_id, tenant_id)`` 外键 → 直接 ``42830``
      （there is no unique constraint matching given keys for referenced table）
  B2  先补 ``uq_projects_entity_tenant``，同一语句即成功

即：**第一条 ADD CONSTRAINT 就会失败，且与数据无关**。所以补齐迁移的第一步是建
4 个目标键（organizations 表本身 + 3 个唯一约束），不是洗数据。

第二个根因：分支把 ``organization_id`` 冗余存在每张域资源上，却没有把「组织一致」
纳入任何约束。两列外键 ``(project_id, tenant_id)`` 看不见 ``organization_id``——
T3 场景 E1/E2 实测：一行「项目属 O1、资源写 O2」的错配数据存在时，两列外键
**照样建成**（``convalidated=t``），换成三列立即 ``23503``。所以本迁移对 A 类
项目级表一律用**三列**外键，把「组织错配」从「写入无人拦」变成「写入即失败」。

为什么全部 ``NOT VALID``
------------------------
``NOT VALID`` 不扫描历史行，新写入照样受约束。这是唯一能在「存量可能脏、且脏行
的归属列是不可变列（ADR-R11，UPDATE 触发 P0001）」的前提下把约束先立起来的做法。
但必须诚实说明它的边界：

  - 它**不是豁免**：``VALIDATE CONSTRAINT`` 才是闭环判据；
  - 它**挡不住 23502**（NULL 进 NOT NULL 列），也挡不住已存在行继续违规；
  - 只要有一行 ``legacy-unassigned`` 哨兵行，``VALIDATE`` 必定 ``23503``（T3 F9）。

因此本迁移**只负责「把约束立起来并保持新写入干净」**，存量修复与 ``VALIDATE``
是独立的一步，写在 ``docs/audits/2026-11-20-ownership-repair-path-decision.md``
的维护窗口 runbook 里，不塞进迁移（数据修复不可回滚，迁移必须可回滚）。

范围与裁剪（不做无条件的「14 张表全加三列外键」）
--------------------------------------------------
- **A 项目级（11 张）**：三列外键 ``(project_id, tenant_id, organization_id)``。
- **B 组织级（1 张）**：``production_routes`` 无 ``project_id``，两列外键到 organizations。
  它被多项目共享，补 project_id 会破坏设计，故不补。
- **C 派生级（2 张）**：``shot_specs`` / ``approval_steps`` 的归属应由更近的父对象
  （revision / ticket）派生。给它们的 ``project_id`` 建外键会固化「两套归属可独立
  漂移」——正是 ``qc_reports`` 的旧问题。故本轮**不给它们建归属外键**，父引用外键
  属另一条线（业务引用），按「归属先立、引用后补」不混入。
- **矩阵 14 表之外多纳入 1 张**：``asset_service.asset_versions``。它在分支侧同为
  项目级、同样带 ``organization_id``、同样 0 外键；且 T3 正是以它为代表性表。
  留着它不补会造成「实验证明它有问题、迁移却绕过它」。**单独成组，可整组删除。**

Revision ID: a9b8c7d6e5f4
Revises: c4e5f6a7b8d9
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9b8c7d6e5f4"
down_revision: str | Sequence[str] | None = "c4e5f6a7b8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLANNING = "project_planning"
PROJECTS = f"{PLANNING}.projects"
ORGS = f"{PLANNING}.organizations"

#: A 类项目级表：(schema, table)。三列外键的目标。
#: 顺序固定，便于快照逐行对照；不要按字母重排，否则 diff 噪声掩盖真实变更。
PROJECT_LEVEL: tuple[tuple[str, str], ...] = (
    ("generation_execution", "generation_tasks"),
    ("generation_execution", "shot_route_selections"),
    ("shot_planning", "shot_drafts"),
    ("shot_planning", "shot_revisions"),
    ("shot_planning", "shot_visual_plans"),
    ("shot_planning", "constraint_packs"),
    ("asset_service", "delivery_manifests"),
    ("asset_service", "asset_versions"),
    ("qc_governance", "gate_decisions"),
    ("qc_governance", "qc_review_sessions"),
    ("workflow", "approval_flows"),
    ("workflow", "approval_tickets"),
)

#: B 类组织级表：只有 (organization_id, tenant_id)，两列外键的目标。
ORG_LEVEL: tuple[tuple[str, str], ...] = (("generation_execution", "production_routes"),)

#: C 类派生级表：本轮**刻意不加**归属外键，列在此处是为了让「没做什么」可被复核。
DERIVED_NO_FK: tuple[tuple[str, str], ...] = (
    ("shot_planning", "shot_specs"),
    ("workflow", "approval_steps"),
)


class Step(NamedTuple):
    """一步一条语句。

    刻意限制为「单语句」：多语句串在一个 ``op.execute`` 里时，任一句失败会让整批
    回滚，无法判断到底哪一句失败——T3 期间踩过这个坑（当时用字符串拼接多条 DDL）。

    **用 ``NamedTuple`` 而非 ``@dataclass`` 是有原因的**：Alembic 用
    ``importlib.util.module_from_spec`` 加载迁移却不先登记 ``sys.modules``，
    dataclass 在解析注解时会回查 ``sys.modules[cls.__module__]``，拿到 ``None``
    后抛 ``AttributeError``。换言之，带 ``@dataclass`` 的迁移**无法被 alembic 加载**。
    验证脚本当时手动补了 ``sys.modules[name] = mod`` 绕过了它，于是「验证 13/13 通过」
    掩盖了「``alembic upgrade`` 根本读不进来」——这是「测试绿、生产不可用」的典型。
    ``NamedTuple`` 是 ``tuple`` 子类，构造与属性访问方式不变，但不触发上述回查。
    """

    code: str
    title: str
    sql: str
    note: str = ""


def _fk_project(table: str) -> str:
    return f"fk_{table}_project_tenant_org"


def _fk_org(table: str) -> str:
    return f"fk_{table}_organization_tenant"


def upgrade_steps() -> list[Step]:
    """按阶段生成全部 upgrade 语句（唯一事实源：迁移与验证脚本共用此函数）。"""
    steps: list[Step] = []

    # ── 阶段 1：organizations 目标键 ────────────────────────────────────
    steps.append(
        Step(
            "O0",
            "前置断言：同一组织不得对应多个租户",
            f"""
            DO $$
            DECLARE bad integer;
            BEGIN
                SELECT count(*) INTO bad FROM (
                    SELECT organization_id FROM {PROJECTS}
                    GROUP BY organization_id HAVING count(DISTINCT tenant_id) > 1
                ) t;
                IF bad > 0 THEN
                    RAISE EXCEPTION
                        'PREFLIGHT: % 个 organization_id 在 projects 中对应多个 tenant_id；'
                        'organization_id 是 organizations 的主键，语义上无法判定该组织属于哪个租户，迁移中止',
                        bad;
                END IF;
            END $$;
            """,
            "fail-closed：不猜、不取第一个，直接中止",
        )
    )
    steps.append(
        Step(
            "O1",
            "建 organizations 表（列与约束对齐 main 实测量，不自行增列）",
            f"""
            CREATE TABLE IF NOT EXISTS {ORGS} (
                organization_id uuid NOT NULL,
                tenant_id       uuid NOT NULL,
                name            character varying(200) NOT NULL,
                created_at      timestamp with time zone NOT NULL,
                created_by      character varying(64) NOT NULL,
                updated_at      timestamp with time zone NOT NULL,
                CONSTRAINT organizations_pkey PRIMARY KEY (organization_id)
            )
            """,
            "分支侧整张表不存在（B1）；列集合取自 main 15443 的 \\d 实测",
        )
    )
    steps.append(
        Step(
            "O2",
            "organizations.tenant_id 索引",
            f"CREATE INDEX IF NOT EXISTS ix_project_planning_organizations_tenant_id "
            f"ON {ORGS} (tenant_id)",
            "与 main 同名同列，便于跨库对照",
        )
    )
    steps.append(
        Step(
            "O3",
            "organizations 复合唯一键（两列外键的被引用键）",
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_organizations_owner_tenant'
                      AND conrelid = '{ORGS}'::regclass
                ) THEN
                    ALTER TABLE {ORGS}
                        ADD CONSTRAINT uq_organizations_owner_tenant
                        UNIQUE (organization_id, tenant_id);
                END IF;
            END $$;
            """,
            "PG 要求复合外键的被引用列组合由 UNIQUE 精确覆盖",
        )
    )
    steps.append(
        Step(
            "O4",
            "把 projects 已声明的组织物化为 organizations 行",
            f"""
            INSERT INTO {ORGS} (organization_id, tenant_id, name, created_at, created_by, updated_at)
            SELECT DISTINCT
                   organization_id,
                   tenant_id,
                   '(unnamed) ' || organization_id::text,
                   now(),
                   '(migration) {revision}',
                   now()
            FROM {PROJECTS}
            ON CONFLICT (organization_id) DO NOTHING
            """,
            "只把已有的 organization_id 提升为独立行，不新增归属事实；"
            "name 用可识别的占位值（分支从未存过组织名），登记为债务而非伪造业务名称",
        )
    )

    # ── 阶段 2：projects 目标键 ─────────────────────────────────────────
    steps.append(
        Step(
            "K1",
            "projects 两列唯一键 (entity_id, tenant_id)",
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_projects_entity_tenant'
                      AND conrelid = '{PROJECTS}'::regclass
                ) THEN
                    ALTER TABLE {PROJECTS}
                        ADD CONSTRAINT uq_projects_entity_tenant
                        UNIQUE (entity_id, tenant_id);
                END IF;
            END $$;
            """,
            "分支缺失（B2）；entity_id 已是主键，故该唯一键结构上必然成立",
        )
    )
    steps.append(
        Step(
            "K2",
            "projects 三列唯一键 (entity_id, tenant_id, organization_id)",
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_projects_entity_tenant_org'
                      AND conrelid = '{PROJECTS}'::regclass
                ) THEN
                    ALTER TABLE {PROJECTS}
                        ADD CONSTRAINT uq_projects_entity_tenant_org
                        UNIQUE (entity_id, tenant_id, organization_id);
                END IF;
            END $$;
            """,
            "三列外键的被引用键；没有它，三列外键同样 42830",
        )
    )

    # ── 阶段 3：projects 自身的组织外键 ─────────────────────────────────
    steps.append(
        Step(
            "P1",
            "projects → organizations（两列，NOT VALID）",
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'fk_projects_organization_tenant'
                      AND conrelid = '{PROJECTS}'::regclass
                ) THEN
                    ALTER TABLE {PROJECTS}
                        ADD CONSTRAINT fk_projects_organization_tenant
                        FOREIGN KEY (organization_id, tenant_id)
                        REFERENCES {ORGS} (organization_id, tenant_id) NOT VALID;
                END IF;
            END $$;
            """,
            "与 main 同名（main 侧 convalidated=t）；分支侧 organization_id 是 NOT NULL，"
            "不存在 MATCH SIMPLE 被 NULL 整体绕过的问题",
        )
    )

    # ── 阶段 4：A 类项目级归属外键（三列，NOT VALID）────────────────────
    for schema, table in PROJECT_LEVEL:
        qualified = f"{schema}.{table}"
        name = _fk_project(table)
        steps.append(
            Step(
                "A",
                f"{qualified} → projects 三列归属外键",
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = '{name}' AND conrelid = '{qualified}'::regclass
                    ) THEN
                        ALTER TABLE {qualified}
                            ADD CONSTRAINT {name}
                            FOREIGN KEY (project_id, tenant_id, organization_id)
                            REFERENCES {PROJECTS} (entity_id, tenant_id, organization_id)
                            NOT VALID;
                    END IF;
                END $$;
                """,
                "三列才能同时锁住「项目存在 + 同租户 + 组织与项目一致」",
            )
        )

    # ── 阶段 5：B 类组织级归属外键（两列，NOT VALID）────────────────────
    for schema, table in ORG_LEVEL:
        qualified = f"{schema}.{table}"
        name = _fk_org(table)
        steps.append(
            Step(
                "B",
                f"{qualified} → organizations 两列归属外键",
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = '{name}' AND conrelid = '{qualified}'::regclass
                    ) THEN
                        ALTER TABLE {qualified}
                            ADD CONSTRAINT {name}
                            FOREIGN KEY (organization_id, tenant_id)
                            REFERENCES {ORGS} (organization_id, tenant_id)
                            NOT VALID;
                    END IF;
                END $$;
                """,
                "组织级资产被多项目共享，不补 project_id",
            )
        )

    return steps


def downgrade_steps() -> list[Step]:
    """严格逆序撤除；只删本迁移建的东西。"""
    steps: list[Step] = []

    for schema, table in reversed(ORG_LEVEL):
        steps.append(
            Step(
                "B-",
                f"撤 {schema}.{table} 组织外键",
                f"ALTER TABLE {schema}.{table} DROP CONSTRAINT IF EXISTS {_fk_org(table)}",
            )
        )
    for schema, table in reversed(PROJECT_LEVEL):
        steps.append(
            Step(
                "A-",
                f"撤 {schema}.{table} 项目外键",
                f"ALTER TABLE {schema}.{table} DROP CONSTRAINT IF EXISTS {_fk_project(table)}",
            )
        )
    steps.append(
        Step(
            "P1-",
            "撤 projects 组织外键",
            f"ALTER TABLE {PROJECTS} DROP CONSTRAINT IF EXISTS fk_projects_organization_tenant",
        )
    )
    steps.append(
        Step(
            "K2-",
            "撤 projects 三列唯一键",
            f"ALTER TABLE {PROJECTS} DROP CONSTRAINT IF EXISTS uq_projects_entity_tenant_org",
        )
    )
    steps.append(
        Step(
            "K1-",
            "撤 projects 两列唯一键",
            f"ALTER TABLE {PROJECTS} DROP CONSTRAINT IF EXISTS uq_projects_entity_tenant",
        )
    )
    steps.append(
        Step(
            "O1-",
            "删 organizations 表",
            f"DROP TABLE IF EXISTS {ORGS}",
            "表与本迁移一并撤销；其中的行是由 projects.organization_id 物化而来，"
            "不含任何独立业务事实。若该表在本迁移之前已存在（main 谱系），"
            "本步会误删——因此 downgrade 前必须先用 preflight 判定「表是否由本迁移创建」",
        )
    )
    return steps


#: 由本迁移创建、需在 VALIDATE 阶段逐一核验的外键（供 runbook 与验证脚本共用）。
OWNERSHIP_FKS: tuple[tuple[str, str], ...] = (
    (
        (PROJECTS, "fk_projects_organization_tenant"),
        *tuple((f"{s}.{t}", _fk_project(t)) for s, t in PROJECT_LEVEL),
        *tuple((f"{s}.{t}", _fk_org(t)) for s, t in ORG_LEVEL),
    )
)


def upgrade() -> None:
    """执行全部补齐语句（每步一条语句，失败即整体回滚由 Alembic 事务负责）。"""
    for step in upgrade_steps():
        op.execute(step.sql)


def downgrade() -> None:
    """逆序撤除。"""
    for step in downgrade_steps():
        op.execute(step.sql)
