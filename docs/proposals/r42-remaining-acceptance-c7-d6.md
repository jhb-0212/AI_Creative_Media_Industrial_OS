---
doc_type: proposal
status: implemented
updated_at: 2026-11-24
authoritative_sources:
  - contracts/action_registry/action_registry.yaml
  - contracts/runtime/action_implementations.yaml
  - contracts/runtime/event_catalog.yaml
  - docs/audits/2026-11-24-r42-authorization-reconciliation.md
open_items: []
---

# R42 剩余验收实施基线：C7-b 人工入队来源核验 + D6-9 受权死信重放

> 性质：**实施基线**（2026-11-24 工程审查建议的采纳登记）+ 实施结果记录。
> 两批次**独立提交、独立验收**，互不夹带（不顺带改 P2 或其它冻结内容）。
> 报告口径：真实 PostgreSQL 实测与静态测试**分开报告**；未实际运行的标 `NOT RUN`，环境阻塞标 `BLOCKED`。
> **两批均已实施完毕**：C7-b 见 §7，D6-9 见 §8（2026-11-24，契约面与授权来源先登记后启动）。
> 判据之外明确挂账的事项见 §7.4 / §8.4——不在 open_items 里，是因为它们是
> 「本基线不做」，不是「本基线没做完」。

---

## 0. 前置侦察：先确认权威来源，再定改动点

上一版是判据清单，没有落点。本版每条判据都指到具体事实。以下 10 条均在本机实测或逐行实读，不是推测。

| # | 事实 | 证据 |
|---|---|---|
| F1 | `QC.TASK.ENQUEUE` 的契约**已写死**入队约束与负向测试编号 | `contracts/action_registry/action_registry.yaml:1167-1176`：`approval_requirement: '仅在生成终态且携带产物引用时入队；同一生成任务不得重复入队'`、`negative_test_id: ACT-QC-NEG-002` |
| F2 | 但该 action 的席位是**系统主体**，与唯一真实使用者（人工 HTTP）矛盾 | 同上 `allowed_principals: '生成 Worker/受信调度服务'`；自动路径 `workers/relay/handlers.py::enqueue_idempotently` 直调服务、`created_by="outbox-relay"`，不经 HTTP |
| F3 | 幂等事实**已在数据库层**统一 | 迁移 `f1a2b3c4d5e6_qc_tasks.py:104-111` 建 `uq_qc_governance_qc_tasks_generation_task` UNIQUE(`generation_task_id`)；`services/qc_governance/repository/orm.py:87-94` 同步 |
| F4 | 跨域核验**不能同 Session**：QC 域角色对生产域零授权 | 实测 `information_schema.role_table_grants`：`qc_governance_role` 在 `production_planning.*` 无任何授权；`app_role` 在四域仅 `outbox.outbox_events SELECT` |
| F5 | 跨域核验的**既有模式**就是两个域各开各的连接 | `workers/relay/asset_registration.py:245-303`：先 production_planning session 核验、再 asset_service session 写入，不共享事务；API 进程已有 `get_production_planning_tenant_uow`（`services/identity_access/deps.py:184-203`） |
| F6 | `outbox_relay_role` 对 `outbox_events` **只有 SELECT + 三列 UPDATE，无 INSERT** | 实测 `information_schema.column_privileges`：SELECT 全列 + UPDATE(`published_at`,`attempts`,`next_attempt_at`,`dead_lettered_at`)；与 `packages/shared/partition_guard.py:42` 的 `RELAY_UPDATE_COLUMNS` 一致 |
| F7 | 运维内部表**不登记**契约对象 | `outbox.maintenance_runs`（`packages/shared/outbox.py:84-100`）在 `object_schemas.schema.json` 与 `state_machines.yaml` 中匹配数均为 **0** |
| F8 | 动作台账只有**二元状态**，CLI-only 无法如实登记 | `contracts/runtime/action_implementations.yaml` 头注：每个 ACTIVE action 必须落 `implemented`（route 由真实加载 `app.openapi()` 校验）或 `not_implemented`（带 reason）；PHASE_GATED 不入台账 |
| F9 | `QC_TASK_QUEUED` 用 `payload_required` 而非严格载荷模型 | `contracts/runtime/event_catalog.yaml:630-640`；无 `payload_schema` |
| F10 | `outbox_events` **没有 `last_error` 列** | `packages/shared/outbox.py:42-82` 全字段实读：只有 `attempts` / `next_attempt_at` / `dead_lettered_at` |

---

## 1. 侦察改变了三条结论

### 1.1 C7-b 不是新需求，是**既有契约条款未实现**

`action_registry.yaml:1172` 已经写明「仅在生成终态且携带产物引用时入队；同一生成任务不得重复入队」，`negative_test_id` 也已登记。实现两条都没做。

因此这不是「给端点加功能」，而是**实现追上已登记的契约**。定性差别很实际：

- 判据不是「加了校验」，而是「契约里已写的不变量，运行时是否真的成立」；
- 契约不需要为此改条款（只改席位，见 2.2）；
- 反过来说，只要这条不成立，`QC.TASK.ENQUEUE` 这个 ACTIVE 动作就长期处于**违约**状态——契约说 A，代码做 B，而闸门看不出来。

另外要纠正一个容易误读的点：`negative_test_id: ACT-QC-NEG-002` **不等于已有用例**。`tests/permission/test_permission_negative.py` 的自述是「仅校验契约本身，不依赖尚未实现的运行时权限代码」——它只断言每个 action 有唯一编号。所以 C7-6 要求的真实 PG 负向测试是**新增工作**，不能记成既有覆盖。

### 1.2 D6 的审计**不能**复用 outbox 事件（推翻直觉方案）

直觉方案是复用 T2 的先例：冲突证据写成 append-only outbox 事件（`ASSET_REGISTRATION_CONFLICT`），不新建表。但那个先例成立有个前提——**写它的主体是业务域角色**（`production_planning_role` 有 `outbox_events INSERT`），本来就有写业务事件的权限。

重放不同：执行重放的是**运维角色**。若让运维角色拿到 `outbox_events INSERT`，它就同时获得「伪造任意业务事件」的能力——这是权限倒退，不是复用。

实测边界（F6）：`outbox_relay_role` 能改三列投递状态，**不能 INSERT**。这个边界正好把两件事分开：

| 动作 | 权限 | 结论 |
|---|---|---|
| 清死信标记（改 `dead_lettered_at`/`next_attempt_at`/`attempts`） | relay 角色已有列级 UPDATE | 复用，**不扩权** |
| 写重放审计 | relay 角色无 INSERT | 必须另找载体 |

所以审计载体是**独立的 append-only 运维表** `outbox.dead_letter_replays`，由专用 ops 角色持有 INSERT。这不是「叠加重复审计表」，而是权限边界决定的：审计表可写、业务事件表不可写，两者不能合并。

按 F7 的 `maintenance_runs` 先例，运维表不进 `object_schemas` / `state_machines`，**不改变契约对象计数**。

### 1.3 C7-5 不需要新机制，现有的唯一键就是统一幂等事实

`uq_qc_governance_qc_tasks_generation_task` UNIQUE(`generation_task_id`) 已在库层存在（F3），自动路径已用它做并发兜底（`enqueue_idempotently` 捕 `IDEMPOTENCY_CONFLICT` 收敛为「已入队 = 成功」）。

所以 C7-5 的「必须从数据库持久化事实保证」**已经成立**，缺的是：

1. 人工路径撞唯一键时的错误映射（应是 409 业务冲突，不是 500）；
2. 真实并发用例（自动与人工互撞、人工双发）。

若在这里再加一套幂等表或分布式锁，就是用户明令禁止的「为满足判据叠加重复层」。

---

## 2. C7-b 批次：人工入队来源核验（六项判据）

### 2.1 判据 → 落点

| 编号 | 判据 | 现状 | 落点 | 验收 |
|---|---|---|---|---|
| C7-1 | 可信身份来源 | **已满足**：`qc_task_routes.py:118` 取 `principal.subject_id` / `principal.tenant_id`；`FinishRequest` 已 `extra="forbid"` | 无代码改动 | 请求体携带 `tenant_id`/`created_by`/`principal` 时 422（补测） |
| C7-2 | 专项授权，不能仅凭普通 QC 权限 | **不满足**：席位是「生成 Worker/受信调度服务」（F1/F2），人工要么被拒、要么被宽席位放行 | 新增 action `QC.TASK.RECOVER`（`action_registry.yaml` +1、`openapi.draft.yaml` +1、`action_implementations.yaml` 登记 `implemented.route`、`negative_test_id` 新分配）；端点依赖改 `require_role("QC.TASK.RECOVER")`。`QC.TASK.ENQUEUE` 回归其声明语义——只描述 relay 自动路径的领域调用 | 用无 `QC.TASK.RECOVER` 但有 `QC.TASK.*` 其它权限的主体调用 → 403；用有该动作的主体 → 通过 |
| C7-3 | 从数据库权威事实核验归属，不以请求体为依据 | **不满足**：`project_id`/`asset_version_id`/`quality_profile_version` 全取自 body | 新增只读核验端口：`production_planning` 独立 session 读 `generation_tasks`，核验 ①任务存在 ②`SUCCEEDED` ③`project_id`/`tenant_id` 与请求一致 ④`asset_version_id` 与任务绑定值一致 ⑤`quality_profile_version` 取任务冻结值（请求体只允许不传或相同）；`asset_service` 独立 session 读 `asset_registration_receipts` 核验回执 `COMPLETED` 且 `asset_version_id` 一致 | 跨租户 `project_id` → 404（RLS 查不到）；body 自报口径与冻结值不同 → 409/400 |
| C7-4 | 仅允许符合恢复条件的任务入队，禁绕状态机 | **不满足**（无任何状态判定） | 同 C7-3 的核验器；「可恢复」定义为：任务 `SUCCEEDED` + 回执 `COMPLETED` + 该 `generation_task_id` 尚无 QC 任务。**不给 `force` / `skip_validation`** | 非 `SUCCEEDED` / 回执缺失 / 回执 `REJECTED` 三类各自拒绝并给可区分原因 |
| C7-5 | 人工与自动共用同一幂等事实，并发也成立 | **事实已在库层**（F3） | 无需新机制；人工路径把 `IDEMPOTENCY_CONFLICT` 映射为 409 并返回既有任务 id | 真实 PG 并发两组：①自动+人工同时入队同一任务 → 只产生 1 行，另一个得 409/幂等成功；②人工双发 → 同上 |
| C7-6 | 完整审计 + 真实验收 | **部分**：`QC_TASK_QUEUED` 载荷有 `qc_task_id`/`project_id`/`asset_version_id`/`generation_task_id`/`quality_profile_version`/`tenant_id`，**无来源与原因** | `event_catalog.yaml:630-640` 的 `payload_required` 增 `source`（`RELAY`/`MANUAL`）与 `reason`；`qc_task_service.enqueue` 增同名参数；自动路径传 `RELAY`，人工传 `MANUAL` + 操作者填写的原因 | 真实 PG 负向四类（越权/跨租户/重复提交/非法状态）+ 并发两类，断言 SQLSTATE 或领域错误码 **并回读影响行数**（RLS 会把越权写静默过滤成 0 行，不认「没抛异常即通过」） |

### 2.2 为什么要新增动作而不是改既有动作的席位

两条路都能满足 C7-2，取舍如下：

| 方案 | 改动 | 代价 |
|---|---|---|
| **A（推荐）新增 `QC.TASK.RECOVER`** | 契约 +1 action、+1 openapi operation、+1 台账条目；`QC.TASK.ENQUEUE` 语义不变 | 契约计数变化需一次修订登记，但权限审计、负向测试、审计事件都能按动作键区分「谁建的 QC 任务」 |
| B 改 `QC.TASK.ENQUEUE` 的 `allowed_principals` 为人工恢复角色 | 只改一行 | 语义失真：同一动作键同时描述「系统主体执行入队」与「人工恢复入队」，出事后无法从动作键判断是自动还是人工建的；且 relay 自动路径并不经 RBAC，改席位只影响 HTTP，等于让契约描述与运行时继续错位 |

选 A 的理由不是「多一个键更规范」，而是：**动作键是这个仓库区分权限主体的既有粒度**（`QC.TASK.ENQUEUE` / `QC.TASK.PROCESS` 已经分开）。人工恢复与系统入队是两个主体集，合成一个键就是把它们重新糊在一起。

### 2.3 三条防返工约束

1. **跨域核验不在同一 Session。** 实测 `qc_governance_role` 对 `production_planning.*` 零授权（F4），在 QC 的 UoW 里 join 生产表会直接被数据库拒绝。正确做法是 F5 的既有模式：按域各开连接、读完即关，核验通过后再进 QC 的写事务。这不是「新增跨域层」，是把 T2 已经验证过的做法用到读侧。
2. **不引入旁路参数。** 若人工路径真的需要 `force` 才救得了现场，说明缺的是「合法但不满足自动条件」的**显式状态**，而不是开关。先补状态。
3. **两个入口的拒绝集合必须可比。** 自动路径的等价伪造面是「直接往 outbox 塞一条假的 `GENERATION_SUCCEEDED`」——T2 已用「事件载荷与持久化事实逐项比对」挡住。人工路径必须复用**同一个核验器**（接受规范化输入：tenant / task / project / asset_version / quality_profile），而不是各写一套。差异即缺陷。

### 2.4 本批契约影响

| 项 | 变化 | 需同步 |
|---|---|---|
| `action_registry.yaml` | actions 105 → **106** | 席位、`audit_event`、`negative_test_id` |
| `openapi.draft.yaml` | operations 105 → **106** | `x-afs-action-key`、`x-afs-negative-test` |
| `action_implementations.yaml` | +1 `implemented.route` | route 由 `app.openapi()` 真实校验 |
| `event_catalog.yaml` | 事件数不变；`QC_TASK_QUEUED` 的 `payload_required` +`source`+`reason` | `qc_task_service.enqueue` 记录字段须覆盖 |

---

## 3. D6-9 批次：受权死信重放（八项判据）

### 3.1 判据 → 落点

| 编号 | 判据 | 落点 | 验收 |
|---|---|---|---|
| D6-1 | 必须引用真实原始死信事件 | 入参 `event_id`（UUID），不接受「重新构造一个同类请求」 | 不存在的 `event_id` → 404；已 `published_at` 非空 → 409 |
| D6-2 | 专项授权 | 新增 action `OUTBOX.DEAD_LETTER.REPLAY`；走 `authorize` 单入口，不在 CLI 里另写判定（红线：禁止新增平行授权层） | 无该动作权限 → 403；且**不能**用 relay 运行时身份执行 |
| D6-3 | 重放前核验 | 同事务 `SELECT ... FOR UPDATE`：行存在、`tenant_id` 与操作者上下文一致、`dead_lettered_at IS NOT NULL`、`published_at IS NULL`、关联业务对象仍可推进（该事件类型的 handler 已注册） | 未死信 / 已投递 / 租户不符 / 无 handler 四类各自拒绝并给可区分原因 |
| D6-4 | 原事件不可变，独立 `replay_id` | 只 UPDATE 三列（列级 GRANT 已限定，F6）；`event_id`/`payload`/`event_type` 不动；`replay_id` 落审计表 | 重放后比对 `payload`、`event_type` 哈希不变 |
| D6-5 | 幂等：不因 `replay_id` 不同而重复产生副作用 | 业务副作用幂等**继续由各 handler 承担**（T2 已有回执唯一键 + 请求指纹）；重放层只负责「让事件重新具备被领取资格」。操作层幂等：`dead_letter_replays.replay_id` UNIQUE + `(source_event_id)` 在途排他 | 同一 `event_id` 重复重放，业务侧资产/回执行数不变 |
| D6-6 | 并发控制（数据库级） | 条件 UPDATE：`WHERE event_id=:eid AND dead_lettered_at IS NOT NULL AND published_at IS NULL`，以 `rowcount` 判定；0 行即「已不在死信态」→ 409。不需新锁 | 两个并发事务重放同一条：一个成功，另一个 rowcount=0 被拒 |
| D6-7 | 追加式审计 | 新表 `outbox.dead_letter_replays`（append-only，角色只有 INSERT/SELECT，无 UPDATE/DELETE）：`replay_id` PK、`source_event_id`、`tenant_id`、`operator`、`reason`、`attempts_before`、`dead_lettered_at_before`、`result`、`failure_reason`、`created_at` | 每次尝试（成功或失败）恰好一行；缺字段即该次视为未发生 |
| D6-8 | 失败保留与可追溯 | 原始死信行**不删除、不覆盖**：失败后 `attempts` 从 0 重新累加、`dead_lettered_at` 重新写入（这是**新一次**失败事实，不是覆盖）；审计表追加失败行 | 重放失败后：原 `event_id` 行仍在、审计有 `result='FAILED'`+原因、`payload` 未变 |

### 3.2 `replay_id` 是操作身份，不是事件身份

这一条必须写死，因为它最容易做错：

- `replay_id` 标识「某人在某时对某条死信做过一次重放」；
- 业务幂等键仍然是 `event_id`（以及下游 handler 自己的业务键，如 T2 的 `(tenant_id, generation_task_id)` 回执）；
- 若把 `replay_id` 当成事件新身份（例如生成新 `event_id` 重新投递），就等于绕过 `event_id` 幂等，让同一业务事件执行两次。

所以验收里有一条硬判据：**重放两次同一条事件，业务副作用行数必须不变**。这比「接口返回 200」有意义得多。

### 3.3 清零 `attempts` 的顺序纪律（最容易做错的一步）

`attempts` 是原始失败事实的一部分。若先清零再写审计，`attempts_before` 就永久丢失——那正是 D6-8 禁止的「覆盖原始失败记录」。

正确顺序（同一事务内）：

1. `SELECT ... FOR UPDATE` 读出 `attempts` / `dead_lettered_at`；
2. **先** INSERT 审计行（含 `attempts_before`、`dead_lettered_at_before`、`result='IN_FLIGHT'`）；
3. **再** UPDATE 三列（`dead_lettered_at=NULL`、`next_attempt_at=now()`、`attempts=0`）；
4. COMMIT；投递结果由 relay 正常 claim 后产生，后续失败会重新累加 `attempts` 并重新进死信——审计行追加对应记录，原始行不被改写。

### 3.4 角色与最小权限面

| 角色 | 权限 | 理由 |
|---|---|---|
| `outbox_relay_role`（既有） | SELECT + UPDATE 三列 | 已有，重放的状态迁移复用它，不扩权 |
| `outbox_ops_role`（新增） | SELECT `outbox_events`；UPDATE 三列；INSERT/SELECT `dead_letter_replays` | 只够「重放 + 记审计」。**刻意不给 `outbox_events INSERT`**——那等于允许伪造任意业务事件 |

重放执行时开两个连接：ops 角色改状态并写审计（同事务），relay 常规 claim 后续投递。运维角色不进入 relay 进程（`outbox_maintenance` CLI 的既有理由：需要更高权限的动作就该独立进程）。

### 3.5 本批契约影响

| 项 | 变化 | 说明 |
|---|---|---|
| `action_registry.yaml` | actions 106 → **107** | `OUTBOX.DEAD_LETTER.REPLAY` |
| `openapi.draft.yaml` | operations 106 → **107** | 见 4 的入口形态裁决 |
| `action_implementations.yaml` | +1 条目 | 见 4 |
| 新表 `outbox.dead_letter_replays` | 迁移 +1 | **不登记** `object_schemas`（按 `maintenance_runs` 先例，F7） |
| `event_catalog.yaml` | **不变** | 审计走表，不发事件（见 1.2） |

---

## 4. 批次顺序（对上一版的反转）

上一版建议「先 D6 后 C7」，理由是 D6 不碰业务状态机。侦察后反转：

| 序 | 批次 | 依据 |
|---|---|---|
| 1 | **C7-b** | 它是**实现既有契约条款**（F1 已写死），现在处于违约状态；且不新增表、不新增 DB 角色 |
| 2 | **D6-9** | 它是**新增能力**：新动作 + 新角色 + 新表。风险面比 C7-b 大，放后面 |

两批都必须各自走完整的「修订登记 → 派生 → 重签 → 闸门」，不得合并成一次登记——合并会让「哪一条批准了什么」不可追溯。

---

## 5. 三点裁决（2026-11-24 已定，见审计记录 §11）

| # | 待裁 | 裁决 | 生效范围 |
|---|---|---|---|
| D-1 | C7-2 的授权形态 | **A：新增 `QC.TASK.RECOVER`**（不改 `QC.TASK.ENQUEUE` 席位） | 已随 A37 契约面落地 |
| D-2 | D6 入口形态 | **A：受权 REST 端点**（CLI 不作唯一入口） | 约束 D6-9 实施；尚需 D6 自己的契约登记 |
| D-3 | `dead_letter_replays` 是否登记为契约对象 | **不登记**（按 `maintenance_runs` 先例） | 同上 |

裁决前本基线不得进入实现批次；D-1 已随 C7-b 实施完成，D-2/D-3 仍是 D6-9 的**实施前置**，
不因为「已经裁决」就跳过 D6-9 自己的契约登记与闸门。

---

## 6. 本基线不做什么

- 不改 ADR-R42 正文，不解除上游 `[V-BLOCKED]`（解除权在 Owner，见审计记录 §9.4）。
- 不为 relay 侧投递失败原因加 `last_error` 列（F10 的既有缺口，另案；D6-8 的「失败原因」只指重放动作自身）。
- 不加 `force` / `skip_validation` / 「直接投递」等旁路。
- 不新建第二套 QC 任务创建机制——保留的是**人工恢复能力**，不是并列的入队通道。
- 不合并两批次的契约修订登记。

---

## 7. C7-b 实施结果（2026-11-24）

Owner 签署 A37 后启动。六项判据逐条落地，**两处与基线不同**，见 7.2。

### 7.1 判据 → 实现 → 验收

| 编号 | 实现落点 | 验收证据 |
|---|---|---|
| C7-1 可信身份 | 路由只从 `Principal` 取 `subject_id` / `tenant_id`；请求模型 `extra="forbid"`，`tenant_id` / `created_by` / `asset_version_id` / `quality_profile_version` 出现即 422 | `test_body_cannot_smuggle_artifact_fields`（真实 PG：422 且库里零行） |
| C7-2 专项授权 | `require_role("QC.TASK.RECOVER")`；席位 `运维恢复操作者`（`A37` 已登记） | `test_other_role_cannot_use_recovery_entry`（`QC 审核人` → 403） |
| C7-3 权威归属 | `packages/shared/generation_ports.py` 声明端口，`services/production_planning/repository/generation_facts_lookup.py` 提供适配（生产域角色按三列授权读项目），组合根 `apps/api/wiring.py` 装配。授权对象是**事实行里的** `project_id`，请求体只作选择键 | `test_project_must_match_database_facts`（错项目 → 404）、`test_cross_tenant_task_is_invisible`（跨租户 → 404） |
| C7-4 生命周期 | `QCTaskService.recover`：非 `SUCCEEDED` → 409；终态但缺产物引用 → 409（不猜值补上） | `test_non_success_terminal_is_rejected`（4 状态参数化）、`test_success_without_artifact_ref_is_rejected` |
| C7-5 统一幂等 | 人工与自动走**同一条** `enqueue`；仓储 `add` 改为 SAVEPOINT + `translate_write_error`，23505 → `DuplicateEntityError` → 服务层 `IDEMPOTENCY_CONFLICT` | `test_concurrent_recovery_creates_exactly_one`（并发 201/409、恰好 1 行）、`test_manual_entry_does_not_bypass_unique_fact_with_relay`、`test_duplicate_generation_task_translates_to_port_error`（绕过先查直接撞索引，证明是索引兜底） |
| C7-6 审计 | `QC_TASK_QUEUED` 载荷新增 `source` / `operator_id` / `reason`；`EnqueueOrigin` 值对象强制「MANUAL 必填理由」 | `test_recovery_derives_every_fact_from_database`（回读 outbox 载荷三字段） |

### 7.2 与基线的两处偏差（都不是妥协，但必须写明）

1. **C7-3 没有另读资产域回执表。** 基线写的是「`asset_service` 独立 session 读 `asset_registration_receipts` 核验回执 `COMPLETED`」。实现改为核验 `generation_tasks.status = SUCCEEDED` 且 `asset_version_id` / `quality_profile_version` 非空——因为 T3（`bind_registration_result`）**只能**从登记回执进入，`SUCCEEDED` 加非空产物引用本身已是「回执已消费」的等价事实。再加一次资产域读，是对同一事实的第二次查询：多一个跨域依赖、多一种失败模式，换不来额外保证。
2. **C7-3 的核验键多了 `project_id`（作为选择键）。** 基线默认「按 `generation_task_id` 查」。实读发现 `generation_tasks` 的唯一键是 `(generation_task_id, project_id, tenant_id)`，单给任务 ID 在库层面不唯一，`scalar_one_or_none` 会抛多行，退化成 `first()` 则是静默择一。要求同时给项目是**为了让「恢复哪一条」有确定答案**，随后由数据库三列共同确认；`project_id` 仍不参与归属判定（判定用的是事实行里的值）。

### 7.3 本批次的契约面

| 项 | 变化 |
|---|---|
| `action_registry.yaml` / `openapi.draft.yaml` | 无（A37 已登记，106 不变） |
| `action_implementations.yaml` | `QC.TASK.ENQUEUE` → `mechanism: in_process`；`QC.TASK.RECOVER` → `implemented`（`POST /qc-tasks`） |
| `event_catalog.yaml` | `QC_TASK_QUEUED.payload_required` +`source` +`operator_id` +`reason`（事件数与对象计数不变） |
| `scripts/contract_check.py` | 台账新增 `mechanism: in_process` 表达：必须写 module + reason、不得同时声明 route；授权落点检查对 in_process 跳过（其触发面由数据库权限决定，不需要也不存在 require_role） |

### 7.4 未做（明确挂账）

- **D6-9 未开始**：仍需自己的契约登记（`OUTBOX.DEAD_LETTER.REPLAY`，106→107）、新表 `outbox.dead_letter_replays`、新角色 `outbox_ops_role`。D-2/D-3 的裁决只定了形态与「是否登记」，不等于已批准实施。
- **`implemented` 台账的 `in_process` 是新表达式**：既有 46 个 HTTP 动作不受影响，但它第一次让「无 HTTP 面的已实现动作」可以被如实登记。这属于闸门能力扩展，不是为某一动作开的特例——同类动作今后应复用，不得各写一套豁免。

---

## 8. D6-9 实施结果（2026-11-24）

契约面（`2342f9e`）与授权来源（`bb258e4`）先行登记后启动。八项判据逐条落地，
**三处与基线不同**，见 8.2。基线 §3.3 的顺序纪律按同事务等价方式改写，理由一并写明。

### 8.1 判据 → 实现 → 验收

| 编号 | 实现落点 | 验收证据 |
|---|---|---|
| D6-1 引用真实原始事件 | 路径参数 `event_id`（UUID）；只按它定位，不接受「重构同类请求」。不存在 → 404 | `test_unknown_event_is_not_found`、`test_non_uuid_path_is_validation_error`（422） |
| D6-2 专项授权 | 新增动作席位 `OUTBOX.DEAD_LETTER.REPLAY`（`消息总线运维操作者`），`require_role` 单入口；执行主体是**新角色** `outbox_ops_role`，与 relay 运行时身份、业务域写入身份都不是同一条 | `test_other_role_cannot_use_replay_entry`（403）、`test_role_is_not_privileged`（NOSUPERUSER / NOBYPASSRLS） |
| D6-3 重放前核验 | 同事务 `SELECT … FOR UPDATE`，顺序：行存在 → `tenant_id` 与 Principal 一致 → `published_at IS NULL` → `dead_lettered_at IS NOT NULL` → 事件类型在契约全集内 | 五类各自 404/409，见负向四条用例（不存在 / 跨租户 / 未死信 / 已投递 / 契约外类型） |
| D6-4 原事件不可变 + 独立 `replay_id` | 只 `UPDATE` 三列；列级 `GRANT` 把 `published_at` / `payload` / `event_type` / `tenant_id` / `aggregate_id` / `occurred_at` 全部挡在库层；`replay_id` 落审计表 | `test_cannot_forge_event_content_or_delivery_time`（6 列参数化，全部 permission denied）、`test_replay_requeues_event_and_records_pre_replay_facts`（回读 payload / event_type 不变） |
| D6-5 操作层与业务层幂等分离 | 业务副作用仍由 handler 承担（重放不复制事件、不直调 handler）；操作层由「`FOR UPDATE` + 条件更新」串行化，第二次必然 409 | `test_second_replay_conflicts_and_keeps_one_audit_row`、`test_concurrent_replay_yields_one_success`（并发 200/409，审计恰好 1 行、事件行数不变） |
| D6-6 并发控制（数据库级） | 条件 `UPDATE … WHERE dead_lettered_at IS NOT NULL AND published_at IS NULL RETURNING event_id`：无返回即「已不在死信态」→ 409 | 同上并发用例；`test_cannot_delete_events` 保证并发下也不会出现「删了重发」 |
| D6-7 追加式审计 | 新表 `outbox.dead_letter_replays`：`replay_id` PK / `source_event_id` / `tenant_id` / `event_type` / `operator` / `reason` / `attempts_before` / `dead_lettered_at_before` / `result`（CHECK `SUCCEEDED`\|`FAILED`）/ `failure_reason` / `created_at` | `test_audit_rows_are_append_only`（UPDATE、TRUNCATE 均被触发器拒绝）、`test_audit_table_is_append_only_by_grant`（授权层三条全拒） |
| D6-8 失败保留与可追溯 | 原始死信行从不删除、从不覆盖；重放后的再次失败由 relay 的 `_FAIL` 重新累加 `attempts` 并重新写 `dead_lettered_at`（新一次失败事实） | `test_replay_requeues_event_and_records_pre_replay_facts`（`attempts=0`、行仍在）、`test_live_event_cannot_be_replayed`（未死信时连 `attempts` 都不动） |

### 8.2 与基线的三处偏差（都是基线不可实施，不是妥协）

1. **§3.3 的顺序纪律改为同事务等价式。** 基线要求「先写 `IN_FLIGHT` 审计 → 清零 → 事后改写终值」，但审计表是 append-only（授权层无 UPDATE、触发器拒绝 UPDATE），**第二次写入终值这一步在库层就做不到**。实现改为「锁行 → 条件清标记 → 写审计(`SUCCEEDED`)」同一事务：提交则三件事一起成立，回滚则一件都没发生。基线真正要保的东西没丢——`attempts` / `dead_lettered_at` 在锁行时已读入内存，审计行永远带真实的「重放前」值，因此不需要二次改写。代价是 `FAILED` 这一枚举在当前实现下不可达（接受后的重放是全有或全无）；`result` 的 CHECK 仍保留两个值，等将来出现多步重放时才有 `FAILED`。**这一点写在代码与审计列注释里，不留给读代码的人去猜。**

2. **D6-3 的「handler 已注册」改为「事件类型在契约全集内」。** 那份 handler 注册表活在 relay 进程里，构建它需要三个域会话工厂——API 进程既拿不到、也不该拿到。诚实的替代是查 `contracts/runtime/event_catalog.yaml`：契约外类型必然没有消费者，重放它只会制造一条永不被 ack 的积压。契约内类型若恰好没注册 handler，后果也不是丢事件——relay 对无处理器事件**不 ack**，积压上涨并报警。判据实质（不因重放而丢事件）仍成立。

3. **D6-6 的 `rowcount` 改为 `RETURNING`。** 语义等价，但 `RETURNING` 返回的就是「这一行确实被改到了」的证据，不必再把结果对象当游标解释；对分区表与普通表行为也一致。

### 8.3 本批次的契约面

| 项 | 变化 |
|---|---|
| `action_registry.yaml` / `openapi.draft.yaml` / `permission` | 无（`bb258e4` 已登记，107 不变） |
| `action_implementations.yaml` | `OUTBOX.DEAD_LETTER.REPLAY`：`not_implemented` → `implemented`（`POST /outbox/dead-letters/{event_id}/replay`） |
| `resource_schema_gaps.yaml` → `resource_schema_exemptions.yaml` | `OutboxEvent` 债务转为豁免：正文没有事件总线的字段口径，为它发明 schema 等于凭空造契约正文 |
| `event_emission_gaps.yaml` | 新增 1 条显式债务：本动作的 `audit_event` 写的是**表名**，审计载体是表不是事件（`outbox_ops_role` 对 `outbox_events` 刻意无 INSERT） |
| `packages/shared/db_grants.py` | 新增 `INFRA_ROLES` / `INFRA_ROLE_GRANTS` / `infra_converge_statements` / `validate_infra_matrix`：运维角色没有自有 schema，塞进 `DOMAINS` 会让「本域表全覆盖」判据变永真式 |
| 新迁移 `c5d6e7f8a9b0` | 建角色 + 建表 + 双触发器 + RLS + 父表授权收敛 + 现存分区补齐授权 |

### 8.4 未做（明确挂账）

- **观测面**：`outbox_maintenance` 的 metrics 里没有「重放次数 / 死信存量」指标。当前只能查表；加指标需要先定口径（按事件类型？按操作者？），不属于本批判据。
- **死信查询入口**：本批只做写入口（重放），查询仍走只读 SQL 排障。加一个 `GET /outbox/dead-letters` 需要新的只读动作与席位登记，不在 D6-9 判据内——但不能因此假装「可查」已由平台提供。
- **`FAILED` 枚举不可达**：见 8.2 第 1 条。若将来出现多步重放（例如先投递到暂存区再确认），这条路径才会活；届时必须同时补「失败也要留痕」的用例，而不是让枚举继续空转。
