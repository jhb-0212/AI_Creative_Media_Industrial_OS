# 数据库迁移对账（真实 PostgreSQL 只读取证）

- 取证对象：`infra/compose` 拉起的 `ai-film-postgres`（PostgreSQL 16.16，容器端口 15432）
- 取证时间：2026-11-20
- 仓库状态：`main` @ `5352d19`（工作树）；对照分支 `origin/feat/identity-data-ownership` @ `59b13af`
- 取证方式：**只读**。全部结论来自 `SELECT` 与系统目录查询；本轮**未执行** `alembic upgrade/downgrade/stamp`，未执行任何 DDL/DML，未 `DROP ROLE`。
- 可复跑：`docs/evidence/db-reconciliation/probe.sql`（11 段只读查询）；原始输出 `probe-output.txt`；结构快照 `db-ai_film_industrial.json`；迁移链逐条对账 `main-rev-matrix.json`。

---

## 1. 结论先行

1. **这个库执行的是 main 的迁移链，不是功能分支的链。** `alembic_version = c1a2b3d4e5f6`，与工作树 `alembic heads` 完全一致；main 链上 34 个 revision 声明的结构对象 **34/34 全部存在，零缺口**。
2. **功能分支的 17 个独有 revision 在库里 0 执行。** 证据是 schema 层：库内只有 `project_planning / asset_service / audit / outbox / auth`，分支新增的 `shot_planning / generation_execution / qc_governance / workflow` 域**一个都不存在**；分支的 8 个 `svc_*` 角色**一个都不存在**。
3. **两分支的迁移图不能续接，只能重建。** 51 个唯一 revision ID 中：23 个共享、11 个仅 main、17 个仅分支；其中有 **1 个硬冲突**（同 ID 不同对象）与 **2 个实质差异**（同 ID 不同数据库效果）。
4. **D4 的最小权限矩阵在真实库上是完整落地的**，且零越权：4 个业务角色对全部业务表 **DELETE/TRUNCATE 全为 false**。

因此此前共识成立并可结案：**以 main 为整合基底，不整体 merge 分支**；分支能力按域重放。

---

## 2. 库现状（原始事实）

| 项 | 值 |
|---|---|
| 库名 / 用户 | `ai_film_industrial` / `postgres`（取证用超级用户，读侧可见全部策略） |
| `alembic_version` | `c1a2b3d4e5f6`（= main head） |
| schema | `project_planning` `asset_service` `audit` `outbox` `auth` `public` |
| 表 / 分区实体 | 14（含 `outbox_events` 分区父表 + 3 个分区 + `alembic_version`） |
| RLS | 业务表与 `audit`、`outbox` 全部 `rls=true`；`auth.revoked_tokens`、`outbox.maintenance_runs`、`alembic_version` 无 RLS |
| FORCE RLS | 业务四表与 `audit` 为 `forced=true`；`outbox` 分区为 `false`（属主是超级用户，FORCE 对超级用户无意义，不构成缺口） |
| 角色 | 19 个，其中业务登录角色 4 个：`project_planning_role` `asset_service_role` `outbox_relay_role` `app_role`；无 `svc_*` |
| RLS 策略 | 11 条，全部 `cmd=ALL`、`roles={public}`、`USING` 已写 |
| 约束 | 30 条：主键 14 + 外键 6 + CHECK 5 + UNIQUE 4 + TRIGGER 1 |
| 默认权限 | 1 条：`outbox` schema 对三域角色的表默认权限（`outbox_relay_role=r`，两业务域 `ar`） |

### 2.1 归属链在库里是真实约束，不是 Service 层约定

```
organizations(organization_id PK, UNIQUE(organization_id, tenant_id))
projects(organization_id, tenant_id) → organizations(organization_id, tenant_id)   [fk_projects_organization_tenant]
episodes(project_id) → projects(entity_id)                                         [fk_episodes_project]
script_revisions(project_id, episode_ref) → episodes(project_id, episode_ref)      [fk_script_revisions_episode]
asset_versions(project_id, tenant_id) → projects(entity_id, tenant_id)             [fk_asset_versions_project_tenant]
qc_reports(asset_version_id) → asset_versions(entity_id)                            [qc_reports_asset_version_id_fkey]
```

**这一条直接推翻了上一轮的 P1 判定**：`asset_versions` 的 `(project_id, tenant_id)` 复合外键在 main 线上**已存在**（迁移 `b3c4d5e6f7a8_asset_project_ownership`），并非缺失。上一轮把「分支缺这条」误写成「main 缺这条」，事实相反。

## 3. 与 `packages/shared/db_grants.py` 矩阵逐项比对

矩阵是唯一授权事实源，库是它的投影。逐项核对结果：**全部符合，无遗漏、无放大**。

| 角色 | 表 | 矩阵声明 | 库内 `relacl` | 一致 |
|---|---|---|---|---|
| `project_planning_role` | `project_planning.projects` | SIU | `arw` | ✅ |
| `project_planning_role` | `project_planning.episodes` | SIU | `arw` | ✅ |
| `project_planning_role` | `project_planning.script_revisions` | SIU | `arw` | ✅ |
| `project_planning_role` | `project_planning.organizations` | S | `r` | ✅ |
| `project_planning_role` | `audit.status_transitions` | SI | `ar` | ✅ |
| `project_planning_role` | `outbox.outbox_events` | SI | `ar` | ✅ |
| `project_planning_role` | `auth.revoked_tokens` | S | `r` | ✅ |
| `project_planning_role` | `public.alembic_version` | S | `r` | ✅ |
| `asset_service_role` | `asset_service.asset_versions` | SIU | `arw` | ✅ |
| `asset_service_role` | `asset_service.qc_reports` | SI | `ar` | ✅ |
| `asset_service_role` | `project_planning.projects`（列级） | S(entity_id, organization_id, tenant_id) | `attacl` 三列 `r` | ✅ |
| `asset_service_role` | `audit.status_transitions` | SI | `ar` | ✅ |
| `asset_service_role` | `outbox.outbox_events` | SI | `ar` | ✅ |
| `asset_service_role` | `auth.revoked_tokens` | S | `r` | ✅ |
| `outbox_relay_role` | `outbox.outbox_events`（列级） | U(attempts, dead_lettered_at, next_attempt_at, published_at) | `attacl` 四列 `w` | ✅ |

补充事实：

- relay 只拿到 4 个**列级** UPDATE，事件业务内容列不可改（`b9e1c2d3f4a5` 收口生效）；
- `asset_service_role` 对 `projects` 是 3 列只读而非整表 SELECT（跨域物理隔离生效，R2 断言的前提在库上成立）；
- 越权扫描（`probe.sql` 第 11 段）返回 **0 行**：四个业务角色对所有业务表的 `DELETE`/`TRUNCATE` 均为 false。

## 4. main 迁移链逐 revision 对账

`main-rev-matrix.json`：34 个 revision，按 `down_revision` 串成 base→head 顺序（不依赖文件名排序），逐个提取「声明建了什么」，再查库「有没有」。

结果：**34/34 全 OK，无 GAP**。链尾即 `c1a2b3d4e5f6`（D4 最小权限收敛迁移），与 `alembic_version` 一致。

这排除了「库被 stamp 到某个版本但结构没跟上」这类最危险的形态——本库是**结构真实执行到 head**，不是记录造假。

## 5. 跨分支 revision 对账矩阵

唯一 revision ID 共 **51** 个（main 34 文件 / branch 40 文件，共享 23）。

| 分类 | 数量 | ID |
|---|---|---|
| A. 共享且文件内容逐字相同 | 13 | 22 个同名文件中，9 个内容不同（见 B/C），其余 13 个 `git diff` 为空 |
| B. 共享同 ID、内容不同但**效果等价** | 7 | `a1b2c3d4e5f6` `b5c6d7e8f9a0` `c1d2e3f4a5b6` `c6d7e8f9a0b1` `d4e5f6a7b8c9` `e5f6a7b8c9d0` `f6a7b8c9d1e2` |
| C. 共享同 ID、**实质差异** | 2 | `9e3b4c5d6e7f` `b7c8d9e0f1a2` |
| D. **同 ID 不同对象（硬冲突）** | 1 | `b3c4d5e6f7a8` |
| E. 仅 main | 11 | `7a8b9c0d1e2f` `8f3b2c1d4e5a` `9f1a2b3c4d5e` `ae8724980e0b` `b9e1c2d3f4a5` `c1a2b3d4e5f6` `c1f2e3d4a5b6` `c7d8e9f0a1b2` `d2e3f4a5b6c7` `d8e9f0a1b2c3` `e9f0a1b2c3d4` |
| F. 仅分支 | 17 | `a2b3c4d5e6f7` `a5b6c7d8e9f0` `a9f1c2e3d4b5` `b1a2c3d4e5f6` `b8c9d0e1f2a3` `c2d3e4f5a6b7` `c4e5f6a7b8d9` `c9d0e1f2a3b4` `d0e1f2a3b4c5` `d3e4f5a6b7c8` `d5e6f7a8b9c0` `d6e7f8a9b0c1` `e1f2a3b4c5d6` `e7f8a9b0c1d2` `e8f9a0b1c2d3` `f0a1b2c3d4e5` `f1a2b3c4d5e6` |

### 5.1 D 类：硬冲突 `b3c4d5e6f7a8`

同一个 revision ID 指两个不同数据库对象：

| 分支 | 文件名 | 建了什么 |
|---|---|---|
| main | `b3c4d5e6f7a8_asset_project_ownership.py` | `asset_versions.project_id` + `(project_id, tenant_id)` 复合外键 |
| branch | `b3c4d5e6f7a8_asset_status_transitions.py` | `asset_status_transitions` 表 |

这不是「合并文本冲突」，而是**同一个版本号在两条线上代表不同的世界**。任何「把分支 merge 进来再 upgrade」的路径都会得到 `alembic_version` 说「已执行」、而库里缺一半对象的假一致。

### 5.2 C 类：同 ID 不同效果（实质）

**`9e3b4c5d6e7f_create_app_role_and_grants`**

| | main | 分支 |
|---|---|---|
| 口令守卫 | `reject_development_default_password(...)` 显式拒绝开发默认口令 | 无 |
| 角色属性 | `ALTER ROLE ... NOBYPASSRLS`（重置危险属性） | 仅 `ALTER ROLE ... WITH PASSWORD`（保留存量属性） |
| downgrade | `DROP ROLE IF EXISTS` | 无 |

差别不是风格：**分支版本无法纠正一个历史上带有 `BYPASSRLS` 的角色**，而 RLS 的全部效力都建立在「连接角色不绕过 RLS」之上。同一 ID 在分支线上产生的是更弱的角色加固。

**`b7c8d9e0f1a2_partition_protection_and_grant_baseline`**

分支调用 `protect_partition_statements(partition, roles=(APP_ROLE,))` 把回收范围**收窄到单一 `app_role`**；main 用默认角色集合。配合分支自己的 `svc_*` 角色体系，两边对「哪些角色该被分区保护」给出不同答案 —— 库的实际分区授权形态会不同。

### 5.3 B 类等价差异（7 个，不构成障碍）

差异形态统一：分支把 `app_role_name()` 换成硬编码 `"app_role"`。`app_role_name()` 默认返回 `"app_role"`，故**数据库效果完全等价**，属分支早于 `db_roles` 收敛时的重构残留。合并时按 main 版本为准即可，不需要单独迁移。

### 5.4 F 类：分支 17 个独有迁移，库内 0 执行

不仅「没执行」，而且**无法在现库上直接执行**：

- `c2d3e4f5a6b7_split_database_roles` 建 8 个 `svc_*` 角色，并在第 4 步**`DROP app_role`**；main 的对应迁移（`8f3b2c1d4e5a` + `c1f2e3d4a5b6`）保留 `app_role` 并收窄其权限。两条线对 `app_role` 的终局相反。
- `d3e4f5a6b7c8_add_organization_and_project_ownership` 给实体加 `organization_id` 并回填 `= tenant_id`、给资产加 `project_id` 回填 `'legacy-unassigned'`；main 由 `9f1a2b3c4d5e` + `d2e3f4a5b6c7` + `e9f0a1b2c3d4` 用真实 `organizations` 表 + 复合外键 + 组织维度 RLS 完成同一目标。在现库执行会因 `organizations` 已存在、`organization_id` 已有真值而失败或产生语义倒退。
- 角色是**集群级**对象，不是库级：`c2d3e4f5a6b7` 若在任何共享同一 PG 实例的库上执行，会影响其他库。

**这是「不能续跑、只能重建迁移图」的硬证据**，不是保守判断。

## 6. 本次取证未覆盖 / 仍需后续验证

| 项 | 状态 | 说明 |
|---|---|---|
| 分支迁移链自身能否 `upgrade head` | 未验证 | 需独立空库（另一个 database name）才能验证，本轮禁止建库 |
| `tests/integration/test_domain_roles.py` 负测 | 未执行 | 会写库，超出只读边界 |
| 迁移 `downgrade` 路径 | 未验证 | 会改结构，禁止 |
| 多租户跨租户绕过实测 | 未验证 | 需构造双租户数据（写操作），留待集成测试环境 |
| `organizations/projects` 策略允许 `organization_id IS NULL` | 已知放宽 | 遗留数据兼容口径，见 §7 |

## 7. 观察到但非本次引入的遗留风险

1. **`projects` / `organizations` 的 RLS 允许 `organization_id IS NULL`**：`(organization_id IS NULL OR organization_id = current_setting('app.current_organization_id'))`。含义是「未归属组织的行，同租户内任何组织身份都可见」。这是 F04 落地时为遗留数据留的兼容口，**属于显式放宽而非漏洞**，但它使「组织隔离」在 NULL 行上退化为「租户隔离」。建议在 `security_boundary.yaml` 登记为已知放宽，并在历史数据回填完成后收紧。
2. **11 条策略全部只写 `USING`、未写 `WITH CHECK`**：PG 对 `FOR ALL` 策略在缺省 `WITH CHECK` 时复用 `USING`，**语义正确**；但「意图」只能靠读者知道这条规则。若要可审计，应显式写出同一表达式。
3. **`app_role` 保留但无业务写入路径**：现仅有 `outbox_events` 的 SELECT 与 `outbox` schema USAGE。`build_database_url()` 文档已声明它「保留给以单一应用角色连接的测试与工具」。符合矩阵「说不出谁在写就不给写权限」的口径，属**有意保留**，不是死角色残留。
4. **`outbox` 分区 `FORCE RLS = false`**：属主为超级用户，FORCE 对超级用户无效，故不构成缺口；但与其他表的 `forced=true` 形态不一致，未来若把属主改为非超级用户需一并打开。

---

## 8. 本轮确立的动作边界

- **不移动远端 ref、不 rebase、不 merge 分支。**
- **不改写任何迁移文件。**
- **不在现库执行任何 DDL/DML/`alembic` 命令。**
- 下一步的整合方案必须建立在「以 main `5352d19` 为基底、分支能力按域重放（新 revision ID）」之上；`b3c4d5e6f7a8` 与 `9e3b4c5d6e7f` 两个 ID **不可复用**。
