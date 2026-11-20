# 迁移对账取证件（可复现）

对应 `docs/audits/2026-11-20-branch-migration-reconciliation.md` 的全部数据。
**所有 SQL 均为只读 SELECT**，不含凭据、不含写操作。

## 归属栈验收清单（任何改动后**全项**绿才算完成）

| 项 | 脚本 | 证明什么 |
| --- | --- | --- |
| 工程规约 | `scripts/contract_check.py` | 契约一致 + **代码不得写死角色名**（CRED-3） |
| **覆盖完整性** | `verify_ownership_closure.py --db …` + `--selftest` | **台账之外没有别的归属表**（集合相等，豁免带可机检前提） |
| **归属一致性** | `verify_ownership_consistency.py --db …` + `--selftest` | **I-1「一致」半边**：本行 `project_id` 与父行相同（复合外键 ⊇ {父引用列, project_id}）；派生候选列必须全部被分类 |
| 例外行为 | `verify_exception.py --migration <b0c9> --versions-dir <dir>` | 从激活前基线出发，13 张守卫接通道且可安全降级 |
| 逐表覆盖 | `verify_guard_coverage.py --guard <b0c9> --fk <a9b8> --batch2 <f8a9>` | 每张表都真的接通例外、且仍受外键约束 |
| 授权完整性 | `verify_grants.py --versions-dir <dir>` | 不变量：**有表写权限 ⇒ 有函数 EXECUTE**（防保护语义漂移） |
| 闭环台账 | `acceptance_ledger.py --db …` + `--selftest` | 「真实」+「一致」两节逐行 `VALIDATE`，且台账能在四种坏状态下判 FAIL |
| RLS 行为 | `rls_acceptance.py` | 隔离项目对业务不可见/不可写、对审计可见/可写 |
| 批次行为 | `verify_batch2/3/4.py --migration <对应迁移>` | 每批：缺口可复现 / 不误伤 / `VALIDATE` 非恒真 / 降级对称 |

> **前两项**曾经不在清单里，代价是 `b0c9` 的 `CRED-3` 违规与 `d2e3` 的审计角色过度授权
> 一起漏检了一整轮——四套装置都在验「迁移行为」，没有一套验「工程规约」。
> 根因与修复见 `docs/audits/2026-11-20-audit-role-overgrant-fix.md`。
>
> **「覆盖完整性」为什么必须单列**：台账能判「这一行闭环了吗」，判不了「这张清单全不全」。
> 后者漏过两次——`qc_reports` 漏列、`episodes` 从未进入判断（`episodes` 的例外通道、
> 修数权限、RLS 旁路全都建好了，唯独归属外键没建）。两次都不是判断错误，而是压根没被
> 判断。清单完整性不能靠人复查，必须由脚本从 `pg_attribute` 实读后与台账求差。
> 根因与修复见 `docs/audits/2026-11-20-ownership-closure-episodes.md`。
>
> **「归属一致性」为什么必须与「覆盖完整性」并列而不是合并**：后者问「这张表有没有被
> 纳入」，前者问「纳入的那条约束强不强」。合并成一个闸门时，前者永远被后者掩盖——
> 28 行台账全 PASS 也可以同时意味着「每张表都被一条**只保目标存在**的外键接住」。
> 本库实测过这种状态：`qc_reports` 一行挂在 `proj-alpha` 的资产上、自己写隔离项目，
> 三列外键一路放行。根因与修复见 `docs/audits/2026-11-20-ownership-consistency.md`。
>
> **装置不写死「归属栈几层」**：层数每批都在涨。回退用
> `lab.rewind_to(keep)` 显式声明「本迁移的增量前提是哪一层」，脚本表达被测命题的前提，
> 而不是模板此刻的偶然位置——后者的代价是 `23503` 事故与 `verify_batch2` 的 P1 断言失效。
> 已应用性由各迁移自己声明的 `applied_probe_sql()` 判定：**未应用的层跳过而不是被
> 「反向应用」**（否则其 downgrade 里的 `ADD CONSTRAINT` 会重建一条早已存在的约束）。
>
> 报告产物一律写在**本目录**，不写进 `data/migrations/versions/`：那个目录的职责只有迁移脚本，
> 混入证据会让 Alembic 目录变成事实源。

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
| `verify_ownership_closure.py` | 归属闭环**覆盖完整性**闸门 | 集合相等 + 豁免可机检；`--selftest` 两侧各证伪一次 |
| `verify_batch3.py` | 批次三（`episodes`）行为验证 | 缺口可复现 / 不误伤 / VALIDATE 非恒真 / 降级可逆，15 项 |

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
