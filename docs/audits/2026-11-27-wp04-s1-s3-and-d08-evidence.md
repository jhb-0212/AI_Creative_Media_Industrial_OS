# WP-04 S1–S3 隔离实验 + D-08 只读取证（第一批）

- 日期：2026-11-27
- 授权：S1–S3 隔离实验、D-08 只读取证；**未改任何正式迁移、未提交、未推送**；实验库 `afs_s1`（一次性，保留供续做）
- 阅前结论：三个样本全部**推翻了"既有授权被撤销"的前提**——它们撤销的是**同一个 revision 新建对象上的多余/默认权限**。这不是三条无关的巧合，而是矩阵"按语句计数"规则的系统性误报。

## 1. S1：`d8e9f0a1b2c3`（#31）表级权限

| 步骤 | 读数 |
| --- | --- |
| 父 revision `c7d8e9f0a1b2` | `audit.status_transitions` **不存在**（`pg_class` 只匹配到 outbox 表） |
| 升到 #31 | 表被创建；`GRANT INSERT, SELECT ... TO roles` 后紧跟 `REVOKE UPDATE, DELETE, TRUNCATE ... FROM roles` |
| 降回父 revision | 表被删除；`relacl` 对比 **无差异** |

结论：`REVOKE UPDATE/DELETE/TRUNCATE` 针对的是两个角色**从未拥有**的权限（表在本迁移里才建、只授了 `INSERT/SELECT`），属于"新对象上写了一句防御性撤销"。**无需恢复**，降级删除对象即可满足父 revision 状态。

## 2. S2：`f9a0b1c2d3e4`（#40）默认权限

| 步骤 | 读数 |
| --- | --- |
| 父 revision `e8f9a0b1c2d3` | `pg_default_acl` 只有 1 条（outbox schema，owner=postgres） |
| 升到 #40 | 默认权限条数**仍为 1**，内容逐字段相同 |
| 降回父 revision | 与父快照 **无差异** |

结论：在干净链上，`project_planning` / `asset_service` 的默认权限在 #40 之前就已被更早的 revision（#35/#37/#38）撤掉，因此 #40 的 10 条 `ALTER DEFAULT PRIVILEGES ... REVOKE` 是**冗余 no-op**。**无需恢复**。

重要推论：**只有"第一次撤销"的那条 revision 才可能真正改变了状态**。所以 14 处里谁需要恢复，必须逐条比对"父 revision 的真实 catalog"，不能按"有没有 REVOKE"判定。

## 3. S3：`b4c5d6e7f8a9`（#61）PUBLIC 函数权限

| 步骤 | 读数 |
| --- | --- |
| 父 revision `f8c9d0e1a2b3` | `qc_maintenance.repair_tenant_task` **不存在**（proc 空） |
| 升到 #61 | 函数创建，`proacl = {owner=X, operator=X, login=X}`，**PUBLIC 不在其中**；`prosecdef=true` |
| 降回父 revision | 函数随降级删除，父状态完全恢复（proc 空） |

结论：`REVOKE ALL ... FROM PUBLIC` 撤销的是 **PG 建函数时自动附带的 PUBLIC EXECUTE**（不是"历史授权"）。这是**正确的安全收紧**（维护/修复函数不应向 PUBLIC 开放），且降级时对象整体消失，天然不需要"恢复 PUBLIC"。

因此 #61 的分类是 **II 类（安全单向）**，但其"单向"并不需要一个降级分支去守卫——降级删除对象即可。若未来该函数改为长期存在（不随降级删除），则必须显式登记 `intentional_one_way` 并禁止恢复 PUBLIC。

## 4. 参考环境（dev）的 ACL 事实（只读）

`ai_film_api_tests`（head）当前：

- `audit.status_transitions`：`project_planning_role=ar`、`asset_service_role=ar`（只有 SELECT/INSERT）——证明 #31 的收紧目标在 dev 已成立，且从未有过 UPDATE/DELETE；
- `outbox.outbox_events`：`app_role=r`、`outbox_relay_role=r`、五个域角色 `ar`、`outbox_ops_role=r`，**无 PUBLIC**；
- `pg_default_acl`：只有 outbox 一条（`outbox_relay_role=r`、`project_planning_role=ar`、`asset_service_role=ar`，owner=postgres）。

这条证据很关键：**干净链与 dev 在默认权限上并不等价**（dev 只有 outbox 一条，clean 链上 outbox 之外也没有）。所以"父 revision 的真实状态"必须以 dev/部署环境为参照补一次，不能只看干净链。

## 5. 分类修正（覆盖 2026-11-27-wp04-grant-restore-plan.md 的 11+3）

按"证据强度"重排，规则按裁决固化为 `R2 → R1 → R3`：

| 类别 | 条目 | 证据 |
| --- | --- | --- |
| 已证 no-restore（新对象/父 revision 无该授权） | #10 `e5f6a7b8c9d0`、#31 `d8e9f0a1b2c3`、#40 `f9a0b1c2d3e4`、#60 `f8c9d0e1a2b3`、#61 `b4c5d6e7f8a9` | S1/S2/S3 实测 + 迁移源码（对象由同一 revision 创建）；#10/#60 由 `a1b2c3d4e5f6` 注释与 #60 建函数确认 |
| 已证 II（安全单向，不得恢复 PUBLIC） | #61（同上）；#10、#60 若未来对象跨 revision 存活则同样按 II 登记 | S3 的 `proacl` 读数 |
| **仍未证明（`unproven_blocking`）** | #35、#37、#38、#39、#44、#50、#52、#54、#58（9 条） | 干净链上多数是 no-op，但"哪一条第一次改变状态"尚未逐条比对父 revision；dev 默认权限只有 outbox 一条，也需要按 revision 时点核对 |

**结论：此前"11 处 I 类需恢复"的初判不成立。**当前最多只能说：9 条候选需要恢复，且必须先拿到"父 revision + 参考环境"两份 ACL 证据；在拿到之前一律 `unproven_blocking`，不得为了通过矩阵而恢复。

## 6. D-08 只读取证（不修改 #24）

### 6.1 失败事实

```
Running downgrade 8f3b2c1d4e5a -> 7a8b9c0d1e2f
DROP ROLE IF EXISTS project_planning_role
asyncpg.exceptions.DependentObjectsStillExistError:
  role "project_planning_role" cannot be dropped because some objects depend on it
```

### 6.2 把库停在 #24 后测到的真实状态

在 `afs_s1` 上 `downgrade 8f3b2c1d4e5a`（从 #60 一路降，**全程成功**，说明 D-02/D-07 修复后链到 #24 是通的），随后查 catalog：

- `pg_shdepend`：该角色 **173 行**依赖（`classid=pg_class` / `pg_default_acl`，`deptype='a'`）；
- 能直接解析到对象名的 ACL 依赖：**8 张表 + 4 条默认权限**（`project_planning` 四表 `arwd`、`outbox.outbox_events` `ar`、`outbox.maintenance_runs_id_seq` `U`、`auth.revoked_tokens` `r`、`public.alembic_version` `r`；默认权限 4 条为 `project_planning`/`asset_service` 的 TABLES 与 SEQUENCES）。

### 6.3 #24 的 downgrade 覆盖了什么

`8f3b2c1d4e5a` 的 downgrade 已经明确做了"先撤销、再 DROP"：两个域的 `ALL TABLES`/`ALL SEQUENCES`、两域的默认权限、outbox schema、auth 表、`alembic_version`，最后 `DROP ROLE IF EXISTS`。

### 6.4 初步判断（**不是裁决**）

- 语句集合看起来覆盖了上面 8 表 + 4 默认权限，但 DROP 仍然失败 → **残留依赖来自它没有覆盖的对象**（例如更晚 revision 建、但降级后仍存活的对象上的 ACL，或 owner 不是 `postgres` 的默认权限条目）。
- 因此责任不能简单归给 #24 的"漏写"，也不能归给"更早的 revision 破坏了不变量"；**173 行里尚未解析到对象名的 161 行是判定关键**。
- 需要的下一步（仍只读）：把 `pg_shdepend.objid` → 具体对象（表/序列/函数/默认权限 namespace+owner），并按"创建该对象的 revision"归因；再判断修复应落在"源 revision（谁授权）"、"失败 revision（#24 清理不完整）"还是"公共契约（降级必须先清理依赖）"。

### 6.5 明确不做

- 不在 #24 加 `IF EXISTS`/条件分支掩盖；
- 不假定 #24 是根因；
- 不改正式迁移，等 §6.4 的对象级归因完成后再提最小修复方案。

### 6.6 对象级归因（已完成，证据到对象）

把 `pg_shdepend` 的 173 行按 `dbid` 拆开，结论与"本库清理不完整"的猜测不同：

| 归属 | 行数 | 说明 |
| --- | --- | --- |
| 其他数据库（`ai_film_api_tests`、`ai_film_head_verify`、`ai_film_identity_equiv`、`ai_film_pg16_clean`、`afs_struct_probe_base`、`ai_film_api_tests_stale_20261126`） | **170** | 每个库各 21 张表 ACL + 5 个 schema + 1 条默认权限 |
| `afs_s1`（当前库） | **3** | 见下 |

`afs_s1` 内部仅剩 3 条，且全部是 #24 的 downgrade **确实没有撤**的对象：

```
pg_namespace   | public                                  | USAGE/CREATE 授予（b'a'）
pg_default_acl | asset_service:r:postgres                | project_planning_role 仍在
pg_default_acl | asset_service:S:postgres                | project_planning_role 仍在
```

对照 #24 的 33 条降级语句（31 条可执行 + 2 条 `DROP ROLE`）：它的撤权循环是"**角色 ↔ 自家 schema** 配对"——`project_planning_role` 只撤 `project_planning`，`asset_service_role` 只撤 `asset_service`。因此：

1. **交叉默认权限漏撤**：`project_planning_role` 在 `asset_service` schema 的 TABLES/SEQUENCES 默认权限由 upgrade 的 `_grant_business_schema`（遍历 schema × 角色）创建，downgrade 只按配对关系撤，交叉项留下；
2. **`public` schema 的显式授权从未撤销**：downgrade 只撤了 `project_planning`/`asset_service`/`outbox`/`auth` 四个 schema，没有 `REVOKE USAGE ON SCHEMA public`；
3. **集群级依赖**：170 行来自同集群的其他库（各自 head 状态），`DROP ROLE` 是集群操作，只要任何一个库还授予该角色就无法删除。

### 6.7 D-08 根因结论（证据支撑，非"失败位置即根因"）

D-08 不是单一 revision 的笔误，而是**两层原因叠加**：

- **A 层（#24 自身，可修复）**：`8f3b2c1d4e5a` 的 upgrade 跨 schema 授予默认权限，downgrade 按"角色↔自家 schema"配对撤权，漏掉交叉默认权限与 `public` schema 授权 → 即使单库干净集群也会在 `DROP ROLE` 失败。责任在 #24（自建对象未自清）。
- **B 层（迁移链契约，非 #24 能单独解决）**：迁移链在降级里 `DROP ROLE`，而角色是**集群级对象**；同集群其他库（dev、verify、clean、stale 克隆）仍持有该角色的授权时，任何库里的降级都不可能成功。这正是 WP-02 判定"迁移不应拥有角色删除权"的同一根因，只是这次以 D-08 的形态出现。

因此修复不能只在 #24 写"再撤两条默认权限"：那只解决 A 层，B 层会让 `head → 23` 在任何共享集群上继续失败。

### 6.8 候选修复方向（未实施，待裁决）

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| F1（推荐） | 把 #24 的 `DROP ROLE` 改成"撤权 + **断言集群级零依赖**"：本库先补齐交叉默认权限与 `public` ACL 的撤销；若 `pg_shdepend` 仍有其他库的依赖，**显式失败并列出库名**，指向受控清理脚本，而不是 `IF EXISTS` 静默跳过 | 需要新增一个跨库依赖检查（只读 pg_shdepend，不需要连其他库） |
| F2 | 保留 `DROP ROLE`，在 #24 前插入一条"清理所有库对该角色的授权"的运维步骤（脚本，非迁移） | 把迁移变成依赖外部脚本，违背"迁移自包含" |
| F3 | 彻底不删角色（WP-02 口径：downgrade 只 REVOKE，不 DROP），角色由 bootstrap 退役流程管理 | 改变 #23 的"角色不存在"目标态，需要 ADR 裁决 |

F1 与 F3 都需要 ADR 层裁决，不属于本轮授权（本轮不改迁移）。


## 7. 对闸门脚本的整改输入（供下一轮实现）

1. **必须按对象+角色+权限集+grant option+grantor 比对**，不能按语句条数；
2. **必须先查对象是否由同一 revision 创建**（S1/S2/S3 都是这一类），否则会把"防御性撤销"误报为"既有授权被撤"；
3. **必须区分 `no-op`（父 revision 无该授权）与 `missing restore`（父 revision 有、降级没有）**——前者不是缺陷；
4. 白名单分两类：`intentional_one_way`（有安全证据）与 `unproven_blocking`（无证据、阻断可逆性验收），两者都必须附理由且不得互换。

## 8. 本轮状态

- S1–S3：完成，结论见 §1–§3；`afs_s1` 保留在 #24（已按 §6.6 做对象级归因，库内已执行过 31 条撤权语句，可继续用于 F1 验证或直接丢弃）。
- D-08：只读取证完成到对象级（§6.6–6.7），未改 #24；给出三方案（§6.8）待裁决。
- 分类：由 11+3 修正为"5 已证 no-restore / 9 未证明 / 0 已证 I 类需恢复"。
- 代码与迁移：本轮零改动；`docs/audits/2026-11-27-wp04-grant-restore-plan.md` 的 §3 计数以本文为准。
- 未做（下一轮）：10 条 `restore_required` 的最小修复方案（需迁移改动授权）。
- 已完成（本轮追加）：`reversibility_matrix.py` V2 集合比对 + 分级闸门（见 `2026-11-27-wp04-matrix-v2.md`）；22 个 revision 的隔离库往返分级（见 `2026-11-27-wp04-triage.md`），最终 17 条 no-op / 10 条需修复 / 1 条 #24 待裁决。
