# ADR-R42 实施授权对账记录（append-only）

> 性质：**对账记录**，不是新裁决。不改 ADR-R42 正文、不改冻结语义、不代 Owner 签署。
> 基线：`main @ dadbb17`（PR #72 合并后）+ 本记录同批 PR-G 补口变更。
> 上游：`docs/audits/2026-11-23-r42-implementation-reconciliation.md` §5 把「实施是否已获
> 单独授权」标为 `[V-BLOCKED][E1]`，明确「不由审计代为裁决」。本记录是该 BLOCKED 项的**对账面**：
> 把散落在修订台账、ADR 附录、提交历史里的授权事实收拢成一页，并把仍缺的验收条件列清。
> 取证方式：逐条实读 ADR 正文 / `contract_amendments.yaml` / 源码 / 迁移 / 提交历史，给出位置。

---

## 0. 一页结论

| 问题 | 结论 | 依据 |
|---|---|---|
| ADR 说「不激活实现」，库里却在跑，算不算违规实施？ | **不算。** ADR 的三处措辞是**自身范围声明**（「本裁定不实现、也不授权变更」），不是对他人后续授权的禁止 | ADR-R42 `:3` `:189-190` `:227` 原文语义；见 §1 |
| 那实施授权到底有没有？ | **有，且已落纸，但是分散的**：`V205-R15-A35` 的 `approved_by` 即业主设计裁决，`source_commits` 指向实际落地提交 | `contract_amendments.yaml:1617,1619-1621`；见 §2 |
| 治理缺口到底是什么？ | 不是「无授权实施」，而是**授权事实没有一页汇总**，且 ADR 正文按设计不随实施同步（这是对的） | 本文即为汇总；见 §2/§3 |
| 审查报告列的 4 个 PR-G 缺口，现在什么状态？ | 3 项已闭合（PREPARED 恢复 / `media_type` 事实化 / `ABANDONED` 终态），第 4 项（授权对账）由本文闭合为「已登记、待签署」 | §3.1 |
| 审查报告把「T2 并发」列为 P0 待办，对吗？ | **过时。** 已在 `1447468`（PR #65，迁移 `f1a2b3c4d5e7`）闭环，本轮复核确认 | §3.2 |
| 还剩什么？ | C7、D6 两项裁定已采纳（§8.2/§8.3），落为开发任务待实施；S1 有条件认可，`[V-BLOCKED]` **不变**（缺 P1/P2） | §4 / §5 / §8 |

---

## 1. 口径校正：范围声明 ≠ 禁止实施

ADR-R42 被引用最多的三句话：

| 位置 | 原文 |
|---|---|
| `:3` | 状态：DECIDED（…固化决策，**不激活实现**） |
| `:189-190` | C1–C7 是**变更清单**，不是已实施事实。任何一项落地前必须按冻结治理走「契约修订登记 → 派生文件 →…」 |
| `:227` | 它是**架构与身份边界裁定**，不是实现方案，**也未授权任何代码变更**或契约派生文件变更。 |

正确读法是**文件自我限定**：这三句约束的是「这份 ADR 自身能产生什么效力」——它声明自己不是实现方案、不构成对代码变更的授权。` :189` 更直接给出了**放行程序**（契约修订登记 → 派生 → 重签），说明实施是被预期的，只是必须走该程序。

把它读成「任何人不得实施」会得出一个自相矛盾的结论：`:94` 已经写明崩溃恢复的权威依据是「未完成的 `PREPARED` 记录」，`:76-100` 已给出 T0–T3 的事务边界与角色分工——这些都是**实现级约束**，只有在实施会发生的前提下才有意义。

**因此本轮结论：治理差异成立（决策文本与实施状态已分离），但不存在「无授权实施」。真正缺的是汇总。** ADR 正文保持历史原文不动，这正是项目红线要求的做法。

---

## 2. 授权事实台账（可复核）

### 2.1 决策与契约层

| 提交 | 日期 | 内容 | 授权登记 |
|---|---|---|---|
| `cb6793e` | 2026-11-23 | ADR-R42 固化（DECIDED，不激活实现） | ADR-R42 `:3` |
| `b41942c` | 2026-11-23 | 事务边界修订为 T0–T3（append-only 附录，不改正文） | ADR-R42 附录 A |
| `28f9da4` | 2026-11-23 | 契约修订 A35 + 派生四阶段契约事实 | `V205-R15-A35`，`approved_by` = 业主设计裁决 |

`V205-R15-A35` 是本对账的关键锚点。它明确登记了授权内容与落地提交：

- `approved_by`：项目所有者（2026-11-23 设计裁决：AssetRegistrar 升级为 Outbox 异步 Saga，T0–T3 四阶段，先完备契约后执行业务）— `contract_amendments.yaml:1617`
- `source_commits`：`b41942c`（ADR 附录）、`716971c`（PR-C 库表）— `:1619-1621`

也就是说：**授权主体、授权内容、落地提交三者齐备**，符合冻结治理的「契约修订登记」程序（A35 同条目 `:1610-1615` 还记录了 PR-C/PR-D 的落地范围与三条可机检删除条件的满足情况）。

### 2.2 实施层

| 提交 | 日期 | 内容 | 迁移 |
|---|---|---|---|
| `716971c` | 2026-11-23 | PR-C：库表、复合归属、不可变回执 | `e1f2a3b4c5d6` |
| `e95127a` | 2026-11-23 | PR-D：T0–T3 运行时与事件入口 | — |
| `291300a` | 2026-11-23 | PR-E：跨域真实 PG 故障注入验收 | — |
| `1447468` | 2026-11-24 | 修复 T2 幂等冲突判定（请求指纹 + append-only 冲突事件） | `f1a2b3c4d5e7` |
| 本批（PR-G） | 2026-11-24 | PREPARED 崩溃恢复 + `media_type` 事实化 + `ABANDONED` 终态 | `a7b8c9d0e1f2` |

**读法**：决策（`cb6793e`）与首次实施（`716971c`）同日，之间没有独立的「实施授权」提交。授权是通过 A35 的 `approved_by` 与后续 PR 评审隐式落纸的。这不是缺授权，是**授权事实缺少单页汇总**——本文补上。

---

## 3. 实施缺口对账

### 3.1 本批闭合的三项（审查报告 §五 / PR-G）

| 缺口 | 原状态 | 根因 | 本批处理 | 证据 |
|---|---|---|---|---|
| PREPARED 崩溃恢复未实现 | T0 提交后崩溃 → 意图成孤儿，重启后重新调模型，产物重复计费 | 缺少「按持久化意图重建登记请求」的入口，`AssetRegistrar` 只能正向新建 | `executor.execute()` 起始扫本任务 PREPARED 意图；可复用时沿用原 `attempt_id` 直接走 T2，不重调模型；三类失败分别处置（见下） | `workers/generation/executor.py` `_recover()` / `_abandon_intent()`；仓储 `list_pending_prepared`；`tests/unit/test_generation_executor_recovery.py` 5 例 |
| `media_type` 未持久化 | 恢复时必须重建产物元数据，但 manifest 只有 locator/sha256，无法判定「同一产物」 | 契约把 media_type 当投影字段，未纳入事实 | manifest 增加 `media_type` 列（NOT NULL + 回填），T0 校验 locator 与 media_type 双射、T1/T2/T3 全链路比对；事件载荷与 manifest 矛盾即拒绝 | 迁移 `a7b8c9d0e1f2`；`artifact_manifest.py` 字段校验；`asset_registration.py` T2 核验；`state_machines.yaml` / `object_schemas.schema.json` / `schema_field_signatures.yaml` 同步 |
| 无「放弃」终态 | 确定不存在/已损坏的意图只能永远停在 PREPARED，无法与「待重试」区分 | 状态机只有 PREPARED→STORED_VERIFIED | 新增 `ABANDONED` 终态与 `PREPARED→ABANDONED` 边（owner = `GENERATION_PROCESS_ACTION`），`frozen_states` 含 ABANDONED；放弃必须带 `audit_ref` 证据，且**不删除意图行** | `services/project_planning/domain/enums.py`、`state_machine.py`；`artifact_manifest.abandon()`；`tests/unit/test_generation_executor_recovery.py` 4 例 |

恢复的三态区分是本批最关键的语义（错判会把可恢复任务判死，或把已丢失产物当可用）：

| 存储侧事实 | 异常 | 处置 | 理由 |
|---|---|---|---|
| 确定不存在 | `StorageObjectMissingError` | 放弃意图 → 重新生成 | 对象确实没了，等下去没有意义 |
| 存在但不可复用（哈希不符/符号链接） | `StorageIntegrityError` | 放弃意图 → 重新生成 | 内容已被破坏，复用等于登记错资产 |
| 暂时不可用（I/O、权限抖动） | `StorageUnavailableError` | **不放弃**，报告失败留待重试 | 一次抖动不代表对象不存在；此处放弃就是永久丢失 |

### 3.2 T2 并发：不是缺口（审查报告判断过时）

审查报告把「T2 并发修复」列为 P0 待办。实读确认已闭环：

- 8 维请求指纹与比对：`packages/shared/registration_fingerprint.py`；T2 比对与冲突处置：`workers/relay/asset_registration.py`（指纹不一致 → `_record_conflict`）
- 并发撞唯一键后的回读收敛：同文件 `except DuplicateEntityError` 分支
- 冲突事件已登记：`contracts/runtime/event_catalog.yaml:95` `ASSET_REGISTRATION_CONFLICT`
- 冲突事实用 append-only outbox 事件承载，未新建独立冲突表（更简，且符合「用事实表而非补丁表」）
- 迁移：`f1a2b3c4d5e7_t2_idempotency_fingerprint.py`

本轮唯一补的是其中「manifest 持久化」这一子项（即 §3.1 的 `media_type`）。

---

## 4. 剩余验收条件与裁定建议

以下两项**不由本记录裁决**，只给事实与建议，等 Owner 签署。

### 4.1 C7：QC 手动入队端点（`services/qc_governance/api/qc_task_routes.py:110-136`）

**事实**：

1. 该端点 `POST /qc/tasks` → `enqueue_qc_task`，挂载于 `apps/api/main.py:100`。
2. 它不是后门：动作键 `QC.TASK.ENQUEUE` 已登记于 `contracts/action_registry/action_registry.yaml:1167`、`contracts/openapi/openapi.draft.yaml:3512`、`contracts/runtime/action_implementations.yaml:55`，并有 `authorize_on_parent` 项目级授权。
3. 但它**不核验来源事实**：`QCTaskService.enqueue` 只做「同 `generation_task_id` 重复入队拒绝」（`services/qc_governance/service/qc_task_service.py`），不检查该生成任务是否 `SUCCEEDED`、`asset_version_id` 是否真实存在且已登记完成，`quality_profile_version` **直接取自请求体**。
3. 因此持有 `QC.TASK.ENQUEUE` 的调用方可以为一个不存在的资产、或自报的质量口径创建一个 QC 任务。这与 C7（QC 只消费登记完成事件）**和** C4（质量口径在任务创建时冻结、不得事后自报）同时冲突——同一类「自报成功」问题，只是入口不同。

**建议（推荐 A）**：

| 方案 | 内容 | 代价 |
|---|---|---|
| **A（推荐）保留端点，补来源核验** | `enqueue` 前校验：Generation 任务存在且 `SUCCEEDED`、其 `asset_version_id` 与请求一致、`quality_profile_version` 取任务冻结值（忽略/拒绝请求体自报）。端点语义改为「补偿运维入口」，与 relay 自动路径共用同一核验器 | 一次服务层改造 + 负向测试；不新增动作、不改契约计数 |
| B 删除端点 | 只留 relay 自动路径 | 失去运维补偿能力；D6 类事件重放场景将无手工入口 |
| C 维持现状、只在 ADR 附录声明 | 不改代码 | 「已登记的动作可以绕开 C4/C7」，属用声明掩盖缺口，与项目红线冲突 |

不选 C 的理由是本项目的既有纪律：**不得用文档声明掩盖能力缺失**（A35 的 `residual_risks` 就是这么写的——缺口要么修，要么显式挂账，不能改写措辞了事）。

> 2026-11-24 裁定采纳方案 A。**可验收的六条开发要求**以 `docs/proposals/r42-remaining-acceptance-c7-d6.md` §2 为准（单一事实源，避免两处各写一份清单后漂移）；本小节只保留「为什么保留端点」的判断。

### 4.2 D6：死信人工重放入口缺失

**事实**：`workers/relay/consumer.py:24,120` 与 `:224` 三处注释声称死信「必须可查、可人工重放」「`drain_tenant` 是重放某租户的真实能力」，但仓库内**没有任何重放端点、脚本或 CLI**（全仓 `replay/重放` 命中均为注释或无关语义）。死信行保留可查、指标 `OUTBOX_RELAY_DEAD` 可见——「可查」成立，「可重放」不成立。

**建议（推荐 A）**：

| 方案 | 内容 | 代价 |
|---|---|---|
| **A（推荐）加受权运维重放** | 一个运维操作（CLI 或受权端点）：按 `event_id` 清 `dead_lettered_at`、`next_attempt_at=now()`、`attempts` 归零，并**写一条 append-only 审计事件**记录操作者/原因/原 attempts。不新增「直接投递」旁路，重放仍走正常 claim → handler → ack | 小；关键是把「谁在什么时候重放了哪条」变成可审计事实，而不是手工 SQL |
| B 明确不支持，改注释 | 把三处「可人工重放」改为「当前不支持，需 DBA 手工 SQL」 | 零代码，但 D6 永久停在 PARTIAL，且 ADR-R42 D6 原文要求「死信与人工处置」 |
| C 维持现状 | — | 注释承诺的能力不存在，读者会据此误判运维可行性 |

> 2026-11-24 裁定采纳方案 A。**可验收的八条开发要求**以 `docs/proposals/r42-remaining-acceptance-c7-d6.md` §3 为准；其中「重放动哪一层」的取舍已在提案 §3.2 钉死为「清死信标记并交回正常 claim」，不得复制新事件、不得直调 handler。

---

## 5. 待 Owner 签署（3 项）—— 2026-11-24 裁定已登记，见 §8

| # | 签署项 | 建议取值 | 裁定结果（2026-11-24） | 影响 |
|---|---|---|---|---|
| S1 | R42 实施状态事实源 | 认可「ADR 正文保持历史原文不动；实施状态以 `2026-11-23-r42-implementation-reconciliation.md` + 本记录为准」 | **有条件认可**：认可文档治理方式；解除 `[V-BLOCKED]` 另设四项前置（§8.1），**当前未满足** | 上游 `[V-BLOCKED]` **不变**；不需改 ADR |
| S2 | C7 口径 | 采纳 4.1 方案 A（保留端点 + 补来源核验） | **采纳方案 A**，六条开发要求见 §4.1 / 提案 `r42-remaining-acceptance-c7-d6.md` §2 | 下一批改 `QCTaskService.enqueue` + 负向测试 |
| S3 | D6 人工重放 | 采纳 4.2 方案 A（受权重放 + 审计事件） | **采纳方案 A**，八条开发要求见 §4.2 / 提案 §3 | 下一批新增运维操作与审计事件登记 |

裁定落地前不得据本记录修改 ADR 正文、契约计数或运行时行为。

---

## 6. 九项状态结论

| 标注 | 取值 | 支撑 | 升级条件 |
|---|---|---|---|
| `[FREEZE-STATUS]` | VERIFIED | 本批变更全部经 A36 登记 + `freeze_manifest` 重签 | — |
| `[CONTRACT-STATUS]` | VERIFIED | `ArtifactManifest` 三态、`media_type` 必填已进契约与派生文件 | — |
| `[ARCHITECTURE-STATUS]` | PARTIAL | T0–T3 四段本地事务、恢复三态区分成立 | C7 第二入口 / D6 人工重放未闭环 |
| `[IMPLEMENTATION-STATUS]` | PARTIAL | C1–C6、T0–T3、D2、D3、PR-G 三项实存 | C7 / D6 裁定已采纳（§8），待实施 |
| `[TEST-STATUS]` | VERIFIED | 新增恢复用例 9 条；故障注入 7 例；全量单测绿 | — |
| `[RUNTIME-STATUS]` | PARTIAL | 真实 PG 四文件 18 例绿、重复运行稳定 | 生产未部署 |
| `[SECURITY-STATUS]` | VERIFIED | D2/D3 身份与归属核验；T2 指纹冲突可审计 | C7 未补核验前，手动入队可绕 C4 |
| `[REMEDIATION-STATUS]` | OPEN | S2/S3 已采纳（§8.2/§8.3）、S1 有条件（§8.1） | C7/D6 落地且 S1 四项前置齐备后转 CLOSED |
| `[RELEASE-STATUS]` | BLOCKED | Phase 1 未验收 | 不在本记录范围 |

---

## 7. 证据边界

- 本记录为 `main @ dadbb17` + 本批 PR-G 变更的时点判断；所有位置为该时点实读位置。
- 测试结论来自本机真实 PG 执行（非 CI 转述）；未执行生产部署验证。
- 授权结论只依据**已落纸的登记**（`contract_amendments.yaml` 的 `approved_by`/`source_commits`）与 ADR 正文，不采信提交信息自述、执行包转述或本记录自身的推断。
- 本记录不改 ADR-R42 正文、不改契约计数、不代 Owner 裁决 S1–S3。

---

## 8. 裁定登记（2026-11-24，追加于文末，不改前文）

**来源**：2026-11-24 工程审查建议（S1 有条件认可 / S2 方案一 / S3 方案一）。
**性质**：这是一份**工程审查结论的采纳登记**，不是 Owner 签署。原文明确「不代表替项目 Owner 完成架构裁决或签署」，本文据此把三项落到「已定方向 + 可验收判据」，并**不**据此解除任何阻塞标记。

### 8.1 S1：ADR 正文不动 —— 有条件认可，`[V-BLOCKED]` 不变

认可的治理方式：ADR 正文保持历史原文不动；实施状态由两份 append-only 审计记录承载（`2026-11-23-r42-implementation-reconciliation.md` + 本记录）。

但「认可以文档治理方式」≠「解除 `[V-BLOCKED]`」。原文的关键限制是：**实施事实不能反向替代实施授权**。因此解除必须另设前置，且每条都要可核验：

| # | 解除前置 | 当前状态 |
|---|---|---|
| P1 | 审计记录必须区分**原始架构决策 / 后续实施授权 / 实际实施状态**三层 | 部分成立：§1 区分了决策与实施；§2 是授权的**来源**（A35 的 `approved_by`），但本记录把三者放在同一层级叙述，尚未逐层分列 |
| P2 | 能追溯到 Owner 明确授权，含**授权时间、范围、对应 PR、未完成事项** | 部分成立：时间（2026-11-23）、范围（T0–T3 + C1–C7）、PR（`716971c`/`e95127a`/`291300a`/`1447468`）齐备；未完成事项分散在 A35/A36 的 `residual_risks` 与本文 §4，尚未归并成一份清单 |
| P3 | 附带**可核验的代码及测试证据** | 成立：提交 + 迁移 id + 测试文件与用例数 + 本机真实 PG 执行结果 |
| P4 | 不得仅凭「已合并」证明「已获批」 | 成立：§2 的结论建立在 `approved_by` 登记上，而非合并事实 |

**结论**：P3/P4 成立，**P1/P2 未闭环**，故 `[V-BLOCKED]` 保持。上游 `2026-11-23-r42-implementation-reconciliation.md` 为 append-only，其 §5/§6 的 BLOCKED 文字不回改——状态以本节为准。

补 P1/P2 的最小动作（下一批做，不属本 PR）：在本记录新增一层「实施授权清单」，按「已授权且已实施 / 已授权未实施 / 未授权」三栏列，每行带授权时间、授权来源、PR、剩余事项。这比在正文里补形容词有用，因为它是可被机器或人逐行核对的表。

### 8.2 S2：C7 保留端点 + 补来源核验 —— 采纳方案 A

采纳。六条开发要求与验收重点落在 `docs/proposals/r42-remaining-acceptance-c7-d6.md` §2（单一事实源）。本文 §4.1 保留「为什么不能删、不能只改措辞」的判断依据。

一处必须点明的实现约束：人工入口与 relay 自动入口对**同一组非法输入**的拒绝集合必须相同。若人工路径需要 `force`/`skip_validation` 才能真正救得了现场，那说明缺的不是旁路参数，而是缺一个「合法但不满足自动条件」的显式状态——先补状态，不要补开关。

### 8.3 S3：D6 受权重放 + 审计事件 —— 采纳方案 A

采纳。八条开发要求落在提案 §3，其中「重放动哪一层」已钉死：**清死信标记 + `next_attempt_at=now()` + `attempts` 归零，交回正常 claim**。禁止复制新事件（绕过 `event_id` 幂等）、禁止直调 handler（绕过 claim/退避/租户隔离）。

一个与既有实现的接口关系需记明：`drain_tenant` 是公开能力（重放某租户），但**它不是重放**——它只是「把当前可领取的事件投一遍」，对死信行无效（`_CLAIM` 显式排除 `dead_lettered_at IS NOT NULL`）。重放必须先把事件从死信态移出，这一步才是需要审计与权限的地方。

### 8.4 状态更新范围（本次实际改了什么）

| 对象 | 动作 |
|---|---|
| ADR-R42 正文 | **未改**（S1 要求） |
| 上游审计 `2026-11-23-…-reconciliation.md` | **未改**（append-only；BLOCKED 状态以 §8.1 为准） |
| 本记录 | 追加 §5 裁定结果列、§6 状态升级条件、§8 本节 |
| 契约事实源（counts/状态机/schema） | **未改**（S2/S3 尚未实施，不预登记） |
| 开发任务 | 新增 `docs/proposals/r42-remaining-acceptance-c7-d6.md`（`status: in_progress`，两项 `open_items` 指向 A36 的残余风险） |

