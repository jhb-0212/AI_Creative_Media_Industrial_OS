# R16 Phase 3 契约变更申请包：QC 待检队列（已签发）

> 状态：`APPROVED` —— Owner 于 2026-11-22 对「`workers/qc` 不能凭空造」的选项明确选择**A 方案**
> （新增 QC 任务对象 + 待检队列），即本包 §1 的授权范围。签发前状态为 `PROPOSED`，
> AI 不自改冻结契约；签发后由 AI 执行落地，落地证据见 §5。
> 谁签：Owner（`docs/architecture/decision-register-r16.md` 的决策人）。
> 本包只解决一件事：**让 QC 有「待检的东西」**，而不是让 Worker 靠内部约定去猜。

## 0. 为什么会有这个包

Phase 2 §1 落地 `workers/generation` 时留下了这条缺口（A25 `residual_risks` 已登记）：

- 契约里 `QC.REPORT.CREATE` 的入参是 `asset_version_id`，它回答的是「怎么登记一份报告」；
- `GenerationTask` 不携带产物引用，也不回答「哪一份产物还没被检」；
- 因此 `workers/qc` 若强行实现，只能自己做一张「任务→资产」的口头映射——那就是在冻结契约之外造事实。

三种可选路径与取舍（Owner 已选 A）：

| 方案 | 做法 | 代价 | 结论 |
| --- | --- | --- | --- |
| A（本包） | 新增 `QCTask` 对象 + 待检队列，生成终态入队 → QC Worker 领取 → 写报告 → 回状态 | 新增 1 对象、1 状态机、2 动作、6 事件、1 迁移 | **采纳**：漏检会留下 `QUEUED` 的可见缺口 |
| B | 给 `GenerationTask` 加产物引用，QC 轮询扫描已成功任务 | 不新增对象，但「已检/未检」只能靠比对 `QCReport`，漏检没有记录 | 否：缺口不可审计 |
| C | QC 由人手动触发，不建队列 | 零契约成本 | 否：等于承认自动质检不做 |

## 1. 申请内容（最小集）

### 1.1 新对象 `QCTask`（`qc_governance` 域）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `qc_task_id` | string | 业务主键，缺省取 `entity_id` |
| `project_id` | string | 必填，RLS 与项目归属的依据 |
| `shot_id` | string | 可空（整项目级质检时无镜头） |
| `asset_version_id` | string | **必填**：被检对象，QC 不得凭空检 |
| `generation_task_id` | string | 必填且**唯一**：一笔生成只入队一次 |
| `quality_profile_version` | string | 必填：用哪版质量标准判 |
| `status` | enum | `QUEUED / RUNNING / PASSED / REJECTED` |
| `lease_owner` / `lease_expires_at` / `attempt_count` | — | 与 `GenerationTask` 同构的租约三字段 |
| `qc_report_id` | string | 判定时必填；空报告视为没有证据 |

### 1.2 状态机（四条边，全部归 `QC.TASK.PROCESS`）

```yaml
allowed_states: [QUEUED, RUNNING, PASSED, REJECTED]
initial_state: QUEUED
transitions:
- {from: QUEUED,  to: RUNNING}    # 领取
- {from: RUNNING, to: PASSED}     # 判定通过（必须带 report）
- {from: RUNNING, to: REJECTED}   # 判定不通过（必须带 report）
- {from: RUNNING, to: QUEUED}     # 租约过期回收
```

### 1.3 动作与事件

| 动作 | 路由 | 语义 |
| --- | --- | --- |
| `QC.TASK.ENQUEUE` | `POST /qc-tasks`（负向 `ACT-QC-NEG-002`） | 入队；同一 `generation_task_id` 重复入队返回 `IDEMPOTENCY_CONFLICT` |
| `QC.TASK.PROCESS` | `POST /qc-tasks/{entity_id}/lease` + `lease/renew`、`lease/release`、`finish` | 领取/续租/到期回收/判定共用一个准入凭据 |

事件 6 条：`QC_TASK_QUEUED / QC_TASK_STARTED / QC_TASK_LEASE_RENEWED / QC_TASK_PASSED / QC_TASK_REJECTED / QC_TASK_LEASE_EXPIRED`，
`emitted_by: services/qc_governance/service/qc_task_service.py`（只登记真实发出点）。

### 1.4 边界

- 表落在 `qc_governance.qc_tasks`，**不借** `production_planning` 的表跨域；
- 组织级 RLS 与 `projects` 对齐（已登记 `_ORG_RLS_TABLES`，否则 SCOPE-4 直接红）；
- 授权只给 `qc` 角色 `SELECT/INSERT/UPDATE`（由迁移 `converge_statements` 收敛）。

## 2. 闸门影响（预估 vs 实际）

| 判据 | 预估 | 实际 |
| --- | --- | --- |
| `schema_objects` | 88 → 89 | 89 ✅ |
| `state_machines` | 47 → 48 | 48 ✅ |
| `actions` / `openapi_operations` | 103 → 105 | 105 / 105 ✅ |
| `events` | 新增 6 条 | 49 ✅ |
| 状态/枚举一致性 | 需登记 `QCTaskStatus` | `--write-schema-signatures` + `gen_status_contracts.py --write` 后 GATE=0 |
| 动作↔OpenAPI | 2 条新路径 | operationId 一一对应，`extra_routes` 登记 3 条子路由 |

## 3. 与 Phase 2 残余缺口的衔接

A25 当时写的是「`workers/qc` 为空目录，补齐需新申请包」。本包即该申请包，缺口从
「无法实现」变为「已实现但检测器未接」：

- 生成侧：`GenerationTask` 进入 `SUCCEEDED` 且执行结果带 `asset_version_id` +
  `quality_profile_version` 时，`generation` Worker 以**独立事务**调用入队；
  缺任一引用则**跳过并告警**——不猜对象，也不把失败任务塞进待检队列。
- QC 侧：`workers/qc` 未配置检测器时 `configured=False`，**拒绝领取**，队列停在
  `QUEUED`。这是刻意的：宁可让队列显出「没人检」，也不产生占位 PASS 的假绿。

## 4. 库层证据（活库 `ai_film_industrial`，迁移 `f1a2b3c4d5e6`）

```
ck_qc_tasks_status              CHECK (status IN (PASSED, QUEUED, REJECTED, RUNNING))
uq_qc_governance_qc_tasks_generation_task  UNIQUE (generation_task_id)
ix_qc_governance_qc_tasks_status           btree (status)
tenant_isolation_qc_tasks       tenant_id = current_setting(app.current_tenant_id)
                                AND project_id 存在于同租户 projects
```

迁移链：`e6f7a8b9c0d1` → `f1a2b3c4d5e6_qc_tasks`（仓库根 `alembic.ini`，已 `upgrade head`）。

## 5. 落地记录（2026-11-22）

| 项 | 申请包写法 | 实际落地 | 为什么 |
| --- | --- | --- | --- |
| 一任务一队列项 | 建议唯一约束 | `generation_task_id` 唯一索引 | 重复入队会让同一产物被检两次，报告互相覆盖 |
| 重复入队语义 | 未指定 | 服务捕获唯一冲突 → `IDEMPOTENCY_CONFLICT`；生成侧视为「已入队」继续 | 生成 Worker 重试是正常行为，不应因已入队而失败 |
| 判定必须有报告 | 申请书写「写报告 → 回状态」 | 领域层强制 `qc_report_id` 非空，否则 `VALIDATION_ERROR` | 空报告等于没证据，PASSED 会变成无法追溯的绿灯 |
| 租约回收 | 与生成同构 | `RUNNING → QUEUED`，`attempt_count` 递增 | 崩溃可重试，且重试次数可见 |
| Worker 未配置检测器 | 未指定 | `QCExecutor.configured` 属性；为 False 时不领取 | 不造假的 PASS |
| 执行器异常 | 未指定 | 不写假 `REJECTED`，保持 `RUNNING` 等租约过期回收 | 写假 REJECTED 会把「人员/系统故障」记成「质量不合格」，污染质量数据 |
| 生成侧入队失败 | 未指定 | 只记日志，不回滚生成终态 | 生成成功是既成事实；回滚会让产物失去登记 |
| 进程身份 | 未登记 | `ProcessRole.qc` + `POSTGRES_QC_GOVERNANCE_PASSWORD` | QC 只连自己的域 |

## 6. 测试证据

- `tests/unit/test_qc_task_queue.py`（8 项）：入队、重复入队冲突、空产物拒绝、领取/重复领取、续租越权、无报告不得终结、REJECTED 事件、过期回收。
- `tests/unit/test_qc_worker.py`（5 项）：未配置不领取、PASSED 写报告、执行器异常不写假 REJECTED、扫尾只收过期、判定字面量。
- `tests/unit/test_generation_worker.py` 新增 3 项：成功带引用入队、失败不入队、缺引用跳过。
- 全量 `python -m pytest tests/unit tests/engineering -q` 退出 0（SKIP 1 项：`2192af9` 不可达的历史定义）；
  `python -m ruff check .` 0；`python scripts/contract_check.py` GATE=0。

## 7. 残余风险（登记在 A27 `residual_risks`）

1. **真实检测器未接**：`workers/qc` 的 `UnconfiguredInspector` 永远 `configured=False`，
   生产环境队列会持续停在 `QUEUED`。接检测能力（读资产 + 跑质量档案）是下一轮工作，
   不是本包能顺带解决的。
2. **质量档案版本只是字符串**：当前 `quality_profile_version` 不做存在性校验，
   指向一个不存在的档案也能入队。等质量档案有独立对象后再加外键式校验。
3. **生成侧入队是「尽力」语义**：入队事务失败只记日志，若进程在生成终态提交后、
   入队前崩溃，该产物不会出现在待检队列。补偿方式是后续加「成功产物对账扫描」，
   当前没有消费者。
