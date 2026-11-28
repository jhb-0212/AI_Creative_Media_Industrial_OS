"""批次七：把「哪些镜头状态算在制」收敛成一个可查询的单一事实源。

要被削掉的那条路
---------------
批次六建了双向守卫，但**阻断集合在四处各写了一次**：

    scenes_archive_guard   : `s.status = 'IN_PRODUCTION'`
    shots_parent_scene_guard: `NEW.status <> 'IN_PRODUCTION'`
    两个触发器的 WHEN 子句 : `NEW.status = 'IN_PRODUCTION'`

这不是四个笔误，是同一个事实的四份副本。它们的失效方式很具体：**新增一个
「算在制」的镜头状态时，守卫函数改了，WHEN 子句没改**——于是触发器根本不
触发，守卫里那段正确的逻辑永远不执行，而所有测试（它们用的还是
`IN_PRODUCTION`）照样全绿。这与本项目在 `qc_reports` / `episodes` /
`OWNERSHIP_STACK` 上三次栽过的形态完全相同：清单不会自己举手，漏掉的条目不
报错，它只是不在那里。

修法不是「四处都记得改」，是让集合只有一处：函数
``shot_planning.shot_archive_blocking_states()``。两个守卫的 SQL 与两个触发器
的 WHEN 子句都从它取值，**触发器不再按状态过滤**——过滤这件事归函数。

为什么把判定放进函数而不是留在 WHEN
----------------------------------
WHEN 子句是性能预过滤，但性能不值这个价：守卫本身是 `BEFORE ... FOR EACH ROW`
的常量级比较，全表更新时多一次函数调用（`IMMUTABLE` SQL 函数会被内联/缓存）
相对于行锁与 WAL 的开销可以忽略。而把状态名留在 WHEN 里，就等于把一个业务
判定藏进了一个「看起来只是优化」的位置——这类藏匿点在本项目里无一例外都
腐化了。

''BLOCKED'' 的裁定（2026-11-21，所有者授权按工程铁律裁定）
-------------------------------------------------------
**不阻断归档。** 理由不是「它介于两者之间，暂不处理」，而是这条路径已经被
另一侧关死了：

    场次归档时，BLOCKED 镜头不构成阻塞（它没在消耗资源）；
    但归档之后，它**永远无法**被推进到 IN_PRODUCTION——
    B 侧守卫（`shots_parent_scene_guard`）在那一刻拒绝。

也就是说 A 侧放行 BLOCKED 不留下任何可被利用的通道：归档后的镜头只能停在
BLOCKED 或走向 CANCELLED，两者都不产生「已封存父下的在制品」。这一条必须
由测试钉住（`verify_batch7.py` 的 S3：BLOCKED 镜头在其场次归档后推进
IN_PRODUCTION 被拒），否则「A 侧放行」就等于「没人管」。

对照组是 IN_PRODUCTION：它**不能**靠 B 侧兜底，因为它已经在制了，归档那一刻
就是缺口发生的时刻，没有「以后会被拦」的机会。这才是它必须进阻断集合的原因。

分区必须完备（这是本批次真正的不变量）
----------------------------------
``BLOCKING_STATES`` 与 ``NON_BLOCKING_STATES`` 构成 Shot 状态集的一个**划分**。
脚本 ``verify_batch7.py`` 与单测 ``test_shot_archive_scope.py`` 都断言：

    set(BLOCKING) ∪ set(NON_BLOCKING) == 契约里的全部 Shot 状态
    set(BLOCKING) ∩ set(NON_BLOCKING) == ∅

于是契约新增一个状态时，两侧都会红——必须有人显式决定它算不算在制。默认
「不算」是最坏的选择：它会静默进入「归档不拦 + 不可复活」的中间地带。
"""

from __future__ import annotations

from typing import NamedTuple

from alembic import op

revision = "k1f2a3b4c5d6"
down_revision = "j0e1f2a3b4c5"
branch_labels = None
depends_on = None

PLANNING = "project_planning"
SHOT = "shot_planning"

FUNC_BLOCKING_STATES = "shot_archive_blocking_states"
FUNC_SCENES_ARCHIVE = "scenes_archive_guard"
FUNC_SHOTS_PARENT = "shots_parent_scene_guard"
TRIG_SCENES_ARCHIVE = "scenes_archive_guard_trigger"
TRIG_SHOTS_PARENT = "shots_parent_scene_guard_trigger"

#: 归档场次时构成阻塞的镜头状态——**唯一事实源**（守卫与触发器都从这里取值）。
#:
#: 只放「此刻正在消耗资源」的状态。放进来的每一条都必须能回答「为什么不能靠
#: B 侧守卫兜底」，因为 B 侧只在**之后**才拦，而归档那一刻的缺口是既成事实。
BLOCKING_STATES: tuple[str, ...] = ("IN_PRODUCTION",)

#: 明确「不算在制」的状态。它不是装饰：与 BLOCKING_STATES 一起构成划分，
#: 缺失项会让完备性断言报红，从而逼出一次显式裁定。
#:
#: - NOT_STARTED / BLOCKED：没有资源在投入；归档即「这一场不做了」，合法。
#: - PRODUCTION_COMPLETE：产物已完成，归档只是把已完成的东西收起来。
#: - CANCELLED：终态，本就不在生产范围内。
NON_BLOCKING_STATES: tuple[str, ...] = (
    "NOT_STARTED",
    "BLOCKED",
    "PRODUCTION_COMPLETE",
    "CANCELLED",
)


class Step(NamedTuple):
    sql: str


def _blocking_states_sql() -> str:
    """建立阻断集合的唯一查询入口。

    ``IMMUTABLE`` 是如实声明（返回值是常量），不是性能噱头：它让规划器可以在
    每条被更新的行上内联这次调用，而不必每行真的进一次函数体。
    """
    literal = ", ".join(f"'{state}'" for state in BLOCKING_STATES)
    return f"""
CREATE OR REPLACE FUNCTION {SHOT}.{FUNC_BLOCKING_STATES}()
RETURNS text[]
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $blocking$
    SELECT ARRAY[{literal}]::text[]
$blocking$;
"""


def _scenes_archive_guard_sql(*, use_function: bool) -> str:
    """A 侧：归档场次前检查其下有无在制镜头。

    ``use_function=False`` 只用于 downgrade——还原批次六的写法（字面量），
    因为那一层的等价语义就是「写死一个状态名」。刻意保留这条分支而不是在
    downgrade 里另抄一份 SQL：两份历史的正文会让「批次六到底检查了什么」
    出现两个版本。
    """
    predicate = (
        f"s.status = ANY ({SHOT}.{FUNC_BLOCKING_STATES}())"
        if use_function
        else "s.status = 'IN_PRODUCTION'"
    )
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
       AND {predicate};

    IF blocking > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = format(
                '%s.scenes %s 仍有 %s 个镜头处于在制状态，禁止归档（ADR-R27/Q3）',
                '{PLANNING}', NEW.scene_id, blocking
            ),
            HINT = '先把这些镜头推进到 PRODUCTION_COMPLETE 或 CANCELLED，再归档场次';
    END IF;

    RETURN NEW;
END;
$guard$;
"""


def _shots_parent_guard_sql(*, use_function: bool) -> str:
    """B 侧：把镜头推进到在制状态前检查父场次是否已归档。

    父行查不到时**刻意放行**：同一事务内「先建场次后建镜头」是合法路径
    （外键此刻可能还没检查），父行缺失由 `fk_shots_scene_project` 兜底成
    23503。在那里拒会把正常路径一起打死（`verify_batch6.py` F4 钉住）。
    """
    if use_function:
        in_set = f"NEW.status = ANY ({SHOT}.{FUNC_BLOCKING_STATES}())"
        old_in_set = f"OLD.status = ANY ({SHOT}.{FUNC_BLOCKING_STATES}())"
    else:
        in_set = "NEW.status = 'IN_PRODUCTION'"
        old_in_set = "OLD.status = 'IN_PRODUCTION'"
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
    IF NOT ({in_set}) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND ({old_in_set}) THEN
        RETURN NEW;
    END IF;

    SELECT s.status INTO parent_status
      FROM {PLANNING}.scenes s
     WHERE s.scene_id = NEW.scene_id;

    IF parent_status IS NULL THEN
        RETURN NEW;
    END IF;

    IF parent_status = 'ARCHIVED' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = format(
                '%s.scenes %s 已归档，不得在其中把镜头 %s 推进到在制状态（ADR-R27/Q3）',
                '{PLANNING}', NEW.scene_id, NEW.shot_id
            ),
            HINT = '场次归档意味着这一场不再生产；如确需继续，先把场次恢复为 ACTIVE';
    END IF;

    RETURN NEW;
END;
$guard$;
"""


def _trigger_sql(*, use_when: bool) -> list[str]:
    """重建两个触发器。

    为什么必须 DROP 再 CREATE：``WHEN`` 子句无法 ALTER，只能重建。批次七
    **去掉** WHEN 里的状态判定（``use_when=False``），让函数成为唯一判据；
    批次七的 downgrade 反过来把它加回去。
    """
    scenes_when = (
        "WHEN (NEW.status = 'ARCHIVED' AND OLD.status IS DISTINCT FROM 'ARCHIVED') "
        if use_when
        else ""
    )
    shots_when = "WHEN (NEW.status = 'IN_PRODUCTION') " if use_when else ""
    return [
        f"DROP TRIGGER IF EXISTS {TRIG_SCENES_ARCHIVE} ON {PLANNING}.scenes",
        f"CREATE TRIGGER {TRIG_SCENES_ARCHIVE} "
        f"BEFORE UPDATE OF status ON {PLANNING}.scenes "
        f"FOR EACH ROW {scenes_when}"
        f"EXECUTE FUNCTION {PLANNING}.{FUNC_SCENES_ARCHIVE}()",
        f"DROP TRIGGER IF EXISTS {TRIG_SHOTS_PARENT} ON {SHOT}.shots",
        f"CREATE TRIGGER {TRIG_SHOTS_PARENT} "
        f"BEFORE INSERT OR UPDATE OF status ON {SHOT}.shots "
        f"FOR EACH ROW {shots_when}"
        f"EXECUTE FUNCTION {SHOT}.{FUNC_SHOTS_PARENT}()",
    ]


def upgrade_steps() -> list[Step]:
    return [
        Step(_blocking_states_sql()),
        Step(_scenes_archive_guard_sql(use_function=True)),
        Step(_shots_parent_guard_sql(use_function=True)),
        *[Step(sql) for sql in _trigger_sql(use_when=False)],
    ]


def downgrade_steps() -> list[Step]:
    """严格对称：先还原批次六的守卫与触发器，最后才删函数。

    顺序不可反——守卫在函数体里引用 `shot_archive_blocking_states()`，
    先删函数会让 `CREATE OR REPLACE` 之后的函数仍解析到不存在的对象
    （plpgsql 是延迟解析，不会在创建时报错，而是在**触发时**报 42883）。
    """
    return [
        Step(_scenes_archive_guard_sql(use_function=False)),
        Step(_shots_parent_guard_sql(use_function=False)),
        *[Step(sql) for sql in _trigger_sql(use_when=True)],
        Step(f"DROP FUNCTION IF EXISTS {SHOT}.{FUNC_BLOCKING_STATES}()"),
    ]


def applied_probe_sql() -> str:
    """本层是否已应用：函数存在即已应用。

    由 ``lab.applied_probe`` 消费。批次七的对象就是这一个函数，探针与它同形；
    用守卫函数的定义文本判断会让「批次六已应用、批次七未应用」这种中间态
    无法区分。
    """
    return f"SELECT (to_regprocedure('{SHOT}.{FUNC_BLOCKING_STATES}()') IS NOT NULL)"


def upgrade() -> None:
    for step in upgrade_steps():
        op.execute(step.sql)


def downgrade() -> None:
    for step in downgrade_steps():
        op.execute(step.sql)
