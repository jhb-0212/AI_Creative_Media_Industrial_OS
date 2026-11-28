# 2026-11-27 第二批状态：D-02 / ADR-R46 / WP-03 方案 A

- 授权边界（全程未变）：**不提交、不推送、不合并**；不改正式环境与 `ai_film_industrial`；主仓 pilot 分支 3 个未提交改动原样保留；本轮实验在独立 worktree `...-d02`（分支 `feat/d02-and-retirement`，基线 `c4b9a52`）
- 本轮定位：执行三项裁决（D-02 第一优先级 / ADR 先行 / WP-03 有条件通过），并如实标注"阶段性审计与实验验收"与"全链路降级安全验收"的差距

## 1. 交付物

| 交付物 | 路径 | 状态 |
| --- | --- | --- |
| D-02 修复 + 报告 | `data/migrations/versions/f9a0b1c2d3e4_*.py`、`docs/audits/2026-11-27-d02-index-name-fix.md` | 完成（targeted 往返 + 回滚证据） |
| D-07 修复（同批阻断） | `packages/shared/db_grants.py`、`c1a2b3d4e5f6_*.py`、`tests/unit/test_legacy_grants_scope.py` | 完成（4 项测试） |
| 数据约束型降级 ADR | `docs/adr/ADR-R46-data-constrained-downgrade.md` | 提议，待裁决 |
| 降级 revision 清单 | `docs/audits/2026-11-27-downgrade-revision-inventory.md` + `.json` + `scripts/downgrade_inventory.py` | 65 条结构事实 + 类别初判（待签字） |
| WP-03 方案 A | `role_specs.py`、`migration_identity.py`、`f2b3c4d5e6a7_*.py`、`bootstrap_identity.py`、`identity_registry.py`、`docs/audits/2026-11-27-wp03-retirement-restore-contract.md` | 实现完成，5/6 项验收实测通过 |

## 2. 关键读数

- **D-02**：修复前 `head → 23` 在 #40 报 `index ... does not exist`；修复后 `#41 → #39 → #41 → head` 全 exit 0；#41 的 4 个索引名与定义与 dev 逐字一致；身份面 `--compare` 差异 0 条。
- **回滚**：`head → 23` 在 #24 失败后，`alembic_version` 仍为 head、`qc_governance.gate_decisions` 四索引完好、无半迁移残留。
- **WP-04 未完成项**：`head → 23` 仍被 #24（pilot `8f3b2c1d4e5a` 的 `DROP ROLE`）阻断；D-08 已登记，不能据此宣称全链可逆。
- **WP-03**：真实库一圈"降级恢复 → 升级退役 → 未授权降级被拒"；恢复后读数 `LOGIN`、无提权、`INHERIT`、零成员；未授权时 exit 1 且版本不变。
- **闸门**：`ruff` / `mypy(218)` / `pytest 2005 passed 0 failed 1 skipped` / `alembic check` / `audit_identity_ddl --check` / `identity_registry --check` / `architecture_check` / `contract_check` 全 PASS；`freeze_manifest --check` 为既存失败（`adr_index.yaml` 与冻结清单不一致，第一轮 worktree 同样红）。

## 3. 不宣称的部分

- 未生成 WP-04 14 处"升级 REVOKE、降级不恢复"的批量整改方案（本轮只完成 D-02/WP-03/ADR/清单，符合 P0→P1 顺序）。
- 清单的"条件检查方式 / 失败行为 / 测试证据"三列尚未逐条补齐（ADR 通过后才落 `assert_data_precondition`）。
- D-02 的 `head → 23` 需要在主仓 pilot 3 个未提交改动所在的实验分支另开 worktree 才能跑通，本轮未搬运。

## 4. 下一步（推荐顺序不变）

1. `D-08` 的方案裁决（pilot revision 的 `DROP ROLE` 是否随实验分支一并修，或在主链补一条独立迁移）。
2. ADR-R46 签字 → 生成带守卫实现与测试证据的完整清单。
3. WP-04 14 处授权恢复整改方案。
