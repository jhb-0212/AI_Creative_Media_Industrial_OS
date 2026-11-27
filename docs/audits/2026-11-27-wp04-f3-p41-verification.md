# WP-04 F3 与 #41 锁断言验证记录（2026-11-27）

范围：隔离 worktree `...-d02`（分支 `feat/d02-and-retirement`，基线 `c4b9a52`，全部未提交）。
数据库：本机 PostgreSQL 16（端口 15444），全部使用一次性库，测试结束即 DROP；未触碰生产/预发布/`ai_film_industrial`。

## 1. F3：迁移不再接管集群级角色生命周期

改动（`8f3b2c1d4e5a_split_domain_roles.py`）：

1. 删除升级路径的 `DROP ROLE IF EXISTS`；
2. 补 A 层交叉撤权：对端域 schema 的默认权限（TABLES/SEQUENCES）+ `public` schema 的 USAGE；
3. 角色生命周期仍由 `role_specs.py` 声明，退役状态走 `RETIRED_ROLES` 四字段契约。

### 多库隔离实测

| 检查 | 结果 |
| --- | --- |
| 库 A `upgrade 8f3b2c1d4e5a` | exit 0 |
| 库 B `upgrade 8f3b2c1d4e5a` | exit 0 |
| 库 A `downgrade 7a8b9c0d1e2f` | exit 0（无 `DROP ROLE`，不再删角色） |
| 降级后 A 中角色是否仍存在 | `asset_service_role, project_planning_role` 均在 |
| A 中两角色的本库 `pg_shdepend` 行数 | 0 / 0（本 migration 管理范围内的授权已清空） |
| A 中两角色的 schema USAGE | 仅剩 `public`（来自 PUBLIC 继承，非本迁移授予） |
| 库 B 降级前后角色授权快照 | 完全一致（`b_unchanged = true`） |
| A 再次 `upgrade 8f3b2c1d4e5a` | exit 0，停在 `8f3b2c1d4e5a`（角色不重建、只按声明重新授权） |

结论：单库降级不再影响其他数据库的角色状态；"本库零授权"与"集群零授权"被区分开，本库 `pg_shdepend=0` 只证明 `managed_scope_revoked`，不代表 `cluster_wide_zero_authorization_verified`。

### 专项单测

`tests/unit/test_f3_role_lifecycle_boundary.py`（6 项）：无 `DROP ROLE` 语句、交叉默认权限撤销存在、`public` USAGE 撤销存在、两域角色仍属活角色且不在退役名单、Bootstrap 过滤退役角色、D-08 记录 F3 裁决。

## 2. #41：ACCESS EXCLUSIVE 锁 + 数据量断言

实现顺序（同一事务内）：relkind 校验 → 按 `_CUTOVER_TABLES` 固定顺序逐表 `ACCESS EXCLUSIVE`（`lock_timeout=10s`）→ 逐表 `count(*)` → 非空即 `RuntimeError`（失败关闭）→ `op.drop_table`（无 `IF EXISTS`）。

### 并发与失败路径实测

| 场景 | 结果 |
| --- | --- |
| 已有写事务先提交（1 行 sentinel） | upgrade exit 1：`cutover 中止：project_planning.budget_ledger 仍有 1 行…`；`alembic_version` 仍为 `f9a0b1c2d3e4`；五表与全部其它表完好，sentinel 行未被删除 |
| 迁移先取锁，写入方等待 | 复刻 guard 的同序锁后，写入方 `lock_timeout=2s` 直接超时（`canceling statement due to lock timeout`），表中 0 行 |
| 锁等待超时 | 竞争者持 `ACCESS SHARE` 15s，迁移在 `LOCK TABLE … ACCESS EXCLUSIVE` 处失败（exit 1）；版本号未变、五表结构未变、无部分变更 |
| 五表全空 | upgrade exit 0，版本推进到 `a0b1c2d3e4f5`，五表已离开 `project_planning` |
| 对象身份异常 | relkind 非 `r` 时直接 `RuntimeError`，不进入删除路径（静态分支，单测覆盖） |

`lock_timeout` 使用 `SET LOCAL`，失败即整事务回滚；未提供"无限等待"或"清空后继续"的分支。

实现备注：guard 最初用 `bind.execute(...).scalar_*` 读取 relkind 与行数，`contract_check` 的静态回放在无结果集时会抛 `AttributeError: '_EmptyResult' object has no attribute 'scalar_one_or_none'`，使 SCOPE-4 / DATA-6 断言假失败。改为单条 `DO $wp04_guard$ … $wp04_guard$` 服务端块（存在性 → 固定顺序加锁 → 计数 → `RAISE EXCEPTION`），迁移代码不再向客户端取标量；静态回放与真实库执行走同一条语句。

## 3. 双向语义闸门：三类覆盖率分开表述

证据文件：`docs/audits/2026-11-27-wp04-bidirectional-snapshot.json`（28 条）。

| 指标 | 数值 | 证据 |
| --- | --- | --- |
| 分类覆盖率 | 28/28 asymmetric revision 均有分类 | `docs/audits/2026-11-27-wp04-reversibility-matrix.json` |
| 快照实测覆盖率 | 28/28 均有 P→upgrade→downgrade→D 记录 | 上述 snapshot JSON（27 条 `no_restore_proven` + 1 条 `unproven_blocking`） |
| 全量 revision 往返覆盖率 | 仍不等于 28/28 已通过：28 条中 1 条阻断 | `8f3b2c1d4e5a`（#24）保持阻断 |
| 排除规则 | 2 条且本次实际生效 0 次 | `SNAPSHOT_IGNORE_RULES = {pg_toast_oid, acl_item_order}`，`ignored=0` |
| 未证明项 | 1 条阻断 | #24：`pg_shdepend` 170 行来自同集群其他 6 个库 |

闸门现在强制：任一 asymmetric revision 缺快照条目 → 阻断；`restore_required` 的分类必须由快照证明已修复，否则仍阻断。

### 本轮覆盖率核验新发现的缺陷

`f8c9d0e1a2b3`（#60）在完整 ACL 快照下报 `restore_required`：降级后 D 多出 `qc_maintenance_owner` 对 `qc_governance` 的 USAGE 与 `qc_tasks` 的 SELECT——upgrade 里由 `maintenance_command_statements()` 授予，旧降级只删策略/函数/表，没有撤这两条授权。

修复：降级先 `REVOKE SELECT ON qc_governance.qc_tasks` 与 `REVOKE USAGE ON SCHEMA qc_governance`，再走原有 DROP 序列。复跑该 revision 往返：`no_restore_proven`，`diff_groups=[]`。

这说明静态分类不能替代快照：旧口径下 #60 被判为"无需恢复"，实际存在降级残留授权。

## 4. 未完成与边界

- `#24` 仍是矩阵唯一阻断项（跨库 `pg_shdepend` 属集群事实，需单独裁定处理方式）。
- WP-03 多轮循环验收只有单轮真库 + 单测；退役恢复的 fail-closed 已在 §7 验证，凭据缺失路径仍未覆盖。
- ADR-R46 待签字。
- 全部改动未提交、未推送、未合并；主仓 3 个未提交改动未触碰。

## 5. 身份 DDL 基线变更（本轮）

基线只记录计数，本文件承担"变更来源 + 审计理由"的人工可查部分；基线更新不替代数据库语义验证（语义验证见 §2、§3）。基线文件只存 13 个整数计数器，不含 `per_migration`；逐文件归因由本节表格承担。

旧值 = `HEAD c4b9a5241525` 的 `data/migrations/identity_ddl_baseline.json`（`24 / 61 / 9 / 9 / 54`）；新值 = 工作区当前基线（`29 / 74 / 5 / 5 / 49`）。

| 类别 | 旧 | 新 | Δ |
| --- | --- | --- | --- |
| `default_privs` | 24 | 29 | +5 |
| `revoke` | 61 | 74 | +13 |
| `grant` | 54 | 49 | −5 |
| `role_create` | 9 | 5 | −4 |
| `role_alter` | 9 | 5 | −4 |

其余 8 类（`converge_statements`、`infra_converge_statements`、`maintenance_identity_statements`、`maintenance_membership_statements`、`owner`、`policy`、`scheduler_statements`、`schema`）未变。

逐文件归因（用 `audit_file` 对"工作区文件 vs HEAD blob"逐文件重算，五类差值之和与上表精确相等）：

| 迁移 | Δ | 来源与理由 |
| --- | --- | --- |
| `7a8b9c0d1e2f` | `role_create` −1、`role_alter` −1 | 移除迁移内 `CREATE/ALTER ROLE {relay_role}`：角色创建、属性与口令的唯一声明处改为 Bootstrap + `role_specs.py` |
| `9e3b4c5d6e7f` | `role_create` −1、`role_alter` −1 | 同上，`app_role` 的创建从迁移移出 |
| `c5d6e7f8a9b0` | `role_create` −1、`role_alter` −1 | 同上，dead-letter 处理角色的创建从迁移移出 |
| `f2b3c4d5e6a7` | `role_create` −1、`role_alter` −1 | `qc_worker` 退役：迁移不再创建/改写该角色，退役声明留在 `role_specs.RETIRED_ROLES` |
| `8f3b2c1d4e5a` | `default_privs` +2、`revoke` +3、`grant` −5 | F3：补交叉域 `ALTER DEFAULT PRIVILEGES … REVOKE`、对端 schema 与 `public` 的撤权；`grant` 从"两 schema 全表一把授"改为按父态逐表还原（净 −5） |
| `c1a2b3d4e5f6` | `default_privs` +2、`revoke` +5 | 最小权限收敛：新域对象与 `public` 的默认权限撤销、schema `USAGE` 与新域对象撤权 |
| `a0b1c2d3e4f5` | `revoke` +1 | 降级补齐本 revision 自有对象的对称撤权 |
| `b3c4d5e6f7a8` | `revoke` +1 | 同上，本 revision 授权对象的对称撤权 |
| `b7c8d9e0f1a2` | `default_privs` +1 | 分区保护对象的默认权限撤销（收紧） |
| `b8c9d0e1f2a3` | `revoke` −1 | 删除旧实现中错误的 `REVOKE USAGE`：父态本就有这些 USAGE，本迁移只是幂等重复授予，降级不应撤 |
| `c6d7e8f9a0b1` | `revoke` +1 | 降级补齐 `auth.revoked_tokens` 的对称撤权 |
| `e6a7b8c9d0e1` | `revoke` +1 | 降级补齐维护身份的对称撤权 |
| `f8c9d0e1a2b3` | `revoke` +2 | 降级补齐 `qc_governance.qc_tasks` 的 `SELECT` 与 schema `USAGE` 撤权 |

证据对应关系：

- 涉及授权增量的 9 个 revision（`8f3b2c1d4e5a`、`c1a2b3d4e5f6`、`a0b1c2d3e4f5`、`b3c4d5e6f7a8`、`b7c8d9e0f1a2`、`b8c9d0e1f2a3`、`c6d7e8f9a0b1`、`e6a7b8c9d0e1`、`f8c9d0e1a2b3`）全部在 28 条双向快照内，`diff_groups=[]`；逐条条目见 `docs/audits/2026-11-27-wp04-bidirectional-snapshot.json`；
- `default_privs` / `revoke` 的每一处增量都是收紧（撤销授权或撤销默认权限），对应证据为 §2/§3 的 `P → upgrade → downgrade → D` 快照 0 差异、`identity_surface_baseline` 差异 0 条，以及 `docs/audits/2026-11-27-wp04-n24-triage.json`；
- `grant` −5 对应 #24 逐表还原清单与 `diff_groups=[]`（§8）；
- `role_create` / `role_alter` −4 只发生在 `7a8b9c0d1e2f`、`9e3b4c5d6e7f`、`c5d6e7f8a9b0`、`f2b3c4d5e6a7` 四个 revision，均无授权增量，对应证据为 `role_specs.py` 唯一事实源与 WP-01/02/03 文档（`2026-11-27-wp01-role-registry-single-source.md`、`2026-11-27-wp02-historical-role-lifecycle.md`、`2026-11-27-wp03-qc-worker-retirement.md`），不需要也不应声称有数据库授权快照证据。

只增不减的例外仅限"撤权"和"默认权限撤销"两类：它们收紧权限面，不会扩大角色能力；任何 `grant` / `policy` / `role_create` 的增加仍按 REGRESSION 处理；基线计数与迁移树必须精确相等（`test_baseline_matches_current_migration_tree`）。

## 6. WP-A 补充证据（#41 并发细节）

### 6.1 执行顺序的代码级证据

`_cutover_guard_sql()` 生成单条 `DO` 块，结构固定为：五表 `IF NOT EXISTS … relkind='r'` 校验 → 五条 `LOCK TABLE … ACCESS EXCLUSIVE`（按 `_CUTOVER_TABLES` 顺序）→ 五段 `IF (SELECT count(*)) > 0 THEN RAISE EXCEPTION`。

`tests/unit/test_cutover_guard_order.py` 直接解析生成的 SQL：断言最后一条锁的下标早于第一个 `count(*)`、锁顺序与声明元组一致、块内没有 `TRUNCATE` / `DELETE FROM` / `DROP TABLE IF EXISTS`。

### 6.2 五表逐表非空

每张表单独建库、单独写 1 行 sentinel、单独执行 `alembic upgrade a0b1c2d3e4f5`：

| sentinel 表 | upgrade | 版本 | 五表存在数 | sentinel 行 |
| --- | --- | --- | --- | --- |
| `production_routes` | exit 1 | `f9a0b1c2d3e4` | 5 | 1 |
| `generation_tasks` | exit 1 | `f9a0b1c2d3e4` | 5 | 1 |
| `shot_budget_requests` | exit 1 | `f9a0b1c2d3e4` | 5 | 1 |
| `gate_decisions` | exit 1 | `f9a0b1c2d3e4` | 5 | 1 |
| `budget_ledger` | exit 1 | `f9a0b1c2d3e4` | 5 | 1 |

五条报错均指向对应表名（`cutover 中止：project_planning.<table> 仍有 1 行…`），没有出现"只保护 budget_ledger"的情况，也没有任何表被清空或重建。

### 6.3 真实迁移持锁 + 第二连接写入

不是复刻 SQL：用真实迁移模块的 `upgrade()`，在 Alembic `MigrationContext` + `Operations` 代理下执行于一个未提交事务；`_guard_cutover()` 返回后写入标记文件并停留 10s，第二连接在此期间执行 INSERT。

| 步骤 | 结果 |
| --- | --- |
| 迁移进入 guard 并取得五表锁 | 是（标记文件在 guard 返回后写出） |
| guard 持锁窗口内第二连接 INSERT | `lock_timeout=2s` 超时：`canceling statement due to lock timeout`，0 行 |
| 迁移提交后旧表 | `project_planning.budget_ledger` 已不存在（count=0） |
| 迁移提交后新表 | `budget_quota.budget_ledger` 存在，列集合与旧表完全一致，0 行 |
| 提交后再写旧表名 | `ERROR: relation "project_planning.budget_ledger" does not exist`——不会向被替换的旧对象报告成功 |

说明：该 harness 直接执行迁移函数体，未走 Alembic 版本记账，因此 `alembic_version` 在库内仍是 `f9a0b1c2d3e4`；结构证据（旧表消失、新 schema 表出现、列集合一致）才是本次要证明的对象，不写假 revision。

### 6.4 仍未覆盖

- 全链路 `head → 23 → head`（WP-B）。
- WP-03 退役角色循环验收与 ADR-R46 签字（WP-C）。

## 7. WP-B：全链路 head → 23 → head

数据库：一次性库 `afs_chain`，当前 head `f2b3c4d5e6a7`（#64），目标降级点 `7a8b9c0d1e2f`（#23，F3 的父态）。

| 步骤 | 结果 |
| --- | --- |
| `upgrade head` | exit 0 |
| `downgrade 7a8b9c0d1e2f`（不带显式确认） | exit 1：`恢复退役身份 "qc_worker" 需要显式确认：设置 AUTHORIZE_RETIRED_ROLE_RESTORE=f2b3c4d5e6a7`；事务回滚，库仍停在 head——缺凭据时 fail-closed |
| `downgrade 7a8b9c0d1e2f`（带 `AUTHORIZE_RETIRED_ROLE_RESTORE`） | exit 0，逐 revision 降级无阻断 |
| 再次 `upgrade head` | exit 0 |
| 身份面快照（升级前 vs 往返后） | `[EQUAL] 差异 0 条`（含角色属性、schema/表/索引、ACL、默认 ACL、成员关系） |
| `alembic check` | `No new upgrade operations detected` |
| `identity_registry --check` | clean（declared 20 / retired 1 / problems []） |
| 身份面基线 | 差异 0 条 |

### 本轮全链路暴露并修掉的两个缺陷

1. **`drop_marked_constraint` 的 SQL 引用错误**：`string_agg` 子查询在 FROM 里引用外层 `con.conkey` 需要 `LATERAL`，否则 PG 报 `missing FROM-clause entry for table "con"`。降级到 #37/#35 段时必然触发。改为 `LEFT JOIN LATERAL (…) cols ON TRUE`，并把 `contype` 一并取出到 PL/pgSQL 变量后判断。
2. **#41 索引名两头不一致**：新域 schema 必须匹配 ORM（`ix_qc_governance_gate_decisions_project_id_gate_key`），旧 schema 必须匹配父 revision（`ix_project_planning_gate_decisions_gate_key`）。此前用一个全局后缀规则，导致 `alembic check` 报索引漂移或 P/D 集合不一致。改为按 schema 区分，两个方向同时成立；#41 复跑往返 `diff_groups=[]`，身份面基线按新名重建、差异归零。

这两处都不是文档问题：没有全链路往返，它们不会暴露。

## 8. #24 证据闭环（F3 收尾）

### 8.1 权责边界

- Bootstrap + `role_specs.py` 拥有角色创建、属性与退役状态；迁移不创建、不删除基础角色。
- 迁移只拥有"本次升级授出的、落在本库本 schema 的授权"，降级只撤销同一张授权面。
- `RETIRED_ROLES` 四字段保持独立：本库 `pg_shdepend=0` 只支持 `managed_scope_revoked`；`cluster_wide_zero_authorization_verified` 仍为 `false`，不因本库干净而置真。

### 8.2 升级授出的授权面与降级撤销清单（逐类对应）

| 升级授出 | 降级对应动作 |
| --- | --- |
| 两域角色对各自 schema 的 `USAGE` + `ALL TABLES`/`ALL SEQUENCES` | 对同 schema 精确 `REVOKE ALL ON ALL TABLES/SEQUENCES` + `REVOKE USAGE` |
| 两域角色在各自 schema 的默认权限（TABLES/SEQUENCES） | `ALTER DEFAULT PRIVILEGES … REVOKE ALL ON TABLES/SEQUENCES` |
| 交叉默认权限（升级时同事务写默认权限，会作用到对端 schema） | 对对端 schema 逐角色 `ALTER DEFAULT PRIVILEGES … REVOKE ALL`（TABLES + SEQUENCES） |
| `public` schema `USAGE` | `REVOKE USAGE ON SCHEMA public` |
| `outbox` 表/序列/`USAGE`、`auth` 表/`USAGE`、`alembic_version` `SELECT` | 逐条 `REVOKE`（与升级语句一一对应） |

`app_role` 的恢复**不再用 `ALL TABLES IN SCHEMA` 一把授**，改为按父态逐表还原：

| 表 | 父态实际权限 |
| --- | --- |
| `asset_service.asset_versions` | `INSERT, SELECT, UPDATE` |
| `asset_service.qc_reports` | `INSERT, SELECT` |
| `project_planning.episodes` / `projects` / `script_revisions` | `DELETE, INSERT, SELECT, UPDATE` |
| `auth.revoked_tokens` | `SELECT` |
| `public.alembic_version` | `SELECT` |
| 序列 / `organizations` | 无 |

旧实现按 schema 全表授 `SELECT, INSERT, UPDATE, DELETE`，会让 D 在 `organizations`、`asset_versions`、`qc_reports` 上多出写权限——完整快照下正是 `restore_required`。

### 8.3 降级后本库实测（一次性库 `afs_n24ev`）

| 检查 | `project_planning_role` | `asset_service_role` |
| --- | --- | --- |
| 角色仍存在 | 是 | 是 |
| 本库 `pg_shdepend` | 0 | 0 |
| 两 schema `USAGE` | false / false | false / false |
| `nspacl` 是否提及该角色 | 否 | 否 |
| 表 `relacl` 提及数 | 0 | 0 |
| `pg_default_acl` 提及数 | 0 | 0 |

### 8.4 父态 / 降级态快照对比

`docs/audits/2026-11-27-wp04-n24-triage.json`：`8f3b2c1d4e5a`（父 `7a8b9c0d1e2f`）P→upgrade→downgrade→D，结果 `no_restore_proven`，`diff_groups=[]`。对比口径含 ACL（`aclexplode` 规范化）、对象集合、依赖行。

### 8.5 多库隔离（复验，降级恢复语句改为逐条后）

| 检查 | 结果 |
| --- | --- |
| A 库升级 → 降级 | exit 0 / 0 |
| 降级后 A 中两角色 | 仍存在 |
| A 本库 `pg_shdepend` / project_planning `USAGE` / `app_role` 对 `projects` `SELECT` | `0 / false / true`（与父态一致） |
| B 库快照（降级前后） | 一致：`13 / true / false` |
| A 再升级 | exit 0，`8f3b2c1d4e5a` |

### 8.6 结论与分类处理

上述清单、逐项撤权、父态快照与多库隔离均已满足。按裁决口径，`8f3b2c1d4e5a` 已具备从 `unproven_blocking` 改为 `no_restore_proven` 的证据条件；**本轮未擅自改分类**，`reversibility_matrix --check` 仍以 #24 阻断，等待验收签字后再改。跨库 `pg_shdepend`（他库 170 行）仍作为集群事实记录，不属于本库可撤销范围。

## 9. WP-C：head → 23 → head 连续三轮

数据库 `afs_wpc2`，每轮前后都做同一套检查；本轮在 #24 降级恢复语句改为"逐表还原"之后重跑。

| 轮次 | 无授权降级 | 库版本 | 授权降级 | 中间版本 | 再升级 | 身份面快照 | `alembic check` | Registry |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | fail-closed | `f2b3c4d5e6a7` | exit 0 | `7a8b9c0d1e2f` | exit 0 | 差异 0 条 | 无漂移 | clean（20/1） |
| 2 | fail-closed | `f2b3c4d5e6a7` | exit 0 | `7a8b9c0d1e2f` | exit 0 | 差异 0 条 | 无漂移 | clean（20/1） |
| 3 | fail-closed | `f2b3c4d5e6a7` | exit 0 | `7a8b9c0d1e2f` | exit 0 | 差异 0 条 | 无漂移 | clean（20/1） |

- 每轮"无授权降级"都在第一步即被拒绝，事务回滚后库仍停在 head，证明 fail-closed 不是一次性现象。
- 三轮的身份面快照（角色属性、对象、ACL、默认 ACL、成员关系）全部与初始 head 快照一致，没有累积性漂移。
- 中间态 `7a8b9c0d1e2f` 下 `qc_worker` 与被拆出的两个域角色同时存在，符合"降级恢复历史定义"的预期。
