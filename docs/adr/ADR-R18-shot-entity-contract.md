# ADR-R18：稳定逻辑镜头的对象契约（Shot）

状态：已实施（契约层，2026-11-18；含字段归属裁决）；运行时未实施
关联：ADR-R17（ShotRevision）、ADR-R16（ShotBreakdownDraft）、ADR-R07（镜头域契约缺口）、ADR-023（支撑资产质量与成片质量分离）、31.5 状态机权威规则、31.6 Action Registry
上游依据：基线正文《AI影视工业化生产平台V2.0.5.md》8.1 / 8.2 / 8.3 / 8.12 / 31.6 / 33.2 / 33.4 + 项目所有者 2026-11-18《Shot 独立建模裁定》与《字段归属裁决》

## 结论

`Shot` 独立建模，作为 `Scene` 下稳定的逻辑镜头实体：

> `Shot` 负责镜头身份、业务编号和粗粒度生产进度；`ShotRevision` 负责镜头内容及其版本历史。
> 首次人工确认 `ShotDraft` 时，在同一事务中创建 `Shot` 和首个 `ShotRevision`；
> 后续修改只新增 `ShotRevision`，不重新创建 `Shot`。

Gate 3 / Gate 4 的通过状态**不进** `Shot.status`：审核决定归 `GateInstance`（`scope=shot`，绑定精确
`shot_revision_id`），画面锁定归 `PictureLock Snapshot`，正式交付归 `DeliveryPackage`。

## 为什么这一轮才建模

ADR-R17 当时把 `Shot` 留成显式缺口，理由是缺权威口径：8.3 给的是一个**合并示例**，`shot_id`、
`shot_revision_id`、`shot_code`、`status` 混在同一段 JSON 里，正文既没说明字段归属，也没给状态集
（唯一出现的 `IN_PRODUCTION` 只是示例值）。猜出来的边界会让每个下游对象跟着错。

项目所有者 2026-11-18 给出了明确裁定与状态集，`不猜契约` 的前置条件解除，本轮据此落地。
这不是「补一个 JSON Schema 就宣布镜头域闭合」——一次落四处，缺一处都算没闭合。

## 字段边界（逐条来源）

| 字段 | 归属 | 依据 |
|---|---|---|
| `shot_id` | Shot | 8.2「Shot 是稳定逻辑镜头」；`shot_id` 不得复用 |
| `scene_id` | Shot | 8.1「项目包含集，集包含场景，场景包含镜头」——归属链唯一事实源 |
| `project_id` | Shot | 租户作用域与 RLS 过滤键；受复合外键约束，不是第二套归属事实 |
| `shot_code` | Shot | 8.2「业务短编号仅用于展示」；8.3 示例 `EP001-SC003-SH012` |
| `shot_revision_id` | Shot | 当前正式版本指针；8.3 示例同处出现，语义上属于稳定实体 |
| `status` | Shot | 8.3 示例 `IN_PRODUCTION` + 所有者裁定五态 |
| `blocked_reason` | Shot | 所有者口径「BLOCKED 必须能够追溯阻塞原因」 |
| `rework_basis_ref` | Shot | 所有者口径「回落到 IN_PRODUCTION 必须有正式返工依据」 |
| `cancellation_ref` / `cancelled_at` | Shot | 所有者口径「已被授权移出本次生产范围」——授权须可指回 |
| `source_draft_ref` | Shot | 19.3 + ADR-R16：草案哪一版产出了这条镜头 |
| 信封 10 项 | Shot | C.3.1 通用实体信封 |

**不属于 `Shot` 的**：镜头描述、叙事意图、角色/场景/道具引用、创意意图（归 `ShotRevision`）；
硬约束与锁定维度（归 `ConstraintPack`）；Prompt、相机参数（归 `ShotSpec` 派生）；
Gate 3 / Gate 4 结论（归 `GateInstance` 与不可变交付产物）。

### 一个刻意的取舍：不存 `episode_id`

归属链是 `scene_id → Scene → Episode → Project`。只保留 `scene_id` 作锚点，`project_id` 作 RLS 键并
由复合外键 `(scene_id, project_id)` 绑定到场景。**不存 `episode_id`**：它是可由场景反查的派生值，
在 `Shot` 上再存一份就是第三套归属事实，必然漂移。

代价是查询「某集下所有镜头」需要 join 场景表。这个代价是明确买入的——漂移的代价比一次 join 高得多。

## 状态集（所有者设计决策，不冒充正文规定）

```
NOT_STARTED ──> IN_PRODUCTION ──> PRODUCTION_COMPLETE
     │               │  ↑                 │
     │               │  └──── 返工 ────────┘
     │               ↓
     ├──────────> BLOCKED ──> NOT_STARTED
     │               │
     └──────────> CANCELLED <─────────────┘
```

三个刻意的边界：

1. **不设 `IN_REVIEW`**：审核结论归 `GateInstance`。写进 `Shot.status` 就会出现「Shot 说通过了、
   Gate 拿不出证据」的双事实。
2. **不设 `REWORK`**：返工归 `ReworkTask`。获准返工后 `Shot` 回到 `IN_PRODUCTION`，
   `rework_basis_ref` 保留依据；把返工做成状态会让「返工中的镜头是否算生产中」永久含糊。
3. **不设 `DELIVERED`**：`PRODUCTION_COMPLETE` 只表示要求的生产产物已完成，**不代表** Gate 3 / Gate 4
   已通过。避免出现 `status=DELIVERED` 却无 Gate 4 证据的经典错位。

`BLOCKED → NOT_STARTED` 是合法回退：阻塞解除后任务可能整体重排而未启动，这既不是取消也不是已开始。

## 守卫落在哪一层

`Shot` 的状态证据分两层：

| 要求 | 落地层 | 说明 |
|---|---|---|
| BLOCKED 必有 `blocked_reason`，其余状态必须为空 | **SCHEMA** | `allOf` 条件必填 + 反向置空，双向都写 |
| CANCELLED 必有 `cancellation_ref` + `cancelled_at`，其余状态为空 | **SCHEMA** | 同上 |
| 非 `IN_PRODUCTION` 时 `rework_basis_ref` 必须为空 | **SCHEMA** | 反向约束可表达 |
| 从 `PRODUCTION_COMPLETE` 回到 `IN_PRODUCTION` 必须携带 `rework_basis_ref` | **RUNTIME + guards** | 正向必填依赖转移上下文，schema 表达不了 |

最后一条是本轮唯一「schema 表达不了」的守卫，因此写进 `state_machines.yaml` 的 `guards` 区，
并由新闸门 `check_state_machine_guards()` 钉住：`transition` 必须是该对象已登记的边，
`fields` 必须是 schema 里真实存在的字段。守卫不再只是自然语言注释——字段名写错、
指向不存在的边，闸门直接报红。

## 与 ADR-R17 的衔接

`ShotRevision.shot_id` 原先只是修订表内的稳定键，没有实体承载。现在它有了真实归属：

```
Shot（身份 + 编号 + 进度，稳定）
  └─ shot_revision_id ──► ShotRevision（内容，不可变）
                            └─ 同一 shot_id 的修订链沿 supersedes 递归
```

两条不变量互锁：`SH-2`（`Shot` 与同一 `shot_id` 的所有修订父级必须一致）与 `SR-5`
（修订间父级不漂移）共同保证归属不会在「实体侧」或「修订侧」单边漂移。

## 动作契约的一处改写

`SHOT.REVISION.CONFIRM` 的 `resource_type` 由 `ShotRevision` 改为 `Shot/ShotRevision`。

理由：首次确认**原子创建两个对象**。若只声明 `ShotRevision`，闸门只会校验修订，
新建的 `Shot` 即使缺必填字段也能通过——这正是「一头有契约、一头没人查」的翻版。
改成组合类型后，`check_active_resource_schemas()` 会要求两个对象都有 schema 且都被覆盖。

后续修订只新增 `ShotRevision`、仅前移 `Shot.shot_revision_id`，不重建 `Shot`——这一点写在
`approval_requirement` 里，避免实现者把「每次确认都建一个新 Shot」当成合理解释。

## 字段归属裁决：时长归 `ShotSpec`，时间线位置归 `TimelineDraft`

ADR-R17 把 `duration_ms` 与 `timecode` 放进了 `ShotRevision`（当时按 8.3 示例照抄）。经裁定，两者
**都不再属于 `ShotRevision`**，且不是「整体搬到 `ShotSpec`」，而是按对象边界拆开：

| 字段 | 唯一权威归属 | 裁决理由 |
|---|---|---|
| `duration_ms` | `ShotSpec.duration_ms` | 8.12 明定 `ShotSpec` 是镜头结构化描述的单一数据源，且示例已含 `duration_ms: 4200` |
| `timecode.start_frame` / `end_frame` | `TimelineDraft.clip_in_frame` / `clip_out_frame` | 8.3 的 `timecode` 描述的是「在某条时间线上的位置」，33.2 已用入出点表达该事实 |
| `timecode.fps_num` / `fps_den` | `TimelineDraft.fps_num` / `fps_den` | 33.4 要求序列时间基准用有理帧率保存 |
| `fps`（镜头自身） | `ShotSpec.fps` | 与序列帧率是两个概念：一个描述镜头规格，一个描述序列规格 |

### 为什么 `duration_ms` 必须归 `ShotSpec`

决定性证据是 8.12 的原文：「`ShotSpec` 是镜头结构化描述的单一数据源，必须版本化并绑定
`ShotRevision`。**不应分别维护冲突的镜头时长或切点**」。

若 `ShotRevision.duration_ms` 与 `ShotSpec.duration_ms` 并存，同一镜头就出现两个可互相矛盾的
权威值，而且没有任何规则能判定谁对——这正是单一数据源原则要消除的形态。因此 `ShotSpec.duration_ms`
是唯一权威值，`ShotRevision` 只保存叙事内容、描述与创意意图。

### 为什么 `timecode` 不能塞进 `ShotSpec`

`ShotSpec` 描述镜头**自身**的结构与执行规格；`timecode` 描述镜头在**某条序列**中的位置。同一个
`ShotRevision` 可以在不同时间线版本里被重排、裁剪或移位。若时间码进规格，一次剪辑重排就会被迫
产生新的镜头内容修订——版本链会被剪辑行为污染。

### 落点的一处修正：不新增 `start_frame` 作为镜头条目字段

所有者口径写作「`start_frame` / `end_frame` 归 `TimelineDraft` 的镜头条目」。但 33.2 的字段组表已经
用 **`shot_order` + `duration_per_shot_frames{}` + `clip_in_frame` + `clip_out_frame`** 表达排序、
每镜头帧数与入出点。若再引入 `start_frame` / `end_frame` 作为镜头条目字段，就会与 `clip_in_*`
构成同一时间范围的第二个事实源——与被裁决掉的重复完全同型。

因此迁入落点定为 `clip_in_frame` / `clip_out_frame`；`start_frame` / `end_frame` 保留给序列 timebase
与字幕、音频对齐、转场条目（33.2 中它们本来就是这些条目的字段）。映射关系写在
`TimelineDraft.x-afs-timecode-intake` 里，机器可读。

### 帧区间语义：左闭右开

所有帧区间一律 `[start, end)`，**`end` 不含末帧**，长度恒为 `end − start`。这条必须写死，否则
「`end_frame` 是否含末帧」会在剪辑、字幕、音频、导出四个模块里各有一个解释。

8.3 的例子恰好自洽：`240 → 366 @ 30/1` 是 `366 − 240 = 126` 帧，`126 / 30 s = 4200 ms`，与同一示例的
`duration_ms: 4200` 完全吻合。这也反证了 8.3 里的 `duration_ms` 与 `timecode` 本是同一事实的两处
表达——正是本轮要消除的重复。

### 承接字段必须先成为真契约，否则迁移是质量暗降

迁移前 `ShotSpec.duration_ms`、`fps`、`cut_points_frames` 与 `TimelineDraft` 的帧字段全是
`type: "string"`（从正文 YAML 块粗提取的壳），而 `ShotRevision.duration_ms` 是
`integer + minimum: 0`。直接把字段搬过去，等于把「双事实源」换成「单一无约束事实」——比原来更差。

所以本轮同步把承接方做实：`duration_ms` → `integer, minimum: 1`；`fps` → `{num, den}` 均为
`integer, minimum: 1` 且 `additionalProperties: false`；`cut_points_frames` → `array<integer>` 且
`minItems: 1`、`uniqueItems`；`TimelineDraft` 的七个帧字段全部落到 `integer` 约束，
`duration_per_shot_frames{}` 落到 `object<integer>`。不做这一步，迁移只是把约束丢掉。

## 历史版本与迁移

ADR-R17 已冻结，不回溯改写。收敛方式与迁移口径分开处理：

1. **历史保留可读。** `ShotRevision` 的两个字段标 `retired_at_schema_version: 2`，并登记
   `historical_ref: git:2192af9#/definitions/ShotRevision/properties/...`。旧版本记录按自身的
   `schema_version` 读取，不从当前 schema 反推；旧字段的定义可从该 commit 精确取回（有测试校验该
   引用可解析）。
2. **新写入收敛。** schema_version 2 起的 `ShotRevision` 不再接受这两个字段；`SR-11` 把「不含时长
   与时间线定位」写成 SCHEMA 级不变量，闸门可校验。
3. **时长迁移。** 旧 `duration_ms` 回填对应 `ShotSpec.duration_ms`；回填值与按帧换算值冲突时进入
   人工复核队列，不得静默覆盖。
4. **时间码迁移。** 只有能确定所属时间线版本的旧 `timecode` 才迁入该版本的镜头条目；无法确定归属的
   保留原值并写入迁移台账，不得猜测。

## 基线 8.3 示例待修订（不在本轮权限内）

8.3 的示例把 `duration_ms` 与 `timecode` 与 `Shot` 的身份 / 编号 / 状态混在同一段 JSON 里，容易让
读者以为它们是稳定 `Shot` 的直接字段。应改为分对象呈现（Shot / ShotRevision / ShotSpec /
TimelineDraft 各自一段）。

但基线文档已冻结：`frozen_inventory.yaml` 记录 `document_bytes: 489607` 与
`document_sha256: 9a9940c5…`，改一个字节即触发冻结漂移。**本轮不私改基线**，登记为基线待修订项，
由基线所有者在下一次基线修订时一并处理；在此之前，以本 ADR 与 `object_schemas` 的口径为准。

## 影响

- `object_schemas.schema.json` 新增 `Shot`（20 个属性 / 15 个必填），覆盖 81 → 82 个对象。
- `ShotRevision` 退役 `duration_ms` / `timecode`：属性 40 → 38，必填 32 → 31，新增 `SR-11` 与
  `x-afs-retired-fields`（含 git 历史引用）。
- `ShotSpec` 由空壳落为真契约：补 `description`、三字段真类型约束、`SS-1`~`SS-4` 与两条排除项。
- `TimelineDraft` 补 `description`、七个帧字段的整数约束与 `duration_per_shot_frames{}` 的 map 约束、
  `TL-1`~`TL-5` 与机器可读的 `x-afs-timecode-intake` 映射。
- `state_machines.yaml` 登记五态与 10 条转移 + 6 条守卫；总数 40 → 41。
- `enum_registry.yaml` 登记五态；对象数 37 → 38。
- `action_registry.yaml` 与 `openapi.draft.yaml`：`SHOT.REVISION.CONFIRM` 资源类型
  `ShotRevision` → `Shot/ShotRevision`，并说明首次原子创建语义。
- `contract_check.py` 新增 `check_state_machine_guards()`（守卫字段与转移必须真实可校验）。
- 契约修订 `V205-R14-A6` 记录 `Shot` 建模，`V205-R14-A7` 记录字段归属裁决与退役。

## 残余风险

1. **`Scene` 仍未建模。** `Shot.scene_id` 指向的 `Scene` 在 `object_schemas` 里没有定义——
   这是既有缺口（`ShotRevision.scene_id` 早已如此），本轮不扩张。后果是 `scene_id → Project`
   的归属一致性无法在 schema 层校验，只能靠运行时复合外键（`SH-2`，`RUNTIME_PENDING`）。
2. **没有运行时，也没有镜头表。** `SHOT.REVISION.CONFIRM` 未实现：原子创建、版本指针前移、
   状态推进、`shot_code` 项目内唯一约束全部无落点。八条不变量中只有 `SH-5`（状态证据）与
   `SH-8`（进度不越权）能由 schema 表达，其余六条标 `RUNTIME_PENDING`。
3. **`shot_code` 没有格式约束。** 不同项目的编号习惯不同（`EP001-SC003-SH012` 只是示例），
   强行加 pattern 会过约束。唯一性靠数据库 `(project_id, shot_code)` 唯一索引，不是 schema。
4. **审批事实仍无独立对象。** `cancellation_ref` / `rework_basis_ref` 指向的是 `ReworkTask` 与
   授权记录，但「谁在什么授权下批准了什么」仍无专用对象——与 AUTH-3 累积式会签同源。
   刻意不在此另造审批子对象，避免第三份审批事实源。
5. **跨对象时长换算没有取整与容差口径。** `ShotSpec.duration_ms` 与 `TimelineDraft` 的帧数必须能
   换算（`帧数 = duration_ms × fps_num / (fps_den × 1000)`），但 8.12 自身示例并不严格：`4200 ms`
   @ `24/1` 是 `100.8` 帧，而 `cut_points_frames` 末点是 `100`。因此实现方**不得**用严格相等做校验，
   必须先定「取整方向 + 允许偏差」的口径；在此之前 `SS-3` 与 `TL-3` 只能校验各自对象内部的单调性
   与区间差，跨对象一致性保持 `RUNTIME_PENDING`。
6. **`ShotSpec` 与 `TimelineDraft` 仍是部分空壳。** 本轮只做实了与本次裁决直接相关的字段
   （时长、帧率、切点、帧区间、每镜头帧数）；两对象其余字段仍为 `type: "string"`，且
   `TimelineDraft` 的 `required` 里混入了本应属于子条目的字段（`text` / `language` / `marker_type` /
   `track_ref`）与带花括号的键名 `duration_per_shot_frames{}`。属既有结构缺陷，独立整改，
   不在本轮扩权修改。
7. **五态是所有者设计，不是正文原文。** 下一版正文若给出不同状态集，须走契约修订对齐，
   而不是让实现与契约各自解释。
