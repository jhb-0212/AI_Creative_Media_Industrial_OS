"""归属外键第二批：三张「有归属列却无归属外键」的表补三列外键（NOT VALID）。

为什么要有这一批
----------------
`a9b8c7d6e5f4` 把 12 张项目级表 + 1 张组织级表接进了归属闭环，并显式列了
`DERIVED_NO_FK = (shot_specs, approval_steps)` 表示「本轮刻意不建」。逐表覆盖矩阵
（`verify_guard_coverage.py`）随后证明这个「刻意」是有代价的：这三张表
（`asset_service.qc_reports` / `shot_planning.shot_specs` / `workflow.approval_steps`）
都在不可变守卫之下、都被受控例外覆盖，但**例外放行后可以把 `project_id` 改成一个
根本不存在的项目，数据库一声不响**（实测 `UPDATE 1` 成功）。这正是 I-1
「归属指向的实体真实」未获保护。

为什么是补外键，而不是改成「显式声明派生、不建约束」
----------------------------------------------------
派生的说法是对的（qc_reports → asset_version、shot_specs → shot_revision、
approval_steps → ticket 都能推出 project），但**数据库层无法强制派生**：

  * `project_id` 是已经落库的事实列，不是视图表达式，派生关系只能靠应用层维护；
  * 一旦这一列写错，父行推出来的值与本行存的值就开始分叉，于是同一个事实有了
    两个源——而「一个事实一个权威源」是本项目的硬性准则（R1）。

补三列外键的代价很小（目标唯一键 `uq_projects_entity_tenant_org` 已存在，
空表直接建得起来），收益是「错的归属根本写不进去」。派生逻辑仍留在应用层，
但数据库负责保证落库的那个值至少指向一个真实且组织一致的项目。

**本迁移明确撤销 `a9b8.DERIVED_NO_FK` 对 `shot_specs` / `approval_steps` 的处置。**
`a9b8` 的字节不动（已应用，改它等于改历史），撤销动作记录在此处与
`docs/audits/2026-11-20-ownership-fk-batch2.md`。

本迁移**不做什么**（必须写清，否则会被读成「闭环了」）
------------------------------------------------------
三列外键 `(project_id, tenant_id, organization_id) → projects(entity_id, tenant_id,
organization_id)` 只保证「目标项目真实存在，且租户与组织跟它一致」。
它**不保证本行的 `project_id` 与父行的 `project_id` 相同**——`qc_reports` 的父行是
`asset_versions`，两者 project 不一致时三列外键照样放行。I-1 的「一致」半边需要
复合外键指向父行的 `(id, project_id, tenant_id, organization_id)` 或等价触发器，
属下一轮议题，不在本迁移假装解决。

为什么仍是 ``NOT VALID``
------------------------
与 `a9b8` 同口径：`NOT VALID` 不扫描历史行，新写入照样受约束，是「存量可能脏、
且脏行不能替它洗白」的唯一正确姿势。本库实测三表 0 违规（`qc_reports` 1 行合规，
另两张空表），但迁移**不假设目标库干净**。闭环判据仍是
`ALTER TABLE ... VALIDATE CONSTRAINT` + 台账 17 行全 PASS，由
`acceptance_ledger.py` 给出，不由本文件自称。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op

revision: str = "f8a9b0c1d2e3"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLANNING = "project_planning"
PROJECTS = f"{PLANNING}.projects"

#: 第二批：(schema, table)。顺序固定，便于快照与台账逐行对照。
BATCH2: tuple[tuple[str, str], ...] = (
    ("asset_service", "qc_reports"),
    ("shot_planning", "shot_specs"),
    ("workflow", "approval_steps"),
)

#: `a9b8` 曾把它们列为「刻意不建」，本迁移撤销该处置。留成常量是为了让
#: 复核者能在代码里直接看到「撤销的是哪两条」，而不是只看到一句注释。
RETIRED_DERIVED_NO_FK: tuple[str, ...] = (
    "shot_planning.shot_specs",
    "workflow.approval_steps",
)


def _fk_project(table: str) -> str:
    """与 a9b8 完全一致的命名。台账与覆盖矩阵都按这个名字识别，不能另起一套。"""
    return f"fk_{table}_project_tenant_org"


class Step(NamedTuple):
    """一步一条语句。

    与 `a9b8`/`c1d2` 同构（``NamedTuple`` 而非 ``@dataclass``：Alembic 用
    ``module_from_spec`` 加载迁移却不登记 ``sys.modules``，dataclass 解析注解时会
    回查 ``sys.modules[cls.__module__]`` 得到 ``None`` 而抛 ``AttributeError``）。
    """

    code: str
    title: str
    sql: str
    note: str = ""


def upgrade_steps() -> list[Step]:
    steps: list[Step] = []

    # ── 阶段 1：前置断言 ────────────────────────────────────────────────
    # 目标唯一键缺失时，建外键会得到 42830（there is no unique constraint
    # matching given keys），而 42830 的根因是「目标键不存在」，与数据脏无关。
    # 提前断言，把「缺键」和「脏数据」两种失败彻底分开。
    steps.append(
        Step(
            "P",
            "前置：projects 三列唯一键存在",
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'uq_projects_entity_tenant_org'
                       AND conrelid = '{PROJECTS}'::regclass
                ) THEN
                    RAISE EXCEPTION
                        'projects 缺少 (entity_id, tenant_id, organization_id) 唯一键，'
                        '无法挂三列归属外键（先确认 a9b8 已执行）';
                END IF;
            END $$;
            """,
            "把 42830 的根因（目标键缺失）与脏数据失败区分开",
        )
    )

    # 三列必须都存在且非空。缺列时 ALTER TABLE 会报 undefined_column，
    # 但那时已经动了两张表，失败信息还会指向「建外键」而不是「表结构不符」。
    for schema, table in BATCH2:
        steps.append(
            Step(
                "P",
                f"前置：{schema}.{table} 归属三列齐备且非空",
                f"""
                DO $$
                DECLARE missing text;
                BEGIN
                    SELECT string_agg(c, ', ') INTO missing
                      FROM unnest(ARRAY['project_id','tenant_id','organization_id']) AS c
                     WHERE NOT EXISTS (
                         SELECT 1 FROM pg_attribute a
                          WHERE a.attrelid = '{schema}.{table}'::regclass
                            AND a.attname = c
                            AND a.attnum > 0
                            AND NOT a.attisdropped
                            AND a.attnotnull
                     );
                    IF missing IS NOT NULL THEN
                        RAISE EXCEPTION '{schema}.{table} 缺少归属列或列可空：%', missing;
                    END IF;
                END $$;
                """,
                "归属列可空时三列外键会退化成「NULL 即跳过」，等于没约束",
            )
        )

    # ── 阶段 2：三列归属外键（NOT VALID）────────────────────────────────
    for schema, table in BATCH2:
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

    return steps


def downgrade_steps() -> list[Step]:
    steps: list[Step] = []
    # 逆序删，与建的顺序对称：一旦某张表的删除因残留依赖失败，
    # 报错位置与升级时的位置对应，便于定位。
    for schema, table in reversed(BATCH2):
        qualified = f"{schema}.{table}"
        name = _fk_project(table)
        steps.append(
            Step(
                "D",
                f"删除 {qualified} 的归属外键",
                f"ALTER TABLE {qualified} DROP CONSTRAINT IF EXISTS {name};",
                "仅删本迁移建的三条，不触碰 a9b8 的既有外键",
            )
        )
    return steps


#: 由本迁移创建、需在 VALIDATE 阶段逐一核验的外键，供台账/runbook 共用。
#: 与 `a9b8.OWNERSHIP_FKS` 同构，两者拼起来才是全量 17 条。
OWNERSHIP_FKS: tuple[tuple[str, str], ...] = tuple((f"{s}.{t}", _fk_project(t)) for s, t in BATCH2)


def upgrade() -> None:
    for step in upgrade_steps():
        op.execute(step.sql)


def downgrade() -> None:
    for step in downgrade_steps():
        op.execute(step.sql)
