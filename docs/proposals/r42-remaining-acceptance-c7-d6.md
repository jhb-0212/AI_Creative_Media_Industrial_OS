---
doc_type: proposal
status: in_progress
updated_at: 2026-11-24
authoritative_sources:
  - contracts/phase0/contract_amendments.yaml
  - contracts/runtime/action_implementations.yaml
  - docs/audits/2026-11-24-r42-authorization-reconciliation.md
open_items:
  - item: >-
      C7 手动 QC 入队端点未做来源核验（裁定 S2 采纳「保留端点 + 补来源核验」，实施未开始）：
      services/qc_governance/api/qc_task_routes.py:110-136 的 POST /qc/tasks 只依赖
      authorize_on_parent 的项目级授权；QCTaskService.enqueue 仅挡「同 generation_task_id
      重复入队」，不校验生成任务是否终态、asset_version_id 是否真实登记完成，且
      quality_profile_version 直接取自请求体——同时冲突 C7（QC 只消费登记完成事件）
      与 C4（质量口径创建时冻结）。债登记于 A36 residual_risks 第 1 条与
      docs/audits/2026-11-24-r42-authorization-reconciliation.md §4.1
    source: "contracts/phase0/contract_amendments.yaml#amendments"
  - item: >-
      D6 死信人工重放无代码入口（裁定 S3 采纳「受权重放 + 审计事件」，实施未开始）：
      workers/relay/consumer.py:24,120,224 三处注释声称死信「可人工重放」，但仓库内
      没有任何重放端点、脚本或 CLI——「可查」成立，「可重放」不成立，属用注释声明能力。
      债登记于 A36 residual_risks 第 2 条与
      docs/audits/2026-11-24-r42-authorization-reconciliation.md §4.2
    source: "contracts/phase0/contract_amendments.yaml#amendments"
---

# R42 剩余验收：C7 入队来源核验 + D6 受权重放

> 状态以文首 front-matter 为准（`status: in_progress`，未完成项见 `open_items`）。
> 裁定已采纳，**实施未开始**（0/2 项落地）。
> 裁定来源：2026-11-24 工程审查建议（S1 有条件认可 / S2 方案一 / S3 方案一），
> 登记于 `docs/audits/2026-11-24-r42-authorization-reconciliation.md` §8。
> **本提案不代表项目 Owner 签署**：S1 的解除条件未满足，上游 `[V-BLOCKED]` 状态不变。

---

## 1. 为什么这两项不能靠文档了事

两项的共同形态都是「声明了一个能力，但能力不存在」：

| 项 | 声明处 | 实际 |
|---|---|---|
| C7 | `QC.TASK.ENQUEUE` 已登记（`action_registry.yaml:1167` / `openapi.draft.yaml:3512` / `action_implementations.yaml:55`），契约 note 写「在生成终态…时创建 QUEUED」 | 端点对**任何** project/asset 都能建任务，质量口径由调用方自报 |
| D6 | `consumer.py` 三处注释写「必须可查、可人工重放」 | 无任何重放入口；死信只能靠 DBA 手工 SQL 处理 |

两者的危害不对称，但根子相同：**契约/注释描述了不变量，运行时没有对应约束**。所以两项都不接受「改措辞」的方案——那会把缺口从「可检测」降级为「不可检测」。

---

## 2. C7：保留端点 + 补来源核验（S2 方案一）

### 2.1 六条开发要求（验收判据）

1. **定位**：端点仅供有权限的操作人员执行人工恢复/运营干预，不作为普通业务入队入口。契约与文档措辞同步改口径（不再表述为「生成终态入队」的唯一路径，而是「补偿入口」）。
2. **身份**：`Principal` 一律取自可信认证上下文（既有 `require_role` 依赖），不得从请求体读取任何身份/租户/操作者字段。请求体出现这类字段应在模型层 `extra="forbid"` 直接拒绝。
3. **归属与状态重核验**：`project_id` 不可信。必须按 `generation_task_id` 读权威数据，核验：任务存在且 `SUCCEEDED`、`asset_version_id` 与任务终态绑定值一致、任务 `project_id`/`tenant_id` 与请求一致、`quality_profile_version` **取任务冻结值**（请求体自报值只允许「不传或与冻结值相同」，冲突即拒绝）。
4. **幂等事实唯一**：人工与 relay 自动路径共用同一幂等事实（`qc_tasks.generation_task_id` 唯一 + 服务层显式冲突检查），**不得**给人工路径留 `skip_validation` / `force` 之类旁路参数——那等于把洞换个名字。
5. **可追踪审计**：写入操作者、来源（`RELAY` / `MANUAL`）、原因、关联任务、操作结果。注意：`QC_TASK_QUEUED` 事件的 `source`/`reason` 字段是**契约变更**，必须按冻结治理走「修订登记 → 派生 → 重签」，不得先实现后补契约。
6. **真实 DB 负向测试**：越权、跨租户、重复提交、非法状态（任务非 SUCCEEDED / 资产未登记完成 / 质量口径冲突）四类，各自断言 SQLSTATE 或领域错误码 + 回读影响行数。RLS 会把越权写静默过滤成 0 行，不认「没抛异常即通过」。

### 2.2 验收重点

> 保留人工恢复能力，但不允许人工入口绕过正常业务状态机。

判据：把人工入口与自动入口对同一组非法输入喂进去，**两者的拒绝集合必须相同**。差异即缺陷。

---

## 3. D6：受权重放 + 审计事件（S3 方案一）

### 3.1 八条开发要求（验收判据）

1. **专门权限**：新增独立权限点（建议 `OUTBOX.DEAD_LETTER.REPLAY`，非 relay 运行时角色持有）。relay 角色只有 outbox 的 SELECT/UPDATE，重放操作必须由受权主体在**独立连接**上执行，不把运维能力塞进 relay 进程。
2. **必须指定原始事件**：入参是 `event_id`，不是「重新构造一个同类请求」。禁止「复制一条新事件代替重放」——那会绕过 `event_id` 幂等、破坏 at-least-once 语义。
3. **重放前核验**：原始事件存在、租户归属与操作者上下文一致、当前确实处于死信态（`dead_lettered_at IS NOT NULL` 且 `published_at IS NULL`）、关联业务对象仍在且处于可推进状态。任一不成立即拒绝并说明原因。
4. **保留原始事件 ID + 独立 replay ID**：`event_id` 不变（它是业务幂等键），重放动作自身有独立 `replay_id`，两者在审计里关联。
5. **幂等**：重复点击/并发执行不得产生重复业务副作用。落地方式：`replay_id` 由调用方传入并作为审计唯一键（或对 `(event_id, 在途标记)` 加排他约束），并发第二次进入直接返回「已在进行中」而非再跑一遍。
6. **审计完整**：操作人、原因、原始事件 ID、replay ID、时间、执行结果、失败原因——一条 append-only 记录，缺一项则该次重放视为未发生。
7. **失败保留可追溯状态**：重放失败不得删除死信行、不得覆盖原始 `attempts`/`dead_lettered_at`/失败原因。原始失败事实与「有人尝试重放但失败」是两个事实，必须并存。
8. **测试口径**：真实 PostgreSQL + 多 worker 并发 + 重复重放三组用例。多 worker 那组必须真的起两个并发 claim，而不是顺序调用两次。

### 3.2 需要先钉死的一个实现取舍（不钉死必然返工）

「重放」到底动哪一层，有三种做法，只有一种对：

| 做法 | 后果 |
|---|---|
| 复制一条新事件（新 `event_id`）投递 | ❌ 绕过 `event_id` 幂等；handler 会当新业务事件处理；业务副作用可能执行两次 |
| 直接调用 handler（跳过 outbox） | ❌ 绕过 relay 的 claim/退避/死信与租户隔离；失败没有任何投递侧记录 |
| **清死信标记 + `next_attempt_at=now()` + `attempts` 归零，交回正常 claim** | ✅ 重放仍走 claim → handler → ack；`event_id` 不变；失败重新按退避累加，可再次进死信 |

第三种是唯一满足「受控恢复，而不是绕过状态机重新执行一次」的做法。业务副作用的幂等由各 handler 自己保证（T2 已由回执唯一键 + 请求指纹兜底），重放层只负责「让事件重新具备被领取的资格」，不越权替下游做幂等。

### 3.3 验收重点

> 重放是受控恢复操作，不是绕过状态机重新执行一次。

---

## 4. 实施顺序与依赖

| 序 | 项 | 依赖 | 触碰面 |
|---|---|---|---|
| 1 | D6 受权重放 | 无（relay 侧独立） | 新权限点 + 审计事件登记 + 运维入口；不动业务域 |
| 2 | C7 来源核验 | 需要给 `QC_TASK_QUEUED` 载荷加 `source`/`reason`（契约修订） | QC 服务层 + 契约修订 + 负向测试 |

先做 D6 的理由：它不引入契约对象、不触碰业务状态机，风险面最小；而 C7 要改事件载荷，需要一次完整的契约修订循环，放在后面可以把两次变更的登记合并到同一轮。

## 5. 本提案不做什么

- 不改 ADR-R42 正文（S1：正文保持历史原文不动）。
- 不解除上游审计的 `[V-BLOCKED]`——S1 的四项解除前置未满足，解除权在 Owner。
- 不删除 C7 端点，不改 D6 注释措辞（改措辞会让缺口不可检测）。
