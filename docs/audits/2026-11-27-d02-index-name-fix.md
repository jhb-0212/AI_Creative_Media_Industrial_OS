# D-02：`f9a0b1c2d3e4` 索引命名阻断修复与隔离库验证

- 日期：2026-11-27
- 范围：`data/migrations/versions/f9a0b1c2d3e4_gate_decisions_table.py`（+ 同批发现的 D-07）
- 授权边界：未提交、未推送、未合并；全部验证在 `afs-step-feat` 集群隔离库执行，未触碰 `ai_film_industrial` 与正式库
- 结论：**D-02 已修复并在隔离库通过 targeted 往返**；`head → 23` 全链仍被 D-08 阻断（非本迁移缺陷）

## 1. 根因（事实，不是猜测）

`f9a0b1c2d3e4` 的 upgrade 在 `project_planning` 建表后建三个索引，命名走 `op.f(...)`：

```
ix_project_planning_gate_decisions_tenant_id
ix_project_planning_gate_decisions_project_id
ix_project_planning_gate_decisions_project_id_gate_key
```

downgrade 用同样的 f-string 逐个 `op.drop_index(...)`。问题是**同一张表在真实链条上有三种索引名形态**：

| 形态 | 何时成立 | 索引名 |
| --- | --- | --- |
| ① 本迁移自建 | 升到 `f9a0b1c2d3e4` 后立即降级 | `ix_project_planning_gate_decisions_<列>` |
| ② 由 `a0b1c2d3e4f5` 降级重建 | 从 head 降级（`a0b1` 已执行过） | `ix_project_planning_gate_decisions_<全部列名>`（`_index_name()` 拼列名） |
| ③ 当前 head 实际存在 | head | `ix_qc_governance_gate_decisions_<列>` |

`a0b1c2d3e4f5` 的 `_index_name()` 是 `ix_{schema}_{table}_{"_".join(columns)}`，与 ① 的 f-string 只差列名拼接方式；它自己的 downgrade 会 `DROP TABLE` 再在 `project_planning` 重建五表与索引。

实测失败（修复前）：

```
head → 7a8b9c0d1e2f   第 40 位
asyncpg.exceptions.InvalidObjectNameError:
  index "ix_project_planning_gate_decisions_gate_key" does not exist
```

按名字删只能命中三种形态中的一种，且**命名缺陷被"名字不存在"这条报错掩盖成一条普通 DDL 错误**。

## 2. 修复

`downgrade()` 不再按名字删索引，只保留：

1. `DROP POLICY`（RLS 策略）；
2. `DISABLE ROW LEVEL SECURITY`；
3. `op.drop_table(_TABLE, schema=_SCHEMA)`。

索引随表删除，因此不再有"名字算错"的可能。三种形态与真实报错原文写进迁移 docstring，后续改这段的人能看到为什么不能加回 `drop_index`。

**明确禁用 `DROP INDEX IF EXISTS`**：那会让名字算错从"报错"变成"静默跳过"，随后 `drop_table` 成功、降级"看起来成功"，但索引定义是否与目标 revision 一致再无人验证。用户裁决"不用 `IF EXISTS` 掩盖错误"即此意。

回归判据：`tests/unit/test_migration_d02_index_ownership.py`（3 项，用 `_OpRecorder` 打桩迁移模块的 `op`）：

- downgrade 不存在任何 `drop_index` 调用；
- downgrade 有 `drop_table("gate_decisions")`；
- upgrade 恰好 3 个 `create_index`，且分别含 `tenant_id` / `project_id` / `gate_key`。

## 3. 隔离库验证（targeted 往返）

集群 `afs-step-feat:15444`，库 `afs_d02_c`（空库），全部命令显式带 `POSTGRES_*`：

| 步骤 | 目标 | 结果 |
| --- | --- | --- |
| 1 | `upgrade a0b1c2d3e4f5`（#41，含 #40） | exit 0 |
| 2 | `downgrade e8f9a0b1c2d3`（#39，跨 #41 与 #40） | **exit 0**（修复前在此处 exit 1） |
| 3 | `upgrade a0b1c2d3e4f5` | exit 0 |
| 4 | `upgrade head` | exit 0 |

读数：

- #39 处 `gate_decisions` 不存在（它是 #40 引入的，符合目标 revision 定义）；
- #41 处表在 `qc_governance`，四个索引名与定义**与 dev 库逐字相同**（`Compare-Object` 差异为空）：

```
gate_decisions_pkey
ix_qc_governance_gate_decisions_project_id
ix_qc_governance_gate_decisions_project_id_gate_key
ix_qc_governance_gate_decisions_tenant_id
```

- `afs_d02_c` 升到 head 后的身份面与空库直建 head 的 `afs_d02_a` 对比：`dump_identity_surface --compare` → `[EQUAL] 差异 0 条`。

## 4. 事务回滚证据（失败不留半成品）

在 `afs_d02_a`（空库直建 head）上执行 `downgrade 7a8b9c0d1e2f`，在 #24 失败后立即查库：

```
version_after_failed_downgrade: f2b3c4d5e6a7      # 仍是 head，未降级
qc_governance.gate_decisions indexes: 4           # 四个索引完好
project_planning.gate_decisions exists: 0         # 没有半迁移残留
```

注意这是**整条降级的单事务回滚**（Alembic "Will assume transactional DDL"），不是"降了一部分"。这条证据同时满足"失败时事务回滚、不留下半完成状态"的验收要求。

## 5. 同批发现的另外两个阻断

### D-07（已修）`c1a2b3d4e5f6` 降级去 REVOKE 更晚才建的表

`head → 23` 在 D-02 修复后继续前进，死在 #34：

```
relation "project_planning.scenes" does not exist
[SQL: REVOKE ALL ON project_planning.scenes FROM project_planning_role]
```

根因：该迁移 upgrade 用**冻结范围** `_GRANTED_AT_THIS_REVISION`（10 张表，不含 `scenes`），downgrade 却调用 `legacy_statements(role, domain)` 按**今天的授权矩阵**生成语句——`scenes` 是更晚的 `a13b0c1d2e3f` 建的表，在降级到 #34 时已经被删掉，于是 `REVOKE` 撞空。

修法（不扩大重构）：

- `packages/shared/db_grants.py`：`legacy_statements(..., *, tables: frozenset[str] | set[str] | None = None)`，按冻结范围过滤；`None` 保留旧行为；
- `c1a2b3d4e5f6` 的 downgrade 传 `tables=_GRANTED_AT_THIS_REVISION`，与 upgrade 同源；
- `tests/unit/test_legacy_grants_scope.py`（4 项）：冻结范围生效、排除更晚建的表、全域可用。

这与 WP-04 的"降级只回滚自己改过的东西"是同一条判据的第二个实例——不是顺手重构，是让往返路径可达。

### D-08（未修，待裁决）`head → 23` 被 pilot revision #24 阻断

继续降级到 #23 时失败：

```
Running downgrade 8f3b2c1d4e5a -> 7a8b9c0d1e2f
asyncpg.exceptions.DependentObjectsStillExistError:
  role "project_planning_role" cannot be dropped because some objects depend on it
[SQL: DROP ROLE IF EXISTS project_planning_role]
```

`8f3b2c1d4e5a` 是 pilot（identity-strip）实验 revision，其 upgrade 建两个域角色、downgrade `DROP ROLE`。**主仓该文件的 3 个未提交改动正是这一实验的产物**，按裁决必须原样留在主仓、不得混入本 worktree；因此本 worktree 里它是基线形态，`head → 23` 无法在此分支跑通。

结论：这是**已登记的实验分支状态**，不是 D-02 的残留缺陷。要拿到 `head → 23` 的读数，需要在那 3 个未提交改动所在的实验分支（另开 worktree）里跑同一套脚本，且仍需授权。本轮不擅自搬运。

## 6. 闸门

| 闸门 | 结果 |
| --- | --- |
| `ruff check .` | PASS |
| `mypy packages services apps workers` | PASS（218 files） |
| 全量 `pytest tests`（对 head 隔库 `afs_d02_c`） | **2005 passed / 0 failed / 1 skipped** |
| `alembic check` | `No new upgrade operations detected.` |
| `audit_identity_ddl --check` | PASS（基线已按本轮改动重生成） |
| `identity_registry --check` | `clean=true`（20 declared + 1 retired） |
| `architecture_check` / `contract_check` | PASS |
| `freeze_manifest --check` | **FAIL（既存问题）**：`docs/adr/adr_index.yaml` 内容与冻结清单不一致；第一轮 worktree 同样失败，说明是本分支基线漂移，非本轮改动。需要走修订流程后 `--write`，本轮不擅自重签 |

注：`test_guard_effectiveness` 曾在注入坏状态时把 `adapters/model_gateway/openai_compatible.py` 留在 `if False:  # BAD-STATE`，导致后续两次全量跑出现两条与身份治理无关的失败；该文件已恢复为 `git` 干净状态（`git diff` 为空），重跑后 0 失败。


## 7. D-02 判定

- D-02 修复本身：**完成**（代码 + 单测 + 隔离库 targeted 往返 + 回滚证据 + 索引签名与 dev 一致）。
- `head → 23` 全链：**未完成**，阻断点 D-08，原因与 D-02 无关。
- `IF EXISTS`：未引入，且有单测锁定"降级不得按名字删索引"。
