# ADR-R44：技术与基础设施基线（完整草案·待 Owner 裁决）

- 状态：**PROPOSED（草案，未定版、未发布、未批准；不构成决策，不得作为实现依据）**
- 日期：2026-11-23
- 版本沿革：本文件**合并取代**原 `ADR-R44-technology-baseline.PROPOSED.md`（边界裁定版，PR #60）。两份同为 R44，按「一个 ADR 一个权威源」收敛为一份完整版；旧文件已删除，不并存。旧版 D1–D6 边界裁定与 TC-1~5 裁决栏全部承继于此，无内容丢失。
- 触发：项目所有者下发《AI Creative Media Industrial OS 完整开发实施方案》（仓库根目录 `总体实施蓝图.txt`，UNTRACKED 输入证据）及《开发启动执行包 v1.0》，要求据其形成技术基线决议（TECH-00）。
- 关联：V2.0.5 冻结基线；ADR-R42（资产登记 Saga，DECIDED）；ADR-R43（画布边界，DECIDED）；ADR-R40（统一授权模型，PROPOSED）；ADR-R41（迁移谱系边界，DECIDED）；
  `docs/audits/2026-11-23-blueprint-vs-repo-reconciliation.md`（方案对账事实依据）；
  `docs/audits/2026-11-23-current-main-baseline.md`（BOOT-00 基线）；
  `docs/audits/2026-11-23-r42-implementation-reconciliation.md`（BOOT-01 审计）；
  `docs/architecture/technology-decision-matrix.md`（本 ADR 的机读决策矩阵）
- 指纹：本正文**不内嵌自身哈希**。若日后纳入冻结产物，按 append-only 口径另建 `ADR-R44-VERSION.txt` 采集登记；定版顺序固定为「内容定稿 → 一致性校验 → 采集指纹 → 登记」（R40 CORRECTION-2 教训）。**未定版前不登记指纹。**
- 范围：本 ADR 只做**基线边界裁定 + 技术选型裁决项登记 + 各候选 PoC 门槛定义**；不改契约、不改数据模型、不引入依赖、不激活任何基础设施升级、不放行任何 PR。
- 计数口径：ADR 记录决策不记录进度。凡「已完成 N 项 / 覆盖率」一类计数一律指向 `docs/audits/` 台账，本正文不内嵌。

---

## 1. Why（为什么需要这份 ADR）

方案把若干候选技术写进「推荐目标架构表」，同时又声明「尚需 Owner 正式批准技术 ADR、不把候选当已批准生产依赖」。当前仓库与方案之间存在**会改变执行模型和数据权威的分歧**：若实现方据方案表直接引入 Temporal / PostgreSQL 18 / Valkey / S3，会在没有裁决的情况下制造第二套长流程执行引擎、触发全链 RLS/分区复验、改变限流故障语义、并迫使 `local://` 定位器契约重签。

BOOT-00 已用实测纠正执行包三处自述（仓库树条目口径、CI run 归属、README Broker 表述）——再次证明**候选版本与官方发布事实必须逐项独立取证，不能采信文档转述**。

根因不是「选哪个组件更好」，而是**「目标架构表」没有权威状态标记**：候选与已批准在一份文档里同形同列，读者无法分辨。本 ADR 的职责是把每项技术的状态显式化（已批准现状 / 候选待 PoC / 冲突待裁决），锁定裁决前行为——**维持现状**——并为每个候选给出可证伪的 PoC 门槛与退出方案。它不替 Owner 做选择。

## 2. 边界裁定（承继旧版 D1–D6，仍有效）

| 编号 | 裁定 | 性质 |
|---|---|---|
| D1 | 方案与执行包**不是**可直接执行的工程指令，只是蓝图 + 现状复述；不得据其直接修改契约、迁移、冻结清单或引入依赖 | 边界裁定（可执行） |
| D2 | 裁决前，仓库**维持现状**：PostgreSQL 16、Redis 7、`local://` 内容寻址、Outbox Saga 长流程、无 Temporal 服务 | 冻结现状裁定（可执行） |
| D3 | 所有技术分歧（§4）标记为 **BLOCKED，需 Owner 逐项裁决**；实现方不得自行采纳方案表中的任何候选 | 治理裁定（可执行） |
| D4 | 方案中与既有铁律**同向**的原则（ADR/契约冻结、事实单源、资产不可覆盖、终态不可复活、审计与补偿、Agent 不越权、验收四态）直接继承，不新立平行规范 | 继承裁定（可执行） |
| D5 | 任何技术选型引入必须走独立 ADR 修订 + 冻结清单重签；不得与领域重构捆绑在同一 PR | 流程裁定（可执行） |
| D6 | 本 ADR 不批准任何阶段、不放行任何 PR、不授权任何数据库写操作 | 范围裁定（可执行） |
| D7 | 本版合并取代旧 PROPOSED 草案，同号 ADR 只保留一份；旧版内容全部承继，删除不等于作废历史裁定（旧版从未定版、未登记指纹） | 版本沿革裁定（可执行） |

## 3. 官方发布事实（已独立取证，非采信方案转述）

> 下表仅为**版本发布的客观事实**取证，不构成「可引入本项目」的结论。证据等级按核验标注规范：E1=文档、E2=仓库、E5=运行证据。官方网页属外部文档证据 `[E1]`，PoC 门槛详见 §5。

| 编号 | 事实 | 取证结论 | 证据 | 对本项目的含义 |
|---|---|---|---|---|
| F-1 | PostgreSQL 18.7 发布 | `[V-VERIFIED][E1]` PostgreSQL 官方公告 2026-11-12 列出 18.7/17.12/16.16/15.20/14.25 与 19 RC2；PG19 GA 计划 2026-11-19 | https://www.postgresql.org/about/news/postgresql-187-1712-1616-1520-1425-and-19-rc-2-released-3442/ | 版本号属实；是否采用取决于 TECH-01 全链复验，见 TC-2 |
| F-2 | Valkey 9.2.0 发布 | `[V-VERIFIED][E1]` Valkey 官方博客 2026-11-19，9.2 为 minor 版本，含 forkless 快照/压缩复制/ACL roles 等新特性，并有少量行为变更（如 `ZRANGEBYSCORE` 边界校验、active expiration 计入 dirty counter、lz4/zstd RDB 降级注意） | https://valkey.io/blog/valkey-9.2-release/ | 发布仅数日且含新数据格式；执行包「首期 9.1.4 作基准、9.2.0 独立测试」策略成立，见 TC-3 |
| F-3 | Redis→Valkey 兼容边界 | `[V-VERIFIED][E1]` 官方迁移指南：Valkey 兼容 Redis OSS 7.2 及更早（协议/配置/RDB/AOF/RESP2/RESP3/redis-cli）；Redis CE 7.4+ 数据文件与 Valkey 不兼容 | https://valkey.io/topics/migration/ | 当前浮动标签 `redis:7` 的实际小版本是迁移可行性的前置条件，必须先固化为 Digest 再判，见 TECH-02 |

**未取证，不得推断**：当前 `redis:7` 镜像的实际小版本 / Digest、`valkey_version` 行为、V2.0.5 冻结清单对组件的约束细节——这些是 TECH-02/PR 实施时必须在隔离环境实读的项，标 `[V-UNVERIFIED]`。

## 4. 待 Owner 裁决的技术分歧（BLOCKED，逐项）

> 状态标注为裁决前的默认；Owner 裁决写入矩阵 `docs/architecture/technology-decision-matrix.md` 与本文件裁决记录栏，未填即维持 BLOCKED。事实依据见对账报告与 BOOT-00。

| 编号 | 议题 | 方案/执行包主张 | 仓库现状（BOOT-00 实测） | 候选选项 | 裁决前默认 | Owner 裁决记录（待填） |
|---|---|---|---|---|---|---|
| TC-1 | 长流程执行引擎 | Temporal 作持久化执行核心（PR-06） | 已装 `temporalio` SDK；无 Temporal 服务/workflow；R42 用 Outbox Saga + Worker + Relay 实现异步流程与补偿 | (a) 维持 Saga、Temporal 降为研究；(b) 仅新流程引入、R42 不迁；(c) 全面迁移（须冻结重签） | 维持 Saga，PR-06 不启动 | — |
| TC-2 | 数据库版本 | PG 18.7 作新开发基线候选 | `postgres:16`，RLS/分区/复合外键全链在此版本验证 | (a) 维持 16 + 独立 upgrade PoC；(b) 立项 18 全链复验后切换 | 维持 16 | — |
| TC-3 | 缓存后端 | Valkey 9.1.4 作首期 PoC 候选；9.2.0 独立测试 | `redis:7`（浮动标签，实际小版本未固化）+ redis-py + fail-open 熔断（有 runbook） | (a) 维持 Redis 7；(b) 独立 Valkey PoC，通过后再改默认 | 维持 Redis 7 | — |
| TC-4 | 对象存储 | S3 API + SeaweedFS 候选 | 本地 FS + `local://` 内容寻址，无 S3/MinIO/SeaweedFS；Generation 入口对非 local 明确 fail-closed | (a) 保留 Storage Port，S3 作新 backend 增量接入；(b) 维持本地 FS 至试播后 | 维持本地 FS | — |
| TC-5 | Shot 域形态 | 独立 `services/shot_planning` + 独立 schema | Shot 在 `services/project_planning`，归属走 Scene→Episode→Project（ADR-R16~19 已定） | (a) 物理拆分但不改归属链；(b) 维持现状，推迟到生产图域立项 | 维持现状 | — |
| TC-6 | Agent 框架 | LangGraph（Phase 2） | `agents/` 为预留空结构；ADR-R26 限制 Agent 权威写入 | (a) Phase 1 不引入，前置条件满足后单独立项；(b) 提前 PoC（违反执行包 §2.5） | (a)：Phase 1 不引入 | — |
| TC-7 | 独立消息代理 | 首期不引入 Broker | PostgreSQL Outbox + Relay 已实现；README 未命中「三选一」表述（BOOT-00 实测） | (a) 维持 Outbox，不引入 RabbitMQ/NATS/Kafka；(b) 有容量/隔离证据后单独立项 | (a)：维持 Outbox | — |

**裁决门槛（统一，缺一即视为未裁决、维持默认）**：
1. 有真实交互 + 故障注入的运行证据（`[E5]`，非 Mock、非仅容器启动）；
2. 有独立 ADR 修订与 Owner 签署；
3. 冻结清单 / 契约派生文件同步重签；
4. 不与领域重构捆绑在同一 PR。

## 5. 各候选的 PoC 门槛与退出方案（TECH-01~04）

> 每项均为**条件预设**，不是授权。PoC 在隔离分支/隔离环境进行，不改共享库、不改生产 Compose 默认、不动冻结基线；每项独立记录结果，禁止四项升级后只凭容器启动判 PASS。

### 5.1 PostgreSQL 18（TECH-01）

- **目标**：验证 PG18.7 能否承载 V2.0.5 全链。
- **PoC 通过条件**：① 空库执行全量 Alembic 升级成功；② freeze/contract/architecture/permission/integration 测试全绿；③ 核心表约束、RLS、GRANT、索引、触发器 catalog 逐项对账无回退；④ 真实开发数据迁移 + 恢复演练成功（**严禁直接复用 PG16 数据目录挂载**）；⑤ 记录 PG18.7 发布说明中受影响索引与增量备份注意事项的处置。
- **退出方案**：任一条件不满足 → 维持 PG16，登记 BLOCKED 与失败证据，不为版本新而强行升级。
- **前置**：Owner 对 TC-2 裁决为 (b) 才启动。

### 5.2 Valkey（TECH-02）

- **目标**：只针对现有 RateLimiter 真实用法（INCR + 首次 EXPIRE 固定窗口）验证兼容性。
- **前置取证**：先固化当前 `redis:7` 实际小版本与镜像 Digest；若为 Redis CE 7.4+，按 F-3 数据文件不兼容，须走另法迁移，不得假定平滑。
- **PoC 通过条件**：① 保留 redis-py 客户端，连接/INCR/EXPIRE/超时/重连/连接池关闭/熔断/高并发计数全通过；② 两者各跑同一矩阵（9.1.4 基准、9.2.0 独立测试）；③ 模拟不可用，确认 fail-open + 熔断语义准确、指标可见；④ 评估切换限流窗口计数重置的短暂空档并写明运维窗口；⑤ 无授权语义回退。
- **退出方案**：任一不通过 → 维持 Redis 7。禁止同时更换服务端、客户端与限流算法。
- **前置**：Owner 对 TC-3 裁决为 (b) 才启动。

### 5.3 Temporal（TECH-03）

- **目标**：最小持久化 PoC，不接管现有 Generation 生产状态机。
- **PoC 通过条件**：① 本地 Temporal CLI dev server + Python SDK 连通；② 最小 Workflow+Activity 验证服务重启、Worker 重启、Activity retry、history 与查询；③ Workflow 输入只含稳定业务引用，不复制业务事实进 Payload；④ Activity 只调领域公开接口，不直写他域表；⑤ Workflow ID / Run ID / 幂等键语义明确；⑥ 生产拓扑用独立持久化库与凭证，不复用平台业务 Schema。
- **退出方案**：不可恢复执行或改变领域事实权威 → 不进入生产接线，Temporal 降为研究。
- **前置**：Owner 对 TC-1 裁决为 (b)/(c) 才启动；(c) 还须 R42 正式修订。

### 5.4 S3 兼容存储（TECH-04）

- **目标**：扩展现有 `ObjectStoragePort`，不新建第二套 Storage API。
- **PoC 通过条件**：① 复用并审查 Port 与 `local://` locator 规则；② 候选 Adapter 实现 put/get/exists（**不加 delete**）；③ 验证条件写入/防覆盖、SHA-256、对象大小、媒体类型、完整读取；④ 验证 Multipart 中断/重试/部分对象清理/版本策略；⑤ 注入网络中断、权限拒绝、对象缺失、Hash 不匹配、服务恢复；⑥ 登记前不产生可被 QC/Delivery 消费的正式资产引用；⑦ 对比 SeaweedFS 与成熟云 S3，不以「API 兼容」单独判 PASS。
- **退出方案**：任一不通过 → 维持 LocalFS；Generation 入口对非 local 保持 fail-closed，禁止「尝试 S3 失败回落 LocalFS」。
- **前置**：Owner 对 TC-4 裁决为 (a) 才启动。

## 6. 与既有 ADR / 基线的关系

- **不覆盖**：本 ADR 不改变 R42（Saga 已 DECIDED）、R43（画布边界已 DECIDED）、R41（迁移谱系已 DECIDED）、R40（授权模型 PROPOSED，阶段 3 不放行）的任何结论。TC-1 若选 (c)，须走 R42 正式修订，而非本草案直接改写。
- **不新立规范**：D4 继承的治理原则其权威仍在 V2.0.5 与已批准 ADR；本 ADR 不定义第二套铁律编号（遵循核验标注规范 §2.3 对自造 IR 编号的禁令）。
- **职责边界落实执行包修正**：不迁 `apps/api`、不按目录树批量建空模块、不重建 AssetRegistrar/QC/Budget、不把 Temporal 当 Saga 替代、ComfyUI 按 R43、首期不引入 Broker、Phase 1 不开放 Agent 权威写路径。
- **证据时效**：本 ADR 引用报告是 main @ `abf77c1…`（BOOT-00）与合并后基线的时点判断；基线推进后须重新对账，不得沿用旧报告结论充当新 SHA 的事实。

## 7. 后果

**正面**：把「目标架构表里的候选」与「已批准现状」分开，堵住据蓝图直接引入组件的路径；把会改变执行模型/数据权威的分歧集中到一处等待裁决；为每个候选给出可证伪的 PoC 门槛与退出方案；官方发布事实独立取证，不被执行包转述带偏。

**代价**：阶段 B 基础设施升级在裁决前不启动；PG18/Valkey/S3/Temporal 潜在收益延后。这是刻意接受的——先守住单一事实源与冻结治理，再谈组件升级。

**风险**：若有人把「候选选项」误读为「已批准可实施」，会同时破坏 D2/D3/D5 与冻结清单。判据是：§4 任一 TC 裁决栏未填且未走冻结重签而现状已变，即视为违反本 ADR。

## 8. 生效条件

本 ADR 目前为草案。仅在以下条件全部满足后才转为 DECIDED：

1. Owner 对 §4 七项 TC 逐项签署裁决（填入裁决栏与决策矩阵）；
2. 内容定稿后另建 `ADR-R44-VERSION.txt`，按「定稿 → 校验 → 采集 → 登记」顺序登记指纹（正文不内嵌）；
3. 对账报告与 BOOT 报告以当期 main SHA 重出，确认无新增冲突；
4. 若裁决涉及契约/迁移变更，冻结清单重签完成。

**未满足以上任一条，本文件保持 PROPOSED 草案状态，不得被引用为决策依据。**
