# AI Creative Media Industrial OS

AI 影视工业化生产平台的工程实现仓库（monorepo）。

> 配套基线文档：`AI影视工业化生产平台V2.0.5.md`（AFS-ARCH-MASTER-V2.0.5，Phase 0 契约冻结）
> 仓库形态：模块化单体 + 异步 Worker（ADR-009），单 GitHub monorepo，不拆微服务。

## 项目定位

平台不是「模板工厂」或「剪辑器替代品」，而是 **AI 影视工业化治理底座**：

- **三极产品体系**：制作（怎么做）→ 资产与知识中枢（用什么做/怎么复用）→ 社区展示（给谁看/怎么学）
- **三条顶层原则**：全等级品质平权、AI 创意自由、质量驱动调度
- **核心红线**：Agent 不得自批自用、终态不可原地复活、资产不可覆盖、质量不可暗降、密钥不落日志

## 仓库目录结构

```
apps/          # 进程入口薄层（api、console）
services/      # 领域服务内核（按数据所有权分模块，目录名下划线命名）
workers/       # 异步执行层（generation/qc/delivery/notifications）
agents/        # Agent 运行时（runtime/planners/evaluators）
adapters/      # 外部适配层（模型/ComfyUI/白模/存储/外部工具）
contracts/     # Phase 0 冻结契约（单一事实源）
packages/      # 横切能力（shared/errors/telemetry）
data/          # 迁移/种子/策略配置
tests/         # 测试（unit/integration/contract/permission/resilience/e2e）
infra/         # 基础设施即代码（docker/compose/k8s）
scripts/       # CI/开发辅助脚本
docs/          # 架构/ADR/runbook/SOP
.github/       # CI 流水线、CODEOWNERS、议题模板
```

> **命名约定**：Python 包/模块目录一律使用下划线（`project_planning`、`asset_service`），
> 禁止连字符（连字符在 Python 导入中是非法标识符）。

详细设计见仓库内 `docs/architecture/repository-structure.md`。

## 技术基线

| 能力 | 基线 |
|---|---|
| API | Python + FastAPI（契约优先） |
| 主数据库 | PostgreSQL（独立 Schema/表前缀） |
| 文件存储 | S3 兼容对象存储 |
| 缓存/限流/短队列 | Redis |
| 持久化工作流 | Temporal |
| 异步事件 | RabbitMQ/NATS/Kafka（Outbox 模式） |
| 生成执行 | ComfyUI + 各模型 Adapter |
| 可观测 | OpenTelemetry |

## 工程铁律

**本仓库不自定义铁律。** 五条强制规则的唯一权威是基线
`docs/baseline/AI影视工业化生产平台V2.0.5.md` §1.3.2「工程质量与 AI 辅助开发规则（实现必须强制）」，
此处只做摘录与指向，措辞以基线为准：

1. **架构无屎山**：分层、模块边界与数据所有权必须显式定义；禁止跨层直连、禁止跨模块直接写表、
   禁止平行重复模块与空壳透传服务；依赖方向单向收敛，新增模块先归位职责与数据所有权再实现。
2. **代码无屎山**：单一职责、可读可维护；禁止条件分支堆叠、超长过程函数、重复实现与绕开领域模型的临时特判；
   复杂逻辑收敛到领域模型、策略对象与声明式配置。
3. **禁止补丁式演进**：缺陷与需求必须在契约、边界或模型层根治；不得以临时绕过、平行表、特判分支、
   兼容分支叠加或魔法注释打补丁；现有设计无法承载时先修正设计与契约，再编码实现。
4. **保持高技术标准**：不因人力、单人开发或进度压力降低选型、实现与验收标准；以可运行代码、自动化测试、
   契约与证据交付，不靠简化架构完整性或省略验证换进度。
5. **全程 AI 辅助开发与生产**：AI 参与编码、评审、测试、重构；AI 产出同样受同一套架构边界、
   红线不变量、测试与可追溯要求约束；架构决策、审批与最终质量由人类负责。

> 历史说明：本 README 曾自造过一版「五条铁律」（把「高并发/高流量」「工业级最佳实践」单列成条），
> 与 §1.3.2 不是一一对应，导致口径分裂。已于第三轮审查后回归基线，详见
> `docs/audits/2026-11-16-review-round3-response.md`。

### 与技术质量指标的区别

高并发、限流、熔断、降级、幂等、可观测、水平扩展属于**技术质量与运行可靠性指标**，
是 §1.3.2 第 4 条「高技术标准」的验收内容，不是独立铁律，也不替代上面五条。
它们的验收证据（压测报告、故障演练记录）单列，当前状态见「未覆盖范围」一节。

## 自动化闸门

铁律靠机器守，不靠记性。下表是**当前真实执行**的闸门，以及它**只能证明到哪一步**：

| 闸门 | 守什么 | 命令 | 覆盖边界（不能证明什么） |
|---|---|---|---|
| `ruff` | 风格与常见缺陷 | `ruff check .` | 不证明设计与边界正确 |
| `mypy` | 类型契约 | `mypy packages services apps workers` | 不覆盖 `scripts/`、`tests/`（脚本与测试的类型错误不在闸门内） |
| `pytest tests` | 行为正确性、负向路径、分层规则 | `pytest tests` | `tests/e2e`、`tests/resilience` 尚不存在（见「未覆盖范围」） |
| `freeze_manifest` | **契约冻结**：基线正文未变 + 十一类产物哈希一致 + 演进有据（名称全集 == 基线 ∪ 已批准修订） | `python scripts/freeze_manifest.py --check` | 不证明契约内容业务正确，只证明「符合 V2.0.5 且变更可追溯」 |
| `contract_check` | 派生一致性：Action ↔ OpenAPI ↔ Schema ↔ 状态机 ↔ 事件 ↔ 债务台账 | `python scripts/contract_check.py` | 只证明派生文件互相自洽，**不能**证明符合基线（由 `freeze_manifest` 负责） |
| `architecture_check` | 层间依赖方向、服务间直连、导入期循环、进程边界（R4：apps 不得 import 签发模块） | `python scripts/architecture_check.py` | 只覆盖顶层分层；服务内 `api/domain/repository` 分层与数据库授权边界未覆盖 |
| `alembic check` | 迁移与模型无漂移 | `alembic check` | 不证明迁移后的权限边界正确 |

`freeze_manifest` 与 `contract_check` 是**两层互补**，不是重复：前者回答「还是不是 V2.0.5」，
后者回答「派生文件之间有没有打架」。历史教训是只有后者时，81↔81 自洽也能一路 PASS。

## 未覆盖范围（诚实登记）

以下项目前**没有**自动化证明，不因闸门全绿而视为已达标。已根治项不再列于此表
（数据所有权、Capability 运行时授权、状态推进入口、进程密钥拆分已在
`feat/security-data-boundary-f01-f05` 落地，见 PR #11）。

| 要求 | 当前状态 |
|---|---|
| 密钥不进入交付包 | 未实现自动扫描 |
| 高并发 / 故障恢复 | 有熔断、Outbox 重试；有可复现压测方案（`docs/perf/performance-acceptance-plan.md` + `scripts/perf/load_test.py`），但**实测证据待目标环境跑出报告** |
| 字段级契约演进 | 已有字段级差异诊断（`contract_check` 的字段签名台账），但字段语义合理性仍需人工在 amendments 登记 |
| Phase 1 生产闭环 | 未完成（见下） |

> 说明：原表「跨模块数据所有权」「Capability 运行时授权」「状态推进入口」
> 「审批与执行分离」四项已由 F01–F06 系统性重构根治，不再属于「未覆盖」；
> 身份签发已拆为独立进程 `workers/identity`（持私钥 + JWKS 端点，API 只持公钥），
> 详见 `contracts/security_boundary.yaml`（11 条不变量 satisfied，含 KEYS-3）与
> `docs/proposals/F01-F05-security-data-boundary-refactor.md`。

## 快速开始

```bash
# 依赖安装（待脚手架完成后启用）
# make install

# 运行测试
# make test

# 本地启动（docker-compose）
# make up
```

## 阶段状态

- **Phase 0**：契约冻结已通过（有条件）。冻结产物十一类齐全，清单与 SHA-256 见
  `contracts/phase0/phase0_manifest.md`；基线正文随仓库交付于 `docs/baseline/`。
  冻结后为 Phase 1 主链增加的 6 个 Action、3 个对象、3 个状态机，已按修订 `V205-R14-A1`
  追认登记（`contracts/phase0/contract_amendments.yaml`），**属事后补登记**。
- **Phase 1**：核心主链实现中，**未通过验收**。契约 `ACTIVE` ≠ 代码已实现 ≠ 集成已通过 ≠ 生产验收，
  四个状态不得混同；当前已实现 Action 见 `contracts/runtime/action_implementations.yaml`。
- **Creator Mode**：`NOT_ENABLED`（符合基线要求，不因 Schema 已存在而提前启用）。

详见 `docs/` 与基线文档。
