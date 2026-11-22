# ADR-R14：分层依赖闸门（铁律 1 的 CI 化）

- 状态：已接受
- 日期：2026-11-16
- 关联：`docs/architecture/repository-structure.md` §3（分层与依赖规则）、
  铁律 1「不屎山框架」、铁律 5「防呆 + 自动化约束」；
  `docs/audits/2026-11-16-code-and-iron-law-review.md`（铁律 1：代码通过、自动化缺失）、
  `docs/audits/2026-11-16-review-round2.md`（同一条缺口第二次被点名）

## 1. 背景

分层规则写在 `docs/architecture/repository-structure.md` §3 已经很久：

> `apps → services → adapters`，`services/workers/agents → packages`；
> 禁止反向依赖、禁止跨层直连。

但两轮审查都记录了同一句话：**「AST 扫描 0 反向依赖；仍无 tests/architecture /
import-linter」**。也就是说，这条铁律至今靠「有人想起来手工扫一遍」维持：

- 只有想起来才扫，扫完的结论会过期，且没人知道是什么时候扫的；
- 手工扫描最容易漏的恰恰是最常见的形态——把反向 import 藏进函数体（惰性导入）；
- 新人加一条反向依赖，`ruff`/`mypy`/`pytest`/`contract_check` 全绿，无人拦截。

按铁律 5「防呆设计 + 自动化约束」，这种状态不可接受：**规则只要不是机器执行的，
就等价于不存在**。

## 2. 决策

新增独立闸门 `scripts/architecture_check.py`（无第三方依赖，纯 `ast` 静态分析），
CI 在 `contract-check` job 里执行；规则表、允许例外、判定范围全部在代码里单点维护。

### 2.1 四条规则

| 编号 | 规则 | 判定范围 | 当前状态 |
|---|---|---|---|
| R0 | 覆盖面防呆：顶层代码目录必须在规则表登记 | 仓库顶层目录 | 0 违规（6 层已全部登记） |
| R1 | 层间依赖方向：按 `LAYER_RULES` 禁止反向/跨层直连 | **所有** import（含函数体、`TYPE_CHECKING`） | 0 违规 |
| R2 | 服务间直连：`services/<A>` 不得 import `services/<B>` | 所有 import（平台能力白名单除外） | 0 违规（白名单 1 项） |
| R3 | 导入期循环：模块级 import 不得成环 | **仅模块级** import | 0 处 |

### 2.2 为什么 R1 和 R3 的判定范围不同

这是本 ADR 唯一需要解释的取舍：

- **R1 覆盖惰性导入**：依赖是结构事实。`def f(): import services.x` 依然是
  `packages` 依赖 `services`，只是把耦合藏得更深——这正是人工扫描最容易漏的形态。
- **R3 只看模块级导入**：本仓库已有既定的、有意的惰性导入用法
  （`packages/shared/logging.py` 为了把 trace_id 与 OTel 打通，
  在函数体内 `from packages.telemetry.tracing import get_current_trace_id`）。
  惰性导入不会造成导入期死锁，把它算成循环会逼人为了「指标好看」去改一段正确代码。
  真问题是**导入期**互相依赖（import 顺序敏感、可能直接 ImportError），那必须拦。

### 2.3 服务间直连的唯一例外：平台能力白名单

`services.identity_access`（鉴权依赖 `require_role`）被 `asset_service`/
`project_planning` 等直接 import，这是**平台能力**而非领域数据耦合：每个服务都要挂
授权，各自实现一遍才是重复。因此登记在 `PLATFORM_SERVICE_DEPENDENCIES`，并附理由。

配套三条约束，防止白名单变成永久后门：

1. 条目必须有 reason（给下一个人看，不是给 CI 看）；
2. 条目**不再被任何服务引用**时报「陈旧白名单」，必须删；
3. 闸门只认这一个入口，任何新例外都要改代码 + 过 review（不存在配置文件里的静默豁免）。

### 2.4 R0 存在的理由

没有 R0，将来新增一个顶层层（比如 `plugins/`）会**天然免疫** R1-R3：规则表里没有它，
就永远不会命中。R0 把「新增一层必须同时登记规则」变成硬约束。

## 3. 明确不做

- **不引入 import-linter / flake8-tidy-imports**：需要新增依赖 + 改 `uv.lock`
  （CI 用 `uv sync --frozen`，锁文件必须同步），而本闸门的三条规则用 `ast` 就能精确
  表达，且能给出中文可读的违规原因。规则一旦复杂到 `ast` 写不清，再评估引入。
- **不做「同层模块内聚度」/「公共依赖下沉」之类的度量**：那是重构指导，不是闸门；
  度量类指标一旦进 CI 就会变成刷分游戏。
- **不做运行时依赖检查**（`sys.modules` 级）：静态分析已覆盖导入期语义，
  运行时再查一遍属于重复建设。
- **不修当前所有「可疑但不违规」的耦合**：闸门只判规则内的方向/直连/循环；
  `packages` 内 `shared ↔ errors` 的**惰性**互相引用（用 `error_codes` 的懒加载兜住）
  属于设计取舍，不在本闸门判定范围内，也未登记为债务（它不是导入期循环）。

## 4. 落地物

- `scripts/architecture_check.py`：`LAYERS` / `LAYER_RULES` /
  `PLATFORM_SERVICE_DEPENDENCIES` + `collect_imports()` /
  `check_layer_direction()` / `check_cross_service()` /
  `check_stale_platform_dependencies()` / `check_import_cycles()` /
  `check_layer_coverage()`。
- `tests/architecture/test_architecture.py`：19 条用例——仓库现状 0 违规 + 闸门自身的
  负向用例（反向依赖、惰性反向依赖、`TYPE_CHECKING` 反向依赖、跨服务直连、
  平台能力放行、陈旧白名单、导入期循环、子模块拉入父包、未登记新层、语法错误）。
- `.github/workflows/ci.yml`：`contract-check` job 增加 layering 步骤；
  `lint-and-test` job 的 pytest 增加 `tests/architecture`。
- `Makefile`：`make arch-check`。
- 文档：`docs/architecture/repository-structure.md` §3 挂上闸门入口；
  `README.md` 增「自动化闸门」表（哪条铁律由哪个命令守）。

## 5. 演进路线

1. 依赖图接入 CI 产物（导出 DOT，PR 里可视化），便于 review 时判断耦合趋势。
2. 若将来引入 `plugins/`、`sdks/` 等新层，R0 会先报红，逼着同时补规则——这是刻意的。
3. 规则复杂到 `ast` 表达不清时（例如需要「允许 interface-only 依赖」），再评估
   import-linter，并把现有规则表作为对照基线。
