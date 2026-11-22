# R16 Phase 2 契约变更申请包（已签发）

> 状态：`APPROVED` —— Owner 于 2026-11-23 以指令「按申请包签发」签署本包（§1 / §2 / §3 全部生效）。
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
