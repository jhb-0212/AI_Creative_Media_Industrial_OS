---
doc_type: proposal
status: proposed
updated_at: 2026-11-29
reviewed_at: 2026-11-29
review_decision: approved_with_conditions
proposal_baseline: 19c1ff9
authoritative_sources:
  - docs/adr/ADR-R47-generation-spec-derivation.md
  - docs/audits/2026-11-29-adr-r47-p1-review-decision.md
  - workers/generation/executor.py
  - services/production_planning/domain/generation_task.py
  - services/project_planning/domain/shot_spec.py
  - services/production_planning/api/generation_routes.py
  - packages/shared/storage_locator.py
  - contracts/schemas/object_schemas.schema.json
open_items: []
---

# GenerationSpec 契约提案（ADR-R47 P1）

状态：以文首 front-matter 为准（`status: proposed`，`review_decision: approved_with_conditions`）。
**本文件是提案，不是契约变更**：不新增对象 schema、不改字段签名、不建迁移、不改代码、不重签冻结清单。
P1 已由 Owner 于 2026-11-29 评审（结论见 `docs/audits/2026-11-29-adr-r47-p1-review-decision.md`）：
三项裁决**批准**（均带附加约束），并追加六项必须在 P2 前固化的约束。P2 仍需单独授权。

## 评审修订记录（2026-11-29，基线 `19c1ff9`）

原文结论不改写，以下增量追加：

| 编号 | 修订 | 章节 |
|---|---|---|
| R-1 | SizePolicy 冻结规则与「冲突不得静默覆盖」 | §4.2 |
| R-2 | 正式任务 / 预览双通道；Preview 不得复用正式语义 | §4.4 |
| R-3 | 哈希身份域、规范化规则、租户入域决定、碰撞检测 | §2.1 |
| R-4 | 不可变三层保护与「可信管理边界」表述 | §5 |
| R-5 | GenerationSpec / ExecutionRecord 边界；字段补 `schema_version`、`derivation_version`、`size_policy_*`、`model_id`、`provider`、`generation_parameters` | §1.1、§1.4 |
| R-6 | 并发唯一冲突与任务幂等语义 | §4.1 |
| R-7 | `prompt_ref` 外部暴露边界（不返回） | §3.3 |
| R-8 | 严格解析清单补 `schema_version`，禁止降级 | §2.3、§6.3 |

关联：ADR-R47（生成规格派生与冻结，方向 DECIDED）、ADR-R42 C4（任务受理时冻结质量策略，本提案沿用「受理时冻结」范式）、
ADR-R44（不涉长流程引擎）、基线 8.1 / 8.12、契约 SR-6。

---

## 0. 一页结论

| 问题 | 提案结论 |
|---|---|
| 提示词放哪 | 放 `GenerationSpec`（不可变规格行），**不**进 Shot / ShotRevision，**不**进 `generation_tasks`，**不**建提示词库 |
| 怎么引用 | `generation_tasks.prompt_ref = "genspec:<generation_spec_id>"`；`generation_spec_id` 由规格内容确定性派生（租户入域） |
| 引用凭什么证明不可变 | 不是「前缀可信」，而是三条结构性保证：确定性 id（内容变则 id 变）+ 任务侧 CHECK 等式 + Worker 重算 `spec_sha256` 并与 id 比对 |
| 类型/尺寸谁说了算 | 规格携带 `media_type` / `size` / `aspect_ratio` + `size_policy_version`；创建时解析冻结、解析失败即拒绝；Worker 不重新解释、不读「最新策略」 |
| 跨租户 | `generation_specs` 按 `tenant_id` 做 RLS；(tenant_id, generation_spec_id) 复合外键；跨租户读不到也引用不上；哈希入租户域 ⇒ 不同租户不同 id |
| 老任务 | `inline:` 保留；`generation_spec_id` 可空；新增 CHECK 只约束新形态，不回填历史 |
| 正式 / 预览 | 正式任务要求 `ShotSpec` = `LOCKED`；`DRAFT` 无预览通道（不支持），不得在正式接口加状态例外 |
| 响应暴露 | `generation_spec_id` 与 `prompt_ref` **都不进**普通任务响应；外部按 `generation_task_id` 查状态 |
| 运行信息 | 不进规格；归 `ExecutionRecord`（只定义边界，P3 之后落地） |
| 新增动作 | **不需要**。GENERATION.REQUEST 不变；规格创建是任务创建的内部步骤 |

---

## 1. 对象：`GenerationSpec`

### 1.1 字段（P1 提案值）

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `generation_spec_id` | string | 是 | ≤64，形态见 §2.1 | 内容确定性 id，不是随机 id |
| `tenant_id` | string | 是 | 非空 | RLS 与复合外键的前提 |
| `entity_version` | int | 是 | ≥1 | 出生即 1，此后不得变（不可变对象没有版本推进） |
| `project_id` | string | 是 | ≤64 | 归属校验用（Worker 侧比对任务 project_id） |
| `shot_revision_id` | string | 是 | ≤64 | 精确修订引用，不允许「当前最新」 |
| `shot_spec_id` | string | 是 | ≤64 | 精确规格引用 |
| `production_route_ref` | string | 是 | ≤64 | 路线引用（类型/尺寸解析的输入之一） |
| `constraint_snapshot_id` | string | 是 | ≤128 | **只登记，不解析**：当前无快照对象（见 §4.3） |
| `template_version` | string | 是 | ≤64，`^[a-z0-9][a-z0-9._-]*$` | 派生规则版本；同输入+同版本 ⇒ 同 spec_sha256 |
| `derivation_version` | string | 是 | ≤64，同上 | `ShotSpec` → 提示词/参数的派生规则版本（与模板可不同步） |
| `size_policy_id` | string | 是 | ≤64，同上 | 尺寸策略标识 |
| `size_policy_version` | string | 是 | ≤64，同上 | 被引用策略版本；**该版本被引用后不得原地修改** |
| `model_id` | string | 是 | ≤128 | 请求的目标模型（不是适配器）；防「配置默认模型变化」导致不可复现 |
| `provider` | string | 是 | ≤64，同上 | 模型服务提供方（与 `base_url` 分开：域名可变，提供方不变） |
| `generation_parameters` | object | 是（可为空对象） | 键必须在**允许清单**内（`seed`/`steps`/`guidance_scale` 等），值只允许 `string` / 整数 / 布尔，键数 ≤16 | 影响结果的实际参数；未知键 → 创建时拒绝（不做自由 blob） |
| `prompt_text` | string | 是 | 1–8192 字符，非纯空白 | 提示词正文（派生结果，冻结） |
| `negative_prompt` | string | 否 | 1–4096 字符 | 缺省 = 不传负向提示词；空串与纯空白**拒绝**（两者语义不同，不能混同） |
| `spec_sha256` | string | 是 | 64 位小写十六进制 | 对规范化字段集计算（§2.1） |
| `media_type` | string | 是 | 枚举：`image/png` / `image/jpeg` / `image/webp` / `video/mp4` / `video/webm` / `audio/wav` | 必须能被 `extension_for_media_type` 映射；`application/octet-stream` **不允许**（它会掩盖「不知道产物是什么」） |
| `size` | string | 是 | `^[1-9][0-9]{2,4}x[1-9][0-9]{2,4}$` | 像素尺寸；不是比例，比例是 `aspect_ratio` |
| `aspect_ratio` | string | 是 | `^[1-9][0-9]{0,2}:[1-9][0-9]{0,2}$` | 与 `size` 必须一致（§4.2） |

`schema_version` 取对象信封既有字段（不新增列），参与哈希身份域：它回答「这一行按哪版结构解释」，
Worker 只执行自己支持的结构版本（§6.4）。`entity_version` 出生即 1 且永不推进——不可变对象不需要版本推进。

### 1.2 不放进字段的东西（刻意的）

| 不放进 | 理由 |
|---|---|
| `prompt_ref` / `generation_task_id` | 规格不知道「谁引用我」。写进去就多了一条可漂移的反向引用；引用由任务侧单向持有 |
| `model_adapter_ref` | 它回答「用哪个适配器调」，已在 `generation_tasks` 上；规格记的是 `model_id` / `provider`（回答「要哪个模型」）——两者不是同一概念，不构成双权威 |
| `locked_dimensions` / 镜头内容 | 属 ShotSpec / ShotRevision，按 SR-6 保持单一事实源 |
| 运行信息（网关主机、延迟、响应 id、HTTP 码） | 属 ExecutionRecord（§1.4）；塞进规格会让它退化成运行日志 |
| `status` | 无生命周期：出生即冻结。P2 在契约里登记为**占位对象**（与现有 35 个占位一致），避免闸门把它当成缺状态机 |

### 1.3 与现有对象的关系

```
ShotRevision (精确修订) ──┐
ShotSpec     (LOCKED)    ──┼─► 任务创建事务 ─► GenerationSpec（不可变, 内容寻址 id）
ProductionRoute          ──┤                       │
ConstraintSnapshot(预留) ──┘                       │ prompt_ref = genspec:<id>
                                                    ▼
                                          GenerationTask（prompt_ref + 可空 generation_spec_id）
```

### 1.4 与 `ExecutionRecord` 的边界（只定义，不落地）

| 维度 | `GenerationSpec` | `ExecutionRecord`（本文不实现） |
|---|---|---|
| 回答的问题 | 当时**要求**生成什么（输入快照） | 当时**实际**发生了什么（运行事实） |
| 时间点 | 任务创建，同事务冻结 | Worker 执行时写入 |
| 内容 | 提示词、类型、尺寸、比例、策略版本、模型 id、参数、摘要 | 网关主机、请求/响应标识、HTTP 码、耗时、重试次数、实际返回字节摘要 |
| 可变性 | 不可变 | 追加写 |
| 归属 | 本提案（P2） | 网关取证 / 可观测性（P3 E5 及之后） |

**为什么必须分开**：把运行信息塞进规格，会让「输入快照」变成「不断膨胀的运行日志」，并让
「同输入同哈希」不再成立——同一份规格重跑两次的耗时必然不同，哈希就不可能稳定。
反过来，把执行事实只留在日志里而不落结构化记录，则半年后无法解释「为什么当时是那个模型/参数」。

**半年后能否解释一次生成**：规格给出 `schema_version` / `derivation_version` / `size_policy_version` /
`model_id` / `provider` / `generation_parameters` / `spec_sha256`；执行侧给出实际端点与响应标识。
两边靠 `generation_task_id` 关联，不靠规格冗余运行字段。

---

## 2. 引用与完整性：`genspec:<id>` 凭什么可信

### 2.1 确定性 id（先解决「id 只是形式」的问题）

```
domain_separator = "afs:generation-spec:v1"
identity_input = domain_separator || 0x0A || tenant_id || 0x0A || canonical_bytes
spec_sha256 = sha256(identity_input)                       # 64 hex
generation_spec_id = "gs1_" + base32(spec_sha256)[:26]     # 30 字符，≤64
```

**租户是否入域：入。** 同一份内容在不同租户会得到不同 id。理由：否则「两个租户的 id 相同」
本身就泄露「规格内容相同」这一事实，且会让跨租户去重变成隐式行为。代价是跨租户不能复用同一规格——
按租户作用域去重即可，没有跨租户复用的业务需求。

**规范化规则（`canonical_bytes` 的唯一来源，实现不得自行发挥）**

| 维度 | 规则 |
|---|---|
| 序列化 | JSON 对象键按 UTF-8 字节序升序；分隔符 `,` 与 `:` 无多余空白；不使用 `ensure_ascii`（中文原样输出） |
| Unicode | 先做 NFC 规范化，再序列化；不做大小写折叠、不去 emoji、不做同义字替换 |
| 空白 | **不 trim**。正文首尾空格会改变生成结果，因此原样入哈希；仅拒绝「纯空白」正文 |
| 换行 | `\r\n` 与 `\r` 统一为 `\n`（跨平台编辑差异不应改变身份） |
| 数值 | 整数不带小数点；`generation_parameters` 中的数值只允许整数，或**由字符串承载**的定点小数（避免二进制浮点尾数差异）；`guidance_scale` 等以字符串 `"7.50"` 形式入哈希 |
| null / 缺省 / 空串 | 必填字段必须存在且非 null；`negative_prompt` 缺省 ⇒ 键不出现（不是 `null`、不是 `""`）；`""` 与缺省在语义上不同且都被显式处理（缺省=不传负向提示词，空串=拒绝） |
| 布尔 | `true` / `false` 小写 |
| 字段集 | 必含 `schema_version` / `project_id` / `shot_revision_id` / `shot_spec_id` / `production_route_ref` / `constraint_snapshot_id` / `template_version` / `derivation_version` / `size_policy_id` / `size_policy_version` / `model_id` / `provider` / `generation_parameters` / `prompt_text` / `negative_prompt` / `media_type` / `size` / `aspect_ratio` |
| 不入哈希 | `generation_spec_id`、`entity_version`、`created_at`、`created_by` 等信封字段（tenant 已在域前缀，不重复入体） |

**碰撞检测（130 bit 不是「不会碰撞」，而是「碰撞要可发现」）**

- id 冲突时（同租户同 id、不同完整 SHA-256）⇒ 拒绝写入并报警，**不允许覆盖**；
- 同 id 且完整 SHA-256 相同 ⇒ 视为同一规格，按复用处理（§4.1）；
- 只用前缀比较 id，永远不用前缀断定内容相同。

**与 ADR §3 的一处偏离（需 Owner 知悉）**：ADR §3 列了 `prompt_sha256` 与 `spec_sha256` 两列；
本提案把前者**并入**后者（正文属于规范化字段集）。若 Owner 需要独立列以便与网关证据对话，
P2 可加一个**派生列** `prompt_sha256`（不参与身份域、不参与一致性判定），不改变哈希规则。

### 2.2 Worker 的三步校验（缺一即失败）

| 步 | 校验 | 失败码 |
|---|---|---|
| 1 | `task.prompt_ref` 必须等于 `"genspec:" + task.generation_spec_id`（DB CHECK 已保证，Worker 再验一次以防绕过写入口） | `spec-mismatch` |
| 2 | 按 id 取规格；不存在 → 失败；`spec.tenant_id` 与任务租户不一致 → 失败（RLS 之外的第二道） | `spec-missing` / `spec-foreign-tenant` |
| 3 | `spec.schema_version` 必须在 Worker 支持集合内（否则失败，不做结构兼容猜测） | `spec-schema-version-unsupported` |
| 4 | 从**库内字段**重算 `spec_sha256` 与 id：哈希不符 → 失败（行被改过）；哈希与 id 不符 → 失败（id 被拼错或伪造） | `spec-tampered` |

四条合起来回答「引用形式不自动证明不可变」：**不可变由 id 派生 + DB 约束 + 读取时重算共同证明**，不靠 `genspec:` 这个前缀。
顺序是刻意的：先证明「引用对得上」，再证明「东西存在且属于你」，再证明「我认识这个版本」，最后才重算哈希——
把最贵的计算放在最后，避免用一次完整重算去回答一个前缀就能回答的问题。

### 2.3 `prompt_ref` 两种形态的约束

| 形态 | 允许条件 | 解析路径 |
|---|---|---|
| `inline:<正文>` | 仅当部署配置 `GENERATION_INLINE_PROMPT_ENABLED=true`（默认 false；开发/联调用），且正文 1–200 字符 | 现 `resolve_inline_prompt`（行为不变） |
| `genspec:<id>` | 生产唯一形态；`id` 必须同任务 `generation_spec_id` | P2 新增 resolver |

- 未知前缀（例如裸 id、`asset:`、`prompt:`）→ `prompt-unresolvable`，**不得回退**为「当正文用」。
- 生产环境（`GENERATION_INLINE_PROMPT_ENABLED=false`）收到 `inline:` → 创建阶段拒绝（`PROMPT_INLINE_FORBIDDEN`），而不是等到 Worker 才失败——早失败省一次排队。
- **`genspec:` 的拒绝清单**（任一命中即失败，且不尝试任何替代路径）：

| 条件 | 失败码 |
|---|---|
| 格式非法（缺 `gs1_`、长度不符、含非法字符） | `spec-unresolvable` |
| 按 id 查不到规格 | `spec-missing` |
| 规格租户 ≠ 任务租户 | `spec-foreign-tenant` |
| 重算哈希 ≠ 库内 `spec_sha256`，或哈希 ≠ id | `spec-tampered` |
| `schema_version` 不在 Worker 支持集合 | `spec-schema-version-unsupported` |
| `prompt_ref` ≠ `genspec:` + `generation_spec_id` | `spec-mismatch` |

「不支持版本」是独立失败码而不是「解析失败」：排障时这两件事的处置完全不同（前者要升级 Worker，
后者要修数据）。**不允许**把 `genspec:` 降级为 `inline:` 解析，也不允许把 `inline:` 当 `genspec:` 读——
两条路径由前缀在入口处分流，之后不再交叉。

---

## 3. 任务侧的结构性绑定（不新增响应字段）

### 3.1 新增列与约束

| 项 | 内容 |
|---|---|
| 新列 | `generation_tasks.generation_spec_id`：string(64)，**可空** |
| CHECK（形态互斥） | `prompt_ref LIKE 'inline:%' AND generation_spec_id IS NULL` **或** `prompt_ref = 'genspec:' || generation_spec_id`（`generation_spec_id` 非空时） |
| 复合外键 | `(tenant_id, generation_spec_id) → generation_specs(tenant_id, generation_spec_id)`，`ON DELETE RESTRICT`（规格不可删） |
| 索引 | `ix_generation_tasks_spec`（`tenant_id, generation_spec_id`）供恢复与审计查询 |

**不新增响应字段**：`GenerationTask.to_dict()` 不加 `generation_spec_id`，与现有 `organization_id` 的处理一致（内部事实不进契约响应形状）。另按 §3.3，`prompt_ref` 也从响应移除（它同样能反推规格 id）。

### 3.2 为什么用 CHECK 而不是「服务层校验」

`prompt_ref` 与 `generation_spec_id` 是两个可以互相矛盾的列。只靠服务层，任何绕过服务（补偿脚本、运维手写 SQL、
未来的批量修复）都能造出「ref 指向 A、id 指向 B」的任务，而 Worker 拿 ref 还是 id 都能跑——**错误会静默**。
CHECK 让矛盾状态在写入时就不可表达。

### 3.3 `prompt_ref` 的对外暴露（评审补充 S-5）

「不返回 `generation_spec_id`」如果仍返回 `prompt_ref = genspec:gs1_…`，等于通过另一个字段泄露了同一个主键。
因此 P1 定：**`prompt_ref` 也是内部字段**。

| 面 | 现状 | P2 变更 |
|---|---|---|
| 请求体 | `prompt_ref` 必填（`RequestGenerationRequest`），客户端自己拼 `inline:` 正文 | 改为**可选且仅接受 `inline:`**（开发/联调）；生产请求不带该字段，由服务端派生 |
| 响应 | `task.to_dict()` 含 `prompt_ref`（契约对象把它列为必填） | **移除**。外部只拿 `generation_task_id` 查状态/结果；这属响应形状变更，走字段签名台账「removed」+ 冻结重签 |
| 内部 | 仓储/Worker 使用 | 不变（columns + resolver） |
| 审计/只读查询 | 无 | 将来提供独立的「按任务查冻结规格」只读能力（有权限），不复用普通任务响应 |

风险与取舍要写明：移除响应字段是**破坏性契约变更**。如果 P2 时判断外部已有消费方，替代方案是
保留字段但只对 `inline:` 任务返回值、对 `genspec:` 任务停止返回——两种方案都改变契约形状，都需要 Owner 在 P2 前确认；
本提案倾向「直接移除」，因为该字段从无外部业务用途，留着就是一个泄露口径。

---

## 4. 创建、冻结与解析

### 4.1 事务边界（任务与规格同一事务）

```
BEGIN
  SELECT ShotSpec FOR SHARE           -- 必须 LOCKED 且 shot_revision_id 一致
  计算 canonical / spec_sha256 / generation_spec_id
  INSERT generation_specs (... )       -- 唯一冲突 = 已存在同内容规格 ⇒ 复用（幂等）
  INSERT generation_tasks (... generation_spec_id, prompt_ref)
  INSERT outbox GENERATION_REQUESTED（沿用现有事件，不新增事件类型）
COMMIT
```

- 任一步失败 ⇒ 全部回滚，不出现「有规格没任务」或「有任务没规格」。
- `SHARE` 锁的作用：防止规格派生完成后 ShotSpec 被并发改成另一版本（那样 spec 指向的规格与任务引用的修订会不一致）。
- 规格唯一键：`(tenant_id, generation_spec_id)`。

#### 4.1.1 并发唯一冲突（评审补充 S-4）

两个请求同时算出同一 `spec_sha256` / 同一 id 时，一方 INSERT 成功、一方撞唯一约束。**不允许捕获异常后无条件复用**：

```
on UniqueViolation(tenant_id, generation_spec_id):
    重读该行
    if 行不存在                     -> 视为异常（约束说冲突但读不到），中止并报警
    if tenant_id 不一致             -> 中止（不跨租户复用）
    if 库内 spec_sha256 != 本次计算  -> 中止并报警（同 id 不同完整哈希 = 碰撞或篡改）
    if 规范化体逐字段不等            -> 中止并报警（哈希相同但内容不可解释）
    else                           -> 复用该规格，继续创建/复用任务
```

「哈希相同」不足以证明内容相同（哈希碰撞、字段集漂移都会让哈希相同而内容不同），所以最后一步做**逐字段比对**，
它只在冲突路径上发生，成本可接受。

#### 4.1.2 任务幂等语义（必须写死，不能靠默认）

| 问题 | 结论 |
|---|---|
| 同一规格能否产生多个任务 | **能**。规格是输入快照，重跑/多版本试排是正当需求；规格不持有任务计数 |
| 同一幂等请求是否必须复用同一任务 | **是**。以调用方提供的 `generation_task_id` 作为幂等键（现有字段），`(tenant_id, generation_task_id)` 唯一 |
| 同幂等键 + 同输入 | 返回既有任务（不新建、不重复发 outbox） |
| 同幂等键 + 不同输入 | **409 冲突**，不静默覆盖（幂等键代表「同一次请求」，输入变了就是另一次请求） |
| 不传 `generation_task_id` | 视为「明确要一个新任务」：服务端生成新 id（现状）；P2 在生产调用约定里要求必传，避免重试放大 |
| 事务回滚是否留孤立规格 | 同事务创建的一并回滚；冲突复用的是**已提交**的既有规格，不算孤立 |

### 4.2 解析优先级与一致性判定（`media_type` / `size` / `aspect_ratio`）

| 顺序 | 来源 | 现状 | P1 口径 |
|---|---|---|---|
| 1 | 任务级明确合法约束（请求里显式给定且通过契约校验） | 请求体当前无媒体字段 | 若将来引入，优先级最高，但**不得**与已锁 `ShotSpec` 的明确值冲突（冲突即拒绝，见下） |
| 2 | `constraint_snapshot_id` 指向的约束快照 | **对象不存在** | 只登记引用。对象立项后由其 `media_constraints` 接管；在此之前**跳过**，不假装解析成功 |
| 3 | `production_route_ref` 对应路线的显式媒体约束 | 当前 `ProductionRoute` 只有 `route_type` / `capability_ref` | 只有路线携带显式字段时才采用 |
| 4 | `ShotSpec.aspect_ratio` + 指定版本 `SizePolicy` | `ShotSpec` 有 `aspect_ratio`（DRAFT 可为空） | 确定性解析：按比例查该**版本**的策略表得到尺寸候选 |
| 5 | 无有效来源 | — | **拒绝创建**，不落默认值 |

**SizePolicy 的版本化 ≠ 不可变（评审补充 A-1）**

| 规则 | 内容 |
|---|---|
| 标识 | `size_policy_id` + `size_policy_version`（只写版本号会撞号：同名策略在不同域可能是不同表） |
| 冻结 | 被任一 `GenerationSpec` 引用过的版本**不得原地修改**；改规则 = 新 version |
| 入规格 | 实际解析出的 `size`、`aspect_ratio`、`media_type` 与 `size_policy_version` **一起**冻结进规格 |
| Worker 禁止 | 不得在执行时重新读取「当前最新 SizePolicy」再算尺寸；只读规格里已冻结的值 |
| 失败 | 版本不存在 / 比例非法 / 尺寸不可解析 ⇒ 创建即失败（`spec-size-policy-version-missing` / `spec-aspect-unresolved` / `spec-size-unresolved`） |

**冲突规则（明确拒绝，不静默覆盖）**

`ShotSpec` 若显式给出尺寸（当前没有该字段），与策略解析结果不一致 ⇒ 拒绝 `spec-size-conflict`，
由上游改规格或改策略版本，**不得**「按优先级取一个」了事；上游要换尺寸，只能产生新规格 + 新任务（规格不可改）。

一致性硬校验：

1. `ShotSpec` 必须 `LOCKED`（`locked_dimensions` 非空）且 `shot_revision_id` 与任务引用一致，否则创建拒绝 `spec-shot-spec-unlocked` / `spec-shot-spec-mismatch`；
2. `ShotSpec.aspect_ratio` 必须非空且可解析，否则 `spec-aspect-unresolved`；
3. `|size_w / size_h − ar_w / ar_h| ≤ 1%`（四舍五入到 4 位），否则 `spec-size-aspect-inconsistent`；
4. `media_type` 必须在 §1.1 枚举内，且 `extension_for_media_type` 能映射，否则 `spec-media-type-unsupported`。

**任何一步失败都不落默认值**。原来的「全局默认 `image/png`」只保留给 `inline:` 的本地联调路径，并在证据里显式标注「联调口径」。

### 4.3 `constraint_snapshot_id` 的预留口径（必须写死，避免臆造对象）

- P1 **不**定义 `ConstraintSnapshot` 对象，也**不**为它建表；它继续是一个 ≤128 字符的引用；
- 规格里登记它，仅用于「将来可追溯」与「解析顺序占位」；
- 如果 P2 落地时该对象仍不存在，解析必须**显式跳过**这一层（不报错、不伪造默认），并在规格里保留该引用；
- 一旦约束域立项并定义 `media_constraints`，只允许新增一条解析规则 + 一次 ADR 修订，不改已有规格（旧规格按当时的解析结果冻结）。

### 4.4 正式任务与预览的双通道（评审补充 A-2）

正式 `GenerationTask` 要求 `ShotSpec` 已 `LOCKED`：生成任务是持久化执行，输入必须在受理时已定稿，
否则「创建时的内容」与「复核时看到的内容」会不一致。但「先试一版再锁」是真实创作需求，不能靠给正式接口
加状态例外来满足。

| 通道 | 输入状态 | 产物语义 | 现状 |
|---|---|---|---|
| A 正式生成 | `ShotSpec` = `LOCKED` | 持久化任务；走既有状态机 / T1–T3 / 预算 / 资产登记 | 本提案 P2 实现 |
| B 预览试生成 | `ShotSpec` = `DRAFT` | 仅供人工评估，不可作为交付、不进资产登记、不占预算终值 | **本提案不实现** |

底线（写成硬约束）：

- Preview **不得**借用正式 `GenerationTask` 的状态机、幂等键或生产语义；
- 现阶段没有独立 Preview 能力 ⇒ **不支持 DRAFT 试生成**，`spec-shot-spec-unlocked` 直接拒绝；
- 将来若做 Preview，必须是独立对象/流程（独立契约提案），不得在 `GENERATION.REQUEST` 上加「DRAFT 例外」开关。

---

## 5. 不可变性与防篡改（三层，不靠约定）

| 层 | 保护 | 能防什么 / 不能防什么 |
|---|---|---|
| 数据库权限 | 应用角色对 `generation_specs` 仅 `INSERT` / `SELECT`；无 `UPDATE` / `DELETE` / `TRUNCATE` | 防应用侧误写；不防表所有者 |
| 数据库约束 | 复合唯一键 `(tenant_id, generation_spec_id)`；任务侧 CHECK 等式；复合外键 `ON DELETE RESTRICT` | 防「正常路径」制造矛盾或删除；不防 `ALTER TABLE` |
| 执行验证 | Worker 重算 `spec_sha256` 与 id 并逐字段核对（§2.2） | 防读取时静默漂移；不防读取前的物理改写 |

| 保证 | 实现 | 失败表现 |
|---|---|---|
| 不可更新 | INSERT-only 权限 + 无正常更新路径；契约登记 `RUNTIME_ENFORCED` 不变量 | 任何 UPDATE 尝试被数据库拒绝 |
| 不可替换 | id 由内容派生（§2.1）；内容变则 id 变 | 想替换只能新建 id，任务侧引用不变 ⇒ 换不了 |
| 不可删除 | 复合外键 `ON DELETE RESTRICT` + 角色无 DELETE 权限 | 删除被拒 |
| 跨租户不可见 | `tenant_id` + RLS（沿用 `set_tenant_context` 口径）+ 复合外键「引用得上的一定同租户」 | 跨租户 SELECT 为空；跨租户引用被 FK 拒绝 |
| 内容未改 | Worker 重算 `spec_sha256` + id 比对 | `spec-tampered` |

**可信管理边界（必须说清，不许含糊）**

- 表所有者、迁移角色、超级用户**仍能改这张表**。任何触发器/约束都只能防「正常数据库路径的误改」，
  **不得**宣称能抵御超级用户——那是安全剧场。
- 超级用户与迁移所有者属于**可信管理边界**：由变更记录、审计日志、最小权限与双人复核治理，
  不靠本对象的结构性约束。
- `genspec:<id>` 不带签名，因此它证明的是「正常写入口没有替换过、读取时没有漂移」，
  不是「数据库行在物理上不可能被改」。

---

## 6. 历史任务、失败与迁移

### 6.1 兼容

| 场景 | 处理 |
|---|---|
| 历史 `inline:` 任务 | 完全保留：`generation_spec_id` 为 NULL，CHECK 放行，执行路径不变 |
| 在途 `REQUESTED` / `RUNNING` / `AWAITING` 任务 | 迁移不回填、不改写；它们继续走原解析路径 |
| 新任务（生产） | 一律 `genspec:`；`inline:` 仅当 `GENERATION_INLINE_PROMPT_ENABLED=true` |
| 迁移失败 | 迁移只做 `ADD COLUMN` + `ADD CONSTRAINT`；约束对历史行天然成立（`prompt_ref` 非 inline 的历史任务目前不存在，若存在则按 §6.2 处理） |

### 6.2 迁移策略（P2 执行，P1 只定顺序）

1. 建 `generation_specs` 表 + RLS + 角色权限（INSERT-only）；
2. `generation_tasks` 加 `generation_spec_id`（nullable）；
3. 加 CHECK 与复合外键；
4. 校验：对全表扫一次 `prompt_ref LIKE 'genspec:%'` 的行数必须为 0（此刻还没有新形态任务）；
5. 应用侧切换：任务创建改为同事务派生；Worker 支持 `genspec:`；
6. 回滚路径：删 CHECK → 删列 → 删表（若无 `genspec:` 引用，回滚无数据损失；已有引用时先停写入再回滚）。

### 6.3 失败码（P2 统一登记，均为任务 FAILED，不改状态机）

| 码 | 触发 | 可重试 |
|---|---|---|
| `PROMPT_INLINE_FORBIDDEN` | 生产环境收到 `inline:`（创建阶段拒绝） | 否（改请求） |
| `spec-unresolvable` | 引用形态非法 / 规格不存在 | 否 |
| `spec-missing` | 按 id 查不到 | 否（数据不一致，需人工） |
| `spec-foreign-tenant` | 规格租户与任务不一致 | 否（安全事件） |
| `spec-tampered` | 重算哈希不符 | 否（安全事件） |
| `spec-mismatch` | `prompt_ref` 与 `generation_spec_id` 不等 | 否（绕过写入口） |
| `spec-schema-version-unsupported` | `schema_version` 不在 Worker 支持集合 | 否（要升级 Worker） |
| `spec-shot-spec-unlocked` / `spec-shot-spec-mismatch` | 创建阶段 ShotSpec 不满足 §4.2（DRAFT 试生成走 §4.4 预览通道，不在正式接口） | 否（先锁定规格） |
| `spec-aspect-unresolved` | `aspect_ratio` 空或不可解析 | 否 |
| `spec-size-unresolved` | 指定版本策略里没有可用尺寸 | 否 |
| `spec-size-policy-version-missing` | `size_policy_version` 不存在 | 否（策略发布问题） |
| `spec-size-conflict` | `ShotSpec` 明示尺寸与策略解析冲突 | 否（上游改规格或改版本） |
| `spec-size-aspect-inconsistent` | 尺寸与比例差 > 1% | 否 |
| `spec-media-type-unsupported` | 类型不在枚举 | 否 |

「可重试=否」是刻意的：这些都是**输入或数据问题**，自动重试只会重复失败并消耗配额。

### 6.4 规格版本兼容（评审补充 S-6 的延伸）

- `schema_version` 是规格结构版本，**不做原地升级**：旧规格永远是旧结构，可执行前提是 Worker 支持该版本；
- Worker 维护「支持版本集合」，不在集合内 ⇒ `spec-schema-version-unsupported`（不猜测、不降级）；
- 需要新结构时：新增版本号 + 新派生规则；旧规格保持只读可追溯，**不迁移**；
- 要重生成只能「新规格 + 新任务」（输入或版本变化本就应产生新身份）；
- `derivation_version` / `template_version` / `size_policy_version` 变化同理：不回溯改写历史规格，只影响新规格。

---

## 7. 契约变更清单（P2 才执行，P1 不动）

| 文件 | 变更 | 闸门影响 |
|---|---|---|
| `contracts/schemas/object_schemas.schema.json` | 新增 `GenerationSpec`（1 个对象，99→100）；`GenerationTask`：移除响应侧 `prompt_ref`（请求侧保留为可选 `inline:` 入口） | 对象总数、字段签名、`$ref`、引用目标可解析 |
| `contracts/runtime/schema_field_signatures.yaml` | 登记新对象字段签名；`GenerationTask.prompt_ref` 记一条 `removed`（含理由与替代查询方式） | 字段级契约演进 |
| `contracts/state_machines/state_machines.yaml` | 登记 `GenerationSpec` 为**占位对象**（无状态机） | 状态枚举 / 占位计数 |
| `contracts/openapi/*` | **不改**（无新动作、无新端点；规格是任务创建内部步骤） | Action Registry 不变 |
| `contracts/phase0/*` | 不改 | — |
| `data/migrations/versions/*` | 新增一个 revision：建表 + 列 + CHECK + FK + 索引 + 角色权限 | 迁移链单 head / 往返深度 |
| 冻结清单 `V205-R15` | **必须重签**：对象 schema 演进（含响应字段移除）属于冻结产物变化 | freeze_manifest |

> `prompt_ref` 的响应移除是**破坏性变更**。P2 开工前必须由 Owner 确认「无外部消费方」；
> 若存在消费方，则改为「保留字段、只对 `inline:` 任务返回」，并在同一次评审里定。

## 8. 测试与验收（P2 的 Definition of Done）

| 类别 | 用例 |
|---|---|
| 契约 | `contract_check` 全 PASS；对象/字段签名/占位登记三处一致；`prompt_ref` 移除有台账记录 |
| 正向 | 创建任务 → 规格与任务同事务可见；`prompt_ref = genspec:<id>`；重发同幂等键 → 返回同一任务 |
| 负向（结构） | 直接 UPDATE 规格 → 拒；INSERT 同 id 不同内容 → 唯一冲突且报警；`prompt_ref` 与 id 不等 → CHECK 拒；跨租户 SELECT → 空 |
| 负向（Worker） | 篡改规格行 → `spec-tampered`；规格不存在 → `spec-missing`；租户不符 → `spec-foreign-tenant`；裸 id → `prompt-unresolvable`；未知 `schema_version` → `spec-schema-version-unsupported`；生产环境 `inline:` → 创建拒绝 |
| 规范化/哈希 | `\r\n` 与 `\n` 同哈希；NFC 等价输入同哈希；键序无关；整数 vs 字符串小数不被混同；`""` 与缺省 `negative_prompt` 行为不同；跨租户同内容 ⇒ **不同 id** |
| 碰撞处理 | 构造前缀相同、完整哈希不同的行 → 拒绝写入并报警，不覆盖 |
| 派生一致性 | 同输入同版本 → 同 `spec_sha256`；`size` 与 `aspect_ratio` 差 >1% → 拒绝；`ShotSpec` 未 LOCK → 拒绝；`SizePolicy` 版本被引用后再改 → 新版本才可发布 |
| 并发/幂等 | 两请求同规格并发 → 一方复用、逐字段比对通过；同幂等键不同输入 → 409；不传幂等键 → 新任务 |
| 迁移 | `upgrade head` → `downgrade -N` → `upgrade head` 往返；独立库（CI 专用库）上跑 |
| 历史 | 存在 `inline:` 任务时迁移成功、执行路径不变 |

## 9. 本提案不做的事（边界）

1. 不建 `ConstraintSnapshot` 对象、不建 Prompt 库；
2. 不改 R42 的 T1/T2/T3 语义、不改状态机、不改事件类型；
3. 不新增 action、不改 OpenAPI；
4. 不在 Worker 里做「按需派生」：规格只由任务创建产生，执行阶段只读取；
5. 不承诺「防有权限的人改库」——见 §5 末段。

## 10. Owner 评审结论（2026-11-29）

| 编号 | 议题 | 结论 |
|---|---|---|
| A-1 | 尺寸解析用 `ShotSpec.aspect_ratio` + 版本化 `SizePolicy` | **批准（附条件）**：`policy_id` + `policy_version`、被引用版本不得原地改、结果与版本一起冻结、Worker 不重算、冲突拒绝不覆盖 → §4.2 |
| A-2 | `ShotSpec` 必须 `LOCKED` 才允许正式任务 | **批准（双通道）**：正式任务仅 LOCKED；DRAFT 走独立 Preview 语义，不得借用正式状态机/幂等键；现阶段不支持 DRAFT 试生成 → §4.4 |
| A-3 | `generation_spec_id` 不进入普通响应 | **批准**：内部保留完整关联链；将来只读审计走独立能力 → §1.4、§3.3 |

追加六项已固化进本提案：S-1 哈希域与规范化（§2.1）／S-2 三层不可变与可信管理边界（§5）／S-3 规格与
`ExecutionRecord` 分离（§1.4）／S-4 并发冲突与任务幂等（§4.1.1–4.1.2）／S-5 `prompt_ref` 外部暴露（§3.3）／
S-6 严格解析与 fail-closed（§2.3、§6.3–6.4）。

结论记录：`docs/audits/2026-11-29-adr-r47-p1-review-decision.md`。

## 11. 仍未决、需要 Owner 在 P2 开工前确认的两件事

1. **`prompt_ref` 响应移除 vs 保留只对 `inline:` 返回**：两者都是破坏性/半破坏性变更，提案倾向移除（§3.3）。
2. **ADR §3 的 `prompt_sha256` 独立列**：本提案把正文并入 `spec_sha256`；若要独立列，P2 加派生列（不入身份域）。

确认后：进入 P2（契约变更 + 迁移 + 实现 + 冻结重签），再走 P3（真实网关 E5）。
