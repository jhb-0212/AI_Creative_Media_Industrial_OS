# R16 MVP 交付报告（生产链 + 预算 + 网关闭环）

> 日期：2026-11-22　基线：V205-R15（冻结清单 75/76/35）
> 范围：落实 D3 决策，把六个 MVP Action 与四个新对象建成可运行、可校验的最小闭环。
> 状态：**契约闸门全绿、非 DB 单元测试全绿**；DB 持久化与后台 Worker 明确列为后续 PR。

## 1. 一句话结论

生产链（路线选择 → 生成请求 → Gate 决策）与预算（单镜预算申请 → 审批 → 账本追加）两条主链
已打通，六个动作全部 `implemented`，四个新对象完成 schema / 状态机 / 枚举 / 事件 / 服务 / 路由接入，
`python scripts/contract_check.py` 通过（exit 0）。当前持久层为进程内内存仓储，SQLAlchemy/迁移与
Worker 为下一阶段。

## 2. 已交付（可验收）

### 2.1 六个 MVP Action（全部 implemented）

| Action | 路由 | 归属服务 |
| --- | --- | --- |
| `CONSTRAINT.PACK.STATUS.CHANGE` | `POST /constraint-packs/{entity_id}/status` | project_planning |
| `PRODUCTION.ROUTE.SELECT` | `POST /production-routes` | production_planning |
| `GENERATION.REQUEST` | `POST /generation-tasks` | production_planning |
| `PRODUCTION.GATE.APPROVE` | `POST /gate-decisions` | qc_governance |
| `BUDGET.SHOT.REQUEST` | `POST /shot-budget-requests` | budget_quota |
| `BUDGET.SHOT.APPROVE` | `POST /shot-budget-requests/{entity_id}/approve` | budget_quota |

### 2.2 四个新对象

| 对象 | 状态机 | 说明 |
| --- | --- | --- |
| `ProductionRoute` | `VERIFIED → REVOKED`（仅 SELECT 入口） | 路线须 VERIFIED；MVP 只开选择入口 |
| `GenerationTask` | `REQUESTED → SUCCEEDED / FAILED` | MVP 同步执行，异步 RUNNING 归 Phase 2/3 |
| `ShotBudgetRequest` | `DRAFT → APPROVED / REJECTED` | 审批人须≠申请人；驳回须理由；APPROVED 同事务记账 |
| `BudgetLedger` | `NO_LIFECYCLE`（envelope_only / 不可变） | 追加 `RESERVE` 流水，幂等键 `shot-budget:{request_id}` |

`GateDecision` 保持既有 `envelope_only`；`PRODUCTION.GATE.APPROVE` 追加一条不可变决策记录。

### 2.3 契约数据联动（全部随 A24 修订）

- `object_schemas.schema.json`：84 → **88** 定义；覆盖率账本 `x-afs-p0-coverage` 同步 88/88。
- `state_machines.yaml`：`total_objects` 44 → **47**（+3 生命周期对象）。
- `status_field_taxonomy.yaml`：+3 lifecycle + BudgetLedger `envelope_only`。
- `resource_schema_gaps.yaml`：移除 4 项（保留 CanonRevision / RightsCase / RightsManifest）。
- `event_catalog.yaml`：+6 事件 → **35** 事件 / 35 发出点。
- `state_machine_gaps.yaml`：`transition_entry_gaps` 清空为 `{ConstraintPack: {}}`（无「可达但无入口」边）。
- `contract_amendments.yaml`：`V205-R15-A24`。

### 2.4 代码落点

- 领域实体：`services/production_planning/domain/{production_route,generation_task}.py`、
  `services/budget_quota/domain/{shot_budget_request,budget_ledger}.py`、
  `services/qc_governance/domain/gate_decision.py`。
- 应用服务：`.../service/{production_route_service,generation_service,budget_service,gate_service}.py`
  （统一走 `transition.py:advance_status` 与 outbox）。
- 路由：四文件，权限用 `require_role(action_key)`；挂入 `apps/api/main.py`。
- 共享：`packages/shared/in_memory.py`（`InMemoryRepository` 乐观锁按「最近落库版本」比对 +
  `InMemoryOutboxWriter`）。
- 闸门登记：`scripts/contract_check.py` 的 `_STATE_MACHINE_SOURCES`(+3)、
  `_RESOURCE_SERIALIZERS`(+4，含 `_serializer_factories` 探针)。

### 2.5 持久层（已在活库验证，路由已接线）

- **ORM**：`services/project_planning/repository/orm_r16.py`（`production_routes` /
  `generation_tasks` / `shot_budget_requests` / `budget_ledger` / `gate_decisions`，
  信封列与 C.3.1 一致）。
- **迁移**：两条，均已在本仓库活库（容器 `ai-film-postgres`，宿主 15432，库
  `ai_film_industrial`）执行，`alembic_version = f9a0b1c2d3e4`：
  - `e8f9a0b1c2d3_r16_production_budget_tables.py`（down_revision `d7e8f9a0b1c2`）：四张
    生产/预算表 + 索引 + RLS + 授权收敛；
  - `f9a0b1c2d3e4_gate_decisions_table.py`（down_revision `e8f9a0b1c2d3`）：Gate 决策表
    + 索引 + RLS + 授权收敛。五表 RLS 均 FORCE，ACL 符合矩阵（账本与决策表仅
    `SELECT/INSERT`，其余三表另加 `UPDATE`、无 `DELETE`）。
- **仓储**：`services/production_planning/repository/sqlalchemy_repos.py`、
  `services/budget_quota/repository/sqlalchemy_repos.py`（乐观锁条件更新；`add` 后 flush）、
  `services/qc_governance/repository/sqlalchemy_repo.py`（追加式，只有 add/get）。
- **路由接线**：四个新端点不再使用模块级内存单例——
  `production_routes` / `generation_routes` 创建类动作经 `authorize_on_parent(Project)`
  做归属判定；`budget_routes` 的审批走 `require_resource_role(resource_type=
  "ShotBudgetRequest")`（资源级判定，不信请求体自称的项目）；`gate_routes` 同创建类口径。
  每个写端点都在返回前 `await uow.commit()`，业务写与 outbox 事件同事务。
  另新增 `POST /production-routes/{entity_id}/revoke`（治理边 VERIFIED→REVOKED，沿用
  `PRODUCTION.ROUTE.SELECT` 这条准入凭据，台账以 `extra_routes` 登记同一动作的第二入口，
  判据在 `scripts/contract_check.py` 同步支持），撤销依据 `revocation_ref` 必填。
  `packages/identity/ownership.py` 相应登记了 `ProductionRoute` / `GenerationTask` /
  `ShotBudgetRequest` / `GateDecision` 四条归属解析（归属一跳到底）。
- **授权矩阵**：`packages/shared/db_grants.py` 增 5 条（账本与 Gate 决策仅 SELECT/INSERT）。
- **边界断言**：`scripts/boundary_assertions.py` 登记 4 张项目作用域表 + 账本「租户兜底」形态，
  并把 `orm_r16` 纳入 ORM 元数据覆盖判据。
- **端到端脚本**：`scripts/verify_r16_persistence.py`（服务→仓储→落库→回读，五表
  PASS，Gate 决策 JSONB 字段往返无损，验证后零残留）。
- **装配守卫**：`tests/unit/test_r16_route_wiring.py` 钉住「路由不得退回内存仓储」
  （行为由端到端脚本证明，形状由本用例证明）。

> 过渡口径：五张表暂落 `project_planning` schema（同角色、同 RLS），独立域拆分见决策登记册。

### 2.6 五域 cutover（`a0b1c2d3e4f5`，已完成）

一次 cutover，不保留新旧两套表（双份事实源比缺权限更难发现）：

| 原表（`project_planning`） | 新 schema | 角色 | 表级权限 |
| --- | --- | --- | --- |
| `production_routes` / `generation_tasks` | `production_planning` | `production_planning_role` | SELECT/INSERT/UPDATE |
| `shot_budget_requests` | `budget_quota` | `budget_quota_role` | SELECT/INSERT/UPDATE |
| `budget_ledger` | `budget_quota` | `budget_quota_role` | **SELECT/INSERT**（追加式不可变） |
| `gate_decisions` | `qc_governance` | `qc_governance_role` | **SELECT/INSERT**（决策不可改写） |

配套改动（全部同一次提交，缺一项就是「矩阵声明了不存在的表」）：

- `db_grants`：`DOMAINS` 五域；三域各自声明本域表 + 平台共享表（audit/outbox/auth/alembic）
  + `project_planning.projects` 的**列级**只读（`entity_id/organization_id/tenant_id`）；
  `converge_statements` 新增生成 schema USAGE（否则「只授表不授 USAGE」会在首次查询
  报 `permission denied for schema`）；
- `database.py`：5 个 DSN 构造器 / 5 引擎 / lifespan 五池 / 逐池 `verify_rls_enforcement`；
- `identity_access/deps.py`：三域各写/只读两个 UoW 依赖；四路由改为注入各自域；
- ORM：`production_planning/repository/orm.py`、`budget_quota/repository/orm.py`、
  `qc_governance/repository/orm.py`，`orm_r16` **删除**（同表两份模型会让 metadata 冲突）；
- 边界断言：`_ORG_RLS_TABLES` 改为新 schema 的相关性字符串、ORM 覆盖判据按域取模块、
  `database.py` 必须逐池验证 RLS（调用点数 ≥ 2，现为 5）；
- 迁移 `downgrade`：重建旧表 + 经 `db_grants.legacy_r16_grant_statements` 恢复旧授权；
  数据不回填（结构性回滚，写明比假装能回滚更安全）。

## 3. 验证结果（诚实分级）

| 项 | 结论 | 证据 |
| --- | --- | --- |
| 契约一致性闸门 | **PASS** | `python scripts/contract_check.py` → 「契约一致性校验通过」（exit 0） |
| R16 新单测（12 条） | **PASS** | `tests/unit/test_production_budget_gate_mvp.py` 全绿 |
| 全量单元测试（除 DB 依赖文件） | **PASS** | 排除 3 个 Postgres 文件后 exit 0，1 项按设计跳过 |
| 覆盖率账本用例（3 条） | **PASS** | shot / breakdown / revision 三套 `coverage_ledger` 用例通过 |
| 转移入口债务用例（8 条） | **PASS** | 含本轮回写后自足化的 2 条 |
| 持久层端到端（活库 15432） | **PASS** | `scripts/verify_r16_persistence.py`：五表写入/回读一致，验证后零残留 |
| DB 集成套件（`project_api`/`resource_scope_api`/`unit_of_work`） | **PASS** | 以活库 DSN（15432）运行，26 条全绿 |
| 路由装配守卫（`test_r16_route_wiring.py`） | **PASS** | 断言四个新端点装配的是 SQLAlchemy 仓储，模块内无内存单例 |
| GitHub 远端 / CI | **NOT RUN** | 用户明令禁止访问远端；本机无 `.git` |
| 容器内端点暴露（重建镜像后） | **PASS** | `ai-film-api:local` 重建并重启，`/openapi.json` 含 6 个新端点（含 revoke），容器 healthy |
| **五域 schema/角色 cutover（`a0b1c2d3e4f5`）** | **PASS** | 五表迁入 `production_planning`/`budget_quota`/`qc_governance` 三 schema，三角色新建；`pg_class.relacl` 实测 `budget_ledger` 与 `gate_decisions` 仅 `ar`，其余 `arw` |
| **授权面 == 矩阵（DATA-6 / SCOPE-4）** | **PASS** | 闸门两项均 PASS：迁移零 GRANT 字面量，schema USAGE 也由 `converge_statements` 生成；启动期逐池 `verify_rls_enforcement`（5 处调用点） |
| **五池启动与就绪** | **PASS** | `lifespan` 建 5 引擎 + 5 会话工厂，`probe_domain_databases` 逐池探活；容器 healthy，`/openapi.json` 6 端点在线 |
| **拆分后持久层端到端** | **PASS** | `scripts/verify_r16_persistence.py` 在新 schema 上五表写入/回读一致 |

> 为配合闸门全绿，本轮**调整了 2 条允许修改的测试**（非冻结契约）：R16 实现了
> `CONSTRAINT.PACK.STATUS.CHANGE` 后真实转移缺口清零，原「依赖真实缺口存在」的
> `detects_dropped_entry` / `detects_missing_file` 改为**自足构造**（monkeypatch
> `_implemented_action_keys` 人为摘掉一个 action），意图不变、仍能验证「无登记/无文件=红」。

## 4. 口径与偏离（供 Owner 复核）

- 生产域统一归 `services/production_planning`；预算域 `services/budget_quota`；QC/Gate 归
  `services/qc_governance`（与 R16-D3-01/02/04 一致）。
- 三个新生命周期对象采「最小闭合、全部边由 MVP Action 拥有」，避免「可达但无入口」债务。
- GenerationTask 同步执行、ProductionRoute 不开放撤销、BudgetLedger envelope_only —— 见
  `docs/architecture/decision-register-r16.md` 的「实施落地结论」。

## 5. 未完成 / 后续 PR

1. ~~契约变更类~~ → **已签发并落地**（Owner 2026-11-23「按申请包签发」，见
   `docs/architecture/amendment-pack-r16-phase2.md` §1/§2/§5/§6）：`GenerationTask` 的
   `RUNNING`/租约/恢复边 + `GENERATION.PROCESS`（`336ce08` + `89c07fd`）；账本
   `BUDGET.SHOT.RELEASE` / `BUDGET.SHOT.REFUND` 与 `transaction_kind` 受控枚举（`b9dfcd3`）。
2. ~~独立域拆分~~ → **已完成**（`a0b1c2d3e4f5`，见 §2.6）：五表出 `project_planning`，
   各自 schema + 独立角色 + 独立连接池，一次性 cutover，不保留双份表。
3. `workers/generation` **已落地**（`89c07fd`：常驻领取 / 心跳续租 / 终态回写 / 过期回收），
   但执行器为占位（`UnconfiguredExecutor` 一律 FAILED）。`workers/qc` 仍为空目录：缺 QC
   任务对象与待检队列（需新申请包），详见 §6 落地记录与 A25 `residual_risks`。
4. `A24.source_commits` 为 `LOCAL-NO-GIT-PLACEHOLDER`，合入团队仓库时替换真实 SHA。
5. API 镜像已重建并重启（新镜像含本轮全部端点，`/openapi.json` 已核对）；容器重建时需要
   `IDENTITY_PUBLIC_KEY_PEM`（本次从身份进程读取同一把公钥注入，属于宿主环境事实，
   建议写入 compose `.env` 以免下次重建再次缺失）。

## 6. Owner 待办

- Phase 0「条件通过 → 无条件通过」正式签署（AI 不得代签）。
- 复核本册全部 D2/D3 决策与「实施落地结论」。
