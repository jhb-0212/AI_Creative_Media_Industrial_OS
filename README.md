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
workers/       # 异步执行层：relay 与 outbox_maintenance 已实现；
               #   generation/qc/delivery/notifications 为占位（Phase 1 交付项）
agents/        # Agent 运行时 —— 占位：目录存在但尚无实现（Phase 1）
adapters/      # 外部适配层 —— 占位：目录存在但尚无实现（Phase 1）
contracts/     # Phase 0 冻结契约（单一事实源）
packages/      # 横切能力（shared/errors/telemetry）
data/          # 迁移/种子/策略配置
tests/         # 测试：architecture/integration/permission/unit 已实现；
               #   contract/resilience/e2e 尚未建立
infra/         # 基础设施即代码（docker/compose/k8s）
scripts/       # CI/开发辅助脚本
docs/          # 架构/ADR/runbook/SOP
.github/       # CI 流水线、CODEOWNERS、议题模板
```

> **目录 ≠ 已实现**。上面是目标结构与当前状态的合并标注：标为「占位」的目录
> 只有空目录，没有代码，也没有被任何测试覆盖。把它们当作"已有能力"来引用，
> 就是 README 过度宣称的老问题（第三轮审查已指出一次）。

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
| `contract_check` | 派生一致性：Action ↔ OpenAPI ↔ Schema ↔ 状态机 ↔ 事件 ↔ 枚举 ↔ 债务台账 | `python scripts/contract_check.py` | 只证明派生文件互相自洽，**不能**证明符合基线（由 `freeze_manifest` 负责） |
| `architecture_check` | 层间依赖方向、服务间直连、导入期循环、**数据库身份旁路**（业务模块不得自建/自选连接池）、**RLS 上下文唯一注入点**（业务代码不得自行写可见范围）、**服务内分层**（`domain/` 与 `service/` 不得 import 框架/驱动） | `python scripts/architecture_check.py` | 不覆盖 `apps/`、`workers/` 内部分层；服务内 `api/repository` 两层刻意不在规则内 |
| `alembic check` | 迁移与模型无漂移 | `alembic check` | 授权正确性由集成测试与 `test_grant_baseline` 证明，不由本命令证明 |
| `test_identity_private_key_boundary` | **签发私钥不进 API 进程**：配置层（`role=api` + 私钥即失败）、入口层（守卫独立成立且**真的被调用**，源码级断言）、结构层（公钥由私钥派生）、分发层（JWKS 文档可独立验签） | `pytest tests/unit/test_identity_private_key_boundary.py` | 只证明"私钥进不来"，不证明"签发进程存在"——仓库内尚无 issuer 部署与签发端点 |

`freeze_manifest` 与 `contract_check` 是**两层互补**，不是重复：前者回答「还是不是 V2.0.5」，
后者回答「派生文件之间有没有打架」。历史教训是只有后者时，81↔81 自洽也能一路 PASS。

## 未覆盖范围（诚实登记）

以下项目前**没有**自动化证明，不因闸门全绿而视为已达标：

| 要求 | 当前状态 |
|---|---|
| 跨模块数据所有权 / 禁止跨模块直接写表 | **已闭合（B1）**：数据库角色按域拆分，越权写入在 PostgreSQL 层被拒；R4 闸门禁止业务模块自选连接身份。证据见 `tests/integration/test_database_role_isolation.py` |
| 单一事实源（契约 ↔ 代码） | **枚举已闭合（C4）**：`contract_check` 的 `check_enum_parity` 双向比对代码枚举与契约状态（缺值/多值都报红），且 `services/*/domain/enums.py` 里每个枚举类必须在 `contracts/runtime/enum_bindings.yaml` 登记或豁免（未登记 = 报红）。`action_key` 层级有既有闸门。**仍双写**：`scope` / `risk_level` 字段在 YAML 与代码各一份，无机械比对 |
| 数据库不变量（资产状态、事件内容不可改） | **已闭合（C1/C2）**：资产 `status` 的每次变化必须在 `asset_service.asset_status_transitions`（迁移播种、应用角色只读）里登记，未登记边由触发器拒绝（含超级用户直连）；`status` 取值由 CHECK 限定为契约枚举；事件表只有投递域拿到 4 个投递状态列的列级 `UPDATE`，事实列不可改且有系统目录断言。见 `docs/adr/ADR-R20-data-invariants-in-database.md` |
| 权限最小化（Capability Token、资源范围） | 部分闭合：**统一授权决策已落地（B2）**——`Principal + Action + Resource + Context → ALLOW/DENY`，动作的 `scope` / `risk_level` 第一次真正参与运行时判定；拒绝带理由码可审计，81 条动作与 10 条 CRITICAL 有穷举性质测试。加上 B3 的组织/项目范围（数据库限制性策略兜底）。见 `docs/adr/ADR-R18-unified-authorization-decision.md`。**仍缺**：Capability Token（短时、按资源签发的运行时凭证） |
| 审批与执行分离 | 部分闭合（B2）：资产审批与剧本锁定共用同一份职责分离判定（`SELF_APPROVAL_FORBIDDEN`），重复实现已删除；**CRITICAL 动作尚未接入路由**，`AGENT_CRITICAL_FORBIDDEN` 与联合审批（`JOINT_APPROVAL_UNSUPPORTED`）只有策略层证明 |
| 密钥不进入交付包 | 未实现自动扫描 |
| 签发与验签分离（私钥不进 API 进程） | **已闭合（B4）**：`KeyStore`（进程内多私钥注册表）与 `from_key_store`（验签借用签发仓储）已删除；`TokenIssuer` 必须显式持私钥才能构造；`role=api` 带私钥**拒绝启动**（配置层 + 入口层两道），验签方只认 `IDENTITY_PUBLIC_KEY_PEM` 或 `IDENTITY_JWKS_FILE`。见 `docs/adr/ADR-R19-signing-key-boundary.md`。**仍缺**：独立的签发进程（`/token` 与 JWKS 发布端点均未实现，当前令牌由测试与脚本签发）、密钥指纹可观测性 |
| 高并发 / 故障恢复 | 有熔断、Outbox 重试；无压测与故障演练证据 |
| Phase 1 生产闭环 | 未完成（见下） |

这些是第三轮审查归并出的根因 F2/F3/F4/F5，收敛计划见
`docs/audits/2026-11-16-review-round3-response.md`。

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
