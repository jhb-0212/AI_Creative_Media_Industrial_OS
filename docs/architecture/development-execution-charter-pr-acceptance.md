# 开发执行总纲 + PR 拆分验收规范

> 上位文件：《AI Creative Media Industrial OS 完整系统架构与分阶段建设方案 V1.0》（下称《建设方案》）
> 执行基线：本地仓库快照（= main @ `b30d361`），**所有操作以本地为准**
> 配套文件：`docs/architecture/skeleton-implementation-checklist.md`（仓库盘点与差距分析，本文档的输入）
> 性质：开发团队执行规范。每张 PR 卡定义任务边界、输入、输出、禁止修改范围、验收证据、退出条件。
> 核心约束：**开发人员只允许在 PR 卡范围内施工；架构决策权不在 PR 卡内，任何结构性改动必须先走 ADR。**

---

## 0. 执行环境与基线

| 项 | 值 |
| --- | --- |
| 代码基线 | 本地目录（SHA `b30d361`，Phase 0 契约层已冻结） |
| 盘点结论 | 14 个 services 目录中 10 个为空；有代码：project_planning(49 py)、asset_service(17)、identity_access(5)、identity_issuance(1)；packages 4 个全有代码；workers 3/7 有代码；adapters、agents 全空 |
| 已存在的基础能力（禁止重建） | 契约层（freeze_manifest / contract_check）、七层测试、38 个迁移版本、CI 门禁、Outbox Worker、Relay、令牌签发与吊销 |
| 必须依赖的验收四态 | `PASS` / `FAIL` / `BLOCKED` / `NOT RUN`；**BLOCKED 与 NOT RUN 一律不等于 PASS** |

---

## 1. 执行总纲

### 1.1 执行顺序（不可重排）

```
PR-00（Phase 0 冻结收口）
  → PR-01~PR-06（Phase 1 骨架整改，S01–S20 证据积累）
  → PR-07（骨架冻结，S01–S20 全部 PASS 才可关闭）
  → PR-08~PR-09（Phase 2 核心创作链）
  → PR-10~PR-12（Phase 3 最小生产链）
  → PR-13（Phase 4 质量闭环）
  → PR-14（Phase 5 成本与知识）
  → PR-15（Phase 6 MVP 验收）
```

依赖关系硬规则：**后一个 PR 的「输入」未满足时不得开工；前一个 PR 未达退出条件时不得宣称完成。**

### 1.2 决策权限分层

| 决策类型 | 谁有权 | 途径 |
| --- | --- | --- |
| 冻结契约 / ADR / 基线状态变更 | Owner | ADR 评审 + 签署 |
| 域边界、数据所有权、Schema 划分 | 架构负责人 + Owner | 架构裁决文件（PR-01 产物） |
| PR 卡内的实现细节 | 开发人员 | 代码审查 |
| 新增架构层 / 新事实源 / 新目录 | 禁止在 PR 内发生 | 必须先 ADR |

### 1.3 AI 辅助开发约束（每 PR 强制执行）

- AI 生成内容不享受豁免，必须过同一套 CI、审查与验收；
- AI 不得修改 `contracts/phase0/` 冻结文件、`docs/baseline/`、已 ACCEPTED 的 ADR；
- AI 产出必须附：修改根因、事实源、测试命令、真实执行结果、未验证项；
- 禁止 AI 以批量生成文件制造工作量假象。

---

## 2. PR 路线图（16 个 PR × 阶段映射）

| PR | 名称 | 阶段 | 依赖 | 本地落点 |
| --- | --- | --- | --- | --- |
| PR-00 | Phase 0 冻结收口 | Phase 0 | 无 | docs/baseline、contracts/phase0、docs/audits |
| PR-01 | 架构事实源对账 | Phase 1 | PR-00 | docs/adr（31 文件/118 条目）、scripts/freeze_manifest.py |
| PR-02 | 数据库边界整改 | Phase 1 | PR-01 | data/migrations（38 版本）、data/policies |
| PR-03 | 身份与授权边界整改 | Phase 1 | PR-01 | packages/identity、services/identity_access |
| PR-04 | API 与 Worker 生命周期 | Phase 1 | PR-03 | apps/api、workers/relay、workers/outbox_maintenance |
| PR-05 | 事件、事务与任务执行 | Phase 1 | PR-02+PR-04 | workers/outbox_maintenance、packages/shared |
| PR-06 | 测试与 CI 收敛 | Phase 1 | PR-02~PR-05 | tests/ 七层、.github/workflows/ci.yml |
| PR-07 | 骨架最终验收（S01–S20） | Phase 1 | PR-01~PR-06 | 全仓 |
| PR-08 | 项目与剧本基础能力 | Phase 2 | PR-07 | services/project_planning |
| PR-09 | Storyboard 与 ShotSpec | Phase 2 | PR-08 | services/shot_planning（新拆）|
| PR-10 | Production Planning | Phase 3 | PR-09 | services/production_graph（新建）|
| PR-11 | Model Gateway 与 Generation | Phase 3 | PR-10 | services/model_gateway、workers/generation、adapters/* |
| PR-12 | Asset 与 AssetVersion | Phase 3 | PR-11 | services/asset_service |
| PR-13 | QC 与人工审核 | Phase 4 | PR-12 | services/qc_governance |
| PR-14 | Budget 与生产记录 | Phase 5 | PR-13 | services/budget_quota |
| PR-15 | MVP 集成验收 | Phase 6 | PR-08~PR-14 | 全仓 + 试播项目数据 |

---

## 3. PR 执行卡（逐张规范）

> 每张卡包含：任务边界 / 输入 / 输出 / 禁止修改范围 / 验收证据 / 退出条件。

### PR-00：Phase 0 冻结收口

- **任务边界**：仅处理冻结决议与状态口径，不写任何业务代码。
  - A-008：基线状态唯一口径维持「条件通过」，不升级（升级必须 Owner 新决议）；
  - A-009：21 条追溯债务台账层已清零，本 PR 只复核矩阵与台账一致性，不改台账；
  - 冻结决议原件：找回或由 Owner 出具新决议（见退出条件）。
- **输入**：`docs/audits/2026-11-22-phase0-independent-review.md`、`docs/audits/2026-11-22-amendment-traceability-matrix.md`。
- **输出**：《Phase 0 冻结收口报告》：状态口径声明、A-008/A-009 复核结论、Owner 决议或裁决记录。
- **禁止修改范围**：`contracts/phase0/` 全部冻结文件、`docs/baseline/`、任何 ADR 内容、任何业务代码。
- **验收证据**：基线哈希与 frozen_inventory 一致（本地重算）；台账 23 条与矩阵逐条一致；Owner 决议文本归档。
- **退出条件**：Owner 对「条件通过 → 无条件通过」作出正式裁定（含签署或明确的拒绝升级记录）。**没有 Owner 裁定，本 PR 不得关闭。**

### PR-01：架构事实源对账

- **任务边界**：建立「ADR ↔ 契约 ↔ ORM ↔ 迁移 ↔ 运行时代码」唯一权威关系；产出架构裁决文件；不实现新功能。
- **输入**：PR-00 收口报告；`docs/adr/adr_index.yaml`（118 条）；`contracts/phase0/frozen_inventory.yaml`；本地 31 个 ADR 文件。
- **输出**：
  - ADR 对账表（118 条目 × 文件映射，无文件条目清单）；
  - 跨域调用面裁决（asset_service、project_planning 对 identity_access 的 8 处调用点逐一归类）；
  - packages/shared → packages/identity 分层裁决；
  - 9 个方案未列目录（blocking_service、budget_quota、creative_exploration、memory_learning、observability、production_policy、rights_governance、orchestrator、qc_governance）的域归属 ADR。
- **禁止修改范围**：冻结契约、ADR 编号（不得补空号）、业务逻辑。
- **验收证据**：对账表逐行有证据链接；每项裁决为 ACCEPTED ADR；`freeze_manifest --check` 仍全绿。
- **退出条件**：无「无文件 ADR 条目」悬空；无「PROPOSED 未决」裁决项；无未解释的跨域依赖边。

### PR-02：数据库边界整改

- **任务边界**：按 PR-01 裁决实施 Schema 划分、迁移链治理与写入权限；含三个 P1 历史问题中的「Shot/ReworkTask 复合归属约束」。
- **输入**：PR-01 架构裁决文件；`data/migrations/versions`（38 文件）；`data/policies/`。
- **输出**：迁移链对账报告；复合唯一键/复合外键迁移（Shot/ReworkTask→Project 归属）；数据库角色最小权限落地；Schema 划分表。
- **禁止修改范围**：冻结契约中的对象字段语义；已上线的读接口返回结构（破坏性变更需 ADR）。
- **验收证据**：`alembic heads` 唯一；upgrade/downgrade 往返真实 PG 通过；越权 UPDATE 负向测试被数据库拒绝（真实角色，非 Mock）。
- **退出条件**：三个 P1 数据库约束在真实 PostgreSQL 上验证 PASS；迁移链唯一。

### PR-03：身份与授权边界整改

- **任务边界**：统一 Principal 来源；API 层全面经验签；清理跨域身份调用；敏感操作 fail-closed。
- **输入**：PR-01 对 8 处跨域调用点的裁决；`packages/identity`(10 py)、`services/identity_access`(5 py)。
- **输出**：可信 Principal 接入规范；身份调用面整改（只读契约或命令接口）；伪造身份字段（owner_id/created_by 等客户端直传）拒绝用例。
- **禁止修改范围**：Token 格式与签发流程的已验证部分；用户密钥存储加密方案。
- **验收证据**：负向测试覆盖：未认证、过期、错签发方、错 audience、吊销、越权、跨项目；全部真实链路。
- **退出条件**：权限负向测试全 PASS；依赖扫描无服务→服务越权调用残留。

### PR-04：API 与 Worker 生命周期

- **任务边界**：api 与 worker 的启动检查、优雅关闭、健康检查（liveness/readiness 分离）、配置校验。
- **输入**：`apps/api/main.py`；`workers/relay`(6 py)、`workers/outbox_maintenance`(3 py)、`workers/identity`(3 py)。
- **输出**：统一生命周期钩子（startup/shutdown/healthz/readyz）；启动失败显式报错（不吞错）。
- **禁止修改范围**：业务路由语义；Worker 消费逻辑（属 PR-05）。
- **验收证据**：三入口真实启动 + 优雅关闭实测；readiness 在依赖不可用时返回非 200；配置缺失时启动报错。
- **退出条件**：三入口生命周期验证 PASS，证据为真实进程输出。

### PR-05：事件、事务与任务执行

- **任务边界**：事务消息/Outbox 接入全部跨域写入路径；任务幂等键与执行契约落地。
- **输入**：`workers/outbox_maintenance`（已有）；`contracts/runtime/`；《建设方案》6.4 执行契约字段。
- **输出**：Outbox 与事务一致性测试；执行契约字段补差（execution_id / idempotency_key / task_version 等）；重复投递与死信处理证据。
- **禁止修改范围**：业务域状态机定义；事件 payload 的冻结字段。
- **验收证据**：事务回滚时事件不丢失不重复（真实 PG）；重复消费幂等测试；死信可观测。
- **退出条件**：事务一致性测试 PASS；幂等测试覆盖「同键重复提交」三层次（API/任务/入库）。

### PR-06：测试与 CI 收敛

- **任务边界**：补齐 CI 门禁缺口（依赖漏洞扫描、SBOM 可 Phase 5 后置）；建立测试报告四态标注模板；清理虚假测试。
- **输入**：`.github/workflows/ci.yml`（已有 ruff/mypy/pytest/alembic/5 自研门禁）；`tests/` 七层。
- **输出**：测试报告模板（已执行/未执行/环境阻塞/Mock/真实库/真实服务/生产）；CI 缺口清单与排期。
- **禁止修改范围**：不得通过改测试让实现通过；不得隐藏失败日志。
- **验收证据**：CI 真实全绿；任一违反案例（如去掉负向断言）能被 CI 抓出。
- **退出条件**：CI 覆盖清单与《建设方案》11.1 一致（除已登记后置项）；无「未执行标记为通过」案例。

### PR-07：骨架最终验收（S01–S20）

- **任务边界**：只做验收与证据汇编，不新增功能。
- **输入**：PR-01~PR-06 全部证据。
- **输出**：骨架冻结报告：S01–S20 逐项四态 + 证据链接 + 基线 SHA；空目录清零证明（`find … -type d -empty` 为 0）。
- **禁止修改范围**：无（本 PR 不修改任何文件，只产出报告与可能的空目录删除——空目录删除须逐项登记）。
- **验收证据**：S01–S20 全部 PASS；每项有可复算证据。
- **退出条件**：S01–S20 无 FAIL / BLOCKED / NOT RUN；骨架版本号与 SHA 归档。

### PR-08：项目与剧本基础能力（Phase 2 起点）

- **任务边界**：项目管理、集数、剧本导入与版本；只扩展 services/project_planning 现有域，不新开目录。
- **输入**：PR-07 骨架冻结；`services/project_planning`（49 py，api/domain/repository/service 结构）。
- **输出**：Script/ScriptVersion/Episode 完整闭环（导入→结构化→人工确认→版本）；确认版本不可被 AI 覆盖的约束落地。
- **禁止修改范围**：Phase 0 冻结契约中 Project/Episode/ScriptRevision 的既有 action 语义；其他域目录。
- **验收证据**：正负向测试；「AI 修改建议 ≠ 已确认版本」分离测试；端到端：导入试播剧本片段→集数→人工确认记录。
- **退出条件**：剧本到集数的真实闭环在本地可复现。

### PR-09：Storyboard 与 ShotSpec

- **任务边界**：把 Shot/ShotSpec/ShotRevision/ReworkTask 运行时从 project_planning 拆出到 services/shot_planning；实现拆镜→ShotSpec→VisualPlan→人工确认。
- **输入**：`skeleton-implementation-checklist.md` P0-02 拆分清单（先完成该清单）。
- **输出**：域拆分完成（import 重写、迁移归属调整）；ShotSpec/VisualPlan 契约落地；拆镜输入输出结构化（禁止塞 Markdown 大文本）。
- **禁止修改范围**：拆分移动中禁止顺带改业务逻辑；冻结的 Shot 相关 action 语义。
- **验收证据**：拆分前后测试全绿；architecture_check 通过；拆镜产物可校验（Schema 校验）。
- **退出条件**：一个真实剧本场景可拆出结构化 Shot 列表并人工确认。

### PR-10：Production Planning

- **任务边界**：新建 services/production_graph 域（先 ADR 后目录）；生产计划、任务 DAG、依赖、失效传播（受影响的场景/镜头/计划识别）。
- **输入**：PR-09；ADR 裁决「production_graph 职责边界」。
- **输出**：ProductionPlan/ProductionTask/DAG 节点契约与最小实现；失效传播规则（按版本引用计算，禁止全量过期）。
- **禁止修改范围**：不得把 DAG 节点状态与业务状态混成一个 Task 字段；不得复用 orchestrator 空目录（该目录按清单处置）。
- **验收证据**：DAG 构建/依赖检查/失败传播测试；「上游变更→影响范围」计算正确性用例。
- **退出条件**：Shot 能生成可执行生产计划并通过依赖校验。

### PR-11：Model Gateway 与 Generation

- **任务边界**：填充 services/model_gateway 空壳（供应商注册、参数规范化、凭证、调用记录）；workers/generation 填充（一条稳定路线：图像生成或图生视频二选一）；**只做一条路线**。
- **输入**：PR-10；`adapters/` 现有空目录（image_models/video_models 按所选路线启用其一）。
- **输出**：USER_OWN_KEY 模式真实调用；调用成本与失败记录；生成参数留档；结果文件校验（大小/Hash/MIME）。
- **禁止修改范围**：不新增第二条生成路线；不实现平台支付（PLATFORM_PAID 仅留模拟位）；密钥不得写入日志/配置/测试报告。
- **验收证据**：真实模型调用成功+失败各至少一次留档；密钥加密落库并可轮换；成本记录与供应商账单口径一致。
- **退出条件**：真实生成 1 个镜头且全链路参数可追溯。

### PR-12：Asset 与 AssetVersion

- **任务边界**：扩展 services/asset_service（已有 17 py）：AssetVersion 不可变、引用明确版本、来源（provenance）链路。
- **输入**：PR-11 的生成产物。
- **输出**：资产入库→版本建立→引用追踪闭环；provenance 记录（模型/版本/参数/参考资产/任务/审核）。
- **禁止修改范围**：Worker 不得直接写正式资产版本（必须经 asset_service 应用服务）；禁止静默覆盖。
- **验收证据**：同资产多次生成产生多版本；引用固定版本不随新版本漂移；来源链可查询。
- **退出条件**：生成产物可在系统内完成版本化入库与追溯。

### PR-13：QC 与人工审核

- **任务边界**：填充 services/qc_governance 空壳：自动 QC 记录、人工审核、质量 Gate、返工任务。
- **输入**：PR-12 资产版本。
- **输出**：QCRecord（检测项/结果/置信度/证据位置）；双屏审核工作台（左侧需求与参考、右侧结果与 QC）；Gate 规则；返工闭环（引用原版本+QC 项+新参数，不覆盖旧版本）。
- **禁止修改范围**：审核权限不得只靠前端隐藏；Gate 不得可被客户端参数绕过。
- **验收证据**：未通过审核的资产无法进入交付（服务端断言）；返工产生新版本且旧版本保留；审核审计记录完整。
- **退出条件**：一个真实生成结果完成「QC→驳回→返工→再审核→通过」全流程。

### PR-14：Budget 与生产记录

- **任务边界**：填充 services/budget_quota 空壳：项目预算池、任务预算、成本账本（调用成本/失败成本/工时）、预算预警。
- **输入**：PR-11 成本记录、PR-13 流程数据。
- **输出**：成本汇总口径（与 budget_caliber.yaml 一致）；USER_OWN_KEY 成本核算；PLATFORM_PAID 仅模拟位（不实现充值/支付/退款/发票）。
- **禁止修改范围**：预算口径契约；不得另建第二份成本事实源。
- **验收证据**：试播项目成本可汇总（调用/失败/工时三类）；超预算预警触发测试。
- **退出条件**：试播项目产生可复算的成本报表。

### PR-15：MVP 集成验收

- **任务边界**：只做集成、修复与验收，不新增功能。
- **输入**：PR-08~PR-14 全部交付。
- **输出**：1 个试播项目、5–10 个真实镜头、完整追溯记录（剧本→分镜→参数→资产→QC→审核→交付）、验收报告。
- **禁止修改范围**：为赶验收修改任何已冻结契约/ADR；把 BLOCKED 写为 PASS。
- **验收证据**：每镜头可从头到尾追溯；失败镜头可解释；成本报表可复算；本地+离线私有化部署可复现。
- **退出条件**：MVP 验收报告由 Owner 确认签署。

---

## 4. 每个 PR 的验收证据登记模板（强制）

```
PR 编号：
Base SHA / Head SHA：
需求来源：
ADR 依据：
修改目的与根因：
事实源变化：
数据库变化：
API/事件变化：
权限影响：
测试命令与真实结果：
真实环境验证（数据库/服务/进程）：
未验证项（须列明 BLOCKED / NOT RUN）：
残余风险：
```

禁止只写「代码已完成」「测试全部通过」这类无法核验的结论。

---

## 5. 全局红线（任何 PR 内触犯即打回）

1. 不得新建与现有目录平行的第二套实现；
2. 不得让空目录（.gitkeep）通过骨架验收；
3. 不得绕过 Owner 改变冻结契约、ADR、基线状态；
4. 不得在 PR 内新增架构层、新事实源、新 Schema 而不先 ADR；
5. 不得用 Validator 堆叠、包装层、定时任务掩盖事务与约束缺陷；
6. 不得以 CI 全绿代替真实环境证据；
7. 不得伪造、隐藏、或把未执行测试标记为通过；
8. 不得让 Worker / Agent 成为业务事实的最终写入者。

---

## 6. 需要 Owner / 架构负责人先行裁决的阻塞项（阻塞对应 PR）

| 阻塞项 | 阻塞的 PR | 裁决方式 |
| --- | --- | --- |
| Phase 0 无条件通过的 Owner 裁定 | PR-00 | Owner 签署或明确维持条件通过 |
| 9 个方案未列目录的域归属 | PR-01 | 架构裁决 ADR |
| shared→identity 分层方向 | PR-01 | ADR |
| 8 处跨域调用点归类 | PR-01/PR-03 | ADR + 整改 |
| 首期生成路线选择（图像 or 图生视频） | PR-11 | Owner 决策 |
| 试播项目剧本与镜头清单 | PR-08/PR-15 | Owner 提供 |
