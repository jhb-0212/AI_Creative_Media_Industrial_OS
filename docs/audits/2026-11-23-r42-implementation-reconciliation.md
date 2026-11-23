# BOOT-01 R42 实施与治理一致性审计（只读）

> 依据：《AI Creative Media Industrial OS 开发启动执行包 v1.0》BOOT-01；ADR-R42；
> 《核验与审计标注规范 V1.0》
> 性质：**只读审计**。本报告期间未修改任何代码、契约、迁移、ADR 正文或冻结清单；未执行 pytest/迁移。
> 基线：main @ `abf77c18ed48dcabf55492acf9b0c8dddc967022`（已 fetch，与 origin 一致）
> 取证方式：逐条实读源码 / 契约 / 迁移 / 测试文件，给出 file:line 证据。**不采信执行包或 ADR 自述为现状。**
> 判定口径：PASS=代码与证据均实存；FAIL=找到反向证据或基准要求未实现；BLOCKED=需 Owner 裁决；
> PARTIAL=部分满足（如实登记中间态）；NOT RUN=本轮未实际执行（测试执行一律 NOT RUN，除非注明 CI 证据）。

---

## 0. 基准澄清（先定标尺，避免误判）

审计基准是 **ADR-R42 正文**（已 DECIDED），不是执行包对它的转述。两处必须先澄清，否则会误判：

1. **D6 的"OpLog 追加式补偿"**：ADR-R42 D6 原文（`:23`）是"持久化回执、幂等重放、死信与人工处置；不删除已登记的不可变资产"。**正文没有要求独立的 `RegistrationOpLog` 表**（"OpLog"一词出现在 ADR-R43 §3.2 与蓝图/执行包，属设计输入描述）。因此本审计按 ADR-R42 原文判定 D6：**回执 + 幂等重放 + 死信三者实存 → PASS（补偿部分）**；"人工处置/重放入口"单列为 PARTIAL（见 D6 行）。若 Owner 后续决定 OpLog 表为硬要求，那是新增裁决，需改 ADR，不由本审计擅自加入基准。

2. **C7 的"唯一路径"**：ADR-R42 C7 与执行包均要求 QC 只消费登记完成事件。实读发现除 Relay 自动路径外，**仍存在 QC API 手动入队端点**（`services/qc_governance/api/qc_task_routes.py:110-136`）。这不是"两条 Worker 入队路径"（Worker 侧已清空），但它是第二条可产生 QCTask 的入口，必须如实登记为 PARTIAL，不得报 PASS 掩盖。

---

## 1. C1–C7 逐条判定

| 项 | 判定 | 代码证据 | 测试证据 | 理由 / 缺口 |
|---|---|---|---|---|
| **C1** 等待态定义与进出条件 | `[V-VERIFIED][E2]` **PASS** | 契约 `contracts/state_machines/state_machines.yaml:935,941,943-944,950-956`；schema `object_schemas.schema.json:9653`；枚举 `enum_registry.yaml:68`；运行时 `services/project_planning/domain/enums.py:199`、`state_machine.py:495-510`；领域方法 `services/production_planning/domain/generation_task.py:200-249`（`await_asset_registration`/`bind_registration_result`/`reject_from_registration`）；DB CHECK `e1f2a3b4c5d6:73-79` | `tests/integration/test_asset_registration_constraints.py`（AWAITING 可写/非等待态拒绝）、`test_asset_registration_saga_e2e.py` | 进=confirm_artifact(T1, `generation_service.py:215-240`)，出=complete_from_receipt(T3, `:365-437`)。契约+代码+DB 三层一致 |
| **C2** Manifest 状态机 + 幂等键 | `[V-VERIFIED][E2]` **PASS** | 契约 `state_machines.yaml:1002-1012`（PREPARED→STORED_VERIFIED, frozen）；领域 `services/project_planning/domain/state_machine.py:533-546`、`services/production_planning/domain/artifact_manifest.py:59-73`；实现 `generation_service.py:195-214`；唯一约束 `e1f2a3b4c5d6:184-187` `uq_artifact_manifests_task_attempt(task_id,attempt_id)` | `test_asset_registration_constraints.py`、E2E | 状态边与唯一键均实存 |
| **C3** 无自报资产字段入口 | `[V-VERIFIED][E2]` **PASS** | `apps/` 全域 grep `asset_version_id\|quality_profile_version` 无命中；`workers/generation/` 仅注释命中（`executor.py:17`）；`GenerationOutcome` 仅 `result`+`evidence_ref`（`worker.py:45-56`）；Finish 模型 `services/production_planning/api/generation_routes.py:104-117` `extra="forbid"` + `result: Literal["FAILED"]`；服务签名 `generation_service.py:322-340` | `tests/unit/test_generation_request_contract.py:67,81`、`test_generation_lease.py:221` | finish 只接收 FAILED，两字段被 `extra=forbid` 拒绝 |
| **C4** QC Profile 冻结链 | `[V-VERIFIED][E2]` **PASS** | 权威常量 `packages/shared/quality_profile.py`；路线创建冻结 `services/production_planning/service/production_route_service.py:56-58`；Task 创建赋值 `generation_service.py:89-103,117`（`require_quality_profile(route.quality_profile_version)`）；DB 列 `e1f2a3b4c5d6:285-294` | `tests/unit/test_quality_profile_authority.py:20,24,31` | Route→Task 单向冻结，创建时不查库取当前版本 |
| **C5** 回执唯一/不可变/复合归属 | `[V-VERIFIED][E2]` **PASS** | 迁移 `e1f2a3b4c5d6`：唯一 `:211-218`（tenant+task、project+entity）；不可变触发器函数 `:242-253` + BEFORE UPDATE/DELETE `:254-258` + TRUNCATE `:259-272`；复合归属 FK `:116-135,219-226`；CHECK `:227-233`；领域 `services/asset_service/domain/asset_registration_receipt.py:16-48` | `test_asset_registration_constraints.py`（回执 append-only、跨租户/错位拒绝）、`test_asset_immutability.py` | 对超管也抛 restrict_violation；复合 FK 防跨项目挂靠 |
| **C6** 事件同事务 + 消费幂等 | `[V-VERIFIED][E2]` **PASS** | 请求事件 `generation_service.py:215-240` → `transition.py:199-227` → `outbox_writer.py:89-159`（session.add 不 commit）；提交 `workers/generation/asset_registrar.py:52-60` 同 session；结果事件 `workers/relay/asset_registration.py:245-273`（create_version + receipt.add + writer.record 同 asset session 后一次 commit）；消费幂等 T2 `:178-184`、T3 `generation_service.py:381-386`、QC `handlers.py:113-142` + DB 唯一 `f1a2b3c4d5e6:106-108` | E2E `test_full_saga_commits_and_replays_idempotently`、`test_relay_handlers.py:92-203` | 代码先查 + DB 唯一两层幂等 |
| **C7** QC 单一入队路径 | `[V-VERIFIED][E2]` **PARTIAL** | Relay 自动路径：注册 `workers/relay/handlers.py:166-173`（`GENERATION_SUCCEEDED`→`handle_generation_succeeded`→enqueue），事件源 `generation_service.py:402`；Worker 明确不入队 `workers/generation/worker.py:157-160,209-210`，`_enqueue_qc/qc_enqueuer/reconcile_qc` 在 workers/generation 零命中；**但第二入口存在**：`services/qc_governance/api/qc_task_routes.py:110-136` `POST /qc/tasks`（手动入队，含 asset_version_id/quality_profile_version）挂载于 `apps/api/main.py:100` | `test_generation_worker.py:264-280`（Worker 无 QC 直入守卫）、`test_relay_handlers.py` | 自动入队确实单一；手动 API 入口仍在。需 Owner 裁定该端点是否属 C7 禁止范围，或限定为补偿运维入口 |

---

## 2. T0–T3 事务边界

| 阶段 | 落点 | 判定 |
|---|---|---|
| T0 `prepare_artifact` | 服务 `generation_service.py:139-172`；调用 `workers/generation/asset_registrar.py:35-47`（内部 commit） | PASS |
| T1 `confirm_artifact` | 服务 `generation_service.py:174-241`；调用 `workers/generation/asset_registrar.py:49-60`（同 session commit） | PASS |
| T2 relay / create_version | handler `workers/relay/asset_registration.py:146-273`（create_version `:204-217`）；服务 `services/asset_service/service/asset_service.py:112`（事件同事务 `:198-212`） | PASS |
| T3 `complete_from_receipt` | 服务 `generation_service.py:365-437`；调用 `workers/relay/asset_registration.py:297-308` | PASS |
| 存储 I/O 在事务外 | `workers/generation/executor.py:207-219`：`registrar.prepare()`（已 commit + 关 session）→ `storage.put()` → `registrar.register()` 新 session；`services/` 全域无 storage.put/get（仅 `workers/qc/asset_inspector.py:173` 读取） | PASS |

**T0/T1/T3 在 Generation 域，T2 在 Asset 域，四段各自本地事务、无共享 Session、无伪 2PC。** 与 ADR-R42 D4 一致。

---

## 3. D2 / D3 / D6

| 项 | 判定 | 证据 | 缺口 |
|---|---|---|---|
| **D2** 内部身份 | `[V-VERIFIED][E2]` **PASS** | `workers/relay/asset_registration.py:51` `_SERVICE_ID="svc:asset-registration"`；`:186-190` 构造 `Principal(subject_id=_SERVICE_ID, roles={"asset_registration"})`；用于 operator_ref `:213`、回执 created_by `:233` | 无（专用内部身份，仅由事件处理器构造） |
| **D3** 租户/归属核验 | `[V-VERIFIED][E2]` **PASS** | `workers/relay/asset_registration.py:149-173`：`repo.get_by_generation_task_id`（持久化任务）→ 状态须 AWAITING → project/tenant 与 request 比对拒绝（`:159-160`）→ manifest STORED_VERIFIED + locator/sha256 一致（`:161-172`）→ 质量口径取 `task.quality_profile_version`（`:173`）；T3 回执比对 `:293-295` | 无（不信任 payload 单方声明） |
| **D6** 补偿 | `[V-VERIFIED][E2]` **PASS（补偿三要素）** / `[V-PARTIAL][E2]` **PARTIAL（人工处置）** | 死信 `workers/relay/consumer.py:98-134,179-183,279-295`（排除死信、指数退避、达到上限写 dead_lettered_at、指标）；列 `f3a4b5c6d7e8:38-56`；死信视图 `a1b2c3d4e5f6:60-74`；幂等重放见 C6；回执不可变见 C5；不删资产（触发器 + 无删除路径） | **人工重放入口**：仅 `consumer.py:24,120` 注释声明"可人工重放"，未找到实际重放端点/脚本 → PARTIAL，登记为待补。OpLog 独立表不在 ADR-R42 D6 要求内（见 §0），不判 FAIL |

---

## 4. 真实 PG 测试证据（测试文件存在性 PASS；执行本轮 NOT RUN）

| 文件 | 覆盖 | 用例 |
|---|---|---|
| `tests/integration/test_asset_registration_fault_injection.py` | 真实 PG T0→T3 故障注入（ADR-R42 §4.4） | 7 用例：storage 失败保留 PREPARED `:245`；载荷篡改可审计冲突 `:294`；AssetService 瞬断无拒绝回执 `:338`；永久冲突写拒绝回执+FAILED `:389`；结果与回执矛盾拒绝 `:483`；重投恢复不建第二资产 `:525`；重复失败进死信 `:601` |
| `tests/integration/test_asset_registration_constraints.py` | 真实 PG C1/C2/C4/C5 结构验收 | catalog 实读、跨租户/未知任务/摘要错位拒绝、回执 append-only、AWAITING+冻结质量值可写（`:153,228,249,260,272,323,371,462`） |
| `tests/integration/test_asset_registration_saga_e2e.py` | 真实 PG 四段各自提交、重复投递/消费幂等 | `test_full_saga_commits_and_replays_idempotently:196` |
| `tests/unit/*`（generation_worker / generation_lease / generation_request_contract / quality_profile_authority / relay_handlers） | C3/C4/C6/C7 契约与守卫 | 见各文件行号（探索取证） |

**执行证据**：CI run `45140680867`（PR-E 故障注入，main）conclusion=success `[V-VERIFIED][E2]`；main HEAD run `45145570659` success。本机复跑 **NOT RUN**——CI 的 PASS 不冒充本机 PASS。

---

## 5. 治理差异（属实，仅登记，不改 ADR 正文）

**事实**：ADR-R42 正文 `:3` "固化决策，不激活实现"、`:9` "只确定决策与契约变更清单，不实现"、`:189-190` "C1–C7 是变更清单，不是已实施事实"、`:227` "未授权任何代码变更"；但以下提交已将 T0–T3 实现进 main：

| 提交 | 内容 |
|---|---|
| `716971c` | PR-C 库表/复合归属/不可变回执（迁移 `e1f2a3b4c5d6`） |
| `e95127a` | PR-D T0–T3 运行时与事件入口 |
| `291300a` | PR-E 跨域真实 PG 故障注入验收 |
| `fa42465` / `abf77c1` | C4 权威源贯通 / C7 收敛（后续修正） |

**判定**：`[V-VERIFIED][E2]` 治理差异成立——ADR 决策文本与实施状态已分离，且 ADR 正文从未被改写（符合"不静默改历史决策"）。执行包 BOOT-01 要求"以 append-only 实施记录/审计报告补正"——**本报告即是该 append-only 实施审计记录**（落在 `docs/audits/`，append-only 新增文件，不改 ADR-R42 正文）。

**仍需 Owner 裁决**：实施是否已获单独授权（从 git 提交看，PR #55~#59 经过 CI 与 FF 合并流程，但 ADR 正文的"不激活实现"未同步更新）。建议 Owner 明确：R42 实施状态以本审计报告 + 台账为事实源，ADR 正文保持历史原文不动。此项标记 `[V-BLOCKED][E1]`，**不由审计代为裁决**。

---

## 6. 汇总判定表

| 类别 | 结论 |
|---|---|
| C1/C2/C3/C4/C5/C6 | 全部 **PASS**（代码+契约+测试证据齐全） |
| C7 | **PARTIAL**——自动入队单一，但 QC API 手动入队端点仍在（`qc_task_routes.py:110-136`） |
| T0–T3 | 全部 **PASS**，存储 I/O 在事务外 |
| D2/D3 | **PASS** |
| D6 | 补偿三要素 **PASS**；人工重放入口 **PARTIAL**（仅注释声明） |
| 真实 PG 测试 | 文件存在 **PASS**，CI 执行 **PASS**（E2 run），本机复跑 **NOT RUN** |
| 治理差异 | 成立，**append-only 审计报告已补**（本文）；授权状态待 Owner `[V-BLOCKED]` |

**待 Owner 裁决 2 项**：① C7 手动 QC 入队端点是否纳入禁止范围；② R42 实施授权口径（ADR 正文不动、以本报告为实施状态事实源是否认可）。两项裁决前不修改任何代码或 ADR。

---

## 7. 九项状态结论

| 标注 | 取值 | 支撑 | 未覆盖 / 升级条件 |
|---|---|---|---|
| `[FREEZE-STATUS]` | NA | R42 实施不改冻结范围 | — |
| `[CONTRACT-STATUS]` | VERIFIED | C1/C2/C7 契约与代码一致 | C7 手动端点口径待裁决 |
| `[ARCHITECTURE-STATUS]` | PARTIAL | Saga 四段事务边界 PASS | C7 第二入口、D6 人工入口未闭环 |
| `[IMPLEMENTATION-STATUS]` | PARTIAL | C1-C6/T0-T3/D2/D3 实存 | C7 手动入口、D6 人工重放 |
| `[TEST-STATUS]` | PARTIAL | 真实 PG 测试文件 + CI success | 本机复跑 NOT RUN |
| `[RUNTIME-STATUS]` | PARTIAL | CI 真实 PG run 成功 | 本机未跑；生产未部署 |
| `[SECURITY-STATUS]` | VERIFIED | D2/D3 身份与归属核验 PASS | — |
| `[REMEDIATION-STATUS]` | OPEN | 2 项待 Owner 裁决 | 裁决后决定是否需要整改 PR |
| `[RELEASE-STATUS]` | BLOCKED | Phase 1 未验收（BOOT-00） | 试播 + Owner 签署 |

---

## 8. 证据边界声明

- 本审计为 main @ `abf77c18…` 的时点判断；所有 file:line 为该 SHA 下的实读位置。
- 本轮**未执行**任何 pytest、迁移或数据库操作；测试"存在"与"CI 通过"均有标注，不等于本机 PASS。
- 所有判定以 **ADR-R42 正文**为基准，不采信执行包/蓝图对它的转述措辞（尤其 D6 的 OpLog、C7 的"唯一"）。
- 本文为 append-only 新增的**实施审计记录**，未修改 ADR-R42 正文、未改冻结清单/契约/迁移；治理差异的授权状态保留为 Owner 裁决项，不由审计代决。
