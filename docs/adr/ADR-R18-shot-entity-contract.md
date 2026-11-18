# ADR-R18：稳定逻辑镜头的对象契约（Shot）

状态：已实施（契约层，2026-11-18）；运行时未实施
关联：ADR-R17（ShotRevision）、ADR-R16（ShotBreakdownDraft）、ADR-R07（镜头域契约缺口）、ADR-023（支撑资产质量与成片质量分离）、31.5 状态机权威规则、31.6 Action Registry
上游依据：基线正文《AI影视工业化生产平台V2.0.5.md》8.1 / 8.2 / 8.3 / 31.6 + 项目所有者 2026-11-18《Shot 独立建模裁定》

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

## 待裁定差异（本轮不动，明确登记）

所有者本轮口径为「镜头规格、时长、时间码、相机参数归 `ShotSpec` 等规格对象」，
但 **`duration_ms` 与 `timecode` 当前在 `ShotRevision` 里**（ADR-R17 落定，已成冻结契约，25 条测试守着）。

本轮**不偷偷改已冻结的 `ShotRevision`**：那需要一次独立修订、会影响 `ShotSpec` 与下游，
且与「本次只建模 Shot」的范围不符。差异在此登记，两条路径留待裁定：

- **路径 A（推荐）**：`duration_ms` / `timecode` 迁到 `ShotSpec`，`ShotRevision` 删除两字段。
  代价：`ShotSpec` 新增两字段、需要 `ShotSpec` 先有 `duration` 的权威口径、改动 A5 记录。
- **路径 B**：承认「内容时长」属修订、「时间线定位」属规格，`ShotRevision` 保留 `duration_ms`、
  `timecode` 迁出。代价：需要解释「同一个镜头的时长在修订里、时间码在规格里」是否会造成不一致。

在裁定前，两边都不能被实现方当成「另一个字段可以随便改」。

## 影响

- `object_schemas.schema.json` 新增 `Shot`（20 个属性 / 15 个必填），覆盖 81 → 82 个对象。
- `state_machines.yaml` 登记五态与 10 条转移 + 6 条守卫；总数 40 → 41。
- `enum_registry.yaml` 登记五态；对象数 37 → 38。
- `action_registry.yaml` 与 `openapi.draft.yaml`：`SHOT.REVISION.CONFIRM` 资源类型
  `ShotRevision` → `Shot/ShotRevision`，并说明首次原子创建语义。
- `contract_check.py` 新增 `check_state_machine_guards()`（守卫字段与转移必须真实可校验）。
- 契约修订 `V205-R14-A6` 记录增补与残余风险。

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
5. **`duration_ms` / `timecode` 归属待裁定**，见上。这是本轮唯一遗留的口径冲突，不是遗漏。
6. **五态是所有者设计，不是正文原文。** 下一版正文若给出不同状态集，须走契约修订对齐，
   而不是让实现与契约各自解释。
