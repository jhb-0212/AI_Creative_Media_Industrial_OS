# ADR-R27：镜头域从 project_planning 正式拆分为独立领域 shot_planning

- 状态：已接受
- 日期：2026-11-17
- 关联：V2.0.5 第 14.2 节「模块数据所有权矩阵」（待修订）、第 8 章领域模型、
  `ADR-032`（AI 拆镜输出默认草案）、`ADR-R07`（ShotDraft 缺对象契约，本 ADR 兑现其债务）、
  `ADR-R16`（数据库运行身份按业务域拆分）、`ADR-R17`（归属链与项目/组织级行隔离）、
  `ADR-009`（模块化单体优先，数据所有权先行）
- 契约修订：`contracts/phase0/contract_amendments.yaml` → `V205-R14-A4`（对象契约，已登记）与 `V205-R14-A5`（基线正文 14.1/14.2 所有权矩阵修订，已登记）

## 1. 背景

V2.0.5 第 14.2 节当前把 `Shot`、`ShotRevision`、`ConstraintPack`、`Intent`、
`ShotSpec` 全部放在 **Project & Planning** 模块的数据所有权之下（正文第 2014 行）。
在 Phase 0 契约冻结阶段，镜头域尚未实现（`SHOT.DRAFT.CREATE` /
`SHOT.REVISION.CONFIRM` / `SHOTSPEC.LOCK` 仍是 `not_implemented`，见 ADR-R07），
因此这个归属定义从未被运行时代码验证过。

进入 Phase 1 之前，必须先回答一个结构性问题：**镜头域是一个"子模块"，还是一个
独立业务域？** 回答错误会直接把未来最核心的生产对象（镜头）塞进一个职责不断
膨胀的 `project_planning`，或者反过来制造两个互相打架的"规划"模块。

## 2. 决策依据：镜头域具备独立领域的全部特征

判断一个领域是否独立，不看改动量，看四件事——复杂度、生命周期、业务职责、
变更频率：

| 维度 | project_planning | shot_planning |
|---|---|---|
| 核心对象 | Project、Episode、Scene、Script、ScriptRevision | Shot、ShotDraft、ShotRevision、ShotSpec、ShotVisualPlan、ConstraintPack |
| 核心职责 | 项目组织、剧本管理、剧本版本与锁定 | 镜头设计、规格、硬约束、版本与审批 |
| 业务生命周期 | 项目及剧本生命周期（相对稳定） | 镜头版本与审批生命周期（创意驱动、高频变更） |
| 主要使用者 | 制片、编剧、项目负责人 | 导演、分镜师、视觉设计师、Agent |
| 核心规则 | 项目归属、剧集组织、剧本版本 | 镜头连续性、视觉方案、硬约束、影响传播 |

镜头域后续要承载：AI 拆镜草案、ShotRevision 人工确认、ShotSpec 结构化规格、
ShotVisualPlan 视觉方案、ConstraintPack 硬约束管理、镜头版本及影响传播、镜头级
创意探索。这是一个拥有**独立领域模型、独立状态机、独立治理规则**的复杂业务域。

**结论**：拆分为独立领域，符合 ADR-009「数据所有权先行」与用户长期架构质量要求。
这不是为了增加模块数量，而是让每个业务域真正拥有自己的模型、规则和数据。

## 3. 决策

### 3.1 正式采用独立 shot_planning 领域

- 独立顶层领域模块 `services/shot_planning/`；
- 独立数据所有权（独立 schema + 独立数据库角色）；
- 独立领域模型和状态机；
- 独立 API、Application Service、Repository；
- 统一复用现有身份、权限、事务、审计基础设施；
- 初期保持模块化单体，不拆独立微服务（符合 ADR-009）。

### 3.2 数据库边界（遵循 ADR-R16 三处 fail-closed 规则）

| 项 | 值 |
|---|---|
| schema | `shot_planning`（独立，不复用 project_planning schema） |
| 数据库角色 | `svc_shot_planning`（`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`） |
| `DatabaseDomain` 枚举 | 新增 `SHOT_PLANNING = "shot_planning"` |
| `_DOMAIN_ROLES` 映射 | `DatabaseDomain.SHOT_PLANNING: "svc_shot_planning"` |
| RLS | 租户隔离 + 项目范围（RESTRICTIVE）双策略，与 `project_planning`/`workflow` 同口径 |

**为什么不复用 project_planning schema**：表复用同一 schema 会让 `svc_shot_planning`
与 `svc_project_planning` 共享命名空间，产生两种隐患——（1）跨域读权需要重新开通道，
与 ADR-R17 第 4 节「不跨域校验」的自洽性冲突；（2）ORM 模型若共用 `Base.metadata`
会泄漏内部结构，违反 ADR-R16「避免共享 ORM Model 泄漏内部结构」。独立 schema 是
唯一与"独立数据所有权"自洽的选项。

**新增域的三处 fail-closed 改动**（ADR-R16 第 4 节）：
1. `packages/shared/db_roles.py` 加枚举值 + 角色映射；
2. 迁移创建角色 + 精确 GRANT + RLS；
3. `partition_guard` 分区回收名单加入新角色。

### 3.3 领域边界（硬约束）

- `project_planning` **只负责**：项目创建、项目配置、集数、场景组织、剧本导入、
  剧本版本和剧本锁定。**不再直接写 Shot 数据**。
- `shot_planning` **只负责**：从剧本及项目上下文接收信息，建立镜头草案、正式镜头、
  镜头版本、镜头规格、视觉方案和硬约束。**不直接读取或修改 project_planning 的
  ORM Model**（跨域读走 Query API/View，见第 4 节）。
- **不允许两个模块同时拥有 Shot 的写权限**。
- `orchestrator` **不得直接修改 Shot 状态**（Shot 状态机唯一权威入口在
  shot_planning，Orchestrator 只经领域命令/事件交互）。
- **AI 只能按授权创建草案**（`SHOT.DRAFT.CREATE`，resource_type=ShotDraft），
  正式确认由授权人类完成（`SHOT.REVISION.CONFIRM`，resource_type=ShotRevision）。

### 3.4 对象归属（最终版）

| 对象 | 最终归属 |
|---|---|
| Shot | shot_planning |
| ShotDraft | shot_planning |
| ShotRevision | shot_planning |
| ShotSpec | shot_planning |
| ShotVisualPlan | shot_planning |
| ConstraintPack | shot_planning |
| Project、Episode、Scene、Script、ScriptRevision | project_planning |
| WorkflowRun、Task、Dependency、GateInstance | orchestrator |
| PrevizVersion、CameraRig、ControlSignalAsset 元数据 | blocking_service |
| Asset、AssetVersion、Manifest | asset_service |
| QCReport、Benchmark、RightsCase、Override | qc_governance |

### 3.5 IntentContract 作用域归属

`Intent`（Intent Contract）按作用域拆分权威归属，**禁止两个模块同时维护同一份
意图事实**：

| Intent 作用域 | 权威归属 |
|---|---|
| 项目级意图（导演对整部作品的整体意图） | project_planning |
| 镜头级意图（导演对某个镜头/镜头组的意图） | shot_planning |

`ShotRevision` 引用固定的 `intent_id + intent_version`（基线 8.8 节）；镜头级意图
由 shot_planning 拥有，项目级意图由 project_planning 拥有，两者通过显式引用关联，
不复制、不共享写权。

## 4. 跨模块交互契约

- **同步**：`shot_planning` 需要项目/集数/场景上下文时，经 project_planning 的
  Query API/View 读取（不直接访问其 ORM Model）；跨模块同步调用须有超时、幂等键、
  鉴权、结构化错误码（基线 14.3）。
- **异步**：Shot 状态变化经 Outbox 发布（如 `ShotRevisionConfirmed`、
  `ShotSpecLocked`），下游 orchestrator / blocking_service / asset_service 经 Inbox
  去重消费。
- **禁止共享数据库写**：shot_planning 代码不得 UPDATE project_planning 表，反之亦然。
  跨模块事务通过 Outbox + Saga/补偿完成（基线 14.3）。

## 5. 需要同步修改的 7 项（架构基线修订）

采用独立 shot_planning 必须正式形成架构决策修订，**不能一边保留旧的所有权定义，
一边新增 shot_planning**。需调整：

1. **V2.0.5 第 14.2 节模块数据所有权矩阵**：从 `Project & Planning` 行拆出
   `Shot、ShotRevision、ConstraintPack、ShotSpec` 到新 `Shot Planning` 行；
   `Intent` 按 3.5 作用域拆分标注。
2. **第 14.1 节工程目录结构**：`services/` 下新增 `shot-planning/`。
3. **Shot 域对象及状态机归属**：`state_machines.yaml` 中 Shot/ShotDraft/ShotRevision/
   ShotSpec/ConstraintPack/ShotVisualPlan 的 `owner_module` 标注到 shot_planning。
4. **Action Registry 与权限矩阵**：`SHOT.DRAFT.CREATE` / `SHOT.REVISION.CONFIRM` /
   `SHOTSPEC.LOCK` / `CONSTRAINT.PACK.CREATE` 的 `owner_module` 标注到 shot_planning。
5. **Shot 与 Project/Episode/Scene/Script 的关联契约**：定义 shot_planning 引用
   project_planning 的 Query API/View 边界（只读、显式契约）。
6. **数据库 Schema、迁移、跨模块查询边界**：新增 `shot_planning` schema + 角色 +
   RLS + 迁移；跨域只读走 View，不共享 ORM。
7. **测试所有权与模块验收标准**：`tests/` 中 shot_planning 域测试 + 域隔离负向测试
   （跨域写/读被数据库拒绝，参照 `test_database_role_isolation.py`）。

> 第 1、2 项直接改动基线正文，会触发 `freeze_manifest.py --check` 的 SHA-256 漂移，
> 必须走契约冻结生命周期（`contract_amendments.yaml` 登记 A4 + 更新 manifest），
> 不得直接静默改写。本条 ADR 是 A4 的决策依据。

## 6. 实施顺序（阶段 1 先行、逐段闸门固化）

1. 补 `ShotDraft` + `ShotRevision` 对象契约（兑现 ADR-R07 债务，字段按基线 19.3
   拆镜流程 + 8.1 核心实体口径）；
2. 建 `services/shot_planning/` 四层（domain/repository/service/api）；
3. `DatabaseDomain.SHOT_PLANNING` + 迁移（schema/表/RLS/GRANT/状态转移表）；
4. `SHOT.DRAFT.CREATE`（AI 拆镜草案）+ `SHOT.REVISION.CONFIRM`（人工确认）用例；
5. `action_implementations.yaml` 登记 implemented；
6. 测试 + 全闸门 + 固化提交；
7. 基线正文 14.1/14.2 修订（A5 冻结流程，独立提交）。

## 7. 后果

**变好的**：
- 镜头域拥有独立模型、状态机、数据所有权，不再与项目/剧本组织耦合；
- 跨模块写 Shot 在数据库层失败（独立 schema + 角色），不依赖 import 纪律；
- 未来 AI 拆镜、分镜设计、镜头优化、控制信号等扩展有清晰归属。

**代价（明说）**：
- 新增一个域需改三处数据库身份（枚举/迁移/分区名单），缺一即 fail-closed；
- Shot 与 Project/Episode/Scene 的关联需跨域只读契约，不再是"同模块直接 join"；
- 基线正文修订涉及契约冻结流程，不能静默改。

## 8. 回退

本 ADR 在代码落地前的回退是"不登记、不建域"（纯文档决策）。一旦第 6 节第 3 步
迁移执行，回退为 `alembic downgrade` 删除 `shot_planning` schema 与角色（与
ADR-R16 第 6 节同口径：集群级角色在多库授权时 DROP ROLE 会拒绝，迁移 docstring
须写明）。基线正文修订的回退为冻结流程的 `revert` 语义，见 freeze_manifest 契约。


## 8. 附录：ShotChanged Event 归属核对（2026-11-17，A5 residual_risk 收口）

**核对结论**：`ShotChanged Event` 是第 14.2 节矩阵「主要对外契约」列的
**概念事件**——基线全文仅此一处出现，无触发条件、无 payload 定义；运行时
零代码引用（grep 0 命中），事件目录（event_catalog.yaml）未登记。

这不是口径漂移，而是两类契约的层次差异：

- 事件目录只登记**真实发出**的运行时事件（双向校验：登记的必须真被发出）；
- 矩阵「对外契约」列描述模块的**契约面**，其中部分事件（ShotChanged、
  PrevizVersionCreated 等）尚未有实现承载，与 Action Registry 的
  `not_implemented` 同属"契约已定义、实现未落地"。

**处置**：A5 已把该事件在矩阵中的归属随镜头域移至 Shot Planning 行，与
代码事实（镜头域拥有镜头内容变更）一致；运行时落地依赖「镜头内容变更
传播」业务需求出现，届时由 shot_planning 域发布并登记事件目录。在此之前
**不登记事件目录**——登记了就是台账撒谎。

**债务登记**：本节核对后，矩阵概念事件（ShotChanged、PrevizVersionCreated
及后续新发现者）的运行时承载条件、责任域与落地路径统一登记在
**ADR-R30《矩阵概念事件债务台账》**——该台账是唯一权威，本节保留核对
过程与结论，条目状态以 ADR-R30 为准。

