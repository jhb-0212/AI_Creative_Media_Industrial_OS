# ADR-R28：Scene / Shot / ShotRevision / ShotBreakdownDraft / ReworkTask 运行时入口

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
| 子 | 镜头进入 `IN_PRODUCTION` | 共享 | `SELECT … FROM scenes WHERE entity_id = ? FOR SHARE` → 锁内确认父场次未归档 |

**为什么子侧是 `FOR SHARE` 而不是 `FOR KEY SHARE`**（2026-11-21 更正）：初版用的是
`FOR KEY SHARE`，理由是「它只与 `FOR UPDATE` 冲突，不挡父行的普通更新，代价更小」。
这个理由在本协议内部成立——归档侧确实持 `FOR UPDATE`——但它把屏障的正确性绑在了
**另一侧的实现细节**上：只要有任何一条改父场次状态的路径没取 `FOR UPDATE`（普通
`UPDATE ... SET status` 取的是 `FOR NO KEY UPDATE`），`FOR KEY SHARE` 就放它过去，
屏障静默失效。同一项目里 Project / Episode 的父行锁都用 `FOR SHARE`，两种强度并存
本身就是缺陷信号。

`FOR SHARE` 的代价是同场次的普通状态变更会与镜头进产串行。这个代价可以接受：
它们本来就是同一场次内的写，且「屏障不依赖对方是否记得取排他锁」值得付。

**一个仍然必须记住的坑**：SQLAlchemy 只给 `key_share=True` 时渲染成
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

### 6.5 归档即冻结子集合（2026-11-21，V205-R14-A19）

§6.3 关掉的是「归档**前**不得有在产镜头」，它的另一侧一直开着：**归档之后仍可在
其下新增子对象**。这不是一处遗漏，而是三层同构的缺口——四条创建路径没有一条看
父对象状态：

| 创建路径 | 父对象 | 修正前校验 | 后果 |
|---|---|---|---|
| `EPISODE.CREATE` | Project | 只查存在 | 归档项目下可新增集数 |
| `SCENE.CREATE`（治理线） | Episode | 存在 + 归属 + 冻结 | 已正确 |
| `SCENE.CREATE`（A13 正式链） | Episode | 只查归属 | **归档项目/集数下可新增场次** |
| `SCRIPT.IMPORT` | Episode | 归属 | 归档集数下可新增剧本修订 |
| `SHOT.DRAFT.CONFIRM` | Scene | 存在 + 归属 | **归档场次下可新增镜头** |

最后一行与倒数第二行的失效尤其值得记录：`create_scene` 的注释原文写着「父链冻结
与组织隔离由数据库守卫/RLS 承担」，而核对结果是库中**既无触发器、也无带状态谓词
的 RLS 策略**——全仓 `CREATE TRIGGER` 零命中，RLS 的 `EXISTS` 子句只做租户隔离。
文档说有、实际没有，比根本没人写更危险：它会让复核者跳过这一段。

### 为什么落点是状态而不是动作

「归档意味着我这一支不再长了」说的是 Project / Episode / Scene 处于某状态时的
**性质**，不是某个动作的属性。写成四个调用点 `if` 的问题是结构性的：漏掉的那条
不会让任何闸门变红。第四张子表接入时同理。

因此落点是 `Lifecycle.frozen_states`（A16 引入）加「判定从状态派生」：

```
Lifecycle.is_frozen(status) -> bool
Lifecycle.assert_child_creation_allowed(status, child_object=...) -> None  # 命中抛 409
```

### 四条路径的共同前置

每条都在**写入之前**持父行的共享锁（`FOR SHARE`）再判定：

- 为什么必须持锁：与 §6.3 的父行锁是同一把。归档侧持 `FOR UPDATE`，创建侧持
  `FOR SHARE`——两者相遇即把「判定归档」与「发生归档」串行化。只判定不持锁，
  就是「查的瞬间成立、写的瞬间不成立」，与 §6.3 被否掉的做法完全相同。
- 为什么是 `FOR SHARE` 而不是 `FOR KEY SHARE`：后者只与「改键值/删除」冲突，
  `UPDATE ... SET status` 照样能穿过——屏障会静默失效。项目内锁模式必须统一，
  否则同一份语义出现两种强度，其中一个是错的。
- 加锁顺序固定为 **Project → Episode → Scene**。`SHOT.DRAFT.CONFIRM` 会引用多个
  场次，必须按 `scene_id` 全序去重后逐个取锁：环只可能由顺序不一致产生，而草案
  里的出现顺序由调用方决定。

### 守卫依赖改为构造期必需

原先 `SceneService` 的 `shot_repository` / `project_repository` 是可选的，靠调用点
`if ... is None: raise INTERNAL_ERROR` 兜底。本轮改到一半时抓到了这种写法的真实
失效：**生产装配 `_scene_service` 确实没传 `project_repository`**，改完后
`create_scene` 全路径 500，而所有闸门依旧全绿（因为没有判据检查装配完整性）。

可选的守卫依赖因此被取消：

> 构造成功 ⇒ 两个不变量都有执行者

装配漏传从「运行期 500」变成「类型错误」。这不是风格选择——守卫依赖的可用性不
该由运行期检查表达，那等于承认它可能不可用。

### 闸门判据

新增 `check_child_creation_freeze_coverage()`，应检集合从 ORM metadata 的
外键关系**派生**：

- 有子表引用的父表，必须映射到声明了 `frozen_states` 的 Lifecycle，或在豁免表里
  写明理由；
- 三向完备性：未登记 → 红；登记了不存在的表 → 红；豁免登记了却已无子表引用 → 红
  （陈旧豁免会放过未来新增的子表）；
- 应检集合按 schema 过滤。此前一度按「当前 import 到的全部表」取集合，结果同一份
  判据在不同入口下给出不同答案——那是最糟的判据形态，比没有判据更误导。

三个豁免各有理由：`organizations`（不是业务容器）、`rework_tasks`（其父 Shot 在
`PRODUCTION_COMPLETE` 之后仍要能建返工任务，`frozen_states` 为空是刻意的）、
`shots`（同前）。

### 6.6 冻结策略进入契约（2026-11-21，V205-R14-A20）

§6.5 之后还剩一个问题没有回答：**冻结哪个状态**本身只写在代码里。改一行元组，
归档后的项目就能重新接受新集数，而四道闸门全绿——覆盖判据只回答「有没有声明」，
不回答「声明的是哪几个状态」。这是本项目里唯一不受契约治理的生命周期事实：转移边
在契约里，改边必须同改契约，`freeze_manifest --check` 还会因哈希漂移报红；冻结态
决定「父对象封存后还能不能长出新子对象」，强度不比转移低，没有理由成为例外。

#### 裁定：`Project.COMPLETED` 继续冻结

依据不是偏好，而是转移图上的事实：Project 从 `COMPLETED` 只有一条出边
（`→ARCHIVED`），**没有任何回到 `ACTIVE` / `DRAFT` 的路**。两个后果：

1. 在 `COMPLETED` 下登记的子对象会合法落库，却永远无法伴随父对象回到可推进的状态
   ——它出生在一条死路上。
2. `COMPLETED` 同时是「交付范围已固化」的事实。事后追加会改写已成立的交付记录，
   而且不留任何状态变更痕迹。

反过来，「交付后补录」这个需求也不该靠把 `COMPLETED` 移出元组来实现：那等于让
补录绕过状态变更事实与审计，**用取消检查来满足一个新流程**。正确落点是另行登记
重开边（`COMPLETED→ACTIVE`）与专属动作，让补录以一次可审计的状态变更开始。该边
当前未登记，所以本项目**尚不支持**该流程——冻结不减少任何已支持的能力。

#### 搬迁：契约成为策略的权威

`state_machines.yaml` 的每个已实现对象新增 `frozen_states`（不冻结写 `[]`）。
「没声明」与「声明为空」在运行期同形，所以一律显式写出，由判据双向比对。

收益不是「多一处校验」，而是**判断变化从此有痕迹**：

| 变化 | 谁会发现 |
|---|---|
| 改代码不改契约 | `check_frozen_state_contract()` 红 |
| 改契约不改代码 | 同一判据红 |
| 两者都改 | 契约文件哈希变化 → `freeze_manifest --check` 红 |
| 重新签清单 | 需要一条已批准的修订登记（`contract_amendments.yaml`） |

三步都要求人看见「冻结语义变了」，而不是让一行 diff 混在别的改动里。

#### 判据形态

`check_frozen_state_contract()` 的应检集合**派生**自 `_STATE_MACHINE_SOURCES`
引用的模块：反射其中的 `Lifecycle` 实例，按 `object_name` 索引。不新建「对象名 →
变量名」的映射表——那又是一份手写清单，漏登记的方向恰好是「少比对一个对象」，
表现为绿灯。五条判据：

1. 已实现状态机找不到 `Lifecycle` → 红（策略无处声明）；
2. 有 `Lifecycle` 却没登记进 `_STATE_MACHINE_SOURCES` → 红（转移与冻结都不会被比对）；
3. 契约条目缺 `frozen_states` 字段 → 红（不能靠默认值）；
4. 值不在 `allowed_states` 内或重复 → 红（`in` 恒假会让冻结静默失效）；
5. 两侧集合不等 → 红，且**分别报出**「契约多 / 实现多」——只报「不一致」等于让人猜。

### 6.7 ShotSpec 的契约补全（2026-11-21，V205-R14-A21）

主链下一环是 `ShotSpec`（`8.12`）。核对契约时发现三处缺口叠在同一个对象上：

| 缺口 | 事实 | 后果 |
|---|---|---|
| 状态 | `object_schemas` 有 `status`，但是裸 `string`；`state_machines.yaml` 无该对象 | 「锁定」没有合法状态可落 |
| 创建入口 | Action Registry 只有 `SHOTSPEC.LOCK` | 锁定没有对象可锁 |
| 分类 | `status_field_taxonomy` 记为 `lifecycle_gap` | 任一状态值都能通过校验 |

#### 状态集：`DRAFT` → `LOCKED`

`8.12` 只给字段与「必须版本化并绑定 `ShotRevision`」，没给状态集。取两态，三个边界都是刻意的：

- **不设 `PARTIALLY_LOCKED`**。`ShotVisualPlan` 的该状态依赖一套「必锁维度全集」，而 `8.12`
  只给 `locked_dimensions` 字段、没给全集。没有全集，「部分」锁定到哪为止不可判定——把
  不可验证的判断写进状态机，比不写更糟。部分锁定的事实仍由 `locked_dimensions` 记录。
- **不设 `SUPERSEDED`**。规格随修订版本化（`SS-2` 绑定精确 `shot_revision_id`），换版发生在
  `ShotRevision` 层：新修订要有新规格，旧规格对其修订仍然有效，没有「被取代」的语义。
- **`LOCKED` 为终态**。改规格必须新修订 + 新规格，不得原地解锁——这是红线「终态不可原地复活」
  在本对象上的落法。

命名共享 `DRAFT` / `LOCKED`，不引入第二套锁定词汇，满足 `status_field_taxonomy` 对该对象的
解除条件（「不得自创第二套锁定命名」）。

#### 创建入口：`SHOTSPEC.CREATE` 与 `SHOTSPEC.LOCK` 分离

`SHOTSPEC.LOCK` 是 HIGH 风险动作，语义是「为已有规格的锁定维度、依据与审批事件盖章」。
让它兼做创建，等于把一次未经创建侧校验的写入藏进审批动作里。创建与确认分离是本项目既有
口径（`SHOT.DRAFT.CREATE` / `SHOT.DRAFT.CONFIRM` 就是两个动作，共用端点但各自登记）。

#### 本批只补契约

`SHOTSPEC.CREATE` / `SHOTSPEC.LOCK` 均登记为 `not_implemented`；表、路由、事件与迁移在下一批。
`SS-3`（切点单调 / fps·时长换算）仍为 `RUNTIME_PENDING`——定级必须与实现同批，否则就是
「声称强制、实际没有」，这正是 A19 抓到的 `create_scene` 注释形态。

### 6.8 ShotSpec 运行时落地（2026-11-21，V205-R14-A22）

§6.7 补完契约后，`SHOTSPEC.CREATE` / `SHOTSPEC.LOCK` 仍登记为 `not_implemented`，`SS-3` 仍
`RUNTIME_PENDING`。本批把「契约存在、运行时为零」补成四层一致，并把定级同时改为
`RUNTIME_ENFORCED`——定级与实现同批，是 §6.7 结尾那条纪律的自我适用。

#### 为什么「同一修订至多一份规格」不能靠服务层查重

服务层「先查没有、再插入」在并发下必被穿透：两个请求都能查到没有。这不是加锁能修的
（锁的是修订行，而唯一性说的是「这张表里不得出现第二行」），唯一约束是唯一能真正拒绝的
东西。因此 `uq_shot_spec_per_revision` 是结构性的，服务层只在 23505 之后负责**解释**：

| 冲突形态 | 语义 | 返回 |
|---|---|---|
| 同一 `entity_id` 重放，内容一致 | 幂等（重试） | 返回原对象，无新事件 |
| 同一 `entity_id`，内容不同 | 幂等键复用 | 409，点明「不能复用」 |
| 该修订已有另一份规格 | 业务冲突 | 409，**点出占用者 `shot_spec_id`** |
| uuid 主键冲突 / CHECK 被穿透 | 代码缺陷形态 | 500，不伪装成 409 |

沿用 §6.4 的逐路径口径：把「代码缺陷」伪装成 409 会让故障看起来像用户输错。

#### 归属：复合外键，不是单列

`fk_shot_specs_revision (project_id, shot_revision_id) → shot_revisions(project_id, entity_id)`。
单列外键只能证明修订存在，挡不住「规格属项目 A、修订属项目 B」——负测 `ACT-SPEC-NEG-002`
打的正是这条。代价是引用目标必须先有 `uq_shot_revisions_entity_project`（`b1c2d3e4f5a6` 已补）。
服务层仍先给可读的 400（谁属于谁），库层外键是最后防线：两层同口径，不是两套规则。

#### 冻结：`ShotRevision.SUPERSEDED` 必须表态

规格是为「可生产的修订」而存在的。已取代的修订再补新规格，会产生一批与「有效规格集」
难以区分的孤儿对象，而它们永远无法交付。故 `ShotRevision` 的 `frozen_states` 增
`SUPERSEDED`——这不是为过闸门而加的一行：冻结覆盖判据（§6.5/§6.6）要求「有子表引用的
父表必须表态」，`shot_specs` 一建表，`shot_revisions` 就欠一个答复。

拒绝码用 `PARENT_LIFECYCLE_FROZEN` 而非 `STATE_TRANSITION_DENIED`：前者说「父已封存、
不该再出现新对象」，重试没有意义；后者说「这条边走不通」。合成一个码，调用方就分不清
该不该重试。

#### SS-3 三层同口径，一份算式

- 领域层 `ShotSpec.__post_init__`：构造即拒（无 IO，纯对象一致性）；
- 服务层：同一函数再调一次，只为给出更早、更可读的错误；
- 库层 `ck_shot_specs_cut_points`：拦住绕过服务的写入（运维脚本、批处理）。

库层不能在 CHECK 里写子查询，故递增性用 `IMMUTABLE` 函数 `cut_points_monotonic(integer[])`；
函数内每个比较都 `COALESCE(..., false)`——**空数组会让 `array_length = NULL`、整个 CHECK
求值为 NULL 而放行**，这个陷阱由 `test_shot_spec_boundaries.py` 的空数组用例钉死。

帧数换算只有一份实现（`total_frames`），库层 CHECK 用整数除法逐字等价（正数截断）。
`30000/1001` 与 `29.97` 在 1 秒处正好差一帧，先除后乘或浮点近似都会造成「库里合法、
领域拒绝」的分歧，这类分歧只在 NTSC 素材上复现。

域内新增一条类型判据：`duration_ms` / `fps_num` / `fps_den` 必须是 `int`（`type(x) is int`）。
只判 `>= 1` 时 `29.97` 与 `True` 都能通过——前者让整数换算静默退化成浮点，后者在库里
变成 1。契约的 `minimum: 1` 不表达类型。

#### 顺序：幂等排在父链锁读与冻结判定之前

重放一个当时成功过的请求是重试，不是新建。若先判冻结，修订在两次请求之间被取代，重试
就会变成 409——而调用方看到 409 只能理解成「创建失败」，与实际（第一次已生效）相反，
且没有任何途径确认。这条顺序由单测断言「项目查询与修订锁**零调用**」来固定，不是靠注释。

#### 读取入口

新增 `SHOTSPEC.READ`（LOW）：规格写完之后没有任何读取入口，「登记成功」在 API 层面不可见。
`total_actions` 97 → 98，与修订台账（基线 75 + 登记 23）逐格对得上。

#### 契约变更点

- Action：新增 `SHOTSPEC.READ`（`total_actions` 97→98），三条动作移入 `implemented`；
- OpenAPI：新增 `/actions/shotspec.read`，与两份 YAML、真实路由三向一致；
- 事件：`SHOTSPEC_CREATED` / `SHOTSPEC_LOCKED` 入 `event_catalog` 并由服务真发出；
- 状态机：`ShotSpec` 进 `_STATE_MACHINE_SOURCES`（DRAFT→LOCKED 一条边，归 LOCK）；
- Schema：`SS-3` 由 `RUNTIME_PENDING` 升为 `RUNTIME_ENFORCED`；
- 冻结策略：`ShotRevision.frozen_states = [SUPERSEDED]`，`shot_revisions` 登记为冻结容器；
- 台账：`V205-R14-A22` 登记上述增量，`freeze_manifest --write` 重签。

### 6.9 ConstraintPack 运行时落地（2026-11-21，V205-R14-A23）

7.9 给了 ConstraintPack 完整的包级 12 字段与条目 8 必填字段、五个状态与四条边，31.6 也
登记了 `CONSTRAINT.PACK.CREATE` / `LOCK`，但库里没有任何对应物。本批按主链顺序（ShotSpec
之后、单一路线生成之前）把它补成四层落地：契约登记 → 库层约束 → 领域层 → 服务/路由/事件。

#### 四条边分属两个动作，不是「一个动作管一个对象」

`DRAFT → ACTIVE` 归 `CONSTRAINT.PACK.LOCK`；三条治理边（`ACTIVE → STALE / SUPERSEDED /
REVOKED`）归**新增**的 `CONSTRAINT.PACK.STATUS.CHANGE`。理由不是命名洁癖：LOCK 回答的是
「锁定义务已尽」（审批人、审批时间、依据、事件四要素一次落齐），治理边回答的是「此后还能
不能被用」。把治理边塞进 LOCK，等于让「重新审批一次锁定」顺带撤销一份已生效的授权——正
是 N-07 修掉的形态：一个入口承载两种准入条件，实际效果是两者都只剩较松的那条。

`STATUS.CHANGE` 本批只登记、不实现：三条治理边没有下游消费者（生成域未落地），现在实现
只能得到「能改状态、没人读」的边，而它会立刻把 §7 的债务伪装成已解决。逐边债务登记在
`state_machine_gaps.yaml`。

#### 身份分层：`entity_id` 是版本行，`constraint_pack_id` 是逻辑包（本批的自我纠错）

初版照抄了 Scene/Shot/ShotRevision/ShotSpec 的 `ck_*_identity_single`（业务 id = entity_id），
并在领域层强制两者相等。那是**拷贝形态而不是读懂契约**：那几个对象没有跨行版本，7.9 的
`version` 却写着「变更须新建版本并 supersede 旧版本」——新版本就是「新行 + 同一个包标识 +
version 递增」。

两列被钉死为同一值的后果不是「多一道防线」，而是同时废掉两条刚要登记为 `RUNTIME_ENFORCED`
的不变量：

- `uq_constraint_pack_version (constraint_pack_id, version)`：同一行不可能与自己冲突两次；
- `uq_constraint_pack_single_active WHERE status='ACTIVE'`：一行只能有一个 status，CP-6 恒真。

恒真的约束比没有约束更危险——闸门会报绿，而「两个 ACTIVE 并存」这件事从未被挡过。故本批
删掉该 CHECK（迁移与 ORM 同步），领域层改为「缺省取 entity_id，允许显式不同」，并在
`planning_seed` 的 `constraint_pack_id` 参数上留出构造第二版本的入口，用集成用例证明两条
约束真的会拒绝。这条纠错也解释了 §9 第 13 条为什么必须有「不同包各自 ACTIVE 放行」的
正测：只有负测时，把部分唯一索引误写成全表唯一同样是绿的。

#### 作用镜头的归属：触发器，不是关联表

`shot_ids` 是数组，外键表达不了「每个元素都是本项目的真实 Shot」。关联表能让复合外键生效，
但会把契约字段降级成派生投影——库里同时存在「数组」与「关联行」两份事实，任何写入路径都
得维护一致性，而两份事实迟早分叉。

触发器 `trg_constraint_packs_shots` 保持「数组是唯一存储形态」，同时以 `ERRCODE='23503'`
抛出，与真外键共用同一条错误映射（`classify_write_error` 按 SQLSTATE 前缀 `23` 判定），
不新增翻译分支——每新增一条分支就多一处「记得同步」。

代价是两层判定顺序必须在文档里说清：触发器**先于** CHECK 执行，因此 `shot_ids` 的空白元素
永远以 23503（引用不成立）被拒，永远不会走到 `text_array_unique` 的空白分支。断言一个取决于
执行顺序的 SQLSTATE 是脆弱的，故库层空白负测改用 `shot_group_ids`（无触发器，直达 CHECK）。

#### 部分唯一索引：把「版本推进没有入口」变成可读的 409

新版本锁不上时，服务层收到的是一条唯一冲突。若不解释，路由只能给 500；若解释成「重复提交」，
调用方会一直重试一条注定失败的请求。正确的话术是第三种：**同一逻辑包已有生效版本，取代入口
尚未实现**。这要求仓储能回答「生效中的是哪一版」（`get_active_by_pack`）。

同一原则适用于 `uq_constraint_pack_version` 冲突：只说「版本已存在」，调用方无法判断该递增
`version` 还是该换 `constraint_pack_id`（两种修法方向相反）。故仓储补 `get_by_pack_version`，
409 里点出占用者的 `entity_id`。`ConstraintPackRepository.save` 也必须翻译驱动异常——部分唯一
索引在 `save`（DRAFT→ACTIVE 的条件更新）上才会被触发，不翻译就只有 500。

#### 闸门升级：转移入口判据从对象级降为边级（方向相反，是升精度）

原判据问的是「这个对象有没有实现任意一个状态变更动作」。ConstraintPack 恰好是「一条边有
入口、三条没有」的形态，对象级判据会直接放行——三条治理边在库里永远走不通，而闸门是绿的。

新判据应检集合直接来自 `Transition.owner_action`：每条边都必须有责任动作，且该动作已实现；
未实现的边必须在 `state_machine_gaps.yaml` 里**逐边**登记，实现后登记项必须删除（反向完备）。
不再有「按命名猜哪个动作管状态变更」的步骤（旧的 `_status_change_action_keys` 已删），
判据也不依赖命名约定。

#### 顺带根治：`advance_status` 失败后聚合必须回到推进前

`advance_status` 是「先改内存、再条件落库」。落库被乐观锁或唯一约束拒绝时，内存里的新状态
已经生效而库里没有；若那个对象被复用（重试、批处理、幂等回读后再推进），第二次推进会把它
当成「当前已经是 ACTIVE」，写进 `TransitionFact.from_status` 的是一条从未在库里存在过的状态。

审计表里最不该有的东西，且没有任何闸门会发现。修复放在唯一入口：失败路径（含
`on_conflict="skip"`）一律用推进前的 `__dict__` 浅拷贝恢复。约束写在注释里——快照是浅拷贝，
`transition_to` 目前只写标量字段；将来若出现就地改集合的转移，那一处必须自己负责。

#### 契约变更点

- Action：新增 `CONSTRAINT.PACK.READ`（LOW）与 `CONSTRAINT.PACK.STATUS.CHANGE`（HIGH），
  `total_actions` 98→100；`CREATE` / `READ` / `LOCK` 移入 `implemented`；
- OpenAPI：新增 `/actions/constraint.pack.read` 与 `/actions/constraint.pack.status.change`；
- 事件：`CONSTRAINT_PACK_CREATED` / `CONSTRAINT_PACK_LOCKED` 入 `event_catalog` 并由服务真发出；
- 状态机：`ConstraintPack` 进 `_STATE_MACHINE_SOURCES`（4 条边），状态枚举与 CHECK 一致；
- Schema：`CP-1…CP-6` 由 `ABSENT` 升为 `RUNTIME_ENFORCED`，`CP-7` 保持 `RUNTIME_PENDING`；
- 台账：`V205-R14-A23` 登记上述增量与身份分层裁定，`freeze_manifest --write` 重签。

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
8. ~~**归档之后仍可在其下新增镜头**~~ → **2026-11-21 已按所有者裁定扩张契约并补齐**
   （A19，§6.5）。补齐范围不止确认路径：`EPISODE.CREATE` / `SCENE.CREATE` 两条
   寻址线 / `SCRIPT.IMPORT` / `SHOT.DRAFT.CONFIRM` 四条创建路径全部接线，且判定
   从 `Lifecycle.frozen_states` 派生、四条路径都在持父行锁后判定。
   仍属债务的部分：
   - **加锁顺序**（Project → Episode → Scene）无自动判据，靠评审约束。
   - **豁免表的理由正确性**需人工判断：闸门能发现「豁免了但不该豁免」只有一种
     形态——该表已不存在或已无子表引用；理由本身是否成立不在机器判断范围内。
   - `Project.COMPLETED` 的语义与冻结策略的契约化见 §6.6（A20，已裁定）。
9. **冻结屏障只判直接父对象**（A22 引入 `shot_specs` 后显性化）：规格的父是
   `ShotRevision`，故只判 `SUPERSEDED`。上游 `Project / Scene / Shot` 已封存、
   而修订仍 `ACTIVE` 时，仍可补登规格。这不是本批新增的漏洞——A13 五张子表
   同形态（只判直接父），`Shot` 自身的 `frozen_states` 为空。收紧它需要同时
   定义「深层父链冻结」的判定链与加锁顺序（父→子，否则死锁），属独立修订；
   在此之前不得只给某一层加判据——单层加判据会造成「有的路径冻结、有的不冻结」
   的错觉，比统一宽松更危险。

10. **`ConstraintPack` 的三条治理边无运行时入口**（A23）：`ACTIVE → STALE / SUPERSEDED /
    REVOKED` 的责任动作 `CONSTRAINT.PACK.STATUS.CHANGE` 已登记但未实现，逐边登记在
    `state_machine_gaps.yaml`。期间「撤销一份已生效的约束包」只能人工改库，而改库不会留下
    `audit.status_transitions` 事实——即一次授权被撤销，审计查不到。这是本批最大的缺口，
    也是「版本推进」缺席的直接后果：没有 SUPERSEDE 入口，新版本锁不上（部分唯一索引会拒）。
11. **`ConstraintPack.source_previz_revision` 的「EXPLORATORY 型不得填入」未校验**：判定需要
    PrevizVersion 对象及其 exploration 类型，该对象尚未建模。当前只校验字段长度，无法区分
    BASELINE 与 EXPLORATORY 来源。
12. **`shot_group_ids` 是自由引用**：`ShotGroup` 未建模，既无法校验取值是否为真实镜头组，
    也无法校验其成员属于本项目。与 `shot_ids` 的差异是刻意的——有对象才做归属校验，为不存在
    的对象编一套校验只会造出假约束。
13. **`shot_ids` 归属用触发器而非复合外键**：存在「校验通过后镜头被删」的窗口。Shot 创建即
    绑定项目且项目归属不可迁移，该窗口不构成新的不变量缺口，但严格意义上弱于复合外键。
14. **`CP-7`（只有 ACTIVE 可作生成输入）保持 `RUNTIME_PENDING`**：生成域未落地，无消费者可
    校验；定级必须与实现同批，否则就是 §6.1 之后反复出现的虚假声明。
15. **唯一冲突的成因仍靠回读区分**（§6.4 债务 7 的延续）：`translate_write_error` 不透出约束名，
    `save` 抛出的 `DuplicateEntityError.constraint` 恒为空，`lock_pack` 只能再用
    `get_active_by_pack` 查一次才知道是「已有生效版本」。多一次往返是可接受的代价，但它是
    「约束名没有从驱动透出来」的症状，不是设计选择。

## 8. 被拒绝的替代方案

| 方案 | 拒绝理由 |
|---|---|
| 只做 Scene + Shot，ShotRevision 先留空 | `Shot.shot_revision_id` 是 required，响应模型直接 500；等于交付一个必然报错的端点 |
| ReworkTask 只登记债务不建表 | 与 D1/D4 矛盾；`rework_basis_ref` 将永远无法校验，返工边成为绕过验收的暗门 |
| 授权记录新建成 `ApprovalRecord` | 等于再造一个域（契约 + 表 + 动作 + 全链路），且与 A12 的 `StatusTransition` 职责重叠 |
| 让 `SHOT.REVISION.CONFIRM` 顺带创建 Scene | 替所有者决定语义，且不可逆；R19 已明确拒绝 |
| 把 `COMPLETED` 移出 `frozen_states` 以支持「交付后补录」 | 补录会绕过状态变更事实与审计，等于用取消检查满足新流程；正确落点是登记 `COMPLETED→ACTIVE` 重开边与专属动作（§6.6） |
| 给冻结态加一张「理由表」并由判据校验非空 | 判据只能验「写了理由」，验不了理由是否成立；真正的闭环是让策略进契约、由哈希与修订登记强制人看见变化（§6.6） |
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
    `FOR SHARE`，且不得出现 `FOR NO KEY UPDATE`（`test_row_lock_protocol.py`）。
    项目内父行锁只有这两种模式，不允许第三种强度混入。
13. 屏障的声明完备性从边表派生：所有目标为 `IN_PRODUCTION` 的边、所有目标为
    `ARCHIVED` 的边必须分别带父/子前置条件；集合由边表计算，不手写。
14. 唯一冲突不得冒成 500：§6.4 表中 1/4/5 三条路径各有负向用例，错误码为 409
    且详情能指出占用者或可重试（`test_scene_service.py`、
    `test_shot_confirm_error_mapping.py`）。
15. 父链冻结（§6.5）：四条创建路径在冻结父对象下必须被拒，错误码
    `PARENT_LIFECYCLE_FROZEN`，且**拒绝发生在 INSERT 之前**（断言仓储 `added`
    为空、无事件）。`SCENE.CREATE` 两条寻址线各有用例，含「上层归档而下层仍
    ACTIVE」的对称路径（只判直接父会漏）。
16. 冻结判据完备性：`check_child_creation_freeze_coverage()` 必须能从 ORM metadata
    派生「有子表引用的父表」全集，并对三向缺陷各有一条负向用例——容器未声明
    `frozen_states`、豁免表登记了不存在的表、父表两处都未登记
    （`test_contract_check.py`）。
17. 守卫依赖不可缺省：`SceneService` 的镜头/父项目仓储是构造期必需参数，装配
    漏传是类型错误而非运行期 500（`mypy` 覆盖；`shot_planning_routes._scene_service`
    是唯一装配点）。
18. 冻结策略是契约事实（§6.6）：`state_machines.yaml` 的 9 个已实现对象显式声明
    `frozen_states`，且与 `Lifecycle.frozen_states` 相等；五类缺陷各有负向用例
    （缺字段、含未声明状态、重复项、两侧不等、Lifecycle 未登记）
    （`test_contract_check.py`）。
19. ShotSpec 的 SS-3 三层同口径（§6.8）：领域构造、服务入口、库层 CHECK 各有一条负向
    用例；库层用例必须包含**空数组**（`COALESCE` 缺失时 CHECK 求值为 NULL 而放行），
    并在真实 PostgreSQL 上验证末点等于帧数被拒（`test_shot_spec_domain.py`、
    `test_shot_spec_boundaries.py`）。
20. 绑定同项目由复合外键保证（§6.8）：以 admin 连接直接 SQL 写入「规格属 A、修订属 B」
    必须 23503；服务层同场景给 400 且说明双方项目（`test_shot_spec_boundaries.py`）。
21. 一修订一规格由唯一约束保证（§6.8）：第二份规格必须 23505；服务层把 23505 解释为
    四类不同语义（幂等返回 / 幂等键复用 409 / 占用者 409 / 代码缺陷 500），
    各有单测（`test_shot_spec_service.py`）。
22. 创建顺序不可换位（§6.8）：幂等回读必须排在项目查询与父行锁之前——单测断言两条
    父链路径**零调用**；否则修订被取代后的重试会变成 4xx，调用方无法确认创建是否生效。
23. `SHOTSPEC.READ` 存在且与契约一致：Action Registry / OpenAPI / 真实路由三向比对，
    `action_implementations.yaml` 的 route 由 `app.openapi()` 校验。
13. 「同一逻辑包至多一个 ACTIVE」双向证明：`test_constraint_pack_boundaries.py` 里
    `test_two_active_versions_of_one_pack_are_rejected`（23505）与
    `test_different_packs_may_each_have_an_active_version`（两个不同 pack 各自 ACTIVE 必须
    成功）成对存在。只有负测时，把部分唯一索引误写成全表唯一同样是绿的。
14. 转移入口判据逐边比对：11 个状态机共 47 条边，每条边的 `owner_action` 必须已实现，或
    在 `state_machine_gaps.yaml` 逐边登记；登记项与实现、实现与登记两个方向都不许缺
    （`tests/unit/test_contract_check.py` 的 `transition_entry_gaps` 用例覆盖四类错法）。
15. 失败推进不污染聚合：`advance_status` 的任一失败路径（守卫拒绝、乐观锁冲突、
    `on_conflict="skip"`）之后，实体的 `status` / `entity_version` 必须回到推进前；
    `test_constraint_pack_service.py::test_single_active_conflict_explains_missing_supersede_entry`
    以「拒绝后仍为 DRAFT」钉死这条。
16. 幂等早于归属：重放已成功过的创建请求时，镜头归属查询必须**零调用**（不是「结果相同」）——
    否则镜头在下游被改名后，重试会变成 4xx，调用方永远无法确认第一次是否生效。
17. 库层七类负测各有 SQLSTATE 断言：跨租户归属 23503、跨项目/悬空镜头 23503、同包双 ACTIVE
    23505、同包同版本 23505、重复镜头/未登记维度/条目缺字段/NOT_APPLICABLE 带内容/版本非正/
    适用性非法 23514。断言具体 SQLSTATE 而不是「报错了」，否则「被错的约束拒绝」也算通过。
