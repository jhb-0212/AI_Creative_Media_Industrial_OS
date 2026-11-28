# D4-9 strip 逐 revision 改造清单（2026-11-28）

> 依据：Owner 2026-11-28 裁决「批准把 R8 的 strip 范式推广为统一身份迁移规范」，以及对
> `8f3b2c1d4e5a`、`a0b1c2d3e4f5`、`a5b6c7d8e9f0`、`e0f1a2b3c4d5`、`e6a7b8c9d0e1` 五个 revision
> 「必须分别核验、不批准机械替换后直接交付」的要求。
> 工作树：`D:\WP04-evidence\strip-impl`（= `311530c` + D4-strip，未提交）。

## 0. 五项核验口径（每行都要有证据，不靠源码替换自证）

| # | 核验项 | 判定方式 |
|---|---|---|
| ① | 是否确属基础角色，而非迁移专属临时身份 | 角色必须出现在 `packages/shared/role_specs.py` 的声明集（`role_specs()` / `retired_role_names()`），否则不得 strip |
| ② | 隐藏依赖（对象所有权 / 默认权限 / 成员关系） | 真实往返 + 离线展开逐条看 `OWNER TO`、`ALTER DEFAULT PRIVILEGES`、`GRANT/REVOKE ... TO <role>`；角色缺失时是否会报错（fresh install 路径） |
| ③ | 新建数据库路径是否 Bootstrap 先于 Alembic | 干净集群（15445，仅 `postgres` 角色）先跑 `alembic upgrade head` 必须 fail-closed；再 `bootstrap_identity.py --roles` 后重跑必须 exit 0 |
| ④ | 已有数据库升级路径是否不受影响 | 15444 存量库（Bootstrap 已建角色）从空库 upgrade 到 head exit 0 |
| ⑤ | downgrade/upgrade 往返是否不再产生身份面差异 | 25 条 P→U→D 真实往返 `diff_groups=[]`（含 e6a7、f2b3） |

## 1. 五个 revision 的改造明细

| revision | 原身份动作 | 角色（①） | 改造后 | 隐藏依赖处理（②） |
|---|---|---|---|---|
| `8f3b2c1d4e5a_split_domain_roles` | `CREATE ROLE` + `ALTER ROLE` 强制重置属性 + 口令 | `project_planning_role`、`asset_service_role`（`role_specs()` 内，基础角色） | `_assert_domain_role()` → `assert_role_present()`；删除 `_domain_password()` 死代码与 `get_settings`/`reject_development_default_password` 引用；模块 docstring 改为「角色归身份治理」 | 角色仍被后续 `OWNER TO` / 授权引用，故保留断言（不是删除引用）；downgrade 保持原授权回收、不删角色 |
| `a0b1c2d3e4f5_domain_split_r16` | `CREATE ROLE`（absent 才建）+ 口令 | `production_planning` / `budget_quota` / `qc_governance` 域角色（基础角色） | `_assert_domain_role()`；upgrade 去掉 `get_settings`/`role_passwords` | 三域 schema 授权与 `OWNER TO` 依赖角色存在 → 断言前置；downgrade 只回收本迁移授权 |
| `a5b6c7d8e9f0_qc_scheduler_identity` | `CREATE ROLE` + `ALTER ROLE` + 口令（每次 upgrade 都重写） | `scheduler_role()`（基础角色） | `_assert_scheduler_role()`；upgrade 首行断言 | `scheduler_statements()`（schema USAGE / 表授权）与 discovery guard 依赖角色 → 断言前置；downgrade 明确注释「角色不删」并只回滚结构与授权 |
| `e0f1a2b3c4d5_r16_domain_schemas` | `CREATE ROLE IF ABSENT`（3 个域角色）+ 口令 | 同上三域角色 | `_assert_domain_role()` ×3；upgrade 改为 `CREATE SCHEMA IF NOT EXISTS` + 断言 | schema 创建与 `OWNER TO` 依赖角色存在；downgrade 只 `DROP SCHEMA`（不删角色）→ 整改后由 asymmetric 变 symmetric |
| `e6a7b8c9d0e1_maintenance_identities` | `CREATE ROLE` ×4（owner/operator/login/**worker**）、`ALTER ROLE ... NOLOGIN`、口令、`_NO_ESCALATION` 常量 | `qc_maintenance_owner/operator/login`（基础角色）+ `worker`（**已退役 `qc_worker`**，`role_specs.RETIRED_ROLES`） | `_assert_role()` ×3（owner/operator/login），**不再创建 worker**；删除 `_NO_ESCALATION` 与 `get_settings` | **重点**：`maintenance_membership_statements()` 里 `REVOKE <operator> FROM <worker>` 在 fresh install 会因角色不存在而报错（不是 warning）→ 改为 SQL 侧存在性守卫 `DO $$ ... IF EXISTS (pg_roles) ... END $$`（存量照旧收回，新建退化为 no-op，且仍可被 inventory 离线展开）。`OWNER TO owner` 依赖 owner 存在 → 断言前置 |

### e6a7 的额外回归（Owner 点名项）

- 旧实测：`qc_worker` 在 D 后残留为 LOGIN 角色（restore_required）。
- strip 后：upgrade 不创建 worker，成员语句对缺失 worker 退化为 no-op；真实往返 P→U→D 逐项比对
  角色集合 / 属性 / LOGIN / 成员关系 / 对象所有权 / 授权与默认权限 / 集群级共享依赖，`diff_groups=[]`。
- 归类变更按 Owner 要求保留历史（见 `2026-11-28-wp04-classification-change.json`）。

## 2. f2b3 的 A 方案（同一裁决，独立条目）

| 项 | 内容 |
|---|---|
| migration 现状 | `upgrade()`：读 `retired_roles_by_name()`（取不到即 `RuntimeError` fail-closed）+ 一条只读 `RAISE NOTICE`；`downgrade()`：`return None` |
| 退役执行载体 | `scripts/retire_legacy_roles.py --retired`：目标来自 `retired_role_names()`；前置检查 `pg_shdepend`（逐库）/ 成员关系 / 对象所有权 / 默认权限与授权；有引用即 `BLOCKED`，不代做清理 |
| 两状态区分 | `RETIRED`（已登记、普通 Bootstrap 不创建）vs `RETIREMENT_EXECUTED`（真实集群已执行并通过验证）；报告字段 `lifecycle_state` |
| 未做 | 未在任何真实集群执行退役；未强制 `DROP ROLE` |

## 3. 路径证据（③④⑤）

| 路径 | 实测 | 结果 |
|---|---|---|
| 新建库，先 Alembic 后 Bootstrap（15445 `fresh_bootstrap_check`） | `alembic upgrade head` | exit 1，尾错误 `role "app_role" is missing: run scripts/bootstrap_identity.py --roles first` → fail-closed 生效 |
| 新建库，Bootstrap 先行（同集群） | `bootstrap_identity.py --roles` → 40 条语句 exit 0；再 `alembic upgrade head` | exit 0，`alembic_version=f2b3c4d5e6a7`；`qc_worker` 不存在，`qc_maintenance_owner/operator/login` 存在 |
| 存量库（15444，Bootstrap 已建角色）从空库到 head | `alembic upgrade head` | exit 0，head=`f2b3c4d5e6a7` |
| 25 条往返（15444） | `wp04_triage_strip_u.py` + `make_strip_sweep.py` | 25/25 `no_restore_proven`，`diff_groups=[]`，`SWEEP_EXIT=0` |
| 离线展开（6 revision） | `wp04_offline_expand.py` | upgrade/downgrade 全部 `[OK]`（f2b3 downgrade 已无 `RuntimeError`） |
| 代码级身份 DDL 扫描 | `wp04_scan_identity_ddl.py`（跳过 docstring） | 9 个相关 revision 全 `CLEAN`（零 `CREATE/ALTER/DROP ROLE`、零 `PASSWORD '`） |

## 4. 不在本清单范围的已知残留（不隐瞒）

- `9e3b4c5d6e7f`、`7a8b9c0d1e2f`、`c5d6e7f8a9b0` 中已不再被调用的 `_app_password/_relay_password/_ops_password`
  口令读取辅助函数未删除（同属「凭据不进 migration」口径，但不属本次 5 revision 范围）。
- `role_specs()` 中其余角色属性的声明、`RETIRED_ROLES` 的 `cluster_wide_zero_authorization_verified=False`
  仍为事实，未修改。
- 生产集群未触碰；`qc_worker` 在真实集群的退役策略仍待 Owner 拍板（依赖清查已完成方法论，未执行）。

## 5. 双签（ADR-R46 附录 C.1，2026-11-28 裁决）

规则：迁移作者签「实现与实测一致」，数据库负责人（DBA）签「目标环境可执行/可回滚」；
两签缺一，本清单不视为通过；两签须落在同一 checklist 版本（指纹见 §6）。

| 角色 | 签什么 | 签署 | 日期 | 状态 |
|---|---|---|---|---|
| 迁移作者（revision 实现者） | 5 revision 的 strip / 守卫 / 断言与 §2 逐条一致；25/25 往返与离线展开可复现 | 实施人（变更包编制侧） | 2026-11-28 | ✅ 已签 |
| 数据库负责人（最终签认，有否决权） | 目标环境角色齐备、备份有效、回滚路径可走 | Project Owner（兼 DBA） | 2026-11-28 | ✅ 已签（范围：**演练库 15444**） |

> 签认范围：DBA 签只覆盖 `wp04-verify-15444 / ai_film_api_tests` 的演练（备份 + 恢复抽验 + 降级/升级 + 7 步验证，
> 见 `docs/audits/2026-11-28-chg-wp04-d4-9-rehearsal-record.md`）。**不覆盖生产主库**——生产实例未书面确认前，
> 本清单与 A.6.6 授权对生产均不生效；生产窗口须重跑同序动作并单独回填记录，不得复用本签。

## 6. 版本指纹

本清单不内嵌自身哈希（承继 ADR-R46-VERSION 的 R2 口径）；指纹登记于
`runs/2026-11-28/checklist-fingerprint.txt`（bytes + sha256_disk + 采集时间）。
两签若发生在指纹登记之后，须重新采集并追加，不得覆盖首条。

