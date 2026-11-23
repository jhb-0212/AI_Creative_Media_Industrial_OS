# BOOT-00 当前 main 基线与证据核验（只读）

> 依据：《AI Creative Media Industrial OS 开发启动执行包 v1.0》BOOT-00；《核验与审计标注规范 V1.0》
> 性质：**只读核验**。本报告期间未修改任何业务代码、契约、迁移、ADR 正文或冻结清单。
> 基线：main @ `abf77c18ed48dcabf55492acf9b0c8dddc967022`（`git rev-parse origin/main` 实测，已 fetch）
> 输入：执行包 v1.0（用户提供，仓库根目录 `总体实施蓝图.txt` 为上一份方案，二者均为工作区输入证据）
> 原则：每条结论指向具体文件 / 提交 / 测试证据；执行包自述**不采信为现状**，一律实读复验。

---

## 1. 基线标识（BOOT-00 检查项 1）

| 项 | 实测值 | 标注 | 复核命令 |
|---|---|---|---|
| origin/main HEAD | `abf77c18ed48dcabf55492acf9b0c8dddc967022` | `[V-VERIFIED][E2]` | `git fetch origin main && git rev-parse origin/main` |
| 与执行包声明一致 | 一致（执行包 §1.1 同 SHA） | `[V-VERIFIED][E1+E2]` | — |
| HEAD 提交时间 | `2026-11-23T17:12:02+08:00`（= `09:12:02Z`） | `[V-VERIFIED][E2]` | `git log -1 --format=%aI origin/main` |
| HEAD 提交标题 | `refactor(c7): 收敛 QC 入队为 GENERATION_SUCCEEDED 事件单一路径` | `[V-VERIFIED][E2]` | — |
| local main / origin main | 均为 `abf77c1…`（一致） | `[V-VERIFIED][E2]` | — |
| 当前工作分支 | `docs/blueprint-reconciliation-adr-r44`（HEAD=`9821647`，已开 PR #60） | `[V-VERIFIED][E2]` | — |
| 工作区状态 | 仅 1 个未跟踪文件 `总体实施蓝图.txt`（输入证据），无已跟踪改动 | `[V-VERIFIED][E2]` | `git status --short` |

**口径差异（如实登记，非错误）**：执行包 §1.1 称"仓库树 846 个条目"。本机 `git ls-tree -r --name-only origin/main | wc -l` = **720**（仅 blob/文件）。两者口径不同——846 应为 GitHub API 递归 tree（含目录 tree 对象）计数，720 为纯文件计数。**都不采信为"代码规模"结论**，仅登记口径，避免用数量判断成熟度。

---

## 2. 决策与冻结事实源（BOOT-00 检查项 2/3）

| 项 | 实测 | 标注 |
|---|---|---|
| 权威基线 | `docs/baseline/AI影视工业化生产平台V2.0.5.md`，冻结标识 V205-R15 | `[V-VERIFIED][E2]`（README:5 + frozen_inventory.yaml:17） |
| README Phase 0 | "契约冻结已通过（**有条件**）"；Phase 1 主链 6 Action/3 对象/3 状态机为 `V205-R14-A1` **事后补登记** | `[V-VERIFIED][E2]`（README:142-145） |
| README Phase 1 | "核心主链实现中，**未通过验收**"；明确"契约 ACTIVE ≠ 代码已实现 ≠ 集成已通过 ≠ 生产验收" | `[V-VERIFIED][E2]`（README:146-148） |
| Creator Mode | `NOT_ENABLED` | `[V-VERIFIED][E2]`（README:150） |
| 冻结清单 | `contracts/phase0/frozen_inventory.yaml`，revision `V205-R15` | `[V-VERIFIED][E2]`（:1,:17） |
| ADR Index | `docs/adr/adr_index.yaml`，`total_registered: 118`，`number_gaps: []` | `[V-VERIFIED][E2]`（:5,:145） |
| R42 | `docs/adr/ADR-R42-asset-registration-saga.md`，状态 **DECIDED**，正文第 3/9 行写"固化决策，**不激活实现** / 本裁定只确定决策与契约变更清单，**不实现**" | `[V-VERIFIED][E2]`（:3,:9-10） |
| R43 | `docs/adr/ADR-R43-canvas-reference-boundary.md`，状态 DECIDED（2026-11-23 拍板） | `[V-VERIFIED][E2]` |
| 迁移链 head | `e1f2a3b4c5d6 (head)`（单 head，R42 Saga 迁移） | `[V-VERIFIED][E2+E5]`（`alembic heads`） |
| 执行包"README 写 RabbitMQ/NATS/Kafka 三选一" | **未在 README 核验段命中**；`grep` 未发现该表述。可能是执行包引用了其他文档或过时记忆 → 列为待核项，不采信 | `[V-UNVERIFIED][E1]` |

**治理差异（属实，仅登记不改）**：R42 正文"不激活实现"与 C/D/E 三个 PR（`716971c`/`e95127a`/`291300a`）已将 T0–T3 实现进 main 之间存在授权缺口。执行包 BOOT-01 要求以 append-only 实施记录补正、不得静默改写历史决策——本报告认同该处置方向，但**改 ADR 体系文档需 Owner 授权**，本轮只读不改。详见 BOOT-01 报告。

---

## 3. CI 状态（BOOT-00 检查项 4）

| 项 | 实测 | 标注 |
|---|---|---|
| CI workflow | `.github/workflows/ci.yml`：`push`/`pull_request` on `[main]`；4 job = lint-and-test(:16) / contract-check(:128) / permission-negative-tests(:161) / image-build(:180) | `[V-VERIFIED][E2]` |
| HEAD 关联 run | main 最新 = `45145570659`（`refactor(c7)` 提交），status=completed, **conclusion=success** | `[V-VERIFIED][E2]`（`gh run list --branch main`） |
| 前两个 main run | `45144159039`（C4）、`45140680867`（PR-E 故障注入）均 success | `[V-VERIFIED][E2]` |
| 执行包引用的 run `45145167478` | 该 run 是 **PR #59 分支**上的运行，不是 HEAD 直接关联的 main run；结论同为 success，但口径不同 | `[V-PARTIAL][E1+E2]`（如实标注差异） |
| 本机复跑 | **NOT RUN**（本轮只读，未执行 pytest/mypy/门禁） | `[V-UNVERIFIED]` |

**解释边界**：CI 绿只证明该 run 覆盖的检查成功，**不能**证明生产部署、Temporal 长流程、真实 S3、真实模型生产、Delivery 全链或 Phase 1 验收通过。与执行包 §1.3 边界一致。

---

## 4. 模块清单（BOOT-00 检查项 5：已实现 / 部分 / 空壳 / 延后 / 被 ADR 禁止）

按 .py 文件数实测（`find <dir> -name '*.py' | wc -l`），并标注性质：

| 域 | 实测 | 分类 | 依据 |
|---|---|---|---|
| services/project_planning | 49 | 已实现（含 Shot，**待拆**） | 目录实测 |
| services/asset_service | 20 | 已实现 | 目录实测 |
| services/qc_governance | 15 | 已实现（成熟度待 BOOT-01 外的审计） | 目录实测 |
| services/production_planning | 14 | 已实现 | 目录实测 |
| services/budget_quota | 12 | 已实现（并发/账本验收待核） | 目录实测 |
| services/identity_access / identity_issuance | 5 / 1 | 已实现 | 目录实测 |
| services/rights_governance / orchestrator / model_gateway / memory_learning / production_policy / observability / creative_exploration / blocking_service | 0 | **空壳目录**（不是能力） | 目录实测，与执行包 §1.4 一致 |
| adapters/model_gateway / storage | 6 / 3 | 已实现（OpenAI-compatible + LocalFS + ComfyUI adapter，后者按 R43 不内嵌画布） | 目录实测 |
| adapters/{image_models,video_models,audio_models,comfyui,blocking,external_tool} | 0 | 空壳目录 | 目录实测 |
| workers/relay / generation / qc / identity 等 | 有代码 | 已实现/部分 | 目录实测 |
| apps/api | 有代码 | 已实现（**不迁 services/api**，执行包 §2-1 同向） | 实测 |
| apps/console | 不存在 | 延后（Phase 4 / P2，遵守 R43） | 实测 |
| Temporal | 无 service/workflow，仅 `temporalio` SDK 依赖 | 延后 + 待裁决（TC-1） | `.venv` + 目录实测 |
| S3 / Valkey / PG18 | 无部署 | 待裁决（TC-2/3/4），裁决前维持现状 | compose 实测 |

分类原则：空目录 = 预留位，**不视为已实现能力**；延后/被禁止项均指向已批准 ADR 或待裁决 TC，不自造进度。

---

## 5. 建议的检查命令（BOOT-00 检查项 6：四态如实标注）

执行包 §7 建议的命令，本轮**只登记命令与口径，不执行**（执行属 BOOT-01 或后续审计动作）：

| 命令 | 本轮状态 | 说明 |
|---|---|---|
| `uv sync --extra dev` | NOT RUN | 环境准备，非只读审计范围 |
| `uv run ruff check .` / `mypy` | NOT RUN | 静态检查，CI 已覆盖（E2 事实），本机不复跑 |
| `uv run pytest tests` | NOT RUN | 集成/E2E 需真实 PG，本轮只读 |
| `freeze_manifest.py --check` | NOT RUN | 冻结一致性，CI contract-check job 覆盖 |
| `contract_check.py` / `architecture_check.py` | NOT RUN | 同上 |
| `alembic check` | **PASS（本机已跑）** | `alembic heads` 单 head `e1f2a3b4c5d6`；`alembic check` 完整迁移漂移检查未在只读范围，如实标 NOT RUN，仅 heads 核验 PASS |

> 四态口径：PASS（实际执行且证据满足）/ FAIL（反向证据）/ BLOCKED（外部阻塞）/ NOT RUN（未执行）。本轮仅 `alembic heads` 为 PASS，其余多为 NOT RUN，**不将 CI 的 PASS 冒充本机 PASS**。

---

## 6. 九项状态结论

| 标注 | 取值 | 支撑 | 未覆盖 / 升级条件 |
|---|---|---|---|
| `[FREEZE-STATUS]` | PARTIAL | README Phase 0 有条件、清单 V205-R15、ADR Index 118 | Owner 无条件签署原件；升级条件：Owner 裁定遗留条件 |
| `[CONTRACT-STATUS]` | VERIFIED | CI contract-check success（run 45145570659） | 本机未复跑；执行包新契约未定义 |
| `[ARCHITECTURE-STATUS]` | PARTIAL | 模块清单 §4；R42/R43 边界 | R42 治理差异未补正；TC-1~5 未裁决 |
| `[IMPLEMENTATION-STATUS]` | PARTIAL | 8 域有代码、8 空壳目录 | 空壳域未建设；Phase 1 未验收 |
| `[TEST-STATUS]` | PARTIAL | CI 四 job success | 本机 NOT RUN；不采信 CI 冒充本机证据 |
| `[RUNTIME-STATUS]` | PARTIAL | 迁移 head 单 head | 无真实 PG 本机复跑、无生产部署证据 |
| `[SECURITY-STATUS]` | VERIFIED | CI permission-negative-tests success | 本机未复跑负向测试 |
| `[REMEDIATION-STATUS]` | OPEN | R42 治理差异待 append-only 补正 | Owner 授权后处理（BOOT-01 详述） |
| `[RELEASE-STATUS]` | BLOCKED | README Phase 1 未通过验收 | PR-17 试播证据 + Owner 签署 |

---

## 7. 证据边界声明

- 本报告为 main @ `abf77c18…` 的**时点判断**；基线推进后须重出，不得沿用。
- 未连接真实 PG 复验业务数据、未执行 pytest/mypy/门禁（除 `alembic heads`）、未做任何写操作。
- 执行包 §1.1 的"846 条目"、§1.3 的"run 45145167478 关联 HEAD"、§1.4 的"README 写三选一 Broker"三处自述与本机实测存在口径差异或未命中，均已如实登记，**不采信为事实**。
- 本轮未修改 ADR-R42 正文、未改冻结清单、未开新 ADR；ADR-R44 合并升级与 TECH PoC 均待 Owner 裁决后另行推进。
