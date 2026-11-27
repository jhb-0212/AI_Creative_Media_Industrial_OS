# WP-03 方案 A：退役角色声明与降级恢复契约（含实现与实测）

- 日期：2026-11-27
- 裁决依据：方案 A **有条件通过**——`RETIRED_ROLES` 属于 `role_specs.py`，补齐五字段、五条硬约束、六项验收
- 范围：`packages/shared/role_specs.py`、`packages/shared/migration_identity.py`、`data/migrations/versions/f2b3c4d5e6a7_*.py`、`scripts/bootstrap_identity.py`、`scripts/identity_registry.py`
- 边界：未提交/未推送/未合并；实测在隔离库 `afs_d02_a`，未触碰 `ai_film_industrial`

## 1. 契约审查结论（先审契约，再改代码）

改造前的状态：`qc_worker` 的"退役"只存在于两处——迁移 `f2b3c4d5e6a7` 的 `upgrade`（`DROP ROLE`）和 `downgrade`（手写 `CREATE ROLE ... PASSWORD`）。这会带来三个问题：

1. **角色只活在提交信息里**：除 revision id 外没有任何地方记录"退役前它是什么属性"；
2. **降级不可判定**：`downgrade` 里的属性是"当时那个人记得的属性"，不是历史事实；口令来源散落在迁移代码里；
3. **Bootstrap 与迁移争夺身份所有权**：Bootstrap 声明集不含该角色，迁移却能重建它，等于两套身份创建路径。

契约因此固定为：**退役也是一条声明**，与"当前目标态"同源（`role_specs.py`），但分属两段；只有降级才能把它恢复，且必须显式授权。

## 2. 五字段落点

| 字段 | 声明值（`qc_worker`） | 代码位置 |
| --- | --- | --- |
| `retired_at_revision` | `f2b3c4d5e6a7` | `RETIRED_ROLES[0]`，`retired_role_problems()` 校验 12 位十六进制、唯一 |
| `retirement_reason` | 空壳登录身份、零依赖零成员、Worker 实际用治理域角色 | 同上；名称由 `maintenance_roles()["worker"]` 派生，不写字面量 |
| `historical_attributes` | `login=True`、四种提权位全 `False`、`inherit=True`、`member_of=()`、`database_domains=()`、`password_setting=<派生>` | 同上；`RetiredRole.login/member_of/database_domains` 为派生属性 |
| `downgrade_restore_policy` | `restore_on_downgrade` | 合法值仅 `restore_on_downgrade` / `never_restore`（`RETIRED_RESTORE_POLICIES`） |
| `credential_source` | `settings.<派生字段名>` | 只允许 `settings.` 前缀；`retired_role_problems()` 拒绝内联口令值 |

登记辅助：`retired_role_names()`、`retired_roles_by_name()`、`retired_role_problems()`、`retired_registry_payload()`。后者进 Registry 生成物的 `retired` 段（`2026-11-27-identity-registry.generated.json` 已重生成，`roles=20`、`retired=1`）。

## 3. 五条硬约束 → 实现与证据

| 约束 | 实现 | 证据 |
| --- | --- | --- |
| ① 仍在 `role_specs.py`，不引第二份身份事实源 | `RetiredRole` / `RETIRED_ROLES` 与 `RoleSpec` 同模块；Registry 只做导出 | `identity_registry.py --dump` 的 `source` 仍指向 `packages/shared/role_specs.py` |
| ② 默认 Bootstrap 只建当前有效角色 | `role_specs()` 不含退役身份；`bootstrap_identity.py --roles` 先查"退役名 ∩ 目标态"并 `[BLOCKED]`，`spec_problems()` 再校验一次 | `test_retired_role_is_not_in_current_target_state`；`test_declarations_are_self_consistent` |
| ③ 降级按历史定义恢复，不按当前默认推导 | `restore_retired_role()` 只读 `historical_attributes`；成员关系按历史 `member_of` 收敛（历史为零成员 → 显式 `REVOKE operator`） | 真实库读数：`rolcanlogin=true`、`rolsuper/createdb/createrole/bypassrls=false`、`inherit=true`、`group_memberships=0` |
| ④ 口令不进生成物/迁移/报告，缺失 fail-closed | 迁移 `downgrade` 只剩一行 `restore_retired_role(...)`，无 `PASSWORD`、无 `get_settings()`；凭据缺失/空值抛 `RuntimeError` | `test_retirement_migration_delegates_and_has_no_inline_password`、`test_restore_fails_closed_when_credential_missing/empty` |
| ⑤ 恢复必须显式确认 + 明确告警 | 环境变量 `AUTHORIZE_RETIRED_ROLE_RESTORE` 必须**等于该 revision**（`=1` 不接受）；恢复语句与 `RAISE WARNING` 在同一 `DO` 块 | `test_restore_requires_explicit_authorization`、`test_restore_rejects_wrong_authorization_value`、`test_restore_applies_historical_attributes` |

## 4. 六项验收（实测读数）

在 `afs_d02_a`（空库直建 head）上跑：

| # | 验收项 | 结果 |
| --- | --- | --- |
| 1 | 当前 head 执行 Bootstrap 不创建退役身份 | 静态：`role_specs()` 无该名；运行期：`--roles` 前置检查会 `[BLOCKED]`。head 库 `pg_roles` 中不存在（`identity_registry --check` `clean=true`） |
| 2 | 降级跨越退役 revision，恢复状态与历史定义一致 | `alembic downgrade b6c7d8e9f0a1` exit 0；读数见 §3 ③ |
| 3 | 再次升级，角色重新退役 | `alembic upgrade head` exit 0；`pg_roles` 中已无该角色，`alembic_version=f2b3c4d5e6a7` |
| 4 | 恢复 → 退役 → 再恢复的循环 | 已跑一轮完整循环（降级恢复、升级退役）；第二轮"未授权降级"被拒（见 #6）。完整多轮循环留给带守卫的集成测试 |
| 5 | 未声明角色不能经 Bootstrap/迁移被隐式创建 | `restore_retired_role("some_random_role")` 抛"不在 `RETIRED_ROLES`"；迁移只恢复声明中的身份 |
| 6 | 凭据缺失/属性不完整/成员关系不符均失败关闭 | 未授权降级：exit 1、`RuntimeError`（信息含变量名与期望值）、`alembic_version` 仍 head、角色仍不存在；口令缺失/空值：单测覆盖，抛错且不执行任何 DDL |

> 说明：#4 的"多轮循环"目前只做了单轮真实库 + 单测级；要证明"反复恢复/退役不残留成员关系或默认权限"，需要在下一批加一个集成测试（用独立库反复 `dowgrade/upgrade` 两轮）。

## 5. 仍未闭合的部分

1. **受控降级入口的形态**：本轮落在环境变量 + revision 值校验（`=1` 无效），优点是不依赖日志、可进 CI；是否再加一道命令行开关（例如 `scripts/downgrade_controlled.py --restore-retired <revision>`）待裁决。
2. **CI 门禁**：目前"未授权即失败"由迁移自身保证；建议在 CI 里加一条"对 head 库跑 `downgrade b6c7` 必须 exit≠0"的断言，防止有人把确认逻辑改成默认放行。
3. **口令轮换**：恢复用的是配置里的当前口令，与"历史口令"不同。这是有意的（历史口令不应长期留存），但需要在审计里写明"恢复的是身份与属性，不是历史口令值"。
4. **`svc_*` 8 个 NOLOGIN 占位**仍未登记"未接入运行"，不在本工作包内。

## 6. 结论

方案 A 的契约与实现已完成，五项硬约束都有代码落点与测试/实测证据；**六项验收中 1/2/3/5/6 已实测通过，#4 的"多轮循环"待补集成测试**。因此本工作包判定为"按当前授权范围完成，仍需一项集成测试收口"。
