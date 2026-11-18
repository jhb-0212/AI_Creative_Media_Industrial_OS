# PR #11 安全与数据边界收敛（2026-11-18）

> 对应外部审查报告《重新限定审查范围：唯一审查对象 AI_Creative_Media_Industrial_OS》
> 的 P1-01…P1-05 / P2-01 / P2-02 / R6–R9 与 CI `image-build` 失败。
> 本文只记录**已落地的根治**与**可复现证据**；未做的明确写在「本轮不做」。

## 0. 结论

| 项 | 结论 | 证据 |
| --- | --- | --- |
| P1-01 Relay 整表 UPDATE 越权 | 已根治（列级授权，三层覆盖） | 迁移 `b9e1c2d3f4a5`；`tests/integration/test_ownership_db_roles.py` 4 项 |
| P1-02 / R9 app_role 残留 outbox 写权 | 已根治（回收 INSERT/UPDATE，保留 SELECT） | 迁移 `c1f2e3d4a5b6`；`test_auth_revocation_and_grants.py`、`test_outbox_partitions.py` |
| P1-03 / R8 组织归属跨租户 | 已根治（租户复合唯一 + 复合外键 + organizations RLS） | 迁移 `d2e3f4a5b6c7`；`test_ownership_fk_constraints.py` 2 项负测 |
| P1-04 / R7 进程配置跨边界 | 已根治（配置读取与进程校验解耦；DSN 显式接收进程上下文） | `packages/shared/config.py`、`database.py`；`tests/unit/test_process_entrypoints.py` |
| P1-05 身份服务物理拆分 | 已落地（`workers/identity` 持私钥 + JWKS；API 只持公钥） | `workers/identity/app.py`；`tests/unit/test_identity_process.py`；契约 `KEYS-3` |
| P2-01 幂等未含 organization_id | 已根治 | `services/project_planning/service/project_service.py` |
| P2-02 / R6 降级不可逆、角色名双源 | 已根治（先 REVOKE 再 DROP；角色名读配置） | 迁移 `7a8b9c0d1e2f`、`8f3b2c1d4e5a`；真实 PG 往返验证见 §6 |
| CI image-build FAIL | 已修复（注入测试公钥，保留 fail-closed） | `.github/workflows/ci.yml`；CI 现场用镜像自身密钥工具生成临时公钥（仓库零密钥材料） |

闸门结果：`ruff` / `mypy` / `pytest tests` / `architecture_check` / `contract_check`（11 条不变量）/
`freeze_manifest --check` / `alembic check` 全绿；`upgrade → downgrade → upgrade` 在真实
PostgreSQL 16 上往返成功。

## 1. P1-01：Relay 只能改投递元数据，不能改事件内容

**根因**：授权模型把「回写投递状态」错授成「整表可改」。`7a8b9c0d1e2f` 与
`ae8724980e0b` 均对 relay 授 `GRANT SELECT, UPDATE`（整表），于是 relay 可
`UPDATE ... SET payload/event_type/tenant_id`，破坏「审计事件不可篡改」。

**治理**（不新增校验层，改授权粒度本身）：
- 列清单收敛为唯一事实源 `RELAY_UPDATE_COLUMNS`（`packages/shared/partition_guard.py`），
  迁移与维护工具共用 `relay_column_update_statements()`；
- 三层收口：父表 + 所有已存在分区 + 撤销未来分区的整表 UPDATE 默认权限；
- `7a8b9c0d1e2f` 新建库时即按列授权，避免在 `b9e1c2d3f4a5` 之前存在越权窗口。

**证据**：`test_relay_cannot_update_event_content`（对 payload/event_type/tenant_id/
aggregate_id 逐个执行 UPDATE，断言 `permission denied`）、
`test_relay_column_privileges_match_contract`（逐列 `has_column_privilege` 与契约比对）。

## 2. P1-02 / R9：旧 app_role 的 outbox 写权限回收

**根因**：角色生命周期没有闭环。`8f3b2c1d4e5a` 拆分域角色时回收了 app_role 对
project_planning / asset_service / auth 的权限，**漏了 outbox**，旧角色因此仍能
伪造/篡改事件——一条绕过新域角色边界的入口。

**治理**：`c1f2e3d4a5b6` 回收 `outbox.outbox_events` 的 INSERT/UPDATE 与未来表的
默认写权限，保留 SELECT（app_role 仍是 RLS 隔离与分区直连拒绝的验证角色）。
`outbox_maintenance` 的权限早已由 `b5c6d7e8f9a0` 收回，DELETE 由 `d4e5f6a7b8c9`
收回，本迁移补齐最后一环，使 app_role 的 outbox 权限收敛为「只读」。

**证据**：`test_legacy_app_role_outbox_grants_reclaimed`（口径 + 逐列 UPDATE 断言）、
`test_legacy_app_role_cannot_write_outbox`（真实 INSERT 被拒）、
`test_app_role_has_only_select_on_outbox`（父表授权集合 == {SELECT}）。

## 3. P1-03 / R8：组织归属必须是「同租户」的

**根因**：`9f1a2b3c4d5e6` 只建了 `organization_id` 单列外键——数据库能证明
「组织存在」，不能证明「组织属于同一租户」。同时 organizations 表未启用 RLS，
归属根可被跨租户全量读取。

**治理**：`d2e3f4a5b6c7` 建 `UNIQUE (organization_id, tenant_id)`，把 projects 的
外键升级为 `(organization_id, tenant_id)` 复合外键，并给 organizations 启用
租户 RLS。ORM 同步（`services/project_planning/repository/orm.py`），
`alembic check` 无漂移。

**证据**：`test_project_cannot_reference_cross_tenant_organization`（A 租户组织 +
B 租户项目 → 复合外键拒绝）、`test_organizations_are_tenant_isolated`（B 上下文读 A 组织 0 行）、
`test_project_with_valid_organization_succeeds`（合法归属不被误伤）。

## 4. P1-04 / R7：配置读取 ≠ 进程身份校验

**根因**：生产密钥校验写在 `Settings` 的 `model_validator` 里，于是**任何读配置的
代码**都隐式承担 api 角色的密钥要求。relay 构造自己的 DSN 时内部调了一次
`get_settings()`，就被要求持有 API 的身份公钥——「配置读取」与「进程身份校验」
耦合，导致跨进程越界。

**治理**（结构上消除，而非给每个进程补环境变量）：
1. 校验外移为 `Settings.validate_process_requirements()`，只由
   `get_process_settings(role)` 在**声明身份那一刻**调用；`get_settings()` 退化为
   纯配置读取。fail-closed 未削弱，位置更准。
2. DSN 构造收敛为唯一实现 `_build_url()`（原五份重复 f-string），五个
   `build_*_database_url(settings=None)` 显式接收进程配置；relay 用
   `create_relay_engine(settings)`（连接池参数与角色同源，不再在入口复制一份）。
3. `workers/outbox_maintenance` 的 `run_once` / `_record` 显式接收本进程 settings。

**证据**：`tests/unit/test_process_entrypoints.py`（每个入口必须
`get_process_settings` 且角色正确；DSN 构造器必须能接收 settings；角色名来自配置）、
`test_bare_construction_has_no_process_semantics`、`test_process_settings_declares_and_validates`。

## 5. P1-05：身份签发物理进程拆分（KEYS-3）

**根因**：S1 只做了模块隔离，签发方与验签方仍编译进同一进程；私钥边界靠
「进程配置清单」的约定挡，而红线禁止「配置声明代替运行时隔离」。

**治理**（按 `docs/proposals/KEYS-1` 已定稿方案 S3/S4）：
- 新增独立进程 `workers/identity`（`python -m workers.identity`）：持私钥，
  提供 `/.well-known/jwks.json` 与 `/health`，**不**新增 token 签发端点；
- 删除 `PublicKeyStore.from_key_store` 派生桥（验签方结构上无法经私钥取公钥）；
- 契约新增 `KEYS-3`（签发/验签进程物理分离），KEYS-1 residual 更新为「已拆出」；
- JWKS 属系统面端点（同 `/health`、`/metrics`），不进 OpenAPI——否则会破坏
  `OpenAPI 操作数 == Action 数` 的派生关系；
- compose 新增 `identity` 服务（同镜像不同 entrypoint，私钥只注入该服务）。

**证据**：`tests/unit/test_identity_process.py`（JWKS 只含公钥参数、响应不含私钥片段、
kid 与签发密钥同源）、`tests/unit/test_identity_public_store.py`
（无 `from_key_store` 桥）、架构闸门 R4（apps 不得 import 签发模块）。

## 6. P2 与 R6：幂等语义、迁移可逆性、角色名单一事实源

- **P2-01**：`_assert_same_request` 纳入 `organization_id`——同一 entity_id 换组织
  再提交是另一个请求，不得静默返回旧记录。
- **P2-02 / R6**：`7a8b9c0d1e2f` / `8f3b2c1d4e5a` 的角色名改为读 `Settings`
  （与 DSN 构造同源）；降级路径先 REVOKE 全部授权与默认权限再 DROP ROLE。
- **真实往返证据**：`alembic downgrade 9f1a2b3c4d5e6`（跨过 relay/域角色两个
  DROP ROLE）→ `alembic upgrade head` 成功；`alembic current` 回到
  `d2e3f4a5b6c7 (head)`。

## 7. CI：镜像冒烟注入测试公钥（不放宽校验）

**根因**：KEYS-1 拆桥后 API 无公钥即拒绝启动（设计如此），而 `image-build` 的
import 冒烟没有提供公钥，于是 FAIL。

**治理**：冒烟阶段用**镜像自身**的密钥工具现场生成一把临时公钥并注入
（仓库零密钥材料：`.gitignore` 已忽略 `*.pem`/`*.key`），同时新增 identity 进程
的 import 冒烟。生产 fail-closed 一字未改——绝不用「放宽校验」换绿。

## 8. 本轮不做（边界，防止范围膨胀）

- Phase 1 生产主链（F05 业务闭环）未推进；
- 密钥自动轮换调度、token 签发 HTTP 端点、OIDC 完整协议；
- `process_role` 的新增（如 `migration`）——alembic 在 `get_settings()` 解耦后
  已不再需要 api 角色密钥，无需新角色维度；
- 高并发压测实测报告（方案与脚本已有，待目标环境跑出）。

## 9. PR 评审对话收口（2026-11-18，Codex 16 条）

Codex 机器人在 PR #11 留下 16 条评审对话，逐条对照代码后：**9 条已被本轮
之前的提交根治**（#1→P1-04、#2/#8→P1-03、#3→P2-01、#4→P1-01、#5/#6→P2-02、
#7→P1-04、#9→P1-02），**7 条未修**，本轮全部落地：

| # | 评审点 | 修复 |
| --- | --- | --- |
| 10 | 授权不校验组织范围：持 `Project:*` 可操作租户内其他组织的项目 | Principal/JWT 增 `org` claim；项目 create/read/status-change 增资源级 ABAC（`_assert_organization_scope`，fail-closed），6 条集成用例 |
| 11 | load_test 不把 HTTP 4xx/5xx 计入错误 | 非 2xx 计入 errors、错误率按总请求数计算、存在错误退出非零 |
| 12 | 生产迁移可用仓库内 `relay_dev_password` 建 relay 角色 | `reject_development_default_password`（共享守卫），relay/app/域角色迁移统一接入 |
| 13 | 同上——域角色 `*_dev_password` | 同上（单一事实源 `DEVELOPMENT_DEFAULT_PASSWORDS`） |
| 14 | identity 进程不连库却被要求 `POSTGRES_APP_PASSWORD` | 必需清单移除该项 + 清除死守卫；用例改断言「identity 无 DB 口令也能启动」 |
| 15 | `/readyz` 只探 project 池 | 显式探测 project+asset 双池，任一失败 503；用例拆成双池负向 |
| 16 | 存量角色只改密码不重置属性 | `ALTER ROLE ... WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD`（relay/app/域三处一致） |

**诚实边界**：#16 修改了已在本分支验证过的迁移文件，此前的
upgrade→downgrade→upgrade 往返证据需在合并后的真实 PG 上重跑（CI 集成测试
覆盖迁移链）；本地沙箱数据库不可用，本轮 7 项修复的运行期验证以 CI 为准。
