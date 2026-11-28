# CHG-WP04-D4-9 变更演练记录（15444 演练库，2026-11-28）

> **范围声明（先读）**：本记录是 **A.6.6 的演练/预演**，执行对象为 `wp04-verify-15444` 的
> `ai_film_api_tests` 库（测试集群），**不是生产主库**。生产实例 ID 尚未书面确认，按 A.6.6 §四
> 「未确认即授权自动失效」，生产执行**未发生**，本记录也不能替代生产变更记录。
>
> 本演练目的：把 A.6.6 §七 的 7 步验证计划与 G5/G6/G7 在真实 PG 16 上跑通一遍，
> 证明「方法可执行、备份可恢复、回滚可走」，让生产窗口只剩"换实例"这一变量。

- 变更编号：`CHG-WP04-D4-9`
- 代码状态：`311530c` + D4-9/D4-strip/§8 整改（隔离工作树 `ci-tree`，未提交）
- 执行时间：2026-11-28（CST）
- 执行主体：实施人（Agent）；复核：Project Owner
- 容器：`wp04-verify-15444`（`postgres:16`，16.16-1.pgdg13+1）
- 证据目录：`runs/2026-11-28/chg-wp04-d4-9/`

## 1. 起点

| 项 | 值 |
|---|---|
| `alembic_version` | `f2b3c4d5e6a7`（链尾 head） |
| `pg_roles` 行数 | 35（快照 `pg_roles.csv`，1,889 B） |
| `pg_auth_members` 行数 | 4（`pg_auth_members.csv`） |
| 成员关系 | `pg_monitor → pg_read_all_settings / pg_read_all_stats / pg_stat_scan_tables`；`qc_maintenance_login → qc_maintenance_operator` |

> 采集更正：首次成员快照命令输出为空（表格格式参数问题），已重采并**重算清单哈希**；
> 旧空文件不保留为证据，避免"零成员"的错误结论。

## 2. G5 目标环境角色齐备（只读）

| 命令 | 结果 |
|---|---|
| `scripts/identity_registry.py --check` | exit 0，`clean=true`（declared 20 / retired 1 / checked 20 / memberships 1） |
| `scripts/audit_identity_ddl.py --check` | exit 0，`role_create/alter/drop = 0`，`total_actions 237`，`[PASS] 只减不增` |

结论：strip 后的 fail-closed 前提在演练库成立——角色由 Bootstrap 提供，迁移不再兜底造身份。

## 3. G6 备份 + 可恢复性抽验

| 产物 | 大小 | sha256（前 16） |
|---|---|---|
| `chg.dump`（`pg_dump -Fc`，结构+数据） | 650,023 B | `49af7d45acfb5c53` |
| `chg-roles.sql`（`pg_dumpall --roles-only`，含口令哈希，按密级管理） | 5,099 B | `c06b61834a8fb7b4` |
| `pg_roles.csv`（属性快照） | 1,889 B | 见 `backup-manifest.json` |
| `pg_auth_members.csv`（成员快照） | 60 B | 见 `backup-manifest.json` |
| `alembic_version` 锚点 | `f2b3c4d5e6a7` | 记录于清单 |

恢复抽验（**不允许"备份了但没验过"**）：

1. `createdb chg_restore_check` → exit 0
2. `pg_restore --no-owner --no-acl` → exit 0
3. 恢复库 `alembic_version` = `f2b3c4d5e6a7`（与源库一致）
4. 恢复库业务表数 = **34**（`information_schema.tables`，排除系统 schema）
5. 抽验后 `DROP DATABASE chg_restore_check`（临时库不留在集群里）

## 4. G7 回滚演练（恢复库上做，不碰源库）

| 步骤 | 命令 | 结果 |
|---|---|---|
| 降一级 | `alembic downgrade -1` | exit 0；版本 `f2b3c4d5e6a7 → b6c7d8e9f0a1` |
| 升回 head | `alembic upgrade head` | exit 0；版本 `→ f2b3c4d5e6a7` |

要点：`f2b3` 的 `downgrade()` 为 `return None`——降级**不会**重建 `qc_worker`，也不会删任何角色；
演练证实"降级只回收本迁移对象/授权"的语义在真实库上成立。

## 5. 7 步验证计划（在演练库执行一遍）

| # | 项 | 结果 |
|---|---|---|
| 1 | `alembic upgrade head` / `alembic current` | exit 0；`f2b3c4d5e6a7` |
| 2 | 身份面核对（`pg_roles` + 成员 vs 快照） | 35 角色 / 4 成员，与 §1 快照一致 |
| 3 | `identity_registry.py --check` | exit 0，`clean=true` |
| 4 | `audit_identity_ddl.py --check` | exit 0，`[PASS]` |
| 5 | `reversibility_matrix.py --check` | exit 0（`asymmetric 23 / no_restore_proven 27 / unclassified 0`） |
| 6 | 业务读路径 | `select count(*) from project_planning.projects` → exit 0 |
| 7 | 记录归档 | 本文件 + `summary.json` + 全部 `*.log` |

`summary.json` 全字段：`start_version f2b3c4d5e6a7`、`g5_registry_exit 0`、`g5_ddl_exit 0`、
`g6_dump_exit 0`、`g6_restored_version f2b3c4d5e6a7`、`g6_restored_tables 34`、
`g7_downgrade_exit 0`、`g7_version_after_down b6c7d8e9f0a1`、`g7_upgrade_exit 0`、
`g7_version_after_up f2b3c4d5e6a7`、`v_upgrade_exit 0`、`v_current f2b3c4d5e6a7`、
`v_registry_exit 0`、`v_ddl_exit 0`、`v_matrix_exit 0`、`v_read_exit 0`。

## 6. 本记录**不**代表什么

1. 不代表生产主库已变更——生产实例未确认，授权按 A.6.6 §四 自动失效，需重新签署后方可执行。
2. 不代表 `RETIREMENT_EXECUTED` 达成——`qc_worker` 在演练库本就不存在，本次未执行任何 `DROP ROLE`。
3. 不代表可跳过生产备份——生产备份必须在生产窗口内重做，演练库的 dump 不能迁移使用。
4. 不代表 D5 已闭环——D5 仍以「生产窗口执行 + 24 h 内回填记录」为闭环条件。

## 7. 生产窗口只剩的三件事

1. Owner 书面确认生产实例 ID（连接别名即可，不入库）。
2. 指派 DBA（或书面授权 Owner 本人兼执行），复核人到场。
3. 窗口内按 §3→§4→§5 顺序重跑，产物落 `runs/<date>/chg-wp04-d4-9-production/`，24 h 内回填本目录同名记录（后缀 `-production`）。
