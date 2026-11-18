---
doc_type: proposal
status: in_progress
updated_at: 2026-11-18
authoritative_sources:
  - contracts/security_boundary.yaml
  - contracts/runtime/action_implementations.yaml
  - scripts/architecture_check.py
open_items:
  - item: >-
      S5（Phase 1 端到端复验 + 性能实测）未执行；25 个 ACTIVE 动作仍无运行时入口，
      因此 Phase 1 不能宣布验收通过
    source: "contracts/runtime/action_implementations.yaml#not_implemented"
  - item: >-
      多主体共同审批（双人审批）未落地：AUTH-3 的 residual_risk 仍成立
    source: "contracts/security_boundary.yaml#AUTH-3"
---

# 工程铁律整改方案（R1）

> 状态：**以文首 front-matter 为准**（`status: in_progress`，未完成项见 `open_items`）。
> 这份文件只承载「复核结论 / 根因 / 目标架构 / 实施序列」，**不再承载完成度清单**——
> 完成度一律引用契约事实源（security_boundary 的断言结果、action_implementations 的
> 台账、architecture_check 的 R1–R5），否则状态一定腐化（P2-04 就是腐化的结果）。
> 基准：main / `574f0ca5734121a3dd6f34b71d2331ce0fe87f9c`（2026-11-18 10:17）
> 输入：第二轮重新审查报告（2026-11-18）；本方案对报告中每一项均做了独立代码复核，不沿用其结论。
> 定位：本文档只做三件事——**证伪/证实问题**、**收敛根因**、**定义目标架构与验收断言**。
> 它不是又一份"整改清单"，也不新增任何 Manager / Factory / Policy 平行层。

---

## 一、复核结论：报告 8 项全部成立，另新增 2 项

复核方式：逐条读实现，不接受 YAML/README 中的状态声明。

| 编号 | 报告结论 | 复核 | 证据（文件:行） |
|---|---|---|---|
| P1-01 | Identity 继承数据库凭证 | **成立，且范围更大** | `infra/compose/docker-compose.yml:17-35` 定义 `x-db-env`（含 `POSTGRES_USER: postgres` + 全部角色口令）；`:144-145` identity、`:119-120` relay、`:196` outbox-maintenance 三处 `<<: *db-env` 全量继承 |
| P1-02 | 资产状态接口可绕过审批 | **成立，但可达路径更窄** | 转移表 `asset_service/domain/state_machine.py:21-28` 只有 `HUMAN_REVIEW→APPROVED` 一条入边，故必须先经 `approve()` 进入 HUMAN_REVIEW 才能绕；绕过点在于 `asset_service.py:420-432` 仅对 GOLD 校验证据，`APPROVED` 无证据门槛 |
| P1-03 | Episode 状态变更缺组织校验 | **成立，且读路径同样缺失** | `episode_service.py:153-193` 无组织入参；`episode_routes.py:104-112` `get_episode` 亦无组织校验（报告未提） |
| P1-04 | 资产无组织/项目归属 | **成立** | `services/asset_service/repository/orm.py:31-49` 仅有 `tenant_id`，无 `project_id`/`organization_id`；而契约 `action_registry.yaml` 对资产动作声明 `scope: project/shot/asset` |
| P2-01 | 端口映射固定 8100 | 成立 | `docker-compose.yml:150`（`IDENTITY_PORT` 可配）、`:155`、`:163` 硬编码 8100 |
| P2-02 | 域角色名可重名 | 成立 | `8f3b2c1d4e5a_split_domain_roles.py::_roles()` 直接返回三个配置值，无互异性断言 |
| P2-03 | Service 依赖 SQLAlchemy `IntegrityError` | 成立，4 处 | `asset_service.py:26,146,236`；`episode_service.py:21,110`；`project_service.py:18,93`；`script_service.py:27,132` |
| P2-04 | 提案状态与实际不符 | 成立 | `docs/proposals/F01-F05-security-data-boundary-refactor.md:3`「提案 / 待评审冻结」，而 F01-F05 已合并 |

### 新增 P1-05：契约闸门是自证的，所以"已满足"不可信

`contracts/security_boundary.yaml` 中 `AUTH-1`（授权决策单一入口）标记 `status: satisfied`，
但其证词与代码矛盾：`project_routes.py:80-114` 的 `_assert_organization_scope` 是**路由层手写的
资源级授权判定**，`episode_routes.py` 干脆没有。同一份契约里 SCOPE-1/SCOPE-2/DATA-1~3/KEYS-1~3
也全部 `satisfied`，而 KEYS-1 的不变量只覆盖"签发私钥"，**完全没有覆盖"进程持有的数据库凭证"**，
所以 P1-01 对闸门是隐形的。

根因在闸门本身：`scripts/contract_check.py::check_security_boundary` 只校验 YAML 的结构自洽
（字段齐备、id 不重复、status 枚举合法、债务子块不残留），**从不校验 `fact_source` 指向的代码是否
真的成立**。这意味着 `status` 是人工填写的自我声明，与第一轮、第二轮审查反复出现的
"宣称即销项"是同一个机制。

> 这条不修，后面所有整改的"完成"都只是换个地方写声明。

### 新增 P1-06：Episode 读路径的组织越权（与 P1-03 同根不同面）

`GET /episodes/{id}` 只做 RBAC + RLS。租户内跨组织可读集数标题、集号、归属项目 id。
与 P1-03 合并处理，但负向测试必须**分别**覆盖读与写——只测写会漏掉这条。

---

## 二、根因收敛：8 项问题只有 4 个根因

审查报告建议"按根因重新收敛"，但给出的六条仍是问题分类，不是根因。逐条追问后收敛为四条：

### R-A 授权决策的输入不完整，所以资源级判定注定溢出到调用点

`rbac.py:43` 的签名是 `authorize(action_key, principal)`——**没有资源**。
决策函数无法回答"能不能碰这个对象"，于是每个路由只能自己补一段归属判断：
project 补了（手写）、episode 没补、asset 补不了（模型里没有归属字段，P1-04）。
这不是三个人分别偷懒，而是接口形状决定的结果。

**解释了**：P1-03、P1-04、P1-06、F02"部分闭合"、AUTH-1 假 satisfied。

### R-B 状态迁移只建模了"结构合法性"，没建模"准入条件"

`state_machine.py` 只回答 `source → target` 是否登记，不回答"够不够格走这条边"。
证据门槛（权利清单、QC 报告）和职责分离被写进**用例方法内部**，于是 `approve()` 与
`change_status()` 各自实现了一份，且必然漂移——`change_status` 只对 GOLD 加门槛就是漂移的产物。

**解释了**：P1-02，以及"禁止补丁式演进"被判"需重点复核"。

### R-C 进程的**凭证面**没有被建模为契约对象，编排文件是配置而非受校验事实

security_boundary 的 `process_keys` 只建模了"密钥"，没建模"数据库凭证"。`x-db-env` 这个
anchor 的存在本身就是设计错误：它把"所有进程用同一套数据库凭证"固化成了默认值，谁 `<<:`
谁就全拿。identity 进程 `workers/identity/app.py` 全文不碰数据库，却拿到了 postgres 超级用户口令。

**解释了**：P1-01、P2-01、P2-02。

### R-D 契约闸门没有验证能力，只能验证格式

见 P1-05。**这是元根因**：它让 R-A/R-B/R-C 的缺口在 CI 里不可见，也让"整改"可以靠改 YAML 完成。

**解释了**：为什么二轮审查仍能挖出 P1；为什么 CI 4/4 全绿。

### 映射表

| 问题 | 根因 | 整改阶段 |
|---|---|---|
| P1-01 凭证继承 | R-C（+R-D 使其隐形） | S3 |
| P1-02 绕过审批 | R-B | S2 |
| P1-03 / P1-06 集数越权 | R-A | S1 |
| P1-04 资产无归属 | R-A（数据模型是其前置） | S0.5 + S1 |
| P1-05 闸门自证 | R-D | S0 |
| P2-01 / P2-02 | R-C | S3 |
| P2-03 | 分层契约缺失（仓储泄漏 ORM 异常） | S4 |
| P2-04 | 流程债（文档未随实施更新） | S4 |

---

## 三、目标架构（四条，均为"消除结构来源"，不是加层）

### T-A 一次授权决策 = (主体, 动作, 资源归属) 的完整输入

```
AuthorizeRequest(principal, action_key, resource_ref)
      │
      ├─ Action 解析（已有）
      ├─ RBAC（已有）
      ├─ Capability（已有）
      ├─ 归属解析 OwnershipResolver.resolve(resource_ref) → OwnershipPath
      │      OwnershipPath(tenant_id, organization_id, project_id)
      ├─ 范围判定：路径上的每一级都必须被 Principal 覆盖（fail-closed）
      └─ 职责分离（需要审批主体的动作）
```

要点：

1. **归属解析是唯一入口**，按 `(resource_type, id)` 返回完整归属链，不返回"某个字段"。
   调用方拿不到"半个归属"，也就不可能只比一半。
2. **归属链只有一条路径**：Asset → Project → Organization → Tenant；Episode → Project → …。
   禁止在子资源上冗余存 `organization_id`（冗余即漂移面）。
3. **路由层禁止出现归属判定**：改造完成后由架构闸门反向锁定——
   `services/*/api/*.py` 不得出现 `organization_id` 比较、不得构造 `AUTH_SCOPE_DENIED`。
   这条闸门是防止 R-A 复发的唯一手段（人靠自觉必然复发）。
4. 资源解析需要一次 DB 读，这是成本；换掉的是三处不一致的手写判断。可后续用
   `Principal` 内的 project 集合 claim 做前置快筛，但不改变判定归属。

**判定对象不是"资源是否存在"**：解析失败与无权限统一 404（沿用现有 `get_version` 的口径），
避免用 403/404 差异探测他人资源。

### T-B 状态机的边自带准入守卫，边的所有者是唯一入口

```python
@dataclass(frozen=True)
class Transition:
    source: Status
    target: Status
    owner_action: str            # 唯一被允许走这条边的动作
    guards: tuple[Guard, ...]    # 准入条件（声明式，纯函数求值）

# AssetVersion
Transition(TECHNICAL_QC, HUMAN_REVIEW, "ASSET.VERSION.APPROVE", (Guards.rights, Guards.qc)),
Transition(HUMAN_REVIEW,  APPROVED,     "ASSET.VERSION.APPROVE", (Guards.rights, Guards.qc, Guards.separation)),
Transition(APPROVED,      GOLD,         "ASSET.STATUS.CHANGE",   (Guards.rights, Guards.qc)),
```

- `change_status` 的语义从"目标状态作参数"变为"**沿当前状态中归本动作所有的边推进**"；
  目标状态若不是本动作拥有的边，直接拒绝。`APPROVED` 的边 owner 是 APPROVE，
  于是通用接口在结构上**无法**到达 APPROVED——不是靠多加一个 if 拦住，而是那条边不在它的可达集里。
- Guard 求值入参是 `(聚合快照, EvidenceView)`；证据由 `EvidencePort` 注入取数，
  守卫本身不查库、不判断"要不要查"。`approve()` 与 `change_status()` 复用同一组 Guard 实例。
- 契约同步：`action_registry.yaml` 中 `ASSET.STATUS.CHANGE` 的 `approval_requirement`
  必须改为"仅可推进本动作拥有的边；APPROVED 与 HUMAN_REVIEW 由 ASSET.VERSION.APPROVE 独占"。

### T-C 凭证面按进程建模，编排由声明校验

`security_boundary.yaml` 扩一条新轴 `credential_scope`（不是塞进 `process_keys`——密钥与凭证是
两类事实，混在一条不变量里就会像 KEYS-1 那样漏掉一半）：

```yaml
credential_scope:
  - id: CRED-1
    name: 进程凭证最小面
    statement: 每个部署单元只能获得其运行时真实使用的凭证；数据库凭证按角色分发，超级用户凭证不出 postgres 容器。
    verification: scripts/verify_credentials.py（解析 compose，比对每服务 env 与声明集合）
```

编排改造（S3）：

- 删除 `x-db-env` 单一 anchor，拆为 `x-db-endpoint`（host/port/db）+ 每个角色一个 anchor；
- `identity`：**不注入任何数据库变量**（它不碰库，一条都不该有）；
- `relay` / `outbox-maintenance`：只注入 `outbox_relay_role`；
- `migrate`：唯一允许持超级用户凭证的非 postgres 单元（DDL 需要），并在契约中显式登记为例外；
- `IDENTITY_PORT` 与端口映射、健康检查同源，消除 P2-01；
- 迁移 `_roles()` 增加互异性断言，重名即拒绝迁移（P2-02）。

### T-D 闸门必须能证伪：`status` 不再手写

两件事同时做，缺一不可：

1. **近期（S0，可立即做）**：把 4 项 P1 + 2 项新增如实登记为 `partial`/`debt`，
   附 `reason / contract_ref / unblocks_when`。目的是让基线诚实——闸门允许"有债"，
   不允许"假绿"。
2. **结构性（S0）**：`status` 从人工字段改为**由 verification 执行结果决定**：
   - 每条不变量声明 `verification`（pytest node id 或校验函数名）；
   - `contract-check` 调用 `verify_boundary` 收集结果，文件里的 `status` 仅允许
     `debt` / `partial`（带原因）或省略（= 由执行结果决定）；
   - 手写 `satisfied` 即报红。这与现有 `state_machine_gaps`/`resource_schema_gaps`
     的"缺口不可静默增长"哲学一致，只是把"声明"换成了"执行"。

---

## 四、实施序列

顺序不可交换：S0 让基线诚实 → S0.5 定归属模型 → S1 授权输入完整 → S2 生命周期 → S3 凭证面
→ S4 债清理 → S5 复验。理由：S1 依赖 S0.5 的归属字段；S2 的 Guard 需要干净的证据端口；
S3 与其余三条无依赖，可并行但不应提前——它最容易做，做完最容易产生"安全已闭合"的错觉。

### S0 基线诚实化 + 闸门去自证（0.5 天）

- 改 `contracts/security_boundary.yaml`：AUTH-1 降为 `partial`（写明路由层手写判定这一事实），
  新增 CRED-1、SCOPE-3（资产归属链缺失）为 `debt`。
- 改 `scripts/contract_check.py::check_security_boundary`：禁止手写 `satisfied`；
  新增 `verify_boundary` 调用点。
- **验收**：故意把 AUTH-1 改回 `satisfied` → `make contract-check` 必须红。

### S0.5 资产归属模型（决策点 D-1，0.5 天）

**这是唯一需要先定模型、不能先写代码的地方。** 三个候选：

| 方案 | 形态 | 判断 |
|---|---|---|
| A 加 `project_id` + `organization_id` 两列 | 归属冗余 | 否。项目换组织时资产不同步，制造第二事实源 |
| B 多态归属 `owner_kind` + `owner_id` | 一列表达 project/shot | 否。多态外键无法建 FK，归属真实性退回服务层校验 |
| **C 归属唯一 + 引用分离（推荐）** | `project_id NOT NULL` 复合外键引用 `(project_id, tenant_id)`；组织从 project 派生不落库；`asset_shot_links` 表表达镜头级引用 | 是。归属链唯一、数据库层可约束、与 Project→Episode 既有做法同构 |

关键取舍：**镜头级资产不是"资产归属镜头"，而是"资产被镜头引用"**。契约 `scope: project/shot/asset`
表达的是"授权作用域"，不是"归属层级"——把作用域误读成归属，正是 P1-04 的由来。
若 D-1 选 C，需同步在 `contracts/schemas/object_schemas.schema.json` 为 AssetVersion 补 `project_id`
（并说明它不对外暴露，与 `tenant_id` 同待遇）。

**验收**：跨 schema 复合外键在库里真实生效（`tests/integration/test_ownership_fk_constraints.py`
新增用例）；`asset_service_role` 对 `project_planning.projects` 只有 `REFERENCES`/`SELECT`，
无写权（负向测试：`INSERT INTO project_planning.projects` 必须被拒）。

### S1 授权输入完整化（2 天，覆盖 P1-03/P1-04/P1-06/F02）

1. `packages/identity/ownership.py`：`OwnershipPath` + `OwnershipResolver`（按 resource_type 派发，
   每类资源一个解析函数，集中在一处）。
2. `services/identity_access/rbac.py`：`authorize` 增加可选 `resource` 形参；
   内部调用 resolver → 逐级判定。**保持向后兼容**：`resource=None` 时行为与今日一致，
   便于分批改造，但改造完成后由闸门禁止 `resource=None` 出现在写路由。
3. 路由改造：project（删掉手写 `_assert_organization_scope`）、episode（读写都接）、asset（接 project 归属）。
4. 架构闸门新增规则：`services/*/api` 不得出现 `organization_id` 比较与 `AUTH_SCOPE_DENIED` 构造。

**验收（负向测试，缺一即不通过）**：
- `EPISODE.READ` 跨组织 → 404（或 403，二者取一并在契约固定）；
- `EPISODE.STATUS.CHANGE` 跨组织 → 拒绝；
- `PROJECT.READ` 跨组织 → 拒绝（回归）；
- `ASSET.VERSION.READ` 跨组织 → 拒绝；
- 组织项目被无 org claim 主体访问 → 拒绝（个人主体不能读组织资产）；
- 架构闸门负测：往 `episode_routes.py` 塞一行 `organization_id` 比较 → `make arch-check` 红。

### S2 生命周期统一（1.5 天，覆盖 P1-02）

1. 改 `state_machine.py`：`Transition` 带 `owner_action` + `guards`；`ASSET_ALLOWED_TRANSITIONS`
   改为 `ASSET_TRANSITIONS`（结构化，不再只是邻接集）。
2. 新增 `domain/guards.py`：`rights_manifest_present`、`qc_evidence_sufficient`、
   `separation_of_duties`——纯函数，入参为聚合 + 证据视图。
3. `asset_service.approve()` / `change_status()` 改为共用 `advance(edge_owner, target)`；
   两者都不再包含任何证据判断代码。
4. 契约同步：`action_registry.yaml` 的两条 `approval_requirement` 改写为"边所有权"表述；
   `state_machines.yaml` 若支持边守卫字段则同步，否则在 `contract_check` 里加一条
   "代码转移表与 YAML 边的 owner 一致"的交叉校验。

**验收**：
- `change_status(target=APPROVED)` 从任意状态 → 拒绝（负向）；
- `change_status(target=GOLD)` 且缺 QC → 拒绝（回归）；
- `approve()` 与 `change_status()` 对同一缺失证据返回**同一错误码**（一致性断言，
  这是"两套规则"被消除的可执行证据）；
- 断言 `asset_service.py` 中不再出现 `rights_manifest_ref` 的空值判断（静态检查）。

### S3 进程凭证面（0.5 天，覆盖 P1-01/P2-01/P2-02）

拆 anchor、identity 去 db-env、端口同源、角色名互异断言、`scripts/verify_credentials.py`。

**验收**：
- `python scripts/verify_credentials.py` 对每个服务断言 env 键集合 ⊆ 声明集合；
- 负测：给 identity 加回 `POSTGRES_PASSWORD` → 断言红、`make contract-check` 红；
- `docker compose config` 渲染后 identity 服务不含任何 `POSTGRES_*`；
- 角色重名配置 → 迁移拒绝（负向）。

### S4 债清理（1 天）

- P2-03：仓储端口统一异常契约（`DuplicateEntity` / `ConcurrencyConflict`），由 SQLAlchemy 实现
  在边界翻译；`services/*/service` 不再 import `sqlalchemy.exc`。
  验收：`grep -r "sqlalchemy" services/*/service/` 为空（纳入 arch-check）。
- P2-04：提案文档状态改为「已实施 · 见 security_boundary 各不变量 + 本方案」，
  未完成项（如双人审批）显式列出。

### S5 Phase 1 复验（1 天）

必须用真实 PostgreSQL + 真实权限，不接受内存仓储通过。

- 未实现的 7 个动作：`SHOT.DRAFT.CREATE`、`SHOT.REVISION.CONFIRM`、`GENERATION.REQUEST`、
  `PRODUCTION.GATE.APPROVE`、`DELIVERY.MANIFEST.CREATE/APPROVE/RELEASE`。
  **本方案不含这些动作的实现**——它们的主链依赖 S1/S2 的授权与生命周期模型，
  顺序反了就会在错误的归属模型上再写一遍同样的补丁。
- 复验口径：CI 4 项 + 全部负向测试 + 端到端主链 + 性能实测（缺实测即"保持高技术标准"不达标）。

### 实施进度（2026-11-18，只记事实源，不手写"完成"）

| 阶段 | 状态 | 事实源 |
|---|---|---|
| S0 闸门去自证 | 已实施 | `scripts/boundary_assertions.py`（15 条断言注册表）+ `contracts/security_boundary.yaml`（断言验证 15 / partial 0 / debt 0）+ `tests/unit/test_contract_check.py` 负向用例 |
| S0.5 资产归属 | 已实施 | `asset_versions.project_id` 复合外键（迁移 `b3c4d5e6f7a8`，表非空即拒绝）+ `tests/integration/test_ownership_fk_constraints.py` |
| S1 授权输入完整化 | 已实施 | `packages/identity/ownership.py`（OwnershipResolver + assert_ownership_scope）+ `require_resource_role` / `authorize_on_parent`；路由层零归属判断（断言 `auth.single_entry_covers_resource`） |
| S2 生命周期统一 | 已实施 | `services/asset_service/domain/state_machine.py`（边带 owner + guards）+ `domain/guards.py`；断言 `lifecycle.edge_ownership` |
| S3 进程凭证面 | 已实施（机制合并，见下） | compose 四份 anchor + `PROCESS_ALLOWED_DB_ENV` 白名单 + `tests/unit/test_credential_surface.py`；断言 `cred.process_credential_surface` / `cred.role_names_distinct` |
| S4 债清理 | 已实施 | `packages/shared/port_errors.py`（端口失败契约）+ 架构闸门 **R5**（`services/*/{service,domain}` 禁止 import 持久化库）+ 提案 front-matter 状态闸门 `check_proposal_status()` |
| S5 Phase 1 复验 | **未执行** | 见文首 `open_items`：`action_implementations.yaml#not_implemented`（25 个 ACTIVE 动作无运行时入口） |

两处与原方案不同，理由写在这里而不是"顺手做了"：

1. S3 没有另建 `scripts/verify_credentials.py`。凭证面判定已作为断言
   `cred.process_credential_surface` 注册进 `boundary_assertions.py`，由闸门执行；
   再写一个独立脚本会形成**第二个事实源**（脚本判一套、闸门判一套），
   正是本轮要消灭的形态。
2. S4 的架构锁定用 R5 表达，而不是"`grep -r sqlalchemy` 为空"。grep 抓不到
   `from sqlalchemy.ext.asyncio import X` 与惰性导入，且无法区分
   `repository/`（合法）与 `service/`（非法）；AST 规则能。

---

## 五、明确不做的事

- 不新增 `AuthzPolicy` / `ResourceManager` / `OwnershipService` 之类的中间层——T-A 是把输入补进
  已有入口，不是新开一层。
- 不在路由里补第二个组织判断（包括"临时"补），S1 的闸门会直接拒绝这类改动。
- 不给 AssetVersion 加一个 `organization_id` 了事（见 S0.5 方案 A 的否证）。
- 不在 `change_status` 里补一段 `if target == APPROVED: 校验证据`——这正是 R-B 的复发形态。
- 不为了让 CI 变绿而把 `status` 改回 `satisfied`。

## 六、待决事项（已拍板）

| 编号 | 决策 | 结论 |
|---|---|---|
| D-1 | 资产归属模型选 A/B/C | **C**（归属唯一 `project_id` + 引用分离）。已落地并加复合外键 |
| D-2 | 越权响应用 404 还是 403 | **404**（跨租户/不可见一律 404，同租户跨组织 403）；口径写死在 `packages/identity/ownership.py::assert_ownership_scope`，不再逐处决定 |
| D-3 | 闸门是否允许手写 `satisfied` | **不允许**。写 `satisfied` 直接报红；只认 `assertion` 执行结果 + `runtime_evidence` 真实测试节点 |

## 七、工作量与顺序收益

S0 + S0.5 + S1 + S2 + S3 + S4 ≈ 6 天，S5 复验 1 天。
先做 S0 的收益是**立刻**的：基线从"4 项 P1 但全绿"变为"4 项 P1 且闸门报红"，
后续任何"已整改"都必须拿测试说话，第三轮审查不会再从同一条缝里挖出同类问题。
