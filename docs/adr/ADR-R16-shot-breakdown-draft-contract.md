# ADR-R16：拆镜草案的对象契约（ShotBreakdownDraft）

状态：已实施（契约层，2026-11-18）；运行时未实施
关联：ADR-R07（ShotDraft 缺对象契约）、ADR-032（AI 拆镜输出默认草案）、31.6 Action Registry、31.5 状态机权威规则
上游口径：项目所有者《拆镜草案字段设计》（2026-11-18）

## 结论

拆镜草案的数据主体定为**文档级聚合根** `ShotBreakdownDraft`，而不是 ADR-R07 与
`docs/proposals/shot-domain-contract-closure.md#四` 草案里那个「一行一个镜头」的 `ShotDraft`。

结构分三层：

```
ShotBreakdownDraft（一版草案 = 一个创作决定）
├── breakdown_scope / plan_name / generation_method   范围与方案标识
├── scene_breakdowns[]                                场次上下文
│   ├── entry_state / exit_state                      剧情状态变化（必填）
│   ├── story_beats[]                                 剧情节拍
│   └── shot_drafts[]                                 镜头草案
├── review_records[]                                  人工审阅事实
└── 版本链：draft_version / root_draft_ref / supersedes_draft_ref
```

状态五态：`PENDING_CONFIRM → IN_REVIEW → CONFIRMED`；`IN_REVIEW → RETURNED`；
`PENDING_CONFIRM / IN_REVIEW → DISCARDED`。`CONFIRMED` / `RETURNED` / `DISCARDED` 均为**版本终态**。

`SHOT.DRAFT.CREATE` 的 `resource_type` 随之由 `ShotDraft` 改为 `ShotBreakdownDraft`。

## 为什么是聚合根，不是逐镜头行

ADR-R07 的草案把草案设计成「一条记录 = 一个拟议镜头」。这条路径在人工确认环节走不通：

- 人工的核心动作是**合并与拆分镜头**。逐行对象一旦落库，"把镜头 5 和 6 合成一个"就变成
  删两行加一行，两条原始创作记录消失——而拆镜的价值恰恰在于记录「AI 提了什么、人改成了什么」。
- 一次拆镜是一个整体创作决定（同一套情绪基调、同一条轴线策略）。逐行切开后，
  「这一版为什么这样组织」这个信息没有归属对象。
- 版本语义无法落在一行上：改了第 3 个镜头，是改这一行还是新建整版？逐行模型只能原地改，
  与「每次修改形成新的草案版本，不能覆盖历史版本」直接冲突。

聚合根把「一版草案」作为不可变快照：任何修改都是新版本，旧版本原样保留，
人工的合并/拆分记录在 `review_records[].shot_decisions` 里，可回放。

## 关键取舍（写明理由，便于后来者反驳）

1. **版本历史不冗余存储。** 设计文档的树里有 `RevisionHistory[]`，本契约没有这个字段，
   改为 `draft_version` + `root_draft_ref` + `supersedes_draft_ref` 的链式引用。
   理由：把历史数组塞进每条记录，等于同一事实存 N 份（第 N 版记录里存着前 N-1 版摘要），
   任何一次写入不一致都会让「历史」与「链」互相矛盾。链式引用只有一个事实源，历史是查询结果。
   代价：回放要递归查链，需要索引 `root_draft_ref`。
2. **镜头字段扁平，不按七组嵌套。** 设计文档按叙事/主体/表演/镜头/声音/连续性/制作说明七组组织，
   契约取扁平字段，分组写进 `x-afs-field-groups`。理由：景别、时长、镜头功能是会被检索和
   统计的字段，嵌套两层后必须走 JSONB 路径查询；分组是阅读便利，不是数据边界。
3. **`RETURNED` 是终态。** 退回后的修改走新版本，不回退到 `PENDING_CONFIRM`。
   这是 31.5「终态不得原地复活」在本对象的落地方式，也保证「被退回的那一版」永远可查。
4. **P1/P2/P3 字段不进 `required`。** 把建议字段设成强制，AI 拆镜会退化成机械填表：
   没想好运镜就随手填个「推」，比留空更糟。`required` 只收 P0（含「镜头功能」——没有功能的
   镜头就是冗余镜头），P2 的「有相关内容时必须记录」依赖内容语义，schema 表达不了，
   留给运行时在提交时校验，已在 `x-afs-criticality.note` 写明这条边界，不含糊过去。
5. **场次起止状态设为必填。** 拆镜最容易犯的错是只盯画面、丢掉剧情状态变化；
   `entry_state` / `exit_state` 强制填写，把「这一场把人物从什么状态推到什么状态」钉在场次上。
6. **节拍→镜头单一定义方向。** 多对多关系只在 `story_beats[].covered_shot_refs` 一侧定义，
   镜头侧不存 `beat_ref`。双向存会让「一个镜头属于哪个节拍」出现两个答案。

## 与所有者流程图的一处差异：确认之后先出 ShotRevision

所有者给的闭环是「剧本导入 → AI 拆镜草案 → 人工确认 → ShotSpec / ShotVisualPlan → 生成」，
其中没有 `ShotRevision`。但既有契约不允许跳过它：`ShotSpec` 的 `required` 里有
`shot_revision_id`，即一个 ShotSpec 必须挂在某个镜头版本上；而 `SHOT.REVISION.CONFIRM`
本来就是「人工确认」这个动作的既有入口。

因此本契约把确认的产出定义为 `confirmed_shot_revision_refs`（复数：一版草案含多个镜头，
确认产出多个 ShotRevision），再由 ShotRevision 派生 ShotSpec / ShotVisualPlan。
链路仍是草案 → 确认 → 版本 → 规格，只是把「确认」的产物显式化成 `ShotRevision`，
避免出现「草案直接跳 ShotSpec、中间没有可追溯版本」的断点。

这不是改所有者的业务意图，是把流程里省略的中间对象补回契约。

## 来源与合规性（必须如实记录）

本契约的字段口径来自**项目所有者的设计文档**，不是基线正文《AI影视工业化生产平台V2.0.5.md》。
基线 8.12 只有 `ShotSpec` 完整字段表，8.3 只有 Shot 示例，`ShotDraft` 在正文中仅出现于
31.6 动作行与 19.3 流程描述，**没有字段表**——这正是 ADR-R07 当初判定「不能凭猜补契约」的原因。

现在字段由所有者给出，属于合法权威来源，但它与「正文派生」不是一回事：

- schema 的 `x-afs-sources` 第一项明写「非基线正文原文，属所有者口径，待下一版正文吸收」；
- 状态机条目 `source_kind: owner_supplied_design`，与 `paragraph_state_machine` 等正文派生类型区分；
- 契约修订 `V205-R14-A4` 的 residual_risks 里把「正文未吸收」列为显式债务。

下一版正文必须补齐 8.x 拆镜条款；补齐后本 ADR 降级为历史说明，schema 的 `x-afs-sources` 改为引用正文条目。

## 影响

- `resource_schema_gaps.yaml` 删除 `ShotDraft` 条目（补了 schema 仍挂债务会被闸门判为陈旧债务）；
  剩余 8 项债务不变。`ShotRevision` 的 `unblocks_when` 更新为「产出关系已固定，自身字段仍缺」。
- `action_implementations.yaml` 中 `SHOT.DRAFT.CREATE` 的未实现理由改为「契约已就绪、缺运行时与状态推进入口」，
  不再声称「对象契约缺失」——那句话现在不成立了。
- OpenAPI 标签与摘要、`x-afs-total-actions`（80 → 84，此前已与 84 个 Action 不一致）同步修正。
- `enum_registry.yaml` 登记该对象的五个状态值。

## 残余风险

1. **状态机无入口**：`SHOT.DRAFT.CONFIRM` / `SHOT.DRAFT.RETURN` / `SHOT.DRAFT.DISCARD` 三个动作尚未登记。
   五态状态集目前是「钉住的契约」，不是「可执行的流程」。按「先开入口、再写守卫」的顺序，
   这三个动作与 `ShotRevision` 契约应一并处理，否则又会出现「有转移边、无入口」的静默缺口。
2. **九条不变式中七条待运行时**：只有 SBD-2（草案不越权定约束）与 SBD-8（节拍单一方向）能由
   schema 表达，其余依赖运行时校验，已在 `x-afs-invariants.enforced_by` 逐条标注 `RUNTIME_PENDING`。
   不把它们写成「已实现」。
3. **`ShotRevision` 仍无 schema**：草案确认要产出 `confirmed_shot_revision_refs`，
   而这些引用指向一个尚未建模的对象。链路目前是「一头有契约、一头是引用字符串」。
4. **改名无法被闸门自动识别**：`SHOT.DRAFT.CREATE.resource_type` 由 `ShotDraft` 改为
   `ShotBreakdownDraft`，冻结闸门按名称集合判定增删，识别不了「同名 action 指向不同对象」。
   本次在 A4 修订中显式记录，但这属于闸门能力的已知盲区。
