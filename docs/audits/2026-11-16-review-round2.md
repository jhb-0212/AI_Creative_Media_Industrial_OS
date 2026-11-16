# 第二轮代码审查（PR #6 修复复核）

- 日期：2026-11-16
- 审查对象：`main` HEAD `8b75fb7`（PR #6 合并后）
- 对比基线：`82536bd`（上一轮审查时的 HEAD）
- 提交范围：
  - `bdb14da` 契约 YAML 加载统一收敛
  - `0efc23b` 修复上一轮 R1-R8 / T1-T2
  - `2ec7e4b` 落地 R9 Webhook 端点熔断
  - `8b75fb7` Merge PR #6
- 说明：上一轮报告 `docs/audits/2026-11-16-code-and-iron-law-review.md`
  基于 `82536bd`，其中 R2/R9 等结论**已被 PR #6 改变**；本报告是第二轮独立结论。

---

## 0. 结论

PR #6 **确实修掉了上一轮的大部分代码问题**，且这次本机数据库可用，
五道闸门可以完整复跑：

- `contract_check` PASS
- `ruff` PASS
- `mypy` 91 files PASS
- `pytest tests` 全量 PASS
- `alembic check` 无漂移（当前 `a1b2c3d4e5f6 (head)`）

但新引入的两块“防呆”仍有两个关键漏洞：

1. **R1 的转移入口债务闸门可被“删条目/删文件”绕过**：
   删掉 `transition_entry_gaps` 后，闸门反而打印 PASS；坏形状还会抛 `AttributeError`。
2. **R9 熔断把 4xx 当成端点故障，且熔断拦截仍消耗事件重试次数**：
   一个 400 事件就能打开端点熔断，后续事件在未真正外发的情况下被计入失败，
   最终可能死信；另外配置文档写“阈值 0 = 关闭熔断”，实际会启动崩溃。

分级：**P2 3 项 / P3 1 项**。

---

## 1. 五道闸门实跑（本机，数据库已可达）

| 闸门 | 结果 | 证据 |
|---|---|---|
| `contract_check` | **PASS** | 79 个定义结构通过；运行时字段一致 **5 个对象**；状态转移入口债务显式登记 **2 项**；事件 10/缺口 0 |
| `ruff check .` | **PASS** | `All checks passed!` |
| `mypy packages services apps workers` | **PASS** | `Success: no issues found in 91 source files` |
| `pytest tests -q` | **PASS** | 全量通过，无失败 |
| `alembic check` | **PASS** | `a1b2c3d4e5f6 (head)`；`No new upgrade operations detected.` |

这次不再有“数据库未启动”的环境阻塞，上一轮“pytest 无法复跑”的限制已解除。

---

## 2. 上一轮 findings 的修复复核

| 上轮编号 | 状态 | 复核证据 |
|---|---|---|
| R1 P2-1 状态误标 | **口径已修正，但闸门可绕过** | 审计表改为“部分修复”（`docs/audits/2026-11-14-execution-loop-audit.md:338`）；但新闸门见 N3 |
| R2 死信计入 backlog | **已修复** | `a1b2c3d4e5f6` 重定义 `list_active_tenants()`；`consumer.py:170-176` 跳过纯死信租户；新增集成断言 |
| R3 响应契约覆盖 | **基本修复** | Project/Episode/Script 路由挂 `response_model`；`_RESOURCE_SERIALIZERS` 扩到 5 个对象；但豁免台账仍陈旧（N4） |
| R4 吊销查询 500 | **已修复** | `packages/identity/token_revocation.py:35-57` 捕获异常转 `AUTH_UNAUTHENTICATED`；`tests/unit/test_token_revocation.py` 覆盖 |
| R5 资产并发幂等 | **已修复** | `asset_service.py:64-72` 抽出 `_same_content`；快速路径与并发路径共用；新增 2 条负测 |
| R6 坏 YAML traceback | **已修复** | `load_yaml_mapping` 捕获 `YAMLError` 转 ValueError；新增 4 条测试 |
| R7 action_registry 裸异常 | **已修复** | `action_registry.py:55-95` 全量转 `DomainError(INTERNAL_ERROR)`；新增 4 条参数化负测 |
| R8 错误路径只给文件名 | **已修复** | `load_yaml_mapping` 使用完整 `path`；新增完整路径断言 |
| R9 熔断缺失 | **已实现，但引入新缺陷** | `packages/shared/circuit_breaker.py`、`webhook.py`、ADR-R13；见 N1/N2 |

结论：**上一轮的代码级问题基本闭环；本轮的新问题集中在新加的 R1 闸门和 R9 熔断。**

---

## 3. 新 findings

### N1（P2）配置写“阈值 0 = 关闭熔断”，实际会让 relay 启动崩溃

**证据**

- 配置注释明确：`packages/shared/config.py:65-68`
  “0 表示关闭熔断（退化永远放行）”。
- relay 入口按此派生开关：
  `workers/relay/__main__.py:48-50`
  `circuit_enabled=...failure_threshold > 0`，但仍把原值 0 传给 dispatcher。
- `WebhookDispatcher` 原样传给 `CircuitBreaker`：
  `workers/relay/webhook.py:81-85`。
- `CircuitBreaker.__init__` 在 `enabled` 判断之前先校验阈值：
  `packages/shared/circuit_breaker.py:62-65`
  `if failure_threshold < 1: raise ValueError(...)`。

**实测**

```
WebhookDispatcher(..., circuit_failure_threshold=0, circuit_enabled=False)
→ ValueError: failure_threshold 必须 >= 1
```

**影响**

按文档把阈值设为 0 关闭熔断时，只要配置了 webhook URL，relay 启动即失败。
这是“配置开关不可用”，不是运行时边缘问题。

**修复方向**

- `CircuitBreaker` 在 `enabled=False` 时跳过阈值校验；或
- `WebhookDispatcher`/`__main__` 在关闭时传 `max(1, threshold)`；
- 补一条“threshold=0 + enabled=False 可构造”的测试；现有
  `tests/unit/test_circuit_breaker.py:157-160` 反而断言 0 必须抛错，需同步调整。

---

### N2（P2）4xx 会打开端点熔断，且熔断拦截被当成失败尝试，可能批量死信

**证据**

- webhook 对任何 `status_code >= 300` 都记端点失败：
  `workers/relay/webhook.py:109-113`。
- 熔断 OPEN 时抛 `WebhookCircuitOpenError`：
  `workers/relay/webhook.py:92-93`。
- relay 对所有异常统一走 `_FAIL`，递增 attempts：
  `workers/relay/consumer.py:237-252`。
- 熔断阈值默认 5：
  `packages/shared/circuit_breaker.py:120-122`。

**实测（400 响应，阈值 1）**

```
第 1 条事件 → WebhookDeliveryError，实际外发 1 次
第 2 条事件 → WebhookCircuitOpenError，实际外发仍为 1 次
```

即：一条事件的 400 响应就能把整个端点打成 OPEN，后续事件全部被拦截。

**影响**

1. **4xx 是事件级问题，不是端点故障**：签名错误、payload 不合法、单条事件被拒
   都会返回 4xx；把它计入端点熔断，会让一条毒事件误伤所有健康事件。
   默认阈值 5 时，同一条毒事件重试 5 次即可打开熔断；阈值 1 时一次 400 就能打开
   （上面的实测）。
2. **熔断拦截仍计入 attempts**：被 `WebhookCircuitOpenError` 拦截的事件没有真正
   外发，却会按 `_FAIL` 递增 attempts，达到 `max_attempts` 后进入死信。
   下游短时故障 + 熔断窗口，可能把一批从未尝试投递的事件打成死信。
3. 这与 ADR-R13 “事件不丢弃、至少一次”的目标相冲突——死信虽可人工重放，
   但自动链路已经放弃。

**修复方向**

- 熔断只对“端点级失败”计数：传输错误、5xx、429；4xx 不 `record_failure`，
  仍按事件失败走退避/死信（或直接按业务错误分类）。
- `consumer.drain_tenant` 对 `WebhookCircuitOpenError` 单独处理：
  不调用 `_FAIL`、不递增 attempts，只保留未投递状态，等熔断恢复后重试。
- 补两条测试：400 不应打开熔断；熔断 OPEN 时事件 attempts 不应增加。

---

### N3（P2）转移入口债务闸门可被“删条目/删文件”绕过，且坏形状会崩

**证据**

- `scripts/contract_check.py:1084-1087`：
  `STATE_GAPS_PATH` 不存在时直接 `PASS`。
- `:1089-1092`：`transition_entry_gaps` 缺失时 `declared = {}`，随后直接 PASS。
- `:1096-1121`：只遍历“已声明”的条目，没有反向检查
  “已实现状态机中仍无状态变更 action 的对象是否必须登记”。
- `_status_change_action_keys()` 扫描的是**完整 action_registry**，不是
  `action_implementations.yaml` 的 implemented 集合（`:1050-1066`）。
  按“契约先登记、再实现”的流程，先登记 `PROJECT.STATUS.CHANGE` 并标记
  `not_implemented`，闸门就会认为“入口已补上、债务陈旧”，要求删掉债务条目——
  但此时 HTTP 入口还不存在。
- `:1102-1106`：`detail` 被假定为 mapping；若 YAML 写成字符串，`.get` 抛
  `AttributeError`。
- `main()` 只捕获 `ValueError`（`:1291-1307`），所以这个 `AttributeError`
  仍会带 traceback 退出。

**实测**

```
state_machine_gaps.yaml 写成 gaps: {}（无 transition_entry_gaps）
→ [PASS] 显式登记 0 项

transition_entry_gaps:
  Project: oops
→ AttributeError: 'str' object has no attribute 'get'
```

**影响**

这正是 R1 想防的“绿着但不可执行”：

- 删掉 `Project`/`Episode` 两条债务 → 闸门 PASS，P2-1 重新静默；
- 删掉整个 `state_machine_gaps.yaml` → 闸门 PASS；
- 债务文件写坏 → 不是结构化 FAIL，而是 traceback；
- 先登记 action、后实现（本项目要求的顺序）会被误判成“陈旧债务”，
  反而逼着人删掉真实缺口。

另外，`tests/unit/test_contract_check.py` 中**没有任何**
`check_transition_entry_gaps` 的用例，新增闸门本身没有被测试锁住。

**修复方向**

- 增加反向完备性检查：遍历 `_STATE_MACHINE_SOURCES`，若对象没有状态变更 action
  且状态机存在非初始边，则必须存在对应债务条目，否则 FAIL。
- `STATE_GAPS_PATH` 缺失不应无条件 PASS；至少当存在已知无入口对象时 FAIL。
- 对 `detail` 做 `isinstance(dict)` 校验，坏形状转 `ValueError`（统一 `[FAIL]`）。
- 陈旧判定只看**已实现**的状态变更 action（与 `action_implementations.yaml`
  的 implemented 集合或运行时路由对齐），不能只看契约登记。
- 补 4 条负向测试：删条目、删文件、坏形状、陈旧条目。

---

### N4（P3）资源 schema 豁免台账与实际契约矛盾，且闸门不识别“冗余豁免”

**证据**

- `object_schemas.schema.json` 已包含三个对象：
  `Project`（`:6678`）、`Episode`（`:6739`）、`ScriptRevision`（`:6804`）。
- 但豁免台账仍写“未进入 object_schemas”：
  `contracts/runtime/resource_schema_exemptions.yaml:17-19`。
- `check_implemented_resource_schemas` 只检查“豁免了但没被任何已实现 action 引用”
  的孤儿条目（`scripts/contract_check.py:487-496`），**不检查**
  “已有 schema 却仍登记豁免”的冗余条目。
- 现有测试还把“4 条豁免”写死为正确状态：
  `tests/unit/test_contract_check.py:235-237`。

**影响**

当前 `_RESOURCE_SERIALIZERS` 仍包含这三个对象，所以直接删除 schema 会被
`check_schema_objects` 拦住；但豁免台账的语义已经失真，且测试把陈旧状态当成
正确基线。未来若移除某个 serializer 探针，冗余豁免就会变成真正的“假绿后门”。

**修复方向**

- 删除 Project/Episode/ScriptRevision 三条豁免（只保留 ScriptLock）。
- 增加 `set(exemptions) & set(defs) → FAIL` 的冗余豁免检查。
- 更新测试中的“4 条豁免”断言。

---

## 4. 工程铁律第二轮判定

| 铁律 | 判定 | 说明 |
|---|---|---|
| 1 不屎山框架 | **代码现状通过；自动化仍缺** | AST 扫描 0 反向依赖；仍无 `tests/architecture` / import-linter |
| 2 不屎山代码 | **明显改善；仍有补丁** | R2/R4/R5/R6/R7/R8 是系统性修复；N3 闸门可绕过、N4 豁免台账陈旧 |
| 3 高并发/高标准 | **有熔断但语义有缺陷** | 限流/幂等/SKIP LOCKED/死信/可观测全在；N1/N2 是新增熔断的边界缺陷 |
| 4 AI 辅助不降质量 | **全量测试通过；新闸门无测试** | pytest 全绿；但 `check_transition_entry_gaps` 零测试 |
| 5 防呆/自动化/系统根治 | **部分通过** | P2-1 口径已诚实；但 N3 可删债务绕过、N4 豁免台账失真 |

---

## 5. 建议修复顺序

1. **N1 先修**：让 `threshold=0` 真正关闭熔断，补构造测试；这是配置开关的直接 bug。
2. **N2 修熔断语义**：4xx 不计端点失败；熔断拦截不计 attempts；补 400 与 OPEN 两条测试。
3. **N3 修闸门完备性**：反向检查未登记债务；坏形状转 ValueError；补 4 条负测。
4. **N4 清豁免台账**：删三条冗余豁免，加冗余检查，更新测试基线。
5. 可选：补 `tests/architecture` 或 import-linter，把铁律 1 从人工扫描变成 CI 约束。

---

## 6. 未验证 / 留白

1. 本轮全量 `pytest` 与 `alembic check` 已通过；未做并发压测、真实 webhook 故障演练。
2. N2 的“批量死信”是从代码路径推导并用 400 单测复现了熔断开闸，未构造
   多事件/多轮 relay 的完整时序；建议修复时补集成测试。
3. 未修改任何代码；本报告为唯一新增产物。
