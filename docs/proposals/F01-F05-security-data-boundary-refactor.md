---
doc_type: proposal
status: in_progress
updated_at: 2026-11-18
authoritative_sources:
  - contracts/security_boundary.yaml
  - contracts/runtime/action_implementations.yaml
open_items:
  - item: >-
      会签累积机制未落地：AUTH-5 已让 `/`（席内 OR）与 `+`（席位间 AND）的语义
      可判定，并让多席位动作显式拒绝而非静默降级；但「多主体分次签署、凑齐后
      放行」需要审批事实契约（谁签了哪个席位）与目标动作的运行时入口，两者均
      未就位。唯一含 '+' 的 ACTIVE 动作 DELIVERY.MANIFEST.APPROVE 属交付链，
      该链 11 个动作全无入口
    source: "contracts/security_boundary.yaml#AUTH-5"
  - item: >-
      边带守卫仅 AssetVersion 落地；Project / Episode / ScriptRevision 的转移边
      只有结构、尚无证据类准入门槛（AUTH-4 的 residual_risk）
    source: "contracts/security_boundary.yaml#AUTH-4"
---

# F01–F05 安全与数据边界统一重构方案

> 状态：**以文首 front-matter 为准**（`status: in_progress`，残余项见 `open_items`）。
> 各阶段的完成度不再手写打勾，一律引用事实源：
> `contracts/security_boundary.yaml`（15 条边界不变量的断言结果）、
> `contracts/runtime/action_implementations.yaml`（动作实现台账）、
> `scripts/architecture_check.py`（R1–R5 分层闸门）。
> 触发来源：外部深度审查报告（2026-11-17），与仓库第三轮系统性审查结论一致。
> 关联准则：R1（一个事实一个权威源）、R6（安全必须真实执行）、R8（不变量放最可靠层级）、
> R10（旧机制必须清理）；用户最高准则「不要打补丁，要系统性根治」。
> 关联 ADR：ADR-R06 / R08 / R10 / R12 / R14 / R15。

---

## 0. 结论先行

外部审查报告的五个 P1（F01–F05）经逐条代码核实，**全部属实**，且指向同一个根因：

> **安全边界（授权与身份）、业务边界（资源归属）、数据边界（数据库所有权）三者尚未统一，
> 各自在代码里用不同机制零散表达，导致「写对的代码」和「安全的系统」可以同时不成立。**

本方案不是给五个问题各写一个补丁，而是把它们收敛为**一次边界重构**，先冻结边界契约，
再落地到授权模型、数据模型、数据库角色、进程配置四条腿。执行顺序严格按依赖：
**先冻结边界 → 数据模型 → 授权模型 → 数据库角色 → 进程密钥**，任何一步都不得在
上一步未验收时开始（否则又会叠床架屋）。

本方案**明确排除**的动作（违反根因治理）：新增 Manager / Factory / Wrapper 平行层、
用配置声明代替运行时鉴权、只让测试过而增加维护成本的补丁。

---

## 1. 现状核实（代码依据）

以下每一句都对应到当前 `main` 分支的真实文件，避免「凭印象重构」。

### 1.1 F01 — Project/Episode 缺状态推进入口（P1 阻断项）

- `services/project_planning/domain/project.py`：`Project.transition_to()` 已存在，
  状态机 `domain/state_machine.py` 的 `assert_transition` 也真实生效。
- 但 `contracts/runtime/action_implementations.yaml` 中 Project 仅登记
  `PROJECT.CREATE` / `PROJECT.READ`；Episode 仅 `EPISODE.CREATE` / `EPISODE.READ`。
- `contracts/runtime/state_machine_gaps.yaml` 的 `transition_entry_gaps` 已显式登记：
  Project 7 条转移边、Episode 3 条转移边，**均无任何状态变更 action 入口**。
- 结论：领域模型有状态机，但没有 HTTP 状态变更入口，项目「能创建不能推进到生产态」。

### 1.2 F02 — RBAC 已落地，Capability Token 未落地（P1 安全阻断项）

- `services/identity_access/rbac.py`：唯一授权判定是
  `principal.roles & allowed`（角色级 RBAC）。
- `packages/identity/principal.py`：`Principal` 只有
  `subject_id / tenant_id / roles / jti` 四字段，**无 capability 范围、资源范围、ABAC 条件**。
- `services/identity_access/action_registry.py` 的 `allowed_principals` 中带 `+` 的
  「多主体共同审批」条目，注释已自认「简单 RBAC 不匹配，后续由 ABAC/双人审批落地」——
  即当前是**主动留白的空转**，不是已实现的授权。
- 结论：`token_required: true` 只表达「需要认证」，不等于「capability 已被验证」。
  授权链路在 RBAC 之后**断掉了**。

### 1.3 F03 — 数据库跨模块写权限过宽（P1 安全阻断项）

- `data/migrations/versions/9e3b4c5d6e7f_create_app_role_and_grants.py`：
  `app_role` 被授予 `project_planning` / `outbox` 两个 schema 的
  `SELECT, INSERT, UPDATE, DELETE ON ALL TABLES`，API 与 Relay **共用同一角色**。
- 后续迁移（`b5c6d7e8f9a0` / `b7c8d9e0f1a2` / `d4e5f6a7b8c9`）已开始收窄 outbox 权限，
  但 `project_planning` 的宽授权仍是单一 `app_role` 全量读写。
- 结论：RLS 解决的是**租户行级隔离**，不是**模块级数据所有权隔离**。一个模块
  在数据库层可以写另一个模块的表，所有权仍靠代码约定（`owner_module` 字段）维持。

### 1.4 F04 — 租户隔离 ≠ 项目隔离（P1 安全阻断项）

- `services/project_planning/repository/orm.py`：`ProjectModel` 无 `organization_id`；
  `EntityMixin`（`packages/shared/orm.py`）信封有 `tenant_id` 但无组织归属。
- 归属关系目前仅靠 `ScriptModel` 的复合外键 `(project_id, episode_ref)` 锁死「脚本↔集数」，
  Project/Episode 之间**没有**同样的数据库级归属约束。
- 结论：同租户下的导演 A（项目 A）和导演 B（项目 B）因 `tenant_id` 相同即可互相访问，
  缺 `Tenant → Organization → Project → Episode → …` 的归属继承与授权规则。

### 1.5 F05 — API 配置仍要求身份签发私钥（P1 架构矛盾）

- `packages/shared/config.py:119-125`：`_PRODUCTION_REQUIRED_SECRETS` 包含
  `("identity_private_key_pem", "IDENTITY_PRIVATE_KEY_PEM")`，生产环境强制非空。
- `services/identity_access/auth_dependency.py`：验签只用公钥（`KeyStore`），
  明确声明「私钥绝不进入本路径」。
- `services/identity_access/__init__.py:36`：身份服务构造时读取私钥（签发用）。
- 结论：同一个全局 `Settings` 同时要求 API 和身份签发服务持有私钥，与「API 只持公钥」
  的架构声明矛盾。进程职责与密钥依赖未拆开。

### 1.6 一处内部不一致（需在评审时澄清，不属本方案修复范围）

审查报告第二节第 2 条（P2 死信统计）与第六节「历史问题已修复 R2」自相矛盾：
`data/migrations/versions/a1b2c3d4e5f6_fix_outbox_backlog_exclude_dead.py` 已把
`list_active_tenants()` 的 `pending_count` 与 `_CLAIM` 领取口径对齐，死信/退避中事件
已用 `FILTER` 从 backlog 排除，并新增 `dead_count` 单独观测。**该问题当前代码已修复**，
报告正文未同步第六节的核对结论。本方案据此把「Outbox 死信统计」从待办中移除，
但保留「事件状态定义与统计口径统一」作为数据模型重构的附属验收项（见 5.3）。

---

## 2. 根因分析

五个 P1 不是五个孤立缺陷，而是**同一根因的五个切面**：

| 切面 | 根因 | 表现 |
|---|---|---|
| F01 缺状态入口 | 业务边界：生命周期契约没接运行时 | 状态机有边无门 |
| F02 缺 Capability | 安全边界：授权模型停在 RBAC | 授权链断裂 |
| F03 跨模块写 | 数据边界：数据库角色粒度=「一个 app」 | 所有权靠约定 |
| F04 缺项目隔离 | 业务+安全边界：资源归属无组织层 | tenant 即权限 |
| F05 私钥越界 | 进程边界：进程职责与密钥依赖未拆 | 配置一刀切 |

根因可以归纳为一句：

> **「谁能对哪个资源在什么范围内做什么」这一授权/归属不变量，当前没有一个统一、
> 在数据库层可执行的事实源；它被拆散在 RBAC 代码、RLS 策略、owner_module 字段、
> 复合外键四处，各管一段，谁都不完整。**

---

## 3. 目标边界契约（冻结基线）

以下四条是本次重构要落地的**边界契约**，先冻结、后实现。每一条都给出「事实源在哪」、
「在哪里执行」，避免再次出现「一个事实多个权威源」。

### 3.1 授权边界（对应 F02 + F04 的授权部分）

**决策链（单一入口，不可分散到业务模块）：**

```
身份认证 → Action 解析 → RBAC → Capability → 资源范围 → ABAC → 职责分离 → 执行
```

| 环节 | 解决的问题 | 事实源 | 执行责任 |
|---|---|---|---|
| 身份认证 | 你是谁 | JWT claims（验签） | `identity_access` 认证依赖 |
| Action 解析 | 要做什么 | `action_registry.yaml` | 统一授权入口 |
| RBAC | 你属于什么角色 | `allowed_principals` | 统一授权入口 |
| Capability | 你被授予哪些资源/动作/有效期 | **新增 capability 事实表** | 统一授权入口 |
| 资源范围 | 你能否访问这个 project/episode | **资源归属链** | 统一授权入口 |
| ABAC | 资源属性/环境是否满足 | 资源当前状态 + 环境 | 统一授权入口 |
| 职责分离 | 申请人与审批人不同主体 | 已有 `requested_by/approved_by` | 统一授权入口 |

**硬性不变量：**
- 授权决策**只有一个入口函数**（如 `authorize(action, principal, resource) -> Decision`），
  业务模块不得自行实现任何形式的「if role in ...」判定。
- Capability 的校验必须在运行时发生（查事实表），不得用配置声明替代。
- 职责分离必须比对真实 `Principal.subject_id`，废除 `lock_script` 中
  `requested_by == approved_by` 的字符串比较。

### 3.2 资源归属边界（对应 F04）

**归属链（每一级都要有明确归属字段与继承规则）：**

```
Tenant → Organization → Project → Episode → Scene → Shot → Asset
```

- `Project` 增加 `organization_id`（可空=个人租户，非空=组织项目）。
- 每一级子资源必须通过**数据库外键**引用父级（不变量放最可靠层），
  而不是仅靠 Service 查询校验。
- 授权继承规则：对子资源的访问权限，默认从父级继承，除非有显式授权覆盖。
  「继承」必须有可测试的规则，不是一句口号。

### 3.3 数据库所有权边界（对应 F03）

**角色拆分（按模块域，不按「更多账号」）：**

| 角色 | 权限边界 |
|---|---|
| `project_planning_role` | 仅 `project_planning` schema 读写 |
| `asset_service_role` | 仅 `asset_service` schema 读写 |
| `identity_service_role` | 仅 `identity`/`auth` schema 读写 |
| `outbox_writer_role` | 仅 outbox 写入（按事件契约） |
| `outbox_relay_role` | 仅 outbox 领取 + 投递状态更新 |
| `migration_role` | 结构迁移（DDL） |

**硬性不变量：**
- 业务模块的运行时角色**只能写自己的 schema**，跨域访问必须走**正式接口**
  （如 outbox 事件、明确的服务 API），不得直接 `INSERT INTO 别人的表`。
- Outbox 写入与投递的**事务边界**要明确：业务写 + outbox 写在**同一事务**内完成
  （当前已有 `OutboxWriter`，需核对是否真的同事务）。

### 3.4 进程密钥边界（对应 F05）

| 进程 | 持有哪些密钥 | 不得持有 |
|---|---|---|
| 身份签发服务 | 私钥（签发）、公钥 | — |
| API | 公钥 / JWKS | 私钥 |
| Relay | 无签发私钥 | 私钥 |

**硬性不变量：**
- 生产启动检查只要求**当前进程真正需要**的密钥，不再有全局统一的密钥清单。
- `identity_private_key_pem` 从 API/Relay 进程的必需密钥中移除。

---

## 4. 执行阶段（严格按依赖顺序）

> 每个阶段有明确的**验收标准**，未达标不得进入下一阶段。这正是「先冻结边界、
> 再改码」的落地方式。

### 第一阶段：冻结边界契约（✅ 已完成）

- [x] 本方案经评审通过，边界契约（第 3 节）冻结为唯一事实源。
- [x] 边界契约写入 `contracts/security_boundary.yaml`，并接入 `contract_check.py`
      的 `check_security_boundary()`（结构化校验，非散文文档）。
- [x] 明确与边界契约冲突的现有机制，以显式债务登记（9 条不变量：7 debt / 2 partial）。

**落地产物（2026-11-17）：**
- `contracts/security_boundary.yaml`：四条边界（authorization / resource_scope /
  data_ownership / process_keys）的 9 条不变量，每条含 status（satisfied/partial/debt）。
- `scripts/contract_check.py` 新增 `check_security_boundary()`：校验边界契约结构自洽
  （债务字段完整、三态一致、id 唯一、陈旧债务报红），并注册进 `main()`。
- `tests/unit/test_contract_check.py` 新增 6 个测试：正向 + 缺字段 / 非法 status /
  债务缺 unblocks_when / 陈旧债务 / 重复 id 五种负向。
- 验证：`contract_check` 全绿（9 条不变量结构自洽）；`freeze-check` 通过（不破坏
  冻结一致性）；`architecture_check` 通过；单测 60 项全绿。

### 第二阶段：数据模型落地（F04 的归属 + F03 的数据基础）

- [x] `Project` 增加 `organization_id`，并补 `Tenant→Organization→Project` 外键链。
- [x] `Project`/`Episode` 之间补数据库级外键（对齐 Script 已有的复合外键做法）。
- [x] 迁移脚本：新增 `organization` 表 + 归属外键（迁移 `9f1a2b3c4d5e6`）。
- [x] 验收：跨项目访问的**负向测试**（同租户不同项目不可互访）在真实数据库上通过。
      证据：`tests/integration/test_ownership_fk_constraints.py`（外键负向 6 项）、
      `tests/unit/test_resource_scope_api.py`（真实端点跨组织 8 项，404/403 口径）。

**落地产物（2026-11-17）：**
- `data/migrations/versions/9f1a2b3c4d5e6_create_organizations_and_ownership_fk.py`：
  新建 `organizations` 表（归属根，最小字段集）+ `projects.organization_id` 外键
  + `episodes.project_id → projects.entity_id` 外键。
- `services/project_planning/repository/orm.py`：新增 `OrganizationModel`，
  `ProjectModel` 加 `organization_id`，`EpisodeModel` 加 `fk_episodes_project`。
- `services/project_planning/domain/project.py`：`Project` 加 `organization_id`
  （可空），`to_dict` 不泄漏该内部归属键（与 tenant_id 一致）。
- `services/project_planning/repository/sqlalchemy_repo.py` / `service/project_service.py`
  / `api/project_routes.py`：organization_id 贯通持久化到 API。
- `tests/unit/test_project_domain.py`：新增 3 个测试（默认 None / 承载 / 不泄漏）。
- `tests/integration/test_ownership_fk_constraints.py`：新增 3 个外键负向/正向测试
  （跨组织、悬空集数、合法归属）。
- 验证：contract_check / freeze-check / architecture_check / mypy 全绿；
  离线单测 109 项全绿；集成测试因无 PG 未执行。

**关键架构决策：**
- organization 是**纯归属/隔离键**，非业务聚合根：无 entity_type、无状态机、
  无对象 schema，与 tenant_id 同构。因此**不触碰契约冻结的 object_schemas**，
  不需要改 contract_amendments.yaml。
- organization 表放 `project_planning` schema（当前唯一消费方是项目域）；
  未来资产域若需归属组织再按数据所有权边界演进。
- `organization_id` 不进 API 响应契约（与 tenant_id 一致），通过可信 Principal
  取归属，而非从响应读可猜测的归属标识。

**风险已解除（2026-11-18 更新）：**
- 旧记录「迁移未在真实 PostgreSQL 执行验证（本地无 PG）」已失效：本轮整改在干净库
  `ai_film_verify` 上执行 `alembic upgrade head` 并跑通全量测试（含 tests/integration）。
- 旧记录「外键真实生效待部署验证」已失效：外键负向测试现在**在真实 PG 上执行**，
  且 SCOPE-1/SCOPE-2/SCOPE-3 已由断言 `scope.*` 在闸门里逐条核对（security_boundary
  不再用 residual_risk 兜底）。

### 第三阶段：授权模型落地（F02）

- [x] 建立统一授权决策入口 `authorize(action_key, principal)`（RBAC → Capability 链）。
- [x] Capability 落地：`Principal.capabilities` + JWT `caps` claim 内嵌（无状态、短效），
      接入统一授权入口，`token_required: true` 从死声明变为真实运行时校验。
- [x] 废除 `lock_script` / `asset_service` 的分散字符串职责分离，统一为
      `require_different_subject`（比对 `Principal.subject_id`）。
- [x] 验收：`tests/unit/test_authorize.py`（12 项）+ `test_identity_jwt.py` caps 用例
      通过；RBAC 越权、缺能力、自批/缺申请人拒绝均有负向覆盖。
- [ ] 遗留：ABAC（资源属性/环境条件）、多主体共同审批（allowed_principals 含 `+`）、
      细粒度资源范围授权（具体 project_id）留待后续阶段（已在 security_boundary.yaml
      以 residual_risk 登记）。

### 第四阶段：数据库角色落地（F03）

- [x] 新增 `outbox_relay_role`（迁移 `7a8b9c0d1e2f`）：Relay 专用角色，仅 outbox
      schema 的 SELECT + UPDATE(published_at)，零业务 schema 权限（进程边界隔离）。
- [x] Relay 进程改用 `build_relay_database_url()`（专用角色连接，不再共用 app_role）。
- [x] config 增加 `postgres_relay_user` / `postgres_relay_password` + 生产必需密钥清单。
- [x] 验收：`tests/integration/test_ownership_db_roles.py`（relay 越界写业务表拒绝 +
      不可伪造/删除事件，6 项负向）；`tests/unit/test_database_dsn.py` relay 角色断言。
- [x] 遗留已收敛（2026-11-18）：project_planning_role / asset_service_role 的按域拆分
      随「按域分连接池」落地；DATA-1/DATA-2/DATA-3 三条均已升级为断言
      （`data.domain_role_split` / `data.cross_domain_via_outbox` / `data.relay_column_grants`），
      由闸门执行判定，不再靠 debt 自我声明。

### 第五阶段：进程密钥拆分（F05）

- [x] 身份签发服务、API、Relay 使用独立配置与独立密钥权限：
      `Settings.process_role` + `_PRODUCTION_REQUIRED_SECRETS_BY_PROCESS` +
      `get_process_settings(role)`，三个进程入口（identity / relay / outbox-maintenance）
      显式声明角色（断言 `keys.process_scoped_validation`）。
- [x] `identity_private_key_pem` 从 API/Relay 必需密钥移除；API 侧只持公钥（R4 结构封死）。
- [x] 凭证面同时收口（第二轮审查 P1-01）：compose 的单一 `x-db-env` 拆为
      endpoint / superuser / business / relay 四份 anchor；identity 进程零 `POSTGRES_*`；
      relay 只持 relay 角色（断言 `cred.process_credential_surface` +
      `tests/unit/test_credential_surface.py`）；域角色名互异由配置期校验拒绝
      （断言 `cred.role_names_distinct`）。

### 第六阶段：状态推进入口（F01，依赖第二/三阶段完成）

- [x] Project / Episode 补齐状态变更 action：`PROJECT.STATUS.CHANGE`（`POST /projects/{id}/status`）
      与 `EPISODE.STATUS.CHANGE`（`POST /episodes/{id}/status`）已登记在
      `contracts/runtime/action_implementations.yaml`（闸门真实加载 OpenAPI 校验存在性）。
- [x] `state_machine_gaps.yaml` 的 `transition_entry_gaps` 清零（闸门显示「显式登记 0 项」）。
- [x] 资产域进一步把准入条件挂到转移边上（AUTH-4）：通用 `ASSET.STATUS.CHANGE`
      结构上无法到达 APPROVED，证据规则只存在于边上（`test_change_status_cannot_reach_approved`）。
- [ ] 其余 25 个 ACTIVE 动作（镜头 / 生成 / 交付域）仍无运行时入口——属 Phase 2 范围，
      不在本方案内，显式登记在 `action_implementations.yaml#not_implemented`。

---

## 5. 附属工程验收项（非本方案主修复，但须同步完成）

### 5.1 事件状态定义统一（呼应审查 P2 的根因）

虽然死信统计已修复，但「事件状态定义和统计口径」仍散落在 `outbox` 的
`published_at / dead_lettered_at / next_attempt_at` 三个字段与 consumer 的领取 SQL 里。
应抽取**统一的事件状态枚举**（PENDING / RETRYING / PUBLISHED / DEAD），让扫描、
监控、运维查询复用同一套判定，消除「口径漂移」的复发风险。

### 5.2 架构与安全测试（对应 F08/F10）

- 新增 `tests/architecture`：跨域直接写表检测、Domain 层禁止依赖 ORM/FastAPI。
- 新增 `tests/security`：Capability 越权、跨项目资源访问、数据库权限负向测试。

### 5.3 工程口径统一（对应 F09）

- `Makefile` 的 `typecheck` 与 `ci.yml` 统一为 `mypy packages services apps workers`，
  消除「本地过、CI 挂」。

---

## 6. 明确排除的动作（红线）

1. **不**新增 `AuthorizationManager` / `PermissionFactory` / 任何平行授权层，
   统一入口只有一个 `authorize`。
2. **不**用配置声明（YAML 里写「该 action 需要 capability X」）替代运行时校验。
3. **不**在 RBAC 上继续堆 `if` 判断来「模拟」Capability。
4. **不**简单地把 `app_role` 拆成更多账号就算完成 F03——必须同时解决跨域访问接口
   和事务边界。
5. **不**只为「让测试过」而在症状处加兜底，任何修复必须能说清「为何从根上解决」。

---

## 7. 验收总标准（对齐用户最高准则）

- 每个阶段完成后，能回答：**「为什么这样改是从根上解决，而不是让测试过了？」**
- 授权、归属、数据所有权、进程密钥四类边界各有一个**且仅有一个**权威事实源，
  并在数据库层/运行时真实执行。
- 旧机制（`owner_module` 字段冒充所有权、字符串职责分离、单一 `app_role`、
  全局密钥清单）在对应阶段完成后**被清理**，而非与新机制并存。

---

## 8. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-17 | 初稿：基于外部审查报告 F01–F05 逐条核实 + 根因收敛，形成统一边界重构方案 |
| 2026-11-18 | 状态改为机器可读 front-matter（`status: in_progress` + `open_items` 指向契约事实源）；<br>同步第二阶段验收（真实 PG 已执行）、第四阶段遗留（DATA-1~3 升为断言）、<br>第五阶段（进程密钥/凭证面拆分）与第六阶段（Project/Episode 状态入口）实际进度；<br>删除已失效的「本地无 PG」风险记录（P2-04） |
