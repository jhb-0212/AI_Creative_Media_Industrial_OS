# ADR-R27（草案）：Scene / Shot 运行时入口——创建动作与状态推进动作

> **状态：PROPOSED，未批准，不构成决策。**
> 本文件是「待裁定事项的论证」，不是已成立的 ADR。批准后另出正式 ADR，
> 并按流程登记 `V205-R14-A13`。**在批准之前，不得改动任何派生契约文件**
> （`action_registry.yaml` / `openapi.draft.yaml` / `contract_amendments.yaml`）——
> 顺序反了就是「先斩后奏」，正是 `contract_amendments.yaml` 开篇点名批评的那种局面。
>
> 日期：2026-11-20　前置：批次五已落地 `scenes` / `shots` 表（提交 `5ae417b`）

---

## 0. 一句话结论

`scenes` / `shots` 两张表**现在只能读，不能写**：`shots.scene_id NOT NULL` 要求先有
Scene，而 Scene 在 84 个动作里**没有任何创建入口**；退一步说，即便绕开这一层，
Shot 的 10 条状态转移边也**没有任何动作能触发**。

这不是「代码没写」，而是**契约层没有入口**。而契约新增入口必须走
`contract_amendments.yaml` 登记，其中 `approved_by` / `approved_at` 只能由所有者给出。
所以本轮任务的性质是**裁定**，不是**补齐**。

---

## 1. 已核实的事实（逐条带出处，杜绝印象）

| # | 事实 | 出处 |
|---|---|---|
| F1 | 契约共 **84** 个动作，其中 **`SCENE.*` 一个都没有**；也没有 `SHOT.CREATE`、`SHOT.STATUS.CHANGE` | `action_registry.yaml`（`total_actions: 84`）逐行核对 |
| F2 | Shot 的**唯一**创建入口是 `SHOT.REVISION.CONFIRM`，原文「首次确认（草案转正式）在同一事务内原子创建 Shot 与首个 ShotRevision」 | `action_registry.yaml` 该条 `approval_requirement` |
| F3 | 该动作 `status: ACTIVE`、`risk_level: HIGH`、`allowed_principals: 导演/镜头负责人` | 同上 |
| F4 | Shot 五态 **10 条边**、Scene 三态 **3 条边**，**零个动作可触发** | `state_machines.yaml` 的 `Shot` / `Scene` 条目 |
| F5 | `permission_matrix.yaml` 已降为**角色摘要级**，`action_matrix` 不再保存 action_key 级副本，「统一由 action_registry.yaml 派生（单一事实源）」 | 该文件 `note:` |
| F6 | `SCENE` 在 `permission_matrix.yaml` 中出现 **0 次** | `grep -c -i scene` = 0 |
| F7 | 批次五**已建齐**状态机 guard 所需的全部 4 列：`blocked_reason` / `rework_basis_ref` / `cancellation_ref` / `cancelled_at`（外加 `source_draft_ref`） | `i9d0e1f2a3b4_scene_shot_tables.py` 的 `_create_shots_sql()` |
| F8 | `shots.scene_id` 与 `scenes.scene_id` 均 `NOT NULL` | 同上，第 137 / 169 行 |
| F9 | ADR-R19 已把待裁问题写死为三条，并明确拒绝「把 `SHOT.REVISION.CONFIRM` 的 `resource_type` 扩成 `Shot/ShotRevision/Scene`」，理由是**那等于替所有者决定「确认拆镜即建立正式场次」** | `ADR-R19-scene-entity-contract.md` §创建入口未定 |
| F10 | 契约新增动作的**唯一合法通道**是 `contract_amendments.yaml`，每条必须给 `id / title / reason / adr / source_commits / approved_by / approved_at`，**缺一项即 FAIL** | 该文件头部「写入口径」 |
| F11 | 既有先例 A2 / A3：面对「有转移表、无入口」的静默缺口，处置方式是**补登记单一 `STATUS.CHANGE` 动作，目标状态作参数**，转移合法性由转移表运行时校验，**不按转移逐条拆分动作** | `V205-R14-A2` / `V205-R14-A3` |
| F12 | 当前计数：`actions: 84`、`openapi_operations: 84`、`schema_objects: 84`、`state_machines: 43` | `V205-R14-A12.counts_after` |
| F13 | `check_action_openapi_consistency()` 要求 **action_key 集合 == openapi operationId 集合**（归一化后） | `scripts/contract_check.py:218` |
| F14 | `check_transition_entry_gaps()` 的反向完备性**只遍历 `_STATE_MACHINE_SOURCES`**（当前 4 个：Project / ScriptRevision / Episode / AssetVersion），故 Shot / Scene **不在检查范围**，闸门现在全绿是**正确行为**，不是漏检 | `scripts/contract_check.py:1298`、`_STATE_MACHINE_SOURCES` |
| F15 | 但一旦把 Shot / Scene 的转移表加进 `_STATE_MACHINE_SOURCES`，闸门**立即**要求「有已实现的状态变更动作，否则必须在 `transition_entry_gaps` 登记债务」 | 同上，反向完备性分支 |
| F16 | 基线正文的 ADR 索引是 `ADR-001…118`，**不含运行期 `ADR-R*` 系列**，新增 R27 不动 `adr_index.yaml` | `docs/adr/adr_index.yaml` 头部 |
| F17 | 三张表均 `relrowsecurity=true` 但 **`relforcerowsecurity=false`**，owner 为 `postgres`（`rolsuper=true` + `rolbypassrls=true`）⇒ **表 owner 绕过 RLS** | 目标库 `15444/ai_film_industrial` 实查 `pg_class` / `pg_roles`（2026-11-20） |
| F18 | `fk_shots_scene_project (project_id, scene_id) → scenes(project_id, scene_id)` **已存在且 `convalidated=true`** ⇒ 一个场次下的所有镜头**必然与其同 `project_id`** | 实查 `pg_constraint`，迁移 `i9d0` 建表语句 |

---

## 2. 硬阻塞链：为什么这两张表现在是「死的」

```
Scene 无创建入口（F1）
        ↓
scenes 表无行可插（除非手工 SQL，绕过全部服务层）
        ↓
shots.scene_id NOT NULL 无合法值可填（F8）
        ↓
SHOT.REVISION.CONFIRM 首次确认必然失败（NOT NULL 或 23503）
        ↓
shots 表无行 → 6 条接边外键（shot_specs / generation_tasks / …）无目标可指
        ↓
镜头域整段无法推进，资产与交付域在镜头处断头
```

**这条链的每一环都不是推测**，是表约束与动作清单的直接推论。所以批次五的成果准确表述是：
**「约束已就位，闭环为 0」**——12 条外键、13 条 RLS 策略、4 条 guard 列全部生效，
但没有一行数据能合法进入。

---

## 3. 第四问：本轮才发现的一条，ADR-R19 未列

ADR-R19 的三问只覆盖 Scene。逐边核对后发现 **Shot 的状态推进同样零入口**（F4），
而且更严重——它牵出一个 ADR 从未记录的边界：

**Shot 的 10 条边里，有 4 条在当前契约下结构上不可执行。**

| 边 | guard 字段 | 依赖对象 | 现状 |
|---|---|---|---|
| `NOT_STARTED→BLOCKED` | `blocked_reason` | 无（text） | ✅ 可执行 |
| `IN_PRODUCTION→BLOCKED` | `blocked_reason` | 无 | ✅ 可执行 |
| `NOT_STARTED→IN_PRODUCTION` | 无 | 无 | ✅ 可执行 |
| `IN_PRODUCTION→PRODUCTION_COMPLETE` | 无 | 无 | ✅ 可执行 |
| `BLOCKED→IN_PRODUCTION` | 无 | 无 | ✅ 可执行 |
| `BLOCKED→NOT_STARTED` | 无 | 无 | ✅ 可执行 |
| `PRODUCTION_COMPLETE→IN_PRODUCTION` | `rework_basis_ref` | **`ReworkTask`（有契约、无表）** | ❌ 不可执行 |
| `NOT_STARTED→CANCELLED` | `cancellation_ref` + `cancelled_at` | **授权记录（无契约）** | ❌ 不可执行 |
| `IN_PRODUCTION→CANCELLED` | 同上 | 同上 | ❌ 不可执行 |
| `BLOCKED→CANCELLED` | 同上 | 同上 | ❌ 不可执行 |

`rework_basis_ref` 指向 `workflow.rework_tasks`——这正是上一轮残留在 DANGLING 里的
那一条；`cancellation_ref` 指向的授权记录连契约都没有（归属闸门的
`AUTH_RECORD_UNMODELED` 豁免就是为它设的，前提一旦被打破就自动退回 DANGLING）。

**结论：可执行 6 条，不可执行 4 条。** 那 4 条必须 **fail-closed 并显式登记债务**
（`state_machine_gaps.yaml` 已有同型分区可放），绝不能实现成「写入即成功」——
「无依据的回退是绕过验收的暗门」这句话就写在 `Shot` 的 guard rule 原文里。

---

## 4. 四个待裁问题

### Q1　Scene 由谁创建？

| 方案 | 内容 | 代价 |
|---|---|---|
| **A1（推荐）** | 新增 `SCENE.CREATE`，镜像 `EPISODE.CREATE` | 需要一个新动作 |
| A2 | 扩展 `SHOT.REVISION.CONFIRM` 的 `resource_type` 为 `Shot/ShotRevision/Scene`，确认时原子建场次 | 零新动作，但见下 |
| A3 | 由拆镜草案确认（`ShotBreakdownDraft` → `CONFIRMED`）时批量建场次 | 零新动作，但把场次创建埋进草案状态机的副作用里 |

**推荐 A1，三条理由按强度排列：**

1. **权限扩散**（最强）。`SHOT.REVISION.CONFIRM` 是 `risk_level: HIGH`、
   `allowed_principals: 导演/镜头负责人`。A2 会让「有镜头确认权的人」自动获得
   「建场次权」。而场次创建在业务上属拆镜/编剧范畴（对齐 `EPISODE.CREATE` 的
   `导演/编剧/制片`）。角色集不同，混入即丢失职责分离——这正是红线里
   「Agent 不得自批自用」要防的那类耦合，只不过换成「一个动作包了两级权限」。
2. **空场次不可表达**。A2 与 A3 都要求「先有镜头才有场次」，于是
   「整场戏被删光、场次仍需保留」（保留编号、留档、后续补拍）永远建不出来。
   而 Scene 自己的 `DRAFT→ARCHIVED` 边本就是为了处理「建错了」。
3. **跨层同构**。`Scene` 的状态机刻意 `derived_from_parent_lifecycle`（镜像 Episode，
   `state_machines.yaml` 原文说明了理由）。入口形状也应同构，否则跨层校验无法复用
   同一套判定——这正是当初拒绝给 Scene 另造一套状态集的同一个理由。

**反证 A2 的唯一强项**：它不需要新动作，绕开了 amendment 流程。
但「省一次登记」换来一个高风险动作的权力膨胀，不值得。
ADR-R19 已经先一步拒绝过 A2（F9），本草案只是补上量化理由。

### Q2　`DRAFT` 由谁推进到 `ACTIVE`？

- **B1（推荐）**：新增 `SCENE.STATUS.CHANGE`，目标状态作参数，按转移表运行时校验。
  完全复用 F11 的 A2/A3 先例与 `services/project_planning/service/transition.py`
  的 `advance_status()`，零新机制。
- B2：建时直接 `ACTIVE`，取消 `DRAFT`。**需改 `state_machines.yaml` 与已冻结的
  `Scene` schema（`enum` 已收 DRAFT）**，且销毁「登记后可修改再启用」的能力。不建议。
- B3：确认拆镜时隐式推进。与 Q1-A2 同一问题（副作用埋入他人动作），且
  「谁把它转 ACTIVE」将不可审计——`StatusTransition` 事实表（A12）记不到真实行为人。

### Q3　归档前如何确认其下没有 `IN_PRODUCTION` 的镜头？

> **状态：已落地**（迁移 `j0e1f2a3b4c5`，验收 `verify_batch6.py` 13/13）。
> 与 Q1/Q2/Q4 不同，本问不依赖新的 action 契约，故先于其他三问实施。
> 实施中推翻了本文早期的一处判断，见下方「早期判断（已作废）」。

这是**跨表存在性不变量**：「不存在 `status='IN_PRODUCTION'` 的子行」。
外键只能表达「引用存在」，表达不了「不得存在」，故 F7 的列与既有 12 条外键都帮不上。

| 方案 | 内容 | 判定 |
|---|---|---|
| **C1（推荐）** | `scenes` 上 `BEFORE UPDATE` 触发器：`status→ARCHIVED` 时检查子行 | 与既有 13 个不可变守卫**同层同风格**，符合「不变量放最可靠层」 |
| C2 | service 层前置查询 | 可直接 `UPDATE` 绕过，等于没有 |
| C3 | 推到 Gate 裁决时检查 | 归档已经发生，检查太晚；且 Gate 是审核事实，不该兼职归属一致性 |

**C1 的核心风险是假阴性与权限层，两者均已实测裁定。** 结论见下，包含一处
对本文早期判断的推翻。

#### 早期判断（已作废，保留以示偏差在哪）

早期版本写：「不需要 `SECURITY DEFINER`，`INVOKER` 就够。理由是
`(project_id, scene_id)` 复合外键保证子行与场次同项目；调用者正在 `UPDATE`
该 `scene`，说明它对该项目可见，于是对该项目的镜头也可见，RLS 过滤不掉。」

这条推理**只覆盖了 RLS 层，漏了表权限层**。两层的差别是决定性的：

> RLS 是「看见哪些行」，表权限是「能不能碰这张表」。前者过滤后返回 0 行
> （静默），后者直接 `42501`（报错）。

#### 实测结论（`verify_batch6.py` P1，含变异反证）

| 事实 | 实测值 | 来源 |
|---|---|---|
| `svc_project_planning` 对 `shot_planning.shots` | **零权限**（`role_table_grants` 里根本没有这一行） | 实库查询 |
| `svc_shot_planning` 对 `project_planning.scenes` | 同样零权限 | 实库查询 |
| 函数用 `SECURITY INVOKER` | 归档报 `42501 permission denied for schema shot_planning` | 变异后 P1 实测 |
| 函数用 `SECURITY DEFINER` | 归档报 `P0001`（业务拒绝），消息含镜头数 | P1 实测 |

后果不是「只挡住不该归档的」，而是 **所有场次归档一律失败**——功能整体不可用，
且报错看起来像业务规则拒绝。这是最坏的一种失败形态（与受控例外陷阱③同型）。

**补授权不是解法**：给 `svc_project_planning` 授予 `shots` 的 `SELECT`，等于让
项目规划域直接读镜头域原始表，破坏「只能经服务/API 交互」的数据所有权边界，
且是永久性权限扩散。

#### 判据是「函数在判什么」，不是「函数是 DEFINER 还是 INVOKER」

表面矛盾，实则同一判据：

- 受控例外的函数在**判定授权**（谁有资格改归属列）。`DEFINER` 会让
  `current_user` 变成 owner、`pg_has_role` 恒真 ⇒ 授权判定被绕过。
- 本触发器在**检查数据完整性**：不判角色、不返回业务数据、不写任何表，
  出口只有 `RAISE` 与 `RETURN NEW`。`DEFINER` 只会让它**看得更全**
  （更严格地拒绝），不扩大任何调用方权限。

因此两条规则并不冲突：**判定授权用 INVOKER，检查数据用 DEFINER**。

`SET search_path` 是 `DEFINER` 的强制配套（否则调用方可植入同名对象劫持解析）。
既有 13 个守卫都没设，这是既存隐患，本批次不改它们，但新函数一律设。

#### 必须双向

只拦归档侧会被对称路径绕过：

    归档场次（被拦） → 先把镜头推进 IN_PRODUCTION（无人拦） → 再归档

故 `j0e1f2a3b4c5` 建两条：A 侧（`scenes` 归档时查子行）+ B 侧（`shots` 进入
`IN_PRODUCTION` 时查父行，含 `INSERT` 直插）。

B 侧有一处刻意放行：**父行此刻查不到时不拒**。理由是同一事务内「先建场次、
后建镜头」是合法路径（外键可能尚未检查），父行缺失由 `fk_shots_scene_project`
兜底成 `23503`；在那里拒会把正常路径一起打死。`verify_batch6.py` F4 钉住这一条。

#### 仍然成立的两条要求

1. 台账加**可复算的违例行数**（能 FAIL），独立于触发器正确性；否则「触发器存在」
   会被当成「不变量成立」，正是 D-10.1 禁止的那件事。已在
   `acceptance_ledger.collect_invariants` 落地，并做过「卸守卫→造存量违例→
   装守卫」的反证（违例行数=1 ⇒ FAIL）。
2. 迁移注释写明：**若 `fk_shots_scene_project` 被降级或删除，A 侧的可见性论证
   失效**，届时必须重做论证——不得假定它会永远在。

另注一处既存风险（**非本草案引入**，仅因其影响上述论证而记录）：F17 的
`force_rls=false` + `owner=postgres` 意味着**任何以 postgres 连接的应用路径，
RLS 全部失效**。这不影响本触发器（它不依赖 RLS），但意味着「RLS 已启用」
不能当作「租户隔离已成立」。列入安全域待审计项，本草案不扩权处理。

#### 范围：只拦 `IN_PRODUCTION`

`NOT_STARTED` / `BLOCKED` 不拦。语义上「归档」= 「这一场不做了」，砍掉未开拍的
场次是合法操作；而在制品归档意味着资源正在投入却突然挂到封存父下。

`BLOCKED` 是否纳入属所有者保留裁定（它介于两者之间）。当前不拦，并有两处
钉子防悄悄变化：`verify_batch6.py` F5 断言它放行，迁移注释写明这是刻意范围。

### Q4　Shot 的状态推进由谁做？（ADR-R19 未列，本轮新增）

- **D1（推荐）**：新增 `SHOT.STATUS.CHANGE`，单一动作 + 目标状态作参数 + 转移表校验
  + guard 字段强制。可执行 6 条边先上，4 条不可执行的 fail-closed 并登记债务（§3）。
- D2：按 10 条边拆成 10 个动作。直接违反 F11 先例，且与转移表重复维护。不建议。
- D3：由 `GateInstance` 裁决副作用推进。会把「审核结论」与「生产进度」耦合，
  而 `Shot` 五态的注释原文明确说「PRODUCTION_COMPLETE ≠ Gate 3/4 通过」——
  两者刻意分离，D3 等于把它们焊回去。不建议。

---

## 5. 若批准，落地清单（精确到条）

### 5.1 契约侧（顺序不可倒）

1. `contract_amendments.yaml` 追加 **`V205-R14-A13`**，`additions.actions` 三条：
   `SCENE.CREATE`、`SCENE.STATUS.CHANGE`、`SHOT.STATUS.CHANGE`。
   `counts_after`：`actions: 87`、`openapi_operations: 87`、`schema_objects: 84`、
   `state_machines: 43`（本次不新增对象与状态机）。
2. **然后**改派生文件：`action_registry.yaml` 三条 + `total_actions: 87`。
3. **同时**改 `openapi.draft.yaml` 三条 operationId——F13 是硬闭环，少一条闸门即红。
4. `state_machine_gaps.yaml` 的 `transition_entry_gaps` 新增 Shot 的 4 条不可执行边债务。
5. **不动**：`object_schemas.schema.json`（Scene/Shot 已有 schema）、`state_machines.yaml`
   （转移表已定，本草案不改一条边）、`permission_matrix.yaml`（F5：action 级已不在此维护）。

### 5.2 代码侧（同型复用，不新造机制）

| 层 | 新增文件 | 参照 |
|---|---|---|
| domain | `services/project_planning/domain/scene.py` | `domain/episode.py` |
| domain | `services/project_planning/domain/shot.py` | 同上，另需五态枚举 |
| repository | `scene_repo.py` / `scene_sqlalchemy_repo.py` / `shot_repo.py` / `shot_sqlalchemy_repo.py` | `episode_repo.py` / `episode_sqlalchemy_repo.py` |
| service | `scene_service.py` / `shot_service.py` | `episode_service.py`（幂等键、归属验证、outbox 同事务） |
| service | 状态推进 | **直接复用** `transition.py:advance_status()`，不另写 |
| api | `scene_routes.py` / `shot_routes.py` | `episode_routes.py` |
| 闸门 | `_STATE_MACHINE_SOURCES` 加 `Scene` / `Shot` | F14/F15：加了就必须有入口，故本清单第 1 步必须先完成 |

`shot_service` 的首次创建**必须**由 `SHOT.REVISION.CONFIRM` 驱动（F2），
不得给 Shot 再开一个 `SHOT.CREATE`——那会成为绕过拆镜确认的后门。

### 5.3 权限绑定建议

按 F5，新动作的权限只写在 `action_registry.yaml`：

| action | risk | allowed_principals | 依据 |
|---|---|---|---|
| `SCENE.CREATE` | MEDIUM | 导演/编剧/制片 | 逐字对齐 `EPISODE.CREATE`（同层级、同状态机形状） |
| `SCENE.STATUS.CHANGE` | MEDIUM | 导演/制片 | 对齐 `EPISODE.STATUS.CHANGE` |
| `SHOT.STATUS.CHANGE` | MEDIUM | 导演/技术导演/制片 | `NOT_STARTED→IN_PRODUCTION` 是生产事实，不应由 Agent 置；`PRODUCTION_COMPLETE` 同理 |

三条都需新增 `negative_test_id`：`ACT-SCENE-NEG-001`（跨集建场次拒绝）、
`ACT-SCENE-NEG-002`（Agent 归档拒绝）、`ACT-SHOT-NEG-003`（Agent 置生产完成拒绝）。

---

## 6. 若不批：替代路径与代价

保持两张表只读，把它们标为**结构性预留**，在台账里登记 `RUNTIME_PENDING`。代价：

- 台账中 `scenes` / `shots` 两行永远 `pending`，且 `shots` 那 6 条接边外键永远无目标行；
- 主链在镜头域**断头**，资产域 / 交付域的镜头级挂载全部无法开始；
- 更实际的代价：批次五的 26 项验证证明了约束正确，但**没有任何一项证明这张表能用**——
  这个落差会一直留着，直到入口裁定。

若所有者选择这条，本草案会转为 `ADR-R27（DEFERRED）`，并把上述代价写进
`resource_schema_gaps.yaml` / `state_machine_gaps.yaml`，让缺口保持可见，
而不是消失在「已完成」的批次报告里。

---

## 7. 本草案**没有**做的事（边界声明）

- 未改动任何契约与派生文件（草案阶段只论证）。
- 未写任何 ORM / service / action 代码（等裁定）。
- 未改 `state_machines.yaml` 一条边、未改 `object_schemas` 一个字段。
- 未动 ADR-R28 / ADR-R40 六个历史版本字节。
- 未擅自把 `ShotBreakdownDraft` 的「五态 5 条边无入口」一并纳入（`SHOT.DRAFT.*` 三个动作
  尚未登记，属拆镜域，与本轮相邻但不同域；已在 `action_implementations.yaml` 显式登记债务）。
  另有一处语义交叠须在拆镜域裁定：草案 `IN_REVIEW→CONFIRMED` 与
  `SHOT.REVISION.CONFIRM` 是**两级确认**（前者「稿件通过审阅」、后者「逐镜头转正式」），
  不冗余，但顺序是否强制，本草案不裁。
