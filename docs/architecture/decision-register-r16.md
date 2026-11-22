# R16 决策登记册（架构裁决，待 Owner 复核）

> 依据用户授权「架构设计全部按最优最好来，做完全部内容后再检查」，本册登记本轮
> 由执行方作出的所有 D1–D3 决策。每项均标 owner_supplied_design / PENDING 复核，
> Owner 复核后打勾生效或驳回重裁。

## D3 决策（架构红线，须 ADR 化）

| 编号 | 决策 | 理由 | 影响 |
| --- | --- | --- | --- |
| R16-D3-01 | 生产域建模：ProductionRoute（路线库）、GenerationTask（生成任务+DAG 依赖）、GateDecision（Gate 决策）由新建服务 `services/production_planning` 所有 | 契约动作 PRODUCTION.ROUTE.SELECT/GENERATION.REQUEST/PRODUCTION.GATE.APPROVE 的 resource_type 归属一处 | 新增服务与 schema |
| R16-D3-02 | 预算域：ShotBudgetRequest + BudgetLedger 由 `services/budget_quota` 所有（现有空目录填充） | 契约 BUDGET.SHOT.* 与 BudgetLedgerTransaction 所有权 | 填充空目录 |
| R16-D3-03 | 单一路线选择 = 图像生成（文生图）；图生视频/音频/3D 延后 | PR-19「一条稳定生成路线」+ 本机无视频模型可验证 | 只实现 image adapter |
| R16-D3-04 | QC 任务与人工审核记录由 `services/qc_governance` 所有；QCReport 仍归 asset_service（不迁移） | 不引入第二套 QC 报告事实源 | 填充空目录 |
| R16-D3-05 | 生成 Worker = `workers/generation`；QC Worker = `workers/qc`（填充空目录） | PR-20/22 | 填充空目录 |
| R16-D3-06 | 知识沉淀 = `services/memory_learning`（内部能力，不进 Action Registry） | PR-26「知识不得成为权威业务事实」 | 填充空目录 |

## D2 决策（跨域设计）

| 编号 | 决策 | 理由 |
| --- | --- | --- |
| R16-D2-01 | 跨域调用方向：生产域读 asset/project 经应用服务或只读 UoW，不直写他域表 | 数据所有权唯一 |
| R16-D2-02 | model_gateway 为进程内库 + 适配器接口，不暴露 HTTP | 避免未登记端点；Worker 直连 |
| R16-D2-03 | 生成成本记录经 BudgetLedger（budget_service），不由 model_gateway 自记 | 账本唯一事实源 |

## 空目录裁决（PR-04 前置）

| 目录 | 裁决 | 理由 |
| --- | --- | --- |
| services/budget_quota | **填充** | R16-D3-02 |
| services/model_gateway | **填充** | PR-19 |
| services/qc_governance | **填充** | R16-D3-04 |
| services/memory_learning | **填充** | R16-D3-06 |
| services/production_policy | 合并入 production_planning（不单独建） | 策略属路线库职责 |
| services/orchestrator | 合并入 production_planning（DAG 调度归其所有） | 避免空壳 |
| services/blocking_service | 合并入 production_planning（任务阻塞语义） | 同上 |
| services/creative_exploration / rights_governance / observability | 超出 V1.0 MVP：删除空目录，CANON/RIGHTS action 维持 not_implemented 显式债务 | 铁律「不造空模块」 |
| adapters/image_models | **填充**（mock + http provider） | R16-D3-03 |
| adapters/{audio_models,blocking,comfyui,external_tool,video_models} | 删除（超 MVP，接口已由 image_models 示范） | 铁律 |
| agents/{evaluators,planners,runtime} | 删除（超 MVP） | 铁律 |
| workers/generation / workers/qc | **填充** | R16-D3-05 |
| workers/{delivery,notifications} | 删除（超 MVP） | 铁律 |

## 待 Owner 复核项（不阻断代码，但须复核）

1. Phase 0「条件通过→无条件通过」的 Owner 正式签署（决议原件缺失，17 处引用 0 原件）——已起草签署模板，AI 不得代签。
2. 本册全部 D2/D3 决策。
3. ADR-R40 版本收敛（见 PR-01 §5）。

## R16-D3 实施落地结论（执行方落地记录，待 Owner 复核）

> 按上表 D2/D3 决策实际落地的结果与刻意偏离。契约闸门 `contract_check.py` 全绿；
> 六个 MVP 动作与四个新对象均已接入 schema / 状态机 / 枚举 / 事件 / 运行时台账。

### 已落地

- 六个 MVP 动作实现并登记 `implemented`（`contracts/runtime/action_implementations.yaml`）：
  - `CONSTRAINT.PACK.STATUS.CHANGE` → `POST /constraint-packs/{entity_id}/status`
  - `PRODUCTION.ROUTE.SELECT` → `POST /production-routes`
  - `GENERATION.REQUEST` → `POST /generation-tasks`
  - `PRODUCTION.GATE.APPROVE` → `POST /gate-decisions`
  - `BUDGET.SHOT.REQUEST` → `POST /shot-budget-requests`
  - `BUDGET.SHOT.APPROVE` → `POST /shot-budget-requests/{entity_id}/approve`
- 四个新对象 ProductionRoute / GenerationTask / ShotBudgetRequest / BudgetLedger：
  入 `object_schemas`（88 定义，覆盖率账本同步 88）、`state_machines`（47 对象）、
  `status_field_taxonomy`、`contract_amendments`（`V205-R15-A24`）。
- 六个新事件入 `event_catalog`（共 35 事件 / 35 发出点）。
- 服务：`services/production_planning`（route/generation）、`services/budget_quota`、
  `services/qc_governance` 四个应用服务；四个路由挂入 `apps/api/main.py`。
- 新增 12 条单元测试全部通过（`tests/unit/test_production_budget_gate_mvp.py`）。
- 持久层：`orm_r16`（5 模型，含 Gate 决策）+ 两条 Alembic 迁移（`e8f9a0b1c2d3` 四表、
  `f9a0b1c2d3e4` Gate 决策表，均含 RLS 与授权收敛）+ 三个 SQLAlchemy 仓储；已在本仓库
  活库（15432）执行并通过端到端验证脚本（五表写入/回读一致，验证后零残留）。
- 路由接线：四个新端点改用 SQLAlchemy 仓储 + UoW，写端点返回前 `commit()`（业务写与
  outbox 事件同事务）；创建类动作经 `authorize_on_parent(Project)`，预算审批走资源级
  `require_resource_role(ShotBudgetRequest)`。归属解析登记 4 条（一跳到底）。
  装配形状由 `tests/unit/test_r16_route_wiring.py` 守卫。
- 撤销入口：`POST /production-routes/{entity_id}/revoke`（沿用 `PRODUCTION.ROUTE.SELECT`
  的边所有权，`revocation_ref` 必填）；台账用 `extra_routes` 表达「同一动作的第二入口」。
- 运行态：`ai-film-api:local` 镜像已按本轮代码重建并重启，`/openapi.json` 已核对 6 个端点。

### 刻意偏离（MVP 最小闭合，非缺陷）

1. **GenerationTask 仅同步执行**：`REQUESTED→SUCCEEDED/FAILED` 由 `GENERATION.REQUEST`
   在一次调用内走完（`result` 由请求入参决定）。异步 `RUNNING` 与 Worker 生命周期归 Phase 2/3。
2. **ProductionRoute 最小状态** `[VERIFIED, REVOKED]`，仅暴露「选择」入口；`VERIFIED→REVOKED`
   边已在状态机声明，但当前不开放撤销端点（避免「可达但无入口」债务）。
3. **BudgetLedger 为 envelope_only / 不可变记录**（`NO_LIFECYCLE`）；审批通过时同事务追加一条
   `RESERVE` 流水（幂等键 `shot-budget:{request_id}`），无独立状态机。
4. **四个服务当前用进程内内存仓储 + 内存 outbox**（`packages/shared/in_memory.py`）；
   SQLAlchemy ORM 与 Alembic 迁移待接入。

### 未完成（后续 PR，非本 MVP 范围）

- ~~**独立域拆分**~~ → **已完成**（迁移 `a0b1c2d3e4f5`）：五表已出 `project_planning`，
  迁入 `production_planning`（routes/tasks）、`budget_quota`（requests/ledger）、
  `qc_governance`（decisions），各配独立角色与独立连接池（lifespan 五池 + 逐池 RLS 验证
  + readiness 五池探活）；账本与 Gate 决策的表级 ACL 实测为 `ar`（只有 SELECT/INSERT）。
- **契约变更类（待 Owner 决策）**：`docs/architecture/amendment-pack-r16-phase2.md`——
  `GenerationTask` 的 `RUNNING`/租约/恢复边 + `GENERATION.PROCESS` 动作键；账本
  `RELEASE`/`REFUND` 的动作键与 `transaction_kind` 受控枚举。冻结契约不由 AI 修改。
- `workers/generation`、`workers/qc` 后台 Worker：依赖上一条契约变更，顺序不可颠倒。
- `V205-R15-A24` 的 `source_commits` 为本地占位 `LOCAL-NO-GIT-PLACEHOLDER`（本地无 `.git`），
  合入团队仓库时须替换真实 SHA。
