# WP-04 整改方案：14 处「升级撤销既有授权、降级未恢复」

- 日期：2026-11-27
- 依据：`2026-11-27-wp04-reversibility-matrix.json`（28 不对称 / 其中 `RISK:升级撤销了既有授权，降级未恢复` 14 处）
- 授权范围：只读分析 + 隔离库实验；**不改代码、不改迁移、不提交**
- 结论先行：14 处不是同一类问题，**不能批量"补 GRANT"**。其中 11 处属于「迁移性收权」（降级必须恢复），3 处属于「纠错性收权」（降级恢复等于把安全漏洞还回去，只能登记为单向，不伪装可逆）。

## 1. 为什么要分类，而不是逐条补 GRANT

一次 `REVOKE` 在语义上可能是两件完全不同的事：

1. **迁移性收权**：旧角色原本拥有这项权限（旧口径），迁移把权限搬到新角色/新表级授权上。此时"降级不恢复"会让库停在一个**比升级前更紧**的状态——这正是本轮要修的不对称。降级必须把权限还给原角色。
2. **纠错性收权**：旧口径本身就是错的（典型是 `PUBLIC` 拥有函数执行或表全权）。此时"降级恢复"会把漏洞原样还回来；正确做法是承认这条迁移**单向**，在清单里标 `intentional_one_way` 并写明理由，而不是把它算作"可逆"。

用同一把尺子（"有没有 GRANT 对应"）去量这两类，就会得到两种错误：要么漏修（把纠错也补回去），要么把不可逆谎报成可逆。ADR-R46 的 `F 不可逆变更` 与 `E 有损` 正是为这种情况准备的容器。

## 2. 判定规则（三条，需签字）

| 规则 | 判据 | 结论 |
| --- | --- | --- |
| R1 旧口径存在 | 迁移前一个 revision 的 ACL 快照里，该角色对该对象确实有被撤销的那组权限 | 属于「既有授权」，降级必须恢复 |
| R2 旧口径是漏洞 | 被撤销的是 `PUBLIC`，且迁移注释/ADR 说明它是安全收紧 | 归 `intentional_one_way`，不恢复 |
| R3 无法证明 | 既没有前序 ACL 证据、也没有注释说明 | 不许默认恢复，标 `UNKNOWN` 并要求补证 |

取证方式（只读）：

- 迁移文件自身的注释与 ADR 引用（`docs/adr/ADR-R25` 等）；
- 前序 revision 的授权构造器（例如 `8f3b2c1d4e5a` 的 `GRANT ... ON ALL TABLES`）；
- 隔离库 ACL 快照：`pg_class.relacl` / `pg_proc.proacl` / `pg_default_acl` 三个 catalog 的差异对比（见 §5）。

## 3. 14 处清单（初判，待签字）

| # | revision | 撤销范围 | 涉及角色 | 判定 | 降级应做的动作 |
| --- | --- | --- | --- | --- | --- |
| 10 | `e5f6a7b8c9d0` | `REVOKE ALL ON FUNCTION outbox.list_active_tenants() FROM PUBLIC`（1 条） | PUBLIC | **II 纠错性** | 不恢复；登记 `intentional_one_way`：枚举租户的函数不应向 PUBLIC 开放 |
| 31 | `d8e9f0a1b2c3` | `audit.status_transitions` 撤 `UPDATE/DELETE/TRUNCATE`（角色 2 条）+ `REVOKE ALL ... FROM PUBLIC`（1 条） | project_planning_role、asset_service_role、PUBLIC | **I（角色）+ II（PUBLIC）** | 对两个角色 `GRANT UPDATE, DELETE, TRUNCATE`；PUBLIC 部分不恢复 |
| 35 | `a13b0c1d2e3f` | 5 schema × {TABLES, SEQUENCES} 默认权限撤 `project_planning_role`（15 条）+ 其他 revoke 10 条 | project_planning_role、PUBLIC | **I** | 按同一 (schema, 对象类) 组合 `ALTER DEFAULT PRIVILEGES ... GRANT ALL`；逐条镜像，不扩大 schema 范围 |
| 37 | `c3d4e5f6a7b8` | 默认权限撤 `project_planning_role`（11 条）+ PUBLIC 6 条 | 同上 | **I / II 混合** | 同上；PUBLIC 部分登记单向 |
| 38 | `d7e8f9a0b1c2` | 同 #37（17 条） | 同上 | **I / II 混合** | 同上 |
| 39 | `e8f9a0b1c2d3` | 默认权限 10 条 + PUBLIC 5 条 | project_planning_role | **I / II 混合** | 同上 |
| 40 | `f9a0b1c2d3e4` | 默认权限 10 条 + PUBLIC 5 条 | project_planning_role | **I / II 混合** | 同上（注意：D-02 已改本迁移的索引删除方式，授权部分未动） |
| 44 | `f1a2b3c4d5e6` | 默认权限撤 `qc_governance_role`（11 条）+ PUBLIC 6 条 | qc_governance_role | **I / II 混合** | 同上 |
| 50 | `e1f2a3b4c5d6` | 默认权限撤 `production_planning_role` 13 条、`asset_service_role` 13 条、PUBLIC 16 条 | 两个域角色 | **I / II 混合** | 同上；该 revision 同时是 B 类数据守卫样板，恢复动作必须排在守卫通过之后 |
| 52 | `e4f5a6b7c8d9` | 6 个域角色各 11 条默认权限 + PUBLIC 30 条（85 条） | 6 个域角色 | **I / II 混合** | 逐个角色 × schema 镜像 `GRANT ALL` 默认权限 |
| 54 | `c5d6e7f8a9b0` | `outbox_ops_role` 4 条（默认权限 2 + 表 2）+ PUBLIC 3 条 | outbox_ops_role、PUBLIC | **I（角色）+ II（PUBLIC）** | 恢复 outbox schema 默认权限与表级权限；PUBLIC 不恢复；`DROP ROLE` 已由 WP-02 移除，不再叠加 |
| 58 | `a8b9c0d1e2f3` | 默认权限撤 `qc_governance_role` 11 条 + PUBLIC 6 条 | qc_governance_role | **I / II 混合** | 同上 |
| 60 | `f8c9d0e1a2b3` | 6 个 `qc_maintenance.*` 函数 `REVOKE ALL ... FROM PUBLIC` | PUBLIC | **II 纠错性** | 不恢复；登记单向：维护函数只能由 operator 组执行 |
| 61 | `b4c5d6e7f8a9` | `qc_maintenance.repair_tenant_task` `REVOKE ... FROM PUBLIC` | PUBLIC | **II 纠错性** | 不恢复；同上 |

汇总：**I 类（需恢复）11 处**；**II 类（登记单向）3 处**（#10 / #60 / #61），另有 #31、#54 与多处混合条目中的 PUBLIC 部分同属 II。

> **2026-11-27 后续实测修正（以此为准）**：S1–S3 三个样本证明 #31、#40、#61 撤销的是"同一 revision 新建对象上的多余/默认权限"，属于 no-op，**不需要恢复**；#10、#60 同理（对象由同一 revision 创建）。因此本表"11 处 I 类"的初判**不成立**，当前状态为：5 条已证 no-restore、9 条 `unproven_blocking`、**0 条已证 I 类**。详见 `2026-11-27-wp04-s1-s3-and-d08-evidence.md`。


## 4. 实现形态（不做通用框架）

每处恢复用**冻结语句表**，与本迁移 upgrade 的撤销集合逐条镜像：

```python
# 迁移内常量：只写本迁移撤过的东西，不在运行时推导
_LEGACY_DEFAULTS = (("project_planning", "TABLES"), ("project_planning", "SEQUENCES"), ...)


def downgrade() -> None:
    ...
    for schema, kind in _LEGACY_DEFAULTS:
        op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT ALL ON {kind} TO {role}")
```

三条硬约束：

1. **镜像而非推导**：`GRANT` 只覆盖 upgrade 撤过的 (schema, 对象类, 角色) 三元组，不按当前矩阵补全；
2. **不恢复 PUBLIC**：II 类在迁移里写注释 + 清单标 `intentional_one_way`，由矩阵规则白名单化；
3. **顺序**：数据守卫（B 类）先过，再做权限恢复；恢复语句必须在破坏性 DDL 之前或之后都有明确位置（#50 是两者都有的样板）。

## 5. 隔离库实验（3 个样本，先证判定再做批量）

样本选择覆盖三种形态：

| 样本 | revision | 证什么 | 快照 |
| --- | --- | --- | --- |
| S1 | `d8e9f0a1b2c3`（#31） | 表级权限被撤后，降级能否 restore 到父 revision 的 ACL | `pg_class.relacl` |
| S2 | `f9a0b1c2d3e4`（#40） | 默认权限被撤后，降级能否重建 `pg_default_acl` | `pg_default_acl` + `pg_class.relacl` |
| S3 | `b4c5d6e7f8a9`（#61） | PUBLIC 函数权限的正确状态是"不恢复" | `pg_proc.proacl` |

步骤：建空库 → 升到父 revision → 存 ACL 快照 → 升到该 revision → 降回父 → 存快照 → 三方 diff。

判定标准：

- I 类：降级后 ACL 与父 revision 快照**逐条相同**（不是"差不多"）；
- II 类：降级后仍为收紧状态，且清单里有 `intentional_one_way` 记录；
- 任一差异都必须能解释为"父 revision 本来就没有"，否则方案不成立。

## 6. 测试与闸门（建议新增）

1. **矩阵新规则**：对每条 upgrade `revoke`，downgrade 必须有匹配的 `grant`（同 object、同 role、同 privilege 集合），或在 `intentional_one_way` 白名单里且附理由。当前 `reversibility_matrix.py` 只做计数比对，需升级到"集合比对"。
2. **单测**：14 处各一条回放断言（`_OpRecorder` 记录 upgrade 的 revoke 与 downgrade 的 grant，逐条配对）。
3. **真 PG 回归**：§5 的 S1–S3 进 CI 的往返矩阵（独立库，跑完即删）。
4. **组合验证**：14 处恢复 + WP-02 三处 + D-02 之后，跑 `head → 23`（仍受 D-08 阻断，需先解决 #24）。

## 7. 风险与顺序

| 风险 | 处理 |
| --- | --- |
| 恢复默认权限会让"未来新建表"自动带权限 | 这正是旧口径；接受，但要在清单注明"恢复的是升级前口径，不是当前最小权限目标" |
| 误把纠错性收权当 I 类恢复（安全回退） | 判定需签字；#10/#60/#61 明确不恢复 |
| 恢复 PUBLIC 权限导致审计面扩大 | 一律不恢复 PUBLIC，登记单向 |
| 一次改 14 处迁移难以定位回归 | 分三批：先 S1–S3 实验证判定，再改 #31/#54 表级，最后改 #35–#58 默认权限 |

建议顺序：**签字 §2 判据 → S1–S3 实验 → 矩阵集合比对规则 → 逐批实现 → 组合往返**。
