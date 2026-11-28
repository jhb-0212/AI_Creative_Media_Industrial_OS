# 完整开发实施方案 vs 仓库现状对账（只读核验）

> 依据：《核验与审计标注规范 V1.0》（`docs/standards/verification-and-audit-annotation-standard.md`）
> 性质：**只读对账**。本报告期间未修改任何业务代码、契约、迁移或数据库。
> 基线：main @ `abf77c18ed48dcabf55492acf9b0c8dddc967022`（本地 `git rev-parse main` 实测）
> 输入源：仓库根目录 `总体实施蓝图.txt`（= 项目所有者本次下发的《AI Creative Media Industrial OS 完整开发实施方案》全文）
> 输入源指纹：[V-VERIFIED][E2] 1510 行 / SHA-256 `dfcdeb2e4df252773007c77a5e3aa7f21998021ff081b7380f06ee540c72f12f` / 当前 **UNTRACKED**（未纳入版本控制，仅为工作区输入证据；复核命令 `sha256sum 总体实施蓝图.txt && wc -l 总体实施蓝图.txt`）
> 定位：方案自述"开发实施基线建议，尚需 Owner 正式批准技术 ADR，没有执行代码修改"。本对账据此**只核对事实与合规缺口，不替 Owner 裁决，不擅自改变冻结基线**。

---

## 0. 证据边界（先声明，避免结论被误读）

- `[V-*]` 核验标注回答"事实是否成立"；`[A-*]` 审计标注回答"是否符合基准"。两者不合并为单一 PASS/FAIL。
- 本报告中的"对账表"（§3）是**人工逐条判断**，不是 `scripts/verification_battery.py` 派生的 C1–C7 核验电池；C1–C7 的当期结果一律引用历史电池报告并标明其 SHA，不在此重算、也不冒充当期结论。
- **NOT RUN** 一律不视为通过；**BLOCKED**（需 Owner 裁决）与 **FAILED**（反向证据）分开登记。
- 主要证据等级：`[E1]` 文档主张（含本方案自身）；`[E2]` 代码/配置/契约/迁移/哈希复算；`[E3]` 静态检查；`[E4]` 测试执行记录；`[E5]` 真实 PG/服务运行。

---

## 1. 结论摘要（先给判断）

| 维度 | 结论 | 标注 |
|---|---|---|
| 方案定位 | 蓝图 + 现状复述，**不是可直接执行的代码指令**；其自身声明需 Owner 批准 | `[V-VERIFIED][E1+E2]` |
| 与现状的关系 | 约 1/3 条目已完成或基本完成，约 1/3 部分完成，约 1/3 为缺口；3 项技术选型与冻结基线**冲突** | 见 §3/§4 |
| 技术选型冲突 | PostgreSQL 18 / Valkey / Temporal 三项须 Owner 裁决后方可动；不得由实现方自行采纳 | `[V-BLOCKED][E1]` + `[A-RISK][P1-HIGH]` |
| 治理一致性 | 方案的 ADR 治理、契约冻结、事实单源、资产不可覆盖/终态不可复活等原则与仓库铁律**同向** | `[A-COMPLIANT][E1+E2]` |
| 冻结基线 | 本报告**未修改**基线文件、冻结清单、契约、ADR 状态或任何迁移 | `[V-VERIFIED][E2]` |

---

## 2. 现状硬事实（对账的判据来源）

| 项 | 实测值 | 标注 |
|---|---|---|
| main SHA | `abf77c18ed48dcabf55492acf9b0c8dddc967022` | `[V-VERIFIED][E2]` |
| 架构基线 | V2.0.5（冻结，`docs/baseline/`） | `[V-VERIFIED][E2]` |
| 迁移链 | 51 个 versions 文件；`alembic heads` = `e1f2a3b4c5d6 (head)`（单 head，R42 Saga 迁移） | `[V-VERIFIED][E2+E5]` |
| 数据库 | `postgres:16`（`infra/compose/docker-compose.yml:71`） | `[V-VERIFIED][E2]` |
| 缓存 | `redis:7`（同上 `:88`）；限流 fail-open + 熔断（`docs/runbooks/rate-limit-degradation.md`） | `[V-VERIFIED][E2]` |
| Temporal | 仅 SDK 依赖 `temporalio 1.36.0`（`.venv`）；**无 temporal 服务、无 workflow 代码、无 workers/temporal** | `[V-VERIFIED][E2]` |
| 对象存储 | 本地 FS + `local://<sha2>/<sha2>/<sha256>.<ext>` 内容寻址（`adapters/storage/local_fs.py`、`packages/shared/storage_locator.py`）；**无 MinIO/SeaweedFS/S3 部署** | `[V-VERIFIED][E2]` |
| 可观测性 | OTel tracing + metrics（`packages/telemetry/`、`packages/shared/metrics.py`）；**无 Prometheus/Grafana 部署清单** | `[V-VERIFIED][E2]` |
| ADR 体系 | `docs/adr/` 下 R06–R43 系列；R42=DECIDED、R43=DECIDED、R40 系列=PROPOSED（多版本并存）、R27 有 `.PROPOSED.md` 与正本并存 | `[V-VERIFIED][E2]` |
| 有代码的服务 | project_planning(49) / asset_service(20) / qc_governance(15) / production_planning(14) / budget_quota(12) / identity_access(5) / identity_issuance(1)（按 .py 文件数） | `[V-VERIFIED][E2]` |
| 空服务目录 | rights_governance / production_policy / orchestrator / observability / model_gateway / memory_learning / creative_exploration / blocking_service（8 个） | `[V-VERIFIED][E2]` |
| 有代码的 adapter | model_gateway(6) / storage(3)；其余 5 个 adapter 目录为空 | `[V-VERIFIED][E2]` |
| 前端 | `apps/` 下仅 `api`；**无 `apps/console`** | `[V-VERIFIED][E2]` |
| CI | 4 job（contract-check / image-build / lint-and-test / permission-negative-tests）于 PR #59 全部通过；main 合并后新 run `45145570659` 排队中（时点判断） | `[V-VERIFIED][E2]`（附时点） |

---

## 3. 二十一个 PR 逐条对账（人工判断）

标注口径：`已落地`=有实现且有交付证据；`部分`=框架/骨架在但闭环或证据不全；`缺口`=基准要求明确而实现未见；`冲突`=方案主张与冻结现状或已定 ADR 不一致，需 Owner 裁决；`NOT RUN`=无执行证据。

| 方案 PR | 方案要求 | 现状核对 | 结论 | 标注 |
|---|---|---|---|---|
| PR-00 Owner 基线决议 | main SHA、ADR 清单、Phase 0 冻结裁定、ADR-R44 | 已有 PR-00 核验报告（基于旧 SHA `b30d361`）；Phase 0 仍"条件通过"，无条件签署原件缺失；ADR-R44 本轮草案待签 | 部分（SHA 已过期需重签） | `[V-PARTIAL][E1+E2]` |
| PR-01 ADR/契约对账 | ADR 状态矩阵、118↔24 映射、契约依赖图 | PR-01/PR-02/PR-03 报告已存在；但 §5 四项 BLOCKED（映射规则、R40 版本收敛、状态矩阵、R27 后缀）未裁决 | 部分 | `[V-PARTIAL][E1+E2]` |
| PR-02 数据库与权限底座 | 独立角色/RLS/复合归属/备份恢复 | 六业务域角色、RLS、复合归属外键、隔离项目已落地（`docs/audits/mig-recon/`）；PG18 升级属另议 | 已落地（PG 版本维持 16） | `[V-VERIFIED][E2+E5]` |
| PR-03 基础设施选型落地 | PG18 PoC / Valkey / S3 / Temporal / OTel | OTel 已在；PG18/Valkey/S3/Temporal 均无 PoC 或部署；方案自身要求"真实交互与故障测试证据" | 缺口 + 冲突 | `[A-GAP][P1-HIGH]` + §4 |
| PR-04 API/Worker 生命周期 | 统一 start/execute/shutdown 接口、优雅停机 | apps/api + 多 worker 有生命周期，但无统一 Worker 接口规范；无 console | 部分 | `[V-PARTIAL][E2]` |
| PR-05 Outbox/Relay 可靠性 | 事务性 outbox、重放、死信、租户隔离 | relay + retry 列 + webhook 熔断 + 分区保留 + 死信/replay 审计均在（R06/R12/R13） | 已落地 | `[V-VERIFIED][E2+E5]` |
| PR-06 Temporal 持久化执行 | Temporal workflow 承载生成流程 | 仅 SDK，无 service/workflow；R42 已用 Outbox Saga 实现异步长流程 | **冲突**（双执行引擎风险） | `[A-RISK][P1-HIGH]` + §4 |
| PR-07 CI/真实 PG 基线 | 全套门禁 + 真实 PG | 4 job CI 全绿；真实 PG 故障注入已补（PR-E `291300a`） | 基本落地 | `[V-VERIFIED][E2+E4+E5]` |
| PR-08 Project Planning | Project/Episode/Script/版本/审批 | 49 文件，主链最完整域 | 已落地 | `[V-VERIFIED][E2]` |
| PR-09 Shot Planning 独立拆分 | 独立 services/shot_planning + 独立 schema/API/权限 | Shot 仍在 project_planning 内；ADR-R16~19 定归属走 Scene→Episode→Project，无独立 domain；方案与既有裁定形态不一致 | **冲突**（拆分形态需裁决） | `[A-RISK][P2-MEDIUM]` |
| PR-10 Production Graph | Graph/Node/Edge/Snapshot，非画布 | 无独立 production_graph 域；`orchestrator` 空目录；方案"不是 ComfyUI 节点画布"与 ADR-R43 同向 | 缺口（边界一致） | `[A-GAP][P2-MEDIUM]` |
| PR-11 R42 C1–C7 契约修订 | C1–C7 七项 | C1–C7 已修订，C4/C7 本轮合并（`fa42465`/`abf77c1`） | 已落地 | `[V-VERIFIED][E2]` |
| PR-12 AssetRegistrar | T0–T3 Saga、故障注入、幂等 | T0–T3 落地（`716971c`/`e95127a`/`291300a`），真实 PG 故障注入已补；T2 幂等冲突审计 + PREPARED 恢复为已知待办 | 基本落地（第三批待办） | `[V-VERIFIED][E2+E5]` |
| PR-13 Model Gateway + Generation | 一种真实图像 Provider 闭环 | model_gateway adapter(6) + workers/generation 有代码；**无真实 Provider 调用证据（E5）** | 部分 | `[V-PARTIAL][E2]`（E5 未取） |
| PR-14 QC Governance | 五维度、报告、Gate、返工闭环 | qc_governance(15) + workers/qc；QC 入队已收敛为单一事件路径；五维度/返工闭环完整度未逐项核 | 部分 | `[V-PARTIAL][E2]` |
| PR-15 Budget/Quota | 追加式账本、并发冻结/释放/冲销 | budget_quota(12) + settlement 迁移；并发与冲销的真实 PG 验收证据未定位 | 部分 | `[V-PARTIAL][E2]` |
| PR-16 Rights/Delivery | 权利治理 + 交付回执闭环 | `rights_governance` 空目录；`workers/delivery` 空目录 | 缺口 | `[A-GAP][P2-MEDIUM]` |
| PR-17 真实项目试播 | 5–10 镜头全链真实生产 | 无试播执行记录 | NOT RUN（Phase 1 验收项） | `[V-UNVERIFIED][E1]` |
| PR-18 Agent Harness | LangGraph + 工具权限 | `agents/` 空目录；ADR-R26=已决策未实施 | 缺口 | `[A-GAP][P2-MEDIUM]` |
| PR-19 Creative Intelligence | Character/World/Style Bible + Memory | `memory_learning` 空目录 | 缺口 | `[A-GAP][P2-MEDIUM]` |
| PR-20 Creator Ecosystem / 专业执行 | 社区/市场/渲染农场 | 方案自身排在 Phase 2–4，当前无 | 缺口（非本期） | `[V-NA][E1]` |

统计（人工计数，仅描述本次对账，不写回 ADR）：已落地/基本落地 8 项，部分 6 项，缺口 5 项，冲突 2 项（PR-03/PR-06，PR-09 形态分歧），NOT RUN 1 项，不适用 1 项。

---

## 4. 技术选型冲突登记（需 Owner 裁决，BLOCKED）

> 这些不是"实现细节"，而是会改变执行模型、数据权威和冻结基线的架构决策。方案自身亦称 Valkey/PG18 为"候选/PoC 后锁定"。**裁决前一律维持现状，不得借本方案直接改动。**

| 编号 | 方案主张 | 仓库现状（硬事实） | 风险/根因 | 建议待裁决选项 | 标注 |
|---|---|---|---|---|---|
| TC-1 | Temporal 作"持久化执行"核心，PR-06 承载生成流程 | 无 Temporal 服务/workflow；R42 已用 Outbox Saga + Worker + Relay 实现异步长流程与补偿 | 双持久化执行引擎 ⇒ 同一"长流程状态"两个权威源，违反"一个事实一个权威源"；且方案未说明与 R42 Saga 的边界 | (a) 维持 Saga，Temporal 降为后备研究；(b) 仅对新流程引入、R42 链不迁；(c) 全面迁移（须 ADR 修订 + 冻结重签） | `[V-BLOCKED][E1+E2]` `[A-RISK][P1-HIGH]` |
| TC-2 | PostgreSQL 18.x，"从现有版本评估升级" | `postgres:16`，迁移链/RLS/分区均在当前版本验证 | 升级涉及 RLS/分区/复合外键全链复验，与"不为替换而重写"原则张力 | (a) 维持 16，单独立 upgrade PoC 子 PR；(b) 立项 18 并完成全链复验后再切 | `[V-BLOCKED][E1+E2]` |
| TC-3 | Valkey "优先候选，PoC 后锁定" | `redis:7` + redis-py + fail-open 熔断 runbook | 缓存故障语义（fail-open）已被限流设计依赖；替换须协议兼容/TTL/Lua/锁/恢复/故障注入全套证据 | (a) 维持 Redis 7；(b) 独立 Valkey PoC，通过后再改默认，不与领域重构同 PR | `[V-BLOCKED][E1+E2]` |
| TC-4 | S3 兼容对象存储负责媒体产物 | 本地 FS + `local://` 内容寻址，无 S3/MinIO/SeaweedFS | 定位器契约已绑定 `local://`；换 S3 须改 `storage_locator` 契约 + 冻结重签 | (a) 保留 Storage Port，S3 作为新 backend 增量接入；(b) 维持本地 FS 至试播后再定 | `[V-BLOCKED][E1+E2]` |
| TC-5 | 目标目录 `services/shot_planning` 独立域 | Shot 在 `services/project_planning`，归属走 Scene→Episode→Project（ADR-R16~19 已定） | 目录形态与既有 ADR 不一致；方案自身亦声明"不机械搬迁" | (a) 维持归属规则，仅做物理拆分且不改归属链；(b) 维持现状，推迟到生产图域立项时一并裁 | `[V-BLOCKED][E1+E2]` |

### 4.1 裁决状态复核（2026-11-28）

原表记录于 2026-11-23，**早于同日 ADR-R44 的签署**，因此「TC-1 待裁决」这一状态已被后续裁决覆盖。复核结论如下（不改原表，只记状态）：

| 编号 | 当前状态 | 依据 |
|---|---|---|
| TC-1 | **已裁决 (b)：仅新流程引入 Temporal，R42 不迁**；本次不做新裁决、不改依赖、不部署 Temporal 服务 | `docs/adr/ADR-R44-technology-and-infrastructure-baseline.md` §4（所有者 2026-11-23 签署，DECIDED）。R44 D3 明确禁止把该结论扩大为「迁移 R42」。Saga 能力取证见 `docs/audits/2026-11-28-durable-execution-boundary-review.md` |
| TC-2 ~ TC-5 | 仍为 R44 已裁方向（PG18 PoC 前维持 16 / Valkey PoC / S3 经 Storage Port 增量接入 / Shot 独立域），PoC 状态均 NOT RUN | 同上；本日未动任何组件 |
| PR-06 | 「已裁决（(b)），非缺口」：R42 链不迁；新流程若用 Temporal 须独立 ADR 且不与 R42 共享状态 | 同上 |

A-001 的验收标准（「明确写出 Saga 与 Temporal 边界的已批准 ADR」）已由 ADR-R44 TC-1 裁决 + 本复核满足；本次未改契约与迁移，故不涉及冻结重签。

---

## 5. 审计发现（每项按十字段格式；根因非症状复述）

### A-001
1. 编号：A-001
2. 审计结论：`[A-RISK]` 方案把 Temporal 列为阶段 B 阻塞前置，而 R42 已用 Saga 承担同一"长流程执行"职责
3. 严重程度：`[P1-HIGH]`
4. 基准条款：V2.0.5 单一事实源原则；ADR-R42（已 DECIDED，支付 Saga 补偿路径）
5. 工程铁律：`[IR-1.3.2-1]`（架构无屎山/无重复权威源）
6. 实际证据：§2 Temporal 行（`.venv` 仅有 SDK）；`docs/adr/ADR-R42-asset-registration-saga.md` 状态 DECIDED；迁移 `e1f2a3b4c5d6` 为 head
7. 根本原因："长流程状态由谁持久化"这一事实在方案与 R42 之间出现两个候选权威，方案未定义二者边界
8. 影响范围：PR-06 及全部依赖它的后续 PR；若两引擎并存，补偿与恢复路径将分叉
9. 系统性整改方案：Owner 在 §4 TC-1 三选项中裁决；裁决前 PR-06 不启动，R42 链保持不变
10. 验收标准：形成一份明确写出"Saga 与 Temporal 边界"的已批准 ADR，且契约/迁移冻结清单同步重签

### A-002
1. 编号：A-002
2. 审计结论：`[A-GAP]` 方案的三项基础设施升级（PG18/Valkey/S3）在仓库无 PoC 与故障测试证据
3. 严重程度：`[P1-HIGH]`
4. 基准条款：方案 §PR-03 "不允许只凭容器启动成功判定通过，必须有真实服务交互和故障测试证据"
5. 工程铁律：`[IR-1.3.2-4]`（保持高技术标准：真实运行证据）
6. 实际证据：§2 数据库/缓存/对象存储三行均为现状实测；无 MinIO/Valkey 部署清单
7. 根本原因：候选技术被写进"目标架构表"但未走 PoC 门槛，存在把候选误当已批准依赖的风险（方案自身已预警此点）
8. 影响范围：阶段 B 基础设施、后续所有依赖存储/缓存的域
9. 系统性整改方案：每项独立 PoC 子 PR，产出真实交互 + 故障注入 + 版本锁定证据后再裁决；不捆绑升级
10. 验收标准：每项有 E5 运行证据与明确版本/Digest，且未与领域重构同 PR

### A-003
1. 编号：A-003
2. 审计结论：`[A-PARTIAL]` 既有 PR-00/PR-01 报告的基线 SHA 已过期，ADR 状态矩阵与映射规则仍为 BLOCKED
3. 严重程度：`[P2-MEDIUM]`
4. 基准条款：方案 §PR-00/PR-01；《核验与审计标注规范 V1.0》§1.4 证据时效
5. 工程铁律：`[IR-1.3.2-4]`（证据时效与可复现）
6. 实际证据：`docs/audits/2026-11-22-pr00-repo-baseline-verification.md` 基线 `b30d361…` ≠ 当期 `abf77c1…`；PR-01 §5 四项 BLOCKED
7. 根本原因：把核验报告当成长期属性而非时点判断；基线推进后未重签
8. 影响范围：PR-00/PR-01 的验收结论不可直接沿用到当前 main
9. 系统性整改方案：以当期 SHA 重出 PR-00 基线事实；ADR 状态矩阵待 Owner 批准后补建（不擅自建第二套编号）
10. 验收标准：新报告基线 SHA = 当期 main；BLOCKED 项逐条有 Owner 裁决或明确保留 BLOCKED

---

## 6. 整改追踪（本对账仅登记，不实施）

| 编号 | 对应审计问题 | 整改方案 | 整改状态 | 验收标准 | 验收证据 |
|---|---|---|---|---|---|
| R-001 | A-001 | Owner 裁决 Saga/Temporal 边界并 ADR 化 | `[R-OPEN]`（待 Owner） | 已批准 ADR + 冻结重签 | 待补 |
| R-002 | A-002 | PG18/Valkey/S3 各出独立 PoC 子 PR | `[R-OPEN]`（待裁决是否立项） | 每项 E5 运行证据 + 版本 Digest | 待补 |
| R-003 | A-003 | 按当期 SHA 重出 PR-00 事实；补 ADR 状态矩阵 | `[R-PLANNED]`（本轮已产出对账报告与 ADR-R44 草案，状态矩阵待 Owner 批） | 基线 SHA 一致 + BLOCKED 逐条裁决 | 本报告 §2 |

---

## 7. 最终结论：九项状态

| 标注 | 取值 | 支撑证据 | 未覆盖面 | 升到 VERIFIED 缺什么 |
|---|---|---|---|---|
| `[FREEZE-STATUS]` | PARTIAL | §2 基线/迁移 head 实测 | Phase 0 无条件签署原件 | Owner 签署决议 |
| `[CONTRACT-STATUS]` | VERIFIED | PR #59 CI contract-check 通过 | 本方案引入的新契约（Temporal/S3）未定义 | 相关契约修订 + 重签 |
| `[ARCHITECTURE-STATUS]` | PARTIAL | §3 对账；§4 五项冲突登记 | Saga/Temporal 边界未裁决 | 已批准 ADR（TC-1） |
| `[IMPLEMENTATION-STATUS]` | PARTIAL | §3：8 落地/6 部分/5 缺口 | Rights/Delivery/Agent/Memory 等空目录 | 对应 PR 实际实现 |
| `[TEST-STATUS]` | PARTIAL | PR #59 四 job 全绿；PR-E 真实 PG 故障注入 | 本方案新增基础设施无测试证据 | 各项 PoC 故障注入证据 |
| `[RUNTIME-STATUS]` | PARTIAL | §2 alembic head=E2+E5 | 真实 Provider/对象存储/S3 运行证据 | 试播 PR-17 的 E5 证据 |
| `[SECURITY-STATUS]` | VERIFIED | permission-negative-tests job 通过（PR #59） | 本方案新增域授权矩阵未定义 | 新域权限负向测试 |
| `[REMEDIATION-STATUS]` | OPEN | §6 三项均 OPEN/PLANNED | 无整改已 VERIFIED | R-001/R-002 完成验收 |
| `[RELEASE-STATUS]` | BLOCKED | Phase 0 条件通过；试播 NOT RUN | 无 Phase 1 发布结论 | PR-17 全链证据 + Owner 签署 |

---

## 8. 证据边界声明（如实）

- 本报告的"现状"均来自 main @ `abf77c18…` 的工作区实测与当期 CI 时点；**未**重跑 C1–C7 核验电池、**未**连接真实 PG 复验业务数据、**未**执行任何迁移。
- 输入源 `总体实施蓝图.txt` 为 UNTRACKED 工作区文件，其 SHA-256 仅证明本次取证时的字节内容，不构成"已批准基线"；Owner 批准须走正式 ADR 流程。
- ADR-R44 草案（现见 `docs/adr/ADR-R44-technology-and-infrastructure-baseline.md`，机读视图见 `docs/architecture/technology-decision-matrix.md`）仅为待签草案，**未定版、未发布、未批准**，不改变任何既有 ADR 状态或冻结清单。原边界裁定版 `ADR-R44-technology-baseline.PROPOSED.md` 已合并入该完整版并删除（TECH-00，同号 ADR 收敛为一份），内容全部承继。
- 方案 §6.1 "不引入 ComfyUI 节点画布"与 ADR-R43 已裁定边界一致；`adapters/comfyui/` 为空目录，本对账**未**恢复或触碰任何 ComfyUI 未提交改动。
