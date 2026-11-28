# 生产身份面核验（2026-11-28）

**结论：生产集群不需要任何角色属性收敛动作。** 声明与生产实况一致，唯一差异是 D4-9 退役动作
在 `quarantine` 下新增的归档对象（预期内、已授权、只读核验确认无其他漂移）。

## 1. 为什么先核验再动手

「生产 8 个 `svc_*` 仍是 LOGIN/INHERIT，需要受控 `bootstrap --roles`」这个判断来自
`data/migrations/identity_surface_baseline.json` 的**重定前手工快照**，不是实测。
在真库上先跑只读核验，才知道要不要改；否则会用一条「对齐声明」的动作，去修一个不存在
的差异，并把真实风险（生产独有的归档对象）掩盖成「已收敛」。

判定口径：**声明是目标态，bootstrap 负责把集群拉过去**（`role_specs.py` 注释同一口径）。
若实测已等于声明的目标态，正确动作是「不动」，而不是「再跑一遍让它变化」。

## 2. 只读核验（生产 `ai-film-postgres`，15432，库 `ai_film_industrial`）

命令与结果均只读（`pg_roles` / `pg_auth_members` / `pg_stat_activity` 与两个核对脚本）：

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 角色属性核对（`scripts/verify_role_attributes.py`） | `declared 20 / checked 20 / problems [] / clean true` | `runs/2026-11-28/verify-prod.txt` |
| `svc_*` 可登录性与继承位 | 8 个全部 `canlogin=false`、`inherit=true`（=声明默认） | `runs/2026-11-28/prod-survey.txt` |
| `rolsuper/createdb/createrole/bypassrls` | 全部 false（除 `postgres`） | 同上 |
| 成员关系 | 仅 `qc_maintenance_login → qc_maintenance_operator` 与三条内置 `pg_monitor` 关系；**无任何 `svc_*` 成员** | `runs/2026-11-28/prod-survey2.txt` |
| 在线连接使用的角色 | `asset_service_role` / `budget_quota_role` / `outbox_relay_role` / `production_planning_role` / `project_planning_role` / `qc_governance_role` / `postgres`（无 `svc_*`） | 同上 |
| 完整身份面 `dump_identity_surface.py --check-baseline` | `[DRIFT] 22 条`，逐条全部落在 `quarantine` | `runs/2026-11-28/surface-prod-check.txt` |

## 3. 那 22 条差异是什么

全部为「只存在于生产、不存在于干净安装基线」的对象：

- `schema quarantine` 及其 owner / `USAGE` / `CREATE` 授权；
- `quarantine.asset_versions_20261128`、`quarantine.qc_reports_20261128` 两表的 owner 与
  表级 ACL（含 `qc_governance_role` 的 `SELECT`）。

它们正是 2026-11-28 R1 空壳资产退役在生产留下的归档面（归档 + 一次性可逆 `GRANT` 已在
变更时执行并留证）。基线语义是「干净安装的产物」，不含生产独有的归档对象，因此这 22 条
**不构成回归**，也不应被写进基线——否则下次干净安装比对会反向报「缺对象」。

## 4. 由此确认的两件事

1. **无需收敛**：生产 `svc_*` 已是 `NOLOGIN`，且无任何成员依赖其继承位；`ALTER ROLE`
   在此既无对象可改，也没有可观测收益——不做，是这一步的最优解。
2. **`DROP ROLE` 仍无对象**：审计已证 `qc_worker` / `afs_*` 在生产不存在，本轮不引入。

## 5. 若将来真要收敛

触发条件：`role_specs.py` 的声明发生变化（例如 R16 启用运行时角色，`login=True` 并补口令
字段），或生产出现「声明与实况不符」的实测差异。届时按顺序：备份 → `bootstrap --roles`
（幂等，`ALTER ROLE ... WITH` 收敛属性）→ 重跑本文件的四项核对 → 再重采基线。
不得用「重采基线」把实况差异抹平。
