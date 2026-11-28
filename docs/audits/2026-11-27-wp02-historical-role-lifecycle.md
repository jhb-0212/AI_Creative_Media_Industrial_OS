# WP-02：三处历史 `DROP ROLE` 统一整改（第 7 / 23 / 54 位）

- 日期：2026-11-27
- 分支：`feat/identity-lifecycle-gov`（未提交）
- 方案：pilot 的 **C 方案**（迁移只拥有自己的授权，角色生命周期归 bootstrap）
- 结论：三处全部剥离；两条独立真库往返（跨 23+7、跨 54）**状态面 0 差异**；角色属性全程不变

## 1. 改了什么

| Revision | 链位 | 角色 | 改前 | 改后 |
| --- | --- | --- | --- | --- |
| `9e3b4c5d6e7f` | 7 | `app_role` | upgrade：`CREATE ROLE` + 已存在则 `ALTER ROLE`（含口令）；downgrade：`REVOKE` 后 `DROP ROLE` | upgrade：`assert_role_present()`；downgrade：只 `REVOKE` 本迁移授出的 schema/表/序列权限 |
| `7a8b9c0d1e2f` | 23 | `outbox_relay_role` | 同上模式 | 同上 |
| `c5d6e7f8a9b0` | 54 | `outbox_ops_role` | 同上模式（`_create_role`） | upgrade：`_assert_role()`；downgrade：删 `DROP ROLE`，保留全部 `REVOKE` / 默认权限回收 / 分区撤权 |

新增 `packages/shared/migration_identity.py`：

```python
def assert_role_present(role: str) -> None:
    """DO 块断言角色存在；缺失即 RAISE EXCEPTION，并在错误里给出修复命令。"""
```

设计要点：

1. **缺失即失败，不静默创建**。迁移建不出正确属性的角色（属性与口令的唯一来源是 bootstrap），静默创建只会造出"看起来能跑、属性可疑"的第二个事实源。
2. **断言不读口令**。原实现从 `get_settings()` 取口令写进 `CREATE/ALTER ROLE`，等于把运行时密钥变成迁移输入；剥离后迁移不再接触口令。
3. **downgrade 只撤自己的授权**。三个角色在真实集群分别有 7 库 108 条 / 9 库 222 条 / 9 库 36 条依赖 —— 迁移撤得掉自己那份，撤不掉别人的；`DROP ROLE` 因此不是"清理"，而是"拆别人的房子"。
4. 三个文件里的 `_ops_password / _relay_password / _app_password` 成为**未被调用的历史函数**，保留作"口令来源"的文字证据（不删，避免下一轮误判这些角色不需要口令）。

## 2. 真库验证（隔离库，15444 集群）

本机集群的 20 个角色是**集群级**对象，新建库天然能看到，因此实验不需要重建角色；这正好符合"角色归 bootstrap、库归迁移"的分工。

### 2.1 E1：干净链 → 跨 23+7 下行 → 回到 head

```
create afs_gov_head / afs_gov_t1        （空库，同集群）
afs_gov_head : alembic upgrade head     （参照）
afs_gov_t1   : alembic upgrade 7a8b9c0d1e2f
afs_gov_t1   : alembic downgrade 8f2a1b3c4d5e   （跨第 23 位与第 7 位）
```

结果：

| 检查 | 结果 |
| --- | --- |
| 下行是否成功 | **exit 0**，`alembic_version = 8f2a1b3c4d5e` |
| 三个角色是否仍存在 | `app_role` / `outbox_relay_role` / `outbox_ops_role` **3/3 存在** |
| 属性是否被改 | `rolcanlogin=t`、`rolsuper=f`、`rolbypassrls=f`、`rolcreatedb=f`、`rolcreaterole=f`，**与下行前一致** |
| 成员关系 | 仅 `qc_maintenance_login → qc_maintenance_operator`（以及 PG 内置 `pg_monitor` 组），迁移未动 |
| 再 upgrade head | exit 0 |
| 与干净 head 的身份面比对 | **`[EQUAL] 差异 0 条`** |

> 闸门修完（`assert_role_present(..., op=op)`）后用全新库 `afs_gov_t4` 复跑同一条路径：升 23 → 降 6 → 升 head，`[EQUAL] 差异 0 条`。六个实验库（`afs_gov_head/t1/t2/t2s/t3/t4`）事后全部 `DROP DATABASE ... WITH (FORCE)`，集群内无残留（`datname LIKE 'afs_gov%'` 为空）。

### 2.2 E2：head 克隆 → 跨 54 下行 → 回到 head

克隆方式：`pg_dump --schema-only -Fc` + `pg_restore`（保留 owner 与 ACL，去掉数据），版本位事后记为 `f2b3c4d5e6a7`（与 dev 实际版本一致）。

| 检查 | 结果 |
| --- | --- |
| `downgrade a7b8c9d0e1f2`（跨第 54 位） | **exit 0**（改前必死在 `DROP ROLE IF EXISTS outbox_ops_role`） |
| `outbox_ops_role` | 仍存在，`LOGIN`、`NOSUPERUSER`、`NOBYPASSRLS`，未变 |
| `outbox.dead_letter_replays` | 已随 downgrade 删除（本迁移自己的对象，删得对） |
| 该表 ACL | 空（对象已不存在，无残留授权） |
| 再 upgrade head | exit 0 |
| 与往返前身份面比对 | **`[EQUAL] 差异 0 条`** |

### 2.3 E3：带数据的克隆下行 —— 撞的是"数据守卫"，不是身份问题

对**带数据**的 dev 副本执行 `downgrade 7a8b9c0d1e2f`，在 `e1f2a3b4c5d6`（第 50 位）失败：

```
CheckViolation: check constraint "ck_generation_tasks_status" of relation "generation_tasks" is violated by some row
[SQL: ALTER TABLE production_planning.generation_tasks ADD CONSTRAINT ck_generation_tasks_status CHECK (status IN ('FAILED','REQUESTED','RUNNING','SUCCEEDED'))]
```

该迁移注释写明这是**刻意行为**：升级把状态集合扩到 5 值，降级要把 `AWAITING_ASSET_REGISTRATION` 等在途任务处置完才能回退。所以：

- 这是**数据可逆性**问题，必须与"身份生命周期"分开登记（见 WP-04 §4），不能混进本工作包。
- 它同时证明：全链 downgrade 的阻断点不止身份一处；把三处 `DROP ROLE` 修完，只能说"身份这一层不再阻断"，不等于"全链可降级"。

### 2.4 附带发现：D-02（`f9a0b1c2d3e4`，第 40 位）确实阻断 head → 23 的降级

把 E1 的起点换成 head（`downgrade 7a8b9c0d1e2f`，跨第 54 与新发现的 40）时，在第 40 位失败：

```
SQL: DROP INDEX project_planning.ix_project_planning_gate_decisions_gate_key
ProgrammingError: index "ix_project_planning_gate_decisions_gate_key" does not exist
```

根因（实读 `pg_indexes` 对比）：

| 动作 | 实际索引名 |
| --- | --- |
| upgrade 建立（`op.f("ix_project_planning_gate_decisions_gate_key")` + `["project_id","gate_key"]`） | `ix_qc_governance_gate_decisions_project_id_gate_key` |
| downgrade 计算（同一 f-string，但 `drop_index` 只给表名、不给列） | `ix_project_planning_gate_decisions_gate_key` |

即：`op.f(...)` 把名字交给命名约定重新生成，**建立时约定带了列名、删除时没有**，两边算出不同名字。属 D-02，不是身份问题，本轮不改 `f9a0b1c2d3e4`（超出 WP-02 授权范围），但必须登记为**全链降级的第二个独立阻断点**。

这同时修正了上一版记录里的一句误述：E1 是"23 → 6"，**没有**跨过第 40 位；跨 40 的新证据即本节。E1 / E2 的结论（跨 23+7、跨 54 均 0 差异）不受影响。

## 3. 回归测试

`tests/unit/test_role_lifecycle_specs.py`（19 项，全通过）：

- 声明自洽 / 20 个角色 / `svc_*` 8 个 NOLOGIN 无口令无域 / 域角色带域与 `migration_refs` / 成员关系方向 / `qc_worker` 不得声明；
- 8 个参数化违规（提权、`inherit=False`、非法生命周期取值）+ NOLOGIN 配口令；
- `registry_payload` 字段完整性；
- **三个迁移文件的执行用字符串常量里不得再出现 `CREATE ROLE` / `ALTER ROLE` / `DROP ROLE`**（用 AST 排除 docstring 与注释，避免"文档里解释一句就被判违规"）。

全量套件：`tests` 1980 项、`failures=0`、`errors=0`、`skipped=1`（104s，真实 PG `15444/ai_film_api_tests`）。

## 3.1 三条闸门反噬与修法（值得记）

改完后先红了一次，三处都不是"代码错"，是**闸门口径与实现形态的冲突**：

| 闸门 | 现象 | 修法（为什么不放水） |
| --- | --- | --- |
| `test_db_role_names`（禁写死默认角色名） | `role_specs._LIFECYCLE_REFS` 用 `"app_role"` 等字面量做 key，8 条违规 | 改成**角色句柄**（`app` / `relay` / `infra:outbox_ops` / `domain:asset_service` / `maintenance:login`），运行时用 `db_roles` 派生名反查。角色名仍可被部署配置改名，闸门不能为了这份表破例 |
| `data.role_grants_least_privilege`（回放迁移） | 闸门把迁移模块自己的 `op` 换成记录器，`migration_identity` 里 import 期绑死的 `alembic.op` 仍是空代理 → `NameError` | `assert_role_present(role, *, op)` 显式接收 op；三个迁移传 `op=op`。这样真实执行与闸门回放走同一条路径，不需要闸门为它开特例 |
| `test_baseline_matches_current_migration_tree` | 基线账本要求与迁移树**精确相等** | 重新生成 `data/migrations/identity_ddl_baseline.json`：`role_create` 9 → 6、`role_alter` 9 → 6，其余 11 类不变（`--check` 仍 PASS，口径仍是"只减不增"） |

## 4. 不纳入本次整改（保持原样）

| Revision | 链位 | 原因 |
| --- | --- | --- |
| `a5b6c7d8e9f0` | 62 | downgrade 已是"只撤权"的正面样板。但矩阵显示其 upgrade 仍 `CREATE ROLE qc_scheduler`、downgrade 不删 → 属"迁移创建 bootstrap 角色"的另一面，留 WP-04 统一处理 |
| `f2b3c4d5e6a7` | 64 | upgrade 退役 `qc_worker`、downgrade 重建，是**退役语义**问题，不是 bootstrap 所有权问题 → WP-03 |

## 5. 验收对照

| 判据 | 结果 |
| --- | --- |
| 三个 revision 不再有 `CREATE/ALTER/DROP ROLE` | 通过（ruff + AST 回归测试） |
| downgrade 只撤本迁移授权 | 通过（逐文件核对 `REVOKE` / `ALTER DEFAULT PRIVILEGES` / 分区撤权保留） |
| 跨 23+7 下行 | 通过，身份面 0 差异 |
| 跨 54 下行 | 通过，身份面 0 差异 |
| 角色属性全程不变 | 通过（属性查询 + 面快照） |
| 生产/预发布历史迁移 | **未触碰** |
| 正式环境执行迁移 | **未执行** |
