# 第四轮代码审查与工程铁律核对

- 仓库：`C:\Users\EDY\Desktop\AI制作工作台\AI_Creative_Media_Industrial_OS`
- 分支：`feat/identity-data-ownership`
- HEAD：`db3645f`（`db3645f773af`）
- 工作区：审查开始时 `git status --short` 为空；本轮只新增本报告文件
- 审查对象：D1–D4、F3、F6 已完成代码；不包含 `docs/proposals/shot-domain-contract-closure.md`（用户明确说本轮先不处理）
- 审查方式：五道闸门实跑 + 全量 pytest + 定向测试 + 逐文件读码 + 真实数据库只读 RLS 探针
- 本轮未修改代码、契约、迁移，未提交、未推送

## 0. 结论先行

**五道闸门确实真绿，但“闸门绿”不等于运行时正确。** 本轮找到 1 个确定的 P1 运行时阻断：

> 交付 worker 只注入 `tenant`，没有注入 `organization` 与 `projects`；按当前 RLS 策略，它读 `asset_service.delivery_manifests` 会看到 0 行，因此任何审批完成事件都会被记成 `skipped`，清单永远无法推进到 `VALIDATED`。

这不是推测。用真实数据库、以 asset 域角色、在同一租户下只读验证：

| RLS 上下文 | `asset_service.delivery_manifests` 可见行数 |
|---|---:|
| 只设 `app.current_tenant_id`（worker 当前做法） | **0** |
| 补设 `app.current_org_id` + `app.current_projects='*'` | **96** |

本轮的其余高置信问题集中在授权语义与事务一致性：审批 `start` 不校验目标资源归属、Capability 的 episode/shot/asset 范围解析后从未执行、Gate 幂等键把 `decision` 算进键里、交付两步落库可能提交半截 `VALIDATING`、共享 outbox 没有事件类型所有权约束。

**当前 HEAD 仍不满足 Phase 1 验收**：`README.md:145` 自己写明“Phase 1 未通过验收”；`contract_check` 仍显示 ACTIVE 39 中 22 个 `not_implemented`。五道闸门只能证明派生一致、分层、冻结、静态质量与既有测试通过，不能替代运行时链路验证。

## 1. 五道闸门实跑结果

| 闸门 | 本轮结果 | 关键数字 | 不能证明什么 |
|---|---|---|---|
| `contract_check.py` | PASS（exit 0） | Action 83；ACTIVE 39（17 implemented / 22 not_implemented）；Schema 80；runtime 8；事件 19/19；状态机 6 implemented；unreachable 10；entry debt 2；exemptions 1；审批绑定 3 个动作 | 不能证明授权语义、运行时上下文、目标资源归属正确 |
| `architecture_check.py` | PASS（exit 0） | R1–R6 全 0 违规；R5 仅 `packages.shared.rls` 注入 | R5 只拦“业务代码自己写 GUC”，不区分 `set_tenant_context` 与 `set_request_context`；worker 用错 helper 仍会绿 |
| `freeze_manifest.py --check` | PASS（exit 0） | 基线 V205-R14 未变；十一类产物哈希一致；3 条修订有据 | 只证明符合冻结基线，不证明业务正确 |
| `release_package.py --check` | PASS（exit 0） | `commit=db3645f773af`，363 files；临时包未落盘 | 不证明运行镜像依赖安全，也不替代故障演练 |
| `ruff` / `mypy` / `pytest` | 全 PASS | ruff 全过；mypy 133 files 无问题；全量 pytest exit 0；定向 43 passed | 测试全绿但未覆盖 worker 路径，正是 R4-1 逃逸的原因 |
| `alembic current` / `alembic check` | PASS | `a2b3c4d5e6f7 (head)`；No new upgrade operations | 不证明授权正确性 |

补充：`tests/` 下没有任何 `consume_tenant` / `run_once` 调用（grep 0 命中）。交付审批消费的集成测试是直接调用 `DeliveryApprovalService`，并用测试夹具设置了完整 tenant+org+projects 上下文：

- `tests/conftest.py:131-153`：`apply_db_context` 调用生产函数 `set_request_context`，默认 `project_ids=("*",)`。
- `tests/integration/test_delivery_approval_consumption.py:144-165`：测试直接 `service.consume_approval(...)`，没有经过 `workers/delivery/consumer.py:consume_tenant`。

这解释了“测试全绿 + worker 不可用”可以同时成立。

## 2. 问题清单

### R4-1（P1，确定）交付 worker 的 RLS 上下文不完整，按当前代码无法推进任何清单

**证据**

- `workers/delivery/consumer.py:28` 只导入 `set_tenant_context`；`:63-64` 在会话中只调用 `set_tenant_context(session, tenant_id)`；`:71-74` 在该上下文下列出审批完成事件；`:80-87` 捕获 `DomainError` 后 `skipped += 1`，最后 `session.commit()`。
- `packages/shared/rls.py:51-60` 的 docstring 已经写明：**只设置租户（不设项目范围）的调用方只应访问不带项目归属的表；访问项目级资源表会因项目范围为空而看不到任何行**。
- `packages/shared/rls.py:87-108` 的 `set_request_context` 才同时设置 tenant + organization + projects。
- `data/migrations/versions/d6e7f8a9b0c1_delivery_manifests.py:77-87` 的 `project_scope` 表达式要求 `organization_id = current_setting('app.current_org_id')` 且 `current_projects='*'` 或 `project_id = ANY(...)`；`:157-174` 同时启用 tenant 策略与 restrictive project 策略。

**实证**

同一 asset 角色、同一租户 `00000000-0000-0000-0000-00000000d5a1`，直接查询 `asset_service.delivery_manifests`：

- 只设 `app.current_tenant_id` → **0 行**；
- 补设 `app.current_org_id` + `app.current_projects='*'` → **96 行**。

**影响**

`consume_tenant` 能在 outbox 中列出事件（outbox 表只按租户隔离），但 `DeliveryApprovalService.consume_approval` 通过 `SqlAlchemyDeliveryManifestRepository.get_by_id` 读清单时会被 RLS 判否，抛 `NOT_FOUND`；`:80-84` 又把所有 `DomainError` 当作“终态重复”记入 `skipped`，最终 `commit()` 一个什么都没改的事务。结果是：**事件被看到、清单永远不动、监控也不报警**。

**根因**

worker 复用了一个“只适合 outbox 表”的 helper，却被用来访问项目级资源表；测试夹具直接走生产 `set_request_context`，把 worker 的真实路径绕开了。R5 闸门只校验“只有 `packages.shared.rls` 能写 GUC”，无法发现“调用了错误的 helper”。

**修复方向（结构性，不是补丁）**

1. 给 worker 一个显式的服务身份上下文（tenant + organization + project scope），调用 `set_request_context`；项目范围必须来自受信配置/编排层，不能由事件 payload 自称。
2. 若 worker 需要跨项目批处理，应设计独立的“消费域”数据库角色与策略，而不是把 `*` 塞给普通资产域角色。
3. `run_once` / `consume_tenant` 必须有集成测试：用真实 asset 角色、只给 tenant → 断言 0 行/不推进；给完整上下文 → 断言推进；再删掉 org/project → 断言失败而不是 skipped。
4. 把 `NOT_FOUND`、`VALIDATION_ERROR`、真实状态冲突与“已终态重复”分开统计；只有后者可以静默幂等。

---

### R4-2（P1/P2）通用审批 `start` 不校验目标资源归属与目标动作状态，存在跨项目审批与 Phase Gate 绕过

**证据**

- `services/workflow/service/approval_service.py:46-71` 的 `start` 只做两件事：`binding_for(target_action_key)` 与 `spec.allowed_principals_text == binding.contract_text`。
- 它**没有**校验 `spec.resource_type == resource_type`，没有校验 `resource_id` 存在或属于 `project_id`，也没有校验目标动作的 `status`（ACTIVE / PHASE_GATED）。
- `:73-81` 只对 `WORKFLOW.START_APPROVAL` 做授权，`ResourceRef` 的 `project_id` 是票据 project，不是目标资源 project。
- `:83-115` 直接把客户端提交的 `resource_type` / `resource_id` / `project_id` 写进 `ApprovalFlow` 与 `ApprovalTicket`。
- `services/workflow/api/approval_routes.py:36-42` 的请求体允许客户端提交任意 `target_action_key` / `resource_type` / `resource_id` / `project_id`；`:62-89` 的 `start_approval` 只 `require_action("WORKFLOW.START_APPROVAL")`，不授权目标动作，也不校验目标资源。
- `packages/identity/action_registry.py:50-56` 的必填字段不含 `status`；`:120-129` 加载 `ActionSpec` 时直接丢弃 YAML 里的 `status`。因此“拒绝为 PHASE_GATED 动作发起票据”在运行时结构上没有落点。
- `contracts/runtime/approval_bindings.yaml:33-49` 为 `COMMUNITY.REUSE.APPROVE` 登记了 binding；`contracts/action_registry/action_registry.yaml:459-469` 该动作 `status: 'PHASE_GATED（Phase 2）'`，`CREATOR.RELEASE.REVOKE` 同样是 PHASE_GATED。
- `services/asset_service/service/delivery_approval_service.py:64-80` 只按 `resource_id` 查事件，不核对票据/事件的 project 与 manifest 的 project；`services/asset_service/repository/approval_event_source.py:19-30` 的 `ApprovalCompletedEvent` 根本没有 `project_id` 字段。

**可利用链（修复 R4-1 之后）**

1. 持有 `WORKFLOW.START_APPROVAL` 的“制片”在项目 A 发起 `DELIVERY.MANIFEST.APPROVE` 票据；
2. `resource_id` 指向项目 B 的 manifest，`project_id=A`；
3. 审批人按票据项目 A 获得 `WORKFLOW.SUBMIT_VOTE` 授权，可在未查看项目 B 资源的情况下完成会签；
4. worker 用正确的项目范围读取 B 的 manifest 后，会把它推进到 VALIDATED；事件本身没有 project 字段，消费端也无法反驳。

同时，`COMMUNITY.REUSE.APPROVE` 这类 Phase 2 动作已有 binding，`start` 不读取动作状态，因此可以为尚未开放的动作建票据并发出 `APPROVAL_TICKET_CREATED/COMPLETED`。

**修复方向**

- `start` 必须校验：目标动作 `resource_type` 与请求一致；目标动作状态为 ACTIVE；目标资源存在且 `project_id` 属于票据项目；目标资源类型必须与 binding 的合同原文一致。
- 目标资源校验必须通过域端口（例如 asset 域的只读检查端口）完成，不能信任客户端声称的 `project_id`。
- `APPROVAL_TICKET_COMPLETED` 事件必须携带 `project_id`（以及 producer/action），消费端必须比较 manifest.project_id、action_key、resource_type。
- `ActionSpec` 需要保留 `status`，并增加“PHASE_GATED 不得 start”的负向测试；`COMMUNITY.REUSE.APPROVE` 与 `CREATOR.RELEASE.REVOKE` 应作为固定样本。

---

### R4-3（P2）交付审批两步落库可能提交半截 `VALIDATING`，且 worker 吞掉所有 `DomainError`

**证据**

- `services/asset_service/service/delivery_approval_service.py:102-114` 先 `DRAFT→VALIDATING` 调 `_save`，再 `VALIDATING→VALIDATED` 调 `_save`；两次 `save_status` 都是独立 `UPDATE` + 乐观锁。
- `workers/delivery/consumer.py:80-87` 捕获 **所有** `DomainError`，只 `skipped += 1`，循环结束后 `session.commit()`。
- `services/asset_service/service/delivery_approval_service.py:93-100` 规定只有 `DRAFT` 可消费审批；一旦第一次 `_save` 被提交，清单就是 `VALIDATING`，后续任何消费都会被这条拒绝。
- `tests/unit/test_delivery_approval_service.py:222-229` 的 fake repo 在**第一次** `save_status` 就抛冲突；`FakeManifestRepo` 在 `:97-110` 只按 `conflict` 标志或版本不符抛，未模拟“第一次成功、第二次冲突”的半事务。
- `tests/integration/test_delivery_approval_consumption.py:151-165` 只覆盖顺序成功路径，没有并发第二次冲突的集成测试。

**影响**

第二次 `_save` 因并发冲突抛 `DomainError` 时，第一次的 `VALIDATING` 已 flush 且未回滚；worker 捕获后继续并最终 `commit()`，清单永久卡在 `VALIDATING`，无法再推进，也没有失败告警。与此同时，`NOT_FOUND`、`VALIDATION_ERROR`、真实状态冲突都被归入 `skipped`，故障被伪装成“幂等重复”。

**修复方向**

- 两次状态推进必须在同一原子边界内：savepoint + 失败回滚到 savepoint，或用一条受控 SQL/数据库函数完成两段登记边的推进。
- worker 捕获 `DomainError` 后必须显式 `rollback()`（或回滚 savepoint）再继续；不能把未回滚的会话直接 `commit()`。
- 区分“已终态且 audit_ref 相同的幂等返回”与真实失败；后者应计入 `failed` 并报警。
- 增加“第一次成功、第二次冲突”的 fake/集成测试，以及 worker 级测试。

---

### R4-4（P2）Gate 裁决幂等键包含 `decision`，同一快照下允许互相矛盾的裁决

**证据**

- `services/qc_governance/domain/gate_decision.py:243-255` 的 `idempotency_key()` 返回 `(gate_instance_id, constraint_snapshot_id, ruleset_version, decision.value)`。
- `services/qc_governance/service/gate_service.py:95-115` 只按这个四元组查重。
- `docs/adr/ADR-R23-gate-decision-as-fact.md:33-35` 明确“裁决可以重做，做法是新建一条（新快照 + 新理由 + 新证据）”；不同结论必须绑定新快照。

**影响**

同一 `gate_instance_id` + 同一 `constraint_snapshot_id` + 同一 `ruleset_version` 下，可以先记 `REJECTED`，再记 `APPROVED`（或反之），两者都被视为不同“事实”。这会让“取同实例最新 APPROVED 再比对快照”的派生逻辑被同一快照下的后写 APPROVED 绕过，削弱“基于同一输入快照的裁决不可改写”。

**修复方向**

- 幂等键应排除 `decision`，或对同一 `(gate_instance, snapshot, ruleset)` 的不同 `decision` 直接拒绝。
- 不同结论必须要求新 `constraint_snapshot_id`；在领域层与数据库唯一约束双份保证。
- 增加“同快照先 REJECTED 后 APPROVED 必须被拒”的负向测试。

---

### R4-5（P2）Capability 的 episode/shot/asset scope 被解析但从未执行

**证据**

- `packages/identity/capability.py:92-94` 定义 `episode_scope` / `shot_scope` / `asset_scope`；`:282-284` 解码时填充这些字段。
- `packages/identity/authorization.py:73-88` 的 `ScopeDimension` 只有 `tenant` / `organization` / `project`，并声称更深的 `shot` / `asset` / `asset_version` / `episode` 由 RLS 执行。
- `packages/identity/authorization.py:380-422` 的 `decide()` 只执行 organization 与 project 两个维度；`ResourceRef`（`:188-203`）也没有 episode/shot/asset 标识。
- `packages/identity/capability.py:164-182` 的审计输出只包含 project/allowed/denied，不输出这些子范围。

**影响**

一个被签发为“只允许 asset A / shot S”的 capability，在同一个 project 内的任意 AssetVersion / Shot 动作上仍然通过。RLS 只能按行上的 `project_id` 过滤，无法表达“这个 token 只允许某个 shot/asset”；因此“Capability 只能收窄”的主张在 project 以下层级不成立。空的 `shot_scope` / `asset_scope` 也没有 fail-closed 语义。

**修复方向**

- 要么在 `ResourceRef` 增加 episode/shot/asset 标识，并在 `decide()` 做子范围比较；
- 要么在契约与 README 明确“Capability 只支持 project 级收窄”，并停止在 token 中签发/解析这些未执行的子范围；
- 无论选哪条，都要有“asset A 的 token 访问 asset B 必须拒绝”的测试。不能既签发又忽略。

---

### R4-6（P2，需威胁模型确认）共享 outbox 的事件类型没有写入方所有权约束

**证据**

- `data/migrations/versions/c2d3e4f5a6b7_split_database_roles.py:186-204` 给项目域/资产域授予 `SELECT, INSERT ON outbox.outbox_events`。
- `data/migrations/versions/e8f9a0b1c2d3_create_qc_governance_gate_decisions.py:237-239` 给 QC 域同样权限。
- `data/migrations/versions/f0a1b2c3d4e5_create_workflow_approval_engine.py:260-261` 给 workflow 域同样权限。
- `services/asset_service/repository/approval_event_source.py:19-30` 的 `ApprovalCompletedEvent` 没有 producer 身份或 `project_id`；`workers/delivery/consumer.py:64-81` 把它当作唯一权威结论。
- 没有数据库约束或触发器限定“只有 workflow 能写 `APPROVAL_TICKET_COMPLETED`”。

**影响**

若任一持有 outbox INSERT 的域被攻破、出现 SQL 注入，或未来新增服务误写事件类型，可以伪造一条 `APPROVAL_TICKET_COMPLETED/APPROVED` 事件推进交付清单。数据库层没有第二道防线；消费端也没有 producer 校验。

**边界**

这不是已确认的外部可利用漏洞，而是“信任边界内的跨域写入”结构风险。是否在当前威胁模型内，需要结合 ADR-R24 的信任边界确认；但代码层面确实没有所有权约束。

**修复方向**

- 事件类型到 producer 的 allowlist（数据库触发器或按事件类型的独立 outbox 表/角色）。
- 事件携带 producer/aggregate 信息，消费端校验 producer 与 action/resource/project。
- 至少加一个“非 workflow 角色插入 APPROVAL_TICKET_COMPLETED 被拒”的数据库测试。

---

### R4-7（P3）发布包构建在 verify 失败时会留下未验证的 ZIP

**证据**

- `scripts/release_package.py:358` 的 docstring 写“任何闸门失败都不留下半成品”。
- `:378-379` 先 `_write_zip(output, ...)` 写出最终路径，再 `verify_release_zip(output, ...)`。
- `verify_release_zip`（`:296-344`）只做检查并抛 `ReleaseBuildError`，没有删除已写出的包。
- `_write_zip`（`:244-269`）只清理临时文件，最终 `temp_path.replace(output)` 已经把包落到 `output`。

**影响**

`build_release_package` 在 verify 失败时，操作员仍可能在输出目录拿到一个未通过校验的 ZIP；docstring 的安全承诺与实现不符。`--check` 路径用临时包，不受影响；但实际构建路径受影响。

**修复方向**

- 先写临时 ZIP，`verify_release_zip(temp_path)` 通过后再 `replace(output)`；或异常时删除 `output`。
- 对 `package_name`（`:363`）做路径字符/长度校验，避免 ZIP 成员名前缀包含路径分隔或控制字符。

---

### R4-8（P3）文档与注释漂移，会误导后续审计

**证据**

- `README.md:21-22` 仍写 `workers/*` 中 generation/qc/delivery/notifications 为占位；`README.md:121` 仍写 `DELIVERY.MANIFEST.APPROVE / RELEASE` 未实现、`workers/*` 仍为空目录。
- 但 D4 已实现 RELEASE：`services/asset_service/api/delivery_routes.py:143-175`；已有 `workers/delivery/consumer.py`。
- `services/asset_service/api/delivery_routes.py:7-10` 的模块注释仍写 APPROVE/RELEASE 尚未实现，与同文件 `:143` 的 RELEASE 路由矛盾。
- `services/asset_service/service/delivery_service.py:6-11` 仍写“APPROVE 尚未实现”。
- `packages/identity/enforcement.py:63` 写“applicant_subject_id 必须提供（缺失即拒绝）”，但 `packages/identity/authorization.py:339-342` 实际把缺失记为 `deferred_checks` 并放行到资源阶段；ADR-R23 已解释这是刻意设计，docstring 未同步。
- `README.md:115` 把 Capability 描述为“只由 decide() 判定主体/租户/组织/动作/项目范围”，与 R4-5 的子范围未执行问题叠加，会让人误以为 project 以下也已收窄。

**影响**

不改变运行时，但会让审计者把“已实现”读成“未实现”、把“已收窄”读成“全范围已收窄”。用户当前正依赖这些审计文档判断 Phase 1 状态，漂移本身会制造错误结论。

**修复方向**

- 同一次提交内更新 README、路由注释、服务 docstring；把“APPROVE 由 worker 消费、RELEASE 已实现”写清楚。
- 将 `enforcement.py:63` 改为“缺失时记 deferred，由资源阶段复核”，与 `authorization.py` 和 ADR-R23 对齐。
- 对“已实现/未实现”的清单，尽量从 `contracts/runtime/action_implementations.yaml` 派生，避免 README 手写。

---

### R4-9（P3）交付 worker 每轮全量扫描历史审批完成事件，没有水位线

**证据**

- `services/asset_service/repository/sqlalchemy_approval_event_source.py:43-56` 的 `list_completed` 只按事件类型/action/resource 过滤，`order_by(occurred_at.asc())`，没有 `limit`、没有 `since`、没有已消费水位。
- `workers/delivery/consumer.py:71-74` 每轮调用 `list_completed`，遍历所有历史事件。
- outbox 事件表是 append-only（迁移只授 SELECT/INSERT），历史只增不减。

**影响**

随着审批完成事件累积，每轮消费的时间与内存线性增长；worker 会反复重放所有历史 APPROVED 事件（虽然幂等），并在租户级别放大。当前功能被 R4-1 阻断，所以还不是首要问题；R4-1 修复后会立即暴露。

**修复方向**

- 增加消费水位（按 `occurred_at + event_id` 游标）或“未消费”索引/状态；不要靠反复全量扫描做幂等。
- 若坚持“以清单状态为幂等”，至少加 `limit` 与分页，并把扫描进度持久化到独立检查点表。

---

## 3. 工程铁律逐条核对

| 铁律 | 当前证据 | 判定 |
|---|---|---|
| 契约先登记、再实现 | `freeze_manifest` 通过，3 条修订有据；`contract_check` 显式登记 22 个 ACTIVE 未实现动作 | **通过（登记口径）**；但 R4-2 说明 binding 已登记、状态却在运行时被丢弃，属于“登记了但没执行” |
| 单一事实源 / 派生一致 | `contract_check` 全 PASS；Action/Schema/Event/Enum/审批台账一致 | **通过**；`ActionSpec` 丢 `status` 是运行时的第二事实缺口 |
| 分层与服务边界 | `architecture_check` R1–R4/R6 全 0 | **通过** |
| 数据所有权 / 按域角色 | 迁移按域授权；`test_delivery_manifest_immutability` 证明资产域列级 UPDATE；其它域无清单写权限 | **通过（静态）**；R4-6 显示 outbox 是共享写面 |
| RLS 上下文唯一注入 | `architecture_check` R5 仅允许 `packages.shared.rls` | **代码层通过，运行时未通过**：R4-1 用错 helper；R5 无法区分 helper |
| 项目范围隔离 | 迁移 restrictive policy；集成测试证明跨租户/跨项目读不到 | **通过（数据库层）**；R4-1 证明 worker 没进入这个保护 |
| append-only / 终态不可改写 | 触发器 + CHECK + 超级用户测试；Gate 端口无 save | **通过** |
| 权限最小化 / 职责分离 | 角色拆分、`SELF_APPROVAL_FORBIDDEN`、Agent CHECK；审批 AND/OR 纯函数 | **部分通过**：R4-2 跨项目审批、R4-5 Capability 子范围、R4-6 outbox 所有权 |
| 事务一致性 | 审批引擎状态+事件同事务；交付两步 `_save` | **未通过**：R4-3 半截 VALIDATING |
| 测试真实性 | 全量 pytest exit 0；定向 43 passed | **通过但覆盖不足**：没有 worker 测试；R4-1 正是被夹具绕开的路径 |
| 文档与实现一致 | README/路由/docstring | **未通过**：R4-8 漂移 |
| 密钥不落日志 / 发布包治理 | `release_package --check` 363 files；密钥扫描；commit-only | **基本通过**；R4-7 构建失败残留 |

## 4. 与前几轮结论的关系

- **N1/N2 修复仍有效，未见回归**：
  - `packages/shared/circuit_breaker.py:68-75` 在 `enabled=False` 时跳过阈值/冷却期校验并把阈值归一到 >=1；
  - `workers/relay/webhook.py:57` 的 `WebhookCircuitOpenError` 同时继承 `WebhookDeliveryError` 与 `DeliveryDeferredError`；
  - `:67-73` 只把 5xx/408/425/429 视为端点失败；`:130-143` 4xx 只记事件失败并 `record_success`；`:112-113` 熔断 OPEN 明确“未外发”；
  - `workers/relay/errors.py:23-29` 与 `workers/relay/consumer.py:249-265` 对 `DeliveryDeferredError` 不递增 attempts、不写退避、不推进死信，并立即停止该租户本轮。
- **N4 资源 schema 豁免已从 4 条降到 1 条**：`contract_check` 本轮输出“豁免 1 个资源类型”。
- **N3 转移入口债务**：`scripts/contract_check.py:1237-1290` 已实现反向完备性（有转移边但无已实现入口、且未登记债务 → FAIL；删条目/删文件不能变绿）。**机制存在**，但本轮未做删除条目/删除文件的实际变异验证，不宣称变异层已闭合。
- **六根因最终核对的边界需要修正**：上一轮称“F3 运行时闭合，仅余 HTTP 签发/nonce 去重债务”。本轮证据表明 F3 的 **project 以下子范围从未执行**（R4-5），因此“运行时闭合”只能按 project 级理解，不能按 Capability 全字段理解。
- **F5 仍为部分闭合**：ACTIVE 39 / implemented 17 / not_implemented 22；Phase 1 未验收。

## 5. 当前代码的正面结论（不因上述问题而否定）

- D1–D4 的领域边界、append-only 清单、内容列不可变触发器、状态转移登记表、按域数据库角色、Outbox 事实/投递分层、Gate 裁决 append-only、审批 AND/OR 纯函数、release 包 commit-only/密钥扫描，均有代码与测试支撑。
- 五道闸门是真绿，没有发现“闸门假绿”级别的新问题；问题在于闸门覆盖边界与运行时实际路径不一致。
- RLS 策略本身是正确的：只设 tenant 看不到项目级资源，这正是 R4-1 的证明；不能因为 worker 用错 helper 就放宽策略。
- `DeliveryApprovalService` 的“只从权威事件读结论、不接受客户端审批结果”的设计方向正确；缺陷在事件记录与项目归属的绑定。

## 6. 建议修复顺序

1. **R4-1**：先让 worker 用完整 RLS 上下文跑通一条真实清单；加 `run_once` 集成测试；把 `DomainError` 分类。没有这一步，D4 在生产上不可用。
2. **R4-2**：`start` 校验目标动作状态/资源类型/资源项目；审批完成事件带 project；消费端比对 manifest project。与 R4-1 叠加后，这是跨项目审批链的关键闸门。
3. **R4-3**：savepoint/单条 SQL 原子推进；worker 显式 rollback；补“第二次冲突”测试。
4. **R4-4**：Gate 幂等键排除 decision 或强制新快照；补同快照矛盾裁决负向测试。
5. **R4-5**：执行 Capability 子范围，或从契约/README 撤回未执行字段；不能继续“签发但忽略”。
6. **R4-6**：按威胁模型决定是否加事件类型 producer allowlist；至少补数据库负向测试。
7. **R4-7/R4-8/R4-9**：发布包清理、文档对齐、消费水位线；不阻断主链，但不能继续积累。

## 7. 本轮边界与未完成事项

- 本轮只做审查，未修改任何代码、契约、迁移；工作区只新增本报告文件。
- 未提交、未推送本轮报告。
- 未做 N3 的破坏性变异验证（删债务条目/删债务文件），只确认反向完备性代码存在。
- 未重新设计或实现任何修复；第 6 节只是修复方向，不是已落地变更。
- RLS 实证使用本地测试库，96 行是测试种子产生的数据，不代表生产数据量；结论是策略行为，不是行数结论。
- `docs/proposals/shot-domain-contract-closure.md` 按用户要求本轮不处理。
