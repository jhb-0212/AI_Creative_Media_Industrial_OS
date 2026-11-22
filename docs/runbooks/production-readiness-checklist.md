# Phase 1 生产准入清单（可勾选，但无证据不勾选）

> 状态：**未通过**。本清单由第三轮工程审查 P1-2 整理，逐项对应报告列出的准入条件。
> 勾选规则：每项必须有**可复算证据**（报告路径 / 命令输出 / 目标环境实测数据），
> 没有证据就留空——"看起来实现了"不等于"验收通过"。这与工程铁律一致：
> **没有证据就不宣称一致，没有通道就不伪造一致**。
>
> CI 4/4 全绿**不等于**生产准入通过：CI 证明代码正确性与静态治理，
> 不证明目标环境的并发承载、故障恢复与数据可靠性。

## 一、当前事实基线（核实于 2026-11-21）

| 项 | 事实 | 来源 |
|---|---|---|
| CI | 4/4 success（lint-and-test / contract-check / permission-negative-tests / image-build） | `.github/workflows/ci.yml` |
| Phase 1 主链 | 契约 ACTIVE ≠ 代码已实现 ≠ 集成通过 ≠ 生产验收 | README「阶段状态」 |
| E2E | `tests/e2e/test_phase1_main_chain.py` 只覆盖已实现段，Scene→Generation→Delivery 全链未证明 | README 测试口径 |
| 高并发 | 有方案 + 脚本（`docs/perf/performance-acceptance-plan.md` + `scripts/perf/load_test.py`），缺目标环境实测报告 | README |
| 故障恢复 | 缺目标环境验收证据 | README |
| 交付包密钥扫描 | 清单建立时未实现 → 本轮已接 CI（见下 C1） | 第三轮审查 P1-2 |
| Resilience 目录 | 清单建立时不存在 → 本轮已建骨架（见 A3），但目标环境演练未完成 | README / `tests/resilience/README.md` |
| ADR-R40 | PROPOSED，文本冻结，阶段 3 不放行 | `ADR-R40-unified-authorization-model.v1.4.7.md` |
| Shot CANCELLED | 服务层 + 领域层 fail-closed，授权对象未建模，保持不可用 | `shot_service.py` / `domain/shot.py` |

## 二、准入条件（逐项勾选）

### A. 功能与链路

- [ ] **A1 核心主链 E2E 完整走通** — 证据要求：Scene→Generation→Delivery 全链 E2E 在目标环境跑通的完整日志与断言；当前仅有已实现段覆盖，**未勾选**。
- [ ] **A2 产品能力边界已公示** — 证据要求：Shot CANCELLED 不可用、ADR-R40 PROPOSED 等限制在 UI/Action 台账标注为不可用；代码已 fail-closed，但界面公示未核实，**未勾选**。
- [x] **A3 Resilience 测试骨架存在** — 证据：`tests/resilience/` 已建立，含故障传播契约测试（Outbox 故障 fail-closed），有负向路径。**注意：仅骨架，不等于目标环境故障演练通过**。

### B. 运行可靠性（需目标环境，均未勾选）

- [ ] **B1 目标并发 / 峰值 / 持续运行实测报告** — 证据要求：目标环境下的压测报告（并发数、峰值 QPS、持续时长、P99 延迟、错误率）；有方案脚本但无实测，**未勾选**。
- [ ] **B2 Redis 故障演练** — 证据要求：Redis 宕机/超时场景下系统 fail-closed 或降级行为实测 + 恢复验证，**未勾选**。
- [ ] **B3 数据库故障演练** — 证据要求：DB 断连/慢查询场景下的行为与恢复实测，**未勾选**。
- [ ] **B4 Outbox / Worker 故障演练** — 证据要求：消息投递失败、Worker 崩溃后任务补偿与不丢不重的实测，**未勾选**。
- [ ] **B5 备份恢复与数据一致性** — 证据要求：目标环境备份恢复演练 + 恢复后数据一致性校验报告，**未勾选**。

### C. 交付安全

- [x] **C1 交付包密钥扫描接入 CI** — 证据：`scripts/secret_scan.py`（6 条高置信规则 + 可机检豁免，陈旧豁免报红）已挂到 required 的 `contract-check` job；负向测试 `tests/unit/test_secret_scan.py` 8 项。

### D. 架构事实边界

- [x] **D1 数据库谱系边界已裁定** — 证据：`docs/adr/ADR-R41-migration-lineage-decision.md`（main 正式链为唯一部署事实，pending 链为验证载体，禁止合并/同时部署/跨链挪用证据）。
- [ ] **D2 正式链归属验收独立取证** — 证据要求：在 `ai_film_api_tests` 上按归属台账判据重跑并登记结果；分叉库 35/35 不能直接挪用，**未勾选**。

## 三、勾选统计

- 已勾选：A3（骨架）、C1（密钥扫描）、D1（谱系裁定）— 共 3 项
- 未勾选：A1、A2、B1~B5、D2 — 共 9 项
- **结论：未达到生产准入。已勾选的 3 项只是"可机检的治理/骨架"落地，不代表 Phase 1 可放行。**

## 四、解除准入的前置条件（按责任人分组的取证要求）

> 本节把 9 项未勾选项变成可交接的任务单。**本 agent 无目标环境权限，不能代替任何一项实测，
> 也不能代签**；下列只是「谁需要交什么、按什么判据算通过」的对齐口径。
> 每项勾选前必须附上「证据路径 + 命令/环境 + 实测数据」三要素，缺一不勾。

### 功能负责人（A1、A2）

| 项 | 需交付的证据 | 验收判据 |
|---|---|---|
| A1 | `tests/e2e/` 下 Scene→Generation→Delivery 全链 E2E 在**目标环境**跑通的完整日志（含时间戳、环境标识、每条断言结果）；现有 `test_phase1_main_chain.py` 只覆盖已实现段，不算全链 | 全链每一跳都有真实断言通过；缺口段（Scene→Generation→Delivery）不得用 mock 顶替后宣称走通 |
| A2 | UI 与 Action 台账中，Shot CANCELLED 不可用、ADR-R40 PROPOSED 等边界的公示截图 + 对应 action 的 fail-closed 实测（调用返回拒绝而非静默成功） | 每个能力边界都能在界面看到标注，且运行时行为与标注一致 |

### 目标环境负责人（B1~B5）

统一前提：在**目标环境**（非本地、非合成数据）执行，报告须含环境标识、数据集规模、执行时间、原始输出路径。已有方案/脚本：`docs/perf/performance-acceptance-plan.md`、`scripts/perf/load_test.py`、`docs/perf/2026-11-18-single-instance-baseline.md`（单机基线，不等于目标环境实测）。

| 项 | 需交付的证据 | 验收判据 |
|---|---|---|
| B1 | 目标环境并发/峰值/持续运行压测报告：并发数、峰值 QPS、持续时长、P99 延迟、错误率 | 达到性能验收方案约定的阈值；单机基线不得充当目标环境结论 |
| B2 | Redis 故障演练：宕机/超时注入下的 fail-closed 或降级行为实测 + 恢复验证 | 故障期行为符合契约、恢复后数据一致、无静默成功 |
| B3 | 数据库故障演练：DB 断连/慢查询场景下的行为与恢复实测 | 同上；写入类断言须核对影响行数与回读，不接受「未抛异常即 PASS」 |
| B4 | Outbox / Worker 故障演练：消息投递失败、Worker 崩溃后任务补偿实测 | 不丢不重有实测数据（消息数/重试数/终态数对账） |
| B5 | 备份恢复演练：目标环境备份恢复 + 恢复后数据一致性校验报告 | 恢复后数据逐表校验通过；只做备份未做恢复不算通过 |

### 数据库负责人（D2）

| 项 | 需交付的证据 | 验收判据 |
|---|---|---|
| D2 | 在正式链测试库 `ai_film_api_tests` 上，按 `docs/audits/mig-recon/acceptance-ledger.md` 的判据（约束存在 ∧ convalidated ∧ 违规行数=0）重跑归属验收，出具独立台账 | 判据三要素逐行可复算；分叉库 `ai_film_industrial` 的 35/35 不得挪用为正式链结论（见 ADR-R41 D5） |

### 最终签署

- 全部 9 项勾选齐备后，由**项目所有者**签署 Phase 1 生产验收——签署权不在本 agent。
- 当前状态：**未通过**。agent 侧可机检的 3 项（A3/C1/D1）已落地，不改变这一结论。
