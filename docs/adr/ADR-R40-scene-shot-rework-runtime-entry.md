# ADR-R40：Scene / Shot / ShotRevision / ShotBreakdownDraft / ReworkTask 运行时入口

- 状态：PROPOSED → 待 Phase 1 复核固化
- 日期：2026-11-20
- 关联：ADR-R16（拆镜草案）、ADR-R17（镜头修订）、ADR-R18（Shot 独立建模）、
  ADR-R19（Scene 独立建模）、ADR-R23（状态迁移事实表）、ADR-R25（最小权限授权）
- 契约修订：V205-R14-A13

---

## 1. 背景：一条闭环的硬阻塞链

ADR-R18/R19 已把 Scene 与 Shot 的对象契约钉死，但运行时一行代码都没有。核对结果是
一条首尾相接的阻塞链，任何一环单独实现都会立刻撞上下一环：

```
Shot 的 required 字段含 shot_revision_id
  → 必须有 ShotRevision 对象（main 无表、无运行时）
  → ShotRevision 的唯一创建入口是 SHOT.REVISION.CONFIRM
  → 该动作的输入是 ShotBreakdownDraft（main 无表、无运行时）
  → 草案的唯一创建入口是 SHOT.DRAFT.CREATE（已登记 ACTIVE，未实现）
```

同时 Shot 的十条转移边里有 6 条带守卫，守卫依赖四个字段。字段本身已落列，但其中
两条指向的对象在库里不存在：

| 守卫边 | 依赖字段 | 依赖对象 | main 现状 |
|---|---|---|---|
| `PRODUCTION_COMPLETE → IN_PRODUCTION` | `rework_basis_ref` | `ReworkTask` | 契约有定义，**无表** |
| `NOT_STARTED/IN_PRODUCTION/BLOCKED → CANCELLED` | `cancellation_ref` | 授权记录 | **无任何契约对象** |

这解释了为什么本轮不能只做「Scene + Shot 两张表」：Shot 的必填字段与守卫边
会把缺的东西一次次拽出来。把链一次性接通，比留三处半成品更便宜，也更安全。

## 2. 决策（项目所有者 2026-11-20 拍板）

| 编号 | 议题 | 裁定 |
|---|---|---|
| D1 | ReworkTask 处置 | **随本批建表**，不登记为债务 |
| D2 | 授权记录承载 | **`authority_ref` 字符串承载**，不新建契约对象 |
| D3 | 交付范围 | **表 + API + 动作全出** |
| D4 | 4 条无入口边 | **边与入口同时开放**，不登记为债务 |

## 3. 必然推论（不是新决策，是既有契约的算术结果）

### 3.1 ShotRevision 与 ShotBreakdownDraft 必须在同一批

`object_schemas.Shot.required` 含 `shot_revision_id`，`contract_model("Shot")` 会把它
生成成必填字段（`create_model(name, shot_revision_id=(str, ...))`）。只要 Shot 的路由
带 `response_model=contract_model("Shot")`，响应里缺这个字段就是 500。

因此「Shot 全出」在物理上等于「ShotRevision 全出」。而 ShotRevision 的唯一合法来源是
`SHOT.REVISION.CONFIRM`，其输入是 `ShotBreakdownDraft`，后者的唯一来源是
`SHOT.DRAFT.CREATE`。**四个对象是一个事务链，不能拆批。**

这也正是 R18 §12 已经写下的设计：首次人工确认时，在**同一事务**内原子创建
`Shot` 与首个 `ShotRevision`。本 ADR 只是把它的前置也补齐。

### 3.2 Scene 的创建入口：独立的 `SCENE.CREATE`

R19 §121 明确记录「Scene 由谁创建，正文没写」，并拒绝擅自把
`SHOT.REVISION.CONFIRM` 的 `resource_type` 扩成 `Shot/ShotRevision/Scene`。

本 ADR 沿用该克制：新增独立的 `SCENE.CREATE`，与 `EPISODE.CREATE` 完全同型
（同层级、同归属校验、同幂等语义）。**不**让拆镜确认动作隐式建场次——那等于替
所有者决定「确认拆镜即建立正式场次」，是不可逆的语义扩张。

`Scene` 的三条边全部归 `SCENE.STATUS.CHANGE`，与 Episode 同构。

### 3.3 ReworkTask 归属 project_planning 域

ReworkTask 有三个候选落点：project_planning、asset_service、新域。

选 project_planning，理由是**依赖方向**而不是语义偏好：

1. `Shot.rework_basis_ref` 的守卫需要确认「引用的 ReworkTask 真实存在且状态合法」。
   若 ReworkTask 在 asset_service，这条校验就是跨服务调用，违反 R2（服务间不得
   互相 import），只能退化成「字符串非空」——守卫名存实亡。
2. 同域后，这条校验是一次同 schema 查询，可以用**复合外键**在库层锁死，不靠 Service。
3. 不新增第三个域角色。D4 的最小权限矩阵当前是二域四角色，新增域意味着新增角色、
   新增默认权限、新增跨域只读列清单——成本远大于收益。

代价：`affected_asset_version_ids` 是指向资产域的 ID 数组，只能做值引用、不能加外键。
这是已登记的真实限制，写在 §7 债务里，不假装它不存在。

## 4. 动作增量

已登记但未实现（本轮实现）：

| action_key | 资源 | 说明 |
|---|---|---|
| `SHOT.DRAFT.CREATE` | ShotBreakdownDraft | 草案创建（AI 拆镜产出，默认草案） |
| `SHOT.REVISION.CONFIRM` | Shot/ShotRevision | 确认：原子创建 Shot + 首个 ShotRevision |

本轮新增登记：

| action_key | 资源 | 拥有的边 |
|---|---|---|
| `SCENE.CREATE` | Scene | —（创建，不改状态） |
| `SCENE.READ` | Scene | — |
| `SCENE.STATUS.CHANGE` | Scene | 3 条（DRAFT→ACTIVE、DRAFT→ARCHIVED、ACTIVE→ARCHIVED） |
| `SHOT.READ` | Shot | — |
| `SHOT.STATUS.CHANGE` | Shot | 10 条（含 6 条守卫边） |
| `SHOT.DRAFT.CONFIRM` | ShotBreakdownDraft | 2 条（PENDING_CONFIRM→IN_REVIEW、IN_REVIEW→CONFIRMED） |
| `SHOT.DRAFT.RETURN` | ShotBreakdownDraft | 1 条（IN_REVIEW→RETURNED） |
| `SHOT.DRAFT.DISCARD` | ShotBreakdownDraft | 2 条（PENDING_CONFIRM→DISCARDED、IN_REVIEW→DISCARDED） |
| `REWORK.TASK.CREATE` | ReworkTask | — |
| `REWORK.TASK.STATUS.CHANGE` | ReworkTask | 4 条（OPEN→IN_PROGRESS→DONE→VERIFYING→CLOSED） |

`SHOT.DRAFT.CONFIRM` 拥有两条边是刻意的：草案的「认领」与「确认」是同一个用例的
两个阶段（先抢占审阅权，避免两人同时确认；再落确认结论）。一次调用在同一事务内
连走两跳，写两条迁移事实，两条事实的 `owner_action` 相同。若拆成两个动作，
「认领」在契约里没有任何业务语义，只是一个技术锁——那不该占据一个动作位。

`REWORK.TASK.STATUS.CHANGE` 不含 `REJECTED` 的入边：契约已记录该状态「合法但当前
无授权入口」，本批不擅自补边（补边需要所有者给出「谁有权驳回返工」的口径）。

## 5. 事件增量

| event_type | 触发点 |
|---|---|
| `SCENE_CREATED` | SCENE.CREATE |
| `SCENE_STATUS_CHANGED` | SCENE.STATUS.CHANGE |
| `SHOT_DRAFT_CREATED` | SHOT.DRAFT.CREATE |
| `SHOT_DRAFT_CONFIRMED` | SHOT.DRAFT.CONFIRM 的第二跳 |
| `SHOT_DRAFT_RETURNED` | SHOT.DRAFT.RETURN |
| `SHOT_DRAFT_DISCARDED` | SHOT.DRAFT.DISCARD |
| `SHOT_CREATED` | SHOT.REVISION.CONFIRM（首次确认） |
| `SHOT_STATUS_CHANGED` | SHOT.STATUS.CHANGE |
| `SHOT_REVISION_CONFIRMED` | SHOT.REVISION.CONFIRM（已登记，本轮真发） |
| `REWORK_TASK_CREATED` | REWORK.TASK.CREATE |
| `REWORK_TASK_STATUS_CHANGED` | REWORK.TASK.STATUS.CHANGE |

`SHOT.DRAFT.CONFIRM` 的第一跳（认领）发 `SHOT_DRAFT_CLAIMED`，第二跳（确认）发
`SHOT_DRAFT_CONFIRMED`，两条迁移事实 + 两个事件在同一事务内落库。为什么认领要独立
成事件：它是并发控制的**可观测点**——出现「两人同时确认」的争议时，必须能查到
「谁在什么时刻先认领」，否则事后只能看到一条 CONFIRMED，无法解释另一个请求为何失败。

## 6. 数据模型

五张表，全部落 `project_planning` schema（复用既有角色与 RLS 机制）：

| 表 | 归属外键 | 关键约束 |
|---|---|---|
| `scenes` | `(project_id, episode_id) → episodes(project_id, entity_id)` | `uq_scene_code_per_project (project_id, scene_code)`；`ck_scenes_identity_single`；status CHECK |
| `shots` | `(project_id, scene_id) → scenes(project_id, entity_id)` | `uq_shot_code_per_project (project_id, shot_code)`；`ck_shots_identity_single`；`ck_shots_evidence_consistency`；status CHECK；`rework_basis_ref` 可空外键 → `rework_tasks.entity_id` |
| `shot_revisions` | `(project_id, shot_id) → shots(project_id, entity_id)` | `uq_shot_revision_no (shot_id, revision_no)`；`ck_shot_revisions_identity_single`；部分唯一索引：每 Shot 恰一个 ACTIVE；status CHECK |
| `shot_breakdown_drafts` | `project_id → projects.entity_id` | status CHECK；`draft_version` 单调 |
| `rework_tasks` | `project_id → projects.entity_id` | status CHECK；`target_ref` 非空 |

> **编号唯一域修正（2026-11-20）**：本 ADR 初稿把唯一域写成父级（集内 / 场次内），
> 与冻结契约 SC-3 / SH-3「编号在**项目内**唯一」不符，属未登记的实现偏离。已按
> 契约收敛为 `(project_id, scene_code)` / `(project_id, shot_code)`：契约是权威源，
> 实现弱于契约不能靠注释解释过去。
>
> **身份单一下沉**：SC-1 / SH-1 / SR-1 原先只有领域层校验，现补三条 CHECK，使
> 「绕过 Service 直连 DB」的写入也失败。

### 6.1 守卫字段落列

`shots` 表必须有四列，否则 6 条守卫边没有承载：

| 列 | 类型 | 语义 |
|---|---|---|
| `blocked_reason` | `text NULL` | 进入 BLOCKED 的原因 |
| `rework_basis_ref` | `varchar(64) NULL` | 指向 `rework_tasks.entity_id` |
| `cancellation_ref` | `varchar(128) NULL` | 授权记录引用（D2：字符串，不建对象） |
| `cancelled_at` | `timestamptz NULL` | 取消时间 |

数据库层再加一条 CHECK，把「状态证据自洽」从领域层下沉：

```sql
CHECK (
  (status = 'BLOCKED' AND blocked_reason IS NOT NULL)
  OR (status = 'CANCELLED' AND cancellation_ref IS NOT NULL AND cancelled_at IS NOT NULL)
  OR (status NOT IN ('BLOCKED', 'CANCELLED')
      AND blocked_reason IS NULL AND cancellation_ref IS NULL AND cancelled_at IS NULL)
)
```

这条 CHECK 与 `object_schemas` 里 Shot 的 `statement` 逐字对应。领域层仍然校验
（快速失败 + 结构化错误），库层再兜一次——两层不是重复，是「不变量放最可靠层」
（R2）与「错误要可读」两个目标的各自落点。

### 6.2 授权

按 ADR-R25 的矩阵，五张表都归 `project_planning_role`，权限 `SELECT INSERT UPDATE`，
**无 DELETE**（删除语义是 ARCHIVED / DISCARDED / SUPERSEDED）。

`rework_tasks` 的 `affected_asset_version_ids` 是值引用，不授资产表任何额外权限。

## 7. 已知债务（本批不解决，必须登记）

1. **`affected_asset_version_ids` 无外键**：跨域 ID 数组，库层无法约束。
   资产被删除后引用会悬空。缓解：消费方只做展示与追溯，不做存在性强校验。
2. **`SHOT.STATUS.CHANGE` 的取消守卫只校验非空**：`cancellation_ref` 是字符串，
   无法证明它指向一次真实授权。这是 D2 的直接代价——用「引用可被审计系统解析」
   替代「引用在库层可验证」。缓解：`authority_ref` 与 `evidence_refs` 同时写入
   `audit.status_transitions`，审计侧可按前缀路由到签发系统。
3. **`ReworkTask.REJECTED` 无入边**：契约已登记，本批不开。
4. ~~**Scene 归档不校验「其下无 IN_PRODUCTION 镜头」**~~ → **已在本批补齐**（A14）。
   实现位置：`SceneService.change_status` 归档分支，同事务内 `has_with_status()` 判定，
   命中抛 `STATE_TRANSITION_DENIED`。残余风险转为「查询与写入之间的并发窗口」：
   另一事务可在此间隙把某镜头推入 IN_PRODUCTION，唯一约束无法表达该不变量。
   彻底解法是归档走行锁重查，属未实现（已登记 residual_risk）。
5. **`SH-2` 后半句无库层承载**：`Shot` 与同一 `shot_id` 的全部修订，其
   `project_id / episode_id / scene_id` 必须完全一致。`shot_revisions` 只有
   `(project_id, shot_id)` 一条外键，`episode_id / scene_id` 与 Shot 是否一致
   由服务写入时保证。故 SH-2 保持 `RUNTIME_PENDING`，不得随 SC-2 一起改判。
6. **`SH-4` 版本指针无库层约束**：`shots.shot_revision_id` 不是外键，
   「必须指向存在且 ACTIVE 的修订」只有服务层保证。补外键会与「首次确认先建修订、
   后落 Shot」的写入顺序冲突（外键要求被引用行先存在），需单独设计，
   例如可延迟约束或先落 Shot 的 DRAFT 占位——两者都要改写入协议，不在本批。
7. **`ShotRevision` 的部分唯一索引（每 Shot 一个 ACTIVE）在并发确认下依赖唯一约束**：
   若两个确认请求同时通过版本校验，第二个会撞唯一索引并抛 409——这是正确行为，
   但需要真实 PG 负测证明它确实拦住了。

## 8. 被拒绝的替代方案

| 方案 | 拒绝理由 |
|---|---|
| 只做 Scene + Shot，ShotRevision 先留空 | `Shot.shot_revision_id` 是 required，响应模型直接 500；等于交付一个必然报错的端点 |
| ReworkTask 只登记债务不建表 | 与 D1/D4 矛盾；`rework_basis_ref` 将永远无法校验，返工边成为绕过验收的暗门 |
| 授权记录新建成 `ApprovalRecord` | 等于再造一个域（契约 + 表 + 动作 + 全链路），且与 A12 的 `StatusTransition` 职责重叠 |
| 让 `SHOT.REVISION.CONFIRM` 顺带创建 Scene | 替所有者决定语义，且不可逆；R19 已明确拒绝 |
| ReworkTask 落 asset_service | 守卫校验被迫跨服务，违反 R2 或退化为字符串非空 |

## 9. 验收判据

1. 五张表的迁移在真实 PostgreSQL 上可 upgrade / downgrade 往返，`alembic check` 无差异。
2. `lifecycle.edge_ownership` 覆盖 5 个对象（Scene 3 边、Shot 10 边、ShotDraft 5 边、
   ShotRevision 1 边、ReworkTask 4 边），无「无主边」。
3. 6 条 Shot 守卫各有负向用例：缺字段 → 拒绝，且拒绝发生在**领域层**而非库层（库层
   CHECK 作为第二道，负测要能区分）。
4. 首次确认在同一事务内产出 1 个 Shot + 1 个 ShotRevision + 2 条迁移事实 + 2 条事件；
   事务中途失败时四者全无。
5. 跨租户 / 跨组织访问返回 404（不是 403），不泄漏对象存在性。
6. 五个业务角色对五张新表无 DELETE / TRUNCATE。
7. 项目内编号唯一：同一项目下跨集登记同名 `scene_code`（或跨场次同名 `shot_code`）
   必须被拒，且错误语义是「编号被占用」而不是「幂等键复用」。
8. 归档前置条件：场次下存在 `IN_PRODUCTION` 镜头时归档被拒，且**状态未变、
   无迁移事实**——「先改状态再回滚」不算拒绝。
9. 身份单一不可绕过：直接以 SQL 写入 `scene_id <> entity_id`（同理 `shot_id` /
   `shot_revision_id`）必须被 CHECK 拒绝，证明不变量不在服务层。
10. 不变量定级不虚高：任何标 `RUNTIME_ENFORCED` 的不变量，其对象必须已有运行时
    状态机；`test_scene_contract.py` 与 `test_shot_contract.py` 共同钉死当前账目。
