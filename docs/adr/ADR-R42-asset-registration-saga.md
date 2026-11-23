# ADR-R42：资产登记的跨域异步 Saga 与身份边界裁定

- 状态：DECIDED（项目所有者 2026-11-23 拍板；固化决策，不激活实现）
- 日期：2026-11-23
- 触发：生产闭环审查（基线 `7e96578`）P1-02 / P1-03 —— `UnwiredAssetRegistrar` 占位与
  `GenerationService.finish()` 接受自报产物 ID
- 关联：ADR-R40（统一授权模型）、ADR-R41（迁移谱系边界）、Phase 0 冻结基线 V205-R15
- 指纹：本正文不内嵌自身哈希；若本 ADR 纳入冻结产物，指纹按 append-only 口径采集登记
- 范围：本裁定只确定**决策与契约变更清单**，不实现、不改契约派生文件、不重签冻结清单。
  实现按第 7 节顺序另行立项。P1-04（真实 PG 并发验收）与本裁定独立，不互相等待。

---

## 1. 裁定摘要

| 编号 | 议题 | 裁定 |
|---|---|---|
| D1 | 登记架构 | **Outbox 驱动的异步 Saga**。禁止在 Generation Worker 内直接跨域调用 AssetService 并拼接两个 Session；禁止引入包装类把两个域的事务边界藏起来。 |
| D2 | 执行身份 | 专用内部工作负载身份 `svc:asset-registration`，仅由受信任的内部事件处理器构造。 |
| D3 | 租户与归属 | 来源 Generation Task 是授权依据，数据库复合外键是最终防线；不信任 Outbox payload 单方声明。 |
| D4 | 事务 | 三个域内本地事务（T1/T2/T3），各域本地原子提交；**不承诺跨域原子性**，不共享 Session，不引入伪 2PC。域间至少一次交付，幂等是运行条件而非附加项。 |
| D5 | 幂等 | 以 Generation Task 为业务登记依据，建立有业务意义的**登记回执**记录，唯一约束至少覆盖 `(tenant_id, generation_task_id)`。随机 `entity_id` 不构成幂等键。 |
| D6 | 失败补偿 | 持久化回执、幂等重放、死信与人工处置；**不删除已登记的不可变资产**，不通过重新生成掩盖登记失败。 |
| D7 | 成功终态权威 | `asset_version_id` 来自 AssetService 登记回执；`quality_profile_version` 来自任务创建时冻结的质量策略；Generation Task 的 SUCCEEDED 来自登记完成事件。Worker 不再拥有自行声明资产已登记的权力。 |

D1–D7 为本次拍板的**四条红线**（D2 身份 / D3 租户 / D4 事务 / D6 补偿）加上由此推导的架构与终态权威。
任一条被以「先跑通再说」绕过，本裁定的验收结论即不成立。

---

## 2. 已核实的现状事实（裁定依据，均基于 `7e96578` 实读）

| 事实 | 证据 |
|---|---|
| GenerationTask 状态枚举只有 `REQUESTED/RUNNING/SUCCEEDED/FAILED`，无等待资产登记态 | `contracts/schemas/object_schemas.schema.json` 状态机定义 |
| `AssetRegistrar.register()` 返回 `(asset_version_id, quality_profile_version)`，占位实现必抛 `AssetRegistrationUnavailableError` | `workers/generation/executor.py:99-120` |
| `GenerationService.finish()` 对 SUCCEEDED 只做 `task.attach_product_refs(...)`，不验证资产是否存在或归属是否匹配 | `services/production_planning/service/generation_service.py:180-232` |
| `ProductionRoute` 记录路线类型、能力引用与依据，无 QC Profile 绑定字段 | `services/production_planning/domain/production_route.py:35-65` |
| 身份层只有 `issue_token()` 签发函数与 JWKS 公钥分发，无内部服务身份签发 HTTP 端点 | `services/identity_issuance/__init__.py:7,69`；`workers/identity/` |
| AssetService 是 `AssetVersion` 唯一业务写入者，创建与 `ASSET_VERSION_CREATED` 事件同事务提交 | `services/asset_service/service/asset_service.py:202` |
| `GENERATION_SUCCEEDED` 由 Generation 域 `advance_status` 写出，供 QC Relay 消费 | `services/production_planning/service/generation_service.py:210-228` |

**关键推论**：现状不是「把占位换成真实调用」即可闭环——`finish()` 的入参语义、任务状态机、质量策略来源
三处都要同时改，否则登记完成即便发生，Generation 仍可能凭一个非空字符串自报成功。

---

## 3. 目标流程与事务边界

```
Generation Worker
    | 模型生成
    v
受控对象存储  ── locator / sha256
    |
    v
Generation Domain (T1，同一个 Gen UoW)
    1. 持久化产物清单（locator、sha256、attempt 标识、写入意图）
    2. 状态 → AWAITING_ASSET_REGISTRATION
    3. 写 ASSET_REGISTRATION_REQUESTED Outbox
    |
    v  Outbox Relay
Asset Registration Handler (Asset Service UoW，T2)
    1. 校验来源任务 / 租户 / 项目 / 产物 / locator+sha256
    2. 幂等登记 AssetVersion + 登记回执（同一资产域事务）
    3. 写 ASSET_REGISTRATION_COMPLETED / REJECTED
    |
    v  Outbox Relay
Generation Domain (T3，同一个 Gen UoW)
    1. 校验登记回执
    2. 绑定 asset_version_id
    3. 状态 → SUCCEEDED
    4. 写 GENERATION_SUCCEEDED Outbox
    |
    v  现有 QC Relay
QC Task
```

| 事务 | 所属域 | 必须原子提交的内容 | 数据库角色 |
|---|---|---|---|
| T1 | Generation | 产物清单、写入意图、等待登记状态、`ASSET_REGISTRATION_REQUESTED` | `production_planning_role` |
| T2 | Asset Service | 登记回执、`AssetVersion`、完成/拒绝事件 | `asset_service_role` |
| T3 | Generation | 回执核验、资产 ID 绑定、任务终态、`GENERATION_SUCCEEDED` | `production_planning_role` |

三个事务分别由各域自己的数据库角色完成。域间以 Outbox 形成**至少一次**交付；重复消费是正常运行条件，
必须由幂等吸收。任何「用一个 Session 跨两个域写半边再补另一半」的实现，无论包装成何种端口，均违反 D4。

---

## 4. 红线细化

### 4.1 身份（D2）

明确**不批准**：

- Worker 内直接构造 `Principal(subject_id="asset-worker", ...)`；
- Generation Worker 直接调用 `issue_token()`，把私钥带入业务进程；
- 用普通用户身份冒充系统身份；
- 由 API 请求体或 Worker 自行声明租户、资产归属、审批身份。

当前 `workers/identity` 只有 JWKS 公钥分发、没有内部 Token 签发端点，签发方与验签方职责已分离。
**不得把尚未存在的系统身份签发能力当成已可用接口。** 若实现期需要真实 Token，须先立项补齐该能力，
并经另行裁定；本 ADR 不授权在业务进程内自签。

`svc:asset-registration` 的权威属性：

| 属性 | 权威来源 |
|---|---|
| 服务主体 | 固定登记的 `svc:asset-registration` |
| 执行动作 | 仅允许生成产物登记 |
| 来源任务 | 已持久化的 Generation Task |
| 租户 | 来源任务的 `tenant_id` |
| 项目 | 来源任务的 `project_id` |
| 来源事件 | `ASSET_REGISTRATION_REQUESTED` |
| 审计创建人 | `svc:asset-registration` |

工作负载身份只能由受信任的内部事件处理器构造，不能由 API 请求体传入。内部处理器通过独立的资产域
数据库角色写入，**仍然必须经过 AssetService 的领域规则**，不得成为绕过资产域的第二套写入实现。

### 4.2 租户与归属（D3）

不只看 Outbox payload 的 `tenant_id` / `project_id`。T2 必须先验证来源任务及其产物清单：

1. 来源任务真实存在；
2. 任务确实处于 `AWAITING_ASSET_REGISTRATION`；
3. 产物属于该任务；
4. 项目与任务一致；
5. 项目与租户归属一致；
6. locator 与 SHA256 与持久化产物清单一致。

确认后在 AssetService UoW 内通过 `set_session_scope()` 注入租户与组织上下文，并保留数据库复合外键
`(project_id, tenant_id)`，使跨租户挂靠在数据库层也无法成立。Generation Task 创建时的项目授权证明
「任务发起合法」；AssetService 验证「登记对象与任务事实一致」。系统身份不天然拥有任意项目的资产创建权。

### 4.3 幂等（D5）

- 一个 Generation Task 的一个正式产物只能登记成一个对应资产；
- 登记请求携带稳定的 `generation_task_id` 与产物标识；
- 相同任务、相同产物重复消费 → 返回**原登记结果**，不新建资产；
- 相同任务出现不同 SHA256 / 项目 / locator → 报**幂等冲突**，不改变已有回执；
- 不允许创建第二个资产来掩盖来源冲突。

登记回执是有业务意义的持久化事实，既作事件去重依据，也是登记成功或永久拒绝的权威记录；
`AssetVersion` 与它在 T2 同一事务内建立关联。它**不是**第二套 Validator。

### 4.4 失败补偿（D6）

| 故障场景 | 权威处理方式 |
|---|---|
| 模型服务临时失败 | 按生成任务重试策略处理 |
| 对象存储临时失败 | 不登记成功，保留失败证据 |
| 产物已存储、登记事件未确认 | 从持久化产物清单恢复登记，不重复调用模型 |
| AssetService 暂时不可用 | Outbox 退避重试 |
| 登记成功、Generation 未完成 | 由登记回执与 Outbox 重放完成关联 |
| 事件重复投递 | 按登记回执幂等返回 |
| 来源任务与资产内容不一致 | 拒绝完成，形成可审计冲突 |
| 永久业务校验失败 | 写正式拒绝回执与拒绝事件，Generation 转 FAILED |
| 多次重试仍无法恢复 | 进入死信与人工处置，不静默丢弃 |

**孤儿对象防护**：对象存储写入成功但 Generation 事务提交前进程崩溃，会留下无资产引用的对象。
登记前先持久化带 locator、摘要与 attempt 标识的**产物写入意图**，成功落盘后再提交正式登记事件；
长期未被引用的对象采用「引用核对 + 保留期保护」的垃圾回收。禁止按文件年龄直接删除，
禁止删除已登记的不可变资产作为补偿。

---

## 5. 必须同步修正的契约问题

| 编号 | 契约项 | 变更要求 |
|---|---|---|
| C1 | GenerationTask 状态机 | 增加 `AWAITING_ASSET_REGISTRATION`，进入条件为 T1 提交；`SUCCEEDED` 只能由登记完成事件驱动的 T3 推进。 |
| C2 | Generation 产物清单 | 新增带 `locator`、`sha256`、`attempt_id` 与写入意图的持久化结构；Worker 不得只在内存里持有产物。 |
| C3 | Finish API | 移除 Worker 自报的 `asset_version_id` / `quality_profile_version` 成功入参；成功终态改由 T3 内部推进。 |
| C4 | ProductionRoute / QC Profile 绑定 | 补齐质量策略权威来源；`quality_profile_version` 在任务创建时按冻结路线确定并作为不可变任务输入持久化。 |
| C5 | 登记回执对象 | 新增有业务意义的登记回执，唯一约束至少 `(tenant_id, generation_task_id)`，与 `AssetVersion` 同事务建立关联。 |
| C6 | Outbox 事件 | 新增 `ASSET_REGISTRATION_REQUESTED` / `COMPLETED` / `REJECTED`；`GENERATION_SUCCEEDED` 仅在 T3 写出。 |
| C7 | QC Task 入队依据 | 保持消费 `GENERATION_SUCCEEDED`；事件负载中的资产 ID 必须来自登记回执而非 Worker 声明。 |

C1–C7 是**变更清单**，不是已实施事实。任何一项落地前必须按冻结治理走「契约修订登记 → 派生文件 →
冻结重签」顺序；字段级演进由现行 runtime 演进台账与 contract 闸门守护。

---

## 6. 显式不批准的实现（验收时逐条对照）

1. 同步双 Session 适配器：在 Worker 内先后开 Generation 与 Asset 两个 Session「制造原子性」。
2. 包装类隐藏事务边界：把跨域调用藏在 `AssetRegistrar` 实现内部，对外仍返回二元组宣称闭环。
3. Worker 自报成功：非空 `asset_version_id` / `quality_profile_version` 即 `SUCCEEDED`。
4. 随机 ID 幂等：只靠 `AssetVersion` 的随机 `entity_id` 去重。
5. 补偿性删除：删除未引用对象时不核引用、不设保留期，或删除已登记资产。
6. 自签系统身份：把签发私钥带入业务进程，或在请求体接受 `principal` / 身份字段。

---

## 7. 落地顺序（决策，不是进度）

| 顺序 | 工作 | 产出 |
|---|---|---|
| 1 | P1-04 真实 PG 并发验收 | 独立并发集成证据（可与本裁定并行，不互相等待） |
| 2 | 本 ADR 固化 | 身份/租户/事务/幂等/补偿的正式决策 |
| 3 | 生产域契约与状态机调整 | `AWAITING_ASSET_REGISTRATION` 与产物清单 |
| 4 | Asset Service 登记回执与内部登记用例 | 唯一业务写入路径 |
| 5 | Outbox Relay 双向交接 | 登记请求、完成、拒绝事件 |
| 6 | 跨域真实 PG 故障注入测试 | 重复投递、提交后崩溃、恢复、拒绝与冲突证据 |

---

## 8. 何时重新开裁

- 内部服务身份签发能力立项落地，且需要真实 Token 而非事件驱动身份；
- 决定引入跨域事务协调器（此时必须重新评估 D4，而非静默加例外）；
- 资产登记范围扩展到非 Generation 来源（审查、导入、人工补录）；
- QC Profile 的权威来源从 ProductionRoute 迁移到独立策略域。

## 9. 本裁定的局限

- 它是**架构与身份边界裁定**，不是实现方案，也未授权任何代码变更或契约派生文件变更。
- 它不宣称 R16 Phase 2/3 完成，不改变 Phase 0 的 CONDITIONAL / BLOCKED 状态；
  生产闭环是否可验收，须待第 7 节全部落地并取得真实 PG 证据后重新审查。
- P1-04 通过标准独立成立：Generation 与 QC 两条链都需真实数据库并发证据；单测事务次数断言、
  `SKIP LOCKED` 静态检查或 CI 全绿均不能替代。
