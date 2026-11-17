# AI 影视工业化生产平台 V2.0.5

> 导演意图驱动 · 创意自由 · 统一高品质 · 三极产品体系 · 可治理复用 · 可追溯交付

文档类型：完整架构基线 + 产品化需求基线 + 工程契约与验收基线（Master Baseline）

版本：V2.0.5（修订标识 V205-R14，2026-11-13；版本号按项目约束保持 V2.0.5，修订标识用于区分同版本不同修订）

状态：V2.0.5 智能时间线、工程包与 Creator Mode 逻辑一致性修订已纳入；在 AI 辅助开发与生产条件下保留完整技术基线与完整能力规划，补全全局红线不变量、工程质量与 AI 辅助开发规则、策略实现约束、资产谱系边界与口径权威指向；有条件通过方向评审；Phase 0 契约冻结已通过（有条件），冻结决议编号 AFS-P0-FREEZE-2026-11-13-01，冻结日期 2026-11-13，条件与责任人见《Phase0-冻结评审决议.md》；“有条件通过方向评审”结论保留为历史记录；Phase 1 实现及各工具 Profile 启用仍须以代码、兼容性、权限、版权、账本、QC/Gate 和部署证据验收；不代表所有功能或第三方适配器已经实现。

版本谱系：V1.2.1（工程架构修订）→ V1.3.1—V1.3.5（架构扩展、策略解耦、计费、社区、反推与生态）→ V1.3.6 产品化整改要求（整合依据）→ V2.0（统一架构与产品基线）→ V2.0.1（文档治理与逻辑一致性修订）→ V2.0.2（外部工具适配层）→ V2.0.3（智能时间线与 Creator Mode 初版契约）→ V2.0.4（时间线/Creator 状态机、发布权限与无渲染边界收敛） → V2.0.5（评审整改：完整技术基线与能力规划保留，红线不变量、工程质量与 AI 辅助开发规则、策略约束、资产谱系与口径权威补全）

适用范围：AI 影视短片、系列短剧、动画/真人混合内容及规模化镜头生产；覆盖制作、资产与知识、社区展示、视频反推、客户审片交付、外部工具适配、智能时间线、个人创作者模式、开放生态和双模式计费。

修订依据：继承 V2.0.2 完整主文档及附录 A—D；吸收 V2.0.3 中的 TimelineDraft、ExportPackage、TimelinePreviewPlayer、CreatorWorkspace、CREATOR_GATE、CREATOR_DELIVERY、CREATOR_RELEASE 契约，并对其状态机、发布授权、渲染边界、Phase 映射和文档顺序进行统一修订。

文档编号：AFS-ARCH-MASTER-V2.0.5  |  修订日期：2026-11-13  |  状态：Phase 0 契约冻结已通过（决议 AFS-P0-FREEZE-2026-11-13-01，条件 C1—C4 已于 2026-11-13 关闭，决议已由项目 owner 签署），Phase 1 未验收

示例占位符规则：所有带省略号的 ID、凭证引用、URL、模型版本、时间戳、金额和用户标识均为 EXAMPLE_PLACEHOLDER；必须由真实环境数据替换，不可直接用于生产。

## 0. 执行摘要

平台目标不是把 AI 影视生产变成更严格的模板工厂，而是建立一个质量基线清晰、创作空间足够开放、生产资源能够自适应调度、资产知识可以安全复用、社区展示有明确边界的人机协同创作平台。

平台不自研替代性的专业剪辑器、音频工作站、3D 建模器或特效合成器；平台自研做深约束、质量、追溯、调度、资产、权限和成本治理，专业工具负责执行。所有外部工具经 L3 统一适配器接入，回流资产仍由平台 AssetVersion、Rights、QC、Gate 和 DeliveryManifest 形成权威闭环。

平台采用三极定位：制作解决“怎么做”；资产与知识中枢解决“用什么做、怎么复用”；社区展示解决“给谁看、怎么学”。生产画布和展示画布分离；分享是协作级授权，发布是展示级审核；发布不等于 Gate4 正式交付。

平台必须同时满足三项要求：

1. **全等级品质平权：** S/A/B 项目执行统一的高品质成片验收基线。B 级可以采用更成熟的工作流、更高比例的资产复用和更精简的审批链，但不得通过降低最终验收标准来换取吞吐量。

2. **AI 创意自由：** 系统规定创作目标、事实边界、连续性要求和交付规格，不把构图、运镜、光影、色彩、表演、视觉隐喻、特效方案等创作手段锁死。AI 可以提出替代方案、主动挑战初始方案，并在授权范围内自主探索。

3. **质量驱动调度：** 项目级 S/A/B 决定基础资源与治理强度；镜头级风险、质量反馈和创意不确定性决定动态升级。资源不足时必须显式暴露差距，由人类调整资源、周期或交付范围，不允许系统暗中降质。

**跨路线控制信号层：** 白模（Blocking/Previz）不是与图生视频、文生视频并列的独立成片路线，而是可选、可复用的生产控制层。它把经结构化描述和校验的相机轨迹、主体走位、空间阻挡关系与时间节奏，转化为版本化、可追溯的 `ControlSignalAsset`。图生视频、文生视频、白模直接生成、2D Layout、虚拟制片/实拍合成和传统 CG 等路线，只能按各自声明的能力消费这些信号。白模默认不承担角色身份、最终外观、材质、灯光风格或精细物理控制。

平台的五条顶层宪法：

- **品质平权：** 所有项目等级、集数和镜头执行同一套项目适配后的高品质验收基线。

- **创意自由：** 只锁定必要约束，不锁定表达路径；模板和历史经验是可复用起点，不是创意上限。

- **人类主导：** 导演/创作负责人掌握创作方向、锁定事实和最终艺术判断；AI 可以建议、质疑和探索，不得擅改已锁定决策。

- **资源服从质量：** 算力、模型、工作流、人工和时间应随镜头难度与质量缺口动态调整。

- **持续进化：** 沉淀技术能力和创作经验，但不把历史偏好固化为未来创作的唯一答案。

本文是目标架构与实施基线，不代表所有能力已经在现有代码骨架中实现。所有“必须”均为目标系统的设计约束；实施状态需以代码、测试和部署验收结果为准。

历史版本修订重点（V2.0.4）：在 V2.0.2 的完整架构与 V2.0.3 的新增能力基础上，补齐智能时间线/工程包/预览的对象边界与状态机，彻底取消平台内帧级剪辑、转场渲染、字幕烧录、混音和 SimpleRender；明确 Creator Release 由用户显式发起、系统自动执行发布资格检查、Agent 不得代发；统一 CREATOR_GATE、CREATOR_PUBLICATION_CHECK、CreatorDeliveryManifest、CreatorRelease 的权威边界；补齐模板预设、10 秒导入指引、工具能力矩阵、回流 QC/Gate、专项验收和 ADR-101—118。

新增章节导航：第 32 章定义外部工具适配层、专业工具接入契约、工程资产模型、工具回流 QC/Gate 闭环及分阶段实施要求。

## 目录与章节导航（完整主文档，含内嵌附件 A—D）

| **章节/附件** | **主题** | **核心范围** |
| --- | --- | --- |
| 第 0 章 | 执行摘要与平台宪法 | 三极定位、白模控制信号层、统一品质、创意自由、人类主导与治理边界 |
| 第 1—4 章 | 目标、创作治理、CEE、质量体系 | 硬约束/创意自由、Director Intent、创意探索、质量基线/QBS/Override |
| 第 5—7 章 | 生产策略、资源调度、DAG/Gate | S/A/B 与镜头等级正交、风险升级、预算、工作流、STALE、Picture Lock |
| 第 8—10 章 | 领域模型、Model Gateway、QC/治理 | ShotSpec/Previz/ControlSignal、Adapter/Route、质量/版权/安全/权限 |
| 第 11—13 章 | 资产记忆、RCA/可观测性、实施路线 | Pattern/PMS、RCA、SLO、Phase 0—4 与验收 |
| 第 14—16 章 | 工程目录、ADR、总验收 | 模块边界、数据所有权、ADR-001—024、原则—契约—验收 |
| 第 17—23 章 | 策略解耦与产品交互治理 | 项目/镜头解耦、双入口、角色卡/Canon、CEE 预算、双屏 QC、团队与 LibTV 吸收 |
| 第 24 章 | 社区与展示层 | 生产/展示画布分离、分享/发布、Showcase、举报/申诉/复用治理 |
| 第 25 章 | 视频反推 | 逐秒分镜、图像/运动提示词、关键帧、光影/音频、REFERENCE_ONLY |
| 第 26 章 | 资产与知识中枢 | 共享主体、Pattern、白模案例、版权授权、健康度和复用 |
| 第 27 章 | 客户审片与发行交付 | 只读门户、时间码批注、DeliverySpec/Manifest、EscapeReport |
| 第 28 章 | 学习与能力平台 | 作品拆解、Onboarding、课程、认证、挑战赛 |
| 第 29 章 | 开放生态与市场 | Agent/Plugin/Adapter/Template Manifest、签名、沙箱、能力公示 |
| 第 30 章 | 运营数据与基准评测 | 社区、交付、复用、反推、成本与归一化基准 |
| 第 31 章 | 文档治理与最终产品化冻结 | 唯一阶段表、Phase 1 硬门、预算、状态机、Action Registry、冻结门与负向测试 |
| 第 32 章 | 外部工具适配层与专业执行生态 | ExternalToolAdapter、工程资产、能力矩阵、回流 QC/Gate |
| 第 33 章 | 智能时间线编排与工程包导出 | TimelineDraft、ExportPackage/Manifest、代理片段预览、剪映草稿/FCPXML/EDL Profile |
| 第 34 章 | Creator Mode 个人创作者模式 | 极简 UI、CREATOR_DEFAULT_POLICY、CreatorGate/PublicationCheck/Delivery/Release、升级 FULL_MODE |
| 附录 A | 统一术语表 | 核心实体、枚举、缩写与规范性用语 |
| 附录 B | 版本修订记录 | 历史版本谱系、各历史版本修订记录与发布说明 |
| 附录 C | 工程契约、预算、权限与架构冻结参考 | Schema、Command/Event、DAG/Gate、权限矩阵、账本与自动化测试 |
| 附录 D | 产品实施、双模式计费与商业边界参考 | 用户旅程、BYOK/平台代付、Model Gateway、成本、隐私与部署边界 |

规范性用语：MUST / MUST NOT 表示系统硬约束；SHOULD 表示原则上应遵循、例外需记录理由和审批；MAY 表示可选能力。文中“目标值/建议值/示例”不是既成实现事实，也不得自动转化为生产 Gate。

## 1. 目标、范围与设计原则

### 1.1 建设目标

- 支撑项目、集数、场景、镜头、资产、任务和交付包的全链路管理。

- 将 Pre-flight、Pre-A、A、F/B/C、D、E 和 Gate 0—4 转换为可执行、可追溯、可恢复的工作流。

- 在统一质量基线下支持 S/A/B 不同资源配置，而不是不同成片质量等级。

- 建立 AI 创意探索机制，使 AI 能从导演意图出发提出多样化表达方案，并在授权范围内自主迭代。

- 建立硬约束与创意自由区分离的 Prompt/Agent 契约，避免规则污染创意表达。

- 支持镜头级风险识别、质量缺口检测、模型路由、资源升级和精准返工。

- 让每个资产可追溯到输入、模型、工作流、参数、版权来源、QC、审核和下游使用位置。

- 支持多项目并发生产、成本核算、质量观测、创意经验沉淀和持续优化。

- 将白模制作纳入 A 阶段可选子轨道，输出可复用控制信号资产，支持多路线消费、路线切换和精准返工传播。

### 1.2 非目标与边界

平台不承诺：

- 所有第三方生成模型都能像素级确定性复现。

- AI 可以独立作出全部剧作、版权、法律、制片和最终成片决策。

- 自动化艺术评分能够客观替代导演判断。

- 第一阶段即建设在线强化学习、自研基础模型或全面微服务集群。

- 通过无限重试、无限候选或无边界 Agent 自主权解决所有质量问题。

平台需要将不可自动化的判断保留在人类流程中；对不确定性、模型能力边界和资源约束进行显式管理。

### 1.3 设计原则

1. **状态以数据库为准：** 消息队列负责传递事件，不是业务状态的唯一存储。

2. **资产版本不可变：** 新生成/修复结果创建新版本，不覆盖已审核资产。

3. **任务至少一次投递 + 幂等执行：** 接受消息重复投递，以幂等键保证业务效果不重复。

4. **DAG 显式表达依赖：** 生产轨道可以并行，但不能绕过必要前置条件。

5. **人类审批是正式业务状态：** Gate 决策必须持久化、可审计、可追责。

6. **策略配置化：** QC 阈值、资源配额、创意自主权、重试策略和模型路由不散落在代码与 Prompt 中。

7. **约束与创意分离：** 事实、版权、安全和交付规格是硬约束；表达方法尽可能开放。

8. **质量不可暗降：** 资源不足触发升级、重新规划或范围协商，不触发静默降低 QC 标准。

9. **经验可复用但可突破：** 黄金资产和 Pattern Library 可被复用、变体或挑战。

10. **故障可恢复、权限最小化：** Agent 仅拥有完成当前任务所需权限；不得自行放宽版权、安全或锁定规则。

### 1.3.1 全局红线不变量（实现必须强制）

1. 权限最小化：Agent 仅持有完成当前任务所需权限，不得自行放宽版权、安全或锁定规则。

2. 审批与执行分离：申请人与批准人不得为同一主体；CRITICAL 决策必须由授权人类职责主体作出，Agent 不得代批。

3. 终态不可原地复活：终态对象不得原地恢复为可编辑状态，须通过新版本或显式 Reopen 命令处理。

4. 资产不可覆盖：已审核 / GOLD 资产不得被覆盖或改标；新结果一律创建新版本。

5. 质量不可暗降：资源不足时触发升级、重新规划或范围协商，不得静默降低 QC 标准或将未达标资产改标 PASS。

6. 密钥不落日志：凭证只存引用，日志、异常与普通任务读取路径不得出现明文密钥。

7. 法律/版权/安全不可豁免：此类红线不接受艺术性 Override 或人工豁免，证据不足时进入隔离与人工复核。

### 1.3.2 工程质量与 AI 辅助开发规则（实现必须强制）

1. 架构无屎山：分层、模块边界与数据所有权必须显式定义；禁止跨层直连、跨模块直接写表、平行重复模块与空壳透传服务；依赖方向单向收敛，新增模块先归位职责与数据所有权再实现。

2. 代码无屎山：遵循单一职责与可读可维护结构；禁止条件分支堆叠、超长过程函数、重复实现与绕开领域模型的临时特判；复杂逻辑收敛到领域模型、策略对象与声明式配置，命名与结构服务于长期维护。

3. 禁止补丁式演进：缺陷与需求必须在契约、边界或模型层根治；不得以临时绕过、平行表、特判分支、兼容分支叠加或魔法注释打补丁；现有设计无法承载时先修正设计与契约，再编码实现。

4. 保持高技术标准：不因人力、单人开发或进度压力降低选型、实现与验收标准；技术难题正面攻克，以可运行代码、自动化测试、契约与证据交付，不靠简化架构完整性或省略验证换取短期进度。

5. 全程 AI 辅助开发与生产：AI 参与编码、评审、测试、重构与生产辅助；AI 产出的代码、契约、配置与文档仍须满足同一套架构边界、红线不变量、测试与可追溯要求；架构决策、审批与最终质量由人类负责，AI 不绕过审批与权限规则。

### 1.4 六层逻辑架构

| **层** | **核心模块** | **职责** | **主要输入/输出** |
| --- | --- | --- | --- |
| L1 交互与管理 | 项目台、导演台、审核台、运营台 | 人类配置、创作选择、审阅、审批、监控 | Brief、创意选择、Review、Gate |
| L2 编排与 Agent | Orchestrator、DAG、状态机、Agent Runtime、Creative Exploration Engine | 任务分解、依赖控制、创意探索、状态推进、人工接管 | WorkflowRun、Task、CreativeProposal、Event |
| L3 生成与执行 | Model Gateway、ComfyUI Adapter、TTS/Video/Image Adapter、ExternalToolAdapter（含 EditingToolAdapter、AudioToolAdapter、3D/VFXToolAdapter）、Worker | 调用模型和工作流；连接外部专业工具执行环境；统一导出/导入、元数据同步和回调接收；回传结果进入平台资产暂存与验证。 | ModelInvocation、ToolExchangePackage、ExternalToolArtifact、AssetVersion |
| L4 数据与资产 | Project DB、Asset Service、对象存储、向量索引、Pattern Library、工程资产索引 | 主数据、元数据、文件、版本、血缘、创作经验；统一管理外部工程文件、时间线交换文件和成片回流版本，不另建剪辑素材库。 | Project、Shot、Asset、AssetVersion、ExternalToolConnection、ToolExchangeSession、Pattern |
| L5 质量与治理 | QC Engine、Review、Gate、Policy、Compliance | 技术/视觉/连续性/艺术评估、版权安全拦截 | QCReport、Review、GateDecision |
| L6 可观测与运营 | Logs、Metrics、Tracing、Cost、Quality Analytics、Adapter Health、Alert | 产能、质量、创意价值、成本、外部工具连接健康度、适配能力验证状态、回流成功率和故障观测。 | 指标、告警、成本账本、AdapterVerificationEvidence |

横切能力：身份认证、RBAC/ABAC、多租户隔离、密钥管理、审计日志、配置版本、数据保留策略。

### 1.5 部署建议

初期采用**模块化单体 + 异步 Worker**：

- `api`：鉴权、请求校验、领域命令入口。

- `orchestrator`：WorkflowRun、Task、Dependency、Gate 状态推进。

- `agent-runtime`：执行 Skills、结构化计划和工具权限控制。

- `creative-exploration`：候选方案生成、差异分析、创意挑战与导演反馈回流。

- `generation-worker`：调用图像/视频/音频/ComfyUI 适配器。

- `qc-worker`：执行自动检测并生成 QCReport。

- `asset-service`：登记资产、版本、元数据和存储位置。

- `notification-worker`：发送飞书/邮件/站内通知。

模块先在一个代码仓库内独立维护。达到吞吐、隔离或可用性门槛后，再拆分独立部署，不以微服务数量作为架构成熟度指标。

### 1.6 建议技术基线

| **能力** | **建议基线** | **说明** |
| --- | --- | --- |
| API | Python + FastAPI 或同类框架 | OpenAPI 契约优先 |
| 主数据库 | PostgreSQL | 项目、任务、审核、资产元数据、审计 |
| 文件存储 | S3 兼容对象存储 | 视频、图像、音频、代理文件和快照 |
| 缓存/短队列 | Redis（可选） | 缓存、限流、轻量队列，不作为唯一状态源 |
| 持久化工作流 | Temporal/同类 Durable Workflow，或自建状态机 | 支持重试、超时、恢复、人工等待 |
| 异步事件 | RabbitMQ/NATS/Kafka 按规模选择 | 通过 Outbox 保证事务一致性 |
| 生成执行 | ComfyUI + 各模型服务 Adapter | 统一输入输出契约 |
| 可观测 | OpenTelemetry 兼容链路 | 统一 trace_id、job_id、project_id |

### 1.7 统一治理强度不等于阈值一刀切

统一高品质的“统一”包含统一的评审流程、严重级别定义、证据要求、责任追溯、门禁原则和最低不可绕过红线；项目可以依据内容类型、交付平台和技术规格配置校准阈值，但每次校准必须版本化、说明适用范围并经治理角色审批。项目阈值不同不意味着可以直接进行未经归一化的横向质量排名。

### 1.8 不可达目标的显式仲裁

当现有资源无法满足当前质量目标时，系统必须形成 Resource Gap Report，并由制片/项目负责人和导演共同选择：增加预算或资源、延长周期、缩小交付范围、调整非核心制作方案，或在法律/安全/版权和关键技术红线之外接受一项明确记录的偏差。AI 和调度器不得自行将未达标资产改标为 PASS；对偏差的接受必须有授权人、适用范围、期限和下游影响记录。

项目级与镜头级策略正交：项目级 S/A/B 定义项目组织、预算边界、审批制度和默认生产策略；镜头级 S/A/B 定义单镜头的制作深度、创意探索、路线和资源投入。项目等级不得成为镜头等级上限，镜头等级变化不得自动改变项目等级。详见第 17 章。

## 2. 核心创作治理模型：硬约束与创意自由

### 2.1 双层约束模型

每个项目、场景和镜头都必须将约束分为两类。不能把所有创作要求都塞进同一个“不可变规则”集合。

### A. Hard Constraints（硬约束区）

硬约束用于保护故事事实、项目意图、连续性、版权安全和交付可用性。典型内容：

- 剧本中已经锁定的核心事件、人物关系、动机和因果关系。

- 角色身份、核心人设、年龄设定（如项目明确）、关键服饰/标志性特征。

- 世界观规则、时间线、空间关系、道具状态和明确的场景连续性要求。

- 导演/制片已批准并锁定的创作决策、镜头功能和叙事信息。

- 版权、肖像、商标、音乐、素材来源和模型使用许可边界。

- 数据安全、隐私、平台安全规范和项目合规要求。

- 项目交付规格：分辨率、帧率、时长、画幅、音频、字幕、文件格式等。

硬约束必须有来源、责任人、版本和适用范围。存在冲突时，系统不得自行猜测优先级，应提出冲突并请求有权限的人类裁决。

**白模锁定规则：** 经导演/授权负责人确认的 `BASELINE` 型 Previz，可将相机内外参、相机路径、主体走位、景别变化节点、切镜时间点及明确锁定的空间关系写入带版本的 ConstraintPack。AI 不得擅自偏离这些已锁定维度；任何偏离必须走变更授权。`EXPLORATORY` 型 Previz 只作为创意比较材料，不写入硬约束。白模本身不得自动把未被导演确认的推断升级为硬约束。

### B. Creative Freedom Zone（创意自由区）

除明确锁定的内容外，AI 可以探索并提出不同表达方式，包括但不限于：

- 镜头景别、构图、机位、镜头运动、焦段感和景深。

- 光影方向、色彩方案、天气、环境氛围和空间细节。

- 演员微表情、动作节奏、停顿、视线、身体语言。

- 环境叙事、视觉隐喻、象征性道具和非对白叙事。

- 转场、剪辑节奏、镜头衔接、画面留白和声音设计建议。

- 特效表现路径、材质、粒子、烟雾、水火、合成方式。

- Prompt 优化、模型选择、工作流组合、LoRA/ControlNet 等技术路径。

创意自由不是无限制生成。它要求输出仍服务于导演意图、叙事功能和质量目标；但不能因为历史模板、默认 Prompt 或参数预设而禁止有价值的新方案。

**白模对创意自由区的边界：** 未锁定时，构图、机位、镜头运动和走位均可作为开放创意维度；一旦进入经批准的 BASELINE 锁定版本，只有被明确列入 `locked_dimensions` 的维度退出创意自由区。材质、最终外观、光影风格、表演细节和精细物理默认仍开放，除非导演另行明确锁定。

### 2.2 约束来源与优先级

建议优先级从高到低：

1. 法律、版权、隐私、安全和平台政策要求。

2. 项目级明确且已批准的硬约束。

3. 已锁定的剧本、角色设定、世界观和时间线事实。

4. 导演针对当前镜头/场景批准的创作决策。

5. 项目风格指南和视觉基线。

6. Pattern Library、黄金资产和历史经验建议。

7. Agent 的临时推断与默认模板。

低优先级内容不得覆盖高优先级内容。Pattern Library 不是硬约束来源；AI 可以说明为何偏离某个历史模式。

### 2.3 Creative Autonomy 等级

自主权按**任务/镜头**授权，与 S/A/B 项目等级彻底解耦。项目可以设置一个独立于 S/A/B 的创作授权默认值作为初始值，但导演可按镜头单独升降；该默认值不得由项目等级自动推导。

| **等级** | **名称** | **AI 权限** | **人类介入** |
| --- | --- | --- | --- |
| L1 | 指令执行 | 按明确方案执行；可提出风险提醒，不擅自改创意方向 | 人类定义方案并验收 |
| L2 | 表达自主 | 在硬约束和导演意图内自主决定构图、运镜、光影、表演等表现方式 | 人类审核关键结果 |
| L3 | 多方案探索 | 主动生成不同创意路径、比较优劣并推荐方案 | 人类选择/组合/修改 |
| L4 | 主动挑战 | 可以质疑当前方案，提出反直觉替代、实验性方案和不同叙事表达 | 人类批准挑战方向和锁定结果 |

规则：

- 自主权等级定义的是 AI 可以做什么，不是质量要求高低。

- L3/L4 不代表可以改写硬约束或绕过 Gate。

- AI 对核心事实、角色身份、已锁定剧本和 Picture Lock 的修改必须请求授权。

- 导演可以对单个镜头临时升降自主权等级，并记录理由、范围和有效期。

- 低风险、重复性执行可采用 L1/L2；创意不确定性高或关键视觉段落可采用 L3/L4。

白模授权规则：探索型白模的构建和多方案比较可按镜头授权 L2/L3；L4 可用于提出主动挑战方案，但仍受探索资源档位约束。基线型白模的批准与锁定属于导演/被委托授权负责人的权限，不由 AI 自主执行。锁定后修改相机或走位属于修改已锁定决策，必须请求授权。

### 2.4 创意提案对象

每个创意提案（CreativeProposal）至少包含：

- `proposal_id`、`project_id`、`episode_id`、`scene_id`、`shot_id`、`shot_revision`。

- `intent_summary`：该镜头要传达的叙事/情绪/信息目标。

- `hard_constraints_snapshot`：提案生成时使用的硬约束快照。

- `creative_direction`：方案方向、视觉语言和表达假设。

- `visual_plan`：构图、运镜、光线、色彩、表演、环境、特效等建议字段。

- `prompt_manifest`：Prompt 版本、模型、工作流、参数和输入资产引用。

- `difference_from_baseline`：相对当前方案的差异。

- `risk_assessment`：事实偏离风险、连续性风险、技术风险、版权风险。

- `expected_quality_gain`、`estimated_cost`、`uncertainty`。

- `status`：DRAFT / PRECHECKED / CANDIDATE / SELECTED / REJECTED / ARCHIVED。

- `created_by`、`reviewed_by`、`decision_reason`、时间戳。

### 2.5 导演意图理解引擎（Director Intent Engine）

Director Intent Engine（DIE）将导演、编剧或制片的自然语言要求转化为可追溯的 Intent Contract。它不是把情绪词直接翻译成唯一 Prompt，也不替导演决定最终艺术方向。

DIE 必须区分：

- `EXPLICIT_INTENT`：导演明确表达的目标或禁区；

- `INFERRED_ASSUMPTION`：AI 根据上下文作出的推断；

- `AI_PROPOSAL`：AI 主动提出的表达建议；

- `OPEN_QUESTION`：缺少信息且会影响生产的关键歧义。

Intent Contract 最少字段：

```
intent_id: intent_...
scope: {project_id: prj_..., episode_id: ep_..., scene_id: sc_..., shot_id: sh_...}
source: {author_id: user_..., source_text: "人物显得渺小，有孤独感，但不要过度悲伤", source_revision: 2}
interpretation:
  narrative_purpose: "强调人物与环境的尺度关系"
  emotional_targets: ["孤独", "克制"]
  avoid: ["煽情", "绝望感过强"]
  audience_effect: "感受到人物处境，但仍保留希望"
  confidence: 0.82
assumptions:
  - text: "人物暂时独处"
    confidence: 0.65
    requires_confirmation: false
hard_constraints: {facts: [], continuity_refs: [], prohibited_changes: []}
creative_space: {open_dimensions: [composition, camera_movement, lighting, color, performance]}
approval: {status: DRAFT, approved_by: null, approved_at: null}
```

#### 2.5.1 置信度与确认策略

置信度阈值由治理配置按意图类型校准，不得把模型自报置信度当成真实概率。建议初始分层如下，需在试点中验证后冻结：

- `>= 0.90`：低影响解释可自动进入草稿态；不得因此自动锁定剧情事实或授权变更。

- `0.70–0.89`：记录假设及来源，可继续低风险预演；高风险镜头需导演确认。

- `< 0.70`：标记 `NEEDS_CLARIFICATION`。若歧义涉及剧情事实、人物关系、版权、关键连续性或交付要求，则阻塞相关生产任务；若仅影响可逆的低风险表达探索，可在明确标记假设的前提下进行低成本探索。

导演超时未响应时，不得把“未回复”解释为批准。任务按风险进入 `WAITING_FOR_HUMAN`、低风险 `CONTINUE_WITH_ASSUMPTION` 或取消/重规划。假设后来被证实错误时，通过 Intent 版本、假设 ID、引用任务和审批记录追溯影响范围；责任归属依据实际授权和决策记录，不自动归咎于提出假设的 AI。

#### 2.5.2 意图解析和冲突处理

流程：Parse → Ground（绑定项目圣经、记忆和镜头上下文）→ Separate（事实/偏好/推断/开放空间）→ Detect Conflict → Clarify → Contract → Confirm → Propagate。法律、安全、版权和平台强制规则优先于项目偏好；同级冲突优先采用更具体、更新且明确批准的版本。无法判断时进入 `NEEDS_CLARIFICATION`，不得静默覆盖。

#### 2.5.3 自主权不等于审批权

L1-L4 只定义 AI 的创意提案、候选生成和授权范围内草稿修改能力，不自动授予 `APPROVE`、`OVERRIDE` 或 `PUBLISH` 权限。即使 L4，也不得擅自批准 Gate、修改锁定事实、作版权豁免或发布交付包。提案、生成、修改、审批、发布是五种独立权限，按第 10 章权限矩阵执行。

## 3. AI 创意探索引擎（Creative Exploration Engine）

### 3.1 定位

Creative Exploration Engine（CEE）负责将导演意图转化为可探索、可比较、可回溯的创意方案。它不是固定模板生成器，也不是无限制的“多抽几次卡”。它要在创意价值、质量概率、技术可行性、成本和时间之间进行透明权衡。

### 3.2 标准流程

1. **Intent Parse：** 解析导演意图、镜头叙事功能、情绪目标和观众需要获取的信息。

2. **Constraint Snapshot：** 获取当前有效的硬约束、已锁定决策、连续性资产和交付要求。

3. **Creative Divergence：** 生成多种差异化方案，而非只改 Prompt 形容词。

4. **Preflight Evaluation：** 检查事实冲突、版权安全、连续性、模型能力和技术可行性。

5. **Candidate Generation：** 对可行候选执行小样/关键帧/低成本预演或完整生成。

6. **Comparative Review：** 对候选的叙事有效性、视觉差异、质量风险、成本和可扩展性进行比较。

7. **Human Direction：** 导演选择、组合、修改、要求继续探索或拒绝。

8. **Decision Capture：** 将选择理由和有效范围记录为决策，不把一次选择无条件推广到整个项目。

9. **Pattern Extraction：** 将可复用经验抽取为 Pattern，附带适用条件和失败边界。

候选数量不应写死为固定的 3 个或 5 个。由创意不确定性、镜头风险、预算、截止时间和已有证据共同决定。简单镜头可以直接执行；高价值高不确定性镜头应增加探索深度。

### 3.3 创意差异度

候选方案需要有真实差异，而不是只改变颜色词或 Prompt 顺序。差异维度可包括：

- 叙事表达：直接呈现、环境叙事、动作暗示、留白、象征。

- 镜头语言：固定机位、跟拍、推拉摇移、长镜头、主观视角等。

- 视觉风格：写实、风格化、极简、超现实、纪实感等（受项目风格边界约束）。

- 表演调度：动作、停顿、视线、肢体重心和情绪层次。

- 声画关系：对白驱动、声音先行、环境音叙事、音乐反差等。

- 制作路径：单模型、多模型、2D/3D 混合、实拍素材合成等。

系统应识别“伪多样性”：若候选在关键表达维度上高度相似，应提示继续发散或减少重复候选。

**白模作为创意比较载体：** 同一镜头的多个 `EXPLORATORY` Previz 方案，可在相机路径、主体走位、空间阻挡、景别节点和节奏维度形成结构化差异证据。差异度检测在 Phase 0/1 以结构化字段对照和人工差异卡为主，不以未经验证的视频语义相似度自动淘汰方案。

### 3.4 AI Creative Challenge

AI 可以主动提出挑战，例如：

- “该信息是否可以通过环境细节表达，而不依赖对白？”

- “是否可以用长镜头保持空间关系，而不是连续切镜？”

- “是否可以通过光影变化呈现人物情绪，而非直接使用表情特写？”

- “当前镜头是否过度解释，能否用留白增强观众参与？”

- “是否存在更低成本但不损失艺术效果的技术路径？”

挑战提案必须包含：当前方案的问题假设、替代方案、预期收益、风险、所触及的硬约束和需要的人类授权。AI 不得把“挑战”直接执行为对已锁定剧本或导演决策的覆盖。

### 3.5 停止条件与探索熔断

探索应在以下情况停止或转人工：

- 已找到满足硬约束和质量目标、且在当前预算/时间内具有合理收益的方案。

- 新候选的边际收益持续低于项目设定阈值。

- 候选高度重复，或模型能力不足以继续改善。

- 预算/时间接近探索上限。

- 发现版权、安全或事实冲突。

**创意熔断与质量熔断分离：** 创意熔断只暂停当前探索分支，可保留已通过方案继续生产；质量熔断则针对严重技术、版权、安全或关键连续性问题暂停对应资产/镜头/交付路径。两者不能混为一个“失败”状态。

白模几何体检失败（例如主体出画、相机穿模、画幅/时长不匹配或关键切点错位）时，必须回到 ShotSpec/分镜或 Blocking 阶段修正，不得进入付费生成模型调用。几何体检只证明已检查的空间/时间条件通过，不证明最终成片质量或模型必然服从控制信号。

### 3.6 创意差异度的可实现边界

Phase 0/1 不把叙事表达、镜头语言、表演调度或声画关系的语义差异度作为自动判定能力。第一阶段采用人工辅助差异卡：为候选分别列出叙事路径、镜头语言、视觉组织、表演动作、声画关系和制作路径的变化点，由导演/评审勾选“实质差异/表面变化/未知”。向量相似度或多模态模型只能提供候选聚类提示，不能单独判定伪多样性。

自动差异检测只有在冻结基准集、分层样本和人工标注验证通过后，才能提升为辅助路由；未达性能门槛时必须标记 `EVIDENCE_ONLY`，不得阻断候选或淘汰创意。

### 3.7 创意停止原因分类

- `CREATIVE_FATIGUE`：候选重复、边际收益不足、预算/时间触顶；只暂停当前探索分支，不影响已批准的生产分支。

- `CREATIVE_GOVERNANCE_HIT`：发现版权、隐私、安全、平台政策或锁定事实冲突；必须同时触发相应治理门禁，对相关素材进入 `QUARANTINED` 或阻断状态，并沿依赖关系评估下游影响。

- `CREATIVE_TECHNICAL_LIMIT`：模型/工作流暂不具备实现能力；触发替代制作路径或资源升级，不等同于合规违规。

“创意熔断与质量熔断分离”不意味着治理命中可以只关闭创意分支而放行受影响资产。

## 4. 统一高品质体系与质量基准（Quality Constitution & Benchmark）

### 4.1 统一质量基线

所有 S/A/B 项目都必须绑定同一个逻辑质量配置：`UNIFIED_HIGH_QUALITY`。不同项目可以根据成片类型、平台、画幅、帧率和客户要求配置具体技术规格，但不能因为项目等级较低而放宽已经确认的质量要求。

质量基线分为：

- **不可绕过红线：** 版权/授权、安全、文件完整性、交付规格、严重技术缺陷、明确锁定的关键事实错误。

- **项目校准阈值：** 清晰度、闪烁、人物一致性、音频响度、色彩偏差等，需要结合内容类型和目标平台校准。

- **艺术判断项：** 表演感染力、构图意图、节奏、情绪、视觉独特性等，AI 只能提供证据与建议，导演/授权评审者作最终判断。

统一基线不是所有镜头必须长得一样，也不是所有艺术指标都要达到单一数值。它保证每个镜头都经过同等严肃的质量判断，并对偏差有记录、有责任人、有处置结果。

### 支撑资产质量基线（SUPPORTING_ASSET_QUALITY）

白模、Previz、CameraRig 和 ControlSignalAsset 属于**支撑资产**，不直接套用成片级 `UNIFIED_HIGH_QUALITY`。支撑资产使用独立的 `SUPPORTING_ASSET_QUALITY`，至少检查技术完整性、空间/几何正确性、相机参数有效性、时间码一致性、控制信号规格匹配和版权许可。支撑资产的视觉风格/艺术表现只作为 `OBSERVATION`，不因“白模不够像成片”而阻断；但主体出画、相机穿模、时间码错位、信号不匹配、纯净度失败或版权授权未知必须阻止其作为控制信号下发。支撑资产通过不代表最终成片通过，最终成片仍必须执行 `UNIFIED_HIGH_QUALITY`。

### 4.2 五维质量模型

| **维度** | **评价内容** | **自动化适用度** | **处置原则** |
| --- | --- | --- | --- |
| 技术质量 | 分辨率、帧率、解码、黑帧、闪烁、噪点、音画同步、响度、文件完整性 | 高 | 硬阈值/确定性规则优先；关键失败阻断 |
| 视觉质量 | 构图、光影、色彩、材质、特效完成度、画面信息层级 | 低至中 | 先查 Intent/CreativeProposal；主观偏差默认呈现证据并人工判定，不以历史 Pattern 为唯一标准 |
| 连续性质量 | 角色身份、服装、道具、场景、空间关系、时间线、动作衔接 | 中（按镜头子类验证） | 仅在对应子类达到基准性能后作为自动分流证据；近景、多人、遮挡、快速运动和关键状态变化强制人工复核 |
| 艺术质量 | 表演、情绪、叙事清晰度、镜头语言、节奏、感染力、独特性 | 低 | 默认只输出 OBSERVATION 级证据与问题提示，不参与 PASS/FAIL 或自动阻断 |
| 控制质量 | 相机轨迹、主体走位、景别变化节点、切镜时间点、时间节奏，以及生成结果对已授权控制信号的符合程度 | 中（按信号类型/路线/镜头子类验证） | 有可用结构化信号时对照 Manifest；未达到检测器性能门槛时仅呈证据并人工复核；控制维度通过不豁免外观验收 |

另设横向治理检查：版权与授权、安全、隐私、平台规范和交付合规。治理检查不是第五个可打分维度，而是独立的准入门槛。

控制质量是按需启用的第五维：仅当镜头引用了 ControlSignalAsset，或 ShotSpec 明确锁定相机/走位/节奏约束时评估。控制维度与视觉/外观维度分别判定；控制通过不能豁免角色身份、材质、光影、表演和最终画面质量验收，外观通过也不能豁免明确锁定的相机/走位约束。控制检测器必须遵守第 4.8 节的基准和决策等级，未经验证的相机/运动估计只能输出证据。

### 4.3 QC 严重级别

- `CRITICAL`：版权/安全/隐私重大风险、文件损坏、交付不可用、关键事实冲突、明确红线违反。必须阻断，不能通过普通 Override 绕过。

- `MAJOR`：明显影响观众理解或成片质量的问题，如角色错误、严重闪烁、关键动作失败、音画错位。修复或经授权作出有证据的处置。

- `MINOR`：局部瑕疵，不显著影响叙事或观看体验。可进入修复队列或经导演记录理由接受。

- `OBSERVATION`：建议性观察、创意差异、可选优化，不作为阻断项。

任何豁免都必须记录问题、影响范围、决策者、理由、证据、有效期限和下游传播范围。版权、安全等法律/政策红线不得以艺术理由豁免。

### 4.4 艺术性 Override

允许导演保留具有明确艺术价值、但自动评分不占优的镜头，条件是：

- 不违反版权、安全、隐私和交付硬红线。

- 不破坏项目锁定事实或关键连续性要求；若偏离，必须获得相应授权。

- 明确记录艺术意图、接受的缺陷和潜在影响。

- 经过有权限的导演/艺术负责人确认。

- 对需要技术修复的部分仍建立独立任务，不得以艺术性为由掩盖可修复的技术问题。

### 4.5 质量门禁

建议每个镜头至少经过：

1. 文件与技术完整性检查。

2. 视觉与连续性检查（自动 + 人工按风险配置）。

3. 音频检查（若镜头包含音频或进入时间线）。

4. 版权/安全/来源检查（按素材和使用场景执行，交付前必须完整）。

5. 镜头意图与艺术效果评审（导演或授权评审者负责）。

低风险镜头可复用已经验证的检测配置和黄金工作流，减少重复操作；不得跳过必要质量门禁。

### 4.6 Quality Benchmark System（QBS）

QBS 将“高品质”落到版本化参考集、缺陷定义、评审量表和检测器校准记录上。它统一质量治理强度，不统一审美风格或所有项目的数值阈值。

Benchmark 分层：

- `TECHNICAL`：编码、分辨率、帧率、色彩空间、响度、黑帧、解码稳定性；

- `IDENTITY`：人物身份、面部/体态特征、服装、发型和关键道具；

- `SCENE`：空间布局、环境状态、天气和光源条件；

- `MOTION`：动作连贯、结构稳定、交互和时间一致性；

- `VISUAL`：构图、光影、色彩、景深、材质和特效完成度；

- `NARRATIVE_PERFORMANCE`：表演可信度、叙事清晰度、节奏和情绪表达；

- `COMPLIANCE`：授权、肖像、商标、音乐来源、隐私和平台规则。

每套基准集必须有样本来源、授权证明、适用项目/镜头子类、标注指南、标注者、争议处理记录、版本、冻结日期和维护责任人。基准资产包括 Reference Reel、角色/场景 Canon Pack、Motion Test Set、Defect Catalog 和通过/条件通过/返工/拒绝的 Acceptance Examples。关键标签采用双人独立标注并对分歧进行仲裁；记录标注一致性和争议率。样本量不足或标注一致性不足时，不得用于自动门禁。不得把来源不明或未获授权素材加入基准集。

### 4.7 Intent-aware QC：先理解意图，再解释偏差

视觉检测器在对构图、色彩、光影、景深或风格偏差作出“缺陷”判断前，必须读取该镜头当前版本的 Intent Contract、CreativeProposal 和 ConstraintPack。QCReport 必须包含：

- `deviation_intent_ref`：对应的 Intent Contract / CreativeProposal 版本引用；

- `deviation_class`：`INTENTIONAL` / `UNINTENTIONAL` / `UNCERTAIN`；

- `intent_match_status`：`MATCH` / `CONFLICT` / `NOT_ASSESSED`；

- `evidence_refs`：具体帧、区域、时间码或对照资产；

- `detector_version`、`benchmark_version`、`confidence` 和适用子类。

对于 L3/L4 授权镜头，主观视觉偏离默认降级为“证据呈现 + 人工判断”，不得仅因偏离黄金资产或历史 Pattern 而自动阻断。若检测到的是确定性技术缺陷（例如编码损坏、画面冻结、音画失步）或明确硬约束冲突，仍按相应严重级别处理。意图记录缺失时，不能把未知偏差自动认定为有意或缺陷，应进入 `UNCERTAIN` 并补充人工判定。

### 4.8 检测器上线门槛与决策权限

检测器注册时必须声明任务类型、适用子类、训练/验证数据来源、版本、性能指标、置信区间、失效条件、输出用途和允许的决策权限。采用以下等级：

- `EVIDENCE_ONLY`：只显示证据，不参与路由或门禁；默认级别。

- `ADVISORY`：提供建议，可创建人工复核任务，不改变资产状态。

- `AUTO_ROUTE`：可按已验证规则把任务送往复核/返工队列，不直接批准或拒绝资产。

- `AUTO_BLOCK`：仅限确定性规则或经过严格基准验证的高置信检测器，且仅能阻断其明确覆盖的缺陷类型。

建议作为初始发布门槛（不是对当前检测器性能的宣称）：关键阻断类检测器在冻结基准集上的 Precision 不低于 99%、Recall 不低于 95%；连续性身份检测器按镜头子类分别达到 Precision 不低于 95%、Recall 不低于 90%，并同时报告 95% 置信区间。若样本量不足、置信区间下界未达到门槛、某个子类表现不达标或分布漂移，检测器自动降级为 `EVIDENCE_ONLY/ADVISORY`。上述数值须通过试点和风险评估校准；每个拟上线自动决策的镜头子类建议至少具备 200 个正例和 200 个负例的冻结评测样本，并报告 95% 置信区间。样本量不足或标签一致性不足时只能证据呈现；不能以总体平均值掩盖近景、遮挡、多人和快速运动等困难子类。

艺术质量、情绪识别和创意差异度默认采用 EVIDENCE_ONLY，不参与 PASS/FAIL。连续性检测分数低于阈值、置信度不足、输入超出支持范围、出现新角色/服装状态或高风险标签时，必须触发人工复核。B 级精简流程不得省略高风险镜头的一致性人工复核。

### 4.9 校准阈值治理与跨项目可比性

项目阈值变更必须由治理管理员审批，记录变更原因、影响的镜头子类、校准集版本、旧/新阈值、验证结果、生效日期和回滚版本。项目 KPI 必须展示 `quality_profile_version`、`benchmark_version`、`detector_version` 和阈值配置版本。不同版本或不同适用范围的 First Pass Rate、Rework Rate 不得直接做未经归一化的横向排名；需要比较时先按共同基准集或分层归一化口径计算。

### 4.10 艺术性 Override 的限额、累积效应与复核

艺术性 Override 仅适用于不触及法律、版权、安全、隐私、技术交付红线的非阻断问题。每次记录 `override_id`、镜头、QC 缺陷、Intent 引用、接受理由、风险、决策者、审批时间、有效范围和后续复核结果。CRITICAL 不得通过艺术 Override 绕过。

运营指标至少包括：

- `Override Rate = 获批 Override 的送审镜头数 / 同期已完成 QC 送审镜头数`；

- 按严重级别加权的 `Weighted Override Rate`；

- 同类缺陷重复 Override 率；

- Override 后被下游发现/交付逃逸的问题率。

建议试点默认阈值：Override Rate 达 5% 发出预警，达到 10% 触发项目治理复核；该值是治理触发器，不是自动拒绝比例，须在积累至少 3 个项目或 100 个送审镜头后校准。即使未达到比例阈值，若同类 MAJOR 问题连续 Override 或发生质量逃逸，也必须专项复核。治理复核应判断是检测器误报、质量标准不适配、导演有意风格偏离，还是质量门禁被系统性架空。

## 5. S/A/B 统一品质与资源策略

### 5.1 核心定义

S/A/B 是**生产资源与治理策略档位**，不是质量档位，也不是创意自由档位。任何等级都必须满足 `UNIFIED_HIGH_QUALITY`。

**强制解耦规则：** `project.level` 与 `creative_autonomy.level` 是两个独立字段，禁止配置继承、默认映射或隐式联动。S/A/B 策略包不得包含 `creative_autonomy_default: Lx` 这类等级绑定字段；镜头任务必须显式记录授权来源、授权人、级别、作用范围和有效期。缺少授权时采用项目独立的安全默认策略，不得根据 S/A/B 自动推断。

| **维度** | **S：旗舰探索型** | **A：高品质商业生产** | **B：敏捷高品质生产** |
| --- | --- | --- | --- |
| 最终质量基线 | 统一高品质 | 统一高品质 | 统一高品质 |
| 创意自主权 | 与 S/A/B 解耦；镜头可独立授权 L1-L4 | 与 S/A/B 解耦；镜头可独立授权 L1-L4 | 与 S/A/B 解耦；镜头可独立授权 L1-L4 |
| 模型探索 | 多模型、多工作流并行验证 | 优先成熟模型，关键镜头多路比较 | 优先稳定高质量路径，允许按需探索 |
| 资产策略 | 专属资产与深度定制 | 黄金资产复用 + 定向定制 | 高质量资产复用 + 允许创新变体 |
| 人工审核 | 高强度、导演深度参与 | 风险驱动审核 | 精简流程但保留必要审片和终审 |
| F-QC | 复杂/高风险镜头全量或专项验证 | 高风险强制，中风险按证据抽样 | 高风险强制，触发升级即执行 |
| 资源弹性 | 高资源池和探索储备 | 按项目目标动态配置 | 基础资源精益配置，风险镜头可升级 |
| 交付验收 | 统一基线 + 项目专项项 | 统一基线 + 项目专项项 | 统一基线 + 项目专项项 |
| 白模/Previz 策略 | 高风险镜头强制基线型 Previz；关键镜头可多方案探索 | 高风险镜头强制，中风险按风险与路线能力选择 | 优先复用合规白模库；高风险触发升级申请，不得跳过必要几何/控制验证 |

### 5.2 各等级策略

### S：旗舰探索型

- 适合高艺术探索、高视觉复杂度、高品牌影响或高不确定性项目。

- 可为适合探索的镜头授权 L3/L4 创意挑战，并采用多模型、多工作流、多路径对照；不因 S 级身份自动授予更高自主权。

- 对核心角色、复杂动作、关键特效和重要场景进行专项验证。

- 可配置专属视觉资产、风格适配和精细后期。

- 采用高强度导演评审和完整创意决策记录。

### A：高品质商业生产

- 适合具有明确交付目标、质量要求高且需要控制成本/周期的项目。

- 优先采用经验证的模型、工作流和黄金资产，但允许偏离模板提出更优方案。

- 按镜头授权、风险和创意不确定性分配探索深度；不因 A 级身份自动限制或提高自主权。

- 关键镜头由导演/艺术负责人重点审阅；一般镜头可用自动化证据辅助审阅。

### B：敏捷高品质生产

- 适合周期紧、规模可控、需要稳定交付的项目。

- 通过高质量资产复用、成熟工作流、自动化编排减少重复劳动。

- 不得将“B 级”解释为低创意自由度、低画质、低一致性或省略必要 QC。

- 当镜头复杂度、质量缺口或创意价值超出基础配置时，自动提出镜头级升级建议；B 级镜头同样可以获得 L3/L4 授权。

- 可以减少非必要的重复审批，但必须保留必要质量检查、版权/安全检查和最终交付验收。

### 5.3 配置字段建议

```
strategy_id: B_AGILE_HIGH_QUALITY_V1
level: B
quality_profile: UNIFIED_HIGH_QUALITY
creative_autonomy:
  authority_source: director_or_delegated_owner
  scope: shot_or_task
  project_tier_coupling: forbidden
  allowed_levels: [L1, L2, L3, L4]
resource_profile:
  concurrency_limit: configurable
  gpu_budget_units: configurable
  exploration_reserve_ratio: configurable
  quality_reserve_ratio: configurable
workflow_profile:
  reuse_golden_assets: preferred
  high_risk_sample: mandatory
  creative_exploration: enabled
  internal_gray_release: risk_driven
  final_qc: mandatory
  rights_security_gate: non_bypassable
approval_profile: streamlined_but_auditable
retry_profile: bounded_quality_aware
fallback_profile: quality_equivalent_or_escalate
```

所有数值必须由真实项目成本、时延和质量数据校准。配置不能提供 `disable_final_qc`、`lower_quality_for_B` 等绕过项。

### 5.4 L4 授权与资源强度分离

L4 是 AI 的创意权限，不代表必须使用多模型并行、无限候选或最高算力。把自主权和探索资源拆为两个字段：`creative_autonomy_level`（L1-L4）与 `exploration_profile`（COMPACT / STANDARD / EXPANDED）。资源配置不能反向改变已批准的自主权等级。

- `COMPACT`：单一主模型或单工作流，多参数/多表达路径探索；限制候选数量和单次预算，但必须允许 Agent 提出真正不同的挑战方案。

- `STANDARD`：多个候选路径，按需要使用第二模型或辅助控制工作流。

- `EXPANDED`：多模型、多工作流、小样对照、较高算力和导演深度参与。

B 级镜头被授权 L4 时，默认先估算 `COMPACT` 的资源需求并触发 `policy/escalations`。若授权目标明确要求多模型/大样本，系统必须申请资源追加。资源不足时，状态进入 `WAITING_FOR_RESOURCE`，由导演/制片选择追加资源、确认缩小探索范围（仍保留 L4 的主动挑战权限）或调整周期；不得静默把 L4 改成 L2/L3。任何探索范围缩小都需记录确认人和影响。

策略解耦补充：本章的镜头级动态资源调度以 ShotProductionProfile 为执行对象；项目级 ProductionProfile 只提供默认值、预算资源池和治理边界。具体策略解析、镜头独立预算及升级工作流见第 17 章。

## 6. 镜头级风险与动态资源调度

### 6.1 两级调度

- 项目级调度： S/A/B 确定基础算力池、并发、人工审核策略、Production Budget、Exploration Reserve、Quality Reserve 与 Contingency Reserve 四类预算账户。

- **镜头级调度：** 根据风险、创意不确定性、质量缺口、历史失败率和交付紧迫度，动态升级模型、算力、候选数量、人工介入和后期路径。

### 6.2 镜头风险评分维度

风险由规则和 Agent 联合提出，保留原因与置信度：

- 人物：近景、多人、遮挡、快速表情、角色数量、身份混淆风险。

- 动作：复杂肢体、交互、连续动作、道具接触、物理合理性。

- 场景：高细节、复杂运动、镜头运动、跨场景连续性。

- 特效：粒子、烟雾、水、火、反射、透明材质和复杂合成。

- 生产：新模型、新工作流、未知参数组合、历史失败率和适配成熟度。

- 创意：表达目标不清晰、方案差异大、导演意图存在多种解释。

- 合规：肖像、商标、音乐、受限素材或授权范围不明。

风险级别可定义 LOW / MEDIUM / HIGH / CRITICAL。风险不是质量分数；高风险意味着需要更多验证，不意味着镜头必然低质量。

- **白模/控制风险：** 空间关系复杂、相机运动复杂、多角色走位、跨镜头/跨场景连续性、控制信号与目标路线能力不匹配、3D 资产许可不明确或 Previz 基线变更。

### 6.3 动态升级触发器

满足任一条件即可触发镜头升级评估：

- 自动 QC 出现 MAJOR 或重复 MINOR。

- 角色/场景/道具一致性低于项目校准阈值。

- 当前模型在该类镜头的历史首过率低于阈值。

- 导演认为当前表达缺乏感染力或偏离创作意图。

- 镜头被标记为叙事关键、视觉记忆点或营销核心画面。

- 模型切换、工作流变更或关键参数变化影响既有基线。

- 创意探索发现具有显著收益的替代方案。

### 6.4 升级动作

升级动作可单独或组合执行：

1. 提升模型能力或切换更适合的模型。

2. 增加分辨率、采样质量、推理步数或计算资源（按模型能力决定）。

3. 增加候选方案或分阶段预演，不机械地无限生成。

4. 引入角色/场景参考资产、姿态控制、深度/边缘控制或 3D 辅助。

5. 将复杂镜头拆分为可控子镜头或分层合成。

6. 增加技术美术、剪辑、声音或导演人工介入。

7. 将生成路径升级为混合制作、人工修复或传统后期。

8. 回到创意阶段重新设计镜头表达，在不损失叙事目标的前提下降低实现难度。

### 6.5 Quality Reserve（质量储备）与预算仲裁

项目预算应分别设立 Production Budget、Exploration Reserve、Quality Reserve 和 Contingency Reserve。Quality Reserve 仅用于 QC 缺陷修复、必要重生成、质量补偿和最终质量保障，不得用于创意探索、多方案试验或非必要风格变化。创意提案、静帧比较、低清动态样片和探索分支只能从 Exploration Reserve 扣费。各账户额度按镜头风险、历史返工率、探索授权和项目周期估算，不采用全项目固定比例。

当预算或周期不足以支撑当前质量目标时，系统必须生成资源差距报告：

- 当前目标与现有资源之间的差距。

- 需要增加的算力、模型费用、人力或时间。

- 可优化的非核心环节和可复用资产。

- 不损失叙事意图的替代镜头方案。

- 缩小交付范围、减少镜头、延长周期等选项及其影响。

由制片人/项目负责人和导演共同决策。Agent 不得自行降低质量阈值、取消必要 QC 或把未达标结果标记为通过。

### 6.6 Cost Forecast：镜头/集/项目成本预测

成本按生成调用、GPU/渲染、存储/传输、人工审核与后期、返工、交付拆分。单镜头预测：

```
Expected Shot Cost
= Σ(任务路径概率 × 单次调用成本 × 预计调用次数)
+ 预期返工成本
+ 分摊人工/算力/存储成本
```

单集预测为镜头预测之和，加音频/剪辑、共享资产分摊、交付成本和风险储备。输出 P50/P80/P95 区间，并记录价格版本、调用路径、历史返工率、人工工时、样本量和置信度。预测区间不是报价承诺。

#### 6.6.1 冷启动三级估算

无历史数据时按以下顺序启动：

1. `ANALOGY`：选同内容类型、相似镜头复杂度和生产路径的历史项目，说明差异修正因子；

2. `EXPERT_RANGE`：由制片/技术负责人输入成本区间、工时范围和返工假设；

3. `CONSERVATIVE_BOUND`：若缺少可靠类比或专家区间，按高风险镜头比例、保守调用次数和更高返工假设给出预算上界。

低置信度（建议初始阈值 `< 0.60`）时必须人工复核预算并标注 `LOW_CONFIDENCE_FORECAST`；不得用伪精确点估计批准预算。随着真实账单和工时回流，按模型/工作流/镜头子类校准。

#### 6.6.2 白模阶段的预算口径

Production Budget、Exploration Reserve 与 Quality Reserve 按各自政策计提和记账；白模构建、几何体检、Previz 渲染与控制信号导出不消耗付费生成模型调用额度，但相关人工、渲染、存储、资产采购和返工仍须进入 CostLedger。基线 Previz 成本默认归 Production Budget 的 PREVIZ 成本类别；仅经批准的探索分支 Previz 才可归 Exploration Reserve。Quality Reserve 不承担创意探索成本。

### 6.7 L4 与资源升级决策

`policy/escalations` 申请必须包含镜头授权级别、探索目标、COMPACT/STANDARD/EXPANDED 方案、候选/模型数量、预计费用区间、周期影响、预期收益和质量风险。资源升级批准不等于创意批准；导演授权和制片预算分别留痕。

### 6.8 容量规划

调度器监测队列长度、预计等待时间、Worker/GPU 容量、Provider 并发与速率限制、任务长尾、截止时间、风险镜头资源预留和预算消耗速度。必须有队列背压和优先级保护，防止低优先级探索挤占交付关键路径。预算或容量不足按第 1.8 节进行显式仲裁。

策略解耦补充：镜头等级变化仅在实际改变任务输入、模型路线、控制信号或验收契约时触发相关 DAG 节点失效；不得因等级字段变化而全量重跑。策略变更的依赖分析规则见第 17 章。

## 7. 工作流、DAG 与 Gate 契约

### 7.1 全局阶段

| **阶段** | **目标** | **主要输出** | **Gate/条件** |
| --- | --- | --- | --- |
| Pre-flight | 接收需求、预算/期限/风险初判 | ProjectBrief、策略包 | 必要信息齐备 |
| Pre-A（按需） | Logline、大纲候选、概念图、概念片 | ConceptPack、CreativeDirection | Gate 0 创意立项 |
| A | 剧本定稿、拆镜、资产查验、视觉预演、Animatic | ScriptLock、ShotList、Animatic、ConstraintPack | Gate 1 生产基线 |
| F | 高风险镜头小样、创意方案验证 | SampleAsset、参数/创意基线 | F-QC |
| B | 出图、图生视频、修复同步、画面粗排 | ShotVisualAsset、QCReport | 镜头级质量门禁 |
| C | TTS、BGM、音效及音频编辑 | AudioPack、AudioTimeline | 音频 QC |
| D | 音画精剪、后期包装、灰度测试 | RoughCut、FineCut、PictureLock Candidate | Gate 2、Gate 3 |
| E | 合规、平台适配、终审、入库 | DeliveryPackage、复盘报告 | Gate 4 |

说明：本节 Pre-flight、Pre-A、A、F/B/C、D、E 为生产工作流阶段，不是平台交付 Phase 0—4；平台产品实施阶段的唯一权威映射见第 31.2 节。

Pre-flight 不强制完整剧本；创意孵化型项目可先 Pre-A，成熟剧本项目可缩短或跳过 Pre-A，但不得跳过适用的质量和合规门禁。

**A 阶段子轨道：** A 阶段拆分为 A1（剧本与分镜）和 A2（Blocking/Previz）。A1 输出 ScriptLock、ShotList、ConstraintPack 和 ShotSpec；A2 在 ShotList/ShotSpec 可用后启动，可与 Animatic 的部分工作并行，输出 PrevizVersion、CameraRig 和 ControlSignalAsset。是否对某镜头执行 A2，由风险、导演要求、路线控制需求和资源仲裁决定，不要求所有镜头一律制作白模。

| **子轨道** | **目标** | **输出** | **Gate** |
| --- | --- | --- | --- |
| A1 剧本与分镜 | 锁定叙事意图、镜头边界和交付约束 | ScriptLock、ShotList、ConstraintPack、ShotSpec | Gate 1-A |
| A2 Blocking/Previz | 白模构建、几何体检、预演渲染、纯净度检查、控制信号导出 | PrevizVersion、CameraRig、ControlSignalAsset | A2-QC（支撑资产门禁） |

只有 `BASELINE` 型 Previz 经授权批准后才将锁定维度写入 ConstraintPack；探索型 Previz 不进入硬约束。

### 7.2 动态 DAG

DAG 必须支持：

- 条件分支：按风险、素材状态、QC 结果和导演选择执行不同路径。

- 动态扩展：发现质量缺口时增加修复、重生成、人工处理或替代方案节点。

- 局部回流：仅重跑受影响节点及其下游依赖，不整集无差别重跑。

- 版本固定：每次任务固定引用 ShotRevision、WorkflowDefinition Version、Policy Version 和输入 AssetVersion。

- 创意分支：不同 CreativeProposal 可并行生成，最终由人类选择或合并。

- 可取消与可恢复：创意分支可以单独熔断，不影响已批准的其他生产分支。

DAG 新增节点类型：`BlockingBuildNode`、`GeometryValidateNode`、`PrevizRenderNode`、`PurityCheckNode`、`ControlSignalExportNode`、`RouteComposeNode`。依赖关系至少保证 `GeometryValidateNode` 通过后才能进入 `PrevizRenderNode`；渲染后必须通过纯净度与 Rights 检查才能导出/发布 ControlSignalAsset；`RouteComposeNode` 依据路线能力声明组合控制信号、外观参考和 Prompt。

### 7.3 F/B/C 轨道规则

- **C 轨：** 可基于 Gate 1 时间码启动对白/TTS/BGM/音效初稿；时间线变化生成新版本，不覆盖旧音频资产。

- **F 轨：** 对高风险、新模型、新工作流或高创意不确定性镜头执行小样验证；F-QC 通过后形成参数/流程参考，不将其固化为所有镜头的强制模板。

- **B 轨：** 低风险镜头可使用已验证工作流；高风险镜头依赖对应 F-QC；任何镜头均可因质量缺口动态回流。

- **D 轨：** 所需镜头资产和音频版本达到要求后启动；Picture Lock 必须绑定完整固定版本清单。

A2 可按镜头设置子 Gate：`A2_GEOMETRY_GATE`、`A2_PURITY_GATE`、`A2_RIGHTS_GATE`。失败时将对应 Previz/信号资产标记为 `BLOCKED` 或 `QUARANTINED`，并阻止依赖节点执行；不得因为普通 A/B 生产流程已启动而绕过。

### 7.4 Gate 通用契约

每个 Gate 必须包含：

- `gate_key`、`gate_instance_id`、`project_id`、`scope`（项目/集/场景/镜头）。

- `entry_criteria`、`required_artifacts`、`ruleset_version`、`constraint_snapshot_id`。

- `reviewers`、审批顺序、是否需要多人会签。

- `decision`：APPROVED / REJECTED / RETURNED / OVERRIDDEN。

- `reason_code`、`comment`、`evidence_refs`、`decided_by`、`decided_at`。

- `exit_actions`：通过后解锁 DAG 节点、创建快照和通知。

Gate 审批对象必须引用不可变版本。输入版本变化时，原 Gate 标记 STALE，按影响范围重新评审。

### 7.5 Gate 0—4

| **Gate** | **必要输入** | **检查重点** | **通过动作** |
| --- | --- | --- | --- |
| Gate 0 创意立项 | ConceptPack、创意说明、初始预算/周期 | 题材方向、受众、叙事目标、资源可行性 | 立项并创建项目基线 |
| Gate 1 生产基线 | ScriptLock、ShotList、Animatic、ConstraintPack、资产缺口 | 剧作、镜头功能、硬约束、交付规格、风险和预算 | 锁定基线并下发 F/B/C DAG |
| Gate 2 粗剪审核 | RoughCut、音频版本、适用维度 QC 报告 | 叙事连贯、节奏、声画、质量问题与艺术意图 | 通过、定点返工或回到 A |
| Gate 3 Picture Lock | FineCut、镜头/音频/字幕固定清单、技术报告 | 画面、声音、字幕、时长、版本完整性 | 创建不可变 PictureLock Snapshot |
| Gate 4 终审交付 | PictureLock、版权证据、平台输出包、终检报告 | 技术规格、版权/授权、安全、格式与完整性 | 发布 DeliveryPackage、正式入库 |

### 7.6 精准返工

返工单至少包含 `issue_id`、对象、缺陷分类、严重程度、根因、建议回流节点、负责人、受影响资产和下游任务。缺陷映射示例：

| **缺陷类别** | **默认回流节点** | **下游影响** |
| --- | --- | --- |
| 剧情/人物动机 | A-剧本 | 分镜、对白、音频、剪辑 |
| 镜头表达/构图 | A-分镜/Animatic 或 CEE | F、B、C 时间码 |
| 角色/资产错误 | A-资产查验 / 资产修复 | F、B、角色一致性 QC |
| 小样风格/参数问题 | F | 对应镜头 B 任务 |
| 视频运动/时序问题 | B2 | B3、粗排、D |
| 图像局部瑕疵 | B3 | 对应镜头及粗排 |
| 配音/BGM/音效问题 | C | 唇形同步、D、字幕 |
| 剪辑节奏问题 | D1 | 音频切点、字幕、包装 |
| 字幕/包装 | D3 | 终检、平台适配 |
| 白模空间/比例/穿模 | A2-Blocking / ShotSpec | 白模重建、信号重导；直接消费信号的 B/F 任务进入 DIRECT_STALE |
| 相机轨迹/主体走位错误 | A2-Blocking / CameraRig | 相关 ControlSignalAsset、生成任务和时间线片段重新评估 |
| 白模时间码/切点错位 | A2-ShotSpec / A2-Blocking | 控制信号、C 时间线、D 粗剪和字幕按依赖范围评估 |
| 控制信号与路线能力失配 | A2-ControlSignalExport / RouteCompose | 重新导出、切换已验证路线或进入混合制作 |
| 白模纯净度/版权不合格 | A2-PurityCheck / Rights Gate | 隔离源资产，不得导出控制信号或进入付费生成 |

失效传播按 DAG 实际依赖边计算。已审核资产不删除；新版本产生后，引用旧版本的下游结果标记为 STALE。

ReworkTask 字段契约（返工单的机器可读字段；缺陷类别与回流节点取值见表 7.6-1）：

| **字段** | **类型** | **必填** | **说明** |
|---|---|---|---|
| `rework_task_id` | string | 是 | 返工单标识，同一 issue 可派生多张返工单 |
| `issue_id` | string | 是 | 绑定的 QCIssue，返工必须可回溯到缺陷证据 |
| `target_ref` | string | 是 | 返工对象：镜头、AssetVersion 或时间线片段的精确引用 |
| `defect_category` | enum | 是 | 取表 7.6-1 的缺陷类别枚举，不得自造类别 |
| `severity` | enum | 是 | BLOCKER / CRITICAL / MAJOR / MINOR；BLOCKER 直接阻断对应 Gate |
| `root_cause` | string | 是 | 根因判定；不得只写“效果不好”这类无法验证的结论 |
| `return_node` | enum | 是 | 建议回流节点，取表 7.6-1 的默认回流节点枚举 |
| `owner` | string | 是 | 返工责任人（主体标识），不得留空由 Agent 代填 |
| `affected_asset_version_ids` | string[] | 是 | 受影响资产版本；驱动 STALE 影响分析 |
| `downstream_task_ids` | string[] | 是 | 下游任务，用于计算传播范围与重跑集合 |
| `constraint_pack_ref` | string | 否 | 涉及硬约束变更时必填，引用 ConstraintPack 新版本 |
| `status` | enum | 是 | OPEN / IN_PROGRESS / DONE / VERIFYING / CLOSED / REJECTED；VERIFYING 须有复核人 |
| `created_at` | datetime | 是 | 创建时间 |

ReworkTask 权威状态机：OPEN → IN_PROGRESS → DONE → VERIFYING → CLOSED；VERIFYING 复核不通过退回 IN_PROGRESS，复核人不得为返工执行人本人。任一未关闭状态可转 REJECTED（撤销返工，须记录理由），CLOSED 为终态。

### 7.7 Picture Lock

Picture Lock Snapshot 冻结视频 checksum、镜头顺序、时间线版本、音频版本、字幕版本、LUT/VFX 版本、帧率、分辨率、音频规格、规则集版本和审批记录。任何修改都创建新 Snapshot，并根据影响范围重跑 Gate 3/4。

### 7.8 STALE 传播分级与资源处置

STALE 不是简单的“全部下游重跑”。变更影响按三类评估：

- `DIRECT_STALE`：直接依赖被修改版本或依赖字段的产物，必须停止发布并重新验证；若任务尚未执行则阻止启动，已执行则保留结果并标记待复核。

- `INDIRECT_STALE`：通过一层或多层依赖间接受影响，先执行影响分析，决定重跑、局部 QC 或无需动作；必须记录决策证据。

- `ADVISORY`：仅提示潜在关联，不阻断生产；可纳入人工抽检或风险看板。

传播以 DAG 显式依赖和 ConstraintPack 的连续性依赖联合计算。默认自动传播深度建议设为 DAG 3 跳；该值可按工作流结构配置。超过 3 跳时必须创建影响分析任务，由责任人确认是否继续传播，不得盲目级联。配置还需限定项目/场景范围。冻结粒度以受影响的 Task、AssetVersion、Gate 或时间线片段为最小单位，不冻结整个项目，除非影响分析认定共享硬约束或交付快照整体失效。

已消耗的算力和费用不因 STALE 删除，必须保留在 CostLedger；只有真正重跑才新增费用。旧资产保持不可变并标记 `STALE/SUPERSEDED`，不得物理覆盖。Gate 失效后只重开其所依赖的必要审核项。

PrevizVersion 或 ControlSignalAsset 创建新版本时，必须将旧版本引用者纳入 STALE 影响分析：直接消费该信号的 ModelInvocation 和生成结果默认 `DIRECT_STALE`；引用其衍生结果的后续资产/时间线按实际字段依赖评估为 `INDIRECT_STALE`；仅共享场景标签但未依赖受影响控制维度的项目可为 `ADVISORY`。不得把所有同场景镜头无差别标记为 DIRECT_STALE。成本不回滚，已消耗调用保留在 CostLedger；仅实际重跑产生新费用。

### 7.9 隐式连续性依赖登记

ConstraintPack 与 Production Memory 必须支持显式记录跨镜头状态约束，例如角色服装/伤口/道具持有状态、场景昼夜/天气/主光方向、空间方位、动作前后状态、同一连续镜头组和叙事时间线。每条记录包含 `constraint_id`、对象、作用镜头范围、状态/规则、来源、权威等级、有效时间和审批引用。

一旦确认镜头间存在共享状态，Orchestrator 必须建立 `CONTINUITY_DEPENDENCY` 边，或登记为可计算的约束组。高风险连续性必须显式建边；无法自动发现的隐式关系通过分镜审查/导演确认补录。STALE 传播同时读取 DAG 边和有效连续性约束，不只依赖任务执行图。

ConstraintPack 字段契约（包级字段；约束条目字段见上段列举，条目内全部必填）：

| **字段** | **类型** | **必填** | **说明** |
|---|---|---|---|
| `constraint_pack_id` | string | 是 | 包标识，同版本内唯一 |
| `project_id` | string | 是 | 所属项目 |
| `scene_id` | string | 否 | 所属场景；镜头级包可为空 |
| `shot_ids` | string[] | 是 | 作用镜头范围；镜头组另由 `shot_group_ids` 补充 |
| `shot_group_ids` | string[] | 否 | 连续镜头组标识，用于跨镜头约束绑定 |
| `applicability` | enum | 是 | APPLICABLE / NOT_APPLICABLE；无锁定约束的镜头必须显式标 NOT_APPLICABLE，不得留空 |
| `locked_dimensions` | string[] | 是 | 已授权锁定维度：camera_intrinsics、camera_extrinsics、camera_path、blocking、shot_size_nodes、cut_points、spatial_relations |
| `constraints` | object[] | 是 | 约束条目集合；每条必含 constraint_id、target、shot_scope、rule、source、authority_level、valid_at、approval_ref |
| `source_previz_revision` | string | 否 | 来源 BASELINE PrevizVersion；EXPLORATORY 型不得填入 |
| `version` | integer | 是 | 包版本；变更须新建版本并 supersede 旧版本，不得原地覆盖 |
| `status` | enum | 是 | DRAFT / ACTIVE / STALE / SUPERSEDED / REVOKED；仅 ACTIVE 可作为生成输入 |
| `approved_by` | string | 是 | 授权人（导演或授权负责人），不得为 Agent |
| `approved_at` | datetime | 是 | 审批时间 |

ConstraintPack 权威状态机：DRAFT → ACTIVE；ACTIVE 后可进入 STALE / SUPERSEDED / REVOKED。只有 ACTIVE 可作为生成输入；标 NOT_APPLICABLE 的包同样必须经 ACTIVE，才能证明“已确认无锁定约束”是由授权人作出的判断，而不是默认值。STALE 表示其引用的 PrevizVersion 或上游锁定维度已失效，须重新确认；SUPERSEDED 表示已被新版本取代；REVOKED 表示授权被撤销，相关路线立即阻断。

### 7.10 创意熔断与治理门禁联动

`CREATIVE_FATIGUE` 仅关闭当前探索分支；`CREATIVE_GOVERNANCE_HIT` 必须创建 Rights/Safety/Fact Gate，并隔离相关 AssetVersion。治理命中影响哪些镜头、镜头组和下游交付，由依赖分析确定；不影响无依赖关系的已批准生产分支。

### 7.11 Temporal 与 Project DB 的状态职责边界

采用 Temporal 或同类 Durable Workflow 时，明确两类状态：

- **Project DB（业务态事实源）：** Project、WorkflowRun 的业务状态、Task、Dependency、Gate、AssetVersion、审批决策、成本账本和审计事件。任何业务状态的最终写入必须经过领域服务并持久化于 Project DB。

- **Temporal（执行态协调器）：** Workflow History、Activity 调度、Timer、Retry、Heartbeat、执行等待、取消信号和恢复位置。Temporal 是执行编排的持久化事实源，但不是业务审批和资产状态的最终事实源。

Temporal Workflow 不得直接绕过领域服务修改业务表；执行结果通过命令/API 更新 Project DB。数据库事务内同时写业务状态和 OutboxEvent；Outbox Publisher 异步投递事件给 Temporal/消费者。执行侧完成后调用领域服务写回业务状态并生成新 OutboxEvent。所有事件包含 `event_id`、`aggregate_id`、`aggregate_version`、`temporal_run_id` 和 `correlation_id`。

一致性规则：

1. Outbox 至少一次投递，消费者按 `event_id` 幂等处理；

2. Project DB 的业务版本号单调递增；重复/乱序事件不得回退业务状态；

3. 定时 Reconciliation Job 对比 WorkflowRun/Task/Gate 业务状态与 Temporal 执行状态；

4. 冲突时按状态映射表和业务事件重建执行态，不能用 Temporal 历史直接覆盖 Gate 或审批结果；

5. 所有修复写入 ReconciliationEvent，并保留差异、修复动作和责任任务。

若不采用 Temporal，自建状态机也必须满足同样的业务态/执行态职责隔离和 Outbox 一致性契约。

策略解耦补充：新增 ProjectProductionProfile、ShotProductionProfile、PolicyResolution、ShotResourceAllocation、ShotBudgetLedger、ShotTierChangeRequest 与 TierChangeAudit 等实体，详见第 17 章。

## 8. 领域模型与数据契约

### 8.1 核心实体

| **实体** | **含义** | **关键关系** |
| --- | --- | --- |
| Project / Episode / Scene / Shot | 项目层级 | 项目包含集，集包含场景，场景包含镜头 |
| ShotRevision | 镜头内容修订 | 绑定镜头描述、约束、创意意图和版本 |
| ConstraintPack | 硬约束快照 | 绑定项目/场景/镜头，记录来源和版本 |
| CreativeProposal | 创意提案 | 关联 ShotRevision、候选资产和导演决策 |
| WorkflowDefinition / WorkflowRun | 工作流定义/实例 | 运行实例固定引用定义版本 |
| Task / Dependency | 可调度任务/依赖 | 属于 WorkflowRun，组成 DAG |
| Asset / AssetVersion | 逻辑资产/不可变版本 | 关联生成快照、来源、QC 和审核 |
| Pattern / PatternVersion | 创作模式/版本 | 记录可复用经验、适用边界和效果 |
| Review / GateInstance | 审核/审批实例 | 指向固定 AssetVersion 或快照 |
| ModelInvocation | 模型调用记录 | 关联 Task、Adapter、请求与用量 |
| QCReport | 质量报告 | 绑定输入版本、检测器和规则版本 |
| CostLedger / AuditEvent / OutboxEvent | 成本/审计/事件 | 支持核算、追责和可靠事件发布 |
| TimelineVersion | 时间线版本 | 绑定画面、音频、字幕、时间码基线 |
| ShotSpec | 镜头结构化规格单一数据源 | 绑定 ShotRevision；派生 Previz、Prompt、控制信号和路线执行规格 |
| PrevizVersion | 白模/预演不可变版本 | 绑定 ShotRevision、ShotSpec、ConstraintPack、CameraRig 和控制信号 |
| ControlSignalAsset | 控制信号逻辑资产 | 关联 PrevizVersion、源 AssetVersion、ModelInvocation 和消费路线 |
| CameraRig | 相机参数与轨迹 | 属于 PrevizVersion，导出 camera_manifest |
| BlockingAsset | 白模场景/角色/道具 3D 资产 | 关联 AssetVersion 和 rights_manifest；记录单位/坐标约定及 3D 许可 |

资产谱系边界：Asset 为逻辑对象，AssetVersion 为不可变版本；ExternalToolArtifact、BlockingAsset、ToolExchangePackage 等均派生自 AssetVersion 谱系，回流成片与外部工程始终新建版本，不得平行建立语义重叠的资产表。

### 8.2 标识与版本规则

- 主键使用 UUID 或等价全局唯一 ID；业务短编号仅用于展示。

- `project_id / episode_id / scene_id / shot_id / task_id / asset_id / asset_version_id` 不得复用。

- `Shot` 是稳定逻辑镜头；`ShotRevision` 保存每次内容修订。

- 工作流定义使用 `workflow_key + version`；运行实例固定引用一个版本。

- Asset 是逻辑对象；AssetVersion 是不可变文件版本。

- 文件内容以 SHA-256 等内容哈希校验；哈希去重不能丢失业务来源关系。

- 所有时间统一存 UTC，前端按项目时区展示。

### 8.3 Shot 契约示例

```
{
  "shot_id": "sh_01J...",
  "shot_revision_id": "shr_01J...",
  "shot_code": "EP001-SC003-SH012",
  "status": "IN_PRODUCTION",
  "duration_ms": 4200,
  "narrative_intent": "角色意识到房间内存在异常",
  "description": "主角推门进入房间，停顿并观察窗边",
  "character_refs": ["char_001"],
  "location_ref": "loc_008",
  "prop_refs": ["prop_002"],
  "constraint_pack_id": "cp_01J...",
  "creative_autonomy": "L3",
  "risk": {"level": "HIGH", "reasons": ["复杂动作", "角色近景"]},
  "quality_profile": "UNIFIED_HIGH_QUALITY",
  "timecode": {"start_frame": 240, "end_frame": 366, "fps_num": 30, "fps_den": 1}
}
```

镜头描述、硬约束、创意意图、Prompt、对白和版权来源应分字段存储，不能混成一个自由文本字段。

### 8.4 AssetVersion Manifest

建议保存：

- 资产 ID、版本号、状态、媒体类型、MIME、分辨率、时长、帧率、编码。

- 存储 URI、缩略图 URI、代理文件 URI、SHA-256、文件大小。

- 来源 Task、父级 AssetVersion、衍生关系。

- `generation_manifest`：provider、model、model_version、seed、prompt、negative_prompt、workflow_hash、节点参数、LoRA、ControlNet、采样器等。

- `rights_manifest`：来源、授权主体、许可范围、有效期、地域限制、署名要求和证据附件。

- `constraint_snapshot_id`、`creative_proposal_id`、`qc_report_id`、审核状态和创建者。

复现能力分级：R0 可追溯；R1 可重复调用；R2 近似复现；R3 确定性复现。不可对所有外部模型承诺 R3。原始输出和 Manifest 是追溯依据。

#### 8.4.1 Previz 与控制信号 Manifest

AssetVersion Manifest 增加：

```
previz_manifest:
  previz_id: pvz_01J...
  previz_type: BASELINE  # EXPLORATORY / BASELINE
  shot_spec_ref: spec_01J...
  camera_manifest_ref: cam_01J...
  geometry_validation_passed: true
  purity_check_status: PASSED
  rights_manifest_ref: rm_01J...
control_signal_manifest:
  produced_signals: []
  consumed_signals:
    - control_signal_id: cs_01J...
      reference_role: MOTION_AND_CAMERA_CONTROL
      model_consumption_mode: WHITE_MODEL_VIDEO_REFERENCE
      supported_dimensions: [camera_motion, subject_motion_path, timing_rhythm]
```

Manifest 必须记录实际消费模式与适配器能力快照；不能只记录“有白模视频”而不说明它控制什么。

### 8.5 Asset 生命周期

`GENERATED → TECHNICAL_QC → HUMAN_REVIEW → APPROVED → GOLD`，并允许进入 `REJECTED / QUARANTINED / REWORK / SUPERSEDED / ARCHIVED`。`QUARANTINED` 不得直接转为 `GOLD`，必须补齐证据并经授权复核。

### 8.6 Task 状态机

```
BLOCKED -> READY -> QUEUED -> RUNNING -> SUCCEEDED
                               |  |  |
                               |  |  +-> FAILED
                               |  +----> RETRY_WAIT -> QUEUED
                               +-------> CANCEL_REQUESTED -> CANCELLED

READY/QUEUED 允许取消；依赖失败时下游转 BLOCKED 或 NEEDS_REPLAN。
```

每个 Task 至少包含 `task_id`、`workflow_run_id`、`task_type`、`status`、`priority`、`idempotency_key`、`attempt`、`max_attempts`、`input_snapshot`、`output_ref`、`error_code`、`lease_owner`、`lease_expires_at` 和时间戳。

幂等键建议由 `workflow_run_id + node_key + shot_revision + input_manifest_hash + execution_variant` 生成。

等待态与未知态：除主链状态外，Task 还使用以下状态并纳入同一状态机：WAITING_FOR_HUMAN 等待导演或授权人确认/决策，超时未回复不得视为批准，可转取消或重规划；WAITING_FOR_RESOURCE 等待资源追加或范围、周期调整，资源缺口显式暴露，不得静默降级；WAITING_FOR_PROVIDER 供应商调用失败或状态不可确认、且无法按质量等价策略安全切换时进入，须先凭 provider_job_id 和 request_id 查询原任务状态，不得假装成功；SUBMISSION_UNKNOWN 提交超时且无法确认任务是否已创建，须凭 provider_job_id、request_id 和幂等键查询供应商状态，确认前不得盲目重复提交。上述等待态不产生权威业务结果；恢复、重试或取消均由服务端领域命令执行并写入追加式审计事件；本节未列出的状态及其转移一律拒绝。

白模与控制信号任务类型扩展：`BLOCKING_BUILD`、`GEOMETRY_VALIDATE`、`PREVIZ_RENDER`、`PURITY_CHECK`、`CONTROL_SIGNAL_EXPORT`。`GEOMETRY_VALIDATE` 失败时，`PREVIZ_RENDER` 和后续导出/生成节点必须保持 `BLOCKED`；`PURITY_CHECK` 或 Rights Gate 失败时，ControlSignalAsset 不得发布给路线适配器。任务事件应携带 `previz_id`、`shot_spec_version`、`control_signal_id`（如有）和 validator/policy version。

### 8.7 事件契约

事件至少包含 `event_id`、`event_type`、`occurred_at`、`producer`、`tenant_id`、`project_id`、`aggregate_type`、`aggregate_id`、`aggregate_version`、`trace_id`、`correlation_id` 和 `payload`。事件必须版本化；消费者容忍重复、乱序和未知字段。强一致性状态推进必须回查数据库。

### 8.8 Intent Contract 与授权快照

Intent Contract 必须版本化，并包含来源文本、解释者/模型版本、置信度、假设、明确指令、待确认问题、适用范围、审批人和状态。ShotRevision 引用固定的 `intent_id + intent_version`；后续意图修改不覆盖被生产任务引用的版本。

### 8.9 QCReport 扩展契约

```
qc_report_id: qc_...
asset_version_id: av_...
quality_profile_version: qpv_...
benchmark_version: benchmark_...
detector_versions: {visual: det_visual_..., identity: det_identity_...}
deviation_intent_ref: {intent_id: intent_..., version: 3}
deviation_class: INTENTIONAL # INTENTIONAL / UNINTENTIONAL / UNCERTAIN
intent_match_status: MATCH # MATCH / CONFLICT / NOT_ASSESSED
findings:
  - category: VISUAL
    severity: OBSERVATION
    confidence: 0.72
    evidence_refs: [frame_120, region_...]
    detector_eligibility: EVIDENCE_ONLY
human_decision: PENDING
```

无意图引用时主观偏差必须是 `UNCERTAIN/NOT_ASSESSED`，不能直接自动阻断。

### 8.10 ContinuityConstraint 契约

建议字段：`constraint_id`、`constraint_type`、`subject_ref`、`expected_state`、`scope_shot_refs`、`valid_from/to`、`authority`、`source_refs`、`confidence`、`approval_ref`、`dependency_edge_refs`。只有经批准的 Canon/Constraint 才能形成硬性阻断；观察到的状态和模型推断先进入待确认队列。

### 8.11 ModelInvocation 外部幂等映射

ModelInvocation 必须记录 `idempotency_key`、`provider_job_id`、`provider_request_id`、`submission_status`、`result_status`、`billing_status`、实际费用、查询时间和尝试序号。内部 Task 的幂等不保证外部供应商不会重复扣费。

外部调用处理规则：

1. 提交前生成唯一幂等键；若供应商支持幂等键，原样传递。

2. 提交超时且无法确认是否创建任务时标记 `SUBMISSION_UNKNOWN`，优先通过 provider_job_id/request_id/幂等键查询状态。

3. 不得在结果未知时立即盲目重提；若供应商不支持查询，进入延迟核查或人工处置。

4. 确认原任务不存在或不可恢复后才创建新 attempt，并记录 `duplicate_charge_risk`。

5. 任何重复计费进入 CostLedger，关联原始 Task 和各次 ModelInvocation。

### 8.12 ShotSpec 契约

`ShotSpec` 是镜头结构化描述的单一数据源，必须版本化并绑定 `ShotRevision`。提示词、白模和控制信号均为其派生输出，不应分别维护冲突的镜头时长或切点。

```
shot_spec_id: spec_01J...
shot_revision_id: shr_01J...
duration_ms: 4200
aspect_ratio: "16:9"
fps: {num: 24, den: 1}
coordinate_system:
  handedness: RIGHT_HANDED
  up_axis: Y
  distance_unit: METER
camera_path:
  type: DOLLY  # STATIC / DOLLY / TRACK / CRANE / HANDHELD / ORBIT
  keyframes: [{frame: 0, transform_ref: ...}, {frame: 100, transform_ref: ...}]
subject_paths:
  - subject_ref: char_001
    keyframes: [{frame: 0, transform_ref: ...}, {frame: 100, transform_ref: ...}]
shot_size_changes: [{at_frame: 0, size: WIDE}, {at_frame: 48, size: MEDIUM}]
cut_points_frames: [0, 48, 100]
locked_dimensions: []  # 仅经授权的维度可进入 ConstraintPack
previz_ref: null
control_signal_refs: []
```

时间必须以整数帧或明确的 rational FPS 表示，不允许混用未经说明的秒/帧单位。坐标系、轴向、单位、相机内参/外参定义和时间基准必须在项目 Schema 中固定；跨引擎转换需记录变换矩阵与精度误差。

### 8.13 PrevizVersion 契约

```
previz_id: pvz_01J...
shot_revision_id: shr_01J...
shot_spec_ref: spec_01J...
previz_type: EXPLORATORY  # EXPLORATORY / BASELINE
status: DRAFT  # DRAFT / GEOMETRY_VALID / RENDERED / APPROVED / LOCKED / SUPERSEDED
camera_rig_id: cam_01J...
blocking_asset_refs: [ba_01J...]
control_signal_refs: [cs_01J...]
locked_dimensions: []
geometry_validation:
  passed: false
  checks: [subject_in_frame, occupancy_bounds, camera_clearance, no_camera_clip, aspect_match, no_dead_time, cut_points_match]
  validator_version: geometry-validator-v1
  evidence_refs: []
  validated_at: null
render_manifest:
  engine: blender  # blender / threejs / ue / other registered engine
  resolution: "1920x1080"
  fps: 24
  duration_ms: 4200
purity_check_status: NOT_RUN
rights_manifest_ref: rm_01J...
created_by: principal_...
approved_by: null
approved_at: null
```

`BASELINE` 状态转为 `LOCKED` 必须有导演/授权负责人的审批事件；锁定后修改需创建新版本并显式 supersede，不得覆盖旧 PrevizVersion。`EXPLORATORY` 不允许自动写入 ConstraintPack。

### 8.14 ControlSignalAsset 契约

```
control_signal_id: cs_01J...
signal_type: REFERENCE_VIDEO  # CAMERA_JSON / DEPTH_SEQUENCE / NORMAL_SEQUENCE / POSE_SEQUENCE / MASK_SEQUENCE
source_previz_id: pvz_01J...
source_asset_version_id: av_01J...
reference_role: MOTION_AND_CAMERA_CONTROL
controlled_dimensions:
  - camera_motion
  - subject_motion_path
  - blocking_position
  - timing_rhythm
  - shot_transitions
uncontrolled_dimensions:
  - identity_consistency
  - final_appearance
  - material
  - lighting_style
  - fine_physics
coordinate_system_ref: project_coordinate_system_v1
timebase: {fps_num: 24, fps_den: 1, start_frame: 0, end_frame: 100}
storage_uri: s3://...
sha256: "..."
purity_check_ref: purity_01J...
rights_manifest_ref: rm_01J...
```

`APPEARANCE_REFERENCE` 不允许作为 ControlSignalAsset 的 reference_role。不同 signal_type 只能声明其实际控制维度；例如 CAMERA_JSON 不自动意味着能约束主体动作，POSE_SEQUENCE 也不自动意味着能约束相机运动。`uncontrolled_dimensions` 必须明确展示，供路线能力匹配与导演预期管理。

### 8.15 CameraRig 与 BlockingAsset

`CameraRig` 记录相机内参/外参、焦距或等效视角、传感器/画幅、裁切、坐标系、运动曲线和关键帧；输出 `camera_manifest` 时携带版本、单位和时间基准。`BlockingAsset` 记录网格/代理体来源、单位比例、坐标轴、拓扑/包围盒摘要、父级资产版本、许可类型和 `rights_manifest_ref`。BlockingAsset 的可见外观仅用于空间预演，不得未经明确标注被当作角色身份/材质/风格参考。

CameraRig 与 BlockingAsset 字段契约：

| **对象** | **字段** | **类型** | **必填** | **说明** |
|---|---|---|---|---|
| CameraRig | `camera_rig_id` | string | 是 | 机位标识，属同一 PrevizVersion 内唯一 |
| CameraRig | `previz_revision_ref` | string | 是 | 所属 PrevizVersion 精确版本引用 |
| CameraRig | `intrinsics` | object | 是 | focal_length、sensor_width、sensor_height、principal_point、distortion_model |
| CameraRig | `extrinsics` | object | 是 | position、rotation，按 coordinate_system 解释 |
| CameraRig | `equivalent_fov` | number | 否 | 等效视角（度），与焦距互为校验 |
| CameraRig | `sensor_format` | string | 是 | 画幅规格（如 36×24、S35、自定义） |
| CameraRig | `crop` | object | 否 | 裁切区域；存在时必须与时间线裁切一致 |
| CameraRig | `coordinate_system` | string | 是 | 坐标系与轴向约定（如右手系 Y-up） |
| CameraRig | `motion_curve` | object[] | 是 | 运动曲线关键帧定义 |
| CameraRig | `keyframes` | object[] | 是 | 时间、位置、朝向；时间基准见 time_base |
| CameraRig | `time_base` | string | 是 | 时间基准与帧率（如 24fps 非丢帧） |
| CameraRig | `unit` | string | 是 | 长度单位（mm / m），导出时必须携带 |
| CameraRig | `camera_manifest_ref` | string | 否 | 导出 camera_manifest 的引用与校验和 |
| CameraRig | `version` | integer | 是 | 版本号；变更新建版本，旧版本标 SUPERSEDED |
| BlockingAsset | `blocking_asset_id` | string | 是 | 白模资产标识 |
| BlockingAsset | `asset_version_ref` | string | 是 | 所属 AssetVersion 谱系引用，不得平行建表 |
| BlockingAsset | `mesh_source` | string | 是 | 网格来源与生成方式 |
| BlockingAsset | `proxy_source` | string | 否 | 代理体来源；存在时须标注精度等级 |
| BlockingAsset | `unit_scale` | number | 是 | 单位比例，与 CameraRig 的 unit 一致 |
| BlockingAsset | `axis_convention` | string | 是 | 坐标轴约定 |
| BlockingAsset | `topology_summary` | object | 否 | 拓扑摘要（顶点/面数/层级） |
| BlockingAsset | `bounding_box` | object | 是 | 包围盒；用于空间体检与穿模检测 |
| BlockingAsset | `parent_asset_version_ref` | string | 否 | 父级资产版本 |
| BlockingAsset | `license_type` | string | 是 | 许可类型；未知授权进入 QUARANTINED |
| BlockingAsset | `rights_manifest_ref` | string | 是 | 权利清单引用 |
| BlockingAsset | `preview_only` | boolean | 是 | 固定 true：仅空间预演，不作身份/材质/风格参考 |
| BlockingAsset | `version` | integer | 是 | 版本号；变更新建版本 |

## 9. Model Gateway 与生成执行

### 9.1 统一接口

所有外部模型和本地工作流通过 Adapter 暴露统一能力，不允许 Agent 直接拼装供应商私有 API。

```
class ModelAdapter:
    def capabilities(self) -> list[dict]: ...
    def validate_input(self, request: dict) -> dict: ...
    def estimate_cost(self, request: dict) -> dict: ...
    def submit(self, request: dict, idempotency_key: str) -> dict: ...
    def get_status(self, provider_job_id: str) -> dict: ...
    def cancel(self, provider_job_id: str) -> dict: ...
    def normalize_output(self, provider_result: dict) -> dict: ...
```

Adapter 注册信息：provider、model、version、capabilities、输入限制、输出规格、地域/数据策略、计费模型、并发上限、健康状态、降级候选和许可信息。

### Control Signal Consumption 能力声明

`ModelAdapter.capabilities()` 必须按信号类型、角色和维度返回结构化声明，不允许使用单一 `supports_white_model_video: true` 布尔字段代替细分能力。例如：

```
control_signal_consumption:
  - signal_type: REFERENCE_VIDEO
    reference_role: MOTION_AND_CAMERA_CONTROL
    supported: true
    controlled_dimensions: [camera_motion, subject_motion_path, timing_rhythm]
    max_duration_sec: 30
    max_count: 1
    required_companion_refs: [character_reference_image]
    prompt_syntax: "provider-specific registered syntax"
  - signal_type: CAMERA_JSON
    reference_role: CAMERA_PATH_CONTROL
    supported: false
```

声明必须带 provider/model/version、能力证据来源、验证日期和限制条件；模型升级或接口变化后需重新验证，不能沿用旧版本的能力结论。

### 9.2 能力路由

路由不能只按“哪个模型便宜/快”决定。至少考虑：

- 任务能力匹配：图像、视频、音频、动作、人物一致性、镜头控制等。

- 质量历史：该模型在相似镜头类别上的首过率、返工率和人工评价。

- 约束能力：是否支持必要参考输入、区域控制、时长、画幅和版权要求。

- 成本与时延：调用成本、排队时间、重试成本和总制作周期。

- 创意适配：是否适合当前视觉风格、表达方向和探索任务。

- 可用性与风险：供应商健康、配额、地域限制和数据使用政策。

#### 9.2.1 BlockingAdapter 接口

```
class BlockingAdapter:
    def build_scene(self, shot_spec: dict) -> dict: ...
    def validate_geometry(self, scene: dict) -> dict: ...
    def render_previz(self, scene: dict, render_spec: dict) -> dict: ...
    def export_reference_video(self, scene: dict, purity_policy: dict) -> dict: ...
    def export_camera_manifest(self, scene: dict) -> dict: ...
    def export_auxiliary_signals(self, scene: dict, signal_types: list[str]) -> dict: ...
    def check_purity(self, video_uri: str, purity_policy: dict) -> dict: ...
    def validate_asset_rights(self, blocking_assets: list[dict]) -> dict: ...
```

适配器可实现于 Blender 脚本化、Three.js 或游戏引擎等后端。`export_reference_video` 必须先执行纯净度检查；失败时拒绝导出，或返回显式 `IMPURE_REFERENCE` 且由治理策略阻断消费。`validate_geometry` 必须输出逐项检查结果、证据、单位/坐标基准和 validator 版本。

若 ShotSpec/导演锁定要求相机、走位或节奏控制，路由器必须筛选覆盖所需控制维度的候选路线；如候选模型只支持部分维度，应显式列出缺口并请求路线升级、混合路径或人工决策。不能把“支持参考视频输入”伪装成“完整支持所有控制维度”。控制信号与外观参考必须分开传递，Manifest 记录每个输入的 `reference_role`。

### 9.3 模型切换与降级

切换流程：故障分类 → 候选能力筛选 → 参数映射 → 新 ModelInvocation → 必要 QC → 质量达标后发布。不同模型的 Prompt/参数不视为完全等价。若候选模型无法达到当前质量目标，必须升级或请求人工选择，不得把不等价降级伪装为成功。

#### 9.2.2 路线适配器抽象（ProductionRoute）

```
class ProductionRoute:
    def supported_control_signals(self) -> list[dict]: ...
    def compose(self, shot_spec: dict, control_signals: list[dict],
                appearance_refs: list[dict], prompt: dict) -> dict: ...
    def required_appearance_refs(self) -> list[dict]: ...
    def qc_dimensions(self) -> list[str]: ...
```

路线适配器负责组合，不重新定义白模语义。首批路线抽象可包括 `ImageToVideoRoute`、`TextToVideoRoute`、`PrevizToVideoRoute`、`2DLayoutRoute` 和 `VirtualProductionRoute`；传统 CG/实拍合成可作为后续路线适配器接入。具体路线是否可用，以能力契约和端到端验证为准。

### 9.4 重试与故障分类

| **错误类别** | **自动动作** | **重试策略** |
| --- | --- | --- |
| 网络超时/临时 5xx | 指数退避 + 抖动 | 有上限 |
| 供应商限流 | 遵守 Retry-After、降低并发 | 有上限 |
| 余额/配额不足 | 暂停供应商队列、告警 | 不立即重试 |
| 参数校验失败 | 返回调用方修正 | 不重试 |
| 输入文件损坏 | 隔离输入资产、创建工单 | 不重试 |
| 生成质量不合格 | 调参、切模型、走 F/人工方案 | 质量驱动、有上限 |
| Worker 崩溃 | 租约到期后重新领取 | 幂等重试 |
| 版权/安全风险 | 隔离资产并进入治理流程 | 禁止自动绕过 |

### 9.5 模型切换的质量等价框架

“质量等价”不是视觉输出完全相同，而是候选模型在项目明确的技术规格、硬约束、关键连续性和交付目标上达到同一验收档位。模型切换前需执行风险分层验证：

- **技术等价：** 分辨率、帧率、时长、编码、音频规格和文件完整性达到相同 profile。

- **连续性等价：** 对受影响角色/场景子类进行相同 benchmark 检查；关键角色/状态不得低于已批准阈值，困难子类强制人工复核。

- **视觉/风格适配：** 检查是否仍符合 Intent Contract 和项目风格边界；允许表达不同，但需证明未破坏导演意图。艺术指标通过人工抽检/导演确认，不要求像素或风格相似。

- **运营等价：** 评估成本、时延、失败率、数据地域、许可和供应商策略。

验证范围由变化影响决定：供应商/模型大版本切换或新能力路由需先跑冻结 Benchmark 的代表性样本；低风险小版本可抽样；涉及高风险角色、镜头运动或声音链路时必须增加对应子集。模型切换验证成本计入 Model Gateway/项目预算。未达到关键技术和连续性要求时不得发布；艺术表达差异由导演决定是否接受。

策略解耦补充：核心质量红线和最终 Gate 由全局策略统一管理，项目等级和镜头等级均不得关闭或降低；人工评审密度可以差异化，但不等于核心 QC 覆盖率可以差异化。详见第 17 章。

## 10. QC、审核与合规治理

### 10.1 QCReport 结构

QCReport 至少包含：`report_id`、输入 AssetVersion、ruleset_version、detector_versions、适用的五维质量结果（技术/视觉/连续性/艺术/控制）、支撑资产检查（如适用）、治理检查结果、指标值、阈值、严重级别、证据时间码/区域、置信度、不确定性、建议动作和人工处置记录。无 ControlSignalAsset 或无锁定控制要求的镜头，控制维度标记 `NOT_APPLICABLE`，不得制造虚假分数。

### 10.2 检测器分层

- 文件技术检测：格式、解码、分辨率、帧率、时长、黑帧、静音、峰值、响度。

- 画面检测：闪烁、畸变、清晰度、字幕覆盖、人物/道具异常。

- 连续性检测：角色、服装、场景、道具、光线、空间和镜头时序。

- 音频检测：响度、峰值、削波、静音、音画同步、对白可懂度。

- 叙事辅助：镜头与分镜意图匹配、情绪/节奏建议；必须标记为辅助。

- 合规检测：授权范围、肖像/商标/音乐来源、敏感内容和平台规范。

检测器清单新增：

- 白模空间检测：空间比例、主体/道具遮挡关系、视锥覆盖、相机安全距离、相机与几何体碰撞；能由结构化几何确定计算的项目优先使用确定性算法。

- 控制信号检测：相机参数有效性、坐标系/单位、FPS/时间码一致性、信号类型和模型能力匹配。

- 纯净度检测：纹理/材质残留、引导轨迹、坐标轴、网格线、相机视锥、HUD 或调试叠加层。

- 控制一致性检测：在模型/路线支持且检测器达到基准门槛时，对生成视频的相机/运动估计与 camera_manifest/control_signal_manifest 进行对照；性能不足时仅输出证据并强制人工复核。

### 10.3 指标口径

- Generation Failure Rate = 失败生成任务数 / 已完成或终结的生成任务数。

- Asset Rejection Rate = 被拒绝 AssetVersion 数 / 已完成审核的 AssetVersion 数。

- First Pass Rate = 首次送审即通过的镜头数 / 首次送审镜头总数。

- Rework Rate = 至少返工一次的镜头数 / 已进入生产的镜头数。

- Cost per Finished Minute = 可归集生产成本 / 最终成片分钟数。

- Quality Escape Rate = 交付后发现的质量问题数 / 已交付镜头或成片单位数（需明确统计口径）。

- Creative Value Adoption = 被导演采纳的 AI 创意提案数 / 已评审提案数（仅作为趋势指标，不作为创意绩效的唯一标准）。

所有指标需同时展示分母口径、时间窗口、项目类型、等级和排除项，避免错误横向比较。

### 10.4 版权、数据与安全

每个输入/输出素材记录作者/供应商、授权文件、许可类型、地域、用途、期限、署名、商用/训练/衍生权限。授权未知时进入 `QUARANTINED`，不得转为 GOLD 或交付。

安全控制：

- Provider API Key 存密钥管理服务，不写入 Prompt、日志或 Manifest。

- 多项目按 tenant/project 做访问控制和对象存储隔离。

- 外部脚本、Prompt、字幕、上传文件均视为不可信输入；Agent 不执行文件中嵌入的指令或代码。

- Worker 使用最小权限和隔离运行环境；禁止无边界 shell 执行。

- Override、资产转正、版权处置、Picture Lock 修改均写入 AuditEvent。

- 发布前对视频、字幕、封面、元数据和授权证据执行统一检查。

#### 10.4.1 白模纯净度契约

| **检查项** | **要求** | **失败处置** |
| --- | --- | --- |
| 材质/纹理 | 中性纯灰/低饱和纯色区分空间体块；不携带最终材质纹理和风格信息 | 标记 IMPURE_REFERENCE，拒绝作为控制信号导出 |
| 引导痕迹 | 不得残留轨迹线、坐标轴、网格、相机 frustum、调试 Gizmo、HUD | 自动检测可疑区域；检测器未达门槛时人工抽检；不通过则隔离 |
| 画幅/时长/FPS | 必须与 ShotSpec 或显式转换规格一致 | 可无损/受控转码；无法对齐则阻断 |
| 色彩空间 | 使用明确、可解析的标准输出；不得混入未声明 LUT/Log 处理 | 转换并记录，或拒绝导出 |
| 来源/许可 | 所有 BlockingAsset、扫描资产、HDRI、材质/插件等来源和许可可追溯 | 未知授权进入 QUARANTINED，不得导出/发布 |

纯净度检测同时检查像素内容与 Manifest/渲染参数。仅靠“全灰”不能证明素材纯净；未经验证的视觉检测器不能自动放行。

### 10.5 Agent 权限、Capability Token 与升级流程

Agent 权限采用 RBAC + ABAC + 单次 Capability Token。Token 由**独立 Authorization Service**签发，不由 Agent 自行创建，也不由 Orchestrator 任意扩权。Token 默认按 Task 粒度签发，绑定：

`task_id + workflow_run_id + agent_identity + project_scope + allowed_tools + allowed_actions + resource_limits + expires_at + policy_version + nonce`。

签发前校验用户/服务身份、项目权限、任务状态、资产数据级别和策略版本；Token 短时有效、不可扩大 scope，敏感操作使用一次性 capability 并写入审计。

Agent 执行中发现权限不足时调用 `POST /authorization/escalation-requests`，提交原 Token、任务、所需动作、工具、目标对象、理由、预计影响和回滚方法。Authorization Service 评估后可拒绝、签发更窄的新 Token，或要求人类审批。涉及锁定事实、版权/安全处置、预算追加、Gate 审批、发布、全局策略和权限管理的操作必须由具备对应角色的人类批准；原 Token 不得被就地扩权。

建议关键接口：

- `POST /authorization/capabilities`：按 Task 签发；

- `POST /authorization/escalation-requests`：请求权限提升；

- `POST /authorization/revoke`：撤销 Token；

- `GET /authorization/audit/{task_id}`：查看授权链。

#### 10.5.1 白模操作权限

新增动作：`LOCK_PREVIZ_BASELINE`（锁定基线型白模）、`APPROVE_PREVIZ_CHANGE`（批准已锁定白模变更）、`EXPORT_CONTROL_SIGNAL`（导出控制信号）。锁定和批准变更要求导演/授权负责人批准并审计；导出由技术美术或具备授权的生产 Agent 在限定项目/镜头范围内执行。创作 Agent 不得把探索型方案自行锁定为基线。

### 10.6 检测器决策权限边界

QC Worker 只能在注册表声明的检测器权限范围内输出结果。`EVIDENCE_ONLY/ADVISORY` 不得修改 Asset 状态；`AUTO_ROUTE` 只能创建待复核/返工任务；`AUTO_BLOCK` 必须引用已批准的规则版本和性能验证报告。任何 Agent 不得通过 Prompt 或临时配置提升检测器权限。

白模版权规则：BlockingAsset 使用的第三方 3D 模型、扫描资产、HDRI、材质库、插件和外部场景均必须写入 `rights_manifest`，包含来源、许可、用途、期限、地域和可否用于生成参考。授权未知的资产进入 `QUARANTINED`，不得转为 GOLD、导出 ControlSignalAsset 或传给外部模型。

### 10.7 数据与模型治理

对素材和元数据实施数据分级，定义是否允许发送第三方模型、可用地域、保留期限、是否用于模型训练、是否包含肖像/声音/个人信息。上传文件、字幕、Prompt、网页和外部素材均视为不可信内容；Prompt Injection 不得提升为系统指令。工具实行 allowlist、参数 Schema 校验、速率限制、审计；密钥由 Secret Manager 注入，禁止进入 Prompt、Manifest 和普通日志。

### 10.8 几何体检规则

`GEOMETRY_VALIDATE` 至少检查：主体关键帧/逐帧是否在相机视锥内；主体屏幕占比是否在 ShotSpec 定义范围内；相机与主体/场景的安全距离；相机是否穿入几何体或发生 near/far clip；关键空间关系/遮挡是否符合锁定约束；画幅与时长是否一致；景别变化和切点是否匹配；是否存在超出容忍度的空帧/死时间/异常缓动尾段。检查项需按镜头类型启用，不适用项标记 `NOT_APPLICABLE`。失败时 PrevizVersion 不得进入 `GEOMETRY_VALID`，下游渲染、导出和付费生成保持 BLOCKED。几何体检通过只证明规则集覆盖范围内的几何条件通过，不代表表演、物理或最终画面质量达标。

### 10.9 控制质量验证边界

控制质量比较必须限定到模型能力契约声明支持的维度，并记录估计方法、检测器版本、置信度和证据。模型不支持或检测器未达门槛的维度标记 `NOT_EVALUATED`，不得伪造“控制通过”。相机轨迹/走位偏差阈值由项目的 ShotSpec 和路线 Profile 版本化配置；关键锁定维度偏差超限时定向返工或人工审查。

## 11. 创意资产、生产记忆与持续学习

### 11.1 Creative Pattern Library

Pattern Library 沉淀的是“创作方法与适用经验”，不是只存 Prompt 模板。建议分类：

- Cinematic Lighting：电影光线与氛围方法。

- Camera Language：镜头运动、景别、视角和空间表达。

- Emotional Expression：情绪、表演、微表情和身体语言。

- Scene Composition：场景构图、视觉层级和空间组织。

- Visual Storytelling：环境叙事、象征、隐喻和留白。

- VFX Patterns：特效表现与合成路径。

- Character Performance：角色动作、节奏和互动。

- Sound & Rhythm：声音叙事、音乐关系和剪辑节奏。

每个 Pattern 记录：创作目标、适用场景、输入条件、表达思路、模型/工作流、结果资产、导演评价、技术经验、成本、失败案例、适用边界、版本和来源。

- **Blocking & Previz Patterns：** 适用镜头类型、相机路径模板、走位模板、几何体检失败模式、控制信号与路线匹配经验、白模复用率和返工/成本数据。

### 11.2 Pattern 使用原则

- 默认是建议，不是强制模板。

- AI 可以直接复用、组合、变体或提出不使用的理由。

- 当项目风格或镜头目标不同，系统应提示适用边界，不得盲目套用。

- 新 Pattern 需要质量证据和来源信息；未经审核的经验标记为实验状态。

- Pattern 不得覆盖硬约束或导演明确要求。

### 11.3 审美反馈与偏好学习

反馈分为：导演偏好、项目风格、角色/场景连续性偏好、技术质量反馈、创意探索结果。需区分 AI 误判、艺术性保留、技术风险豁免、商业/交付约束和紧急例外。

避免只学习“历史被选中的结果”导致审美固化：

- 保留一定比例的探索候选用于评估新表达路径。

- 同时记录未被选中方案及拒绝原因，避免只保留正例。

- 区分项目偏好和跨项目通用经验，防止风格污染。

- 优化先使用离线评估和人工验证；未经批准不得在线改变 QC 红线或审批权限。

### 11.4 Production Memory System（PMS）

记忆类型包括 Project、Story、Character、Location、Creative 和 Production Memory。记忆是辅助决策数据，不自动成为硬约束。每条记录至少包含：`memory_id`、`memory_type`、`content`、`source_refs`、`confidence`、`authority`、`scope`、`valid_from/to`、`status`、`supersedes`、创建时间和责任人。

Character Memory / Location Memory 可新增 `previz_ref`、`shot_spec_ref` 和来源版本。经批准的 BASELINE Previz 可作为空间结构、相机位置/路径和主体走位的 `APPROVED_DECISION` 或 ConstraintPack 证据；EXPLORATORY 仅记为 `OBSERVED`。灯光方向、材质和外观除非被单独明确锁定，不得因为出现在白模渲染中自动成为 Canon。

权威等级：

- `CANON_LOCKED`：经授权确认的剧情/角色/世界观事实；

- `APPROVED_DECISION`：已批准创作或制作决策；

- `OBSERVED`：从资产/生产记录观察到的事实；

- `PREFERENCE`：导演偏好，可被当前明确指令覆盖；

- `HEURISTIC`：历史经验或模型推断，仅供建议；

- `DISPUTED`：来源冲突或尚待确认。

#### 11.4.1 分层检索与混合排序

先按 tenant/project、scope、validity 和 authority 做结构化过滤，再在候选集合内进行向量/关键词排序；`CANON_LOCKED` 事实维护精确索引，不依赖向量召回。对关键角色、时间线和场景状态，先查询权威 Canon 索引，再补充语义相关的观察/经验记忆。排序不能用一个可调权重把低权威高相似记录排到高权威事实之前。

记忆读取必须输出来源引用和版本；冲突记忆保留双方证据并标记 `DISPUTED`。新事实经审批后创建新版本并标记 supersedes，不覆盖历史记录。过期记忆不得参与当前决策，除非明确标为历史参考。

### 11.5 Pattern Library 防止审美固化

Pattern 必须标记为 `SUGGESTION_NOT_MANDATORY`，并展示适用边界、来源、历史采用率、适配项目和失败案例。推荐排序不能只按历史采纳率；应混合质量证据、上下文适配度和新颖性，并为低历史采纳率但高差异度的方案保留探索机会。

设置治理角色控制的 `exploration_reserve_ratio` 和 `novelty_injection_ratio`。项目组可以提高，不得自行降低到治理下限以下。建议初始治理下限为：在适合探索且不触及生产红线的候选任务中，至少 10% 的探索预算用于非主流/新路径候选；推荐候选中预留至少 20% 的位置给“高差异、低历史采纳”路径。该比例以 eligible exploration opportunities 为分母，不强制每个简单镜头增加候选；紧急豁免必须填写原因并经治理角色批准。比例应在试点后按项目类型校准。

系统同时记录被拒绝候选和拒绝原因，避免只学习成功样本。创意偏好学习先离线评估、灰度、版本化和可回滚，不可在线改变质量红线或权限。

## 12. 可观测性、运营指标、RCA 与运行可靠性

### 12.1 运营看板

按项目/集数/场景/镜头聚合：任务成功率、首过率、返工率、QC 缺陷分布、镜头风险分布、模型调用成本、每成片分钟成本、Gate 等待时间、队列积压、资产复用率、创意提案采纳趋势和交付后质量逃逸率。

- 白模任务周期、Previz First Pass Rate、几何体检失败率、纯净度失败率、控制信号失配率、白模返工率、白模复用率、白模到生成的成本比、各路线控制信号消费成功率。

### 12.2 告警

- 任务队列积压或 Worker 心跳异常。

- 模型供应商错误率、延迟或费用异常。

- 同类镜头 QC 失败率突增。

- 角色/场景一致性指标持续偏离。

- Quality Reserve 或 Exploration Reserve 消耗过快、四类预算账户任一接近上限或账户间出现未经批准的调拨。

- 版权证据缺失、授权到期或资产越权访问。

- Gate 长时间等待、Picture Lock 输入版本变更。

- 几何体检失败率突增、纯净度检测批量失败、控制信号与目标模型能力不匹配、白模 3D 资产版权缺失、白模/控制信号 STALE 传播范围异常扩大。

### 12.3 核心风险及控制

| **风险** | **控制机制** |
| --- | --- |
| S/A/B 被误用为质量等级 | 统一质量配置；策略校验禁止降质字段 |
| 模板固化创意 | Creative Freedom Zone、L3/L4 授权、Pattern 非强制 |
| AI 擅自改写剧情事实 | ConstraintPack、版本快照、Gate、人类授权 |
| 只追求自动评分 | 适用维度 QC + 导演艺术判断 + Override 记录 |
| 预算不足暗中降质 | Quality Reserve、资源差距报告、人工仲裁 |
| 无限候选造成成本失控 | 边际收益停止条件、预算/时间上限 |
| 模型切换质量漂移 | 新 ModelInvocation、对照 QC、质量等价验证 |
| 历史偏好导致审美固化 | 探索候选、负反馈归因、离线评估 |
| 版权/数据来源不明 | Rights Manifest、QUARANTINED、不可绕过治理门禁 |

控制信号相关指标定义：

- `Previz First Pass Rate` = 首次几何体检通过的 Previz 数 / 首次提交几何体检的 Previz 数。

- `Control Signal Mismatch Rate` = 路线组合/生成后发现控制信号类型、时间基准或目标能力不匹配的任务数 / 消费控制信号任务总数。

- `Purity Check Failure Rate` = 纯净度不通过的参考视频数 / 进入纯净度检查的参考视频数。

- `Blocking Rework Rate` = 至少返工一次的 Previz 数 / 进入 Previz 生产的镜头数。

- `White-model-to-Generation Cost Ratio` = Previz/控制信号制作成本 / 对应镜头生成成本；需同时报告样本范围，不能将低成本解释为零成本。

### 12.4 Production RCA Engine（生产根因分析）

RCA 将重复 QC 失败、返工和质量逃逸转化为有证据的根因案例。分类至少包含 `INTENT`、`STORY`、`REFERENCE`、`PROMPT`、`MODEL`、`WORKFLOW`、`MOTION`、`CONTINUITY`、`COMPOSITION`、`POST`、`QC`、`GOVERNANCE` 和 `CAPACITY`。一个案例可以保留多个根因候选及置信度，不得把模型猜测直接当成已确认根因。

RCA 记录至少包含症状、scope、相关 ShotRevision/AssetVersion/Task/ModelInvocation/QCReport/WorkflowDefinition 版本、候选根因、证据、确认根因、影响范围、纠正措施、预防措施、责任人、成本、状态和复测结果。

流程：聚合证据 → 提出候选原因 → 跨镜头检查 → 隔离变量/小样实验 → 人类确认 → 建立纠正/预防任务 → 评估下游重跑范围 → 复测 → 经验沉淀。任何学习结果要先离线验证并经过灰度/回滚，不自动改变质量红线。

#### 12.4.1 RCA SLA 与实验成本归属

建议初始 SLA：CRITICAL 事件 1 个工作日内完成分诊、3 个工作日内给出初步根因/“证据不足”结论；MAJOR 重复问题 3 个工作日内分诊、5 个工作日内形成分析计划；普通趋势问题进入每周复盘。SLA 根据项目工作时间、团队规模和合同约定校准。

实验成本必须归属到 RCA Case，并与项目成本分开标记为 `RCA_DIAGNOSTIC`、`CORRECTIVE_ACTION` 或 `PREVENTIVE_ACTION`。如果根因来自供应商/模型回归，记录供应商责任证据；若来自输入不全、创作变更或项目参数，分别归属对应成本中心。归属用于管理分析，不自动判定合同责任。

### 12.5 运营指标补充

运营看板新增：Override Rate、Weighted Override Rate、重复 Override 率、Override 后质量逃逸率、检测器误报/漏报率、人工复核触发率、Detector Benchmark Version、Quality Profile Version、Threshold Version、STALE 数量与传播深度、重跑成本、未知提交状态数、重复计费、RCA SLA 达成率和 RCA 未结案例账龄。

First Pass Rate、Rework Rate 等跨项目指标必须同时展示项目类型、镜头构成、Benchmark/阈值版本和人工复核覆盖率。比较不同阈值版本时先分层或归一化，禁止仅凭原始比例做榜单。

### 12.6 运行可靠性目标

每个部署环境定义并审批 SLO：API 可用性、任务状态推进延迟、队列积压、Worker 恢复时间、Outbox 延迟、模型调用成功率和交付可追溯率。下表作为初始生产环境建议值，不构成 SLA 承诺，必须在容量测试和恢复演练后按成本/风险校准：

| **组件/指标** | **建议起始目标** | **说明** |
| --- | --- | --- |
| 核心 API 可用性 | 月度 99.5% | 排除已公告维护窗口，按业务关键 API 统计 |
| Outbox 事件发布延迟 | P95 ≤ 30 秒，P99 ≤ 120 秒 | 以提交到可消费为测量区间 |
| Worker 崩溃恢复 | 任务租约到期后 5 分钟内重新调度 | 不等于外部模型结果已恢复，需先查询 Provider Job |
| Project DB | RPO ≤ 15 分钟，RTO ≤ 2 小时 | 关键审批/业务状态需启用 PITR 和定期恢复演练 |
| Workflow Runtime | RPO ≤ 15 分钟，RTO ≤ 1 小时 | 需与 Project DB reconciliation 验证 |
| 对象存储 | RPO ≤ 1 小时，RTO ≤ 4 小时 | 原始交付资产可按项目要求采用跨区复制/版本保护 |
| 审计事件 | RPO ≤ 5 分钟 | 关键授权、Override、发布事件应优先保护 |

建议为 Project DB、对象存储、队列和 Workflow Runtime 分别定义 RTO/RPO，并通过定期备份恢复、Worker 崩溃恢复和供应商故障演练验证。具体目标需在 Phase 0 根据项目生产等级、成本和部署条件确认，不得把建议起始值表述为已达到的运行承诺。

运行层必须包含队列背压、Provider 熔断/切换、Worker Lease 回收、数据库备份、对象存储生命周期/版本保护、灾备演练、审计保留和密钥轮换。供应商失败时先查询原任务状态，再按质量等价策略路由；不可安全切换时进入 `WAITING_FOR_PROVIDER`，不得假装成功。

策略解耦补充实施项：先冻结全局质量策略和项目/镜头 Profile Schema，再实现双维度策略解析、镜头预算账本、等级变更审批和最小化 STALE 传播。新增阶段验收用例见第 17 章。

## 13. 实施路线与验收标准

本章描述长期目标平台的能力演进路线。V2.0 唯一阶段总表见第 31.2 节；P0/P1/P2 表示优先级，Phase 0—4 表示产品与平台实施阶段，两者相互独立。P0-ARCH 表示冻结接口/Schema/权限边界；P0-MVP 表示首发核心生产闭环必须真实可用，不绑定固定开发周期。本章及附录 C.8、D.8 的阶段说明均为非权威摘要，交付范围与启用状态以 31.2 唯一阶段总表为准。

## Phase 0：架构基线与基础设施契约（P0）

交付：

1. S/A/B 与 L1-L4 解耦配置校验；L4 与 COMPACT/STANDARD/EXPANDED 资源配置分离。

2. Intent Contract、CreativeProposal、ConstraintPack、QCReport、ContinuityConstraint、Memory、ModelInvocation、Capability Token Schema。

3. Quality Profile、Benchmark Registry、Detector Registry、Override Policy 和阈值变更审批流程。

4. Temporal 执行态与 Project DB 业务态边界、Outbox/Inbox、Reconciliation 机制。

5. 模块数据所有权矩阵、模块间通信契约、模块事务边界和 Saga 补偿规则。

6. STALE 三级传播、连续性依赖登记、下游冻结粒度和成本账本策略。

7. 权限签发/升级流程、数据分级、威胁模型、审计策略。

8. ShotSpec、PrevizVersion、ControlSignalAsset、CameraRig、BlockingAsset Schema 与坐标/时间基准冻结。

9. SUPPORTING_ASSET_QUALITY、白模纯净度契约、几何体检最小规则集和控制维度 QC。

10. BlockingAdapter、ProductionRoute 能力声明、ControlSignalAsset 的 Rights/STALE 传播规则及 ADR-021~024。

Phase 0（契约冻结）验收：核心生产、社区、反推、资产、交付、学习和开放生态的 Schema/状态机/权限/审计/成本/STALE 契约可校验；未启用能力标记 NOT_ENABLED/UNVERIFIED。Phase 0 不要求全部 UI 或服务上线。

## Phase 1：核心生产闭环与辅助最小能力（P0/P1）

交付：核心生产主链完成项目创建、剧本导入、AI 拆镜草案、人工确认、ShotSpec/ShotVisualPlan、生成有效 ConstraintPack（如适用；无锁定约束镜头标记 NOT_APPLICABLE）、单一路线生成、核心 QC、人工 Gate 和可追溯 DeliveryManifest；辅助产品域按第 31.2/31.3 节单独灰度验收，不得阻塞核心生产主链。

验收：核心生产闭环作为 Phase 1 唯一主硬门；辅助域按其各自最小验收独立灰度，不得反向阻塞主链。至少一条真实 USER_OWN_KEY 路线可运行；输入、调用、资产、QC、人工决定、权利和交付均可追溯。白模 PoC/ControlSignal 消费能力未验证时不作为主生产路线前置。

## Phase 2：创意探索、记忆、组织社区与受控复用（P1）

交付：CEE 受控探索、DIE、L1-L4 镜头授权、Production Memory、Pattern 生命周期、动态调度；组织内社区、ReuseRequest、模板市场灰度、学习拆解增强和开放生态权限灰度。

验收：低置信意图按风险分流；L4 与资源 Profile 分离；Quality Reserve 不被探索占用；Pattern 非强制且经过治理；社区复用创建新 Project/ShotRevision 并重新执行权限/版权/Canon/预算/Gate 校验。

## Phase 3：RCA、公开社区与运营生态（P1/P2）

交付：RCA 生命周期、纠正/预防措施、离线 Pattern 评估、公开社区、TV Show 式首页、创作者主页、公开模板市场、发行适配、多语言、跨项目归一化基准和受控外部生态市场。

验收：重复重大缺陷有 RCA 或证据不足结论；公开内容通过完整 Publication Gate；能力矩阵有验证证据；市场内容可撤销、回滚并追踪受影响调用。

## Phase 4：规模化、可靠性与商业化扩展（P2）

交付：多项目并发、SLO/RTO/RPO、灾备、供应商故障切换、审计保留、成本偏差告警、交付包可重建、平台代付正式结算及经专项评审通过的收益分成/撮合/实时协作能力。

验收：完成 Worker/数据库/对象存储/工作流恢复演练；供应商未知状态不重复盲目计费；平台代付具备账务、支付、退款、对账、资金安全和适用地区合规证据；私有化 USER_OWN_KEY 主链路不依赖平台计费服务。

## Phase 0/1 待验证的技术决策

以下事项保留为显式待决项，不在架构层预先指定唯一答案。每项必须用代表性镜头样本、Adapter 实测、许可审查和成本数据形成技术评估记录；未验证能力标记为 `UNVERIFIED`，不得作为生产路由的可用能力。

1. **白模引擎首发选型：** Blender 脚本化、Three.js/CozyClay 或游戏引擎。比较无头/批处理能力、ShotSpec 到场景的自动化程度、相机/动画导出、坐标与单位转换、几何体检可实现性、渲染吞吐、资产许可、团队技能和迁移成本。建议使用静态构图、单主体运动、多主体/复杂相机三类代表镜头做 PoC，不以渲染画质单项决定。

2. **首发消费模型与网关接入：** Seedance 2.5 等候选仅作为待测对象，不是架构硬依赖。必须验证是否实际支持 `REFERENCE_VIDEO + MOTION_AND_CAMERA_CONTROL`，并检查时长、参考数量、提示语法、异步 Job 查询、输出规格、许可/数据处理条件和分维度控制误差。测试结论绑定 Model/Version/Adapter，不得外推到其他型号。

3. **角色身份一致性策略：** 对比“角色参考图 + 人工抽检”与独立 identity-preserving 服务。身份一致性属于外观/角色连续性控制，不属于白模 ControlSignalAsset 的默认能力。需按近景、遮挡、多人、快速运动分层评估误报/漏报、人工复核工作量和成本。

4. **白模直接生成的适用范围：** 不在架构阶段承诺全镜头覆盖。优先验证静态及低至中复杂度镜头；高复杂度多人交互、精细手部动作、复杂物理、强表演或多段连续运动保留图生视频、传统 CG、实拍合成或人工修正的混合路径。扩展范围必须由路线能力证据和项目验收数据推动。

每项决策记录候选方案、测试镜头集、能力覆盖矩阵、质量/成本/周期结果、许可与数据风险、未覆盖维度、回滚/替换方案、责任人和 ADR 版本。架构定义能力接口，不预设某个供应商当前一定满足能力要求。

### 白模能力增量的工作量评估说明

下表是历史技术工作量参考，不作为固定工期承诺。实际实施顺序与阶段门槛以依赖关系、风险、资源和验收证据为准；若外部依赖或模型 API 不稳定，应调整非核心能力，不得压缩核心 QC、权限和交付验收。

| **优先级** | **工作项** | **初步估算** | **主要交付** |
| --- | --- | --- | --- |
| P0 | ShotSpec/PrevizVersion/ControlSignalAsset Schema、支撑资产基线、BlockingAdapter 契约、几何体检、STALE、ADR-021~024 | 3–5 人周 | 冻结 Schema、接口、策略与测试 |
| P0 | 文档结构合并、去重、术语和 ADR 统一 | 1–2 人周 | 单一编号架构基线 |
| P1 | 一个 BlockingAdapter、一个消费模型、三元调用、控制维度 QC 原型 | 6–10 人周 | 单项目端到端闭环 |
| P1 | 权限矩阵、成本分解、S/A/B 策略和验收清单更新 | 1–2 人周 | 配置、审计与验收更新 |
| P2 | 跨模型路由、白模库、2D Layout/虚拟制片适配器和看板 | 8–12 人周 | 多路线扩展原型 |

这些估算不得视为交付承诺，也不代表上述功能已经实现。

## 优先级说明

- P0：区分 P0-ARCH（冻结全平台长期 Schema/接口/权限/状态边界）与 P0-MVP（Phase 1 核心生产闭环必须可运行）；同时冻结社区、反推、资产、客户审片、交付、学习和开放生态的最小契约。

- P1：组织内社区、受控复用、CEE、PMS/Pattern、角色卡/Canon 完整工作流、反推增强、交付中心增强和运营能力；按 Phase 2—3 启用。

- P2：平台代付真实充值/支付/退款/发票/月结、外部 Agent API 与第三方 Skill 市场、无限画布、自动创意差异度、多模型智能路由、复杂配额/RCA/SLO/灾备和跨项目高级分析。

策略解耦补充模块：13-Production-Policy 负责生产策略、全局质量策略、策略解析、镜头资源分配、预算审批与等级变更治理。新增第 24 章契约矩阵是其与全平台模块的统一验收补充，不替代既有模块数据所有权。

## 14. 工程目录、模块边界与数据所有权

```
my-ai-studio/
├── 01-pipeline-orchestrator/
│   ├── SKILL.md
│   └── references/
│       ├── api-contracts.md
│       ├── dag-gates.md
│       └── routing-policy.md
├── 02-ip-script/
│   ├── SKILL.md
│   └── references/
│       ├── stage-a.md
│       ├── stage-b.md
│       ├── stage-c.md
│       ├── quality-modules.md
│       └── economy.md
├── 03-storyboard/
│   ├── SKILL.md
│   └── references/
│       ├── shot-language.md
│       ├── shot-visual-plan.md
│       └── continuity.md
├── 04-production-creative/
│   ├── SKILL.md
│   └── references/
│       ├── production-routes.md
│       ├── control-signals.md
│       ├── qc-and-review.md
│       └── pattern-library.md
├── apps/
│   ├── api/
│   └── console/
├── services/
│   ├── orchestrator/
│   ├── project-planning/
│   ├── shot-planning/
│   ├── asset-service/
│   ├── rights-governance/
│   ├── model-gateway/
│   ├── qc-governance/
│   ├── creative-exploration/
│   ├── blocking-service/
│   ├── production-policy/
│   ├── identity-access/
│   ├── budget-quota/
│   └── observability/
├── workers/
│   ├── generation/
│   ├── qc/
│   ├── delivery/
│   └── notifications/
├── agents/
│   ├── runtime/
│   ├── planners/
│   └── evaluators/
├── adapters/
│   ├── comfyui/
│   ├── image_models/
│   ├── video_models/
│   ├── audio_models/
│   ├── blocking/
│   │   ├── blender/
│   │   ├── threejs/
│   │   └── ue/
│   ├── production_routes/
│   └── storage/
├── skills/
│   ├── project-intake/
│   ├── script-to-shots/
│   ├── creative-exploration/
│   ├── blocking-previz/
│   ├── control-signal-export/
│   ├── asset-resolution/
│   ├── shot-production/
│   ├── qc-triage/
│   ├── gate-review/
│   └── delivery/
├── data/
│   ├── schemas/
│   ├── migrations/
│   ├── seeds/
│   ├── examples/
│   └── policies/
├── scripts/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── permission/
│   ├── resilience/
│   └── e2e/
├── infra/
└── docs/
    ├── architecture/
    ├── adr/
    ├── runbooks/
    └── sop/
```

目录治理：Skills 定义 Agent 任务契约和边界；可执行代码放在 agents/workers/services；Adapter 只处理外部系统差异；Orchestrator 是工作流状态推进唯一权威入口；策略和质量规则配置化并版本管理；密钥不得进入仓库。

### 14.2 模块数据所有权矩阵

以下矩阵定义模块主数据写入权。模块内可通过领域服务读写本模块数据；其他模块只能经 API/命令或事件交互，不得直接写入对方数据表。

| **模块** | **核心数据所有权** | **允许写入者** | **主要对外契约** |
| --- | --- | --- | --- |
| Project & Planning | Project、Episode、Scene、Intent（项目级意图，作用域拆分见 ADR-R27 §3.5） | Project/Planning Domain Service | Project API、Intent API（项目级） |
| Shot Planning | Shot、ShotDraft、ShotRevision、ShotSpec、ShotVisualPlan、ConstraintPack、Intent（镜头级意图，作用域拆分见 ADR-R27 §3.5） | Shot/Planning Domain Service | Shot API、ShotChanged Event |
| Blocking & Previz | PrevizVersion、CameraRig、ControlSignalAsset 元数据、Geometry/Purity Validation | Blocking Domain Service | Previz API、ControlSignal Events、PrevizVersionCreated Event |
| Creative Intelligence | CreativeProposal、ExplorationRun、CreativeDecision、Pattern | Creative Service | Proposal API、Exploration Events |
| Orchestration | WorkflowRun、Task、Dependency、GateInstance、Outbox/Inbox | Orchestrator Domain Service | Command API、Task/Gate Events |
| Asset Management | Asset、AssetVersion、Manifest、Storage Metadata、BlockingAsset 文件版本 | Asset Service | Asset API、AssetVersionCreated Event |
| Model Gateway | Provider、ModelCapability、ModelInvocation、ProviderJob | Model Gateway | Submit/Status API、Invocation Events |
| Quality & Governance | QCReport、DetectorRegistry、Benchmark、QualityProfile、RightsCase、Override | QC/Governance Service | Evaluate API、Governance Gate Events |
| Memory & Learning | Memory、Canon Index、MemoryVersion、Feedback、Pattern Evidence | Memory Service | Memory Query/Write API |
| Cost & Operations | CostLedger、Forecast、Capacity Snapshot、RCA、SLO Metrics | Cost/Ops/RCA Domain Services | Forecast/RCA API、Operational Events |
| Authorization | Principal、Role/Policy、CapabilityToken、EscalationRequest、Audit | Authorization Service | Capability API、Audit API |

表仅定义逻辑所有权；初期模块化单体可共用一个 PostgreSQL 实例，但必须使用独立 Schema/表前缀、仓储接口和迁移责任边界。跨模块禁止直接写表；只读查询也应通过明确的 Query API/View，避免共享 ORM Model 泄漏内部结构。

### 14.3 模块间通信契约

- **同步 API/领域命令：** 用于需要立即得到业务校验结果的请求，如创建 Shot、审批 Gate、请求 Token、提交模型调用。跨模块同步调用必须有超时、幂等键、鉴权和结构化错误码。

- **异步事件：** 用于状态变化广播，如 `AssetVersionCreated`、`TaskCompleted`、`GateDecisionRecorded`、`RCAResolved`。事件由 Outbox 写入后发布，消费者 Inbox 去重。

- **Blocking & Previz 边界：** Blocking Service 拥有 ShotSpec 的派生 PrevizVersion、CameraRig、ControlSignalAsset 元数据与验证报告；Asset Service 拥有底层文件和 AssetVersion；Rights/Governance 拥有许可事实与审核状态。跨模块只通过 API/领域命令和 Outbox 事件连接，不得共享写表。

- **禁止共享数据库写：** 不得由 Module A 的代码直接 UPDATE Module B 的表。跨模块事务通过 Outbox + Saga/补偿动作完成。

- **模块内事务：** 单一领域模块内允许本地 ACID 事务；跨模块流程采用 Saga，明确每个步骤的补偿动作和不可补偿动作。

- **版本兼容：** API/事件 Schema 使用显式版本；向后兼容升级优先，破坏性变更需迁移窗口和 ADR。

模块化单体拆分条件：初期不拆微服务。只有当吞吐瓶颈、资源隔离、安全边界、团队独立发布或故障域隔离达到可量化门槛时才拆分。拆分时保持既有 API/Event Contract，不迁移跨模块直接写表模式。模块化单体必须从第一天执行数据所有权，不能把“以后再拆”作为共享数据模型的理由。

## 15. 架构决策记录（ADR）

每项核心决策均包含决策、理由、影响和对应正文。ADR 变更需提交新版本，不直接改写历史决策。

#### ADR-001：统一高品质基线

**决策：** S/A/B 共用同一逻辑质量治理基线；项目技术阈值可以校准，但必须审批、版本化并展示在 KPI 中。

**理由：** 生产资源等级不得决定成片质量等级。

**影响：** 第 1、4、5、12、16 章；跨项目比较需按 Benchmark/阈值版本分层。

#### ADR-002：硬约束与创意自由分离

**决策：** ConstraintPack 管理事实/版权/安全/交付硬约束，Creative Freedom Zone 管理开放表达空间。

**理由：** 避免模板锁死创意，同时保护故事与治理红线。

**影响：** 第 2、3、8 章；所有候选保存约束快照。

#### ADR-003：创意自主权按镜头授权且不等于审批权限

**决策：** L1-L4 与 S/A/B 完全解耦；自主权仅授权提案/生成/范围内修改，不含审批和发布。

**理由：** 镜头差异与项目等级标签无直接必然关系。

**影响：** 第 2、5、10、13、16 章。

#### ADR-004：艺术质量不由单一自动分数裁决

**决策：** 艺术质量默认 OBSERVATION；视觉偏差必须关联 Intent/Proposal；L3/L4 主观偏离先呈证据并人工判定。

**理由：** 检测器可能惩罚有意的风格创新。

**影响：** 第 4、8、10、16 章。

#### ADR-005：创意疲劳与治理命中分流

**决策：** CREATIVE_FATIGUE 只暂停探索分支；CREATIVE_GOVERNANCE_HIT 同时触发治理 Gate 和资产隔离。

**理由：** 版权/安全/事实冲突不能当作普通探索停止。

**影响：** 第 3、7、10、16 章。

#### ADR-006：镜头风险驱动资源调度

**决策：** 项目等级给基础资源，镜头风险与质量缺口触发升级；资源不足显式仲裁。

**理由：** 项目内镜头风险存在显著差异。

**影响：** 第 5、6、13 章。

#### ADR-007：Pattern 是建议，不是硬模板

**决策：** Pattern 标记为非强制，并通过治理控制探索储备和新颖性注入。

**理由：** 防止历史偏好固化审美。

**影响：** 第 3、11、12 章。

#### ADR-008：业务状态、执行状态和外部调用状态分层

**决策：** Project DB 保存业务态；Temporal 保存执行编排态；Provider Job 保存外部执行态；通过 Outbox/Reconciliation 对齐。

**理由：** 避免双写冲突并支持可恢复执行。

**影响：** 第 7、8、9、14 章。

#### ADR-009：模块化单体优先，数据所有权先行

**决策：** 初期模块化单体 + 异步 Worker；模块从第一天拥有明确数据写权限和 API/Event 契约。

**理由：** 控制部署复杂度并降低未来拆分成本。

**影响：** 第 1、14、16 章。

#### ADR-010：导演意图先结构化

**决策：** 自然语言导演意图形成版本化 Intent Contract，明确置信度、假设和审批状态。

**理由：** 防止推断伪装成导演明确指令。

**影响：** 第 2、8、16 章。

#### ADR-011：检测器必须经基准验证后才能升级决策权限

**决策：** 检测器按 EVIDENCE_ONLY/ADVISORY/AUTO_ROUTE/AUTO_BLOCK 分级；性能不足只呈证据。

**理由：** 避免不可靠检测器参与自动门禁。

**影响：** 第 4、10、13、16 章。

#### ADR-012：Override 透明化并接受累积治理

**决策：** 所有艺术性 Override 纳入比率、严重度、重复原因和质量逃逸看板；超阈值触发治理复核。

**理由：** 防止人工例外累积架空统一基线。

**影响：** 第 4、12、16 章。

#### ADR-013：L4 自主权与探索资源强度分离

**决策：** L4 可使用 COMPACT/STANDARD/EXPANDED；资源不足触发升级/确认，不静默降级自主权。

**理由：** 防止 B 项目因资源策略而让 L4 授权名存实亡。

**影响：** 第 5、6、13 章。

#### ADR-014：STALE 传播分级并按影响最小化返工

**决策：** DIRECT_STALE/INDIRECT_STALE/ADVISORY 联合 DAG 与连续性约束计算，局部冻结，成本留账。

**理由：** 避免隐式依赖漏检和全局级联重跑。

**影响：** 第 7、8、12、16 章。

#### ADR-015：外部模型调用防重复计费

**决策：** 保存 idempotency_key/provider_job_id 映射；提交状态未知先查状态，不盲目重提。

**理由：** 内部幂等不能保证供应商不重复扣费。

**影响：** 第 8、9、12、16 章。

#### ADR-016：Memory 先做权威过滤再语义排序

**决策：** 先按 scope/authority/validity 结构化过滤，CANON_LOCKED 使用精确索引。

**理由：** 防止高相似低权威内容覆盖锁定事实。

**影响：** 第 8、11、16 章。

#### ADR-017：成本预测必须支持冷启动

**决策：** 使用类比项目、专家区间和保守上界三级策略，低置信度强制人工复核。

**理由：** 新项目/新模型缺少历史数据时仍需可解释估算。

**影响：** 第 6、12、13 章。

#### ADR-018：Agent 采用独立授权服务与 Task 级 Token

**决策：** Capability Token 由 Authorization Service 按 Task 签发；权限提升重新签发并审计。

**理由：** 防止 Agent/Orchestrator 自行扩大权限。

**影响：** 第 10、14、16 章。

#### ADR-019：RCA 与学习结果可追溯、可复测、可回滚

**决策：** 根因必须基于证据；实验成本入账；模型/Pattern 学习离线验证后灰度。

**理由：** 避免把相关性当因果、把未经验证的经验变成生产规则。

**影响：** 第 11、12、13 章。

#### ADR-020：可靠性目标按组件定义并演练

**决策：** 分别定义 DB、对象存储、队列、Workflow Runtime、Worker 的 SLO/RTO/RPO，并做恢复演练。

**理由：** “可恢复”必须可量化和验证。

**影响：** 第 12、13、16 章。

#### ADR-021：白模是跨路线控制信号层，不是单一生成路线

**决策：** Blocking/Previz 通过 ControlSignalAsset 输出相机、空间/走位和时间控制信息；各生产路线通过能力契约选择消费方式。白模不得默认承担身份、最终外观、材质、灯光或精细物理控制。

**理由：** 将镜头控制与外观生成解耦，支持路线切换、混合制作和精确 STALE 传播。

**影响：** 第 0、1、7、8、9、14、16 章。

#### ADR-022：探索型 Previz 与基线型 Previz 分离

**决策：** `EXPLORATORY` 仅用于创意比较，不进入 ConstraintPack；`BASELINE` 经导演/授权负责人批准后，按 `locked_dimensions` 将空间/运动约束写入 ConstraintPack。

**理由：** 避免过早锁定抑制创意，同时保护已确认的相机、走位和时间决策。

**影响：** 第 2、3、7、8、10、16 章。

#### ADR-023：支撑资产质量与成片质量分离，控制质量单独评估

**决策：** 白模/Previz/ControlSignalAsset 使用 `SUPPORTING_ASSET_QUALITY`；成片仍使用 `UNIFIED_HIGH_QUALITY`。Control Quality 为按需启用的第五维，与视觉外观质量分开判定。

**理由：** 防止对白模套用成片审美标准，也防止控制信号达标被误认为最终画面达标。

**影响：** 第 4、8、10、12、13、16 章。

#### ADR-024：白模预算分类和路线能力声明必须显式化

**决策：** 白模/预演不占用付费生成模型预算，但算力、人工、3D 许可、渲染和返工仍进入 CostLedger；路线只能消费其 Adapter 明确声明并验证过的控制信号维度。

**理由：** 避免“免费”被误解为零成本，避免把参考输入能力误报为确定控制能力。

**影响：** 第 5、6、8、9、10、12、13、16 章。

策略解耦补充验收：必须验证项目/镜头等级正交、跨等级组合、核心质量策略不可覆盖、预算超限阻断、等级变更审计以及 DAG 最小化 STALE 传播；另须通过附录 C 的数据契约、权限和账本验收。

## 16. 总验收清单与原则—契约—验收映射

本清单是长期目标架构冻结和工程验收的总入口；Phase 1 核心生产闭环的实际功能范围与发布门槛由第 31.3 节细化。第 13、17、18—25 章的局部验收要求均须回填到本清单或其引用的版本化测试矩阵；不得存在绕开总验收清单的局部放行。

### 16.1 长期目标架构 P0 契约与能力验收清单

以下清单用于长期目标架构的契约冻结与能力验收，不代表所有能力必须在首发阶段同时实现。首发阶段只实现第 25 章明确的 P0-MVP；其余项目冻结 Schema/接口/权限边界，并明确标记 NOT_ENABLED/UNVERIFIED，待后续 Phase 启用。

- [ ] S/A/B 与 L1-L4 无配置继承、映射或隐式联动；L4 与资源强度分离。

- [ ] Temporal 执行态与 Project DB 业务态职责明确；Outbox/Inbox、版本检查和 Reconciliation 通过故障注入测试。

- [ ] 模块数据所有权、API/Event 契约、禁止跨模块写表规则和 Saga 边界明确。

- [ ] QC 检测器有 Benchmark、适用子类、性能报告和决策权限；未达门槛只能呈证据/建议。

- [ ] Intent-aware QC 已实现 `deviation_intent_ref`，并能区分有意/无意/未知偏差。

- [ ] 艺术质量不参与自动 PASS/FAIL；L3/L4 主观视觉偏差默认人工判定。

- [ ] Override Rate、加权 Override、重复 Override 和质量逃逸指标可计算；预警/治理复核规则可触发。

- [ ] STALE 支持 DIRECT_STALE/INDIRECT_STALE/ADVISORY；融合 DAG 与 ContinuityConstraint；局部冻结和 CostLedger 有效。

- [ ] 隐式连续性约束可登记、授权、建立依赖边并参与失效传播。

- [ ] Capability Token 独立签发、Task 级绑定、过期/撤销和权限升级流程通过测试。

- [ ] CRITICAL 法律/版权/安全/隐私问题不得由艺术 Override 绕过。

- [ ] ShotSpec、PrevizVersion、ControlSignalAsset、CameraRig、BlockingAsset 数据契约和坐标/时间基准已冻结。

- [ ] 探索型/基线型 Previz 状态分离；只有获批 BASELINE 的明确锁定维度可进入 ConstraintPack。

- [ ] 白模/控制信号使用 SUPPORTING_ASSET_QUALITY，不套用成片审美基线。

- [ ] 几何体检失败时 PREVIZ_RENDER、控制信号导出和付费生成保持 BLOCKED。

- [ ] 白模纯净度、Rights Gate 通过后才能发布控制信号；未知版权资产进入 QUARANTINED。

- [ ] A2 子轨道、Blocking DAG 节点、白模缺陷回流映射和 STALE 传播已实现。

- [ ] ModelAdapter/ProductionRoute 能力声明细分到 signal_type、reference_role 和 controlled_dimensions。

- [ ] 控制质量只评估已声明、已验证的维度；能力缺口不得伪装成等价支持。

- [ ] 路线调用记录控制信号、外观参考和 Prompt 的独立引用及 reference_role。

- [ ] 白模费用不占付费生成模型预算，但算力/人工/许可/渲染/返工进入 CostLedger。

- [ ] 四类预算账户用途唯一且全篇一致；Quality Reserve 不可被 CEE 常规探索扣费；预算账本采用追加式交易和幂等键。

- [ ] CharacterCard、CharacterCanon、CharacterAppearance、CharacterVoiceProfile、CharacterReferenceSet、ShotVisualPlan、SkillManifest、PatternRecord、ExplorationBranch、QCReviewSession、QuotaReservation 的 Schema/Owner/状态/权限已冻结。

- [ ] 正式生产轨道与探索分支的 DAG 节点、Gate、Promote/Reject 和 STALE 传播规则已定义并通过契约测试。

- [ ] RBAC/ABAC 角色矩阵、职责分离规则和 Capability Token 资源范围/动作/预算/时效约束已通过正反向授权测试。

- [ ] 预算与算力配额预留、承诺、释放、供应商未知结果对账、重复回调和 Worker 崩溃恢复通过并发及故障注入测试。

- [ ] P0/P1/P2 与 Phase 0—4 映射已统一；内部 Skill Runtime 与外部 Skill API 的范围和上线阶段已区分。

### 16.2 P1 实施前门槛

- [ ] Intent 置信度分层、低风险继续/高风险阻塞和超时策略实现；未确认不等同批准。

- [ ] B 级 L4 可走 COMPACT 探索或资源追加；资源不足不会静默降级授权。

- [ ] 成本冷启动支持类比、专家估算和保守上界；低置信度有人工复核。

- [ ] ModelInvocation 记录 idempotency_key/provider_job_id；未知提交先查询；重复计费进入 CostLedger。

- [ ] 模型切换有技术、连续性、视觉/风格、合规与运营等价验证框架。

- [ ] Memory 检索先做 scope/authority/validity 过滤；Canon 精确索引不依赖向量召回。

- [ ] Pattern 标记非强制；探索保留比例由治理角色控制，项目不可擅自降到下限以下。

- [ ] RCA 有证据链、SLA、实验成本归属、纠正措施和复测结果。

- [ ] 至少一个 BlockingAdapter 与一个 ProductionRoute 完成端到端集成测试。

- [ ] 白模变更可按 DIRECT_STALE/INDIRECT_STALE/ADVISORY 传播到所有实际消费对象。

- [ ] Blocking Pattern、Character/Location Memory 对 Previz 的引用有权威等级和审批来源。

- [ ] CharacterAppearance、CharacterVoiceProfile、CharacterReferenceSet 独立版本化并能与 CharacterCard/Canon 正确关联。

- [ ] Pattern 生命周期包含实验、审核、验证、复用、拒绝和失败归档；单次偏好不会自动变成跨项目硬规则。

- [ ] QCReviewSession 支持多人意见、冲突裁决、人工豁免审计和检测器反馈闭环。

- [ ] 配额服务具备原子预留、租约超时释放、并发公平调度和 CostLedger 对账。

- [ ] 受控外部 Skill API 有版本、限流、撤销、审计和沙箱边界；不得获得内部数据库写权限。

### 16.3 P2 规模化门槛

- [ ] 自动创意差异度只有在冻结基准与分层性能验证通过后才可升级，否则保持人工辅助。

- [ ] 跨项目 KPI 展示阈值/基准版本，并采用分层或归一化比较。

- [ ] SLO、RTO、RPO、灾备、Worker 恢复、队列背压和供应商切换完成演练。

- [ ] 审计、数据保留、权限撤销、密钥轮换和资产来源证据满足项目治理要求。

### 16.4 原则—契约—验收映射表

| **原则** | **关键契约/机制** | **对应验收** |
| --- | --- | --- |
| 品质平权 | QualityProfile、Benchmark、ThresholdVersion | 16.1 检测器/Override，16.3 KPI 归一化 |
| 创意自由 | IntentContract、CreativeProposal、deviation_intent_ref | 16.1 Intent-aware QC，16.2 差异度降级 |
| 自主权解耦 | AutonomyGrant、ExplorationProfile | 16.1 S/A/B-L1-L4 解耦与 B-L4 |
| 状态可靠 | Project DB、Temporal、Outbox、Reconciliation | 16.1 状态一致性测试 |
| 精准返工 | DAG、ContinuityConstraint、STALE 分类 | 16.1 STALE 与局部冻结 |
| 费用可控 | ModelInvocation、CostLedger、CostForecast | 16.2 未知提交和冷启动 |
| 安全治理 | CapabilityToken、AuthorizationService、AuditEvent | 16.1 Token 和红线不可绕过 |
| 持续学习 | MemoryAuthority、Pattern、RCA、OfflineEvaluation | 16.2 Memory/Pattern/RCA |
| 工程可演进 | ModuleOwnership、API/Event Contract、Saga | 16.1 模块边界 |
| 白模支撑资产 | ShotSpec、PrevizVersion、SUPPORTING_ASSET_QUALITY、Geometry/Purity Gate | 16.1 白模支撑资产和几何/纯净度门禁 |
| 跨路线控制 | ControlSignalAsset、ModelCapability、ProductionRoute、Control QC | 16.1 路线能力与控制维度验收 |
| 白模返工 | Previz/ControlSignal version、DAG、STALE、CostLedger | 16.1 STALE 传播和成本留账 |

### 16.5 冻结条件

第 16.1 节是长期目标架构的契约冻结清单，不代表所有能力必须在同一阶段实现。Phase 1 发布/验收以第 31.3 节核心生产硬门清单为准；未实现的 P1/P2 能力必须保持 NOT_ENABLED/UNVERIFIED，不能用接口预留宣称已交付。

## 17. 项目级与镜头级生产策略解耦

本章将项目生产分级与镜头生产分级定义为两个正交维度，补充第 5 章统一品质与资源策略、第 6 章镜头级动态资源调度、第 8 章领域模型、第 10 章质量治理、第 13 章实施路线和第 16 章验收体系。

核心决议：项目等级决定项目整体的组织方式、预算边界、审批制度和默认生产策略；镜头等级决定单个镜头的创意探索深度、制作路线、资源投入和人工介入程度。项目等级不构成镜头生产等级的上限，镜头等级不改变项目整体等级。

### 17.1 设计目标与非目标

- 解除项目 S/A/B 与镜头 S/A/B 的强绑定，允许跨等级组合。

- 把高价值、高风险、高复杂度镜头的资源集中起来，而不是按项目等级平均投入。

- 允许旗舰项目中的普通镜头采用高效率路线，避免全片过度制作。

- 允许标准或敏捷项目中的关键镜头申请更高制作资源，但必须经过权限、预算和审批校验。

- 通过策略解析、预算账本、版本快照和审计记录，使资源调整可解释、可追溯、可回滚。

- 所有项目和镜头等级均不能降低全局质量基线、关闭最终 Gate 或绕过版权、安全与合规要求。

非目标：本机制不是让镜头等级自动决定艺术质量；不是允许导演或 Agent 无预算上限地申请旗舰资源；不是要求所有镜头都使用白模、LoRA、多分支探索或同一种生产路线；也不是项目等级变化后自动重做全部镜头。

### 17.2 双维度模型

| **维度** | **核心对象** | **负责决定** | **不负责决定** |
| --- | --- | --- | --- |
| 项目级 | ProjectProductionProfile | 项目预算与资源池、交付周期、人工管理制度、项目资产策略、默认镜头策略、项目风险策略 | 不直接规定每个镜头必须采用的制作等级 |
| 镜头级 | ShotProductionProfile | 创意探索深度、模型路线、白模/控制信号使用、专属资产投入、算力预算、人工评审密度 | 不改变项目整体等级、合同方案或项目级治理制度 |
| 全局治理 | GlobalQualityPolicy | 核心质量红线、强制检查、最终交付门禁、不可豁免合规规则 | 不规定统一艺术风格，不锁死所有镜头的创意表达 |

### 17.3 项目生产方案与镜头生产等级

项目级 S/A/B 继续作为项目组织和商业方案标签；镜头级 S/A/B 作为单镜头制作策略标签。对外商业名称可采用旗舰定制版、商业标准版、敏捷高效版；内部系统保留 S/A/B 作为稳定枚举值。

| **镜头等级** | **定位** | **典型场景** | **生产策略特征** |
| --- | --- | --- | --- |
| S｜旗舰级 | 高价值、高复杂度或高不确定性镜头 | 主角特写、关键情绪戏、复杂动作、核心视觉奇观 | 深度创意探索、多方案小样、专属资产、复杂控制信号、全镜头导演深度评审 |
| A｜标准级 | 常规叙事与中等复杂度镜头 | 普通叙事镜头、重要对话、常规场景 | 受控探索、复用与定向定制结合、关键节点人工评审 |
| B｜敏捷级 | 低复杂度、低不确定性镜头 | 过场、转场、简单环境镜头、标准化素材 | 默认单方案、成熟模板和资产复用、自动化 QC 为主，风险触发人工评审 |

镜头 B 级表示制作复杂度和探索投入较低，不表示允许降低核心质量标准。无法达到统一验收标准时，应返工、改路线或申请资源升级，不得降低验收阈值。

### 17.4 项目等级 × 镜头等级二维矩阵

| **镜头等级 / 项目等级** | **旗舰项目 S** | **标准项目 A** | **敏捷项目 B** |
| --- | --- | --- | --- |
| S 级镜头 | 按项目预算与审批执行旗舰生产 | 可申请旗舰资源，审批通过后执行 | 可申请旗舰资源，审批通过后执行 |
| A 级镜头 | 标准化生产，可共享项目资产 | 标准化生产 | 标准化生产 |
| B 级镜头 | 高效生产，不因项目旗舰身份强制过度制作 | 高效生产 | 高效生产 |

该矩阵表达策略兼容性，不是自动授权。任何镜头的实际资源投入仍须通过权限、预算、排期、风险和模型能力校验。

### 17.5 生产策略解析引擎（Policy Resolution Engine）

策略解析引擎按字段解析策略，不采用简单的整对象覆盖。建议决策顺序如下：

| **顺序** | **策略来源** | **处理规则** |
| --- | --- | --- |
| 1 | GlobalQualityPolicy | 核心质量红线与不可豁免规则始终生效，任何项目或镜头配置不得覆盖。 |
| 2 | 法律、版权、安全与合规约束 | 执行强制限制；不得通过人工例外绕过法律或合规要求。 |
| 3 | 导演明确锁定的镜头约束 | 尊重已授权锁定的镜头事实；修改须走对应变更流程。 |
| 4 | 镜头级已审批生产策略 | 在授权范围内优先于项目默认策略。 |
| 5 | 项目级默认生产策略 | 为未配置或未审批的镜头提供默认值。 |
| 6 | 系统推荐策略 | 仅作为建议，不自动获得预算、审批或执行权限。 |

每次解析必须生成不可变 PolicyResolution 快照，记录输入策略版本、字段来源、决议结果、校验结果、生效时间，并绑定 ShotRevision、项目策略版本和 GlobalQualityPolicy 版本。执行中的任务读取固定快照，不得被后台静默改写。

实现约束：策略规则以配置表 + 声明式规则实现，禁止在领域服务内堆叠项目等级、镜头等级、自主权等级与资源 Profile 的条件分支；每条规则必须带版本、适用范围与生效时间，解析结果生成不可变 PolicyResolution 快照。

### 17.6 镜头级资源调度与预算治理

ShotResourceAllocator 根据镜头叙事价值、创意不确定性、技术复杂度、角色一致性风险、失败/返工风险、截止时间和预算余额提出资源分配建议。算法只负责评估与推荐，不得越权批准预算或突破项目资源池。

| **预算账户** | **用途** | **约束** |
| --- | --- | --- |
| Production Budget | 正常制作、模型调用、人工制作和资产定制 | 项目总预算约束；不得隐瞒超支或跨项目挪用 |
| Exploration Reserve | 创意提案、静帧、多方案小样、探索分支 | 不得替代质量修复预算 |
| Quality Reserve | 质量问题返工、质量修复和必要的质量保障 | 不得被常规创意探索消耗 |
| Contingency Reserve | 经批准的突发变更和不可预见事项 | 不得成为绕过预算审批的隐性资金池 |

项目预算是总约束，镜头预算是分配额度。Phase 1 仅允许在同一预算账户内，对已批准额度进行有审计的重新分配；Phase 1 不启用跨账户自动转拨。跨账户转拨长期保留 BudgetTransferRequest，须由人工申请、人工审批并经原子记账校验，最早归入 Phase 2 以后启用；不得通过后台任务、Agent 或 Worker 自动改变账户归属。Quality Reserve 不得自动转入 Exploration Reserve，镜头预算不足时停止新增承诺并请求人工决策。

Phase 1 的同账户额度重分配不单设 action，由 `BUDGET.SHOT.APPROVE` 承载（resource 为 ShotBudgetRequest/BudgetLedger，落账由 BudgetLedger 内部 adjust 命令执行）：必须走 ShotBudgetRequest 申请、申请人与审批人分离、每次调整在 BudgetLedger 留下可追溯流水；无审批的重分配、跨账户重分配、以及借重分配挪用 Quality Reserve 的行为一律拒绝。负向测试挂在 ACT-BUDGET-NEG-002 之下，新增三个用例：未审批重分配、跨账户伪装成同账户、挪动 Quality Reserve。

### 17.7 镜头等级变更工作流

镜头升级、降级和生产策略重配置均须通过正式变更流程；禁止直接修改 effective_tier 或后台自动变更已锁定策略。状态建议：DRAFT → SUBMITTED → EVALUATING → APPROVED / REJECTED → EXECUTING → COMPLETED；另支持 CANCELLED、FAILED、ROLLED_BACK。

变更记录至少包含：变更前后等级、字段级差异、申请原因、预期收益、成本增量、周期影响、预算来源、申请人、审批人、权限凭证、策略快照版本、关联 ShotRevision、DAG 影响范围、实际成本、执行结果和回滚记录。

### 17.8 与 DAG、Gate、STALE 的关系

- 镜头等级变化本身不等于镜头所有资产失效。

- 只有策略变化实际影响任务输入、参数、模型路线、控制信号或验收契约时，才将受影响任务及其下游依赖按既有 STALE 规则传播。

- 不受影响且版本、权限、版权状态均有效的资产允许复用。

- 所有等级都必须经过相同的核心质量 Gate；人工评审密度可以不同，但核心自动化检查不能因等级降低而关闭。

- 新策略增加的 QC 项必须在正式交付前完成；不得通过更改等级删除已经产生的不合格结果。

- Agent 可以在 Capability Token 授权范围内发起变更申请，但不得自行审批、突破预算或绕过 Gate。

### 17.9 全局质量基线与人工评审分离

质量检查覆盖率与人工评审密度是两个不同概念。所有镜头均执行核心自动化检查和最终门禁；项目或镜头等级仅影响额外人工评审、创意评审和深度复核投入。

| **检查/评审项目** | **所有等级共同要求** | **等级差异** |
| --- | --- | --- |
| 核心技术 QC | 所有镜头强制执行 | 不允许关闭 |
| 角色一致性与环境特效基线 | 所有镜头强制执行 | 不允许因等级降低而豁免 |
| 版权、权利来源与合规检查 | 所有镜头强制执行 | 不允许关闭；权利不明按既定隔离规则处理 |
| 最终质量 Gate | 所有镜头强制执行 | 不允许关闭 |
| 导演深度评审 | 按风险和项目策略安排 | S 镜头通常更高密度；关键镜头可强制人工评审 |
| 创意多方案评审 | 按创意价值和不确定性决定 | S 更常见，A 受控，B 默认单方案但可申请 |

禁止静默降质：任何缩减资源、改变制作路线、降低评审密度或调整验收参数的操作，都必须明确标记变更类型、影响范围、责任人和审计记录。法律、版权、安全和不可豁免质量红线不得通过例外流程绕过。

### 17.10 核心数据实体与字段

| **实体** | **关键字段建议** | **职责** |
| --- | --- | --- |
| ProjectProductionProfile | project_id, project_tier, default_shot_tier, budget_pool_id, policy_version | 项目生产策略与默认值 |
| ShotProductionProfile | shot_id, requested_tier, effective_tier, profile_version, status | 镜头生产策略 |
| PolicyResolution | resolution_id, shot_id, source_versions, resolved_fields, validation_result | 字段级策略解析快照 |
| ShotResourceAllocation | shot_id, resource_type, allocated_amount, reserved_amount, approval_id | 镜头资源分配 |
| ShotBudgetLedger | shot_id, account_type, transaction_id, amount, source, approval_id | 镜头成本流水 |
| ShotTierChangeRequest | request_id, from_tier, to_tier, reason, cost_delta, requester, approver, status | 镜头等级变更申请 |
| TierChangeAudit | audit_id, request_id, before_snapshot, after_snapshot, actor, timestamp | 变更审计 |

#### 17.10.1 配置示例

```
ProjectProductionProfile:
  project_id: PROJECT-001
  project_tier: B
  default_shot_tier: A
  budget_pool_id: PROJECT-BUDGET-001
  global_quality_policy: GQP-001
  policy_version: 1.0

ShotProductionProfile:
  shot_id: SHOT-023
  requested_tier: S
  effective_tier: S
  status: APPROVED
  production_policy:
    creative_exploration: deep
    previz_control: enabled
    dedicated_assets: enabled
    human_review: full
  approval:
    status: APPROVED
    approver_id: USER-APPROVER-001
    budget_approval_id: BUDGET-APPROVAL-023
  inherited_from:
    project_profile_id: PROJECT-001
  policy_resolution_id: RESOLUTION-023
```

### 17.11 新增模块与目录建议

```
13-Production-Policy/
├── SKILL.md
├── policies/
│   ├── global-quality-policy.yaml
│   ├── project-tier-policy.yaml
│   ├── shot-tier-policy.yaml
│   ├── resource-allocation-policy.yaml
│   └── exception-policy.yaml
├── services/
│   ├── policy-resolution-engine.py
│   ├── shot-resource-allocator.py
│   ├── tier-change-service.py
│   ├── budget-allocation-service.py
│   └── policy-validation-service.py
├── workflows/
│   ├── shot-tier-change.yaml
│   └── budget-approval.yaml
├── schemas/
│   ├── project-profile.schema.json
│   ├── shot-profile.schema.json
│   ├── policy-resolution.schema.json
│   ├── tier-change-request.schema.json
│   └── resource-allocation.schema.json
└── tests/
    ├── test_quality_policy_immutability.py
    ├── test_project_shot_decoupling.py
    ├── test_budget_ceiling.py
    ├── test_tier_change_workflow.py
    └── test_stale_minimal_propagation.py
```

### 17.12 实施阶段与验收标准

| **阶段** | **实施内容** | **验收标准** |
| --- | --- | --- |
| Phase 0｜治理底座 | GlobalQualityPolicy、项目/镜头 Profile Schema、不可覆盖规则、策略版本与审计 | 项目/镜头等级无法关闭核心 QC 或最终 Gate；策略可校验、可追溯 |
| Phase 1｜双维度策略 | ProjectProductionProfile、ShotProductionProfile、Policy Resolution | B 项目可配置经批准的 S 镜头；S 项目可使用 B 镜头；两者互不改写 |
| Phase 2｜资源与预算 | ShotResourceAllocator、镜头预算账本、预算审批 | 预算不足时不执行新增资源；生产、探索、质量返工分账 |
| Phase 3｜变更与 DAG | 等级变更工作流、DAG 影响分析、STALE 精确传播、回滚 | 变更可审计；不相关资产不被无差别置为 STALE；可恢复到指定策略版本 |
| Phase 4｜规模化调度 | 跨项目资源池、镜头优先级调度、策略效果分析 | 在预算和质量红线约束下优化资源利用率，不以降低质量作为优化手段 |

### 17.13 关键验收用例

1. B 级项目中的 SHOT-023 申请 S 级：预算充足、权限合规、审批通过后，镜头策略变为 S，项目等级仍为 B。

2. S 级项目中的 SHOT-010 设置为 B：允许采用敏捷生产策略，但核心 QC 和最终 Gate 仍全部执行。

3. 镜头申请 S 级但预算不足：不得静默执行，也不得自动挪用 Quality Reserve。

4. 镜头等级变更但不影响现有资产输入和任务契约：不触发无关 DAG 节点 STALE。

5. 镜头等级变更导致模型路线或控制信号输入变化：只使受影响任务及其依赖方按规则 STALE。

6. 任何项目或镜头配置尝试关闭核心质量红线或最终 Gate：策略校验失败并写入审计日志。

7. Agent 提交镜头等级变更：必须持有相应 Capability Token；Agent 不得自行审批或绕过预算 Gate。

8. 镜头策略升级执行失败：保留原策略快照、记录已发生费用，并按审批流程回滚或重新申请。

### 17.14 架构决策记录（ADR）

| **ADR** | **主题** | **决策摘要** |
| --- | --- | --- |
| ADR-025 | 项目级与镜头级生产策略解耦 | 项目等级不构成镜头等级上限；项目与镜头分别建模。 |
| ADR-026 | 生产策略字段级解析 | 通过 Policy Resolution Engine 解析全局、合规、导演锁定、镜头审批和项目默认策略。 |
| ADR-027 | 镜头级资源分配与预算账本 | 镜头独立分配资源，项目总预算约束；Production、Exploration、Quality、Contingency 分账。 |
| ADR-028 | 镜头等级变更与 DAG 最小化 STALE | 变更必须审批审计；只影响真实依赖变化的任务，不全量重跑。 |
| ADR-086 | 平台 Phase 0—4 只有第 31.2 节为权威阶段映射；其他章节和附录的阶段表均为非权威摘要或生产工序说明。 | P0/P1/P2 与 Phase 分离；CI/文档检查确保引用 31.2，不允许重复定义交付边界。 |
| ADR-087 | Phase 1 社区范围只包含只读分享、内部审核记录和组织内封闭灰度，不开放公开审核发布。 | 公开发布入口关闭；组织内正式社区审批属于 Phase 2，公开社区属于 Phase 3。 |
| ADR-088 | ConstraintPack 是存在锁定约束时的核心生产有效输入；无锁定约束镜头显式标记 NOT_APPLICABLE。 | ShotSpec/ShotVisualPlan → ConstraintPack（如适用）→ 单一路线生成；锁定约束缺失/失效时阻断对应路线。 |
| ADR-089 | Action Registry 记录完整资源、作用域、风险、Token、审批、审计、负向测试和启用状态；历史动作名仅映射别名。 | 规范 action_key 是 API/Agent/UI/审计/测试唯一动作标识；含义不明确的旧名禁止自动映射。 |
| ADR-090 | CommunityPost、PublicationReview、Appeal、ReuseRequest、DeliveryManifest、Pattern 等对象状态机分别独立定义。 | Appeal 决定码、ReuseRequest 异常态、DeliveryManifest 生命周期和 Pattern DEPRECATED/RETIRED 状态必须纳入测试。 |
| ADR-091 | Phase 1 不启用跨预算账户自动转拨；仅允许同一预算账户内经审批的额度重分配。 | BudgetTransferRequest 长期保留；跨账户转拨人工审批，Phase 2 以后启用，必须原子记账并具备幂等/回滚测试。 |
| ADR-092 | 客户观看/批注统一使用“客户审片”术语；“审计”仅用于日志、证据链和权限追溯。 | 客户审片门户、审片链接、审片中心与内部审计日志在文案、权限和业务对象上分离。 |

ADR-025 至 ADR-028 为项目级与镜头级策略解耦新增决策，承接既有 ADR-001 至 ADR-024，不覆盖既有决策。

### 17.15 最终架构决议

**项目级生产策略与镜头级生产策略彻底解耦。项目等级决定生产组织方式、预算资源池和默认策略；镜头等级决定单镜头的制作深度、探索投入、模型路线和人工介入程度。项目等级不构成镜头生产等级的上限，镜头等级变化不改变项目整体等级。所有策略变更必须经过权限、预算、依赖、质量策略和审计校验。所有等级共享统一核心质量红线与最终交付门禁。**

## 18. 统一工作台、Agent Skill API 与工作流交互层

本章补齐产品入口与执行入口的统一架构。导演工作台和 Agent Skill API 是同一领域服务、同一权限体系、同一 DAG 与同一资产版本体系的两种交互入口，不得各自实现一套生产状态或绕过统一 Gate。

### 18.1 双入口、单一事实源

- 导演工作台（Director Workbench）：面向导演、制片、编剧、美术、剪辑和 QC 人员，提供项目概览、剧本/分镜审阅、镜头画布、DAG 状态、资产版本、双屏 QC、审批和预算看板。

- Agent Skill API：面向内部 Agent、自动化脚本和经授权的外部 Agent，提供结构化任务能力；Skill 只声明任务目标、输入输出契约、权限要求和失败语义，不直接持有跨模块数据库写权限。

- 两种入口统一调用 API / Domain Command，由 Orchestrator 推进状态；任何 UI 或 Agent 均不得直接修改 Task、Gate、ShotRevision、PolicyResolution 或预算账本的权威状态。

- P0 先冻结统一命令接口、内部 Skill Manifest 与权限契约；内部 Skill Runtime 在 Phase 1 上线。P1 受控外部 Skill API 在 Phase 3 上线；P2 第三方生态与复杂画布在 Phase 4 评估。

### 18.2 DAG 可视化与创意分支隔离

- 工作台必须展示节点状态、输入版本、输出资产、Gate 决策、依赖边、失败原因、STALE 等级、重试次数、成本和负责人。

- 正式生产轨道（Formal Track）与创意探索分支（Exploration Branch）在数据对象、预算账本、权限和状态空间上隔离；探索分支通过明确的 Promote/Reject 操作进入正式轨道。

- 局部重算只针对发生变化且依赖受影响的节点；复用资产必须验证版本、权利状态、策略快照和输入契约仍有效。

- 任何分支合并都生成新的 ShotRevision 或 CreativeProposal 版本，并保留父版本、差异摘要、审批记录和来源引用；禁止覆盖式合并。

### 18.3 Agent Skill 契约与权限

每个 Skill 至少定义：skill_id/version、purpose、input_schema、output_schema、required_capabilities、allowed_commands、budget_scope、timeout/retry、idempotency_key、side_effects、human_approval_points、failure_codes、audit_fields。

Skill 的输出默认为 DRAFT / PROPOSAL；涉及 Canon、ConstraintPack、ScriptLock、ShotLock、PolicyResolution、预算拨付、Gate 通过和正式交付的写入，必须调用独立领域命令并满足对应审批。Capability Token 应绑定主体、项目、镜头、动作、资源范围、有效期和调用次数/额度。

### 18.4 导演工作台最小信息架构

| **工作区** | **核心能力** | **权威数据来源/边界** |
| --- | --- | --- |
| 项目驾驶舱 | 项目状态、集/镜头进度、预算池、风险、Gate、资源负载 | Project DB 与只读聚合视图；不得直接改写子域状态 |
| 创作工作区 | 剧本、角色、场景、分镜、ShotVisualPlan、CreativeProposal | DIE/Script/Shot 领域服务；草案与锁定版本分离 |
| 镜头生产工作区 | ShotSpec、Previz、路线选择、模型能力、任务 DAG | Shot/Blocking/Orchestrator 服务 |
| 资产与记忆工作区 | CharacterCard、Canon、Pattern、PMS、版权状态 | Asset/PMS/Rights 服务 |
| 质量与审核工作区 | QC 双屏、差异定位、人工意见、Gate 审批 | QC/Governance 服务 |
| 运营工作区 | 成本账本、预算池、配额、RCA、审计 | CostLedger/Policy/Observability 服务 |

## 19. 角色资产、Canon、ShotVisualPlan 与 Skill 内容治理

### 19.1 CharacterCard 与 CharacterCanon 分离

CharacterCard 是可版本化的制作资产集合，用于角色外观和表演参考；CharacterCanon 是经授权确认的故事事实与身份连续性权威记录。二者必须分离，避免参考图、生成结果或模型推断自动升级为剧情事实。

- CharacterCard 建议字段：character_id、card_version、approved_appearance_refs、front/side/three_quarter_views、wardrobe_variants、expression_refs、body_language_refs、voice_profile_ref、negative_constraints、rights_manifest、source_refs、approval_status。

- CharacterCanon 建议字段：canon_id、character_id、canon_version、immutable_facts、timeline_state、relationships、known_unknowns、authority、approved_by、valid_from/to、supersedes。

- 角色卡可包含多个经批准的造型版本；造型变化必须关联剧情时间、场景、服装状态或导演决策，不得把所有参考图混成一个无条件身份模板。

- 生成资产可提出 CharacterCard 更新建议，但不得自行覆盖已批准卡片或 Canon；更新必须经过来源、版权、连续性和人工审批。

### 19.2 ShotVisualPlan：专业参数与创意自由边界

ShotVisualPlan 是镜头表达计划，不是对生成模型的僵硬模板。它应结构化表达导演意图和已确定的专业参数，同时保留未锁定的创意自由区。

- 建议字段：shot_id/version、intent_summary、story_function、emotional_beat、subject_performance、camera_language、shot_size、lens_range、camera_motion_intent、lighting_intent、composition_notes、color_mood、VFX_intent、sound_rhythm_intent、locked_fields、open_fields、references、confidence、approval_state。

- 未锁定字段是建议或探索空间；锁定字段必须带锁定人、时间、理由、适用版本和变更审批策略。ShotVisualPlan 不自动等于 ShotSpec；只有涉及可执行的相机/走位/时长/画幅等约束时，才经确认映射到 ShotSpec/ConstraintPack。

- AI 可以对导演初始方案提出替代构图、运镜、光线、表演或视觉隐喻；必须说明预期收益、风险和与已锁定意图的差异，不得静默修改锁定字段。

### 19.3 AI 剧本拆镜 Skill：草案优先、人工确认

Script-to-Storyboard Skill 负责将剧本段落拆解为场景、节拍、镜头候选、对白/动作/声音提示和连续性依赖。其输出是可编辑草案，不直接写入 ScriptLock、ShotLock 或正式 ConstraintPack。

- 推荐流程：输入剧本版本 → 解析场景与叙事节拍 → 生成镜头候选 → 连续性/时长/可拍性检查 → 人工逐镜或批量确认 → 创建正式 ShotRevision → 进入 DAG。

- 确认界面必须支持接受、编辑、拆分、合并、拒绝和重新生成；所有变更保留来源段落、模型/Skill 版本和差异。

- 对白、事实、角色关系、时间线和世界观冲突必须标记，不允许 Skill 自行补写成 Canon。

## 20. CEE 创意探索预算、分支策略与 Pattern 治理

### 20.1 四类预算账户与用途边界

| **预算账户** | **允许用途** | **禁止用途/耗尽行为** |
| --- | --- | --- |
| Production Budget | 已批准正式生产、模型调用、渲染、合成和交付 | 不得无审批转为探索预算 |
| Exploration Reserve | 候选方案、关键帧探索、低清动态样片、创意挑战 | 不得支付常规质量修复；耗尽后停止新增探索 |
| Quality Reserve | QC 失败修复、质量补偿、必要重生成和最终质量保障 | 不得用于常规创意分支或追求非必要风格变化 |
| Contingency Reserve | 不可预见故障、供应商变化、紧急恢复 | 必须经指定角色审批和单独记账 |

预算账户必须由独立账本记录授权额度、冻结额度、承诺成本、实际成本、退款/补偿、调拨来源和审批人。预算不足只能触发停止、排队或人工决策，不得自动借用其他账户。

### 20.2 CEE 探索漏斗与动态候选数

- 探索漏斗：文本提案（低成本）→ 关键帧静帧（中成本）→ 低清短动态样片（较高成本）→ 正式高清生产（最高成本）。按镜头不确定性和证据充分度选择阶段，不要求每个镜头走完整漏斗。

- 候选数按叙事价值、创意不确定性、技术风险、历史失败率、预算余额和截止时间动态建议；简单镜头可只有一个方案，中等镜头可少量对比，关键镜头可申请更深探索。候选数是策略结果，不写死为固定 3/5 个。

- 探索触发应有证据：导演主动请求、关键叙事节点、视觉差异不足、模型路线不确定、连续性风险或预期质量收益显著。普通镜头默认不自动开多分支。

### 20.3 停止条件与熔断

- 停止条件包括：边际质量收益低、候选伪多样化、探索预算/时间耗尽、模型或版权边界触发、连续性冲突无法解决、重复失败、分支级成本熔断。

- CEE 创意熔断与 QC 治理熔断分开：创意熔断停止新增探索，不得阻止必要质量修复；QC 熔断阻止不合格资产进入下一 Gate。

- 保留人工判断通道：AI 对差异度、艺术价值和边际收益的判断仅为建议；Phase 0/1 不得依赖未经验证的自动语义评分决定淘汰关键创意方案。

### 20.4 Pattern Library 与实验记录分级

- REUSABLE_PATTERN：经过质量证据、适用边界、来源/版权和复用验证，可跨项目推荐。

- EXPERIMENT_RECORD：探索记录，可能有价值但尚未完成验证；默认仅限原项目或明确授权范围。

- REJECTED_PROPOSAL：被拒绝方案，保留拒绝原因和上下文，不等同于失败。

- FAILURE_CASE：有可复现失败证据、原因分类和适用边界的失败案例。

- Pattern 推荐必须展示适用边界、来源、版本、历史采用情况和失败案例；默认 SUGGESTION_NOT_MANDATORY。不得因历史采纳率高而压制新路径。

## 21. 人机双轨质量工作台与审核闭环

### 21.1 左右双屏对比工作台

QC 工作台应支持左右双屏或等价并排视图：一侧呈现当前候选/成片，另一侧呈现参考资产、导演意图、基线 Previz、已锁定约束或上一版本。支持同步播放、时间码联动、帧级定位、差异标注和版本切换。

- 对比对象可按 QC 任务选择：候选 vs 参考、当前版 vs 上一版、成片 vs ControlSignal/Previz、结果 vs CharacterCard/Canon 允许的视觉证据。

- 每条问题必须绑定 shot_id、asset_version、timecode/frame_range、QC dimension、severity、evidence、detector_version、human_decision 和 disposition。

- 人工结论类型至少包括 CONFIRM_DEFECT、ARTISTIC_INTENTIONAL、FALSE_POSITIVE、NEEDS_MORE_EVIDENCE、WAIVE_WITH_AUTHORITY。豁免必须写明理由和授权，不得豁免法律/版权/安全红线。

### 21.2 AI 检测与人工判断分工

- AI 检测器负责发现、定位、量化和提供证据；不得单独裁决复杂艺术意图。

- 人类负责意图解释、艺术判断、例外审批和最终责任；其决定必须可追溯、可复核。

- 对已确认缺陷创建修复任务并进入 DAG；对误报记录检测器反馈；对艺术性偏差保留 deviation_intent_ref；对证据不足的问题进入待复核队列。

- 双轨不是双重重复审批：人工复核密度可按风险和镜头策略变化，但所有镜头的核心自动化 QC 覆盖率和最终 Gate 保持一致。

### 21.3 反馈闭环与防止审美固化

审核反馈进入 PMS/Pattern 前必须区分项目偏好、Canon 事实、技术缺陷、艺术性选择、检测器误报和临时交付约束。未经治理审核的单次人工偏好不得直接变成跨项目硬规则。

## 22. 团队协作、算力配额与运营权限

### 22.1 团队与项目空间

- 支持组织/团队/项目/集/镜头多层级作用域；成员权限按角色与资源属性组合判定（RBAC + ABAC）。

- 项目成员可拥有导演、编剧、美术、制片、技术导演、QC、版权合规、财务/资源管理员、只读观察者等角色；同一用户可在不同项目拥有不同角色。

- 审批人和申请人分离；关键 Gate、预算调拨、策略降级、Canon 修改和版权状态变更应执行职责分离。

### 22.2 资源配额与并发治理

- 配额维度至少包括组织、项目、镜头、模型供应商、模型类型、并发任务、GPU/渲染资源、存储和时间窗口。

- 配额服务负责预留、承诺、扣减、释放和对账；与 CostLedger、ShotBudgetLedger、ExplorationReserve、Quality Reserve 关联。

- 队列调度按优先级、截止时间、依赖就绪、质量风险和公平性综合排序；不得仅按项目等级简单抢占资源。

- 配额耗尽时提供预计等待、替代路线和成本/质量影响，由授权人选择；任何替代路线都必须通过能力声明和质量等价校验。

### 22.3 权限边界

Agent、工作台用户和自动化 Worker 均通过统一授权服务获得短期、范围受限的能力凭证。预算管理员不能代替 QC 通过质量 Gate；QC 不能修改版权许可事实；导演不能绕过安全/版权红线；系统管理员不能无审计修改业务事实。

## 23. 优先级、实施阶段与 ADR 增补

### 23.1 P0/P1/P2 与 Phase 0—4 的定义（导航用；与 31.2 不一致以 31.2 为准；CI 校验 phase_map.yaml）

P0/P1/P2 是功能优先级和上线必要性；Phase 0—4 是工程实施阶段和交付顺序。二者不是同一维度。一个 Phase 可以交付多个优先级的部分能力；一个 P1 能力也可能在 Phase 0 冻结其 Schema/权限边界、在 Phase 2 才启用。下方平台阶段表仅作章节摘要，不构成第二套路线定义；唯一权威阶段表见第 31.2 节。

| **功能优先级** | **定义** | **典型范围** |
| --- | --- | --- |
| P0 | 上线闭环必需；缺失会造成状态不可信、权限越界、质量红线失守或无法审计 | 统一领域命令、内部 Skill Runtime、DAG/Gate、核心 QC、预算账本基础、权限审计、版本与 STALE |
| P1 | 显著提升创作效率、资产复用和协作能力；可在核心闭环稳定后启用 | CharacterCard/Canon 与外观声音资产、ShotVisualPlan、CEE 受控探索、Pattern/PMS、双屏 QC 增强、团队配额、受控外部 Skill API（Phase 3） |
| P2 | 规模化体验与生态增强；不阻塞单项目高质量闭环 | 无限画布、第三方 Skill 生态、自动语义多样性评分、跨项目高级分析；Phase 4 按证据实施 |

| **平台阶段（非权威摘要；唯一口径见 31.2）** | **核心生产** | **社区** | **反推/资产/交付/学习/生态** |
| --- | --- | --- | --- |
| Phase 0 | 冻结核心 Schema、ShotSpec/AssetVersion、DAG/Gate、权限、预算、STALE、统一 Action Registry 和测试契约。 | 只冻结 ShowcaseSnapshot、ShowcaseVersionTree、ShareLink、CommunityPost、PublicationGate、审计和撤销契约。 | 只冻结 ReverseAnalysisReport、ReuseRequest、ClientReviewPortal、DeliveryManifest、学习内容引用、Agent/Plugin/Adapter Manifest、签名/沙箱/权限契约。 |
| Phase 1 | 项目创建 → 剧本导入 → AI 拆镜草案 → 人工确认 → ShotSpec/ShotVisualPlan → 生成有效 ConstraintPack（如适用；无锁定约束的镜头标记 NOT_APPLICABLE）→ 单一路线生成 → 核心 QC → 人工 Gate → DeliveryManifest。 | 只读分享/审片链接最小版；仅保留内部审核记录或组织内封闭灰度；公开社区发布与公开审核发布入口均关闭。 | 反推最小版；组织内只读资产目录+人工复用申请；客户只读审片+时间码批注；作品拆解页+Onboarding；开放生态仅冻结契约，不开放市场。 |
| Phase 2 | CEE 受控探索、PMS/记忆、动态调度与资产复用治理。 | 组织内社区 MVP、发布审批、举报/下架/申诉、ReuseRequest。 | 模板市场灰度、学习拆解增强、交付中心增强、开放生态权限灰度；不做收益分成和交易市场。 |
| Phase 3 | RCA、团队协作、运营优化和更完整的生产调度。 | 公开社区、TV Show 式首页、创作者主页、公开作品发现与模板市场。 | 发行规格适配、多语言、跨项目归一化基准、受控外部 Agent/Plugin/Adapter 市场。 |
| Phase 4 | 规模化、高可用、灾备、供应商容错和可靠性工程。 | 社区商业化能力按法律/运营准备度开放。 | 收益分成、二创授权、项目撮合/外包市场、实时协作画布增强；必须单独完成商业、资金、版权与隐私评审。 |

### 23.2 新增 ADR

| **ADR** | **决策** | **关键约束** |
| --- | --- | --- |
| ADR-029 | 导演工作台与 Agent Skill API 双入口、单一领域事实源 | 统一服务、DAG、权限和 Gate；入口不得直写权威状态 |
| ADR-030 | CharacterCard 与 CharacterCanon 分离 | 参考资产不自动成为故事事实；变更需审批和版本追溯 |
| ADR-031 | ShotVisualPlan 结构化但不锁死创意表达 | 锁定字段与开放字段分离；只将确认的可执行约束映射进 ShotSpec |
| ADR-032 | AI 剧本拆镜输出默认是草案 | 人工确认前不得写入 ScriptLock/ShotLock/正式 ConstraintPack |
| ADR-033 | ExplorationReserve 与 Quality Reserve 独立 | 探索耗尽不影响必要质量修复；禁止自动跨账本挪用 |
| ADR-034 | CEE 候选数动态决策且默认不自动多分支 | 按价值、风险、不确定性、预算和时限触发；创意熔断独立于 QC 熔断 |
| ADR-035 | 人机双轨 QC 采用证据定位与人工意图裁决 | 核心自动 QC 和最终 Gate 一致；人工复核密度可按风险调整 |
| ADR-036 | P0/P1/P2 与 Phase 0—4 分离管理 | 优先级定义上线必要性，Phase 定义工程交付阶段 |

### 23.3 架构整改与冻结候选检查

- 所有新增对象具有 owner、schema_version、状态机、权限边界、审计字段和数据保留策略。

- 导演工作台和 Agent API 对同一命令产生相同领域校验、DAG 事件和审计结果。

- 角色卡更新不会隐式覆盖 Canon；ShotVisualPlan 未锁定字段不会被误写为硬约束。

- CEE 探索预算无法挪用 Quality Reserve；分支停止不会关闭质量修复流程。

- 双屏 QC 的每条人工结论可定位到资产版本和时间码；Gate 结果可追溯到证据。

- 团队配额与镜头预算、项目预算、供应商成本完成对账；Agent 无法自批自用。

- P0/P1/P2 与 Phase 0—4 映射表进入版本控制；任何优先级变更均记录 ADR 或修订记录。

### 23.4 最终架构决议

前序架构版本已建立项目级与镜头级策略解耦、白模跨路线控制信号和创作治理能力；本版保留这些基础，并继续完善预算、数据契约、DAG/Gate、权限、配额、状态机及产品域扩展。平台坚持统一质量红线、导演意图主导、创意自由、正式生产与探索分支隔离、最小化 STALE、独立预算账本和全程审计。

## 24. 社区与展示层：生产画布、分享画布与发布治理

本章新增社区展示与分享产品域。其目标是让创作过程、审片结果、作品拆解和可复用经验在受控范围内流通，同时保护生产主链路、版权、Canon、隐私和预算边界。社区内容是生产资产的只读呈现/引用，不是生产项目本身；发布行为不得更改 Gate 0—4、ShotRevision、PictureLock 或 DeliveryPackage 的权威状态。

社区功能的实施阶段与平台整体 Phase 0—4 采用同一套阶段定义：Phase 0 冻结社区契约；Phase 1 只开放只读分享/审片链接最小版；Phase 2 建设组织内社区和受控复用；Phase 3 开放公开社区与模板市场；Phase 4 以后再开放收益分成、撮合和实时协作增强。各社区章节中的子功能只能细化阶段，不得另行定义冲突的 Phase 编号。

### 24.1 产品定位与边界：发布不等于交付

Showcase/Community 是围绕不可变展示快照的内容分发与协作层。它不直接暴露 Project DB 的可写接口，不允许社区帖子直接修改 ShotSpec、ShotVisualPlan、Canon、AssetVersion、Gate 或 CostLedger。所有复用行为都必须创建新的业务对象，并重新执行目标项目的权限、版权、Canon、预算和质量校验。

必须严格区分生产验收与社区发布：Gate 0—4 负责生产立项、基线、粗剪、Picture Lock 和正式交付；Publication Gate 负责展示内容分类、来源完整性、版权/隐私检查、审批和受众范围。通过 Publication Gate 不代表 Gate 4 通过；Gate 4 未通过的内容不得标注为 DELIVERED_FINAL。

| **展示内容类型** | **定义** | **页面强制展示标签** | **Gate 4 要求** |
| --- | --- | --- | --- |
| WORK_IN_PROGRESS | 仍在制作中的过程内容、工作版本或阶段性成果 | 制作中 / 非最终成片 | 不要求；不得标为正式交付 |
| REVIEW_PREVIEW | 供指定审片人查看的审阅样片 | 审片预览 / 仅供审阅 | 不要求；必须受访问范围限制 |
| EXPERIMENT_RECORD | 创意实验、失败案例或对比测试 | 实验记录 / 非正式成片 | 不要求；需说明实验属性 |
| APPROVED_SHOWCASE | 通过展示发布门禁的精选展示内容，但不代表正式交付 | 已批准展示 / 非 Gate 4 交付 | 不要求；不得冒称最终成片 |
| DELIVERED_FINAL | 已通过 Gate 4 的正式交付版本 | 正式交付 / Gate 4 已通过 | 必须存在有效 Gate4 PASS 和 DeliveryPackage 引用 |
| TEMPLATE_PREVIEW | 供模板/工作流市场浏览的预览及拆解内容 | 模板预览 / 复用须申请 | 模板本身不等于交付；关联成片按其真实 Gate 状态标注 |

展示页必须在首屏或内容标题附近持续显示 content_type、是否通过 Gate 4、当前快照版本和访问范围。禁止通过模糊的“已完成”“精选作品”文案暗示正式交付。若 Gate 4 状态被撤销、交付包被废止或源资产权利失效，必须同步更新展示状态或执行下架。

### 24.2 Phase 0：现在冻结契约，不实现社区功能

Phase 0 仅冻结数据结构、权限动作、发布门禁、审计事件和 STALE 规则；不开发公开社区 UI、不启用公开发现页、不开放模板市场。冻结内容至少包括：

- ShowcaseSnapshot 最小 Schema：展示快照 ID、来源项目/集/场景/镜头、精确 AssetVersion 引用、内容类型、来源 Gate 状态、展示摘要、版权/隐私/Canon 可见性策略、水印/脱敏配置、快照哈希、受众范围、状态、STALE 状态与原因、创建人与时间。

- CommunityPost 最小 Schema：帖子 ID、snapshot_id、作者/组织、标题/说明、分类/标签、受众范围、发布状态、PublicationGate 引用、审核状态、互动策略、复用策略、发布时间和审计引用。

- 权限动作：快照创建、分享链接创建/撤销、帖子提交、组织审批、组织发布、公开发布、评论、收藏、举报、下架、快照撤销、复用申请与复用审批。

- 发布门禁：内容类型披露、源版本与来源完整性、版权/隐私/敏感信息检查、水印/脱敏策略、组织/公开受众审批和撤销能力。

- 审计事件：快照创建、链接签发/撤销、提交/批准/拒绝、组织/公开发布、受众修改、评论/收藏/举报、下架、复用申请/审批、STALE 标记和快照撤销。

- STALE 标记规则：源版本变化、权利撤销、隐私泄露、Canon 可见性变化、Gate4/交付状态变化分别定义处理级别；快照不可被静默覆盖。

### 24.3 核心数据契约：不可变快照与帖子分离

ShowcaseSnapshot 是内容快照，不是可变的项目副本。创建时必须冻结具体版本引用和展示策略；更新源项目后不得自动覆盖快照内容。需要更新展示内容时，创建新的 snapshot_version，并由帖子显式切换引用或重新发布。CommunityPost 负责标题、说明、受众、互动和审核状态，不复制生产事实。

| **对象（权威实体）** | **字段（最小建议）** | **不变量与约束** |
| --- | --- | --- |
| ShowcaseSnapshot | snapshot_id、schema_version、snapshot_version、source_project_id、source_episode_id、source_scene_refs、source_shot_refs（含 shot_revision/asset_version）、content_type、source_gate_state、policy_resolution_ref、delivery_package_ref、display_asset_refs、rights_manifest_refs、privacy_scan_ref、canon_visibility_profile、watermark_profile_ref、redaction_profile_ref、audience_scope、snapshot_hash、stale_state、stale_reason_codes、created_by/at、revoked_at | 引用精确版本；快照不可变；不得包含凭证、隐藏密钥、未授权原始素材或不应公开的内部字段；每次内容变化创建新快照。 |
| CommunityPost | post_id、schema_version、snapshot_id、author_id、organization_id、title、description、category、tags、content_type、audience_scope、status、publication_gate_ref、moderation_status、interaction_policy、reuse_policy、published_at、unlisted_at、taken_down_at、audit_ref | 帖子仅包装快照和社区元数据；不能改变源项目状态；修改内容或展示版本要保留版本与审计。 |
| ShareLink | share_link_id、snapshot_id、token_hash、audience_scope、permission_set、expires_at、revoked_at、created_by、created_at、access_policy_ref | 仅只读；令牌不可预测且不存明文；可过期/撤销；访问必须校验快照状态、受众与组织成员关系。 |
| ReuseRequest | request_id、source_post_id、source_snapshot_id、requester_id、target_project_id、target_shot_revision_id、requested_assets、requested_scope、rights_check_ref、canon_check_ref、budget_check_ref、decision、approved_by、created_at | 通过申请后创建新 Project/ShotRevision 并保留来源引用；严禁直接 Fork 源项目或覆盖源 ShotRevision。 |
| ShowcaseVersionTree | tree_id、root_snapshot_id、nodes[]、edges[]、active_snapshot_ref、created_at、audit_ref | 只记录快照版本谱系；边指向不可变快照；禁止原位替换历史节点或自动切换对外展示版本。 |
| Comment | comment_id、post_id、parent_comment_id、author_id、body_ref、visibility、status、created_at、audit_ref | 评论只属于社区内容，不得写入生产项目、ShotRevision 或 Canon；删除/隐藏保留审计。 |
| Report | report_id、target_type、target_id、category、reporter_id、evidence_refs、status、decision_ref、created_at | 举报需分类并进入 ModerationQueue；举报内容不得自动视为事实，处理决定需人工审核。 |
| TakeDown | takedown_id、target_ref、reason_code、scope、authority_ref、evidence_refs、notice_ref、taken_down_at、appeal_ref | 下架动作必须记录依据、执行人、影响范围和通知；不修改源生产事实。 |
| Appeal | appeal_id、takedown_ref、appellant_ref、grounds、evidence_refs、reviewer_ref、decision_code（DECISION_UPHELD / DECISION_OVERTURNED）、decided_at | 申诉由不同于原处理人的复核者处理；DECISION_UPHELD=原决定维持，DECISION_OVERTURNED=原决定被撤销/改判；申诉提交不自动恢复展示。 |
| ReviewAnnotation | annotation_id、snapshot_id、asset_version_ref、timecode/frame_range、dimension、note、author_ref、visibility | 锚定精确快照和媒体版本；仅用于展示评注/拆解，不替代 QCIssue 或客户批注。 |

### 24.4 发布门禁、权限动作与审批职责

Publication Gate 与 Gate 0—4 并行但相互独立。Gate 4 是正式交付状态的唯一权威来源；Publication Gate 只决定某个快照能否在指定受众范围展示。推荐将发布门禁拆为四类检查：PG0 快照完整性与版本固定；PG1 内容类型/来源/免责声明；PG2 版权、隐私、Canon、敏感信息、水印和脱敏；PG3 作者提交、组织/版主审批、受众范围和发布/下架动作。公开发布必须执行完整 PG0—PG3。

| **历史动作名/兼容别名（仅迁移参考；规范映射见第 31.6.1 节）** | **允许角色（示例）** | **ABAC 限制/审批要求** |
| --- | --- | --- |
| SHOWCASE.SNAPSHOT.CREATE / SHOWCASE.SHARE.CREATE | 项目成员、导演、制片、经授权 Agent | 只能针对有权访问的项目/镜头/资产版本；Agent 只能创建 DRAFT 快照，不能批准或公开发布。 |
| COMMUNITY.POST.SUBMIT | 作者、导演、项目管理员 | 必须引用已创建快照；内容类型、来源 Gate 状态和受众范围必填。 |
| COMMUNITY.POST.APPROVE / PUBLISH_ORG | 组织内容审核员、组织管理员 | 审批人与申请人分离；组织资产/商业秘密/合同范围检查通过。 |
| COMMUNITY.POST.PUBLISH.PUBLIC | 公共内容审核员、平台版主/授权管理员 | PG0—PG3 全部通过；版权/隐私证据有效；不能仅凭组织内审批自动公开。 |
| COMMUNITY.COMMENT.CREATE / FAVORITE / REPORT | 组织成员或公众访客（按可见范围） | 遵循帖子 audience_scope、评论开关、账号状态和速率限制。 |
| COMMUNITY.POST.UNLIST / COMMUNITY.POST.TAKEDOWN / SHOWCASE.SNAPSHOT.REVOKE | 作者（自有内容）、组织管理员、版主、版权/安全职责人员 | 紧急权利/隐私/安全问题可先冻结展示，再补充复核；所有处置写入审计。 |
| COMMUNITY.REUSE.REQUEST | 已登录且符合帖子受众条件的用户/组织成员 | 只能发起申请；不得自动复制源项目或获得源资产下载权限。 |
| COMMUNITY.REUSE.APPROVE | 源资产权利人/授权方、目标项目管理员及相关审批职责 | 分别核验授权范围、Canon 冲突、预算、目标项目权限和生产质量策略。 |

本节只规定 Publication Gate 的权限与审批职责，不重复定义各业务对象状态机。PublicationReview、Appeal、CommunityPost、ShowcaseSnapshot、ShareLink 和 ReuseRequest 均为独立对象；其唯一规范状态机见第 31.5 节。公开发布必须通过 PG0—PG3；Phase 1 不开放公开审核发布。

V2.0 状态机摘要（非权威；权威定义见 31.5）：ShowcaseSnapshot：PREPARING → READY → REFERENCED；源版本更新后标记 OUTDATED，运营/合规下架标记 TAKEN_DOWN，权利/隐私/安全失效标记 REVOKED，超过有效期标记 EXPIRED。CommunityPost：DRAFT → SUBMITTED → PENDING_REVIEW → APPROVED → PUBLISHED；发布后可转 UNLISTED、TAKEN_DOWN 或 ARCHIVED。PublicationReview：QUEUED → IN_REVIEW → APPROVED / REJECTED / RETURNED。Appeal：SUBMITTED → IN_REVIEW → DECISION_UPHELD / DECISION_OVERTURNED → CLOSED。ShareLink：ACTIVE → EXPIRED / REVOKED。ReuseRequest：REQUESTED → ELIGIBILITY_CHECK → RIGHTS_CHECK → APPROVED / REJECTED → CREATED / ROLLED_BACK。申诉、审核和快照失效均为独立对象/状态，不得混入 CommunityPost 状态字段。

### 24.5 Phase 1：生产闭环 + 分享最小能力

- 单项目生产闭环保持原设计：剧本—拆镜—ShotSpec/ShotVisualPlan—路线—生成—QC—Gate—交付，不因社区能力改变生产主链路。

- 增加分享画布/审片链接，只读展示指定 ShowcaseSnapshot；默认禁止下载，采用受控代理播放和动态水印，支持有效期、撤销、访问审计。技术上无法绝对防止屏幕录制，必须在分享告知中披露风险。

- 分享画布只展示经过白名单选择的内容，不直接浏览 Project DB；分享画布与客户审片门户在产品权限和业务状态上分开，但底层可以共用只读 Media Viewer、转码、访问控制和水印服务。

- Phase 1 发布入口关闭：只记录内部审核意见/PublicationReview 草案，或对指定组织、指定项目进行封闭灰度；不开放公开审核发布、公开社区首页、创作者主页、公开发现和模板市场。任何组织内灰度不得被解释为公开发布授权；组织内正式发布能力按第 31.2 节进入 Phase 2，公开发布按 Phase 3。

- 快照展示明确标注 WORK_IN_PROGRESS、REVIEW_PREVIEW、EXPERIMENT_RECORD、APPROVED_SHOWCASE 或 DELIVERED_FINAL；DELIVERED_FINAL 必须核对 Gate4 PASS。

### 24.6 Phase 2：组织内社区 MVP

- 提供只读展示画布、组织内发布审批、水印与脱敏、收藏、评论、举报、下架和作品拆解视图。拆解视图只开放作者/组织批准的字段与资产，隐藏凭证、私密提示词、未授权素材、内部成本、其他项目数据和敏感 Canon。

- 组织内帖子默认不可被搜索引擎或组织外用户访问；访问按成员关系、项目权限、帖子受众和内容授权多重校验。

- 举报进入 ModerationQueue，按版权、隐私、安全、骚扰、误导性标签和其他类别分类；支持限流、隐藏、下架、申诉和恢复，保留处理证据。

- 复用采用“申请—权利核验—目标项目创建—新 ShotRevision—策略/Canon/预算/Gate 校验”的流程；不提供一键 Fork，不复制源项目的隐性权限、预算、密钥或 Canon。

- 复用批准仅授予申请中明确的资产/信息/用途范围；目标项目仍需自己的生产策略和质量验收，源作品的 Gate 状态不能转移给目标项目。

### 24.7 Phase 3：公开社区与模板市场

- 建设 TV Show 式内容首页、创作者主页、公开申请复用、模板市场、排行榜和内容分类浏览；公开展示仍以不可变 ShowcaseSnapshot 为底层。

- 模板市场中的模板/Pattern/Skill 必须显示版本、适用范围、输入依赖、授权范围、验证证据、失败边界和使用限制；模板是建议，不是硬性生产规则。

- 公开复用仍走申请流程：批准后在目标空间创建新 Project/ShotRevision，重新验证权利、Canon、权限、预算、模型能力和质量 Gate；不能因模板下载而绕过治理。

- 推荐系统与 Pattern Library 治理一致：区分已验证 REUSABLE_PATTERN、EXPERIMENT_RECORD、REJECTED_PROPOSAL、FAILURE_CASE；推荐可解释、可反馈、可撤回。

- 排行榜与推荐必须保留探索新颖性：除热度/互动外，兼顾内容质量证据、主题多样性、新作者曝光、重复度和版权/治理状态；不得把单一点击率作为唯一排序信号。

### 24.8 审计事件、STALE 传播与撤销规则

| **事件/变化** | **必须记录的审计字段** | **展示/STALE 处理** |
| --- | --- | --- |
| SHOWCASE_SNAPSHOT_CREATED | snapshot_id、source_version_refs、content_type、created_by、policy_snapshot、snapshot_hash | 生成不可变快照；记录引用源版本与展示策略。 |
| SHARE_LINK_CREATED / REVOKED | share_link_id、snapshot_id、audience_scope、expires_at、actor、reason | 链接只读；撤销后立即拒绝后续访问，已缓存副本按缓存策略清除。 |
| COMMUNITY_POST_SUBMITTED / APPROVED / REJECTED / PUBLISHED | post_id、snapshot_id、actor、reviewer、decision、gate_evidence、audience_scope | 审批人和申请人分离；每次状态变化追加审计事件。 |
| COMMUNITY_COMMENT / FAVORITE / REPORT | post_id、actor、object_id、moderation_state、timestamp | 按内容可见范围记录；敏感举报内容限制访问权限。 |
| COMMUNITY_UNLISTED / TAKEN_DOWN / RESTORED | post_id、snapshot_id、reason_code、authority、evidence_ref、timestamp | 不删除历史审计；展示立即隐藏或恢复，操作可追溯。 |
| 源 AssetVersion/ShotRevision 更新 | snapshot_id、changed_source_ref、old_version、new_version、impact_class | 标记 STALE_NEW_VERSION_AVAILABLE；旧快照不静默替换，作者选择重新生成快照或保持旧版。 |
| 版权撤销/授权过期/隐私泄露/敏感 Canon 暴露 | affected_snapshot_ids、rights/privacy incident、detector/manual evidence、actor | 标记 REVOKED_BLOCKED；暂停链接和帖子访问，必要时立即 TAKEDOWN。 |
| Gate4 或 DeliveryPackage 状态变化 | old_gate_state、new_gate_state、delivery_package_ref、affected_posts | 若展示为 DELIVERED_FINAL 而 Gate4 不再有效，立即撤下或改标并要求复核；不得保留误导标签。 |

STALE 级别建议：S1（仅源版本更新）标记 STALE_NEW_VERSION_AVAILABLE，旧快照仍可按授权只读展示；S2（版权/隐私/Canon 可见性/安全状态失效）标记 REVOKED_BLOCKED 并立即冻结访问；S3（正式交付状态撤销或展示标签失实）强制下架或更正标签并重新走 PG 门禁。社区 STALE 只影响展示域，不应反向触发源生产 DAG 重跑；除非生产对象自身的依赖契约也发生变化。上述 STALE 标记的权威状态落点与三层对应关系见 24.12 的枚举映射，权威状态值以 31.5 为准。

审计事件至少包括：SHOWCASE_SNAPSHOT_CREATED、SHARE_LINK_CREATED、SHARE_LINK_REVOKED、COMMUNITY_POST_SUBMITTED、COMMUNITY_POST_APPROVED、COMMUNITY_POST_REJECTED、COMMUNITY_POST_PUBLISHED、COMMUNITY_POST_UNLISTED、COMMUNITY_POST_TAKEN_DOWN、COMMUNITY_POST_RESTORED、COMMUNITY_COMMENT_CREATED、COMMUNITY_FAVORITE_ADDED、COMMUNITY_REPORT_CREATED、COMMUNITY_REUSE_REQUESTED、COMMUNITY_REUSE_APPROVED、COMMUNITY_REUSE_REJECTED、SHOWCASE_STALE_MARKED、SHOWCASE_SNAPSHOT_REVOKED。事件采用追加式记录，包含 actor、scope、object_ref、policy_version、timestamp、reason_code、correlation_id。

### 24.9 社区产品指标与阶段验收

| **维度** | **指标** | **验收/观察口径** |
| --- | --- | --- |
| 分享效率 | 分享链接创建成功率、审片打开成功率、链接撤销生效时间 | 按组织/受众/快照版本统计；撤销后不得继续通过源链接访问。 |
| 发布治理 | PG0—PG3 通过率、审批等待时长、拒绝原因分布 | 区分组织内与公开发布；不得用发布通过率替代 Gate4 质量通过率。 |
| 内容真实性 | 内容类型标签完整率、DELIVERED_FINAL 标签准确率 | 所有帖子必须有 content_type；DELIVERED_FINAL 必须绑定有效 Gate4 PASS。 |
| 版权与安全 | 权利阻断数、隐私/敏感信息拦截数、下架响应时间 | 记录证据、处置人和恢复/申诉结果。 |
| 社区质量 | 有效评论率、举报处置时长、重复/低质内容比例 | 按组织范围与公开范围分层；反滥用和申诉需要同时衡量。 |
| 复用质量 | 复用申请通过率、复用后新项目 Gate 通过率、复用违规率 | 目标项目独立验收；不得直接继承源项目 Gate。 |
| 推荐健康度 | 新作者曝光率、主题多样性、重复推荐率、Pattern 采用/失败反馈 | 避免单一热度排序；监控新颖性与质量治理的平衡。 |

### 24.10 新增架构决策记录（ADR-052～061）

| **ADR** | **决策** | **关键约束** |
| --- | --- | --- |
| ADR-052 | 社区发布与生产 Gate 解耦 | 发布不等于 Gate4；展示页必须披露 content_type 和 Gate4 状态。 |
| ADR-053 | ShowcaseSnapshot 采用不可变精确版本快照 | 引用确切 AssetVersion/ShotRevision；更新生成新快照，不静默覆盖。 |
| ADR-054 | CommunityPost 与 ShowcaseSnapshot 分离 | 帖子只承载社区元数据、受众和审核状态，不拥有生产事实。 |
| ADR-055 | Phase1 仅开放只读分享链接 | 默认私密/组织内/受邀访问；链接可过期、撤销并记录审计。 |
| ADR-056 | 独立 Publication Gate（PG0—PG3） | 检查快照、来源披露、版权隐私、审批与受众，不替代 Gate0—4。 |
| ADR-057 | 复用采用申请制，不提供直接 Fork | 批准后创建新 Project/ShotRevision，重新进行权利、Canon、权限、预算和质量校验。 |
| ADR-058 | 社区展示域拥有独立 STALE 与撤销规则 | 源版本更新可标记过期；权利/隐私失效必须阻断；不反向污染生产 DAG。 |
| ADR-059 | 组织内社区提供水印、脱敏、审核与举报下架 | 互动和审核全量留痕；组织可见范围不自动等于公开授权。 |
| ADR-060 | 公开社区和模板市场延后到 Phase3 | 模板/Pattern 可挑战、可验证、可撤回；公开复用继续申请制。 |
| ADR-061 | 社区推荐与 Pattern 治理协同并保护新颖性 | 推荐兼顾质量证据、多样性、新作者发现、重复度与治理状态，不以热度单指标排序。 |

V2.0 社区产品决议：社区分享与发布必须走独立权限和状态机；不可变展示快照不得原位覆盖；下架、申诉、复用分别留痕；发布不等于 Gate4 交付。

### 24.11 社区状态机、分享链接与复用状态机

社区域采用三个相互独立的状态机。发布状态管理展示审核与发布；分享状态管理只读协作链接；复用状态管理对被许可资产发起新项目/镜头创建。不得将三个状态机压缩成一个“发布状态”，也不得以社区发布状态代替 Gate 0—4。

| **对象** | **状态摘要（非权威；详见 31.5）** | **业务边界（规范状态机以 31.5 为准）** |
| --- | --- | --- |
| ShowcaseSnapshot | PREPARING → READY → REFERENCED；另可 OUTDATED / REVOKED / EXPIRED | 精确引用不可变版本；失效状态独立管理。 |
| CommunityPost | DRAFT → SUBMITTED → PENDING_REVIEW → APPROVED → PUBLISHED；另可 UNLISTED / TAKEN_DOWN / ARCHIVED | PUBLISHED 必须具备 PG0—PG3 通过记录；Phase 1 不开放公开发布。 |
| PublicationReview | QUEUED → IN_REVIEW → APPROVED / REJECTED / RETURNED | 独立审核对象，不是 CommunityPost 状态。 |
| Appeal | SUBMITTED → IN_REVIEW → DECISION_UPHELD / DECISION_OVERTURNED → CLOSED | UPHELD=原决定维持；OVERTURNED=原决定被撤销/改判；恢复展示需单独执行。 |
| ShareLink | ACTIVE → EXPIRED / REVOKED | 只读链接；权限、有效期、水印和访问审计独立。 |
| ReuseRequest | REQUESTED → ELIGIBILITY_CHECK → RIGHTS_CHECK → APPROVED → EXECUTING → CREATED；异常为 REJECTED / FAILED / ROLLED_BACK | 创建目标新对象；失败/回滚保留执行记录。 |

### 24.12 发布前最小证据与 SHOWCASE_QUALITY

发布前必须形成可机器检查的 PublicationEvidencePack，至少包括：版权/肖像/商标/音乐许可；3D 资产、HDRI、材质库许可；客户 NDA 和项目保密条款；个人信息与敏感信息脱敏报告；模型供应商展示政策；水印策略；有效期；下架联系人；展示级 QC 结果；内容类型披露；快照来源版本与 Gate 状态。证据缺失、过期或互相冲突时，发布门禁拒绝通过。

新增 SHOWCASE_QUALITY 配置，只检查展示技术完整性、代理文件可播放性、水印、脱敏、元数据、禁止泄露工程文件、版权状态和内容标签。SHOWCASE_QUALITY 不替代也不套用 UNIFIED_HIGH_QUALITY；若展示对象是成片片段，其成片质量仍必须满足对应生产 Gate，且只有 Gate4 PASS 才能标记 DELIVERED_FINAL。 运行态标记统一为 SHOWCASE_OUTDATED（对应旧快照源版本更新但仍可展示）、TAKEN_DOWN（下架）、REVOKED_BLOCKED（权利/隐私/安全原因阻断）；源版本变化不得自动替换快照。上述三项是 31.5 中 ShowcaseSnapshot 状态 `OUTDATED / TAKEN_DOWN / REVOKED` 在展示与观测层的运行时投影别名，不另立状态：权威状态值以 31.5 为唯一出处，API 契约、审计字段与状态机测试一律使用权威值，仅在展示文案与前端观测标记中使用投影别名；跨域过期提示沿用 `STALE_NEW_VERSION_AVAILABLE`。三层对应关系为——权威状态值 `OUTDATED` ↔ 运行时投影 `SHOWCASE_OUTDATED` ↔ 跨域 STALE 标记 `STALE_NEW_VERSION_AVAILABLE`；权威状态值 `REVOKED` ↔ 运行时投影 `REVOKED_BLOCKED`；权威状态值 `TAKEN_DOWN` ↔ 运行时投影 `TAKEN_DOWN`（同名）。

### 24.13 权限动作、Agent 限制与成本归属

| **历史动作名/兼容别名（仅迁移参考；规范映射见第 31.6.1 节）** | **建议授权主体** | **强制约束** |
| --- | --- | --- |
| SHARE_CANVAS / CREATE_SHARE_LINK / REVOKE_SHARE_LINK | 导演、制片或项目管理员 | 只允许对明确白名单快照创建链接；限制受众、有效期和下载能力。 |
| COMMUNITY.POST.PUBLISH / COMMUNITY.POST.APPROVE | 作者提交；组织负责人/版主审批 | 提交人与审批人分离；公开发布需要更高授权范围。 |
| MODERATE_SHOWCASE / COMMUNITY.REPORT.HANDLE / COMMUNITY.POST.TAKEDOWN | 社区版主、版权合规、安全运营 | 下架依据、范围、通知和证据必须记录；不得修改源生产对象。 |
| APPEAL_COMMUNITY.POST.TAKEDOWN | 作者/权利主体 | 由不同于原处理人的复核者处理；申诉不自动恢复展示。 |
| COMMUNITY.REUSE.REQUEST / COMMUNITY.REUSE.APPROVE | 申请人；目标项目负责人及版权角色审批 | 只授权明确资产和用途；批准后创建新项目/镜头版本。 |
| Agent 社区动作 | 仅允许创建 DRAFT / PROPOSAL | Agent 不得自行发布、审批、复用、下架、回复社区或修改受众；必须由人类持有效 CapabilityToken 执行审批命令。 |
| REVERSE.ANALYSIS.CREATE | 项目成员/导演/TD（受源素材权限约束） | 必须核验源视频上传、分析和截帧权利；输出默认为 REFERENCE_ONLY。 |
| REVERSE.ANALYSIS.CONFIRM | 导演/授权审核人 | 人工确认只确认参考报告，不得直接锁定 ShotSpec、ConstraintPack 或 Canon。 |
| CLIENT.REVIEW.CREATE | 制片/项目负责人 | 仅可创建指定版本、指定受众、带有效期和水印策略的只读门户。 |
| CLIENT.REVIEW.ANNOTATE | 受邀客户/委托方 | 批注必须锚定 AssetVersion 与时间码；不得修改生产 Gate。 |
| DELIVERY.SPEC.APPROVE | 制片/交付负责人/授权客户代表（按合同） | 规格审批冻结 DeliverySpec 版本；不得代替 Gate4 或版权/终检审批。 |

权限动作以第 31.6 节完整 Action Registry 为唯一规范来源；本章及历史表中的旧名称仅用于迁移，完整旧名→规范 action_key 映射见第 31.6.1 节。新 API、内部 Skill、Agent Runtime、CapabilityToken、审计事件和自动化测试只能使用规范 action_key。

社区运营、展示转码、反推分析、客户审片与学习内容处理分别归入 COMMUNITY_OPS_COST、SHOWCASE_TRANSCODE_COST、REVERSE_ANALYSIS_COST、CLIENT_REVIEW_COST 等成本分类；不得计入 Exploration Reserve 或 Quality Reserve。预算与实际成本口径详见第 31.4 节。

## 25. 视频反推与参考分析体系（Reverse Analysis）

视频反推用于从用户有权处理的参考视频中提取镜头结构、关键帧、视觉描述、运动描述、光影变化和音频线索，帮助创作者学习和形成新的参考草案。反推结果不是生产事实，不自动获得版权许可，不可直接进入正式生产契约。

### 25.1 核心实体与数据契约

| **实体** | **关键字段** | **不变量** |
| --- | --- | --- |
| ReverseAnalysisReport | report_id、source_asset_version、source_rights_ref、analysis_model/version、status、timebase、segments[]、review_ref、created_by、analyzed_at | 必须绑定源视频不可变版本、许可状态、分析模型版本和分析时间。 |
| ReverseShotSegment | segment_id、start_tc、end_tc、fps_num/den、shot_type、scene_ref、camera_description、lighting_description、motion_description、confidence、keyframe_refs、segment_granularity | 支持逐秒/逐镜头片段；时间码必须明确 FPS 分子/分母；区间不得越界或重叠冲突；光影与运动描述应记录置信度。 |
| ReversePromptPair | image_prompt、motion_prompt、negative_prompt、language、source_segment_ref、confidence | 图像提示词与运动提示词分离；不得把推测写成确定事实。 |
| ReverseKeyframeRef | asset_version_ref、timecode、frame_hash、crop_ref、rights_status | 关键帧仅引用受控源资产；导出/分享前重新校验权利和脱敏。 |
| ReverseAudioDescription | dialogue_summary、sound_events、music_description、ambience、time_range、confidence | 不得默认转录或公开未授权音乐/人声；必要时仅存摘要和时间码。 |

### 25.2 状态机与人工确认

状态机：DRAFT → ANALYZED → NEEDS_HUMAN_CONFIRM → CONFIRMED / REJECTED → ARCHIVED。CONFIRMED 表示分析结果可作为参考材料使用，不代表其已成为 ShotSpec、ConstraintPack 或 CharacterCanon。任何将反推内容用于新项目的操作，都必须创建新的 ShotVisualPlan/ShotRevision 草案，并通过独立的导演、版权、Canon、质量和预算门禁。

### 25.3 反推结果的边界与版权治理

- 默认产出类型为 REFERENCE_ONLY / DRAFT；反推报告不得直接写入 ShotSpec、ConstraintPack、ScriptLock 或 Canon。

- 每条 ReverseShotSegment、ReversePromptPair、ReverseKeyframeRef 均绑定源视频 AssetVersion 与时间码范围。

- 反推任务前必须核验用户是否有权上传、分析、截帧、转录和内部展示该视频；未授权或权利不明时进入 QUARANTINED，不允许发布社区。

- 源视频版本发生变化或被替换时，报告标记 REVERSE_STALE；权利撤销时标记 REVOKED_BLOCKED 并停止展示/导出。

- 图像提示词、运动提示词、镜头语言、光线/色彩描述、声音描述和对白摘要分字段保存，记录置信度与人工修订。

### 25.4 DAG、成本与验收

反推可以作为辅助分析任务节点运行，但不得成为正式生产 DAG 的强制前置条件。任务输入固定源视频版本、许可快照、分析器版本与参数；输出为不可变 ReverseAnalysisReport。新增成本分类 REVERSE_ANALYSIS_COST，独立于探索和质量储备。

| **验收用例** | **预期结果** |
| --- | --- |
| 反推输出写入生产契约 | 接口拒绝直接写入 ShotSpec/ConstraintPack/Canon，并产生 AUTH_SCOPE_DENIED 或 DOMAIN_INVARIANT_VIOLATION。 |
| 未授权源视频 | 报告进入 QUARANTINED，关键帧和提示词不得发布或复用。 |
| 源视频版本变化 | 报告标记 REVERSE_STALE，不静默替换分析结果。 |
| 人工确认 | 确认只将报告标记为可引用参考，不自动锁定任何生产字段。 |
| 提示词分离 | image_prompt 与 motion_prompt 分别存储、版本化和展示。 |

## 26. 资产与知识中枢（Asset & Knowledge Hub）

资产与知识中枢是平台第三极：生产域解决“怎么做”，资产与知识中枢解决“用什么做、怎么复用”，社区展示域解决“给谁看、怎么学”。它管理可授权、可追溯、可复用的主体参考资产、Pattern、工作流、白模案例和权利许可，但不取代各生产域对对象状态的权威所有权。

### 26.1 组成与核心实体

| **资产域** | **核心实体** | **管理范围** |
| --- | --- | --- |
| 共享主体库 | SharedSubjectAsset（容器）、CharacterCard、CharacterAppearance、CharacterReferenceSet | 可共享的角色/主体制作参考；默认不共享 CharacterCanon；旧称 CharacterCardRef、CharacterAppearanceRef、ReferenceSet 为废弃别名，规范名见 31.5 后的命名统一清单。 |
| Pattern / 工作流库 | PatternEntry、WorkflowTemplate、SkillTemplate、PatternEvidence | 经过验证的经验、工作流和模板；明确适用范围、失败边界及来源。 |
| 白模案例库 | BlockingCase、PrevizCase、ControlSignalCase | 白模场景、相机、走位和控制信号案例；按真实验证能力标记状态。 |
| 版权授权中心 | RightsAsset、RightsGrant、LicenseScope、RevocationRecord | 权利主体、授权范围、地域、用途、期限、署名、再许可和撤销信息。 |
| 资产健康度 | AssetHealthReport、UsageLineage、ValidationRecord | 完整性、可读性、版本、权利有效性、引用状态和复用可靠性。 |
| 复用申请 | ReuseRequest、ReuseApproval、ReuseInstantiation | 将资产引入新项目/镜头的资格审查和实例化流程。 |

### 26.2 资产生命周期和权威边界

共享参考资产状态建议为 DRAFT → RIGHTS_CHECK → VALIDATED → AVAILABLE → DEPRECATED / REVOKED / QUARANTINED。Pattern 状态区分 EXPERIMENT_RECORD、IN_REVIEW、REUSABLE_PATTERN、REJECTED_PROPOSAL、FAILURE_CASE。只有完成质量证据、版权审查、适用边界说明和责任人审批的 Pattern 才能标记 REUSABLE_PATTERN。

- 共享主体库只共享经许可的制作参考资产，如外观图、三视图、服装版本、表演参考和音色配置引用；CharacterCanon 始终按项目/作品权利范围独立授权，不因主体资产可见而自动共享。

- 资产健康度由可验证事实计算，包括引用完整性、哈希一致性、文件可读性、版权有效期、版本兼容性和历史失败记录；不能用点赞数替代健康度。

- 任何资产被撤销授权时，阻止新复用，定位既有引用，按合同和权利处置规则决定隐藏、替换或下架，并记录影响报告。

### 26.3 复用工作流与防止直接 Fork

复用必须走 ReuseRequest：REQUESTED → ELIGIBILITY_CHECK → RIGHTS_CHECK → APPROVED / REJECTED → CREATED / ROLLED_BACK。批准后由目标项目的领域服务创建新的 Project、ShotRevision 或 AssetInstance，重新解析目标 ProjectProductionProfile、ShotProductionProfile、PolicyResolution、Canon、预算和质量 Gate。不得继承源项目的密钥、成员权限、预算余额、私有 Canon 或未授权资产。

### 26.4 经验治理与社区热度隔离

社区热度、点赞、收藏、评论和排行榜数据只能作为发现/排序信号，不得直接晋升 Pattern，不得直接进入 PMS 权威记忆或覆盖导演偏好。Pattern 的晋升、降级、撤销必须经过证据审核；推荐排序需要同时考虑质量验证、适用性、版权状态、失败边界、新颖性和主题多样性。

### 26.5 成本、验收和实施阶段

资产扫描、转码、校验和知识索引分别记录成本与资源消耗；共享资产成本不应转嫁给原项目的 Quality Reserve。Phase 0 冻结对象 Schema、版权字段、权限动作和复用门禁；Phase 1 提供组织内只读资产目录与人工申请；Phase 2 增加跨项目复用灰度、健康度看板和 Pattern 审核工作流；Phase 3 再扩展公开模板/资产发现。

| **验收用例** | **预期结果** |
| --- | --- |
| 共享主体资产但不共享 Canon | 接收方只能访问被授权的 CharacterCard/Appearance 引用，无法读取源项目 Canon。 |
| Pattern 尚未验证 | 标记 EXPERIMENT_RECORD，不出现在“已验证可复用”入口。 |
| 版权撤销 | 阻止新复用并生成既有引用影响清单。 |
| 社区热度上升 | 不自动改变 Pattern 状态或 PMS 权威记忆。 |
| 复用批准 | 创建新的目标对象并执行目标项目权限、版权、Canon、预算和 Gate 校验。 |

## 27. 客户审片与发行交付（Client Review & Delivery）

客户审片门户面向外部客户/委托方提供受控只读审阅。它与内部 QC 双屏工作台分离：客户意见是业务反馈，不是内部质量判定，也不能直接推进生产 Gate。交付中心负责规格、Picture Lock、版权证据、终检报告和可重建交付清单。

### 27.1 核心实体与边界

| **实体** | **核心字段** | **边界** |
| --- | --- | --- |
| ClientReviewPortal | portal_id、project_ref、audience、access_policy、expiry、watermark_policy、status | 只读、受邀、可撤销；默认禁止下载工程文件和源资产。 |
| ClientAnnotation | annotation_id、asset_version、timecode/frame_range、comment、author、visibility、disposition | 批注必须锚定精确版本和时间位置；不得直接改 Gate。 |
| DeliverySpec | spec_id、container、codec、resolution、fps、audio_layout、caption/subtitle、color_space、naming | 按客户/平台/发行渠道定义交付要求，审批后冻结版本。 |
| DeliveryManifest | manifest_id、picture_lock_ref、asset_refs、rights_evidence_refs、qc_final_ref、spec_ref、checksum、rebuild_recipe | 可追溯、可校验、可重建；记录每个文件来源与批准链。 |
| EscapeReport | escape_id、delivery_manifest_ref、issue_type、severity、discovered_at、impact_scope、rca_ref、resolution | 记录交付后质量逃逸，关联 RCA、返工、通知和关闭证据。 |

### 27.2 客户审片状态机与意见处理

状态机（对象 ClientReviewPortal）：DRAFT → SENT → IN_REVIEW → COMMENTED → APPROVED / CHANGES_REQUESTED → CLOSED。ClientReviewPortal 是每份交付的客户审片门户实例，ClientAnnotation 是其下挂的批注对象；不存在独立的 ClientReviewSession 领域对象，“客户审片会话”指该门户的一次访问，不单列状态机。客户默认只读；批注必须绑定 AssetVersion 和 timecode/frame_range。批注经内部责任人确认后可转为 QCIssue 或 ReworkTask；不得直接更改 ShotRevision、PictureLock、Gate 状态或 DeliveryManifest。客户批准只代表其业务审阅结论，不能替代内部终检。

### 27.3 交付门禁与 Manifest

- DeliveryManifest 必须引用有效 PictureLock、最终 QC 报告、版权/许可证据、DeliverySpec、源资产版本、文件哈希和批准链。

- 交付前执行文件完整性、规格、音画同步、字幕/语言、色彩空间、响度/音轨、版权和交付清单检查；通过 Gate4 后才可标记正式交付。

- Manifest 应支持重建：明确源文件、处理步骤、工具/版本、参数、输出校验和；仅有文件清单而无法重建，不视为完整 Manifest。

- 交付后发现缺陷建立 EscapeReport，按严重度、影响范围和合同义务分类，进入 RCA；不得覆盖已交付版本，修复后创建新交付版本并保留前后关系。

- DeliverySpec 的变更可能使现有交付标记 DELIVERY_STALE；重新生成后必须创建新的 Manifest 版本。

### 27.4 权限、隐私和成本

规范动作使用 CLIENT.REVIEW.CREATE、CLIENT.REVIEW.ANNOTATE、DELIVERY.SPEC.APPROVE；旧名称 CREATE_CLIENT_REVIEW、ANNOTATE_CLIENT_REVIEW、APPROVE_DELIVERY_SPEC 仅作为兼容别名，映射见第 31.6.1 节。客户只访问明确指定的代理文件与评论范围；链接可到期、撤销并保留审计。客户门户不展示供应商密钥、内部提示词、非必要项目成本、未授权 Canon、工程源文件或其他客户项目数据。新增 CLIENT_REVIEW_COST，客户代理转码计入 SHOWCASE_TRANSCODE_COST 或单列交付转码成本，按账务分类口径统一配置，不占 Exploration/Quality Reserve。

代理转码与预览规则：分享与客户审片只输出受控代理文件/预览片段，不暴露工程源文件；代理转码前校验素材权利状态，权利不明或撤销中不得生成可访问预览。代理转码成本归入 SHOWCASE_TRANSCODE_COST 或 CLIENT_REVIEW_COST，按账务分类口径归集，不占 Exploration/Quality Reserve；代理文件与预览片段设缓存有效期，源版本或权利状态变化时使对应缓存失效并按撤销规则清除。权利撤销、链接过期或越权访问时立即阻断访问，已缓存副本按缓存策略清除；预览片段全程标注非交付/非成片，不得进入发布素材选择器。

### 27.5 验收用例

| **验收用例** | **预期结果** |
| --- | --- |
| 客户尝试访问未授权项目/资产 | 访问被拒绝并写入审计事件。 |
| 客户提交带时间码批注 | 生成 ClientAnnotation，锚定对应 AssetVersion，不修改 Gate。 |
| 批注转返工 | 经内部责任人确认后创建 QCIssue/ReworkTask，进入正常 DAG。 |
| Manifest 缺少版权证据或终检报告 | 交付 Gate 拒绝通过。 |
| 交付后质量逃逸 | 创建 EscapeReport，关联原 Manifest 和 RCA，不覆盖原交付。 |

## 28. 学习与能力平台（Learning & Capability Platform）

学习平台将经授权的作品拆解、工作流案例、失败案例和实战任务转化为可学习内容，帮助导演、创作者、TD、QC 和运营人员理解生产方法。学习内容与生产资产分层管理，不把教学示例自动变成生产模板或 Pattern。

### 28.1 产品组成

| **能力** | **最小产品形态** | **优先级建议** |
| --- | --- | --- |
| 作品拆解页 | 按授权展示镜头序列、创作意图、制作路线、QC 决策和最终结果 | P1 |
| 课程/实战营 | 课程大纲、案例、任务、提交物、导师反馈和完成记录 | P2 |
| 认证体系 | 能力标准、考核任务、评分 Rubric、证书版本和有效期 | P2 |
| 挑战赛 | 题目、授权素材包、规则、提交、评审、结果公示 | P2 |
| 失败案例库 | 可复现失败、原因分类、环境/模型版本、修复过程和适用边界 | P1/P2 |
| 新人 Onboarding | 角色任务清单、权限说明、模拟项目和操作演练 | P1 |

### 28.2 内容分级和复用治理

- 学习内容引用 ShowcaseSnapshot 或经授权的教学资产，不直接暴露生产画布、客户资料、未授权提示词和私有 Canon。

- 作品拆解必须标注内容类型、Gate4 状态、是否为探索分支/Previz/实验案例，以及哪些制作环节为推断而非事实。

- 课程示例和挑战赛素材应记录权利范围、教学用途、地域/期限和二次分发条件。

- 完成课程或获得认证不自动授予生产权限；权限仍由 RBAC/ABAC 和 Capability Token 决定。

- 教学模板不得自动晋升 Pattern；只有进入 Pattern 治理流程并通过验证，才可标记 REUSABLE_PATTERN。

### 28.3 验收与阶段

P0 冻结学习内容的引用权限、版权字段、内容类型披露和审计事件；P1 上线作品拆解页和新人 Onboarding 最小版；P2 增加课程、实战营、认证与挑战赛。学习数据只用于经告知的能力评估，不得未经授权用于推断个人敏感属性或改变其生产权限。

## 29. 开放生态与市场（Open Ecosystem & Marketplace）

开放生态面向经过授权和审查的外部 Agent、插件、ProductionRoute Adapter、模型供应商和模板作者。其核心不是开放任意代码执行，而是在稳定契约、最小权限、版权和供应链安全边界内扩展平台能力。

### 29.1 生态对象与能力矩阵

| **对象** | **必要契约** | **治理规则** |
| --- | --- | --- |
| AgentPackage | manifest、version、publisher、input/output schema、capabilities、data_access、network_access、cost_scope | 沙箱执行、签名、版本固定、权限最小化、可撤销。 |
| PluginPackage | plugin_id、API contract、scopes、webhook/event contract、retention、security review | 权限逐项授权，不默认读取全项目数据。 |
| AdapterPackage | provider/model、signal types、controlled/uncontrolled dimensions、quality evidence、validation date | 未验证能力标记 UNVERIFIED，不进入生产自动路由。 |
| TemplatePackage | template version、inputs、dependencies、rights、applicability、failure boundaries | 模板为建议，不覆盖导演意图或硬约束。 |
| Capability Registry Entry | capability_id、status、version、evidence、limitations、last_validated_at | 能力公示必须有证据、限制和撤销状态。 |

### 29.2 权限与安全边界

- P0 冻结扩展包 Manifest、签名校验、沙箱、网络/文件访问策略、Capability Token 作用域、审核和撤销接口；不开放市场 UI。

- Agent 商店、插件/Adapter 市场、模板市场进入 P2/P3 灰度；上线前必须经过安全审查、版权审查、兼容性测试和供应商风险评估。

- 外部 API 只能调用公开的领域命令和资源视图，不能访问内部数据库、密钥库、Gate 状态表或成本账户余额写接口。

- 所有外部包必须固定版本、可撤销、可回滚；出现供应链风险时可冻结分发、阻止新调用并定位存量任务。

- 收益分成、二创授权、项目撮合和外包交易市场后置；不与核心软件授权、BYOK 或平台代付绑定。

### 29.3 能力矩阵公示和验收

对外能力目录必须展示已验证能力、未覆盖维度、适用镜头类型、质量证据、验证日期、权利状态和版本兼容性。不得使用“支持所有模型/所有控制信号”等无证据营销表述。

| **验收用例** | **预期结果** |
| --- | --- |
| 未签名或被撤销的扩展包 | 安装/调用被拒绝并记录安全审计。 |
| 扩展包申请超范围能力 | Capability Token 无对应 scope，服务端拒绝。 |
| Adapter 能力未验证 | 标记 UNVERIFIED，不进入自动生产路由或公开能力承诺。 |
| 外部 API 尝试直写 Gate/Canon/CostLedger | 拒绝请求并记录越权事件。 |
| 扩展包出现漏洞 | 支持禁用、撤销、版本回滚和受影响任务定位。 |

## 30. 运营数据与基准评测（Operations & Benchmarking）

运营数据体系为生产交付、社区运营、资产复用、视频反推、质量与成本提供统一口径。指标平台不得只统计点击量或产出数量；所有比较必须考虑项目类型、镜头复杂度、模型版本、内容类型、授权范围和样本量，避免跨项目不公平比较。

### 30.1 五类运营看板

| **看板** | **核心内容** | **决策用途** |
| --- | --- | --- |
| 社区运营看板 | 发布量、审核队列、举报/下架/申诉 SLA、访问、互动、内容类型分布 | 发现治理压力和社区健康风险，不单独决定作品质量。 |
| 交付看板 | 交付周期、Gate 等待、客户批注、返工、交付规格缺陷、EscapeReport | 改善交付流程和客户体验。 |
| 资产复用看板 | 复用申请通过率、复用后修改量、权利撤销、健康度、Pattern 失效率 | 衡量复用价值与风险。 |
| 反推质量看板 | 镜头分段准确度、时间码偏差、提示词人工修订率、关键帧覆盖率、权利隔离事件 | 评估 ReverseAnalysisReport 的参考价值。 |
| 跨项目基准看板 | 经归一化的单位镜头成本、人工分钟、QC 缺陷、返工率和模型路线效果 | 支持经营和技术决策；必须给出分组和置信区间/样本量。 |

### 30.2 指标字典与归一化规则

每项 KPI 必须注册 metric_id、定义、公式、事件/表来源、统计窗口、维度、过滤规则、owner、数据新鲜度、基线版本和目标版本。跨项目比较至少按镜头复杂度、时长、角色数量、运动强度、路线/模型版本、交付规格和内容类型分层；样本量不足时显示 NOT_ENOUGH_DATA，不输出排名。

| **指标** | **建议口径** | **注意事项** |
| --- | --- | --- |
| Community Review SLA | 从提交审核到首次有效处理/最终决定的时长 P50/P80 | 按内容级别和组织范围分层。 |
| Reuse Adoption Rate | 获批 ReuseRequest 中实际创建目标 Project/ShotRevision 的比例 | 同时看复用后的修改量、质量结果和权利撤销。 |
| Reuse Quality Delta | 复用资产与基线方案在统一 QC 维度上的差异 | 必须控制镜头类型和模型版本，不能把相关性当因果。 |
| Reverse Edit Rate | 人工确认前被实质修改的镜头描述/提示词比例 | 按字段统计，区分事实错误与艺术性改写。 |
| Client Review Turnaround | 客户门户 SENT 到 APPROVED/CHANGES_REQUESTED 的时间 | 区分客户等待与内部处理时间。 |
| Delivery Escape Rate | 交付后确认缺陷数/已交付资产数或标准化工时 | 明确统计口径和缺陷严重级别。 |
| Community Ops Cost | 审核人力+转码+存储+安全处置等成本 | 不得计入 Exploration/Quality Reserve。 |
| Benchmark Coverage | 具备足够样本和可比条件的项目/镜头占比 | 不足样本不发布排名和对外基准。 |

### 30.3 成本分类与账本关系

CostLedger 新增 COMMUNITY_OPS_COST、SHOWCASE_TRANSCODE_COST、REVERSE_ANALYSIS_COST、CLIENT_REVIEW_COST；可按租户、项目、帖子、快照、报告、门户和交付 Manifest 维度归集。社区/反推/客户审片费用不得挪用 Exploration Reserve 或 Quality Reserve。若成本由某客户/组织合同承担，必须记录 cost_bearer、allocation_rule_version 和 settlement_type；不得将用户自付模型费用记为平台收入。

跨域 STALE 标记：SHOWCASE_OUTDATED 仅提示展示快照引用了较旧但仍获准展示的源版本；REVERSE_STALE 表示反推报告绑定的源视频版本或分析依赖已变化；DELIVERY_STALE 表示交付规格、PictureLock、源资产或权利证据变化导致原 Manifest 需要重新核验。若涉及权利撤销、隐私泄露或安全风险，应优先进入 REVOKED_BLOCKED/TAKEN_DOWN，而非仅标记普通 STALE。

### 30.4 隐私、基准评测和指标治理

- 对外基准报告须有数据授权、脱敏、最小样本量和重识别风险审查；禁止公开客户项目、未公开作品和个人行为明细。

- 社区热度只能作为运营信号；不得自动改变 Pattern 状态、Canon、推荐权威或生产质量阈值。

- 指标定义和计算逻辑版本化；重算必须保留指标版本、原始数据快照和变化原因。

- 排行榜/推荐兼顾质量证据、主题多样性、新作者曝光、重复度和版权/治理状态，不能只按点击率或点赞量排序。

### 30.5 全量阶段路线摘要（非权威；与 31.2 不一致以 31.2 为准；CI 校验 phase_map.yaml）

| **平台阶段（非权威摘要；唯一口径见 31.2）** | **核心生产** | **社区** | **反推/资产/交付/学习/生态** |
| --- | --- | --- | --- |
| Phase 0 | 冻结核心 Schema、ShotSpec/AssetVersion、DAG/Gate、权限、预算、STALE、统一 Action Registry 和测试契约。 | 只冻结 ShowcaseSnapshot、ShowcaseVersionTree、ShareLink、CommunityPost、PublicationGate、审计和撤销契约。 | 只冻结 ReverseAnalysisReport、ReuseRequest、ClientReviewPortal、DeliveryManifest、学习内容引用、Agent/Plugin/Adapter Manifest、签名/沙箱/权限契约。 |
| Phase 1 | 项目创建 → 剧本导入 → AI 拆镜草案 → 人工确认 → ShotSpec/ShotVisualPlan → 生成有效 ConstraintPack（如适用；无锁定约束的镜头标记 NOT_APPLICABLE）→ 单一路线生成 → 核心 QC → 人工 Gate → DeliveryManifest。 | 只读分享/审片链接最小版；仅保留内部审核记录或组织内封闭灰度；公开社区发布与公开审核发布入口均关闭。 | 反推最小版；组织内只读资产目录+人工复用申请；客户只读审片+时间码批注；作品拆解页+Onboarding；开放生态仅冻结契约，不开放市场。 |
| Phase 2 | CEE 受控探索、PMS/记忆、动态调度与资产复用治理。 | 组织内社区 MVP、发布审批、举报/下架/申诉、ReuseRequest。 | 模板市场灰度、学习拆解增强、交付中心增强、开放生态权限灰度；不做收益分成和交易市场。 |
| Phase 3 | RCA、团队协作、运营优化和更完整的生产调度。 | 公开社区、TV Show 式首页、创作者主页、公开作品发现与模板市场。 | 发行规格适配、多语言、跨项目归一化基准、受控外部 Agent/Plugin/Adapter 市场。 |
| Phase 4 | 规模化、高可用、灾备、供应商容错和可靠性工程。 | 社区商业化能力按法律/运营准备度开放。 | 收益分成、二创授权、项目撮合/外包市场、实时协作画布增强；必须单独完成商业、资金、版权与隐私评审。 |

本节仅汇总运营与业务领域的阶段影响，供阅读导航；Phase 0—4 的阶段边界、交付范围和验收门槛仅以第 31.2 节为准。

### 30.6 端到端验收用例

| **测试 ID** | **场景** | **预期结果** |
| --- | --- | --- |
| TC-SHOW-01 | 未审批资产尝试发布；客户 NDA 项目尝试公开 | 发布被拒绝，输出明确门禁缺项。 |
| TC-SHOW-02 | 发布快照引用源资产版本，源版本后续 STALE | 展示快照标记 OUTDATED，不自动替换源内容。 |
| TC-SHOW-03 | 版权撤销、分享链接过期或越权访问 | 版权撤销触发 TAKEN_DOWN/REVOKED_BLOCKED；链接过期/越权访问被拒绝并审计。 |
| TC-SHOW-04 | Agent 调用发布/审批/复用接口 | 无人工审批和有效 CapabilityToken 时拒绝；Agent 不可自批自用。 |
| TC-REUSE-01 | 复制社区模板/作品 | 必须新建项目或 ShotRevision，重新执行权限、版权、Canon、预算、质量校验，不允许直接 Fork。 |
| TC-REV-01 | 反推结果尝试直写 ShotSpec/ConstraintPack/Canon | 拒绝；只允许保存 REFERENCE_ONLY 草案。 |
| TC-REV-02 | 未授权内容反推或源视频 STALE | 未授权内容禁止发布；源视频变化时报告标记 REVERSE_STALE。 |
| TC-CLIENT-01 | 客户提交锚定时间码的批注 | 可转 QCIssue/ReworkTask，但不直接改变 Gate。 |
| TC-DELIVERY-01 | DeliveryManifest 缺版权证据或终检报告 | 交付 Gate 拒绝；通过后可核验资产来源、批准链和重建信息。 |
| TC-ESCAPE-01 | 交付后出现质量逃逸 | 生成 EscapeReport 并关联 RCA，不覆盖原 Manifest。 |
| TC-COST-01 | 社区转码、审核、反推、客户审片成本发生 | 进入对应独立成本类别，不占 Exploration/Quality Reserve。 |
| TC-PATTERN-01 | 社区点赞/评论增长 | 不直接晋升 Pattern 或进入 PMS 权威记忆；模板复用必须通过治理审核。 |
| TC-DUAL-01 | 工作台与 Agent API 发起相同分享/反推/交付命令 | 领域校验、DAG 事件、审计结果一致；权限差异仅来自 actor context。 |
| TC-SUBJECT-01 | 共享主体库资产 | 记录来源和版权状态，不自动暴露或升级 CharacterCanon。 |
| TC-TYPE-01 | 展示探索分支、Previz、Pattern 或未交付案例 | 展示页明确标注 WORK_IN_PROGRESS / EXPLORATION / PREVIZ / PATTERN / NOT_DELIVERED 等内容类型。 |

### 30.7 新增架构决策记录（ADR-062～073）

| **ADR** | **决策** | **关键约束** |
| --- | --- | --- |
| ADR-062 | 视频反推输出默认 REFERENCE_ONLY | 必须绑定源版本和时间码；不得直写 ShotSpec、ConstraintPack 或 Canon。 |
| ADR-063 | 反推报告独立状态与失效治理 | 源版本变化标记 REVERSE_STALE；权利撤销阻止展示与复用。 |
| ADR-064 | 资产与知识中枢作为独立产品域 | 资产目录与生产权威状态分离；数据通过领域服务引用。 |
| ADR-065 | 共享主体库不共享 CharacterCanon | 外观参考按许可共享，故事事实保持独立权限和审批。 |
| ADR-066 | 客户审片与内部 QC 分离 | 客户批注可以转 QC/返工任务，但不能直接改变 Gate。 |
| ADR-067 | 交付 Manifest 必须可追溯且可重建 | 引用 PictureLock、权利证据、终检报告、规格、哈希和构建步骤。 |
| ADR-068 | 学习内容不自动获得生产模板权限 | 学习材料需授权、分级；Pattern 晋升走独立治理。 |
| ADR-069 | 开放生态采用签名扩展包与沙箱能力 | Agent/Plugin/Adapter 按最小权限运行，可撤销、固定版本和回滚。 |
| ADR-070 | 市场和收益交易与核心生产解耦 | 模板/插件市场分阶段开放；收益分成和撮合市场后置。 |
| ADR-071 | 社区/反推/交付成本独立分类 | 不占 Exploration Reserve 或 Quality Reserve；结算主体明确。 |
| ADR-072 | 跨项目基准必须归一化且保护隐私 | 控制镜头复杂度、模型版本、规格和样本量；低样本不排名。 |
| ADR-073 | 社区热度不直接改变 Pattern/PMS 权威状态 | 热度只作发现信号，复用与记忆需治理审核。 |

本版产品架构决议：制作、资产与知识、社区展示构成平台三极。生产画布保持私有和可写权限治理；社区只传播不可变展示快照和只读展示画布。分享是协作级授权，发布是展示级审核，发布永远不等于 Gate4 交付。反推输出是参考草案，Agent 只能生成候选，不得自行发布、复用或审批。

## 31. V2.0.1 文档治理与逻辑一致性修订

本章承接 V2.0.1 建立的文档治理与逻辑一致性规则，并作为 V2.0.2 的现行统一裁决层。第 31.2 节是平台 Phase 0—4 唯一权威阶段表；第 31.5 节是相关对象唯一权威状态机；第 31.6 节是唯一 Action Registry 及别名映射来源。前文和附录中的相关表格只作为非权威摘要或历史兼容说明，不构成第二套规则。V2.0.1 完成文档治理整改，V2.0.2 在此基础上新增外部工具适配规范；Phase 0 正式冻结仍须通过代码、测试、部署和权限/账本/状态证据。

### 31.1 V2.0.1 产品定位、范围和冻结原则

| **层/工作空间** | **用户价值** | **产品边界** |
| --- | --- | --- |
| 制作系统 / 制作工作台 | 怎么做：从剧本、拆镜、镜头、生成、QC、Gate 到交付。 | 生产画布是权威可写空间；所有状态经统一领域命令推进。 |
| 资产与知识中枢 | 用什么做、怎么复用：主体、外观、场景、道具、Pattern、工作流、白模案例、版权和健康度。 | 提供目录、索引和复用申请；不成为 Project/Asset/Canon 的第二权威数据源。 |
| 社区展示系统 | 给谁看、怎么学：作品展示、拆解、互动、学习和复用发现。 | 社区只消费不可变 ShowcaseSnapshot 和只读展示画布，不公开生产画布。 |
| 视频反推工作台 | 从有权处理的视频提取分镜、关键帧、图像/运动/光影/音频描述。 | 默认 REFERENCE_ONLY；人工确认后只创建 ShotVisualPlan 草案。 |
| 客户审片与交付中心 | 客户只读观看、时间码批注、交付规格和可追溯交付。 | 与内部 QC/Gate 分离；客户批注不得直接改生产状态。 |
| 运营与开放生态 | 组织治理、能力注册、成本/社区运营、Agent/Plugin/Adapter 扩展。 | 通过 Manifest、签名、沙箱、Action Registry 和 CapabilityToken 开放能力。 |

产品范围假设（待市场验证）：首要客户假设为 AI 影视工作室及专业制作团队；第二类为独立 AI 创作者；后续扩展企业影视制作与品牌内容团队。该顺序是产品验证假设，不是已经证实的市场份额或收入结论。

发布不等于 Gate4 交付；分享是协作级权限，发布是展示级审核；Agent 只能提出候选和草案；生产画布与展示画布必须隔离；资产复用需授权并在目标项目创建新对象。

### 31.2 平台 Phase 0—4 唯一阶段总表

以下是平台 Phase 0—4 唯一权威阶段映射。第 7 章生产工作流阶段及第 13、23—30 章和附录中的阶段说明均为工序说明或非权威摘要，必须引用本表，不得另建冲突的 Phase 编号或交付范围。P0/P1/P2 是功能优先级，Phase 0—4 是产品与平台交付阶段，二者分离。

| **平台阶段（唯一权威）** | **核心生产** | **社区** | **反推/资产/交付/学习/生态** |
| --- | --- | --- | --- |
| Phase 0 | 冻结核心 Schema、ShotSpec/AssetVersion、DAG/Gate、权限、预算、STALE、Action Registry；冻结 ExternalToolAdapter、TimelineDraft、ExportPackage/ExportManifest、CreatorWorkspace.workspace_mode、CREATOR_DEFAULT_POLICY、CREATOR_GATE、CREATOR_PUBLICATION_CHECK、CREATOR_DELIVERY、CREATOR_RELEASE、权限动作、审计与 CRITICAL 阻断契约。只冻结接口和数据结构，不开发 UI。 | 只冻结 ShowcaseSnapshot、ShareLink、CommunityPost、PublicationGate、审计、撤销与申诉契约。 | 冻结 ReverseAnalysisReport、ReuseRequest、ClientReviewPortal、DeliveryManifest、学习内容引用、Agent/Plugin/Adapter Manifest、签名/沙箱/权限契约；详细领域契约见第 32—34 章。 |
| Phase 1 | 核心主链：项目创建→剧本导入→AI 拆镜草案→人工确认→ShotSpec/ShotVisualPlan→ConstraintPack（如适用；无锁定约束标 NOT_APPLICABLE）→单一路线生成→核心 QC→人工 Gate→DeliveryManifest。CREATOR_MODE、Timeline UI、Preview、ExportPackage UI 全部 NOT_ENABLED。 | 只读分享/审片链接最小版；仅内部审核记录或组织内封闭灰度；不开放公开社区发布。 | 反推、组织内只读资产目录、客户审片和作品拆解最小版；外部工具仅开放已验证 L1 文件互通；Creator Mode 不得借用工业 Gate/DELIVERED_FINAL。 |
| Phase 2 | CEE 受控探索、PMS/记忆、动态调度与资产复用治理；启用 Creator Mode MVP：极简输入、默认策略、TimelineDraft 元数据编排、TimelinePreview 代理片段顺序预览、模板预设、已验证 Profile 的剪映草稿/FCPXML/EDL 工程包导出、一键升级 FULL_MODE。严禁平台帧级剪辑/渲染/混音。 | 组织内社区 MVP、发布审批、举报/下架/申诉、ReuseRequest；Creator 作品在 Phase 2 只可经 CommunityPost + ShowcaseSnapshot + PublicationReview 作组织内展示，**不产生 CreatorRelease**；CreatorRelease 及其前置 CREATOR.PUBLICATION.CHECK、发布动作 CREATOR.RELEASE.PUBLISH 均属 Phase 3，Phase 2 不得启用自动发布通道。 | 模板市场灰度、学习/交付增强；外部工具 L2 元数据同步。 |
| Phase 3 | RCA、团队协作、运营优化；启用外部工具插件/签名回调/工程和成片回传；插件不得直接写权威状态。 | 公开社区、TV Show 首页、创作者主页、公开作品发现；符合资格的 Creator Release 可由用户显式发起并通过自动化检查后发布，Agent 不得代发。 | 发行适配、多语言、受控生态；外部工具 L3 插件/L4 签名事件回调，只能触发白名单 DAG，不直写 Gate/Canon/Rights/账本。 |
| Phase 4 | 高可用、灾备、供应商容错和规模化可靠性；扩展 Creator Mode 模板与更多格式 Profile。 | 社区商业化能力按法律/运营准备度开放。 | 扩展 Audio/3D/VFX 适配器、模板市场与生态；收益分成、二创授权和交易市场另行完成商业、资金、版权与隐私评审。 |

### 31.3 Phase 1 核心生产硬门与辅助最小范围

Phase 1 的唯一主硬门是单项目核心生产闭环，路径为：项目创建 → 剧本导入 → AI 拆镜草案 → 人工确认 → ShotSpec/ShotVisualPlan → 生成有效 ConstraintPack（如适用；无锁定约束的镜头必须标记 NOT_APPLICABLE）→ 单一路线生成 → 核心 QC → 人工 Gate → DeliveryManifest。ConstraintPack 不要求对所有镜头强制生成；但凡存在经批准的锁定约束，必须形成有效、版本固定且可追溯的 ConstraintPack，缺失或失效时不得进入依赖该约束的生成路线。辅助产品域有独立验收、可以灰度，但不得成为核心生产链路的前置依赖，也不得因辅助域尚未完成而阻塞核心生产发布。

| **能力域** | **Phase 1 最小交付** | **明确不做/阻断规则** |
| --- | --- | --- |
| 核心生产（硬门） | 项目创建 → 剧本导入 → AI 拆镜草案 → 人工确认 → ShotSpec/ShotVisualPlan → 生成有效 ConstraintPack（如适用；无锁定约束的镜头标记 NOT_APPLICABLE）→ 单一路线生成 → 核心 QC → 人工 Gate → DeliveryManifest。 | 必须真实可运行、失败可恢复、版本可追溯；不得绕过质量/版权门禁。 |
| 社区分享 | 只读分享/审片链接最小版；只保留内部审核记录或组织内封闭灰度；不开放公开社区发布或公开审核发布。 | 不开放公开社区、排行榜、公开主页、公开发布入口和市场；组织内灰度不得向组织外公开发现。 |
| 视频反推 | 获准上传/链接 → 逐秒/逐镜头分段 → 图像/运动提示词分离 → 关键帧/光影/音频描述 → 人工确认 → 导入为 ShotVisualPlan 草案。 | 不直写 ShotSpec、ConstraintPack、ScriptLock、Canon；未授权源素材 QUARANTINED。 |
| 资产知识 | 组织内只读目录、版权状态查询、人工复用申请。 | 不做跨组织自动复用；不得直接 Fork。 |
| 客户审片/交付 | 只读客户门户、时间码批注、DeliveryManifest 最小版。 | 客户批注不改 Gate；客户批准不等于内部终检。 |
| 学习 | 作品拆解页、新人 Onboarding。 | 课程、认证、挑战赛延后；学习完成不授予生产权限。 |
| 开放生态 | 冻结 Manifest、数字签名、沙箱、网络/文件访问、CapabilityToken 作用域和撤销接口。 | 不开放市场 UI；外部扩展不得直写 Gate/Canon/CostLedger。 |
| 白模/ControlSignal | 冻结契约和能力矩阵；可进行 PoC，若无已验证控制消费路线则不启用。 | 白模不是主生产路线硬前置；UNVERIFIED 不进入自动路由，不向客户承诺。 |
| 计费 | USER_OWN_KEY 可真实调用、统计用量和成本估算；PLATFORM_PAID 保留架构位。 | 不接真实充值、支付、退款、发票和月结；BYOK 不受平台余额拦截。 |

### 31.4 预算账户、账本与成本分类唯一口径

产品交互层采用“项目预算池 + 镜头级申请/审批 + Quality Reserve 受保护标记 + 成本分类标签”。底层继续使用 BudgetLedger、CostLedger、QuotaReservation，并与长期四类预算分类兼容。Phase 1 仅允许同一预算账户内额度重分配，不启用跨账户自动转拨；跨账户 BudgetTransferRequest 保留为长期契约，必须人工审批并执行原子记账，最早在 Phase 2 以后启用。产品层简化不等于底层合并账本。

| **对象/分类** | **唯一职责** | **禁止事项/规则** |
| --- | --- | --- |
| ProjectBudgetPool | 项目级预算视图，展示预算参考、已审批、已承诺、已发生/估算成本和风险预警。 | 不是平台托管资金账户，不直接承担充值/支付/退款。 |
| BudgetLedger | 预算额度、审批、预留、承诺、调整和反向流水的权威记录。 | 不得删除/覆盖历史流水；不与 CostLedger 混用。 |
| CostLedger | 记录用量、估算成本、可信实际成本、单价快照、成本分类和来源。 | estimated_cost 不得冒充 actual_cost；不作为支付账本。 |
| QuotaReservation | GPU/并发/存储/供应商资源数量预留、租约、释放和对账。 | 不代表金额冻结；不得与 BudgetLedgerReservation 混为一个余额。 |
| Production Budget | 正式生产、已批准路线、基础/基线 Previz、合成、后期和交付。 | 不自动挪给探索或其他项目。 |
| Exploration Reserve | CEE 提案、候选、关键帧、低清样片、探索分支和探索型 Previz。 | 耗尽后停止新增探索；不得用于质量修复。 |
| Quality Reserve | QC 缺陷修复、必要重生成、质量补偿和最终质量保障。 | 绝对不得被探索/多方案试验/非必要风格变化占用。 |
| Contingency Reserve | 经批准的供应商故障、紧急恢复和不可预见事项。 | 不得成为常规制作或探索隐性资金池。 |
| 社区/反推/客户成本 | COMMUNITY_OPS_COST、SHOWCASE_TRANSCODE_COST、REVERSE_ANALYSIS_COST、CLIENT_REVIEW_COST 等独立类别。 | 不得计入 Exploration Reserve 或 Quality Reserve；按明确 cost_bearer 分摊。 |

BYOK 规则：平台只按用户自有凭证调用供应商，统计用量和估算成本；不扣平台余额、不充值、不因平台余额或平台估算成本拦截供应商调用。用户显式配置的执行策略/供应商配额仍可按其自身规则处理。PLATFORM_PAID 真实余额预留与硬性超额阻断只在商业化能力正式启用后生效。

### 31.5 社区对象状态机最终定义

| **对象** | **唯一权威状态机** | **状态迁移约束** |
| --- | --- | --- |
| ShowcaseSnapshot | PREPARING → READY → REFERENCED；另可 OUTDATED / TAKEN_DOWN / REVOKED / EXPIRED | 精确引用 AssetVersion、ShotRevision、PolicyResolution 和 Rights Manifest；不可变；OUTDATED 不自动替换内容；TAKEN_DOWN 与 REVOKED 均为终态，前者表示运营/合规下架、后者表示权利/隐私/安全失效，两者不得互相替代；终态不得原地恢复——恢复展示必须新建 `snapshot_id` 并递增 `snapshot_version`、重走 Publication Gate，旧快照保留为历史记录（不删除、不覆盖、不改写其终态）；REVOKED 阻断访问。 |
| ExternalToolConnection | DRAFT → VALIDATING → ACTIVE → SUSPENDED / REVOKED（权威定义见 32.5） | 只引用 Secrets Manager 的 credential_ref，不保存密钥值；按租户/组织/项目隔离；验证未通过不得进入 ACTIVE；SUSPENDED/REVOKED 后不得再执行生产导出或回流。 |
| CommunityPost | DRAFT → SUBMITTED → PENDING_REVIEW → APPROVED → PUBLISHED；发布后可 UNLISTED / TAKEN_DOWN / ARCHIVED | PUBLISHED 必须校验并引用 PG0—PG3 全部通过的 PublicationReview/PublicationGate 决定；Phase 1 不开放公开审核发布。 |
| PublicationReview | QUEUED → IN_REVIEW → APPROVED / REJECTED / RETURNED | 独立审核对象；保存审核人、规则版本、证据、原因和决定时间；审核人与提交人分离。 |
| Appeal | SUBMITTED → IN_REVIEW → DECISION_UPHELD / DECISION_OVERTURNED → CLOSED | DECISION_UPHELD 表示原处置决定维持；DECISION_OVERTURNED 表示原决定被撤销或改判；申诉不自动恢复内容，恢复须单独执行并审计。 |
| ShareLink | ACTIVE → EXPIRED / REVOKED | 只读、默认禁止下载；具有访问范围、水印、有效期、撤销和访问审计。 |
| ReuseRequest | REQUESTED → ELIGIBILITY_CHECK → RIGHTS_CHECK → APPROVED → EXECUTING → CREATED；异常终态/分支：REJECTED / FAILED / ROLLED_BACK | 审批后进入 EXECUTING；仅完成目标项目/镜头新对象创建并完成权利/Canon/预算/质量校验后进入 CREATED；失败、重试和补偿必须留痕。 |
| DeliveryManifest | DRAFT → VALIDATING → VALIDATED → RELEASED；异常/失效：INVALID / STALE / REVOKED / SUPERSEDED | 每个 Manifest 版本不可变；VALIDATED/RELEASED 需 Gate4 PASS、PictureLock、权利证据、终检报告、规格和校验和齐备；源或规格变化标记 STALE；权利/安全撤销标记 REVOKED。 |
| Pattern | EXPERIMENT_RECORD → IN_REVIEW → REUSABLE_PATTERN / REJECTED / FAILURE_CASE；REUSABLE_PATTERN → DEPRECATED → RETIRED | Pattern 晋升必须有证据、适用边界、来源权利和人工审核；社区热度不能改变权威状态；DEPRECATED 停止新推荐，RETIRED 禁止新复用。 |

状态机实现规则：所有转移必须由服务端领域命令执行并写入追加式审计事件；未列出的转移一律拒绝。终态对象不得原地恢复为可编辑对象；申诉改判、复用失败重试、Manifest 重新验证均通过独立命令/新版本执行。DELIVERED_FINAL 仅在有效 Gate4 PASS 且 DeliveryManifest 状态为 RELEASED、引用的 DeliveryPackage 有效时展示。

状态机注册表索引：本平台各对象权威状态机及唯一出处如下，其他章节引用不得另立状态：Task 见 8.6（状态集：BLOCKED、READY、QUEUED、RUNNING、SUCCEEDED、FAILED、CANCELLED、CANCEL_REQUESTED、RETRY_WAIT、NEEDS_REPLAN、WAITING_FOR_HUMAN、WAITING_FOR_RESOURCE、WAITING_FOR_PROVIDER、SUBMISSION_UNKNOWN）；ConstraintPack 见 7.9；ReworkTask 见 7.6；CreatorWorkspace 见 34.1；CreatorGate 见 34.5；AssetVersion 见 8.5；PatternRecord 见 C.3 对象总表（26.2 另给准入条件与命名差异，按 C.3 裁决）；ReverseAnalysisReport 见 25.2；ClientReviewPortal 见 27.2；ExternalToolConnection 见 32.5；TimelineDraft、ExportPackage 见第 33 章；ToolExchangeSession 见第 32 章；ShowcaseSnapshot、CommunityPost、PublicationReview、Appeal、ShareLink、ReuseRequest、DeliveryManifest、Pattern 见本节上表；CreatorDeliveryManifest 见 34.7；CreatorPublicationCheck、CreatorRelease、CreatorShareLink 见 34.8。新增对象必须先在本注册表登记权威状态机章节，未登记状态的转移一律拒绝。

对象与动作命名统一（废弃名称 → 规范名称；冻结后不得再引入新的废弃别名，出现即按错误处理）：

| 废弃/错误名称 | 规范名称 | 说明 |
|---|---|---|
| ClientReviewSession | ClientReviewPortal | 客户审片门户；不存在独立的客户审片会话领域对象，31.7 角色口径与 31.10 指标口径统一用 Portal |
| ReviewAnnotation（客户批注语境） | ClientAnnotation | ReviewAnnotation 仅指社区展示评注，不得用于客户批注 |
| CharacterCardRef | CharacterCard | 26.1 表中的简称，Schema 与状态机一律用全名 |
| CharacterAppearanceRef | CharacterAppearance | 同上 |
| ReferenceSet | CharacterReferenceSet | 同上，不得简写为 ReferenceSet |
| SharedSubjectAsset | 保留原名（未建模容器） | 共享主体库容器，只承载上述三个已建模对象的共享引用关系，不计入 P0 76 个对象口径 |
| client_review_session_id（字段名，非对象） | client_review_portal_id | 32.7 元数据映射表的外部同步键；此处 session 只表示门户的一次访问，字段不得反向催生 ClientReviewSession 对象 |
| CharacterCardRef、CharacterAppearanceRef、ReferenceSet 之外的历史简称 | 见 31.6.1 | 动作名映射仍以 31.6.1 为准，本节只补对象名 |

版本树关系字段统一为 supersedes、outdated_by、taken_down_by、restored_from。ShowcaseSnapshot 更新必须生成新版本；旧版本不自动切换对外展示。版权撤销/隐私泄露/安全事件触发 REVOKED/TAKEN_DOWN 并冻结访问；恢复展示需重新执行 Publication Gate。PublicationReview、Appeal、ShareLink、ReuseRequest、DeliveryManifest 和 Pattern 的独立状态机以本节状态表为唯一规范。

跨章节状态机差异裁决（冻结评审决议 AFS-P0-FREEZE-2026-11-13-01）：同一对象在多章出现状态集或转移差异时，按本节注册表索引所指权威章节为准，并按下列裁决执行，不得各自解读。①CharacterCanon：以 C.7 为准，CONFLICT_REVIEW 为合法状态，事实冲突未裁决前相关镜头不得自动合并事实；C.3 未列该状态属描述省略，不构成第二套状态集。②ShotVisualPlan：以 C.7 为准，PARTIALLY_LOCKED、LOCKED 为合法状态，锁定时须记录锁定人、时间、理由与版本；C.3 的 DRAFT→REVIEW→APPROVED/REJECTED 为其子集。③ExplorationBranch：以 C.7 为准，FAILED 为合法终态，生成失败不得并入 REJECTED（REJECTED 指候选被否，FAILED 指分支执行失败，语义不同）。④QCReviewSession：以 C.7 为准，RESOLVED 之后必须先经 GATE_ELIGIBLE 才可到 CLOSED，禁止 RESOLVED→CLOSED 跳关；跳过 Gate 资格校验等同绕过 Gate 门禁，一律拒绝。⑤PatternRecord：以 C.3 对象总表为准，EXPERIMENT_RECORD→IN_REVIEW→REUSABLE_PATTERN/REJECTED/FAILURE_CASE，REUSABLE_PATTERN→DEPRECATED→RETIRED；26.2 的 REJECTED_PROPOSAL 与 REJECTED 同义，按 C.3 记 REJECTED，26.2 未列 DEPRECATED、RETIRED 属描述省略；26.2 所列晋升条件（质量证据、版权审查、适用边界说明、责任人审批）为准入规则，与状态转移并行适用。上述裁决同步登记于 state_machines.yaml 的 conflict_rulings 字段；派生表与正文不一致时以本节为准。

DELIVERED_FINAL 的唯一条件：源资产版本通过有效 Gate4 PASS，且绑定有效 DeliveryPackage/DeliveryManifest。缺少任一条件，只能显示 APPROVED_SHOWCASE、WORK_IN_PROGRESS、REVIEW_PREVIEW、EXPERIMENT_RECORD、PREVIZ 或其他真实内容类型，不得暗示正式交付。

### 31.6 Action Registry 与 Agent 最小权限

所有工作台、Agent API、内部 Skill、外部工具适配器和自动化测试必须引用同一份 Action Registry。当前共注册 75 项规范 action_key；每条记录包含 resource_type、scope、risk_level、CapabilityToken 要求、允许主体/审批要求、audit_event、negative_test_ids 和 status。新接口只使用规范 action_key；历史名称只通过明确兼容映射使用。

机器可读注册表与校验：Action Registry 必须导出机器可读清单（YAML/JSON），与本表一致；CI 校验 action_key 全局唯一、命名规范、audit_event 与规范动作一一对应、Token/审批字段完整，禁用别名与拼写变体进入新接口。历史别名仅保留在兼容映射表，不得作为新接口动作。

角色与会签矩阵：各动作允许主体、审批要求与禁止角色以 ActionRegistry 的 approval_requirement 为准；申请人与批准人职责分离，Agent/Skill/Plugin 不得自批自用，CRITICAL 动作必须本人显式意图加短时 CapabilityToken。31.7 角色旅程与本节权限要求冲突时，以 ActionRegistry 为准。

| **action_key（规范）** | **resource_type / scope / risk_level** | **Token / 允许主体 / approval_requirement** | **audit_event / negative_test_ids / status** |
| --- | --- | --- | --- |
| PROJECT.CREATE | Project；tenant+organization；MEDIUM | Token=Y；项目管理员/制片；成员资格与配额校验，无自批权限。 | PROJECT_CREATED；ACT-PROJ-NEG-001 跨组织创建拒绝；ACTIVE |
| SCRIPT.IMPORT | ScriptRevision；project/episode；MEDIUM | Token=Y；导演/编剧/制片；检查项目写权限、文件安全与导入来源。 | SCRIPT_IMPORTED；ACT-SCRIPT-NEG-001 越权导入拒绝；ACTIVE |
| SCRIPT.LOCK.APPROVE | ScriptLock；project/episode；HIGH | Token=Y；导演/授权编剧；人工确认，申请人与批准职责分离。 | SCRIPT_LOCK_APPROVED；ACT-SCRIPT-NEG-002 Agent 自锁拒绝；ACTIVE |
| SHOT.DRAFT.CREATE | ShotDraft；project/episode/scene；LOW | Token=Y；Agent/编剧/导演；Agent 仅生成 DRAFT，不写正式 ShotRevision。 | SHOT_DRAFT_CREATED；ACT-SHOT-NEG-001 Agent 写正式镜头拒绝；ACTIVE |
| SHOT.REVISION.CONFIRM | ShotRevision；project/episode/scene/shot；HIGH | Token=Y；导演/镜头负责人；人类确认，版本冲突必须拒绝。 | SHOT_REVISION_CONFIRMED；ACT-SHOT-NEG-002 Agent 确认拒绝；ACTIVE |
| SHOTSPEC.LOCK | ShotSpec；project/shot；HIGH | Token=Y；导演/授权 TD；锁定维度、依据、版本和审批事件齐备。 | SHOTSPEC_LOCKED；ACT-SPEC-NEG-001 未审批锁定拒绝；ACTIVE |
| CONSTRAINT.PACK.CREATE | ConstraintPack；project/shot；MEDIUM | Token=Y；Agent/TD/导演；Agent 仅能创建 DRAFT；无锁定约束标记 NOT_APPLICABLE。 | CONSTRAINT_PACK_CREATED；ACT-CNST-NEG-001 未授权约束升级拒绝；ACTIVE |
| CONSTRAINT.PACK.LOCK | ConstraintPack；project/shot；HIGH | Token=Y；导演/授权 TD；仅锁定经批准的 BASELINE 维度；审批与版本固定。 | CONSTRAINT_PACK_LOCKED；ACT-CNST-NEG-002 Agent 锁定拒绝；ACTIVE |
| PRODUCTION.ROUTE.SELECT | ProductionRoute；project/shot/task；MEDIUM | Token=Y；制片/TD/授权调度器；路线须处于 VERIFIED 且满足 rights/capability。 | PRODUCTION_ROUTE_SELECTED；ACT-ROUTE-NEG-001 UNVERIFIED 路线拒绝；ACTIVE |
| GENERATION.REQUEST | GenerationTask；project/shot；MEDIUM | Token=Y；导演/制片/Agent（授权范围内）；预算、配额、依赖与约束快照通过。 | GENERATION_REQUESTED；ACT-GEN-NEG-001 预算/权限越界拒绝；ACTIVE |
| ASSET.VERSION.CREATE | AssetVersion；project/shot/asset；MEDIUM | Token=Y；Worker/Model Adapter；仅新增不可变版本，不覆盖旧版本或改变 GOLD。 | ASSET_VERSION_CREATED；ACT-ASSET-NEG-001 覆盖旧版本拒绝；ACTIVE |
| ASSET.VERSION.APPROVE | AssetVersion；project/shot/asset；HIGH | Token=Y；授权审核人/QC/导演按职责；权利、QC 和证据通过。 | ASSET_VERSION_APPROVED；ACT-ASSET-NEG-002 Agent 批准拒绝；ACTIVE |
| QC.REPORT.CREATE | QCReport；project/shot/asset_version；MEDIUM | Token=Y；QC Worker/受信检测服务；只写检测结果与证据，不改变 Gate。 | QC_REPORT_CREATED；ACT-QC-NEG-001 缺证据报告不得 PASS；ACTIVE |
| PRODUCTION.GATE.APPROVE | GateDecision；project/shot/delivery；CRITICAL | Token=Y；对应 Gate 授权人类职责主体；强制角色分离、证据与审批记录。 | PRODUCTION_GATE_APPROVED；ACT-GATE-NEG-001 Agent/非授权人审批拒绝；ACTIVE |
| CANON.CHANGE.APPROVE | CharacterCanon/CanonRevision；project/character；CRITICAL | Token=Y；导演/Canon Owner；变更理由、影响范围、版本和回滚关系齐备。 | CANON_CHANGE_APPROVED；ACT-CANON-NEG-001 Agent/跨项目写入拒绝；ACTIVE |
| RIGHTS.STATUS.CHANGE | RightsCase/RightsManifest；tenant/project/asset；CRITICAL | Token=Y；版权/法务授权人；双人或职责分离审批；权利证据必须可追溯。 | RIGHTS_STATUS_CHANGED；ACT-RIGHTS-NEG-001 无证据变更拒绝；ACTIVE |
| BUDGET.SHOT.REQUEST | ShotBudgetRequest；project/shot/account；MEDIUM | Token=Y；项目成员/Agent 可提交草案；必须指定账户、用途、成本类别和上限。 | SHOT_BUDGET_REQUESTED；ACT-BUDGET-NEG-001 跨账户伪装请求拒绝；ACTIVE |
| BUDGET.SHOT.APPROVE | ShotBudgetRequest/BudgetLedger；project/account；HIGH | Token=Y；预算管理员/制片；申请人与审批人分离；不得改变 Quality Reserve 用途；同账户已批准额度重分配由本动作承载（跨账户走 BUDGET.TRANSFER.*）。 | SHOT_BUDGET_APPROVED；ACT-BUDGET-NEG-002 自批/质量储备挪用/跨账户伪装同账户拒绝；ACTIVE |
| BUDGET.TRANSFER.REQUEST | BudgetTransferRequest；tenant/project/accounts；HIGH | Token=Y；预算管理员/制片提交；Phase 1 仅登记/不执行跨账户转拨。 | BUDGET_TRANSFER_REQUESTED；ACT-TRANSFER-NEG-001 Phase 1 执行拒绝；PHASE_GATED（Phase 2+） |
| BUDGET.TRANSFER.APPROVE | BudgetTransferRequest/BudgetLedger；tenant/project/accounts；CRITICAL | Token=Y；双人职责分离的预算审批人；原子记账与幂等校验。 | BUDGET_TRANSFER_APPROVED；ACT-TRANSFER-NEG-002 无审批/非原子转账拒绝；PHASE_GATED（Phase 2+） |
| SHOWCASE.SNAPSHOT.CREATE | ShowcaseSnapshot；project/shot/asset_version；MEDIUM | Token=Y；项目成员/Agent；Agent 仅创建 DRAFT，引用必须精确且可见。 | SHOWCASE_SNAPSHOT_CREATED；ACT-SHOWCASE-NEG-001 包含密钥/未授权素材拒绝；ACTIVE |
| SHOWCASE.SNAPSHOT.REVOKE | ShowcaseSnapshot；snapshot/audience；HIGH | Token=Y；版权/安全/组织授权人；记录原因、范围、证据和影响快照。 | SHOWCASE_SNAPSHOT_REVOKED；ACT-SHOWCASE-NEG-002 无权限撤销拒绝；ACTIVE |
| SHOWCASE.SHARE.CREATE | ShareLink；snapshot/audience；MEDIUM | Token=Y；导演/制片/项目管理员；只读、下载默认关闭、有效期/水印/受众必填。 | SHARE_LINK_CREATED；ACT-SHARE-NEG-001 超范围或无限期链接拒绝；ACTIVE |
| SHOWCASE.SHARE.REVOKE | ShareLink；share_link/audience；HIGH | Token=Y；链接创建授权人/组织管理员/版权安全人员；撤销理由留痕。 | SHARE_LINK_REVOKED；ACT-SHARE-NEG-002 跨组织撤销拒绝；ACTIVE |
| COMMUNITY.POST.SUBMIT | CommunityPost；organization/post；MEDIUM | Token=Y；作者/内容权利人；绑定有效快照、内容类型、受众和权利证据。 | COMMUNITY_POST_SUBMITTED；ACT-COMM-NEG-001 无快照提交拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.POST.APPROVE | PublicationReview；organization/post；HIGH | Token=Y；组织审核员/版主；提交人与审批人分离，证据审查并留决定。 | COMMUNITY_POST_APPROVED；ACT-COMM-NEG-002 自审/PG 缺项拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.POST.PUBLISH.ORG | CommunityPost；organization/post；HIGH | Token=Y；组织授权发布人；PG0—PG3 通过并核验组织受众。 | COMMUNITY_POST_PUBLISHED_ORG；ACT-COMM-NEG-003 Phase 1 发布拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.POST.PUBLISH.PUBLIC | CommunityPost；public/post；CRITICAL | Token=Y；平台版主/公开发布授权人；PG0—PG3 完整通过，权利范围含公开传播。 | COMMUNITY_POST_PUBLISHED_PUBLIC；ACT-COMM-NEG-004 无公开权利/PG 缺项拒绝；PHASE_GATED（Phase 3） |
| COMMUNITY.POST.UNLIST | CommunityPost；organization/public/post；MEDIUM | Token=Y；作者/组织管理员/版主；记录原因、受众、影响和通知。 | COMMUNITY_POST_UNLISTED；ACT-COMM-NEG-005 越权隐藏他人内容拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.POST.TAKEDOWN | CommunityPost；organization/public/post；HIGH | Token=Y；版主/版权/安全授权人；依据、范围、通知和证据必填。 | COMMUNITY_POST_TAKEN_DOWN；ACT-COMM-NEG-006 无依据下架/修改源项目拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.COMMENT.CREATE | CommunityComment；organization/post/comment；LOW | Token=Y；在帖子受众范围内的登录用户/获邀访客；评论开关、内容安全、速率限制和帖子状态校验。 | COMMUNITY_COMMENT_CREATED；ACT-COMMENT-NEG-001 越权评论/已关闭评论拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.FAVORITE.CREATE | Favorite；organization/post/user；LOW | Token=Y；在帖子受众范围内的登录用户；校验可见性、账号状态和幂等唯一键。 | COMMUNITY_FAVORITE_CREATED；ACT-FAVORITE-NEG-001 收藏不可见帖子拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.FAVORITE.REMOVE | Favorite；organization/post/user；LOW | Token=Y；收藏关系所属用户；只能移除本人收藏，要求幂等。 | COMMUNITY_FAVORITE_REMOVED；ACT-FAVORITE-NEG-002 移除他人收藏拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.REPORT.CREATE | CommunityReport；post/comment/user；LOW | Token=Y；可见范围内用户；举报不自动构成事实裁决。 | COMMUNITY_REPORT_CREATED；ACT-COMM-NEG-007 不可见对象举报限流/拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.REPORT.HANDLE | ModerationCase；post/comment/user；HIGH | Token=Y；版主/版权/安全人员；独立调查、证据、处理时限与通知。 | COMMUNITY_REPORT_HANDLED；ACT-COMM-NEG-008 举报人自裁/越权处理拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.APPEAL.SUBMIT | Appeal；takedown/post；MEDIUM | Token=Y；作者/权利主体；须引用原处置与申诉理由/证据。 | COMMUNITY_APPEAL_SUBMITTED；ACT-APPEAL-NEG-001 伪造他人申诉拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.APPEAL.REVIEW | Appeal；takedown/post；HIGH | Token=Y；独立复核人；必须与原处置人分离，决定码固定。 | COMMUNITY_APPEAL_DECIDED；ACT-APPEAL-NEG-002 原处置人复核拒绝；PHASE_GATED（Phase 2） |
| COMMUNITY.REUSE.REQUEST | ReuseRequest；source_snapshot/target_project；MEDIUM | Token=Y；申请人；只可申请明确资产/信息/用途范围。 | COMMUNITY_REUSE_REQUESTED；ACT-REUSE-NEG-001 直接 Fork/越权下载拒绝；ACTIVE（Phase 1 人工申请） |
| COMMUNITY.REUSE.APPROVE | ReuseRequest；source_asset/target_project；CRITICAL | Token=Y；源权利人+目标项目负责人+相关职责审批；Agent 只建草案。 | COMMUNITY_REUSE_APPROVED；ACT-REUSE-NEG-002 无权利/Canon/预算校验拒绝；PHASE_GATED（Phase 2） |
| REVERSE.ANALYSIS.CREATE | ReverseAnalysisReport；source_video/project；MEDIUM | Token=Y；已获源素材上传/分析/截帧权利的成员；结果默认为 REFERENCE_ONLY。 | REVERSE_ANALYSIS_CREATED；ACT-REV-NEG-001 缺授权素材分析拒绝；ACTIVE |
| REVERSE.ANALYSIS.CONFIRM | ReverseAnalysisReport；report/project；HIGH | Token=Y；导演/授权审核人；只确认参考报告，不锁定 ShotSpec/ConstraintPack/Canon。 | REVERSE_ANALYSIS_CONFIRMED；ACT-REV-NEG-002 确认即写生产事实拒绝；ACTIVE |
| CLIENT.REVIEW.CREATE | ClientReviewPortal；project/asset_version/audience；MEDIUM | Token=Y；制片/项目负责人；指定版本、受众、有效期、水印与只读权限。 | CLIENT_REVIEW_CREATED；ACT-CLIENT-NEG-001 暴露源文件/未授权受众拒绝；ACTIVE |
| CLIENT.REVIEW.ANNOTATE | ClientAnnotation；asset_version/timecode；LOW | Token=Y；受邀客户/委托方；批注锚定精确版本与时间码，不改 QC/Gate。 | CLIENT_REVIEW_ANNOTATED；ACT-CLIENT-NEG-002 客户改 Gate/Canon 拒绝；ACTIVE |
| DELIVERY.SPEC.APPROVE | DeliverySpec；project/delivery；HIGH | Token=Y；交付负责人/合同授权客户代表；确认规格，不替代内部终检。 | DELIVERY_SPEC_APPROVED；ACT-DELIVERY-NEG-001 客户规格审批代替 Gate4 拒绝；ACTIVE |
| DELIVERY.MANIFEST.CREATE | DeliveryManifest；project/asset_version/delivery；MEDIUM | Token=Y；交付服务/授权制片；仅生成 DRAFT 并绑定完整证据引用。 | DELIVERY_MANIFEST_CREATED；ACT-DELIVERY-NEG-002 缺引用/哈希 Manifest 拒绝；ACTIVE |
| DELIVERY.MANIFEST.APPROVE | DeliveryManifest；project/delivery；HIGH | Token=Y；交付负责人+内部 QC/权利职责；Gate4、PictureLock、权利证据和终检报告齐备。 | DELIVERY_MANIFEST_APPROVED；ACT-DELIVERY-NEG-003 缺 Gate4/权利证据拒绝；ACTIVE |
| DELIVERY.MANIFEST.RELEASE | DeliveryManifest；project/delivery/package；CRITICAL | Token=Y；交付负责人/授权发行主体；只释放已 VALIDATED 的不可变版本。 | DELIVERY_MANIFEST_RELEASED；ACT-DELIVERY-NEG-004 非 RELEASED 不能标 DELIVERED_FINAL；ACTIVE |
| EXTERNAL_TOOL.ADAPTER.REGISTER | ExternalToolAdapterManifest；tenant/organization；HIGH | Token=Y；平台集成管理员/安全负责人；签名、来源、沙箱、权限范围和供应商版本信息齐备；登记人不得单独批准生产启用。 | EXTERNAL_TOOL_ADAPTER_REGISTERED；ACT-EXT-NEG-001 未签名适配器登记拒绝；PHASE_GATED（Phase 0 契约/注册能力） |
| EXTERNAL_TOOL.ADAPTER.VERIFY | AdapterCapabilityEvidence；adapter/version/capability；HIGH | Token=Y；集成工程负责人执行验证，安全/QC职责独立复核；必须提交格式往返、权限、回调、隔离和回滚证据；不得手工将未测试能力标为 VERIFIED。 | EXTERNAL_TOOL_ADAPTER_VERIFIED；ACT-EXT-NEG-002 无证据声明 VERIFIED 拒绝；PHASE_GATED（Phase 0 验证规范） |
| EXTERNAL_TOOL.ADAPTER.REVOKE | ExternalToolAdapterManifest/AdapterCredential；tenant/adapter；CRITICAL | Token=Y；平台安全负责人/授权管理员；可紧急暂停或撤销；必须吊销短期 Token、连接凭证和回调密钥并记录影响范围。 | EXTERNAL_TOOL_ADAPTER_REVOKED；ACT-EXT-NEG-003 撤销后继续调用拒绝；PHASE_GATED（Phase 0 安全控制） |
| EXTERNAL_TOOL.CONNECTION.CREATE | ExternalToolConnection；organization/project/adapter；MEDIUM | Token=Y；项目管理员/TD；只能引用 Secrets Manager credential_ref，不接收或回显明文密钥；明确租户、项目、工具实例和授权范围。 | EXTERNAL_TOOL_CONNECTION_CREATED；ACT-EXT-NEG-004 跨租户/明文密钥写入拒绝；PHASE_GATED（Phase 1） |
| EXTERNAL_TOOL.CONNECTION.VALIDATE | ExternalToolConnection/AdapterVerificationEvidence；organization/project；MEDIUM | Token=Y；TD/集成 Worker；验证工具版本、连接、权限、格式和能力声明；验证失败不得创建生产可用连接。 | EXTERNAL_TOOL_CONNECTION_VALIDATED；ACT-EXT-NEG-005 未验证连接执行生产导出拒绝；PHASE_GATED（Phase 1） |
| EXTERNAL_TOOL.EXPORT.CREATE | ToolExchangePackage；project/shot/asset_version；MEDIUM | Token=Y；具有导出权的项目成员/制片；必须固定源 AssetVersion、导出范围、Rights/Policy 快照和有效期；不得导出密钥、私有提示词或未授权素材。 | EXTERNAL_TOOL_EXPORT_CREATED；ACT-EXT-NEG-006 超范围/无版权依据导出拒绝；PHASE_GATED（Phase 1） |
| EXTERNAL_TOOL.IMPORT.SUBMIT | ExternalToolArtifact/ToolExchangeSession；project/asset；MEDIUM | Token=Y；项目成员/授权工具回调；只进入隔离暂存和验证流程；必须校验哈希、格式、来源血缘、恶意文件和版权，不得直接创建 APPROVED/GOLD 版本。 | EXTERNAL_TOOL_IMPORT_SUBMITTED；ACT-EXT-NEG-007 未扫描产物进入正式资产库拒绝；PHASE_GATED（Phase 1） |
| EXTERNAL_TOOL.METADATA.SYNC | ToolMetadataSnapshot/ReviewAnnotation；project/shot/asset_version/timecode；MEDIUM | Token=Y；项目成员/QC/客户审片受邀主体按字段授权；只同步可映射元数据和批注建议；不得覆盖 QCReport、GateDecision、Canon 或 Rights 状态。 | EXTERNAL_TOOL_METADATA_SYNCED；ACT-EXT-NEG-008 时间码/版本不匹配写入拒绝；PHASE_GATED（Phase 2） |
| EXTERNAL_TOOL.EVENT.INGEST | ExternalToolEvent；tenant/organization/project/adapter；HIGH | Token=Y；受信 Adapter Runtime/签名回调服务；校验签名、时间窗、nonce、幂等键和 allowlist；事件只可触发白名单领域命令，不得直写业务状态。 | EXTERNAL_TOOL_EVENT_INGESTED；ACT-EXT-NEG-009 伪造/重放/未注册事件拒绝；PHASE_GATED（Phase 3） |
| CREATOR.DELIVERY.MANIFEST.CREATE | CreatorDeliveryManifest；creator_workspace/render_asset；MEDIUM | Token=Y；Creator Delivery Service；要求外部渲染回流、新 AssetVersion、CreatorGate PASS 和 Rights 通过；不得创建工业 DeliveryManifest。 | CREATOR_DELIVERY_MANIFEST_CREATED；ACT-CR-NEG-003 缺回流渲染/权利证据拒绝；PHASE_GATED（Phase 2） |
| CREATOR.GATE.EVALUATE | CreatorGate；creator_workspace/creator_asset；HIGH | Token=Y；受信 Creator Policy Worker；只生成 PASS/BLOCKED/NEEDS_USER_INPUT 评估证据，不授予发布权、不改变工业 Gate。 | CREATOR_GATE_EVALUATED；ACT-CR-NEG-002 Agent 自写 PASS/替代 Gate4 拒绝；PHASE_GATED（Phase 2） |
| CREATOR.MODE.UPGRADE | FullWorkspace；project/creator_workspace；HIGH | Token=Y；创作者本人/项目管理员显式发起；创建 FULL_MODE 后继工作区；旧 Creator 资产仅作 DRAFT 参考，不继承 Gate4/DELIVERED_FINAL。 | CREATOR_MODE_UPGRADED；ACT-CR-NEG-006 静默转正/继承 Gate4 拒绝；PHASE_GATED（Phase 2） |
| CREATOR.RELEASE.PUBLISH | CreatorRelease/CommunityPost；creator_workspace/publication；CRITICAL | Token=Y；仅允许 CreatorWorkspace 所有者本人显式点击发布；必须绑定 user_intent_id 和短时 CapabilityToken；自动 CREATOR_PUBLICATION_CHECK=PASSES 后发布；Agent/Skill/Plugin 不得代发。 | CREATOR_RELEASE_PUBLISHED；ACT-CR-NEG-005 Agent 代发/无用户意图/CRITICAL 未通过拒绝；PHASE_GATED（Phase 3 公开） |
| CREATOR.RELEASE.SUBMIT | CreatorRelease；creator_workspace/snapshot；MEDIUM | Token=Y；创作者/作品权利主体；绑定 CREATOR_DELIVERY、受众及 Rights 证据；提交仅形成发布候选，不公开；Phase 2 不产生 CreatorRelease，本动作无对象可提交，故随发布链整体后置到 Phase 3。 | CREATOR_RELEASE_SUBMITTED；ACT-CR-NEG-004 无效清单/权利未知提交拒绝；PHASE_GATED（Phase 3） |
| CREATOR.WORKSPACE.CREATE | CreatorWorkspace；tenant/organization/account；MEDIUM | Token=Y；账号所有者/项目管理员；创建时固化 workspace_mode=CREATOR_MODE 和策略版本；禁止跨租户创建。 | CREATOR_WORKSPACE_CREATED；ACT-CR-NEG-001 伪装 FULL_MODE/跨租户创建拒绝；PHASE_GATED（Phase 2） |
| EXPORT.PACKAGE.CREATE | ExportPackage；project/timeline/asset；MEDIUM | Token=Y；有导出权限的创作者/导演/制片；固定素材版本、受众、Format Profile 与 Rights/Policy 快照。 | EXPORT_PACKAGE_CREATED；ACT-EXP-NEG-001 越权素材打包拒绝；PHASE_GATED（Phase 2） |
| EXPORT.PACKAGE.EXPORT | ExportPackage；project/package/audience；HIGH | Token=Y；包所有者/获授权项目成员；仅允许导出 VALIDATED 不可变包；记录受众、下载范围与审计。 | EXPORT_PACKAGE_EXPORTED；ACT-EXP-NEG-003 未验证/超受众导出拒绝；PHASE_GATED（Phase 2） |
| EXPORT.PACKAGE.IMPORT.ACK | ToolExchangeSession/ExportPackage；project/package；LOW | Token=Y；创作者手动确认/可信适配器；只登记“用户确认已导入”事实，不代表工具能力验证成功。 | EXPORT_PACKAGE_IMPORT_ACKED；ACT-EXP-NEG-004 伪造回调不得改变资产状态；PHASE_GATED（Phase 2） |
| EXPORT.PACKAGE.VALIDATE | ExportManifest/ExportPackage；project/package；HIGH | Token=Y；Export Validator/版权合规 Worker；校验文件、哈希、时间码、字幕、音轨、格式与 CRITICAL 权利状态。 | EXPORT_PACKAGE_VALIDATED；ACT-EXP-NEG-002 CRITICAL 未关闭仍验证通过拒绝；PHASE_GATED（Phase 2） |
| EXTERNAL_TOOL.RETURN.INGEST | ExternalToolArtifact/AssetVersion；project/asset；HIGH | Token=Y；授权用户/受信 Adapter Runtime；先进入 Staging/Quarantine；扫描、Rights、血缘通过后创建新 AssetVersion 并重新 QC/Gate。 | EXTERNAL_TOOL_RETURN_INGESTED；ACT-EXT-NEG-010 回流继承 Gate4/覆盖旧版本拒绝；PHASE_GATED（Phase 2 手动/Phase 3 插件） |
| TIMELINE.DRAFT.CREATE | TimelineDraft；organization/project/episode；LOW | Token=Y；Creator/导演/授权 Agent；Agent 仅创建 DRAFT；project/workspace scope 必须匹配。 | TIMELINE_DRAFT_CREATED；ACT-TL-NEG-001 跨租户/项目创建拒绝；PHASE_GATED（Phase 2） |
| TIMELINE.DRAFT.REVISE | TimelineDraftVersion；project/timeline；MEDIUM | Token=Y；时间线所有者/导演/授权协作者；expected_version 必填；每次修改生成新版本，不覆盖历史。 | TIMELINE_DRAFT_REVISED；ACT-TL-NEG-002 旧版本覆盖拒绝；PHASE_GATED（Phase 2） |
| TIMELINE.DRAFT.VALIDATE | TimelineDraft/TimelineValidationReport；project/timeline；MEDIUM | Token=Y；验证 Worker/授权用户；只生成验证报告；不得绕过 Rights、素材完整性、帧率/时间码错误。 | TIMELINE_DRAFT_VALIDATED；ACT-TL-NEG-003 缺素材/时间基准仍标 VALIDATED 拒绝；PHASE_GATED（Phase 2） |
| TIMELINE.PREVIEW.START | TimelinePreviewSession；project/timeline/asset_version；LOW | Token=Y；项目授权成员/创作者；只播放低清代理片段序列；无文件导出、无拼接媒体生成。 | TIMELINE_PREVIEW_STARTED；ACT-TL-NEG-004 预览生成 MP4 请求拒绝；PHASE_GATED（Phase 2） |
| TIMELINE.DRAFT.ARCHIVE | TimelineDraft；workspace/project/timeline；MEDIUM | Token=Y；时间线所有者/导演/Creator Workspace 所有者；只能归档本人有权管理的非当前活动版本；保留血缘与审计。 | TIMELINE_DRAFT_ARCHIVED；ACT-TL-NEG-006 越权归档/归档当前锁定输入拒绝；PHASE_GATED（Phase 2） |
| TIMELINE.TEMPLATE.APPLY | TimelinePreset/TimelineDraft；workspace/project/timeline；LOW | Token=Y；创作者/导演/授权协作者；仅把短视频/短剧/口播预设应用为可编辑草案；不得锁定表达或覆盖旧版本。 | TIMELINE_TEMPLATE_APPLIED；ACT-TL-NEG-005 未授权模板/覆盖时间线拒绝；PHASE_GATED（Phase 2） |
| CREATOR.PUBLICATION.CHECK | CreatorPublicationCheck；creator_release/audience；HIGH | Token=Y；受信 Publication Policy Worker；自动执行适用 PG0—PG3 规则、Rights/Privacy/Safety 检查；只能输出 PASSED/BLOCKED/NEEDS_HUMAN_REVIEW 与证据。 | CREATOR_PUBLICATION_CHECKED；ACT-CR-NEG-007 不完整/未知权利自动 PASSED 拒绝；PHASE_GATED（Phase 3） |
| CREATOR.RELEASE.REVOKE | CreatorRelease/ShareLink；creator_release/audience；CRITICAL | Token=Y；创作者本人、版权/安全职责人或受信 Rights Revocation Worker；撤销须有原因/依据/范围，吊销访问与下载凭证并生成影响清单。 | CREATOR_RELEASE_REVOKED；ACT-CR-NEG-008 越权/无依据撤销拒绝；PHASE_GATED（Phase 3） |

#### 31.6.1 历史动作别名映射（兼容层；不构成独立授权规则）

| **历史动作名 / 兼容别名** | **规范 action_key** | **映射规则 / 状态** |
| --- | --- | --- |
| SHARE_CANVAS；CREATE_SHARE_LINK；SHARE_LINK_CREATE | SHOWCASE.SHARE.CREATE | 语义一致的旧名称可直接映射；COMPAT_ALIAS_ONLY。 |
| REVOKE_SHARE_LINK；SHARE_LINK_REVOKE | SHOWCASE.SHARE.REVOKE | 必须保留原始 actor、scope、reason；COMPAT_ALIAS_ONLY。 |
| COMMUNITY.POST.PUBLISH | COMMUNITY.POST.PUBLISH.ORG 或 COMMUNITY.POST.PUBLISH.PUBLIC | 旧名不做静态自动映射；必须读取 audience_scope 并重新授权；无法唯一判定时拒绝。 |
| PUBLISH_ORG | COMMUNITY.POST.PUBLISH.ORG | 只限组织受众并通过 PG0—PG3；COMPAT_ALIAS_ONLY。 |
| COMMUNITY.POST.APPROVE | COMMUNITY.POST.APPROVE | 规范名保持不变；兼容接口仍须执行职责分离。 |
| MODERATE_SHOWCASE | COMMUNITY.REPORT.HANDLE 或 COMMUNITY.POST.TAKEDOWN | 动作语义不明确；必须由调用方显式选择，禁止自动映射。 |
| COMMUNITY.REPORT.HANDLE | COMMUNITY.REPORT.HANDLE | 规范名保持不变。 |
| COMMUNITY.POST.TAKEDOWN | COMMUNITY.POST.TAKEDOWN | 规范名保持不变。 |
| APPEAL_COMMUNITY.POST.TAKEDOWN | COMMUNITY.APPEAL.SUBMIT | 仅映射申诉提交；复核必须使用 COMMUNITY.APPEAL.REVIEW。 |
| COMMUNITY.REUSE.REQ | COMMUNITY.REUSE.REQUEST | 旧缩写映射到完整规范名；COMPAT_ALIAS_ONLY。 |
| REVERSE.ANALYSIS.CREATE；REVERSE.ANALYSIS.CONFIRM | 同名规范 action_key | 保留原 key；调用时重新验证源素材授权。 |
| CREATE_CLIENT_REVIEW | CLIENT.REVIEW.CREATE | COMPAT_ALIAS_ONLY；不允许授予客户写生产数据权限。 |
| ANNOTATE_CLIENT_REVIEW | CLIENT.REVIEW.ANNOTATE | COMPAT_ALIAS_ONLY；批注只锚定资产版本/时间码。 |
| APPROVE_DELIVERY_SPEC | DELIVERY.SPEC.APPROVE | COMPAT_ALIAS_ONLY；客户确认不替代 Gate4。 |
| DELIVERY.PACKAGE.APPROVE | DELIVERY.MANIFEST.APPROVE | 需校验历史对象确实对应 DeliveryManifest；不匹配时拒绝迁移。 |
| CREATE_SHOT_DRAFT；SHOT_DRAFT_CREATE | SHOT.DRAFT.CREATE | COMPAT_ALIAS_ONLY；仅创建 DRAFT。 |
| IMPORT_SCRIPT；SCRIPT_IMPORT | SCRIPT.IMPORT | COMPAT_ALIAS_ONLY；导入来源与项目作用域必验。 |
| COMMUNITY.COMMENT.CREATE；COMMENT.CREATE | COMMUNITY.COMMENT.CREATE | 语义一致时直接映射；仍须检查帖子可见范围、评论开关和内容安全。COMPAT_ALIAS_ONLY。 |
| FAVORITE | COMMUNITY.FAVORITE.CREATE 或 COMMUNITY.FAVORITE.REMOVE | 旧动作是 toggle/intent 不明时禁止自动映射；仅当请求显式提供 desired_state=true/false 时分别映射 CREATE/REMOVE。COMPAT_ALIAS_ONLY。 |

Agent 仅可在被授权范围内生成 DRAFT/PROPOSAL，或调用明确列入注册表且无权威副作用的分析/生成动作；不得自行发布、审批、批准复用、下架、回复社区、批准预算、改变 Gate/Canon/Rights/CostLedger 或释放交付。高风险/关键动作必须由具备职责资格的人类主体作出审批，并由服务端验证短时、最小范围 CapabilityToken。Token 证明执行能力，不代替审批记录；必须保存人类审批人、对象版本、理由、证据、规则版本和决策时间。

双入口一致性：同一领域命令从导演工作台和 Agent Runtime 发起时，业务校验、DAG 事件类型/关键 payload、幂等结果和审计对象保持一致；actor、入口和 Token 信息可以不同。不得由 UI 或 Agent 客户端自行判断权限。

外部工具适配层新增 action_key 直接登记在第 31.6 节 Action Registry；当前不存在可据此认定的历史外部工具动作别名。任何剪辑插件、脚本或旧系统的自定义动作名均不得模糊映射或回退放行，必须先完成调用清单盘点，再按“旧名/调用方/参数语义/目标 action_key/权限差异/负向测试”逐项登记；无法唯一映射时拒绝请求。

阶段禁用动作的统一契约：Action Registry 中 phase 状态标为 `PHASE_GATED` 的动作，在未启用阶段被调用时一律返回结构化错误 `PHASE_NOT_ENABLED`（category=PHASE、retryable=false），且不得产生任何业务副作用——不写入权威状态、不占用预算或资源配额、不创建资产版本、不触发下游任务；仅记录一条拒绝审计（action_key、actor、目标对象、请求阶段与实际阶段），不得伪装为参数错误或权限不足。客户端不得据此重试；进入启用阶段后同一 action_key 自动转为可调用，无需改动契约。负向测试必须覆盖四项：禁用阶段调用被拒、无副作用、拒绝审计可检索、启用后正常放行。

### 31.7 用户旅程与只读 Viewer 复用

| **角色** | **端到端旅程** | **关键证据/完成条件** |
| --- | --- | --- |
| 导演/创作者 | 意图输入→拆镜草案→确认 ShotRevision→审阅候选→确认创作决策→QC/交付审阅。 | 锁定/开放字段、意图版本、审批记录和艺术 Override 证据。 |
| 制片/项目负责人 | 项目准入→预算/路线→资源审批→进度/成本→交付范围确认。 | ProjectBrief、BudgetRequest、Resource Gap Report、DeliveryManifest。 |
| TD/技术负责人 | 凭证/Endpoint→模型连通性→能力矩阵→路线配置→失败诊断→资产/控制信号验证。 | credential_ref、Adapter 版本、能力验证证据、任务日志。 |
| QC/版权合规 | 查看精确资产版本→定位问题→人工结论/版权核验→返工或 Gate 建议。 | asset_version、frame_range、severity、evidence、rights_manifest 和处理记录。 |
| 作品作者 | 创建快照→填写内容类型/说明→提交审核→发布→版本更新/下架。 | ShowcaseSnapshot、PublicationReview、发布审计。 |
| 社区浏览者/学习者 | 发现作品→观看→打开拆解→收藏/评论/举报→学习。 | 访问范围、内容标签、授权拆解字段和互动审计。 |
| 复用申请者 | 检索资产/Pattern→查看许可和边界→申请复用→审批→创建新目标项目/镜头。 | ReuseRequest、Rights/CANON/Budget Check、目标新版本。 |
| 社区版主/版权人员 | 审核队列→举报/版权投诉→冻结/下架→申诉复核→恢复或维持处置。 | ModerationCase、TakeDown、Appeal、SLA 与审计。 |
| 客户 | 收到指定版本链接→只读观看→时间码批注→确认/要求修改。 | ClientReviewPortal、ClientAnnotation；客户决定不直接改变 Gate。 |
| 生态开发者 | 登记扩展包→签名/安全审查→授权安装→调用→更新/撤销。 | Manifest、签名、沙箱策略、CapabilityToken 和版本兼容证据。 |

Share Viewer 与 Client Review Viewer 共用底层 Media Viewer、代理转码、时间码寻址、播放能力、访问控制、水印和审计日志基础服务；但 SHARE_VIEW 与 CLIENT_REVIEW 的业务权限、受众、批注能力和会话状态分开。分享默认禁止下载；客户审片允许指定时间码批注；二者均不暴露生产画布写操作。术语统一：外部客户的观看、批注、意见收集统一称“客户审片/审片链接/审片中心”；“审计”仅指日志、证据链和权限追溯，不用于称呼客户观看流程。

### 31.8 社区内容供给与冷启动机制

| **阶段** | **内容供给策略** | **治理要求/成功信号** |
| --- | --- | --- |
| 平台精选 | 邀请授权创作者提供精选作品、Production Case、Previz 案例和失败案例。 | 每项内容有发布授权、权利范围、内容类型和拆解来源；先保证质量与完整性。 |
| 组织内部沉淀 | 从真实项目的正式交付、授权片段、拆解和已验证 Pattern 中形成内容。 | 组织内访问控制、客户 NDA、脱敏、水印和撤销能力通过。 |
| 公开作者入驻 | 逐步开放创作者主页、公开发布、公开拆解和挑战内容。 | 作者身份/权利声明、PG0—PG3、举报申诉和版主流程就绪。 |
| 复用价值闭环 | 作品发现→拆解学习→复用申请→目标项目生产→新作品展示。 | 衡量拆解转化、复用创建和复用后 Gate4 质量，不仅看点赞/浏览。 |

### 31.9 视频反推、资产与学习边界最终决议

- 视频反推完整交互：上传/粘贴获准链接→来源/权利/安全检查→逐秒/逐镜头分析→关键帧→图像提示词与运动提示词分离→光影/声音/环境描述→人工确认→创建 ShotVisualPlan DRAFT。输出必须绑定源 AssetVersion、时间码、FPS、分析模型版本和置信度。

- 拥有分析权限不等于拥有公开展示、音乐/人声转录、二次分发、商业复用或高度相似重构权限。未经授权素材进入 QUARANTINED；源视频变化标记 REVERSE_STALE；反推成本归 REVERSE_ANALYSIS_COST。

- 共享主体库只共享授权的 CharacterCard、CharacterAppearance、CharacterReferenceSet 和外观/声音参考；不共享 CharacterCanon。新项目必须建立自己的 Canon 并单独审批。

- Pattern 生命周期统一为 EXPERIMENT_RECORD → IN_REVIEW → REUSABLE_PATTERN / REJECTED / FAILURE_CASE；已验证的 REUSABLE_PATTERN 可转 DEPRECATED → RETIRED。DEPRECATED 停止新推荐但保留既有引用证据；RETIRED 禁止新复用。社区热度、点赞、收藏、评论不得直接晋升、降级或改变 Pattern/PMS 权威状态。

- 学习内容只引用 ShowcaseSnapshot 或已授权教学资产；作品拆解必须标注 content_type、Gate4 状态、事实与推断；课程/认证/挑战赛不自动授予生产权限，教学模板不自动晋升 Pattern。

- 版权撤销必须生成既有引用影响清单，覆盖公开展示、组织复用、在制项目、交付引用和历史快照；按授权/法律要求进行阻断、下架、隔离、替换或重新审批，不静默删除历史。

### 31.10 产品指标与北极星指标

建议北极星指标为“每月通过有效 Gate4 并生成有效 DeliveryManifest 的正式交付项目数”，并设置质量逃逸率、单位交付成本、单位交付人工工时、延期率和重大版权/安全事故作为护栏。不得通过放宽质量基线提升北极星指标。

| **指标域** | **metric_id 示例** | **计算口径/数据来源** | **治理要求** |
| --- | --- | --- | --- |
| 核心生产 | DELIVERY_PROJECTS_G4 | 月度有效 Gate4 PASS 且绑定有效 DeliveryManifest 的项目数；来源 GateDecision/DeliveryManifest。 | 按项目类型和规格分层；质量护栏不得省略。 |
| 质量护栏 | DELIVERY_ESCAPE_RATE | 确认的交付后严重缺陷数/已交付资产数；来源 EscapeReport。 | 按严重级别分层，报告统计窗口和样本量。 |
| 社区 | COMMUNITY_REUSE_CONVERSION | 获批 ReuseRequest 中实际创建目标 Project/ShotRevision 的比例。 | 同时统计复用后 Gate4 通过率和权利违规率。 |
| 社区治理 | COMMUNITY_TAKEDOWN_RESPONSE_P80 | 有效举报/版权投诉到实际限制访问的时长 P80。 | 按严重级别和组织/公开范围分层。 |
| 反推 | REVERSE_HUMAN_EDIT_RATE | 人工确认前被实质修改的反推字段比例。 | 不得单独优化为越低越好；同时统计关键帧覆盖率和采纳率。 |
| 客户审片 | CLIENT_REVIEW_TURNAROUND_P80 | ClientReviewPortal 由 SENT 到 APPROVED/CHANGES_REQUESTED 的时长 P80。 | 区分客户等待和内部处理时间。 |
| 资产复用 | REUSE_POST_G4_RATE | 由复用资产启动的目标项目中通过 Gate4 的比例。 | 按镜头复杂度、模型版本和资产类别归一化。 |
| 成本 | COMMUNITY_OPS_COST | 从 CostLedger 按 COMMUNITY_OPS_COST 成本分类汇总；来源 CostLedger。 | 不得计入 Exploration Reserve 或 Quality Reserve。 |
| 成本 | REVERSE_ANALYSIS_COST | 从 CostLedger 按 REVERSE_ANALYSIS_COST 成本分类汇总；来源 CostLedger。 | 不得计入 Exploration Reserve 或 Quality Reserve。 |
| 成本 | CLIENT_REVIEW_COST | 从 CostLedger 按 CLIENT_REVIEW_COST 成本分类汇总；来源 CostLedger。 | 不得计入 Exploration Reserve 或 Quality Reserve。 |

每项指标必须注册 metric_id、定义、公式、数据源、统计窗口、维度、过滤规则、owner、数据新鲜度、基线版本和目标版本。低样本量统一显示 NOT_ENOUGH_DATA，不排名、不生成伪精确比较。跨项目比较至少按镜头复杂度、时长、角色数量、运动强度、模型/Adapter 版本和交付规格分层。

### 31.11 负向测试与 V2.0 最终冻结门

| **测试 ID** | **场景** | **预期结果** |
| --- | --- | --- |
| AGENT-NEG-001 | Agent 审批自己创建的发布/预算/Canon/Gate 请求。 | 拒绝；无权威状态变化；记录 AUTH_SCOPE_DENIED 和职责分离事件。 |
| AGENT-NEG-002 | Agent 越权发布、下架、回复社区或批准复用。 | 拒绝；Agent 只可创建 DRAFT/PROPOSAL；人类审批及 CapabilityToken 缺一不可。 |
| AGENT-NEG-003 | Agent/API/Worker 直接写 Gate、Canon、Rights 或 CostLedger。 | 数据库/服务层拒绝；审计记录调用主体和目标对象。 |
| AGENT-NEG-004 | Agent 调用超预算/超配额动作。 | 按明确预算/配额策略返回结构化错误；BYOK 不因平台余额不足被拦截。 |
| SHOW-NEG-001 | 客户 NDA 项目或未审批快照公开发布。 | PG 门禁拒绝；缺失证据明确列出。 |
| SHOW-NEG-002 | 源版本 STALE、版权撤销或 Gate4 失效。 | 分别标记 OUTDATED、REVOKED/TAKEN_DOWN 或更正 DELIVERED_FINAL 标签；不静默替换。 |
| SHOW-NEG-003 | 分享链接过期/撤销/越权访问或试图下载。 | 访问拒绝；下载受策略阻断；访问尝试和结果留痕。 |
| REV-NEG-001 | 反推报告写入 ShotSpec/ConstraintPack/Canon。 | 拒绝；仅允许创建 ShotVisualPlan DRAFT/REFERENCE_ONLY。 |
| REV-NEG-002 | 未授权源视频、SSRF/私网链接或授权范围不含转录/公开。 | QUARANTINED 或拒绝；不得公开关键帧/提示词/音频信息。 |
| ASSET-NEG-001 | 共享主体库读取源 CharacterCanon。 | 拒绝；仅开放有权的 CharacterCard/Appearance/ReferenceSet。 |
| ASSET-NEG-002 | 社区热度上升尝试自动晋升 Pattern/PMS。 | 不改变 Pattern/PMS 权威状态；进入独立治理审核。 |
| CLIENT-NEG-001 | 客户批注直接改变 QCReport/Gate。 | 拒绝；需内部 triage 后创建 QCIssue/ReworkTask。 |
| DELIVERY-NEG-001 | DeliveryManifest 缺 PictureLock、版权证据或终检报告。 | Gate4 拒绝；原版本不覆盖。 |
| COST-NEG-001 | 社区/反推/客户审片成本占用 Exploration/Quality Reserve。 | 拒绝并记录 BUDGET_ACCOUNT_PURPOSE_VIOLATION。 |
| DUAL-NEG-001 | 工作台与 Agent API 提交相同领域命令。 | 校验、领域事件、DAG 结果、幂等结果一致；actor/入口字段按真实调用记录。 |

| **冻结门** | **必须满足** | **验收证据** |
| --- | --- | --- |
| Gate A：文档基线 | 版本/页眉/页脚/目录/编号/交叉引用/表格/附件和渲染通过。 | DOCX 结构检查、PDF 渲染、逐页视觉检查、无占位残留。 |
| Gate B：核心生产 | 剧本到交付 Manifest 端到端可执行；QC/Gate 不可绕过。 | 集成测试、资产版本追溯、恢复与幂等测试。 |
| Gate C：范围与阶段 | Phase 1 主硬门唯一；辅助域最小范围清楚；全篇 Phase 映射一致。 | 阶段总表与各章节映射自动检查。 |
| Gate D：预算与计费 | BudgetLedger/CostLedger/QuotaReservation 分离；Quality Reserve 不探索；BYOK 不依赖平台余额。 | 预算用途负向测试、成本对账和双模式专项测试。 |
| Gate E：权限 | Action Registry 唯一；CapabilityToken/职责分离有效；Agent 不能自批自用。 | 权限正/负向测试和审计检索。 |
| Gate F：产品域 | 社区、反推、资产、客户审片交付、学习和生态状态/权利/权限契约一致。 | 端到端流程测试、版权/隐私/撤销演练。 |
| Gate G：指标与运营 | metric_id/公式/来源/窗口/owner/版本完整；低样本不排名；成本分类隔离。 | 指标字典、数据血缘、样本量和归一化报告。 |

Phase 0 契约冻结证据（文档与契约层，不需要系统运行即可验收）：①Gate A 文档基线——版本标识、章节编号、标题层级、交叉引用、表格完整性、内部链接与渲染检查无残留（Markdown 无页眉页脚与自动目录域，对应检查项不适用）；源文档以 Markdown 为准，如需 PDF 渲染版按导出流程另行生成并留档；②Gate C 阶段一致性——阶段总表与各章节 Phase 映射自动检查通过；③契约文件齐备——对象 JSON Schema 草案（object_schemas.schema.json，76 个对象已建模，字段分别取自 C.3、17.10、25.1、27.1、32.5 数据契约表、8.12—8.14 与 2.5 的 YAML 契约块、2.4/8.10/8.11 的段落字段列举、24.3「字段（最小建议）」、32.4 AdapterManifest 字段组、33.2 TimelineDraft 字段组、33.5 ExportPackage、34.7 CreatorDeliveryManifest 五张形态特殊但确有字段定义的表，以及 7.4 Gate 字段列举、21.1 缺陷字段列举，以及按冻结评审条件 C1—C3 新补的 7.6 返工单、7.9 ConstraintPack、8.15 CameraRig 与 BlockingAsset、34.1 CreatorWorkspace、34.5 CreatorGate、34.8 CreatorPublicationCheck 与 CreatorRelease 字段契约表（均含字段标识、类型、必填与版本规则）；P0 契约对象全集 76 个（原 70 个清单 + 6 个由 24.3 带出的展示域附属对象）已全部建模，待补项清零）、OpenAPI 草案（openapi.draft.yaml，75 条路径与 Action Registry 一一对应）、各对象状态机表（state_machines.yaml，覆盖 31.5、C.7、32.6、33.3、34.x 登记的表格式、图示类与段落箭头式状态机共 35 个对象，含 Task、BudgetLedgerTransaction、34.7 的 CreatorDeliveryManifest，按字段契约补登的 ConstraintPack（7.9）、ReworkTask（7.6）、CreatorWorkspace（34.1）、CreatorGate（34.5），以及从正文提取的 AssetVersion（8.5）、ReverseAnalysisReport（25.2）、ClientReviewPortal（27.2）、ExternalToolConnection（32.5）；PatternRecord 以 C.3 完整箭头链为权威，26.2 的状态集合与命名差异已按 31.5 第⑤条裁决，不属“无状态机”；共享参考资产（26.2）有完整状态链但对象尚未建模，登记于 text_authority_only 并注明建模后须补登记状态机，不属“无状态机”）、Action Registry 机器可读清单（action_registry.yaml，75 项与 31.6 主表键集合及顺序双向一致）、权限矩阵表（permission_matrix.yaml，C.5 角色级 7 角色 × 9 关键操作摘要 + 由 31.6 派生的 75 项 action_key 级条目 + C.5.1 职责分离规则）、预算与成本口径表（budget_caliber.yaml，4 类预算账户及 Creator Mode 成本规则）、指标字典（metric_dictionary.yaml，含 Gate G 的字典部分）、冻结包清单（phase0_manifest.md，逐份登记文件名、字节数、SHA-256、对应章节与校验命令；清单自身为自指文件，其自身哈希由同目录 phase0_manifest.md.sha256 承载，不列入上述十一类文件哈希清单）、枚举注册表（enum_registry.yaml，登记 35 个对象的状态值，并把权威状态值、展示投影别名、跨域 STALE 标记分三层，别名必须落在权威状态集内）、ADR 主索引（adr_index.yaml，全文 118 条 ADR 的编号、章节归属、决策与同号重复及编号缺口检出）、阶段表机器可读版（phase_map.yaml，只收录 31.2 的 5 个阶段，其余章节同名阶段表一律登记为非权威摘要）；P0 契约对象按两个口径计：全集 76 个（含 24.3 带出的 6 个展示域附属对象 Appeal、Comment、Report、ReviewAnnotation、ShowcaseVersionTree、TakeDown），冻结范围 70 个（不含上述 6 个，其中已建模 70 个、待补 0 个）；6 个附属对象经冻结评审裁决不计入 Phase 0 冻结范围，随展示域在 Phase 1 另行冻结，其字段仍保留在 Schema 中供参考；上述十一类文件统一存放于项目根目录，文件名固定，并与本文档对应章节保持可校验的一致；④静态可校验性——Schema 结构合法且 required 与 properties 自洽、action_key 全局唯一且命名规范、状态机表覆盖 31.5、C.7、32.6、33.3、34.x 登记对象且与正文无静默差异、权限矩阵无空缺角色、预算账户用途无交叉；同一对象在不同章节出现不一致状态机定义时，须在状态机表中显式标记并逐项裁决，不得静默合并或以派生文件覆盖正文。上述证据齐备仅表示契约层可提交冻结评审，不构成对实现能力的认可。Phase 0 契约冻结状态以文件头部与决议 AFS-P0-FREEZE-2026-11-13-01 为准（当前为条件通过），任何章节不得另述冻结状态。

正文修订与哈希：本文件修订标识 V205-R14（2026-11-13），正文 SHA-256 与字节数由 `phase0_manifest.md` 登记。正文哈希一旦变化即视为新修订，必须重跑派生器、整包校验并重建清单哈希与 `.sha256`，不得沿用旧哈希；文档版本号按项目约束保持 V2.0.5，同版本不同修订由修订标识区分，禁止在未重建哈希的情况下宣称“哈希一致”。

P0 契约对象口径的排除边界：76 个对象（冻结范围 70 + 展示域附属 6）之外，还有两类明确排除、不计入待补统计的附属对象——①展示域 6 个（Appeal、Comment、Report、ReviewAnnotation、ShowcaseVersionTree、TakeDown），随展示域在 Phase 1 另行冻结；②资产域共享主体库容器 `SharedSubjectAsset`（26.1），它只承载已建模的 CharacterCard、CharacterAppearance、CharacterReferenceSet 的共享引用关系，本身不新增业务字段，P0 期间以 26.2 正文为权威，Phase 1 建模时另行纳入。因此 `SharedSubjectAsset` 未建模不构成待补项，“待补 0”的口径不含上述两类。

待补清零与启用前复核（冻结评审条件 C4）：P0 契约对象本轮已全部建模，待补项为 0，不因此放宽新增对象的准入。新增 P0 对象、或解除任一对象的 FORBIDDEN 状态前，必须先在该对象所属章节补字段契约（字段标识、类型、必填、版本规则与变更控制），由派生器建模并重跑整包校验，未通过不得启用；每次启用前由文档管理员复核一次并留存记录，禁止只改派生文件不改正文。Phase 1 实现层验收按《Phase1-实现验收清单》执行：该清单不属本段十一类冻结文件，改动不需重算清单哈希；但为通过验收而修改契约时，仍须走上述复核流程。

Phase 1 实现验收证据（系统开发后取得，不以 Phase 0 冻结为前提）：①Gate B 核心生产——剧本到交付 Manifest 端到端集成测试、资产版本追溯、恢复与幂等测试；②Gate D 预算与计费——预算用途负向测试、预算与成本账本对账、双模式专项测试；③Gate E 权限——权限正向与负向测试（含本节负向测试表 AGENT-NEG-001～DUAL-NEG-001 全部用例）、审计检索；④Gate F 产品域——社区、反推、资产、客户审片交付端到端流程测试，以及版权、隐私、撤销演练；⑤状态机转移测试——合法转移通过、未登记转移拒绝；⑥Gate G 数据部分——指标数据血缘、样本量与归一化报告；⑦Action Registry CI 流水线实际运行（校验规则已在 31.6 定义，此处指流水线落地）；⑧部署与兼容性验收。上述证据未取得前，对应能力不得标记 ENABLED，也不得宣布 Phase 1 完成。Gate 归属口径：Gate A、Gate C 及 Gate G 的字典部分在 Phase 0 验收；Gate B、Gate D、Gate E、Gate F 及 Gate G 的数据部分在 Phase 1 验收。

### 31.12 新增架构决策记录（ADR-074～092，其中 ADR-086～092 见 17.14）

| **ADR** | **决策** | **关键约束** |
| --- | --- | --- |
| ADR-074 | V2.0 统一阶段总表为全平台唯一阶段映射。 | 第 13、24—30 章及附录不得定义冲突 Phase；P0/P1/P2 与 Phase 分离。 |
| ADR-075 | Phase 1 核心生产闭环是唯一主硬门。 | 辅助域可灰度但不得阻塞主链；白模未验证时不是生产前置。 |
| ADR-076 | 社区、快照、发布审核、申诉、分享和复用使用独立状态机。 | CommunityPost、ShowcaseSnapshot、PublicationReview、Appeal、ShareLink、ReuseRequest 不混写状态。 |
| ADR-077 | 统一 Action Registry 是所有入口权限判定的唯一来源。 | 旧动作名只作兼容别名；工作台、Agent、API、审计和测试共用 action_key。 |
| ADR-078 | Agent 仅创建草案/候选，不掌握权威审批权。 | 人类审批与 CapabilityToken 双重校验；Token 不替代审批记录。 |
| ADR-079 | 产品预算池是四类预算分类的简化视图，不合并底层账本；Phase 1 不启用跨账户自动转拨。 | BudgetLedger、CostLedger、QuotaReservation 分离；同账户额度可按审批重分配；跨账户 BudgetTransferRequest 人工审批，Phase 2 以后启用；Quality Reserve 不用于探索。 |
| ADR-080 | 分享 Viewer 与客户审片 Viewer 共用底层媒体能力但分离业务权限。 | ShareLink 默认禁止下载、受控代理、水印、有效期、撤销和访问审计；ClientReview 单独批注权限。 |
| ADR-081 | 视频反推默认 REFERENCE_ONLY，只能产生新生产草案。 | 不直写 ShotSpec/ConstraintPack/Canon；来源授权按上传、分析、截帧、转录、展示、复用分别核验。 |
| ADR-082 | 共享主体库不共享 CharacterCanon；Pattern 晋升独立治理。 | 社区热度不直接改变 PMS/Pattern；权利撤销生成既有引用影响清单。 |
| ADR-083 | 交付 Manifest 必须可追溯、可校验、可重建。 | 客户批注不直接改 Gate；EscapeReport 关联 RCA 且不覆盖原交付。 |
| ADR-084 | 社区/反推/客户审片成本独立分类。 | COMMUNITY_OPS_COST、SHOWCASE_TRANSCODE_COST、REVERSE_ANALYSIS_COST、CLIENT_REVIEW_COST 不占 Exploration/Quality Reserve。 |
| ADR-085 | V2.0 以文档/契约/自动化证据作为正式冻结依据。 | 功能存在于文档不等于已实现；未验证能力必须标记 NOT_ENABLED/UNVERIFIED。 |

V2.0.1 最终文档决议：完整保留 V1.3.5 的有效领域内容、技术契约、ADR 和附录；本次统一平台阶段、状态机、Action Registry、预算转拨与产品边界。生产画布不公开，社区只传播经授权的不可变快照；分享不等于发布，发布不等于交付；Agent 不得自批自用；反推不形成生产权威事实；预算成本与资金结算分离；所有能力必须以测试、审计和部署证据确认实现状态。本文件为 Phase 0 契约冻结条件通过的修订基线（决议 AFS-P0-FREEZE-2026-11-13-01）；冻结范围只限文档与机器可读契约层，Phase 1 实现与各工具 Profile 启用仍须以代码、测试、权限、版权、账本、QC/Gate 和部署证据验收，不代表任何功能已经实现。文档状态以本行与文件头部为准，其他章节不得另述冻结状态。

### 31.13 V2.0.1 评审意见逐项关闭记录

| **评审项** | **V2.0.1 处理结果** | **状态** |
| --- | --- | --- |
| 六层架构 L1—L6 | 复核原表已列全六层，保留完整定义并继续作为唯一架构分层表。 | 已核验 |
| Task 状态机与 Previz 字段 | 复核原文状态转移无数字乱码；Previz/previz_id 均为正确拼写，保留并纳入渲染检查。 | 已核验 |
| 字段/枚举/成本名 | 复核 stale_state、request_id、cost_bearer、PG0—PG3、QCReport、COMMUNITY_OPS_COST 和“终检报告”均为规范写法。 | 已核验 |
| 唯一阶段总表 | 31.2 为唯一权威表；23.1、30.5、C.8、D.8 和生产工序阶段均明确标注摘要/非权威或不同语义。 | 已修订 |
| Phase 1 社区范围 | 仅只读分享、内部审核记录或组织内封闭灰度；公开审核发布关闭；组织内正式发布 Phase 2，公开发布 Phase 3。 | 已修订 |
| ConstraintPack 硬门 | 核心路径加入“有效 ConstraintPack（如适用）”；无锁定约束镜头须标记 NOT_APPLICABLE。 | 已修订 |
| Action Registry | 新增完整字段与核心生产/社区/反推/客户审片/交付/预算动作注册表，并新增旧名→规范 action_key 映射。 | 已修订 |
| 状态机 | PublicationReview/Appeal 分离；Appeal 决定码明确；ReuseRequest 加 EXECUTING/FAILED；新增 DeliveryManifest 与 Pattern 生命周期。 | 已修订 |
| 客户审片术语 | 客户观看和批注统一称客户审片；审计仅用于日志和证据追溯。 | 已核验并统一 |
| 预算转拨 | Phase 1 禁止跨账户自动转拨；同账户额度经审批重分配；跨账户申请人工审批，Phase 2 以后启用。 | 已修订 |
| 第 0 章规范性用语 | 统一为“所有必须均为目标系统的设计约束；实施状态以代码、测试和部署验收为准”。 | 已核验 |

V2.0.1：统一架构、产品路线、工程契约和验收基线；包含主章节 0—31 与内嵌附件 A—D。

Action Registry：统一权限动作注册表；规范 action_key 是 UI、API、Agent、CapabilityToken、审计和测试的唯一动作标识。

PublicationReview：独立于 CommunityPost 的展示发布审核对象。

Appeal：独立申诉对象；申诉不自动恢复被下架内容。

Media Viewer：分享与客户审片共用的只读媒体播放/代理转码底层服务。

ProjectBudgetPool：项目级预算交互视图，不等同于资金账户；底层账本仍独立。

NOT_ENOUGH_DATA：样本量或证据不足时的统一指标展示状态；不排名、不输出伪精确结论。

EXAMPLE_PLACEHOLDER：文档中所有示意 ID、凭证引用、时间戳、URL、模型版本和金额均为示例占位符，不可直接作为生产配置。

## 32. 外部工具适配层与专业执行生态（External Tool Adapter & Professional Execution Ecosystem）

本章是 V2.0.2 新增的规范性架构章节。它不新增平台逻辑层、不改变 L1—L6 六层骨架、不改变统一质量基线、QC、Gate、权限、预算和审计的权威归属。平台的核心竞争力是工业化治理能力；外部专业工具负责具体创作执行和专业操作，平台通过统一适配器接入，并对所有输入、输出、版本、权利、质量和流程状态负责。阶段映射以第 31.2 节唯一权威阶段总表为准，本章只补充外部工具域的详细契约和验收。

### 32.1 战略定位、职责边界与非目标

平台不是 Premiere、DaVinci Resolve 或剪映专业版的替代品，也不建设完整多轨剪辑器、专业音频工作站、3D 建模器或特效合成器。平台自研并做深的是硬约束、质量治理、全链路追溯、生产调度、资产沉淀复用、权限安全、成本预算七类工业化能力；专业工具负责时间线操作、音频制作、3D/特效创作和具体内容加工。

*表 32-1 平台与外部专业工具的责任边界*

| **能力域** | **平台必须自研并保持权威** | **外部工具负责执行** |
| --- | --- | --- |
| 硬约束与 Canon | 剧本事实、角色 Canon、世界观连续性、锁定 ShotSpec/ConstraintPack、版权与用途规则；维护权威版本和冲突处置。 | 按平台提供的有效约束和引用执行编辑/创作；不得改变权威事实或自行解除锁定。 |
| 质量治理 | 统一五维质量模型、QCReport、缺陷结构化、质量基线、Override Policy、Gate 及审批证据。 | 执行专业加工；可回传检测结果/操作元数据，但不能自行签发平台 QC PASS 或 Gate PASS。 |
| 追溯与资产 | AssetVersion 不可变、哈希、来源血缘、工具/操作者/版本、DeliveryManifest 和审计。 | 保存原生工程和导出文件；向平台回传工程版本、产物、来源引用和必要操作日志。 |
| 调度与预算 | DAG、Task、S/A/B 策略、镜头风险升级、配额、四类预算账户、CostLedger 与失败恢复。 | 按已授权任务执行并返回状态、用量、失败码；不得自行扩大预算或更改调度策略。 |
| 资产复用与版权 | 统一资产中枢、Rights Manifest、许可范围、复用审批、跨项目隔离和下架撤销。 | 只使用授权下发的素材；新引入素材必须申报来源、授权和用途，不得默认拥有二次传播权。 |
| 权限与安全 | RBAC+ABAC、Capability Token、职责分离、租户隔离、密钥托管、审计与撤销。 | 执行最小权限调用；不持有平台根密钥，不直接写平台数据库，不允许工具本地角色替代平台授权。 |

平台允许提供 Media Viewer、时间码批注、资产检索和审批入口，但这些属于查看、定位和治理交互，不构成专业剪辑/特效编辑器。不得以“工作台”“画布”名义重新引入多轨剪辑、特效包装、字幕编辑、混音或 3D 创作工具型功能。

### 32.2 在六层架构中的位置与模块关系

ExternalToolAdapter 是 L3“生成与执行”层内的统一适配器抽象，不新增第七层。EditingToolAdapter 是其针对剪辑工具的专用实现；AudioToolAdapter、ThreeDToolAdapter、VFXToolAdapter 可按相同 SPI 扩展。L2 负责任务编排和授权校验，L3 负责外部工具通信与文件交换，L4 负责工程文件和回流资产版本，L5 继续独立执行 Rights/QC/Gate，L6 观测连接、运行、用量和故障。

*表 32-2 六层职责映射（仅解释适配器，不改变原六层定义）*

| **层级** | **与外部工具集成有关的职责** | **禁止越界事项** |
| --- | --- | --- |
| L1 交互与管理 | 选择工具、授权连接、发起导出/回流、展示适配能力状态和任务结果。 | UI 不得直接决定工具是否具备 VERIFIED 能力或绕过权限。 |
| L2 编排与 Agent | 生成 ToolExchangeSession、检查依赖和预算、调度适配器任务、消费签名事件并推进白名单 DAG。 | Agent 不得自行审批连接、版权、QC、Gate 或正式发布。 |
| L3 生成与执行 | ExternalToolAdapter SPI、格式转换、连接器运行时、工具调用、上传下载、回调验签和故障恢复。 | 不得直接写 L4 权威资产状态或 L5 决策。 |
| L4 数据与资产 | 统一归档原生工程、时间线交换文件、素材包、成片和 AssetVersion 血缘。 | 不得以外部路径或可变文件名替代平台资产 ID/版本/哈希。 |
| L5 质量与治理 | 按既有基线执行版权、安全、QC、人工 Review、Gate 和 Override。 | 不得因工具为外部专业软件而减少门禁或采用工具自报 PASS。 |
| L6 可观测与运营 | 采集 Adapter Health、兼容性状态、导入/导出成功率、延迟、成本、回调重试和错误分类。 | 指标缺失不得被解释为工具能力已验证或链路已完成。 |

外部工具、插件、连接器和回调服务一律视为不可信执行边界。平台只通过明确版本化的适配器契约通信；适配器不得使用数据库账号直连 Project DB、Asset Service 或 Gate 数据表，所有业务写操作必须转换为已注册领域命令。

### 32.3 四级统一接入能力

四级能力是适配器能力声明，不表示所有工具必须一次实现全部级别，也不表示高等级自动包含低等级。每个工具产品、版本、部署模式和格式 Profile 必须分别给出证据；仅可将实际通过验证的能力标记为 VERIFIED。

*表 32-3 外部工具四级接入模型*

| **级别** | **能力范围** | **剪辑工具示例** | **平台验收要点** | **阶段边界** |
| --- | --- | --- | --- | --- |
| L1 文件级互通 | 工程/时间线/素材包/成片导出与回流、自动归档。 | 导出已验证的 XML/EDL 时间线和素材包；归档原生工程文件；最终渲染回流。 | 格式 Profile、媒体引用、哈希、帧率/时间码、版权和血缘校验；回流新建 AssetVersion。 | Phase 0 冻结契约；Phase 1 实现最小文件闭环。 |
| L2 元数据级同步 | 镜头元数据、QC 缺陷、审片批注、时间码和状态提示的双向映射。 | 将 QC 缺陷/客户审片批注按镜头与时间码定位至剪辑工程；将剪辑标记回传为建议。 | 字段映射、版本锚点、时间码精度、冲突处理；回传数据不得直接改变 QC/Gate。 | Phase 2；优先验证 Premiere Pro 与剪映专业版。 |
| L3 插件级调用 | 在工具内部通过轻量插件访问平台资产、查看任务、提交草案/审批请求。 | 剪辑插件内检索授权素材、查看镜头信息、提交回流或 Gate 申请。 | 签名插件、最小权限 Token、双入口一致性、密钥不落本地、工具卸载与撤销。 | Phase 3；仅开放已经安全验证的工具版本。 |
| L4 流程级联动 | 工具事件回传并触发平台白名单 DAG 节点，自动同步任务状态。 | 保存/导出完成事件触发资产导入验证和 QC 任务；失败事件进入重试/人工队列。 | 签名、幂等、时间窗、重放防护、状态对账、允许事件白名单和回滚。 | Phase 3 起逐步开放；不得让外部事件直接写权威业务状态。 |

能力状态（生命周期）取值为 DECLARED、TESTING、VERIFIED、PILOT、ACTIVE、SUSPENDED、RETEST_REQUIRED、REVOKED；ACTIVE 表示已完成测试、权限/安全审查和业务批准并允许进入生产路由，PILOT 为受限试点。VERIFIED 必须绑定 adapter_version、tool_product_version、部署模式、格式 Profile、测试报告和验证日期；工具主版本升级、格式行为变化、安全事件或测试失败时，应将对应能力降为 SUSPENDED 或 RETEST_REQUIRED，不得沿用旧证据。33.6 能力矩阵中的 VERIFIED/PARTIAL/UNSUPPORTED/UNVERIFIED 是能力项支持级别（support_level），不是本状态集，两者不得混用。

### 32.4 ExternalToolAdapter 统一 Manifest 与 SPI 契约

每个适配器必须提供签名、版本化的 AdapterManifest，并注册到 Adapter Registry。Manifest 是能力声明，不等于生产授权；只有通过验证、审批和作用域绑定后，连接才可进入可用状态。

*表 32-4 AdapterManifest 必需字段*

| **字段组** | **字段** | **规范要求** |
| --- | --- | --- |
| 身份与版本 | adapter_id、adapter_version、adapter_type、provider、product、supported_product_versions | adapter_id 稳定唯一；adapter_version 使用语义化版本；产品/版本范围明确，不得只写“最新版”。 |
| 能力声明 | capability_levels、supported_operations、format_profiles、media_types | 按能力级别逐项声明；每种格式标注导入/导出、方向、限制和兼容版本。 |
| 连接与部署 | auth_modes、deployment_modes、credential_ref_policy、network_policy | 只传 credential_ref；支持云端/混合/本地私有化的能力按实际验证声明。 |
| 安全与隔离 | signature_ref、sandbox_profile、permission_scopes、data_residency、egress_policy | 签名可验证；沙箱、文件读写目录、出站网络和租户隔离策略明确。 |
| 事件与兼容性 | event_schema_versions、callback_auth、idempotency_policy、min_platform_version | 回调必须签名和幂等；声明支持的事件 Schema 与平台最低版本。 |
| 验证与生命周期 | verification_evidence_refs、verified_capabilities、lifecycle_status、revocation_ref | 能力验证证据可追溯；支持暂停、撤销、回滚和版本退役。 |

统一 SPI 至少包含以下操作；具体 SDK、HTTP、消息总线或本地 Connector Runtime 只是传输实现，不改变业务契约：

*表 32-5 ExternalToolAdapter 标准接口*

| **接口** | **输入/输出职责** | **不允许的副作用** |
| --- | --- | --- |
| get_manifest() | 返回 adapter_id/version、能力等级、格式 Profile、状态和证据引用。 | 只读；不得在读取时自动提升 VERIFIED 或创建生产连接。 |
| validate_connection() | 测试 credential_ref、工具实例版本、权限范围和网络连通性，生成验证报告。 | 不得回显密钥、扩大权限或修改业务资产。 |
| export_package() | 接收固定 AssetVersion refs、Rights/Policy 快照和格式 Profile，生成 ToolExchangePackage。 | 不得读取未授权项目或导出秘密、私有提示词和未授权素材。 |
| submit_import() | 接收外部产物引用/文件，先进入 Staging/Quarantine 并返回 ImportJob。 | 不得直接批准资产、通过 QC/Gate 或标记 DELIVERED_FINAL。 |
| sync_metadata() | 按 Schema 映射镜头标记、QC 缺陷和审片批注，返回字段级同步结果。 | 不得覆盖 QCReport、GateDecision、Canon、Rights 或客户决策。 |
| verify_event() | 验证回调签名、nonce、时间窗、adapter_version、事件 Schema 和幂等键。 | 未通过验证不得触发 DAG；不得接受重放或未注册事件。 |
| health_check()/revoke() | 报告运行健康状态；撤销时停止连接、Token、密钥引用和回调订阅。 | 健康检查不得写业务状态；撤销必须保留审计和影响清单。 |

统一命令/响应信封应复用附录 C.3.2 的 Domain Command、Domain Event 和 Error Envelope，不另建平行协议。Adapter 请求必须携带 request_id、idempotency_key、adapter_id/version、actor_context、scope、capability_token_ref、target_asset_version_ids、format_profile_id、rights_snapshot_ref、policy_snapshot_ref 和 trace_id；响应必须返回 operation_id、status、artifact_refs、sha256、tool_product_version、adapter_version、evidence_refs、error_code 和 trace_id。

### 32.5 工程资产模型与 AssetVersion 统一治理

外部工具工程文件不是独立于平台的第二套素材库。工程文件、时间线交换文件、素材包和最终渲染均纳入 L4 Asset Service，使用平台 Asset/AssetVersion、哈希、Rights Manifest 和血缘模型；外部工具本地目录只作为执行副本或缓存。

*表 32-6 外部工具域对象*

| **对象** | **核心字段** | **所有权/生命周期规则** |
| --- | --- | --- |
| ExternalToolConnection | connection_id、tenant_id、organization_id、project_id、adapter_id/version、external_project_ref、credential_ref、scope、status、validated_at | 由 Integration/Connection Domain 管理；DRAFT→VALIDATING→ACTIVE→SUSPENDED/REVOKED；按租户/组织/项目隔离。 |
| ToolExchangePackage | exchange_id、source_asset_version_ids、file_manifest、format_profile_id、rights_snapshot_ref、policy_snapshot_ref、expires_at、sha256 | 由 Asset/Integration Domain 管理；源版本固定不可变；下载链接短时有效，过期/撤销可追溯。 |
| ToolExchangeSession | session_id、exchange_id、connection_id、external_operation_id、operator_principal、tool_version、started_at、ended_at、status | 由 Orchestrator 管理；状态只描述工具交互，不替代 Asset/QC/Gate 状态。 |
| ExternalToolArtifact | artifact_id、session_id、file_name、media_type、size_bytes、sha256、staging_ref、source_refs、tool_metadata、scan_results | 由 Staging/Asset Intake 管理；先隔离和验证，合格后再创建正式 AssetVersion。 |
| TimelineManifest | timeline_id、sequence_id、frame_rate_num/den、timebase、drop_frame、tracks、clips、source_asset_version_ids、clip_in/out_frames、mapping_profile | 作为版本化时间线描述；时间以整数帧/有理帧率表达，避免浮点秒数造成偏移。 |
| AssetVersion（工程资产） | asset_version_id、asset_type、sha256、storage_ref、tool_product/version、adapter_id/version、source_asset_version_ids、operator_ref、rights_manifest_ref | 仍由 Asset Service 创建；新回流生成新版本，不覆盖原版本，不自动设为 APPROVED/GOLD。 |

建议新增或启用的 asset_type 包括 EDITING_PROJECT、TIMELINE_EXCHANGE、MEDIA_PACKAGE、FINAL_RENDER、AUDIO_SESSION、THREE_D_PROJECT、VFX_PROJECT。具体类型按阶段和实际接入工具启用；未启用的类型可保留 Schema，不得宣传为已支持。原生工程文件与标准交换文件应分别归档，不能以 XML/EDL 替代原生工程文件，也不能假设标准交换格式可以完整表达某个工具的全部特效/插件状态。

AssetVersion Manifest 的工具扩展字段至少包括 external_tool_provider、external_tool_product、external_tool_version、adapter_id、adapter_version、external_project_ref、external_operation_id、format_profile_id、source_asset_version_ids、operator_principal_ref、operation_started_at、operation_completed_at、imported_at、sha256、rights_snapshot_ref、policy_snapshot_ref 和 tool_metadata_ref。扩展字段只能补充现有 Manifest，不得另建旁路权威版本号。

### 32.6 文件导出、工程执行与成片回流闭环

外部剪辑属于可选执行分支。未启用外部剪辑工具的项目仍按既有核心生产主链路完成；启用外部剪辑时，工具导入/导出节点插入 DAG，但不能改变平台硬门和 Gate 顺序。

原“内部剪辑工作台”不作为平台自研编辑器或独立工具产品保留，调整为 DAG 中可选的 Editing Delivery Adapter Node（剪辑交付适配节点），只负责工程导出、成片回流、版本登记和触发 QC；不承担多轨剪辑、字幕编辑、特效包装、混音或专业素材编辑操作。

1. 导出申请：用户选择具体项目、镜头和 AssetVersion；平台按 RBAC/ABAC、Rights Manifest、导出用途、受众、数据等级和预算/配额执行预检。

2. 快照打包：平台固定源 AssetVersion、媒体文件哈希、时间线引用、帧率/时间基准、工具格式 Profile、Rights/Policy 快照和操作范围，创建不可变 ToolExchangePackage。

3. 安全交付：通过短时、单用途、可撤销的授权链接或受控本地 Connector Runtime 交付文件；不得在文件包或插件日志中携带平台根密钥、供应商明文密钥和私有提示词。

4. 外部执行：操作者在 Premiere Pro、DaVinci Resolve、剪映专业版或其他已验证工具中完成专业剪辑/加工；平台记录工具产品版本、工程引用、操作者、任务和操作时间。

5. 回流暂存：成片、工程文件、XML/EDL、音频或其他产物进入 Staging/Quarantine；先做文件类型识别、大小限制、恶意文件扫描、哈希计算、格式解析和版权来源检查。

6. 血缘校验：核对产物所引用的源 AssetVersion、素材清单、工具/适配器版本、操作者、操作 ID、时间码映射和授权范围；未知来源或授权不明的新增素材自动隔离。

7. 创建新版本：验证通过的产物由 Asset Service 创建新的 DRAFT/待审 AssetVersion；不得覆盖既有 AssetVersion，不得自动改变 GOLD/APPROVED 或 PictureLock 状态。

8. 重新质量治理：新 AssetVersion 自动进入适用的 QC 检测、版权核验和人工 Review；工具自带 QC 结果只能作为 evidence/参考，不能替代平台 QCReport。

9. Gate 与交付：只有平台 Gate 通过、权利证据有效、PictureLock/终检要求满足后，DeliveryManifest 才能引用回流版本；DELIVERED_FINAL 仍以有效 Gate4 PASS 和有效 DeliveryManifest 为准。

若同一外部操作被重复上传或回调，平台必须基于 tenant_id + adapter_id/version + external_operation_id + artifact sha256 + idempotency_key 做去重。若供应商或工具状态未知，先查询/对账，不得盲目重复创建 AssetVersion 或重复计费。返工必须产生新 ToolExchangeSession 和新 AssetVersion，并保留父版本与差异证据。

*表 32-7 ToolExchangeSession 状态机（与 AssetVersion/QC/Gate 分离）*

| **对象** | **允许状态与转移** | **规则** |
| --- | --- | --- |
| ToolExchangeSession | REQUESTED→AUTHORIZED→EXPORTED→IN_PROGRESS→IMPORT_PENDING→VALIDATING→COMPLETED | 只描述一次工具交换；AUTHORIZED 前不得导出；VALIDATING 完成前不得视为回流完成。 |
| 异常状态 | FAILED / REJECTED / EXPIRED / REVOKED / RECONCILIATION_REQUIRED | 每个异常态须保留 error_code、证据和恢复动作；权限/版权/恶意文件失败不得自动重试绕过。 |
| AssetVersion | 由既有 Asset 生命周期管理 | 回流 Session COMPLETED 不等于 AssetVersion APPROVED、QC PASS、Gate PASS 或 DELIVERED_FINAL。 |
| QC/Gate/DeliveryManifest | 各自沿用既有权威状态机 | Session 只能发起或提供证据，不得合并对象状态或直接写入最终业务状态。 |

### 32.7 时间码、时间线和元数据双向映射

剪辑工具的时间线映射必须以精确帧基准为核心，不得只用浮点秒或字符串时间码作为主键。TimelineManifest 至少保存 frame_rate_num、frame_rate_den、timebase、drop_frame、sequence_start_frame、clip_in_frames、clip_out_frames、track_id、source_asset_version_id、source_in_frame、source_out_frame 和 mapping_profile_id。音频可另存 sample_rate、sample_start 和 sample_end。

*表 32-8 时间码/元数据映射规则*

| **数据类型** | **平台规范表示** | **映射与冲突规则** |
| --- | --- | --- |
| 镜头/素材引用 | shot_id、asset_version_id、sha256、source_range | 必须精确指向平台不可变版本；工具内文件名或路径仅作显示信息。 |
| 时间线位置 | 整数帧 + frame_rate_num/den + drop_frame | 转换必须绑定 sequence Profile；无法精确映射时标记 UNMAPPABLE 并人工处理，不做静默四舍五入。 |
| QC 缺陷 | qc_report_id、finding_id、asset_version_id、frame_range、severity、evidence_ref | 同步至工具侧只作为批注/标记；工具修改标记不得反向修改原 QCReport。 |
| 客户审片批注 | client_review_portal_id、annotation_id、asset_version_id、timecode/frame_range、author_ref | 只同步获授权的审片意见；不得将批注自动转换为 Gate 结论或替客户作决定；`client_review_portal_id` 指向 ClientReviewPortal 门户实例，此处的“会话”只表示一次门户访问，不构成独立领域对象，字段名不得反向催生 ClientReviewSession。 |
| 剪辑标记/注释 | external_marker_id、tool_version、source_asset_version_id、mapped_frame_range、mapping_confidence | 回传为 ToolMetadataSnapshot 或人工待办；字段白名单之外的值不得写入核心领域对象。 |
| 时间线差异 | base_timeline_version、new_timeline_version、change_set_ref | 保留增删镜头、时长、素材替换和转场差异；冲突进入人工复核，不允许覆盖锁定 ShotSpec。 |

元数据映射必须有版本化 Mapping Profile。若时间线变更使原 QC 缺陷或客户审片批注的时间码失效，应标记 ANCHOR_STALE，保留原始锚点并提示重新定位；不得悄悄把旧批注映射到新镜头或新版本。

### 32.8 权限、版权、安全、成本与审计底线

外部工具不能成为平台权限体系之外的旁路。每次导出、连接、导入、元数据同步和事件回调均须由统一授权服务检查主体、租户、组织、项目、资源、动作、时间和工具实例；Capability Token 必须短时、单用途、最小作用域，并绑定 adapter_id/version 与受众。

*表 32-9 外部工具治理控制*

| **控制面** | **强制规则** | **违反时处理** |
| --- | --- | --- |
| 权限与职责分离 | 工具侧身份映射到平台 principal；Token 绑定动作/项目/资源版本/工具/有效期；工具侧不能直接批准 Gate、Canon、Rights 或预算。 | 拒绝调用并写 AUTH_SCOPE_DENIED；高风险尝试触发告警和安全审查。 |
| 密钥与数据 | 密钥仅保存在 Secrets Manager；导出包使用短时授权；敏感提示词、客户资料、私有 Canon、密钥和其他项目数据默认不导出。 | 隔离文件、撤销连接、轮换凭证并记录泄露/访问影响。 |
| 版权与授权 | 导入素材必须有来源、许可范围、可用地域/媒介/期限和再传播权证据；导出授权不等于公开发布/商业使用授权。 | Rights 不明进入 QUARANTINED；禁止进入正式资产库、社区发布和交付。 |
| 质量与门禁 | 任何回流最终成片必须重新执行平台 QC、人工 Review 和适用 Gate；外部工具结果不具备权威 PASS 效力。 | 不满足门禁时保持 BLOCKED/REWORK；不得生成 DELIVERED_FINAL。 |
| 预算与成本 | 沿用现有四类预算账户、BudgetLedger、CostLedger、QuotaReservation 和成本分类字典；工具费用记录 provider/tool/version、price_version、source_ref 和估算/实付属性。 | 用途冲突按 BUDGET_ACCOUNT_PURPOSE_VIOLATION 阻断；不得新增旁路余额或暗中挪用 Quality Reserve。 |
| 审计与追溯 | 记录 actor、connection、adapter/tool 版本、动作、源版本、导出范围、输出哈希、Rights/Policy 快照、时间、错误和 QC/Gate 结果。 | 审计证据缺失时不得标记链路完成；按 RCA/安全事件流程处理。 |

Agent 可以按授权创建导出/导入草案或发起工具任务，但不得批准适配器进入生产、批准版权、确认关键镜头/Canon、通过 QC/Gate、修改账本或执行撤销以外的高危治理动作。外部插件必须复用第 31.6 节规范 action_key；不允许以插件自定义 action 直接调用后端写接口。

### 32.9 Action Registry 扩展与事件白名单

本章新增的外部工具动作已逐项写入第 31.6 节 Action Registry；该表仍是唯一规范动作注册表。新增规范 action_key 为：EXTERNAL_TOOL.ADAPTER.REGISTER、EXTERNAL_TOOL.ADAPTER.VERIFY、EXTERNAL_TOOL.ADAPTER.REVOKE、EXTERNAL_TOOL.CONNECTION.CREATE、EXTERNAL_TOOL.CONNECTION.VALIDATE、EXTERNAL_TOOL.EXPORT.CREATE、EXTERNAL_TOOL.IMPORT.SUBMIT、EXTERNAL_TOOL.RETURN.INGEST、EXTERNAL_TOOL.METADATA.SYNC、EXTERNAL_TOOL.EVENT.INGEST。各动作的 resource_type、scope、risk_level、Token 要求、允许主体、审批要求、audit_event、negative_test_ids 和 phase 状态以第 31.6 节逐行定义为准。

ExternalToolEvent 只是一条外部执行事实，不等于领域命令。回调事件类型必须在 AdapterManifest 中注册并通过签名验证，事件映射到的内部命令必须是 allowlist；禁止外部工具直接调用任意 DAG node、直接修改 Task/Gate/Asset 状态或伪造平台 actor。

*表 32-10 允许事件的最小分类*

| **事件类别** | **可接受示例** | **平台动作边界** |
| --- | --- | --- |
| 运行状态 | TOOL_JOB_STARTED、TOOL_JOB_PROGRESS、TOOL_JOB_FAILED | 只更新 ToolExchangeSession/运行观测；不更改资产或 Gate 结果。 |
| 导出完成 | TOOL_EXPORT_COMPLETED | 核对 package_id、签名和哈希后，将 Session 转入可下载/可执行状态。 |
| 回流提交 | TOOL_IMPORT_SUBMITTED | 创建或更新隔离导入任务；触发扫描、格式验证和血缘校验。 |
| 工程/元数据更新 | TOOL_PROJECT_SAVED、TOOL_METADATA_CHANGED | 生成 ToolMetadataSnapshot 或差异提案；按阶段决定是否同步。 |
| 回调安全事件 | TOOL_CALLBACK_REPLAYED、TOOL_SIGNATURE_INVALID | 拒绝事件并产生安全审计/告警；不得触发生产任务。 |

### 32.10 与 Phase 0—4 的实施边界

本节为第 31.2 节唯一阶段总表的外部工具实施补充，不建立第二套阶段权威表。

- Phase 0：冻结 ExternalToolAdapter SPI、AdapterManifest、能力声明与证据模型、ExternalToolConnection、ToolExchangePackage、ExternalToolArtifact、TimelineManifest、AssetVersion 扩展字段、签名/沙箱/密钥/数据隔离、Action Registry、错误码、回调契约和 conformance 测试；仅冻结接口和数据结构，不宣称已完成任何具体工具适配。

- Phase 1：落地 L1 文件级互通最小闭环，包括可验证的时间线 XML/EDL Profile、素材包导出、原生工程归档、成片回流、哈希/版权/血缘检查、新 AssetVersion、自动 QC 和人工 Gate。若目标软件/版本未通过往返测试，则该格式不标记 VERIFIED；不得宣称通用 XML/EDL 对所有工程功能无损。

- Phase 2：落地 L2 元数据同步，将平台 QC 缺陷、客户审片批注和镜头信息按版本/时间码映射到剪辑工程；优先验证 Premiere Pro 与剪映专业版，其他工具根据客户实际环境和验证证据排序。

- Phase 3：落地 L3 插件级调用和 L4 流程级联动。插件只暴露授权范围内的资产检索、任务提交、回流和审批申请；签名事件只能触发 allowlist DAG 节点，必须具备重放防护、幂等和对账。

- Phase 4：扩展 Audio、3D、VFX 等专业工具适配和生态市场；每类新适配器均需签名、沙箱、能力验证、撤销和单独的版权/隐私/供应商风险评审。

Phase 0 的验收结果是“契约可执行、校验可自动化、权限可测试”，不是“已接通所有工具”；Phase 1 的验收结果是“至少一个经过实际验证的工具 Profile 完成文件级闭环”，不是“平台提供剪辑功能”。

### 32.11 兼容性验证、上线门槛与负向测试

每个 Adapter/Tool 组合按产品版本、平台部署模式、操作系统、文件格式 Profile 和能力级别生成独立的 AdapterCapabilityEvidence。适配器只有在测试通过、权限审查、安全审查和业务负责人批准后才能由 VERIFIED 进入 PILOT/ACTIVE；验证过期或依赖版本变化必须重新测试。

*表 32-11 外部工具适配器最小验收矩阵*

| **测试 ID** | **验收目标** | **通过条件/负向条件** |
| --- | --- | --- |
| EXT-ACC-001 | Manifest 与签名 | 未知签名、篡改包、版本不兼容必须拒绝；签名、版本、能力和证据引用可追溯。 |
| EXT-ACC-002 | 连接与租户隔离 | 跨租户/跨项目连接失败；连接只引用 credential_ref；日志、错误和响应不泄露密钥。 |
| EXT-ACC-003 | 文件级往返 | 样例工程在明确版本/Profile 下导出和导入；素材引用、帧率、时间码、音频轨和关键时间线结构满足测试阈值。 |
| EXT-ACC-004 | 时间码映射 | 帧索引和帧率换算可复算；drop-frame/非 drop-frame 不混用；无法映射时进入人工队列。 |
| EXT-ACC-005 | 版本不可变 | 回流始终生成新 AssetVersion；重复上传按幂等键去重；覆盖已审批版本的请求被拒绝。 |
| EXT-ACC-006 | 版权与恶意文件 | 缺 Rights Manifest、越权素材、病毒/恶意脚本/不支持格式进入隔离；隔离产物不可被生产读取。 |
| EXT-ACC-007 | QC/Gate 不可绕过 | 回流产物无论工具侧自报结果如何，均需平台 QC/人工 Gate；伪造 PASS 和直接 DELIVERED_FINAL 必须拒绝。 |
| EXT-ACC-008 | 元数据同步 | 缺陷和批注锚定正确资产版本与时间码；冲突标记 STALE/UNMAPPABLE；不能改变 QCReport/GateDecision。 |
| EXT-ACC-009 | 回调安全与幂等 | 无签名、超时、重放、nonce 复用或未知事件必须拒绝；相同事件多次投递不重复触发 DAG。 |
| EXT-ACC-010 | 故障恢复与撤销 | 断网、上传中断、工具崩溃可恢复或显式失败；撤销后新调用被拒绝，既有操作可对账。 |
| EXT-ACC-011 | 成本与审计 | 工具调用/运行用量与 CostLedger、QuotaReservation 对齐；actor、tool_version、adapter_version、source/output hashes 完整。 |
| EXT-ACC-012 | 能力声明准确性 | 未通过测试的功能不得标记 VERIFIED；工具升级后过期证据不能继续支撑原能力声明。 |

建议纳入运营指标：Adapter Connection Success Rate、Export Completion Rate、Import Validation Pass Rate、External Return-to-QC Lead Time、Timeline Mapping Error Rate、Duplicate Import Prevention Rate、Callback Replay Rejection Rate、External Tool Cost per Approved Minute。统计时必须分工具产品/版本/部署模式/Profile 报告，样本不足时标记 INSUFFICIENT_DATA，不得将不同工具的成功率混算。

### 32.12 工程目录与模块数据所有权

在现有模块化单体和 L1—L6 目录结构中新增适配器模块，不新增微服务拆分要求。建议目录归属如下：

*表 32-12 建议模块与文件边界*

| **目录/模块** | **职责** | **数据所有权与边界** |
| --- | --- | --- |
| external-tool-adapter/ | 统一 SPI、Adapter Registry、Manifest 校验、能力声明和版本兼容。 | 管理 AdapterManifest、AdapterCapabilityEvidence；不拥有 Project/Asset/Gate。 |
| editing-tool-adapter/ | 剪辑工具专用格式 Profile、时间线解析/序列化、工程文件封装与回流映射。 | 生成 ToolMetadataSnapshot/TimelineManifest 草案；不维护独立素材库。 |
| tool-connection/ | 连接配置、credential_ref、部署模式、健康检查、撤销和安全审计。 | 管理 ExternalToolConnection；密钥值由 Secrets Manager 所有。 |
| tool-exchange/ | ToolExchangePackage、ToolExchangeSession、导出/导入任务、幂等和对账。 | 管理交换生命周期；正式资产版本由 Asset Service 创建。 |
| asset-intake-staging/ | 文件隔离、杀毒、MIME/格式识别、哈希、Rights 和血缘检查。 | 管理暂存产物；验证前禁止进入正式资产检索/生产路线。 |
| metadata-mapping/ | QC/客户审片批注/镜头元数据与外部时间线映射。 | 只保存映射 Profile、快照和差异提案；不拥有 QC/Gate 权威状态。 |

具体部署可将上述模块作为同一应用中的包、Worker 和受控本地 Connector Runtime；不要为追求“平台化”提前拆成多个微服务。模块之间继续使用第 14 章的 Application Service、Domain Command、Outbox/Inbox 和数据所有权规则。

### 32.13 架构决策记录（ADR-093～100）

*表 32-13 外部工具适配层 ADR*

| **ADR** | **决策** | **关键约束** |
| --- | --- | --- |
| ADR-093 | 平台核心定位为工业化治理底座，不自研替代性专业剪辑/音频/3D/特效工具。 | 不得以末端工具功能扩张挤占约束、质量、追溯、调度、资产、权限和合规研发资源。 |
| ADR-094 | ExternalToolAdapter 放置于现有 L3 执行层，保持 L1—L6 六层骨架不变。 | EditingToolAdapter 是专用实现；工具接入不能引入新的权威状态层或独立素材库。 |
| ADR-095 | 外部工程文件、时间线、素材包和成片统一纳入 Asset Service。 | 任何回流产物均生成新的不可变 AssetVersion；不得覆盖旧版本或绕过 Rights/QC/Gate。 |
| ADR-096 | 外部工具按文件、元数据、插件、流程四级能力逐步接入。 | 每个 Tool/Version/Profile 独立验证；未验证能力不得宣传、路由或自动执行。 |
| ADR-097 | 外部执行不改变平台质量治理与审批权威。 | 外部 QC 结果仅作证据；回流版本重新 QC/Review/Gate，客户批注不等于 Gate 决策。 |
| ADR-098 | 外部工具接入按 Phase 0—4 渐进实施。 | Phase 0 冻结契约；Phase 1 文件级闭环；Phase 2 元数据同步；Phase 3 插件和签名事件；Phase 4 扩展专业工具生态。唯一阶段表仍为 31.2。 |
| ADR-099 | 工具连接、导出、导入、同步、事件和撤销使用唯一 Action Registry 与 Capability Token。 | 不允许工具旁路写 DB、Gate、Canon、Rights、Budget 或 DeliveryManifest；默认拒绝未注册动作和事件。 |
| ADR-100 | 标准交换格式按具体工具版本/Profile 验证，不承诺全功能无损互通。 | XML/EDL 只在实际测试覆盖范围内标记 VERIFIED；原生工程文件与交换文件分别归档，映射不确定时人工复核。 |
| ADR-113 | TimelineDraft 与 ExportPackage/ToolExchangeSession 使用分离状态机。 | 外部导入/回流是交换会话事件；UI 可显示流程投影，但不得混入 TimelineDraft 权威 status。 |
| ADR-114 | 禁止任何模式在平台内执行 SimpleRender 或最终成片渲染。 | CREATOR_MODE/FULL_MODE 均不做帧拼接、转场/字幕/混音/色彩渲染；未来云渲染必须另立 ADR。 |
| ADR-115 | 工程包必须包含导入指引、Rights Notice 与 Usage Restrictions。 | 不具备 Profile 能力矩阵或导入说明时不得标记 ExportPackage VALIDATED。 |
| ADR-116 | 模板预设只提供可编辑元数据起点。 | 短视频/短剧/口播预设不得锁定镜头、不改 Canon、不生成渲染媒体。 |

### 32.14 本章冻结检查与实施前置条件

- 六层骨架保持 L1—L6，不新增平台主逻辑层；ExternalToolAdapter 归属 L3，工程资产归属 L4，QC/Gate/Rights 仍归属 L5。

- AdapterManifest、SPI、ToolExchangeSession、ToolExchangePackage、ExternalToolArtifact、TimelineManifest 和 AssetVersion 扩展字段具备 Schema 与版本兼容测试。

- 新增 10 项规范 action_key 已纳入第 31.6 节 Action Registry，全部具有资源、scope、风险、Token、允许主体、审批、审计、负向测试和阶段状态。

- 导出包权限最小化；工具连接无明文密钥；回调具备签名、nonce、时间窗、幂等、allowlist 和撤销机制。

- 回流产物在进入正式资产库前完成文件安全、哈希、格式、Rights 和血缘校验；任何异常均可隔离、拒绝和审计。

- 回流最终成片重新执行平台 QC、人工 Gate 和 DeliveryManifest；外部工具无权改变质量基线或审批状态。

- Phase 1 只实现经过验证的文件级互通；不将元数据同步、插件调用、自动流程联动或完整剪辑功能列为 Phase 1 前置。

冻结判定：本章完成后，可将外部工具适配层接口与数据契约纳入 Phase 0 架构基线；只有当 Schema、权限负向测试、文件安全、幂等、版本不可变、QC/Gate 回流和工具能力验证证据全部通过，方可批准具体 Adapter 进入 Phase 1/后续阶段。文档纳入不代表任何第三方工具已连接、已认证或已完成生产适配。

## 33. 智能时间线编排与工程包导出（Timeline Orchestration & Export Package）

本章定义“智能编排 + 打开即排好的外部工程包 + 代理片段顺序预览”的产品能力。平台负责镜头顺序、时长、帧级入出点、字幕 cue、音频对齐引用、转场标记和格式投影；外部工具负责专业剪辑与最终渲染。平台不自研帧级剪辑内核，不做帧拼接、转场渲染、字幕烧录、混音、色彩渲染或最终 MP4 生成。该边界对 FULL_MODE 与 CREATOR_MODE 同样适用。

规范性禁止项：平台不执行帧级拼接/转场渲染/字幕烧录/混音/最终成片渲染；不提供 SimpleRender 或任何形式的可下载合成预览文件。该限制适用于 FULL_MODE 与 CREATOR_MODE，任何未来云渲染能力必须另行 ADR 审批。

### 33.1 产品边界与用户文案

推荐文案：已为你生成素材与预编排时间线；一键预览时间线；一键导出剪映草稿/工程包；导入外部剪辑工具后可继续微调并完成最终输出。禁止使用“一键成片”“平台已完成剪辑/混音/渲染”等误导性文案。平台的“预览”只验证顺序、粗节奏和素材齐全性，不是成片、不是交付、不是社区发布文件。

### 33.2 TimelineDraft / EditDecisionList 权威对象

TimelineDraft 是平台侧唯一权威、版本化的元数据编排对象。EditDecisionList（EDL）是按指定 Format Profile 从 TimelineDraft 投影出的交换表示，不是另一份可独立写入的权威时间线。FCPXML、EDL、剪映草稿和其他原生工程均为 ExportPackage 的导出产物；工具侧改动只有回流并创建新 AssetVersion/TimelineDraft 版本后才进入平台血缘。

| **字段组** | **字段/类型** | **规范与不变量** |
| --- | --- | --- |
| 身份 | timeline_draft_id、project_id、workspace_id、episode_id、sequence_id | 唯一 ID；workspace_id 属于同一租户/组织；记录 workspace_mode。 |
| 来源 | source_shot_revision_ids[]、source_asset_version_ids[]、source_rights_snapshot_ref | 必须引用精确不可变 ShotRevision/AssetVersion；禁止只保存“最新版本”可变指针。 |
| 排序与时长 | shot_order[]、duration_per_shot_frames{}、clip_in_frame、clip_out_frame | 排序、入出点和时长以整数帧表达；fps 用有理数保存；不允许浮点秒累计漂移。 |
| 字幕轨 | subtitle_cues[]：cue_id、start_frame、end_frame、text、language、style_profile_ref | 只保存字幕内容/时间码/样式引用；导出可映射 SRT/ASS/工具轨道；平台不烧录字幕。 |
| 音频对齐 | audio_alignment[]：audio_asset_version_id、start_frame、offset_frames、track_ref | 只保存音频版本引用、轨道与对齐点；不保存混音结果，不做响度/效果器渲染。 |
| 转场标记 | transition_markers[]：cut_id、marker_type、start_frame、duration_frames、intent_note | 只保存剪辑意图提示；导出器可映射为工具侧可编辑标记或列入 unsupported_features；平台不渲染转场。 |
| 序列规格 | fps_num、fps_den、width、height、pixel_aspect_ratio、color_space、audio_sample_rate | 每条时间线固定技术参数；目标 Profile 不支持时必须显式报告。 |
| 版本与状态 | version、parent_version_id、status、change_summary、created_by、created_at | 每次调整产生新版本；TimelineDraft 状态为 DRAFT/VALIDATED/SUPERSEDED/STALE/ARCHIVED。 |
| 治理快照 | rights_snapshot_ref、policy_snapshot_ref、validation_report_ref、stale_state | 导出时固定快照；素材、Rights、Policy、时间基准或 Profile 变化触发 STALE。 |

示意数据结构（EXAMPLE_PLACEHOLDER）：timeline_draft_id: td_...；shot_order: [shot_001, shot_002]；duration_per_shot_frames: {shot_001: 72, shot_002: 96}；fps: {num: 24, den: 1}；subtitle_cues: [{cue_id: cue_01, start_frame: 18, end_frame: 64}]；audio_alignment: [{asset_version_id: av_..., start_frame: 0, offset_frames: 0}]；transition_markers: [{cut_id: cut_01, marker_type: DISSOLVE_HINT, start_frame: 72, duration_frames: 12}]；version: 3；parent_version_id: tdv_...；status: DRAFT。

### 33.3 状态机、编辑与 STALE 传播

| **对象** | **权威状态机/进度** | **规则** |
| --- | --- | --- |
| TimelineDraft | DRAFT → VALIDATED；编辑产生新版本；旧版本 → SUPERSEDED/STALE/ARCHIVED | TimelineDraft 不承担外部导入/回流状态；VALIDATED 只代表元数据、引用、时间码及目标 Profile 预检通过。 |
| ExportPackage | DRAFT → VALIDATING → VALIDATED → EXPORTED；异常为 REJECTED/EXPIRED/REVOKED | 每次导出形成不可变包和 ExportManifest；VALIDATED 不代表软件已成功导入。 |
| ToolExchangeSession | 见 32.6 表 32-7（本节不另立状态；权威主链为 REQUESTED→AUTHORIZED→EXPORTED→IN_PROGRESS→IMPORT_PENDING→VALIDATING→COMPLETED，异常态 FAILED/REJECTED/EXPIRED/REVOKED/RECONCILIATION_REQUIRED） | IMPORT/RETURN 是交换会话事件；用户确认或可信插件事件必须有 actor、evidence 和幂等键。 |
| TimelineFlowStatus（UI 投影） | DRAFT → VALIDATED → EXPORTED → IMPORTED → RETURNED | 仅用于进度展示，不是新的权威状态表；IMPORTED/RETURNED 分别来自 ToolExchangeSession 事件。 |

任何外部工具/插件不得因 TimelineFlowStatus=RETURNED 或 ToolExchangeSession=COMPLETED 推导 AssetVersion 已批准、QC PASS、Gate PASS 或 DELIVERED_FINAL。上游 ShotRevision、AssetVersion、Rights、Policy、模板或 Format Profile 变化时，根据真实依赖标记 DIRECT_STALE/INDIRECT_STALE/ADVISORY；旧导出包不自动更新。

### 33.4 时间基准、帧率与时间码

权威时间使用整数帧和有理帧率 fps_num/fps_den；保存 timebase、drop_frame、sequence_start_timecode、sequence_start_frame。音频另保存 sample_rate/sample_start/sample_end。Drop-frame 与 non-drop-frame 不得混用；映射不确定或 Profile 不支持时标记 UNMAPPABLE 并交由用户确认，不得静默四舍五入。源媒体替换、重编码或帧率变化后，旧 clip_in/clip_out 和字幕锚点标记 STALE。

### 33.5 ExportPackage / ExportManifest 契约

ExportPackage 是一次不可变工程交换包；ExportManifest 是该包的内容清单、验证结果和权利/用途快照。工程包不是 DeliveryManifest、不是最终成片，也不具有 Gate4/DELIVERED_FINAL 语义。export_type 仅允许 CREATOR_EXPORT 或 FULL_EXPORT；export_status 采用 DRAFT/VALIDATING/VALIDATED/EXPORTED，异常状态独立记录。

| **对象/字段** | **必需内容** | **不变量** |
| --- | --- | --- |
| ExportPackage | export_package_id、project_id/workspace_id、timeline_draft_ref/version、export_type、format_profile_id、export_status、expires_at | 不可变；修改源时间线/素材/授权后创建新包；不覆盖历史包。 |
| 素材清单 | asset_refs[]、source_asset_version_ids[]、sha256、media_type、duration、source_range、include_mode | 所有素材精确引用 AssetVersion；默认引用/代理，不默认复制原始媒体。 |
| 工程文件 | timeline_exchange_file、native_project_file（如适用）、subtitle_files[SRT/ASS]、audio_track_refs | 时间线交换文件与原生工程文件分开记录；SRT/ASS 编码 UTF-8；音频为轨道引用或获准打包的独立文件。 |
| 技术规格 | fps_num/den、resolution、aspect_ratio、color_space、audio_sample_rate、channel_layout | 必须与目标 Profile 匹配；不兼容项列入 unsupported_features，不得静默丢弃/变换。 |
| 权利与使用 | rights_manifest_ref、rights_snapshot_ref、policy_snapshot_ref、attribution_notice、usage_restrictions、audience_scope | 授权不明、过期、超受众或 CRITICAL 阻断时禁止导出。 |
| 完整性与快速指引 | file_manifest、sha256、package_size、quick_start_guide、rights_notice、usage_limit、support_matrix_ref | 逐文件校验；附 10 秒导入步骤、版权声明和使用限制；不得包含 API Key、CapabilityToken 或隐藏系统提示词。 |

打包策略：REFERENCE_ONLY（仅引用与编排元数据）、PROXY_BUNDLE（包含获准代理素材）、FULL_MEDIA_BUNDLE（经用户显式授权后包含原始媒体）。默认 REFERENCE_ONLY 或 PROXY_BUNDLE；原始文件打包必须重新校验受众、下载权、保存期限、再传播权和第三方授权。

### 33.6 ExternalToolAdapter 能力矩阵与版本验证

每个工具产品/版本/操作系统/部署模式/Format Profile 必须独立登记能力证据。能力矩阵逐项声明 timeline_order、clip_duration、media_relink、subtitle_track、subtitle_style/font、audio_track、audio_effects、transition_marker、color_metadata、native_project、round_trip；逐项的 `support_level` 取值为 VERIFIED/PARTIAL/UNSUPPORTED/UNVERIFIED（这是能力项支持级别，不是 Adapter 生命周期状态；生命周期见 32.3 的 DECLARED—REVOKED 状态集）。工具升级、适配器升级或格式行为变化后必须重新验证。

| **工具/格式 Profile（示例）** | **版本/适配器证据** | **能力状态与限制** | **发布规则** |
| --- | --- | --- | --- |
| 剪映专业版草稿 Profile | 必须填实际产品 build、系统版本、adapter_id/version 和样本工程证据；当前版本未知 | 未完成往返测试前统一标记 UNVERIFIED；重点验证素材 relink、顺序/时长、字幕轨、音轨、画幅/帧率。 | 只允许已验证产品版本启用导出；不得笼统宣称“完整支持剪映”。 |
| Premiere Pro FCPXML/XML Profile | 必须填实际 Premiere build、FCPXML/XML 版本、Adapter 版本和回归证据 | FCPXML/XML 不能等同原生工程；特效、插件、字体、转场和轨道能力逐项验证。 | 不支持项必须写入 unsupported_features，并在导出前告知。 |
| DaVinci Resolve EDL Profile | 必须填实际 Resolve build、EDL 方言/限制、Adapter 版本 | EDL 通常只表达有限时间线信息；字幕样式、复杂转场、Fusion/调色节点需单独声明。 | EDL 不代表可还原完整 Resolve 工程。 |
| DaVinci Resolve Native Project Profile | 必须填实际 Resolve build、工程数据库/交换方式、Adapter 版本 | 原生工程读写、媒体 relink、调色/Fusion、插件兼容全部独立验证。 | 未验证能力不得进入自动路由或销售承诺。 |

### 33.7 TimelinePreviewPlayer 预览边界

| **能力** | **允许行为** | **禁止行为** |
| --- | --- | --- |
| 代理片段播放 | 按 TimelineDraft 顺序切换低清代理片段；显示镜头号、预计时长、字幕 cue 和素材引用。 | 不做帧级拼接、转场合成、字幕烧录、混音、色彩渲染或最终文件生成。 |
| 预览标识 | 全程标注“时间线顺序预览 / 非交付 / 非成片”；允许播放、定位和逐镜头跳转。 | 不得生成可下载 MP4；不得进入 CreatorRelease、ShowcaseSnapshot、DeliveryManifest 或发布素材选择器。 |
| 访问与安全 | 代理继承 Rights/Policy 受众控制；短时 URL、水印/账号标记、访问审计和撤销失效。 | CRITICAL Rights/Privacy/Safety 时不播放；不得提供录制、合成文件导出接口。 |

TimelinePreviewPlayer 可采用 playlist + source switching 播放连续预览，但不得在后台生成拼接媒体文件或可下载缓冲文件；播放片段仅作为交互预览，不能作为声音混合、转场、色彩或最终画质验收证据。

### 33.8 导出前预检与导出流程

- 冻结 TimelineDraft 版本、ShotRevision、AssetVersion、Rights/Policy 快照和目标 Profile；缺少固定版本或引用漂移时拒绝验证。

- 验证素材可读、哈希、代理/原始引用、镜头时长、源时间码、帧率/画幅/色彩空间。

- 验证字幕文件编码、cue 起止帧、字体/样式支持情况、音轨引用/采样率/声道映射；不支持项必须列示。

- 逐素材执行版权/肖像/商标/音乐/字体/模板/隐私/安全检查；CRITICAL 或权利不明时阻断导出。

- 把 TimelineDraft 投影到已验证 Format Profile；输出 unsupported_features 和需要用户确认的差异，不允许静默删轨、换字体或改规格。

- 创建 ExportPackage/ExportManifest、逐文件 sha256、10 秒导入说明、版权声明、使用限制、有效期与审计记录。

### 33.9 外部编辑、回流与重新质检

1. 用户在剪映/PR/达芬奇等外部工具打开工程包，完成专业剪辑、字幕/音频/转场/调色微调与最终渲染；外部工具是专业执行端。

2. 回传工程/成片进入 Staging/Quarantine；校验文件类型、哈希、恶意文件、Rights、素材来源、工具/Adapter/Profile 版本与回流修改范围。

3. 验证通过的工程/成片由 Asset Service 创建新的不可变 AssetVersion；不得覆盖原版本或自动继承 GOLD/APPROVED/QC PASS/Gate PASS。

4. 回流成片重新执行 Rights、安全、适用 QC、人工 Review 和 Gate；FULL_MODE 走工业 Gate/DeliveryManifest；CREATOR_MODE 走 CreatorGate/CreatorDeliveryManifest。

重复上传/回调按 tenant_id + adapter_id/version + external_operation_id + artifact sha256 + idempotency_key 去重；供应商/工具状态未知时先查询和对账，不得盲目重复创建版本或重复计费。

### 33.10 权限、版权、成本与审计

导出前由 Authorization Service 校验租户/组织/项目/资源/受众/下载/商业用途/转授权范围；Token 绑定 action_key、workspace_mode、asset_version、format_profile、adapter_id/version 和有效期。Agent 只能创建 TimelineDraft/ExportPackage DRAFT 和预检请求，不得确认正式 ShotRevision、锁定 Canon/Rights、批准导出例外、发布或将回流资产设为 PASS。

成本记入 CostLedger：FULL_MODE 按既有 PRODUCTION/EXPLORATION 等分类；CREATOR_MODE 按 cost_category=EXPLORATION、cost_subcategory=CREATOR_MODE_COST 归集；不得新建“剪辑预算池”，不得占用 Quality Reserve。记录 operator_principal_ref、workspace_id/mode、timeline_draft_id/version、export_package_id、format_profile_id、adapter/tool 版本、哈希、Rights/Policy 快照、成本快照、Token jti、审批事件和 trace_id。

### 33.11 模板预设与 10 秒导入指引

| **模板预设** | **默认元数据配置** | **可编辑范围/限制** |
| --- | --- | --- |
| 短视频 | 竖屏比例预设、目标帧率、镜头节奏建议、字幕 cue 样式引用、BGM 轨道引用。 | 仅为可编辑 TimelineDraft 起点；不锁定镜头顺序/时长，不渲染转场/字幕/混音。 |
| 短剧 | 横/竖屏规格、对白/环境音轨引用、镜头顺序和对白字幕 cue 建议。 | 允许导演/创作者逐镜头修改；不替代剧本/ShotRevision/Canon 审批。 |
| 口播 | 人物主画面轨、口播音频引用、字幕 cue 对齐、片头/片尾标记。 | 仅生成时间线元数据；不自动去口误、混音、降噪或烧录字幕。 |

每个 ExportPackage 附 10 秒导入指引：①打开目标工具；②选择“导入工程/草稿”；③选择包内工程文件；④如提示媒体缺失，按 Manifest 选择代理包或定位源素材；⑤检查不支持项后由用户自行微调并在外部工具输出最终文件。指引同时附 Rights Notice、Usage Restrictions 和 Profile Support Matrix。

### 33.12 阶段与非目标

阶段以第 31.2 唯一权威表为准：Phase 0 冻结 Schema/SPI/权限/状态机/验收；Phase 1 Timeline UI、Preview、ExportPackage UI 固定 NOT_ENABLED；Phase 2 实现智能编排、代理片段预览、模板预设、剪映草稿/FCPXML/EDL 已验证 Profile 导出；Phase 3 开发剪映/达芬奇插件、签名回调和回流闭环；Phase 4 扩展更多专业工具与模板生态。

全平台（含 CREATOR_MODE）均不建设 NLE 剪辑内核，不执行帧级拼接、转场渲染、字幕烧录、音轨合成/混音、色彩渲染或最终 MP4 导出。任何未来云渲染能力必须另立 ADR、产品范围、预算和版权安全评审，不得通过内部帧渲染合成服务或预览服务偷偷引入。

### 33.13 风险与缓解

| **风险** | **缓解机制** | **验收证据** |
| --- | --- | --- |
| 工程包导入失败/样式丢失 | Profile 版本化、能力矩阵公示、Unsupported Features 明示、样本工程回归。 | 具体产品 build 的打开/导入报告与往返差异清单。 |
| 新手仍觉得导入麻烦 | 剪映草稿优先验证、10 秒引导、三类预设、代理时间线预览。 | 新手可用性测试、首次导入成功率、失败原因。 |
| 插件 API 不稳定 | Phase 2 不押插件；Phase 3 才开发签名插件和回调。 | 签名/撤销/回滚测试、工具升级兼容报告。 |
| 版权/隐私泄露 | 导出 CRITICAL 阻断、短时链接、下载审计、默认 PROXY_BUNDLE/REFERENCE_ONLY。 | 未授权素材导出负向测试、撤销和审计证据。 |
| 回流绕过 QC/Gate | 回流隔离、新 AssetVersion、重跑 Rights/QC/Gate。 | 旧 PASS 不继承、源版本不覆盖、完整审计。 |
| 预览被误认为成片 | 固定 UI 文案、无文件导出 API、不进入发布素材选择器。 | 预览不可下载/发布；UI/API 负向测试。 |

### 33.14 专项验收矩阵

| **测试 ID** | **场景** | **预期结果** |
| --- | --- | --- |
| TL-001 | 智能编排后用户调整 shot_order/duration。 | 创建新 TimelineDraft version 和 parent_version_id；旧版本只读，差异可追溯。 |
| TL-002 | 导出已验证剪映草稿/工程 Profile。 | 素材引用/代理、字幕、音轨和时间码完整；unsupported_features 明示。 |
| TL-003 | 目标工具版本或 Profile 未验证。 | 拒绝标记支持或正式导出；返回 UNVERIFIED/不兼容原因。 |
| TL-004 | 平台尝试自动拼接多段素材输出 MP4。 | 服务端拒绝；不产生合成文件或可下载预览。 |
| TL-005 | 导出包包含 Rights CRITICAL/授权未知素材。 | 导出前阻断，记录 RightsCase、主体、素材、受众与审计事件。 |
| TL-006 | 导出包在外部工具编辑后回流成片。 | 先隔离；扫描/权利/血缘通过后创建新 AssetVersion 并重跑 QC/Gate。 |
| TL-007 | TimelinePreviewPlayer 播放代理时间线。 | 仅按序播放代理片段；不生成 MP4/混音/烧字幕，不可下载/发布/交付。 |
| TL-008 | 源 AssetVersion/Rights/Policy/Profile 变化。 | 依赖链标记 STALE；旧包禁止继续新导出，不自动替换内容。 |
| TL-009 | Creator Mode 导出费用归集。 | CostLedger=EXPLORATION + CREATOR_MODE_COST；Quality Reserve 余额不变。 |
| TL-010 | 重复提交相同 export idempotency_key。 | 返回既有结果，不重复生成包、扣费或创建回流版本。 |
| TL-011 | 导出包缺 10 秒指引/版权声明/使用限制。 | Manifest 校验失败或要求补齐；不能标记 VALIDATED。 |
| TL-012 | 用户应用短视频/短剧/口播预设。 | 仅生成可编辑 TimelineDraft 新版本；不写硬约束、不触发渲染。 |

### 33.15 架构决策记录（ADR-101～105、113～116）

| **ADR** | **决策** | **关键约束** |
| --- | --- | --- |
| ADR-101 | 智能编排是元数据编排，不是剪辑内核。 | 平台管理镜头顺序/时长/字幕 cue/音频对齐/转场标记；不做帧级剪辑、转场渲染、混音或最终成片渲染。 |
| ADR-102 | TimelineDraft 是唯一时间线权威对象。 | EDL/FCPXML/原生草稿是版本化导出投影；帧用整数、有理帧率；来源绑定不可变 AssetVersion。 |
| ADR-103 | ExportPackage/ExportManifest 不等于 DeliveryManifest。 | 工程包仅用于外部编辑，不标 DELIVERED_FINAL；最终渲染回流后重新 QC/Gate。 |
| ADR-104 | ExternalToolAdapter 能力矩阵必须公开、版本化、实测。 | Profile 绑定工具产品/版本/Adapter/证据；不支持项显式提示；升级后回归。 |
| ADR-105 | 平台预览只播放代理片段序列，不输出可下载成片。 | TimelinePreviewPlayer 不拼接、不渲染、不发布；界面标“非交付/仅顺序预览”。 |
| ADR-113～116 | 见 32.13（同一组决策，此处仅作本章索引）。 | 不在本章重复定义，避免同一 ADR 出现两套措辞；引用时以 32.13 为准。 |

## 34. Creator Mode 个人创作者模式（Simplified Creator Workspace）

Creator Mode 面向个人创作者和低门槛用户，采用“UI 降门槛，底层不降级”。L1/L2 隐藏 DAG、复杂分镜编辑和常规审批弹窗；L3—L6 继续复用统一版本、账本、Rights/Safety CRITICAL 阻断、CapabilityToken、STALE、QC 和审计底座。Creator Mode 输出为个人创作素材、预编排工程包，以及经外部工具渲染回流后的个人作品，不是平台自动生成的工业成片。

### 34.1 模式定位与隔离原则

| **维度** | **CREATOR_MODE** | **FULL_MODE（专业工作台）** |
| --- | --- | --- |
| 目标用户 | 个人创作者、短视频/口播/轻量短片用户。 | 导演、制片、TD、QC、版权合规及专业团队。 |
| L1/L2 体验 | 少量输入、默认策略、自动生成草案、进度摘要、一键预览时间线/导出工程包。 | 完整项目/剧本/分镜/约束/DAG/Gate/资源调度控制。 |
| 底层能力 | 复用 AssetVersion、Rights/Safety、Budget/CostLedger、Token、QC、STALE。 | 同一底座，使用正式生产策略与工业 Gate0—4。 |
| 权威对象 | CreatorWorkspace、CREATOR_DEFAULT_POLICY、CreatorGate、CreatorPublicationCheck、CreatorDeliveryManifest、CreatorRelease。 | Project/Workspace、ShotRevision、Canon、ConstraintPack、QCReport、GateDecision、DeliveryManifest。 |
| 发布/交付 | 个人作品标 CREATOR_RELEASE；不产生 DELIVERED_FINAL。 | 只有 Gate4 PASS + 有效 DeliveryManifest/DeliveryPackage 才能标 DELIVERED_FINAL。 |
| 统计口径 | 独立 Creator metrics，不计入工业 Gate4 通过率和正式交付北极星指标。 | 计入工业生产、Gate4、交付和质量护栏指标。 |

workspace_mode 在 Workspace 创建时固定为 CREATOR_MODE 或 FULL_MODE；创建后不可直接改写。升级为专业工作台必须创建有 parent/derived_from 关系的 FULL_MODE successor workspace，不能在同一个工作区内原地切换模式。

CreatorWorkspace 字段契约：

| **对象** | **字段** | **类型** | **必填** | **说明** |
|---|---|---|---|---|
| CreatorWorkspace | `workspace_id` | string | 是 | 工作区标识 |
| CreatorWorkspace | `workspace_mode` | enum | 是 | CREATOR_MODE / FULL_MODE；创建后不可改写，升级须新建 successor |
| CreatorWorkspace | `tenant_id` | string | 是 | 租户；禁止跨租户创建 |
| CreatorWorkspace | `organization_id` | string | 否 | 组织；个人创作者可为空 |
| CreatorWorkspace | `owner_principal_id` | string | 是 | 所有者主体；发布权唯一归属此人 |
| CreatorWorkspace | `parent_workspace_id` | string | 否 | 升级 successor 时必填，指向原工作区 |
| CreatorWorkspace | `derived_from_workspace_id` | string | 否 | 派生来源，与 parent 二选一 |
| CreatorWorkspace | `policy_snapshot_ref` | string | 是 | CREATOR_DEFAULT_POLICY 版本快照引用 |
| CreatorWorkspace | `quota_scope` | string | 是 | 配额与预算隔离域；Creator 成本不计入 Quality Reserve |
| CreatorWorkspace | `status` | enum | 是 | ACTIVE / SUSPENDED / ARCHIVED |
| CreatorWorkspace | `created_at` | datetime | 是 | 创建时间 |

CreatorWorkspace 权威状态机：ACTIVE → SUSPENDED → ARCHIVED。ARCHIVED 为终态，不得恢复；升级为 FULL_MODE 不是状态迁移，而是新建 successor 工作区，旧工作区只标记 ARCHIVED 并保留派生关系。

### 34.2 极简输入、过程透明与结果页

| **用户界面** | **最小字段/行为** | **隐藏但必须执行的后台逻辑** |
| --- | --- | --- |
| 输入表单 | 创意描述；参考图片/素材（可选）；风格参考（可选）；目标时长；画面比例。 | 素材 Rights/Privacy/Safety 检查；创建 IntentContract DRAFT；绑定 CREATOR_DEFAULT_POLICY 版本。 |
| 过程反馈 | 创意理解 → 分镜规划 → 素材生成 → 时间线编排 → 工程包准备。 | 真实 DAG/Task/Worker/模型路由/预算/QC/CreatorGate/异常恢复照常执行；不展示拓扑编辑器。 |
| 成功结果 | 展示镜头素材、顺序/时长摘要、时间线预览、“一键导出剪映草稿/工程包”和“升级为专业工作台”。 | 资产版本、Prompt/模型调用、成本、Rights、QC、TimelineDraft 和 ExportPackage 全程可追溯。 |
| 异常/阻断 | 用普通用户能理解的原因和下一步操作说明，不展示复杂规则细节。 | CRITICAL 版权/安全/隐私必须阻断/隔离；不隐藏、不自动绕过、不允许 Agent 解除。 |

结果页禁止使用“一键成片”或展示平台最终 MP4 作为成果。平台只提供单镜头素材预览、时间线顺序预览和外部工程包；用户需要最终成片时必须在外部剪辑工具完成并回流。

### 34.3 CREATOR_DEFAULT_POLICY 策略包

| **字段** | **规范** | **权威边界** |
| --- | --- | --- |
| policy_id/version | CREATOR_DEFAULT_POLICY + semver + content_hash | 策略不可变；更新生成新版本；任务/CreatorGate/CreatorPublicationCheck 固定记录策略版本。 |
| input_contract | creative_description、reference_asset_ids、style_reference_ref、target_duration、aspect_ratio | 只暴露必要字段；参考素材上传先过 Rights/Safety。 |
| generation_trigger | USER_ONLY；一次用户显式点击/指令对应一次生成请求 | 禁止 Agent 自动连发、循环优化、批量抽卡；用户明确发起重试才创建新任务并计入额度。 |
| generation_limits | 单任务预算上限、候选数、超时、重试次数、存储/代理限制 | 预算不足时暂停并向用户解释；不静默降质、不暗中追加调用。 |
| governance_rules | CRITICAL_BLOCK、rights_required、privacy_required、token_scope、audit_required、stale_rules | 用户/Agent 不得关闭；策略变更须版本化并由治理职责批准。 |
| output_contract | IntentContract/ShotSpec DRAFT、TimelineDraft、proxy_preview_refs、ExportPackage/ExportManifest | 无外部渲染回流文件时不得创建 CreatorDeliveryManifest/CreatorRelease。 |

Creator Mode 默认自动加载策略包。普通创作者不填写复杂 QC、模型、预算、DAG 或 Gate 参数；可调整的视觉/模板/时长参数必须处于策略白名单，不能改变版权、安全、隐私、资源限额和账本隔离红线。

### 34.4 Creator Mode 端到端工作流

| **步骤** | **后台对象/服务** | **用户可见体验** | **阻断与边界** |
| --- | --- | --- | --- |
| 1. 创建工作区 | CreatorWorkspace(workspace_mode=CREATOR_MODE)、Policy Snapshot | 输入创意和可选参考。 | 账号/租户/组织配额校验；workspace_mode 不可变。 |
| 2. 解析意图 | IntentContract DRAFT、reference rights/safety check | 显示理解后的主题/风格/时长概要。 | 不认定上传者天然拥有素材权利；不写 Canon。 |
| 3. 拆镜与生成 | ShotSpec/ShotVisualPlan DRAFT、ConstraintPack（如适用）、Task/DAG、AssetVersion | 显示剧本/分镜/素材生成进度。 | CRITICAL 权利/安全/隐私失败即阻断；不因隐藏 UI 放宽底层校验。 |
| 4. 智能编排 | TimelineDraft、subtitle cues、audio alignment、transition markers | 显示顺序和时长摘要。 | 只做元数据编排，不剪辑、不渲染、不混音。 |
| 5. 预览与导出 | TimelinePreviewPlayer、ExportPackage/ExportManifest | 预览代理片段；导出已验证工程 Profile。 | 预览不生成文件；CRITICAL 失败不得导出。 |
| 6. 外部编辑 | 剪映/PR/达芬奇等外部工具 | 提示用户打开工程、微调并在外部工具渲染。 | 外部工具结果不能直接写平台 Gate/PASS。 |
| 7. 回流与个人作品 | ExternalToolArtifact、新 AssetVersion、CreatorGate、CreatorDeliveryManifest | 回流后显示“个人创作 · 创作者发布”候选。 | 必须新建 AssetVersion 并重跑 Rights/CreatorGate；不产生工业 DeliveryManifest。 |

### 34.5 CREATOR_GATE 与工业 Gate0—4 隔离

| **维度** | **CREATOR_GATE** | **工业 Gate0—4** |
| --- | --- | --- |
| 目的 | 个人模式的权利、安全、隐私、滥用风险与基础资产可用性评估。 | 正式工业生产的项目准入、剧本锁定、生产准备、质量终检与交付决策。 |
| 状态 | PASS / BLOCKED / NEEDS_USER_INPUT；评估由受信 Policy Worker 生成。 | 遵循正式 Gate 状态机与授权人类审批。 |
| 用户体验 | 成功时不弹复杂审批窗；失败时给出可理解的阻断原因与补充动作。 | 按职责展示评审证据、审批任务和返工流程。 |
| CRITICAL | 版权、安全、隐私、恶意内容/文件、未授权素材必须阻断；无法判断时 NEEDS_USER_INPUT/HELD。 | 按既有 Rights/Safety/QC/Gate 规则阻断。 |
| 权威边界 | CreatorGate PASS 只表示 Creator 策略检查通过，不等于发布批准，不等于 Gate4。 | Gate4 PASS + DeliveryManifest 才能形成工业 DELIVERED_FINAL。 |
| 统计 | 写入 CreatorGateMetrics；不计入工业 Gate4 通过率。 | 写入 IndustrialGateMetrics 和正式交付 KPI。 |

CreatorGate 字段契约：

| **对象** | **字段** | **类型** | **必填** | **说明** |
|---|---|---|---|---|
| CreatorGate | `creator_gate_id` | string | 是 | 评估记录标识 |
| CreatorGate | `creator_workspace_id` | string | 是 | 所属 Creator 工作区 |
| CreatorGate | `creator_asset_version_id` | string | 是 | 被评估的回流 AssetVersion；不存在回流文件不得评估 |
| CreatorGate | `policy_snapshot_ref` | string | 是 | 评估时固定的策略版本，不得事后改写 |
| CreatorGate | `decision` | enum | 是 | PASS / BLOCKED / NEEDS_USER_INPUT；只表示 Creator 策略结论，不授予发布权、不等于 Gate4 |
| CreatorGate | `reason_codes` | string[] | 否 | 阻断或补充原因码，面向用户解释用 |
| CreatorGate | `rights_snapshot_ref` | string | 是 | 权利快照引用 |
| CreatorGate | `safety_report_ref` | string | 否 | 安全检查报告引用 |
| CreatorGate | `evaluated_by` | string | 是 | 受信 Creator Policy Worker 标识，不得为 Agent 自评 |
| CreatorGate | `evaluated_at` | datetime | 是 | 评估时间 |

CreatorGate 权威结论状态机：PENDING → PASS / BLOCKED / NEEDS_USER_INPUT。三者均为终态结论，不得原地改写；条件变化须新建评估记录并保留前次结论。该结论只属 Creator 策略层，不进入工业 Gate 状态机，也不改变任何工业 Gate 结果。

### 34.6 四类账本、CREATOR_MODE_COST 与成本隔离

| **成本口径** | **Creator Mode 规则** | **禁止事项** |
| --- | --- | --- |
| CostLedger | cost_category=EXPLORATION；cost_subcategory=CREATOR_MODE_COST；绑定 workspace/task/asset/policy/price_version。 | 不得作为第五类预算账户；不得与 PRODUCTION/QUALITY_REPAIR/CONTINGENCY 混算。 |
| BudgetLedger/QuotaReservation | 沿用既有预算与配额；用户可见预计额度/本次消耗；超限停止、排队或提示明确选择。 | 不得隐藏资源差距或绕过用户/组织配额；未知供应商结果先查询再重试。 |
| Quality Reserve | Creator Mode 不以 Quality Reserve 作为探索资金来源；质量储备受保护。 | 任何 Creator Mode 成本不得扣减 Quality Reserve，不得内部转拨伪装来源。 |
| BYOK | USER_OWN_KEY 仍由用户向供应商结算；平台只记录用量和估算成本。 | 不得因平台余额为零拦截 BYOK；不得静默回退 PLATFORM_PAID。 |

### 34.7 CreatorDeliveryManifest 与 CREATOR_RELEASE 定义

CreatorDeliveryManifest 仅在外部工具已渲染并回传最终个人作品文件、平台创建了新的 AssetVersion 且 Rights/CreatorGate 通过后才能生成。若只有素材、TimelineDraft、Preview 或 ExportPackage，没有外部渲染回流文件，则不得创建 CreatorDeliveryManifest/CREATOR_RELEASE。

| **对象/字段** | **规范** | **与工业交付的区别** |
| --- | --- | --- |
| CreatorDeliveryManifest | manifest_type=CREATOR_DELIVERY；creator_delivery_manifest_id；creator_workspace_id；source_export_package_id；timeline_draft_ref/version；render_asset_version_id。 | 必须引用外部渲染回流的新 AssetVersion；不是工业 DeliveryManifest，不声明 Gate4 PASS。 |
| 内容类型 | content_type=CREATOR_RELEASE；页面固定显示“个人创作 · 创作者发布”。 | 不得显示 DELIVERED_FINAL，不计入正式工业交付数量。 |
| 门禁引用 | creator_gate_ref、creator_publication_check_ref（发布时）、rights_snapshot_ref、safety_report_ref、policy_snapshot_ref。 | CreatorGate 不替代公开发布资格检查；CREATOR_PUBLICATION_CHECK 独立评估发布资格。 |
| 工具/血缘 | external_tool_product/version、adapter_id/version、format_profile_id、operator、source_asset_version_ids、sha256、change_scope。 | 必须追踪导出包、素材、操作人、工具/Adapter 版本和修改范围。 |
| CreatorDeliveryManifest 状态 | DRAFT → VALIDATING → VALIDATED；旁路治理态 STALE / REVOKED / SUPERSEDED。 | 不包含 PUBLISHED；发布状态属于 CreatorRelease，不得混写。 |

### 34.8 分享、社区发布与公开权限

默认 PRIVATE。分享优先复用 ShareLink 与 ShowcaseSnapshot：受众、有效期、水印、下载权限和快照范围必须明确；默认禁止下载。CREATOR_RELEASE 是内容类型，不自动获得公开传播权。上传素材、音乐/声音、字体、模板、肖像/商标和生成内容的适用权利必须明确；权利不明不得公开。

| **对象** | **独立状态机** | **规范规则** |
| --- | --- | --- |
| CreatorPublicationCheck | PENDING → PASSED / BLOCKED / NEEDS_HUMAN_REVIEW | 由受信策略 Worker 自动执行适用 PG0—PG3 自动检查、Rights/Privacy/Safety/平台规则；只产生证据和结论，不自行发布。 |
| CreatorRelease | DRAFT → PUBLISH_REQUESTED → AUTO_CHECKING → PUBLISHED / HELD_FOR_REVIEW / BLOCKED；发布后可 UNLISTED / TAKEN_DOWN / REVOKED / ARCHIVED | 发布必须由 CreatorWorkspace 所有者/权利主体本人显式点击；低风险且权利明确、自动检查 PASSED 时可直接发布，不要求前置人工内容审核；有不确定/敏感/第三方权利风险时转 HELD_FOR_REVIEW，不得公开。 |
| CreatorShareLink | ACTIVE → EXPIRED / REVOKED | 只读、默认禁止下载；水印、有效期、受众限制和访问审计。 |

Creator 专属发布通道只适用于 CREATOR_MODE 且满足自动化低风险条件的个人作品。一般 FULL_MODE/工业作品仍遵循社区 PublicationReview 与 PG0—PG3 的既定人工审核规则。Agent/Skill/Plugin 可以准备发布草案、解释发布资格和提示用户，但绝对不得调用 CREATOR.RELEASE.PUBLISH，不得代替用户点击发布。每次用户发布动作必须绑定 actor_principal、user_intent_id、CapabilityToken jti、CreatorPublicationCheck 结果和审计事件。

公开发布后的举报、下架、撤销、申诉和限流继续复用社区治理能力。单位时间发布上限与最小间隔属于反滥用软限流；触发时提示“发布过于频繁，请稍后再试”，不得把频控误称为人工内容审核。

阶段边界：CreatorRelease 及其发布动作（CREATOR.PUBLICATION.CHECK、CREATOR.RELEASE.PUBLISH）整体属 Phase 3，PRIVATE / ORGANIZATION / PUBLIC 三种受众一律不提前启用；Phase 2 不创建 CreatorRelease，Creator 作品的组织内展示走 CommunityPost + ShowcaseSnapshot + PublicationReview，因此 `publication_check_ref` 在 Phase 2 无对象可填。

Phase 2 组织内展示的引用对象也已明确：ShowcaseSnapshot 通过 `display_asset_refs` 引用 CreatorDeliveryManifest 产出的 AssetVersion，通过 `delivery_package_ref` 引用其导出产物；CreatorDeliveryManifest 只作证据引用，不得经 `source_gate_state` 冒充工业 Gate4 PASS，快照内容不得以 manifest 对象版本为来源。

CreatorRelease 低风险判定与抽检：低风险自动发布须同时满足权利明确、无第三方肖像/音乐/商标未授权、无敏感安全内容、内容类型与模板预设一致；满足后由自动检查 PASSED 直接发布，不设前置人工内容审核。平台对自动发布作品按比例抽检，覆盖权利、隐私、安全与标签一致性；命中不确定或风险项转 HELD_FOR_REVIEW/BLOCKED。作者或权利主体对下架、阻断决定可申诉，由不同于原处理人的复核者处理；申诉 SLA 与社区治理一致，申诉提交不自动恢复展示，恢复须单独命令并审计。反滥用软限流按单位时间发布上限与最小间隔执行，触发时仅提示稍后重试，不得将频控记为人工内容审核结论。

CreatorPublicationCheck 与 CreatorRelease 字段契约：

| **对象** | **字段** | **类型** | **必填** | **说明** |
|---|---|---|---|---|
| CreatorPublicationCheck | `publication_check_id` | string | 是 | 检查记录标识 |
| CreatorPublicationCheck | `creator_release_id` | string | 是 | 被检查的发布候选 |
| CreatorPublicationCheck | `audience_scope` | enum | 是 | PRIVATE / ORGANIZATION / PUBLIC；Phase 2 仅前两者 |
| CreatorPublicationCheck | `pg_rules_applied` | string[] | 是 | 适用的 PG0—PG3 规则子集，须可追溯到规则版本 |
| CreatorPublicationCheck | `decision` | enum | 是 | PENDING / PASSED / BLOCKED / NEEDS_HUMAN_REVIEW；只产出证据与结论，不自行发布 |
| CreatorPublicationCheck | `rights_snapshot_ref` | string | 是 | 权利快照；权利不明不得 PASSED |
| CreatorPublicationCheck | `privacy_report_ref` | string | 否 | 隐私检查报告 |
| CreatorPublicationCheck | `safety_report_ref` | string | 否 | 安全检查报告 |
| CreatorPublicationCheck | `policy_version` | string | 是 | 执行时的策略版本 |
| CreatorPublicationCheck | `evidence_refs` | string[] | 是 | 证据引用，缺失即视为检查不完整 |
| CreatorPublicationCheck | `checked_at` | datetime | 是 | 检查时间 |
| CreatorRelease | `creator_release_id` | string | 是 | 发布候选标识 |
| CreatorRelease | `creator_delivery_manifest_id` | string | 是 | 绑定的 CreatorDeliveryManifest；不得引用工业 DeliveryManifest |
| CreatorRelease | `creator_workspace_id` | string | 是 | 所属工作区 |
| CreatorRelease | `content_type` | enum | 是 | 固定 CREATOR_RELEASE；不产生 DELIVERED_FINAL |
| CreatorRelease | `audience_scope` | enum | 是 | PRIVATE / ORGANIZATION / PUBLIC；PUBLIC 仅 Phase 3 开放 |
| CreatorRelease | `publication_check_ref` | string | 是 | 发布资格检查引用；未 PASSED 不得发布 |
| CreatorRelease | `user_intent_id` | string | 是 | 所有者本人显式点击发布的意图标识；Agent 不得代填 |
| CreatorRelease | `capability_token_jti` | string | 是 | 短时令牌 jti，与发布动作一一绑定 |
| CreatorRelease | `status` | enum | 是 | DRAFT / PUBLISH_REQUESTED / AUTO_CHECKING / PUBLISHED / HELD_FOR_REVIEW / BLOCKED / UNLISTED / TAKEN_DOWN / REVOKED / ARCHIVED |
| CreatorRelease | `published_at` | datetime | 否 | 实际发布时间；未发布为空 |
| CreatorRelease | `revoke_reason` | string | 否 | 撤销或下架时的原因、依据与范围；撤销动作必填 |

### 34.9 升级为 FULL_MODE：创建后继工作区，不转正旧结果

| **步骤** | **执行规则** | **禁止的捷径** |
| --- | --- | --- |
| 升级申请 | CreatorWorkspace 所有者/授权管理员发起 CREATOR.MODE.UPGRADE；明确目标工作区、成员、预算、受众与用途。 | Agent 不得自发升级，不得原地修改 workspace_mode。 |
| 创建 successor | 创建新的 FULL_MODE Workspace/Project；保存 parent_creator_workspace_id、derived_from refs 和迁移操作者。 | 不复用 Creator Workspace 权威状态容器，不改变旧 Creator 对象。 |
| 旧资产迁入 | 按权利与用户授权，将 Creator AssetVersion/TimelineDraft/ExportPackage 作为来源引用或导入副本，保留 source version/hash。 | 不把 CreatorGate PASS 转成工业 QC PASS；不继承 Gate0—4、Canon 锁定或 DELIVERED_FINAL。 |
| 建立专业事实 | 以素材 AssetVersion、TimelineDraft、ShotSpec 草案为起点；按需补做 Canon/正式 ShotRevision 人工确认；无锁定约束标 NOT_APPLICABLE。 | 不把 AI 草案或外部工程直接标为锁定事实。 |
| 重新验收 | 只有在需要工业交付时，才按 FULL_MODE 正式 DAG、Rights、QC、人工 Gate0—4 和 DeliveryManifest 执行；也可先停留在专业精修草案。 | 不提供“升级即正式交付”的快捷路径。 |

### 34.10 版本不可变、STALE 与撤销传播

IntentContract、ShotSpec 草案、TimelineDraft、AssetVersion、ExportPackage、CreatorGate、CreatorPublicationCheck、CreatorDeliveryManifest 和 CreatorRelease 均版本化或追加式记录。每次重新生成/编辑创建新版本，不覆盖已分享或已审查版本。源资产、Rights、Policy、模型/Adapter/Profile/模板/时间线变化时，按真实依赖传播 DIRECT_STALE/INDIRECT_STALE/ADVISORY。

Rights 撤销、隐私投诉或安全事件触发访问冻结、CreatorRelease 撤销/下架、导出链接吊销和既有引用影响清单生成。不得通过生成新 CreatorRelease 默默恢复旧权限；恢复前必须重新验证 Rights/Safety 和适用发布资格，恢复动作单独审计。

### 34.11 Agent、CapabilityToken 与角色分离

| **Agent 可做** | **必须由人类/受信服务完成** | **绝对禁止** |
| --- | --- | --- |
| 解析创意，生成 IntentContract/ShotSpec/TimelineDraft DRAFT；提出模板建议；执行已授权素材预检；请求 CreatorGate/CreatorPublicationCheck；创建待发布草案。 | Authorization Service 签发短时 CapabilityToken；CreatorGate/PublicationCheck Worker 生成评估证据；用户确认权利；CreatorWorkspace 所有者显式发起发布/升级；Rights/Safety Worker 执行撤销。 | Agent 自批 CreatorGate/CreatorPublicationCheck PASS、代发、修改 Canon/Rights、关闭 CRITICAL、伪造导入确认、占用 Quality Reserve、把 CreatorRelease 改为 DELIVERED_FINAL。 |
| 在授权范围内执行生成、编排、预览和工程包任务。 | 平台服务验证租户/组织/工作区 scope、预算、配额、策略版本、发布资格、Token 与审计。 | 通过隐藏 UI、脚本、插件回调或直接 DB 写绕过 Action Registry/Authorization Service。 |

### 34.12 阶段路线与启用状态

- Phase 0：冻结 workspace_mode、CREATOR_DEFAULT_POLICY Schema、CreatorGate/CreatorPublicationCheck、CreatorDeliveryManifest、CREATOR_RELEASE、TimelineDraft/ExportPackage/ExportManifest、动作权限、账本分类、审计事件、CRITICAL 阻断与升级迁移验收用例；不开发 UI。

- Phase 1：CREATOR_MODE 固定 NOT_ENABLED；不做 Quick UI、TimelinePreviewPlayer、工程包导出或 Creator Release；优先打稳工业核心生产硬门。

- Phase 2：实现极简 UI、默认策略、智能编排、代理片段顺序预览、短视频/短剧/口播预设、已验证 Profile 的剪映草稿/FCPXML/EDL 工程包导出、一键升级 FULL_MODE；插件不作为 Phase 2 前置。组织内分享按既有边界开放。

- Phase 3：开发剪映/达芬奇插件、签名回传与完整工程/成片回流；对符合低风险资格的 Creator Release 开放“用户显式发起 + 自动化发布检查 + Agent 禁止代发”的公开发布通道。

- Phase 4：完善个人模板库、更多工具适配、模板市场与创作者生态；商业化、收益分成和再授权另行完成法律/版权/资金评审。

以上阶段只细化第 31.2 唯一权威阶段表，不构成第二套阶段定义。Creator Mode 的 UI 极简不改变 L3—L6 的质量、版权、安全、账本、权限和审计底线。

### 34.13 Creator Mode 指标隔离

| **指标** | **计算口径** | **统计边界** |
| --- | --- | --- |
| creator_workspace_activation | 完成 CreatorWorkspace 创建且首次成功生成素材的工作区数。 | 单独统计，不与工业项目数混计。 |
| timeline_export_success_rate | 成功导出的 ExportPackage 数 / 完成验证的导出请求数。 | 按 format_profile_id、工具版本和 adapter_version 分层。 |
| creator_gate_block_rate | BLOCKED/NEEDS_USER_INPUT 的 CreatorGate 评估数 / 总评估数。 | 按版权、安全、隐私分类；不以降低阻断率为优化目标。 |
| creator_publication_check_rate | PASSED/BLOCKED/NEEDS_HUMAN_REVIEW 数量及原因分布。 | 区分自动通过、风险升级和明确阻断；不将自动通过等同内容审核人工通过。 |
| creator_release_count | 有效 CreatorDeliveryManifest 对应的已发布 CREATOR_RELEASE 数。 | 不计入 Gate4、DELIVERED_FINAL 和工业交付北极星指标。 |
| creator_mode_cost | CREATOR_MODE_COST 子类别成本及每工作区/每分钟内容估算成本。 | 区分 BYOK 估算与可信账单；不占 Quality Reserve。 |
| upgrade_to_full_rate | 成功创建 FULL_MODE successor workspace 的 Creator 工作区比例。 | 不能作为诱导用户接受未经确认迁移的目标。 |

### 34.14 Creator Mode 专项验收矩阵

| **测试 ID** | **场景** | **预期结果** |
| --- | --- | --- |
| CR-001 | Phase 1 用户访问 Creator Mode。 | 接口/前端返回 NOT_ENABLED；无 Creator Workspace、账本或发布副作用。 |
| CR-002 | Phase 2 创建 CREATOR_MODE 工作区。 | workspace_mode 不可变；加载固定版本 CREATOR_DEFAULT_POLICY。 |
| CR-003 | 只输入创意描述和一张获准参考图。 | 自动生成 IntentContract/ShotSpec/TimelineDraft 草案；无需填写复杂 DAG/Gate。 |
| CR-004 | 参考素材存在版权不明、隐私或安全 CRITICAL。 | 阻断生成/导出/发布；向用户显示可执行原因；Agent 不可关闭。 |
| CR-005 | Agent 尝试批准 CreatorGate/PublicationCheck、修改 Canon 或发布。 | 服务端拒绝并写审计事件；无权威状态副作用。 |
| CR-006 | Creator Mode 发生生成/预览/导出费用。 | CostLedger=EXPLORATION + CREATOR_MODE_COST；Quality Reserve 余额不变。 |
| CR-007 | 平台内预览尝试生成可下载 MP4。 | 请求被拒绝；TimelinePreviewPlayer 只按顺序播放代理片段。 |
| CR-008 | 仅有 ExportPackage，未有外部渲染回流。 | 不得创建 CreatorDeliveryManifest 或 CREATOR_RELEASE。 |
| CR-009 | 外部渲染回流创建新 AssetVersion 且 CreatorGate/Rights 通过。 | 可以创建 CREATOR_DELIVERY 候选；不产生工业 DeliveryManifest。 |
| CR-010 | 低风险作品由创作者本人点击公开发布，自动检查 PASSED。 | 允许进入 CreatorRelease PUBLISHED；记录 user_intent_id/Token/检查证据；不要求前置人工审核。 |
| CR-011 | Agent 代替创作者调用 CREATOR.RELEASE.PUBLISH。 | 拒绝；无用户显式意图不发布。 |
| CR-012 | CreatorPublicationCheck 为 BLOCKED 或 NEEDS_HUMAN_REVIEW。 | 不得公开；BLOCKED 保持阻断，NEEDS_HUMAN_REVIEW 进入人工处理队列。 |
| CR-013 | CreatorRelease 尝试标记 DELIVERED_FINAL。 | 拒绝；不写工业 Gate4/DeliveryManifest 或正式交付指标。 |
| CR-014 | Creator Mode 点击升级 FULL_MODE。 | 创建 successor workspace；旧结果以 DRAFT/来源引用导入，不继承 Gate4/正式 QC。 |
| CR-015 | 源 Rights 撤销或 TimelineDraft 上游变化。 | 传播 STALE/REVOKED，吊销链接并阻止继续导出/发布。 |
| CR-016 | Creator Mode 指标统计汇总。 | CreatorGate/CreatorPublicationCheck/CreatorRelease/CreatorModeCost 与工业 KPI 分开。 |

### 34.15 架构决策记录（ADR-106～112、117～118）

| **ADR** | **决策** | **关键约束** |
| --- | --- | --- |
| ADR-106 | Creator Mode 采用“UI 降门槛，底层不降级”。 | L1/L2 可隐藏复杂 UI；L3—L6 的资产、Rights/Safety、权限、账本、Token、STALE 和审计不放宽。 |
| ADR-107 | CREATOR_GATE 与工业 Gate0—4 独立。 | CreatorGate 不计入 Gate4 通过率，不批准发布，不生成 DELIVERED_FINAL。 |
| ADR-108 | 个人模式成本以 EXPLORATION + CREATOR_MODE_COST 子分类归集。 | Quality Reserve 不得使用；BYOK 不受平台余额拦截。 |
| ADR-109 | 个人作品使用 CREATOR_DELIVERY / CREATOR_RELEASE。 | 必须有外部渲染回流、新 AssetVersion、CreatorGate 与 Rights 证据；与工业 DeliveryManifest 隔离。 |
| ADR-110 | workspace_mode 创建后不可变；升级创建 FULL_MODE 后继工作区。 | 旧个人作品成为待复核草案/引用，不继承正式审批、Canon、Gate4 或交付标记。 |
| ADR-111 | Phase 0 冻结 Creator 契约，Phase 1 NOT_ENABLED，Phase 2 实现极简 UI。 | Phase 2 提供 TimelinePreview 和已验证工程包导出；Phase 3 再做插件回流。 |
| ADR-112 | Creator Release 采用独立的发布资格检查。 | 发布权来自用户显式操作与 CapabilityToken，不来自 CreatorGate PASS。 |
| ADR-117 | Creator Release 公开发布采用“用户发起 + 自动检查 + 风险分流”。 | 低风险且 Rights 明确的个人作品可自动检查通过后发布；不确定/敏感/第三方权利问题进入人工复核或阻断；Agent 绝不代发。 |
| ADR-118 | CreatorDeliveryManifest、CreatorPublicationCheck、CreatorRelease 使用独立状态机。 | 交付候选、发布资格和实际发布状态不得混写；撤销与申诉保留证据并可追溯。 |

## 附录 A：统一术语表

| **标准术语** | **中文名称** | **定义/使用边界** |
| --- | --- | --- |
| Creative Autonomy | 创意自主权 | AI 在指定任务/镜头内可提案、生成和修改的权限等级 L1-L4；不包含审批/发布权 |
| Exploration Profile | 探索资源档位 | COMPACT/STANDARD/EXPANDED，描述探索算力/候选/模型数量，与自主权分离 |
| Quality Reserve | Quality Reserve（质量储备） | 仅用于 QC 缺陷修复、必要重生成、质量补偿和最终质量保障；不得支付常规创意探索。 |
| Quality Profile | 质量配置 | 针对项目类型/平台的验收配置，须版本化并经治理审批 |
| Benchmark | 质量基准集 | 具有来源、授权、标注、版本和适用范围的参考样本集合 |
| Intent Contract | 导演意图契约 | 将自然语言创作意图、推断、假设和审批状态结构化的版本化对象 |
| Creative Proposal | 创意提案 | 可比较、可回溯的表达路径方案 |
| CEE | 创意探索引擎 | Creative Exploration Engine，负责分支探索、比较和停止 |
| DIE | 导演意图理解引擎 | Director Intent Engine，负责意图解析和结构化 |
| PMS | 生产记忆系统 | Production Memory System，负责跨项目范围内授权的事实/经验记忆 |
| RCA | 根因分析 | Root Cause Analysis，基于证据定位生产问题并验证纠正措施 |
| STALE | 依赖失效 | 输入版本变化后，下游对象需要重评估的状态；分 DIRECT/INDIRECT/ADVISORY |
| Outbox | 事务发件箱 | 与业务写入同事务保存的事件记录，用于可靠异步发布 |
| Capability Token | 能力令牌 | Authorization Service 为单个 Task 签发的短时、最小权限令牌 |
| Override Rate | 人工例外率 | 获批 Override 镜头数/完成 QC 送审镜头数 |
| Quality Escape | 质量逃逸 | 问题在交付后才被发现 |
| Pattern Library | 创作经验库 | 非强制的创作方法、适用条件、证据和失败经验库 |
| Temporal Execution State | 工作流执行态 | Durable Workflow Runtime 中的活动、计时器、重试和恢复状态 |
| Project Business State | 项目业务态 | Project DB 中的 WorkflowRun、Task、Gate、资产和审批最终业务状态 |
| CharacterCard | 角色制作参考卡 | 角色外观、表演参考及引用资产集合；不等于 Canon。 |
| CharacterCanon | 角色权威设定 | 经授权确认的角色事实、时间线、关系与连续性权威记录。 |
| CharacterAppearance | 角色外观版本 | 按时间、场景、服装或状态区分的可版本化外观记录。 |
| CharacterVoiceProfile | 角色声音档案 | 角色音色/语音特征、来源和使用许可的版本化资产。 |
| CharacterReferenceSet | 角色参考集 | 三视图、表情、姿态、材质/服装参考的有来源集合。 |
| ShotVisualPlan | 镜头视觉计划 | 结构化表达导演意图、专业镜头语言、锁定字段和开放创意字段。 |
| SkillManifest | Skill 契约清单 | 声明 Skill 输入/输出、能力权限、预算作用域、审批点、幂等和错误码的版本化契约。 |
| PatternRecord | Pattern 记录 | 包含适用边界、证据、来源权利、版本和失败案例的经验资产。 |
| ExplorationBranch | 创意探索分支 | 与正式轨道隔离、独立预算和状态的候选试验分支。 |
| QCReviewSession | 质量复核会话 | 围绕固定资产版本和 QC 证据组织的人机审核及裁决记录。 |
| QuotaReservation | 资源配额预留 | 对并发、GPU、供应商额度或存储配额进行原子预留的账务对象。 |
| BudgetLedgerTransaction | 预算账本交易 | 对预算账户进行 Reserve/Commit/Release/Refund/Transfer 的追加式不可变记录。 |
| ExplorationReserve | 创意探索储备 | 仅用于获批的创意探索分支和候选试验，不承担 QC 缺陷修复。 |
| BudgetLedger | 预算账本 | 记录预算账户额度、预留、承诺、释放、退款和调拨的权威账本。 |
| CostLedger | 成本账本 | 记录实际发生的模型、算力、存储、人力、供应商账单和返工成本。 |
| BudgetTransferRequest | 预算转拨申请 | 跨预算账户转拨时的申请、审批、原子执行和审计对象。 |
| Formal Track | 正式生产轨道 | 受正式 ShotRevision、Gate 和质量策略约束的生产路径。 |
| Exploration Branch | 探索分支 | 不得直接改写正式生产状态，须经 Promote/Reject 决策。 |
| ExternalToolAdapter | 外部工具统一适配器 | L3 统一接入抽象；负责外部工具能力声明、连接、文件交换、元数据同步和事件验证，不拥有平台权威业务状态。 |
| EditingToolAdapter | 剪辑工具适配器 | ExternalToolAdapter 的剪辑类专用实现；负责工程/时间线格式、素材包、成片回流和时间码映射。 |
| AdapterManifest | 适配器清单 | 描述适配器身份、版本、工具兼容范围、能力、格式 Profile、权限、沙箱、签名、事件 Schema 和验证证据的版本化声明。 |
| AdapterCapabilityEvidence | 适配能力验证证据 | 针对具体工具产品/版本、适配器版本、部署模式、格式 Profile 和能力级别形成的测试与审批证据。 |
| ExternalToolConnection | 外部工具连接 | 绑定租户/组织/项目、适配器、外部项目引用和 credential_ref 的受限连接对象。 |
| ToolExchangePackage | 工具交换包 | 固定源 AssetVersion、文件清单、Rights/Policy 快照、格式 Profile、哈希和有效期的不可变导出包。 |
| ToolExchangeSession | 工具交换会话 | 描述一次外部工具导出、执行、回流和对账的独立状态对象，不等于 AssetVersion、QC 或 Gate 状态。 |
| ExternalToolArtifact | 外部工具暂存产物 | 外部工具回传后进入 Staging/Quarantine 的文件和元数据记录；通过安全、版权和血缘校验后才可创建正式 AssetVersion。 |
| TimelineManifest | 时间线清单 | 以整数帧、有理帧率、时间基准和明确素材版本引用描述序列、轨道、片段入出点的版本化对象。 |
| ToolMetadataSnapshot | 工具元数据快照 | 从外部工具回传的镜头标记、缺陷提示和批注映射结果；属于建议/证据，不直接修改 QCReport、GateDecision 或 Canon。 |
| TimelineDraft | 时间线草案 | 平台侧唯一权威的版本化时间线元数据；不包含渲染后的合成媒体。 |
| EditDecisionList | 剪辑决策列表 | 从 TimelineDraft 按 Format Profile 投影的交换表示，不是独立权威时间线。 |
| ExportPackage | 工程交换包 | 供外部专业工具打开的不可变包；不等于最终成片或 DeliveryManifest。 |
| ExportManifest | 工程包清单 | 列明素材、文件哈希、Profile、权利、限制与导入说明的包内事实清单。 |
| TimelinePreviewPlayer | 时间线顺序预览播放器 | 按顺序播放低清代理片段；不生成可下载/可发布的合成媒体。 |
| CreatorWorkspace | 个人创作者工作区 | workspace_mode=CREATOR_MODE 的隔离工作区；模式创建后不可原地改变。 |
| CREATOR_GATE | 创作者模式门禁 | Creator Mode 基础质量/安全/权利评估；不等于工业 Gate4 或公开发布批准。 |
| CREATOR_PUBLICATION_CHECK | 创作者发布资格检查 | 对用户发起的 Creator Release 发布执行自动 PG0—PG3/权利/隐私/安全检查。 |
| CREATOR_DELIVERY | 创作者交付清单类型 | 记录外部渲染回流的个人作品 AssetVersion；不属于工业 DeliveryManifest。 |
| CREATOR_RELEASE | 个人创作者发布类型 | 对外标记“个人创作·创作者发布”；不得标记 DELIVERED_FINAL。 |
| CREATOR_MODE_COST | 创作者模式成本子分类 | CostLedger 中 EXPLORATION 的子分类；不得占用 Quality Reserve。 |
| TimelineFlowStatus | 时间线流程展示状态 | 供 UI 展示导出/导入/回流进度的投影，不是 TimelineDraft 权威状态。 |

P0-ARCH：必须冻结长期兼容所需接口/Schema/权限边界，但不要求在首发阶段实现完整功能。

P0-MVP：Phase 1 必须实现并通过端到端验收的核心生产闭环能力；不绑定固定月份或个人开发周期。

USER_OWN_KEY：用户使用自有模型凭证并直接向模型供应商结算的模式。

PLATFORM_PAID：平台使用统一密钥池调用模型并向用户统一结算的模式。

USER_DIRECT：用户直接向供应商结算的 CostLedger 结算类型。

PLATFORM_SETTLED：平台代付并向客户统一结算的 CostLedger 结算类型。

OFFLINE_PRIVATE：不依赖平台商业计费服务、仅使用用户自有密钥/内部模型的私有化部署模式。

ProjectBudgetPool：MVP 项目预算参考池，不等同于平台托管资金账户。

ShowcaseSnapshot：引用精确生产版本、不可变且可撤销的只读展示快照。

CommunityPost：绑定 ShowcaseSnapshot 的社区帖子，承载标题、说明、受众、互动和审核状态，不改变生产事实。

Publication Gate（PG0—PG3）：独立于 Gate0—4 的展示发布门禁，负责快照完整性、披露、版权隐私和发布审批。

DELIVERED_FINAL：仅当来源版本通过有效 Gate4 且绑定有效 DeliveryPackage 时允许使用的展示内容类型。

STALE_NEW_VERSION_AVAILABLE：源版本发生更新但旧快照仍可按授权展示时的过期提示状态。

REVOKED_BLOCKED：因版权、隐私、安全或授权撤销而必须冻结展示访问的状态。

ReuseRequest：社区复用申请；批准后创建新的 Project/ShotRevision，不直接 Fork 源项目。

V1.3.5 历史修订记录：在前序版本基础上增加视频反推、资产与知识中枢、客户审片与交付、学习与能力平台、开放生态与市场、运营数据与基准评测六个领域；新增第 24—30 章、附录 C/D 和 ADR-052～073。该记录仅说明历史变更，不代表当前版本。

V1.3.5 历史实施说明：当时提出的三极定位、生产/展示画布分离、发布不等于 Gate4、REFERENCE_ONLY 和 Agent 禁止自批等原则继续继承，并在 V2.0 中统一为最终产品规则。

**规范性用语：MUST / MUST NOT / SHOULD / MAY：**分别表示强制要求、禁止要求、建议要求和可选能力；“目标/建议”不得误读为已实现或强制门禁。

**生产画布（Production Canvas）：**承载可写生产事实、ShotRevision、DAG、Gate、资产版本和权限控制的内部工作区。

**展示画布（Showcase Canvas）：**基于不可变 ShowcaseSnapshot 的只读展示界面，不暴露生产画布写操作。

**ShareLink：**具有访问范围、有效期、撤销能力和访问审计的只读分享凭证。

**ReverseAnalysisReport：**从有权处理的参考视频生成的结构化反推报告，默认 REFERENCE_ONLY。

**ReverseShotSegment：**绑定源视频版本、时间码和帧率的反推镜头片段。

**ReuseRequest：**复用申请；批准后在目标空间创建新项目/镜头版本，不直接 Fork。

**ClientReviewPortal：**客户只读审片入口；客户批注不直接改变内部 QC 或 Gate。

**DeliveryManifest：**可追溯、可校验、可重建的正式交付资产清单。

**EscapeReport：**记录交付后质量逃逸并关联 RCA 的问题报告。

**ShowcaseSnapshot：**引用精确 AssetVersion、ShotRevision、PolicyResolution 与权利清单的不可变展示快照。

**SHOWCASE_QUALITY：**展示技术完整性、水印、脱敏、代理文件和权利状态检查，不替代成片质量 Gate。

V2.0.5 当前版本声明：本文为统一架构基线、产品路线基线和工程契约/验收基线。它继承 V1.3.5 完整内容并纳入 V2.0.1 文档治理整改及 V2.0.2 外部工具适配层规范，不代表所有功能或第三方工具适配器已经实现。实际实现状态须由代码、Schema、自动化测试、灰度记录和部署验收证据确认。

V2.0.4 修订记录（历史）：在 V2.0.2 的清洁主文档上整合 V2.0.3 的 TimelineDraft、ExportPackage、TimelinePreviewPlayer 和 Creator Mode 契约；重写第 33—34 章并置于附录 A—D 之前；将 TimelineDraft 与 ExportPackage/ToolExchangeSession 状态机分离；删除任何平台内 SimpleRender/帧级剪辑/转场/字幕烧录/混音/最终 MP4 生成路径；补充三类时间线模板、10 秒导入指引、能力矩阵与 unsupported_features；统一 CreatorGate、CreatorPublicationCheck、CreatorDeliveryManifest、CreatorRelease 独立对象；明确公开 Creator Release 由用户显式发起、低风险自动检查通过后可发布、Agent 禁止代发；Action Registry 由 56 项扩展至 75 项；新增 ADR-101—118。

V2.0.4 发布说明（历史）：本版本是架构与产品化契约修订基线，不代表 Creator Mode UI、TimelinePreviewPlayer、工程包导出、剪映/PR/达芬奇 Profile 或插件已经开发上线/完成兼容性测试。Phase 0 冻结前须完成 Schema/Action/权限/版权/账本/状态机/负向测试；Phase 2/3 能力只有在具体工具版本、格式 Profile、导入/回流、QC/Gate、撤销和回滚证据通过后方可标记 ENABLED。

## 附录 B：版本修订记录

V1.3.3 修订记录（历史）：在前序架构整改基础上增加产品实施策略、双模式计费架构、USER_OWN_KEY、自有密钥安全、PLATFORM_PAID 商业隔离和离线私有化规则；后续版本保留其商业架构约束，并取消固定周期交付假设。

V1.3.3 补充说明（历史）：该版本曾包含固定周期实施建议；后续版本改为长期分阶段路线，双模式计费安全边界、私有化规则和 CostLedger 口径继续有效。

注：本文是目标架构和分阶段产品路线基线，不代表所有能力已实现；任何初始阈值、权限、推荐策略、配额和估算须经代表性场景验证、审批并版本化。

1. 合并 V1.2 历史章节并归并至主题章节，清理重复路线图、重复验收清单和旧版结束标记。

2. 补齐 Intent-aware QC、deviation_intent_ref、Detector Registry、检测器决策权限、Override Rate、重复 Override 与质量逃逸治理。

3. 解耦 L4 创意自主权与 COMPACT/STANDARD/EXPANDED 资源强度，明确项目级/镜头级策略、资源升级及质量红线不可覆盖。

4. 明确 Project DB、Temporal、Provider Job 状态边界，补充 Outbox/Reconciliation、STALE 三级传播、连续性依赖、外部模型幂等和重复计费规则。

5. 将白模/Previz 定义为跨路线控制信号层，补齐 ShotSpec、PrevizVersion、ControlSignalAsset、CameraRig、BlockingAsset、BlockingAdapter、ProductionRoute、Rights Manifest、Control Quality 与 ADR-021～024。

V2.0.1 修订记录（历史）：基于 V2.0 完整主文档完成本轮 P0 文档治理与逻辑一致性整改；增加完整 Action Registry 与历史别名映射；明确唯一阶段表、Phase 1 发布边界、ConstraintPack 适用性硬门、独立状态机、客户审片术语及预算转拨阶段策略；增加评审意见逐项关闭记录。经核对已正确的 L1—L6、Task、Previz、stale_state、request_id、cost_bearer、PG0—PG3、QCReport、终检报告和成本枚举保持原样，不作无依据改写。

V2.0.1 发布说明（历史）：本文件为 V2.0.1 修订主版本；附录 C/D 为本文件内嵌规范性附件。本文档治理问题已按评审清单修订，但“文档完成”不代表软件功能已经实现。该句中“Phase 0 是否冻结”的表述属 V2.0.1 时点的历史陈述，现行冻结状态以文件头部与 31.11 为准，不以本段为准。

V2.0.2 修订记录（历史）：在 V2.0.1 基础上完成平台自研治理核心与外部专业工具执行层的战略边界固化；保持 L1—L6 六层骨架不变，在 L3 增加统一 ExternalToolAdapter 及 EditingToolAdapter 等专用适配器，在 L4 统一纳入工程文件和时间线资产；新增第 32 章，定义 Adapter Manifest/SPI、四级接入能力、工程资产对象、文件/元数据交换、时间码映射、导入隔离、回流 QC/Gate、权限版权成本、Action Registry 规范动作、分阶段验收和 ADR-093～100。

V2.0.2 发布说明（历史）：本版本完成外部工具适配层的架构与产品化设计，不代表 Premiere Pro、DaVinci Resolve、剪映专业版、Audition、Blender、UE 或 AE 的具体适配器已经实现或通过兼容性验证。工具和版本的可用能力必须以 AdapterCapabilityEvidence 和 VERIFIED/PILOT 状态为准；任何外部回流产物均不得绕过平台 AssetVersion、Rights、QC、人工 Gate 和 DeliveryManifest。

V2.0.5 修订记录（当前）：按不堆代码、不叠框架、不畏难点、全程 AI 辅助开发与生产的评审标准，对基线做定点整改：保留完整技术选型与完整智能能力规划，不做单人/MVP 向的组件替代与能力退化；统一阶段、预算、状态机口径的权威来源；补充全局红线不变量清单、工程质量与 AI 辅助开发规则（反屎山架构与代码、禁止补丁式演进、保持高技术标准、AI 辅助产出同样受红线与验收约束）、策略实现约束与资产谱系边界。长期架构与扩展位保留不变。

V2.0.5 发布说明：本版本为架构与工程契约修订基线，不代表任何 Creator Mode、时间线、工程包、外部工具适配器已经实现；实现状态须以代码、自动化测试、部署与审计证据验收为准。

## 附录 C：工程契约、预算、权限与架构冻结参考

本章将此前审核发现的原则性描述转化为可实现、可测试的工程契约。附录 C补充规则与前文冲突时，以本章明确修订的预算、权限、状态机和实施阶段规则为准；其他已冻结原则继续沿用原章节及 ADR。任何实现必须通过第 16 章总验收清单，不得以本章表格替代自动化测试。

### C.1 整改优先级与处理边界

| **级别** | **整改项** | **目标结果** | **验收证据** |
| --- | --- | --- | --- |
| P0 / 冻结阻塞 | 预算账户冲突、数据契约、DAG/Gate、权限矩阵、预算/配额事务、版本和阶段口径 | 消除实现歧义，确保质量红线、成本归属、状态和权限不可绕过 | Schema 校验、契约测试、并发/故障注入、授权负向测试、审计记录 |
| P1 / 规模化前 | 角色外观/声音分资产、Pattern 生命周期、多人 QC 裁决、受控外部 Skill API | 形成可复用资产与团队协作闭环 | 集成测试、审核演练、权限撤销测试、复用证据 |
| P2 / 规模化后 | 无限画布、Skill 市场、自动语义多样性评分、高级调度优化 | 提升体验与生态能力，不阻塞高品质单项目生产 | 灰度指标、性能数据、回滚演练和商业价值评估 |

### C.2 四类预算账户的唯一口径与账本规则

本节统一覆盖第 6.5、6.6.2、12.2、17.6、20.1 和 22.2 节。若其他章节出现不同用途描述，以本节为准，并在后续版本中同步修正文案。

| **账户** | **唯一允许用途** | **明确禁止** | **预算不足时的动作** |
| --- | --- | --- | --- |
| Production Budget | 正式生产、已批准路线调用、基础/基线 Previz、合成、后期、交付及其必要人力/算力 | 不得隐瞒超支；不得自动跨项目挪用 | 暂停新增承诺，提出成本/周期/路线影响供授权人决策 |
| Exploration Reserve | CEE 提案、候选分支、关键帧探索、低清动态样片、探索型 Previz 和经批准的创意比较 | 不得承担 QC 缺陷修复或替代 Quality Reserve | 停止新增探索分支；保留已产出证据，不影响正式质量修复 |
| Quality Reserve | QC 缺陷修复、必要重生成、质量补偿、最终质量保障及经证据确认的返工 | 不得用于新创意分支、非必要风格尝试或多方案选优 | 保留质量修复额度；不足时阻断交付并请求追加预算/缩小交付范围 |
| Contingency Reserve | 经批准的供应商故障恢复、不可预见事件和紧急变更 | 不得成为常规制作/探索的隐性资金池 | 单独申请、说明事件、批准额度和事后审计 |

预算核算必须区分三种事实：BudgetLedger 记录账户额度、预留和资金流；CostLedger 记录实际发生的资源成本；QuotaReservation 记录并发/GPU/供应商资源占用。三者通过 transaction_id、reservation_id、task_id、provider_job_id 和 project/shot scope 关联，不得以修改账户余额代替账本流水。

预算账户命名口径：正文展示名统一使用 Production Budget、Exploration Reserve、Quality Reserve、Contingency Reserve；代码、字段、配置与账本标识统一使用无空格的 ProductionBudget、ExplorationReserve、QualityReserve、ContingencyReserve。QualityReserveProtected 仅用于描述质量储备的受保护金额/比例/上限标记，不是第五类账户，不得作为账户标识；其正文展示名统一为“Quality Reserve 受保护额度”，字段标识与配置键保持 QualityReserveProtected，二者一一对应。除此之外不得引入其他变体。

#### C.2.1 预算交易状态与不变量

| **交易动作/状态** | **规则** | **失败/补偿规则** |
| --- | --- | --- |
| RESERVE | 在单个数据库事务中校验可用额度、创建唯一 reservation_id、锁定账户版本并写入幂等键 | 额度不足则整体失败，不得先预留一部分再静默继续 |
| COMMIT | 仅按供应商账单/可核实实际费用或批准的固定费用提交；保留原预留引用 | 提交重复时按 idempotency_key 返回原结果，不重复扣款 |
| RELEASE | 任务取消、失败或结束后释放未使用额度；必须关联原预留 | 释放本身也幂等；超时释放需确认任务/供应商状态 |
| REFUND / ADJUSTMENT | 退款、折让或对账差额以独立反向/调整流水记录 | 禁止删除或覆盖原交易；保留原交易和凭证 |
| TRANSFER | 账户间转拨必须走 BudgetTransferRequest，包含来源、目标、金额、理由、申请/批准人和审计 | 默认不自动转拨；Quality Reserve 不得转入 Exploration Reserve；不得突破项目总预算与质量修复最低保护额 |

核心不变量：预算交易追加写、不可物理删除；账户余额由账本汇总得出；同一业务动作必须有幂等键；跨账户转拨在启用后必须原子化；预留额度不能同时被两个任务占用；对账不确定时先查询供应商/支付状态，不得盲目重试或再次扣款。Phase 1 仅允许同一预算账户内已审批额度重新分配，不启用跨账户自动转拨；跨账户 BudgetTransferRequest 由人工申请和人工审批，最早在 Phase 2 以后启用。

#### C.2.2 BudgetTransferRequest 长期契约与启用边界

BudgetTransferRequest 至少包含 transfer_id、source_account_id、target_account_id、amount、currency、reason_code、requested_by、approved_by、approval_ref、source_balance_snapshot、target_balance_snapshot、idempotency_key、ledger_transaction_ref、status、created_at、executed_at、audit_ref。状态建议：DRAFT → SUBMITTED → IN_REVIEW → APPROVED → EXECUTING → EXECUTED；拒绝进入 REJECTED；执行失败进入 FAILED，部分写入时必须通过原子事务回滚或显式补偿；不得将 FAILED 伪装为 EXECUTED。Phase 1 API/界面仅允许登记需求或返回 PHASE_NOT_ENABLED，不得触发跨账户流水；Phase 2 启用前必须通过并发、幂等、双人审批、账本对账和回滚负向测试。

### C.3 新增领域对象的数据契约与所有权

新增领域对象统一使用版本化 Schema，Schema 采用 JSON Schema/OpenAPI 兼容形式存放于 data/schemas，并由 CI 执行校验。每个对象必须明确 owner_module、写入命令、可读范围、状态、版本策略、审计和权利来源。公共字段按实体适用性定义，不要求无意义字段强行填充。

| **实体** | **权威所有者** | **必需业务字段（核心）** | **生命周期/关键约束** |
| --- | --- | --- | --- |
| CharacterCard | Asset Service / Character Domain | character_id、card_version、appearance_refs、reference_set_refs、negative_constraints、rights_manifest_ref | DRAFT→IN_REVIEW→APPROVED→ACTIVE→SUPERSEDED/REVOKED；生成结果只能创建更新提案 |
| CharacterCanon | Script/Canon Domain | canon_id、character_id、canon_version、facts、timeline_state、relationships、authority、approved_by | DRAFT→IN_REVIEW→APPROVED→ACTIVE→SUPERSEDED；事实冲突必须显式标记，不得由模型自动裁定 |
| CharacterAppearance | Character Asset Domain | appearance_id、character_id、time_scope、scene_scope、wardrobe_state、visual_refs、source_refs | 独立版本化；必须绑定适用时间/场景/状态，不得覆盖其他造型状态 |
| CharacterVoiceProfile | Audio/Character Asset Domain | voice_profile_id、character_id、voice_asset_refs、voice_attributes、license_scope | 独立版本化；声音素材、合成许可和可用范围可审计 |
| CharacterReferenceSet | Asset Service | reference_set_id、character_id、reference_items、view_type、source_refs、rights_manifest_ref | 集合不可变版本；每项参考素材保留来源、角色和用途标签 |
| ShotVisualPlan | Shot Planning Domain | shot_id、revision、intent_summary、camera_language、shot_size、lens_range、motion_intent、lighting_intent、locked_fields、open_fields | DRAFT→REVIEW→APPROVED/REJECTED；变更生成新 revision，锁定字段逐字段授权 |
| SkillManifest | Agent Runtime / Skill Registry | skill_id、skill_version、input_schema、output_schema、required_capabilities、allowed_commands、budget_scope、retry_policy、failure_codes | DRAFT→VALIDATED→PUBLISHED→DEPRECATED/REVOKED；运行时只执行已发布版本 |
| PatternRecord | PMS / Pattern Library | pattern_id、pattern_version、pattern_type、applicability、evidence_refs、rights_manifest_ref、failure_boundaries | EXPERIMENT_RECORD → IN_REVIEW → REUSABLE_PATTERN / REJECTED / FAILURE_CASE；REUSABLE_PATTERN → DEPRECATED → RETIRED |
| ExplorationBranch | Creative Exploration Service | branch_id、shot_id、base_shot_revision、proposal_refs、candidate_refs、exploration_budget_ref、stop_reason | DRAFT→RUNNING→CANDIDATES_READY→REVIEW→PROMOTED/REJECTED/STOPPED/EXPIRED |
| QCReviewSession | QC Governance | session_id、asset_version_id、qc_report_ref、reference_set、reviewers、findings、decisions | OPEN→IN_REVIEW→DECISION_PENDING→RESOLVED→GATE_ELIGIBLE/CLOSED；结果固定指向资产版本 |
| QuotaReservation | Budget/Quota Service | reservation_id、scope、resource_type、quantity、lease_expiry、idempotency_key、task_id | REQUESTED→RESERVED→COMMITTED/RELEASED/EXPIRED/FAILED；不可超卖，过期需核验任务状态 |
| BudgetLedgerTransaction | Budget/Quota Service | transaction_id、account_id、transaction_type、amount、currency、reservation_id、idempotency_key、approval_ref | APPENDED 追加式记录；修正只能新增反向/调整流水，不得覆盖原流水 |

#### C.3.1 通用 Schema 信封与版本规则

所有权威领域对象必须具备 entity_id、entity_type、schema_version、entity_version、status、owner_module、created_at、created_by、updated_at、audit_ref。按对象补充 scope（project_id/episode_id/scene_id/shot_id）、source_refs、rights_manifest_ref、policy_snapshot_ref 等字段；不适用字段可省略，不得使用空字符串伪造值。

```
{
  "entity_id": "shotvp_01J...",
  "entity_type": "ShotVisualPlan",
  "schema_version": "1.0.0",
  "entity_version": 3,
  "status": "APPROVED",
  "owner_module": "shot-planning",
  "scope": {"project_id": "prj_...", "episode_id": "ep_...", "shot_id": "sh_..."},
  "locked_fields": ["shot_size", "duration_ms"],
  "open_fields": ["camera_language", "lighting_intent", "composition_notes"],
  "source_refs": ["script_revision:...", "director_intent:..."],
  "policy_snapshot_ref": "policy_resolution:...",
  "created_by": "principal_...",
  "created_at": "2026-11-02T00:00:00Z",
  "updated_at": "2026-11-02T00:00:00Z",
  "audit_ref": "audit_..."
}
```

#### C.3.2 API 命令、事件信封与错误码

命令信封统一包含 command_id、command_type、command_version、idempotency_key、actor_context、scope、target_ref、expected_entity_version、payload、trace_id 和 requested_at。服务端按“Schema 校验→身份/权限→对象版本→策略/Gate→预算/配额→执行→Outbox 事件”顺序处理；失败不得产生部分权威状态。跨模块动作使用 Saga，补偿动作和不可补偿动作必须写入流程定义。

| **契约类型** | **必需字段/规则** | **说明** |
| --- | --- | --- |
| Domain Command | command_id、command_type/version、idempotency_key、actor_context、scope、target_ref、expected_version、payload、trace_id | 幂等键按 principal + command + target scope 唯一；基于 expected_version 防止并发覆盖 |
| Domain Event | event_id、event_type/version、occurred_at、aggregate_id/version、trace_id、correlation_id、causation_id、actor_ref、payload_ref | Outbox 原子写入；消费者 Inbox 去重；保留事件 Schema 版本和重放策略 |
| Asset Reference | asset_id、asset_version_id、sha256、media_type、storage_ref、rights_manifest_ref | 所有下游引用指向不可变版本，不得只引用可变逻辑资产 ID |
| Error Envelope | error_code、category、retryable、message、details、correlation_id、retry_after | 调用方按结构化 error_code 处理；不得依赖自然语言 message 判断重试 |

| **错误码类别/示例** | **含义** | **默认处理** |
| --- | --- | --- |
| SCHEMA_INVALID / CONTRACT_VERSION_UNSUPPORTED | 输入不符合 Schema 或契约版本不兼容 | 不重试；返回字段级错误和支持版本 |
| AUTH_SCOPE_DENIED / CAPABILITY_EXPIRED / CAPABILITY_REVOKED | 权限或 Capability Token 作用域/有效期不满足 | 不重试；拒绝并写审计 |
| ENTITY_VERSION_CONFLICT / INVALID_STATE_TRANSITION | 对象版本冲突或状态机不允许该转移 | 读取最新版本并重新发起命令；不得覆盖写 |
| GATE_BLOCKED / RIGHTS_QUARANTINED / QUALITY_REDLINE_VIOLATION | Gate、版权隔离或质量红线阻断 | 不自动重试；进入指定治理/修复流程 |
| BUDGET_EXCEEDED / BUDGET_ACCOUNT_PURPOSE_VIOLATION | 额度不足或预算账户用途不匹配 | 不重试；预算用途冲突必须阻断并告警 |
| QUOTA_EXCEEDED / PROVIDER_RATE_LIMITED | 资源配额不足或供应商限流 | 可排队；遵守 Retry-After 和调度策略 |
| PROVIDER_SUBMISSION_UNKNOWN / PROVIDER_RESULT_UNKNOWN | 外部提交/结果状态未知 | 先查询供应商或 reconciliation；不得盲目重复提交 |
| DEPENDENCY_STALE / INPUT_ASSET_INVALID | 输入依赖失效或资产损坏 | 按依赖图重算/隔离；不得继续使用失效输入 |

字段约束基线：ID 使用不可猜测的全局唯一 ID；schema_version 使用语义版本；entity_version 使用单调递增整数；时间统一存 UTC ISO-8601 并保留原始业务时区；金额使用 Decimal 与 ISO 4217 币种，不用浮点数；媒体引用必须含版本和校验和；帧/时间码使用明确 FPS 分子/分母，禁止混用浮点秒作为唯一时间基准；枚举值未知时按兼容策略拒绝或 QUARANTINE，不得静默映射。

#### C.3.3 SkillManifest 运行契约示例

```
skill_id: script-to-shots
skill_version: 1.0.0
status: PUBLISHED
input_schema_ref: schemas/script-to-shots.input.schema.json
output_schema_ref: schemas/script-to-shots.output.schema.json
required_capabilities: [script.read, shot.draft.create]
allowed_commands: [CreateShotDraft, SubmitStoryboardProposal]
denied_commands: [ApproveScriptLock, ApproveGate, ModifyCharacterCanon]
budget_scope: {accounts: [ProductionBudget], max_amount: 0, currency: CNY}
side_effects: [create_draft_records]
human_approval_points: [script_lock, shot_revision_promote]
timeout_seconds: 600
retry_policy: {max_attempts: 2, retryable_errors: [PROVIDER_RATE_LIMITED]}
idempotency_key_required: true
failure_codes: [SCHEMA_INVALID, AUTH_SCOPE_DENIED, ENTITY_VERSION_CONFLICT]
```

### C.4 DAG 节点、Gate、轨道与失效传播矩阵

Orchestrator 是正式状态推进的唯一权威入口。工作台和 Agent 只提交领域命令；所有节点必须固定输入版本、输出版本、策略快照和权限上下文。探索分支不得直接将候选资产写入正式 ShotRevision。

| **阶段/任务节点** | **主要输入 → 输出** | **Gate/阻断条件** | **失效/回流规则** |
| --- | --- | --- | --- |
| SCRIPT_INGEST / SCRIPT_ANALYZE | 剧本文件/版本 → ScriptRevision、场景/节拍解析 | 输入完整性、权利来源、文件安全；失败阻断后续解析 | 剧本新版本只使引用段落和相关镜头分析失效 |
| SCRIPT_TO_SHOTS | ScriptRevision、DIE 意图 → ShotDraft、ShotVisualPlan Draft | 连续性/时长/事实问题输出提示；不自动锁定 | 草案阶段不触发正式生产；来源段落变化重算相关草案 |
| HUMAN_CONFIRM / SCRIPT_LOCK | ShotDraft → ScriptLock、ShotRevision、ShotList | Gate 1-A；必须有授权确认人、版本与差异 | 未确认不得进入正式 A/B/F 轨道 |
| CANON_VALIDATE / ASSET_RESOLVE | Canon、CharacterCard、Appearance、Rights → Continuity/Asset Resolution | 事实冲突、权利未知、引用版本失效时阻断或隔离 | Canon/Appearance 版本变更只传播到实际引用对象 |
| VISUAL_PLAN_APPROVE / SHOTSPEC_COMPILE | ShotVisualPlan → 锁定字段映射、ShotSpec、ConstraintPack | 仅批准且可执行字段进入 ConstraintPack；开放创意字段不转硬约束 | 锁定字段变化触发相关下游 DIRECT_STALE；开放字段变化不自动触发硬约束重算 |
| BLOCKING_BUILD / GEOMETRY_VALIDATE | ShotSpec、BlockingAsset → Scene、GeometryReport | 几何失败则 PREVIZ_RENDER、CONTROL_SIGNAL_EXPORT 和付费生成保持 BLOCKED | 白模几何/相机/走位变化使实际消费该信号的任务失效 |
| PREVIZ_RENDER / PURITY_CHECK / CONTROL_SIGNAL_EXPORT | 基线或探索 Previz → PrevizVersion、ControlSignalAsset | A2-QC；纯净度、规格、权利、几何与版本均通过 | 只影响消费相关 ControlSignalAsset 的任务；保留可复用资产 |
| ROUTE_CAPABILITY_VALIDATE | ShotSpec、控制信号、外观参考、ModelAdapter.capabilities → RoutePlan | 必须匹配 signal_type/reference_role/controlled_dimensions；UNVERIFIED 不可作为可用能力 | 能力/Adapter 版本变化只重评相关路线组合 |
| CEE_PROPOSE / BRANCH_RUN / COMPARE | CreativeProposal、预算快照 → ExplorationBranch、CandidateAsset、ComparisonRecord | 独立 Exploration Reserve；候选对比由人类/证据决策 | 探索节点不写正式资产状态；熔断只停止该分支 |
| PROMOTE / REJECT | 获选候选、差异、来源、QC、批准 → 新 ShotRevision 或拒绝记录 | Promote 需要导演/授权人确认、预算核验、rights/QC 校验 | 晋升创建新版本并按差异做最小 STALE；不得覆盖基线 |
| FORMAL_GENERATION / POST_PROCESS | 正式 ShotRevision、RoutePlan → AssetVersion、Timeline Segment | 模型调用幂等、来源权利、成本预留、任务结果完整 | 输入或策略快照变化按依赖边传播 STALE |
| AUTO_QC / HUMAN_QC | AssetVersion、QCReport、Intent、Reference → QCReviewSession | 核心技术 QC 与必需检查全量执行；高风险/不确定项人工复核 | 缺陷创建修复任务；人工误报/意图判定保留审计 |
| GATE_DECISION / DELIVERY | 固定版本、QC、权利证据、终检清单 → GateDecision、DeliveryPackage | Gate 绑定固定快照；合规、安全、版权不可豁免 | Gate 失败回到指定节点；交付包由 Manifest 重建，不修改已锁版本 |

### C.5 RBAC / ABAC / Capability Token 权限矩阵

RBAC 决定角色可申请的动作集合；ABAC 决定动作在组织、项目、集、场景、镜头、资产分类、风险、预算和时间窗口上的适用范围；Capability Token 将一次授权收敛为短时、限范围、可撤销的执行能力。任何入口均调用统一授权服务，禁止 UI/Agent 自行判断权限。

| **关键操作** | **导演/创作负责人** | **编剧** | **技术导演** | **QC 审核人** | **制片/预算管理员** | **版权合规** | **Agent/Worker** |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 提交创意/镜头方案 | 允许 | 允许 | 允许 | 只读建议 | 只读 | 只读 | Token 范围内提交草案 |
| 确认 ScriptLock / ShotRevision | 可批准授权范围 | 可提交/编辑 | 技术校验 | 只读 | 只读 | 权利审查意见 | 不得自批；只能请求确认 |
| 修改 CharacterCanon | 提出/批准授权事实 | 提出/编辑草案 | 不得修改事实 | 不得修改事实 | 不得修改事实 | 审查权利来源 | 仅创建 Canon Draft |
| 锁定 ShotVisualPlan 字段 | 批准创作字段 | 提出建议 | 批准技术字段（授权范围） | 只读 | 只读 | 只读 | 不得锁定；可生成建议 |
| 批准镜头等级/策略变更 | 创作/策略审批范围 | 无 | 技术可行性评估 | 无 | 预算审批/资源确认 | 合规审查 | 仅提交申请 |
| 批准质量 Gate | 不得代替 QC | 不得 | 仅限技术 Gate 职责且职责分离 | 批准 QC 职责范围 | 不得 | 批准合规 Gate 职责范围 | 不得通过 Gate |
| 预算预留/释放/调拨 | 申请或授权范围 | 无 | 可申请技术资源 | 无 | 审批及账本管理 | 无 | 仅限已签发 budget_scope |
| 版权许可确认/隔离解除 | 提供创作来源 | 提供来源 | 提供技术来源 | 发现并报告 | 无 | 唯一权威确认角色 | 不得确认或解除隔离 |
| 系统配置/密钥/策略发布 | 无 | 无 | 按运维授权 | 无 | 无 | 审计参与 | Worker 最小权限；系统管理员操作全审计 |

#### C.5.1 Capability Token 最小契约

Capability Token 至少绑定：subject/principal、issuer、project_scope、episode_scope、shot_scope、asset_scope、allowed_actions、denied_actions、budget_scope/account_allowlist、max_amount/currency、resource_quota、delegation_chain、approval_ref、issued_at、not_before、expires_at、jti、revocation_ref。Token 必须短时有效、可撤销、不可扩大权限；每次命令仍须服务端重新校验 Gate、预算、对象版本和职责分离。

强制职责分离：申请人不得审批自己的预算/Canon/Gate 变更；预算管理员不得批准质量 Gate；QC 不得修改权利事实；版权角色不得代替导演决定艺术意图；Agent、Worker 和系统管理员不得通过技术权限绕过业务审批。系统管理员的紧急访问必须限时、双人授权并写入不可篡改审计。

### C.6 QuotaReservation 与供应商成本对账

| **场景** | **必须执行的控制** | **验收用例** |
| --- | --- | --- |
| 并发争抢最后额度 | 数据库事务/行锁或等价原子机制校验可用量并创建唯一 Reservation；禁止先读余额再无锁写入 | 两个并发请求争抢剩余额度，只能一个成功 |
| Worker 崩溃/租约超时 | Reservation 绑定 lease_expiry 与 task_id；超时后先查询工作流/供应商状态，再决定释放 | Worker 被杀后不会永久占用资源，也不会在供应商仍运行时误释放 |
| 供应商提交响应丢失 | 记录 idempotency_key 和 request hash；未知状态先查询 provider_job_id 或供应商幂等接口 | 模拟提交成功但响应丢失，不重复收费 |
| 异步回调重复/乱序 | Webhook Event ID 去重；状态按允许转移校验；原始事件保留 | 同一回调多次到达只生成一次成本承诺/提交 |
| 供应商账单与预估差异 | CostLedger 保存 estimate、committed、actual、currency、price_version、usage evidence | 账单差异超过阈值告警并触发对账工单 |
| 任务取消/部分计费 | 区分 provider_cancel_requested、provider_cancel_confirmed 和 actual_usage；只释放未消费部分 | 取消后仍计费时，实际费用入账且未消费预留释放 |
| 替代路线/模型切换 | 必须重新做 capabilities、rights、quality-equivalence、budget 校验；不得自动降质 | 替代模型不支持已锁定控制维度时不得自动切换 |

QuotaReservation 和 BudgetLedgerReservation 是两个不同概念：前者占用并发/GPU/存储/供应商资源配额，后者冻结账户可用金额；二者应通过 task_id 和 reservation linkage 关联，但不得混为同一余额。资源耗尽时返回 QUOTA_EXCEEDED / BUDGET_EXCEEDED 及可选等待/替代建议，由授权人决定后续动作。

### C.7 关键状态机与版本不可变规则

| **对象** | **状态流转** | **变更/终态规则** |
| --- | --- | --- |
| CharacterCard / CharacterAppearance | DRAFT→IN_REVIEW→APPROVED→ACTIVE→SUPERSEDED；也可 REJECTED/REVOKED | 批准后版本不可覆盖；变更创建新版本；Rights 不合格进入 QUARANTINED |
| CharacterCanon | DRAFT→IN_REVIEW→APPROVED→ACTIVE→SUPERSEDED；冲突进入 CONFLICT_REVIEW | Canon 事实只由授权人确认；冲突期间相关镜头不得自动合并事实 |
| ShotVisualPlan | DRAFT→REVIEW→APPROVED；可 PARTIALLY_LOCKED/LOCKED；拒绝为 REJECTED | 锁定字段单独记录锁定人/时间/理由/版本；变更创建新 revision，并评估 ShotSpec/ConstraintPack 影响 |
| ExplorationBranch | DRAFT→RUNNING→CANDIDATES_READY→REVIEW→PROMOTED/REJECTED；或 STOPPED/EXPIRED/FAILED | PROMOTED 产生新的正式 ShotRevision；STOPPED 仅停该分支；不可覆盖正式基线 |
| QCReviewSession | OPEN→IN_REVIEW→DECISION_PENDING→RESOLVED→GATE_ELIGIBLE→CLOSED | 多人冲突触发裁决；WAIVE_WITH_AUTHORITY 记录授权与理由；法律/版权/安全不允许豁免 |
| QuotaReservation | REQUESTED→RESERVED→COMMITTED/RELEASED/EXPIRED/FAILED | 同一 reservation 幂等；过期释放前需核验任务和供应商状态 |
| BudgetLedgerTransaction | APPENDED；后续通过新流水 REFUND/ADJUST/REVERSAL | 不可修改/删除；交易 ID 与幂等键唯一；账户余额通过流水计算 |

所有版本状态变更应通过领域命令完成，并写入 Outbox Event；事件消费者使用 Inbox 去重。终态对象不得原地恢复成可编辑对象，需通过新版本或显式 Reopen 命令处理。状态机中未列出的转移一律拒绝并输出结构化错误码。

### C.8 P0/P1/P2 与 Phase 0—4 映射摘要（非权威；与 31.2 不一致以 31.2 为准；CI 校验 phase_map.yaml）

本节仅保留功能优先级与实施阶段的查询摘要，不定义独立阶段表；唯一平台 Phase 0—4 及各阶段验收门槛见第 31.2 节。P0/P1/P2 不是阶段编号；Phase 0 可以冻结 P1/P2 的 Schema，但不代表这些功能已经上线。

| **功能项** | **优先级** | **实施阶段（摘要；以 31.2 为准）** | **明确交付边界** |
| --- | --- | --- | --- |
| 统一领域命令、DAG、Gate、版本快照、STALE、核心 QC、预算/权限审计 | P0 | Phase 0 契约冻结；Phase 1 生产闭环 | 必须先建立单一事实源和不可绕过后端门禁 |
| 内部 Skill Runtime 与四个核心 SKILL.md | P0 | Phase 0 冻结 Manifest/接口；Phase 1 启用 | 只调用统一领域命令；Skill 无跨模块数据库写权限 |
| 最小导演工作台 | P0 | Phase 1 | 覆盖项目/镜头、任务 DAG、审批、QC 和成本只读看板 |
| 一条白模/Previz 路线与一条 ControlSignal 消费路线端到端 | P0 | Phase 1 | 验证最小闭环；不得承诺所有镜头/模型普遍适用 |
| CharacterCard/Canon、Appearance/Voice/ReferenceSet、ShotVisualPlan | P1 | Phase 0 冻结 Schema；Phase 2 落地 | 角色资产与事实分离，VisualPlan 锁定字段/开放字段可审计 |
| CEE 受控分支、独立探索预算、Pattern 生命周期 | P1 | Phase 0 冻结账本与状态机；Phase 2 落地 | 默认人工触发，不自动对普通镜头全量开分支 |
| 完整双屏 QC、多人裁决、反馈闭环 | P1 | Phase 1 核心复核；Phase 3 增强 | 核心自动 QC 与最终 Gate 不得延后或关闭；增强功能可分期 |
| 团队空间、细粒度配额和高级调度 | P1 | Phase 0 冻结接口；Phase 3 落地 | 先解决原子预留、对账和角色作用域，再做高级公平调度 |
| 受控外部 Skill API | P1 | Phase 3 | 只开放版本化白名单动作、限流、审计、撤销和范围 Token |
| 第三方 Skill 市场、无限画布、自动语义差异/新颖性评分 | P2 | Phase 4 | 必须先有安全沙箱、版权治理、回滚和指标证据 |

白模/ControlSignal 对齐：Phase 0 冻结契约；Phase 1 可开展 PoC，但白模不是主生产闭环硬前置；只有在实测覆盖对应控制维度、权利通过且能力状态 VERIFIED 后，才可选作生产路线；Phase 2 扩展多模型/多路线、白模库、2D Layout、虚拟制片或实拍合成。未经实测标记 UNVERIFIED，不进入自动生产路由。

### C.9 外部产品能力吸收与取舍矩阵（LibTV 研究结论）

| **能力/交互范式** | **决策** | **本平台落地方式** | **优先级** |
| --- | --- | --- | --- |
| 导演可视化工作台 + Agent Skill API 双入口 | 改造吸收 | 共用领域服务、命令、DAG、权限与数据模型；入口不拥有权威业务状态 | P0/P1 |
| DAG 节点工作流与局部重算 | 改造吸收 | 加入 Gate、版本快照、Formal/Exploration 隔离、STALE 和成本可见性 | P0/P1 |
| 角色卡/参考资产库 | 改造吸收 | CharacterCard + Canon 分离；Appearance/Voice/ReferenceSet 独立版本和 Rights | P1 |
| 工作流/Skill 模板 | 改造吸收 | 沉淀为 Pattern/SkillManifest；可挑战、可弃用、不得成为质量唯一来源 | P1 |
| 专业镜头参数 | 直接吸收并结构化 | ShotVisualPlan；锁定字段进入约束，开放字段保留创意自由 | P1 |
| 无限画布 | 延后吸收 | 后端 DAG 和版本治理稳定后再做 UI 交互画布 | P2 |
| 团队协作/算力配额 | 改造吸收 | 接入 RBAC/ABAC、BudgetLedger、QuotaReservation 和 CostLedger | P1 |
| AI 剧本拆镜 | 改造吸收 | Skill 输出草案；经人工确认后才产生正式 ShotRevision/ScriptLock | P1 |
| 无 Gate 的自由生产/模板强制质量保障 | 不吸收 | 不得绕过质量红线；模板只作参考，不能锁死创作表达 | 拒绝 |
| Agent 直接写跨模块数据库/拥有隐式审批权 | 不吸收 | 统一领域命令、Capability Token 和职责分离；禁止自批自用 | 拒绝 |

### C.10 最终工程目录与四个核心 SKILL 的职责边界

第 14 章已将根目录统一为 my-ai-studio/。四个核心 SKILL 文档承担领域级流程编排和规则引用；skills/ 下的细粒度 Skill 承担具体任务契约；services/、agents/、workers/ 与 adapters/ 放置可执行业务代码和外部适配。四类目录职责不得混用。

| **目录层** | **负责内容** | **禁止事项** |
| --- | --- | --- |
| 01—04 四个核心 SKILL | 总体调度、IP/剧本、分镜、生产创意领域规范；引用 references 中的契约/质量规则 | 不直接实现数据库写操作；不绕过 Orchestrator/Gate |
| skills/ 细粒度 Skills | 单一任务输入输出、工具权限、失败语义、审批点、预算作用域 | 不得隐式扩大权限或以 Prompt 作为质量保障机制 |
| services/ 与 agents/ | 领域服务、策略决策、Agent Runtime、规划/评估逻辑 | 不得跨模块直接写表；Agent 不拥有审批权 |
| workers/ 与 adapters/ | 异步执行、模型/存储/Blocking/ProductionRoute 外部差异封装 | Worker 不决定业务 Gate；Adapter 不改变领域事实 |
| data/、scripts/、tests/、infra/、docs/ | Schema/迁移/策略数据、自动化脚本、测试、部署、架构/ADR/SOP | 不得把运行期密钥写入版本库；Schema 与迁移必须纳入 CI |

### C.11 架构冻结前自动化验收用例

| **测试 ID** | **场景** | **预期结果** |
| --- | --- | --- |
| BUD-001 | CEE 分支尝试从 Quality Reserve 扣费 | 拒绝并记录 BUDGET_ACCOUNT_PURPOSE_VIOLATION；Quality Reserve 余额不变 |
| BUD-002 | Exploration Reserve 耗尽但 QC 发现严重缺陷 | 停止新增探索；质量修复仍可按 Quality Reserve 策略申请 |
| BUD-003 | 同一 idempotency_key 重复提交预算预留 | 返回同一 reservation 结果，不重复冻结额度 |
| SCH-001 | ShotVisualPlan 未锁定 camera_language 被修改 | 生成新 revision 或提案；不自动改写 ConstraintPack |
| CAN-001 | 生成结果试图直接覆盖 CharacterCanon | 拒绝；只能创建 Canon Draft/变更申请 |
| DAG-001 | ExplorationBranch 候选未 Promote 直接请求正式 Gate | 拒绝；正式轨道状态不变 |
| DAG-002 | Promote 后仅一个已锁字段改变 | 只传播到真实依赖该字段的节点，不全量重跑 |
| AUTH-001 | Agent 尝试审批自己发起的预算/Canon/Gate 请求 | 拒绝并记录职责分离违规 |
| AUTH-002 | QC 角色尝试修改 Rights Manifest 权威许可状态 | 拒绝；需版权合规授权 |
| QUO-001 | 两个任务并发争抢最后一个 GPU/供应商额度 | 原子预留只允许一个成功，另一个获得可重试配额错误 |
| QUO-002 | 供应商已受理但提交响应丢失 | 先查询/对账，不重复提交或重复计费 |
| QC-001 | 多人 QC 意见冲突 | 进入 DECISION_PENDING，未经裁决不允许 Gate 自动通过 |
| PHASE-001 | Phase 1 只有一条验证过的白模消费路线 | 允许最小闭环上线；未验证路线仍为 UNVERIFIED |
| API-001 | 外部 Skill API 请求超出 scope 或 budget_scope | 拒绝；记录 principal、token jti、动作和审计证据 |

### C.12 架构整改基线判定标准（历史基线）

- [ ] 预算账户唯一口径已修复：第 6.5、6.6.2、12.2、17.6、20.1、22.2 和附录 C不存在用途冲突。

- [ ] 新增领域对象的 Schema、owner、版本、状态机、权限、来源/版权字段、事件和错误码进入版本控制并由 CI 校验。

- [ ] 导演工作台、内部 Skill Runtime 和未来外部 Skill API 共享同一领域命令；所有 Gate 在服务端强制执行。

- [ ] Formal Track 与 Exploration Branch 状态、预算和晋升协议隔离；Promote 创建新版本并最小化 STALE。

- [ ] RBAC/ABAC/Capability Token 通过角色正向/负向测试、作用域越权测试和职责分离测试。

- [ ] 预算账本、成本账本和资源配额账本可对账；预留、承诺、释放、退款、重复回调和崩溃恢复具备自动化证据。

- [ ] 关键状态机不存在未定义转移；终态对象不可原地覆写；审计事件和 Outbox/Inbox 可重放。

- [ ] 四个核心 SKILL 与细粒度 Skills、Services、Workers、Adapters 的边界在代码仓库中一致。

- [ ] 第 16 章总验收清单全部 P0 通过；P1/P2 的 Schema、权限和不可自动判定边界已冻结。

- [ ] 文档版本、页眉、修订记录、目录根名和 Phase 映射一致；渲染后表格无跨页断行和附录原始 Markdown。

### C.13 新增架构决策记录（ADR-037～044）

| **ADR** | **决策** | **关键约束** |
| --- | --- | --- |
| ADR-037 | 四类预算账户用途唯一、质量储备不可挪作探索 | Quality Reserve 与 Exploration Reserve 分账；所有交易追加式、幂等、可对账 |
| ADR-038 | 新增领域对象统一 Schema 信封和数据所有权 | 明确 owner、版本、状态、命令、审计、来源/权利引用和兼容策略 |
| ADR-039 | DAG/Gate 对正式轨道和探索分支强隔离 | 探索候选需 Promote 才创建正式版本；失败/停止只作用于相关分支 |
| ADR-040 | RBAC + ABAC + Capability Token 与职责分离统一执行 | 入口不得自判授权；Agent 不得自批；Gate 和 Rights 需独立职责 |
| ADR-041 | 预算账本、CostLedger、QuotaReservation 分离且关联 | 原子预留、幂等提交、租约释放、供应商未知结果先查询再重试 |
| ADR-042 | 角色、视觉计划、探索分支、QC 会话状态机显式化 | 只允许定义的状态转移；变更产生新版本；终态不可覆盖 |
| ADR-043 | 四个核心 SKILL 与细粒度 Skills/服务代码分层 | 核心 SKILL 描述领域流程；细粒度 Skill 描述任务；服务/Worker 执行代码 |
| ADR-044 | 外部 Skill API 延后到受控 P1，第三方生态为 P2 | 内部 Runtime 先上线；外部 API 范围白名单、限流、Token、审计和撤销 |

历史工程契约整改决议：架构不再以新增功能数量作为成熟度标准，而以数据/状态/权限/预算/DAG/Gate 的契约完整性和自动化证据作为冻结依据。任何未通过验证的模型能力、检测器判断或外部 Skill 都必须显式标记 UNVERIFIED/NOT_ENABLED，不得以文档描述冒充实现完成。

## 附录 D：产品实施、双模式计费与商业边界参考

本章定义核心生产闭环、双模式计费及其分阶段启用边界，不以单人开发周期或固定交付月份限制长期产品能力。长期目标架构描述平台最终能力；首发核心范围、后续增强和商业化能力分别按产品价值、风险、依赖关系与验证证据启用。不得将架构预留误认为功能已经上线。

### D.1 版本定位与优先级双层定义

V2.0 继承前序版本的长期领域模型、统一质量红线、项目/镜头策略解耦、DAG/Gate、白模控制信号、社区展示治理和双模式计费设计。本章不设置固定时间盒；产品按核心生产闭环、分享能力、组织内社区、公开社区与商业化能力分阶段建设。百集级能力仍须通过持续生产和负载验证，不因架构预留而视为已验证。

| **标签** | **定义** | **实施要求** |
| --- | --- | --- |
| P0-ARCH：架构预留 | 必须冻结长期兼容所需的字段、接口、状态边界和权限原则，但不要求实现完整功能。 | 例如 PLATFORM_PAID、ControlSignalAsset、多路线扩展的接口边界。不得把“字段已存在”表述为功能已上线。 |
| P0-MVP：首版必做 | 首发阶段必须形成可运行、可测试、可演示的端到端能力。 | 真实 USER_OWN_KEY 调用、剧本拆镜确认、单一路线生成、资产版本、核心 QC、人工 Gate、交付追溯。 |
| P1：增强能力 | 只有 P0-MVP 主链稳定后，才根据剩余时间择优完成。 | 白模 PoC/有限路线、手动 CEE、角色资产增强、平台代付模拟等。 |
| P2：商业化与规模化 | 不进入首发产品范围，保留架构位和 ADR。 | 平台代付真实支付/充值/退款/开票、外部 Agent API、复杂配额、无限画布及企业级高可用。 |

验收原则：P0-ARCH 以 Schema/接口/负向测试证明“未来可扩展且当前不会误启用”；P0-MVP 以运行中的端到端证据证明“当前确实可用”。二者必须分开登记。

### D.2 首发核心生产闭环与扩展边界

| **能力域** | **Phase 1 最小交付** | **边界/不得做** |
| --- | --- | --- |
| 核心生产 | 项目创建 → 剧本导入 → AI 拆镜草案 → 人工确认 → ShotSpec/ShotVisualPlan → 生成有效 ConstraintPack（如适用；无锁定约束的镜头标记 NOT_APPLICABLE）→ 单一路线生成 → 核心 QC → 人工 Gate → DeliveryManifest。 | Phase 1 唯一主硬门；辅助域不能阻塞这条主链。 |
| 社区与分享 | 只读分享/审片链接；受限 ShowcaseSnapshot；只保留内部审核记录或组织内封闭灰度，不开放公开审核发布。 | 不做公开社区、公开审核发布、排行榜、公开创作者主页和市场。 |
| 视频反推 | 获准视频上传/链接→逐秒/逐镜头分段→关键帧→图像/运动提示词分离→光影/音频描述→人工确认→创建 ShotVisualPlan 草案。 | 默认 REFERENCE_ONLY；不得直写 ShotSpec/ConstraintPack/Canon；无授权进入 QUARANTINED。 |
| 资产知识 | 组织内只读资产目录、版权状态查询、人工复用申请。 | 不做跨组织自动复用；批准后创建新 Project/ShotRevision，不直接 Fork。 |
| 客户审片与交付 | 客户只读门户、时间码批注；DeliveryManifest 可追溯/可重建。 | 客户批准不等于内部终检；客户批注不能直接改变 QC/Gate。 |
| 学习 | 作品拆解页和新人 Onboarding。 | 课程、认证、挑战赛后续阶段再启用；学习不授予生产权限。 |
| 开放生态 | 冻结 Manifest、签名、沙箱、网络/文件访问、CapabilityToken 作用域和撤销契约。 | 不开放市场 UI，不允许扩展包直写 Gate/Canon/CostLedger。 |
| 计费 | USER_OWN_KEY 真实可用；CostLedger 用量/成本快照；PLATFORM_PAID 保留接口。 | 不接真实充值/支付/退款/发票/月结；BYOK 不受平台余额拦截。 |

首发核心生产闭环建议通过代表性试点项目验证：覆盖普通镜头、角色连续性镜头、技术风险镜头和至少一条真实视频生成路线。样本规模由实际项目类型、成本和风险覆盖确定；试点通过不等同于百集级产能或公开社区运营能力已经验证。

### D.3 五类核心角色的端到端用户旅程

Phase 1 只围绕导演、制片、技术导演/TD、QC、版权合规五类角色设计关键动作。单人开发不意味着首版必须建成五套独立工作台；可以在同一工作台中通过角色视图、权限和任务队列体现职责。

| **角色** | **关键旅程与操作** | **必须人工确认/证据** | **建议采集的人工作业指标** |
| --- | --- | --- | --- |
| 导演 | 项目意图 → 审阅 AI 拆镜草案 → 调整镜头表达 → 确认 ShotSpec/ShotVisualPlan → 审阅候选与艺术性偏差。 | ScriptLock/ShotRevision 确认；锁定字段、开放字段和创意偏差说明。 | 拆镜确认分钟/镜头；镜头意图修改次数；候选审阅分钟/镜头。 |
| 制片 | 创建项目 → 设定周期/预算参考 → 选择生产路线 → 审批资源申请 → 跟踪进度和成本估算 → 确认交付范围。 | 项目范围、预算估算、异常资源差距报告、交付清单。 | 项目配置耗时；预算调整次数；交付等待时间。 |
| 技术导演/TD | 配置凭证与 Endpoint → 验证模型连通性 → 检查路线能力 → 处理生成异常 → 核验资产与控制信号。 | 凭证可用性、能力矩阵证据、生产路线选择、技术异常处置记录。 | 模型接入耗时；失败定位时间；路线配置返工次数。 |
| QC | 打开候选与参考双屏 → 定位时间码/帧区间 → 标记质量问题 → 选择人工结论 → 创建返工或建议 Gate。 | asset_version、frame_range、QC dimension、severity、evidence 和人工结论。 | 人工复核分钟/镜头；误报率；返工定位耗时。 |
| 版权合规 | 核验参考素材、角色/场景资产、模型/供应商条款和交付使用范围。 | rights_manifest、来源、授权范围、有效期/限制；未知来源进入 QUARANTINED。 | 素材核验耗时；待补证数量；版权阻断问题关闭时间。 |

核心生产流程统一为：项目创建 → 剧本导入 → AI 拆镜草案 → 人工确认 → ShotSpec/ShotVisualPlan → 生成有效 ConstraintPack（如适用；无锁定约束镜头标记 NOT_APPLICABLE）→ 路线能力检查 → 生成任务 → QC 双屏 → 人工 Gate → 交付 Manifest。所有 AI/Agent 操作均通过统一领域命令，不能直接写正式状态；分享与社区发布另走 Showcase/Community 发布门禁，不等同于生产 Gate。

| **Gate** | **最小必填证据** | **允许结果/失败处理** |
| --- | --- | --- |
| Gate 0：项目准入 | 项目范围、负责人、素材来源清单、部署/计费模式、基础模型配置。 | PASS 才进入创作；权利来源不明的素材隔离，不自动成为正式参考。 |
| Gate 1：剧本与拆镜确认 | 剧本版本、AI 拆镜草案、导演确认记录、正式 ShotRevision。 | DRAFT 不得进入正式生产；拒绝/修改后产生新版本。 |
| Gate 2：镜头生产准备 | ShotSpec、锁定/开放字段、参考资产版本、版权状态、路线能力声明。 | 能力缺口明确标注；未验证控制能力不能作为已支持能力承诺。 |
| Gate 3：生成与资产入库 | ModelInvocation、billing_mode 快照、Task 结果、AssetVersion、来源/价格快照。 | 外部结果未知先查询；失败可有限重试；不得丢失成本或资产来源记录。 |
| Gate 4：QC 与交付 | QCReport、人工结论、缺陷关闭状态、Rights 状态、交付 Manifest。 | 质量/版权阻断项未关闭不得交付；艺术 Override 需有授权和审计。 |

人工工时 SLA 不在没有代表性生产数据时虚构。试点镜头应记录各 Gate 的开始/结束时间、操作者、操作类型和返工轮次，用 P50/P80 形成基线；先建立可测量性，再设效率目标。

### D.4 双入口一致性与 Agent 产品级拦截

导演工作台与内部 Skill Runtime 必须调用同一 Application Service 和领域命令。入口身份可以不同，但不能出现 UI 能通过而 Agent 绕过、或两套入口分别推进任务状态的情况。

| **测试 ID** | **测试方式** | **预期结果** |
| --- | --- | --- |
| DUAL-001 | 同一 CreateShotDraft 命令分别由工作台和 Skill Runtime 提交，输入、scope、expected_version 相同。 | 归一化后的业务校验结果、领域事件类型/关键 payload、DAG 后续节点和资产副作用一致；审计主体/入口字段按实际调用者不同。 |
| DUAL-002 | Skill 输出拆镜结果后检查工作台显示。 | 状态显示为 DRAFT/PROPOSAL，标注生成来源、Skill/模型版本和待确认动作；未确认不得显示为正式锁定。 |
| DUAL-003 | Agent 尝试 ApproveGate、ModifyCharacterCanon、BudgetTransfer 或修改正式锁定字段。 | 服务端返回结构化拒绝码；不产生权威状态变化；生成安全审计事件。 |
| DUAL-004 | 工作台与 Agent 对同一版本并发提交修改。 | expected_entity_version 冲突被检测；不得静默覆盖；由用户选择重载/合并并生成新版本。 |
| DUAL-005 | 重复提交相同 idempotency_key。 | 返回同一命令结果或既有任务引用，不重复创建生产任务和重复调用供应商。 |

产品表现：Agent 生成内容始终标记“AI 草案/待人工确认”；审批按钮仅对具有对应权限的人类主体开放。Agent 不得自批自用，不得通过管理接口、脚本或 Worker 绕过服务端 Gate。

### D.5 双模式计费与 Model Gateway 架构（P0 核心决策）

平台同时支持 USER_OWN_KEY 与 PLATFORM_PAID 两种 billing_mode。双模式属于长期产品能力；实施阶段不同：USER_OWN_KEY 为 P0-MVP 必须完整实现，PLATFORM_PAID 为 P0-ARCH 保留接口、P1 做模拟/沙箱验证、P2 才允许真实资金交易和统一结算。不得把“预留”描述成“已支持商业代付”。

| **模式** | **密钥来源与结算关系** | **MVP 行为** | **正式能力阶段** |
| --- | --- | --- | --- |
| USER_OWN_KEY | 用户项目级凭证/自有 Endpoint；用户直接与模型厂商结算。settlement_type=USER_DIRECT。 | 真实启用：配置、测试、调用、用量统计、单价快照和成本估算；平台不扣费、不充值、不做平台余额拦截。 | P0-MVP；私有化部署必须可用。 |
| PLATFORM_PAID | 平台密钥池；平台与供应商结算，再按约定向客户提供统一账单。settlement_type=PLATFORM_SETTLED。 | 只冻结字段/接口/权限；P1 可用模拟凭证和模拟余额完成沙箱流程，不接真实支付。 | P2 正式启用：充值/支付/授信、余额预留、账单、退款、开票和对账。 |

#### D.5.1 Model Gateway 调用契约

Model Gateway 的调用上下文至少包含：project_id、shot_id、provider、model、model_version、billing_mode、credential_ref、endpoint_ref、price_version、idempotency_key、task_id 和 data_policy_ref。密钥明文不得写入 Task、ModelInvocation、CostLedger、应用日志或异常堆栈；调用记录只保存 credential_ref。

推荐的调用上下文示例（示意，不可直接作为生产密钥或真实凭证）：

```
billing_mode: USER_OWN_KEY
credential_ref: cred_project_x_provider_y
endpoint_ref: endpoint_project_x_provider_y
price_version: price_2026_11
settlement_type: USER_DIRECT
idempotency_key: invoc_...
```

用户自有密钥的配置至少支持 provider、model、API Key、Endpoint、单价/计价单位、币种、启用状态和凭证引用。API Key 通过凭证存储加密保存，支持脱敏显示、轮换、撤销和权限隔离。自定义 Endpoint 需受部署策略约束，避免任意地址请求造成 SSRF 或访问内网敏感服务。

每次 Task/ModelInvocation 创建时冻结 billing_mode、credential_ref、endpoint_ref、model_version 和 price_version 快照。用户可以在后续任务间显式切换模式；运行中的任务不得中途切换密钥来源或结算模式。USER_OWN_KEY 调用失败时严禁静默回退到 PLATFORM_PAID。

在 USER_OWN_KEY 模式下，平台记录用量与估算成本，不从平台账户扣款、不因平台余额不足拦截供应商调用。平台自身的质量 Gate、权限和用户明确设置的业务审批仍然有效，但不得伪装成平台支付/余额控制。供应商自身的额度限制由供应商执行。

#### D.5.2 CostLedger 口径与字段

CostLedger 是用量/成本事实记录，不是支付账本。建议新增 settlement_type、billing_mode、credential_ref、provider、model、model_version、usage_metrics、unit_price_snapshot、price_version、estimated_cost、actual_cost、actual_cost_source、currency、task_id、provider_job_id 和 idempotency_key。金额使用 Decimal 与 ISO 4217 币种，不使用浮点数。

| **字段/口径** | **USER_OWN_KEY** | **PLATFORM_PAID（未来）** |
| --- | --- | --- |
| settlement_type | USER_DIRECT | PLATFORM_SETTLED |
| estimated_cost | 按用户配置单价和用量估算；必须带 price_version。 | 按平台价格目录和用量估算，供预算预留参考。 |
| actual_cost | 只有导入/取得可信供应商账单或可靠用量计费证据时才填写；否则为空。 | 按供应商账单/合同计费规则对账后填写。 |
| 资金动作 | 不创建平台扣款、充值、退款或余额冻结记录。 | P2 独立 Billing/Payment Ledger 处理；CostLedger 与其通过业务引用关联。 |
| 异常处理 | 统计失败不应改写供应商结算关系；费用估算失败不得伪造实际费用。 | 未知供应商提交先查询；以幂等交易处理预留、结算、释放和退款。 |

历史单价修改不回写旧调用；每次调用以单价快照复算。平台不能把按用户配置计算的 estimated_cost 标注为供应商 actual_cost，也不能把用户直接付给供应商的费用列为平台收入。

#### D.5.3 平台代付模块隔离与未来交易流程

P2 商业模块独立封装：BillingAccount、UsageSettlement、BillingStatement、Payment、Order、Recharge、Invoice、Refund、ProviderReconciliation。它们不得成为 Orchestrator、DAG、QC、Asset 或交付主链路的强制依赖。私有化部署可不安装/关闭整个商业模块，不影响 USER_OWN_KEY 生产。

未来 PLATFORM_PAID 的标准交易顺序：账户状态/额度校验 → 预算/余额预留 → Model Gateway 使用平台密钥调用 → 取得供应商结果和费用证据 → 供应商成本对账 → 客户结算 → 释放未使用预留/处理差额。每一步必须幂等、可重放、追加式记账；未知结果先查询，禁止盲目重试和重复扣款。

平台代付与软件授权费分离。模型调用成本不做隐性加价；软件授权费单独展示与结算。用户可随时在后续任务切换回 USER_OWN_KEY，不得因历史充值或供应商绑定造成锁定。真实充值、余额有效期、退款、发票和税务规则须按实际运营地区完成专业合规审查后才可上线，本版本不承诺具体法定期限或退款规则。

内容责任与数据处理边界必须透明：用户对输入素材的授权和生成内容的使用负责；平台仍须履行其适用的数据安全、保密和服务义务。云端 Gateway 在转发调用时可能处理提示词、参考资产和生成结果，不能笼统承诺平台从未接触内容。私有化部署由用户控制数据面和密钥面，具体取决于部署拓扑。

#### D.5.4 离线私有化强制规则

```
DEPLOYMENT_MODE = OFFLINE_PRIVATE
ALLOWED_BILLING_MODES = [USER_OWN_KEY]
DEFAULT_BILLING_MODE = USER_OWN_KEY
PLATFORM_BILLING_REQUIRED = false
EXTERNAL_BILLING_DEPENDENCY = false
```

OFFLINE_PRIVATE 必须只支持 USER_OWN_KEY；不依赖平台密钥池、支付、充值、统一结算或平台计费服务器。Endpoint 可指向经本地管理员允许的互联网供应商或用户自有内部模型服务；纯离线且无外部网络时，只能调用本地可达模型，不得承诺调用公网供应商。

### D.6 MVP 预算交互简化与长期账本兼容

长期目标架构保留 Production、Exploration、Quality、Contingency 四类成本/预算分类；MVP 不要求一线制片操作四个独立资金账户。首版产品采用“一个项目预算池 + 镜头级申请/审批 + Quality Reserve 受保护标记 + 成本分类标签”。

| **MVP 产品对象** | **用户体验** | **底层实现边界** |
| --- | --- | --- |
| ProjectBudgetPool | 一个项目预算总览：估算额度、已预留、已发生估算成本、可用参考额度。 | 作为项目级预算视图/策略对象；USER_OWN_KEY 下不是平台托管余额，不执行平台扣款。 |
| QualityReserveProtected | 质量储备的受保护金额/比例/上限标记，专用于必要 QC 修复与质量保障。 | 底层通过预算分类与保护规则实现；探索成本不得侵占该额度。 |
| CostCategory | PRODUCTION / EXPLORATION / QUALITY_REPAIR / CONTINGENCY 标签分类。 | 先写 CostLedger 标签和价格快照，不做复杂跨账户转拨。 |
| ShotBudgetRequest | 镜头级预算估算、理由和制片确认。 | 只记录申请/批准和估算；BYOK 模式不等于平台代扣或模型调用余额门禁。 |

MVP 预算界面以预警、预测和可解释成本为主。USER_OWN_KEY 模式下平台不得因自身余额或成本估算拦截模型调用；用户可以看到项目预算参考、费用预估和风险提示。PLATFORM_PAID 未来上线后，才增加账户余额、额度预留和硬性超额阻断。Quality Reserve 的质量用途红线保留，不因产品端简化而允许探索占用。

### D.7 白模首发产品边界与能力矩阵

白模在长期架构中继续作为跨路线控制信号层；但 MVP 不承诺所有镜头均使用白模，也不把白模消费能力写成视频模型的普遍能力。建议在早期验证阶段并行完成 PoC，尽早暴露技术风险，但不阻塞主生成路线。

| **维度** | **首发规则** |
| --- | --- |
| 优先适用镜头 | 静态构图、单主体、低至中复杂度相机运动、空间关系清晰的镜头。 |
| 暂不承诺全自动 | 多人复杂交互、精细手部动作、复杂物理、强表演、多段连续运动；改走图生视频、实拍合成、传统 CG 或人工修正。 |
| 能力矩阵字段 | signal_type、reference_role、controlled_dimensions、uncontrolled_dimensions、shot_type_scope、provider/model/version、adapter_version、validation_date、evidence_ref、rights_status、capability_status。 |
| 能力状态 | UNVERIFIED / VERIFIED / DEGRADED / REVOKED；只有 VERIFIED 且权利状态通过的维度才能进入生产路由。 |
| 3D 资产权利 | 记录模型/场景/材质/动作来源、许可范围、商业使用与再分发限制；未知授权进入 QUARANTINED。 |
| 技术 PoC | 对比 Blender 脚本化与备选方案的自动化、坐标/相机导出、几何检查、资产许可和迁移成本；用静态单主体、低中复杂相机运动样本验证一个消费模型。 |

若 PoC 未证明某个控制维度可被消费，系统必须将该维度标记 UNVERIFIED，不进入生产路线、不向客户承诺。白模资产、ShotSpec、ControlSignal Schema 和 Adapter 边界仍可保留，后续再迭代。

### D.8 分阶段实施工作包摘要（非权威；与 31.2 不一致以 31.2 为准；CI 校验 phase_map.yaml）

实施原则：不设固定月份和个人工期承诺。按依赖关系分批交付；每个阶段以可运行能力、数据证据、风险关闭和回滚能力验收。下表仅按业务工作包归纳实施顺序，不定义或覆盖平台 Phase 0—4；统一阶段映射与验收门槛只见第 31.2 节。外部模型接口、内容质量、供应商政策和运营合规变化均可触发路线调整，但不得压缩质量、权限和版权门禁。

| **实施工作包（摘要；Phase 以 31.2 为准）** | **主要工作** | **阶段验收 / 决策门** |
| --- | --- | --- |
| 核心生产闭环 | 冻结核心 Schema/命令；模块化单体、数据库、文件存储、任务状态；USER_OWN_KEY Model Gateway；剧本导入、AI 拆镜草案、人工确认；ShotSpec/ShotVisualPlan；单一生产路线、AssetVersion、DAG、核心 QC、人工 Gate 和交付 Manifest。 | 能从项目创建完整走通剧本—镜头—生成—QC—Gate—交付；调用、密钥、资产、成本和人工决策可追溯。 |
| 分享能力 | 冻结并实现 ShowcaseSnapshot、CommunityPost 最小契约；组织内只读分享画布/审片链接；内容类型标签、短期授权链接和撤销。 | 分享不改变生产 Gate；快照只读、不可变；内容类型在展示页清晰可见；访问和撤销可审计。 |
| 组织内社区 | 只读展示画布、组织发布审批、水印/脱敏、收藏评论、举报下架、作品拆解视图；复用申请创建新 Project/ShotRevision 并重新校验权限/版权/Canon/预算。 | 组织成员只能按作用域访问；无直接 Fork；所有互动、审批、下架和复用申请有审计链。 |
| 公开社区与市场 | TV Show 式首页、创作者主页、公开申请复用、模板市场、排行榜和社区推荐；推荐与 Pattern 治理协同，提供新颖性保护。 | 公开内容必须通过发布门禁；权利、隐私和内容类型披露充分；推荐可解释、可治理、可降权/撤回。 |
| 商业化与规模化 | 按运营地区和合规能力开放 PLATFORM_PAID 真实结算；扩展外部 Agent API、多路线生产、组织治理和运行可靠性。 | 真实资金动作通过独立账务/支付/对账系统；私有化 USER_OWN_KEY 主链路不受影响。 |

核心生产风险门：若真实模型调用、可追溯资产、QC/Gate/交付尚未形成闭环，暂停社区公开化、复杂配额和高级自动化等扩展项，优先完成生产主链路；该决策门按实际迭代评审触发，不绑定周数。

### D.9 单人开发工程策略与明确不做清单

| **领域** | **分阶段实施策略** | **明确不做** |
| --- | --- | --- |
| 架构形态 | 模块化单体；统一 Application Service；一个主要数据库；清晰模块数据所有权。 | 不拆十几个微服务；不提前引入 Kubernetes/服务网格/复杂多租户。 |
| 工作流 | 持久化任务状态、最小 DAG、有限重试、查询未知结果、关键 STALE。 | 不自建复杂分布式调度平台；Temporal 不作为 MVP 前置。 |
| Agent | 内部 Skill Runtime 与四个核心 SKILL.md；Skill 只能调用领域命令。 | 不做第三方 Skill 市场、不开放公网外部 Agent API。 |
| 生产路线 | 一个供应商/模型适配器和一个可用主路线；模型能力以实测为准。 | 不做多模型自动择优和全路线并行。 |
| 资产与记忆 | AssetVersion、版权来源、基础 CharacterCard/Canon（时间允许）。 | 不做大型资产市场和自动长期记忆训练平台。 |
| 运营 | 项目成本估算、错误日志、最小指标。 | 不做全自动 RCA、复杂企业配额、完整 SLO/灾备平台。 |
| 商业化 | USER_OWN_KEY 真实可用；PLATFORM_PAID 字段/接口预留与模拟验证。 | 不接充值、真实支付、退款、发票、月结和代付真实密钥池。 |

工程原则：优先完成可演示、可重复、可恢复的端到端闭环；长期架构按领域边界保留扩展能力，具体模块按产品阶段启用。新增模块必须说明业务价值、依赖、状态边界、运维成本与验收证据；不以短期时间盒作为架构删减理由。

### D.10 产品 KPI 与阶段验收证据

所有指标必须给出公式、数据源、统计窗口和适用项目/内容范围。代表性试点建立基线后再设 SLA；没有实际数据时不得伪造目标值。

| **指标** | **建议计算口径** | **阶段验收必须留下的证据** |
| --- | --- | --- |
| 首个项目交付周期 | 项目创建至交付 Manifest 通过的自然时间/工作时间。 | 项目时间线、Gate 时间戳、阻塞原因和交付版本。 |
| 人工复核分钟/镜头 | 拆镜确认、QC、Override、版权检查等人工耗时按镜头归集。 | 操作者、起止时间、审核类型、镜头 ID；至少有 5—10 镜头样本。 |
| QC 误报率 | 人工判为 FALSE_POSITIVE 的检测问题数 / 人工复核检测问题总数。 | 检测器版本、问题证据、人工结论；样本量不足时标注置信度不足。 |
| 返工定位耗时 | 发现问题到定位任务/资产/帧区间并创建返工任务的时间。 | 缺陷记录、frame_range、返工 Task 和时间戳。 |
| CEE 候选采纳率 | 被 Promote 的探索候选数 / 完成评审的候选总数。 | 仅在启用人工触发 CEE 后统计；不要求 MVP 阶段追求高采纳率。 |
| 探索预算消耗率 | 探索类估算成本 / 项目探索预算参考额度。 | 按 CostLedger cost_category 和 price_version 计算；BYOK 仅为估算。 |
| 预算超支率 | 实际/已知费用或估算成本相对项目预算参考值的偏差。 | 区分 BYOK 估算与平台真实结算；不得混算。 |
| 质量逃逸率 | 交付后发现的严重质量问题数 / 已交付镜头数。 | 交付后问题单、严重级别和归因。 |
| Override Rate | 人工 Override 数 / 可 Override 的 QC 决策数。 | 按原因、授权人、镜头和质量结果分层。 |
| Agent 越权拦截率 | 被拒绝的未授权命令数 / 测试集中未授权命令数。 | 负向测试报告、拒绝码、无状态副作用证据。 |
| 双入口一致性通过率 | 通过的跨入口一致性测试数 / 总测试数。 | 领域校验、事件、DAG 副作用和审计字段对照报告。 |
| 白模控制消费成功率 | 成功满足已声明控制维度的样本数 / 已验证消费样本数。 | 必须绑定模型/版本/Adapter、验证日期、维度误差和样本类型。 |
| 平台代付就绪度 | 模拟预留、调用、结算、释放、失败查询和幂等测试通过情况。 | 仅用于 P1 沙箱；不代表已支持真实付款或开票。 |

核心产品验收底线：至少一条真实 USER_OWN_KEY 生产路线、完整资产来源和版本追溯、核心 QC 与人工 Gate、可生成交付包；离线私有化不依赖平台商业计费服务。试点项目的样本规模与覆盖范围由阶段验收方案确定。

### D.11 双模式专项验收矩阵

| **测试 ID** | **场景** | **预期结果** |
| --- | --- | --- |
| BILL-001 | 项目配置 USER_OWN_KEY 并使用项目凭证完成真实模型调用。 | 成功调用；ModelInvocation 保存 credential_ref 和 billing_mode 快照；无明文密钥日志。 |
| BILL-002 | 用户修改单价配置后查询历史调用。 | 历史调用仍使用原 price_version；新调用使用新版本。 |
| BILL-003 | USER_OWN_KEY 调用时平台账户余额为零/不存在。 | 不因平台余额拦截；调用费用由用户与供应商直接结算；平台只记录用量和估算成本。 |
| BILL-004 | 用户自有密钥失效或调用超时。 | 返回凭证/供应商错误；不得自动切换到 PLATFORM_PAID。 |
| BILL-005 | 运行中的任务发起计费模式切换。 | 当前任务继续使用创建时快照；模式切换只影响后续新任务。 |
| BILL-006 | OFFLINE_PRIVATE 环境关闭/不存在平台计费服务。 | USER_OWN_KEY 仍可运行；PLATFORM_PAID 不可启用；无平台支付依赖。 |
| BILL-007 | 平台代付沙箱模拟余额预留、供应商模拟调用、结算和释放。 | P1 模拟流程幂等；不产生真实支付、真实充值或真实平台扣款。 |
| BILL-008 | 普通用户在 MVP 尝试启用真实 PLATFORM_PAID。 | 因功能未启用而被明确拒绝；不生成充值/订单/扣款记录。 |
| BILL-009 | 日志与异常采集包含 API Key 字符串。 | 日志脱敏或拒绝写入；安全测试确认凭证无法通过普通日志或 Task 读取。 |
| BILL-010 | 成本估算数据与供应商实际账单数据混用。 | 系统区分 estimated_cost / actual_cost 和 actual_cost_source；没有可信账单来源不得标记 actual。 |

### D.12 ADR-045～051：产品实施与双模式计费决策

| **ADR** | **决策** | **关键约束** |
| --- | --- | --- |
| ADR-045 | USER_OWN_KEY 是默认且必须可用的 P0 模式。 | 用户直接向供应商结算；平台只记录用量/估算成本；私有化必须支持。 |
| ADR-046 | Model Gateway 使用 billing_mode 双模式抽象。 | USER_OWN_KEY / PLATFORM_PAID；每个任务冻结调用快照；禁止静默切换密钥来源。 |
| ADR-047 | CostLedger 区分 settlement_type 与估算/实际成本。 | USER_DIRECT / PLATFORM_SETTLED；估算不得冒充供应商实际账单；CostLedger 不等于支付账本。 |
| ADR-048 | PLATFORM_PAID 商业结算模块独立隔离，真实功能列为 P2。 | MVP 只保留接口/字段；P1 可沙箱模拟；支付、充值、退款、发票和真实统一结算不进入首发产品范围。 |
| ADR-049 | OFFLINE_PRIVATE 强制 USER_OWN_KEY。 | 不依赖平台密钥池、支付和平台计费服务器；商业模块可整体关闭。 |
| ADR-050 | MVP 预算产品端简化为项目预算池和成本标签。 | 质量储备受保护；探索/应急先作为标签与软限额；长期账本兼容保留；BYOK 不做平台余额拦截。 |
| ADR-051 | 优先采用模块化单体和可验证的生产闭环迭代。 | 一个主生成路线、5—10 个代表性镜头；P1/P2 不能阻塞 P0-MVP；白模 PoC 不作为首发硬门槛。 |

V2.0.1 产品决议：制作、资产与知识中枢、社区展示构成平台三极；核心生产闭环是首发产品硬门，辅助产品域按统一 Phase 0—4 阶段表启用。USER_OWN_KEY 必须独立可用，PLATFORM_PAID 与私有化主链路隔离；社区、反推、资产复用和客户交付均不得绕过权限、版权、Canon、预算和质量门禁。
