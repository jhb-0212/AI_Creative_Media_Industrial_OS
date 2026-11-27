# #41 五表重建：数据保全独立审计

- 日期：2026-11-27
- 对象：`a0b1c2d3e4f5`（R16 五表从 `project_planning` cutover 到 `production_planning / budget_quota / qc_governance`，降级重建回 `project_planning`）
- 结论：**结构整改通过；数据保全未获得证明**。不得把本迁移描述为"已验证无损的表重建"。
- 方法：父 revision `f9a0b1c2d3e4` 一次性库 `afs_p41probe` 上的 catalog 取证 + 迁移源码审计。

## 1. 已在父态确认的事实

`afs_p41probe`（upgrade 到 `f9a0b1c2d3e4`）实测：

```
production_routes: 0      generation_tasks: 0      shot_budget_requests: 0
budget_ledger: 0          gate_decisions: 0
view dependents: 0        fk dependents: 0          user triggers: 0
```

索引清单与 P 快照一致，包含 `project_planning.ix_project_planning_gate_decisions_gate_key`
（即 #41 修复后沿用父态名的那个索引）。

这说明：**在干净链上**，cutover 时五表为空、无外键/视图/触发器依赖，`DROP TABLE` 不会连带破坏别的对象。

## 2. 未被证明的三件事

| 问题 | 事实 | 判断 |
|---|---|---|
| cutover 后是否可能有写入 | 升级把五表 `DROP` 掉，业务改写到新 schema；运行时确实会写新表 | 可能，且**未迁移**（降级只重建空表） |
| upgrade 与 downgrade 之间是否可能产生数据 | 新 schema 的表在两次迁移之间是活动写入目标 | 可能，降级 `DROP TABLE` 会直接丢掉这些行 |
| 是否有其他服务在该窗口写入旧表 | 旧表在 cutover 前是 `project_planning` 的正式写入目标；`DROP TABLE` 取 `ACCESS EXCLUSIVE` 只保证"锁后没人写"，不保证"锁前没写" | 可能存在"检查为空 → 锁生效"之间的写入，并被静默丢弃 |

## 3. 关键缺口：迁移没有强制"0 行"

源码审计（`a0b1c2d3e4f5_domain_split_r16.py`）：

- 第 10 行与第 325 行只有**注释**声明"cutover 时点五表均为 0 行"；
- 升级路径没有 `LOCK TABLE ... IN ACCESS EXCLUSIVE MODE`、没有 `SELECT count(*)` 断言、
  没有 advisory lock、没有维护窗口开关；
- `DROP TABLE IF EXISTS` 是无条件执行。

所以"0 行"是**部署时的运营假设**，不是迁移可验证的前提。任何在 cutover 前已提交的行都会
被删除，且迁移不会报错——这与"降级不可逆"是两件事：前者是**升级方向**的数据丢失风险，
后者是降级重建空表。

## 4. 结论与关闭条件

- 结构层面（对象身份、索引名、ACL）已按父态修复并复跑 0 差异，可作为隔离实验结论保留；
- 数据保全层面必须按"未证明"处理。关闭该项至少需要满足其一：
  1. **迁移内断言 + 锁**：在 cutover 前 `LOCK TABLE ... ACCESS EXCLUSIVE`，随后
     `SELECT count(*)`，非 0 则 `RAISE EXCEPTION`（把运营假设变成可验证前提）；
  2. **显式维护窗口契约**：在 ADR/运维手册中声明"本 revision 只能在停机窗口执行，
     执行前由运维证明五表 0 行"，并让迁移在非空时失败（仍需 1 的断言）；
  3. **数据搬迁**：cutover 前把旧表行复制到新 schema（当前设计明确不做）。

在上述任一项落地前，`#41` 的验收口径只能是"结构可逆"，不能是"数据无损"。
