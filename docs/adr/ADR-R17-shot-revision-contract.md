# ADR-R17：镜头内容修订的对象契约（ShotRevision）

状态：已实施（契约层，2026-11-18）；运行时未实施
后续：本文第「未做的一件事：Shot 本身没有建模」所述缺口已由 ADR-R18 闭合（Shot 独立建模，2026-11-18 项目所有者裁定），该节保留作为当时的决策记录，不再代表当前契约状态。
后续修订：`duration_ms` 与 `timecode` 已于 `schema_version 2` 退役（ADR-R18 字段归属裁决）——时长归 `ShotSpec.duration_ms`，时间线位置归 `TimelineDraft.clip_in_frame / clip_out_frame`。本文表格中的这两个字段是当时的决策记录，保留原样不改写；**当前契约以 `object_schemas.schema.json` 与 ADR-R18 为准**。
关联：ADR-R16（拆镜草案 ShotBreakdownDraft）、ADR-R07（镜头域契约缺口）、ADR-023（支撑资产质量与成片质量分离）、31.6 Action Registry、31.5 状态机权威规则
上游依据：基线正文《AI影视工业化生产平台V2.0.5.md》8.1 / 8.2 / 8.3 / 8.8 / 2.3 / 4.1 / 19.3 / 31.6 / Gate 1-A

## 结论

`ShotRevision` 落为**不可变的镜头内容版本快照**：`Shot` 是稳定逻辑镜头，`ShotRevision` 保存每次内容修订，
被任务、`ShotSpec`、`PrevizVersion` 引用的版本固定不变，任何修改一律新建修订，禁止覆盖。

状态两态：`ACTIVE → SUPERSEDED`。`SHOT.REVISION.CONFIRM` 是唯一创建入口（人类确认、版本冲突必须拒绝）。

这正是 ADR-R16 遗留的那一环：草案确认产出的 `confirmed_shot_revision_refs` 现在指向真实对象，
`ShotSpec` 的 `required.shot_revision_id` 也不再是悬空引用。

## 字段从哪来（逐条可查，不是拼凑）

| 字段组 | 字段 | 正文依据 |
|---|---|---|
| 身份与归属 | `shot_revision_id`、`shot_id`、`project_id`、`episode_id`、`scene_id` | 8.1（项目⊃集⊃场⊃镜头）、8.2（Shot 是稳定逻辑镜头；标识不得复用）、8.3 示例 |
| 修订内容 | `description`、`narrative_intent`、`duration_ms`、`character_refs`、`location_ref`、`prop_refs` | 8.1（绑定镜头描述、约束、创意意图）、8.3 示例 |
| 约束 | `constraint_pack_applicability`、`constraint_pack_id` | 8.1（绑定约束）、8.3、D.3 主流程「如适用；无锁定约束镜头标记 NOT_APPLICABLE」 |
| 创意授权 | `creative_autonomy` | 8.3、2.3（L1–L4） |
| 风险与质量 | `risk`、`quality_profile` | 8.3、782 行（LOW/MEDIUM/HIGH/CRITICAL）、4.1（UNIFIED_HIGH_QUALITY） |
| 意图固定引用 | `intent_ref.intent_id` / `intent_version` | 8.8（ShotRevision 引用固定的 intent_id + intent_version） |
| 可追溯 | `basis_script_revision_id`、`script_anchors`、`provenance` | 19.3（保留来源段落、模型/Skill 版本和差异） |
| 版本链 | `revision_no`、`supersedes_shot_revision_id`、`change_summary`、`changed_fields` | 8.2（保存每次内容修订）、2635 行（保留父版本、差异摘要、审批记录、来源引用；禁止覆盖式合并） |
| 确认事实 | `confirmed_by`、`confirmed_at` | 31.6（人类确认）、Gate 1-A（授权确认人、版本与差异） |

> 表中「修订内容」行的 `duration_ms` 与 `timecode` 已按 ADR-R18 迁出（`schema_version 2`）：前者归
> `ShotSpec`，后者归 `TimelineDraft` 的镜头条目。保留在此作为当时的来源记录，不代表当前写入契约。

## 关键取舍

1. **两态，不是多态。** 正文从未给 `ShotRevision` 单列状态集合。两态是从不可变版本语义推出的最小必要集：
   一个修订要么当前有效，要么已被同一 `shot_id` 的更高 `revision_no` 取代。没有 `DRAFT` 态——
   草案态属于 `ShotBreakdownDraft`，确认动作直接产出正式版本（19.3「创建正式 ShotRevision」）。
   宁可只登记两态，也不虚构 `IN_PRODUCTION` 之类的进度状态。
2. **`shot_code` 与生产进度状态不进本对象。** 二者在 8.3 的示例 JSON 里出现，但它们属于 `Shot`
   而非「内容修订」：改一个展示短编号不该产生新的内容版本；把 `IN_PRODUCTION` 写进不可变修订，
   会让进度推进被迫新建修订，版本链立刻失去意义。这是本轮唯一一处对 8.3 示例的**主动拆分**，
   理由与遗留问题见下文。
3. **`constraint_pack_applicability` 是显式枚举。** 「无锁定约束的镜头」必须标 `NOT_APPLICABLE`，
   不能靠留空表达：留空既可能是「没有约束」也可能是「忘了绑」，而这两种情况在 Gate 2 的判断完全相反。
4. **`quality_profile` 收窄为单值 `UNIFIED_HIGH_QUALITY`。** 4.1 规定所有 S/A/B 项目绑定同一逻辑质量配置；
   把 `SUPPORTING_ASSET_QUALITY` / `SHOWCASE_QUALITY` 堵在门外，正是 ADR-023 要防的
   「拿白模标准衡量成片、拿控制信号达标冒充画面达标」。
5. **版本链而不是历史数组。** 与 ADR-R16 同一口径：历史是查询结果，不是第二份事实。
   代价是回放要沿 `supersedes_shot_revision_id` 递归，需要在该列建索引。
6. **`intent_ref` 必填。** 8.8 是硬要求，不是建议。确实没有意图来源时，必须先建 Intent 版本，
   不能留空——留空会让「这个镜头为何这样拍」永久失去依据，而这正是后续 QC 判定艺术偏差的锚点。

## 与 ADR-R16 的一处补正

ADR-R16 把确认的产物写成 `confirmed_shot_revision_refs`（复数），并注明「ShotRevision 仍无 schema，
引用指向尚未建模的对象」。本 ADR 补上对象后，这条链闭合：

```
ShotBreakdownDraft（一版草案，人工确认）
  └─ confirmed_shot_revision_refs[] ──► ShotRevision（每镜一条，不可变）
                                          └─ ShotSpec.shot_revision_id ──► ShotSpec
```

`ShotRevision.revision_no` 在同一 `shot_id` 内单调递增，`source_draft_ref` 指回产生它的草案版本，
因此「草案哪一版 → 确认出哪几条修订」可双向追。

## 未做的一件事：Shot 本身没有建模

`Shot` 是 8.2 明文承认的稳定实体（「Shot 是稳定逻辑镜头」），但本轮**不建它的契约**，原因是缺权威口径：

- 8.3 给的是一个**合并示例**：`shot_id` 与 `shot_revision_id` 同时出现，`status: IN_PRODUCTION` 与
  `quality_profile` 混在同一段 JSON 里，没有说明哪些字段属于稳定实体、哪些属于修订；
- 正文没有给 `Shot` 的状态集合（唯一出现的 `IN_PRODUCTION` 只是一个示例值，不是枚举）；
- 拆分归属是需要业务裁定的事，猜出来的边界会让每个下游对象都跟着错。

因此本轮把 `shot_id` 作为稳定身份键，归属四元组（`shot_id` / `project_id` / `episode_id` / `scene_id`）
在修订表内自洽（不变式 SR-5：同一 `shot_id` 的所有修订父级不得漂移）。这不依赖一张不存在的表就能保证一致性。
`Shot` 的对象契约与状态机作为显式缺口登记，等正文或所有者给出状态集再补。

## 影响

- `object_schemas.schema.json` 新增 `ShotRevision`（40 个属性，32 个必填），覆盖台账 81 个对象。
- `state_machines.yaml` 与 `enum_registry.yaml` 登记两态；状态机总数 39 → 40，枚举对象 36 → 37。
- `resource_schema_gaps.yaml` 删除 `ShotRevision` 条目（补了 schema 仍挂债务会被判陈旧债务），剩余 7 项。
- `action_implementations.yaml` 中 `SHOT.REVISION.CONFIRM` 的理由改为「契约已就绪、缺修订表与版本冲突拒绝」，
  不再声称「缺版本表才能拒绝冲突」这种前半句对、后半句推不出的说法。
- 契约修订 `V205-R14-A5` 记录增补与残余风险。
- 后续修订（ADR-R18，`V205-R14-A7`）：退役 `duration_ms` 与 `timecode`，属性 40 → 38、必填 32 → 31，
  新增 `SR-11` 与 `x-afs-retired-fields`；`ShotSpec` / `TimelineDraft` 同步承接并做实类型约束。

## 残余风险

1. **`Shot` 未建模**：如上所述。当前 `shot_id` 只是修订表内的稳定键，没有独立实体承载
   `shot_code`、生产进度与镜头级生命周期。这是本轮**主动留出的缺口**，不是遗漏；
   补它需要所有者先裁定 8.3 示例的字段归属。
2. **没有运行时，也没有修订表**：`SHOT.REVISION.CONFIRM` 未实现，`revision_no` 分配、
   版本冲突拒绝、`ACTIVE→SUPERSEDED` 推进都无落点。十条不变量中只有 SR-4（版本链闭合）与
   SR-6（不越权定约束）能由 schema 表达，其余八条标 `RUNTIME_PENDING`，不得视为已落地。
3. **审批事实仍无独立对象**：`confirmed_by` / `confirmed_at` + 信封 `audit_ref` 承载确认事实，
   但「谁在什么授权下批准了什么」没有专用对象。这与 AUTH-3 累积式会签同源——缺的是审批事实契约，
   不是缺字段。设计上刻意不在此处再造一个 `approval` 子对象，避免出现第三个审批事实源。
4. **正文 8.3 的合并示例仍与契约不一致**：契约把 `shot_code` 与 `IN_PRODUCTION` 划给 `Shot`，
   正文没有明说。本次不改基线正文（改正文等于改所有者文档指纹），差异在 A5 中登记，
   待下一版正文明确 Shot 与 ShotRevision 的字段边界。
   后续：其中 `duration_ms` / `timecode` 的归属已由 ADR-R18 裁决（分别归 `ShotSpec` 与 `TimelineDraft`），
   `shot_code` / `IN_PRODUCTION` 归 `Shot` 已由 ADR-R18 落为契约；剩下的只有 8.3 示例本身的表述，
   登记为基线待修订项。
5. **`quality_profile` 单值枚举可能过窄**：若将来引入镜头级质量档（例如特殊镜头单独升档），
   需要走契约修订扩展枚举，而不是在实现里偷偷放过其他值。
