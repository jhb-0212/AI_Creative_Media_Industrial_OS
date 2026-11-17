# ADR-R24：交付清单审批消费与状态推进（D4）

- 编号：ADR-R24
- 状态：已采纳
- 日期：2026-11-17
- 相关包：D（Phase 1 主链与交付闸门）
- 契约依据：`action_registry.yaml#DELIVERY.MANIFEST.APPROVE / RELEASE`；
  `state_machines.yaml#DeliveryManifest`；`ADR-R11-G`（通用审批引擎）；
  `ADR-R22`（清单是 append-only 证据引用）

## 背景

D1 把交付清单做成 append-only（禁止一切 UPDATE），清单只能停在 DRAFT——这是
刻意的"起点"实现，ADR-R22 把它登记为明确债务。但契约状态机要求
`DRAFT → VALIDATING → VALIDATED → RELEASED`，`DELIVERY.MANIFEST.APPROVE` 与
`RELEASE` 两个 ACTIVE 动作一直没有落点。D3 落地了通用审批引擎后，业务侧消费
APPROVAL_TICKET_COMPLETED 并推进自身状态，成为 Phase 1 交付闭环的最后一段。

此时有两条看似可行的路，都有结构性缺陷：

1. **给清单开表级 UPDATE，让审批端点直接写 status**：会在同一条 UPDATE 上把
   D1 守住的"内容不可变"一起打开——`UPDATE ... SET asset_refs=..., status='VALIDATED'`
   能同时篡改交付内容并自我放行。这正是资产状态机在 C1（ADR-R20）修掉的那类洞。
2. **APPROVE 做成 HTTP 端点**：`allowed_principals='交付负责人+内部 QC/权利职责'`
   含 `+`（AND 语义），授权层按设计恒 `fail-closed`，客户端也无法在请求体里
   声明"第二方已同意"。做这样的端点等于把 AND 悄悄读成 OR。

## 决策

### 1. 内容不可变 + 状态沿登记边推进（复用 C1 模式）

清单的列分成两类，在三个层次一致：

| 层次 | 内容列 | 状态投影列（status / entity_version / updated_at / audit_ref） |
|---|---|---|
| 端口 | 无更新方法 | `save_status(expected_version)` 窄端口 |
| SQL 授权 | 无 UPDATE | 仅四列的列级 UPDATE |
| 触发器 | 逐列比对禁止变化 | status 变化必须命中 `delivery_manifest_status_transitions` |

转移表只播种三条有明确来源的边（DRAFT→VALIDATING→VALIDATED→RELEASED），应用
角色对转移表只读——"加一条边自我放行"在数据库层被关掉。四个异常态
（INVALID/STALE/REVOKED/SUPERSEDED）契约只写"可进入"未给来源边，不造边，
登记在 `state_machine_gaps.yaml`。

`VALIDATING` 不是内存临时值：消费时 DRAFT→VALIDATING 与 VALIDATING→VALIDATED
**分两次落库**。集成测试实证过——若只在内存连推两步后直接写 VALIDATED，触发器
会把它当作未登记的 DRAFT→VALIDATED 跳跃拒绝。这恰好证明数据库约束真的在起作用。

### 2. 结论来自权威事件，客户端无法声明

APPROVE 没有专用 HTTP 端点：

- 发起走 `WORKFLOW.START_APPROVAL`（target_action_key=DELIVERY.MANIFEST.APPROVE，
  resource_type=DeliveryManifest）；投票走 `WORKFLOW.SUBMIT_VOTE`；
- 引擎发 `APPROVAL_TICKET_COMPLETED`，`DeliveryApprovalService` 从 outbox 读取，
  校验 `action_key / resource_type / resource_id / decision=APPROVED` 后推进状态；
- 服务层没有任何"审批结果"入参，`gate_pass` 类字段在对象契约里根本不存在。

"未发生就声明通过"因此在结构上做不到，不是靠校验函数自觉。

### 3. 消费由 asset 域 worker 完成，不塞进 outbox relay

relay 用 OUTBOX_RELAY 角色，只能读事件 + 回写投递状态列，**写不了业务表**。
把状态推进塞进 relay handler 会破坏这条安全边界。因此独立一个
`workers/delivery`：用 ASSET 角色，逐租户消费 outbox 的完成事件。租户列表由
运维显式提供（不擅自扩大资产域的跨租户枚举权限）。

幂等以清单状态为准，而非"事件是否投递"：同一事件重放时清单已 VALIDATED 且
audit_ref 一致 → 直接返回；不同事件试图再推进终态 → 拒绝。这与 outbox
「至少一次」语义相容，无需 worker 侧维护已读表。

### 4. RELEASE 是独立授权动作

`DELIVERY.MANIFEST.RELEASE` 的 `allowed_principals='交付负责人/授权发行主体'`
用 `/`（OR 语义），可以直接授权。API 层 `require_action` 完成动作授权 + 资源级
授权（项目取清单自身），用例层只校验 VALIDATED 前置并推进 RELEASED。Agent 由
CRITICAL 红线在授权层拒绝。响应在 commit 前用内存对象构造——SET LOCAL 的 RLS
上下文随事务结束清除，commit 后重查会误报 404（D3 实测教训）。

## 3. 闸门证据

| 性质 | 证明方式 |
|---|---|
| 内容不可变（含超级用户） | `tests/integration/test_delivery_manifest_immutability.py`：改内容列被触发器拒绝 |
| 状态沿边推进 | 同上：DRAFT→VALIDATING 成功、DRAFT→RELEASED 被拒、异常态不可达已登记 |
| 最小授权 | 同上：列级 UPDATE 恰为四列、转移表对应用角色只读 |
| 结论不可伪造 | `tests/unit/test_delivery_approval_service.py`：无事件/REJECTED 事件不推进 |
| 幂等与终态 | 同上：同一事件重放无副作用、不同事件改写终态被拒 |
| 并发可控 | 同上：乐观锁冲突转领域错误；集成测试真实两阶段落库 |
| 端到端 | `tests/integration/test_delivery_approval_consumption.py`：真实 RLS 下 event→VALIDATED→RELEASED，跨租户事件不可见 |
| 契约对齐 | contract_check：状态机 3 条转移一致、事件 19/19、运行时覆盖 39 个 ACTIVE |

## 4. 未闭合项（诚实登记）

1. **跨域证据对象存在性校验未实现**：清单引用的 PictureLock / 权利证据 /
   终检报告对象在 Phase 1 主链尚未落地（报告 F5）。Gate4 PASS 的判定由通用
   审批引擎的 qc_rights 分组投票承载（approval_bindings.yaml），本服务消费其
   裁决结果，不直连 qc_governance 库表（跨模块读他人数据违反 V2.0.5 第 14 章）。
   待那些域落地后，在此接入"引用对象确实存在且有效"的校验。
2. **worker 租户发现未自动化**：tenant 列表由运维显式提供；跨租户枚举能力
   （`outbox.list_active_tenants()`）目前只授给 relay 角色。要自动化需一次显式的
   授权扩展，不在本次范围。
3. **DELIVERY.MANIFEST.APPROVE 在运行时台账仍是 not_implemented**：因为它没有
   以自身 key 命名的独立端点（联合审批由通用引擎承接）；实际业务闭环已由
   `WORKFLOW.START_APPROVAL` + 消费用例完成。台账原因已如实写明。
4. **重建执行器仍未实现**：`rebuild_projection()` 只给出输入与哈希，真正跑一次
   渲染重建需要 workers/delivery 与模型网关对接，仍为空缺（ADR-R22 同款债务）。
