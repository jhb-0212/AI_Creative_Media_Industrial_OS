"""归属「一致」闸门：证明本行的 `project_id` 与**父行**相同，而不只是指向真实项目。

为什么这道闸门必须存在
----------------------
I-1 有两半，长期被混为一谈：

* **「真实」半边**：本行 `project_id` 指向的 `projects` 行存在，且 org/tenant 与它一致。
  三列外键 `(project_id, tenant_id, organization_id) → projects` 已经做到。
* **「一致」半边**：本行 `project_id` 与**它的父行**（资产版本 / 镜头修订 / 审批流 …）
  的 `project_id` 相同。

只做「真实」半边时，下面这件事完全合法：`qc_reports` 挂在 `asset_versions/av-1`（属于
`proj-alpha`）上，而它自己的 `project_id` 写成 `proj-beta`。两个项目都真实、组织也一致，
三列外键一路放行。结果是同一条 QC 事实在「按资产溯源」与「按项目统计」两个口径下
归属不同——**一个事实两个答案**，正是 R1 要禁的东西。

本库实测存在这样的行（`qc_reports` 唯一一行即如此），所以这不是假想风险。

判定规则（一条，而不是一堆特例）
--------------------------------
「本行与父行一致」的**充分条件**是：存在一个复合外键，其子列集合 ⊇
``{父引用列, project_id}``，且 ``confrelid`` 指向父表，且 ``convalidated``。

为什么这条就够，不需要连 tenant_id / organization_id 一起要求：

* 外键把 ``project_id`` 钉到父行的 ``project_id`` 上 ⇒ 两者指向 ``projects`` 的**同一行**；
* 两边各自的 ``(project_id, tenant_id, organization_id) → projects`` 三列外键
  又把这个 ``project_id`` 钉到唯一一组 ``(tenant, org)`` 上；
* 于是 ``tenant``/``org`` 的一致性**传递成立**，无需在复合外键里重复。

这也是 ``script_revisions`` 能用 ``(project_id, episode_ref) → episodes`` 两列外键
就解决问题的原因——它早就把这件事做对了，只是没被推广。

反向情形同样要抓：若某列**已有**单列外键（只引 ``entity_id``，不含 ``project_id``），
它看起来「有保护」，实际上只保「父行存在」，一致性仍是空的。判定为
``FK_NOT_COMPOSITE``，并明确要求升级为复合外键——这类「看起来做了」比「没做」更危险。

集合类事实一律派生
------------------
候选列从 ``pg_attribute`` 实读，不写死清单：凡「含 project_id 的表」上以 ``_id``/``_ref``
结尾、且不是信封列/主键列的列，全部进入派生集。派生集里的每一列**必须**在
``CLASSIFICATION`` 里出现一次，否则判 ``UNCLASSIFIED`` 直接 FAIL。

这是对「清单不会自己举手」那条通则的直接回答：漏掉的条目现在会报错，
而不是安静地不在那里。

用法::

    python docs/audits/mig-recon/verify_ownership_consistency.py --db ai_film_industrial --port 15444
    python docs/audits/mig-recon/verify_ownership_consistency.py --selftest
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import asyncpg
import lab

OUT_MD = Path(__file__).resolve().parent / "verify-ownership-consistency.md"
OUT_JSON = Path(__file__).resolve().parent / "verify-ownership-consistency.json"

#: 归属批次放在 `pending/feat-ownership/` 而不是 `versions/`：它们的 `down_revision`
#: 指向另一条链的 head，放进 `versions/` 会让 `upgrade head` 出现二义性。
#: 自检要回退批次四，必须到这个目录找文件——指错目录时 `rewind_to` 会「回退 0 层」
#: 却照样返回成功，于是 S0 在已修复的库上跑出全绿并误判为闸门失效。
VERSIONS_DIR = Path(__file__).resolve().parents[3] / "data" / "migrations" / "pending" / "feat-ownership"

#: 业务 schema。刻意不含 `outbox` / `auth`：它们不承载项目归属
#: （`outbox` 靠 tenant 分区，`auth` 是凭证域），纳入只会制造无关噪声。
BUSINESS_SCHEMAS = (
    "project_planning",
    "shot_planning",
    "generation_execution",
    "asset_service",
    "qc_governance",
    "workflow",
)

#: 信封列：每张表都有，语义由架构统一规定，不是「指向某个实体」。
#: `audit_ref` 指审计流水、`project_id`/`tenant_id`/`organization_id` 是归属三列
#: （它们本身由别的闸门管），`entity_id` 是身份主键。
ENVELOPE_COLUMNS = frozenset(
    {"project_id", "tenant_id", "organization_id", "entity_id", "audit_ref"}
)

#: 派生 SQL。`NOT IN (主键列)` 是关键：`qc_reports.qc_report_id` 既是主键又匹配
#: `_id$`，把它当「父引用」会要求它指向某个父行，属于把身份误判为引用。
DERIVE_SQL = """
WITH owned AS (
    SELECT c.oid AS relid,
           n.nspname || '.' || c.relname AS tbl
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
     WHERE c.relkind = 'r'
       AND n.nspname = ANY($1::text[])
     GROUP BY 1, 2
    HAVING bool_or(a.attname = 'project_id')
)
SELECT o.tbl, a.attname AS col
  FROM owned o
  JOIN pg_attribute a ON a.attrelid = o.relid AND a.attnum > 0 AND NOT a.attisdropped
  JOIN pg_index i ON i.indrelid = o.relid AND i.indisprimary
 WHERE (a.attname ~ '_id$' OR a.attname ~ '_ref$')
   AND a.attname <> ALL($2::text[])
   AND a.attname NOT IN (
       SELECT pa.attname FROM pg_attribute pa
        WHERE pa.attrelid = o.relid AND pa.attnum = ANY(i.indkey))
 ORDER BY 1, 2
"""


NonRefCategory = Literal[
    "external_artifact",  # 外部系统/存储/第三方产物的标识，不在本库实体图内
    "soft_pointer",       # 跨模块软引用：目标模块自有归属闭环，本行不据它定位父行
    "reservation",        # 预算/配额预留单号，由预算域生成，不是实体身份
    "own_business_key",   # 本表自己的业务键（如 episodes.episode_ref），不是引用
    "polymorphic",        # 多态引用：目标类型由另一列决定，无法用单条外键表达
    "subject_identity",   # 主体（人/服务）标识，跨身份证域，不是项目实体
    "embedded_object",    # jsonb 内的对象引用（id + 版本），单列外键表达不了
]


@dataclass(frozen=True)
class Edge:
    """一条真实存在、且**应当**被一致性外键强制的父子归属边。

    `parent` 用 ``schema.table`` 全名。判定时不重写键列：由 `pg_constraint` 实读，
    父表上到底哪个唯一键可作为外键目标，以库里事实为准。
    """

    parent: str
    note: str


@dataclass(frozen=True)
class NonRef:
    """一条**不是**归属边的列，以及它属于哪一类非边。

    刻意要求 ``category`` 是枚举而不是自由文本：豁免/分类一旦允许随手写理由，
    它必然退化成垃圾桶（本项目已发生过一次纯文字豁免清单的退化）。枚举让
    「同类为什么被判为非边」可以在报告里按类汇总，复核者一次看一类。
    """

    category: NonRefCategory
    note: str


CLASSIFICATION: dict[tuple[str, str], Edge | NonRef] = {
    # ---- 资产域：QC 报告 / QC 会话挂在资产版本上 ----
    ("asset_service.qc_reports", "asset_version_id"): Edge(
        "asset_service.asset_versions", "QC 报告的被检对象；报告的项目归属必须与资产版本相同"
    ),
    ("qc_governance.qc_review_sessions", "asset_version_id"): Edge(
        "asset_service.asset_versions", "QC 会话围绕某个资产版本展开，归属随版本"
    ),
    ("qc_governance.qc_review_sessions", "qc_report_ref"): Edge(
        "asset_service.qc_reports", "QC 会话引用 QC 报告；报告归属随会话同一项目"
    ),
    # ---- 镜头域：规格挂在镜头修订上；分镜草案/修订挂在分集上 ----
    ("shot_planning.shot_specs", "shot_revision_id"): Edge(
        "shot_planning.shot_revisions", "规格是对某次镜头修订的量化，归属随修订"
    ),
    ("shot_planning.shot_drafts", "episode_ref"): Edge(
        "project_planning.episodes", "分镜草案按集组织；集是项目内的稳定分区"
    ),
    ("shot_planning.shot_revisions", "episode_ref"): Edge(
        "project_planning.episodes", "镜头修订按集组织；集是项目内的稳定分区"
    ),
    ("project_planning.script_revisions", "episode_ref"): Edge(
        "project_planning.episodes", "剧本修订按集组织（既有两列外键即此形状）"
    ),
    # ---- 审批域：票据随流程、步骤随票据 ----
    ("workflow.approval_tickets", "flow_id"): Edge(
        "workflow.approval_flows", "审批票据是流程的实例；跨流程挂票据等于跨项目审批"
    ),
    ("workflow.approval_steps", "ticket_id"): Edge(
        "workflow.approval_tickets", "审批步骤是票据的组成；跨票据挂步骤会使票数统计错位"
    ),

    # ---- 以下为「不是归属边」，按类归并 ----
    ("asset_service.asset_versions", "adapter_id"): NonRef(
        "external_artifact", "生成适配器标识，指向外部工具而非库内实体"
    ),
    ("asset_service.asset_versions", "operator_ref"): NonRef(
        "subject_identity", "操作者引用，属身份域"
    ),
    ("asset_service.asset_versions", "rights_manifest_ref"): NonRef(
        "external_artifact", "权利清单外部引用，可能来自法务系统"
    ),
    ("asset_service.asset_versions", "storage_ref"): NonRef(
        "external_artifact", "对象存储路径，天然在库外"
    ),
    ("asset_service.delivery_manifests", "picture_lock_ref"): NonRef(
        "soft_pointer", "锁画引用，指向已锁定的交付快照，可能跨版本存在"
    ),
    ("asset_service.delivery_manifests", "qc_final_ref"): NonRef(
        "soft_pointer", "终检引用；交付清单可汇总多次 QC 结论，不构成单父边"
    ),
    ("asset_service.delivery_manifests", "spec_ref"): NonRef(
        "soft_pointer", "交付规格引用，规格本身另有归属闭环"
    ),
    ("asset_service.qc_reports", "deviation_intent_ref"): NonRef(
        "soft_pointer", "偏离意图引用，意图文档由创作域维护"
    ),
    # ---- 批次五新建表的身份列与来源列 ----
    # 身份列被派生 SQL 抓进来是**预期的**：它只看列名像 ID。
    # 判为「本表业务键」而不是 Edge，依据是它与 `entity_id` 同值且由
    # `ck_*_single_identity` 强制——两层标识如果是两个值，那就是两个身份。
    ("project_planning.scenes", "scene_id"): NonRef(
        "own_business_key", "本表身份键，与 entity_id 同值（ck_scenes_single_identity）"
    ),
    ("shot_planning.shots", "shot_id"): NonRef(
        "own_business_key", "本表身份键，与 entity_id 同值（ck_shots_single_identity）"
    ),
    ("shot_planning.shots", "source_draft_ref"): NonRef(
        "embedded_object",
        "jsonb 内嵌 {draft_id, draft_version}；外键无法表达版本化内嵌引用，来源追溯由应用层校验"
    ),
    # 契约 ShotRevision 用 `scene_id` 作归属（本闸门就按它判）。`scene_ref` 是
    # 草案期的场次业务编码，保留只为「这条修订从哪个草案版本来」的追溯；
    # 归属已由 `scene_id` 承担，故它不是第二条归属边。
    ("shot_planning.shot_revisions", "scene_ref"): NonRef(
        "soft_pointer", "草案期场次业务编码，仅作来源追溯；归属由 scene_id 承担"
    ),
    # ---- 批次十/十一：契约字段落库后新增的列 ----
    # 三条真实引用边必须建外键，不登记成 NonRef——那等于把「跨项目借用
    # 剧本依据/约束包/上一版」从判据里删掉（批次十一已建四列复合外键）。
    ("shot_planning.shot_revisions", "basis_script_revision_id"): Edge(
        "project_planning.script_revisions",
        "剧本依据必须是同项目同租户同组织的真实版本；legacy 夹具行为 NULL，由 ck_sr_basis_script 限死"
    ),
    ("shot_planning.shot_revisions", "constraint_pack_id"): Edge(
        "shot_planning.constraint_packs",
        "applicability=BOUND 时必填且须同项目；NOT_APPLICABLE 时必须为空，双向 CHECK 已落库"
    ),
    ("shot_planning.shot_revisions", "supersedes_shot_revision_id"): Edge(
        "shot_planning.shot_revisions",
        "自引用复合外键：只允许同项目的上一版修订被取代；首版为空合法"
    ),
    # 三条非边：jsonb 内嵌精确版本引用，单列外键表达不了；地点是外部
    # 主数据/文本引用，不决定项目归属（归属由 scene_id 承担）。
    ("shot_planning.shot_revisions", "intent_ref"): NonRef(
        "embedded_object", "jsonb 内嵌 {intent_id, intent_version}，精确版本引用且由契约 schema 校验结构"
    ),
    ("shot_planning.shot_revisions", "source_draft_ref"): NonRef(
        "embedded_object", "jsonb 内嵌 {draft_id, draft_version}；与 shots.source_draft_ref 同类"
    ),
    ("shot_planning.shot_revisions", "location_ref"): NonRef(
        "soft_pointer", "地点为外部主数据引用/文本，不是项目实体；归属由 scene_id 承担"
    ),
    ("generation_execution.generation_tasks", "budget_reservation_ref"): NonRef(
        "reservation", "预算预留单号，由预算域生成"
    ),
    ("generation_execution.generation_tasks", "quota_reservation_ref"): NonRef(
        "reservation", "配额预留单号，由配额域生成"
    ),
    ("generation_execution.generation_tasks", "constraint_snapshot_ref"): NonRef(
        "soft_pointer", "约束快照引用，快照是不可变历史，不参与归属链"
    ),
    ("generation_execution.generation_tasks", "result_ref"): NonRef(
        "external_artifact", "任务产物引用，指向生成结果（库外对象）"
    ),
    ("generation_execution.generation_tasks", "rights_manifest_ref"): NonRef(
        "external_artifact", "权利清单外部引用"
    ),
    ("generation_execution.generation_tasks", "shot_spec_ref"): NonRef(
        "soft_pointer", "规格软引用；任务与规格的归属已各自锁定，此列是使用关系而非从属关系"
    ),
    ("generation_execution.generation_tasks", "visual_plan_ref"): NonRef(
        "soft_pointer", "视觉方案软引用，同 shot_spec_ref"
    ),
    ("qc_governance.gate_decisions", "constraint_snapshot_id"): NonRef(
        "soft_pointer", "约束快照标识，快照为不可变历史"
    ),
    ("shot_planning.shot_specs", "previz_ref"): NonRef(
        "soft_pointer", "预演引用，预演产物另有版本链"
    ),
    ("workflow.approval_flows", "resource_id"): NonRef(
        "polymorphic", "多态引用：目标表由 resource_type 决定，单条外键无法表达"
    ),
    ("workflow.approval_steps", "voter_subject_id"): NonRef(
        "subject_identity", "投票主体标识，属身份域"
    ),
    ("project_planning.episodes", "episode_ref"): NonRef(
        "own_business_key", "本表自身的业务键（与 project_id 组成唯一键），不是对父行的引用"
    ),

    # ---- 批次五新建的两张业务对象表 ----
    # `scenes` / `shots` 自己也是归属表，它们的身份列与引名列必须同样被认领。
    # 新建表第一次让这些边进入视野：以前表不存在，没有人问过它们指向谁。
    ("project_planning.scenes", "episode_id"): Edge(
        "project_planning.episodes", "场次唯一的上行归属锚点（scene_id → Scene → Episode）"
    ),
    ("shot_planning.shots", "scene_id"): Edge(
        "project_planning.scenes", "镜头归属经场次收敛到集，不由自身推导"
    ),
    ("shot_planning.shots", "shot_revision_id"): Edge(
        "shot_planning.shot_revisions", "当前正式版本指针（与首个修订同事务创建）"
    ),
    ("shot_planning.shot_revisions", "episode_id"): Edge(
        "project_planning.episodes", "正式修订按身份归集（契约 ShotRevision.episode_id）"
    ),
    # 返工依据由批次九建成真实对象，边仍登记为 Edge：目标表现在存在，
    # 由复合外键 (project_id, rework_basis_ref) → (project_id, rework_task_id) 强制。
    # 若把它改成 NonRef，就等于把「授权可跨项目借用」这件事从判据里删掉。
    ("shot_planning.shots", "rework_basis_ref"): Edge(
        "workflow.rework_tasks",
        "返工依据引用（ReworkTask 授权记录）；回退到 IN_PRODUCTION 时必须能指回授权",
    ),

    # ---- 批次九新表：只有决定 project 归属的列才登记为 Edge ----
    # 其余四列是「跟着别人的归属走」或「本表自己的键」，不是父子归属边。
    # 全登记成 Edge 会让判据要求给每列建外键，那是另一种形式的假阳性。
    ("workflow.rework_tasks", "rework_task_id"): NonRef(
        "own_business_key", "与 entity_id 同值的本表业务键（CHECK 强制），不是指向别处的引用"
    ),
    ("workflow.rework_tasks", "constraint_pack_ref"): NonRef(
        "soft_pointer", "追溯用约束包标识；返工任务的项目归属由自身 project_id 承担"
    ),
    ("workflow.rework_tasks", "issue_id"): NonRef(
        "soft_pointer",
        "缺陷单号是跨模块软引用；QCIssue 对象契约已登记但库内无实体表，"
        "它的实施是独立债务，不决定本行的项目归属",
    ),
    ("workflow.rework_tasks", "target_ref"): NonRef(
        "polymorphic", "多态目标引用（目标类型由业务上下文决定），无法用单条外键表达"
    ),

    # ---- 父表在库中尚不存在：显式登记为悬空边，见 DANGLING 说明 ----
    # 这四类不写成 NonRef —— 它们是**真实设计的归属边**，只是目标表还没建。
    # 归类为「不是边」会把「未实施」永久洗白，那正是本项目反复踩的坑。
    ("shot_planning.constraint_packs", "scene_id"): Edge(
        "project_planning.scenes", "约束包作用于某个场次"
    ),
    # 正式修订按**身份**归场次：契约 ShotRevision.scene_id 是身份键，
    # 旧列 `scene_ref` 是草案期业务编码（见 EXEMPTIONS），不是归属事实。
    ("shot_planning.shot_revisions", "scene_id"): Edge(
        "project_planning.scenes", "镜头修订按场次组织"
    ),
    ("shot_planning.shot_revisions", "shot_id"): Edge(
        "shot_planning.shots", "修订属于某个镜头身份"
    ),
    ("shot_planning.shot_specs", "shot_id"): Edge(
        "shot_planning.shots", "规格属于某个镜头身份"
    ),
    ("shot_planning.shot_visual_plans", "shot_id"): Edge(
        "shot_planning.shots", "视觉方案属于某个镜头身份"
    ),
    ("generation_execution.generation_tasks", "shot_id"): Edge(
        "shot_planning.shots", "生成任务服务于某个镜头"
    ),
    ("generation_execution.shot_route_selections", "shot_id"): Edge(
        "shot_planning.shots", "路线选择是针对某个镜头做的"
    ),
    # ---- 组织级边：父表自身没有 project_id，一致性只能锁到组织层 ----
    # 不单独建一张「组织级边」表：父表有没有 project_id 是**库里的事实**，
    # 由判定函数实读后自动选择规则。另立一张表等于把同一个事实写两遍，
    # 父表结构一变就会静默错判——这正是 R1 要禁的第二事实源。
    ("generation_execution.generation_tasks", "route_id"): Edge(
        "generation_execution.production_routes", "任务使用某条生成路线；路线为全组织复用"
    ),
    ("generation_execution.shot_route_selections", "route_id"): Edge(
        "generation_execution.production_routes", "路线选择指向某条生成路线"
    ),
}

# ---------------------------------------------------------------------------
# 带可机检前提的豁免
# ---------------------------------------------------------------------------
#: 「不建外键」有两种，必须分开：
#:   * `NonRef`——它本来就不是对象引用（`audit_ref` 之类），永远不建；
#:   * `Exempt`——看着像引用，但**当前**按契约不该建，且这个判断有可机检依据。
#:
#: 只写文字理由的豁免清单一定会退化成垃圾桶：三个月后没人知道那条理由还成不成立，
#: 于是「暂时不建」永久留在里面。故每条豁免必须带一个能从契约文件/系统目录**求值**
#: 的前提；前提一旦不成立，该列立即退回 `DANGLING`，不会因为曾经豁免过而继续豁免。
SCHEMAS_JSON = (
    Path(__file__).resolve().parents[3] / "contracts" / "schemas" / "object_schemas.schema.json"
)


@dataclass(frozen=True)
class Exempt:
    """一条豁免：一个可求值的码 + 给人看的理由。"""

    code: str
    note: str


EXEMPTIONS: dict[tuple[str, str], Exempt] = {
    (
        "shot_planning.shot_drafts", "scene_ref"
    ): Exempt(
        "DRAFT_INTERNAL",
        "`ShotBreakdownDraft` 契约把场次放在 `scene_breakdowns[]` 数组内，**没有**顶层场次"
        "引用字段；本列是草案期内部标识，不是对象引用。若将来契约补了顶层 `scene_id`，"
        "该豁免自动失效并要求建边。",
    ),
    (
        "shot_planning.shots", "cancellation_ref"
    ): Exempt(
        "AUTH_RECORD_UNMODELED",
        "取消授权所指向的「授权记录」对象**尚未建模**——`contract_amendments.yaml` 明载"
        "「专用 Approval 对象仍未建模（见 ADR-R18）」。契约要求该列在 CANCELLED 时必填，"
        "但没有任何对象可供外键指向。若将来建模为 Approval 或复用 GateDecision，此豁免自动失效。",
    ),
    (
        "qc_governance.gate_decisions", "gate_instance_id"
    ): Exempt(
        "PARENT_NO_SCHEMA",
        "`GateInstance` 无 schema——`contracts/phase0/contract_amendments.yaml`（V205-R14-A8 "
        "residual_risks）已把它登记为「下一个同型缺口」。无契约的表不能凭空建，"
        "否则造出来的是新的无约束事实源，比留一条已知缺口更坏。",
    ),
}

_defs_cache: dict | None = None


def _contract_defs() -> dict:
    global _defs_cache
    if _defs_cache is None:
        _defs_cache = json.loads(SCHEMAS_JSON.read_text(encoding="utf-8"))["definitions"]
    return _defs_cache


async def exemption_holds(conn: asyncpg.Connection, code: str) -> tuple[bool, str]:
    """求豁免前提。返回 (是否仍成立, 依据)。

    刻意**不**吞异常：契约文件缺失时这里应当炸掉，而不是静默判定「前提成立」。
    """
    defs = _contract_defs()
    if code == "DRAFT_INTERNAL":
        props = set((defs.get("ShotBreakdownDraft") or {}).get("properties") or {})
        bad = props & {"scene_id", "scene_ref"}
        if bad:
            return False, f"契约已为 ShotBreakdownDraft 增加顶层场次字段 {sorted(bad)}"
        return True, "契约确认该对象无顶层场次引用字段"
    if code == "AUTH_RECORD_UNMODELED":
        # 只认「契约里确实没有任何可承载授权的对象」。一旦出现 Approval
        # （或显式把 GateDecision 指定为授权记录），必须回来建边。
        candidates = [n for n in ("Approval", "AuthorizationRecord") if n in defs]
        if candidates:
            return False, f"契约已建模授权对象 {candidates}，须建外键"
        return True, "契约无 AuthorizationRecord / Approval，无可指向的授权对象"
    if code == "PARENT_NO_SCHEMA":
        if "GateInstance" in defs:
            return False, "契约已补 GateInstance schema，须建外键"
        exists = await conn.fetchval(
            "SELECT to_regclass('qc_governance.gate_instances') IS NOT NULL"
        )
        if exists:
            return False, "库中已有 gate_instances，但契约仍无定义（无契约的事实源，须裁撤或补契约）"
        return True, "契约无 GateInstance 且库中无该表"
    raise ValueError(f"未知豁免码：{code}")


#: 组织级边的判断不落常量：父表有无 `project_id` 由 `pg_attribute` 实读决定
#: （见 `evaluate` 里的 `parent_has_project`）。若将来父表补了 `project_id`，
#: 判定会自动从「锁组织」升级为「锁项目」，不需要改这里——把同一事实写两遍
#: 就会出现「常量说组织级、库已经是项目级」的静默错判。

Status = Literal[
    "PASS",
    "NO_FK",
    "FK_NOT_COMPOSITE",
    "PENDING_VALIDATE",
    "DANGLING",
    #: 带**可机检前提**的豁免：不是「暂时没做」，而是按契约它压根不是对象引用，
    #: 或父对象尚无 schema（无契约的表不能凭空建）。前提一旦不成立即退回正常判定，
    #: 故它不会因为「曾经豁免过」而永远豁免。
    "EXEMPT",
    "UNCLASSIFIED",
]


@dataclass
class Finding:
    table: str
    column: str
    parent: str
    status: Status
    detail: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    derived: int = 0

    def count(self, status: Status) -> int:
        return sum(1 for f in self.findings if f.status == status)

    @property
    def failed(self) -> list[Finding]:
        # `EXEMPT` 不算失败，但**也绝不折算成 PASS**：它有自己的节、
        # 自己的前提说明，混进 PASS 就等于把「按契约不建」说成「已保护」。
        return [f for f in self.findings if f.status not in ("PASS", "EXEMPT")]

    @property
    def exempt(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "EXEMPT"]

    @property
    def ok(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
FK_SQL = """
SELECT c.conname,
       c.convalidated,
       pg_get_constraintdef(c.oid) AS def
  FROM pg_constraint c
 WHERE c.contype = 'f'
   AND c.conrelid = to_regclass($1)
   AND c.confrelid = to_regclass($2)
"""


async def _find_composite(
    conn: asyncpg.Connection, child: str, col: str, parent: str
) -> tuple[list[str], list[str]]:
    """返回 (含 project_id 的合格外键列表, 只含本列的不合格外键列表)。

    刻意用 ``conkey``/``confkey`` 的**列名集合**判断，而不是 ``pg_get_constraintdef``
    的字符串匹配：约束定义的文本格式随版本变化，而列名是稳定事实。
    """
    rows = await conn.fetch(FK_SQL, child, parent)
    good: list[str] = []
    weak: list[str] = []
    for r in rows:
        cols = await conn.fetch(
            """
            SELECT a.attname
              FROM pg_constraint c
              JOIN unnest(c.conkey) AS k(attnum) ON TRUE
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
             WHERE c.oid = (SELECT oid FROM pg_constraint
                             WHERE conname = $1 AND conrelid = to_regclass($2)
                               AND confrelid = to_regclass($3))
            """,
            r["conname"],
            child,
            parent,
        )
        names = {x["attname"] for x in cols}
        if col in names and "project_id" in names:
            if r["convalidated"]:
                good.append(r["conname"])
            else:
                weak.append(r["conname"] + "（未 VALIDATE）")
        elif col in names:
            weak.append(r["conname"] + "（单列，未含 project_id）")
    return good, weak


async def evaluate(
    conn: asyncpg.Connection,
    classification: dict[tuple[str, str], Edge | NonRef] | None = None,
) -> Report:
    cls = CLASSIFICATION if classification is None else classification
    rows = await conn.fetch(DERIVE_SQL, list(BUSINESS_SCHEMAS), list(ENVELOPE_COLUMNS))
    report = Report(derived=len(rows))

    for r in rows:
        tbl, col = r["tbl"], r["col"]

        ex = EXEMPTIONS.get((tbl, col))
        if ex is not None:
            held, why = await exemption_holds(conn, ex.code)
            if held:
                report.findings.append(
                    Finding(tbl, col, "-", "EXEMPT", f"{ex.code}｜{ex.note}（前提成立：{why}）")
                )
                continue
            # 前提失效不再豁免。报 DANGLING 而不是 UNCLASSIFIED：前者说的是
            # 「这条边该建但还没建」，后者说的是「没人认领它」——处置动作不同。
            report.findings.append(
                Finding(tbl, col, "-", "DANGLING", f"豁免前提已失效：{why}；须建外键")
            )
            continue

        spec = cls.get((tbl, col))
        if spec is None:
            # 派生出来却没人认领 —— 这一条正是「清单不会自己举手」的反面：
            # 漏掉的条目在这里会**自己喊出来**，而不是安静地不参与判断。
            report.findings.append(
                Finding(tbl, col, "-", "UNCLASSIFIED",
                        "派生集中出现但未在 CLASSIFICATION 中分类")
            )
            continue

        if isinstance(spec, NonRef):
            continue

        exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", spec.parent)
        if not exists:
            report.findings.append(
                Finding(tbl, col, spec.parent, "DANGLING",
                        f"{spec.note}；目标表在库中不存在")
            )
            continue

        parent_has_project = await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM pg_attribute
                              WHERE attrelid = to_regclass($1) AND attname = 'project_id'
                                AND attnum > 0 AND NOT attisdropped)""",
            spec.parent,
        )

        if not parent_has_project:
            # 父行本身只到组织层：只能要求同组织，不要求同项目。
            ok, weak, pending = await _find_org_level(conn, tbl, col, spec.parent)
            if ok:
                report.findings.append(Finding(tbl, col, spec.parent, "PASS",
                                               f"组织级边：{ok[0]}"))
            elif pending:
                # 「已建未 VALIDATE」不能报成 NO_FK：前者只需一条 VALIDATE，
                # 后者要重新设计约束。混为一谈会让运维重复建键，而真正待办
                # （跑 VALIDATE）永远不出现在清单里。
                report.findings.append(
                    Finding(tbl, col, spec.parent, "PENDING_VALIDATE",
                            f"组织级边已建、未验证：{'; '.join(pending)}")
                )
            else:
                report.findings.append(
                    Finding(tbl, col, spec.parent, "NO_FK",
                            f"{spec.note}；父表为组织级（无 project_id），"
                            f"需两列外键 (col, organization_id)"
                            + (f"；现有：{'; '.join(weak)}" if weak else ""))
                )
            continue

        good, weak = await _find_composite(conn, tbl, col, spec.parent)
        if good:
            report.findings.append(Finding(tbl, col, spec.parent, "PASS", good[0]))
        elif any("未 VALIDATE" in w for w in weak):
            report.findings.append(
                Finding(tbl, col, spec.parent, "PENDING_VALIDATE", "; ".join(weak))
            )
        elif weak:
            report.findings.append(
                Finding(tbl, col, spec.parent, "FK_NOT_COMPOSITE",
                        "只保父行存在、未锁 project 一致：" + "; ".join(weak))
            )
        else:
            report.findings.append(
                Finding(tbl, col, spec.parent, "NO_FK", spec.note)
            )
    return report


async def _find_org_level(
    conn: asyncpg.Connection, child: str, col: str, parent: str
) -> tuple[list[str], list[str], list[str]]:
    """组织级边：外键子列须 ⊇ {col, organization_id}。

    返回 (合格, 不合格, 待验证)。三者分开而不是合成一个 bool：``待验证`` 只需
    再跑一条 VALIDATE，``不合格`` 要改结构——处置完全不同，不能共用一种状态。
    """
    rows = await conn.fetch(FK_SQL, child, parent)
    good: list[str] = []
    weak: list[str] = []
    pending: list[str] = []
    for r in rows:
        cols = await conn.fetch(
            """SELECT a.attname FROM pg_constraint c
                 JOIN unnest(c.conkey) AS k(attnum) ON TRUE
                 JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                WHERE c.oid = (SELECT oid FROM pg_constraint WHERE conname = $1
                                AND conrelid = to_regclass($2) AND confrelid = to_regclass($3))""",
            r["conname"], child, parent,
        )
        names = {x["attname"] for x in cols}
        if col in names and "organization_id" in names:
            (good if r["convalidated"] else pending).append(r["conname"])
        elif col in names:
            weak.append(r["conname"])
    return good, weak, pending


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def render_md(report: Report, *, db: str, port: int, note: str) -> str:
    head = [
        "# 归属「一致」闸门（机器生成，勿手改）",
        "",
        f"- 目标库：`{db}` @ `localhost:{port}`",
        f"- 派生候选列：**{report.derived}** 条（`pg_attribute` 实读）",
        "- 判定：`PASS ⇔ 存在含 project_id 的复合外键 ∧ 指向父表 ∧ convalidated`",
        "",
    ]
    if note:
        head += [f"> {note}", ""]

    order: list[Status] = [
        "UNCLASSIFIED", "DANGLING", "NO_FK", "FK_NOT_COMPOSITE", "PENDING_VALIDATE",
        "EXEMPT", "PASS",
    ]
    rows = [f"| {s} | {report.count(s)} |" for s in order]
    head += ["| 状态 | 条数 |", "| --- | --- |", *rows, ""]

    verdict = "**整体判定：PASS**" if report.ok else "**整体判定：FAIL**"
    head += [verdict, ""]

    for s in order:
        items = [f for f in report.findings if f.status == s]
        if not items:
            continue
        head += [f"## {s}（{len(items)}）", ""]
        if s == "PASS":
            head += ["| 表 | 列 | 父表 | 约束 |", "| --- | --- | --- | --- |"]
            head += [f"| `{f.table}` | `{f.column}` | `{f.parent}` | `{f.detail}` |"
                     for f in sorted(items, key=lambda x: (x.table, x.column))]
        else:
            head += ["| 表 | 列 | 父表 | 说明 |", "| --- | --- | --- | --- |"]
            head += [f"| `{f.table}` | `{f.column}` | `{f.parent}` | {f.detail} |"
                     for f in sorted(items, key=lambda x: (x.table, x.column))]
        head.append("")

    dangling = [f for f in report.findings if f.status == "DANGLING"]
    if dangling:
        head += [
            "## DANGLING 的性质与处置边界",
            "",
            "这些是**设计上真实存在**的归属边，目标表（`shots` / `scenes` / `gate_instances`）",
            "在库中尚未创建。不能靠外键修复，也不能归入「不是边」——归入即等于把",
            "「运行时未实施」永久洗白。正确处置是补建目标表，属镜头域运行时落地范畴。",
            "",
            "在此之前，这些列仍是**裸字符串**：写一个不存在的 `shot_id` 不会报错。",
            "这一点必须留在报告里，不得因为「其它都 PASS」而被读成保护完备。",
            "",
        ]
    return "\n".join(head)


def dump_json(report: Report) -> str:
    return json.dumps(
        {
            "derived": report.derived,
            "ok": report.ok,
            "counts": {s: report.count(s) for s in
                       ("PASS", "NO_FK", "FK_NOT_COMPOSITE", "PENDING_VALIDATE",
                        "DANGLING", "UNCLASSIFIED")},
            "findings": [f.__dict__ for f in report.findings],
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# 自检：证明它能在坏状态下 FAIL，且失败原因正确
# ---------------------------------------------------------------------------
BASELINE_KEEP = "g7b8c9d0e1f2"  # 一致半边修复之前归属栈的 head


async def selftest() -> int:
    """五个场景，两侧各证伪一次。

    * S0 —— 修复前的库必须红，且红的原因**精确**（指定列必须是 NO_FK / DANGLING）。
      没有这一条，「全绿」可能只是因为没有可比对的候选列。
    * S1 —— 挖掉一条分类，必须报 UNCLASSIFIED（证明分类不是摆设）。
    * S2 —— 把一条边的父表改成不存在的表，必须报 DANGLING（证明它真去查了目标）。
    * S3 —— 把一条边的父表改成组织级父表，必须走组织级判定而不是误报 PASS。
    * S4 —— 豁免的前提一旦不成立，必须自动退回 DANGLING（证明豁免不是白名单）。
    """
    admin = await lab.admin_conn(lab.DEFAULT_HOST, lab.DEFAULT_PORT,
                                lab.DEFAULT_USER, lab.DEFAULT_PASSWORD)
    labdb = "ai_film_consistency_selftest"
    results: list[tuple[str, bool, str]] = []
    try:
        await lab.fresh_clone(admin, labdb, lab.DEFAULT_TEMPLATE)
        conn = await lab.lab_conn(lab.DEFAULT_HOST, lab.DEFAULT_PORT,
                                  lab.DEFAULT_USER, lab.DEFAULT_PASSWORD, labdb)
        try:
            # 回到「一致半边未修」的基线：撤掉批次四（若已存在）。
            rewound = await lab.rewind_to(conn, VERSIONS_DIR, keep=BASELINE_KEEP)
            report = await evaluate(conn)

            must_fail = {
                # 已有单列外键、但不含 project_id —— 必须判 FK_NOT_COMPOSITE，
                # 而不是「有外键就算 PASS」。这是本闸门最容易写成恒真的地方。
                ("asset_service.qc_reports", "asset_version_id"): "FK_NOT_COMPOSITE",
                ("shot_planning.shot_specs", "shot_revision_id"): "NO_FK",
                ("workflow.approval_tickets", "flow_id"): "NO_FK",
                ("shot_planning.shot_revisions", "shot_id"): "DANGLING",
            }
            got = {(f.table, f.column): f.status for f in report.findings}
            bad = [f"{t}.{c} 期望 {s} 实得 {got.get((t, c), '缺失')}"
                   for (t, c), s in must_fail.items() if got.get((t, c)) != s]
            results.append((
                "S0 修复前必须红且原因精确",
                not bad and not report.ok,
                f"回退 {len(rewound)} 层；FAIL {len(report.failed)} 条"
                + ("；" + "；".join(bad) if bad else ""),
            ))

            # S1：挖掉一条分类
            mutated = dict(CLASSIFICATION)
            mutated.pop(("asset_service.qc_reports", "asset_version_id"))
            r1 = await evaluate(conn, mutated)
            hit = [f for f in r1.findings
                   if f.table == "asset_service.qc_reports"
                   and f.column == "asset_version_id"
                   and f.status == "UNCLASSIFIED"]
            results.append(("S1 未分类必须被点名", bool(hit),
                            f"UNCLASSIFIED {r1.count('UNCLASSIFIED')} 条"))

            # S2：父表改成不存在的表
            mutated = dict(CLASSIFICATION)
            mutated[("asset_service.qc_reports", "asset_version_id")] = Edge(
                "asset_service.no_such_table", "selftest")
            r2 = await evaluate(conn, mutated)
            hit = [f for f in r2.findings
                   if f.table == "asset_service.qc_reports"
                   and f.column == "asset_version_id"
                   and f.status == "DANGLING"]
            results.append(("S2 目标表不存在必须报 DANGLING", bool(hit),
                            "已命中" if hit else "未命中"))

            # S3：改指组织级父表 —— 不能因为「父表没有 project_id」就误判 PASS
            mutated = dict(CLASSIFICATION)
            mutated[("asset_service.qc_reports", "asset_version_id")] = Edge(
                "generation_execution.production_routes", "selftest")
            r3 = await evaluate(conn, mutated)
            f3 = [f for f in r3.findings
                  if f.table == "asset_service.qc_reports"
                  and f.column == "asset_version_id"]
            results.append((
                "S3 组织级父表走组织级判定",
                bool(f3) and f3[0].status == "NO_FK",
                f3[0].status if f3 else "缺失",
            ))

            # ---- S4：豁免前提失效必须自动退回 ----
            # 豁免的全部价值在「前提一不成立就自己失效」。只证明它此刻生效、
            # 不证明它**会**失效，等于给了一个永久白名单——那正是要防的东西。
            # 这里在克隆库里凭空建一张 `gate_instances`（无契约的表），
            # 豁免的前提（契约无定义 ∧ 库中无表）立刻不成立，必须退回 DANGLING。
            base = {(f.table, f.column): f.status for f in report.findings}.get(
                ("qc_governance.gate_decisions", "gate_instance_id"))
            await conn.execute(
                "CREATE TABLE qc_governance.gate_instances (entity_id varchar PRIMARY KEY)"
            )
            try:
                r4 = await evaluate(conn)
            finally:
                await conn.execute("DROP TABLE qc_governance.gate_instances")
            f4 = {(f.table, f.column): f for f in r4.findings}.get(
                ("qc_governance.gate_decisions", "gate_instance_id"))
            results.append((
                "S4 豁免前提失效必须退回 DANGLING",
                base == "EXEMPT" and f4 is not None and f4.status == "DANGLING",
                f"建表前={base} 建表后={f4.status if f4 else '缺失'}",
            ))
        finally:
            await conn.close()
    finally:
        await lab.drop_lab(admin, labdb)
        await admin.close()

    print("\n自检结果")
    all_ok = True
    for name, ok, detail in results:
        print(f"  [{'OK' if ok else 'FAIL'}] {name} —— {detail}")
        all_ok = all_ok and ok
    print(f"\n{'全部通过' if all_ok else '存在失败'}：{sum(1 for _, o, _ in results if o)}/{len(results)}")
    return 0 if all_ok else 1


async def main_async(args: argparse.Namespace) -> int:
    conn = await asyncpg.connect(host=args.host, port=args.port, user=args.user,
                                 password=args.password, database=args.db)
    try:
        report = await evaluate(conn)
    finally:
        await conn.close()

    md = render_md(report, db=args.db, port=args.port, note=args.note)
    OUT_MD.write_text(md, encoding="utf-8")
    OUT_JSON.write_text(dump_json(report), encoding="utf-8")

    print(f"派生候选 {report.derived} 条；FAIL {len(report.failed)} 条")
    for s in ("UNCLASSIFIED", "DANGLING", "NO_FK", "FK_NOT_COMPOSITE",
              "PENDING_VALIDATE", "EXEMPT", "PASS"):
        print(f"  {s:18s} {report.count(s)}")
    if report.failed:
        print("\n未通过的边：")
        for f in sorted(report.failed, key=lambda x: (x.table, x.column)):
            print(f"  [{f.status:18s}] {f.table}.{f.column} -> {f.parent}")
    print(f"\n报告：{OUT_MD.name} / {OUT_JSON.name}")
    return 0 if report.ok else 1


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=lab.DEFAULT_TEMPLATE)
    p.add_argument("--host", default=lab.DEFAULT_HOST)
    p.add_argument("--port", type=int, default=lab.DEFAULT_PORT)
    p.add_argument("--user", default=lab.DEFAULT_USER)
    p.add_argument("--password", default=lab.DEFAULT_PASSWORD)
    p.add_argument("--note", default="")
    p.add_argument("--selftest", action="store_true",
                   help="在克隆库上回退批次四并证明闸门能 FAIL")
    args = p.parse_args(list(argv) if argv is not None else None)
    if args.selftest:
        return asyncio.run(selftest())
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
