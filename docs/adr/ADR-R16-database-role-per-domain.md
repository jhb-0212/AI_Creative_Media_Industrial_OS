# ADR-R16：数据库运行身份按业务域拆分

- 状态：已接受
- 日期：2026-11-16
- 关联：审查第三轮 F2「数据所有权」、F3「身份与授权」；
  铁律 2「不屎山代码」、铁律 3「禁止补丁式演进」、铁律 5「防呆 + 自动化约束」；
  `docs/adr/ADR-R10-partition-rls-and-grant-baseline.md`（权限基线反转，本 ADR 的前置）、
  `docs/adr/ADR-R14-layering-ci-gate.md`（R4 规则的挂载点）
- 迁移：`c2d3e4f5a6b7_split_database_roles.py`

## 1. 背景

审查第三轮指出一条成立的事实：

> 单一 `app_role` 跨 Schema 授权。`9e3b4c5d6e7f` 对每个 Schema
> `GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES`。

后果不是"权限给多了"这么抽象，而是：

- **模块边界只存在于目录里**。`services/asset_service` 拼一条
  `UPDATE project_planning.projects` 的 SQL 完全可行，`architecture_check`
  的 R1/R2 也拦不住——它只看 `import`，而越权写表常常只是字符串；
- **relay 与 API 同身份**。Outbox Relay 是只该读事件、回写投递状态的进程，
  但它与 API 共用角色，也就共同拥有业务表的写权；
- **RLS 的防线只剩一层**。租户隔离有效，但"这个模块能不能写这张表"没有答案。

ADR-R14 已经把「模块之间不许互相 import」变成 CI 约束，但它挡不住第二种越权：
不经 import，直接连线数据库。

## 2. 决策

把数据库运行身份按**业务域**拆分，一个域一个 PostgreSQL 角色、一个连接池，
连接身份在建立连接时固定。

### 2.1 角色与授权（真实执行的部分）

| 域 | 角色 | 可访问对象 |
|---|---|---|
| `project_planning` | `svc_project_planning` | `projects`/`episodes`/`script_revisions` 读写；`outbox.outbox_events` SELECT+INSERT |
| `asset` | `svc_asset` | `asset_versions` SELECT/INSERT/UPDATE；`qc_reports` SELECT/INSERT；`outbox.outbox_events` SELECT+INSERT |
| `outbox_relay` | `svc_outbox_relay` | `outbox.outbox_events` SELECT + **列级** UPDATE(`published_at`,`attempts`,`next_attempt_at`,`dead_lettered_at`)；`list_active_tenants()` EXECUTE |
| `identity` | `svc_identity` | `auth.revoked_tokens` SELECT/INSERT |

全部角色：`LOGIN` / `NOSUPERUSER` / `NOCREATEDB` / `NOCREATEROLE` / `NOBYPASSRLS`。
旧 `app_role` 在迁移末尾被 `DROP`——**保留它等于保留一条所有人都会走的回退路径**。

三条刻意的设计选择：

1. **不设 `ALTER DEFAULT PRIVILEGES`**。新表必须显式 `GRANT`；忘记授权会立刻
   报权限错误（fail-closed），而不是静默获得跨域写权。
2. **Outbox 正文用列级授权保护**。relay 拿不到 `payload` 的写权，
   "事件正文只追加"因此在数据库层成立，不依赖代码自觉。
3. **角色名写进代码（`packages.shared.db_roles`），不做配置项**。删除了
   `POSTGRES_APP_USER`：用户名可配置等于权限边界可配置，把值改成 `postgres`
   就能绕过全部按域授权。

### 2.2 代码侧：域绑定工作单元

- `DomainPools`（`packages/shared/database.py`）持有四个域的引擎与会话工厂，
  生命周期在 lifespan 统一收口（漏关一个池比漏开更隐蔽）。
- `build_domain_uow_dependency(domain)` 是唯一的取池入口，
  `build_tenant_uow_dependency(..., domain=...)` 在其上叠加 RLS 上下文；
  `deps.py` 导出 `get_project_uow` / `get_asset_uow` 等具名依赖。
- **没有默认域**：不提供无参的 `get_uow()`。默认值会让忘记声明的代码悄悄
  以某个角色运行，正是本 ADR 要消灭的形态。

### 2.3 闸门：R4 数据库身份旁路

`scripts/architecture_check.py` 新增 R4：业务模块不得

- `import` `DomainPools` / `create_engine` / `create_sessionmaker` / `build_database_url`；
- 访问 `app.state.pools`（含 `getattr(request.app.state, "pools")` 这种等价写法）。

允许清单 `DATABASE_ACCESS_ALLOWLIST` 逐条写明理由（连接池定义处、UoW 构造处、
依赖装配处、就绪探针、relay 入口），**无命中即报「陈旧登记」**——白名单不许常驻。

## 3. 为什么不选另外两条路

| 方案 | 否决理由 |
|---|---|
| 单角色 + 运行时 `SET ROLE` | 忘了切角色就退回全权；"记得切"是人的纪律，不是结构约束 |
| 每个 repository 自建 engine | 连接池数量随模块数增长，且给了每个模块"选身份"的自由，正好反向 |

## 4. 后果

**变好的**：

- 跨模块写表在数据库层失败（`permission denied`），与 `import` 无关；
- relay 被攻破也改不了业务数据、改不了事件正文；
- 连接池按域隔离，一个慢域不会吃光全部连接。

**代价（明说）**：

- 一次请求可能触碰两个域时（目前没有），需要显式开两个工作单元，
  跨域事务不再"免费"——这正是数据所有权要求的边界；
- 测试必须声明域（`project_sm` / `asset_sm` / `relay_sm` / `identity_sm`），
  不复存在"一个万能池"；
- 新增域要改三处（`db_roles` 枚举、迁移授权、分区回收名单），
  缺一处即 fail-closed。

## 5. 验证

| 手段 | 证明什么 |
|---|---|
| `tests/integration/test_database_role_isolation.py`（11 条） | 跨域写/读、事件正文改写、审计删除均被数据库拒绝；`app_role` 已不存在；全部角色非超级用户且 `NOBYPASSRLS` |
| `tests/integration/test_auth_revocation_and_grants.py::test_grant_baseline` | 维护元数据无人可写；吊销表只给身份域 |
| `tests/integration/test_outbox_partitions.py` / `test_tenant_isolation_invariants.py` | 分区直连对任何应用角色都不可用；父表路径仍然 fail-closed |
| `scripts/architecture_check.py` R4 + 6 条负向测试 | 业务模块不能自行挑连接身份 |
| `alembic check` | 迁移与 ORM 模型一致 |

## 6. 回退

`alembic downgrade c2d3e4f5a6b7` 会重建 `app_role` 的旧口径授权并删除四个域角色；
但 append-only 的收紧（`asset_versions` 无 DELETE、`qc_reports` 无 UPDATE/DELETE）
**不随角色回滚放开**——那是 ADR-R10 的结论，与本 ADR 无关。

`alembic check` 与 `test_grant_baseline` 会在回退后立即报红，避免"回退了但没人发现"。
