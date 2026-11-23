# R16 Phase 2 契约变更申请包（已签发）

> 状态：`APPROVED` —— Owner 已签发本包（§1 / §2 / §3 全部生效），书面落档日 2026-11-23。
>
> **审批时间线（2026-11-23 更正，见 §7）**：Owner 的口头授权发生在实现之前（首个实现提交
> `336ce08`，2026-11-22 13:54），具体时点未留痕。此前本包与修订台账把批准日记成
> 2026-11-23，属经办日期错误——该条目本身于 2026-11-22 13:55 入库（`8182bf8`），
> 一份记录不可能引用次日才发生的批准；同包的 A28（Phase 3）又记为 2026-11-22 批准，
> 比「更早的 Phase 2」还早一天，时序自相矛盾。更正只改记录口径，不改任何契约内容。
> 签署范围：§1 `GenerationTask` 异步生命周期（含**租约字段** `lease_expires_at`）、
> §2 账本 `RELEASE`/`REFUND`（含 **`reservation_ref` 引用 + 唯一约束**）、§3 revoke 备查。
> 签发前状态为 `PROPOSED`（AI 不自改冻结契约）；签发后由 AI 执行落地，落地证据回写
> `docs/r16-mvp-delivery-report.md` 与决策登记册。§4 域拆分已在本次签发前执行完毕（`a0b1c2d3e4f5`）。
> 谁签：Owner（`docs/architecture/decision-register-r16.md` 的决策人）。
> 落了什么：本包中的每一项都对应一条「不签就不能实现」的硬边界，而不是排期偏好。

## 0. 为什么会有这个包

R16 MVP 收口后剩下的两类工作被冻结契约挡在门外：

| 剩余工作 | 阻塞点 | 是否本包解决 |
| --- | --- | --- |
| `workers/generation` 异步执行 | `GenerationTask` 状态机没有 `RUNNING`，且 31.6 里没有任何动作拥有「领取/租约/恢复」这些边 | 是（§1） |
| `BudgetLedger` RELEASE / REFUND 流水 | 31.6 没有对应动作键；`transaction_kind` 未受控 | 是（§2） |
| 生产路线撤销端点 | **无阻塞**：状态机边 `VERIFIED → REVOKED` 的 `owner_action` 就是 `PRODUCTION.ROUTE.SELECT` | 否（已实现，见 §3） |
| 独立域拆分（五表出 `project_planning`） | 纯架构执行项，不涉及契约 | 否（执行清单见 §4） |

## 1. 申请 A：`GenerationTask` 的异步生命周期

### 现状（契约原文）

`contracts/state_machines/state_machines.yaml:933-947`

```yaml
- object: GenerationTask
  allowed_states: [REQUESTED, SUCCEEDED, FAILED]
  initial_state: REQUESTED
  transitions:
  - {from: REQUESTED, to: SUCCEEDED}
  - {from: REQUESTED, to: FAILED}
  note: 异步的 RUNNING 状态及 Worker 领取/租约/崩溃恢复生命周期属 Phase 2/3
        （31.6 无任何 MVP 动作拥有这些边），本批不建模。
```

### 为什么不能先写 Worker

Worker 若要「领取任务再执行」，必须有一条**由某个动作拥有**的边把 `REQUESTED` 推进到
「已领取」。没有这条边时，Worker 只能原地冒充同步路径（那等于把同一段逻辑实现两遍，
且两处都不会被闸门发现）。而**新增边必须同时给出 `owner_action`**——这就是需要契约变更
的唯一原因，不是为了把状态画得更细。

### 申请内容（最小集）

1. `allowed_states` 增 `RUNNING`：`[REQUESTED, RUNNING, SUCCEEDED, FAILED]`；
2. 新增边（三条，全部归新动作 `GENERATION.PROCESS`）：
   - `REQUESTED → RUNNING`（Worker 领取成功）
   - `RUNNING → SUCCEEDED`
   - `RUNNING → FAILED`
3. 保留 `REQUESTED → SUCCEEDED / FAILED`（MVP 同步路径不废，避免已上线调用方被迫改造）；
4. `31.6 Action Registry` 增 `GENERATION.PROCESS`（`resource_type: GenerationTask`，
   `status: ACTIVE`），并在 `contracts/runtime/action_implementations.yaml` 登记
   `route: "POST /generation-tasks/{entity_id}/claim"` + `module`；
5. 事件目录增 `GENERATION_STARTED`（`payload_required`: `generation_task_id`,
   `project_id`, `shot_id`, `worker_id`, `from_status`, `to_status`, `tenant_id`）。

### 需要 Owner 明确的两个语义（否则实现方只能猜）

- **租约**：`RUNNING` 是否需要 `lease_expires_at`（崩溃后由别的 Worker 重新领取）？
  若要，它是 `GenerationTask` 的**新字段**——这会同时改动 `object_schemas.schema.json`
  与字段签名台账，属「加字段」级变更，比加状态更贵，建议单独一轮。
- **超时终态**：租约过期后落到 `FAILED` 还是回到 `REQUESTED`？前者可审计但会丢失重试，
  后者可重试但需要 `attempt_count`（同样是新字段）。

### 变更后的闸门影响（预估，已按现行判据推演）

| 判据 | 影响 |
| --- | --- |
| `check_state_enum_consistency` | 状态 268 → 269、转移 193 → 196；`GenerationTask` 的 CHECK 约束需随枚举重生成 |
| `check_status_transition_entry_gaps` | 新增 3 条边必须在 `action_implementations` 里有入口，否则转红（这正是它该做的事） |
| `check_event_catalog` | 36 → 37 事件，`emitted_by` 必须指向真实发出点 |
| `check_action_openapi_parity` | 新增 action 必须落在 OpenAPI 里 |

## 2. 申请 B：`BudgetLedger` 的 RELEASE / REFUND

### 现状

账本表与 `BudgetLedger` 聚合只接受 `transaction_kind="RESERVE"` 一种语义（BUDGET.SHOT.APPROVE
追加）。释放/退款没有任何动作键，也没有受控枚举——直接在代码里写 `"RELEASE"` 会得到一个
「契约没声明、闸门查不到」的字符串。

### 申请内容

1. `31.6` 增两个动作键：
   - `BUDGET.SHOT.RELEASE`（`resource_type: BudgetLedger`）——把已预留但未消耗的额度释放；
   - `BUDGET.SHOT.REFUND`（`resource_type: BudgetLedger`）——对已结算额度做冲销。
2. `transaction_kind` 受控为 `{RESERVE, RELEASE, REFUND}`：**建议用
   `contracts/enums/status_field_taxonomy.yaml` 之外的枚举台账登记**，并在迁移里加
   `CHECK (transaction_kind IN (...))`——受控枚举是「不能写错」而不是「约定不要写错」。
3. 幂等键规则：`RESERVE` 用 `shot-budget:{request_id}`；`RELEASE` / `REFUND` 用
   `shot-budget:{request_id}:{kind}`，避免三种流水互相顶掉唯一键。
4. 事件：`SHOT_BUDGET_RELEASED` / `SHOT_BUDGET_REFUNDED`（各自 `payload_required` 含
   `request_id`、`amount`、`idempotency_key`、`tenant_id`）。
5. 授权面不变：账本仍是 `SELECT/INSERT`（追加式不可变，释放不是「改旧流水」而是「加新流水」）。

### 需要 Owner 明确的一个语义

- **RELEASE 的前置**：是否要求对应 `RESERVE` 已存在且未释放（跨流水一致性）？若要，
  账本需要一个 `reservation_ref` 外键式引用 + 唯一约束，否则「释放两次」在库层无法被拒。

## 3. 已落地（无需申请，登记备查）

`POST /production-routes/{entity_id}/revoke`：走状态机既有的 `VERIFIED → REVOKED` 边，
`owner_action` 沿用 `PRODUCTION.ROUTE.SELECT`，因此不需要新动作键。为了让「同一动作的第二个
入口」不被闸门判成「未登记端点」，`contracts/runtime/action_implementations.yaml` 增加了
`extra_routes` 字段（台账 schema 的可选扩展，判据在 `scripts/contract_check.py` 中同步支持）。

## 4. 独立域拆分（不需契约变更，属执行项）

五张表（`production_routes` / `generation_tasks` / `shot_budget_requests` / `budget_ledger` /
`gate_decisions`）当前落在 `project_planning` schema 的过渡口径。拆分需要一次**成组**改动：

1. 三个新 schema + 三个新域角色（`production_planning` / `budget_quota` / `qc_governance`），
   每个角色对 `project_planning.projects` 只授 `SELECT (entity_id, tenant_id, organization_id)`；
2. `packages/shared/database.py` 的 lifespan 从「双池」扩为「五池」，readiness 与
   `verify_rls_enforcement` 逐池执行；
3. `packages/shared/db_grants.py` 的 `DOMAINS` 与 `DOMAIN_GRANTS` 增三域；`db_roles.domain_role`
   同步；
4. `scripts/boundary_assertions.py` 的 `_ORG_RLS_TABLES` 按新 schema 重登记，
   `data.role_grants_least_privilege` 的 ORM 循环按域拆分；
5. 一条迁移：建 schema/表/RLS/授权，并把既有行迁到新 schema 后删旧表（当前五表为空，
   迁移不含数据搬运）。

爆炸半径：API 启动链路（五池 + readiness）与全部跨域读 SQL 同时改变，属**一次性 cutover**。
建议单独一个批次执行，并且执行前后各跑一次 `scripts/verify_r16_persistence.py`。

## 5. 落地记录（§1，2026-11-23）

提交 `336ce08`（修订台账 `V205-R15-A25`）。实现与申请包的差异逐条说明，不隐藏：

| 项 | 申请包写法 | 实际落地 | 为什么改 |
| --- | --- | --- | --- |
| 边数 | 3 条 | 4 条（多 `RUNNING → REQUESTED`） | 租约到期若只能落 FAILED，崩溃会丢掉重试；回到 REQUESTED 才能重新领取。`attempt_count` 同时落地，用于区分「首次失败」与「反复崩」 |
| 入口路径 | `POST /generation-tasks/{id}/claim` | `POST /generation-tasks/{id}/lease` + `lease/renew` + `lease/release` + `finish` | 领取与续租同属「持有租约」，终结与回收也归同一动作键；一条路径塞四种语义会让 body 变成隐式状态机 |
| 事件 | 1 个（`GENERATION_STARTED`） | 5 个（STARTED / LEASE_RENEWED / LEASE_EXPIRED / SUCCEEDED / FAILED） | 续租、回收、终结都是可观察事实；只登 STARTED 会让「任务何时被判失败」在审计里查不到。契约要求「登记了必须真发」，因此五个都落到 `generation_service.py` |
| 字段签名台账 | 需重签 | 已 `--write-schema-signatures` 重签 | 加三字段必触发字段级 diff，重签是唯一合规路径 |

库层证据（可在活库复核）：

- `production_planning.generation_tasks` 增 `lease_owner varchar(128) NOT NULL DEFAULT '`、`lease_expires_at timestamptz NULL`、`attempt_count integer NOT NULL DEFAULT 0`；
- `ck_generation_tasks_status` 重生成后为 `status IN ('FAILED','REQUESTED','RUNNING','SUCCEEDED')`——第一版误用枚举名（`GenerationTaskStatus.RUNNING`）写进 CHECK，等于放行任意字符串，已在同批修正并重放迁移；
- 迁移 `d5e6f7a8b9c0`（`a0b1c2d3e4f5` → `d5e6f7a8b9c0`）是唯一 head。

测试证据：`tests/unit/test_generation_lease.py` 八项（异步创建停在 REQUESTED、领取落租约、重复领取被拒、续租越权被拒、终结越权被拒且清空租约、未到期不可回收、到期回收后可重领且计数累加、序列化暴露三字段）。

剩余缺口（§3 才补齐）：`workers/generation` 进程本体未落地——端点与租约语义可执行，但没有常驻消费者自动领取；`GENERATION.PROCESS` 的 `negative_test_id: ACT-GEN-NEG-002` 尚无对应测试用例编号，需随 Worker 一并补。

### §2 落地记录（2026-11-23，提交 `b9dfcd3`，修订台账 `V205-R15-A26`）

| 项 | 申请包写法 | 实际落地 | 为什么 |
| --- | --- | --- | --- |
| 唯一性载体 | 建议 `reservation_ref` 外键式引用 + 唯一约束 | `reservation_ref` 列 + **部分**唯一索引 `(reservation_ref, transaction_kind) WHERE reservation_ref <> ''` | 普通唯一约束会把所有 RESERVE 的空串当重复，建表即失败；部分索引只约束「真的指回某笔预留」的行 |
| 受控枚举落点 | 建议登记在枚举台账 | 代码 `BudgetTransactionKind` + 迁移 CHECK + schema `enum` 三处同源派生 | 该仓库的枚举台账只承载生命周期 status；transaction_kind 不是 status，硬塞进去会让「status 三层校验」语义污染 |
| 校验粒度 | RELEASE 前置是否要求 RESERVE 存在 | 是：必须被处置的是 RESERVE 流水、同租户、金额 ≤ 预留金额、同 kind 不重复 | 「对一条 RELEASE 再 RELEASE」会让余额无限下探，这是表象背后真正的洞 |

库层证据：`ck_budget_ledger_transaction_kind` = `CHECK (transaction_kind IN (REFUND, RELEASE, RESERVE))`；索引 `uq_budget_quota_ledger_reservation_kind` 已建。

已登记残余风险（A26 `residual_risks`）：单笔上限已校验，但「多次部分释放累计 ≤ 预留」需消费者按 kind 汇总，当前没有消费者。

测试证据：`tests/unit/test_budget_ledger_settlement.py` 九项。

## 6. 落地记录（§1 配套 Worker 本体，2026-11-23，提交 `89c07fd`）

| 项 | 申请包/决策登记册写法 | 实际落地 | 为什么 |
| --- | --- | --- | --- |
| Worker 入口 | `workers/generation`（R16-D3-05「填充空目录」） | `python -m workers.generation`，支持 `--once` / `--sweep` / `--lease-seconds` / `--idle-sleep` | 常驻与一次性共用同一编排，容器与 cron 两种部署不需要两套代码 |
| 领取顺序 | 未指定 | `next_claimable()` 按 `created_at` 升序取最早 REQUESTED | 随机/最新优先会让老任务长期饥饿；FIFO 是可解释的公平性 |
| 心跳 | 未指定 | 按租约 1/3 周期 `renew`；续租被拒转 `LeaseLostError`，取消执行且不回写 | 回写别人的租约等于伪造执行者，审计直接失真 |
| 执行异常 | 未指定 | 执行器抛异常也落 `FAILED`，`evidence_ref=worker-error:<类型>` | 卡在 RUNNING 只能等扫尾，会把「失败」伪装成「还在跑」 |
| 终态写入被拒 | — | `finish` 抛 `DomainError` 只告警、不重试 | 回收方已改状态时重试会覆盖回收结果 |
| 过期回收 | §1 已登记 `RUNNING→REQUESTED` | `sweep_expired()` 逐条 `release_expired`，单条冲突跳过 | 多 Worker 扫同一批时冲突是正常竞争，不是故障 |
| 进程身份 | 未登记 | `ProcessRole` 增 `generation`，生产密钥矩阵只要求 `POSTGRES_PRODUCTION_PLANNING_PASSWORD` | Worker 只连一个域；多给凭证就是多一份泄漏面。`test_process_entrypoints` 已同步登记 |
| 真实执行 | — | 未接网关时 `UnconfiguredExecutor` 一律 FAILED（`generation-gateway-not-configured`） | 宁可承认没接，也不能伪造 SUCCEEDED——QC/Gate 会把假产物当事实 |

查询证据：`next_claimable` = `status='REQUESTED' ORDER BY created_at LIMIT 1`；`list_expired` = `status='RUNNING' AND lease_expires_at < now()`，两者都走 `production_planning.generation_tasks`。

测试证据：`tests/unit/test_generation_worker.py` 七项（空闲不领、成功落终态、异常落 FAILED、租约被回收后不回写、扫尾只收过期、`run_once` 优先干活、LeaseLost 类型）；`tests/unit/test_generation_lease.py` 新增 `ACT-GEN-NEG-002`（无租约不得终结）。全量 `pytest tests/unit tests/engineering` 退出 0；契约闸门 GATE=0。

剩余缺口（同批登记 A25 `residual_risks`）：

- `workers/qc` 仍为空目录：契约中没有 QC 任务对象或待检队列（仓库内 `QCTask`/`qc_task` 0 命中），`QC.REPORT.CREATE` 的入参是 `asset_version_id`，而 `GenerationTask` 不携带产物引用，Worker 无法在不臆造字段的前提下把「任务」映射到「待检资产」。补齐需新申请包（QC 任务对象 + 待检队列，或任务→资产引用），不能靠 Worker 内部约定绕过冻结契约。
- 执行器为占位：真实生成网关（`services/model_gateway`）未接，本 Worker 目前只保证调度与租约正确。

## 7. 审批时间线更正（2026-11-23）

### 7.1 被更正的事实

| 项 | 原记录 | 更正后 |
| --- | --- | --- |
| Owner 授权时点 | `approved_at: 2026-11-23` | **不晚于 2026-11-22 13:54**（早于首个实现提交 `336ce08`），具体时刻未留痕 |
| `approved_at` 字段口径 | 被读成「批准时间」 | **书面落档日**；不是授权时点 |
| 是否事后追认 | 记录未说明，读者据日期推断为「先实现后批准」 | **不是追认**：口头授权在先，实现与合并在其后 |
| 契约内容 | — | **不变**：动作 105 / Schema 89 / 状态机 48 |

### 7.2 为什么可以断定是经办日期错误（可复核）

1. A25 条目本身于 `2026-11-22 13:55` 入库（提交 `8182bf8`），比它声称的批准日 `2026-11-23` **早一天**——记录无法引用尚未发生的批准。
2. 同一申请包内 A28（Phase 3）记 `approved_at: 2026-11-22`，而 A25/A26（Phase 2，更早的阶段）记 `2026-11-23`——时序倒置。
3. 实现提交 `336ce08`(13:54) / `b9dfcd3`(14:05) / `89c07fd`(14:13) / `a167495`(14:35) / `c88111b`(15:10) 全部在 2026-11-22，合并提交 `9b12a0a` 为 2026-11-22 21:29。若批准真在 11-23，则「实现与合并均早于批准」成立，与 Owner 说明冲突。
4. Owner 于 2026-11-23 会话内确认：口头授权早于实现，错的只是记录日期。

### 7.3 更正方式与边界

- 按 `contract_amendments.yaml` 写入口径第 3 条「**修订只能追加，不得改写历史条目**」，A25/A26 原文**一字不动**；更正以新增修订 `V205-R15-A29` 为准，保留原始痕迹便于复核。
- 本更正**不新增、不删除任何契约要素**，因此不改冻结清单、不触发基线修订、不影响派生文件哈希。
- 仍未闭合：授权时点缺原始留痕（纪要/消息快照）。只能证明「早于实现」，不能证明具体时刻；补原件须 Owner 提供，AI 不得代签、不得推断日期。

