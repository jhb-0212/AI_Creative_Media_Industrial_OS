# WP-INDUSTRIAL-LINEAGE：`ai_film_industrial` 谱系调查（只读）

- 日期：2026-11-27
- 库：`localhost:15444/ai_film_industrial`（`afs-step-feat` 集群）
- 授权：只读；**不改库、不改迁移、不 stamp、不删角色/对象**
- 结论：**从 UNKNOWN 升级为「已识别的分支谱系」**——该库跑的是 `feat-ownership` 分支链，最新停在 `o5c6d7e8f9a0`；仍**保持隔离**，不并入 main 链

## 1. 关键证据

### 1.1 revision 有源，且在仓库里

| 证据 | 结果 |
| --- | --- |
| `alembic_version` | `o5c6d7e8f9a0` |
| 仓库是否存在该 revision | **存在**：`data/migrations/pending/feat-ownership/o5c6d7e8f9a0_shot_revision_reference_edges.py` |
| 其父链 | `o5c6d7e8f9a0 ← n4b5c6d7e8f9 ← … ← a9b8c7d6e5f4 ← c4e5f6a7b8d9`（`c4e5f6a7b8d9` 是分支链 head，**不在 main 65 个 revision 内**） |
| 引入提交 | `d90a90f`（批次十/十一，只落在 `pending/`） |
| 后续触碰 | `157520b`（lint 清理） |
| 是否在 `versions/` 任一存活 ref 里 | **否**。`git ls-tree` 扫描所有本地/远程分支的 `data/migrations/versions/` 均无该文件；只有 `pending/feat-ownership/` 有 |
| 是否有 tag | 否（3 个 tag 均为备份用） |

所以它不是"被删除的历史迁移"，也不是"手工 SQL"：**源文件一直在仓库里，只是刻意放在 alembic 扫描范围之外**（`pending/README.md` 写明"放进 `versions/` 会造出第二个 head，`alembic upgrade head` 立刻二义性"）。

### 1.2 库内对象与该 revision 对得上

| 检查 | 结果 |
| --- | --- |
| `o5c6` 建立的三个外键 | `fk_sr_basis_script`、`fk_sr_constraint_pack`、`fk_sr_supersedes` **全部存在**（`shot_planning.shot_revisions`） |
| `o5c6` 建立的两组四列唯一键 | `uq_script_revisions_entity_project_tenant_org`、`uq_constraint_packs_entity_project_tenant_org` 存在 |
| 前序批次产物 | `project_planning.organizations`、`ownership_repair_map`、`rls_policy_backup`、`project_planning.scenes`、`shot_planning.shots`、`workflow.rework_tasks` **全部存在** |
| 关系总数 | **39** 个（此前报告写的"18 张业务表"是只统计了带域归属列的子集，读数口径不同，不是矛盾） |
| `afs_audit` / `afs_data_repair` | **集群级已不存在**（与 `pending/README.md` 记载的 2026-11-26 退役一致）；`quarantine_*` 策略已随之移除 |
| 集群内 `*_role` | 8 个（main 链的域角色），无 `afs_*` 残留 |

结论：库的状态与 `pending/feat-ownership` 批次 1–11 的产物集合**一致**，不是"半截迁移"或"乱序手工执行"。

### 1.3 谱系形状

```
main 65 revision 链（head f2b3c4d5e6a7）
   └─（分叉点之前共享一部分历史）

feat-ownership 分支链（head c4e5f6a7b8d9）
   └─ a9b8 → b0c9 → c1d2 → d2e3 → e3f4 → f8a9 → g7b8 → h8c9 → i9d0 → j0e1 → k1f2 → l2a3 → m3b4 → n4b5 → o5c6d7e8f9a0  ← ai_film_industrial 在这里
```

`integration/phase1-identity-data-ownership` 分支现存 head 只到批次三（`838ea99`），说明**激活用的分支已不再保留完整副本**；但源在 `pending/`，谱系可解释。

## 2. 裁决对齐

用户裁决：`ai_film_industrial` 保持隔离调查、不纳入本轮迁移修改；「找到其他分支迁移链 → 建立独立 lineage，不强行并入当前链」。本次调查结果正好落在该分支。

| 禁止项 | 本轮执行情况 |
| --- | --- |
| `alembic stamp head` | 未执行 |
| 伪造 revision 映射 | 未执行 |
| 手改 `alembic_version` | 未执行 |
| 对当前链 `downgrade` | 未执行 |
| 删角色/对象让它"符合" Registry | 未执行 |
| 写入该库 | 未执行（全程只读查询） |

## 3. 仍需补的证据（建议 WP-INDUSTRIAL-02）

1. **重放验证**：在隔离空库上从 `c4e5f6a7b8d9` 起按 `pending/feat-ownership` 顺序跑到 `o5c6`，与 `ai_film_industrial` 做九层面比对（角色/成员/ACL/默认权限/owner/策略/对象集合）。这能证明"库=源文件执行结果"，而不是"库=某次手工改动"。
2. **ownership 基线**：该分支的核心产物是归属外键与受控例外。需登记 35 条归属外键的 `convalidated` 状态、`ownership_repair_map` 行数、`rls_policy_backup` 行数，作为"历史解释"的可核对基线。
3. **角色引用**：确认该库 ACL 里是否还有已退役的 `afs_*` 授权残留（当前只有对象层面的检查，未逐条扫 ACL grantee）。
4. **收敛方案**：该库要么永久保留为分支谱系样本（只读、不参与 CI），要么整体归档；**不允许**把它升级到 main head——两条链的对象集合不同（分支链独有 `organizations` / `scenes` / `rework_tasks` / `rls_policy_backup`），强行 upgrade 只会撞出一堆"表已存在/约束缺失"。

## 4. 对 WP-01 的影响

该库**不参与** `identity_registry --check`：它没有 `afs_*` 角色、也没有 main 链后段（第 54–64 位）的域角色授权。把它纳入对账只会产生"库里有、声明里没有"的噪声，掩盖真正的差异。登记为**独立 lineage 环境**，不进默认对账集合。
