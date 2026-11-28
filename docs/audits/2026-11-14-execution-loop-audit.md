# 执行闭环审计：已建成基线的真实能力评估

- 日期：2026-11-14
- 范围：`AI_Creative_Media_Industrial_OS` 主线（PR #1 R06 保留期 / #2 资产域闭环 / #3 R08c 可达性闸门 / #4 R09 ACTIVE schema 覆盖）
- 基线：本地 main `e86bc10`（工作区干净，五道闸门全绿）
- 方式：只读审计。**未修改任何代码、契约、迁移**；本文件为唯一新增产物。
- 证据：`file:line` + 数据库实测 + 运行时复现脚本（附录 A 可复跑）

---

## 0. 一句话结论

**"声明自洽"已经做到了；"声明真实生效"还没有。**

五道闸门全绿、248 个测试全过、契约 37 个 ACTIVE 动作全部有落点，但同一套代码下仍能实测出：

- 应用角色**不带租户上下文直查 Outbox 分区，读到 276 行跨租户审计数据**（RLS 在分区上等于不存在）；
- 把 A 资产的 QC 报告 id 提交到 B 资产端点，**接口 201 返回 A 的报告、B 静默停在 GENERATED**；
- 状态推进 4 次、事件只写 3 条，**HUMAN_REVIEW 这个真实发生过的状态没有任何事件**；
- 把 76 个契约对象删掉 46 个，`contract_check` 依然打印 `[PASS] Schema 对象完整：30 个定义`。

原因不是"漏写了几行"，而是三个结构性缺陷（见 §5）：**默认宽授权 + 一次性保护**、**闸门只验存在不验行为**、**不变量写在 ADR 里而不是 DB/测试里**。

分级计数：**P0 2 项 / P1 4 项 / P2 4 项 / P3 6 项**。

> 2026-11-14 晚更新：**P0-1（分区 RLS）与 P0-2（双实例识别）已关闭**，见
> ADR-R10 + 迁移 `b7c8d9e0f1a2` + `tests/integration/test_tenant_isolation_invariants.py`。
> 逐项状态见 §10「修复进度」。

---

## 1. 七环 × 模块矩阵（现状判定）

图例：✅ 成立 ／ ⚠️ 名义成立、有缺口 ／ ❌ 未成立（能力缺失）

| 模块 | ①契约定义 | ②领域实现 | ③权限校验 | ④数据持久化 | ⑤事件驱动 | ⑥自动化测试 | ⑦质量验收 |
|---|---|---|---|---|---|---|---|
| 项目 Project | ⚠️ 状态机在契约，但无动作入口 | ⚠️ 仅 CREATE/READ | ✅ require_role | ✅ RLS | ⚠️ 仅 CREATED | ✅ | ⚠️ 状态不可达未被闸门发现 |
| 集数 Episode | ⚠️ 契约标注 `PENDING_PHASE1_REVIEW` | ⚠️ 仅 CREATE/READ | ✅ | ✅ | ⚠️ | ✅ | ⚠️ 同上 |
| 剧本 ScriptRevision | ✅ | ✅ | ✅ 职责分离真实比对 | ✅ | ✅ 4 类事件 | ⚠️ 缺跨内容幂等负测 | ⚠️ 幂等弱于资产域 |
| 资产 AssetVersion | ✅ | ✅ 幂等键已绑定资产（S3） | ✅ | ✅ 内容列结构级不可变（S3） | ✅ 逐跳两事件 + 契约（S3） | ✅ 跨资产/不可变断言（S3） | ⚠️ schema 闸门仍假绿（P1-3） |
| Outbox 分区/relay/归档 | ✅ | ✅ | ✅ 分区已保护（S1） | ✅ 只能经父表访问 | ⚠️ 毒事件无上限 | ✅ 不变量断言（S1） | ⚠️ |
| 契约闸门体系 | ✅ | ✅ | — | — | — | ⚠️ 自测只验函数返回 | ❌ schema 假绿（实测） |
| 运维 Runbook | ✅ 三份 | — | — | — | — | — | ⚠️ 未提示双实例 |

---

## 2. P0：会导致"隔离承诺"当场失效

### P0-1 Outbox 分区的 RLS 是装饰：app_role 无租户上下文可直读 276 行

**实测（Docker 库，head `c1d2e3f4a5b6`）**

```
-- 以 app_role、不设置 app.current_tenant_id
SELECT count(*) FROM outbox.outbox_events_y202611;      --> 276
-- 同一会话、同一状态下查资产域
SELECT count(*) FROM asset_service.asset_versions;      --> 0        （资产域 fail-closed 正确）
```

`pg_class.relrowsecurity`：父表 `outbox_events = t`，`outbox_events_default / y202611 / y202612 / y202701 / y202702` **全部 = f**。

授权侧：`information_schema.role_table_grants` 显示 `app_role` 对**每一个分区**都有 `INSERT,SELECT,UPDATE`，并非 ADR 宣称的"只授权父表"。

**为什么闸门没动机**

- RLS 只在迁移里对父表 `ENABLE` 一次：`data/migrations/versions/f6a7b8c9d1e2_partition_outbox_events.py:112-121`；
- 分区由维护/迁移动态创建，创建时**不会**自动 `ENABLE RLS`，而 PG 对"通过父表访问"会应用父表策略、对"直接点名分区"应用分区自身策略（未开启 = 不校验）；
- 分区授权来自默认权限：`9e3b4c5d6e7f_create_app_role_and_grants.py:84-93` 对 schema 执行 `ALTER DEFAULT PRIVILEGES ... GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES`，此后任何新表/分区**自动**获得权限；`f6a7b8c9d1e2:105-111` 只对父表补了 `GRANT` + `REVOKE DELETE`，管不住子表；
- ADR 把未验证的假设写成了结论：`docs/adr/ADR-R06-2-outbox-retention-partitioning.md:41-44`「分区子表不单独授权」「父表策略应用到所有分区」——两条都与 DB 现实相反；
- 现有 RLS 负向测试只覆盖 `project_planning` 三表：`tests/integration/test_rls_isolation.py`（无 outbox、无 asset_service）。

**影响**

"研发即使写 `SELECT *` 也会被 RLS 拦"这条核心承诺，在审计事件（含 payload、租户、业务 id）这张表上不成立。当前应用代码只走父表，所以不是"今天就能被外部利用"，而是**下一行代码（维护脚本、排查 SQL、BI 导出、注入）就会把隔离击穿**——防线建立在"没人会直连分区"的约定上。

**根治方向（不是给分区补一条 GRANT）**

1. 权限模型反转：默认零授权（去掉 schema 级 `ALTER DEFAULT PRIVILEGES`），改由迁移/维护按表白名单授予；
2. 分区生命周期统一收口：建分区后立即 `ENABLE ROW LEVEL SECURITY` + `CREATE POLICY` + `REVOKE ALL FROM app_role`（或只保留 SELECT），封装成一个 `protect_partition()`，迁移与 `workers/outbox_maintenance` 共用；
3. 加一条**断言型**测试：`对 outbox 每个分区断言 relrowsecurity=true 且 app_role 无直连权限`，并纳入 CI（可复用 `check_schema_objects` 同一闸门）；
4. 修正 ADR-R06-2：把"事实"改成"必须成立的断言 + 由哪条测试保证"。

---

### P0-2 15432 端口上并存两个 Postgres，`127.0.0.1` 与 `localhost` 是两个世界

**实测**

```
Get-NetTCPConnection -LocalPort 15432 -State Listen
127.0.0.1:15432  PID 5688   postgres.exe      （本地集群 D:\pgcluster15432）
::1:15432        PID 34764  wslrelay          （Docker 转发）
::15432          PID 23216  com.docker.backend（Docker 转发）
```

| 连接方式 | 实际落点 | `alembic_version` | `asset_service` schema |
|---|---|---|---|
| `-h localhost`（应用/测试/`.env`） | Docker `ai-film-postgres` | `c1d2e3f4a5b6`（head） | 存在 |
| `-h 127.0.0.1`（最常用的排查写法） | 本地旧集群 | `b8c9d1e2f3a4` | **不存在（0 张表）** |

同一库名、同一端口、两套 schema。任何人（包括 runbook 里"连库看看"的习惯动作）用 `127.0.0.1` 排查，会得出"迁移没跑/资产域根本没建"的错误结论，然后去"修"一个并不存在的问题。本轮审计最初就撞上了这个假世界（先查到 b8c9 无表，改 `localhost` 才是 head）。

**根治方向**

- 环境层：停掉/改端口本地集群（这是"两个真相源"，不是配置问题）；
- 应用层：启动自检打印 `server_version + inet_server_addr + alembic current`，`/readyz` 暴露 revision，让"连错库"在 10 秒内可见；
- 文档层：`runbook-db-migration.md` 所有 psql 命令固定 `-h` + 端口，并加一节「同端口多实例识别」；
- 测试层：`test_database_dsn.py` 增加"revision == 代码 head"断言（现在只校验 DSN 形状）。

---

## 3. P1：闭环个别环节真实断裂

### P1-1 QC 报告幂等键跨资产复用：A 的证据挂到 B 的请求上，B 静默不推进

代码：`services/asset_service/service/asset_service.py:176-179`

```python
report_id = qc_report_id or uuid.uuid4().hex
existing = await self._qc_reports.get_by_id(report_id)
if existing is not None:
    return asset, existing  # ← 没有校验 existing.asset_version_id == asset_version_id
```

**运行时复现（内存 fake，与生产同一段用例代码）**

```
A_STATUS TECHNICAL_QC
B_STATUS GENERATED                       ← B 完全没动
B_RETURNED_REPORT_ASSET asset-A           ← 返回的是 A 的报告
EVENTS ['ASSET_VERSION_CREATED','ASSET_VERSION_CREATED','QC_REPORT_CREATED']
                                         ← 只有 A 一条 QC 事件
```

**影响**：客户端拿到 201 + 一份"合法"报告，以为 B 已完成技术质检；B 的 Gate 证据链实际缺失。跨租户时有 RLS 兜底（A 的报告对 B 租户不可见 → NOT_FOUND），但**同租户跨资产**没有任何拦截。

**根治方向**：`qc_report_id` 的作用域定义为 `(tenant_id, asset_version_id)`；命中但资产不一致 → `409 CONFLICT / IDEMPOTENCY_KEY_REUSED`，并补一条跨资产负向测试（现在只有同资产幂等：`tests/unit/test_asset_service.py:176-199`）。

---

### P1-2 两跳批准只落一条事件：HUMAN_REVIEW 是"发生过但不存在于事件流"的状态

代码：`services/asset_service/service/asset_service.py:287-315`（先 `transition_to(HUMAN_REVIEW)` + save，再 `transition_to(APPROVED)` + save，最后 `record()` 一条 `ASSET_VERSION_APPROVED`，用 `human_review_in_same_call=true` 作补偿字段）。

**DB 对账（Docker 库）**

```
asset_versions:  APPROVED = 11, entity_version=4 的有 11 条
outbox_events:   ASSET_VERSION_APPROVED = 11
                 （无任何 HUMAN_REVIEW 迁移事件）
```

`entity_version=4` = 创建→TECHNICAL_QC→HUMAN_REVIEW→APPROVED 四次推进；事件流只有 CREATED、QC_REPORT_CREATED、APPROVED。**下游（relay/webhook/审计回溯/指标）无法观测 HUMAN_REVIEW**，也无法从事件重建"谁在何时把资产推进到人工复核"。

这与 R08 ADR 的"每次状态迁移写事件"口径冲突，且与 31.5 状态机（HUMAN_REVIEW 是一等状态）不一致。

**根治方向**：让"状态迁移"成为事件的唯一产地——在 `Entity._advance_version()` 或 UoW 层统一 `record(transition)`，业务用例不再手写事件；`approve` 拆成两次真实迁移、两条事件（`ASSET_VERSION_REVIEWED` / `ASSET_VERSION_APPROVED`），删除补偿字段。补一条断言：`事件数 == entity_version - 1`（对每个聚合）。

---

### P1-3 闸门假绿：删掉 46/76 个契约对象，`contract_check` 仍然 PASS

代码：`scripts/contract_check.py:684-704` —— 只判断 `definitions` 非空、每项是 dict，然后**打印**数量，没有断言 76，也不校验 `required/properties`。

**实测（把 76 个定义裁到 30 个后调用同一函数）**

```
[PASS] Schema 对象完整：30 个定义
GATE_RESULT_WITH_30_DEFS True
```

这意味着"76 个 P0 对象已建模"是**打印出来的事实**，不是**被守住的口径**：删对象、并对象、塞 `{"type":"object"}` 都不会报红。

同时，`check_schema_objects` 只验"对象存在"，不验"字段级实现"。已找到一个字段级漂移：契约 `QCReport` 声明了 `deviation_intent_ref`（`contracts/schemas/object_schemas.schema.json` → QCReport.properties），运行时 `QCReport` 域对象与 `to_dict()` 完全没有该字段（`services/asset_service/domain/qc_report.py:29-92`，全仓 grep 0 命中）。

**根治方向**

1. 数量从"打印"变"断言"：`len(definitions) == 76` 且对象名清单与 `contracts/runtime/object_manifest.yaml`（新建，单一事实源）逐项比对；
2. 结构断言：每个定义必须有 `type`、`required` 非空、`properties` 覆盖 required；
3. 字段级断言：对"已实现资源类型"，比对契约 `properties` 与运行时序列化键（允许显式豁免清单，不允许静默缺失）；
4. 自测要覆盖"负向"：删一个对象/删一个 required → 必须 FAIL（现测试只覆盖 ACTIVE 缺口函数的返回，不覆盖 schema 完整性被破坏）。

---

### P1-4 "不可变"只是注释：内容列每次迁移都被回写，且 DB 给了 UPDATE/DELETE

- `packages/shared/versioning.py:78-93` 的 `_orm_update_values()` 取**除主键与 entity_version 外的所有列**；
- `services/asset_service/repository/sqlalchemy_asset_repo.py:86-93` 的 `save()` 把它直接塞进 `UPDATE ... SET`；
- 结果：每次状态迁移都会把 `sha256 / storage_ref / asset_type / source_asset_version_ids / rights_manifest_ref` 一并写回——**内容列出现在 UPDATE 语句里**，路径一旦写错或恶意，就能借"改状态"改内容；
- 授权：`c1d2e3f4a5b6_create_asset_service_tables.py:132-141` 给 `asset_service` 全表（含 `qc_reports`）`SELECT,INSERT,UPDATE,DELETE`，并设置同口径默认权限；与 outbox 的 `REVOKE DELETE`（`d4e5f6a7b8c9`）口径不一致；
- 全库 `CREATE TRIGGER` 数量 = 0，即"append-only / 不可变"没有任何结构层约束。

**根治方向**（二选一或叠加，但要选一种写进契约）

- 结构派：`save()` 只写 `status / entity_version / updated_at / audit_ref` 白名单列；`qc_reports`、`asset_versions` 撤 `DELETE`（`qc_reports` 连 `UPDATE` 也撤），加 `BEFORE UPDATE` 触发器禁止内容列变更；
- 权限派：把"内容不可变"落在 `REVOKE UPDATE (内容列)` 的列级授权上（PG 支持列级 GRANT）。

两者都要配一条负向测试：`UPDATE asset_versions SET sha256=...` 必须失败。

---

## 4. P2：能力/可靠性缺口

### P2-1 Project / Episode 状态机没有执行入口

- 契约给了 Project 7 条、Episode 3 条转移；域里有 `transition_to()`（`services/project_planning/domain/{project,episode}.py`）；
- 但 `contracts/action_registry/action_registry.yaml` 里 **Project/Episode 只有 CREATE/READ**，没有任何"状态变更"动作；全仓没有任何 service/route 调用 `project.transition_to()` / `episode.transition_to()`（grep `transition_to(` 仅命中 domain 定义与 script/asset 服务）；
- 闸门的"状态可达性"是在**转移表常量**上做图可达性（`scripts/contract_check.py:111-125, 802-820`），不校验"是否有动作能触发这条边"。于是"4 个状态机可达"给人的安心感，与"项目根本走不到下一个状态"同时成立。

对百集级生产的直接后果：平台目前**无法把项目推进到生产态**，项目/集数只是可创建可读的档案。

**方向**：要么把状态变更动作登记进 action_registry 并实现（含权限、事件、负测），要么在 `state_machine_gaps.yaml` 里把"无入口的转移"显式登记为债务（现在只登记了 AssetVersion 的 5 个不可达状态）。二选一，不能留在"绿着但不可执行"。

### P2-2 relay 对毒事件无上限、无死信：队头可以永久阻塞

`workers/relay/consumer.py:179-221`：claim 按 `occurred_at` 升序取 batch，handler 抛异常 → `failed += 1` → `continue`，**不记尝试次数、不隔离、不告警（只有指标）**。

一旦某类事件因下游契约变化持续失败，它们会每轮占据 batch 头部（默认 100），新事件被挤在后面；系统表现为"积压增长、无明确死因"，排查只能翻日志。

**方向**：`attempts` 列 + 指数退避 + 超过阈值移入 `outbox_dead_letters`（或 `status=dead`），配一个"最老未投递事件年龄"的告警指标。

### P2-3 只读 UoW 没覆盖资产 GET

`services/asset_service/api/asset_routes.py:148-156` 的 `GET /assets/{id}` 通过 `_get_service(uow=Depends(get_tenant_uow))` 拿写工作单元；而 `services/identity_access/deps.py:36` 早就备好了 `get_read_tenant_uow`，项目/剧本域都用了（`project_routes.py:51`、`script_routes.py:80`）。

即 `SET TRANSACTION READ ONLY` 的数据库层兜底（`packages/shared/unit_of_work.py:54-58`）**对资产读接口没生效**——"GET 即使误写也写不进去"这条防呆在资产域是空的。

### P2-4 响应体与契约存在双向漂移，且没有 response_model 兜底

- `packages/shared/entity.py:105-119` 的 `_envelope_dict()` 输出 `tenant_id`；契约 `AssetVersion.required` 是 10 个信封字段（不含 `tenant_id`），`openapi.draft.yaml` 全文 0 处 `tenant_id`；
- 资产路由全部返回 `-> dict[str, Any]` 且无 `response_model`（`asset_routes.py:83-156`），FastAPI 不做响应校验 → 多字段/少字段/类型变化都不会被闸门看见；
- 契约 → 运行时的字段级缺失见 P1-3（`deviation_intent_ref`）。

**方向**：给已实现动作补 `response_model`（由 `object_schemas.schema.json` 生成 Pydantic 模型），或加一个"契约字段 ⊇ 响应字段"的闸门；`tenant_id` 是否对外，先在正文口径上定，再改 `_envelope_dict`。

---

## 4.1 P3：一致性、可维护性与部署矛盾

**P3-1 剧本导入的幂等弱于资产域**
`services/project_planning/service/script_service.py:100-104`：同 `entity_id` 命中即返回旧版本，**不比对内容**。客户端"改了标题再导入"，拿到 200 + 旧内容，且不会产生新版本，也不会报冲突。资产域已做成"同 id 不同内容 409"（`test_duplicate_different_content_is_rejected`），两个域口径不一致。

**P3-2 `jti` 防重放只是声明**
`packages/identity/jwt_core.py:64-79` 生成 `jti`、`:138` 解析进 Principal，但全仓无 denylist/一次性校验：同一令牌在有效期内可无限重放。短期 TTL（15 分钟）是唯一缓解，不是"防重放"。要么实现（Redis 记录 jti + TTL），要么把文档口径改成"jti 用于审计关联"。

**P3-3 测试污染共享开发库**
`outbox_events` 里留下 `aggregate_type='UoWTest'` 9 条（本轮实测）。测试没有走独立库/事务回滚，用 RLS 隔离数据但**不隔离事件流**：这些行会进入 relay backlog 统计和保留期清理路径，也会让"每租户事件数"类断言在将来失真。

**P3-4 Runbook 有 Windows 不友好项，且没提示双实例**
`docs/runbooks/runbook-db-migration.md:94-108` 的备份段：`docker compose exec ... pg_dump > /backup/x.dump` 是 bash 重定向 + 容器外绝对路径混写，在 PowerShell 下 `/backup` 不成立；正文要求"备份文件位置与校验和"，但没给算校验和的命令；整份 runbook 未提示"同端口可能连着另一个本地集群"（P0-2 的坑）。

**P3-5 `outbox.maintenance_runs` 无 RLS**
`relrowsecurity=false`，`app_role` 有 `INSERT/SELECT/UPDATE`。它只有维护元数据（起止时间、条数），不含租户数据，风险低；但它证明 P0-1 的根因是**模式**而非个例：新表默认不带保护。

**P3-6 生产启动强制要求不存在的能力**
`packages/shared/config.py:108-117` 把 `s3_access_key_id / s3_secret_access_key / mq_url / model_gateway_api_key` 列入"生产必需密钥"，而全仓没有任何 S3/MQ/模型网关实现（`boto3`/`s3` 客户端 0 命中，只有 `s3_endpoint_url` 配置项）。按当前代码，`APP_ENV=production` 会因"缺少根本用不到的密钥"而拒绝启动。要么把这几项从强制清单摘出并登记为债务，要么先实现最小闭环。

---

## 5. 根因：三个结构性缺陷（比清单更重要）

**A. 权限模型是"默认开、逐处关"**
`schema` 级 `ALTER DEFAULT PRIVILEGES GRANT SELECT,INSERT,UPDATE,DELETE`（`9e3b4c5d6e7f:84-93`、`c1d2e3f4a5b6:138-141`）决定了**新对象天生超额**；之后靠人工在少数迁移里 `REVOKE`。只要有人新建表/分区没跟着 REVOKE，就回到"全权限"。P0-1、P1-4 是同一个根因的两次显形。

**B. 安全不变量是"一次性动作"，不是"每次新建都执行的钩子"**
RLS 在迁移里 `ENABLE` 一次，分区动态创建时没人再 `ENABLE`；反过来说，任何"动态产生对象"的能力（分区、未来按月归档表、租户分表）都会绕过同一类保护。缺的不是一条 SQL，而是"对象创建 = 保护就位"的唯一入口 + 闸门断言。

**C. 闸门只验"声明自洽"，不验"行为成立"**
- `check_schema_objects` 打印数量（P1-3）；
- `check_state_reachability` 只看转移表常量（P2-1）；
- `check_implemented_permission_wiring` 是源码文本级校验（其 docstring 已自述局限：只证明动作键被引用，不证明挂在正确路由上）；
- 没有任何闸门断言"每次状态迁移必有一条事件"、"分区 RLS 已开启"、"内容列不可 UPDATE"、"GET 走只读事务"。
于是"绿"衡量的是"我写下的声明彼此不矛盾"，而不是"系统在运行时真的这样"。这也解释了为什么 248 个测试全绿、P0/P1 同时存在：**测试覆盖的是代码路径，闸门覆盖的是文本一致性，没有一层覆盖数据库不变量与事件时序。**

---

## 6. 距离"百集级工业化平台"还差什么（只列事实，不粉饰）

1. **镜头/分镜域整体缺位**：`ShotDraft / ShotRevision / ShotBudgetRequest / BudgetLedger / GenerationTask / ProductionRoute / CanonRevision / RightsCase / RightsManifest` 9 个类型只有债务登记（R09），没有实现；`SHOT.DRAFT.CONFIRM / REJECT` 未登记。第 2~4 步卡在 V2.0.5 正文（本机没有该文档）。
2. **生产推进不可执行**：项目/集数无状态变更入口（P2-1）；没有排期、任务分派、Gate0—4 决策对象。
3. **媒体链路未接**：S3 只有配置与"未实现"抛错；生成任务、模型调用、成本账本（BudgetLedger）均未落地。
4. **可靠性未证明**：无并发/负载/长事务实测；relay 毒事件无隔离（P2-2）；`export_partition_local` 写后未回读复算摘要，归档可信度依赖调用方。
5. **可观测面偏薄**：指标有（relay backlog、限流降级、错误计数），但没有"最老未投递年龄""每次迁移事件缺失数""分区保护状态"这类**能直接暴露上述缺陷**的业务指标。
6. **运维有 twin-instance 陷阱**（P0-2），且 runbook 备份路径 `/backup/...` 在 PowerShell 下不成立（`runbook-db-migration.md:96-98` 用 bash 重定向与容器外路径混写），"校验和"只在正文要求、未给命令。

---

## 7. 建议的根治顺序（一次只开一个口子，每步都配闸门）

| 批次 | 内容 | 完成判据（可执行断言） |
|---|---|---|
| S1 止血·隔离 | 权限反转：默认零授权；`protect_partition()` 收口；分区 REVOKE + ENABLE RLS；修复 ADR 表述 | 对每个分区 `relrowsecurity=true` 且 `app_role` 无直连权；新增 DB 不变量测试进 CI |
| S2 止血·环境 | 停/迁本地 15432 集群；`/readyz` 暴露 revision；runbook 固定 host + 双实例识别节 | 任一 psql 写法落到同一 revision；readyz 可见 head |
| S3 正确性 | QC 幂等加资产校验（409）；两跳拆两事件；内容列白名单 UPDATE + 撤 DELETE/触发器 | 跨资产用例 409；`事件数 == entity_version-1` 断言；越权 UPDATE sha256 失败 |
| S4 闸门升级 | schema 计数/required/字段级断言；转移表 ↔ 动作绑定检查；分区保护检查 | 删任一对象/required 必红；无入口转移必红 |
| S5 relay 可靠性 | attempts + 退避 + 死信 + 年龄指标 | 毒事件 3 次后进死信；积压不因单事件停滞 |
| S6 一致性收尾 | 资产 GET 改只读 UoW；`response_model` 或响应字段闸门；script 同 id 内容比对；测试数据隔离（迁移专用库/事务回滚） | 六条各自有负向测试；`UoWTest` 事件不再落共享库 |

不建议并行开工：S1/S2 是"同一类根因（保护没落结构）"的两面，先立住再谈 S3；否则每修一处都在旧模型上打补丁。

---

## 8. 值得保留的地基（审计确认成立）

- **契约单一事实源 + 缺口显式债务**（R09）：`resource_schema_gaps.yaml` 的 `referenced_by/reason/contract_ref/unblocks_when` 与五类双向校验，方向正确，是这批工作里最有价值的机制；
- **职责分离在运行时以真实主体比对**（`script_service.py:236-245`），不是客户端字符串；
- **RLS fail-closed 在资产域真实生效**（无租户上下文 0 行，实测）；
- **错误信封扁平且不泄漏内部**；`/errors` 目录默认关闭 + Token 门；
- **限流 fail-open 是显式取舍并计指标**（`packages/shared/rate_limit.py:9-20, 92-97`）；
- **JWT 只接受 RS256、issuer/audience 强校验、roles 类型 fail-closed**（`packages/identity/jwt_core.py:101-132`）；
- **outbox relay 语义诚实**：没有下游就不 ack、积压可见（`consumer.py:192-199`）；
- 248 个测试与五道闸门本身可稳定跑绿（本轮复跑：`contract_check` 全 PASS / `pytest` 248 全过）。

---

## 9. 未覆盖 / 无法验证（明确写出来，避免"审过了"的错觉）

1. **V2.0.5 正文不在本机**：镜头域、32.7 元数据表命名、Phase 1 验收细节无法对正文核验；本报告中所有"契约"结论仅基于仓库内 `contracts/`。
2. **未做并发压测与长事务实测**：`b2c3d4e5f6a7`、`f6a7b8c9d1e2` 标注的重迁移风险只在文档层面确认。
3. **未做渗透测试**：SQL 注入、越权链（JWT→路由→服务→DB）只做了静态阅读与单点验证。
4. **未在真实密钥环境验证认证链路**：审计用的是测试密钥；生产 PEM 注入路径未跑。
5. **未验证 S3/模型网关/Temporal/MQ 的真实连通性**（均未实现或只有配置）。
6. **未核对 `.env` 是否被误提交历史**：本轮只确认工作区干净，未扫 git 历史。

---

## 10. 修复进度（滚动更新）

| 编号 | 问题 | 状态 | 落地物 |
|---|---|---|---|
| P0-1 | 分区 RLS 失效、可直读跨租户事件 | **已修复** | 迁移 `b7c8d9e0f1a2`；`packages/shared/partition_guard.py`；维护工具建分区即保护；4 条不变量断言 |
| P0-2 | 双实例端口，`127.0.0.1` 假世界 | **识别侧已修复**（旧集群未停，属用户环境决策） | `/readyz` 回报 `schema_revision`；runbook 新增「先确认连的是哪个库」与旧集群处置节 |
| P1-1 | QC 幂等键跨资产复用 | **已修复**（S3） | `_assert_report_owner`（409）+ 跨资产单测 |
| P1-2 | 两跳只发 1 条事件 | **已修复**（S3） | 逐跳事件 `ASSET_VERSION_HUMAN_REVIEW_ENTERED` + 事件契约 |
| P1-3 | schema 闸门假绿（删 46/76 仍 PASS） | **已修复**（S4） | `check_schema_objects()` 结构+行为校验（object/properties/required/形状/$ref/运行时双向比对）+ 4 条负向测试 |
| P1-4 | 不可变未落结构（内容列回写 + DELETE） | **已修复**（S3 触发器 + R10 授权） | 迁移 `d1e2f3a4b5c6`；仓储白名单；`qc_reports` 禁 UPDATE/DELETE |
| P2-1 | Project/Episode 无状态变更入口 | **部分修复**（S6 补创建事件，状态变更入口仍缺） | S6 同事务发 `PROJECT_CREATED`/`EPISODE_CREATED`（事件缺口清零）；但 Project/Episode 的 HTTP 状态变更 action 仍未登记（ADR-R12 明示「需等正文口径，不猜契约」），域转移表的非初始边无 action 入口，已登记于 `contracts/runtime/state_machine_gaps.yaml` 的 `transition_entry_gaps` |
| P2-2 | relay 毒事件无上限/无死信 | **已修复**（S5） | 迁移 `f3a4b5c6d7e8`（attempts/next_attempt_at/dead_lettered_at）；指数退避 + 死信 + runbook 巡检/重放 |
| P2-3 | 资产 GET 未走只读 UoW | **已修复**（S4） | `asset_routes.read_asset_version` 改用 `_get_read_service`（`get_read_tenant_uow`） |
| P2-4 | 响应/契约字段漂移 | **已修复**（S4） | 响应模型由契约动态生成（`packages/shared/contract_models.py`）；`to_dict` 去 tenant_id 等内部字段；4 个资产路由挂 `response_model` |
| P3-1 | 剧本同 id 幂等命中即返回旧版本 | **已修复**（S6） | 剧本/项目/分集统一内容比对，内容不一致 `409 IDEMPOTENCY_CONFLICT` |
| P3-2 | JWT 有 jti 无吊销 | **已修复**（S6） | 迁移 `c6d7e8f9a0b1`（`auth.revoked_tokens`）；认证依赖验签后查吊销；`scripts/revoke_token.py` 运维 CLI |
| P3-3 | outbox 残留 UoWTest 事件 | **已修复**（S6） | 迁移 `a4b5c6d7e8f9` 清理；`test_unit_of_work.py` 用迁移角色自清理 |
| P3-4 | 归档 runbook 缺失 | **已修复**（R10） | `runbook-outbox-retention.md` §3 校验、§4 处置 |
| P3-5 | `maintenance_runs` 无 RLS 且 app_role 可写 | **已修复**（S6） | 迁移 `b5c6d7e8f9a0` 收回 app_role 全部权限（运维走迁移角色） |
| P3-6 | 生产必需密钥含未实现依赖 | **已修复**（S6） | `Settings._PRODUCTION_REQUIRED_SECRETS` 只保留真实依赖（DB+PEM）；新增回归测试 |

闸门复跑（S1/S2 完成后）：`contract_check` 全 PASS（ACTIVE 37 / 76 定义不变）、
`ruff` 0、`mypy` 87 files 0、`pytest` **253 passed**、`alembic check` 无漂移；
迁移 `downgrade -1 → upgrade head` 往返验证通过。

S3 完成后复跑：`contract_check` 全 PASS（新增事件契约 8 / 缺口 2）、`ruff` 0、
`mypy` 87 files 0、`pytest` **263 passed**、`alembic check` 无漂移；
迁移 `d1e2f3a4b5c6` 往返通过，触发器与内容列保护完整恢复。

S4/S5/S6 完成后复跑：`contract_check` 全 PASS（Schema 76 定义结构校验 +
运行时字段一致 2 对象；事件 10 / 缺口 0）、`ruff` 0、`mypy` **90 files 0**、
`pytest` **272 passed**、`alembic check` 无漂移；
迁移 `e2f3a4b5c6d7 → f3a4b5c6d7e8 → a4b5c6d7e8f9 → b5c6d7e8f9a0 →
c6d7e8f9a0b1` 已执行到 head。新增迁移的 downgrade 未逐条往返（见 ADR-R12
「未做项」）。

---

## 附录 A：复现命令（只读，不写业务数据）

```powershell
# 1) 认清连的是哪个实例（关键：不要用 127.0.0.1）
Get-NetTCPConnection -LocalPort 15432 -State Listen |
  Select-Object LocalAddress,LocalPort,OwningProcess
$env:PGPASSWORD="<从 .env 取>"
& "D:\Program Files\PostgreSQL\16\bin\psql.exe" -h localhost -p 15432 -U postgres -d ai_film_industrial -w `
  -c "select current_database(), version_num from alembic_version;"

# 2) 分区 RLS 与直连授权
-c "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='outbox' order by 1;"
-c "select table_name, string_agg(privilege_type,',' order by privilege_type) from information_schema.role_table_grants where grantee='app_role' and table_schema='outbox' and table_name like 'outbox_events%' group by 1 order by 1;"
# 以 app_role、无租户上下文直查分区（应为 0 行；当前 276）
-c "select count(*) from outbox.outbox_events_y202611;"

# 3) 事件/状态对账
-c "select aggregate_type, event_type, count(*) from outbox.outbox_events group by 1,2 order by 1,2;"
-c "select count(*) as approved, count(*) filter (where entity_version=4) as v4 from asset_service.asset_versions where status='APPROVED';"

# 4) 闸门假绿复现：删 46 个定义后仍 PASS
& ".\.venv\Scripts\python.exe" "D:\tmp\audit_cc_probe.py"

# 5) QC 跨资产复用 + 两跳事件缺口复现
& ".\.venv\Scripts\python.exe" "D:\tmp\audit_asset_probe.py"
```

本轮实测摘要（供对账）：

```
contract_check : ACTIVE 37 / 27 类型 / 债务 9 / 豁免 4 / Schema 76 / 授权落点 12 / 可达性 4 机 5 债务 → 全 PASS
pytest         : 248 passed（72*3 + 32 个点）
outbox 事件    : APPROVED 11 / CREATED 47 / QC_REPORT_CREATED 11 / SCRIPT_IMPORTED 126 /
                 SCRIPT_LOCK_APPROVED 27 / SCRIPT_LOCK_REQUESTED 36 / SCRIPT_SUPERSEDED 9 / UOW_TEST 9
分区直查       : 276 行（app_role，无租户上下文）
schema 闸门    : 30 个定义仍 PASS
```

## 附录 B：证据索引

| 结论 | 证据 |
|---|---|
| 分区 RLS 未生效、可直读 | `f6a7b8c9d1e2_partition_outbox_events.py:105-121`；`ADR-R06-2:41-44`；`pg_class.relrowsecurity` 实测 |
| 默认宽授权根因 | `9e3b4c5d6e7f_create_app_role_and_grants.py:84-93`；`c1d2e3f4a5b6:132-141` |
| 双实例 | `Get-NetTCPConnection` 三个监听 + 两个 `alembic_version` 实测 |
| QC 幂等键域错误 | `asset_service.py:176-179`；运行时复现输出 |
| 两跳单事件 | `asset_service.py:287-315`；事件/版本计数对账 |
| 不可变未落结构 | `versioning.py:78-93`；`sqlalchemy_asset_repo.py:86-93`；迁移 `c1d2e3f4a5b6:132-141`；全库 0 触发器 |
| 闸门假绿 | `contract_check.py:684-704`；30 定义 PASS 实测 |
| 状态机无入口 | `contract_check.py:111-125, 802-820`；action_registry 仅 PROJECT/EPISODE CREATE/READ |
| relay 毒事件 | `workers/relay/consumer.py:179-221` |
| 只读 UoW 未覆盖 | `asset_routes.py:148-156` vs `deps.py:36`、`unit_of_work.py:54-58` |
| 响应契约漂移 | `entity.py:105-119`；`openapi.draft.yaml` 0 处 `tenant_id`；`qc_report.py` 无 `deviation_intent_ref` |
| script 幂等弱 | `script_service.py:100-104` |
| jti 声明未实现 | `jwt_core.py:64-79,138`；全仓无 denylist |
