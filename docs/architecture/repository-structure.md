# AI 影视工业化生产平台 · 仓库目录设计（GitHub monorepo）

> 仓库地址：https://github.com/jhb-0212/AI_Creative_Media_Industrial_OS.git
> 仓库名：AI_Creative_Media_Industrial_OS（当前为空仓库，待初始化）
> 配套基线：`AI影视工业化生产平台V2.0.5.md`（AFS-ARCH-MASTER-V2.0.5）
> 权威依据：第 1.4 六层架构 / 1.5 部署建议 / 1.6 技术基线 / 第 14 章工程目录与数据所有权 / 17.11 目录建议 / ADR-009
> 设计原则：不屎山框架、不屎山代码、高并发/高流量/高标准、全程 AI 辅助开发

---

## 1. 仓库形态决策

**结论：单个 GitHub monorepo（模块化单体 + 异步 Worker），不拆多仓库、不拆微服务。**

| 决策 | 依据 | 说明 |
|---|---|---|
| 单仓库 monorepo | ADR-009「模块化单体优先」、1.5 部署建议 | 初期控制部署复杂度，降低未来拆分成本 |
| 模块化单体，非微服务 | ADR-009、14.3「模块化单体拆分条件」 | 以微服务数量作为架构成熟度指标是反模式 |
| 数据所有权从第一天执行 | 14.2 数据所有权矩阵 | 独立 Schema/表前缀/仓储接口，禁止跨模块直写表 |
| 契约优先 | Phase 0 冻结产物 | OpenAPI/Schema/状态机/Action Registry 作为单一事实源入仓 |

**未来拆分触发门槛（可量化，不臆断）：** 吞吐瓶颈、资源隔离、安全边界、团队独立发布、故障域隔离达到阈值时才拆，且保持既有 API/Event 契约不变。

---

## 2. 顶层目录结构

```
AI_Creative_Media_Industrial_OS/         # GitHub 仓库根（monorepo，与仓库名一致）
├── apps/                               # 进程入口层（薄层，只做装配与协议）
│   ├── api/                            #   HTTP API 服务（FastAPI，契约优先）
│   └── console/                        #   运营/管理控制台（后续按需）
│
├── services/                           # 领域服务层（模块化单体内核，按数据所有权分模块）
│   ├── orchestrator/                   #   编排：WorkflowRun/Task/DAG/Gate 状态推进
│   ├── project-planning/               #   项目/剧本/拆镜：Project/Shot/ShotSpec/Intent
│   ├── asset-service/                  #   资产：Asset/AssetVersion/Manifest/存储元数据
│   ├── rights-governance/              #   版权/许可事实/审核状态
│   ├── qc-governance/                  #   质量治理：QCReport/检测器/基准/Gate
│   ├── creative-exploration/           #   创意探索：Proposal/ExplorationRun
│   ├── blocking-service/               #   白模/Previz/ControlSignal
│   ├── production-policy/              #   生产策略解析（17.11）
│   ├── model-gateway/                  #   模型网关：Provider/ModelInvocation
│   ├── identity-access/                #   鉴权：Principal/Role/CapabilityToken
│   ├── budget-quota/                   #   预算/配额：CostLedger/Forecast
│   ├── memory-learning/                #   记忆/学习：Memory/Canon Index（Phase 2+）
│   └── observability/                  #   可观测：指标/追踪/告警聚合
│
├── workers/                            # 异步执行层（独立进程，消费 Outbox 事件）
│   ├── generation/                     #   图像/视频/音频/ComfyUI 生成
│   ├── qc/                             #   自动检测与 QCReport
│   ├── delivery/                       #   交付/DeliveryManifest
│   └── notifications/                  #   飞书/邮件/站内通知
│
├── agents/                             # Agent 运行时（Skills 契约执行）
│   ├── runtime/                        #   Agent 执行运行时
│   ├── planners/                       #   规划器
│   └── evaluators/                     #   评估器
│
├── adapters/                           # 外部适配层（只处理外部系统差异）
│   ├── image_models/                   #   图像模型
│   ├── video_models/                   #   视频模型
│   ├── audio_models/                   #   音频模型
│   ├── comfyui/                        #   ComfyUI
│   ├── blocking/                       #   Blender/Three.js/UE 白模
│   ├── external_tool/                  #   外部专业工具（剪映/Premiere/达芬奇）
│   └── storage/                        #   S3 兼容对象存储
│
├── contracts/                          # 契约层（Phase 0 冻结产物，单一事实源）
│   ├── schemas/                        #   object_schemas.schema.json
│   ├── openapi/                        #   openapi.draft.yaml
│   ├── state-machines/                 #   state_machines.yaml
│   ├── action-registry/                #   action_registry.yaml
│   ├── permission/                     #   permission_matrix.yaml
│   ├── budget/                         #   budget_caliber.yaml
│   ├── metrics/                        #   metric_dictionary.yaml
│   ├── enums/                          #   enum_registry.yaml
│   └── phases/                         #   phase_map.yaml
│
├── packages/                           # 横切共享能力（库，被上层层依赖）
│   ├── shared/                         #   鉴权/RBAC/ABAC/审计/密钥管理/多租户隔离
│   ├── errors/                         #   结构化错误码（PHASE_NOT_ENABLED/AUTH_SCOPE_DENIED...）
│   └── telemetry/                      #   OpenTelemetry 链路/日志/指标
│
├── data/                               # 数据与迁移
│   ├── migrations/                     #   Schema 正向迁移 + 回滚脚本
│   ├── seeds/                          #   种子数据
│   ├── examples/                       #   示例
│   └── policies/                       #   策略配置（版本化）
│
├── tests/                              # 测试层（负向优先）
│   ├── unit/                           #   单元测试
│   ├── integration/                    #   集成测试
│   ├── contract/                       #   契约测试（Schema/OpenAPI 一致性）
│   ├── permission/                     #   权限负向测试（B 组）
│   ├── resilience/                     #   韧性测试（幂等/重试/降级）
│   └── e2e/                            #   端到端主链测试（C 组）
│
├── infra/                              # 基础设施即代码 + 部署清单
│   ├── docker/                         #   Dockerfile/镜像
│   ├── compose/                        #   docker-compose（本地可复现，A1）
│   └── k8s/                            #   K8s 清单（Phase 4 规模化）
│
├── scripts/                            # CI/开发辅助脚本
├── docs/                               # 文档
│   ├── architecture/                   #   架构文档
│   ├── adr/                            #   架构决策记录
│   ├── runbooks/                       #   运维手册
│   └── sop/                            #   标准作业程序
│
├── .github/                            # GitHub 专属
│   ├── workflows/                      #   CI（lint/test/契约校验/构建）
│   ├── CODEOWNERS                      #   模块代码所有权
│   └── ISSUE_TEMPLATE/                 #   议题模板
│
├── pyproject.toml                      # Python 项目管理（依赖锁）
├── poetry.lock / uv.lock               # 依赖锁文件（A1 可复现）
├── Makefile                            # 统一命令入口
├── .env.example                        # 环境变量样例（不含真实密钥）
├── .gitignore                          # 密钥/本地文件不入库
├── .pre-commit-config.yaml             # AI 辅助开发的提交前把关
└── README.md                           # 仓库导航
```

---

## 3. 分层与依赖规则（不屎山框架的硬约束）

```
apps ──▶ services ──▶ (领域模型/仓储)
  │          │
  │          └──▶ adapters（经接口，不直连外部实现）
  │
  └──▶ packages/shared（横切能力）
```

- **依赖方向单向收敛**：`apps → services → adapters`，`services/workers/agents → packages`。禁止反向依赖、禁止跨层直连。
- **apps 是薄层**：只做协议适配（HTTP 鉴权、请求校验、DTO 转换），业务逻辑全部下沉到 services。
- **services 是内核**：每个模块拥有明确数据写权限（14.2 所有权矩阵），跨模块只经 API/命令 + Outbox 事件，禁止共享 ORM Model 直写他表。
- **workers 消费事件**：通过 Outbox 模式保证事务一致性，消费者 Inbox 去重、幂等。
- **adapters 不写权威状态**：只处理外部系统差异，回传结果进入平台资产暂存与验证。
- **contracts 是单一事实源**：代码从契约派生，CI 校验契约与实现一致（不重复维护两份）。

---

## 4. 数据所有权映射（14.2 → 目录）

| 模块目录 | 核心数据所有权 | 对外契约 |
|---|---|---|
| project-planning | Project/Episode/Scene/Shot/ShotRevision/ConstraintPack/Intent/ShotSpec | Project API、Intent API、ShotChanged Event |
| blocking-service | PrevizVersion/CameraRig/ControlSignalAsset 元数据 | Previz API、ControlSignal Events |
| creative-exploration | CreativeProposal/ExplorationRun/CreativeDecision/Pattern | Proposal API、Exploration Events |
| orchestrator | WorkflowRun/Task/Dependency/GateInstance/Outbox | Command API、Task/Gate Events |
| asset-service | Asset/AssetVersion/Manifest/Storage Metadata | Asset API、AssetVersionCreated Event |
| model-gateway | Provider/ModelCapability/ModelInvocation/ProviderJob | Submit/Status API、Invocation Events |
| qc-governance | QCReport/DetectorRegistry/Benchmark/QualityProfile/RightsCase/Override | Evaluate API、Gate Events |
| memory-learning | Memory/Canon Index/MemoryVersion/Feedback | Memory Query/Write API |
| budget-quota | CostLedger/Forecast/Capacity Snapshot/RCA/SLO | Forecast/RCA API、Operational Events |
| identity-access | Principal/Role/Policy/CapabilityToken/EscalationRequest/Audit | Capability API、Audit API |

> 初期模块化单体共用一个 PostgreSQL 实例，但每个模块用独立 Schema/表前缀，迁移脚本按模块划分。

---

## 5. 技术基线对应（1.6 → 落地）

| 能力 | 基线 | 目录落点 |
|---|---|---|
| API | Python + FastAPI | apps/api |
| 主数据库 | PostgreSQL | data/migrations（独立 Schema） |
| 文件存储 | S3 兼容对象存储 | adapters/storage |
| 缓存/限流/短队列 | Redis | packages/shared + infra |
| 持久化工作流 | Temporal | workers/ + services/orchestrator |
| 异步事件 | RabbitMQ/NATS/Kafka（Outbox） | services/orchestrator/outbox |
| 生成执行 | ComfyUI + 模型 Adapter | adapters/ |
| 可观测 | OpenTelemetry | packages/telemetry |

---

## 6. 工程质量约束（不屎山代码 + 高标准）

1. **单一职责**：禁止条件分支堆叠、超长函数、重复实现、绕开领域模型的临时特判。
2. **契约优先 + CI 校验**：`.github/workflows/` 中固化 lint（ruff/mypy）、单测（pytest）、契约一致性校验、负向权限测试。
3. **负向优先**：权限、状态机、Gate 验收先证明「该拒绝的拒绝了」。
4. **密钥不入库**：`.env.example` 只放占位，真实密钥走密钥管理服务；日志/异常不得出现明文密钥（红线 6）。
5. **可观测**：trace_id/job_id/project_id 全链路贯通，无断链（A3）。
6. **AI 辅助开发把关**：`.pre-commit-config.yaml` + CI 门禁，AI 产出代码/契约/配置同样过测试与红线校验（1.3.2 第 5 条）。

---

## 7. GitHub 仓库初始化建议

- **仓库地址**：`https://github.com/jhb-0212/AI_Creative_Media_Industrial_OS.git`（已存在，当前为空仓库）
- **仓库名**：`AI_Creative_Media_Industrial_OS`
- **默认分支**：`main`；保护分支 + PR 强制评审（对应「不得自批自用」在工程侧的落地）
- **CI 流水线（.github/workflows）**：
  - `ci.yml`：lint + unit + contract + permission 负向测试
  - `contract-check.yml`：校验 contracts/ 与实现派生一致性
  - `build.yml`：镜像构建 + 摘要固化（A1）
- **CODEOWNERS**：按 services/ 模块划分所有权，审批人与申请人职责分离
- **Secret 管理**：GitHub Actions Secrets / 外部密钥服务，绝不进仓库

### 7.1 首次初始化步骤（空仓库 → 脚手架）

1. 本地建同名目录并 `git init -b main`，关联远程 `git remote add origin <地址>`
2. 落地本设计的目录骨架 + 根文件（README、pyproject.toml、.gitignore、.env.example、Makefile、.github/）
3. 提交首个 commit 并 `git push -u origin main`
4. 在 GitHub 设置开启分支保护、配置 Actions Secrets、添加 CODEOWNERS 评审规则

---

## 8. 与 Phase 1 验收的映射

| 验收组 | 目录落点 |
|---|---|
| A 组（可运维） | infra/、data/migrations/、packages/telemetry/、scripts/ |
| B 组（权限职责分离） | services/identity-access/、tests/permission/ |
| C 组（首发主链） | services/orchestrator + project-planning + qc、tests/e2e/ |
| D 组（状态机/错误契约） | contracts/state-machines/、packages/errors/、tests/contract/ |
| E 组（成本配额） | services/budget-quota/、services/model-gateway/ |
| F 组（Creator 域） | services/ 预留，Phase 1 标 NOT_ENABLED |
| G 组（交付证据） | services/asset-service/、tests/e2e/、docs/runbooks/ |
