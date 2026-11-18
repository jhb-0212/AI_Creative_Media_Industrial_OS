# 全仓库工程基线与根因核验报告（2026-11-18）

> 阶段：整改 Phase 0 —— 全仓库重新核验（**只核验，不改业务代码**）
> 基线：`docs/baseline/AI影视工业化生产平台V2.0.5.md`（AFS-ARCH-MASTER-V2.0.5，V205-R14，489607 字节，SHA-256 `9a9940c5…`）
> 代码基线：`origin/main` @ `45fa58b`（PR #15 合并后）
> 定位：本报告是后续 ADR 与架构整改的**唯一输入依据**。凡本文未列为问题的，不得以「顺手优化」名义改动；凡本文列为问题的，整改前必须补齐 ADR。
> 核验立场：**不接受任何文档、注释、契约 `status` 的自我声明**。每条结论都由读代码、查运行库、跑闸门、跑测试之一独立证实。

---

## 0. 结论摘要

### 0.1 一句话判断

**契约层与安全边界层已达到可交付水准；运行时层与事实持久化层仍是骨架。**

17 条安全不变量全部有可执行断言（debt 0 / partial 0），三道闸门 + 静态检查 + 743 项测试全绿——这些是真实的。但同一份仓库里：

- 40 个 ACTIVE 动作只有 15 个有运行时入口（37.5%）；
- 14 个服务域中 10 个是空目录（0 个 `.py`）；
- 12 张真实业务表对应 83 个契约对象；
- **存在两份不同的「基线正文」**，README 指向的那份未纳入版本控制；
- **审批与状态迁移没有第一类事实表**，唯一载体是会按保留期归档的 outbox。

这不是「实现质量差」，而是**契约先行、运行时滞后、事实源尚未收口**的阶段特征。Phase 1 不可宣布验收。

### 0.2 闸门实测结果（本次亲自执行，非引用）

| 闸门 | 结果 | 说明 |
| --- | --- | --- |
| `freeze_manifest.py --check` | **PASS** | 基线 V205-R14 未变；11 条修订；Action 84 = 75+9，Schema 83 = 76+7，状态机 43 = 35+8 |
| `contract_check.py` | **PASS** | 21 YAML + 1 JSON；17 条不变量「断言验证 17 / partial 0 / debt 0」 |
| `architecture_check.py` | **PASS** | R1 方向 0 / R2 跨服务 0（平台能力 1 项）/ R3 循环 0 / R4 进程边界 0 / R5 内核持久化 0 |
| `ruff check .` | **PASS** | All checks passed |
| `mypy packages services apps workers` | **PASS** | 103 源文件无问题 |
| `pytest tests`（真实 PostgreSQL 16） | **PASS** | 743 项，0 failed / 0 error / 0 skipped |
| `alembic check` | **PASS** | 「No new upgrade operations detected」 |
| 迁移 head | — | `c7d8e9f0a1b2`，31 个迁移版本 |

> ⚠️ `freeze_manifest.py --check` 与 `contract_check.py` 必须用项目虚拟环境执行。系统 Python 缺 `pydantic_settings` / `cryptography`，会在中途 `ModuleNotFoundError` 退出——**且退出码可能被 `tail` 掩盖成「看起来跑完了」**。建议 CI 之外不要用裸 `python` 跑闸门。

### 0.3 规模事实

| 维度 | 数量 |
| --- | --- |
| Python 文件 / 代码行 | 217 / 34,695 |
| 真实业务表（不含 `alembic_version`） | 12 |
| 契约动作（Action） | 84（ACTIVE 40 / PHASE_GATED 44） |
| 契约对象 Schema | 83 |
| 契约状态机 | 43 对象 / 258 状态 / 187 转移 |
| 契约事件 | 13（发出点 13，缺口 0） |
| 安全不变量 / 可执行断言 | 17 / 17 |
| 运行时 HTTP 端点 | 15 |
| 运行时已实现状态机 | 4（Project / Episode / ScriptRevision / AssetVersion） |
| 测试 | 743 |

---

## 1. 模块依赖图（实测）

### 1.1 分层与依赖方向

六层，规则由 `scripts/architecture_check.py::LAYER_RULES` 强制（R1）：

```
apps          薄装配（协议适配）──┐
                                  ├─→ services（领域内核）
workers       独立进程 ──────────┤     ├─→ packages（横切库）
agents        Skills 执行 ───────┤     └─→ adapters（外部适配）
adapters      外部实现 ──────────┘           └─→ packages
```

禁止方向（R1 强制，0 违规）：`packages` ↔ 任何层；`adapters` → services/apps/workers/agents；`agents` → services/workers/adapters/apps；`services` → apps/workers/agents；`workers` → apps/agents；`apps` → workers/agents/adapters。

服务间直连（R2）唯一例外：`services.identity_access`（鉴权平台能力），白名单条目带失效条件，陈旧登记会报红。

### 1.2 域实现密度：这是本轮最结构性的一个事实

| 服务域 | 文件 | 行数 | 状态 |
| --- | --- | --- | --- |
| `project_planning` | 29 | 2,612 | 已实现（Project/Episode/ScriptRevision） |
| `asset_service` | 17 | 1,719 | 已实现（AssetVersion/QCReport） |
| `identity_access` | 5 | 670 | 已实现（认证/RBAC/归属依赖） |
| `identity_issuance` | 1 | 108 | 已实现（签发 + JWKS） |
| `orchestrator` | 0 | 0 | **空壳** |
| `qc_governance` | 0 | 0 | **空壳** |
| `rights_governance` | 0 | 0 | **空壳** |
| `model_gateway` | 0 | 0 | **空壳** |
| `budget_quota` | 0 | 0 | **空壳** |
| `production_policy` | 0 | 0 | **空壳** |
| `blocking_service` | 0 | 0 | **空壳** |
| `creative_exploration` | 0 | 0 | **空壳** |
| `memory_learning` | 0 | 0 | **空壳** |
| `observability` | 0 | 0 | **空壳** |

- `adapters/*`：7 个全空（audio/image/video_models、comfyui、external_tool、storage、blocking）。
- `agents/*`：3 个全空（planners / evaluators / runtime）。
- `workers/*`：有实现的是 `relay`(6 文件/675 行)、`outbox_maintenance`(3/567)、`identity`(3/117)；`generation` / `delivery` / `notifications` / `qc` 全空。

**结论**：架构闸门的 R1–R5 全绿，是因为空目录不产生依赖——**闸门证明的是「已写的没写错」，不能证明「该写的写了」**。后者的证据在 `contracts/runtime/action_implementations.yaml`（见 §5），两份证据必须同时看。

### 1.3 重复实现 / 废弃机制核查

| 检查项 | 结论 |
| --- | --- |
| 状态转移表是否有第二份 | 无。`services/project_planning/domain/state_machine.py` 与 `services/asset_service/domain/state_machine.py` 各管自己的域，`contract_check` 反射比对 YAML。 |
| 授权判定是否散落在路由 | 无 `AUTH_SCOPE_DENIED` 构造，无 `organization_id` 比较（`project_routes.py` 里的 `Organization` 是**判定对象声明**，不是比较）。 |
| Service 是否 import 持久化库 | 无（R5 + 实测 grep 为空）。异常契约统一在 `packages/shared/port_errors.py`。 |
| 是否有 `Manager` / `Factory` / `Policy` 平行层 | 无。`SqlOwnershipResolver` 是实现类，不是新层。 |
| 是否有 `TODO` / `FIXME` / 占位返回 | 仅有 Protocol 的 `raise NotImplementedError`（合法）与 relay 的一处 `contextlib.suppress`。**无空壳 Service、无「只返回成功」的接口**。 |

---

## 2. 数据所有权矩阵（以运行库为准）

以下全部由 `information_schema` 与 `pg_class` 实测得出，**不采信迁移文本**。

### 2.1 角色与表

| 表 | owner | RLS | 可写角色 | 权限明细 |
| --- | --- | --- | --- | --- |
| `project_planning.projects` | postgres | ✅ | project_planning_role | SELECT/INSERT/UPDATE/**DELETE** |
| `project_planning.organizations` | postgres | ✅ | project_planning_role | SELECT/INSERT/UPDATE/**DELETE** |
| `project_planning.episodes` | postgres | ✅ | project_planning_role | SELECT/INSERT/UPDATE/DELETE |
| `project_planning.script_revisions` | postgres | ✅ | project_planning_role | SELECT/INSERT/UPDATE/DELETE |
| `asset_service.asset_versions` | postgres | ✅ | asset_service_role | SELECT/**INSERT/UPDATE**（DELETE 已收回） |
| `asset_service.qc_reports` | postgres | ✅ | asset_service_role | SELECT/INSERT（UPDATE/DELETE 已收回，append-only） |
| `outbox.outbox_events`（+3 分区） | postgres | ✅ | 域角色 INSERT+SELECT；relay **列级** UPDATE | relay 仅 `attempts` / `next_attempt_at` / `published_at` / `dead_lettered_at` |
| `auth.revoked_tokens` | postgres | ❌ | 仅 postgres（运维 CLI） | 域角色仅 SELECT |
| `outbox.maintenance_runs` | postgres | ❌ | 仅 postgres | — |

跨域读：`asset_service_role` 对 `project_planning.projects` **只有 3 列**（`entity_id` / `tenant_id` / `organization_id`），与 `_RESOLVE_SQL` 的取数需求逐列对齐。
反向：`project_planning_role` 对 `asset_service.*` **零权限**（实测空集）。

### 2.2 RLS 的真实力度（关键）

```
策略：tenant_isolation_*    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
覆盖：projects / organizations / episodes / script_revisions / asset_versions / qc_reports / outbox_*
未覆盖：auth.revoked_tokens、outbox.maintenance_runs（均非租户数据）
```

- `current_setting(..., true)` 未设置时返回 NULL → `tenant_id = NULL` 为 false → **fail-closed**（查不到任何行），行为正确。
- 上下文用 `set_config(key, value, true)`（`is_local = true`）→ 事务级，随 commit/rollback 自动清除，**不存在连接池跨请求泄漏**。
- `relrowsecurity = true` 但 **`relforcerowsecurity = false`**，且表 owner 是 `postgres`。当前所有应用连接都用域角色 → RLS 生效；但**只要有人把 DSN 配成 `postgres`，RLS 会静默全部失效**，且没有任何检查会报错。

**实测组织隔离力度**（这是本轮新增的量化证据）：

```
以 project_planning_role 连接，仅设置 app.current_tenant_id（单一租户）
  → 可见 51 个 organization、1584 个 project
```

即：**数据库层只隔离租户，完全不隔离组织与项目**。跨组织隔离 100% 依赖应用层调用 `authorize` + `OwnershipPath` 逐级判定。

这在「授权入口已收口到一处」的前提下是可控的，但它与项目自身原则 R2「不变量放最可靠层」存在张力：一旦某个新查询忘了过授权入口，数据库不会拦。见 §7 N-03。

### 2.3 归属链的真实约束

- `projects.organization_id` → `organizations(organization_id, tenant_id)` 复合外键（SCOPE-1）。
- `episodes.project_id` → `projects(entity_id, tenant_id)` 复合外键（SCOPE-2）。
- `script_revisions (project_id, episode_ref)` → `episodes` 复合外键。
- `asset_versions.project_id` NOT NULL + 复合外键 → `projects(entity_id, tenant_id)`（SCOPE-3）。
- 组织归属**不冗余落库**（只有 `projects.organization_id` 一处），资产的组织沿 `asset → project → organization` 派生。这是正确的单事实源选择。

`alembic check` 报「无漂移」，但注意它的语义边界：**只比对 ORM 已声明的表（12 张）**。83 个契约对象中 71 个没有 ORM、没有表，`alembic check` 对它们无话可说。

---

## 3. 身份与授权链路

### 3.1 进程凭证面（容器 env 实测）

| 进程 | 持有的数据库凭证 | 持有的身份密钥 |
| --- | --- | --- |
| `api` | `POSTGRES_PROJECT_PLANNING_*`、`POSTGRES_ASSET_SERVICE_*` | **仅** `IDENTITY_PUBLIC_KEY_PEM` |
| `identity` | **零**（无任何 `POSTGRES_*`） | `IDENTITY_PRIVATE_KEY_PEM` + `IDENTITY_PUBLIC_KEY_PEM` |
| `relay` | `POSTGRES_RELAY_*`（`outbox_relay_role`） | 零 |
| `outbox_maintenance` | `POSTGRES_*`（超级用户，DDL 例外，已登记） | 零 |

compose 侧以四份最小 anchor（`db-endpoint` / `db-superuser` / `db-business` / `db-relay`）表达，`PROCESS_ALLOWED_DB_ENV` 白名单 + 断言 `cred.process_credential_surface` 兜底。**旧的全量 `x-db-env` anchor 已彻底删除**。

### 3.2 授权链

```
Bearer JWT
  → build_principal_dependency（验签 + iss/aud + jti 吊销）
  → Principal(subject_id, tenant_id, roles, capabilities, organizations?)
  → authorize(action_key, principal, ResourceRef, OwnershipPath)
       ├─ Action 解析（action_registry）
       ├─ RBAC（allowed_principals，席位表达式 `/`=OR、`+`=AND）
       ├─ Capability
       ├─ 归属范围（OwnershipPath 逐级覆盖，fail-closed）
       └─ 职责分离（require_different_subject）
```

**四种进入方式（这是一个结构问题，见 §7 N-11）**：

| 方式 | 位置 | 用于 |
| --- | --- | --- |
| `require_role(action)` | 依赖 | 无受控资源的动作 |
| `require_resource_role(action, resource_type, param_name)` | 依赖 | 路径参数即资源 id（读/状态变更） |
| `authorize_on_parent(action, parent_type, parent_id)` | 函数 | 创建子资源（Project/AssetVersion 为父） |
| 路由内 `resolver.resolve()` + `authorize()` | 内联 | `PROJECT.CREATE`（父级 Organization 可为空） |

### 3.3 归属解析（`packages/identity/ownership.py`）

六类资源各一条显式 SQL：`Organization` / `Project` / `Episode` / `ScriptRevision` / `AssetVersion` / `QCReport`。全部 JOIN 到 `projects` 取 `tenant_id + organization_id`，不拼接、不猜表名。

响应语义（已定死，不逐处决定）：
- 跨租户 / 资源不存在 → **404**（RLS 下本就不可见，用 403/404 差异探测他人资源属信息泄漏）；
- 同租户跨组织 → **403 `AUTH_SCOPE_DENIED`**（可见但无权，404 会掩盖真实原因）。

### 3.4 `kid` 与密钥轮换

`kid` = 公钥 RFC 7638 JWK Thumbprint（`{e,kty,n}` 规范序列化 → SHA-256 → base64url 无填充），`SigningKey.kid` 为 `cached_property`，四个密钥入口**已无 `kid` 形参**（AST 断言 `keys.kid_content_addressed` 强制）。运行态三处（identity 签发、API 验签、JWKS）kid 一致。

---

## 4. 业务状态机总表

### 4.1 运行时已实现（4 个）

| 对象 | 状态 | 转移 | 边带守卫 | 边有唯一 owner | 终态 |
| --- | --- | --- | --- | --- | --- |
| Project | DRAFT/ACTIVE/ON_HOLD/COMPLETED/ARCHIVED | 7 | ❌ | ❌（纯邻接集） | ARCHIVED |
| Episode | DRAFT/ACTIVE/ARCHIVED | 3 | ❌ | ❌ | ARCHIVED |
| ScriptRevision | DRAFT/LOCK_PENDING/LOCKED/SUPERSEDED | 5 | ❌（但服务层强制职责分离） | ❌ | SUPERSEDED |
| **AssetVersion** | GENERATED/TECHNICAL_QC/HUMAN_REVIEW/APPROVED/GOLD | 4 | ✅ | ✅ | （GOLD 为终态） |

`AssetVersion` 是唯一按 AUTH-4 目标形态落地的对象：`Transition(source, target, owner_action, guards)`，`change_status` 取边后校验 owner，**APPROVED 那条边的 owner 是 `ASSET.VERSION.APPROVE`，通用接口结构上不可达**——不是靠 `if target == APPROVED` 拦住。

`Project/Episode/ScriptRevision` 仍只有结构（邻接集），所有者未登记。当前**无实际绕过风险**（这三个对象的状态集里没有"需审批"的状态，Script 的锁定走独立动作 `SCRIPT.LOCK.APPROVE` 且强制职责分离，无通用状态接口可绕）。但这是「同一问题换对象复发」的现成土壤。

### 4.2 契约已定义、运行时未实现（39 个对象）

契约 43 个状态机对象中，39 个只有 YAML/no runtime。它们不是缺陷（PHASE_GATED 或未排期），但意味着 **258 个状态、187 条转移里只有 19 条转移被代码执行过**。

### 4.3 契约缺口（V2.0.5 正文本身未定义）

| 台账 | 数量 | 含义 |
| --- | --- | --- |
| `status_field_taxonomy.yaml` → `lifecycle_gap` | **19** | 应有生命周期但正文没登记状态机 |
| 同上 → `envelope_only`（含 `status_split`） | 17（其中 `status_split` 3 个） | 无独立生命周期，status 仅占信封位 |
| `resource_schema_gaps.yaml` → `gaps` | 7 | ACTIVE 动作引用的资源类型无 schema |
| 同上 → `schema_gaps` | 4 | 有状态机但对象无 schema |

合计 **30 项契约/基线缺口**。**这 30 项无法用代码消除**——它们的 `unblocks_when` 全部指向「正文补章节」。见 §7 N-05。

---

## 5. 契约 ↔ 运行时映射

| 契约动作 | 数量 | 运行时 |
| --- | --- | --- |
| 全部 | 84 | — |
| ACTIVE | 40 | **15 已实现 / 25 登记未实现** |
| PHASE_GATED | 44 | 不要求实现 |

15 个已实现动作与 15 个 HTTP 端点**一一对应**（`contract_check` 双向校验：台账里的 route 必须真实存在于 `app.openapi()`，运行时不允许多出未登记端点）。**没有「有端点没授权」或「有授权没端点」的悬空项。**

25 个未实现动作按域分布：

| 域 | 数量 | 动作 |
| --- | --- | --- |
| 镜头 | 6 | SHOT.DRAFT.CREATE、SHOT.REVISION.CONFIRM、SHOTSPEC.LOCK、CONSTRAINT.PACK.CREATE/LOCK、PRODUCTION.ROUTE.SELECT |
| 交付 | 4 | DELIVERY.SPEC.APPROVE、DELIVERY.MANIFEST.CREATE/APPROVE/RELEASE |
| 展示 | 5 | SHOWCASE.SNAPSHOT.CREATE/REVOKE、SHOWCASE.SHARE.CREATE/REVOKE、COMMUNITY.REUSE.REQUEST |
| 生成 / Gate | 2 | GENERATION.REQUEST、PRODUCTION.GATE.APPROVE |
| 预算 | 2 | BUDGET.SHOT.REQUEST、BUDGET.SHOT.APPROVE |
| 反推 | 2 | REVERSE.ANALYSIS.CREATE/CONFIRM |
| 客户评审 | 2 | CLIENT.REVIEW.CREATE/ANNOTATE |
| Canon / 权利 | 2 | CANON.CHANGE.APPROVE、RIGHTS.STATUS.CHANGE |

**Phase 1 主链 7 环（计划 §六）全部落在这个未实现集合里**：SHOT.DRAFT.CREATE → SHOT.REVISION.CONFIRM → GENERATION.REQUEST → PRODUCTION.GATE.APPROVE → DELIVERY.MANIFEST.CREATE/APPROVE/RELEASE。

端到端主链证据（`tests/e2e/test_phase1_main_chain.py`，3 例）真实覆盖了 **Project → Episode → Script → 锁定申请 → 锁定批准 → AssetVersion → QC → 批准** 这条链，含「剧本锁定不能自批」「通用状态接口不能到 APPROVED」两个负向断言。**它止于资产批准，没有镜头、生成、Gate、交付。**

---

## 6. 历史问题销项表（独立复核，不采信文档自述）

| 编号 | 原问题 | 复核结论 | 独立证据（本次亲自验证） |
| --- | --- | --- | --- |
| P1-01 | Identity 继承数据库凭证 | **已根治** | 容器 env 实测：identity 无任何 `POSTGRES_*`；全量 anchor 已删除，改为四份最小 anchor；断言 `cred.process_credential_surface` |
| P1-02 | 资产状态接口绕过审批 | **已根治** | `ASSET_TRANSITIONS` 边带 owner + guards；`change_status` 走 `edge_owned_by(..., STATUS_CHANGE_ACTION)`，APPROVED 边 owner 为 APPROVE → 结构不可达；e2e 负向用例 |
| P1-03 / P1-06 | Episode 读写跨组织越权 | **已根治** | `get_episode` / `change_episode_status` 均用 `require_resource_role`；`create_episode` 用 `authorize_on_parent`；归属解析按 `Episode` → JOIN projects |
| P1-04 | 资产无组织/项目归属 | **已根治** | `project_id` NOT NULL + 复合外键；`_RESOLVE_SQL["AssetVersion"]` JOIN projects；跨项目复用幂等键被 409 拒绝 |
| P1-05 | 契约闸门自证（meta 根因） | **已根治** | `status: satisfied` 手写即报红（`contract_check.py:2519`）；17 条不变量全部改由 `boundary_assertions.py` 断言执行；`runtime_evidence` 校验 pytest 节点存在 |
| P2-01 | 端口硬编码 8100 | **已根治** | compose 中容器端口、宿主映射、健康检查三处同源于 `${IDENTITY_PORT}` |
| P2-02 | 域角色名可重名 | **已根治** | `Settings._require_distinct_database_roles` 构造期拒绝 |
| P2-03 | Service 依赖 SQLAlchemy 异常 | **已根治** | `port_errors.py` + R5 架构规则；实测 `services/*/{service,domain}` 零 `sqlalchemy` import |
| P2-04 | 提案状态与实际不符 | **已根治** | `check_proposal_status()` 闸门；4 份提案（implemented 1 / in_progress 3） |

**结论：历史 10 项 P1/P2 全部真实关闭，不是「改文档销项」。** 第三轮审查指认的四大根基（契约事实源 / 数据模型 / 身份治理 / 事务资源生命周期）中，前两项与第四项已实质落地；第二项的身份治理在**机制**上完整，在**审批事实上**仍有缺口（见 N-02）。

---

## 7. 新发现问题清单

> 编号规则：`N-xx`。严重度：**P1** = 违反工程铁律或造成事实源分裂，须在 Phase 1 前解决；**P2** = 结构性债，有明确触发条件；**P3** = 一致性/可维护性。
> 每条给出：位置 → 当前实现 → 违反铁律 → 根因 → 影响 → 目标设计 → 需删除的旧机制 → 验收标准。

---

### N-01（P1）基线正文存在两份，README 指向未纳管的那一份

**位置**
- `AI影视工业化生产平台V2.0.5.md`（仓库根，**untracked**，5107 行，CRLF，SHA-256 `d71d71e8…`）
- `docs/baseline/AI影视工业化生产平台V2.0.5.md`（tracked，5095 行，LF，SHA-256 `9a9940c5…`，受 `freeze_manifest` 校验）
- `README.md:5` 指向根目录那份；`README.md:55` 指向 `docs/baseline/` 那份；`docs/architecture/repository-structure.md:5` 指向根目录文件名。

**当前实现**：根目录副本已被人工编辑，与冻结基线实质不同：
- 架构树新增 `services/shot-planning/`、`services/generation-execution/`；
- 数据所有权表被重写，拆出 `Shot Planning` 行；
- 31.6 正文从「当前共注册 75 项」改为「**83 项**」，并手工追加 8 行 action；
- 引用了 `ADR-R27 §3.5`——**该 ADR 不存在**（`docs/adr/` 只到 R21）。

而真实契约注册表是 **84** 个动作，冻结基线是 75 + 9 修订 = 84。于是同一件事有三个数字：**75 / 83 / 84**。

**违反铁律**：铁律 1「架构无屎山」（事实源分裂）、铁律 3「禁止补丁式演进」（改正文绕过修订登记）、R1「一个事实一个权威源」、R3「状态可追溯」。

**根因**：基线正文的**写权限没有收口**。`freeze_manifest` 只校验 `docs/baseline/` 的哈希，而 README 把另一个路径称为「配套基线文档」。只要有人对着根目录那份改正文，CI 完全看不见——这与 P1-05「闸门只能证明有人写了 satisfied」是同一形态的第二次出现：**校验的对象不是人实际读的对象**。

**影响**
- 新人（或 Agent）读 README 会读到一份**不是契约来源**的正文，据此实现的字段/动作会与 `action_registry` 冲突；
- 悬空引用 `ADR-R27` 会让「作用域拆分」这条设计决策失去可追溯的上游；
- 83 与 84 的差值意味着至少一个已登记动作没有出现在正文里——正文与契约已经分叉。

**目标设计**
1. 基线正文只保留一个权威路径：`docs/baseline/`。根目录不再保留同名副本。
2. `freeze_manifest.py` 增加一条断言：仓库内**不得存在与基线同名但游离的 `.md`**（按文件名匹配，不只看固定路径）；发现即报红并给出「要么纳入基线、要么删除」。
3. 正文修订必须走既有流程：`contract_amendments.yaml` 登记 → 改派生文件 → `freeze_manifest --write` 重签。
4. `ADR-R27` 要么补写（若确有其决策），要么删除引用（若属笔误）。**不允许悬空引用留在基线正文里。**

**需删除的旧机制**：根目录 `AI影视工业化生产平台V2.0.5.md`；`README.md:5` 与 `repository-structure.md:5` 的根目录路径指称。

**验收标准**
- `freeze_manifest --check` 对「游离同名正文」报红（负向测试：把基线复制一份到根目录 → 必须红）；
- `grep -rn "ADR-R2[2-9]"` 结果全部命中真实存在的 ADR 文件（纳入闸门）；
- README / repository-structure 中出现的基线路径唯一且存在。

---

### N-02（P1）审批与状态迁移没有第一类事实表，唯一载体是会归档的 outbox

**位置**
- `packages/shared/entity.py:99-103`：`_advance_version(audit_ref)` **单字段赋值** `self.audit_ref = audit_ref`
- `services/asset_service/service/asset_service.py:457`：`audit_ref=f"{edge.owner_action}:{context.actor}"`
- `docs/adr/ADR-R06-2-outbox-retention-partitioning.md`：outbox 按月分区，到期 `DETACH`（`--drop-detached` 才真删，默认改名归档）
- `contracts/security_boundary.yaml` AUTH-3/AUTH-5 `residual_risk`：已承认「审批事实仍无独立对象」

**当前实现**：一次状态迁移会在三处留痕——实体上的 `audit_ref`（**被下一次迁移覆盖**）、outbox 事件（**会随保留期归档**）、无第三处。数据库 12 张表中**没有** `approvals`、`status_transitions`、`audit_log`。

**违反铁律**：红线「审批与执行分离」、红线「质量不可暗降」；R7「状态可追溯」；铁律 4「高技术标准」。

**根因**：审批被建模成**一次 HTTP 请求的副作用**，而不是**一条事实**。因为「谁签了哪个席位」没有契约对象（ADR-R17/R18 已明确"刻意不另造审批子对象，避免第三份事实源"），所以只能塞进事件流。这个取舍在单席位审批下勉强成立，但它把「可追溯性」押在一个**有保留期的主表**上。

**影响**
- 保留期一到，`outbox_events_y2026xx` 被 DETACH 成 `outbox_events_archived_*`，审批历史退出可查询范围；要做审计只能去翻冷备 dump。
- 「为什么要改成这个状态」只能从事件 payload 反推，无法按 `(resource, transition)` 索引查询。
- 累积式会签（AUTH-5）**结构上无法实现**——需要「哪个席位、谁签的、何时签的」的持久事实，而当前没有落点。这不是排期问题，是模型缺位。
- 唯一的实体侧字段 `audit_ref` 每次迁移被覆盖，等于「最后一跳的凭据覆盖了全部历史」。

**目标设计**（不改红线语义，只补事实）
1. 新增**唯一**的审批/迁移事实表（一个，不是两个）：`*_status_transitions`（或 `approvals`），列至少 `(tenant_id, resource_type, resource_id, from_status, to_status, owner_action, actor_subject, evidence_ref, occurred_at, event_id)`。
2. 与状态变更**同事务**写入（沿用 R07），并作为 outbox 事件的 `source_ref`——事件不再承担「唯一历史」职责，只承担「通知下游」职责。
3. 该表 append-only：收回 UPDATE/DELETE/**TRUNCATE**（对齐 `qc_reports` 的既有做法；后者实测只装了行级守卫，`TRUNCATE` 仍可清空，同批补齐）。
4. 明确它与 `audit_ref` 的关系：`audit_ref` 降级为「指向最新一条迁移记录的指针」，不再是自由字符串；迁移前的自由串按 `legacy` 处理，不参与指针解析（否则升级即误报，见 ADR-R23 D2-c）。
5. ~~会签在此表上表达为「同一 `(resource, from, to)` 下多条席位签署记录，凑齐后放行」——AUTH-5 的 `unblocks_when` 因此可以关闭。~~
   **此条已撤回（2026-11-18 写 ADR 时发现不成立）**：本表记录「推进已经发生」，会签是「推进之前某席位签了字」，是另一类事实（可驳回/可超时/可凑不齐）。塞进本表只能靠 no-op 行或 `kind` 判别列。AUTH-5 改由 `audit.approval_signatures`（D2b 单独 ADR）解锁。

**需删除的旧机制**：把 outbox 当审计主表的隐含假设；`audit_ref` 作为自由字符串承载审批语义。

**验收标准**
- 负向测试：保留期归档后，`GET` 某资源的迁移历史仍可完整返回（证明历史不依赖 outbox）；
- 负向测试：`UPDATE`/`DELETE` 迁移事实表被数据库拒绝；
- 并发测试：两个主体同时批同一资源 → 一条成功、一条 `CONCURRENT_MODIFICATION`，事实表只多一条；
- 契约：新增该对象的 schema + 状态机（若需要），走 amendments 登记。

---

### N-03（P1）组织/项目隔离只在应用层，数据库只隔离租户；且 RLS 未 FORCE

**位置**
- `data/migrations/versions/8f2a1b3c4d5e_add_tenant_id_and_rls.py` 等：全部 RLS 策略形如 `tenant_id = current_setting('app.current_tenant_id')`
- 运行库实测：`relrowsecurity = true`，`relforcerowsecurity = false`，owner = `postgres`

**当前实现**：见 §2.2 实测——同租户内 `project_planning_role` 可读 51 个组织、1584 个项目。组织与项目边界 100% 由 `authorize` 判定。

**违反铁律**：铁律 4「高技术标准」（纵深防御不足）；与项目自身 R2「不变量放最可靠层」的取向不一致；计划 §4.1「数据库 RLS 继续作为数据库访问边界」。

**根因**：RLS 上下文只有一个维度（`tenant_id`）。`Principal` 里已有组织信息，但没有把它注入数据库会话，于是数据库没有判定组织所需的输入——**不是漏写策略，是策略缺输入**。这与 R-A「授权决策输入不完整」是同一根因在数据库层的投影。

**影响**
- 任何新增查询只要绕过 `authorize`（新端点忘记挂依赖、内部 Worker 直查、脚本直连），组织边界即刻失效，且**数据库不会拒绝**；
- `FORCE ROW LEVEL SECURITY` 未启用：若 DSN 误配为 `postgres`，**全部 RLS 静默失效**，无任何启动检查会报错（`Settings` 不校验 `postgres_user != "postgres"`）。

**目标设计**
1. RLS 增加组织维度：注入 `app.current_organization_ids`（数组）或 `app.current_organization_id`，策略改为 `tenant_id = ... AND (organization_id IS NULL OR organization_id = ANY(...))`。归属链上 `organization_id` 已经存在（`projects.organizations` 一处），不需要新列。
2. 对业务表启用 `FORCE ROW LEVEL SECURITY`，使 owner 也受策略约束（迁移角色需在 DDL 事务内显式 `SET LOCAL row_security = off`，或迁移改用独立 owner）。
3. `Settings.validate_process_requirements` 增加一条：生产环境业务进程的 `postgres_user` **不得等于表 owner**（`postgres`）；启动期拒绝，而不是运行期静默。
4. 组织维度的策略必须与 `authorize` 的判定**同源**：`OwnershipPath` 与 RLS 谓词由同一份定义派生（避免出现"应用放行、库拒绝"或反之的两套规则）。

**需删除的旧机制**：`tenant_id` 单维 RLS 策略（保留租户维度，但不再是唯一维度）。

**验收标准**
- 负向测试：以 `project_planning_role` + 单租户上下文，**不带组织条件**查询 `projects` → 只能看到该主体所属组织的行（实测当前是 1584 行，整改后应等于该组织行数）；
- 负向测试：`postgres_user = postgres` 且 `APP_ENV=production` → 启动失败；
- 正向回归：现有 743 项测试不因 RLS 收紧而失败（测试装配需显式注入组织上下文，这本身也是一次「测试是否真按生产路径跑」的检验）。

---

### N-04（P1）25/40 ACTIVE 动作无运行时，Phase 1 主链 7 环缺失

**位置**：`contracts/runtime/action_implementations.yaml#not_implemented`（25 项，逐条带 reason）；10 个空服务目录。

**当前实现**：见 §1.2 与 §5。台账**诚实登记**了每一项，闸门双向校验、不会静默过期——这一点做对了。问题不在"隐藏"，而在**规模**。

**违反铁律**：铁律 5「全程 AI 辅助开发与生产」的验收侧（"正式 Action 均有真实实现或明确冻结状态"）；计划 §六「禁止只在 Action Registry 中增加名称，却没有完整运行时实现」。

**根因**：契约与运行时是**两条独立推进的线**。契约侧有 43 个状态机、83 个对象、84 个动作，全部机器可校验；运行时侧只有 15 个入口。两者之间没有「按主链顺序推进」的机制——`not_implemented` 台账记录缺口，但不排序、不阻塞。

**影响**
- Phase 1 无法验收（主链 7 环全缺）；
- 25 项 reason 里有多项写着"先开入口再写守卫，顺序不能反"——说明**依赖顺序已经识别清楚，但没有被结构化为执行队列**；
- 空目录（10 services + 7 adapters + 3 agents）会让 `architecture_check` 持续全绿，形成"架构已就绪"的错觉。

**目标设计**
1. 把 `not_implemented` 从「平铺清单」升级为**依赖有序的主链队列**：每条声明 `blocked_by`（其他 action / 契约缺口 / 决策点），闸门校验 `blocked_by` 指向的项确实未完成（防陈旧阻塞）。
2. Phase 1 主链 7 环按链序实现，每环绕「Action + Schema + 状态机 + 权限 + 输入输出 + 归属 + 审批 + 失败处理 + 幂等 + 审计 + 测试」十项齐备才算完成（计划 §六）。
3. 空服务目录在实现前不保留 `.gitkeep` 之外的诱惑：要么在 `docs/architecture/repository-structure.md` 标注 `NOT_ENABLED`，要么删除目录、按需创建。**空目录不是架构，是占位符。**

**需删除的旧机制**：无（这是补齐，不是替换）。

**验收标准**
- `action_implementations` 中 Phase 1 七环全部从 `not_implemented` 移入 `implemented`，且 route 真实存在（闸门已能校验）；
- 每环至少 1 条正向 + 1 条负向 e2e，含一次跨组织拒绝；
- 端到端从 Project 一路跑到 `DELIVERY.MANIFEST.RELEASE`。

---

### N-05（P2）30 项契约缺口属基线未定义，不能用代码消除，且无整改归属

**位置**：`contracts/enums/status_field_taxonomy.yaml`（19 `lifecycle_gap`）、`contracts/runtime/resource_schema_gaps.yaml`（7 + 4）。

**当前实现**：每项都有 `reason` / `contract_ref` / `unblocks_when`，且**`unblocks_when` 全部指向「正文补章节」**。

**违反铁律**：无直接违反——台账机制是正确的。但计划 §5.2 把「19 个 lifecycle_gap / 18 个 envelope_only / 4 个双 status」列为待处理项，**没有区分"实现欠账"与"基线欠账"**。

**根因**：V2.0.5 正文对 30 个对象的生命周期/字段定义不完整。这不是实现者的疏漏，是基线的边界。计划又要求「V2.0.5 保持最高基线、不擅自修改」——于是这 30 项在「不改基线」和「不留缺口」之间**没有出路**。

**影响**
- 若按「缺口清零」推进，唯一出路是编造状态集（台账注释已明确拒绝：「宁可留一个带债务登记的裸字段」）；
- 若不处理，`lifecycle_gap` 会长期停在 19，成为"永远不动的数字"，最终腐化成背景噪声。

**目标设计**
1. 把这 30 项从「待办」重新归类为**基线修订候选**，由项目 owner 按批次裁定：补正文（→ 差额消失）/ 判为 `envelope_only`（→ 改分类）/ 声明为 Phase 2+（→ 打上阶段标记）。
2. 每批次形成一份 amendments 记录 + ADR，**不逐项零散处理**。
3. 台账增加 `decision_status: pending_owner | accepted_baseline_change | deferred_phaseN`，使"未裁定"本身可查询、可计数，而不是只靠 reason 散文。

**需删除的旧机制**：无。

**验收标准**：30 项全部具备 `decision_status`，`pending_owner` 数量随批次单调下降；任何一项从 `pending_owner` 变为其他状态都伴随一条 amendments 记录。

---

### N-06（P2）`project_planning_role` 持有契约里不存在的权限（含 organizations 的 DELETE/INSERT）

**位置**：运行库实测（§2.1）。

**当前实现**

```
project_planning.projects       : SELECT/INSERT/UPDATE/DELETE
project_planning.organizations  : SELECT/INSERT/UPDATE/DELETE
project_planning.episodes       : SELECT/INSERT/UPDATE/DELETE
project_planning.script_revisions: SELECT/INSERT/UPDATE/DELETE
```

而契约里**没有任何 `ORGANIZATION.*` 动作，也没有任何 `*.DELETE` 动作**（删除语义是 `ARCHIVED` / `SUPERSEDED` 状态，不是物理删除）。

**违反铁律**：红线「权限最小化」；DATA-1「模块级角色隔离」的实质（角色权限应等于该模块真实使用的权限集）。

**根因**：迁移用 `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA` 做**全量授予**，然后只对资产域的 `qc_reports` / `asset_versions` 做了**点对点收紧**（`8f3b2c1d4e5a:155-156`）。资产域做了，项目域没做——因为当时只修了被点名的两个对象。**这正是"不能因为增加了列级授权就判定边界闭合"的具体例证**：Outbox 修好了，业务 schema 的同类问题还在。

**影响**
- `organizations` 可被 INSERT/DELETE 却无授权入口、无审计事件 → 组织这条归属链的**根节点**可以不经任何业务动作被创建或删除；
- 物理 DELETE 与「终态不可原地复活 / 资产不可覆盖」的红线语义相悖：删掉一行再插回来，比"复活"更彻底地绕过状态机；
- 复合外键会拦住"删有子行的项目"，但删空组织/空项目不受任何阻拦。

**目标设计**
1. 逐表按**真实写入路径**授予，而不是按 schema 全量：`projects` / `episodes` / `script_revisions` 收回 `DELETE`（删除即 `ARCHIVED`）；`organizations` 在 `ORGANIZATION.CREATE` 动作落地前**只给 SELECT**。
2. 把「角色权限 ⊆ 该域已实现动作所需的最小权限集」做成断言（`data.role_grants_least_privilege`），由 `boundary_assertions.py` 执行——否则下一次新增表又会走全量授权。
3. 迁移里禁止 `GRANT ... ON ALL TABLES IN SCHEMA` 用于业务表（仅允许用于只读 schema）。

**需删除的旧机制**：`GRANT ALL TABLES IN SCHEMA` 的全量授予习惯；资产域之外的点对点补丁式收紧。

**验收标准**
- 负向测试：以 `project_planning_role` 执行 `DELETE FROM project_planning.projects` → 被拒（`insufficient_privilege`）；
- 负向测试：`INSERT INTO project_planning.organizations` → 被拒（在 ORG 动作落地前）；
- 断言 `data.role_grants_least_privilege` 对「多授一项」报红。

---

### N-07（P2）状态机形态不对称：只有 AssetVersion 有边守卫

**位置**：`services/asset_service/domain/state_machine.py`（`Transition` 带 owner+guards）vs `services/project_planning/domain/state_machine.py`（纯邻接集）。

**当前实现**：见 §4.1。

**违反铁律**：铁律 1「架构无屎山」（同一概念两套形态）；铁律 3（下一次同类问题的复发土壤）。

**根因**：AUTH-4 的整改范围按「已出问题的对象」圈定，而不是按「概念统一」圈定。`residual_risk` 已诚实写明"当前只有 AssetVersion 落地了边带守卫……后续按同一模式扩展"，但**没有触发条件**，等于把复发留给下一个人的记忆。

**影响**：一旦 Project/Episode/ScriptRevision 出现"需审批才能进入"的状态（Gate、发布、交付都会），`change_status` 就会重演 P1-02：通用接口可达审批态。这不是"可能"，是 V2.0.5 后续阶段必然要加的状态。

**目标设计**：三个域统一为 `Transition(source, target, owner_action, guards)`；无守卫的边显式写空 tuple，使「无条件准入」也是**声明**而不是"没写"。闸门加一条断言：**已实现状态机的所有转移边必须声明 owner_action**，缺失即红。

**需删除的旧机制**：`*_ALLOWED_TRANSITIONS` 邻接集作为权威（保留为 `ASSET_TRANSITIONS` 的投影用于 contract_check 比对）。

**验收标准**：断言 `lifecycle.edge_ownership` 覆盖全部 4 个已实现状态机；负向测试：给 Project 加一个无 owner 的边 → 闸门红。

---

### N-08（P2）CI 无安全扫描，交付链缺生产部署产物

**位置**：`.github/workflows/ci.yml`（4 个 job：lint-and-test / contract-check / permission-negative-tests / image-build）；`infra/k8s/` 只有 `.gitkeep`；`infra/monitoring/` 只有 1 个 `outbox-rules.yml`。

**当前实现**
- 静态检查有 ruff + mypy + 自研三道闸门；
- **无**依赖漏洞扫描（pip-audit / osv-scanner）、**无** SAST（bandit/semgrep）、**无** secret 扫描、**无** SBOM、**无** 镜像扫描；
- 无 k8s manifest、无 Helm chart、无部署 job；
- 监控只有 outbox 的 Prometheus 规则，无 dashboard、无告警路由、无 SLO 定义。

**违反铁律**：计划 §7.1「密钥扫描」、§7.5「生产验证」、红线「安全、法律、版权不可豁免」。

**根因**：CI 的设计目标是"证明契约与实现一致"，不是"证明可以上线"。两者是不同的验收面，前者的闸门已经做得很扎实，后者尚未开工。

**影响**：`pyproject`/`uv.lock` 里任何一个 CVE 都不会被 CI 发现；镜像里是否混入了开发依赖不会被检查；`.dev-keys/*.pem` 是否误提交只能靠 `.gitignore` 自觉。

**目标设计**
1. CI 增加 `security-scan` job：依赖漏洞 + secret 扫描 + 镜像扫描，**分级**（CRITICAL/HIGH 阻断，其余报告）。
2. 以 `infra/compose` 为唯一事实源，派生 k8s manifest（或声明"当前仅单机部署，k8s 属 Phase 3"并登记债务）。**空目录不能继续充当"已规划"。**
3. 监控：补齐 `/metrics` 到 dashboard 的链路，或以债务形式登记「Phase 1 仅有 outbox 指标」。

**需删除的旧机制**：无。

**验收标准**：新增一个已知有 CVE 的依赖 → `security-scan` 红；`infra/k8s` 要么有可 `kubectl apply --dry-run` 的 manifest，要么该目录不存在且有债务登记。

---

### N-09（P2）并发验证以模拟为主，缺真实双连接竞争与消费端幂等

**位置**：`tests/unit/test_optimistic_lock*.py`、`test_status_change_service.py`（in-memory 仓储模拟冲突）。

**当前实现**：乐观锁冲突、并发幂等、supersede 竞态都有用例，但**冲突是构造出来的**（在内存仓储里让 `save` 抛 `ConcurrentModificationError`），不是在两条真实连接上竞争出来的。

**违反铁律**：计划 §7.4「并发验证」要求"并发审批 / 并发状态迁移 / 重复任务提交 / Outbox 重复投递 / Worker 重试"以真实竞争验证。

**根因**：测试分层做得干净（unit 用内存端口），但没有补一层"真实竞争"的 integration。乐观锁的 SQL 语义（`WHERE entity_version = :expected` 命中 0 行）只在 integration 里被间接验证过一次。

**影响**：一旦 `save()` 的条件更新写错（例如漏了 `entity_version` 条件、或 `rowcount` 判定错），单元测试仍全绿——因为冲突是测试自己抛的。

**目标设计**：新增 `tests/integration/test_concurrent_transitions.py`：两个 `asyncio` 任务持各自连接，对同一资产同时 `approve`，断言一成一败且事实表/事件各只有一条。Outbox 侧补「同一 event 被投递两次，消费端只生效一次」。

**需删除的旧机制**：无。

**验收标准**：故意把 `save()` 的条件去掉 → 该用例必须红（当前单元测试对这类改动不敏感）。

---

### N-10（P3）授权入口有四种形态

**位置**：`services/identity_access/deps.py`（`require_role` / `require_resource_role` / `authorize_on_parent`）+ `project_routes.py:99-111`（内联 resolver + authorize）。

**当前实现**：四种都能到 `authorize`，但调用形状不同。`PROJECT.CREATE` 因为父级 Organization 可为空，绕开了 `authorize_on_parent` 的"parent_id 必须非空"约束，自己写了两行。

**违反铁律**：计划 §4.1「禁止业务路由各自实现一套授权逻辑」的精神（同一决策存在四种写法，下一个人会挑最省事的那一种）。

**根因**：`authorize_on_parent` 的签名假设"父资源必然存在"，而创建个人项目的父级是隐式的（`OwnershipPath(tenant_id)`）。这不是设计冲突，是**接口没覆盖一个真实情形**，于是调用点自己补。

**影响**：低。当前四种形态都正确，且断言 `auth.single_entry_covers_resource` 保证最终都进 `authorize`。风险在未来：新增创建类动作时容易抄错那一种。

**目标设计**：把「创建时的归属候选」作为 `authorize_on_parent` 的一等输入（`parent: ResourceRef | OwnershipPath`），让 `PROJECT.CREATE` 回到统一入口；路由层不再直接 import `resolver` 与 `authorize`（由断言强制：`services/*/api` 不得 import `SqlOwnershipResolver`）。

**需删除的旧机制**：路由内联的 resolver 调用。

**验收标准**：断言新增一条「api 层不得 import resolver / authorize」；`PROJECT.CREATE` 的 404/403/201 三条路径保持。

---

### N-11（P3）`app_role` 作为遗留角色仍持有残留权限

**位置**：运行库：`app_role` 对 `outbox.outbox_events` 有 SELECT、对 `auth.revoked_tokens` 有 SELECT；`packages/shared/config.py:56` 仍以 `app_role` 为默认 `postgres_app_user`。

**当前实现**：生产路径全部用域角色；`app_role` 只在测试/工具链的兼容路径使用（`database.py:162` 明确标注）。

**违反铁律**：R9「旧机制必须清理」（较弱形态）、红线「权限最小化」。

**根因**：角色拆分（DATA-1）时保留了向下兼容，兼容层没有退役计划。

**影响**：低（无业务表权限、RLS 仍生效、租户内可见）。但它是一条**平行的授权面**：若 DSN 回到 `app_role`，业务接口会因无权限而失败（安全），但 outbox 读取会绕过域隔离（不理想）。

**目标设计**：给出退役条件与日期；退役时删除角色、删除 `postgres_app_user` 配置项、删除 `database.py:162` 的兼容构造器。在此之前，把「`app_role` 不得出现在生产 DSN」加入启动期校验。

**验收标准**：`APP_ENV=production` 且 DSN 用 `app_role` → 启动失败。

---

## 8. 架构债务清单（按整改依赖排序）

> 顺序即整改顺序：前者是后者的前置。**不可并行开工**。

| # | 债务 | 前置 | 对应问题 | 交付物 |
| --- | --- | --- | --- | --- |
| D1 | 基线事实源单一化（含游离副本、悬空 ADR 引用） | 无 | N-01 | 删副本 + `freeze_manifest` 新断言 + README 修正 |
| D2 | 审批/迁移事实表 | D1（契约修订需走 amendments） | N-02 | 新表 + 契约 + 迁移 + 断言 |
| D3 | RLS 组织维度 + FORCE + DSN 校验 | D2（策略需与归属同源） | N-03 | 迁移 + 断言 + 负向测试 |
| D4 | 角色最小权限（业务 schema） | D3（同一批迁移内完成，避免二次改权限） | N-06 | 迁移 + 断言 `data.role_grants_least_privilege` |
| D5 | 状态机形态统一 | D2（守卫需要证据端口） | N-07 | `Transition` 统一 + 断言 |
| D6 | 30 项基线缺口分批裁定 | D1 | N-05 | amendments 批次 + `decision_status` |
| D7 | Phase 1 主链 7 环 | D2/D3/D5（授权与生命周期模型必须先定型） | N-04 | 7 环运行时 + e2e |
| D8 | 并发真实竞争验证 | D2（需事实表断言"只有一条"） | N-09 | integration 用例 |
| D9 | 安全扫描 + 部署产物 | 无（可并行，但不该提前，否则产生"安全已闭合"的错觉） | N-08 | CI job + manifest/债务登记 |
| D10 | 授权入口形态收敛、`app_role` 退役 | D7 | N-10 / N-11 | 断言 + 配置清理 |

**关于 D9 的顺序说明**：它与 D1–D8 无技术依赖，是最容易做、做完最容易产生"安全已收口"错觉的一项。按 R1 方案的既有判断（S3 应与其余阶段并行但不提前）保持：先让基线诚实、让事实落库，再补扫描。

---

## 9. 明确不做的事

- **不修改 V2.0.5 正文**。D6 的 30 项缺口一律走「owner 裁定 + amendments 登记 + 重签」，不直接编辑正文。
- **不新增 `ApprovalManager` / `OwnershipService` / `AuthzPolicy` 平行层**。D2 是一张表 + 一条写入路径，D3 是把已有输入 `organization_ids` 注入数据库会话。
- **不在路由层补第二套组织判断**（包括"临时"补），D3 的断言会拒绝这类改动。
- **不为让 CI 变绿而调整闸门口径**。本报告发现的问题全部是可执行的（断言 / 测试 / 迁移），没有一条需要靠"放宽闸门"解决。
- **不在本阶段写 Phase 1 业务代码**。D7 依赖 D2/D3/D5，顺序反了就是在错误的归属与生命周期模型上再写一遍补丁。

---

## 10. 核验方法与可复现命令

```bash
# 0. 环境（必须用项目虚拟环境，系统 python 缺依赖会静默中断）
.venv/Scripts/python.exe --version        # 3.12
docker compose -f infra/compose/docker-compose.yml ps

# 1. 三道闸门
.venv/Scripts/python.exe scripts/freeze_manifest.py --check
.venv/Scripts/python.exe scripts/contract_check.py
.venv/Scripts/python.exe scripts/architecture_check.py

# 2. 静态与测试（真实 PostgreSQL）
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m mypy packages services apps workers
POSTGRES_HOST=localhost POSTGRES_PORT=15432 POSTGRES_USER=postgres \
POSTGRES_PASSWORD=postgres POSTGRES_DB=ai_film_industrial \
REDIS_URL=redis://localhost:16379/0 \
.venv/Scripts/python.exe -m pytest tests
.venv/Scripts/python.exe -m alembic check

# 3. 授权面实测（不采信迁移文本）
docker exec ai-film-postgres psql -U postgres -d ai_film_industrial -c \
  "SELECT table_schema||'.'||table_name tbl, privilege_type, grantee
     FROM information_schema.role_table_grants
    WHERE grantee NOT LIKE 'pg_%' AND table_schema IN
          ('project_planning','asset_service','outbox','auth') ORDER BY 1,2,3;"

# 4. 组织隔离力度实测（N-03 证据）
docker exec -e PGPASSWORD=project_planning_dev_password ai-film-postgres \
  psql -U project_planning_role -d ai_film_industrial -c \
  "SELECT set_config('app.current_tenant_id','<某租户>',false);
   SELECT count(DISTINCT organization_id), count(*) FROM project_planning.projects;"
# → 当前返回 51 / 1584

# 5. 基线副本分裂实测（N-01 证据）
sha256sum AI影视工业化生产平台V2.0.5.md docs/baseline/AI影视工业化生产平台V2.0.5.md
git status --short   # 根目录那份是 ??
```

---

## 11. 结论

1. **历史 10 项 P1/P2 全部真实关闭**，且关闭方式是"改机制"而不是"改声明"。这是前三轮整改最实在的成果，必须保持。
2. **闸门体系是可信的**：17 条不变量、17 条可执行断言、双向校验的缺债台账、`status` 禁止手写。第三轮审查指认的元根因（闸门自证）已经消除。
3. **新发现的 11 项问题里，N-01/N-02 是本轮最要紧的两项**：前者让"基线"这个词失去确定性，后者让"审批可追溯"挂在一张会归档的表上。两者都不是实现 bug，是事实源缺位——与前三轮同类问题**同一形态的第三次出现**，因此必须在同一轮里用机制堵死，而不是逐项修复。
4. **Phase 1 不可验收**：主链 7 环缺失、25/40 ACTIVE 动作无入口。这不是质量问题，是进度事实；台账已经诚实登记，不需要再"发现"一次，需要的是按 D7 排队。
5. **下一步**：按 §8 的 D1 → D2 顺序先出两份 ADR（基线事实源单一化、审批与迁移事实表），再动代码。**在此之前不改任何业务实现。**

> 本报告只做核验与定位。每一项的整改方案必须另立 ADR 并经确认后实施——这是计划 §2.3 的顺序要求，也是前三轮"先写代码再倒推架构"教训的直接产物。
