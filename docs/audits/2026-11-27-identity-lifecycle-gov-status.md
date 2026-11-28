# 身份生命周期治理：本轮交付状态（2026-11-27）

- 分支：`feat/identity-lifecycle-gov`（worktree `AI_Creative_Media_Industrial_OS-identity-gov`），基线 `c4b9a52`
- 授权边界：允许实验分支开发 + 隔离库实验；**不提交、不推送、不合并**；不改生产/预发布历史迁移；不在正式库执行迁移
- 未提交改动：5 个修改 + 10 个新增（代码/工具/测试/审计文档），主仓 pilot 分支的 3 个实验改动**原样保留、未触碰**

## 交付对照

| 工作包 | 产物 | 状态 |
| --- | --- | --- |
| WP-01 角色事实源 | `packages/shared/role_specs.py`（生命周期字段 + `spec_problems` + `registry_payload`）、`scripts/identity_registry.py`、`docs/audits/2026-11-27-identity-registry.generated.json`、`docs/audits/2026-11-27-wp01-role-registry-single-source.md` | **完成**，`--check` 20/20 `clean=true` |
| WP-02 三处历史整改 | 三个迁移改为 `assert_role_present(..., op=op)` + downgrade 只撤权；`packages/shared/migration_identity.py`；`data/migrations/identity_ddl_baseline.json` 重生成（create 9→6、alter 9→6）；`docs/audits/2026-11-27-wp02-historical-role-lifecycle.md` | **完成**，两条隔离库往返 0 差异 |
| WP-03 `qc_worker` 退役语义 | `docs/audits/2026-11-27-wp03-qc-worker-retirement.md`（3 个方案 + 推荐 A） | **调查完成**，待裁决 |
| WP-04 全链可逆性 | `scripts/reversibility_matrix.py`、`tests/unit/test_reversibility_matrix.py`、`docs/audits/2026-11-27-wp04-migration-reversibility.md`、`docs/audits/2026-11-27-wp04-reversibility-matrix.json` | **矩阵完成**（40 含身份动作 / 28 不对称），整改未批量执行 |
| WP-INDUSTRIAL-LINEAGE | `docs/audits/2026-11-27-wp-industrial-lineage.md` | **完成**：结论从 UNKNOWN 变为「已识别分支谱系（`o5c6d7e8f9a0`）」，仍隔离 |

## 闸门读数（worktree，最终一次）

| 闸门 | 结果 |
| --- | --- |
| `ruff check .` | PASS |
| `mypy packages services apps workers` | PASS（218 files） |
| `pytest tests` | **1986 passed / 0 failed / 1 skipped**（103.8s） |
| `identity_registry.py --check` | `declared=20 checked=20 problems=[] clean=true` |
| `audit_identity_ddl.py --check` | PASS（只减不增） |
| `alembic check` | `No new upgrade operations detected.` |

## 阻断点（按优先级）

| # | 阻断点 | 证据 | 归属 |
| --- | --- | --- | --- |
| 1 | `f9a0b1c2d3e4`（第 40 位）降级 `DROP INDEX ... does not exist` | head → 23 在第 40 位失败；实际索引名 `ix_qc_governance_gate_decisions_project_id_gate_key` vs 计算名 `ix_project_planning_gate_decisions_gate_key` | **D-02**，全链可逆性前置 |
| 2 | `e1f2a3b4c5d6`（第 50 位）降级 `ck_generation_tasks_status` VALIDATE 失败 | 带数据克隆实测；迁移注释认定为刻意守卫 | 数据兼容，需 ADR 分级 |
| 3 | 28 个不对称 revision（含 14 处 `REVOKE` 未恢复） | `2026-11-27-wp04-reversibility-matrix.json` | WP-04 分批整改 |
| 4 | `qc_worker` 降级重建的"退役身份复活" | `f2b3c4d5e6a7` upgrade DROP / downgrade CREATE | WP-03 方案 A 待裁决 |

## 需要裁决的三件事

1. **WP-03 是否采纳方案 A**（`role_specs` 增加 `RETIRED_ROLES` 维度 + 降级告警），而不是让 head 的 downgrade 失败关闭。
2. **D-02 是否纳入下一批**：不修它，全链往返永远下不到第 39 位以下；修它只改索引名字，不改语义。
3. **数据守卫分级**：哪些 revision 允许"有数据就拒绝降级"必须在 ADR 里列明，否则 WP-04 的"往返 0 差异"判据对生产库不可执行。

## 未做（明确边界）

- 未提交、未推送、未合并；未创建 PR。
- 未修改任何生产/预发布迁移；未在正式库执行迁移。
- 未对 `ai_film_industrial` 做任何写操作（未 stamp、未改版本号、未删角色/对象）。
- 未批量整改 28 个不对称 revision。
