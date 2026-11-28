# 长流程执行引擎边界复核（2026-11-28）

## 0. 这份复核在纠正什么

`docs/audits/2026-11-23-blueprint-vs-repo-reconciliation.md` §4 把 TC-1 记为「待 Owner 三选一裁决（BLOCKED）」。该表写于 **2026-11-23 当天、早于 ADR-R44 签署**，此后 ADR-R44 §4 已由项目所有者逐项裁决，TC-1 的结论是：

> **(b) 仅新流程引入 Temporal，R42 不迁**（DECIDED，2026-11-23）

因此「TC-1 待裁」「PR-06 是缺口」都是过期读数。本复核不改任何代码、依赖或配置，只做三件事：确认裁决边界、给出 R42 已具备的持久化执行能力证据、写明将来唯一允许的扩展路径。

`[V-VERIFIED][E1+E2+E5]`

---

## 1. 裁决边界（引用，不重述为新决策）

| 项 | 内容 | 出处 |
|---|---|---|
| TC-1 | (b) 仅新流程引入 Temporal，R42 不迁 | ADR-R44 §4 |
| 禁止 | 不得迁移 R42 的业务事务/回执/幂等/补偿权威 | ADR-R44 §4 执行约束 |
| 禁止 | 不得把「Temporal 用于新流程」扩大为「迁移 R42」 | ADR-R44 D3 |
| 前置 | 任何引入须独立 ADR 修订 + 冻结重签，且不与领域重构捆绑 | ADR-R44 D5 |
| 现状 | `temporalio` 仅为 SDK 依赖，无服务、无 workflow 代码、无 `workers/temporal` | 全仓检索（2026-11-28） |

**本次特意没有做的两件事**（避免用「长期最优」扩大解释权）：

1. **没有移除 `temporalio` 依赖**。它是 R44 (b) 里「留新流程能力」的入口，删除它等于单方面把 (b) 改成 (a)。
2. **没有部署 Temporal 服务或编写 workflow**。裁决是方向，不是切换（R44 §4.1）。

---

## 2. R42 已具备的持久化执行能力（E5 取证）

| Temporal 的核心承诺 | R42 对应实现 | 证据 |
|---|---|---|
| 持久化队列 + 租户隔离 | `outbox_events.tenant_id NOT NULL` + `ix_outbox_tenant` + 分区 | `packages/shared/outbox.py`；`tests/unit/test_outbox.py`；`tests/integration/test_outbox_partitions.py` |
| 重试 / 死信 / 重放审计 | relay consumer 重试列、死信查询与重放、webhook 熔断 | `workers/relay/consumer.py`、`webhook.py`；`test_outbox_relay_retry.py`、`test_outbox_dead_letter_list_pg.py`、`test_outbox_dead_letter_replay_pg.py` |
| 领取 + 心跳 + 超时回收 | 租约三元组（`lease_owner` / `lease_expires_at` / `attempt_count`）；常驻循环空闲时 `sweep_expired()` 回收 | `services/production_planning/domain/generation_task.py`；`workers/generation/worker.py:185-213`；`tests/unit/test_generation_lease.py`、`test_generation_worker.py`、`tests/integration/test_generation_worker_concurrency_pg.py` |
| 崩溃后不重复计费 | PR-G：调用模型前查历史未确认意图，对象在则续 T1，缺则 `abandon`；绝不盲目重调 | `workers/generation/executor.py:307-411`；`tests/unit/test_generation_executor_recovery.py` |
| 终态只由被确认的结果写入 | T1 → `AWAITING_ASSET_REGISTRATION`；`asset_version_id` 只由 T2 回执经 T3 写回 | `workers/generation/asset_registrar.py`、`workers/relay/asset_registration.py`；`test_asset_registration_saga_e2e.py` 等 |
| 可审计决策链 | outbox replay 审计 + OpLog 追加（C4/C7） | ADR-R42；`test_outbox_dead_letter_replay_pg.py` |

本次实跑：`runs/2026-11-28/saga-evidence.log` —— 6 个单测文件共 68 个用例全部通过（exit 0）。

结论：Temporal 在本仓库要补的能力，Saga 已有对应实现与测试。R44 的 (b) 因此不是「技术落后」，而是「不为已有能力再买一套分布式系统」。

---

## 3. 将来唯一允许的扩展路径

如果新流程真的需要 Temporal（而不是「顺手用一下」），必须同时满足：

1. **独立 ADR**：写明该流程为什么无法用 Saga 步骤表达（具体到哪一步做不到：跨天人工等待 / 步骤数 / 跨部署单元重放）。
2. **不共享状态**：不与 R42 链共用任务表、回执语义或幂等键；跨引擎的边界只允许是业务事件，不是状态同步。
3. **冻结重签**：按 R44 D5 走独立修订，不与领域重构同 PR。
4. **可回滚**：新流程失败不得影响 R42 链的在途任务。

缺任一项即视为无效提案，按 D2 维持现状。

---

## 4. 与本次其它改动的分工

- 提示词与产物形态的建模方向：见 `docs/adr/ADR-R47-generation-spec-derivation.md`（派生一次、冻结为 GenerationSpec；不建提示词库）。
- 网关单段取证脚本与交接单更正：见 PR #76（`73a3433`）。
- 本复核不涉及任何契约、迁移、依赖或部署变更。
