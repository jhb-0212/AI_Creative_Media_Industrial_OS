# 技术决策矩阵（TECH-00 · PROPOSED 待 Owner 签署）

> 权威 ADR：`docs/adr/ADR-R44-technology-and-infrastructure-baseline.md`（PROPOSED，未批准）。本矩阵是其机读视图，状态与裁决以 ADR 正文为准；两者不一致时以 ADR 正文为准并须同步修正本矩阵。
> 基线：main @ `abf77c1…`（BOOT-00 锁定）。核验标注规范：`docs/standards/verification-and-audit-annotation-standard.md`。
> 状态语义：`PROPOSED` 草案待签｜`DECIDED` 已裁决｜`BLOCKED` 待 Owner 输入（≠ PASS）｜`NOT RUN` 未执行（≠ PASS）。

## 决策项

| ID | 能力 | 现状（裁决前默认） | 候选 | 淘汰/风险理由 | PoC 前置条件 | 退出方案 | 状态 | Owner 裁决（待填） |
|---|---|---|---|---|---|---|---|---|
| TC-1 | 长流程执行引擎 | Outbox Saga + Worker + Relay（R42 已 DECIDED） | Temporal（SDK 已装，无服务） | 引入第二套执行引擎；R42 业务事务/回执/幂等/补偿权威不能被 Temporal 取代 | TECH-03 六项全过；(c) 另需 R42 正式修订 + 冻结重签 | 不可恢复或改变领域事实权威→降为研究 | BLOCKED | — |
| TC-2 | 业务数据库 | PostgreSQL 16（全链已验证） | PostgreSQL 18.7（官方 2026-11-12 发布，F-1 已核验） | 主版本升级触发 RLS/分区/复合外键全链复验；禁挂旧数据目录 | TECH-01 五项全过 + 迁移恢复演练 | 任一不过→维持 16，登记 BLOCKED | BLOCKED | — |
| TC-3 | 缓存后端 | Redis 7（浮动标签，实际小版本待固化）+ redis-py + fail-open 熔断 | Valkey 9.1.4（基准）/ 9.2.0（独立测试） | Redis CE 7.4+ 数据文件与 Valkey 不兼容（F-3 已核验）；9.2 含新数据格式且发布仅数日（F-2） | 先固化 redis:7 Digest；TECH-02 五项全过 | 任一不过→维持 Redis 7；禁止同时换服务端/客户端/算法 | BLOCKED | — |
| TC-4 | 对象存储 | 本地 FS + `local://` 内容寻址（Generation 对非 local fail-closed） | S3 API；SeaweedFS 为私有化候选 | 无 S3 后端；放宽配置会破坏受控 Locator/Hash 与 fail-closed | TECH-04 七项全过 + 同一 Port | 任一不过→维持 LocalFS；禁止失败回落 | BLOCKED | — |
| TC-5 | Shot 域形态 | Shot 在 project_planning，归属 Scene→Episode→Project（R16~19） | 独立 services/shot_planning + 独立 schema | 物理拆分不得改归属链；不得双写/双 Repository/双事实源 | SHOT-01 归属裁决方案获批 | 无双写且真实 PG 数据对账通过才切换 | BLOCKED | — |
| TC-6 | Agent 框架 | agents/ 预留空结构；Phase 1 不开放权威写入（R26） | LangGraph（Phase 2） | 违反执行包 §2.5（Phase 1 不提前引入） | Phase 1 主链 Owner 验收 + R26 前置实施 ADR | 前置不满足→不立项 | BLOCKED | — |
| TC-7 | 独立消息代理 | PostgreSQL Outbox + Relay（已实现） | RabbitMQ / NATS / Kafka | 执行包 §2.6 首期不引入；Temporal Task Queue ≠ 业务事件总线 | 有容量或消费者隔离的量化证据 + 独立 ADR | 无证据→维持 Outbox | BLOCKED | — |

## 官方版本发布事实（外部 E1 取证，非「可引入」结论）

| ID | 事实 | 状态 | 证据 | 核验日 |
|---|---|---|---|---|
| F-1 | PostgreSQL 18.7 于 2026-11-12 随官方公告发布（含 19 RC2，GA 计划 11-19） | VERIFIED [E1] | postgresql.org 官方公告（见 ADR-R44 §3） | 2026-11-23 |
| F-2 | Valkey 9.2.0 于 2026-11-19 发布，minor 版本，含新数据格式与少量行为变更 | VERIFIED [E1] | valkey.io 官方博客（见 ADR-R44 §3） | 2026-11-23 |
| F-3 | Valkey 兼容 Redis OSS 7.2-；Redis CE 7.4+ 数据文件不兼容 | VERIFIED [E1] | valkey.io/topics/migration（见 ADR-R44 §3） | 2026-11-23 |

## 裁决门槛（统一，缺一即维持默认）

1. 真实交互 + 故障注入运行证据 `[E5]`（非 Mock、非仅容器启动）；
2. 独立 ADR 修订 + Owner 签署；
3. 冻结清单 / 契约派生文件同步重签；
4. 不与领域重构捆绑在同一 PR。

> 本矩阵中所有 `BLOCKED` 项不等于 PASS，不构成任何实施授权。Owner 逐项填写「Owner 裁决」栏并满足四项门槛后，状态方可更新为 DECIDED。
