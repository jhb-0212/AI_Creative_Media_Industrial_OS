# ADR-R20：状态枚举单一事实源（allowed_states → 派生 → 四层闸门）

状态：已实施（契约层 + 四张已落库表的运行时收口，2026-11-18）
关联：ADR-R19（Scene / 引用目标闸门）、ADR-R18（Shot）、ADR-R17（ShotRevision）、ADR-R08（资产生命周期旁支）、31.5 状态机权威规则、C.3.1 通用信封、V205-R14-A9
上游依据：项目所有者 2026-11-18 立项指令（「状态枚举统一迁移，批准立项；第一步是从当前 main 重新生成准确的状态枚举清单」）

## 结论

状态集合从此只有一个权威源：

```
contracts/state_machines/state_machines.yaml   objects[].allowed_states
        │
        ├─► contracts/enums/enum_registry.yaml             （整份生成）
        └─► object_schemas.schema.json  各对象 status.enum  （按块生成）
                 │
                 └─► Runtime：Domain Enum + 数据库 status CHECK（仅已落库对象）
```

任何一层与权威集合不一致，`scripts/contract_check.py::check_status_enum_consistency`
与 `scripts/gen_status_contracts.py --check` 都会失败。派生结果不允许手改——闸门的
实现就是「重新生成一遍，逐字节比对」。

## 为什么这一轮必须先重建清单

上一轮（ADR-R19）留下的数字是「41 个状态机对象 / 32 个裸 `status` / 9 个收 `enum`」。
在 `main` 上重新数一遍，三个数都不对：

| 口径 | ADR-R19 记录 | 当前 main 实测 |
| --- | --- | --- |
| 状态机对象数 | 41 | 42（含一条 `CharacterCard / CharacterAppearance` 合并条目，不是合法对象名） |
| 有 `status` 字段的对象 | 未统计 | 76（Schema 83 个定义） |
| 裸 `string` 的 `status` | 32 | 70 |
| 收 `enum` 的 `status` | 9 | 6（`Task` / `BudgetTransferRequest` / `Scene` / `Shot` / `ShotBreakdownDraft` / `ShotRevision`） |

所以「32」不能作为本轮的迁移目标：它既不是缺口数，也不是剩余数。先重建清单再动手，
否则会一边迁移一边沿用错误的基数。

更关键的是，**三份列表已经各自漂移，而闸门全绿**：

| 漂移形态 | 实例 | 旧闸门为何看不见 |
| --- | --- | --- |
| 状态机有、Schema 无 | 70 个 `status` 是裸 `string` | 旧闸门只看对象名集合，不读 Schema 的 `status` |
| 枚举注册表有、状态机无 | `PG0` / `PG3`（`CommunityPost`、`CreatorPublicationCheck`）、`PMS`（`PatternRecord`）、`PASS`（`DeliveryManifest`、`CreatorDeliveryManifest`）、`PUBLISHED`（`CreatorDeliveryManifest`） | 旧闸门注释明写「刻意不比状态集」 |
| Schema 有、权威集合无 | `BudgetTransferRequest` 的 8 个值，`state_machines` 里根本没有这个对象 | 没有任何规则连接 Schema 与状态机 |

第二条尤其值得记下来：那些假状态值是**从备注文字里抓出来的**——`PG0—PG3 全部通过`
抓出 `PG0`、`PG3`；`PMS / Pattern Library` 抓出 `PMS`；`不声明 Gate4 PASS` 抓出 `PASS`。
`enum_registry.yaml` 头部写着「由 state_machines 派生」，实际上一直是手写或机械抓取，
这就是「派生文件」名存实亡的样子。

## 核心裁决：状态集合 ≠ 状态转移图

这是本轮唯一的结构性决定，其余都是它的推论。

`AssetVersion` 允许 `QUARANTINED`，但 8.5 只写了「可进入」，没给来源边；
`Task` 允许 `WAITING_FOR_PROVIDER`，8.6 明确它「纳入同一状态机」但图里没有这条边。
如果从转移边反推状态集合，这些合法旁支会被判成非法值；如果为了凑状态集合去编造边，
就违反了 31.5「未登记转移一律拒绝」。

所以两者分开存储：

```yaml
- object: AssetVersion
  allowed_states: [APPROVED, ARCHIVED, GENERATED, GOLD, HUMAN_REVIEW, QUARANTINED,
                   REJECTED, REWORK, SUPERSEDED, TECHNICAL_QC]
  transitions:
  - {from: GENERATED, to: TECHNICAL_QC}
  - {from: HUMAN_REVIEW, to: APPROVED}
  ...
```

- `allowed_states` 是「这个字段允许出现哪些值」的契约，Schema 的 `enum`、枚举注册表、
  Domain Enum、数据库 CHECK 全部由它派生；
- `transitions` 是「已授权哪些边」，只收正文明确给出的边；旁支没有入边是**合法状态 +
  当前无授权入口**，缺口登记在 `contracts/runtime/state_machine_gaps.yaml`；
- `guards` 仍是「某条转移必须携带哪些字段」，但 `transition` 也改为 `{from, to}`
  结构化边：`parse_state_chain()` 连同 v1 的状态链文本一起删除。守卫若还写
  `"A→B"` 字符串，闸门直接报红——状态值不允许再从自然语言里「抓」出来。

一个立竿见影的后果：`AssetStatus` 从 5 个值变成 10 个。旧实现只列有入边的五态，理由是
「不伪造转移」——方向对，落点错：把「没有入边」错当成「不是合法值」。真实数据里一旦
出现 `QUARANTINED`（权利复核的必然产物），序列化就会失败，而契约本来允许它。

`allowed_states` 按字母序书写：它是集合不是序列，顺序不承载语义；字母序让手工增删与
diff 稳定，也让「两份文件顺序不同」永远不会被误判为契约漂移。

## 语义例外的逐条裁决

机械处理 76 个 `status` 会在下面几处出错，逐个裁定：

| 对象 / 记号 | 问题 | 裁决 |
| --- | --- | --- |
| `BudgetLedgerTransaction` | `APPENDED` 与 `REFUND` / `ADJUST` / `REVERSAL` 不同类 | `allowed_states: [APPENDED]`、`transitions: []`（追加即不可变）；后三者是**流水类型**，登记为 `transaction_kind` 维度（`status_layers.yaml`），不进 `status` |
| `TimelineFlowStatus` | UI 进度投影，不是持久化对象 | 移入 `state_machines.projections`，不计入对象数，不进枚举注册表，Schema 不得为它收 `enum`（它连 schema 都不该有） |
| `REVERSE_STALE` / `REVOKED_BLOCKED` | 失效/阻断标记，不是生命周期 | 留在 `status_layers.cross_domain_marks`；闸门禁止它出现在任何 `allowed_states`（会把「过期提示」变成「一种业务进度」） |
| `SHOWCASE_OUTDATED` | 展示别名 | 留在 `status_layers.state_projection_aliases`，`maps_to_state` 必须落在目标对象的 `allowed_states` 上 |
| `PG0` / `PG3` / `PMS` / `PASS` / `PUBLISHED` / `REJECTED_PROPOSAL` | 曾被当成状态值 | 登记为 `prohibited_state_values`（按对象收窄：`PASS` 在 `CreatorGate` 是合法终态），闸门禁止再次出现 |
| `CharacterCard` / `CharacterAppearance` | C.7 用一条状态机登记两个对象 | 拆成两条，共享同一 8 态集合；C.3 的 `CharacterCard` 条目缺 `REJECTED` / `QUARANTINED` 属描述省略（与 `CharacterCanon` / `ShotVisualPlan` 同一裁决口径），作为 `other_definitions` 保留证据 |
| `BudgetTransferRequest` | Schema 有 enum，状态机从来没有 | 反向补登 `allowed_states` = 既有 8 个值，`transitions: []`（正文未给边），`registration_status: PENDING_PHASE2_REVIEW`；这是本轮唯一「由实现反推契约」的条目，如实标注 |
| `PublicationReview` / `CreatorShareLink` / `Pattern` / `SharedReferenceAsset` | 有状态机、无 schema | 保留显式缺口（`status_field_taxonomy.schema_gaps`），不建空表、不豁免、不合并 |

## 76 个 `status` 字段的分类结果

`status` 是 C.3.1 信封必填项，所以**每个对象都有它**——但这不等于每个对象都有生命周期。
台账 `contracts/enums/status_field_taxonomy.yaml` 把 76 个字段落成三类，互斥且必须全覆盖：

| 分类 | 数量 | 含义 | 强制 |
| --- | --- | --- | --- |
| `lifecycle` | 39 | 有权威状态机 | `status.enum` 必须逐值等于 `allowed_states` |
| `envelope_only` | 18 | 无独立生命周期（`immutable_record` 5 / `child_structure` 7 / `derived_projection` 2 / `status_split` 4） | 禁止有 `enum`，必须给 `basis` + `reason` |
| `lifecycle_gap` | 19 | 应有生命周期但未登记状态机 | 必须给 `reason` / `contract_ref` / `unblocks_when`；补上状态机后必须改分类 |

`envelope_only` 的 18 个不是「豁免」，是**有依据的判定**，每条都写了为什么：
批注与反推片段挂靠父对象（`child_structure`）、审计与下架记录不可改写（`immutable_record`）、
策略解析与版本树可重算（`derived_projection`）、`ModelInvocation` 的状态被
`submission_status` / `result_status` / `billing_status` 三个字段拆走（`status_split`）。
最后这一类还暴露一个**未闭合的结构问题**：这些对象存在「两个 status 事实」（信封 `status`
+ 专用状态字段），按「消除重复机制」应当合并，但合并属契约修订，本轮只显式登记。

`lifecycle_gap` 的 19 个是真实缺口，不是遗漏：`ShotSpec` 有 `locked_dimensions`（锁定就是
状态语义）、`ShotTierChangeRequest` 有申请人/批准人（标准两步审批）、
`ExternalToolArtifact` 的描述就是「先隔离验证、合格后转正」、`ToolExchangePackage` 有
有效期与撤销……它们各自需要什么样的状态集，只有正文能定，所以本轮不编造。

## 闸门矩阵

`check_status_enum_consistency()` 的八项检查，每项都有独立负向测试
（`tests/unit/test_status_enum_contract.py`，33 例）：

| 检查 | 失败意味着 |
| --- | --- |
| 状态机 → 枚举注册表（对象集 + 每对象状态集） | 派生文件与权威源脱钩 |
| 状态机 → Schema（每个 lifecycle 对象都有 enum） | 裸 `string` 重新出现，任何值都会被接受 |
| Schema → 权威集合（无多余、无遗漏） | 契约里出现了正文没给过的状态 |
| 转移边 → 状态集合（两端都在 `allowed_states` 里） | 打错字母的边，运行时表现为「永远进不去」 |
| 有状态机无 Schema | 缺口隐形（`PublicationReview` 类） |
| 有 `status` 无分类 | 新对象带着裸字段悄悄进来 |
| 别名 / 跨域标记 / 禁用值 | 展示层语义被当成权威状态 |
| 已实现对象的 Domain Enum 与 DB CHECK | 契约收紧了，库里仍收任意字符串 |

其中第八项的两个值都来自同一处：数据库约束由
`packages/shared/status_constraint.py::status_check()` 从枚举生成，闸门用同一个函数算期望值
再与模型里的真实约束**逐字比对**——不存在「改了契约没改 DDL」的灰色地带。

## 运行时只收口已落库的四张表

`projects` / `episodes` / `script_revisions` / `asset_versions` 加 `status` CHECK，
Domain Enum 同步到全量状态集。其余 38 个对象没有 ORM、没有表、没有约束——契约层级
约束已成立，运行时不可执行，如实标记，**不提前建空表**。这是「未实现 ≠ 假装已实现」
的同一口径。

`data/migrations/versions/c7d8e9f0a1b2_add_status_check_constraints.py` 在加约束前会先查
存量违规值并给出可诊断报错（哪张表、哪个值、多少行），而不是让 PostgreSQL 回一句
「check constraint is violated by some row」。迁移里的值列表是**当时的快照**，不 import
活的枚举——否则将来枚举一变，历史迁移的行为随之改变，重建库与当初上线的库不一致。

## 刻意的取舍

- **不为没有权威状态集的对象编造 enum**。给 `CameraRig` 造一套 `DRAFT/ACTIVE/ARCHIVED`
  只是把「未知」换成「看起来知道了」，实现方会照着假枚举写 CHECK，最后连正文都没说过的
  东西成了数据库不变量。宁可留一个带债务登记的裸字段。
- **不让 `enum_registry` 继续收别名层**。派生与手写混在一个文件里，两边都不能自动化：
  改权威源不敢重生成（会冲掉手写部分），于是只能手改派生部分——这正是它漂出
  `PG0`/`PMS` 的机制。现在职责切开：`enum_registry` 纯派生，`status_layers` 纯手工。
- **不整份重排 Schema JSON**。它是手工排版的，`json.dumps` 会产出近八千行无意义 diff，
  评审时真正的改动会被淹没。生成器只重写每个定义的 `status` 属性块，其余字节原样保留，
  并自证「除 enum 外没有任何字段被改动」（`_assert_only_enum_changed`）。
- **不把 `Pattern` 与 `PatternRecord` 合并**。两者状态链逐字相同，但 31.5 与 C.3 分别登记，
  是否同一对象需要正文之外的裁定；本轮只登记缺口（`Pattern` 无 schema），不擅自合并。

## 未闭合项（显式登记，非遗漏）

- 19 个 `lifecycle_gap` 对象的 `status` 仍是裸 `string`：已有债务登记与双向校验，
  但「有债务」不等于「已约束」。
- 4 个 `status_split` 对象存在两个 `status` 事实，合并待裁定。
- `Pattern` / `PublicationReview` / `CreatorShareLink` / `SharedReferenceAsset` 仍无 schema。
- `BudgetTransferRequest` 的状态集由既有 schema 反推，Phase 2 启用前须所有者复核。
- 38 个对象无运行时：契约约束成立，但数据库层不拒绝非法值。
- 引用目标覆盖边界（456 个未声明字段）与 `GateInstance` / `AuditEvent` 缺口不变，见 ADR-R19。
- 累积式会签仍被审批事实契约阻塞。

## 验收口径

不以「增加了多少个 enum」计完成度，而以「状态定义与实际可校验契约之间是否还有脱钩」计：

- 39 个已建模对象的 `status` 不再是裸字符串，且合法状态集合只有一个权威源；
- 枚举注册表与 Schema 的 `enum` 都是可验证的派生结果（重算比对，逐字节）；
- 旁支状态、展示别名、跨域标记、流水类型各有明确边界，混入即报红；
- 任一处的状态集合漂移都会被闸门发现（七个负向场景逐个验证）；
- 未实现的运行时部分如实标记为未实现，没有为了「看起来完整」而建的空表或假约束。

## 勘误：v2 结构化迁移漏抄的转移边（PR #14 评审，V205-R14-A11）

本 ADR 声称「合法状态集合只有一个权威源」，但合入前的自动评审暴露了一个盲区：
`allowed_states` 收全了合法状态，`transitions` 却只抄了正文中画成箭头的链，正文用自然语言
授权的边（「发布后可 X/Y/Z」「READY/QUEUED 允许取消」）被漏掉。结果是 `7 个负向场景`
全过、闸门全绿，而契约声明的状态根本没有可执行入口。逐条比对基线正文后补齐 11 条边：

| 对象 | 补齐的边 | 正文依据 |
|---|---|---|
| CommunityPost | PUBLISHED → UNLISTED / TAKEN_DOWN / ARCHIVED（3） | 31.5「发布后可 UNLISTED / TAKEN_DOWN / ARCHIVED」 |
| CreatorRelease | PUBLISHED → UNLISTED / TAKEN_DOWN / REVOKED / ARCHIVED（4） | 34.8「发布后可 UNLISTED / TAKEN_DOWN / REVOKED / ARCHIVED」 |
| TimelineDraft | DRAFT → STALE / ARCHIVED（2） | 33.3「旧版本 → SUPERSEDED/STALE/ARCHIVED」，note 已解释旧版本含 DRAFT |
| Task | READY / QUEUED → CANCEL_REQUESTED（2） | 8.6「READY/QUEUED 允许取消」 |
| ReuseRequest | RIGHTS_CHECK → REJECTED（1） | 26.3 / 31.5「RIGHTS_CHECK → APPROVED / REJECTED」 |
| ShotVisualPlan | REVIEW → REJECTED（1） | C.3 `states_text` 与 conflict_ruling「DRAFT→REVIEW→APPROVED/REJECTED」 |

后两条是推送后审查器第二轮发现、同样比对正文确认的漏抄。合计 13 条边——全部是恢复迁移时
丢失的正文语义，不是新增契约。补边后新增一条测试逐条锁定这 13 条边，防止未来误删仍能全绿。

`ReworkTask` 例外：7.6 只给 status 枚举、未画转移图，`VERIFYING→IN_PROGRESS` 与进入
`REJECTED` 的边无正文依据，按「不猜契约」不推定，改以 `transitions_gap` 登记显式债务
（含 `unblocks_when`），待所有者补登每条边的 `owner_action` 与前置条件后再写入。

同时把 `status_field_taxonomy` 的 `basis` 校验从「非空」升级为「必须属于 `basis_kinds`
封闭枚举」：拼错类别（如 `immutabel_record`）此前会静默通过，台账自称的封闭枚举形同虚设。
补两个负向测试（非法值报红 / 合法值放行）。

`field_authority` 闸门同样补上完备性：原逻辑只校验「已登记的裁定是否合法」，若新增一个
`status_split` 对象却忘了登记权威裁定，闸门全绿，ADR-R21 刚消除的「两个事实源」会重新隐形。
现要求台账里每个 `basis: status_split` 对象都有一条 `field_authority` 裁定（反向不要求相等，
`same_fact_on_status` 类历史裁定如 PrevizVersion 合法保留）。补负向测试验证缺口会被发现。

**根因与防治**：这次缺陷不是「谁漏抄了一条边」，而是 v2 迁移缺少一条
「note/正文声明的转移语义 vs transitions 转移表」的一致性校验。补边只能修复当下这批，
真正防止重演的是两件事——逐条锁定正文授权边的回归测试，以及「正文自然语言授权的边
必须同步进 transitions」这一迁移口径的明确化。中文措辞（「发布后可」）无法用箭头正则
自动发现，故对 `PENDING_PHASE1_REVIEW` 对象的正文语义核对仍是人工复核环节，不假装自动化。
