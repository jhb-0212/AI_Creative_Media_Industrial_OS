"""批次六：把「归档场次不得留下在制品」变成数据库不变量。

要被削掉的那条路
---------------
`UPDATE project_planning.scenes SET status='ARCHIVED'` 现在什么都拦不住：
外键能表达「子行必须有父」，表达不了「子行**不得存在**」。于是可以归档一个
仍在拍摄的场次，其 `IN_PRODUCTION` 镜头从此挂在一个已封存的父下——
后续按场次聚合、排期、结算的每一步都会读到「已归档场次却有在制镜头」。

这不是服务层漏了一个 if：任何人拿 `svc_project_planning` 直连改一行都能复现。
所以判定放在数据库层，与既有 13 个守卫同层。

为什么必须双向
-------------
只拦归档侧的话，对称路径照样穿透：

    归档场次（被拦） → 改成先把镜头推进 IN_PRODUCTION（无人拦） → 再归档

所以两侧都建：
  A 侧  `scenes` 更新为 ARCHIVED 时，若存在 IN_PRODUCTION 子镜头 → 拒；
  B 侧  `shots` 更新为 IN_PRODUCTION（或直接 INSERT 成该状态）时，若父场次
        已 ARCHIVED → 拒。

为什么用 SECURITY DEFINER（与受控例外的结论看似相反，实则同一判据）
----------------------------------------------------------------
受控例外里「判定授权的函数必须 INVOKER」——因为那里函数在做**授权判定**，
DEFINER 会让 `current_user` 变成 owner、`pg_has_role` 恒真，例外退化为
「任何有表写权限者可用」。

这里函数做的是**数据完整性检查**：不判角色、不返回业务数据、不写任何表，
只有 `RAISE` 与 `RETURN NEW` 两条出口。判据是「函数在判什么」，不是「函数是
DEFINER 还是 INVOKER」。

实测决定的理由（不是偏好）：两个域角色互不持有对方表的权限——

    svc_project_planning 对 shot_planning.shots ：**零权限**
    svc_shot_planning    对 project_planning.scenes ：**零权限**

若用 INVOKER，A 侧触发器查 `shots` 会直接 `42501 权限不足`，结果是
**所有场次归档一律失败**，而不是「只挡住不该归档的」——这是最坏的一种失败：
功能整体不可用，且报错看起来像业务拒绝（与受控例外陷阱③同型）。

补授权不是解法：那会让项目规划域直接读镜头域原始表，破坏「只能经服务/API
交互」的边界，而且是永久性的权限扩散。

`SET search_path` 是 DEFINER 的强制配套：不固定 search_path 时，调用方可把
同名对象放进自己的 schema 劫持函数内解析。既有守卫函数都没设——这是既有隐患，
不在本批次修，但新函数一律设。

范围：刻意只拦 IN_PRODUCTION
--------------------------
`NOT_STARTED` / `BLOCKED` 不拦。理由是「归档」的语义是「这一场不做了」，
砍掉一个还没开拍的场次是合法的正常操作；而在制品（IN_PRODUCTION）归档
意味着有资源正在投入却突然挂到封存父下，才是必须拒绝的那种。

`BLOCKED` 是否也该拦属所有者保留裁定（它在语义上介于两者之间）。当前**不拦**，
并在台账与测试里各钉一条，防止它被悄悄改掉。
"""

from __future__ import annotations

from typing import NamedTuple

from alembic import op

revision = "j0e1f2a3b4c5"
down_revision = "i9d0e1f2a3b4"
branch_labels = None
depends_on = None

PLANNING = "project_planning"
SHOT = "shot_planning"

FUNC_SCENES_ARCHIVE = "scenes_archive_guard"
FUNC_SHOTS_PARENT = "shots_parent_scene_guard"
TRIG_SCENES_ARCHIVE = "scenes_archive_guard_trigger"
TRIG_SHOTS_PARENT = "shots_parent_scene_guard_trigger"


class Step(NamedTuple):
    sql: str


def _scenes_archive_guard_sql() -> str:
    return f"""
CREATE OR REPLACE FUNCTION {PLANNING}.{FUNC_SCENES_ARCHIVE}()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, {SHOT}
AS $guard$
DECLARE
    blocking integer;
BEGIN
    -- 只在「从非归档变成归档」时检查：重复写 ARCHIVED 不该再被检查一次，
    -- 否则一条历史脏数据会让该行永远无法被再次保存。
    IF NEW.status <> 'ARCHIVED' OR OLD.status = 'ARCHIVED' THEN
        RETURN NEW;
    END IF;

    SELECT count(*) INTO blocking
      FROM {SHOT}.shots s
     WHERE s.scene_id = NEW.scene_id
       AND s.status = 'IN_PRODUCTION';

    IF blocking > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = format(
                '%s.scenes %s 仍有 %s 个镜头处于 IN_PRODUCTION，禁止归档（ADR-R27/Q3）',
                '{PLANNING}', NEW.scene_id, blocking
            ),
            HINT = '先把这些镜头推进到 PRODUCTION_COMPLETE 或 CANCELLED，再归档场次';
    END IF;

    RETURN NEW;
END;
$guard$;
"""


def _shots_parent_guard_sql() -> str:
    return f"""
CREATE OR REPLACE FUNCTION {SHOT}.{FUNC_SHOTS_PARENT}()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, {PLANNING}
AS $guard$
DECLARE
    parent_status text;
BEGIN
    IF NEW.status <> 'IN_PRODUCTION' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.status = 'IN_PRODUCTION' THEN
        RETURN NEW;
    END IF;

    SELECT s.status INTO parent_status
      FROM {PLANNING}.scenes s
     WHERE s.scene_id = NEW.scene_id;

    -- 父行此刻查不到不是「可以放行」而是「不归本触发器判」：
    -- 同一事务内先建场次后建镜头是合法的（外键此刻可能还没检查），
    -- 父行缺失由 fk_shots_scene_project 兜底成 23503。
    -- 若在这里拒，会把「同事务建场次+镜头」这条正常路径一起打死。
    IF parent_status IS NULL THEN
        RETURN NEW;
    END IF;

    IF parent_status = 'ARCHIVED' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = format(
                '%s.scenes %s 已归档，不得在其中把镜头 %s 推进到 IN_PRODUCTION（ADR-R27/Q3）',
                '{PLANNING}', NEW.scene_id, NEW.shot_id
            ),
            HINT = '场次归档意味着这一场不再生产；如确需继续，先把场次恢复为 ACTIVE';
    END IF;

    RETURN NEW;
END;
$guard$;
"""


def upgrade_steps() -> list[Step]:
    return [
        Step(_scenes_archive_guard_sql()),
        Step(
            f"CREATE TRIGGER {TRIG_SCENES_ARCHIVE} "
            f"BEFORE UPDATE OF status ON {PLANNING}.scenes "
            "FOR EACH ROW "
            "WHEN (NEW.status = 'ARCHIVED' AND OLD.status IS DISTINCT FROM 'ARCHIVED') "
            f"EXECUTE FUNCTION {PLANNING}.{FUNC_SCENES_ARCHIVE}()"
        ),
        Step(_shots_parent_guard_sql()),
        Step(
            f"CREATE TRIGGER {TRIG_SHOTS_PARENT} "
            f"BEFORE INSERT OR UPDATE OF status ON {SHOT}.shots "
            "FOR EACH ROW "
            "WHEN (NEW.status = 'IN_PRODUCTION') "
            f"EXECUTE FUNCTION {SHOT}.{FUNC_SHOTS_PARENT}()"
        ),
    ]


def downgrade_steps() -> list[Step]:
    """严格对称撤销。先撤触发器再撤函数：只删函数会因依赖失败。"""
    return [
        Step(f"DROP TRIGGER IF EXISTS {TRIG_SHOTS_PARENT} ON {SHOT}.shots"),
        Step(f"DROP FUNCTION IF EXISTS {SHOT}.{FUNC_SHOTS_PARENT}()"),
        Step(f"DROP TRIGGER IF EXISTS {TRIG_SCENES_ARCHIVE} ON {PLANNING}.scenes"),
        Step(f"DROP FUNCTION IF EXISTS {PLANNING}.{FUNC_SCENES_ARCHIVE}()"),
    ]


def upgrade() -> None:
    for step in upgrade_steps():
        op.execute(step.sql)


def downgrade() -> None:
    for step in downgrade_steps():
        op.execute(step.sql)
