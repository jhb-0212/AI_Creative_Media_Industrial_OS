# PR-F 根因分析：R42 T2 幂等冲突审计缺口（只读）

> 依据：ADR-R42 §4.3/§4.4/D5/D6/C5；用户 2026-11-23 第三批整改裁决；《核验与审计标注规范 V1.0》
> 性质：**根因取证（只读）**。本文件期间未修改任何代码、契约、迁移、ADR 正文或冻结清单；未执行 pytest / 迁移（本地 15432 端口未开）。
> 基线：main @ `b6ab0aa`（= ADR-R44 七项签署后；abf77c1→b6ab0aa 间仅治理文档变更，T2 实现未动）
> 取证方式：逐条实读源码 / 契约 / 迁移 / 测试，给出 file:line。**不采信 ADR 或执行包自述为现状。**
> 判定口径：PASS=代码与证据实存；FAIL=找到反向证据；PARTIAL=中间态；NOT RUN=未实际执行。

---

## 0. 基准澄清（先定标尺）

**缺口是"实现未达 ADR"，不是"ADR 决策缺失"。** ADR-R42 §4.3 已明确（`:149-151`）：

- 相同任务、相同产物重复消费 → 返回原登记结果，不新建资产；
- 相同任务出现**不同 SHA256 / 项目 / locator** → 报**幂等冲突**，不改变已有回执；
- 不允许创建第二个资产来掩盖来源冲突。

§4.4（`:166`）进一步要求"来源任务与资产内容不一致 → 拒绝完成，形成可审计冲突"；C5（`:185`）"内容冲突产生可审计的 REJECTED"。ORM 注释也声明（`:117-120`）"已有回执时复用原结果并**另记冲突审计事实**"。

因此 PR-F 是**补齐已定决策的实现**，不需要新增 ADR 决策。但实现需要承载冲突证据的新字段与新事件，属于**契约/迁移演进**，按"修订登记 → 源契约 → 派生 → 检查 → 重签"办理（本文件只做根因，不动这些）。

用户第三批方案把指纹覆盖面从 ADR 原文的（SHA/项目/locator）扩展到 `attempt_id / media_type / quality_profile_version`——这是**更严格的实现约束**，方向一致（都服务于"同一幂等键语义相同才幂等"），本根因按扩展面设计，但明确标注哪些是 ADR 明示字段、哪些是加固字段。

---

## 1. 缺口逐条取证

### G-1 【核心】回执存在即早返回，不比对请求语义

**证据**：`workers/relay/asset_registration.py:178-184`

```python
existing = await receipts.get_by_tenant_task(request.tenant_id, request.generation_task_id)
if existing is not None:
    # 重复投递：不新建资产、不改写回执，也不再发一次结果事件。
    await session.commit()
    return
```

**分析**：只要 `(tenant_id, generation_task_id)` 回执存在，任何后续请求都被当作正常重复投递直接 ACK，**不比较本次请求与原始登记是否语义一致**。回执表本身也不存原始请求指纹（ORM `services/asset_service/repository/orm.py:114-168` 无指纹列），因此即使想比对也无可比对对象。

**后果**：同一幂等键收到语义不同的请求（如不同 SHA256 / media_type）时，被静默确认为正常重复，违反 §4.3"报幂等冲突"与 §4.4"形成可审计冲突"。

**判定**：`[V-VERIFIED][E2]` **FAIL**（ADR 明示要求未实现）。

> 注：部分字段在生成域核验阶段会先拦住——`asset_registration.py:168-172` 比对 manifest 的 locator/sha256。但该核验只覆盖"当前事件 vs 当前 Manifest"，且**不含 media_type / quality_profile_version**；更重要的是它发生在"查回执"之前，无法替代"同一幂等键语义一致性"判定（详见 G-2/G-3）。

### G-2 【加固】media_type 未进入核验与持久化，指纹维度缺失

**证据链**：

- ArtifactManifest 领域对象 `services/production_planning/domain/artifact_manifest.py:32-37` 字段仅 `generation_task_id / project_id / locator / sha256 / attempt_id / status`，无 `media_type` / `size_bytes`。
- 迁移建表 `e1f2a3b4c5d6_asset_registration_saga_schema.py:176-202`（manifest 表）同样无这两列。
- 生成侧 T1 事件 payload **有** `media_type`（`generation_service.py:229`，来源 `confirm_artifact(media_type=...)`，由 `workers/generation/asset_registrar.py:52-59` 从 `artifact.stored.media_type` 传入）。
- T2 解析 `RegistrationRequested`（`asset_registration.py:58-67`）**含** media_type，但仅用于推导资产扩展名（`:194-196` `extension_for_media_type`），**不参与任何一致性核验**（`:168-172` 只比 locator/sha256）。

**分析**：media_type 在 T0 已可得（`workers/generation/executor.py:198` 在 `registrar.prepare()` 前取得 `artifact_bytes.media_type`），但未被固化进 Manifest，导致 T2 无法把"请求媒体类型"与"写入意图媒体类型"比对。用户方案要求同时持久化 `media_type` 与 `size_bytes`——数据可获得性是实存的，缺的是字段与传递。

**质量策略维度**：`quality_profile_version` 在 T1 payload 中（`generation_service.py:231`）但 `RegistrationRequested` 不解析（`asset_registration.py:58-67`）。按 D7/用户裁决，它必须从**冻结的 GenerationTask** 读取（handler 已这样做了：`:173` `frozen_quality_profile = task.quality_profile_version`），不能信事件自报——因此指纹应纳入它，但取值来源必须是 task 而非 payload。

**判定**：`[V-VERIFIED][E2]` **FAIL**（media_type 核验/持久化缺失）；quality_profile 取值来源正确（PASS），但需纳入指纹。

### G-3 【核心】并发命中唯一键时未回读，冲突被当失败而非幂等收敛

**证据链**：

- `SqlAlchemyAssetRegistrationReceiptRepository.add()`（`services/asset_service/repository/sqlalchemy_receipt_repo.py:79-85`）用 `begin_nested()` SAVEPOINT 捕获 `IntegrityError/ProgrammingError` 并调 `translate_write_error()`；其注释声称"调用方能回读既有回执"。
- `translate_write_error()`（`packages/shared/port_errors.py:199-213`）把 SQLSTATE `23505` 归类为 `DuplicateEntityError`（`idempotent_recoverable=True`，`:87-100`）。
- 但 T2 handler 调 `await receipts.add(receipt)`（`asset_registration.py:245`）**没有 `except DuplicateEntityError` 回读分支**——其 try/except（`:203-226`）只包住 `create_version`。

**分析**：两个 Relay 并发消费同一登记请求，都通过"查回执为空"（`:178-180`），都创建资产、都写回执；后写者命中 `uq_asset_registration_receipts_tenant_task` → `DuplicateEntityError` 冒到 relay 计失败/死信，而不是回读原回执收敛为幂等。这违反 §4.3"返回原登记结果"与用户测试矩阵"多 Relay 并发正确收敛到唯一登记结果"。

**判定**：`[V-VERIFIED][E2]` **FAIL**（唯一键冲突未按幂等处理；repo 注释与 handler 实现脱节）。

### G-4 【存储】冲突证据无合适权威存储，禁止新建平行审计系统

**候选机制排查（三条，均可复用性不足）**：

| 候选 | 证据 | 为什么不适用 |
|---|---|---|
| Outbox 事件 | `packages/shared/outbox.py:42-82`（append-only、无 DELETE）；`workers/relay/consumer.py:112-134` 只 UPDATE published_at | 永不 ack 的"冲突事件"会永久抬高 backlog（`a1b2c3d4e5f6` 的 pending 口径）、卡住分区归档（`workers/outbox_maintenance/__main__.py:224-247` 摘分区前查 `published_at IS NULL`）；且生产 `default_handler=WebhookDispatcher`（`workers/relay/__main__.py:80-84`）可能把未登记类型直接 ack → 证据无声消失。**用它当冲突证据是拿可靠性机制做审计，会反噬可靠性。** |
| `audit.status_transitions` | `d8e9f0a1b2c3_audit_status_transitions.py:94-123` | 有 `CHECK (from_status <> to_status)`（`:112`）。冲突不改变回执状态（§4.3"不改变已有回执"），无 from/to 可填，硬塞会伪造状态推进、破坏该表的状态图语义。 |
| 回执表自身加列 | `orm.py:114-168` + 不可变触发器（迁移 `:242-272`） | 回执不可变（连超管 UPDATE 都被 restrict_violation 挡），无法记录"后来的第 N 次冲突"。 |

**结论**：现有机制无一合适。按用户裁决"没有合适的权威存储时，再设计最小的 append-only 冲突记录，不能直接另建一套平行审计系统"——本项目采用**最小领域事实表** `asset.registration_conflicts`：与回执同域、同事务写入、append-only（行级 + TRUNCATE 守卫，复用 `d8e9f0a1b2c3` 的工程模式），只服务"登记请求语义冲突"这一件事，不复制通用 audit 的能力。它不是第二套审计系统，而是回执事实的伴随证据。

**判定**：现有机制 `[V-VERIFIED][E2]` **FAIL（无合适存储）→ 需最小新表（设计决策，见 §2）**。

### G-5 【派生】事件目录未登记冲突事件

**证据**：`contracts/runtime/event_catalog.yaml` 仅三条 R42 事件（`:38-51` REQUESTED、`:53-63` COMPLETED、`:65-73` REJECTED），无 CONFLICT。双向校验 `scripts/contract_check.py::check_event_catalog()`（`:3046-3160`）要求 emitted/declared 双向相等，新增事件须同时改目录与代码字面量。

**判定**：`[V-VERIFIED][E2]` **PASS（现状自洽）**，但作为 G-1~G-4 修复的派生动作，需登记 `ASSET_REGISTRATION_CONFLICT`。

---

## 2. 根因归纳（一句话）

**病根**：T2 把"幂等键存在"等同于"请求语义相同"，且缺少三件让二者可区分的东西——(a) 持久化的请求指纹，(b) 回执已存在时的语义比对与冲突落库，(c) 唯一键冲突后的回读收敛。三个缺口共享同一根因：**只做了"有没有"，没做"是不是同一个"**。

这正是"写入类验收不能只断言无异常"的反面：早返回 `commit(); return` 看似幂等成功，实际把内容冲突洗成了正常重复。

---

## 3. 建议修复方案（供契约/迁移/实现层采纳）

### 3.1 统一登记请求指纹（FINGERPRINT）

规范化输入（顺序固定、分隔符转义，canonical JSON + SHA-256）：

| 字段 | 来源 | 维度 |
|---|---|---|
| `tenant_id` | 事件（已由 task 核验） | ADR 明示 |
| `generation_task_id` | 事件 | 幂等键 |
| `project_id` | 冻结 GenerationTask（核验后） | ADR 明示 |
| `attempt_id` | 事件（Manifest 存在性已核验） | 用户加固 |
| `locator` | Manifest（核验后） | ADR 明示 |
| `sha256` | Manifest（核验后） | ADR 明示 |
| `media_type` | Manifest 新增列（T0 固化） | 用户加固 |
| `quality_profile_version` | **冻结 GenerationTask，不信事件** | 用户加固 / D7 |

指纹工具新建于 `packages/shared/registration_fingerprint.py`（仓库无通用 canonical json 工具，已核实），纯函数、可单测。

### 3.2 T2 判定矩阵

| 收到请求 | 处理 |
|---|---|
| 回执不存在 + 指纹与 Manifest 一致 | 正常登记（现有路径） |
| 回执存在 + 指纹完全相同 | 幂等返回原回执，ACK |
| 回执存在 + 指纹不同 | 写 `registration_conflicts` 冲突事实 + 发 `ASSET_REGISTRATION_CONFLICT` 事件（同事务），**不建第二资产、不改回执、不 ack 原事件为成功** |
| 唯一键并发冲突（23505） | 回读原回执，转指纹比对流程，不冒泡死信 |
| 生成域核验失败（来源不存在/状态不对/归属不符/Manifest SHA 不符） | 维持现有 `ReceiptEventError` → 退避重试/死信 |

### 3.3 最小冲突事实表 `asset.registration_conflicts`

信封列（entity_id / entity_type / tenant_id / project_id / created_at / created_by / schema_version / entity_version）+ 业务列：`generation_task_id`、`expected_fingerprint`、`observed_fingerprint`、`conflict_fields`（JSONB 数组）、`observed_payload_ref`（指向事件 aggregate/event id）、`resolution`（默认 `PENDING_REVIEW`）。约束：复合项目租户外键（复用 `project_tenant_fk`）、RLS、append-only 守卫（行级 + TRUNCATE）、GRANT 走 `DOMAIN_GRANTS` 收敛。

> 冲突表只记"差异事实"，不改回执状态、不驱动 GenerationTask 状态机；人工处置/重放入口仍属 D6 既有 PARTIAL，不在 PR-F 范围。

### 3.4 契约 / 迁移 / 实现顺序

1. 契约修订登记 → 事件目录加 `ASSET_REGISTRATION_CONFLICT`、object_schemas 加冲突对象、state_machines 不改（无新状态边）；
2. 迁移（down_revision=`e1f2a3b4c5d6`）：manifest 加 `media_type`/`size_bytes`/`fingerprint`，新建 conflicts 表；
3. 实现：指纹工具 → T2 判定矩阵 → receipt repo 并发回读；
4. 真实 PG 测试 + 故障注入（依赖 CI 环境，本地 15432 未开，标 NOT RUN 待 CI）。

---

## 4. 判定汇总

| 缺口 | 判定 | 严重度 | 修复归属 |
|---|---|---|---|
| G-1 回执存在即早返回，无语义比对 | `[V-VERIFIED][E2]` **FAIL** | P0 | PR-F 实现 |
| G-2 media_type 未核验/未持久化 | `[V-VERIFIED][E2]` **FAIL** | P1 | PR-F 契约+迁移+实现 |
| G-3 唯一键冲突未回读收敛 | `[V-VERIFIED][E2]` **FAIL** | P0 | PR-F 实现 |
| G-4 无合适冲突证据存储 | `[V-VERIFIED][E2]` **FAIL（需最小新表）** | P1 | PR-F 迁移 |
| G-5 冲突事件未登记 | `[V-VERIFIED][E2]` **PASS（现状自洽）** | — | PR-F 契约派生 |
| 真实 PG / 故障注入测试 | `[NOT RUN]`（本地 PG 未启） | — | CI |

---

## 5. 边界与不做的事

- **不改 ADR-R42 正文**：冲突要求已 DECIDED（§4.3/§4.4/C5），本修复是实现补齐；若要把指纹维度写入 ADR 正文，属新增裁决，另走 Owner 签署。
- **不碰 R42 授权口径 / C7 手动入队 / D6 人工重放**：三项维持 BOOT-01 原判定，不在 PR-F 范围。
- **不做 PREPARED 恢复（PR-G）**：本轮只做 T2 幂等冲突（PR-F）；`media_type`/`size_bytes` 的 manifests 加列会为 PR-G 铺路，但恢复逻辑不动。
- **不新建通用审计系统**：冲突表是最小领域事实，不复制 status_transitions 的状态图能力。
