# 代码审查与工程铁律复核（R12 之后）

- 日期：2026-11-16
- 范围：`main` HEAD `82536bd`（PR #5 合并后）+ 工作区 2 个未提交改动
  （`scripts/contract_check.py`、`services/identity_access/action_registry.py`）
- 方式：只读审查 + 实跑闸门 + 运行时探针；**未修改任何代码、契约、迁移**
- 证据口径：`file:line`、闸门实跑输出、异常类型探针

---

## 0. 一句话结论

**闸门是绿的，但“全绿”仍不等于“审计表里 17 项全部关闭”。**

本轮发现 1 项状态误标（P2-1）、2 项覆盖不足（P2-4 / Schema 运行时比对）、
4 项代码级缺陷（死信积压、吊销 500、并发幂等分支、契约加载异常），
以及 2 处未提交改动缺少负向测试。它们都不影响当前闸门通过，但会在真实流量、
契约损坏或并发场景下暴露。

分级：**P1 1 项 / P2 4 项 / P3 4 项 / 测试缺口 2 项**。

---

## 1. 五道闸门实跑结果（本机，Python 3.13.14）

| 闸门 | 结果 | 证据 |
|---|---|---|
| `contract_check` | **PASS** | Schema 76 定义结构通过；运行时字段一致 2 对象；事件 10/缺口 0；ACTIVE 37 |
| `ruff check .` | **PASS** | `All checks passed!`，exit 0 |
| `mypy packages services apps workers` | **PASS** | `Success: no issues found in 90 source files` |
| `pytest tests -q` | **环境阻塞，不能判红** | 20+ 用例 `ConnectionRefused`；本机 `localhost:15432` 未监听（`TCP15432=False`） |
| `alembic check` | **未执行** | 同上，数据库不可达；不伪造“无漂移”结论 |

补充：受本次改动影响的定向用例全过——
`tests/unit/test_contract_check.py`、`test_action_registry_runtime.py`、
`test_asset_service.py` 共 **59 passed**。

> 结论：本地“三道静态闸门”真实全绿；涉及数据库的两道在本环境无法复现，
> 应回到有 Docker PG 的环境补跑，不能用上一轮的 272 passed 代替本轮证据。

---

## 2. 代码级与工程级 findings

### R1（P1）P2-1 被标成“已修复”，但 Project/Episode 仍没有状态变更入口

**证据**

- 审计进度表：`docs/audits/2026-11-14-execution-loop-audit.md:338`
  写 P2-1 **已修复**，落地物是“同事务发 `PROJECT_CREATED`/`EPISODE_CREATED`”。
- 同一份 ADR 却写：`docs/adr/ADR-R12-contract-gates-outbox-deadletter-and-token-revocation.md:106-107`
  “**Project/Episode 的 HTTP 状态变更入口仍不存在（只补了创建事件）**”。
- `contracts/action_registry/action_registry.yaml` 中 Project/Episode 仍只有
  `CREATE`/`READ`；全仓 `project.transition_to()` / `episode.transition_to()`
  仍只出现在 domain 定义与测试，没有任何 service/route 调用。

**为什么这是 P1**

原审计 P2-1 的问题不是“创建没事件”，而是“**项目/集数无法推进到下一个状态**”
（`docs/audits/2026-11-14-execution-loop-audit.md:205-213`）。S6 补的是创建事件，
解决的是事件缺口，不是状态入口。结果是：项目创建后仍停在 DRAFT，平台仍无法把
项目推进到生产态，但进度表已宣布关闭。

**修复方向（二选一，必须写进契约）**

1. 在 action_registry 登记 `PROJECT.STATUS.CHANGE` / `EPISODE.STATUS.CHANGE`，
   实现 service/route/权限/事件/负测；
2. 或在 `state_machine_gaps.yaml` 把“无入口的转移”显式登记为债务，并把审计表
   状态改回“部分修复/债务登记”。

不能保留“绿着但不可执行”。

---

### R2（P2）死信仍被算进 backlog，租户扫描列表永远清不掉

**证据**

- 领取 SQL 已排除死信：`workers/relay/consumer.py:89-91`
  （`dead_lettered_at IS NULL`）。
- 但租户枚举函数仍只按 `published_at IS NULL` 统计：
  `data/migrations/versions/e5f6a7b8c9d0_add_outbox_tenant_enumeration_function.py:49-52`
  → `pending_count` 包含死信与“未到重试时间”的事件。
- 指标直接使用该统计：`workers/relay/consumer.py:165`
  `OUTBOX_RELAY_BACKLOG.set(sum(pending_count))`。

**影响**

一条事件进入死信后：

1. `pending_count` 永不下降，`OUTBOX_RELAY_BACKLOG` 报警永远消不掉；
2. 该租户永远留在 `list_active_tenants()` 的扫描列表里，每轮白跑一次 claim；
3. 运维无法区分“真积压”与“已放弃的死信”，S5 的死信可观测性被抵消。

**修复方向**

- 枚举函数改为 `WHERE published_at IS NULL AND dead_lettered_at IS NULL`；
- 另返回 `dead_count` / `oldest_dead_at`，单独出指标；
- 补一条集成断言：事件进入死信后，`pending_count` 必须归零，但死信计数为 1。

---

### R3（P2）响应契约只保护了资产域，Project/Episode/Script 仍无 `response_model` 与运行时比对

**证据**

- 运行时 Schema 比对只登记 2 个对象：
  `scripts/contract_check.py:704-712`（`AssetVersion`、`QCReport`）。
- 资产四路由都挂了 `response_model`：
  `services/asset_service/api/asset_routes.py:107,132,158,173`。
- 但项目/集数/剧本路由仍返回裸 `dict`，无 `response_model`：
  - `services/project_planning/api/project_routes.py:60-66,80-85`
  - `services/project_planning/api/episode_routes.py:71-76,91-95`
  - `services/project_planning/api/script_routes.py:87-91,138-142`

**影响**

已实现的资源类型至少 6 个（Project/Episode/ScriptRevision/ScriptLock/AssetVersion/QCReport），
行为级只覆盖 2 个。Project/Episode/Script 现在即使字段漂移、漏字段、类型变化，
FastAPI 不会校验，`contract_check` 也不会红——P2-4 只修了资产域，
但审计表把它写成整体“已修复”。

**修复方向**

- 用 `contract_model("Project"/"Episode"/"ScriptRevision"/"ScriptLock")` 给对应路由挂
  `response_model`；
- 把 4 个 domain 的 `to_dict()` 探针加入 `_RESOURCE_SERIALIZERS`，让闸门做双向比对；
- 补 4 条负向：删一个 required / 多一个内部字段 → 必须红。

---

### R4（P2）吊销查询失败会冒泡成 500，而不是约定的 401 fail-closed

**证据**

- 认证依赖只处理布尔结果：
  `services/identity_access/auth_dependency.py:108-116`
  `if await revocation_check(request, principal.jti): raise 401`。
- 吊销查询本身没有异常转换：
  `packages/identity/token_revocation.py:35-41`，
  数据库 `OperationalError` / 连接超时会直接从依赖冒泡。
- ADR 约定：`docs/adr/ADR-R12-contract-gates-outbox-deadletter-and-token-revocation.md:67-69`
  “查询失败 fail-closed”。

**影响**

安全上仍是“拒绝”，但客户端收到 500（无统一错误信封），监控会把“数据库抖动”
记成服务端错误，而不是认证拒绝；同时违反“认证失败统一 401”的口径。

**修复方向**

在 `is_token_revoked()` 或 `check_request_revocation()` 内捕获 `Exception`，
转 `DomainError(AUTH_UNAUTHENTICATED, detail="无法校验令牌吊销状态")`，
并补一条“sessionmaker 抛 OperationalError → 401”的单测。

---

### R5（P2）并发幂等分支只比对 `sha256`，与快速路径的判定不一致

**证据**

- 快速路径同时比对内容和类型：
  `services/asset_service/service/asset_service.py:102-115`
  （`sha256` **且** `asset_type`）。
- `IntegrityError` 回查分支只比对 `sha256`：
  `services/asset_service/service/asset_service.py:136-138`
  `if existing is not None and existing.sha256 == normalized_sha: return existing`。

**影响**

同一 `asset_version_id` 并发写入、`sha256` 相同但 `asset_type` 不同（例如同一文件
被登记为 `video` 与 `audio`）时，异常路径会静默返回旧对象，而顺序路径会 409。
同一业务规则出现两种结果，属于竞态下的行为不一致。

**修复方向**

把判定收敛成一个纯函数（如 `_same_content(existing, sha, asset_type)`），
两条路径共用；补并发负向用例。

---

### R6（P3）未提交改动声称“契约损坏统一结构化报红”，但 YAML 语法错误仍会冒泡

**证据**

- 新 helper：`scripts/contract_check.py:74-87`
  只处理“解析结果不是 dict”，没有捕获 `yaml.YAMLError`。
- `main()` 只捕获 `ValueError`：
  `scripts/contract_check.py:1179-1194`。
- 实测（临时文件，不动仓库契约）：

```
empty.yml   ValueError      （符合预期）
list.yml    ValueError      （符合预期）
bad.yml     ParserError     （未捕获 → traceback）
```

**影响**

`check_yaml()` 虽然会先打印一次 `[FAIL]`，但 `main()` 继续执行到
`load_yaml_mapping()` 时仍抛 `ParserError`，最终是堆栈而不是统一的
`[FAIL] 契约加载失败`。这正是这次改动想根治的场景，但只根治了“空文件/非映射”，
没根治“语法损坏”。

**修复方向**

```python
try:
    data = yaml.safe_load(...)
except yaml.YAMLError as exc:
    raise ValueError(f"{path} YAML 解析失败: {exc}") from exc
```

并把 `main()` 的捕获改为 `except (ValueError, yaml.YAMLError)` 或统一转 ValueError。

---

### R7（P3）`action_registry` 的 fail-fast 会绕过结构化错误，且 `actions` 形状未校验

**证据**

`services/identity_access/action_registry.py:55-74`：

- 文件缺失 → `FileNotFoundError`；
- 顶层非映射 → `ValueError`；
- `actions` 为 `null` 或非列表 → `for action in data.get("actions", [])` 抛 `TypeError`；
- 条目不是 dict → `action.get(...)` 抛 `AttributeError`。

模块 docstring 却承诺“未注册 action_key 一律 fail-fast 转
`DomainError(INTERNAL_ERROR)`，结构化信封”（同文件 `:11-12`）。
文件级损坏时这个承诺没有兑现。

**修复方向**

- 用 `DomainError(INTERNAL_ERROR)` 包住加载期异常；
- 显式断言 `actions` 是 list、每个条目是 dict；
- 补空文件 / `actions: null` / 条目为字符串三条负测。

---

### R8（P3）新 helper 的错误信息只有文件名，缺少完整路径

`scripts/contract_check.py:86` 使用 `path.name`。契约目录里存在同名 YAML 时
（例如多个 `*_gaps.yaml`），报错无法定位到具体文件。

建议改为 `str(path)`（相对仓库根或绝对路径），并把“可能为空文件”与
“顶层类型是 list”区分开。

---

### R9（P3）README 列为硬要求的“熔断”没有代码实体

**证据**

- README 铁律 3 明确包含“熔断”：
  `README.md:56`。
- 仓库没有 `CircuitBreaker` 组件；对外 webhook 只有超时、重试和死信：
  `workers/relay/webhook.py:53-60,75-78`。
- `docs/runbooks/runbook-tenant-leak.md:11-14` 的“熔断”是人工应急步骤，
  不是系统自动触发的降级。

**影响**

下游 webhook 持续故障时，每个事件仍会独立尝试（最多 3 次）直到死信；
没有“连续失败→快速失败”的熔断，故障期间会持续放大对外请求。
这不是正确性缺陷，但按 README 字面验收，该项未落地。

**修复方向**

为 webhook 增加按 endpoint 的熔断器（连续失败阈值 + 半开探测），
或在 ADR 中明确“当前阶段不做熔断，只有超时/退避/死信”，
不能既列为铁律又不登记取舍。

---

### T1/T2（测试缺口）未提交改动的 fail-fast 路径没有测试

- `tests/unit/test_contract_check.py` 中没有任何 `load_yaml_mapping` 用例
  （全文件只测 `parse_state_chain`、schema 负向、事件缺口等）。
- `tests/unit/test_action_registry_runtime.py:14-19` 只测“未知 action_key”，
  没有测“契约文件为空/非映射/actions 非列表”。

按铁律 4/5（修改必须配测试、自动化约束），这两处改动目前只靠人工探针验证，
不能防止回归。

---

## 3. 工程铁律逐条取证

先给总判定：**1 条通过（代码现状）、3 条部分通过、1 条存在明确未落地项**。
不是“五条全部通过”，也不是“五条全红”。

### 铁律 1：不屎山框架（分层清晰、边界明确、依赖单向、可测试可演进）

**判定：代码现状通过；自动化守卫缺失（部分通过）**

已核对：

- 用 AST 扫描 `packages/`、`services/`、`apps/`、`workers/` 的 import，
  **0 条反向依赖**：`packages` 不 import `services/apps/workers`；
  `domain` 不 import `service/api/repository/sqlalchemy/fastapi`；
  `service` 不 import `api`。
- 领域层确实只依赖错误包和同域模块：
  `services/project_planning/domain/project.py:20-23`、
  `episode.py:22-25`、`script.py:23-26`。
- 测试分层存在：`tests/unit`、`tests/integration`、`tests/permission`、
  `tests/contract`、`tests/e2e`，领域/服务/路由各有单测。

缺口：

- **没有架构测试**：`tests/architecture` 不存在；ruff 未启用
  `flake8-tidy-imports` / `import-linter`，依赖方向目前靠人工扫描，
  下一次改动可以静默破坏而 CI 不红。
- `tests/resilience` 只有 `.gitkeep`，韧性测试目录是空的（详见铁律 3）。
- `Makefile:19` 的 `typecheck` 只查 `packages services apps`，
  漏了 CI 已纳入的 `workers`（`ci.yml:63`），本地与 CI 口径不一致。

结论：**分层现状是干净的，但“自动化约束”没有把这条铁律锁住**。

### 铁律 2：不屎山代码（单一职责、清晰命名、合理抽象、禁止补丁式演进）

**判定：部分通过**

做对的部分：

- 未提交改动把 7 处 `yaml.safe_load(...) or {}` 收敛为
  `load_yaml_mapping()`（`scripts/contract_check.py:74-87`），
  这是系统性收口，不是逐点打补丁。
- 资产/剧本幂等判定已抽成共享断言（`script_service.py:100-107`），
  不是每个入口各写一套。

仍是补丁式演进的部分：

- `_RESOURCE_SERIALIZERS` 只登记 2 个序列化探针
  （`scripts/contract_check.py:704-712`），而 action_registry 已实现 6 类资源
  （Project/Episode/ScriptRevision/ScriptLock/AssetVersion/QCReport）。
  P2-4 只在资产域挂了 `response_model`，其余三域没有结构保证，属于
  “先修被审计点名的那 4 个路由”，不是把同类问题一次收口。
- `action_registry` 只补了顶层 `isinstance(data, dict)`
  （`services/identity_access/action_registry.py:58-64`），
  没有校验 `actions` 是 list、条目是 dict；仍是局部补丁。
- P2-1 用“补创建事件”替代“补状态变更入口”，是典型的“修相邻问题、
  让表格变绿”，不是解决原问题（见 R1）。

结论：**方向对，但只修了主路径；同类问题的另一半仍在。**

### 铁律 3：高并发/高流量/高标准

**判定：部分通过；有 1 项字面要求未落地**

已落地且可验证：

- **限流**：Redis 固定窗口，带 fail-open 与降级指标
  （`packages/shared/rate_limit.py:8-20,81-103`）；
  中间件接入 `apps/api/main.py:89-99`；有专门测试
  `tests/unit/test_rate_limit.py`。
- **幂等**：资产快速路径同时比对 `sha256 + asset_type`
  （`asset_service.py:102-115`）；剧本同 id 内容比对
  （`script_service.py:100-107`）；项目/集数冲突降级为幂等返回。
- **并发**：Outbox relay 用 `FOR UPDATE SKIP LOCKED`
  （`workers/relay/consumer.py:84-95`），支持多副本水平扩展；
  乐观锁并发测试存在（`tests/unit/test_optimistic_lock*.py`）。
- **异步解耦/重试/死信**：attempts、指数退避、`dead_lettered_at`
  已落地（`workers/relay/consumer.py:89-91,107-119`）。
- **可观测**：relay backlog、限流降级、错误计数等指标存在
  （`workers/relay/consumer.py:165`、`packages/shared/metrics.py:34-41`）。

明确缺口：

- **R2 死信仍计入 backlog**：枚举函数不排除 `dead_lettered_at`
  （`e5f6...:49-52`），导致指标永远不降、租户永远被扫描。
- **R4 吊销查询失败是 500**，不是约定的 401 fail-closed
  （`auth_dependency.py:108-116`、`token_revocation.py:35-41`）。
- **R5 并发幂等分支只比 sha256**，与快速路径的 `sha256 + asset_type`
  不一致（`asset_service.py:105` vs `:136-138`）。
- **R3 契约可校验只覆盖 2/6 资源**，Project/Episode/Script 可静默漂移。
- **熔断器未落地**：README 把“熔断”列为硬要求，但仓库没有
  `CircuitBreaker` 组件；对外 webhook 只有 5s 超时 + 重试 + 死信
  （`workers/relay/webhook.py:53-60,75-78`），
  `docs/runbooks/runbook-tenant-leak.md:11-14` 的“熔断”是人工应急步骤。
  按字面验收，这一项不能算完成。
- **韧性测试目录为空**：`tests/resilience/.gitkeep` 是唯一文件；
  relay 重试/死信测试在 `tests/integration`，但“幂等/重试/降级”没有
  独立韧性套件。

结论：**限流、幂等、异步、水平扩展、可观测都有真实实现；
但死信观测面、吊销错误口径、并发幂等分支、契约覆盖、熔断仍有缺口。**

### 铁律 4：全程 AI 辅助开发，不降质量

**判定：CI/闸门结构通过；本次改动未满足“修改配测试”（部分通过）**

已核对：

- CI 五个闸门全在：ruff（`ci.yml:57-58`）、mypy（`:60-63`）、
  pytest（`:90-98`）、alembic check（`:100-107`）、
  contract_check 独立 job（`:122-123`）；另有权限负测（`:138-139`）
  和镜像构建/import 冒烟（`:144-166`）。
- pre-commit 配了 ruff、ruff-format、mypy、check-yaml、check-json、
  detect-private-key（`.pre-commit-config.yaml:1-24`）。
- 本机定向跑 `test_contract_check.py`、`test_action_registry_runtime.py`、
  `test_asset_service.py`：**59 passed**。

缺口：

- 本次两个未提交改动**没有负向测试**（T1/T2）：
  `load_yaml_mapping` 的空文件/非映射/坏 YAML 没测；
  `action_registry` 的空 registry/非映射/`actions` 非列表没测。
  闸门绿是因为没测到，不是因为测过了。
- 本机完整 `pytest` 与 `alembic check` 因 15432 未监听无法复跑；
  CI 的 272 passed 不能替代本环境的证据。
- 静态观察：pre-commit 的 mypy hook 只装了 `pydantic`、`sqlalchemy`
  （`.pre-commit-config.yaml:12-13`），而项目 import fastapi/redis/httpx/PyYAML；
  在隔离 hook 环境里很可能无法通过 strict mypy。**此项未实跑，列为待验证风险**，
  建议在发布前执行一次 `pre-commit run mypy --all-files`。

结论：**“有闸门”成立，“本次改动的质量闭环”不成立**。

### 铁律 5：工业级最佳实践（防呆 + 自动化约束 + 系统性根治）

**判定：部分通过**

已落地：

- 契约闸门能拦住空文件/非映射（`load_yaml_mapping` 的 ValueError 兜底）；
- pre-commit 的 check-yaml/check-json 能在提交期拦住语法坏文件；
- Schema 结构+行为闸门、死信、jti 吊销、权限负测都是系统性方向；
- CI 把契约、权限、镜像冒烟分成独立 job，避免“测试绿但镜像起不来”。

缺口：

- **R6 坏 YAML 仍会 traceback**：`load_yaml_mapping` 没捕获 `yaml.YAMLError`，
  `main()` 只捕获 `ValueError`（`contract_check.py:74-87,1179-1194`）；
  实测 `a: [` 抛 `ParserError`，不是统一 `[FAIL] 契约加载失败`。
- **R1 审计状态失真**：P2-1 标“已修复”，ADR 自认状态入口不存在
  （审计 `:338` vs `ADR-R12:106-107`）。防呆不仅防代码，也要防“报告假绿”。
- **R3 覆盖不足**：只防住 2/6 资源的响应漂移。
- **无架构自动化守卫**：依赖方向靠人工（见铁律 1）。
- **死信 backlog 口径错误**：指标会把“已放弃”当“待处理”，
  运维无法区分真积压与死信（R2）。

结论：**有防呆骨架，但还没做到“任何修改都按此验收”的自锁状态。**

### 铁律判定汇总

| 铁律 | 判定 | 关键证据 |
|---|---|---|
| 1 分层/依赖单向/可测试 | **通过（现状）；自动化缺失** | AST 扫描 0 违规；无 `tests/architecture`、无 import-linter |
| 2 单一职责/禁补丁 | **部分通过** | `load_yaml_mapping` 收敛正确；`_RESOURCE_SERIALIZERS` 2/6、action_registry 只补顶层、P2-1 修相邻问题 |
| 3 幂等/限流/熔断/异步/可观测/契约 | **部分通过；熔断未落地** | 限流/幂等/SKIP LOCKED 已落地；R2/R4/R5/R3 + 无 CircuitBreaker |
| 4 AI 辅助不降质量 | **部分通过** | CI 五闸门齐全；本次改动零负测；本机 DB 闸门未复跑 |
| 5 防呆/自动化/系统根治 | **部分通过** | check-yaml/契约闸门在；R1 报告假绿、R2 指标口径、R3 覆盖不足、无架构守卫 |

**总计：0 条完全满足“代码 + 自动化 + 本次改动”三重口径；
1 条代码现状通过，4 条部分通过，其中 1 条有明确未落地项（熔断）。**

---

## 4. 与审计报告/ADR 的对账

| 审计项 | 报告状态 | 本次复核 | 证据 |
|---|---|---|---|
| P2-1 Project/Episode 无状态入口 | 已修复 | **误标**：只补了创建事件，状态入口仍不存在 | 审计 `:338` vs ADR-R12 `:106-107`；action_registry 仅 CREATE/READ |
| P2-2 relay 毒事件 | 已修复 | **主路径已修，观测面残留**：死信仍计入 backlog | `e5f6...:49-52`、`consumer.py:165` |
| P2-4 响应契约漂移 | 已修复 | **仅资产域**：Project/Episode/Script 无 `response_model`，闸门只比 2 个对象 | `_RESOURCE_SERIALIZERS:704-712`；各 routes |
| P3-2 jti 吊销 | 已修复 | **功能在，错误路径不合口径**：查询失败 500 | `auth_dependency.py:108-116` |
| P3-1 剧本幂等 | 已修复 | 代码比对 title/project/episode，成立 | `script_service.py:100-107` |

---

## 5. 建议修复顺序（一次只开一个口子）

1. **R1 先定性**：登记状态变更动作，或把 P2-1 改回“债务登记”。这是文档诚实性问题，
   不改代码也必须先改口径。
2. **R2 修观测面**：枚举函数排除死信 + 单独 dead 指标 + 集成断言。
3. **R3 扩契约面**：4 个 domain 探针 + 4 条路由 `response_model` + 负向测试。
4. **R4/R5 修错误路径**：吊销异常转 401；资产并发判定收敛为纯函数。
5. **R9 明确取舍**：实现按 endpoint 的熔断，或在 ADR 中登记“当前不做熔断”；
   不能继续把“熔断”列在铁律里却不落地。
6. **R6/R7/R8 收尾未提交改动**：捕获 `YAMLError`、完整路径、`actions` 形状断言、
   补 6 条负向测试。

---

## 6. 未验证 / 明确留白

1. 本机 `localhost:15432` 未监听，`pytest` 全量与 `alembic check` 无法复跑；
   本轮不把“上一轮 272 passed”当作本轮证据。
2. 未做并发压测、长事务实测；R5 是静态阅读 + 代码路径比对，未构造真实并发。
3. 未启动 Docker/数据库，因此死信 backlog 的数据库实测未做（R2 为 SQL 语义证据）。
4. 未修改任何代码；本报告是唯一新增产物。

---

## 附录：两处未提交改动的代码审查 JSON

```json
{
  "findings": [
    {
      "severity": "medium",
      "category": "bug",
      "line": 84,
      "end_line": 87,
      "message": "load_yaml_mapping 未捕获 yaml.YAMLError；main() 仅捕获 ValueError，语法损坏的契约仍会以 traceback 结束，而非统一的 [FAIL] 契约加载失败。",
      "suggestion": "在 helper 内 try/except yaml.YAMLError 并转 ValueError（带完整 path）；main 捕获该异常。",
      "confidence": "high",
      "basis": "in_context"
    },
    {
      "severity": "low",
      "category": "style",
      "line": 86,
      "end_line": null,
      "message": "错误信息只用 path.name，存在同名契约文件时无法定位。",
      "suggestion": "改为 str(path)，并区分空文件与顶层非映射两种原因。",
      "confidence": "high",
      "basis": "in_context"
    }
  ]
}
```

```json
{
  "findings": [
    {
      "severity": "medium",
      "category": "bug",
      "line": 58,
      "end_line": 64,
      "message": "文件缺失抛 FileNotFoundError、顶层非映射抛 ValueError，均不是 DomainError；与模块 docstring 承诺的结构化错误信封不一致。",
      "suggestion": "加载期异常统一转 DomainError(INTERNAL_ERROR)，detail 带路径与原因。",
      "confidence": "high",
      "basis": "in_context"
    },
    {
      "severity": "low",
      "category": "bug",
      "line": 66,
      "end_line": 70,
      "message": "actions 为 null/非列表或条目非 dict 时会抛 TypeError/AttributeError，缺少形状断言。",
      "suggestion": "显式校验 actions 是 list、每个条目是 dict、action_key/allowed_principals 非空。",
      "confidence": "high",
      "basis": "in_context"
    }
  ]
}
```
