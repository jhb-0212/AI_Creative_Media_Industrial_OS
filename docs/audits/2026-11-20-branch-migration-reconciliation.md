# 主支 / 功能分支：数据库迁移逐库对账（第一轮）

> 日期：2026-11-20
> 性质：**只读取证 + 对账**。未执行 `alembic upgrade/downgrade/stamp`，未执行任何 DDL/DML，
> 未移动分支引用，未修改迁移文件。
> 固定提交：`main` = `5352d197`；`origin/feat/identity-data-ownership` = `59b13af7`
> （本地 `integration/phase1-identity-data-ownership` 与远端功能分支同一提交）。
> 原始证据：`docs/audits/mig-recon/catalog-dump.txt`（647 行，两库目录全量导出）
> 工具输出：`inventory-output.txt`（迁移图）、`reconcile-output.txt`（逐 revision 矩阵）

---

## 0. 结论（三条，其中两条与既有审查不一致）

**结论 1（推翻了「一主支库 + 一功能分支库」的前提）**
本机集群里**没有功能分支的库**。两个业务库都在**主支链**上：

| 库 | `alembic_version` | 在主支链的位置 | 判定 |
| --- | --- | --- | --- |
| `ai_film_industrial` | `c1a2b3d4e5f6` | 第 34 / 34（= main head） | 已应用到主支最新 |
| `ai_film_verify` | `b3c4d5e6f7a8` | 第 30 / 34 | **落后 4 个 revision** |

`verify` 的 `b3c4d5e6f7a8` 是**主支**的「资产项目归属」实现，不是功能分支的「资产状态迁移」。证据是结构性的，不是推断：
`verify` 有 `project_planning.organizations`（主支 `9f1a2b3c4d5e6` 的效果）、`asset_versions.project_id`，
但**没有 `audit` schema**（功能分支 `b3c4d5e6f7a8` 会建 `audit.status_transitions`）。

**结论 2（冲突面比审查报告的 1 处大一个量级）**
两支共用的 **23 个 revision 中，10 个的内容已经分叉**（13 个逐字节相同）。审查只点名了 `b3c4d5e6f7a8`。
这 10 处里：

- 1 处是**文件名都不同**的硬冲突（`b3c4d5e6f7a8`）；
- 9 处是**同 ID、同文件名、内容不同**——Alembic 对此**完全无感**（它只比 ID）。

其中 7 处**只差**「角色名来源」（主支配置驱动 `app_role_name()`，分支硬编码 `APP_ROLE = "app_role"`），
在默认配置下数据库效果等价；另外 2 处还**叠加了真实效果差异**：`9e3b` 的 `ALTER ROLE` 属性收敛与开发口令守卫、
`b7c8` 依赖的 helper 签名不兼容（见 §3.1 / §3.2）。

**结论 3（`verify` 是过期的验收库，不能当「主支当前状态」用）**
`verify` 缺的 4 个 revision 恰好全是安全收敛项，实测差距：

| 校验项 | `ai_film_industrial` | `ai_film_verify` | 对应 revision |
| --- | --- | --- | --- |
| `ck_*_status` 检查约束 | **4 条** | **0 条** | `c7d8e9f0a1b2`（#31） |
| `audit` schema + `status_transitions` | 有（4 个标记全中） | **无（0/4）** | `d8e9f0a1b2c3`（#32） |
| `FORCE ROW LEVEL SECURITY` 表数 | **7** | **0** | `e9f0a1b2c3d4`（#33） |
| 业务 schema 默认权限 | 1 行（仅 outbox） | **5 行，含 `arwd`（asset/project）** | `c1a2b3d4e5f6`（#34） |
| 触发器 / 函数 | 7 / 6 | 2 / 3 | — |
| RLS 策略数 | 11 | 10（缺审计表那条） | — |

也就是说：**拿 `verify` 去验收「主支当前的安全边界」，会得出「FORCE RLS 未启用、默认权限仍在」的结论，
而那只是因为它落后 4 个 revision。** 这与 `verify` 库是否「混跑角色」是两个问题，不能混为一谈。

---

## 1. 取证方法与边界（先说道理，再给数据）

### 1.1 为什么不能只看 `alembic_version`

`alembic_version` 只有一行 head，且它记录的是**节点 ID**，不是「执行过哪些 DDL」。
当同一 ID 在两支代表不同实现时，这一行数字对两支**同时成立**——
`verify` 的 `b3c4d5e6f7a8` 就是这种「一个数字、两种可能」的实例。

因此本轮的判定链是三层，缺一层就不下结论：

1. **迁移图**：从固定提交的 git 对象读取 revision / down_revision，重建每支的线性链，算 head；
2. **结构标记**：从迁移源码抽取它创建的对象（schema/table/column/index/constraint/trigger/function/policy/RLS），
   扣掉后续迁移 drop 掉的，再逐库核对存在性；
3. **人工锚点**：对「对象名由 f-string 拼出、或纯 GRANT/REVOKE」的 revision（无法静态抽取），
   用可判别的实测值单独锚定（§4.2 的 6 行就是这一步）。

### 1.2 工具与局限（必须写明，否则矩阵会被误读）

| 项 | 说明 |
| --- | --- |
| 抽取工具 | `docs/audits/mig-recon/inventory.py`（迁移图）、`reconcile.py`（标记 + 矩阵） |
| 常量解析 | 模块级 `SCHEMA = "audit"` 等常量会替换进 f-string，否则 `CREATE TABLE {SCHEMA}.{TABLE}` 抽不出名字 |
| 动态名字 | `tenant_isolation_{table}`、`ck_{table}_status` 这类**故意不记标记**：前缀匹配会把「别的表上的同名对象」当证据（假阳性比缺证据更糟） |
| 已知盲区 | 纯 `GRANT/REVOKE/DEFAULT PRIVILEGES` 的 revision（#8/#9/#10/#15/#20/#24~#28/#33/#34）没有静态标记，靠 §4.2 锚点与既有 E12 证据覆盖 |
| 不计入判定 | `无静态标记` ≠ 「未应用」，也 ≠ 「已应用」；它只表示本方法不下结论 |

### 1.3 本轮做过的操作（逐条列清，便于复核）

| 操作 | 性质 |
| --- | --- |
| `docker start ai-film-postgres`（容器昨晚 `Exited (255)`） | 启动服务，未改数据 |
| 12 组目录导出 SQL × 2 库（`q_catalog.sql`） | 只读 SELECT |
| 1 组集群角色 / 成员关系查询 | 只读 SELECT |
| `git ls-tree` / `git show` / `git diff`（固定提交） | 只读 |
| 未执行 | `alembic upgrade/downgrade/stamp`、`GRANT/REVOKE`、`DROP/CREATE`、任何 DML |

---

## 2. 两支迁移图

| 项 | `main` | `origin/feat/identity-data-ownership` |
| --- | --- | --- |
| 唯一 revision 数 | 34 | 40 |
| head | `c1a2b3d4e5f6` | `c4e5f6a7b8d9` |
| 链是否线性 | 是（单链 34 节点） | 是（单链 40 节点） |
| 与对方共有 | 23 | 23 |
| 共有中逐字节相同 | 13 | 13 |
| 共有中内容不同 | **10** | **10** |
| 独有 | 11 | 17 |

两支共享前 21 个节点（到 `a1b2c3d4e5f6`），从第 22 个开始分岔：
主支走 `9f1a2b3c4d5e6`（Organization 实体）→ … → `b3c4d5e6f7a8`（资产项目归属）；
分支走 `c2d3e4f5a6b7`（八域角色）→ `d3e4f5a6b7c8`（organization_id = tenant_id 回填）→ `e7f8a9b0c1d2`（项目级 RLS）→ `b3c4d5e6f7a8`（资产状态迁移）。

> 注意：分岔点在 `a1b2c3d4e5f6` 之后，而**这之前的 21 个节点里已有 8 个内容不同**
> （`9e3b`、`b5c6`、`b7c8`、`c1d2`、`c6d7`、`d4e5`、`e5f6`、`f6a7`、`a1b2` 中的 9 个）。
> 也就是说：**分岔早于「最后一个共同 revision」**，不能说「前 21 个是同一段历史」。

---

## 3. 同 ID 分叉：10 处，逐条定性

| # | revision | 主支 | 功能分支 | 差异性质 | 库效果是否等价 |
| --- | --- | --- | --- | --- | --- |
| 1 | `b3c4d5e6f7a8` | `asset_project_ownership`：资产项目归属 + 复合外键 | `asset_status_transitions`：audit 表 + 状态守卫触发器 | **不同迁移** | **否，硬冲突** |
| 2 | `9e3b4c5d6e7f` | `app_role_name()` 配置驱动；`ALTER ROLE … NOBYPASSRLS`；`reject_development_default_password` | 常量 `APP_ROLE`；`ALTER ROLE … PASSWORD`（不收敛属性）；无口令守卫 | 角色名来源 + **属性收敛 + 口令守卫** | **否**（命名配置化、已存在角色的 BYPASSRLS 是否被清、开发默认口令是否被拒） |
| 3 | `b7c8d9e0f1a2` | `protect_partition_statements(partition)`（`role=None` 默认取配置） | `protect_partition_statements(partition, roles=(APP_ROLE,))`（**helper 签名不同**） | 角色清单来源 + helper API | **否**（见下） |
| 4 | `c1d2e3f4a5b6` | 配置驱动 | 常量 | 角色名来源 | 默认配置下等价 |
| 5 | `a1b2c3d4e5f6` | 配置驱动 | 常量 | 角色名来源 | 默认配置下等价 |
| 6 | `b5c6d7e8f9a0` | 配置驱动 | 常量 | 角色名来源 | 默认配置下等价 |
| 7 | `c6d7e8f9a0b1` | 配置驱动 | 常量 | 角色名来源 | 默认配置下等价 |
| 8 | `d4e5f6a7b8c9` | 配置驱动 | 常量 | 角色名来源 | 默认配置下等价 |
| 9 | `e5f6a7b8c9d0` | 配置驱动 | 常量 | 角色名来源 | 默认配置下等价 |
| 10 | `f6a7b8c9d1e2` | 配置驱动 | 常量 | 角色名来源 | 默认配置下等价 |

### 3.1 为什么第 3 条是「效果不等价」，而不是风格差异

两支的 `packages/shared/partition_guard.py` **函数签名不兼容**：

```python
# main
def protect_partition_statements(partition, *, schema=OUTBOX_SCHEMA, role: str | None = None)
# feature
def protect_partition_statements(partition, *, roles: Sequence[str], schema=OUTBOX_SCHEMA)
```

后果是机械可判定的：

- 若 merge 后保留**分支的** `b7c8` 迁移文本、而 `partition_guard` 取主支版本 → `TypeError: unexpected keyword 'roles'`；
- 若保留**主支的** `b7c8` 文本、而 helper 取分支版本 → `TypeError: missing required keyword 'roles'`。

也就是说：**「分支的这一份迁移」与「主支的这一份 helper」不能同时存在**。
这不是「取一侧文件」能解决的，必须把两者作为一个整体决定。

### 3.2 为什么第 2 条不能只按「配置默认值相同」放过

`9e3b` 的 `ALTER ROLE` 分支版本只写 `PASSWORD`，主支版本写
`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD`。

对一个**已存在**的角色（本集群就存在，见 §5），两者的差别是：
前者保留该角色此前可能带上的 `SUPERUSER/BYPASSRLS`，后者强制收敛。
RLS 对 `BYPASSRLS` 角色不生效，这一条直接决定「数据库是不是最后一道隔离边界」。
另外主支版本会拒绝开发默认口令，分支版本不会——这条在部署到非开发环境时是真实风险。

---

## 4. 逐库逐 revision 矩阵

### 4.1 主支链（34 节点）× 两库（自动结构标记）

标注约定：`命中 n/n` = 该 revision 的全部结构标记在库中都存在；`无静态标记` = 本方法不判定（见 §1.2）；
`0/n` = 全部缺失（可判「未应用」）。

| # | revision | 文件名 | 标记数 | `industrial` | `verify` |
| --- | --- | --- | --- | --- | --- |
| 1 | `8d8d59a91012` | create_project_planning_projects_table | 1 | 命中 1/1 | 命中 1/1 |
| 2 | `e22d6fd3e8d3` | create_script_revisions_table | 0 | 无静态标记 | 无静态标记 |
| 3 | `facd8d42d07f` | add_script_active_version_unique_index | 0 | 无静态标记 | 无静态标记 |
| 4 | `0fcaf4db4573` | create_episodes_table | 1 | 命中 1/1 | 命中 1/1 |
| 5 | `6a7b8c9d0e1f` | add_script_episode_composite_fk | 1 | 命中 1/1 | 命中 1/1 |
| 6 | `7c8d9e0f1a2b` | create_outbox_events_table | 3 | 命中 3/3 | 命中 3/3 |
| 7 | `8f2a1b3c4d5e` | add_tenant_id_and_rls | 1 | 命中 1/1 | 命中 1/1 |
| 8 | `9e3b4c5d6e7f` | create_app_role_and_grants | 0 | 无静态标记 | 无静态标记 |
| 9 | `b2c3d4e5f6a7` | add_tenant_id_to_outbox_events | 0 | 无静态标记 | 无静态标记 |
| 10 | `d4e5f6a7b8c9` | restrict_outbox_grants | 0 | 无静态标记 | 无静态标记 |
| 11 | `e5f6a7b8c9d0` | add_outbox_tenant_enumeration_function | 0 | 无静态标记 | 无静态标记 |
| 12 | `f6a7b8c9d1e2` | partition_outbox_events | 0 | 无静态标记 | 无静态标记 |
| 13 | `b8c9d1e2f3a4` | create_maintenance_runs | 1 | 命中 1/1 | 命中 1/1 |
| 14 | `c1d2e3f4a5b6` | create_asset_service_tables | 5 | 命中 5/5 | 命中 5/5 |
| 15 | `b7c8d9e0f1a2` | partition_protection_and_grant_baseline | 0 | 无静态标记 | 无静态标记 |
| 16 | `d1e2f3a4b5c6` | asset_immutability_triggers | 0 | 无静态标记 | 无静态标记 |
| 17 | `e2f3a4b5c6d7` | qc_report_deviation_intent_ref | 1 | 命中 1/1 | 命中 1/1 |
| 18 | `f3a4b5c6d7e8` | outbox_relay_retry_columns | 3 | 命中 3/3 | 命中 3/3 |
| 19 | `a4b5c6d7e8f9` | cleanup_uow_test_events | 0 | 无静态标记 | 无静态标记 |
| 20 | `b5c6d7e8f9a0` | revoke_maintenance_runs_from_app_role | 0 | 无静态标记 | 无静态标记 |
| 21 | `c6d7e8f9a0b1` | create_auth_revoked_tokens | 1 | 命中 1/1 | 命中 1/1 |
| 22 | `a1b2c3d4e5f6` | fix_outbox_backlog_exclude_dead | 0 | 无静态标记 | 无静态标记 |
| 23 | `9f1a2b3c4d5e6` | create_organizations_and_ownership_fk | 1 | 命中 1/1 | 命中 1/1 |
| 24 | `7a8b9c0d1e2f` | create_outbox_relay_role | 0 | 无静态标记 | 无静态标记 |
| 25 | `8f3b2c1d4e5a` | split_domain_roles | 0 | 无静态标记 | 无静态标记 |
| 26 | `ae8724980e0b` | grant_outbox_partitions_to_domain_roles | 0 | 无静态标记 | 无静态标记 |
| 27 | `b9e1c2d3f4a5` | relay_column_level_update | 0 | 无静态标记 | 无静态标记 |
| 28 | `c1f2e3d4a5b6` | revoke_app_role_outbox_write | 0 | 无静态标记 | 无静态标记 |
| 29 | `d2e3f4a5b6c7` | organization_tenant_ownership_fk | 1 | 命中 1/1 | 命中 1/1 |
| 30 | `b3c4d5e6f7a8` | **asset_project_ownership（主支）** | 1 | 命中 1/1 | 命中 1/1 |
| 31 | `c7d8e9f0a1b2` | add_status_check_constraints | 0 | 无静态标记 | 无静态标记 |
| 32 | `d8e9f0a1b2c3` | audit_status_transitions | 4 | **命中 4/4** | **0/4** |
| 33 | `e9f0a1b2c3d4` | organization_scope_rls | 0 | 无静态标记 | 无静态标记 |
| 34 | `c1a2b3d4e5f6` | least_privilege_business_schema | 0 | 无静态标记 | 无静态标记 |

### 4.2 人工锚点（覆盖「无静态标记」的 6 个关键节点）

| 锚点 | 判别式 | `industrial` | `verify` | 结论 |
| --- | --- | --- | --- | --- |
| #31 状态枚举约束 | `ck_*_status` 存在且含全部枚举 | 4 条（projects/episodes/script_revisions/asset_versions） | 0 条 | 仅 `industrial` 已应用 |
| #32 审计事实表 | `audit.status_transitions` + 2 个触发器 + 函数 | 存在 | 不存在（`audit` schema 也没有） | 仅 `industrial` |
| #33 组织级 FORCE RLS | `relforcerowsecurity=true` 的表数 | 7 | 0 | 仅 `industrial` |
| #34 最小权限收敛 | 业务 schema 默认权限是否仍有 `arwd` | 否（仅 outbox 残留 1 行） | **是**（`asset_service`/`project_planning` 各 `arwd`） | 仅 `industrial` |
| #8/#25 角色 | 业务角色集合 | `app_role`+2 域角色+relay | 同 | 集群级，两库共享（见 §5） |
| #15/#16 不可变性与分区保护 | 触发器数量 | 7 | 2 | 仅 `industrial` 具备完整集 |

> 这 6 行互相独立，结论一致：`verify` 停在 `b3c4d5e6f7a8`，`industrial` 在主支 head。

### 4.3 功能分支独有 17 个 revision × 两库

| # | revision | 文件名 | 标记数 | `industrial` | `verify` |
| --- | --- | --- | --- | --- | --- |
| 1 | `c2d3e4f5a6b7` | split_database_roles（八域角色） | 0 | 无静态标记 | 无静态标记 |
| 2 | `d3e4f5a6b7c8` | add_organization_and_project_ownership | 0 | 无静态标记 | 无静态标记 |
| 3 | `e7f8a9b0c1d2` | add_project_scope_rls | 0 | 无静态标记 | 无静态标记 |
| 4 | `d6e7f8a9b0c1` | delivery_manifests | 8 | **0/8** | **0/8** |
| 5 | `e8f9a0b1c2d3` | qc_governance_gate_decisions | 11 | **0/11** | **0/11** |
| 6 | `f0a1b2c3d4e5` | workflow_approval_engine | 9 | **0/9** | **0/9** |
| 7 | `a2b3c4d5e6f7` | delivery_manifest_status_transitions | 2 | **0/2** | **0/2** |
| 8 | `b1a2c3d4e5f6` | shot_planning_tables | 11 | **0/11** | **0/11** |
| 9 | `d5e6f7a8b9c0` | shot_planning_spec_pack_tables | 6 | **0/6** | **0/6** |
| 10 | `a5b6c7d8e9f0` | generation_execution_route_table | 6 | **0/6** | **0/6** |
| 11 | `b8c9d0e1f2a3` | generation_task_table | 5 | **0/5** | **0/5** |
| 12 | `c9d0e1f2a3b4` | add_generation_task_rights_ref | 0 | 无静态标记 | 无静态标记 |
| 13 | `d0e1f2a3b4c5` | add_generation_task_lease | 1 | **0/1** | **0/1** |
| 14 | `e1f2a3b4c5d6` | shot_route_selection | 4 | **0/4** | **0/4** |
| 15 | `f1a2b3c4d5e6` | qc_review_sessions | 4 | **0/4** | **0/4** |
| 16 | `a9f1c2e3d4b5` | shot_route_selection_route_fk | 0 | 无静态标记 | 无静态标记 |
| 17 | `c4e5f6a7b8d9` | state_constraints | 1 | **0/1** | **0/1** |

12 个有标记的 revision 全部 0 命中；5 个无静态标记的（角色、列回填、RLS、列引用、外键）。
**没有任何一项功能分支的结构对象出现在这两个库里。**

---

## 5. 角色是集群级：一个必须单独说的证据

| 事实 | 实测 |
| --- | --- |
| 8 个 `svc_*` 角色 | **存在**（`svc_asset`/`svc_project_planning`/`svc_shot_planning`/`svc_generation_execution`/`svc_qc_governance`/`svc_workflow`/`svc_outbox_relay`/`svc_identity`），均 `LOGIN`、`NOSUPERUSER`、`NOBYPASSRLS` |
| 4 个过渡角色 | `app_role`/`project_planning_role`/`asset_service_role`/`outbox_relay_role` 存在 |
| 3 个基础设施角色 | `afs_owner`/`afs_migrator`/`ops_admin` **均不存在** |
| 角色成员关系 | 业务角色之间**没有任何**成员边（仅有 PG 内置 `pg_monitor` 三条） |
| `svc_*` 的表级权限 | **全为零**（与 E12 ⑮ 一致） |

**这说明什么、不说明什么**（必须分开写）：

- 说明：功能分支的 `c2d3e4f5a6b7`（建八域角色）**至少部分执行过**——否则这 8 个角色不会存在；
- 说明：它只执行了「建角色」这一步，**没有任何库对象**来自功能分支（§4.3）；
- **不说明**：它们是怎么被创建的（哪个库、哪次执行、是否配套跑过别的迁移）。现有证据无法区分
  「只在某个临时库执行过角色创建」与「曾执行更多但库被重建」。这是一个**未解决事实**，不是结论。
- 对退役决策的影响：Role 是集群级对象，`DROP ROLE` 的影响面**跨库**。
  ADR-R40 §7.0.4 的七项闸门（所有权/ACL/默认权限逐库 + 成员关系/会话集群级 + 可回滚版本 + 外部部署）
  对这批角色的**任何**处置都是前置条件，不能因为「表权限为零」就认为可以直接删。

---

## 6. 六态归并（按审查要求的分类）

| 状态 | 数量 | 具体 |
| --- | --- | --- |
| ① 两库都已执行 | 30 | 主支链 #1~#30 |
| ② 仅 `industrial` 已执行 | 4 | #31 `c7d8`、#32 `d8e9`、#33 `e9f0`、#34 `c1a2` |
| ③ 仅 `verify` 已执行 | **0** | — |
| ④ 两库都未执行 | 17 | 功能分支独有全部 17 个（12 个有标记的 0 命中 + 5 个无标记但无任何配套对象） |
| ⑤ 同 ID 不同效果 | **10** | §3 表；这 10 处**都落在①的区间内** |
| ⑥ 记录与结构不一致 | **0 项确证** | 见下 |

### 6.1 为什么⑤是本轮最危险的一条

`alembic_version` 说 `industrial` 执行过 `b3c4d5e6f7a8`，但**无法从库反推它执行的是哪一版**：
两个版本的差异对象（`project_id` 列 vs `audit` 表）在 `industrial` 里**都存在**
（`audit` 表由 #32 建，`project_id` 由 #30 建），所以结构证据无法区分「当时执行的是主支版本」
与「当时执行了分支版本、后来又被主支版本覆盖」。

可判定的只有一条**间接推理**：`industrial` 后续执行了 #31~#34，而这 4 个迁移依赖
主支 `b3c4` 之后的表结构（`ck_*_status` 依赖 `projects.status` 枚举、`audit` 表依赖
`asset_versions.entity_id` 类型）。若 #30 执行的是分支版本（它不改 `project_id`），
#31 之后的链不会失败但会与主支假设不符——**不会报错**，所以这条推理不能当作证明。

结论必须如实写：**「`industrial` 执行的是主支版本」目前只有「它在主支链上且后续 4 个节点全部命中」这一条弱证据，
不构成可复核的证明。** 要变强，只能在**另一个干净库**上按主支链重放到 head，与 `industrial` 逐对象 diff。

### 6.2 第⑥态：目前没有确证，但有一个「待确认项」

按定义「数据库结构与其迁移记录不一致」应为：记录说做过、结构说没做（或反之）。
本轮判为 **0 项确证**，理由是 §4 的所有 0/命中都与 head 位置自洽。
唯一未闭合的是：`verify` 的 `b3c4d5e6f7a8` 到底是哪一版（§6.1 同理）。
若要把这一项从「未闭合」变成「确证」，所需证据是 `verify` 上 `b3c4` 的执行日志或当时的迁移快照——
两者都不在本机。

---

## 7. 对合并方案的影响（逐条改写审查结论）

| 审查原结论 | 本轮证据支持的部分 | 必须修正的部分 |
| --- | --- | --- |
| 「一个已确认的阻断问题：`b3c4d5e6f7a8` 冲突」 | 成立 | **不止一个**：另有 9 处同 ID 内容分叉，其中 2 处效果不等价（§3.1/§3.2） |
| 「必须先确定两边数据库分别应用了哪些迁移」 | 成立 | 已完成：**两边不是「各一支」，而是「同一支的新旧两个时点」**（工业库=head，验收库落后 4 个） |
| 「对已应用过功能分支 `b3c4d5e6f7a8` 的数据库，必须单独设计兼容迁移」 | 原则成立 | 本机**不存在**这样的库：两库的 `b3c4` 都指向主支实现（有 `organizations`、无 `audit`）；但**不能**据此推断生产/他人环境 |
| 「功能分支 D3 会把主支资产回填成 `legacy-unassigned`」 | 成立（源码事实） | 本机无 D3 痕迹，故这是**合并后才会发生**的风险，不是现存数据风险 |
| 「共享角色要按集群级对账」 | 成立 | 实测：8 个 `svc_*` 已存在但零授权，3 个基础设施角色不存在；`DROP` 影响面跨库 |

### 7.1 由此产生的行动顺序（与审查一致的部分不再重复）

1. **先把 `verify` 升到主支 head**（4 个 revision），否则它的任何验收结论都不可用。
   —— 这是**写操作**，本轮未做，需明确授权。
2. **做一次「干净库重放」作为对照**：空库从 `8d8d59a91012` 重放到 `c1a2b3d4e5f6`，与 `industrial` 逐对象 diff。
   这是把 §6.1 的弱证据变成强证据的唯一办法，也是后续判断「10 处同 ID 分叉到底落在哪个版本」的基线。
3. **9 处同 ID 分叉先合并成单一文本**，再谈重排 revision。
   顺序反了会静默选边：Alembic 只比 ID，`alembic upgrade` 不会提示「这个 ID 的内容变了」。
4. `b7c8` + `partition_guard.py` 必须**成对决定**（§3.1）；`9e3b` 的 `ALTER ROLE` 属性收敛必须保留（§3.2）。
5. 角色处置按 ADR-R40 §7.0.4 七项闸门逐项核；「表权限为零」不是可删依据。

---

## 8. 需所有者裁决项（2026-11-20 已裁决）

| 编号 | 事项 | 所有者裁决 |
| --- | --- | --- |
| Q1 | 是否授权把 `ai_film_verify` 升级到 `c1a2b3d4e5f6` | **不批准**。保留在当前 `b3c4d5e6f7a8`，作为历史状态与结构差异的参照物；升级须在克隆库/一次性库另做 |
| Q2 | 同 ID 分叉以哪一侧为准 | **不整侧胜出**。按「已执行事实 + 目标约束 + 前向收敛」逐项裁决；`b3c4`/`b7c8`/`9e3b` 保留已执行身份，能力缺口用**唯一新 Revision** 前向补齐 |
| Q3 | 8 个 `svc_*` 角色的来源与处置 | **保留**。它们是选型 B 的目标运行时角色，按 D-9 在 IP-4 核对，不在阶段 3 退役 |
| Q4 | 生产/其他环境是否执行过功能分支迁移 | **暂列未知**。本机证据不得外推；需环境侧按实例、`alembic_version`、部署 SHA、迁移 checksum 逐项采集 |

**计数更正（2026-11-20）**：Q2 原写「8 处是配置化改造」，与同句主语的「9 处」自相矛盾。
逐文件核对结果为 **7 处配置化改造（T2）+ 1 处属性收敛/口令守卫（T3）+ 1 处 helper 调用点绑定（T4）= 9 处同路径**，
另有 1 处跨路径冲突（T1，`b3c4`），合计 **10 个分叉 revision**。

**后续唯一输入**：`docs/audits/2026-11-20-fork-inventory-frozen.md`（冻结清单，含每行双侧 SHA256 与 blob 锚点）。

---

## 9. 复现方式

```bash
# 1) 迁移图（固定提交，不读工作区）
python docs/audits/mig-recon/inventory.py . main origin/feat/identity-data-ownership

# 2) 目录导出（只读；每库一次）
psql -h 127.0.0.1 -p 15432 -U <readonly> -X -d ai_film_industrial -f docs/audits/mig-recon/q_catalog.sql
psql -h 127.0.0.1 -p 15432 -U <readonly> -X -d ai_film_verify     -f docs/audits/mig-recon/q_catalog.sql

# 3) 逐 revision 矩阵
python docs/audits/mig-recon/reconcile.py . docs/audits/mig-recon/catalog-dump.txt main origin/feat/identity-data-ownership
```

文件清单：

| 文件 | 内容 |
| --- | --- |
| `q_catalog.sql` | 12 组只读目录导出 |
| `catalog-dump.txt` | 两库原始输出（647 行，未手工改写） |
| `inventory.py` / `inventory-output.txt` | 迁移图与同 ID 对照 |
| `reconcile.py` / `reconcile-output.txt` | 结构标记抽取与逐 revision 矩阵 |
