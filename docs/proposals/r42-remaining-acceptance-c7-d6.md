---
doc_type: proposal
status: in_progress
updated_at: 2026-11-24
authoritative_sources:
  - contracts/action_registry/action_registry.yaml
  - contracts/runtime/action_implementations.yaml
  - contracts/runtime/event_catalog.yaml
  - docs/audits/2026-11-24-r42-authorization-reconciliation.md
open_items:
  - item: >-
      C7-b 人工 QC 入队缺来源核验与专项授权（判据 C7-1..C7-6，实施未开始）：
      POST /qc/tasks 的契约（action_registry.yaml:1167-1176）已写死
      「仅在生成终态且携带产物引用时入队；同一生成任务不得重复入队」，实现两条都没做；
      且该 action 的 allowed_principals 是「生成 Worker/受信调度服务」，而自动路径
      根本不经 HTTP（workers/relay/handlers.py 直调 service.enqueue），REST 端点唯一
      真实使用者是人工——席位声明与真实调用主体互相矛盾，等于没有专项授权。
      债登记于 A36 residual_risks 第 1 条与审计记录 §4.1
    source: "contracts/phase0/contract_amendments.yaml#amendments"
  - item: >-
      D6-9 死信重放无任何入口（判据 D6-1..D6-8，实施未开始）：
      workers/relay/consumer.py:24,120,224 三处注释声称死信「可人工重放」，仓库内
      没有端点、脚本或 CLI。重放的写权限面已存在（outbox_relay_role 列级
      UPDATE(dead_lettered_at, next_attempt_at, attempts)），但该角色**无
      outbox_events INSERT**——审计事件不能由它写入，这是权限边界而非实现遗漏。
      债登记于 A36 residual_risks 第 2 条与审计记录 §4.2
    source: "contracts/phase0/contract_amendments.yaml#amendments"
---

# R42 剩余验收实施基线：C7-b 人工入队来源核验 + D6-9 受权死信重放

> 性质：**实施基线**（2026-11-24 工程审查建议的采纳登记），不是 Owner 签署，也不是 ADR 签署。
> 两批次**独立提交、独立验收**，互不夹带（不顺带改 P2 或其它冻结内容）。
> 报告口径：真实 PostgreSQL 实测与静态测试**分开报告**；未实际运行的标 `NOT RUN`，环境阻塞标 `BLOCKED`。
> 状态以文首 front-matter 为准（`status: in_progress`，两项均未开始）。

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

## 5. 需 Owner 裁决的三点（本基线不代决）

| # | 待裁 | 选项 | 建议 |
|---|---|---|---|
| D-1 | C7-2 的授权形态 | A 新增 `QC.TASK.RECOVER` / B 改 `QC.TASK.ENQUEUE` 席位 | **A**，理由见 2.2 |
| D-2 | D6 入口形态 | A 受权 REST 端点 / B 独立 CLI / C 两者（端点为主、CLI 薄封装） | **A**。因为 F8：台账只有 `implemented`（route 被真实校验）与 `not_implemented` 二元状态，CLI-only 会让「已用 CLI 实现」失真地登记成 `not_implemented`；而走端点可如实登记，权限也统一走 `authorize` 单入口 |
| D-3 | `dead_letter_replays` 是否登记为契约对象 | 登记 / 不登记 | **不登记**，按 `maintenance_runs` 先例（F7）：它是运维台账，不是业务聚合，进 `object_schemas` 会把它拖进状态机与序列化器的一致性面 |

---

## 6. 本基线不做什么

- 不改 ADR-R42 正文，不解除上游 `[V-BLOCKED]`（解除权在 Owner，见审计记录 §9.4）。
- 不为 relay 侧投递失败原因加 `last_error` 列（F10 的既有缺口，另案；D6-8 的「失败原因」只指重放动作自身）。
- 不加 `force` / `skip_validation` / 「直接投递」等旁路。
- 不新建第二套 QC 任务创建机制——保留的是**人工恢复能力**，不是并列的入队通道。
- 不合并两批次的契约修订登记。
