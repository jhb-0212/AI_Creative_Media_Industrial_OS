# ADR-R43：画布参考边界与可借鉴机制裁定

- 状态：DECIDED（项目所有者 2026-11-23 拍板；固化边界与原则，不引入任何画布库、不激活画布实现）
- 日期：2026-11-23
- 触发：项目所有者在否决 ComfyUI 接入后，要求横向调研高星画布/工作流/编排项目的实现，判断哪些可借鉴
- 关联：Phase 0 冻结基线 V205-R15（第 27、2921、3334–3336、3715 行画布边界）、ADR-R42（资产登记 Saga 的补偿路径）
- 指纹：本正文不内嵌自身哈希；若纳入冻结产物，按 append-only 口径另行采集登记
- 范围：本 ADR 是**调研与边界裁定**，不改契约、不改数据模型、不引入依赖。任何画布实现仍按基线 Phase 4 / P2 另立。

---

## 1. 裁定摘要

| 编号 | 议题 | 裁定 |
|---|---|---|
| D1 | 节点图画布是否进平台 | **不进**。ComfyUI 等生成节点图是平台外的生成配方，平台只通过 `workflow_id` 引用；在平台内再存一份等于同一事实两个权威源。 |
| D2 | 平台画布的形态 | **镜头 DAG 只读视图 + 受控输入表单 + 领域命令入口**。不是节点编辑器。复杂节点图折叠为受限表单（ComfyUI App Mode 形态），再加平台独有的角色权限、状态机、审计证据链。 |
| D3 | 可借鉴的东西 | 只借鉴**机制与架构纪律**：双表示分离、多视图共享单一领域模型、引擎/模型/渲染/执行分层、装配期类型校验、失败路径独立建模、按变化频率切分失效域、缺失值保持 unknown、静默失败必须可机检。 |
| D4 | 明确不借鉴 | 不引入 Litegraph / tldraw / CRDT / 内嵌元数据签名；不抄 `.passthrough()` 式的未知字段放行；不抄「未知版本按旧版校验」；不抄 n8n 的「继续失败 / 重跑整个执行」到有不可逆约束的 Saga。 |
| D5 | 垂类直接同类的定位 | Open Shot Designer 与影策（open-ai-canvas）证明了本平台的领域形态可行，但两者都**明确不是多租户治理系统**（前者零后端 GPLv3，后者默认个人/本地/可信环境）。它们是功能参照物，不是架构参照物。 |
| D6 | 与 ADR-R42 的关系 | 编排/血缘类项目的失败处理机制可作为 Saga 补偿路径的**设计输入**，但不改变 R42 已拍板的 D1–D7；补偿仍走 OpLog 追加，不做原地回滚。 |

任一条被以「先做个画布再说」绕过，基线第 3334–3336 行的边界禁令即被破坏。

---

## 2. 调研范围（四轮，按品类）

星数与许可为检索时点（2026-11）的第三方快照，仅用于判断社区规模与许可风险，不作为事实源。

### 2.1 生成节点画布

| 项目 | 架构要点 | 许可 |
|---|---|---|
| ComfyUI 前端 | Vue 3 + Pinia + Vite + TS；画布基于 Litegraph；图模型抽为独立包 `@comfyorg/graph-model`（`serialisation.ts`、`workflowSchema.ts`、`linkTopology.ts`、`SubgraphEventMap.ts`、`appMode.ts`） | 开源 |
| ComfyUI UI 格式 vs API 格式 | 保存格式带 `nodes/links/位置/颜色/分组`，给人编辑；API 格式 `节点id → {class_type, inputs}`，给机器执行，UI 元数据丢弃 | — |
| 产物元数据回灌 | PNG `tEXt` / WebP EXIF / MP4 tag 内嵌 `workflow` + `prompt`；文档自述「不是数字签名」「不含模型文件与输入资产」「重新编码会丢」「视为不可信输入」 | — |

### 2.2 工作流 / 图编辑 SDK

| 项目 | 架构要点 | 许可 |
|---|---|---|
| xyflow (~38k) | `@xyflow/system` 共享图引擎，React/Svelte/Vue 是三层封装；节点/边受控状态，`changes.applyTo(nds)` 汇总变更 | MIT |
| tldraw (~51.5k) | store 驱动 + Editor API + `@tldraw/sync` 自托管协作；starter kit 自带 Workflow / Image pipeline | **生产需 license key** |
| n8n | 每个工作流可配独立 **error workflow**；错误数据带 `execution.id` / `retryOf` / `lastNodeExecuted` / `error.stack` | Sustainable Use License |
| BaklavaJS | `@baklavajs/core` + `interface-types` + `engine` + `renderer-vue` 四包分离；端口声明类型，非法连线建不起来 | MIT |
| Rete.js (12.3k) | 可视化编程框架，节点有类型化 input/output，渲染插件支持 React/Vue/Angular/Svelte | MIT |
| LogicFlow (11.8k) | 滴滴出品，`@logicflow/core` 提供画布 + 节点/边模型 + 事件 + 主题；文档是 `{nodes, edges}` 纯 JSON；自带浏览器端自执行引擎，支持与 BPMN/Turbo 数据结构转换 | Apache-2.0 |
| JointJS (5.4k) | SVG，MVC 分离；port 级连接规则由库强制；虚拟渲染支撑大图；undo/redo 把级联删除合并为一个原子操作；布局/路由拆为独立包 | MPL-2.0 核心 + 商业 + |
| AntV G6 / X6 | G6 是关系图可视化引擎（布局/分析/交互）；X6 是图编辑引擎，插件丰富 | 开源 |
| Cytoscape.js / maxGraph | 前者图论分析与网络可视化；后者是归档 mxGraph 的 Apache-2.0  successor | 开源 |
| yFiles / GoJS | 商业自动布局与大图渲染，许可成本明确 | 商业 |

### 2.3 无限画布

| 项目 | 架构要点 | 许可 |
|---|---|---|
| Excalidraw (136k) | Canvas 2D；`@excalidraw/excalidraw` npm 包；开放 `.excalidraw` JSON；local-first 存浏览器；只读分享链接 + E2EE 协作；用 fractional indexing 排序 | MIT |
| AFFiNE (75.9k) | 文档/白板/表格「超融合」于同一模型；BlockSuite 统一 block；y-octo（Rust CRDT）+ OctoBase 本地优先同步 | MIT(CE) / EE 未发布 |
| Drawnix (15.1k) | 思维导图 + 流程图 + 自由画，Mermaid/Markdown 转图形 | 开源 |
| Konva (15k) | 底层 Canvas 2D 框架，只做交互图元 | MIT |

Excalidraw 性能演进的共同主题：把 `zoom`、`snapLines`、`suggestedBinding` 这类每次 pointermove 都变的字段移出 `UIAppState`，避免整棵 UI 重渲染。

### 2.4 DAG 编排 / 状态机 / 血缘 / 事件溯源

| 项目 | 架构要点 | 许可 |
|---|---|---|
| Airflow (46.7k) | Python DAG，任务中心；生态最广；scheduler 在大规模 mapped task 下有压力 | Apache-2.0 |
| Dagster (16.1k) | **资产中心**，按产出的 data asset 定义流水线，内建 lineage / freshness / quality / 成本与影响半径 | Apache-2.0 |
| Prefect (23.8k) | 动态 Python 工作流，不强制静态 DAG | Apache-2.0 |
| Temporal (23.7k) | **event-history replay** 的 durable execution；事件不可变 ⇒ Update 用 Speculative Workflow Task + Message Protocol；外部目的地用 Outbound Queue + 熔断池；CHASM 把 Workflow 抽象成 ASM tree | MIT |
| Argo Workflows (16.9k) / Flyte (7.5k) | K8s 原生；Flyte 支持 checkpoint / 自动重试 / 状态跟踪 | Apache-2.0 |
| Kestra (28k) | YAML 声明式、事件驱动 | Apache-2.0 |
| XState (30.4k) | SCXML 启发的状态机/状态图 + Actor；可视编辑在 Stately Studio | MIT |
| Stately graph model | **图模型不取代运行时**：读取 XState / Redux / Zustand / LangGraph / Step Functions / Temporal / Mermaid / SCXML，输出测试路径、运行中检查器、TypeScript；提供「不可变提案 + 审查轨迹 + 验证裁决」 | 编辑器商业，SDK/CLI MIT |
| Camunda 8 / Zeebe | BPMN 2.0 + DMN；Zeebe 分区 + Raft log + RocksDB 状态；**CQRS**：写经 gateway→broker→engine→exporter 落二级存储，读从 gateway 直连二级存储绕过 engine；人工任务 Tasklist、权限 Identity、监控 Operate | 开源核心 + 商业 |
| Flowable | 原 Activiti 团队，BPMN/CMMN/DMN，Java 轻量引擎 | Apache-2.0 |
| OpenLineage (~2.7k) | **血缘事件标准**：run/job/dataset + facets，事件发射与存储解耦；Airflow/Spark/dbt/Flink 原生发事件 | Apache-2.0 |
| Marquez (~2.3k) | OpenLineage 参考后端，PostgreSQL + REST + lineage graph UI | Apache-2.0 |
| DataHub (12.9k) / OpenMetadata (15.7k) | 元数据图 + 字段级血缘 + 影响分析 + 所有权 + RBAC/ABAC；DataHub 基于 Kafka/ES | Apache-2.0 |
| Axon Framework (3.6k) | JVM 上 DDD/CQRS/事件溯源/Saga；命令/事件/查询统一消息模型 | Apache-2.0 |

### 2.5 影视 / 制片垂类（与本项目领域最近）

| 项目 | 架构要点 | 许可 |
|---|---|---|
| **Open Shot Designer** (v1.15.2) | React 19 + TS + Vite；**零后端**，IndexedDB + 内容寻址资产库；`src/domain/` 是与框架无关且单测覆盖的业务逻辑；每次持久化 schema 变更都带版本化迁移 + fixture 测试 | GPLv3 |
| **影策 open-ai-canvas** (~1.9k) | AI 影视/短剧工作台：小说→拆集→角色→分镜→逐段视频→时间线成片；无限画布 + Agent + 素材库 + 任务系统 + 管理后台。前端 React + Zustand + localForage + SSE；后端 Go/Gin → service → repository/model，SQLite 或 PostgreSQL+Redis，异步 task worker + 权限 + 模型中转 | MIT |
| OpenFrame | 分镜面板 + 镜头表 + 角色/场景 + mood board + call sheet；Next.js 14 + localStorage，无后端 | MIT |

**两个垂类项目各自补上了对方缺的一半**：Open Shot Designer 有极强的数据纪律但零后端/无权限；影策有完整后端与权限但自述「默认适合个人、本地或可信环境部署；未经安全配置，不要直接作为公网多人服务使用」。本平台要的正是两者之间的治理层。

---

## 3. 可借鉴机制（按采纳强度分级）

### 3.1 直接采纳：与现有铁律同构，不需要新依赖

1. **双表示分离**（ComfyUI UI/API 格式）↔ 本平台草案业务编码 / 正式身份键。画布视图（人看）与执行契约（机器跑）必须是两个可独立校验的表示，不能混成一份。
2. **多视图共享单一领域模型**（AFFiNE BlockSuite、Open Shot Designer）↔ `Shot` 不存 `episode_id`，归属只走 `scene_id → Scene → Episode → Project`。Open Shot Designer 的原文表述可直接作为本项目视图纪律的判据：
   > A shot exists in four places at once, and every view edits the same thing.
   > Line a speech in the script and a camera lands on the floor plan, a row appears in the shot list, and a frame appears on the board. Delete that camera and all three go with it.
3. **派生而非二次存储**（Open Shot Designer）↔ 本项目「集合类事实一律派生」。其连续性日志把完成态从 takes 推导，明确写道：
   > nothing stores "this shot is done" separately, so the two halves cannot disagree.
   这是「不变量放最可靠层」在制片领域的独立印证。
4. **缺失值保持 unknown，不用 0 顶替**（Open Shot Designer）↔ 本项目「列不存在 ≠ 0 条违规，须判不可计算」。原文多处：
   > Unknown wattages stay unknown — they are never counted as zero.
   > A leg nobody assigned is excluded rather than silently loaded onto L1.
   > missing technical data stays `unknown` instead of being substituted with `0`.
5. **静默失败必须可机检**（Open Shot Designer 对 Resolve 导入）↔ 本项目「写入类验收禁止「未抛异常即 PASS」」。该工具发现两个失败点都是静默的（Resolve 报成功但没挂上元数据），对策是把表头写成与模板逐字节一致并**用测试断言守住**：
   > a test asserts it against that file, so a tidy-up can never quietly break the import.
6. **来源标记独立于业务编号**（Open Shot Designer）↔ 终态不可原地复活、资产不可覆盖：
   > Provenance is a flag, not part of the number.
   > an unplanned shot never joins the plan retroactively.
7. **按变化频率切分失效域**（Excalidraw）↔ 高频状态（任务进度、队列长度）不与低频状态（契约、权限、归属）共用缓存失效域。这是现有分层之外新增的一个切分维度。
8. **装配期非法配置不可建立**（BaklavaJS `interface-types`、JointJS port 规则）↔ 网关「注入点必须是字面量、连线就拒」。镜头 DAG 的边应携带状态语义，非法推进连不上，而不是等 `SHOT.STATUS.CHANGE` 返回 409。

### 3.2 参考：作为设计输入，不引入实现

1. **失败路径独立建模**（n8n error workflow、Temporal event history、Camunda exporter）→ ADR-R42 的 Saga 补偿路径参照其携带 `retryOf` / `lastNodeExecuted` / `execution.id` 的进度标记形式；但补偿必须 OpLog 追加，不能原地回滚。
2. **CQRS 读写分离**（Camunda 8）→ 读模型走独立投影，不受写引擎性能影响；与本平台 `ShowcaseSnapshot` 只读隔离同向。
3. **资产中心而非任务中心**（Dagster）→ 关注「产出物健康与影响半径」，而不是只记录任务跑完。与本平台资产登记优先于任务成功的终态权威一致。
4. **图模型不取代运行时**（Stately）→ 状态图可建模、分析、验证、生成测试路径，但不替换真实执行引擎。本平台的契约/状态机定义同理：它是可判定判据，不是 Worker 的替代品。
5. **App Mode 折叠复杂图**（ComfyUI）→ 业务画布的最小可行形态：受控输入表单 + 结果预览，节点图留平台外。

### 3.3 明确禁止

1. **不引入 tldraw**：生产需 license key，且富交互绘制对治理画布是过剩能力。
2. **不引入 CRDT / y-octo**：它解决「多人无冲突编辑」，本平台问题是「受控状态推进 + 不可变守卫 + RLS」。引入 CRDT 等于把 Gate 的可判定性交给最终一致性。
3. **不烤元数据进二进制当证据**：ComfyUI 自述「不是数字签名」「不含依赖」「重新编码会丢」「不可信」。产物只带 lineage 引用，登记在资产域完成。
4. **不抄 `workflowSchema.ts` 的 `.passthrough()`**：本平台模型层 `extra="forbid"`，静默忽略比 422 危险。
5. **不抄「未知版本按 0.4 校验」**：本平台有 append-only 契约修订与指纹登记，未知版本必须 fail-closed。
6. **不抄 n8n 的「继续失败 / 重跑整个执行」**：本平台有不可逆约束（终态不可复活、资产不可覆盖），重跑会变成重复注册资产。
7. **不内嵌 ComfyUI 节点画布**：同一配方两处存储必然漂移，违反一个事实一个权威源。
8. **不以 GPLv3 项目代码为平台画布基线**：Open Shot Designer 是 GPLv3，复制实现会把传染性许可带入平台。

---

## 4. 对 Phase 4 无限画布的约束

基线把无限画布排在 P2 / Phase 4 且要求按证据实施。若届时立项，本 ADR 施加以下约束：

- 画布是**投影**，不是事实源；镜头 DAG 的权威在 `services/` 领域层，前端可渲染、可发命令，不可私有化模型。
- 只读展示视图消费不可变 `ShowcaseSnapshot`，与生产画布权限隔离；社区侧传播必须过权限审核与脱敏（基线第 2921 行）。
- 不得以「工作台 / 画布」名义重新引入多轨剪辑、特效包装、字幕编辑、混音或 3D 创作工具型功能（基线第 3715 行）。
- 画布交互状态与领域状态分属不同失效域；每次 pointermove 变化的量不得触发领域模型重算。
- 若引入任何第三方图库，必须先过许可审查与「core/engine/renderer 是否可从平台剥离」测试；不满足则不引入。

---

## 5. 后果

**正面**：把「要不要做画布」从产品直觉问题变成有边界的工程决策；给出可直接写进 Phase 4 验收的判据；为 ADR-R42 的补偿路径补上行业参照；明确排除付费/传染性许可与语义不匹配的 CRDT。

**代价**：调研结论不产生可运行代码；业务画布仍为空（`apps/console` 未实现），Phase 4 前平台没有可视化入口。这是刻意接受的——先解决治理层的全链阻塞（ADR-R42），再谈可视化外壳。

**风险**：若后续把「参考机制」误读为「引入某个库」，会同时破坏 D1/D2 与基线边界。判据是：任何新增依赖若把领域模型或状态推进逻辑移出 `services/`，即视为违反本 ADR。
