# ADR-R40：Scene / Shot / ShotRevision / ShotBreakdownDraft / ReworkTask 运行时入口

- 状态：PROPOSED → 待 Phase 1 复核固化
- 日期：2026-11-20
- 修订：2026-11-21 补 §6.3（SC-5 父行锁协议）、§6.4（23505 逐路径核对），
  债务 4 / 7 结清
- 关联：ADR-R16（拆镜草案）、ADR-R17（镜头修订）、ADR-R18（Shot 独立建模）、
  ADR-R19（Scene 独立建模）、ADR-R23（状态迁移事实表）、ADR-R25（最小权限授权）
- 契约修订：V205-R14-A13、V205-R14-A14、V205-R14-A15

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

### 6.0 行级安全（初版遗漏，已补）

初版迁移只建表、配授权、下沉约束，**没有给五张表加任何 RLS 策略**。后果不是
「少一层防御」，而是这五张表在库层完全敞开：`project_planning_role` 在任意上下文里
都能读出全部租户、全部组织的场次与镜头，组织边界退回「只能靠应用层 `authorize`
判定」——正是 `e9f0a1b2c3d4` 迁移说明里记录过的那个失效模式。

漏检的原因在判据而不在代码：组织边界断言 `scope.org_rls_enforced` 当时只对一份
**手写表清单**求值，新表不在清单里就根本不参与判定；取数点也只回放**一个**固定的
迁移文件。两处写死让「加表」等于「漏检」。已同时修掉：

| 侧 | 改动 |
|---|---|
| 实现 | 迁移 `a13b0c1d2e3f` 为五张表 `ENABLE` + `CREATE POLICY` + `FORCE ROW LEVEL SECURITY`；谓词由 `tenant_scope_predicate()` 与父项目存在性生成，不手抄 SQL |
| 判据 | 应检集合改为从真实授权矩阵派生（`_tenant_scoped_business_tables()`），未登记的业务表直接报红；语句来源改为按版本链回放**全部**迁移 |
| 证据 | `tests/integration/test_scene_rls_isolation.py`：catalogs 上 `relrowsecurity`/`relforcerowsecurity`、跨租户读 0 本租户读 1、跨组织读 0、跨租户写被拒（42501） |

子表不存 `organization_id`：组织规则由 `projects` 自己的策略在 `EXISTS` 子查询内
执行，与 `episodes` / `script_revisions` 同一形态。

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

### 6.3 SC-5 的并发语义：父行锁协议（2026-11-21 补齐）

A14 把「归档前确认其下无 IN_PRODUCTION 镜头」实现成「同事务查询 + 拒绝」，并
如实登记了残余风险：查询与写入之间仍有窗口。本轮把窗口关掉，代价是**写入协议
多一条不变量**——不是在症状处加兜底，而是让两侧的写入在同一行上排队。

#### 为什么所有「不加锁」的方案都不成立

| 方案 | 为什么不成立 |
|---|---|
| 归档时再查一次 | 查与写仍不在同一把锁下，窗口只是变窄，不是消失 |
| 延迟约束触发器（`DEFERRABLE INITIALLY DEFERRED`） | READ COMMITTED 下两个事务各自在提交时判定，互相看不到对方**未提交**的改写（write skew），两边都通过。约束在事务开始时求值，而破坏发生在这之后 |
| 提升到 SERIALIZABLE | 确实能让一个事务失败，但代价是全库**所有**写路径都要承担序列化失败与重试；为一个跨聚合前置条件付全库成本 |
| 应用层互斥锁 / 单进程信号量 | 只保护单进程。多副本部署即失效，且失效方向是「多写一行非法数据」——静默 |

#### 协议（两侧成对，缺一即失效）

| 侧 | 操作 | 锁 | 语句 |
|---|---|---|---|
| 父 | 归档（`Scene → ARCHIVED`） | 排他 | `SELECT … FROM scenes WHERE entity_id = ? FOR UPDATE` → 锁内重判「其下无在产镜头」→ 写 |
| 子 | 镜头进入 `IN_PRODUCTION` | 共享 | `SELECT … FROM scenes WHERE entity_id = ? FOR KEY SHARE` → 锁内确认父场次未归档 |

**为什么子侧是 `FOR KEY SHARE` 而不是 `FOR SHARE`**：它只与 `FOR UPDATE` 冲突，
不挡父行的普通更新（`DRAFT→ACTIVE` 之类，PG 取 `FOR NO KEY UPDATE`）。那些更新
并不建立 SC-5 的前置条件，为它们付串行化代价没有收益。

**一个必须记住的坑**：SQLAlchemy 只给 `key_share=True` 时渲染成
`FOR NO KEY UPDATE`——那与自身不冲突，屏障一次都没挡住，而代码读起来完全正确。
必须 `read=True` 与 `key_share=True` 同时给。渲染结果由
`tests/unit/test_row_lock_protocol.py` 逐字钉住，方言升级不会悄悄改语义。

#### 触发条件由边表派生，不是服务层手写

进入 `IN_PRODUCTION` 的边有**三条**（`NOT_STARTED` / `BLOCKED` /
`PRODUCTION_COMPLETE`），进入归档的边有**两条**（`DRAFT` / `ACTIVE`）。
写成 `if target is IN_PRODUCTION` 只覆盖作者当时想到的那条，漏掉的那条不会让任何
闸门变红。所以两侧的前置条件声明在 **`Transition` 边上**：

```python
SHOT_IN_PRODUCTION_PARENT_PRECONDITION = ParentPrecondition(
    object_name="Scene", forbidden_status="ARCHIVED", message="父场次已归档，镜头不可进入 IN_PRODUCTION（SC-5 的并发侧）"
)
SCENE_ARCHIVE_CHILD_PRECONDITION = ChildPrecondition(
    object_name="Shot", forbidden_status="IN_PRODUCTION", message="场次下仍有 IN_PRODUCTION 的镜头，拒绝归档（SC-5）"
)
```

服务层只问「这条边有没有屏障，怎么加锁」；「要不要」由边表回答。
完备性判据（`tests/unit/test_row_lock_protocol.py`）从边表**派生**应检集合：
`[e for e in edges if e.target is IN_PRODUCTION]` 必须全部带声明——手写清单的
陈旧方向恰好是不安全的一侧（新增一条边就是漏检，而漏检表现为绿灯）。

#### 死锁

两侧都**先取父行锁**，再碰子行或子查询；加锁顺序一致，不存在环。归档侧内部的
`conditional_update` 复用的是已持有的排他锁，不会再取一次。

#### 拒绝语义（都在写之前发生，不留半合法状态）

| 情形 | 错误码 |
|---|---|
| 归档时存在在产镜头 | `STATE_TRANSITION_DENIED`（详情来自边声明） |
| 父场次已归档时镜头进产 | `STATE_TRANSITION_DENIED`（同上） |
| 父行读不到（已删 / 不在作用域） | `NOT_FOUND`——不当作「未归档」放行 |

#### 可观测的代价

镜头每次进入在产多一条行锁语句（单行、除归档进行中外不阻塞）；归档多一条排他
锁语句。量级不变，但「加锁顺序必须是父→子」从此是**写这条路径的前提**，
新增跨聚合前置条件时必须遵守同一顺序，否则会把死锁引进来。

### 6.4 唯一冲突（23505）的逐路径核对（2026-11-21）

A13 的债务第 7 条写着「应用层不会把 23505 翻译成统一的 409，需要确认路径的异常
映射覆盖该分支」。本轮把五个对象的**全部**写入路径逐条核对了一遍——不是抽查，
因为这类缺陷的形态是「某一条分支冒 500」，而 500 在监控上等于「服务端坏了」。

| # | 写入路径 | 语句 | 可触发的 23505 | 处理 | 证据 |
|---|---|---|---|---|---|
| 1 | `SCENE.CREATE` | `INSERT scenes` | 主键 / `uq_scene_code_per_project` | 回读同 id → 幂等返回；否则按项目+编号回读 → **409 编号占用**；两者都读不到 → 500 | `test_scene_service.py`（3 条） |
| 2 | `SCENE.STATUS.CHANGE` | `UPDATE scenes` | 无（不改唯一列） | `rowcount = 0` → 409 `CONCURRENT_MODIFICATION` | `test_scene_service.py` |
| 3 | `SHOT.DRAFT.CREATE` | `INSERT shot_breakdown_drafts` | 仅主键 | 回读 → 幂等；否则 500（uuid 主键冲突只能来自代码缺陷） | — |
| 4 | `SHOT.DRAFT.CONFIRM` | `INSERT shots ×N` | `uq_shot_code_per_project` | 回读占用者 → **409**，并区分「草案内重号」与「项目内已被占用」 | `test_shot_confirm_error_mapping.py` |
| 5 | `SHOT.DRAFT.CONFIRM` | `INSERT shot_revisions ×N` | `ux_shot_active_revision` / `uq_shot_revision_no` | **409 `CONCURRENT_MODIFICATION`**（可重试） | 同上 |
| 6 | `SHOT.STATUS.CHANGE` | `UPDATE shots` | 唯一列不变（`shot_code` 不随状态变） | `rowcount = 0` → 409 | `test_shot_service.py` |
| 7 | `REWORK.TASK.CREATE` | `INSERT rework_tasks` | 仅主键 | 回读 → 幂等；否则 500（同上） | — |
| 8 | `REWORK.TASK.STATUS.CHANGE` | `UPDATE rework_tasks` | 无 | `rowcount = 0` → 409 | `test_shot_planning_runtime.py` |
| 9 | 五表 `save()`（状态推进） | `UPDATE` | 库层 CHECK（证据自洽等） | **500 fail-closed** | `test_shot_planning_runtime.py`（6 条守卫） |

三条修正（都曾是 500 分支）：

1. **#1 的 `entity_id` 分支**：带了幂等键但回读不到同 id 行，说明撞的是编号唯一
   约束。原实现直接报 `INTERNAL_ERROR`——重试永远不会成功，而调用方看到 500 只会
   重试。现按「项目 + 编号」回读，报 409 并点出占用者。
2. **#4**：确认路径原先完全没有捕获 `DuplicateEntityError`，草案里两个镜头同号
   会把整个确认报成 500。现回读占用者，区分两种成因（修复方向相反：一个改草案，
   一个换编号）。
3. **#5**：同上，现收敛为可重试的 409 并发冲突。

**仍然保留 500 的两处（#3、#7）是刻意的**：它们的唯一来源是同主键插入，而主键是
服务端生成的 uuid——能撞上只能是代码缺陷。把代码缺陷伪装成 409，等于让真实故障
看起来像用户输错了。

**库层 CHECK 违反（#9）同样保留 500**：领域层对六条守卫边先行判定，能走到库层
拒绝说明领域守卫漏了——这是缺陷，不是业务冲突。

一条附带结论：RLS 对 `UPDATE` 的表现是 `rowcount = 0`（不可见的行被跳过），不是
`42501`。所以跨租户推进状态得到的是 409 并发冲突，而不是权限错误——fail-closed，
且不泄漏对象是否存在。`42501` 只在 `INSERT` 的 `WITH CHECK` 与显式拒绝时出现。

## 7. 已知债务（本批不解决，必须登记）

1. **`affected_asset_version_ids` 无外键**：跨域 ID 数组，库层无法约束。
   资产被删除后引用会悬空。缓解：消费方只做展示与追溯，不做存在性强校验。
2. **`SHOT.STATUS.CHANGE` 的取消守卫只校验非空**：`cancellation_ref` 是字符串，
   无法证明它指向一次真实授权。这是 D2 的直接代价——用「引用可被审计系统解析」
   替代「引用在库层可验证」。缓解：`authority_ref` 与 `evidence_refs` 同时写入
   `audit.status_transitions`，审计侧可按前缀路由到签发系统。
3. **`ReworkTask.REJECTED` 无入边**：契约已登记，本批不开。
4. ~~**Scene 归档不校验「其下无 IN_PRODUCTION 镜头」**~~ → **已在 A14 补齐**，
   **并发窗口已在 2026-11-21 关闭**（§6.3）。当前实现：父侧持排他行锁重判，
   子侧进入在产前取同一行的共享锁。仍属债务的部分只剩**代价**而不是缺口：
   每次进产多一条行锁语句，且「加锁顺序必须父→子」成为写这条路径的前提。
5. **`SH-2` 后半句无库层承载**：`Shot` 与同一 `shot_id` 的全部修订，其
   `project_id / episode_id / scene_id` 必须完全一致。`shot_revisions` 只有
   `(project_id, shot_id)` 一条外键，`episode_id / scene_id` 与 Shot 是否一致
   由服务写入时保证。故 SH-2 保持 `RUNTIME_PENDING`，不得随 SC-2 一起改判。
6. **`SH-4` 版本指针无库层约束**：`shots.shot_revision_id` 不是外键，
   「必须指向存在且 ACTIVE 的修订」只有服务层保证。补外键会与「首次确认先建修订、
   后落 Shot」的写入顺序冲突（外键要求被引用行先存在），需单独设计，
   例如可延迟约束或先落 Shot 的 DRAFT 占位——两者都要改写入协议，不在本批。
7. ~~**23505 未翻译成统一的 409**~~ → **2026-11-21 逐路径核对并修复**（§6.4）。
   八条写入路径中三条曾是 500 分支，现全部收敛到 409；保留 500 的三处
   （主键冲突 ×2、库层 CHECK）都是「代码缺陷」的失败形态，伪装成 409 会让真实
   故障看起来像用户输入错误。
   `ux_shot_active_revision` 的并发行为另由
   `tests/integration/test_shot_revision_concurrency.py` 显式构造并证明
   （A 未提交插入 → B 阻塞 → A 提交 → B 得 23505）。
   仍属债务的部分：`translate_write_error` 目前不透出约束名（`constraint` 参数
   恒为空），服务层只能靠回读数据来区分成因——这是可行但多一次往返的做法。
8. **归档之后仍可在其下新增镜头**（`SHOT.DRAFT.CONFIRM` 只校验场次存在且属本项目，
   不校验场次状态）。SC-5 只规定「归档前不得有在产镜头」，没有规定「归档后不得
   新增子对象」；因此这不是实现偏离，而是**契约缺口**。后果有限：新镜头以
   `NOT_STARTED` 出生，仍可走 `BLOCKED` / `CANCELLED`，只是无法进入在产（§6.3 的
   子侧屏障会拒）。是否把「归档即冻结子对象集合」写进契约，需要所有者裁定
   （要不要连带禁止在已归档场次下建草案/建镜头），本批不擅自扩张语义。

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
   CI 已覆盖：`alembic upgrade head` → `alembic check` → `alembic downgrade -1`
   → `alembic upgrade head`（`Migration downgrade roundtrip` 步骤）。
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
11. 归档前置条件的并发语义（§6.3）：真实 PostgreSQL 上两个方向各一条用例证明
    两侧的锁确实相遇——归档持排他锁时子侧进产被阻塞（`test_scene_archive_concurrency.py`
    方向一），子侧持共享锁时归档被阻塞（方向二）。只验一个方向会漏掉另一侧的
    锁模式写错。
12. 锁子句逐字固定：`scene_lock_statement` 的渲染结果必须是 `FOR UPDATE` /
    `FOR KEY SHARE`，且不得出现 `FOR NO KEY UPDATE`（`test_row_lock_protocol.py`）。
13. 屏障的声明完备性从边表派生：所有目标为 `IN_PRODUCTION` 的边、所有目标为
    `ARCHIVED` 的边必须分别带父/子前置条件；集合由边表计算，不手写。
14. 唯一冲突不得冒成 500：§6.4 表中 1/4/5 三条路径各有负向用例，错误码为 409
    且详情能指出占用者或可重试（`test_scene_service.py`、
    `test_shot_confirm_error_mapping.py`）。
