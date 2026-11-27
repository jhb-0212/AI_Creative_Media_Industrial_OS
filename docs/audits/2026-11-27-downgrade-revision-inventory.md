# 降级 revision 清单（ADR-R46 §4 交付物，初判）

- 日期：2026-11-27
- 状态：**初判（结构事实已核对，类别待签字）**。本清单不宣称"已具备可逆性"，只把每条 revision 的升降级动作与数据约束摆到可裁决的位置。
- 事实来源：`data/migrations/versions/*.py` 的 AST（`op.*` 调用）+ `2026-11-27-identity-migration-inventory.json`（授权/策略类语句级事实）
- 复现：`python scripts/downgrade_inventory.py --json docs/audits/2026-11-27-downgrade-revision-inventory.json`
- 类别口径：ADR-R46 §2 六类（A 无数据要求 / B 数据前置条件 / C 任务静默 / D 可逆数据转换 / E 有损 / F 不可逆）
- 自动降级口径：`auto` 可自动；`guarded` 必过守卫；`manual` 需保全方案 + 审批；`unsupported` 明确不支持

读表注意：
1. `drop_table` / `drop_column` / `drop_constraint` 在降级侧一律先判 **E 有损**，因为升级后写入的数据会随对象消失；若该对象是"本迁移刚建、尚无业务写入"的空结构，签字时可降级为 A，但必须写明依据。
2. `execute` 是语句块，真实内容看 `source`/`sql`（inventory JSON）；表里只能看到它存在，不能看到它做了什么。
3. "影响对象"取自 `op.*` 的首个字符串参数（表名/索引名），不是完整血缘。

## 清单

| # | revision | 升级动作 | 降级动作 | 影响对象 | 类别 | 自动降级 | 依据 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | `8d8d59a91012` | execute, create_table | drop_table | projects, projects | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 1 | `e22d6fd3e8d3` | create_table, create_index | drop_index, drop_table | script_revisions, script_revisions | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 2 | `facd8d42d07f` | create_index | drop_index | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 3 | `0fcaf4db4573` | create_table, create_index, create_index | drop_index, drop_index, drop_table | episodes, ux_episode_ref_per_project, ux_episode_ref_per_project, episodes | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 4 | `6a7b8c9d0e1f` | drop_index, create_unique_constraint, create_foreign_key, execute | drop_constraint, drop_constraint, create_index | ux_episode_ref_per_project, fk_script_revisions_episode, fk_script_revisions_episode, ux_episode_ref_per_project | E 有损数据变更 | manual | 降级含 ['drop_constraint']：升级后写入的数据会丢失（初判，待签字） |
| 5 | `7c8d9e0f1a2b` | execute, create_table, create_index, create_index | drop_index, drop_index, drop_table, execute | outbox_events, ix_outbox_aggregate, ix_outbox_published, ix_outbox_published | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 6 | `8f2a1b3c4d5e` | add_column, add_column, execute, alter_column, create_index, execute | drop_column, execute, execute, drop_index, drop_column | script_revisions, script_revisions | E 有损数据变更 | manual | 降级含 ['drop_column']：升级后写入的数据会丢失（初判，待签字） |
| 7 | `9e3b4c5d6e7f` | execute, execute, execute, execute, execute | execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 8 | `b2c3d4e5f6a7` | add_column, execute, alter_column, create_index, execute, execute | execute, execute, drop_index, drop_column | — | E 有损数据变更 | manual | 降级含 ['drop_column']：升级后写入的数据会丢失（初判，待签字） |
| 9 | `d4e5f6a7b8c9` | execute, execute | execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 10 | `e5f6a7b8c9d0` | execute, execute, execute | execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 11 | `f6a7b8c9d1e2` | execute, execute, execute, execute, execute, execute | execute, execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 12 | `b8c9d1e2f3a4` | create_table, create_index | drop_index, drop_table | ix_outbox_maintenance_runs_started, ix_outbox_maintenance_runs_started | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 13 | `c1d2e3f4a5b6` | execute, create_table, create_index, create_index, create_table, create_index | execute, execute, drop_table, drop_table, execute, execute | asset_versions, ix_asset_service_asset_versions_tenant_id, ix_asset_service_asset_versions_type_sha256, qc_reports | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 14 | `b7c8d9e0f1a2` | execute, execute, execute, execute, execute, execute | execute, execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 15 | `d1e2f3a4b5c6` | execute, execute | execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 16 | `e2f3a4b5c6d7` | add_column | drop_column | qc_reports, qc_reports | E 有损数据变更 | manual | 降级含 ['drop_column']：升级后写入的数据会丢失（初判，待签字） |
| 17 | `f3a4b5c6d7e8` | add_column, add_column, add_column | drop_column, drop_column, drop_column | outbox_events, outbox_events | E 有损数据变更 | manual | 降级含 ['drop_column']：升级后写入的数据会丢失（初判，待签字） |
| 18 | `a4b5c6d7e8f9` | execute | — | — | A 无数据要求 | auto | 降级委托共享 helper（语句不在本文件），需人工核对 helper 是否带数据守卫 |
| 19 | `b5c6d7e8f9a0` | execute | execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 20 | `c6d7e8f9a0b1` | execute, create_table, create_index, execute, execute | execute | ix_auth_revoked_tokens_expires_at | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 21 | `a1b2c3d4e5f6` | execute, execute, execute, execute | execute, execute, execute, execute | outbox.list_active_tenants, outbox.outbox_events | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 22 | `9f1a2b3c4d5e6` | create_table, add_column, create_foreign_key, create_foreign_key | drop_constraint, drop_constraint, drop_column, drop_table | organizations, projects, fk_projects_organization, fk_episodes_project | E 有损数据变更 | manual | 降级含 ['drop_column', 'drop_constraint', 'drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 23 | `7a8b9c0d1e2f` | execute, execute, execute, execute, execute | execute, execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 24 | `8f3b2c1d4e5a` | execute, execute, execute, execute, execute, execute | execute, execute, execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 25 | `ae8724980e0b` | execute, execute, execute, execute | execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 26 | `b9e1c2d3f4a5` | execute, execute, execute | execute, execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 27 | `c1f2e3d4a5b6` | execute, execute | execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 28 | `d2e3f4a5b6c7` | create_unique_constraint, drop_constraint, create_foreign_key, execute, execute | execute, execute, drop_constraint, create_foreign_key, drop_constraint | uq_organizations_owner_tenant, fk_projects_organization, fk_projects_organization_tenant, fk_projects_organization_tenant | E 有损数据变更 | manual | 降级含 ['drop_constraint']：升级后写入的数据会丢失（初判，待签字） |
| 29 | `b3c4d5e6f7a8` | create_unique_constraint, add_column, create_foreign_key, create_index, execute, execute | execute, drop_index, drop_constraint, drop_column, drop_constraint | asset_versions, asset_versions | E 有损数据变更 | manual | 降级含 ['drop_column', 'drop_constraint']：升级后写入的数据会丢失（初判，待签字） |
| 30 | `c7d8e9f0a1b2` | create_check_constraint | drop_constraint | — | E 有损数据变更 | manual | 降级含 ['drop_constraint']：升级后写入的数据会丢失（初判，待签字） |
| 31 | `d8e9f0a1b2c3` | execute, execute, execute, execute, execute, execute | execute, execute, execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 32 | `e9f0a1b2c3d4` | execute, execute, execute | execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 33 | `e0f1a2b3c4d5` | execute | execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 34 | `c1a2b3d4e5f6` | execute | execute | — | A 无数据要求 | auto | D-07：降级按冻结范围回滚授权，只碰自己 upgrade 动过的表 |
| 35 | `a13b0c1d2e3f` | create_table, create_index, create_index, create_table, create_index, create_index | drop_index, drop_index, drop_table, drop_index, drop_index, drop_index | rework_tasks, scenes, shots, shot_revisions | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 36 | `b1c2d3e4f5a6` | execute, create_unique_constraint, drop_constraint, execute | drop_constraint, create_foreign_key | — | E 有损数据变更 | manual | 降级含 ['drop_constraint']：升级后写入的数据会丢失（初判，待签字） |
| 37 | `c3d4e5f6a7b8` | create_table, create_index, create_index, execute, execute, execute | — | — | A 无数据要求 | auto | 降级委托共享 helper（语句不在本文件），需人工核对 helper 是否带数据守卫 |
| 38 | `d7e8f9a0b1c2` | create_table, create_index, create_index, create_index, execute, execute | execute, execute, drop_index, drop_index, drop_index, drop_table | — | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 39 | `e8f9a0b1c2d3` | execute | execute, execute, drop_index, drop_index, drop_table | — | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 40 | `f9a0b1c2d3e4` | execute | execute, execute, drop_table | — | A 无数据要求 | auto | D-02 修复后降级只 DROP TABLE，索引随表删除（实测往返通过） |
| 41 | `a0b1c2d3e4f5` | execute, execute, execute, execute | create_table, execute, create_index, execute, execute | — | A 无数据要求 | auto | R16 cutover 结构搬迁，降级重建旧表；D-02 组合往返实测通过 |
| 42 | `d5e6f7a8b9c0` | add_column, add_column, add_column, drop_constraint, create_check_constraint | drop_constraint, create_check_constraint, drop_column, drop_column, drop_column | — | E 有损数据变更 | manual | 降级含 ['drop_column', 'drop_constraint']：升级后写入的数据会丢失（初判，待签字） |
| 43 | `e6f7a8b9c0d1` | add_column, create_index, create_check_constraint | drop_constraint, drop_index, drop_column | — | E 有损数据变更 | manual | 降级含 ['drop_column', 'drop_constraint']：升级后写入的数据会丢失（初判，待签字） |
| 44 | `f1a2b3c4d5e6` | execute | execute, execute, drop_index, drop_index, drop_index, drop_table | — | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 45 | `a2b3c4d5e6f7` | add_column, add_column | drop_column, drop_column | — | E 有损数据变更 | manual | 降级含 ['drop_column']：升级后写入的数据会丢失（初判，待签字） |
| 46 | `c4d5e6f7a8b9` | execute, execute, execute, execute | execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 47 | `b8c9d0e1f2a3` | execute | execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 48 | `c9d0e1f2a3b4` | execute, execute, drop_constraint | drop_constraint, execute, execute | — | E 有损数据变更 | manual | 降级含 ['drop_constraint']：升级后写入的数据会丢失（初判，待签字） |
| 49 | `d0e1f2a3b4c5` | execute, alter_column, execute, execute | execute, alter_column | — | E 有损数据变更 | manual | 降级含 ['alter_column']：升级后写入的数据会丢失（初判，待签字） |
| 50 | `e1f2a3b4c5d6` | create_unique_constraint, create_unique_constraint, create_table, create_table, execute, execute | execute, execute, drop_column, execute, execute, execute | artifact_manifests, asset_registration_receipts, asset_service.reject_receipt_truncate, production_routes | B 数据前置条件 | guarded | ck_generation_tasks_status 的 VALIDATE 在 `AWAITING_ASSET_REGISTRATION` 在途行存在时失败（实测） |
| 51 | `f1a2b3c4d5e7` | add_column, execute | execute, drop_column | — | E 有损数据变更 | manual | 降级含 ['drop_column']：升级后写入的数据会丢失（初判，待签字） |
| 52 | `e4f5a6b7c8d9` | execute, execute, execute, execute, execute, execute | execute, execute, execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 53 | `a7b8c9d0e1f2` | add_column, execute, alter_column | drop_column | — | E 有损数据变更 | manual | 降级含 ['drop_column']：升级后写入的数据会丢失（初判，待签字） |
| 54 | `c5d6e7f8a9b0` | execute, execute | execute, execute, execute, execute, execute, execute | — | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 55 | `e7f8a9b0c1d2` | drop_constraint, create_check_constraint | drop_constraint, create_check_constraint | — | E 有损数据变更 | manual | 降级含 ['drop_constraint']：升级后写入的数据会丢失（初判，待签字） |
| 56 | `f8a9b0c1d2e3` | execute, drop_constraint, create_check_constraint | drop_constraint, create_check_constraint | — | E 有损数据变更 | manual | 降级含 ['drop_constraint']：升级后写入的数据会丢失（初判，待签字） |
| 57 | `f9b0c1d2e3f4` | execute | execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 58 | `a8b9c0d1e2f3` | execute | execute, execute, execute, execute, drop_table, execute | — | E 有损数据变更 | manual | 降级含 ['drop_table']：升级后写入的数据会丢失（初判，待签字） |
| 59 | `e6a7b8c9d0e1` | execute, execute | execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 60 | `f8c9d0e1a2b3` | execute | execute, execute, execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 61 | `b4c5d6e7f8a9` | execute, execute, execute | execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 62 | `a5b6c7d8e9f0` | execute, execute, execute, execute | execute, execute, execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 63 | `b6c7d8e9f0a1` | — | execute, execute | — | A 无数据要求 | auto | 只有授权/策略/索引类动作（初判，待签字） |
| 64 | `f2b3c4d5e6a7` | execute | — | — | A 无数据要求 | auto | 降级委托共享 helper（语句不在本文件），需人工核对 helper 是否带数据守卫 |

## 需要人工确认的重点条目

| revision | 位置 | 待确认事项 |
| --- | --- | --- |
| `f9a0b1c2d3e4` | #40 | D-02 已修：降级只 `DROP TABLE`，索引随表删除；隔离库 targeted 往返通过（见 `2026-11-27-d02-index-name-fix.md`） |
| `a0b1c2d3e4f5` | #41 | R16 cutover：降级重建 `project_planning` 五表，需确认"重建后的索引/约束与 #40 定义一致" |
| `e1f2a3b4c5d6` | #50 | B 类样板：`ck_generation_tasks_status` 的守卫必须在破坏性 DDL 之前，并把"条数"写进错误信息 |
| `c1a2b3d4e5f6` | #34 | D-07 已修：降级按冻结范围回滚授权；清单口径应改为"只回滚本迁移授过的表" |
| `9e3b4c5d6e7f` / `7a8b9c0d1e2f` / `c5d6e7f8a9b0` | #7 / #23 / #54 | WP-02 已改：降级不再 `DROP ROLE`，只 `REVOKE`；需在清单里写明"身份不随降级消失" |
| `f2b3c4d5e6a7` | #64 | WP-03：降级恢复 `qc_worker` 走 `RETIRED_ROLES` + 显式确认 + 告警；未授权即失败关闭 |
| `8f3b2c1d4e5a` | #24 | D-08：pilot revision 的 `DROP ROLE` 阻断 `head → 23`；该文件在主仓有 3 个未提交改动，需在实验分支单独验证 |

## 尚未随本清单交付的部分

- 每条 revision 的"条件检查 SQL / 失败行为 / 测试证据"三列：本轮只做到结构事实与类别初判，具体守卫实现要按 ADR-R46 落 `assert_data_precondition` 后再补。
- C 类（任务静默条件）目前没有机读判定形态，只有 `e1f2a3b4c5d6` 的个案证据，需 ADR 落地时统一。
