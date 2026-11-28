---
doc_type: proposal
status: proposed
updated_at: 2026-11-29
authoritative_sources:
  - docs/adr/ADR-R47-generation-spec-derivation.md
  - workers/generation/executor.py
  - services/production_planning/domain/generation_task.py
  - services/project_planning/domain/shot_spec.py
  - packages/shared/storage_locator.py
  - contracts/schemas/object_schemas.schema.json
open_items: []
---

# GenerationSpec 契约提案（ADR-R47 P1）

状态：以文首 front-matter 为准（`status: proposed`）。**本文件是提案，不是契约变更**：
不新增对象 schema、不改字段签名、不建迁移、不改代码、不重签冻结清单。P1 获批后，P2 才按本提案落地
（提案评审 → 契约变更 → 迁移 → 实现 → 冻结重签）。

关联：ADR-R47（生成规格派生与冻结，方向 DECIDED）、ADR-R42（T2/T3 回执；本提案不改其语义）、
ADR-R44（长流程引擎边界；本提案不涉及执行引擎）、基线 8.1 / 8.12、契约 SR-6（ShotRevision 不含 prompt）。

---

## 0. 一页结论

| 问题 | 提案结论 |
|---|---|
| 提示词放哪 | 放 `GenerationSpec`（不可变规格行），**不**进 Shot / ShotRevision，**不**进 `generation_tasks`，**不**建提示词库 |
| 怎么引用 | `generation_tasks.prompt_ref = "genspec:<generation_spec_id>"`；`generation_spec_id` 由规格内容确定性派生 |
| 引用凭什么证明不可变 | 不是「前缀可信」，而是三条结构性保证：确定性 id（内容变则 id 变）+ 任务侧 CHECK 等式 + Worker 重算 `spec_sha256` 并与 id 比对 |
| 类型/尺寸谁说了算 | 规格携带 `media_type` / `size` / `aspect_ratio`；创建时解析、解析失败即拒绝；Worker 不重新解释 |
| 跨租户 | `generation_specs` 按 `tenant_id` 做 RLS；(tenant_id, generation_spec_id) 复合外键；跨租户读不到也引用不上 |
| 老任务 | `inline:` 保留；`generation_spec_id` 可空；新增 CHECK 只约束新形态，不回填历史 |
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
| `prompt_text` | string | 是 | 1–8192 字符，非纯空白 | 提示词正文（派生结果，冻结） |
| `negative_prompt` | string | 否 | ≤4096 字符 | 为空表示「不传负向提示词」，不是「空字符串也算一个值」 |
| `media_type` | string | 是 | 枚举：`image/png` / `image/jpeg` / `image/webp` / `video/mp4` / `video/webm` / `audio/wav` | 必须能被 `extension_for_media_type` 映射；`application/octet-stream` **不允许**（它会掩盖「不知道产物是什么」） |
| `size` | string | 是 | `^[1-9][0-9]{2,4}x[1-9][0-9]{2,4}$` | 像素尺寸；不是比例，比例是 `aspect_ratio` |
| `aspect_ratio` | string | 是 | `^[1-9][0-9]{0,2}:[1-9][0-9]{0,2}$` | 与 `size` 必须一致（§4.2） |
| `spec_sha256` | string | 是 | 64 位小写十六进制 | 对规范化字段集计算（§2.2） |

### 1.2 不放进字段的东西（刻意的）

| 不放进 | 理由 |
|---|---|
| `prompt_ref` / `generation_task_id` | 规格不知道「谁引用我」。写进去就多了一条可漂移的反向引用；引用由任务侧单向持有 |
| `model_adapter_ref` | 已在 `generation_tasks` 上；规格再存一份就是两个权威 |
| `locked_dimensions` / 镜头内容 | 属 ShotSpec / ShotRevision，按 SR-6 保持单一事实源 |
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

---

## 2. 引用与完整性：`genspec:<id>` 凭什么可信

### 2.1 确定性 id（先解决「id 只是形式」的问题）

```
canonical = JSON(规范化字段集, sort_keys, 无空白, UTF-8)
spec_sha256 = sha256(canonical)
generation_spec_id = "gs1_" + base32(spec_sha256)[:26]      # 30 字符，≤64
```

- **同输入 + 同 template_version ⇒ 同 spec_sha256 ⇒ 同 id**：重试幂等，不会因为重发多出一份「看起来一样」的规格。
- **内容变 ⇒ 哈希变 ⇒ id 变**：想「替换规格」只能造一个新 id，旧 id 仍指向旧内容。这是结构性的，不依赖纪律。
- id 不含租户前缀：租户隔离由 `tenant_id` 列 + RLS 保证，不靠 id 编码（编码进 id 会让跨租户判断变成字符串前缀判断，反而容易被伪造）。

规范化字段集**必须包含**：project_id / shot_revision_id / shot_spec_id / production_route_ref /
constraint_snapshot_id / template_version / prompt_text / negative_prompt / media_type / size / aspect_ratio。
**不包含**：generation_spec_id 自身、tenant_id、entity_version、created_at 等信封字段——否则 id 无法自洽。

### 2.2 Worker 的三步校验（缺一即失败）

| 步 | 校验 | 失败码 |
|---|---|---|
| 1 | `task.prompt_ref` 必须等于 `"genspec:" + task.generation_spec_id`（DB CHECK 已保证，Worker 再验一次以防绕过写入口） | `spec-mismatch` |
| 2 | 按 id 取规格；不存在 → 失败；`spec.tenant_id` 与任务租户不一致 → 失败（RLS 之外的第二道） | `spec-missing` / `spec-foreign-tenant` |
| 3 | 从**库内字段**重算 `spec_sha256` 与 id：哈希不符 → 失败（行被改过）；哈希与 id 不符 → 失败（id 被拼错或伪造） | `spec-tampered` |

三条合起来回答「引用形式不自动证明不可变」：**不可变由 id 派生 + DB 约束 + 读取时重算共同证明**，不靠 `genspec:` 这个前缀。

### 2.3 `prompt_ref` 两种形态的约束

| 形态 | 允许条件 | 解析路径 |
|---|---|---|
| `inline:<正文>` | 仅当部署配置 `GENERATION_INLINE_PROMPT_ENABLED=true`（默认 false；开发/联调用），且正文 1–200 字符 | 现 `resolve_inline_prompt`（行为不变） |
| `genspec:<id>` | 生产唯一形态；`id` 必须同任务 `generation_spec_id` | P2 新增 resolver |

- 未知前缀（例如裸 id、`asset:`、`prompt:`）→ `prompt-unresolvable`，**不得回退**为「当正文用」。
- 生产环境（`GENERATION_INLINE_PROMPT_ENABLED=false`）收到 `inline:` → 创建阶段拒绝（`PROMPT_INLINE_FORBIDDEN`），而不是等到 Worker 才失败——早失败省一次排队。

---

## 3. 任务侧的结构性绑定（不新增响应字段）

### 3.1 新增列与约束

| 项 | 内容 |
|---|---|
| 新列 | `generation_tasks.generation_spec_id`：string(64)，**可空** |
| CHECK（形态互斥） | `prompt_ref LIKE 'inline:%' AND generation_spec_id IS NULL` **或** `prompt_ref = 'genspec:' || generation_spec_id`（`generation_spec_id` 非空时） |
| 复合外键 | `(tenant_id, generation_spec_id) → generation_specs(tenant_id, generation_spec_id)`，`ON DELETE RESTRICT`（规格不可删） |
| 索引 | `ix_generation_tasks_spec`（`tenant_id, generation_spec_id`）供恢复与审计查询 |

**不新增响应字段**：`GenerationTask.to_dict()` 不加 `generation_spec_id`，与现有 `organization_id` 的处理一致（内部事实不进契约响应形状）。契约只改 `prompt_ref` 的 `pattern` / description。

### 3.2 为什么用 CHECK 而不是「服务层校验」

`prompt_ref` 与 `generation_spec_id` 是两个可以互相矛盾的列。只靠服务层，任何绕过服务（补偿脚本、运维手写 SQL、
未来的批量修复）都能造出「ref 指向 A、id 指向 B」的任务，而 Worker 拿 ref 还是 id 都能跑——**错误会静默**。
CHECK 让矛盾状态在写入时就不可表达。

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
- 规格唯一键：`(tenant_id, generation_spec_id)`；并发同内容插入的一方会撞唯一约束，按「已存在则复用」处理（幂等，不吞错）。

### 4.2 解析优先级与一致性判定（`media_type` / `size` / `aspect_ratio`）

| 顺序 | 来源 | 现状 | P1 口径 |
|---|---|---|---|
| 1 | `constraint_snapshot_id` 指向的约束快照 | **对象不存在** | 只登记引用。对象立项后由其 `media_constraints` 接管；在此之前**跳过**，不假装解析成功 |
| 2 | `production_route_ref` 对应路线的显式媒体约束 | 当前 `ProductionRoute` 只有 `route_type` / capability_ref，无媒体字段 | 只有路线携带显式字段时才采用；没有就落到第 3 步 |
| 3 | `ShotSpec.aspect_ratio` + 版本化 `SizePolicy` | `ShotSpec` 有 `aspect_ratio`（DRAFT 可为空） | 按比例选尺寸；`SizePolicy` 是**显式表**（比例 → 尺寸候选），版本纳入 `template_version` |

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

---

## 5. 不可变性与防篡改（结构性，不靠约定）

| 保证 | 实现 | 失败表现 |
|---|---|---|
| 不可更新 | `generation_specs` 只允许 INSERT；应用角色无 UPDATE/DELETE 权限（P2 用既有角色模型：注册专用角色或复用既有服务角色但收回写权限）；契约登记 `RUNTIME_ENFORCED` 不变量 | 任何 UPDATE 尝试被数据库拒绝 |
| 不可替换 | id 由内容派生（§2.1）；同 id 内容必然相同；不同内容必然不同 id | 想替换只能新建 id，任务侧引用不变 ⇒ 换不了 |
| 不可删除 | 任务复合外键 `ON DELETE RESTRICT`；规格行无删除授权 | 删除被拒 |
| 跨租户不可见 | `tenant_id` + RLS（沿用 `set_tenant_context` 口径）；复合外键再保证「引用得上的一定同租户」 | 跨租户 SELECT 返回空；跨租户引用被 FK 拒绝 |
| 内容未改 | Worker 重算 `spec_sha256` + id 比对（§2.2） | 行被改 ⇒ 哈希不符 ⇒ `spec-tampered` |

**明确不承诺的事**：`genspec:<id>` 本身不带签名，也不防「有写权限的人篡改数据库行」。它防的是
「通过正常写入口替换/伪造引用」与「读取时静默漂移」；数据库行的物理防篡改由备份、审计与权限治理承担（与本提案无关）。

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
| `spec-shot-spec-unlocked` / `spec-shot-spec-mismatch` | 创建阶段 ShotSpec 不满足 §4.2 | 否（先锁定规格） |
| `spec-aspect-unresolved` | `aspect_ratio` 空或不可解析 | 否 |
| `spec-size-aspect-inconsistent` | 尺寸与比例差 > 1% | 否 |
| `spec-media-type-unsupported` | 类型不在枚举 | 否 |

「可重试=否」是刻意的：这些都是**输入或数据问题**，自动重试只会重复失败并消耗配额。

---

## 7. 契约变更清单（P2 才执行，P1 不动）

| 文件 | 变更 | 闸门影响 |
|---|---|---|
| `contracts/schemas/object_schemas.schema.json` | 新增 `GenerationSpec`（1 个对象，99→100）；`GenerationTask.prompt_ref` 加 `pattern` 与说明；`generation_spec_id` 不进入响应 schema | 对象总数、字段签名、`$ref`、引用目标可解析 |
| `contracts/runtime/schema_field_signatures.yaml` | 登记新对象字段签名 | 字段级契约演进 |
| `contracts/state_machines/state_machines.yaml` | 登记 `GenerationSpec` 为**占位对象**（无状态机） | 状态枚举 / 占位计数 |
| `contracts/openapi/*` | **不改**（无新动作、无新端点；规格是任务创建内部步骤） | Action Registry 不变 |
| `contracts/phase0/*` | 不改 | — |
| `data/migrations/versions/*` | 新增一个 revision：建表 + 列 + CHECK + FK + 索引 + 角色权限 | 迁移链单 head / 往返深度 |
| 冻结清单 `V205-R15` | **必须重签**：对象 schema 演进属于冻结产物变化 | freeze_manifest |

## 8. 测试与验收（P2 的 Definition of Done）

| 类别 | 用例 |
|---|---|
| 契约 | `contract_check` 全 PASS；对象/字段签名/占位登记三处一致 |
| 正向 | 创建任务 → 规格与任务同事务可见；`prompt_ref = genspec:<id>`；重发同请求 → 复用同 id（幂等） |
| 负向（结构） | 直接 UPDATE 规格 → 拒；INSERT 同 id 不同内容 → 唯一冲突；`prompt_ref` 与 id 不等 → CHECK 拒；跨租户 SELECT → 空 |
| 负向（Worker） | 篡改规格行 → `spec-tampered`；规格不存在 → `spec-missing`；租户不符 → `spec-foreign-tenant`；裸 id → `prompt-unresolvable`；生产环境 `inline:` → 创建拒绝 |
| 派生一致性 | 同输入同模板 → 同 `spec_sha256`；`size` 与 `aspect_ratio` 差 >1% → 拒绝；`ShotSpec` 未 LOCK → 拒绝 |
| 迁移 | `upgrade head` → `downgrade -N` → `upgrade head` 往返；独立库（CI 专用库）上跑 |
| 历史 | 存在 `inline:` 任务时迁移成功、执行路径不变 |

## 9. 本提案不做的事（边界）

1. 不建 `ConstraintSnapshot` 对象、不建 Prompt 库；
2. 不改 R42 的 T1/T2/T3 语义、不改状态机、不改事件类型；
3. 不新增 action、不改 OpenAPI；
4. 不在 Worker 里做「按需派生」：规格只由任务创建产生，执行阶段只读取；
5. 不承诺「防有权限的人改库」——见 §5 末段。

## 10. 需要 Owner 批准的三个点（P1 评审即这三条）

1. **尺寸解析的第 3 层**：接受「`ShotSpec.aspect_ratio` + 版本化 `SizePolicy` 表」作为约束快照缺位时的权威来源（即：允许 P2 建这张显式的比例→尺寸表，而不是把尺寸写死成常量）。
2. **`ShotSpec` 必须 LOCKED** 才允许创建生成任务：这会挡住「先试一版再锁」的早期流程，但能保证规格与引用一致。
3. **`generation_spec_id` 不进入 `GenerationTask` 响应**：与现有 `organization_id` 的处理一致（内部事实不进契约形状）；如果要求对外可见，需要在同一提案里加响应字段并重走字段签名。

批准后：进入 P2（契约变更 + 迁移 + 实现），再走 P3（真实网关 E5）。
