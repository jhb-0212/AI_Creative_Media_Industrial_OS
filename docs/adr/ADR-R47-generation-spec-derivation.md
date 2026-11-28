# ADR-R47：生成规格派生与冻结（Prompt 由 ShotSpec 派生，不作为存储事实）

- 状态：方向 DECIDED（项目所有者 2026-11-28 会话指令「按长期最优的来」；本条由实现方转录，非手写签名）。**实现分批落地**：契约演进须走 `contracts/proposals/` 提案，本 ADR 不改任何契约、不新增迁移。
- 日期：2026-11-28
- 触发：真实网关接入前的两处建模缺口——`prompt_ref` 只接受 `inline:<正文>`（≤200 字符），真实生产提示词进不来；产物 `media_type` / `size` 由全局配置默认值决定，隐含「一律 png」的假设
- 关联：V2.0.5 8.1 / 8.12（提示词与相机参数是 ShotSpec 的派生输出）；`contracts/schemas/object_schemas.schema.json` 中 Shot / ShotRevision 的 `x-afs-exclusions`（prompt 不得写入修订，SR-6）；ADR-R42；ADR-R44（长流程引擎边界）；ADR-R46；`docs/audits/2026-11-28-durable-execution-boundary-review.md`

---

## 1. 裁定

| 编号 | 议题 | 裁定 |
|---|---|---|
| D1 | 提示词是不是存储事实 | **不是**。不得在 Shot / ShotRevision 增加 `prompt` 字段（违反基线 8.1 与契约 SR-6，会让提示词变成第二份事实源）；也不得把提示词正文写进 `generation_tasks`（同样制造可漂移的第二份正文）。 |
| D2 | 怎么把正文交给 Worker | **派生一次、冻结引用**。任务创建时从 ShotRevision（内容）+ ShotSpec（结构化规格）+ 版本化模板派生一份**生成规格**（GenerationSpec），内容不可变；`generation_tasks.prompt_ref` 收敛为 `genspec:<generation_spec_id>`（≤200 字符，够用）。Worker 的解析器只承认 `inline:` 与 `genspec:` 两种形态，其余一律 fail-closed——保持「绝不把 id 当正文发给网关」这条既有结论。 |
| D3 | 产物类型与尺寸由谁决定 | **由生成规格携带**（`media_type` / `size` / `aspect_ratio`），来源是 `production_route_ref` + `constraint_snapshot_id` + `ShotSpec.aspect_ratio` 的解析结果。Worker 不再用全局默认值决定产物形态。解析失败在**任务创建时**拒绝（fail-closed），不允许「生成了 png 才发现要 mp4」。 |
| D4 | 派生规则的可复现 | 模板与模型参数带版本号，并写入 GenerationSpec；改模板只影响新规格，旧规格不受影响。 |
| D5 | 明确不做 | 不建「提示词库」作为独立事实源（那会把派生输出库化成第二权威，重犯 D1 的错误）；不在 executor 里对「看起来像 id」的输入做任何兜底猜测。 |

### 1.0 状态口径（避免误读为「已实现」）

本 ADR 只定方向。**在 P2 合并之前，代码行为不变**：Worker 仍只认 `inline:<正文>`，产物形态仍取配置默认值，失败码仍是 `prompt-unresolvable`。若 Owner 在 P1 提案评审时驳回，本 ADR 不影响任何现有实现，届时以新 ADR 取代；若通过，则以「提案 + 迁移 + 冻结重签」推进，不在本 ADR 里预支实现。

### 1.1 顺序（为什么 D2 与 D3 必须一起做）

只做 D2，Worker 拿到正文但产物形态仍是常量；只做 D3，尺寸随路线变了但正文还是 `inline:` 片段。两者都指向同一个「任务创建时一次性冻结输入规格」的动作，拆成两个 PR 会让中间态多一次契约往返。

---

## 2. 现状硬事实（判据）

| 事实 | 依据 |
|---|---|
| `prompt_ref` 上限 200 字符，仅作为引用 | `services/production_planning/domain/generation_task.py:96` |
| 执行器只认 `inline:<正文>`，否则 `prompt-unresolvable` 失败 | `workers/generation/executor.py:77-92`、`216-219` |
| 仓库内无提示词派生/构建实现（`git grep -in prompt` 仅命中 API/存储/解析器） | 2026-11-28 全仓检索 |
| `media_type` / `size` 来自构造参数（配置默认 `image/png`、空尺寸） | `workers/generation/executor.py:190-203`、`workers/generation/__main__.py` |
| `ShotSpec` 有 `aspect_ratio` / `camera_path` / `subject_paths` / `shot_size_changes`，无 `prompt` 字段 | `services/project_planning/domain/shot_spec.py` |
| `constraint_snapshot_id` 只是 ≤128 字符的引用，**没有对应的快照对象** | `git grep constraint_snapshot`：只出现在任务/门禁字段与契约签名，无实体 |

最后一行是必须说清的边界：D3 里的「约束快照」目前没有可解析对象。**本 ADR 不为它新造一个域**——那会与 PR-10（Production Graph）/PR-12（约束域）的立项冲突，并留下一个将来必然漂移的形态。P1 只登记「解析顺序里的这一环」与「缺失即拒绝」，把对象建模留给约束域立项时一并裁。

---

## 3. GenerationSpec 的形态（提案输入，不是已签契约）

| 要素 | 取值 | 说明 |
|---|---|---|
| 标识 | `generation_spec_id`（≤64） | 与任务同事务创建；不可复用 |
| 输入引用 | `shot_revision_id`、`shot_spec_id`、`production_route_ref`、`constraint_snapshot_id`、`template_version` | 全部是精确版本引用，不是「当前最新」 |
| 派生输出 | `prompt_text`、`negative_prompt`、`media_type`、`size`、`aspect_ratio`、`model_params` | 冻结后只读；正文存哪里由 P1 决定（候选：受控存储 + 文本定位符，或独立的不可变正文表），**不得**写回 Shot/ShotRevision |
| 完整性 | `prompt_sha256`、`spec_sha256` | 与网关证据里的摘要同口径，便于把「发出去的正文」与「取证记录」对上 |
| 状态 | 出生即冻结（无生命周期状态机） | 重派生 = 新 spec + 新任务，不原地改 |

`generation_tasks.prompt_ref = "genspec:<generation_spec_id>"` 之后，Worker 的职责变成：解析引用 → 读 spec → 取正文/类型/尺寸 → 调网关。解析不到一律失败，且失败原因可区分（引用无效 / spec 缺失 / spec 与任务不匹配）。

---

## 4. 分批落地

| 批次 | 内容 | 前置 |
|---|---|---|
| P1 | 契约提案：GenerationSpec schema、`prompt_ref` 允许 `genspec:` 前缀、`media_type/size` 的来源字段、完整性字段 | 本 ADR；Owner 对契约演进的批准（提案评审） |
| P2 | 迁移与实现：表/约束、任务创建时派生、Worker 解析器认 `genspec:` 并从 spec 取类型/尺寸、fail-closed 用例 | P1 通过 + 冻结重签 |
| P3 | E5：真实网关下 A1 复跑，产物类型/尺寸来自 spec（不再有常量默认） | P2 合并 + 真实凭据 |

---

## 5. 不变式

1. Shot / ShotRevision 永远不含 `prompt`（SR-6 不变）。
2. `generation_tasks` 永远不存提示词正文；只存引用。
3. Worker 对无法解析的 `prompt_ref` 必须失败，不得回退到「把 ref 当正文」。
4. 产物形态由 spec 决定；全局默认值只允许出现在 `inline:` 的本地联调路径，并在证据里显式标注为「联调口径」。
5. 派生必须可复现：同一组输入引用 + 同一 `template_version` 必须得到同一 `spec_sha256`。
