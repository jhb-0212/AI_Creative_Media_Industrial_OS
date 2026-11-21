"""归属外键第三批：`episodes` 补三列归属外键（NOT VALID）。

为什么这张表必须单列一批
------------------------
`project_planning.episodes` 是本轮唯一一张**例外机制已为它完整建好、而它本该被
守护的那个约束从未创建**的表：

* `c1d2` 给它建了 `project_scope_episodes`（RESTRICTIVE）；
* `d2e3` 把它列进 `KEY_COLS` 并授予 `afs_data_repair` 修数权限；
* `e3f4` 把它列进 `SCOPED_POLICIES` 并装上了 RLS 旁路。

也就是说，整套修数通道是照着「它的 `project_id` 可能会指错、需要订正」这个前提
搭的——唯独「指错」这件事本身没有任何约束去定义。修数角色的权限在，可以把它改到
任意字符串；数据库一声不响。

为什么此前没被发现
------------------
它既不在 `a9b8.PROJECT_LEVEL`（12 张项目级表），也不在 `a9b8.DERIVED_NO_FK`
（`shot_specs` / `approval_steps`）。两次人工清单都没列它，于是建表、建 RLS、
建守卫、装例外这四轮全部跳过它，而台账 17 行也从不包含它。

这与 `qc_reports` 属同一类：**不是判断错了，是压根没进入判断**。所以本轮的根治
手段不只是这条外键，而是 `verify_ownership_closure.py` 这道闸门——它从
`pg_attribute` 实读「带归属列的表」，与台账集合求差，差集非空即 FAIL。闸门先行，
本迁移是它抓出来的第一张表。

为什么 `script_revisions` 不需要单独一条
----------------------------------------
它已有 `fk_script_revisions_episode (project_id, episode_ref) → episodes
(project_id, episode_ref)`。该复合外键要求子行的 `project_id` **等于父行的
`project_id`**；而父行 `episodes` 由本迁移约束到真实项目。于是
`script_revisions.project_id` 的一致性由父行**传递成立**，再挂一条三列外键
只会重复同一事实（且引入第二个可被违反的入口）。

这条传递关系不是口头承诺：`verify_ownership_closure.py` 的豁免登记要求它
**可机检**——该复合外键必须存在、列序为 `(project_id, episode_ref)`、且
`convalidated = true`，否则豁免不成立、闸门转红。

为什么 `organizations` 不需要
-----------------------------
它是归属链终点。全库唯一指向它的归属外键就是 `projects → organizations`，
没有更上层实体能当它的父。它是链条的根，不是漏掉的表。

本迁移不做什么
--------------
三列外键只保「目标项目真实存在 + 同租户 + 组织与项目一致」，**不保本行的
`project_id` 与父行相同**——`episodes` 的父是 `projects` 本身，故对这张表而言
两者恰好等价；但对 `qc_reports` 那一类（父行是 `asset_versions`）仍不等价，
I-1 的「一致」半边仍需复合外键指向父行或触发器，属下一轮议题。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op

revision: str = "g7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLANNING = "project_planning"
PROJECTS = f"{PLANNING}.projects"
EPISODES = f"{PLANNING}.episodes"

#: 本批只有一张表，但仍写成元组：与 `BATCH2` 同构，后续若再补可直接扩展，
#: 不必改脚本形状。命名单一来源在 `_fk_project()`。
BATCH3: tuple[tuple[str, str], ...] = ((PLANNING, "episodes"),)


def _fk_project(table: str) -> str:
    """与 `a9b8` / `f8a9` 完全一致的命名。台账与闸门都按此名识别。"""
    return f"fk_{table}_project_tenant_org"


class Step(NamedTuple):
    """一步一条语句。

    ``NamedTuple`` 而非 ``@dataclass``：Alembic 用 ``module_from_spec`` 加载迁移却
    不登记 ``sys.modules``，dataclass 解析注解时会回查 ``sys.modules[cls.__module__]``
    得到 ``None`` 而抛 ``AttributeError``（`a9b8` 的原始事故，勿再踩）。
    """

    code: str
    title: str
    sql: str
    note: str = ""


def upgrade_steps() -> list[Step]:
    steps: list[Step] = []

    # ── 阶段 1：前置断言 ────────────────────────────────────────────────
    # 42830（there is no unique constraint matching given keys）的根因是**目标键缺失**，
    # 与数据脏无关——空表上建两列外键也会直接 42830。提前断言，把两类失败分开。
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
            "42830 是目标键缺失，不是数据脏；提前分开这两种失败",
        )
    )

    # 归属列可空会让三列外键退化成「NULL 即跳过」——约束在，保护不在。
    for schema, table in BATCH3:
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
                "可空即等于「NULL 时跳过检查」，约束在而保护不在",
            )
        )

    # ── 阶段 2：三列归属外键（NOT VALID）────────────────────────────────
    for schema, table in BATCH3:
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
    for schema, table in reversed(BATCH3):
        qualified = f"{schema}.{table}"
        name = _fk_project(table)
        steps.append(
            Step(
                "D",
                f"删除 {qualified} 的归属外键",
                f"ALTER TABLE {qualified} DROP CONSTRAINT IF EXISTS {name};",
                "只删本迁移建的一条，不触碰前两批",
            )
        )
    return steps


#: 由本迁移创建、需在 VALIDATE 阶段逐一核验的外键。
#: 与 `a9b8.OWNERSHIP_FKS` + `f8a9.OWNERSHIP_FKS` 拼起来才是全量 18 条。
OWNERSHIP_FKS: tuple[tuple[str, str], ...] = tuple(
    (f"{s}.{t}", _fk_project(t)) for s, t in BATCH3
)


def upgrade() -> None:
    for step in upgrade_steps():
        op.execute(step.sql)


def downgrade() -> None:
    for step in downgrade_steps():
        op.execute(step.sql)
