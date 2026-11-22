# 游离基线副本移除归档（2026-11-22）

> 本文件是**归档**，不是基线正文，不具有任何权威性。
> 它记录 2026-11-22 从项目根目录移除游离副本 `AI影视工业化生产平台V2.0.5.md` 之前的完整差异，
> 目的是「移除副本但一粒字都不丢」。
>
> 权威基线唯一路径：`docs/baseline/AI影视工业化生产平台V2.0.5.md`（V205-R15，489580 字节，
> SHA-256 `b922a1e1c6e5c43b0c7e6ee30b5f036b7919fcc946935594f225b90bf441945e`）。

## 一、被移除副本的事实

| 项目 | 值 |
| --- | --- |
| 原路径 | `AI影视工业化生产平台V2.0.5.md`（项目根目录） |
| 字节数 | 490050 |
| SHA-256 | `b6c1f0cce766c49e8a940987c3d0f5b63e072a273631674a7d01d2c48ea753cd` |
| 修订标识 | V205-R14（2026-11-13） |
| 判定 | 与权威基线同名，构成「第二权威源」；`freeze_manifest.py::check_stray_baseline_copies()` 按文件名扫描，该文件存在即报红 |
| 关键风险行 | 第 17 行写「Phase 0 契约冻结已通过（……条件 C1—C4 已于 2026-11-13 关闭，决议已由项目 owner 签署）」——与权威基线「条件通过」及决议原件缺失的事实冲突 |

## 二、与权威基线的完整差异（共 7 个 hunk）

```diff
--- a/AI影视工业化生产平台V2.0.5.md（游离副本）
+++ b/docs/baseline/AI影视工业化生产平台V2.0.5.md（权威基线）
@@ -5,7 +5,7 @@
 文档类型：完整架构基线 + 产品化需求基线 + 工程契约与验收基线（Master Baseline）
 
-版本：V2.0.5（修订标识 V205-R14，2026-11-13；版本号按项目约束保持 V2.0.5，修订标识用于区分同版本不同修订）
-
-状态：V2.0.5 智能时间线、工程包与 Creator Mode 逻辑一致性修订已纳入；在 AI 辅助开发与生产条件下保留完整技术基线与完整能力规划，补全全局红线不变量、工程质量与 AI 辅助开发规则、策略实现约束、资产谱系边界与口径权威指向；有条件通过方向评审；Phase 0 契约冻结已通过（有条件），冻结决议编号 AFS-P0-FREEZE-2026-11-13-01，冻结日期 2026-11-13，条件与责任人见《Phase0-冻结评审决议.md》；“有条件通过方向评审”结论保留为历史记录；Phase 1 实现及各工具 Profile 启用仍须以代码、兼容性、权限、版权、账本、QC/Gate 和部署证据验收；不代表所有功能或第三方适配器已经实现。
+版本：V2.0.5（修订标识 V205-R15，2026-11-22；版本号按项目约束保持 V2.0.5，修订标识用于区分同版本不同修订）
+
+状态：V2.0.5 智能时间线、工程包与 Creator Mode 逻辑一致性修订已纳入；在 AI 辅助开发与生产条件下保留完整技术基线与完整能力规划，补全全局红线不变量、工程质量与 AI 辅助开发规则、策略实现约束、资产谱系边界与口径权威指向；有条件通过方向评审；Phase 0 契约冻结条件通过，冻结决议编号 AFS-P0-FREEZE-2026-11-13-01，冻结日期 2026-11-13，条件与责任人见《Phase0-冻结评审决议.md》；“有条件通过方向评审”结论保留为历史记录；Phase 1 实现及各工具 Profile 启用仍须以代码、兼容性、权限、版权、账本、QC/Gate 和部署证据验收；不代表所有功能或第三方适配器已经实现。
 
 版本谱系：V1.2.1（工程架构修订）→ V1.3.1—V1.3.5（架构扩展、策略解耦、计费、社区、反推与生态）→ V1.3.6 产品化整改要求（整合依据）→ V2.0（统一架构与产品基线）→ V2.0.1（文档治理与逻辑一致性修订）→ V2.0.2（外部工具适配层）→ V2.0.3（智能时间线与 Creator Mode 初版契约）→ V2.0.4（时间线/Creator 状态机、发布权限与无渲染边界收敛） → V2.0.5（评审整改：完整技术基线与能力规划保留，红线不变量、工程质量与 AI 辅助开发规则、策略约束、资产谱系与口径权威补全）
@@ -15,5 +15,5 @@
 修订依据：继承 V2.0.2 完整主文档及附录 A—D；吸收 V2.0.3 中的 TimelineDraft、ExportPackage、TimelinePreviewPlayer、CreatorWorkspace、CREATOR_GATE、CREATOR_DELIVERY、CREATOR_RELEASE 契约，并对其状态机、发布授权、渲染边界、Phase 映射和文档顺序进行统一修订。
 
-文档编号：AFS-ARCH-MASTER-V2.0.5  |  修订日期：2026-11-13  |  状态：Phase 0 契约冻结已通过（决议 AFS-P0-FREEZE-2026-11-13-01，条件 C1—C4 已于 2026-11-13 关闭，决议已由项目 owner 签署），Phase 1 未验收
+文档编号：AFS-ARCH-MASTER-V2.0.5  |  修订日期：2026-11-13  |  状态：Phase 0 契约冻结条件通过（决议 AFS-P0-FREEZE-2026-11-13-01，条件与责任人见《Phase0-冻结评审决议.md》），Phase 1 未验收
 
 示例占位符规则：所有带省略号的 ID、凭证引用、URL、模型版本、时间戳、金额和用户标识均为 EXAMPLE_PLACEHOLDER；必须由真实环境数据替换，不可直接用于生产。
@@ -1941,6 +1941,4 @@
 │   ├── orchestrator/
 │   ├── project-planning/
-│   ├── shot-planning/
-│   ├── generation-execution/
 │   ├── asset-service/
 │   ├── rights-governance/
@@ -2014,6 +2012,5 @@
 | **模块** | **核心数据所有权** | **允许写入者** | **主要对外契约** |
 | --- | --- | --- | --- |
-| Project & Planning | Project、Episode、Scene、Intent（项目级意图，作用域拆分见 ADR-R27 §3.5） | Project/Planning Domain Service | Project API、Intent API（项目级） |
-| Shot Planning | Shot、ShotDraft、ShotRevision、ShotSpec、ShotVisualPlan、ConstraintPack、Intent（镜头级意图，作用域拆分见 ADR-R27 §3.5） | Shot/Planning Domain Service | Shot API、ShotChanged Event |
+| Project & Planning | Project、Episode、Scene、Shot、ShotRevision、ConstraintPack、Intent、ShotSpec | Project/Planning Domain Service | Project API、Intent API、ShotChanged Event |
 | Blocking & Previz | PrevizVersion、CameraRig、ControlSignalAsset 元数据、Geometry/Purity Validation | Blocking Domain Service | Previz API、ControlSignal Events、PrevizVersionCreated Event |
 | Creative Intelligence | CreativeProposal、ExplorationRun、CreativeDecision、Pattern | Creative Service | Proposal API、Exploration Events |
@@ -2021,5 +2018,4 @@
 | Asset Management | Asset、AssetVersion、Manifest、Storage Metadata、BlockingAsset 文件版本 | Asset Service | Asset API、AssetVersionCreated Event |
 | Model Gateway | Provider、ModelCapability、ModelInvocation、ProviderJob | Model Gateway | Submit/Status API、Invocation Events |
-| Generation Execution | ProductionRoute、GenerationTask（生成任务） | Generation Domain Service | Route Select API、GenerationTask Events |
 | Quality & Governance | QCReport、DetectorRegistry、Benchmark、QualityProfile、RightsCase、Override | QC/Governance Service | Evaluate API、Governance Gate Events |
 | Memory & Learning | Memory、Canon Index、MemoryVersion、Feedback、Pattern Evidence | Memory Service | Memory Query/Write API |
@@ -3639,5 +3635,5 @@
 Phase 0 契约冻结证据（文档与契约层，不需要系统运行即可验收）：①Gate A 文档基线——版本标识、章节编号、标题层级、交叉引用、表格完整性、内部链接与渲染检查无残留（Markdown 无页眉页脚与自动目录域，对应检查项不适用）；源文档以 Markdown 为准，如需 PDF 渲染版按导出流程另行生成并留档；②Gate C 阶段一致性——阶段总表与各章节 Phase 映射自动检查通过；③契约文件齐备——对象 JSON Schema 草案（object_schemas.schema.json，76 个对象已建模，字段分别取自 C.3、17.10、25.1、27.1、32.5 数据契约表、8.12—8.14 与 2.5 的 YAML 契约块、2.4/8.10/8.11 的段落字段列举、24.3「字段（最小建议）」、32.4 AdapterManifest 字段组、33.2 TimelineDraft 字段组、33.5 ExportPackage、34.7 CreatorDeliveryManifest 五张形态特殊但确有字段定义的表，以及 7.4 Gate 字段列举、21.1 缺陷字段列举，以及按冻结评审条件 C1—C3 新补的 7.6 返工单、7.9 ConstraintPack、8.15 CameraRig 与 BlockingAsset、34.1 CreatorWorkspace、34.5 CreatorGate、34.8 CreatorPublicationCheck 与 CreatorRelease 字段契约表（均含字段标识、类型、必填与版本规则）；P0 契约对象全集 76 个（原 70 个清单 + 6 个由 24.3 带出的展示域附属对象）已全部建模，待补项清零）、OpenAPI 草案（openapi.draft.yaml，75 条路径与 Action Registry 一一对应）、各对象状态机表（state_machines.yaml，覆盖 31.5、C.7、32.6、33.3、34.x 登记的表格式、图示类与段落箭头式状态机共 35 个对象，含 Task、BudgetLedgerTransaction、34.7 的 CreatorDeliveryManifest，按字段契约补登的 ConstraintPack（7.9）、ReworkTask（7.6）、CreatorWorkspace（34.1）、CreatorGate（34.5），以及从正文提取的 AssetVersion（8.5）、ReverseAnalysisReport（25.2）、ClientReviewPortal（27.2）、ExternalToolConnection（32.5）；PatternRecord 以 C.3 完整箭头链为权威，26.2 的状态集合与命名差异已按 31.5 第⑤条裁决，不属“无状态机”；共享参考资产（26.2）有完整状态链但对象尚未建模，登记于 text_authority_only 并注明建模后须补登记状态机，不属“无状态机”）、Action Registry 机器可读清单（action_registry.yaml，75 项与 31.6 主表键集合及顺序双向一致）、权限矩阵表（permission_matrix.yaml，C.5 角色级 7 角色 × 9 关键操作摘要 + 由 31.6 派生的 75 项 action_key 级条目 + C.5.1 职责分离规则）、预算与成本口径表（budget_caliber.yaml，4 类预算账户及 Creator Mode 成本规则）、指标字典（metric_dictionary.yaml，含 Gate G 的字典部分）、冻结包清单（phase0_manifest.md，逐份登记文件名、字节数、SHA-256、对应章节与校验命令；清单自身为自指文件，其自身哈希由同目录 phase0_manifest.md.sha256 承载，不列入上述十一类文件哈希清单）、枚举注册表（enum_registry.yaml，登记 35 个对象的状态值，并把权威状态值、展示投影别名、跨域 STALE 标记分三层，别名必须落在权威状态集内）、ADR 主索引（adr_index.yaml，全文 118 条 ADR 的编号、章节归属、决策与同号重复及编号缺口检出）、阶段表机器可读版（phase_map.yaml，只收录 31.2 的 5 个阶段，其余章节同名阶段表一律登记为非权威摘要）；P0 契约对象按两个口径计：全集 76 个（含 24.3 带出的 6 个展示域附属对象 Appeal、Comment、Report、ReviewAnnotation、ShowcaseVersionTree、TakeDown），冻结范围 70 个（不含上述 6 个，其中已建模 70 个、待补 0 个）；6 个附属对象经冻结评审裁决不计入 Phase 0 冻结范围，随展示域在 Phase 1 另行冻结，其字段仍保留在 Schema 中供参考；上述十一类文件统一存放于项目根目录，文件名固定，并与本文档对应章节保持可校验的一致；④静态可校验性——Schema 结构合法且 required 与 properties 自洽、action_key 全局唯一且命名规范、状态机表覆盖 31.5、C.7、32.6、33.3、34.x 登记对象且与正文无静默差异、权限矩阵无空缺角色、预算账户用途无交叉；同一对象在不同章节出现不一致状态机定义时，须在状态机表中显式标记并逐项裁决，不得静默合并或以派生文件覆盖正文。上述证据齐备仅表示契约层可提交冻结评审，不构成对实现能力的认可。Phase 0 契约冻结状态以文件头部与决议 AFS-P0-FREEZE-2026-11-13-01 为准（当前为条件通过），任何章节不得另述冻结状态。
 
-正文修订与哈希：本文件修订标识 V205-R14（2026-11-13），正文 SHA-256 与字节数由 `phase0_manifest.md` 登记。正文哈希一旦变化即视为新修订，必须重跑派生器、整包校验并重建清单哈希与 `.sha256`，不得沿用旧哈希；文档版本号按项目约束保持 V2.0.5，同版本不同修订由修订标识区分，禁止在未重建哈希的情况下宣称“哈希一致”。
+正文修订与哈希：本文件修订标识 V205-R15（2026-11-22），正文 SHA-256 与字节数由 `phase0_manifest.md` 登记。正文哈希一旦变化即视为新修订，必须重跑派生器、整包校验并重建清单哈希与 `.sha256`，不得沿用旧哈希；文档版本号按项目约束保持 V2.0.5，同版本不同修订由修订标识区分，禁止在未重建哈希的情况下宣称“哈希一致”。
 
 P0 契约对象口径的排除边界：76 个对象（冻结范围 70 + 展示域附属 6）之外，还有两类明确排除、不计入待补统计的附属对象——①展示域 6 个（Appeal、Comment、Report、ReviewAnnotation、ShowcaseVersionTree、TakeDown），随展示域在 Phase 1 另行冻结；②资产域共享主体库容器 `SharedSubjectAsset`（26.1），它只承载已建模的 CharacterCard、CharacterAppearance、CharacterReferenceSet 的共享引用关系，本身不新增业务字段，P0 期间以 26.2 正文为权威，Phase 1 建模时另行纳入。因此 `SharedSubjectAsset` 未建模不构成待补项，“待补 0”的口径不含上述两类。
```

## 三、处置

1. 差异已完整归档于本文件；
2. 原始文件备份于项目目录外 `D:\Users\9527\Desktop\_stray_copy_backup\`；
3. 项目根目录的游离副本已移除，全仓仅保留 `docs/baseline/` 一处权威基线。

## 四、注意

- 副本中「shot-planning / generation-execution 服务域拆分」「数据所有权表拆行」「ADR-R27 §3.5 引用」
  等设计主张的裁定结论见 `docs/audits/2026-11-18-stray-baseline-copy-drift.md` §3（D6 输入），本归档不重复裁定。
