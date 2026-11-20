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
| main 固定提交 | `5352d1979421f41bfb9943774378671cfe94de71` |
| feature 固定提交 | `59b13af73640d10b81703b514202173976c432c5`（`origin/feat/identity-data-ownership`） |
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

## 1. 迁移分叉清单（10 个 revision）

| 类型 | 含义 | 计数 |
| --- | --- | --- |
| **T1** | 同一 Revision ID、不同文件路径、数据库效果互斥 | 1 |
| **T2** | 同路径同 ID，仅角色名来源不同（配置读取 → 本地常量） | 7 |
| **T3** | 同路径同 ID，叠加角色属性收敛与口令守卫 | 1 |
| **T4** | 同路径同 ID，叠加对 helper 接口的调用点绑定 | 1 |
| **合计** | | **10** |

| # | 路径（两侧） | Revision | main SHA256 | feature SHA256 | 冲突类型 |
| --- | --- | --- | --- | --- | --- |
| 1 | main: `b3c4d5e6f7a8_asset_project_ownership.py`<br>feat: `b3c4d5e6f7a8_asset_status_transitions.py` | `b3c4d5e6f7a8` | `fbc0da26e3caf0fc99ef8464fb780d231451ed83362a2bf004f94a50382a4823` | `0d1c44d24f10d3b231a29457fd5968c35b8a71b43a446ae0a2d15957f7a6e606` | **T1** |
| 2 | `data/migrations/versions/9e3b4c5d6e7f_create_app_role_and_grants.py`<br>（两侧同名） | `9e3b4c5d6e7f` | `76e330519a95d2981b1b3b414294d3c207d62ace3406bb529ba63f2b31af3418` | `57c3d775ac07e90a8ecce53e39449f226c3cd68d38f9c984f2d474130ad81497` | **T3** |
| 3 | `data/migrations/versions/a1b2c3d4e5f6_fix_outbox_backlog_exclude_dead.py`<br>（两侧同名） | `a1b2c3d4e5f6` | `5945a047a92e6061ec46c8eda891ac30cd588bf83104a1db5326e959dd6fdd48` | `a3913fcb5fabfebfa3c24837f84258f6f367723bcfec0a843a08171fe4733a99` | **T2** |
| 4 | `data/migrations/versions/b5c6d7e8f9a0_revoke_maintenance_runs_from_app_role.py`<br>（两侧同名） | `b5c6d7e8f9a0` | `2231518088b96bbac71a20d0bf40ff7b13dc421fef801c01dbd289e5cf4f9b25` | `f686de889d1e84b46825e0d5003ef674018b4137d8fbb5e7a5e99d1290efcf07` | **T2** |
| 5 | `data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py`<br>（两侧同名） | `b7c8d9e0f1a2` | `0caf8be5f0bd19c10d7c8349c24be9532547e1925c1a55df99951a5a9317630c` | `2a8d7d2395c5d936b6e6be2be393c437a781f664c68d0f26773d7fe210f27c8f` | **T4** |
| 6 | `data/migrations/versions/c1d2e3f4a5b6_create_asset_service_tables.py`<br>（两侧同名） | `c1d2e3f4a5b6` | `4e3e9dbfaf58622a69297ce703b73b51ecedfc8716e829e5864c6c5dd2f921c3` | `525a23270fd78c89e6e8ee8bfc0946e93fdcc0ab6730e675299c99adf2c45809` | **T2** |
| 7 | `data/migrations/versions/c6d7e8f9a0b1_create_auth_revoked_tokens.py`<br>（两侧同名） | `c6d7e8f9a0b1` | `ad6705cb7a04520a178161d9ff3db54d1d1e29c5c5cbb81359dfeab40720d29f` | `bd40ef42796117989f8d388b57ca4784ec68f1a7707de44a7a21b0b480345645` | **T2** |
| 8 | `data/migrations/versions/d4e5f6a7b8c9_restrict_outbox_grants.py`<br>（两侧同名） | `d4e5f6a7b8c9` | `4c468f56490c1691cabfb37a3f9acbbaed2cc54ffde982c59e9228ad89c399b9` | `239b00967fa9d0ab2e53e3edb4cc44d4bf8270602f1ae4916666367649488fe0` | **T2** |
| 9 | `data/migrations/versions/e5f6a7b8c9d0_add_outbox_tenant_enumeration_function.py`<br>（两侧同名） | `e5f6a7b8c9d0` | `e6a1ae52d9ae43e4213cbdef2720ac11cb740f5c4a1310931d0e3ccc23e4fe0a` | `3a17dbac31b58117a95a2a03d1e0fcd1b79b8b8fc5fd0ea1ebc67f3a18cafc4c` | **T2** |
| 10 | `data/migrations/versions/f6a7b8c9d1e2_partition_outbox_events.py`<br>（两侧同名） | `f6a7b8c9d1e2` | `4cc9a1291d964240876fc8c360e8988574987b01aaeb6ddb76b3922482ec1950` | `e01331efb969f66490f738d17349c4286890d5f78ad3095bb893486613490145` | **T2** |

### 1.1 逐条差异摘要与 blob 锚点

| Revision | 类型 | 差异摘要 | main blob SHA1 | feature blob SHA1 |
| --- | --- | --- | --- | --- |
| `b3c4d5e6f7a8` | T1 | 文件名与数据库效果均不同：主支建资产→项目复合外键；分支建状态转移表并改写资产状态守卫 | `dbc51ce794d95cd62245edde52ab7f5ca3cb64dd` | `8899ef436376c211af3b91a403e7d482a9afa89d` |
| `9e3b4c5d6e7f` | T3 | 角色名来源 + 属性收敛：主支 `ALTER ROLE ... NOBYPASSRLS NOCREATEDB NOCREATEROLE` 重置存量属性并拒绝开发默认口令；分支仅 `ALTER ROLE ... PASSWORD` | `3b5b6753dc4876622e0a74a5fa0f84e91f8fe145` | `41faff78a58e068775db8befa6745c8d5bc99691` |
| `a1b2c3d4e5f6` | T2 | 仅角色名来源：`app_role_name()` → 本地常量 `APP_ROLE` | `78a31e49b267a710fcab12ed00c21398b6bf423c` | `7285b67106ec943fe0c82a3c37dbea74dbbd286f` |
| `b5c6d7e8f9a0` | T2 | 仅角色名来源 | `cc756baa79df3d3732a46d5a30f8396227abf2b5` | `a08f17c95da21e02384750572d7b7fde104663a9` |
| `b7c8d9e0f1a2` | T4 | 角色名来源 + 调用点绑定 helper 接口：分支对 `protect/unprotect_partition_statements` 必须显式传 `roles=(APP_ROLE,)` | `dbae1fc43715b33c6334a86e25debb7a915f69ed` | `816a0f1c337601d36b2309646622c208cc56df81` |
| `c1d2e3f4a5b6` | T2 | 仅角色名来源 | `91d1f7308a032e4338208d7d6ebd5118b9570ac6` | `26f543e8a7381bc46db33823bfed6dfd4e98069c` |
| `c6d7e8f9a0b1` | T2 | 仅角色名来源 | `fe3e78d772a7e206c23958d17712ce3a610d2f14` | `80a1b71e7f99869a2e765f8288530c87ab5582d2` |
| `d4e5f6a7b8c9` | T2 | 仅角色名来源 | `b57a6d2ede1eeb0caadc2f18b0131f6a0f1c2617` | `2079b1654780346f38d2ee51b1e3a4d677482255` |
| `e5f6a7b8c9d0` | T2 | 仅角色名来源 | `760cf198c78df011d15d526f61fdf791796849cb` | `3c0b563d118680778f3ec5df661897c820d82733` |
| `f6a7b8c9d1e2` | T2 | 仅角色名来源 | `8c536fc20776105f0508fdd5d652bc0b62d07e57` | `7c16a5aad1a4c60b80ab3d488ba17e92de15648e` |

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

| # | 路径 | 接口 / 符号 | main SHA256 | feature SHA256 | 冲突类型 |
| --- | --- | --- | --- | --- | --- |
| 1 | `packages/shared/partition_guard.py` | `protect_partition_statements / unprotect_partition_statements` | `81721bca5accf17c1c8dff1afc2abb22e88d8fc385ecb18ce03fdc1dcdd55858` | `9d497928ecbcd0f273b6594aaa5ca00c9529d01445bb485f7122ce4cc20d1a30` | **T5-接口** |
| 2 | `packages/shared/db_roles.py` | `app_role_name / role_for / all_business_roles` | `63484a9443ff7049db89a178eb5d70ac5dd621b1e439e1ae24bdda7fe8d7505b` | `a6b324a589ad2795d4ce22cacae566a364ea5006438af23f13062a5962f9f9d4` | **T5-接口** |
| 3 | `packages/shared/database.py` | `build_database_url / build_migration_url / DomainPools` | `c5cdbb91d2e0c659175685fa68a4d44a5d1eabff66b35fac30ed5befa404519c` | `2067e94325867381b79d2378d81092d03733f7963c224eb46a049bbea9550763` | **T5-接口** |
| 4 | `packages/shared/config.py` | `Settings 域连接池字段` | `cbfd0f2cece3ea8b2b121fc5e19d8f862bb3827336e67b9a0a6757bafcaab074` | `df5208dea6664cb33924dc6e0f9cd8e79775b82755a549b142e536ee1df4407d` | **T5-配置** |
| 5 | `infra/compose/docker-compose.yml` | `x-db-env 锚点 / 容器 environment` | `0679822e4ec232dbb5cceec695fbb94d0c7072ba0770b37fbaebe657962dd7c8` | `7429ce29d71de05220b4b9bc4cca4d1daa1b1f9cf8a56dfee75f60f5331ba826` | **T5-配置** |

### 2.1 逐条说明

| 路径 | 类型 | 差异摘要 |
| --- | --- | --- |
| `packages/shared/partition_guard.py` | T5-接口 | 主支 `role: str | None = None`（单角色、可省略）；分支 `roles: Sequence[str]`（角色集合、必填）。参数名、类型、必填性三者同时不同 |
| `packages/shared/db_roles.py` | T5-接口 | 符号集不相交：主支导出 `app_role_name()`、`relay_role_name()`、`domain_role*`；分支导出 `role_for()`、`all_business_roles()` |
| `packages/shared/database.py` | T5-接口 | `build_migration_url` 两侧都存在（主支第 89 行、分支第 73 行），**不是分支独有**；真正的差异是分支新增 `DomainPools`/`_DomainPool` 与按域取 URL 的入参（`domain: DatabaseDomain`） |
| `packages/shared/config.py` | T5-配置 | 分支按八个 DatabaseDomain 组织连接配置；主支按四个应用角色组织 |
| `infra/compose/docker-compose.yml` | T5-配置 | 分支第 78、111 行（api、relay）以 `<<: *db-env` 继承含 `POSTGRES_USER/POSTGRES_PASSWORD` 的超管凭据；主支已拆为 superuser/business/relay 三个锚点 |

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
| 分叉 revision 数量 | 记载 **3** 个（`b3c4`/`b7c8`/`9e3b`） | **10** 个 | ADR 需扩为 10 项，或明确「只列效果不等价项」并给出其余 7 项的等价条件 |
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
