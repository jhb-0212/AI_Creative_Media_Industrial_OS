# ADR-R47 P1 评审结论（2026-11-29）

> 记录者：实现方转录（Owner 2026-11-29 书面评审意见的整理）。**这不是新 ADR，也不改变 ADR-R47 的方向裁决**；
> 它只把「P1 提案评审」的结论、附加约束与边界固定下来，供 P2 授权时引用。
>
> 提案基线：`19c1ff9`（PR #78 合并后的 `docs/proposals/generation-spec-contract-p1.md`，`status: proposed`）。

## 1. 结论摘要

| 编号 | 裁决项 | 结论 | 附加条件 |
|---|---|---|---|
| A-1 | 尺寸解析第 3 层：`ShotSpec.aspect_ratio` + 版本化 `SizePolicy` | **批准** | `SizePolicy` 必须有 `policy_id` + `policy_version`；被引用版本不得原地修改；解析结果与版本一起冻结进规格；Worker 不得重算；冲突不得静默覆盖，必须拒绝或由上游改规格 |
| A-2 | `ShotSpec` 必须 `LOCKED` 才允许正式生成任务 | **批准，双通道** | 正式任务仅 `LOCKED`；`DRAFT` 走独立的 Preview 语义，**不得**借用正式任务状态机 / 幂等键 / 生产语义；现阶段无 Preview 能力就不支持 DRAFT 试生成，不在正式接口加状态例外 |
| A-3 | `generation_spec_id` 不进入普通任务响应 | **批准** | 内部保留完整关联链（task → prompt_ref → spec → spec_sha256 / schema_version / prompt / 类型 / 尺寸 / 比例 / policy_version）；将来只读审计走独立能力，不得把内部主键塞进普通响应 |

三项均属「在原方案上加约束」，不推翻 P1 结构：不建提示词库、不建文本对象存储、不建第二套规格权威。

## 2. 进入 P2 前必须固化的六项补充约束

| 编号 | 补充项 | Owner 要求要点 | 落在提案哪里 |
|---|---|---|---|
| S-1 | 内容规范化与哈希身份域 | 明确 `spec_sha256` 对什么计算：domain separator + 规范化体；字段排序、Unicode 规范化、数值格式与精度、null/缺省/空串区别、换行符处理；**显式决定是否含租户**；26 位 Base32 需碰撞检测（冲突比完整 SHA-256，不同则拒绝并报警，禁止覆盖） | §2.1 |
| S-2 | 数据库级不可变性与可信管理边界 | 三层：权限（应用角色仅 INSERT/SELECT）／约束（FK `ON DELETE RESTRICT`、无正常 UPDATE 路径）／执行验证（Worker 重算）。若加触发器，职责只是「防正常路径误改」，**不得**宣称抵御超级用户；超级用户与迁移所有者属可信管理边界，由审计/权限隔离/变更记录治理 | §5 |
| S-3 | `GenerationSpec` 与 `ExecutionRecord` 分离 | 规格记「创建时冻结的生成输入」：`schema_version`、`derivation_version`、`size_policy_version`、`model_id`、`provider`、`generation_parameters`；运行时的网关/延迟/响应标识归 ExecutionRecord，不得塞进规格，否则规格退化成运行日志 | §1.1、§1.4 |
| S-4 | 并发唯一冲突与任务幂等语义 | 唯一冲突后不得无条件复用：必须重读、校验完整哈希 + 租户 + 规范化内容一致，不一致则中止；明确「同规格能否多任务」「同幂等键是否必须复用同一任务」「回滚是否留孤立规格」 | §4.1 |
| S-5 | `prompt_ref` 的外部暴露边界 | 不返回 `generation_spec_id` 却返回 `prompt_ref = genspec:gs1_…` 等于间接泄露；若 `prompt_ref` 无外部业务用途，也不返回；外部用任务 ID 查状态 | §3.3 |
| S-6 | `genspec:` 严格解析与 fail-closed | 格式非法 / 找不到 / 租户不一致 / 哈希不一致 / 不支持的 `schema_version` 一律拒绝；不得降级回旧 `inline:` 解析；两形态互不混淆 | §2.3、§6.3 |

## 3. Owner 明确不建议引入的（防止架构膨胀）

| 方案 | 判断 | 理由 |
|---|---|---|
| 独立提示词库 | 不采用 | 第二权威 |
| Prompt 对象存储 | 不采用 | ≤8192 字符数据库存储足够 |
| `ConstraintSnapshot` 新对象 | 暂不采用 | 无真实业务对象支撑 |
| GenerationSpec 版本继承树 | 不采用 | 内容哈希已能区分 |
| Temporal 迁移 R42 | 不采用 | 违反 ADR-R44 (b) |
| Worker 二次派生 Prompt | 不采用 | 破坏冻结语义 |
| 生成后回写 ShotSpec | 不采用 | 破坏单向依赖 |

原则：**生成规格是输入快照，不是新的创作编辑中心。**

## 4. 位置纠正

P1 提案的正式载体是 `docs/proposals/`（`scripts/contract_check.py::PROPOSALS_DIR` 校验此处），
不是 `contracts/proposals/`（不存在）。此前记录该错误路径的地方以本节为准；
后续审查以 `19c1ff9` 的 `docs/proposals/generation-spec-contract-p1.md` 为基线。

## 5. 边界（本记录不授权什么）

- 不授权 P2：契约变更、迁移、实现、冻结重签都仍需单独批准；
- 不改 ADR-R47 正文、不改已合入提案的既有结论（补充以「评审修订记录」追加，不覆盖原文）；
- 不改 R42 的 T1/T2/T3、状态机、事件类型；
- 不新建 `ConstraintSnapshot` / Prompt 库 / ExecutionRecord 实现（ExecutionRecord 仅定义边界，不落地）。

## 6. 追加裁定（2026-11-29，Owner「全部按最好的方案做」）

提案 §11 的两处未决由 Owner 同日授权按最优方案定案（本节追加，不改上文）：

| 项 | 最终裁定 | 为什么这是最优 |
|---|---|---|
| `prompt_ref` 的外部暴露 | **直接移除**，不做「只对 `inline:` 返回」的半保留 | 半保留会让同一字段在不同任务语义不同，客户端与排障都要多一条分支；该字段从无外部业务用途；删一次比先保留再删少一次破坏性变更与冻结重签；将来外部需要给独立只读查询 |
| `prompt_sha256` | **保留独立派生列**（`sha256(正文 NFC 字节)`，不参与身份域） | 网关证据记的是「发出正文的摘要」，无法与含租户与全部字段的 `spec_sha256` 直接比对；独立列让「发出去的正文」与证据逐字节对上，同时身份仍只有 `spec_sha256` 一个 |

P2 实施口径（已写入提案）：

- `GenerationTask` 契约对象移除 `prompt_ref`；请求体 `prompt_ref` 改为可选（生产仅 `inline:` 联调允许）；
- 字段签名台账记 `removed`（理由 + 替代查询 = 按 `generation_task_id` 查状态/结果）；
- 规格 INSERT 时两列由同一段代码一次算出；Worker 读取时两列都重算，任一不符即 `spec-tampered`；
- 证据对账：网关侧正文摘要必须等于 `prompt_sha256`；不符按取证失败处理，不允许「重算覆盖证据」。

## 7. 下一步

1. 把 S-1～S-6 固化进 P1 提案（本记录同日随提案修订一并提交）；
2. Owner 确认修订后的提案 → 申请 P2 授权（契约变更 + 迁移 + 实现 + 冻结重签）；
3. 真实网关凭据到位后按交接单 §1.3 先跑 `verify_model_gateway.py`，再谈 A1 / PR-17。
