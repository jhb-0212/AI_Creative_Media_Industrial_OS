# ADR-R36：QC 复查会话（Phase 1 验收 C5「核心 QC」的契约载体）

状态：已接受（2026-11-17）
关联：ADR-R33 §4（C5 无证据的出处）、ADR-R35 §3 债务 4、ADR-R20（状态机下沉
数据库）、ADR-R28（跨域只读端口）、ADR-R23（Gate 单一裁决点）、
契约修订 V205-R14-A10、Phase 1 验收清单 C5

## 1. 背景与问题

C5 的验收要求是：

> 一次含缺陷的 QC 全流程；缺陷绑定 `QCIssue` 必填项；`RESOLVED→CLOSED` 跳关
> 被拒绝（须经 GATE_ELIGIBLE）

逐项对照 C.7 与 21.1 后，运行时缺的不是"实现细节"，而是**三处结构性空洞**：

1. **`QCIssue` 缺三项必填**：21.1 要求每条问题绑定"shot_id、asset_version、
   timecode/frame_range、QC dimension、severity、evidence、detector_version、
   human_decision、disposition"九项。契约里 `QCIssue` 此前没有 `issue_id`
   （7.6 的 ReworkTask 却要按它引用）、没有 `qc_dimension`、没有 `shot_id`——
   三条必填在 schema 层就不存在，"九项缺一即拒"无从校验。
2. **`QCReviewSession` 没有任何动作入口**：C.7 给了六态状态链，31.5 裁决 ④
   又明确禁止 `RESOLVED→CLOSED`，但动作矩阵里 OPEN / DECIDE 都不存在，
   enum_registry 登记了状态、表与接口全无。结果就是"状态机存在，平台永远停在
   OPEN"——和 ADR-R35 §1 描述的 C4 空洞是同一类病：契约写了、运行时摸不到。
3. **`QCReviewSession` 缺 `project_id`**：会话是项目级业务数据（要回答"这次
   复核属于哪个项目"），缺归属列就无法做项目级授权与 RLS 隔离，跨项目读取
   也没有拒绝依据。

## 2. 决策

### 2.1 契约一次补到位（V205-R14-A10）

| 变更 | 内容 |
| --- | --- |
| `QCIssue` | 补 `issue_id` / `qc_dimension` / `shot_id` 进 required 与 properties |
| `QCReviewSession` | 补 `project_id` 进 required 与 properties |
| Action | 新增 `QC.REVIEW.OPEN`（MEDIUM）、`QC.REVIEW.DECIDE`（HIGH） |
| OpenAPI | 新增 `/actions/qc.review.open`、`/actions/qc.review.decide` |
| 事件 | 新增 `QC_REVIEW_OPENED`、`QC_REVIEW_DECIDED` |
| 枚举绑定 | `QCReviewSession → QCReviewSessionStatus (source: enum_registry)` |

动作计数从 83 变 85。三条新登记都写在 `contract_amendments.yaml` 里，且
`freeze_manifest.py --write` 重签——**先登记修订、再改派生文件、最后重签**，
顺序不能反：否则"派生文件改了但没人签字"就会变成静默漂移。

### 2.2 缺陷是值对象，不是独立聚合

`QCIssue` 建模为 frozen dataclass，随会话的 `findings` 列（JSONB）一起存。

**为什么不给 `QCIssue` 建独立表与状态机**：21.1 给的九项是"这条缺陷能不能被
复核"的门槛，而缺陷的生命周期（派生 ReworkTask、修复后复核关闭）属于**返工域**，
尚未建模。现在建表只能得到一个"能创建、不能推进"的骨架——那比"明确地说还没
做"更难维护：后来的人会以为返工流程已经有了，只是没接线。

**为什么保留检测器多给的字段**（`extra`）：8.9 的检测器输出比契约九项更多
（confidence / evidence_refs / detector_eligibility / category）。校验必填项时
顺手丢掉未知字段，等于让证据在落库时被裁剪——上游再也拿不回自己写进去的数据。
因此 `from_mapping` 只对**必填项**与**取值枚举**做校验，其余原样保留。

### 2.3 结论只追加，不改写；生效值取最新一条

`decisions` 是历史列表：对同一缺陷改判是**追加一条新记录**，不是覆盖旧记录。
`latest_decisions()` 给出"生效值"口径。

改写历史会让"谁在什么时候判了什么"永久丢失，而 QC 的全部价值就在这条链上——
Gate 要回答"这个 CRITICAL 谁批的豁免"，答案必须能追溯到人，且不能是"最后一次
覆盖后剩下的那个说法"。

与之相对，`RESOLVED` 之后**拒绝**新结论（`STATE_TRANSITION_DENIED`）：已解决
的会话若能被事后改写，等价于"会话冻结"这条规则不存在。要重新结论，开新会话。

### 2.4 状态推进要满足前置条件，而不只是边合法

只有登记边是不够的：

- 进入 `DECISION_PENDING` 要求**每个缺陷都有结论**（否则会话会带着未裁决的
  问题进入终局）；
- 进入 `RESOLVED` 还要求**没有未闭合缺陷**：`NEEDS_MORE_EVIDENCE`（证据不足）
  与 `CONFIRM_DEFECT`（已确认待返工）都算未闭合（见 §2.10）；
- `CLOSED` 只能从 `GATE_ELIGIBLE` 来（31.5 裁决 ④）。

错误码刻意分工：**前置条件不满足 = 422 `VALIDATION_ERROR`**（输入不完整，补
数据即可），**边未登记 = 409 `STATE_TRANSITION_DENIED`**（调用顺序错，改调用
即可）。混用会让调用方分不清该补什么。

### 2.5 状态机三层镜像（契约 / 领域 / 数据库）

| 层 | 载体 | 作用 |
| --- | --- | --- |
| 契约 | `state_machines.yaml` C.7 + `enum_registry.yaml` | 权威定义 |
| 领域 | `state_machine.py::QC_REVIEW_SESSION_ALLOWED_TRANSITIONS` | 服务层判定 |
| 数据库 | 迁移 `f1a2b3c4d5e6` 的转移表 + 守卫触发器 | 直连 SQL 也拦 |

数据库层不是冗余：ADR-R20 的教训是"状态机只在 Python 里"时，一句
`UPDATE ... SET status='CLOSED' WHERE status='RESOLVED'` 就能绕过全部校验。
守卫触发器做三件事，缺一不可：

1. **未登记边拒绝**（读转移表比对 `OLD.status → NEW.status`）；
2. **不可变内容列**：白名单之外任何列变化即 RAISE。白名单只有
   `decisions`（追加结论）与 `reviewers`（登记参与人）；`findings` /
   `reference_set` / `qc_report_ref` / `asset_version_id` 全在不可变侧——
   允许事后改输入，等于允许把"复核了什么"改写成另一个更好看的故事；
3. **禁 DELETE**：删会话等于抹掉证据链。

转移表与白名单在迁移里写成**模块字面量**而不是 import 领域代码：迁移是历史
快照，若它随代码变化，重放同一个 revision 会得到结构不同的库。代价是"代码改了、
迁移没跟上"不会被任何闸门发现，因此专门有
`tests/unit/test_qc_review_parity.py` 把四份副本（契约链 / 枚举 / 领域表 /
迁移字面量）互相钉住。

### 2.6 "可表示 ≠ 可进入"：`WAIVE_WITH_AUTHORITY`

`enum_registry` 把 `WAIVE_WITH_AUTHORITY` 登记为 `QCReviewSession` 的**状态值**，
而 C.7 状态链里没有它，21.1 又把它列为**人工结论类型**（五种 `human_decision`
之一）。删掉它 → enum_parity 报缺值；给它造边 → 凭空发明一条来源边。

处理方式与 `GateDecisionValue.OVERRIDDEN` 同口径：**保留取值、不建任何转移边**。
它是"可表示、不可进入"的状态——这在类型上也说得通（缺陷级豁免不需要把整个
会话推进到一个叫"豁免"的状态）。差异登记在本 ADR §3，待契约裁决。

### 2.7 CI 与红线不可豁免

两处拦截，刻意都在领域层再拦一次：

- `ReviewDecision.record` 要求主体是 **HUMAN**（21.2：AI 只发现与提供证据，
  不单独裁决）。授权层按动作角色拦一次，这里拦第二次——写进 `decisions` 的
  主体类型是要长期可追溯的事实，不能只靠"入口恰好没放行"。
- `WAIVE_WITH_AUTHORITY` 必须带 `waiver_authority` + `waiver_reason`，且
  **只对非 CRITICAL 有效**。4.3 说 CRITICAL 必须阻断、"不能通过普通 Override
  绕过"，21.1 说不得豁免法律/版权/安全红线——两条在这里合成一道闸。

### 2.8 跨域只读：QC 报告引用经端口（ADR-R28）

会话必须绑定一份真实存在、且**属于同一项目与同一资产版本**的 QC 报告。
这个校验经 `QCReportRefPort` 完成，实现放在组合根
（`apps/api/resource_ownership.py::AssetQCReportRefAdapter`），用 `svc_asset`
身份读 `asset_reports`——qc_governance 不获得资产域任何表权限。

没有这条校验，会话可以引用一个不存在的报告，复核就变成"凭空宣布有人看过证据"。
报告**不可见**与**不存在**返回同一个 404：区分会让跨租户探测成为可能。

### 2.9 身份不从请求体来

`created_by` / `decided_by` 一律取自可信 Principal。客户端能填就是身份伪造——
与 GateDecision 同口径（`test_decided_by_cannot_be_forged_in_body`）。

授权对象还必须等于实际操作资源：授权层按请求体的 `project_id` 判定，服务层
再比对 `session.project_id`（不一致 422）。少了这一条，具备多项目权限的主体
可以在 A 项目的授权下裁决 B 项目的会话——RLS 只能挡住"完全无权"，挡不住
"有权但对象错了"。

### 2.10 已确认缺陷 ≠ 已解决缺陷

`unresolved_issue_ids` 的初版只认 `NEEDS_MORE_EVIDENCE`。后果是一条 CRITICAL
缺陷被判 `CONFIRM_DEFECT` 后，会话仍可 `RESOLVED → GATE_ELIGIBLE → CLOSED`——
**确认即放行**。这与 4.3「CRITICAL 必须阻断」和 21.2「对已确认缺陷创建修复任务
并进入 DAG」都冲突。

修正后的口径：只有 `FALSE_POSITIVE` / `ARTISTIC_INTENTIONAL` /
`WAIVE_WITH_AUTHORITY` 把缺陷移出"未闭合"；`NEEDS_MORE_EVIDENCE` 与
`CONFIRM_DEFECT` 都阻断 `RESOLVED`，拒绝时分别列出 `evidence_pending_issue_ids`
与 `pending_rework_issue_ids`（"等证据"和"等返工"是两种不同处置）。

**业务后果要说清楚**：返工域未建模时，含已确认缺陷的会话**永远不能**走到
`GATE_ELIGIBLE`。这不是缺陷——它就是"这个 QC 报告发现了真问题"的正确表达。
恢复路径是修复后重出 QC 报告、开新会话复核；本会话留在 `DECISION_PENDING`。
唯一在本会话内解除阻断的方式是**改判**（追加一条新结论，历史保留），因此
"放行"永远带着一条署名记录。

## 3. 诚实登记的债务（不允许伪装通过）

1. **`QCIssue` 未建独立表**（§2.2）：缺陷随会话 JSONB 存储，无法按 `issue_id`
   索引查询、也无法承载独立生命周期。解除条件：返工域（ReworkTask）立项时
   一并决定缺陷表的归属与状态机。
2. **`reviewers` 不是会签**：它记录"谁参与过"，不表达"多方是否都同意"。
   C.7 note 提到的"多人冲突触发裁决"**未实现**——当前任一复核人都能追加结论，
   冲突只能靠追加与历史追溯发现，没有阻断机制。
3. **多人冲突裁决未实现**：同上。真正落地需要 ADR-R11-G 联合审批（AND 语义）
   的签名能力；授权层现在识别到 `+` / `、` 一律拒绝，是正确的 fail-closed。
4. **`WAIVE_WITH_AUTHORITY` 契约差异**（§2.6）：enum_registry 登记为状态、C.7
   不认、无转移边。需契约裁决它到底是状态还是结论类型。
5. **没有读端点**：动作矩阵里没有 QC 复查的 READ 动作，按 openapi 头部规则
   未注册 Action 不得暴露端点。会话只能从"开启/裁决"的响应里看到。读能力随
   QC 工作台读接口一并设计。
6. **severity 不含 `BLOCKER`**：`BLOCKER` 只在 7.6 的 ReworkTask 行出现，属
   返工单级别。把它并入缺陷级别会让"这条缺陷到底多严重"随返工单变动。
   `QCIssueSeverity` / `QCIssueHumanDecision` 在 `enum_bindings.yaml` 里是
   **显式豁免**（enum_registry 未登记 QCIssue 对象），取值一致性由
   `test_qc_review_parity.py` 对照字面量守住。
7. **`GATE_ELIGIBLE` 不等于 Gate 通过**：它只表示"证据齐备、可以进 Gate"。
   Gate 结论只能由 `PRODUCTION.GATE.APPROVE` 产生（ADR-R23 单一裁决点）。
8. **不派生 ReworkTask**：21.2 的"对已确认缺陷创建修复任务"仍缺载体（同债务 1）。
9. **缺陷会话没有终态出口**（§2.10 的直接后果）：含已确认缺陷的会话只能停在
   `DECISION_PENDING`，C.7 没有 `ABANDONED` / `SUPERSEDED` 这类"本条复核作废"
   的状态。长期看这些会话会堆积；解除条件是在契约里补一个"被取代"终态
   （指向新会话），而不是允许它们 `RESOLVED`。
10. **`decisions` 的追加约束已下沉数据库**（迁移 c4e5f6a7b8d9 加前缀超集校验），
    但"谁是合法复核人"仍只由角色决定，没有按缺陷维度分派——`reviewers` 是
    参与人清单，不是分工表。

## 4. 后果

- 正面：C5 从"文档要求"变成**结构事实**——九项必填在领域层校验、状态链在
  领域层判定、在数据库层再拦一次；`RESOLVED→CLOSED` 跳关有三个独立证据点
  （领域 409 / 数据库触发器 / 直连 SQL 拒绝）。"这份证据复核过吗、谁判的、
  判了几次"从"翻 outbox 猜"变成一次主键级查询。
- 代价/风险：会话无法物理清理（守卫禁 DELETE），测试与运维只能靠唯一键
  （`tenant_id, project_id, qc_report_ref`）与项目隔离避免互扰；`reviewers`
  语义容易被误读为会签（§3 债务 2），API 文档必须写明。
- 计数变化：schema 对象仍 85（`QCIssue` / `QCReviewSession` 已存在，本轮只补
  字段）、状态机 42（`QCReviewSession` 条目本就在契约里，本轮补的是运行时）、
  Action 85（+2）、事件 28（+2）；新增迁移 1 个（`f1a2b3c4d5e6`）、
  领域文件 4 个、仓储 2 个、服务 1 个、路由 1 个、组合根适配器 1 个、
  测试文件 3 个（领域 / 四副本一致性 / 集成与数据库守卫）。
- 对 Phase 1 验收的贡献：C5 由"无证据"转为**有证据**——证据为
  `docs/evidence/phase1-c5-qc-review.md`，指向
  `tests/integration/test_qc_review_api.py`（22 个用例）与
  `tests/unit/test_qc_review_domain.py`（24 个用例）、
  `tests/unit/test_qc_review_parity.py`（11 个用例）。
