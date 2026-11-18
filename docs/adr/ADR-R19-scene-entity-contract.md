# ADR-R19：场次对象契约（Scene）与引用目标闸门

状态：已实施（契约层，2026-11-18）；运行时未实施
关联：ADR-R18（Shot）、ADR-R17（ShotRevision）、ADR-R16（ShotBreakdownDraft）、ADR-R07（镜头域契约缺口）、31.5 状态机权威规则、C.3.1 通用信封
上游依据：基线正文《AI影视工业化生产平台V2.0.5.md》8.1 / 8.2 / 8.3 / C.3.1 + 项目所有者 2026-11-18 裁决（`UNIQUE (project_id, shot_code)` 与 `FOREIGN KEY (project_id, scene_id) REFERENCES scene(project_id, scene_id)`）

## 结论

`Scene` 建模为场次聚合根，**只承载身份、归属与生命周期**：

> `Scene` 负责场次身份、业务编号与启用/归档；场次描述性内容不进入本对象。
> 它是 `Shot.scene_id` / `ShotRevision.scene_id` 的引用目标，也是复合外键
> `(project_id, scene_id)` 的被引用对象。

字段只有四项业务字段：`scene_id`、`scene_code`、`project_id`、`episode_id`，其余为 C.3.1 十项信封。
状态集 `DRAFT → ACTIVE → ARCHIVED`，`ARCHIVED` 终态。

## 为什么这一轮才建模：一个被闸门结构性地漏掉的缺口

真实情况不是「忘了写」，而是「没有任何机制能发现它被忘了」：

| 事实 | 后果 |
| --- | --- |
| `Shot.scene_id`、`ShotRevision.scene_id` 指向 `Scene` | `Scene` 没有 schema，引用悬空 |
| 已有闸门只校验 `action_registry.resource_type` 引用的类型 | 字段级「对象指向对象」从未被检查 |
| `resource_schema_gaps.yaml` 也只按 `resource_type` 记账 | `Scene` 连「缺口」都不是，是「不存在」 |
| 三闸门全绿 | 悬空引用只能靠人肉比对发现 |

`Scene` 因此同时是**被引用对象缺失**和**缺口台账看不见**的叠加结果。只补一个 schema
解决前一半，后一半会在下一个对象上重演（`GateInstance`、`AuditEvent` 已经在排队）。
所以本轮同时新增引用目标闸门，见下文。

## 字段边界（逐条来源）

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| 信封 10 项 | C.3.1 | `status` 必填；未知枚举值必须拒绝，不得静默映射 |
| `scene_id` | 8.2「scene_id 不得复用」 | 复合外键被引用列，也是 `Shot` / `ShotRevision` 的引用目标 |
| `scene_code` | 8.2「业务短编号仅用于展示」+ 8.3 | 8.3 的 `shot_code: EP001-SC003-SH012` 中 `SC003` 即场次编号；建议形态 `EP001-SC003`，项目内唯一但不作关联主键 |
| `project_id` | 所有者裁决 | 复合外键 `(project_id, scene_id)` 的成员列，同时是 RLS 过滤键 |
| `episode_id` | 8.1「集包含场景」 | **唯一**归属锚点，向上经 `Episode` 收敛到 `Project` |

### 刻意不存的字段

| 字段 | 归属 | 理由 |
| --- | --- | --- |
| `scene_name` / `location` / `time_of_day` / `scene_goal` / `core_event` / `entry_state` / `exit_state` | `ShotBreakdownDraft.scene_breakdowns[]` | 场次描述性内容已有落点（ADR-R16）；复制到稳定实体就是第二个事实源 |
| `shots[]` / `shot_ids[]` | `Shot.scene_id` 反查 | 存数组就是第二份归属事实 |
| `scene_order` | 剧本场次序列 + `scene_code` | 正文未给场次排序规则；先造一个排序字段就与 `scene_code` 形成两个顺序事实 |
| `constraint_pack_id` | `ConstraintPack` 自身 scope | 8.1 已规定约束包自己记录绑定对象 |
| `gate_result` / `total_duration_ms` | `GateInstance` / `ShotSpec` | 审核结论与时长各有权威归属 |

## 状态集：为什么镜像 `Episode`

正文对 `Scene` 只有层级关系一句话，没有任何状态集。三条可选路径：

| 路径 | 问题 |
| --- | --- |
| `status` 留裸 `string` | 直接违反 C.3.1「未知枚举值必须拒绝，不得静默映射」——任何字符串都能通过 |
| 为场次造一套新状态 | 同层级聚合各有一套命名，实现方要为每层单独学习，跨层校验无法复用 |
| 镜像 `Episode`（本选择） | 不引入任何新语义；`DRAFT` / `ACTIVE` / `ARCHIVED` 与集数逐字相同 |

刻意不设的三种状态，理由与 `Shot` 同源：

- 不设 `IN_REVIEW`：审核结论归 `GateInstance`（`scope=scene` 时绑定精确对象版本）。
- 不设 `LOCKED`：锁定是剧本版本与资产的语义，场次没有可锁定的对象。
- 不设任何生产进度态：那是 `Shot` 的职责。

登记时标注 `source_kind: derived_from_parent_lifecycle` + `registration_status: PENDING_PHASE1_REVIEW`，
不冒充正文规定。正文若给出不同状态集，须走契约修订对齐，不得让实现与契约各自解释。

### 一处必须说清的不一致

`Scene.status` 是 `enum`，而 `Project` / `Episode` / `ScriptRevision` 的 `status` 仍是裸 `string`——
同为层级聚合，口径不同。原因是可验证的：

- 实测 41 个有状态机的对象里，**32 个**的 `status` 是裸 `string`；只有 9 个收了 `enum`。
- `Scene` 是新对象，没有历史数据，可以从第一天闭合；另外三个是既有对象，改成 `enum`
  属于一次覆盖 32 个对象的统一迁移，混在本轮里做等于把「补一个缺口」变成「改半个契约」，
  且无法逐个验证语义（部分对象的状态集本身还有别名层与旁支状态需要先裁定）。

本轮不夹带，登记为下一项独立工作（见「未闭合」第 4 条）。

## `additionalProperties: false` 是新对象才有的余量

历史对象都用 `true`（82 个对象一次性收紧不现实），`Scene` 从第一天就用 `false`。
这不是风格偏好，而是让 `SC-6`（内容不越权）成为**真正的 SCHEMA 级不变量**：
多塞一个 `scene_name` 在契约层就会被拒绝，而不是靠文档承诺「我们不加字段」。
若哪天要给场次加字段，必须走契约修订——契约先行在这里是能执行的，不是口号。

## 新增闸门一：引用目标解析（`check_reference_targets`）

把引用意图显式声明成 `x-afs-reference-targets`（字段 → 目标对象），逐条解析：

1. 声明的字段必须真的在该对象 `properties` 里（防声明腐化）；
2. 声明的目标必须是已定义对象（悬空引用报红）；
3. 目标缺失时正确处置是补 schema 或登记缺口台账，**不得删声明**——删声明只会让
   悬空引用重新变隐形，这正是本闸门要防的动作。FAIL 文案里写死了这句。

当前声明 24 条、覆盖 4 个对象（`Shot` 6 / `ShotRevision` 10 / `ShotBreakdownDraft` 5 / `Scene` 3）。

### 为什么不用命名自动推导

实测全 schema 有 475 个 `*_id` / `*_ref` 类字段，其中只有 82 个按命名能解析到已定义对象，
393 个解析不到——但绝大多数是 `entity_id`、`audit_ref`、`<自身对象>_id` 这类**非对象引用**。
按命名自动推导会产出 393 条误报，闸门会立刻被关掉，等于没做。

因此只对场次域与镜头域显式声明。**覆盖边界写在 PASS 输出里**（当前 456 个未声明），
不制造「全部引用都已校验」的假象。

## 新增闸门二：枚举覆盖（`check_enum_registry_coverage`）

`enum_registry.yaml` 自称「由 state_machines 派生」，实测却是 41 个对象 vs 38 个：
`Project` / `Episode` / `ScriptRevision` 有状态机、从未登记，没有任何闸门比对两个文件的对象集。
本轮补齐三条（另加 `Scene`），并新增闸门比对**对象名集合**。

只比对象名、不比状态集，是刻意的：注册表允许收录转移链尚未覆盖的旁支状态
（如 `AssetVersion` 的 `QUARANTINED`、`Task` 的 `WAITING_FOR_PROVIDER`），
强行要求状态集相等会把合法设计判死。

## 创建入口未定（本轮不擅自扩大语义）

`Scene` 由谁创建，正文没写，所有者本轮也只裁定了「独立建模 `Shot`、首次确认原子创建
`Shot` + `ShotRevision`」，没提场次。因此本轮**没有**把 `SHOT.REVISION.CONFIRM` 的
`resource_type` 扩成 `Shot/ShotRevision/Scene`——那等于替所有者决定「确认拆镜即建立正式场次」。
这需要在 `Scene` 运行时设计时一并裁定：确认动作是否原子创建 `Scene`、`DRAFT` 由谁推进到
`ACTIVE`、归档前如何确认其下没有 `IN_PRODUCTION` 的镜头。

## 未闭合（显式登记，不是遗漏）

1. **`Scene` 运行时全无**：无 ORM、无迁移、无 service、无 action。所有者要求的
   `FOREIGN KEY (project_id, scene_id)` 与 `UNIQUE (project_id, shot_code)` 仍是纸面约束。
2. **创建入口未定**，见上一节。
3. **稳定场次是否应持有内容、是否需要 `SceneRevision`**：正文未定义，待裁定。
4. **32 个对象的 `status` 仍是裸 `string`**：状态机有状态集、schema 不约束，属同一类
   「声明与可校验脱钩」，需要一次独立迁移。
5. **`GateInstance` / `AuditEvent` 仍无 schema**：后者被信封 `audit_ref` 在 82 个对象上引用。
6. **456 个引用类字段未声明**：引用闸门只覆盖场次域与镜头域。

## 影响

- `object_schemas.schema.json` 新增 `Scene`（14 属性 / 14 必填，`additionalProperties: false`），
  覆盖 82 → 83 个对象；`Shot` / `ShotRevision` / `ShotBreakdownDraft` 新增 `x-afs-reference-targets`。
- `state_machines.yaml` 登记 `Scene` 三态两转移（`DRAFT→ACTIVE`、`ACTIVE→ARCHIVED`、`DRAFT→ARCHIVED`），
  总数 41 → 42。
- `enum_registry.yaml` 补 `Scene` / `Project` / `Episode` / `ScriptRevision`，对象数 38 → 42。
- `contract_check.py` 新增 `check_reference_targets()`、`check_enum_registry_coverage()`。
- 契约修订 `V205-R14-A8` 记录增补与残余风险。
