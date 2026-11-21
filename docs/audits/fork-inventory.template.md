# 分叉清单冻结：迁移与接口层（双路径重放实验的唯一输入）

> **用途**：本文件是「隔离 PostgreSQL 实例双路径重放实验」的输入清单。实验的每一个判定结论
> 都必须能回溯到本表某一行。
>
> **状态**：**FROZEN**。本文件不随后续代码变动而更新；如需变更，出新版本并重算全部哈希。
> 生成脚本：`docs/audits/mig-recon/build_fork_inventory.py`（哈希由程序填入，无手工转录）。
>
> **不构成**：IP-3 通过、阶段 3 放行、任何角色的退役许可、任何迁移文件的改写许可。

## 0. 固定点与哈希口径

| 项 | 值 |
| --- | --- |
| main 固定提交 | `{{MAIN_COMMIT}}` |
| feature 固定提交 | `{{FEAT_COMMIT}}`（`origin/feat/identity-data-ownership`） |
| 共同起点 revision | `8d8d59a91012`（两支均存在，各 1 个文件） |
| main 迁移头 | `c1a2b3d4e5f6`（34 个文件，单头） |
| feature 迁移头 | `c4e5f6a7b8d9`（40 个文件，单头） |
| 采集时间 | 2026-11-20（GMT+8） |

**SHA256 口径（唯一）**：`git show <ref>:<path>` 输出的**原始字节**，按 LF 原样哈希。
不含 git 对象头，不做任何换行转换，不读工作区文件。

复核命令（任选其一，结果必须逐字符相同）：

```bash
git show main:data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py | sha256sum
# 或
git cat-file blob "$(git rev-parse main:data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py)" | sha256sum
```

**为什么必须钉死口径**：`ADR-R40` v1.4.2 §6.4 那张表共 6 行 SHA256，
实测其中 **2 行按 CRLF 形态算出**（`b7c8` main = `7af954b5…`、`9e3b` main = `0dd3d2fd…`），
另外 4 行按 LF 算出。同一张表两种口径 → 横向不可比、复核者按文档复现必然失败。
本清单统一为 LF，并在 §4 要求 ADR 重算。

**二级锚点**：每行另给 git blob SHA1（`git rev-parse <ref>:<path>`）。它只取决于内容字节，
不受换行、编码、工作区状态影响，可作为独立于 SHA256 的交叉校验。

## 1. 迁移分叉清单（{{TOTAL}} 个 revision）

| 类型 | 含义 | 计数 |
| --- | --- | --- |
| **T1** | 同一 Revision ID、不同文件路径、数据库效果互斥 | 1 |
| **T2** | 同路径同 ID，仅角色名来源不同（配置读取 → 本地常量） | 7 |
| **T3** | 同路径同 ID，叠加角色属性收敛与口令守卫 | 1 |
| **T4** | 同路径同 ID，叠加对 helper 接口的调用点绑定 | 1 |
| **合计** | | **{{TOTAL}}** |

{{MIGRATION_ROWS}}

### 1.1 逐条差异摘要与 blob 锚点

{{MIGRATION_NOTES}}

### 1.2 关于 T2「等价」的成立条件（不可省略）

T2 的 7 处差异在**默认配置下**不改变数据库效果，但这**不是无条件等价**：

- 主支版本调用 `app_role_name()` → `get_settings().postgres_app_user`，
  其默认值是 `"app_role"`（`packages/shared/config.py` 第 67 行）；
- 分支版本在迁移文件内硬编码 `APP_ROLE = "app_role"`。

因此等价性的**唯一前提**是 `POSTGRES_APP_USER=app_role`（或未设置而取默认）。
若任一环境的部署配置改过该变量，T2 立刻变成**真实的角色名分叉**：
主支会操作被配置指定的角色，分支仍操作 `app_role`。

> 这条前提在实验中必须显式固定并记录，否则「T2 等价」这个结论无法复核。

### 1.3 关于 T3 为什么是效果差异（不是风格差异）

对**已存在**的角色，两支的 `ALTER ROLE` 语句语义不同：

- 主支：`ALTER ROLE ... LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD ...`
  —— 重置属性，把存量角色上可能存在的 `BYPASSRLS` 收敛掉；
- 分支：`ALTER ROLE ... PASSWORD ...` —— 只改口令，**保留**存量属性。

`BYPASSRLS` 为真的角色绕过 RLS，而 RLS 是本方案的最后一道数据库边界。
所以这处差异直接决定「迁移跑完后数据库还是不是隔离边界」，不能按默认配置一笔带过。

此外主支在迁移内调用 `reject_development_default_password(...)`，分支没有该守卫。

## 2. 接口层分叉（非 revision，决定「能否组合」）

这一层不体现在 `alembic_version` 里，但决定「取一侧迁移文件 + 另一侧共享模块」是否可运行。

{{INTERFACE_ROWS}}

### 2.1 逐条说明

{{INTERFACE_NOTES}}

## 3. 组合约束矩阵（为什么不能「取一侧文件」）

迁移文件与共享模块是**版本耦合**的，交叉组合会在运行期失败。已核实的四种组合：

### 3.1 `db_roles.py` × 迁移

| 组合 | `app_role_name` 是否存在 | 迁移如何取角色名 | 结果 |
| --- | --- | --- | --- |
| main 迁移 + main `db_roles` | 是 | `from ... import app_role_name` | 可运行 |
| feature 迁移 + feature `db_roles` | 否 | 文件内 `APP_ROLE` 常量 | 可运行 |
| **main 迁移 + feature `db_roles`** | **否** | `from ... import app_role_name` | **ImportError** |
| feature 迁移 + main `db_roles` | 是 | 不 import，用本地常量 | 可运行（main 的多余导出无害） |

**这条约束的严重性**：整合时最可能的做法是「以 main 为底座、吸收 feature 的域池实现」，
即保留 main 的历史迁移文件（迁移历史通常不改）但合入 feature 的 `db_roles.py`。
按上表，这正是 **ImportError** 那一行——历史迁移将无法重放。
双路径实验必须把这一组合单独跑一遍，否则这个坑要到生产重放时才暴露。

### 3.2 `partition_guard.py` × `b7c8`

| 组合 | 调用形态 | 结果 |
| --- | --- | --- |
| main helper + main `b7c8` | `protect_partition_statements(partition)`（用默认 `role=None`） | 可运行 |
| feature helper + feature `b7c8` | `protect_partition_statements(partition, roles=(APP_ROLE,))` | 可运行 |
| **main helper + feature `b7c8`** | 传 `roles=` | **TypeError**（unexpected keyword argument 'roles'） |
| **feature helper + main `b7c8`** | 不传 `roles` | **TypeError**（missing required keyword-only argument 'roles'） |

所以 `b7c8` 与 `partition_guard.py` **只能成对选择**，不能各取一侧。

### 3.3 结论

「按文件取最新版本」在这两处会产生**不可运行的组合**。任何整合方案必须先定接口契约、
再统一改全部调用点（当前调用点仅 `b7c8` 一处，但需在整合后重新全量检索）。

## 4. 与 ADR-R40 v1.4.2 的差异（两者不是同一份清单）

| 项 | ADR v1.4.2 | 本清单（实测） | 处置建议 |
| --- | --- | --- | --- |
| 分叉 revision 数量 | 记载 **3** 个（`b3c4`/`b7c8`/`9e3b`） | **{{TOTAL}}** 个 | ADR 需扩为 10 项，或明确「只列效果不等价项」并给出其余 7 项的等价条件 |
| `b7c8` main SHA256 | `7af954b5…`（**CRLF 口径**） | LF 口径见 §1 | ADR 该行需重算 |
| `9e3b` main SHA256 | `0dd3d2fd…`（**CRLF 口径**） | LF 口径见 §1 | ADR 该行需重算 |
| 接口层分叉 | 无 | 5 项（§2） | 建议纳入；否则 §3 的组合失败无法被预判 |
| 分叉计数口径 | 未定义「同路径/跨路径」 | T1~T4 四类 | 建议采用本分类 |

> 另需注意：ADR 中 `§7.0.4` 的正文**确实存在**（第 723 行），但它是加粗段落而非标题，
> 且**排在 `§7.0.3` 之前**（7.0.4 在第 723 行、7.0.3 在第 760 行）。这是章节序缺陷，
> 不影响内容，但会让复审误判为「条款不存在」。修正属于排版级改动，会改变文档 SHA256，
> 故本轮**未动**，列为待裁决项。

## 5. 计数纠正：是 7 不是 8

前一份报告（`2026-11-20-branch-migration-reconciliation.md`）第 331 行写作
「8 处是配置化改造，1 处是属性收敛+口令守卫，1 处是 helper API」，合计 10，
但同一句的主语是「9 处同 ID 分叉」——**自相矛盾**。

实测（逐文件 `git diff -U0` 全量核对差异行）的正确分解：

```
同路径同 ID 分叉  9 = T2(7) + T3(1) + T4(1)
跨路径同 ID 分叉  1 = T1(1)
────────────────────────────────────
revision 总数    10
```

即：**「配置化改造」是 7 处，不是 8 处**。另有一处 `b3c4d5e6f7a8` 是跨路径冲突，
不属于「同 ID 同路径」的 9 处，但计入 10 个分叉 revision。

## 6. 重放实验的可判定范围（严格）

| 问题 | 重放能否判定 | 依据 |
| --- | --- | --- |
| 某 Revision 的 main 版与 feature 版**结构效果差异** | **能** | 两套实例分别重放，逐对象 diff |
| 现存 `ai_film_industrial` 的结构**更像哪一版** | **能**（结构层） | 与两套实例的最终结构比对 |
| 历史上**实际执行的是哪一份文件** | **不能** | 只能靠部署日志 / 迁移作业记录；`alembic_version` 只存 ID |
| feature 独有的 17 个 revision 是否执行过 | **能** | 对象存在性检查（已初判：两库均无分支 schema 对象） |
| 生产及其他环境是否执行过分支迁移 | **不能** | 本机证据不外推 |
| 某个角色的口令/凭证来源 | **不能** | 不在结构层 |

> 结论：实验能给出「哪一版的效果与现存结构一致」，**不能**给出「当时执行了哪一份文件」。
> 两者不可互相替代。

## 7. 实验边界（所有者裁决，不可放宽）

- 只允许在**独立、可销毁的 PostgreSQL 实例**中写入；
- **不得**在现有 PostgreSQL 集群内新建库来代替（角色与成员关系是集群级共享对象）；
- 不修改 `ai_film_verify`（保留在当前 `b3c4d5e6f7a8`）、不修改 `ai_film_industrial`、不动生产；
- 不修改 GitHub 分支、迁移文件或 Revision ID；
- 不执行任何 `svc_*` 的 `DROP`、`REVOKE` 或重建；
- 本实验结果**不等于** IP-3 通过，**不等于** ADR-R40 阶段 3 放行。

## 8. 待裁决 / 未决

| # | 事项 | 状态 |
| --- | --- | --- |
| 1 | ADR §6.4 表 2 行 SHA256 为 CRLF 口径，需重算 | 未改（改则文档 SHA256 变化，需所有者确认） |
| 2 | ADR 分叉记载为 3 项，实测 10 项 | 未改 |
| 3 | ADR `§7.0.4` 排在 `§7.0.3` 之前（章节序） | 未改（排版级） |
| 4 | 前一份报告「8 处配置化改造」应为 7 处 | **本轮已在报告内修正** |
| 5 | 双路径实验执行 | 已获批准，清单冻结后开展 |
| 6 | 生产及其他环境的迁移状态 | 未知，需环境侧取证 |
