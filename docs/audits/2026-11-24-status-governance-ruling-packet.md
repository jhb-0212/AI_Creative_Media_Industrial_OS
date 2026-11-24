# #35 状态治理后续：所有者裁定清单

> 只读取证，未改任何契约事实源、迁移、生产代码或冻结台账。
> 基线：`main @ c439ad3`（PR #66 合并后），取证于 2026-11-24。

## 0. 结论先行

1. 两项双向闸门当前全绿：`gen_status_contracts --check` PASS；`contract_check` 报
   `47 lifecycle + 18 占位 + 18 缺口`，`状态枚举一致` PASS。
2. 三项债务都是**显式登记**的，不是闸门漏检。它们的解除条件全部写成了
   `unblocks_when`，而其中绝大多数要求「所有者裁定语义」。按项目红线
   （无裁定私改契约事实源），助手不能自行销项。
3. 因此本轮只做一件事：把当前 main 上的真实债务范围、分组和所需裁定摆出来，
   请所有者给顺序与批量授权。

## 1. 口径校正（与任务描述、历史调查的差异）

| 来源 | lifecycle | envelope_only | lifecycle_gap | 缺失 Schema |
| --- | --- | --- | --- | --- |
| 任务 #35 描述 | — | 18（写的是 18） | 19 | 未列数 |
| 调查任务 #167 结论 | — | 17 | 19 | 11 |
| 台账文件头部/小节标题 | 41 / 40 | 17 / 17 | 18 / 19 | — |
| **本次从权威台账派生（实际）** | **47** | **18** | **18** | **8** |

派生方式：

```python
import yaml
d = yaml.safe_load(open("contracts/enums/status_field_taxonomy.yaml", encoding="utf-8"))
Counter(o["class"] for o in d["objects"])   # lifecycle 47 / envelope_only 18 / lifecycle_gap 18
len(d["schema_gaps"])                        # 4
```

缺失 Schema 的 8 项由三处显式台账合并去重得到：

- `status_field_taxonomy.yaml: schema_gaps` 4 项：
  `PublicationReview / CreatorShareLink / Pattern / SharedReferenceAsset`（有状态机无 schema）
- `runtime/resource_schema_gaps.yaml` 3 项：
  `CanonRevision / RightsCase / RightsManifest`（ACTIVE 动作引用无 schema）
- `runtime/resource_schema_exemptions.yaml` 1 项：
  `ScriptLock`（豁免理由已说明它不是独立对象，通常不需要建模，但必须保留在判据内）

> `contract_amendments.yaml` 中的「39+18」「17 个 envelope_only」「19 个 lifecycle_gap」
> 是 A9/A10/A21 的历史问题陈述，属时间胶囊，**不因其与当前数字不符而回改**。

同时发现一处无语义的文案漂移：`contracts/enums/status_field_taxonomy.yaml` 的头部注释
与小标题把当前计数写成 41/40、17、18/19，与实际派生的 47/18/18 不一致。该文件非冻结
产物，建议在 #35 开工时一并改为「计数由 `contract_check` 派生输出」，不再在注释里写死数字。

## 2. G-B：status_split 权威字段（3 项，语义杠杆最高）

来源：`contracts/enums/field_authority.yaml`。三者的信封 `status` 均已退役为哨兵，
真正缺的是权威字段的值集：

| 对象 | 权威字段 | 需要所有者裁定 |
| --- | --- | --- |
| `ExternalToolAdapterManifest` | `lifecycle_status` | 完整允许状态集 + 转移边（含暂停/回滚/退役的字面值） |
| `ModelInvocation` | `submission_status` / `result_status` / `billing_status` | 三字段各自值集 + 交叉组合约束，尤其是 `SUBMISSION_UNKNOWN` 与结果/计费的规则 |
| `QCIssue` | `human_decision` / `disposition` | 是否各建 enum；与 `ReworkTask` 的联动不变量 |

## 3. G-A：lifecycle_gap 18 项

`unblocks_when` 已把每项的解除路径写在台账里。按「是否需要新造状态集」分三组，
便于批量裁定而不逐项重复讨论：

### 3.1 可同构复用既有权威状态机（6 项）

| 对象 | 台账给出的形状线索 | 建议裁定 |
| --- | --- | --- |
| `BlockingAsset` | 与 `AssetVersion` 8.5 同形，但隔离/纯净度判定不同 | 复用 AssetVersion 状态集 + 差异另列字段约束 |
| `ContinuityConstraint` | 可能与 `ConstraintPack` 共享状态集 | 复用或独立，需明确单条失效是否影响包 |
| `ControlSignalAsset` | 与 `AssetVersion.QUARANTINED` 语义对齐 | 复用 AssetVersion 状态集 |
| `ExternalToolArtifact` | 暂存→扫描→验证→转正/废弃 | 与 AssetVersion 的 QUARANTINED 对齐，避免两套 |
| `ShotResourceAllocation` | 与 `QuotaReservation` 同构 | 复用 `REQUESTED→RESERVED→COMMITTED/RELEASED` |
| `ShotTierChangeRequest` | 与 `ScriptRevision.LOCK_PENDING` 同形 | 复用 `DRAFT→PENDING→APPROVED/REJECTED` + 职责分离 |

### 3.2 需「二选一归类」（5 项）

| 对象 | 二选一 | 影响 |
| --- | --- | --- |
| `CameraRig` | 自建状态链 / 随 `PrevizVersion` 版本化（后者改 envelope_only + child_structure） | 分类改写 |
| `CharacterReferenceSet` | 自建权利校验状态 / 归 `CharacterCard` 组成部分（后者改 child_structure） | 分类改写 |
| `ProjectProductionProfile` | 策略版本生效/取代状态 / 只追加不改写（后者改 immutable_record） | 分类改写 |
| `ShotProductionProfile` | 同上 | 分类改写 |
| `TimelineManifest` | 自建版本状态 / 只是 `TimelineDraft` 快照（后者改 envelope_only + derived_projection） | 分类改写 |

### 3.3 需明确新状态集（7 项）

| 对象 | 台账给出的处置链 |
| --- | --- |
| `CharacterVoiceProfile` | 授权生效 / 撤销 / 过期 |
| `CreativeProposal` | 待评审 → 采纳 / 否决（不得复用 ShotVisualPlan 命名掩盖语义差异） |
| `DeliverySpec` | 草案 → 审批 → 冻结 / 取代 |
| `EscapeReport` | 发现 → RCA → 返工 → 关闭 |
| `IntentContract` | 批准前后语义不同，需批准与变更口径 |
| `Report` | 待处置 → 已处置（与 Appeal 申诉链衔接） |
| `ToolExchangePackage` | 有效 / 过期 / 撤销（与 ToolExchangeSession 的 EXPORTED 事件衔接） |

## 4. G-C：缺失 Schema（8 项）

| 对象 | 登记位置 | 需裁定/建模内容 |
| --- | --- | --- |
| `PublicationReview` | schema_gaps | 审核人、规则版本、证据、原因、决定时间；审核人与提交人分离的字段级约束 |
| `CreatorShareLink` | schema_gaps | 水印、有效期、受众限制、撤销依据（与 31.5 `ShareLink` 不是同一对象） |
| `Pattern` | schema_gaps | **二选一**：与 `PatternRecord` 合并为一条状态机，或确属两个对象并说明字段差异 |
| `SharedReferenceAsset` | schema_gaps + text_authority_only | 建模后从 `text_authority_only` 移入 `objects` 并登记状态机 |
| `CanonRevision` | resource_schema_gaps | 设定变更授权链与冲突标记字段 |
| `RightsCase` | resource_schema_gaps | 权利案件主体与证据引用字段 |
| `RightsManifest` | resource_schema_gaps | 被 `rights_manifest_ref` 引用，却一直未定义 |
| `ScriptLock` | resource_schema_exemptions | 豁免理由成立则保留，不建模；仅需在闸门内保持可机检 |

## 5. envelope_only 18 项：多数无需动作

按 basis 派生：`child_structure` 7 / `derived_projection` 2 / `immutable_record` 6 /
`status_split` 3。前 15 项的 `unblocks_when` 都要求「把子结构提升为独立聚合根」或
「允许改写不可变记录」——这些是把权威语义改坏的方向，保持现状才是正确销项。
**真正需要动作的只有第 2 节那 3 项 status_split。**

## 6. 推荐顺序与依赖理由

1. **先 G-B（3 项权威字段值集）**：这是「一个状态事实只有一个权威写入位置」的唯一
   现存违例面，且值集一旦定了，后续 Schema/CHECK 才有唯一来源。
2. **再 G-A（18 项状态机）**：按 3.1 同构复用 → 3.2 归类二选一 → 3.3 新状态集的顺序
   批量处理。同构复用不需要逐项发明新命名，风险最低；归类二选一会改写分类，需要在
   同一修订里同步台账、Schema enum 与冻结重签。
3. **最后 G-C（8 项 Schema）**：Schema 的 `status.enum` 由状态机派生，所以落在语义裁定
   之后才不会二次返工。`Pattern` 的合并/分立裁定需单独给。

每一步都必须：先改权威事实源 → 重跑 `gen_status_contracts --check` 与 `contract_check` →
按 ADR-R15 登记修订 → 重签字段签名与 phase0 冻结清单 → 单独 PR。

## 7. 请所有者给出

- A. 是否按上述 ①②③ 顺序推进；
- B. 3.1 的 6 项是否授权「按台账形状线索批量复用既有状态机」（不逐项另造命名）；
- C. 3.2 的 5 项与 `Pattern` 的归类，是逐项裁定还是一并授权按 `unblocks_when` 的
  第二选项（改为 envelope_only / immutable_record / 与 PatternRecord 合并）执行；
- D. 3.3 的 7 项新状态集，是否可用正文给出的处置链字面值作为权威集；
- E. #13 是否维持 blocked（推荐：维持，先解开 #35 再谈交付链入口与审批事实契约）。
