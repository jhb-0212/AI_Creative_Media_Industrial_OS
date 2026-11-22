# PR-02 契约事实源对账（只读核验）

> 依据：《开发执行总纲与 PR 验收规范 V1.0》第四章 PR-02
> 性质：只读对账，不复制契约、不伪造一致性。差异逐项列明，不静默豁免。

## 1. 契约矩阵（权威位置）

| 契约类型 | 权威文件 | 规模 |
| --- | --- | --- |
| API 契约 | `contracts/openapi/openapi.draft.yaml`（+ 运行时路由由 `action_implementations.yaml` 台账对齐） | 100 action |
| 领域契约 | `contracts/schemas/object_schemas.schema.json` | 84 对象定义 |
| 事件契约 | `contracts/runtime/event_catalog.yaml` | 29 事件 |
| Agent Tool 契约 | `contracts/action_registry/action_registry.yaml`（Phase 0 未启用 Agent Tool 运行时） | 0 运行时 |
| Skill 契约 | `contracts/schemas` 内 `SkillManifest`（对象已冻结，未建运行时） | 0 运行时 |
| Worker I/O 契约 | `workers/*` 运行时（Outbox 消费）+ `event_catalog` payload | 见 §4 |
| 状态机契约 | `contracts/state_machines/state_machines.yaml` | 44 对象 |
| 权限契约 | `contracts/permission/permission_matrix.yaml` | 100 action |

## 2. 代码 ↔ 契约差异报告（实测，来源 contract_check 基线全绿输出）

| 维度 | 数量 | 说明 |
| --- | --- | --- |
| ACTIVE action | 56 | PHASE_GATED 不在实现要求内 |
| 已实现 | 35 | 运行时路由 + 授权 + schema 全覆盖 |
| 登记未实现 | 21 | 逐条有 reason（不静默） |
| Schema 显式债务 | 7 | ShotBudgetRequest / BudgetLedger / GenerationTask / ProductionRoute / CanonRevision / RightsCase / RightsManifest |
| 状态转移入口债务 | 3 | ConstraintPack 三条治理边（ACTIVE→REVOKED/STALE/SUPERSEDED） |
| 不可达状态债务 | 6 | ReworkTask.REJECTED + AssetVersion 5 个旁支 |
| 事件缺口 | 0 | 29 事件 / 29 发出点，全对齐 |

## 3. 未实现 action 清单（21 项，按域分组）

| 域 | action | 归属 PR |
| --- | --- | --- |
| 约束治理 | CONSTRAINT.PACK.STATUS.CHANGE | PR-14 |
| 生产 | PRODUCTION.ROUTE.SELECT / GENERATION.REQUEST / PRODUCTION.GATE.APPROVE | PR-17/19/24 |
| 权利 Canon | CANON.CHANGE.APPROVE / RIGHTS.STATUS.CHANGE | 超出 V1.0 MVP |
| 预算 | BUDGET.SHOT.REQUEST / BUDGET.SHOT.APPROVE | PR-25 |
| 社区展示 | SHOWCASE.* ×4 / COMMUNITY.REUSE.REQUEST | 超出 V1.0 MVP |
| 反推/审片/交付 | REVERSE.* ×2 / CLIENT.REVIEW.* ×2 / DELIVERY.* ×4 | 超出 V1.0 MVP |

## 4. Worker 输入输出契约现状

| Worker | 入口契约 | 输出契约 | 状态 |
| --- | --- | --- | --- |
| relay | 消费 outbox_events（published_at IS NULL） | 外发 webhook + published_at 回写 | 已实现 |
| outbox_maintenance | 归档/重试/死信（R06 保留期策略） | outbox 分区维护 | 已实现 |
| identity | 身份域任务 | 令牌/密钥维护 | 已实现 |
| generation / qc / delivery / notifications | — | — | 空目录（见 PR-04 裁决） |

## 5. 结论

- 契约事实源唯一：action/openapi/schema/state_machine/enum/event 各有一处权威，无第二套事实源。
- 差异全部显式登记：21 未实现 + 7 schema 债务 + 3 转移入口债务 + 6 不可达状态债务，无「绿着但不可执行」的静默缺口。
- 未实现但属 V1.0 MVP 的 action（6 项）是后续 PR 的直接实现对象；超出 MVP 的 15 项维持 `not_implemented` 显式债务，不伪造。

## 6. 禁止事项说明

本报告未复制任何契约文件作为「统一版本」——契约事实源仍在其 §1 权威位置，本报告只做定位与差异枚举。
