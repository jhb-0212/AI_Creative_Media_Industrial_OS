# 技术决策矩阵（TECH-00 · DECIDED 2026-11-23）

> 权威 ADR：`docs/adr/ADR-R44-technology-and-infrastructure-baseline.md`（DECIDED）。本矩阵是其机读视图，状态与裁决以 ADR 正文为准；两者不一致时以 ADR 正文为准并须同步修正本矩阵。
> 基线：main @ `abf77c1…`（BOOT-00 锁定），签署时点 `47abb3a…`（延续核实见 ADR-R44 §9.3）。核验标注规范：`docs/standards/verification-and-audit-annotation-standard.md`。
> 状态语义：`DECIDED` 已裁决（方向/PoC 授权）｜`NOT RUN` 未执行（≠ PASS，指 PoC/立项未启动）｜`BLOCKED` 待 Owner 输入｜`N/A` 不适用。**DECIDED ≠ 已切换**：现行技术栈在 PoC 通过 + 独立 ADR 修订 + 冻结重签前不变。

## 决策项

| ID | 能力 | 现行基线（切换前不变） | 候选 | Owner 裁决（2026-11-23） | 裁决理由 | 执行约束（不得越界） | 裁决状态 | PoC/立项状态 |
|---|---|---|---|---|---|---|---|---|
| TC-1 | 长流程执行引擎 | Outbox Saga + Worker + Relay（R42 已 DECIDED） | Temporal（SDK 已装，无服务） | **(b) 仅新流程引入，R42 不迁** | temporalio 仅 SDK 依赖；R42 已在 main；保单一权威源 | 不得迁 R42 业务事务/回执/幂等/补偿权威；(c) 须另走 R42 修订 + 冻结重签 | DECIDED | NOT RUN |
| TC-2 | 业务数据库 | PostgreSQL 16（全链已验证） | PostgreSQL 18.7（F-1 已核验） | **(b) 先做 PG18.7 全链 PoC，验证前继续 PG16** | 早期验证升级收益最高；F-1 已核验 | 隔离库执行、禁挂旧数据目录；切换须 TECH-01 五项全过 + ADR 修订 + 冻结重签 | DECIDED | NOT RUN |
| TC-3 | 缓存后端 | Redis 7（浮动标签，Digest 待固化）+ fail-open 熔断 | Valkey 9.1.4（基准）/ 9.2.0（独立测试） | **(b) 独立 PoC，保留 redis-py，不切生产默认** | F-2/F-3：9.2 含新数据格式、CE 7.4+ 不兼容 | 先固化 redis:7 Digest；禁同时换服务端/客户端/算法；PoC 不改 Compose 默认 | DECIDED | NOT RUN |
| TC-4 | 对象存储 | 本地 FS + `local://`（非 local fail-closed） | S3 API；SeaweedFS 为私有化候选 | **(a) 经现有 Storage Port 增量接入，先做 S3/SeaweedFS 对比 PoC** | 媒体存储需扩展性；不建第二套 API 是铁律 | 复用 Port、不加 delete；入口保持 fail-closed，禁失败回落；切换须 TECH-04 七项全过 | DECIDED | NOT RUN |
| TC-5 | Shot 域形态 | Shot 在 project_planning，归属 Scene→Episode→Project（R16~19） | 独立 services/shot_planning + 独立 schema | **(a) 独立领域与 Schema，归属链不变** | 保持 Shot 独立所有权；episode_id 不冗余 | 先有 SHOT-01 归属/迁移/无双写方案，不得直接搬代码；无双写且真实 PG 对账通过才切换 | DECIDED | NOT RUN（SHOT-01 待立项） |
| TC-6 | Agent 框架 | agents/ 预留空结构；Phase 1 不开放权威写入（R26） | LangGraph（Phase 2） | **(a) Phase 1 不引入，Phase 2 候选** | 遵守 R26；执行包 §2.5 | Phase 1 不开放 Agent 权威写；引入须主链验收 + R26 前置 ADR | DECIDED | NOT RUN |
| TC-7 | 独立消息代理 | PostgreSQL Outbox + Relay（已实现） | RabbitMQ / NATS / Kafka | **(a) 首期不引入，保留 Outbox** | 已实现；Task Queue ≠ 业务事件总线 | 不部署独立 Broker；引入须量化容量/隔离证据 + 独立 ADR | DECIDED | NOT RUN |

## 官方版本发布事实（外部 E1 取证，非「可引入」结论）

| ID | 事实 | 状态 | 证据 | 核验日 |
|---|---|---|---|---|
| F-1 | PostgreSQL 18.7 于 2026-11-12 随官方公告发布（含 19 RC2，GA 计划 11-19） | VERIFIED [E1] | postgresql.org 官方公告（见 ADR-R44 §3） | 2026-11-23 |
| F-2 | Valkey 9.2.0 于 2026-11-19 发布，minor 版本，含新数据格式与少量行为变更 | VERIFIED [E1] | valkey.io 官方博客（见 ADR-R44 §3） | 2026-11-23 |
| F-3 | Valkey 兼容 Redis OSS 7.2-；Redis CE 7.4+ 数据文件不兼容 | VERIFIED [E1] | valkey.io/topics/migration（见 ADR-R44 §3） | 2026-11-23 |

## 切换门槛（统一，缺一即维持现行基线）

1. 真实交互 + 故障注入运行证据 `[E5]`（非 Mock、非仅容器启动）；
2. 独立 ADR 修订 + Owner 签署；
3. 冻结清单 / 契约派生文件同步重签；
4. 不与领域重构捆绑在同一 PR。

> 七项 `DECIDED` 仅表示方向与 PoC 授权已定，**不构成任何组件切换授权**。切换前须满足上列四项门槛并在对应独立 ADR 上登记。PoC/立项状态全为 `NOT RUN`，本矩阵不启动任何 PoC。

## 签署信息

- 裁决日：2026-11-23。授权来源与经办核实见 ADR-R44 §9.1；定版指纹见 `ADR-R44-VERSION.txt`。
- 不在本次裁决范围（维持原判定）：R42 实施授权口径（BLOCKED）、C7 QC 手动入队端点边界（PARTIAL）、D6 人工重放入口（PARTIAL）。
