# 游离基线副本的差异归档（N-01 证据）

> 本文件是**归档**，不是基线正文，不具有任何权威性。
> 它记录 2026-11-18 删除根目录游离副本 `AI影视工业化生产平台V2.0.5.md` 之前的完整差异，
> 目的是「删除副本但一粒字都不丢」，并作为 D6（30 项基线缺口分批裁定）的输入之一。
>
> 权威基线唯一路径：`docs/baseline/AI影视工业化生产平台V2.0.5.md`（V205-R14，489607 字节，
> SHA-256 `9a9940c5…`）。冻结清单与冻结流程见 ADR-R15，单一事实源决策见 ADR-R22。

## 一、两份文件的事实

| 项目 | 权威基线 | 根目录副本（已删除） |
| --- | --- | --- |
| 路径 | `docs/baseline/AI影视工业化生产平台V2.0.5.md` | `AI影视工业化生产平台V2.0.5.md` |
| 纳管状态 | tracked（随仓库交付） | **untracked**（从未提交） |
| 行数 / 编码 | 5095 / LF | 5107 / CRLF |
| SHA-256 | `9a9940c5…` | `d71d71e8…` |
| 受 `freeze_manifest` 校验 | 是（字节数 + 哈希） | **否**（闸门只认固定路径） |
| 被 README 称作 | `README.md:55` 正确指称 | `README.md:5`、`repository-structure.md:5` 指称 |

归一化换行符后，差异只有 **4 个 hunk / 新增 15 行 / 删除 3 行**。也就是说副本不是另写一份，
而是在权威基线基础上做了 4 处定点编辑。

## 二、完整差异（归一化换行后，verbatim）

```diff
@@ -1940,6 +1940,8 @@
 ├── services/
 │   ├── orchestrator/
 │   ├── project-planning/
+│   ├── shot-planning/
+│   ├── generation-execution/
 │   ├── asset-service/
 │   ├── rights-governance/
 │   ├── model-gateway/
@@ -2011,12 +2013,14 @@
 | **模块** | **核心数据所有权** | **允许写入者** | **主要对外契约** |
 | --- | --- | --- | --- |
-| Project & Planning | Project、Episode、Scene、Shot、ShotRevision、ConstraintPack、Intent、ShotSpec | Project/Planning Domain Service | Project API、Intent API、ShotChanged Event |
+| Project & Planning | Project、Episode、Scene、Intent（项目级意图，作用域拆分见 ADR-R27 §3.5） | Project/Planning Domain Service | Project API、Intent API（项目级） |
+| Shot Planning | Shot、ShotDraft、ShotRevision、ShotSpec、ShotVisualPlan、ConstraintPack、Intent（镜头级意图，作用域拆分见 ADR-R27 §3.5） | Shot/Planning Domain Service | Shot API、ShotChanged Event |
 | Blocking & Previz | PrevizVersion、CameraRig、ControlSignalAsset 元数据、Geometry/Purity Validation | Blocking Domain Service | Previz API、ControlSignal Events、PrevizVersionCreated Event |
 | Creative Intelligence | CreativeProposal、ExplorationRun、CreativeDecision、Pattern | Creative Service | Proposal API、Exploration Events |
 | Orchestration | WorkflowRun、Task、Dependency、GateInstance、Outbox/Inbox | Orchestrator Domain Service | Command API、Task/Gate Events |
 | Asset Management | Asset、AssetVersion、Manifest、Storage Metadata、BlockingAsset 文件版本 | Asset Service | Asset API、AssetVersionCreated Event |
 | Model Gateway | Provider、ModelCapability、ModelInvocation、ProviderJob | Model Gateway | Submit/Status API、Invocation Events |
+| Generation Execution | ProductionRoute、GenerationTask（生成任务） | Generation Domain Service | Route Select API、GenerationTask Events |
 | Quality & Governance | QCReport、DetectorRegistry、Benchmark、QualityProfile、RightsCase、Override | QC/Governance Service | Evaluate API、Governance Gate Events |
 | Memory & Learning | Memory、Canon Index、MemoryVersion、Feedback、Pattern Evidence | Memory Service | Memory Query/Write API |
 | Cost & Operations | CostLedger、Forecast、Capacity Snapshot、RCA、SLO Metrics | Cost/Ops/RCA Domain Services | Forecast/RCA API、Operational Events |
@@ -3427,7 +3431,7 @@
 ### 31.6 Action Registry 与 Agent 最小权限
 
-所有工作台、Agent API、内部 Skill、外部工具适配器和自动化测试必须引用同一份 Action Registry。当前共注册 75 项规范 action_key；……
+所有工作台、Agent API、内部 Skill、外部工具适配器和自动化测试必须引用同一份 Action Registry。当前共注册 83 项规范 action_key；……
@@ -3510,6 +3514,8 @@
 | CREATOR.RELEASE.REVOKE | CreatorRelease/ShareLink；creator_release/audience；CRITICAL | Token=Y；创作者本人、版权/安全职责人或受信 Rights Revocation Worker；撤销须有原因/依据/范围，吊销访问与下载凭证并生成影响清单。 | CREATOR_RELEASE_REVOKED；ACT-CR-NEG-008 越权/无依据撤销拒绝；PHASE_GATED（Phase 3） |
+| PROJECT.READ | Project；tenant/organization/project；LOW | Token=Y；项目管理员/制片/导演/编剧/QC 审核人；读取项目元数据与状态，不得绕过 RLS 租户边界。 | PROJECT_READ；ACT-PROJ-NEG-002 无读取权限角色访问项目拒绝；ACTIVE |
+| EPISODE.CREATE | Episode；project；MEDIUM | Token=Y；导演/编剧/制片；Episode 必须归属已存在 Project（复合归属校验），杜绝悬空引用。 | EPISODE_CREATED；ACT-EPISODE-NEG-001 越权登记集数拒绝；ACTIVE |
+| EPISODE.READ | Episode；project/episode；LOW | Token=Y；导演/编剧/制片/QC 审核人；读取集数元数据，归属与租户边界由复合外键与 RLS 保证。 | EPISODE_READ；ACT-EPISODE-NEG-002 无读取权限角色访问集数拒绝；ACTIVE |
+| SCRIPT.READ | ScriptRevision；project/episode；LOW | Token=Y；导演/编剧/制片/QC 审核人/版权合规；读取剧本版本元数据，不得绕过 RLS 租户边界。 | SCRIPT_READ；ACT-SCRIPT-NEG-004 无读取权限角色访问剧本版本拒绝；ACTIVE |
+| SCRIPT.LOCK.REQUEST | ScriptLock；project/episode；MEDIUM | Token=Y；导演/编剧；仅提交锁定申请（DRAFT→LOCK_PENDING）；申请人与批准人职责分离。 | SCRIPT_LOCK_REQUESTED；ACT-SCRIPT-NEG-003 无提交权角色申请锁定拒绝；ACTIVE |
+| ASSET.VERSION.READ | AssetVersion；tenant/organization/project；LOW | Token=Y；授权审核人/QC/导演（按职责）/制片；只读资产版本元数据与证据摘要，不得绕过 RLS 租户边界，不得返回存储凭证。 | ASSET_VERSION_READ；ACT-ASSET-NEG-003 无读取权限角色访问资产版本拒绝；ACTIVE |
+| PROJECT.STATUS.CHANGE | Project；tenant/organization/project；HIGH | Token=Y；项目管理员/制片；目标状态作参数，运行时按 Project 转移表校验；终态 ARCHIVED 不可原地复活。 | PROJECT_STATUS_CHANGED；ACT-PROJ-NEG-003 非法/越权状态转移拒绝；ACTIVE |
+| EPISODE.STATUS.CHANGE | Episode；project/episode；MEDIUM | Token=Y；导演/编剧/制片；目标状态作参数，运行时按 Episode 转移表校验；终态 ARCHIVED 不可原地复活。 | EPISODE_STATUS_CHANGED；ACT-EPISODE-NEG-003 非法/越权状态转移拒绝；ACTIVE |
```

## 三、逐条裁定

### 3.1 与已登记修订一致的部分（8 条 action，可直接销项）

副本新增的 8 行 action 与 `contract_amendments.yaml` 的 **A1（6 条）+ A2（2 条）完全一致**，
一字不差：

| 副本新增行 | 对应修订 |
| --- | --- |
| `PROJECT.READ`、`EPISODE.CREATE`、`EPISODE.READ`、`SCRIPT.READ`、`SCRIPT.LOCK.REQUEST`、`ASSET.VERSION.READ` | V205-R14-A1 |
| `PROJECT.STATUS.CHANGE`、`EPISODE.STATUS.CHANGE` | V205-R14-A2 |

这不是越权新增，是**有人把已批准的修订手工回填到正文表格里**。动机可以理解（让正文看起来完整），
但它绕过了唯一合法的正文修改流程（改正文 → 派生 → 重签），因此不能保留。

### 3.2 那个「差 1」到底是什么（这是本条最有价值的发现）

副本写「当前共注册 83 项」，而真实注册表是 84：

```
75（基线）+ 6（A1）+ 2（A2）+ 1（A3）= 84
                       ↑
                  副本漏掉的正是这条
```

`V205-R14-A3` 只登记了 1 个动作：`ASSET.STATUS.CHANGE`（APPROVED→GOLD 授权闭环）。
副本的回填停在 A2，A3 之后（A4–A11，对象/状态机/枚举类修订）都没有回填——所以「83」不是笔误，
而是**回填到一半的真实产物**：回填者数了自己加的行，写下了 83，然后停手了。

结论：正文里的自述数量一旦允许手工维护，就必然与真实状态分叉。这正是 `freeze_manifest`
新增 `check_baseline_self_reported_counts()` 的理由——不靠人核对，靠断言。

### 3.3 待所有者裁定的设计内容（D6 输入，不擅自采纳也不擅自丢弃）

以下 3 处不是已登记修订，属于「副本作者提出的设计变更」。它们**未经裁定**，因此既不能进正文，
也不该被当成错误抹掉。登记在案，交 D6 统一处理：

| # | 副本主张 | 与现状关系 | 需要谁裁定 |
| --- | --- | --- | --- |
| 1 | 架构树新增 `services/shot-planning/`、`services/generation-execution/` 两个服务域 | 仓库实际只有 `project_planning`，Shot 相关代码在 `project_planning` 内；两份服务域都不存在 | 所有者 + D7（Phase 1 主链落地时才知道要不要拆域） |
| 2 | 数据所有权表拆出 `Shot Planning` 行（Shot/ShotDraft/ShotRevision/ShotSpec/ShotVisualPlan/ConstraintPack/镜头级 Intent），`Project & Planning` 只留 Project/Episode/Scene/项目级 Intent | 与 ADR-R18（Shot 独立建模）、ADR-R16（ShotBreakdownDraft 文档级聚合根）方向一致，但「Intent 按作用域拆两半」是全新主张，正文未授权 | 所有者（作用域拆分是否成文，见 §3.4） |
| 3 | 新增 `Generation Execution` 行（ProductionRoute、GenerationTask） | `GenerationTask` 无 schema、无状态机；`PRODUCTION.ROUTE.SELECT` 的 resource_type 是 ProductionRoute | D6 与 D7（N-05 已登记该缺口） |

### 3.4 悬空引用 `ADR-R27 §3.5`

副本两处引用「作用域拆分见 ADR-R27 §3.5」，而 `docs/adr/` 当前只到 **R21**，`ADR-R27` 从未存在。

处置（ADR-R22 决策 D1-d）：

- **不补空号、不预留编号**。ADR 编号按实际签发顺序分配，先占号后补写等于制造第二个悬空引用；
- 「Intent 作用域拆分」若经所有者裁定确需成文，在 D3/D10 落地时按当时的实际编号签发正式 ADR，
  并在此处回填真实链接；
- 在此之前，该设计主张仅以本文件 §3.3 第 2 条的形式存在，属于**未裁定主张**，不是已生效决策。

## 四、这份归档怎么用

1. **不要**把本文件的 diff 直接应用回基线正文——正文修改必须走 ADR-R15 的流程；
2. D6 处理 30 项基线缺口时，§3.3 的三条主张要作为候选一并裁定，裁定通过的按修订登记后落地，
   裁定否决的在此标注 `REJECTED` 并写明理由（保留否决理由本身就是治理资产）；
3. 若将来正文确实需要包含 A1–A3 的动作表，正确做法是走修订流程 + `freeze_manifest --write`
   重签，而不是手工编辑。

## 五、验证

```bash
# 闸门现在会拒绝任何与基线同名的副本（不看内容，同名即歧义）
.venv/Scripts/python.exe scripts/freeze_manifest.py --check

# 确认仓库内已无同名副本
find . -name "AI影视工业化生产平台V2.0.5.md" -not -path "./.venv/*" -not -path "./.git/*"
# 期望：只有 docs/baseline/ 一处
```
