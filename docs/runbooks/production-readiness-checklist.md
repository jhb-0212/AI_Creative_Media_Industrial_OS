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

## 四、解除准入的前置条件

1. A1、A2 由功能负责人补齐 E2E 与产品边界公示证据；
2. B1~B5 由目标环境负责人完成实测并出具报告（本 agent 无目标环境权限，不能代替）；
3. D2 由数据库负责人在正式链测试库重跑归属取证；
4. 全部勾选后，再由项目所有者签署 Phase 1 生产验收——**签署权不在本 agent**。
