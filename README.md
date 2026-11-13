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

1. **不屎山框架**：分层清晰、模块边界明确、依赖单向收敛、可测试可演进
2. **不屎山代码**：单一职责、清晰命名、合理抽象、禁止补丁式演进
3. **高并发/高流量/高标准**：幂等、限流、熔断、降级、异步解耦、水平扩展、可观测、契约可校验
4. **全程 AI 辅助开发**：AI 提效不降质量、不破红线，产出经人机复核 + 契约校验 + 测试 + CI 把关

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

- **Phase 0**：契约冻结已通过（有条件）—— `contracts/` 目录为冻结产物
- **Phase 1**：核心主链实现（进行中）

详见 `docs/` 与基线文档。
