# WP-04：迁移可逆性审计（全链语义矩阵）

- 日期：2026-11-27
- 对象：65 个 revision（head `f2b3c4d5e6a7`）
- 授权：只读调查 + 隔离测试设计；**未改任何历史迁移**
- 判据：**状态语义可逆**，不比较 SQL 语句条数

## 1. 结论先说

1. 40 个 revision 含身份动作，其中 **28 个**在"升级做了什么 / 降级做了什么"上不对称；不对称里最严重的一类是 **升级 `REVOKE` 撤掉的既有授权，降级完全没有 `GRANT` 恢复**（矩阵里标 `RISK`）。
2. 不对称不等于错误：有 4 类确实是**升级收紧、降级不恢复**的结构性问题；也有 1 类是**设计的数据守卫**（见 §4）。
3. WP-02 修的三处（第 7/23/54 位）已经把"降级删角色"这一类清掉，并在两个隔离库里证明**往返后身份面 0 差异**。这只覆盖"角色层"，不覆盖授权层的全部不对称。
4. 本轮给出矩阵与验收协议，**不批量改 28 个文件**：先把"必须逐条证明等价"的清单固化成闸门，再按 revision 分批整改。

## 2. 方法

### 2.1 数据来源

语句级数据来自 `docs/audits/2026-11-27-identity-migration-inventory.json`（65 revision / 1097 条，运行时展开，含 `direction / operation / roles / object / sql`）。

### 2.2 配对规则（语义，不数条数）

| upgrade | downgrade 必须做到 |
| --- | --- |
| `GRANT` | 撤销本迁移新增的授权（`REVOKE`），且只撤自己那部分 |
| `REVOKE` | 恢复本迁移撤销的原有授权（`GRANT`），且必须能证明"升级前确实存在"、恢复原主体与 grant option |
| `ALTER DEFAULT PRIVILEGES`（GRANT） | 回滚为升级前的默认权限（反向 `ALTER DEFAULT PRIVILEGES ... REVOKE`） |
| `ALTER ... OWNER` | 恢复原 ownership |
| `CREATE POLICY` / `DROP POLICY` | 恢复原策略集合（含 `RESTRICTIVE` / `PERMISSIVE` 属性） |
| `CREATE ROLE` | 只有**迁移专属**角色才能由迁移删除 |
| `DROP ROLE` | **不得删除** bootstrap/governance 管理的身份 |
| `GRANT/REVOKE <role> TO/FROM <role>` | 反向成员操作，且方向不能反 |

产出：`docs/audits/2026-11-27-wp04-reversibility-matrix.json`（每个 revision 的 upgrade/downgrade 计数、findings、verdict）。

### 2.3 闸门命令（可重复）

```bash
python scripts/reversibility_matrix.py --print
```

脚本只读盘点 JSON、不连库；`--print` 逐条列出不对称 revision 的 findings，便于分批。配对规则写在脚本里（`PAIR_INVERSE` + 四条 `RISK` 判定），改规则必须改脚本并重跑，不允许在文档里口头放宽。

## 3. 矩阵读数（基于整改前的 inventory）

| 指标 | 值 |
| --- | --- |
| 含身份动作的 revision | 40 |
| 判定 asymmetric | 28 |
| 其中含 `RISK:升级撤销既有授权、降级未恢复` | 14 |
| 其中含 `RISK:升级动了默认权限、降级未回滚` | 10 |
| 其中含 `RISK:升级创建角色、降级不删除` | 4（#33 / #59 / #62 / #41） |
| 其中含 `RISK:升级退役身份、降级重建` | 1（#64） |

### 3.1 重点 revision

| 链位 | revision | upgrade 身份动作 | downgrade 身份动作 | 判断 |
| --- | --- | --- | --- | --- |
| 52 | `e4f5a6b7c8d9` | default 75 / grant 10 / revoke 85 / policy 1 | policy 1 | **最大失衡**：85 条撤销、75 条默认权限，降级只删策略 |
| 34 | `c1a2b3d4e5f6` | default 75 / grant 56 / revoke 135 | default 50 / grant 75 / revoke 25 | 数量差 135↔75、75↔50；须逐条证明恢复的是同一主体/对象 |
| 41 | `a0b1c2d3e4f5` | default 45 / grant 38 / revoke 85 / create 3 / alter 3 | grant 5 / policy 10 | 建了 3 个 bootstrap 角色、降级不删；85 条撤销只回 5 条 |
| 35 | `a13b0c1d2e3f` | default 15 / grant 6 / revoke 25 | policy 5 | 撤销与默认权限均未恢复 |
| 50 | `e1f2a3b4c5d6` | default 30 / grant 8 / revoke 42 | policy 2 | 同上（另有数据守卫，见 §4） |
| 54 | `c5d6e7f8a9b0` | （整改后）grant 5 / default 3 / policy 1 / revoke 7 | default 2 / policy 1 / revoke 6 | 角色越权已清；**仍缺"升级撤销的 7 条授权如何恢复"** |
| 59 | `e6a7b8c9d0e1` | create 4 / alter 4 / grant 5 / 成员关系双向 / revoke 3 | grant 1 | 迁移创建 4 个 bootstrap 角色；成员关系未反向 |
| 62 | `a5b6c7d8e9f0` | create 1 / alter 1 / default 1 / grant 3 / revoke 8 | default 1 / grant 2 / revoke 2 | downgrade 不删角色（正确）；upgrade 仍建 bootstrap 角色（应改断言） |
| 64 | `f2b3c4d5e6a7` | drop 1 | create 1 / alter 1 / revoke 1 / 成员 1 | 退役/复活语义 → WP-03 |
| 33 | `e0f1a2b3c4d5` | create 3 / alter 3 | **无** | 迁移建 3 个 bootstrap 角色，降级完全不管；身份泄漏 |

*注：表中第 7/23/54 位是整改后的口径；矩阵 JSON 是基于整改前 inventory 生成的，重跑 inventory 后这三行的 `create_role/alter_role/drop_role` 会归零。*

## 4. 两类必须分开的"不可逆"

| 类别 | 例 | 判据 |
| --- | --- | --- |
| **状态不可逆**（本 WP 主对象） | 升级 `REVOKE` 了某授权，降级不 `GRANT`；升级改了默认权限，降级不回滚 | 空库/纯 Schema 往返后，身份面与升级前不等价 |
| **数据兼容守卫**（不是缺陷） | `e1f2a3b4c5d6`（第 50 位）：降级把 `generation_tasks.status` 收窄回 4 值，存有 `AWAITING_ASSET_REGISTRATION` 行时 `VALIDATE` 必然失败 | 迁移注释写明"回退必须先把在途任务处置完"；这是**该失败**，不是要修成能通过 |
| **对象名不一致**（缺陷 D-02） | `f9a0b1c2d3e4`（第 40 位）：`op.f()` 在 `create_index` 时按列名生成 `…_project_id_gate_key`，`drop_index` 时按表名生成 `…_gate_key`，名字不同 → `does not exist` | 建立与删除必须用**同一个**显式名字；这是纯粹的命名 bug，修复不改变任何语义 |

E3 实测（带数据克隆 `downgrade 7a8b9c0d1e2f`）在 `e1f2a3b4c5d6` 处被 `ck_generation_tasks_status` 拦下 —— 证明这类阻断与身份无关，不能混进 WP-02。可逆性审计的报告必须把"schema 等价"与"数据前置条件"分开陈述。

## 5. 验收协议（每个 revision 至少一遍）

1. 保存 upgrade 前状态快照（身份面 JSON + `pg_dumpall --globals-only`）。
2. `upgrade` 到该 revision。
3. 保存 upgrade 后快照。
4. `downgrade` 到其父 revision。
5. 再导出状态。
6. 与步骤 1 比较。

必须比较的层（缺一层就不算通过）：

| 层 | 来源 |
| --- | --- |
| 角色属性（LOGIN / SUPERUSER / CREATEDB / CREATEROLE / BYPASSRLS / INHERIT） | `pg_roles` |
| 成员关系（含 grant option、`pg_auth_members` 方向） | `pg_auth_members` |
| Schema ACL / 表与列 ACL / 序列 ACL | `pg_namespace.nspacl`、`pg_class.relacl`、`pg_attribute.attacl` |
| Default privileges | `pg_default_acl`（按 owner + schema + objtype 分组） |
| Function ACL | `pg_proc.proacl` |
| Ownership | `pg_class.relowner` / `pg_proc.proowner` / `pg_namespace.nspowner` |
| RLS 策略（含 `polpermissive`、`polroles`、表达式） | `pg_policy` |
| 迁移创建/删除的对象集合 | `pg_class` / `pg_proc` / `pg_constraint` |

**`pg_dump --schema-only` 不能代替上述任何一层**：它不表达 grant option、不区分策略的 permissive/restrictive、不带 `pg_shdepend` 依赖、也不表达默认权限的 owner 维度。

## 6. 本轮已验证的部分（作为协议样板）

| 场景 | 结果 |
| --- | --- |
| 干净链 head ↔（升到 23 → 降到 6 → 升回 head） | `[EQUAL] 差异 0 条`（E1） |
| head 克隆 ↔（降跨 54 → 升回 head） | `[EQUAL] 差异 0 条`（E2） |

两例跑的都是 `scripts/dump_identity_surface.py` 的九层面快照，不是 `pg_dump` 文本比对。

## 7. 下一步（需裁决后执行）

1. 用整改后的三处迁移重跑 inventory，刷新矩阵（第 7/23/54 位读数会变）。
2. 按"风险类别"分批：先修 `REVOKE` 未恢复（14 个），再修默认权限未回滚（10 个），最后处理迁移建 bootstrap 角色（#33 / #41 / #59 / #62）。
3. 每批只允许"实验分支 + 隔离库 + 往返 0 差异"三件套同时满足才算过。
4. 全链往返（head → 第 1 位 → head）作为最终门禁，但需要先解决 §4 的数据守卫分级（哪些 revision 允许"有数据就失败"必须在 ADR 里显式声明）。
5. **全链往返的前置阻断已定位**：`head → 7a8b9c0d1e2f` 在第 40 位 `f9a0b1c2d3e4` 死于 `DROP INDEX ix_project_planning_gate_decisions_gate_key ... does not exist`。根因是 `op.f(...)` 配命名约定：建立时算出 `ix_qc_governance_gate_decisions_project_id_gate_key`（带列名），删除时算出 `ix_project_planning_gate_decisions_gate_key`（不带列名）。这是 D-02，与身份无关，但**必须先修**，否则下探到 39 以下永远走不通。
