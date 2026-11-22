# PR-00 仓库基线核验报告（真实现状）

> 依据：《开发执行总纲与 PR 验收规范 V1.0》第四章 PR-00
> 性质：只读核验。**本报告期间未修改任何业务代码。**
> 基线：本地仓库快照（= main @ `b30d361430271b36d5ddaeb683220cf14f392abb`）
> 核验方式：本机逐目录盘点、逐文件统计、哈希重算；引用仓库内证据文件处均标明出处
> 处置记录：核验前由审计环节移除了根目录游离基线副本（差异已归档于 `docs/audits/2026-11-22-stray-baseline-copy-removal.md`，原件备份在项目外 `_stray_copy_backup\`），该动作不属于本 PR 范围，仅如实登记。

---

## 1. 基线信息

| 项 | 值 | 证据等级 |
| --- | --- | --- |
| main SHA | `b30d361430271b36d5ddaeb683220cf14f392abb` | 仓库声明（本地快照与其一致） |
| 当前分支状态 | **NOT RUN**：本地目录不含 `.git`，无法执行 `git status` / `git branch` | — |
| 架构基线版本 | V2.0.5（修订标识 V205-R15） | 实测：`docs/baseline/…V2.0.5.md` 头部 |
| 基线文件字节/哈希 | 489580 / `b922a1e1…`，与 frozen_inventory 登记一致 | 实测（本地重算 SHA-256） |
| 冻结清单自指哈希 | `2b8d4444…`，与侧车一致 | 实测 |
| Phase 0 冻结状态 | 条件通过（A-008 已统一为条件通过口径；无条件签署未完成） | 实测 + `2026-11-22-phase0-independent-review.md` |

## 2. 目录结构（全量）

```
apps/          api（有代码）
services/      14 个域目录（4 个有代码，10 个空）
packages/      shared / identity / errors / telemetry（全部有代码）
workers/       7 个（relay、outbox_maintenance、identity 有代码；4 个空）
adapters/      6 个（全部空）
agents/        3 个（全部空）
contracts/     12 类契约 + security_boundary（Phase 0 已冻结）
data/          migrations（38 版本）+ policies + seeds + examples
infra/         compose / docker / k8s / monitoring
docs/          baseline / adr / audits / architecture / evidence / standards / runbooks / proposals / perf
scripts/       10 个门禁与工具脚本
tests/         unit / integration / architecture / permission / resilience / engineering / e2e
.github/       workflows/ci.yml（单文件）
```

## 3. 已实现模块（实测代码量）

| 模块 | 文件数 | 行数 | 职责 |
| --- | --- | --- | --- |
| services/project_planning | 49 | 7,892 | Project/Episode/ScriptRevision/Scene/Shot 运行时（含 Shot 域，待拆） |
| services/asset_service | 17 | 1,383 | 资产与 QC 报告（append_only 两表） |
| services/identity_access | 5 | 467 | 权限判定/授权 |
| services/identity_issuance | 1 | 67 | 令牌签发 |
| packages/shared | 28 | 4,125 | 共享内核（含 Outbox/事务基座） |
| packages/identity | 10 | 691 | 身份基座 |
| packages/errors | 2 | 162 | 错误分类 |
| packages/telemetry | 2 | 62 | 日志/追踪 |
| workers/relay | 6 | 484 | Outbox 投递 |
| workers/outbox_maintenance | 3 | 451 | Outbox 维护 |
| workers/identity | 3 | 79 | 身份 Worker |
| apps/api | 2 | 264 | API 入口 |
| scripts/ | 10 | 7,526 | 门禁：freeze_manifest、contract_check、architecture_check、boundary_assertions、merge_readiness、secret_scan、verification_battery 等 |

业务与包代码合计约 **15,600 行**；门禁脚本 7,526 行。

## 4. 未实现 / 空目录清单（共 23 个）

| 位置 | 空目录 |
| --- | --- |
| services/ | blocking_service、budget_quota、creative_exploration、memory_learning、model_gateway、observability、orchestrator、production_policy、qc_governance、rights_governance（10） |
| adapters/ | audio_models、blocking、comfyui、external_tool、image_models、video_models（6） |
| agents/ | evaluators、planners、runtime（3） |
| workers/ | delivery、generation、notifications、qc（4） |

**结论：这些是目录预留位，不是已实现能力。** 按《总纲》1.2，骨架冻结时不允许空目录存在，须在 PR-04 前逐项裁决（删除/合并/填充）。

## 5. 正式迁移链

| 项 | 值 | 来源 |
| --- | --- | --- |
| 版本文件 | 38 个（data/migrations/versions/） | 实测 |
| head / current | `d7e8f9a0b1c2` / `d7e8f9a0b1c2` | 仓库证据 `2026-11-22-phase0-remediation.md` C6.1 |
| 数据库 Schema | project_planning、asset_service、outbox 等（域角色隔离迁移 `8f3b2c1d4e5a_split_domain_roles.py` 已存在） | 实测迁移文件 |
| 域角色 | project_planning_role / asset_service_role / app_role，写权限按 schema 隔离 | 实测迁移文件 |
| 降级验证 | 未发现往返验证证据 | **NOT RUN**（归 PR-02/PR-06） |

## 6. 契约体系现状

- Phase 0 契约层已冻结：frozen_inventory.yaml（V205-R15）+ phase0_manifest.md + 12 类契约文件；
- 11 类产物中 10 类逐字节、逐哈希与本机重算一致（第 8 类自指豁免）——实测；
- 修订台账 23 条，`traceability_debt: []`；矩阵 21 条来源映射台账层回填完成——实测；
- 追溯矩阵覆盖 A2–A22（A1、A23 自带 commits，不在债务内）；7 条字段级修订仍只有提交消息级证据（`[V-PARTIAL]`）。

## 7. ADR 现状（PR-01 的直接输入）

| 项 | 实测 |
| --- | --- |
| adr_index.yaml 登记总数 | 118 条（ADR-001 … ADR-118） |
| 独立 ADR 文件 | docs/adr/ 下 31 个 .md |
| 状态字段 | **索引无 status 字段**（无 ACCEPTED/SUPERSEDED/PROPOSED 状态矩阵） |
| 同号重复 | 已登记 duplicate_ids（ADR-101、ADR-001、ADR-021 等），same_wording=true |
| 编号缺口 | 由索引 number_gaps 字段承载，本报告未逐条展开（归 PR-01） |
| 关键缺口 | 118 条目 ↔ 31 文件映射表不存在；ADR 状态矩阵不存在；ADR 与实际实现的「已实现/未实现」标注不存在 |

## 8. 数据所有权现状

- 4+ schema 与域角色已由迁移建立（project_planning / asset_service 写隔离）；
- 所有权模型与 14 表矩阵审计存在（`docs/audits/2026-11-20-ownership-model-and-14-table-matrix.md`）；
- Shot / ReworkTask 的 `(project_id, …)` 复合归属约束：契约层已声明，**真实 PostgreSQL 验证证据以电池 C6 为据（迁移版本一致），但复合约束的负向写入测试未在本地复跑** → PR-02 复验项；
- 8 处跨域调用点（asset_service、project_planning → identity_access）待裁决。

## 9. 身份与权限现状

- 令牌签发（identity_issuance）、验签与权限判定（identity_access）、权限矩阵（permission_matrix.yaml 75 项 action 级）已存在；
- 负向用例在 tests/permission（电池 C7.2 = 126 passed 含权限负向）；
- 风险点：客户端可直传的身份字段（owner_id 等）是否全面拒绝，需 PR-03 逐接口核验。

## 10. CI 与真实环境证据

| 项 | 现状 |
| --- | --- |
| CI 文件 | `.github/workflows/ci.yml`：ruff / mypy / pytest / alembic upgrade+check / freeze_manifest / contract_check / architecture_check / merge_readiness / secret_scan / docker build |
| 最近 CI 结果 | 4/4 Job 成功（仓库声明；本机未执行） |
| 核验电池 | `eb72a54` 时点：14/14 V-VERIFIED，C7.1=1106 / C7.2=126 / C7.3=3 passed，0 failed |
| 本机复跑 | **NOT RUN**（本地无 .venv / 依赖；哈希与静态数据已用独立脚本重算替代） |

## 11. 已知风险清单（分级）

| 级别 | 风险 | 状态 |
| --- | --- | --- |
| P1 | Phase 0 无条件通过无 Owner 裁定（决议原件缺失，引用 17 处、原件 0 份） | 阻塞 PR-00 收口 → 需 Owner |
| P1 | Shot/ReworkTask 复合归属约束未在真实 PG 复验 | 归 PR-02 |
| P1 | 8 处跨域调用点未裁决；packages/shared → packages/identity 分层方向未裁决 | 归 PR-01/PR-03 |
| P1 | ADR 无状态矩阵、118 条目与 31 文件无映射 | 归 PR-01 |
| P2 | 23 个空目录（预留位）未裁决 | 归 PR-04 前置 |
| P2 | 7 条字段级修订只有消息级追溯证据 | 已登记 `[V-PARTIAL]` |
| P2 | 本地目录无 .git：分支/工作树状态不可核验；CI 与电池不可本地复跑 | 环境限制，如实标注 |
| P0 | 未发现 | — |

## 12. 第一道门禁结论

按《总纲》最终执行指令，PR-00 交付即本报告。进入 PR-01（ADR 与架构事实源对账）的前提：

1. 本报告经架构负责人/Owner 审阅确认无重大出入；
2. §11 中标注「需 Owner」的 P1 项明确处理方式（裁定 or 维持登记为 BLOCKED）；
3. PR-01 的输入（本报告 §7 ADR 缺口清单）被接受为工作范围。

**未满足以上任何一条，不允许开始骨架重构（PR-04 及以后）。**

## 13. 证据边界（如实声明）

- 「实测」= 本机对本地快照直接统计/重算；
- 「仓库证据」= 引自仓库内审计/电池文件（文件名已标注）；
- 「NOT RUN」= 本机因环境或权限未能执行，**一律不视为通过**；
- 本报告不采信 README/PR 描述作为现状判断依据。
