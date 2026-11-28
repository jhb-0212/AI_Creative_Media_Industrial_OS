# Phase 0 契约冻结评审决议

**决议编号：** AFS-P0-FREEZE-2026-11-13-01
**评审日期：** 2026-11-13
**评审对象：** `AI影视工业化生产平台V2.0.5.md`（468816 字节，SHA-256 `66ec66d8b047d92f714f6bfa1d6412f9f72c3d19eee23d90331d2e2415a80082`）及 `phase0_manifest.md` 所列十一类契约文件
**评审依据：** 评审方《正式冻结程序执行方案》第三节程序；会前准备件 `phase0_冻结评审执行包.md`

**评审执行方说明：** 本次评审由项目 owner 委托 AI 评审执行，程序、核验项与表决均按上述方案执行；本决议为技术评审结论，法务或对外效力意义上的签署仍以项目 owner 确认为准（见第八节签署栏）。

---

## 一、结论

**Phase 0 契约冻结：有条件通过。**

判定依据：文档与契约证据齐备（`verify_phase0_md.py` 20 项通过、0 失败），十一类文件命名固定、字节与哈希可复核，四项一致性缺陷已在前序整改中关闭；但契约冻结范围仍含 1 个主链对象无字段契约、7 项待补对象依赖后续补齐，故不判无条件通过。

---

## 二、表决结果

| 表决项 | 结果 | 说明 |
|---|---|---|
| A. 确认文档与契约证据齐备 | **通过** | 11 类文件齐备（含清单自身第⑧类）；10 份文件字节与哈希全部匹配；清单自身哈希由独立 `.sha256` 闭合；`verify_phase0_md.py` 20 项 0 失败 |
| B. 确认 8 项 PENDING 为显式延期、不阻塞已建模对象冻结 | **有条件同意** | 8 项均具备 owner / 接口冻结=否 / 启用状态 FORBIDDEN / 复审时点 / due_at_status / blocking_scope / waiver_reason，延期形式成立；但 `ConstraintPack` 位于首发主链（ShotSpec/ShotVisualPlan → ConstraintPack → 生成），不能仅以 FORBIDDEN 了结，单列为 Phase 1 准入前置条件（条件 C1）。其余 7 项接受延期 |
| C. 确认 Phase 0 契约冻结通过 | **有条件通过** | 条件见第三节 |
| D. 确认冻结后变更须走新版本 + ADR + 重新验证 | **通过** | 与既有 31.12/33.15 ADR 机制一致，无需另立规则 |

技术与法律意义上的争议记录：委员意见 1 条（见第七节），不构成反对票。

---

## 三、通过条件（C1—C4）

| 编号 | 条件 | 责任方 | 关闭期限 | 未关闭后果 |
|---|---|---|---|---|
| C1 | `ConstraintPack` 补字段契约（字段标识、类型、必填、版本规则），并由派生器生成 Schema | 契约维护方 | Phase 1 第一迭代开工前；不晚于 Phase 1 准入评审 | Phase 1 主链不得开工（ShotSpec→ConstraintPack 段缺失，无法实现无锁定约束标 NOT_APPLICABLE 的契约语义） |
| C2 | `CameraRig`、`BlockingAsset`、`ReworkTask` 补字段标识 | 契约维护方 | Phase 1 对应域（机位/走位、返工治理）启用前 | 对应域保持 FORBIDDEN，不得启用 |
| C3 | `CreatorWorkspace`、`CreatorGate`、`CreatorPublicationCheck`、`CreatorRelease` 补字段标识 | 契约维护方 | Phase 2 Creator 域启用前（Phase 1 固定 NOT_ENABLED） | Creator 域不得启用 |
| C4 | 8 项待补的 `due_at` 回填具体日期，并由文档管理员在每次启用前复核一次 | 文档管理员 + 项目 owner | 与 C1—C3 各自的关闭时点同批完成 | 该对象视为未排期，不得解除 FORBIDDEN |

说明：C1—C4 不改变 Phase 0 已冻结的 62 个已建模对象（冻结范围内），也不改变 6 个展示域附属对象的排除结论（第四节）。

---

## 四、遗留事项裁决

### 4.1 6 个展示域附属对象是否计入 P0

**裁决：不计入 Phase 0 冻结范围。**

理由：`Appeal`、`Comment`、`Report`、`ReviewAnnotation`、`ShowcaseVersionTree`、`TakeDown` 由 24.3 展示域表带出，不在首发主链（项目创建→剧本导入→AI 拆镜→人工确认→ShotSpec/ShotVisualPlan→ConstraintPack→生成→QC→Gate→DeliveryManifest）路径上，Gate 0—4 均不经过；Phase 1 固定 `CREATOR_MODE NOT_ENABLED`，展示域实际不可用。

处理：P0 契约对象并列两个口径——全集 76（原 70 + 6 附属）、冻结范围 70（已建模 62 + 待补 8）；6 个附属对象字段仍保留在 Schema 中供参考，随展示域在 Phase 1 另行冻结。已同步至正文 31.11 与 `object_schemas.schema.json` 的 `x-afs-p0-coverage.p0_freeze_scope`。

### 4.2 4 处状态机跨章节差异

统一原则：按 31.5 注册表索引所指权威章节判定，权威顺序为 31.5 > 32.6/C.7 > 34.x > 33.3 > C.3。逐项裁决：

| 对象 | 差异 | 裁决 | 实质理由 |
|---|---|---|---|
| `CharacterCanon` | C.7 含 `CONFLICT_REVIEW`，C.3 未列 | **以 C.7 为准**，`CONFLICT_REVIEW` 为合法状态；C.3 未列属描述省略 | Canon 事实冲突必须显式挂起，C.3 自身也要求“事实冲突必须显式标记、不得由模型自动裁定”，缺状态无法承载该要求 |
| `ShotVisualPlan` | C.7 含 `PARTIALLY_LOCKED`/`LOCKED`，C.3 未列 | **以 C.7 为准**，两锁定态合法；C.3 的 `DRAFT→REVIEW→APPROVED/REJECTED` 为其子集 | 锁定字段需逐字段授权并记录锁定人/时间/理由/版本，无锁定态则锁定语义无处落地 |
| `ExplorationBranch` | C.7 含 `FAILED`，C.3 未列 | **以 C.7 为准**，`FAILED` 为合法终态；生成失败不得并入 `REJECTED` | 两者语义不同：`REJECTED` 指候选被否，`FAILED` 指分支执行失败；并态会掩盖执行故障 |
| `QCReviewSession` | C.3 允许 `RESOLVED→GATE_ELIGIBLE/CLOSED`，C.7 要求经 `GATE_ELIGIBLE` 再到 `CLOSED` | **以 C.7 为准，禁止跳关**：`RESOLVED` 后必须经 `GATE_ELIGIBLE` 才可 `CLOSED` | `GATE_ELIGIBLE` 是 Gate 资格校验点；允许跳过等同绕过 Gate 门禁，属质量基线暗降，不可接受 |

处理：裁决原文已写入正文 31.5（“跨章节状态机差异裁决”段），并登记于 `state_machines.yaml` 的 `conflict_rulings`（`ruled_by` 指向本决议编号）。派生表与正文不一致时以正文 31.5 为准。

### 4.3 Gate A 是否接受 Markdown 作为源文档基线

**裁决：接受。**

理由：Gate A 的可校验对象是版本标识、章节编号、标题层级、交叉引用、表格完整性与内部链接，这些在 Markdown 下可被哈希与脚本稳定复核，强于不可解析的 PDF 渲染件；PDF 渲染版降为归档件，非权威源。正文已声明“源文档以 Markdown 为准，如需 PDF 渲染版按导出流程另行生成并留档”。

---

## 五、本次决议即时生效的正文改动

以下改动已落正文，随本决议同步生效：

1. 31.5 新增“跨章节状态机差异裁决”段（第四节 4.2 全部内容）；
2. 31.11 新增 P0 两个口径（76 全集 / 70 冻结范围）与附属对象排除结论；
3. 文档头部状态改为“Phase 0 契约冻结已通过（有条件）”，并保留“有条件通过方向评审”为历史记录；
4. `object_schemas.schema.json` 增加 `p0_freeze_scope`（decision / total 70 / modeled 62 / pending 8 / excluded_supplement_objects）；
5. `state_machines.yaml` 增加 `conflict_rulings` 4 条。

版本号未变更，仍为 V2.0.5。

---

## 六、冻结后变更控制

任何契约变更（Schema 字段、状态机转移、action_key、权限、预算口径、指标、枚举、ADR 编号、阶段映射）须：提交新版本 → 对应 ADR → 重新运行 `verify_phase0_md.py` → 更新 `phase0_manifest.md` 与 `phase0_manifest.md.sha256`。不得原地覆盖已冻结文件。本决议不授权任何“就地改一改”的例外。

---

## 七、争议记录

**委员意见 1（保留）：** 严格意义上，`ConstraintPack` 位于首发主链却未取得字段契约，因此“Phase 0 契约冻结”在字面上并不完整。本决议以 C1 作为 Phase 1 准入前置条件化解，但该对象在冻结范围内的缺失应被记录在案，不得在对外表述中略去。表决时未投反对票，理由是该缺口已有可执行的关闭路径与明确后果。

无弃权。

---

## 八、签署

| 角色 | 签署 | 说明 |
|---|---|---|
| 评审主持（AI 评审，受项目 owner 委托） | AFS-P0-FREEZE-2026-11-13-01 | 程序与核验按《正式冻结程序执行方案》第三节执行 |
| 记录员（AI 评审） | 同上 | 表决过程与证据见 `phase0_冻结评审执行包.md` 第三节核验表 |
| 项目 owner 确认 | 敬宏波（2026-11-13） | 对外或法务效力以本栏签署为准。签署人确认：第七节所列条件 C1—C4 已于 2026-11-13 关闭，第十三、十四份结论及两条补充观察已处置完毕，Phase 0 契约冻结条件正式关闭；Phase 1 实现与各工具 Profile 启用仍须按代码、权限、账本、QC/Gate 与部署证据验收 |
| 日期 | 2026-11-13 | — |

---

## 九、归档清单

| 文件 | 字节 | SHA-256 |
|---|---|---|
| `AI影视工业化生产平台V2.0.5.md` | 468816 | `66ec66d8b047d92f714f6bfa1d6412f9f72c3d19eee23d90331d2e2415a80082` |
| `phase0_manifest.md` | 见清单自身 `.sha256` | 见 `phase0_manifest.md.sha256` |
| 十一类契约文件（10 份 + 清单自身） | 见 `phase0_manifest.md` 文件清单 | 逐份登记 |
| `verify_phase0_md.py` 输出 | — | 20 项通过、0 失败 |
| `phase0_冻结评审执行包.md` | — | 会前准备件 |
| 本决议 | — | 归档主件 |

---

## 十、下一步

1. 项目 owner 在第八节确认签署；
2. 按 C1—C4 跟踪关闭，`due_at` 由 owner 指定后回填 manifest 与 Schema；
3. 进入 Phase 1 核心生产闭环实现（一条真实 `USER_OWN_KEY` 路线）；
4. Phase 1 准入评审时复核 C1 是否关闭，未关闭则主链不开工。

---

## 十一、条件执行回执（C1—C4 关闭情况）

回执日期：2026-11-13。执行方：契约维护方。版本号未变更（仍 V2.0.5）。

| 条件 | 执行结果 | 证据 | 状态 |
|---|---|---|---|
| C1 `ConstraintPack` 字段契约 | 在 7.9 段后新增字段契约表 13 项：`constraint_pack_id`、`project_id`、`scene_id`、`shot_ids`、`shot_group_ids`、`applicability`、`locked_dimensions`、`constraints`、`source_previz_revision`、`version`、`status`、`approved_by`、`approved_at`；`applicability` 显式承载“无锁定约束镜头标 NOT_APPLICABLE”，`status` 规定仅 ACTIVE 可作为生成输入 | 正文 7.9；Schema `ConstraintPack`（22 属性、19 必填） | **已关闭** |
| C2 `CameraRig`、`BlockingAsset`、`ReworkTask` | `CameraRig` 14 项（`intrinsics`/`extrinsics`/`motion_curve`/`keyframes`/`time_base`/`unit` 等，其中 3 项可选）；`BlockingAsset` 13 项（含 `preview_only` 固定 true、`rights_manifest_ref` 必填）；`ReworkTask` 13 项（`issue_id`、`defect_category`、`return_node`、`severity`、`owner`、`affected_asset_version_ids` 等，`status` 含 VERIFYING） | 正文 8.15、7.6；Schema 对应三对象 | **已关闭** |
| C3 Creator 四对象 | `CreatorWorkspace` 11 项（`workspace_mode` 创建后不可改写、`owner_principal_id` 唯一发布权）；`CreatorGate` 10 项（`decision` 明示不授予发布权）；`CreatorPublicationCheck` 11 项（`evidence_refs` 必填，缺失即检查不完整）；`CreatorRelease` 11 项（`user_intent_id` + `capability_token_jti` 强制绑定、`audience_scope` PUBLIC 仅 Phase 3） | 正文 34.1、34.5、34.8；Schema 对应四对象 | **已关闭** |
| C4 `due_at` 回填与启用前复核 | 待补项已清零，8 项不再存在到期日问题；复核机制转为规则写入正文 31.11“待补清零与启用前复核”：新增对象或解除 FORBIDDEN 前必须补字段契约、重跑派生与校验、由文档管理员复核留痕 | 正文 31.11；清单待补表为空 | **已关闭（机制性）** |

执行结果汇总：P0 待补 8 → 0；已建模 68 → 76（冻结范围 62 → 70）；Schema 属性 1259 → 1429；`verify_phase0_md.py` 20 项通过、0 失败。

字段来源说明：四组字段全部落在既有正文语义上，不新增业务规则。唯一需要显式化的是 `applicability` 的 NOT_APPLICABLE 取值，依据为 ADR-088 与 31.11 既有表述“无锁定约束镜头显式标记 NOT_APPLICABLE”。

由此，第四节 4.1 与第七节保留意见中所指的“主链对象无字段契约”缺口已闭合，Phase 0 冻结范围（70 个对象）全部具备字段契约。

### 11.1 状态机补登（同日）

字段契约补齐后复核发现，4 个对象新增的 `status` / `decision` 字段缺少权威状态机登记，违反正文 31.5“新增对象必须先在本注册表登记权威状态机章节”的规则：

| 对象 | 状态机 | 登记位置 | 关键约束 |
|---|---|---|---|
| `ConstraintPack` | DRAFT → ACTIVE；ACTIVE → STALE / SUPERSEDED / REVOKED | 7.9；31.5 索引 | 仅 ACTIVE 可作生成输入；NOT_APPLICABLE 包同样须经 ACTIVE，以证明“无锁定约束”是授权人判断而非默认值 |
| `ReworkTask` | OPEN → IN_PROGRESS → DONE → VERIFYING → CLOSED；VERIFYING 不通过退回 IN_PROGRESS；未关闭状态可 REJECTED | 7.6；31.5 索引 | VERIFYING 复核人不得为返工执行人本人 |
| `CreatorWorkspace` | ACTIVE → SUSPENDED → ARCHIVED | 34.1；31.5 索引 | ARCHIVED 终态；升级 FULL_MODE 是新建 successor，不是状态迁移 |
| `CreatorGate` | PENDING → PASS / BLOCKED / NEEDS_USER_INPUT | 34.5；31.5 索引 | 三者均为终态结论，不得原地改写；不进工业 Gate 状态机 |

已同步 `state_machines.yaml`（27 → 31 个对象）与 `enum_registry.yaml`（27 → 31 个对象），正文 31.11 与清单登记数一并改为 31。本节是对 C1—C3 结论的补强，不改变其“已关闭”判定；决议第二至十节保持冻结日快照，不回改。

### 11.2 三项遗留处置（同日）

| 遗留 | 处置 | 影响 |
|---|---|---|
| 指标字典成本行三指标合并 | 正文 31.10 拆为三行；`gen_metric_dictionary.py` 改为从 Markdown 派生，断言升级为“唯一且原子” | 指标 15 → 17；契约内容变更，须纳入冻结包并重算哈希 |
| 段落状态机未入派生 | `AssetVersion`（8.5）、`ReverseAnalysisReport`（25.2）纳入，`PatternRecord` 以 C.3 为准并新增第⑤条裁决；共享参考资产（26.2）与 `ClientReviewSession`（31.7）无成组状态机，登记于 `text_authority_only` | 状态机 31 → 33；跨章差异 4 → 5 处 |
| Phase 1 实现验收无判据 | 新增 `Phase1-实现验收清单.md`（A—G 七组 25 项，含契约出处、证据要求、通过标准） | 不属十一类冻结文件，不进清单哈希 |

过程中修正两处自身缺陷：MANUAL 未去重导致 `PatternRecord` 双登记与对象数虚高；枚举抽取把注释中的失效标记（`REVERSE_STALE`、`REVOKED_BLOCKED`）与适用性取值（`NOT_APPLICABLE`）误当状态。均已改为覆盖式合并与显式排除。

冻结结论据此维持“通过”，口径以 `phase0_manifest.md` 与正文同步后的版本为准。

### 11.3 第十三份结论处置（同日）

8 项 P0 中 7 项成立、1 项不成立，已成立的 7 项在正文原地修正：

| 编号 | 处置 | 结果 |
|---|---|---|
| P0-1 文档状态矛盾 | 头部状态与旧决议末句统一为“Phase 0 契约冻结已条件通过、Phase 1 未验收”，并声明“冻结状态以头部为准，其他章节不得另述” | 状态可审计 |
| P0-2 Creator Release Phase 2 冲突 | Phase 2 不产生 CreatorRelease，组织内展示改走 CommunityPost + ShowcaseSnapshot + PublicationReview；34.8 增“阶段边界”段，三种受众一律 Phase 3 | 消除产品边界分叉，不动 75 项 action |
| P0-3 ShowcaseSnapshot 缺 TAKEN_DOWN | 31.5 补 `TAKEN_DOWN`，并区分其与 `REVOKED` 的语义 | 与 24.12 三层映射恢复一致 |
| P0-4 客户审片命名 | `CLIENT.REVIEW.ANNOTATE` 的 resource 改为 `ClientAnnotation`；27.2 状态机归 `ClientReviewPortal`，声明无独立 ClientReviewSession 对象；31.7 与指标口径同步更名 | 一对象一名称 |
| P0-5 状态注册与能力状态集 | 31.5 补登 `ExternalToolConnection`；32.3 状态集补 `ACTIVE`、`RETEST_REQUIRED`；33.6 四值明确为 `support_level` | 状态机 33 → 35 |
| P0-6 32.9 漏 action | **不成立**：32.9 已列 10 项含 `EXTERNAL_TOOL.RETURN.INGEST`，31.6 表 3488 行同名，注册表亦有 | 不改正文 |
| P0-7 31.11 证据描述失实 | 改为 PatternRecord 以 C.3 为权威、27.2 与 32.5 已入册、共享参考资产“有状态链但未建模” | 描述与正文一致 |
| P0-8 同账户重分配无 action | 明确由 `BUDGET.SHOT.APPROVE` 承载 + BudgetLedger 内部 adjust，挂三个负向用例 | 不留“能做但无动作”的洞 |

校验：35 个状态机对象、5 处跨章差异、枚举 35、指标 17、Action 75、P0 待补 0、`verify_phase0_md.py` 20 项通过 0 失败。版本仍 V2.0.5。

### 11.4 第十四份确认意见处置（同日）

评审方认可 7 条 P0 为“关闭候选”，并提出 4 个衔接点、1 个流程问题、3 条 P1。处置如下：

| 项 | 处置 | 结果 |
|---|---|---|
| 衔接①：31.11/历史说明残留句 | 31.11 末句改为“契约层可提交冻结评审、不构成对实现能力的认可”，并规定“冻结状态以头部与决议为准”；V2.0.1 历史句标注为历史陈述 | 全文只剩一套状态表述 |
| 衔接②：ShowcaseSnapshot 引用对象 | 34.8 明确 `display_asset_refs` 引用 manifest 产出的 AssetVersion、`delivery_package_ref` 引用导出产物，manifest 只作证据；31.2 残留句整句删除 | Phase 2 展示链无悬空引用 |
| 衔接③：`TAKEN_DOWN` 终态 | 31.5 写死 `TAKEN_DOWN`/`REVOKED` 均为终态，恢复须新建 `snapshot_version` 并重走 Publication Gate | 与 `COMMUNITY_POST_RESTORED` 对齐 |
| 衔接④：共享参考资产计数 | 31.11 明确展示域 6 个附属对象与资产域 `SharedSubjectAsset` 并列排除，不计入 76 与待补 | “待补 0”口径可解释 |
| 流程：版本号未动 | 采方案 B：版本仍 V2.0.5，新增修订标识 `V205-R14`（头部 + 清单双登记），31.11 写死“正文哈希变化即新修订、须重建哈希” | 同版本不同修订可审计，不违反“不升版本号” |
| P1：阶段表表头 | 23.1/30.5/C.8/D.8 标题统一加“非权威；与 31.2 不一致以 31.2 为准；CI 校验 phase_map.yaml” | 防复发 |
| P1：废弃名称清单 | 31.5 后新增 7 行“废弃名称→规范名称”，并声明冻结后不得引入新别名 | 悬空引用不再再生 |

校验：正文 488897 字节、35 状态机、20 项全通过。评审提出的“关闭条件冻结”前置条件已满足；正式宣布关闭仍待本决议签署。

### 11.5 两条补充观察处置（同日）

| 观察 | 核实 | 处置 |
|---|---|---|
| `CREATOR.RELEASE.SUBMIT` 标 Phase 2，与“Phase 2 不产生 CreatorRelease”冲突 | 成立，31.6 与 `action_registry.yaml` 均标 Phase 2 | 改标 Phase 3（不改“Phase 2 不产生 CreatorRelease”的既有边界），并在 31.6 该行注明后置理由；注册表 status 同步 |
| `TAKEN_DOWN` 恢复路径未定 `snapshot_id` 是否可复用 | 成立，原文只提 `snapshot_version` | 31.5 写死“新建 `snapshot_id` + 递增 `snapshot_version` + 重走 PG + 旧快照保留”；`Phase1-实现验收清单.md` 补 D4 用例 |

另修正 Phase 1 清单 D1 的“33 对象”为 35。正文 489112 字节，20 项校验全通过。

### 11.6 32.7 字段名收口（同日）

32.7 元数据映射表的 `client_review_session_id` 全文仅一处，且 `ClientReviewPortal` 在 Schema 中只有 `portal_id`，该字段未建模、只是外部同步键。已直接改名为 `client_review_portal_id`（与 `portal_id` 对齐），并在行内注明“会话仅指一次门户访问、字段不得反向催生对象”；31.5 命名统一表新增一条字段级别名登记。复验：该旧名仅剩别名表登记 1 处，`ClientReviewSession` 4 处均为否定或废弃语境，无活引用。正文 489537 字节，20 项校验全通过。

### 11.7 签署生效（2026-11-13）

项目 owner 敬宏波已于第八节签署栏确认签署。据此：

- 第七节条件 C1—C4 全部关闭，Phase 0 契约冻结**条件正式关闭**，冻结结论由“有条件通过”转为“通过”；
- 第十三份结论（P0-1—P0-8，7 成立 1 不成立）、第十四份确认意见（4 个衔接点 + 1 个流程问题 + 2 条低成本 P1）、两条补充观察（`CREATOR.RELEASE.SUBMIT` 阶段标注、`TAKEN_DOWN` 恢复路径）、32.7 字段名收口，均已处置完毕并留痕于评审记录第二十二至二十五节；
- 正文头部状态同步为“Phase 0 契约冻结已通过（条件已关闭、决议已签署），Phase 1 未验收”；
- 冻结包的机器可读口径维持：对象 76（待补 0）、状态机 35（跨章差异 5 处均附裁决）、枚举 35、指标 17、Action 75、OpenAPI 75、ADR 118，`verify_phase0_md.py` 20 项通过；
- 本签署只关闭文档与契约层的冻结，不构成任何功能已实现。Phase 1 仍须按代码、自动化测试、权限与职责分离、账本与配额、QC/Gate、版权与安全、部署与可回滚证据逐项验收（见 `Phase1-实现验收清单.md`）。
