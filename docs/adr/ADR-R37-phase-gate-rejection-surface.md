# ADR-R37：平台阶段门禁的运行时表达（Phase 1 验收 F1）

状态：已接受（2026-11-17）
关联：`Phase1-实现验收清单.md` F1、`contracts/phases/phase_map.yaml`（31.2 唯一
阶段表）、`action_registry.yaml` 的 `PHASE_GATED`、ADR-R12（契约门禁、
`PHASE_NOT_ENABLED` 的出处）、ADR-R26（Capability Token 运行时执行）
契约修订：**无**（本 ADR 不改动任何契约产物，冻结清单无需重签）

## 1. 背景与问题

F1 的验收要求是：

> 调用 Creator 动作 → 返回 `PHASE_NOT_ENABLED`，无副作用；不得以配置开关绕过。

实测三件事，合起来说明缺的不是"再加一条校验"，而是一个**语义**：

1. **契约有地址、运行时没有路由**。44 个动作登记为 `PHASE_GATED（Phase N）`，
   在 Action Registry 与 OpenAPI 里都有规范地址 `/actions/<小写点分 key>`，
   但运行时一个都没实现。调用得到 **404**。
2. **`PHASE_NOT_ENABLED` 只有一个落点**，且不在动作面上：
   `services/workflow/service/approval_service.py:84` 在"为阶段门禁动作建审批
   票据"时拒绝（`tests/unit/test_approval_service.py::test_start_rejects_phase_gated_action`）。
   这堵住了**绕过路径**（先把未开放动作做成票据再消费），但没有回答
   **直接调用**该怎么办。
3. **404 与"路径拼错"不可区分**。客户端无法判断该重试、该升级客户端、还是
   该改用别的能力；指标里也看不出"有人在敲未开放能力"——它和手抖打错一个
   字母的请求混在同一个计数器里。

因此问题定义为：**契约已登记、但当前阶段不提供服务的动作，平台应当怎样被拒绝。**

## 2. 决策

### 2.1 单一拒绝面：`/actions/{action_key}`

新增一个端点，`include_in_schema=False`，接受 GET/POST/PUT/PATCH/DELETE，
职责**只有拒绝**。

- **为什么用这个地址**：契约里这些动作的规范地址本来就是 `/actions/<key>`
  （见 `openapi.draft.yaml` 的 `/actions/creator.mode.upgrade` 等 8 条）。
  用它做拒绝面，不需要新增任何契约操作，也不需要为 44 个动作各造一个端点。
- **为什么不入 schema**：这些操作在契约里是"已登记、未实现"。把拒绝面写进
  OpenAPI 等于让文档声称"可用"，而实际可用性是零。契约与运行时的一致性由
  `scripts/contract_check.py` 按**动作台账**（`action_implementations.yaml`）
  判定；拒绝面不承载业务能力，不入 schema 是刻意的（该脚本枚举的是
  `app.openapi()`，因此也不会误判为"未登记端点"）。

### 2.2 阶段是代码常量，不是配置项

`packages/identity/phase_gate.py` 里 `PLATFORM_PHASE = PlatformPhase.PHASE_1`。

- **为什么不用环境变量**：F1 明写"不得以配置开关绕过"。若阶段来自 env，
  一个 `AFS_PLATFORM_PHASE=4` 就把 Phase 1 变成 Phase 4，验收要求当场失效。
  阶段是**发布级事实**（一次发布对应 `phase_map.yaml` 里的一个阶段），
  改它必须改代码、过评审、重走发布闸门；"谁把阶段推上去了"由
  `release_package.py` 写入包元数据的 commit 可追溯。
- **为什么要 `IntEnum`**：判定是"是否已达到"，本质是数值比较。字符串比较会
  引入排序误解（`"Phase 10" < "Phase 2"`）。

### 2.3 三态分流（本决策最容易做错的地方）

| 情形 | 响应 | 理由 |
|---|---|---|
| 未登记的 key | `404 NOT_FOUND` | 不能因为"猜是阶段问题"就给 409 |
| 非门禁动作 / 阶段已到 | `404 NOT_FOUND`，`details` 说明 status | 该走自己的业务端点，不是"阶段未启用" |
| 门禁且阶段未到 | **`409 PHASE_NOT_ENABLED`**，`details` = `action_key` / `current_phase` / `required_phase` | 这是唯一需要与"不存在"区分的情形 |

**为什么非门禁动作也必须 404**：若对"已启用动作的契约路径"返回 409 或 200，
`/actions/*` 就退化成**动作发现面**——随便敲一个 key，用 409/404 的差别就能
枚举出契约里有哪些能力、哪些已启用。拒绝面必须比它拒绝的东西更"哑"。

`details` 带 `required_phase` 是有意的：调用方需要知道"要不要等"，
只回一句"未启用"等于让它去猜什么时候能启用。

### 2.4 计数只计真门禁拒绝

`phase_gate_rejections_total{action_key}`，只在第三态 `inc()`。

不把 404 也计进来：404 已由 `http_requests_total{status_code="404"}` 覆盖，
重复计数没有信息增量；而这个计数器的**唯一价值**是让"敲未开放能力"从
"拼错路径"里分离出来——一旦混入 404，它就失去价值。

## 3. 未覆盖（诚实登记）

1. **拒绝面无鉴权前置**：未认证调用者同样能看到 409 与 `required_phase`。
   当前不构成泄露（这些信息在 `openapi.draft.yaml` 里本就可见），但若将来契约
   转为不公开，这里必须先加鉴权再判断阶段。
2. **`required_phase` 只解析阶段号，不解析括号里的条件**。契约里有
   `PHASE_GATED（Phase 3 公开）`，其中"公开"是启用条件而非阶段。到 Phase 3 时
   "公开"是否等于可直接发布，本 ADR 不裁定——属 34.8 受众范围决策。
3. **只区分 phase，不区分 entitlement**。"阶段已启用但该租户/组织未开通"
   是另一种拒绝，语义不同（应可自助申请），不在本 ADR 范围。
4. **阶段常量与 `phase_map.yaml` 之间没有机器校验**。`phase_map.yaml` 描述的是
   "每个阶段做什么"，**没有"当前处于哪个阶段"字段**，因此无法交叉验证。
   改阶段靠评审与全闸门，不靠 CI 断言。补这个需要给 `phase_map.yaml` 加字段，
   属契约修订，未做。
5. **动作面的"已启用但无端点"仍然是 404**。当前 15 个 ACTIVE 动作无端点
   （见 `phase1-coverage-audit.md` §4），它们走本端点会得到 404 + `status=ACTIVE`
   的 `details`——比裸 404 好，但仍是"不存在"。真正的解是补端点（B1）。

## 4. 后果

- **正面**：F1 的通过标准（8 个 Creator 动作返回 `PHASE_NOT_ENABLED`、无副作用）
  被真正满足，且不是给 Creator 开的特例——同一机制覆盖全部 44 条门禁动作
  （测试里用 `COMMUNITY.POST.SUBMIT` 证明通用性）。尝试调用未开放能力在指标
  与日志里可见。
- **代价**：多一个**永不承载业务**的端点。它最大的风险是被后人扩展成"通用动作
  入口"——那会把一张干净的拒绝面变成一个绕过动作矩阵的后门。代码注释里写了
  硬约束，测试 `test_active_action_path_stays_404` 钉住了这个边界。
- **测试桩**：`test_current_phase_is_phase_1` 断言阶段常量等于 Phase 1。
  阶段跃迁时必须同时改这条断言，强迫提交者面对"这是阶段跃迁，不是普通重构"。
- **契约产物零变更**：不动 action_registry / openapi / schema / 状态机 / 事件 /
  枚举，因此 `freeze_manifest.py --check` 仍应通过（实测通过）。
  新增迁移：**无**（本段不碰数据库）。
