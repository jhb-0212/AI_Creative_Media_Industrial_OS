# 骨架建设实施清单（基于当前仓库实际代码）

> 依据：《AI Creative Media Industrial OS 全局工程骨架建设与分阶段功能开发设计方案》（下称《总体设计》）
> 盘点时点：2026-11-22，main @ b30d361（本地快照 = 远端 main）
> 生成方式：逐目录盘点 + 跨模块依赖扫描（35 个模块 / 128 个 .py 文件）+ 契约与 CI 实测，非照搬方案
> 性质：执行清单。每一项都给出文件/模块、动作、修改边界、依赖关系、验收证据。
> 铁律：**不得按《总体设计》的目录树批量创建文件。** 本清单的 Phase 0 全部为盘点与裁决，Phase 1 才有结构调整。

---

## 0. 仓库基线盘点（实测数据，决策依据）

### 0.1 目录与代码密度

| 位置 | 子模块 | 实况 |
| --- | --- | --- |
| services/ | 14 个目录 | **只有 4 个有代码**：project_planning(49 py)、asset_service(17)、identity_access(5)、identity_issuance(1)；**10 个空目录**：blocking_service、budget_quota、creative_exploration、memory_learning、model_gateway、observability、orchestrator、production_policy、qc_governance、rights_governance |
| packages/ | errors(2)、identity(10)、shared(28)、telemetry(2) | 全部有代码 |
| apps/ | 仅 api（main.py + __init__） | 单入口，符合现状 |
| workers/ | 7 个目录 | 有代码 3 个：relay(6)、identity(3)、outbox_maintenance(3)；空目录 4 个：delivery、generation、notifications、qc |
| adapters/ | 7 个目录 | 有代码：model_gateway、storage；其余空：audio_models、blocking、external_tool、image_models、video_models |
| agents/ | 3 个目录 | 全部空：evaluators、planners、runtime |
| contracts/ | 13 项（action_registry、openapi、state_machines、schemas、permission、budget、metrics、phase0、phases、enums、runtime、authorization、security_boundary） | **Phase 0 契约层已冻结且有自校验体系**（freeze_manifest / contract_check） |
| data/ | migrations(38 个版本文件)、policies、seeds、examples | 迁移链存在，CI 有 `alembic upgrade head` + `alembic check` |
| tests/ | architecture、engineering、unit、integration、permission、resilience、e2e | 七层测试已建立，C7.1=1106 / C7.2=126 / C7.3=3 通过 |
| scripts/ | freeze_manifest、contract_check、architecture_check、boundary_assertions、merge_readiness、secret_scan、verification_battery 等 10 个 | 质量门禁脚本齐备 |
| docs/adr | 31 个 .md 文件，adr_index.yaml 登记 118 条 ADR | 索引条目数与文件数不一致，需 Phase 0 盘清映射 |
| .github/workflows | 仅 ci.yml | ruff / mypy / pytest / alembic / 5 个自研门禁 / docker build 全在 |

### 0.2 跨模块依赖扫描（128 个 .py 文件实测）

| 依赖边 | 判定 |
| --- | --- |
| apps.api → services.asset_service / services.project_planning | 允许（API 编排层） |
| services.asset_service → services.identity_access | **需裁决**：跨域调用是否经公开只读接口 |
| services.project_planning → services.identity_access | **需裁决**：同上 |
| packages.shared → packages.identity | **需裁决**：共享内核依赖身份包，分层方向可疑 |
| workers.* → packages.shared / services.identity_issuance | 允许（Worker 依赖共享包） |
| 循环依赖 | 未发现 |

---

## 1. 业务域映射：保留 / 重构 / 合并 / 删除 / 新建

《总体设计》11 个一级业务域 vs 仓库实况：

| 《总体设计》域 | 现有落点 | 处置 | 理由 |
| --- | --- | --- | --- |
| Identity & Access | services/identity_access（有代码）+ identity_issuance(1 py) | **保留 + 合并** | identity_issuance 是同一域的发码职责，并入域内模块，不单列一级目录 |
| Project Planning | services/project_planning（49 py） | **保留 + 拆分** | Shot/ShotSpec/ShotRevision/ReworkTask 运行时在此目录内，需拆出为 Shot Planning |
| Production Graph | 无 | **新建** | 独立域；不得把 orchestrator 空目录复用为此域（职责不同） |
| Shot Planning | project_planning 内部 | **新建（拆出）** | 数据所有权矩阵要求 Shot 归 Shot Planning；拆分即 P1 历史问题①的落点 |
| Asset Management | services/asset_service（17 py） | **保留** | 命名不强制改（改名为 asset_management 无实质收益，属无谓 churn） |
| Timeline & Editing | 契约已冻结，无运行时 | **新建（Phase 3 前只建边界）** | V1 轻量；Phase 0/1 只定边界与契约归属 |
| Task Orchestration | services/orchestrator（空） | **改造，不新建平行目录** | 把 orchestrator 空目录改造为 task_orchestration 的落点；禁止新老并存 |
| Agent Runtime | agents/runtime（空） | **保留目录 + 补职责** | agents/ 定位为控制面的 Agent 能力，与方案一致 |
| Model Gateway & Connector | services/model_gateway（空） | **保留目录 + 补职责** | 已有正确落点 |
| Quality Governance | services/qc_governance（空） | **保留目录 + 补职责** | 同上 |
| Creator Ecosystem | 契约冻结，无服务 | **新建（Phase 3 前只建边界）** | 同 Timeline |

### 1.1 方案未列、但仓库已有的目录裁决（必须逐项 ADR，不得默认保留）

| 现有目录 | 实况 | 建议裁决 | 需要谁确认 |
| --- | --- | --- | --- |
| services/blocking_service | 空 | 白模/Previz 属控制面能力还是执行面适配器？定域后补职责或并入 adapters/blocking | 架构负责人 + Owner |
| services/budget_quota | 空 | 预算与配额在控制面有完整契约（budget_caliber.yaml）；定一级域或并入 Project Planning | 同上 |
| services/creative_exploration | 空 | 创意探索域归属（Creative Intelligence） | 同上 |
| services/memory_learning | 空 | 记忆学习域归属 | 同上 |
| services/production_policy | 空 | 生产策略与 Quality Governance 的关系 | 同上 |
| services/rights_governance | 空 | 版权治理独立域 or 并入 Asset Management | 同上 |
| services/observability | 空 | 横切能力，建议下沉 packages/telemetry + infra/monitoring，删除 services 级目录 | 同上 |
| workers/delivery、generation、notifications、qc | 空 | 执行面 Worker：保留目录 + 补任务契约，或删除待 Phase 3 建 | 同上 |
| adapters/ 全部 6 个 | 空 | 执行面适配器边界已定（ExternalToolAdapter 契约），保留目录 + 补实现 | 同上 |

**硬约束**：空目录在骨架冻结时不允许存在。裁决结果只能是三种之一：① 填职责并纳入验收；② 删除；③ 合并。任何"先留着"都必须登记为 ADR 状态 PROPOSED 且有责任人。

---

## 2. Phase 0：架构差距分析与决策冻结（实施清单）

> 目标：解决数据归属、模块边界、依赖、数据库权限、关键 ADR 决策。**此阶段不改业务代码。**

| 编号 | 任务 | 文件/模块 | 动作 | 修改边界 | 依赖关系 | 验收证据 |
| --- | --- | --- | --- | --- | --- | --- |
| P0-01 | 域映射裁决 | 全部 1.1 节条目 | 逐项 ADR | 只写 ADR，不改目录 | 无 | 每项 ADR 状态为 ACCEPTED，责任人明确 |
| P0-02 | Shot 域拆分方案 | services/project_planning/**（49 py） | 列出 Shot/ShotSpec/ShotRevision/ReworkTask 相关文件、表、迁移、action 归属清单 | 只盘点不改 | 依赖 F-02 数据所有权 | 拆分清单逐文件列出，含迁移与 action_key |
| P0-03 | 跨域调用面审计 | services/asset_service、project_planning 中 import identity_access 的 8 处调用点 | 逐点判定：只读接口 / 写入 / 绕过 | 只盘点不改 | 依赖 P0-02 | 每处调用点归类为「允许(只读契约)」或「整改」 |
| P0-04 | packages 分层裁决 | packages/shared → packages/identity 依赖边 | ADR：identity 下沉或 shared 解除依赖 | 只写 ADR | 无 | 依赖图无 shared→identity 边或已裁决接受 |
| P0-05 | 数据库写入权限矩阵 | data/policies/、迁移 38 个版本、数据库角色定义 | 盘点现有 DB role 与表级权限，对照服务写入路径 | 只盘点不改 | 依赖 P0-02/P0-03 | 矩阵逐表列出「拥有域 / 允许写入角色 / 实际写入路径」 |
| P0-06 | ADR 索引对账 | docs/adr/（31 文件）vs adr_index.yaml（118 条） | 盘清 118 条条目与 31 个文件的映射，找出无文件条目 | 只盘点不改 | 无 | 映射表 + 无文件条目清单 |
| P0-07 | 三个 P1 历史问题落位 | Shot 取消写入边界 / 授权语义 / Shot-ReworkTask 复合约束 | 在 ADR 中明确各自归属的域、验收方式 | 只写 ADR | 依赖 P0-02 | 三问题各有 ADR、责任人、验收标准 |
| P0-08 | 数据库 Schema 规划 | data/migrations | 对照《总体设计》5.2，确认现有 schema 划分与目标差距 | 只盘点不改 | 依赖 P0-05 | schema 差距清单（保留/新增/移动） |
| P0-09 | 执行契约落位 | contracts/runtime/、workers/ | 确认 Execution Contract 现有形态（execution_id / idempotency_key 等字段）与《总体设计》6.4 差距 | 只盘点不改 | 无 | 字段级差距表 |
| P0-10 | 架构基线 SHA | 仓库 main | 固定盘点起点 SHA 并记录 | 无 | 无 | 基线 SHA 写入报告 |

**Phase 0 出口**：产出《架构差距分析报告》+ 全部 ACCEPTED ADR + 三个 P1 问题的处置 ADR。任一未决（PROPOSED）则不进 Phase 1。

---

## 3. Phase 1：全局工程骨架建设（实施清单）

> 目标：按冻结设计实施结构与边界。**先处理空目录，再动有代码的目录。**

| 编号 | 任务 | 文件/模块 | 动作 | 修改边界 | 依赖关系 | 验收证据 |
| --- | --- | --- | --- | --- | --- | --- |
| P1-01 | 空目录清理/填充 | 1.1 节全部空目录 | 按 P0-01 裁决执行：删除、合并或补最小职责 | 只动空目录，不碰有代码模块 | P0-01 | `find services packages agents adapters workers -type d -empty` 为 0 |
| P1-02 | Shot Planning 拆分 | services/project_planning → services/shot_planning | 按 P0-02 清单移动文件、迁移、测试 | 移动 + import 重写；禁止顺带改业务逻辑 | P0-02/P0-03 | 全部测试通过；architecture_check 通过；无 services→project_planning 内部实现依赖 |
| P1-03 | Task Orchestration 落位 | services/orchestrator → services/task_orchestration | 目录改名 + 最小任务契约骨架 | 只建契约与骨架，不写调度实现 | P0-01 | 目录名与域一致；空目录问题归零 |
| P1-04 | 跨域调用整改 | asset_service、project_planning 中 P0-03 判为「整改」的调用点 | 改为公开契约（接口/命令）调用 | 只改调用面，不改业务语义 | P0-03 | architecture_check 通过；调用点全部经契约 |
| P1-05 | packages 分层整改 | packages/shared、packages/identity | 按 P0-04 ADR 执行 | 只改包依赖方向 | P0-04 | 依赖扫描无违规边 |
| P1-06 | 数据库角色与权限落地 | data/policies/、迁移 | 按 P0-05 矩阵实施最小权限 | 只增不删权限；禁止扩大写权限 | P0-05 | 权限负向测试：非法角色 UPDATE 核心表报错 |
| P1-07 | 身份链路接入 | packages/identity、services/identity_access | 统一 Principal 来源；API 层全部经验签 | 只改接入点 | P1-05 | 负向测试：伪造 owner_id/created_by 被拒 |
| P1-08 | 执行契约框架 | contracts/runtime/、workers/ | 补 P0-09 差距字段与 Worker 接入骨架 | 只补契约，不写业务执行器 | P0-09 | 契约校验用例通过 |
| P1-09 | 事件可靠性（Outbox） | workers/outbox_maintenance、packages/shared | 确认并接入事务消息/Outbox 模式 | 只改基础设施层 | P1-05 | 事务与事件一致性测试通过 |
| P1-10 | 日志与 Trace 关联 | packages/telemetry、services/observability(按裁决) | request/trace/correlation/execution ID 四段贯通 | 只改观测层 | P1-05 | 一次链路可被完整关联 |

**Phase 1 出口**：目录结构与域边界一致，空目录清零，所有依赖边合法，CI 全绿。**不要求新业务功能。**

---

## 4. Phase 2：骨架验证与冻结（验收清单，对齐《总体设计》F-01~F-16）

| 编号 | 验收项 | 验证方式（禁止 Mock 顶替） | 通过标准 |
| --- | --- | --- | --- |
| F-01 | 架构边界 | scripts/architecture_check.py + 依赖扫描 | 无违规边 |
| F-02 | 数据所有权 | 数据所有权矩阵 vs 迁移 vs 代码写入路径三方对账 | 逐表唯一拥有者 |
| F-03 | 依赖方向 | 依赖扫描 + 循环检测 | 无循环 |
| F-04 | 迁移链 | `alembic heads`（唯一）+ upgrade/downgrade 往返 | head 唯一 |
| F-05 | 数据权限 | 真实 PostgreSQL 负向测试 | 越权写入被数据库拒绝 |
| F-06 | 身份认证 | 真实 Token 签发→验签→拒签链路 | 负向用例通过 |
| F-07 | 授权 | 敏感操作 fail-closed 测试 | 无授权依据即拒绝 |
| F-08 | 契约 | freeze_manifest --check + contract_check + openapi 校验 | 全绿 |
| F-09 | 运行环境 | 真实启动 api + relay + outbox_maintenance | 三入口真实运行 |
| F-10 | Worker 最小链 | 真实任务从 API → 队列 → Worker → 结果 | 端到端最小链完成 |
| F-11 | 可观测性 | 一次操作的四段 ID 关联查询 | 日志与 Trace 可关联 |
| F-12 | 测试体系 | 七层测试全量执行 + 报告标注真实/未执行/阻塞 | 无虚假通过 |
| F-13 | 迁移验证 | 真实 PG 验证 P1 三个历史问题的复合约束（Shot/ReworkTask project 归属） | 复合唯一键/复合外键真实生效 |
| F-14 | 并发安全 | 并发测试（非 sleep） | 关键不变量有确定性证据 |
| F-15 | 文档一致 | ADR 与代码对账 | 无未解释冲突 |
| F-16 | 版本基线 | 骨架版本 + Git SHA | 可追溯 |

**冻结出口**：发布骨架版本、固定基线 SHA、冻结核心 ADR。BLOCKED / NOT RUN 一律不等于 PASS。

---

## 5. 三个 P1 历史问题在本清单中的落点

| 优先级 | 问题 | 落点编号 | 验收证据 |
| --- | --- | --- | --- |
| P1 | Shot 取消操作的数据库写入边界 | P0-07 → P1-04 → F-07 | 取消路径全部 fail-closed；负向测试覆盖未授权取消 |
| P1 | ADR、契约与运行时代码的授权语义 | P0-06 → P0-07 → F-15 | ADR 索引对账清单；授权语义单一事实源 |
| P1 | Shot/ReworkTask 项目归属约束 | P0-08 → P1-06 → F-13 | 真实 PostgreSQL 复合约束验证通过 |

---

## 6. 执行顺序（总控约束）

```
0) 仓库基线盘点（本清单即产物）
1) Phase 0：架构差距分析 + ADR 决策冻结（先于一切结构调整）
2) Phase 1：骨架建设（先空目录，后拆分，再整改调用面）
3) Phase 2：骨架验证与冻结（真实环境证据）
4) Phase 3：按业务域分批实现（生产管理基础 → 资产与执行 → 智能生产 → 编辑与生态）
```

**管理禁令（逐字执行）**：
1. 不允许开发人员按《总体设计》目录树批量创建文件；
2. 不允许新旧两套目录并存——空目录裁决必须是删除/合并/填充三选一；
3. 不允许用「先建骨架再迁移」为由冻结期内保留空目录；
4. 每个域的 ADR、迁移、契约、测试、验收报告五件套缺一不可；
5. Phase 0 任一 ADR 未 ACCEPTED，Phase 1 不得开始。

---

## 7. 遗留待 Owner / 架构负责人裁决项（阻塞清单）

1. services/ 内 9 个方案未列目录的域归属（见 1.1 表）；
2. packages/shared → packages/identity 的分层方向；
3. asset_service / project_planning 对 identity_access 的 8 处调用点归类；
4. observability 是删除 services 级目录还是保留；
5. Phase 3 第一组（Project Planning / Production Graph / Shot Planning）的确切依赖顺序。
