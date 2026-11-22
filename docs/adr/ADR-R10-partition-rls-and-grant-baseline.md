# ADR-R10：把"分区也受 RLS 保护"变成结构层不变量（权限基线反转）

- 状态：已实施（迁移 `b7c8d9e0f1a2`，2026-11-14）
- 相关：ADR-R06-2（Outbox 分区与保留期）、ADR-R07/R08/R09（契约与执行闭环）
- 触发：审计发现「app_role 无租户上下文直查 outbox 分区读到跨租户行」

## 1. 问题不是漏授权，是权限模型方向反了

实测事实（审计报告 §P0-1）：

| 事实 | 证据 |
|---|---|
| 父表 `outbox_events` RLS 已开，**全部分区 `relrowsecurity=false`** | `pg_class` |
| app_role 对**每个分区**都有 `INSERT,SELECT,UPDATE` | `information_schema.role_table_grants` |
| 无租户上下文直查 `outbox_events_y202611` → **276 行** | psql as app_role |
| ADR-R06-2 声称"分区子表不单独授权""父表策略应用到所有分区" | `docs/adr/ADR-R06-2-...md:41-44` |

根因两条：

1. **默认宽授权**：`9e3b4c5d6e7f` 对 schema 执行
   `ALTER DEFAULT PRIVILEGES ... GRANT SELECT,INSERT,UPDATE,DELETE`，此后任何
   新表/新分区**天生**带权限；后续 `d4e5f6a7b8c9` 只在 outbox 摘掉 DELETE。
2. **保护是一次性动作**：RLS 只在 `f6a7b8c9d1e2` 对父表 `ENABLE` 一次；分区由
   维护工具/后续迁移动态创建，创建时没人再 `ENABLE`。PG 的父表策略只约束
   "通过父表访问"，直接点名分区时按分区自身策略——未开启 = 不校验。

结论：这不是"补一条 GRANT"能解决的，必须改模型：**默认零授权 + 创建即保护**。

## 2. 决策

1. **权限基线反转**：三个应用 schema（`project_planning` / `outbox` /
   `asset_service`）取消"新表自动授权"。今后新表/新序列必须显式
   `GRANT`。忘记授权的后果是权限错误（fail-closed），而不是静默获得权限。
2. **分区保护与创建绑定**：新增 `packages/shared/partition_guard.py`，产出幂等
   的四条语句（ENABLE RLS / DROP POLICY IF EXISTS / CREATE POLICY /
   REVOKE ALL FROM app_role）。迁移与 `workers/outbox_maintenance` 共用同一实现。
3. **append-only 收紧**：`qc_reports` 撤销 `UPDATE, DELETE`；
   `asset_versions` 撤销 `DELETE`（状态迁移所需的 `UPDATE` 保留）。
4. **不变量进 CI**：新增
   `tests/integration/test_tenant_isolation_invariants.py`，断言的是规律而不是
   某张表——凡分区必有 RLS、必有策略、app_role 必无直连权限；直连必被拒绝。

## 3. 落地物

| 文件 | 作用 |
|---|---|
| `packages/shared/partition_guard.py` | 分区保护的唯一 SQL 实现（标识符白名单 + 加引号） |
| `data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py` | 存量分区补保护、默认权限反向、append-only 收紧、`alembic_version` 只读授权 |
| `workers/outbox_maintenance/__main__.py` | `ensure_partition` 在创建分区的同一事务里执行保护语句 |
| `tests/integration/test_tenant_isolation_invariants.py` | 上述不变量的断言（CI 必跑） |
| `apps/api/main.py` | `/readyz` 回报 `schema_revision`，用于识别"连错库实例" |

## 4. 影响面（必须知道的代价）

- **新表不再自动可用**：任何新增表/序列的迁移必须自带 `GRANT`，否则应用 403/500。
  这是刻意的：把"忘记授权"从"静默放行"变成"立刻报错"。
- **分区只能通过父表访问**：任何代码/脚本若直接点名 `outbox.outbox_events_y*`
  都会 `permission denied`。归档与安全摘除由迁移角色执行（不受影响）。
- **`qc_reports` 不再可 UPDATE**：现有代码只做 `add` / `get_by_id` / `count`，
  无写入路径；若将来需要更正证据，必须走"新增报告"而不是原地改。

## 5. 验证（本地实测口径）

```
alembic upgrade head                        # b7c8d9e0f1a2
python -m pytest tests -q                   # 含 4 条新不变量断言
python scripts/contract_check.py            # 契约闸门不变
alembic check                               # 无漂移
```

复验 SQL（应全部满足）：

```sql
-- 1) 每个分区 RLS 已开
SELECT c.relname, c.relrowsecurity FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname='outbox' AND c.relname LIKE 'outbox_events%' AND c.relkind='r';

-- 2) app_role 不再有分区直连权（除父表）
SELECT table_name, privilege_type FROM information_schema.role_table_grants
WHERE grantee='app_role' AND table_schema='outbox' AND table_name LIKE 'outbox_events_y%';

-- 3) 直连被拒绝
SELECT count(*) FROM outbox.outbox_events_y202611;  -- 以 app_role：permission denied
```

## 6. 未做项（明确登记，不假装完成）

- ~~**内容列不可变**（`sha256 / storage_ref` 等仍会随状态迁移被 `UPDATE` 回写）：
  需要列级授权或 `BEFORE UPDATE` 触发器，属 S3 批次；本 ADR 只关闭"整行删改"。~~
  **已由 ADR-R11（S3）关闭**：仓储白名单三列 + `asset_versions`/`qc_reports`
  触发器兜底（迁移 `d1e2f3a4b5c6`）。
- **`outbox.maintenance_runs` 无 RLS**：仅运维元数据、无租户字段；保留现状，
  但它同样是"新表默认无保护"的例证，待未来统一到"含租户列即必须有 RLS"的检查。
- **`project_planning` 现有表的 DELETE 授权未收紧**：该域尚无 append-only 承诺的
  对象（剧本版本走 SUPERSEDED 而非删除），留待该域确认口径后单独处理。
