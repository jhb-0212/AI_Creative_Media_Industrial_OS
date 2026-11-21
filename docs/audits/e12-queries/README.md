# E12 采集脚本（可复现）

对应 `docs/audits/2026-11-19-r40-readonly-evidence.md` 的 **E12** 节，
原始输出存档：`docs/audits/2026-11-19-r40-e12-raw.txt`。

这些文件是**纯 SELECT 查询**，不含任何凭据、不含任何写操作。
用于让复核者不依赖我的环境即可重跑并逐行比对。

## 文件

| 文件 | 对应 | 作用域 | 说明 |
| --- | --- | --- | --- |
| `qc_infra.sql` | E12.1 | 集群级（`-d postgres`） | 三个基础设施角色的属性 + `role_exists` + 成员边计数 |
| `qc_inv10b_judge.sql` | E12.2 | 集群级（`-d postgres`） | **INV-10b 合取式判定**，输出 `PASS` / `FAIL`；角色缺失时输出 FAIL |
| `qc_inv10b.sql` | E12.3 | 集群级（`-d postgres`） | 原始违规边查询（对照用，展示「0 行」的真空真形态） |
| `qc_closure.sql` | E12.4 | 集群级（`-d postgres`） | 递归成员关系传递闭包（admin/inherit/set/grantor/depth/path/cycle） |
| `q09.sql` | E12.5/E12.6 | **每个业务库各一次** | ⑨ 全量有效权限，`relkind IN ('r','p')` + 输出 `relkind` |
| `q13.sql` | E12.7/E12.8 | **每个业务库各一次** | ⑬ 分区父子 ACL 对照（**含 `COLLATE "C"` 修复**） |
| `q14.sql` | E12.9/E12.10 | **每个业务库各一次** | ⑭ 全对象类型最终有效权限（6 类对象） |
| `q15.sql` | E12.11/E12.12 | **每个业务库各一次** | ⑮ 零权限补集布尔矩阵（显式 f/t） |

## 重跑方式

```bash
export PGPASSWORD=<readonly-credential>
PSQL=psql
BASE=(-h 127.0.0.1 -p 5432 -U <readonly-user> -X -P pager=off)

# 集群级（一次）
for f in qc_infra qc_inv10b_judge qc_inv10b qc_closure; do
  "$PSQL" "${BASE[@]}" -d postgres -f $f.sql
done

# 库级（每库各一次）
for DB in ai_film_industrial ai_film_verify; do
  for f in q09 q13 q14 q15; do
    "$PSQL" "${BASE[@]}" -d "$DB" -f $f.sql
  done
done
```

## 注意事项

1. **必须用只读账号跑**。E12 当初是用 `postgres` 超级用户跑的，所以结论只到「catalog 状态」，
   不代表真实角色的运行时行为（超级用户绕 RLS）。要验证 RLS/ACL 行为必须用真实域角色连接（§12 E1）。
2. `q13.sql` 的 `COLLATE "C"` 不可删。去掉会在 PG16 报
   `recursive query "t" column 6 has collation "default" ... but "C" overall`。
3. Windows 命令行下不要在 `psql -c` 里直接传中文（会因 locale 编码报 `invalid byte sequence for encoding "UTF8"`），
   `qc_inv10b_judge.sql` 含中文，请用 `-f` 执行。
