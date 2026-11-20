# 迁移对账取证件（可复现）

对应 `docs/audits/2026-11-20-branch-migration-reconciliation.md` 的全部数据。
**所有 SQL 均为只读 SELECT**，不含凭据、不含写操作。

## 文件

| 文件 | 作用 | 说明 |
| --- | --- | --- |
| `q_catalog.sql` | 12 组目录导出 | schema / 表与 RLS / 列 / 约束 / 索引 / 触发器 / 函数 / 策略 / 序列 / 默认权限 / 表级授权 |
| `catalog-dump.txt` | 原始输出（647 行） | 两库各一次，`unaligned + '|'`，未手工改写 |
| `inventory.py` | 从 git 对象重建两支迁移图 | 不读工作区，固定提交 |
| `inventory-output.txt` | 迁移图输出 | head、链、ID 差异、同 ID 的文件名/内容哈希对照 |
| `reconcile.py` | 逐 revision 结构标记抽取 + 逐库核对 | 含常量解析、动态名字规避、drop 扣减 |
| `reconcile-output.txt` | 逐 revision 矩阵 | 主支链 34 行 + 功能分支独有 17 行 |
| `build_fork_inventory.py` | 生成冻结清单（五列 + 双侧 SHA256） | 哈希程序化填入，无手工转录 |
| `step_snapshot.py` | 在**全新集群**上逐 revision 重放并取样 | 每个冲突 Revision 后立即快照 |
| `snapshots/*.sql` | 三份 `pg_dump --schema-only` | `industrial` / `replay-main` / `replay-feat` |
| `steps/{main,feat}/*.json` | 步进快照原始数据 | schema/表/约束/角色/授权/成员边 |
| `diverged.json` | 10 个分叉的路径与双侧哈希 | 冻结清单的机器可读副本 |

上述后四项对应 `docs/audits/2026-11-20-fork-inventory-frozen.md`（清单）与
`docs/audits/2026-11-20-dual-path-replay-experiment.md`（实验结论）。

## 重跑

```bash
export PGPASSWORD=<readonly-credential>
PSQL=psql
BASE=(-h 127.0.0.1 -p 5432 -U <readonly-user> -X)

# 1) 迁移图
python docs/audits/mig-recon/inventory.py . main origin/feat/identity-data-ownership

# 2) 目录导出（每库一次，追加到同一个 txt）
: > catalog-dump.txt
for DB in ai_film_industrial ai_film_verify; do
  echo "########## DATABASE $DB ##########" >> catalog-dump.txt
  "$PSQL" "${BASE[@]}" -d "$DB" -f docs/audits/mig-recon/q_catalog.sql >> catalog-dump.txt
done

# 3) 矩阵
python docs/audits/mig-recon/reconcile.py . docs/audits/mig-recon/catalog-dump.txt main origin/feat/identity-data-ownership
```

## 三条注意事项

1. **必须使用只读账号**。`q_catalog.sql` 全是 SELECT，但用超级用户跑会掩盖「普通角色看不到
   `pg_authid`/`proacl` 全貌」这类边界；本轮用 `postgres` 是为了拿到全量 ACL，仅限本地开发库。
2. **不要为了「让矩阵全绿」而改抽取规则**。`无静态标记` 是诚实结论：它表示方法不下判断，
   不表示未应用。把动态 DDL 强行按前缀匹配，会把「别的表上的同名对象」算成证据（假阳性）。
3. **`b3c4d5e6f7a8` 在两支是不同迁移**。任何按 ID 比较的工具（含 Alembic 自身）都会把它当同一个节点；
   核对时必须同时比文件名与内容哈希，否则得到的是「一致」的假结论。
4. **重放实验必须用全新集群，不能复用已重放过的实例**。角色是集群级对象：复用实例时
   `CREATE ROLE` 会退化为 `ALTER ROLE`，观测不到「首次创建」分支——`9e3b4c5d6e7f` 的属性收敛差异
   恰好只在该分支之外显现（见实验报告 §7）。
5. **构造交叉组合时不要只看效果**。`b7c8d9e0f1a2` 与 `partition_guard.py` 必须成对：
   一侧迁移配另一侧 helper 会 `TypeError`，而在各自成对时效果等价。
