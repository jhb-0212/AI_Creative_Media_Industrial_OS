# Phase 1 验收覆盖审计（2026-11-17）

审计对象：`Phase1-实现验收清单.md`（A1–G3，共 27 项）对照当前仓库 HEAD `44262fb`。

> **性质声明**：本文档是**缺口清单**，不是通过证书。它存在的意义是让"还差什么"
> 可被下一个人直接读到，而不是让"看起来做了很多"掩盖缺口。清单自身第二节
> 写着"证据优先、负向优先、不通过即阻塞"——本文档按这三条执行。

---

## 1. 判定口径

| 判定 | 含义 |
|---|---|
| `PASS` | 有可执行用例或可复核产物，且断言覆盖清单"通过标准"的**全部**分句 |
| `PARTIAL` | 有实现但证据不全，或证据只覆盖通过标准的一部分 |
| `MISSING` | 无实现（含"只有空壳目录""只在契约里存在"） |
| `SCOPE-DRIFT` | 清单通过标准里的数字/对象已与当前契约不符，判定本身失效，需先裁定 |

三条硬规则：

1. **"代码里有类"不算证据** —— 证据必须能被第三方用一条命令复现（`pytest <nodeid>`、
   `scripts/*.py`、文件路径 + 行号）。
2. **正向路径不替代负向路径** —— 清单第二节"负向优先"：权限、状态机、门禁类验收，
   先证明该拒绝的拒绝了。
3. **契约里有 ≠ 运行时能执行** —— ADR-085 已写明；`contract_check` 只证明"文档与
   声明自洽"，不证明系统按契约运行。

审计方法：逐项在仓库里定位实现与用例，未找到即判 `MISSING`，不做"应该实现了"的推断。

---

## 2. 先决问题：清单基准已漂移（SCOPE-DRIFT）

清单是在契约规模更小的基线上写的，当前契约已增长。这导致三项验收**无法机械判定**：

| 清单原文 | 当前事实 | 冲突点 |
|---|---|---|
| B1「**75 项** action_key 服务端落地」 | `action_registry.yaml` 共 **85** 个 action（ACTIVE 41 / PHASE_GATED 44） | "75 项全落地"的分母不存在；且 PHASE_GATED 动作按设计不要求实现 |
| D1「`state_machines.yaml`（**35 对象**）」 | 契约 **42** 个状态机；已实现 **10** 个 | "每个状态机至少一条非法转移用例"对 32 个未实现状态机无从执行 |
| A2「迁移后 **76 对象** 的 `schema_version` 与 `entity_version` 语义不变」 | `object_schemas.schema.json` **85** 个定义 | 76 这个数字在契约里已无对应集合 |

**这不是文字疏忽，是验收失去判据**：分母过期时，"全绿"取决于审计人挑哪个分母，
清单就退化成解释权。必须在继续验收前做一次口径重裁定，把三项改写成"按契约当前
计数 + 明确排除项"，并把裁定结论写进清单本身（清单不进 Phase 0 冻结包十一类哈希，
改它不需要重签 manifest，见清单第十一节）。

本审计的处理：下文对这三项先按**当前契约计数**判定，并标注 `SCOPE-DRIFT`，
裁定后需复核。

---

## 3. A 组 · 基础与可运维（4 项）

| 编号 | 判定 | 证据指针 | 缺口 |
|---|---|---|---|
| A1 环境可复现 | `PASS`（2026-11-17 补齐） | `uv.lock`（`uv sync --frozen`，漂移即失败）；**配置清单** `docs/evidence/phase1-runtime-inventory.md`（由 `scripts/runtime_inventory.py` 从 `Settings` 与 Dockerfile 生成，43 字段 / 11 敏感 / 3 条启动期校验，`tests/unit/test_runtime_inventory.py` 6 例防漂移）；**镜像摘要**：基础镜像按索引 digest 固定（`sha256:e5b65587…`），业务镜像构建并自检（`/health` 返回 `version=2.0.5`、Python 3.12.12、uv 已移除、uid=10001）。详见 `docs/evidence/phase1-a1-environment.md` | 本轮修掉一个实质缺陷：镜像未声明 `image.version`，继承了基础镜像的 `0.9.30-…`（uv 版本）——已用 `ARG APP_VERSION` 写入并由测试钉住三处版本一致。边界：不声称 bit-for-bit 可复现、未在 CI 重建比对、单架构验证（见该文档 §7） |
| A2 Schema 迁移与回滚 | `PASS`（2026-11-17 演练） | `scripts/migration_rollback_drill.py` 在一次性 scratch 库执行 `upgrade head → 写哨兵 → downgrade -1 → upgrade head → alembic check`，11 条断言全过（哨兵 `schema_version=205`/`entity_version=7` 跨回滚未变；被降级迁移建的表消失又恢复；表数 33→32→33）；证据 `docs/evidence/phase1-migration-rollback.json`。38 个迁移均有 `downgrade()` | 只演练降一步：降到 base 会执行 `c2d3e4f5a6b7` 的 `DROP ROLE`，而角色是跨库对象，共享集群上会删掉其它库在用的角色（边界已写入证据 `limitations`）。清单原文"76 对象"仍属 SCOPE-DRIFT |
| A3 可观测链路 | `MISSING` | `packages/telemetry/tracing.py`（97 行）提供 `setup_telemetry` / `get_current_trace_id`；错误信封（`packages/errors/error_codes.py:179`）与日志（`packages/shared/logging.py:91`）自动带 trace_id；`infra/monitoring/prometheus/outbox-rules.yml` | **业务链零 span**：`grep -rn "start_span\|tracer()" packages/ services/ apps/ workers/` 除 `tracing.py` 自身外零命中。没有任何 Task / Gate / 生成调用创建 span，所以"一次主链任务的 trace 全链路"必然断链——trace_id 能串起日志，串不起调用层级。清单要求的"每个 Task/Gate/生成调用可在 trace 中定位"不成立 |
| A4 备份与恢复 | `PASS`（2026-11-17 演练） | `scripts/backup_restore_drill.py`：scratch 库 → 种子一条校验和自洽的 DeliveryManifest → `pg_dump -Fc`（177212 字节）→ `pg_restore` 到第二库 → **33 张表逐表内容哈希一致** → 从恢复出的行用 `compute_manifest_checksum` 重算，等于存储值与备份前值；7 条断言全过；证据 `docs/evidence/phase1-backup-restore.json`。备份策略另见 `docs/runbooks/runbook-db-migration.md` §2（`pg_dump` + SHA256 + 留痕要求） | 只覆盖 pg_dump 快照点恢复，不覆盖 PITR（WAL 归档 + `recovery_target_time`）与物理备份——属运维/Phase 4 灾备，证据 `limitations` 已写明 |

---

## 4. B 组 · 权限与职责分离（4 项）

| 编号 | 判定 | 证据指针 | 缺口 |
|---|---|---|---|
| B1 动作服务端落地 | `MISSING` + `SCOPE-DRIFT` | `contract_check`：`ACTIVE 41 个（已实现 26 / 登记未实现 15）`；`已实现 action 的授权落点：26 个动作均已挂 require_action`（机器校验，声明无法撒谎） | **15 个 ACTIVE 动作无端点**，明细见 `contracts/runtime/action_implementations.yaml` 的 `not_implemented`。按域聚簇：预算 2（`BUDGET.SHOT.REQUEST/APPROVE`）、社区展示 4（`SHOWCASE.SNAPSHOT.CREATE/REVOKE`、`SHOWCASE.SHARE.CREATE/REVOKE`）、反推 2、客户审片 2、权利/Canon 2、复用申请 1、交付 2（`DELIVERY.SPEC.APPROVE`、`DELIVERY.MANIFEST.APPROVE`）。清单要求"每个 action_key 在服务端有校验"——未实现的 15 个既无校验也无端点 |
| B2 Agent 不得自批自用 | `PASS` | `packages/identity/authorization.py:342` `DecisionReason.AGENT_CRITICAL_FORBIDDEN`；多层负向：授权决策（`tests/permission/test_authorization_policy.py:171,180,416,465`）、领域（`tests/unit/test_shot_spec_service.py:160,258`、`test_constraint_pack_service.py:100-157`）、数据库 CHECK（`tests/integration/test_workflow_approval_invariants.py:194 test_agent_voter_rejected_by_check`） | 唯一缺口是**编号映射**：清单要求 AGENT-NEG-001～004 四条执行记录，用例存在但未逐条标注对应哪个编号。属证据整理问题，不属能力缺失 |
| B3 CapabilityToken 生命周期 | `PASS` | `packages/identity/` 的 `capability.py` / `issuer.py` / `jwt_core.py` / `jwks.py` / `keys.py` / `token_revocation.py` / `revoked_token_model.py`；签发/使用/过期/撤销四类用例：`tests/unit/test_capability_token.py`、`test_token_revocation.py`、`test_identity_jwks_rotation.py`、`tests/integration/test_jwks_endpoint.py`、`test_auth_revocation_and_grants.py`；ADR-R26 / ADR-R29 | 无（`capability_token_jti` 与 `user_intent_id` 的绑定在 `capability.py:164 to_audit_dict` 与审批链用例中体现） |
| B4 职责分离 | `PASS`（局部） | `DecisionReason.SELF_APPROVAL_FORBIDDEN`（`tests/permission/test_authorization_policy.py:194`，并把 `separation_of_duties` 记入 `deferred_checks`）；业务面 `tests/unit/test_asset_service.py:298`、`tests/unit/test_shot_visual_plan_service.py:122 test_separation_of_duties` | C.5.1 的对象覆盖不全：仅剧本锁、视觉方案、资产审批、交付审批有分离校验；其余"申请—审批"对未逐一验证 |

---

## 5. C 组 · 首发主链（6 项）

| 编号 | 判定 | 证据指针 | 缺口 |
|---|---|---|---|
| C1 建项目→剧本→拆镜→人工确认 | `PASS` | `tests/integration/test_main_chain_e2e.py`（端到端，含"未确认不得进入 ShotSpec"的负向断言，见 ADR-R33 §4） | 无 |
| C2 ShotSpec / ShotVisualPlan 版本不可变 | `PASS` | `tests/unit/test_shot_spec_service.py`（`test_lock_spec_version_mismatch_denied`、`test_lock_spec_cross_project_denied`、`test_lock_fields_records_metadata`）；`tests/integration/test_asset_immutability.py`（内容列 UPDATE 拒绝、DELETE 拒绝、仅状态列可写） | 无 |
| C3 ConstraintPack 硬门 | `PASS` | `tests/unit/test_constraint_pack_service.py`；主链内联负向断言（DRAFT 约束包不得作为生成输入，ADR-R33 §2.5） | NOT_APPLICABLE 包的 ACTIVE 前置校验已含；无 |
| C4 单一路线生成 | `PASS` | ADR-R35 / 契约修订 A9；`tests/unit/test_shot_route_selection_domain.py`、`test_shot_route_selection_parity.py`；`tests/integration/test_generation_route_api.py`（并发恰一成功一 409 + 唯一约束直连 SQL 证据） | 无 |
| C5 核心 QC | `PASS` | ADR-R36 / 契约修订 A10；`docs/evidence/phase1-c5-qc-review.md`（E1–E33，57 用例） | 无（债务已登记 ADR-R36 §3） |
| C6 Gate 决策 + DeliveryManifest | `PARTIAL` | `tests/unit/test_gate_api.py`、`test_gate_decision_domain.py`、`test_delivery_manifest_domain.py`、`test_main_chain_e2e.py`（清单引用 APPROVED 资产 + Gate4 裁决，含"未批准资产被拒"负向断言） | ① 清单要求"**Gate 0—4 各一次**决策记录"，当前只对 Gate4 与主链末段有取证，Gate 0—3 无逐 Gate 记录；② `DELIVERY.MANIFEST.APPROVE` 未实现（`allowed_principals` 含 `+` 为 AND 语义，联合审批未落地，权限层 fail-closed 拒绝），交付止于 DRAFT——ADR-R33 §3 债务 1 已登记 |

---

## 6. D 组 · 状态机与错误契约（4 项）

| 编号 | 判定 | 证据指针 | 缺口 |
|---|---|---|---|
| D1 未登记转移一律拒绝 | `PARTIAL` + `SCOPE-DRIFT` | 三层镜像已成立：契约转移表 / 领域状态机 / 数据库守卫触发器（未登记边拒绝 + 内容列白名单 + 禁 DELETE，见 ADR-R20）；`contract_check`：`状态可达性：10 个已实现状态机；显式登记不可达 10 项`、`状态转移入口债务：显式登记 2 项` | 契约 42 个状态机，**只有 10 个有实现与非法转移用例**（Project、ScriptRevision、Episode、AssetVersion、DeliveryManifest、ApprovalTicket、ConstraintPack、ProductionRoute、GenerationTask、QCReviewSession）。清单要求"每个状态机至少一条非法转移用例"——对 32 个未实现对象，"不存在能写进去但没登记的路径"这句话不可能成立，因为**连写入口都没有**。注意这与 15 个未实现动作同源：都是域未建 |
| D2 结构化错误码 | `PASS` | `packages/errors/error_codes.py` 注册表（含 `PHASE_NOT_ENABLED`、`AUTH_SCOPE_DENIED`、`STATE_TRANSITION_DENIED` 等，带 category + http_status）；`tests/unit/test_error_codes.py`、`test_error_envelope.py`；拒绝无副作用在状态机类用例中逐条断言（拒绝后 `entity_version` 不变） | "无副作用"未做**全量**验证，只在状态机与会话类用例中断言 |
| D3 幂等 | `PARTIAL` | 两处落点：GateDecision（`idempotency_key` 列 + `_assert_same_intake` 语义，`services/qc_governance/**`）；SCRIPT.IMPORT（`_assert_same_request`，`tests/unit/test_script_domain.py:106` 同 entity_id 不同内容 → `IDEMPOTENCY_CONFLICT`） | 8.6 要求是普遍能力，当前只有 2 条链有幂等键。其余写端点未见幂等设计 |
| D4 ShowcaseSnapshot 终态恢复 | `MISSING` | 契约有状态机：`contracts/state_machines/state_machines.yaml:299 object: ShowcaseSnapshot` | `services/` 下无 showcase 域（`ls services/ | grep -i show` 零命中）。P0-3 裁决要求的"恢复须新 snapshot_id + snapshot_version 递增 + 重走 Publication Gate"无任何落点 |

---

## 7. E 组 · 成本与配额（3 项）

| 编号 | 判定 | 证据指针 | 缺口 |
|---|---|---|---|
| E1 BYOK 真实启用 | `MISSING` | `services/budget_quota/` 为**空目录**（无任何 `.py`） | 无账户模型、无余额、无 BYOK 分支。"平台余额为 0 时 BYOK 不被拦截"无从验证 |
| E2 四账户隔离 | `MISSING` | 同上；`contracts/budget/budget_caliber.yaml` 有契约定义 | Quality Reserve 不可被占用的**结构性**保证（约束/触发器/独立账户行）不存在 |
| E3 估算与真实账单分离 | `MISSING` | 同上 | `cost_bearer` / `allocation_rule_version` 无落点 |

> E1、E2 是清单第十节明列**必绿**项。当前空白，属出口硬阻塞。

---

## 8. F 组 · Creator 域（3 项）

| 编号 | 判定 | 证据指针 | 缺口 |
|---|---|---|---|
| F1 CREATOR_MODE Phase 1 = NOT_ENABLED | `PASS`（2026-11-17 由 ADR-R37 补齐） | **两条腿**：① 审批面：`services/workflow/service/approval_service.py:84-86` 对 `spec.is_phase_gated` 的动作建票据时抛 `PHASE_NOT_ENABLED`（`tests/unit/test_approval_service.py:277`），堵住"先把未开放动作做成票据再消费"的绕过路径；② 动作面：`packages/identity/phase_gate.py` + `apps/api/main.py` 的 `/actions/{action_key}` 拒绝面，8 个 `CREATOR.*` 动作全部返回 `409 PHASE_NOT_ENABLED`（`details` 带 `action_key` / `current_phase` / `required_phase`），非 Creator 门禁动作同样适用（`COMMUNITY.POST.SUBMIT` 验证通用性）。阶段是**代码常量** `PLATFORM_PHASE`，不是配置项（F1"不得以配置开关绕过"）。证据：`tests/unit/test_phase_gate.py`（27 例）；决策记录 ADR-R37 | 无（ADR-R37 §3 登记了 5 项边界：拒绝面无鉴权前置、括号内条件不解析、只区分 phase 不区分 entitlement、阶段常量与 phase_map 无机器交叉校验、已启用无端点仍为 404） |
| F2 CreatorGate 不授予发布权 | `MISSING` | `CREATOR.GATE.EVALUATE` 契约在（PHASE_GATED）；无实现 | CreatorGate 未建模，"PASS 后发布仍被拒"无从验证 |
| F3 PUBLIC 发布禁用 | `MISSING` | 契约有 `audience_scope=PUBLIC` 定义 | 无 `audience_scope` 字段落点（`grep -rn audience_scope --include=*.py services/ packages/ apps/` 零命中；代码里的 `PUBLIC` 仅出现在 `IDENTITY_PUBLIC_KEY` 等无关上下文） |

---

## 9. G 组 · 交付与证据（3 项）

| 编号 | 判定 | 证据指针 | 缺口 |
|---|---|---|---|
| G1 DeliveryManifest 可重建 | `PARTIAL` | `tests/unit/test_delivery_manifest_domain.py`：`test_rebuild_projection_matches_original_checksum`、`test_checksum_ignores_reference_order`、`test_checksum_excludes_manifest_id_and_timestamps`、`test_checksum_is_stable_hex_digest`；ADR-R22 | 清单要求"**用清单重建一次交付**"的端到端记录。当前只到"投影重算校验和相等"这一层，未验证"按清单从存储取回全部证据并逐项比对"，也未验证"缺证据时明确列缺项、不静默补全" |
| G2 交付后缺陷闭环 | `MISSING` | `EscapeReport` 仅出现在 `contracts/metrics/metric_dictionary.yaml` 与 `contracts/phase0/frozen_inventory.yaml`（均为契约侧） | 无 EscapeReport 对象、无 RCA 记录、无"不改写原交付"的关联链 |
| G3 写操作审计覆盖 | `PARTIAL` | 审计经 outbox 事件承载：28 个事件 / 28 个发出点（`contract_check` 机器校验）；outbox 不可变与保留策略见 ADR-R11 / ADR-R11-2 | 无独立审计表，审计"不可被业务侧删除"由 outbox 追加性间接保证。清单要求"**抽样 10 个写操作**查审计，全部有调用主体、目标对象、前后状态"——该抽样从未做过；且并非所有事件的 payload 都含 `from_status`/`to_status`（仅状态转移类含） |

---

## 10. 出口判定（按清单第十节机械推导）

清单规则：**A、B、C 三组全绿；D1、D2、E1、E2 必绿；F1 必绿。**

| 组 | 要求 | 实测 | 结论 |
|---|---|---|---|
| A | 全绿（A1–A4） | A1 PASS、A2 PASS、**A3 MISSING**、A4 PASS | ✗（仅剩 A3） |
| B | 全绿（B1–B4） | B1 MISSING（15 动作无端点）、B2/B3/B4 PASS | ✗ |
| C | 全绿（C1–C6） | C1–C5 PASS、**C6 PARTIAL** | ✗ |
| D | D1、D2 必绿 | **D1 PARTIAL**（10/42 状态机）、D2 PASS | ✗ |
| E | E1、E2 必绿 | **E1 MISSING、E2 MISSING** | ✗ |
| F | F1 必绿 | F1 PASS（ADR-R37）；F2/F3 MISSING，按规则需逐项登记 owner / 阻塞范围 / 复审时点 | ✓ 条件成立 |

**Phase 1 出口不成立。** 五组各有阻塞项（A 组只剩 A3 一项），B/E 两组是零到一的
缺口（不是打磨问题）。

必须诚实的一句话：当前形态是**"主链单点闭环 + 横切能力齐备，域覆盖不足"**——
C1–C5 这条竖切是真的端到端通（含负向断言与数据库层证据），但横向的
预算、社区展示、权利、反推、客户审片、Creator 六个域尚未开始，而清单的
出口规则要求横向也全绿。

---

## 11. 缺口归因（三类，处置方式完全不同）

### 类 1 · 域未建（占缺口绝大多数）
预算（E1/E2/E3 + 2 动作）、社区展示（D4 + 4 动作）、权利/Canon（2 动作）、
反推（2 动作）、客户审片（2 动作）、Creator（F1/F2/F3 + 8 动作）、
交付规格与联合审批（G2 + 2 动作）。

**共 15 个 ACTIVE 动作 + 32 个状态机 + 5 个验收项落在这一类。**
处置只有两条路：建域，或**把 Phase 1 范围裁到与已建域一致**。
后者需要一次正式的阶段裁剪裁定——因为 `phase_map.yaml` 里 Phase 1 明写
"反推、组织内只读资产目录、客户审片和作品拆解最小版"是 Phase 1 内容，
裁掉等于改契约，须走 31.11 流程。

### 类 2 · 契约有、实现无（小而明确）
- A3 业务 span（tracing 基建已在，缺 span 埋点）。
- ~~F1 动作面的 `PHASE_NOT_ENABLED` 统一拒绝面~~：**已闭合（2026-11-17，
  ADR-R37）**。补充一条对本审计原先判断的修正——我此前以为它需要走 31.11 契约
  修订，实际不需要：这 44 个动作的规范地址本来就登记在 OpenAPI 里
  （`/actions/<小写点分 key>`），复用该地址做拒绝面**不新增任何契约操作**，
  且拒绝面 `include_in_schema=False` 不入 schema，故契约产物零变更、
  冻结清单无需重签。
- D3 幂等键推广到其余写端点。

**这一类是投入产出比最高的**：不需要建域，不动契约模型，改完立刻多三项绿。

### 类 3 · 实现有、证据缺（只差记录）
- ~~A1 镜像摘要与配置清单未成文~~：**已闭（2026-11-17）**。配置清单改为
  代码生成 + 6 条防漂移断言；基础镜像按 digest 固定；顺带修掉"镜像继承基础
  镜像版本标签"的实质缺陷。见 `phase1-a1-environment.md`。
- ~~A2 迁移回滚演练未跑~~：**已闭**。`scripts/migration_rollback_drill.py`，
  11 条断言，证据 `phase1-migration-rollback.json`。
- ~~A4 恢复演练未跑~~：**已闭**。`scripts/backup_restore_drill.py`，33 张表
  逐表哈希比对 + 清单校验和重算，证据 `phase1-backup-restore.json`。
- C6 Gate 0—3 逐 Gate 决策记录未取。
- G1 端到端重建未做。
- G3 十个写操作审计抽样未做。
- B2 用例与 AGENT-NEG-001～004 编号未对齐。

**这一类全是"工程已做、验收记录没写"，可以靠一次集中的取证作业清零**，
且不需要新写业务代码。

---

## 12. 建议的收敛顺序（按 阻塞出口 × 成本 排序）

1. **先做口径重裁定**（第 2 节）。理由：分母不定，"全绿"没有定义，
   后面所有取证可能白做。产出：清单 A2/B1/D1 三行改写 + 裁定记录。
2. **清类 3**（证据补齐）。A1/A2/A4 已完成（2026-11-17）；剩余 C6 的逐 Gate
   记录、G1 端到端重建、G3 十个写操作抽样、B2 编号对齐，仍是一次集中作业。
3. **清类 2**（A3 span / D3 幂等；F1 已完成，见 ADR-R37）。A3 与 D3 都不动
   契约模型，属于可以连续推进的工程项。
4. **决策类 1**：建域 or 裁范围。这是产品/架构决策，不是工程任务。
   E1/E2 与 15 个动作同属此类，必须一起决策——单独补预算域而留下
   展示/权利/反推空白，出口规则依然不成立。

---

## 13. 本审计不声称什么

- **不声称**仓库质量差。C1–C5 的取证密度（含数据库守卫的超级用户直连 SQL 证据、
  并发 CAS 的确定性构造）高于多数 Phase 1 项目；缺口集中在**广度**而非**深度**。
- **不声称**上述判定永久有效。仓库在动，任何一项的判定都只是 `44262fb` 时点的事实。
- **不声称**清单第十节规则本身合理。"A/B/C 全绿 + E1/E2/F1 必绿"是在
  契约规模 75 动作 / 35 状态机时定下的出口条件，当前契约已扩到 85 / 42；
  规则是否仍适用，属第 1 项裁定的范围。
- **未审计**：性能与压测（清单不含）、安全渗透、许可证合规。

---

## 附：本次审计用到的复现命令

```bash
# 动作实现覆盖（15 个缺口）
python scripts/contract_check.py | grep "运行时 ↔ 契约覆盖"
awk '/^not_implemented:/{f=1} f' contracts/runtime/action_implementations.yaml

# 状态机覆盖
# 契约对象数只数 objects: 段（62 行里另有 conflict_rulings / text_authority_only 两类，
# 直接 grep "^- object:" 会得到 48，比权威集合多出 6 条冲突裁决与纯文本条目）
awk '/^objects:/{f=1} /^[a-z_]+:/{if($0!~/^objects:/)f=0} f&&/^- object:/{c++} END{print c+0}' \
  contracts/state_machines/state_machines.yaml                      # 应为 42
ls services/*/domain/state_machine.py                               # 实现落点（6 个文件 / 10 个状态机）

# A3：业务 span 是否存在（零命中即为断链）
grep -rn "start_span\|tracer()" packages/ services/ apps/ workers/

# 空壳域（零输出即为空）
for d in budget_quota observability blocking_service rights_governance \
         creative_exploration memory_learning model_gateway orchestrator \
         production_policy; do find services/$d -name "*.py"; done

# 迁移回滚能力
grep -rlc "def downgrade" data/migrations/versions/ | wc -l

# F1：阶段门禁的动作面（27 例；含 8 个 Creator 动作逐个断言）
python -m pytest tests/unit/test_phase_gate.py -q
# 手工复核一条（未认证也应得到 409，见 ADR-R37 §3 债务 1）
curl -s -X POST localhost:8000/actions/creator.mode.upgrade | jq .error_code
```
