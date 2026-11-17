# ADR-R11-G：通用审批工作流引擎（业务与审批经 Outbox 解耦）

- 编号：ADR-R11-G
- 状态：已采纳
- 日期：2026-11-16
- 文件命名说明：按任务指定使用 `ADR-R11-generic-approval-workflow.md` 文件名；
  但 `ADR-R11` 已用于「幂等键作用域、逐跳事件与内容列不可变」，
  因此本文编号加后缀 `-G`，不覆盖既有决策。
- 关联：ADR-R18（统一授权决策）、ADR-R23（Gate 决策事实）、
  `docs/audits/2026-11-16-review-round3-response.md`（包 D）

## 1. 背景：审批逻辑正在变成第二个业务系统

包 D 落地交付清单与 Gate 决策后，暴露出一条结构性缺陷：

1. `PRODUCTION.GATE.APPROVE` 的允许主体是描述性短语，靠运行时台账解释；
2. `DELIVERY.MANIFEST.APPROVE` 的允许主体含 `+`（AND 语义），现有授权层
   一律 fail-closed 拒绝——这是正确行为，但意味着该动作永远不可调用；
3. 联合审批还有 `COMMUNITY.REUSE.APPROVE`（三方）、
   `CREATOR.RELEASE.REVOKE`（两方），每个都自带角色、状态、事件与并发规则。

如果继续在每个业务域里写 `if 审批通过 then 改状态`，会得到 N 套互相漂移的
审批实现：角色判定不同、驳回语义不同、并发行为不同、事件名不同。
第三轮审查已经抓到过同类问题（职责分离在两个 service 各写一份）。

## 2. 决策

### 2.1 审批是一等公民，不是业务表上的字段

新增独立域 `services/workflow` 与独立数据库角色 `svc_workflow`，三张表：

| 表 | 性质 | 内容 |
| --- | --- | --- |
| `approval_flows` | 配置事实（append-only） | 目标动作、资源、会签/或签模式、参与者分组、规则版本 |
| `approval_tickets` | 状态机投影（可更新状态与版本） | 当前状态、完成时间、乐观锁版本 |
| `approval_steps` | 投票事实（append-only） | 谁、代表哪个分组、什么结论、何时、依据 |

业务表里**不放** `approved_by` / `approval_status` / `gate_pass` 这类字段。
业务对象只持有"审批票据 id"的引用：审批过程在引擎里，业务只消费结论事件。
这样"某状态是怎么来的"永久可追，而不是靠一张可改的状态列。

### 2.2 AND / OR 在引擎里判定，授权层不拆 `+`

`packages.identity.authorization` 对含 `+` / `、` 的动作继续返回
`JOINT_APPROVAL_UNSUPPORTED`（不得把 AND 读成 OR）。通用审批动作本身
（`WORKFLOW.START_APPROVAL` / `WORKFLOW.SUBMIT_VOTE`）不含 `+`，可以正常授权；
**多主体语义由票据的分组与模式表达**：

- `ALL`（会签）：每个分组至少一票 APPROVE 才完成；任一分组 REJECT 立即驳回；
- `ANY`（或签）：任一分组 APPROVE 即通过；所有分组均 REJECT 才驳回。

分组不是角色集合的直接映射，而是经 `contracts/runtime/approval_bindings.yaml`
 Data 化：`DELIVERY.MANIFEST.APPROVE` → `ALL`，两组
「交付负责人」与「内部 QC/权利职责」。原文指纹与冻结契约逐字校验，
与 `principal_bindings` 同一口径：不改冻结契约、漂移即 fail-fast。

### 2.3 业务与审批通过 Outbox 解耦

- 业务服务创建审批意图 → 引擎票据 → 投票事实 →
  完成后发 `APPROVAL_TICKET_COMPLETED`（payload 含目标 action、资源、结论、
  分组投票摘要、票据 id）；
- 业务域消费者按 `event_id` 幂等处理该事件，再推进自己的状态机；
- 业务服务**不直接调用**审批引擎的服务层（跨服务直连被 R2 禁止），
  也**不接受客户端声明的审批结果**（如 `gate_pass`）。

这条链把"谁批的"与"批完做什么"分开：票据是唯一裁决点，业务状态是它的投影。

### 2.4 并发与不可伪造

- 投票事实 append-only：端口无 `save`；数据库只授 `SELECT/INSERT` +
  触发器；同一 `(ticket_id, group, voter_subject_id)` 唯一，重复投票返回既有事实；
- 票据状态用乐观锁 `version`：并发投票冲突抛 `ConcurrentModificationError`，
  由服务层重读后按当前状态重新判定，而不是"后写覆盖"；
- 结论事件与票据完成同事务，且以 `ticket_id + decision` 为幂等键：
  并发驳回只产生一个事件；
- `decided_by` 只能来自已验签 Principal；请求体不接受决策人、时间、状态；
- Agent 不得投票：`WORKFLOW.SUBMIT_VOTE` 设为 CRITICAL，授权层按主体类型拒绝，
  领域层再按 `SubjectType.AGENT` 拒绝。

### 2.5 不做什么

- 不在本轮实现 ALL 模式下的"审批顺序"（先后置）；当前只表达 AND/OR；
- 不实现委托、代理投票、超时自动通过——契约未定义，不接受客户端声明；
- 不删除已有的专用审批动作（如 `ASSET.VERSION.APPROVE`）：它们在迁移期继续运行；
  新增审批一律走引擎，已废弃项在运行时台账里写明原因。

## 3. 闸门证据

- 数据层：append-only 触发器对超级用户生效；唯一索引；只授最小权限；RLS 双策略；
- 用例层：AND 缺一组不完成、ANY 一票通过、并发驳回只发一次事件、
  无权限投票 403、越权发起 403、重复投票幂等；
- 契约层：`check_principal_bindings` 同款原文指纹校验扩展到 `approval_bindings`，
  新增动作必须在 Action Registry / OpenAPI / 冻结修订台账三处对齐。

### 3.1 测试中发现并修复的两处真实缺陷

补全负向测试时抓到两个此前一直潜伏的问题，记录在此以防回滚：

1. **commit 后读票据会误报 404（API 层）**。`submit_vote` 最初在
   `await uow.commit()` 之后调用 `service.get_ticket()` 构造响应。SET LOCAL
   注入的 RLS 上下文随事务结束清除，commit 后的查询因三个 GUC 全空而看不到
   任何行（诊断实测：同一 ticket_id，commit 前 GUC 有值可查到，commit 后 GUC
   全空查不到）。修复为在 commit **之前**读取快照，同一事务内还能看到乐观锁
   推进后的最新状态。这条教训推广到所有 RLS 资源：响应读取必须在写事务内完成。
2. **分组资格的第二套角色判定（领域层）**。`ApprovalGroup.accepts()` 最初直接
   访问 `principal.roles & self.roles`，被 W3 授权接线闸门判定为"绕过统一授权
   的第二套 RBAC"。修复为复用 `packages.identity.authorization.holds_any_role()`
   （身份包内唯一的角色比对入口），分组资格作为 `authorize()` 之后的额外约束，
   不另起判定。没有采用加豁免的做法——那会削弱闸门对未来同类问题的覆盖。
3. **审批绑定台账路径离线**。`packages/approval_bindings.py` 的台账路径最初用了
   `parents[2]`（从 `packages/identity/` 出发的层级），但该文件位于 `packages/`
   根目录，正确层级是 `parents[1]`；用错层级时台账静默读不到，所有目标动作都
   fail-closed。契约闸门直接读 YAML 所以没发现，直到服务层单测才暴露。

## 4. 未闭合（诚实登记）

- 事件消费侧：业务域消费 `APPROVAL_TICKET_COMPLETED` 后推进状态的处理器
  需逐域接入；本轮先交付引擎与事件，不假装业务状态已自动流转；
- 审批顺序、委托、超时策略未实现；
- 与 Capability Token 的结合已由 F3 收口：所有审批动作在统一 `authorize()`
  入口消费 Principal 上挂载的短时 capability，角色、Agent 红线、职责分离与
  项目范围仍在同一决策里执行；一次性 nonce 的落库去重尚未实现（见 ADR-R26）。
