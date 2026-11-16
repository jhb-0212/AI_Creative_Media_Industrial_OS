# 第二轮审查（N1-N4）修复记录

- 日期：2026-11-16
- 上游报告：`docs/audits/2026-11-16-review-round2.md`（基线 `8b75fb7`）
- 范围：仅修该报告的 4 条新 findings（N1 P2 / N2 P2 / N3 P2 / N4 P3），不改契约语义、不做无关重构。

## 0. 一句话结论

4 条全部落地，且每条都补了**能复现原缺陷的负向测试**；五道闸门复跑全绿
（`contract_check` / `ruff` / `mypy` 92 files / `pytest` 308 cases / `alembic check` 无漂移）。

## 1. N1（P2）阈值 0 ≠ 关闭熔断，而是启动崩溃

| 项 | 内容 |
|---|---|
| 根因 | `CircuitBreaker.__init__` 先校验 `failure_threshold >= 1`，再判断 `enabled`；而配置口径用「阈值 0」表达关闭 |
| 修复 | `packages/shared/circuit_breaker.py`：仅在 `enabled=True` 时校验阈值/冷却期；关闭态跳过校验并把阈值归一化到 `>=1`；新增只读属性 `enabled` |
| 配套 | `workers/relay/__main__.py` 补注释说明「enabled 由阈值派生、原值透传由 breaker 归一化」 |
| 测试 | `tests/unit/test_circuit_breaker.py::test_threshold_zero_means_disabled_and_is_constructible`（替换原「0 必须抛错」断言，另立 `test_invalid_parameters_rejected_when_enabled`）；`test_outbox_webhook.py::test_threshold_zero_disables_circuit_without_crashing` 走真实 dispatcher 构造路径 |

## 2. N2（P2）4xx 打开端点熔断 + 熔断拦截被当成失败尝试

| 项 | 内容 |
|---|---|
| 根因 ① | `webhook.py` 对任何 `status_code >= 300` 都 `record_failure()`，一条毒事件即可打爆端点熔断 |
| 根因 ② | 熔断 OPEN 抛的异常被 `consumer` 统一按 `_FAIL` 处理 → attempts+1 → 达上限进死信，等于把「从未外发」的事件判死 |
| 修复 ① | `webhook.py`：新增唯一判定口径 `is_endpoint_failure()`（传输错误、5xx、408/425/429 计入熔断；其余 4xx 不计入并 `record_success()`——端点能应答说明它活着） |
| 修复 ② | 新增 `workers/relay/errors.py`：`OutboxDeliveryError` / `DeliveryDeferredError`（「本次没外发」的窄语义）；`WebhookCircuitOpenError` 双继承 `WebhookDeliveryError` + `DeliveryDeferredError` |
| 修复 ③ | `consumer.drain_tenant`：捕获 `DeliveryDeferredError` → 不计 attempts、不写退避/死信，`RelayStats.deferred` 单独计数，并**立即结束本租户本轮**（端点级闸门对整批成立） |
| 测试 | `test_outbox_webhook.py`：400 连续三次都不开闸、熔断拦截是 `DeliveryDeferredError`；`tests/integration/test_outbox_relay_retry.py::test_deferred_delivery_does_not_consume_attempts`（`max_attempts=1` 下连跑 3 轮：attempts 仍为 0、无死信、无 ack） |
| 文档 | ADR-R13 新增「端点故障 ≠ 事件故障」与修订记录；§3 删除「熔断失败仍走 `_FAIL`」的旧结论 |

## 3. N3（P2）入口债务闸门可被删条目/删文件绕过、坏形状崩溃

| 项 | 内容 |
|---|---|
| 根因 | 只做正向遍历；文件/分区缺失即 PASS；陈旧判定看 `action_registry` 全量（不区分是否实现）；`detail` 直接 `.get` |
| 修复 | `scripts/contract_check.py`：<br>① `_declared_transition_entry_gaps()` 统一读+校验形状，坏形状抛 `ValueError`（由 `main` 转 `[FAIL]`，不再 traceback）；<br>② 反向完备性：`_STATE_MACHINE_SOURCES` 中「有转移边且无已实现状态变更 action」的对象必须登记债务，否则 FAIL（删条目、删文件都绕不过）；<br>③ 陈旧判定改为 `_status_change_action_keys(obj) & _implemented_action_keys()`——契约先登记后实现不算陈旧；<br>④ `_declared_state_gaps()` 同口径校验 `gaps` 分区，顺带修掉可达性闸门同款 `AttributeError` 隐患 |
| 测试 | `test_contract_check.py` 新增 6 条：当前契约 PASS、删 `Episode` 条目 FAIL、删文件 FAIL、坏形状 `ValueError`、陈旧条目（登记 AssetVersion）FAIL、契约已登记未实现仍 PASS |

## 4. N4（P3）豁免台账与实际契约矛盾

| 项 | 内容 |
|---|---|
| 修复 | `contracts/runtime/resource_schema_exemptions.yaml` 删除 `Project`/`Episode`/`ScriptRevision` 三条（三者均已进 `object_schemas`），仅保留 `ScriptLock`；表头补第 4 条规则 |
| 闸门 | `check_implemented_resource_schemas()` 新增冗余豁免检查：`set(exemptions) & set(defs)` 非空即 FAIL |
| 测试 | `test_check_resource_schemas_passes_on_current_contract` 断言豁免集合恰为 `{ScriptLock}`；新增 `test_check_resource_schemas_fails_for_redundant_exemption` |
| 文档 | ADR-R07 第 3 条补「已有 schema 的类型不得再登记豁免」及对应负向用例 |

## 5. 闸门复跑证据

| 闸门 | 结果 |
|---|---|
| `python scripts/contract_check.py` | PASS：豁免 1 项；入口债务 2 项（4 个状态机无陈旧/越界/漏登记） |
| `ruff check .` | `All checks passed!` |
| `mypy packages services apps workers` | `Success: no issues found in 92 source files`（+1 = `workers/relay/errors.py`） |
| `pytest tests` | 308 用例全绿（4 条 webhook + 1 条 relay 集成 + 6 条契约闸门为新增） |
| `alembic check` | `No new upgrade operations detected.` |

## 6. 仍然留白（诚实登记）

1. 未做并发压测与真实 webhook 故障演练；N2 的多副本时序仍靠单测+集成推导。
2. 反向完备性只覆盖 `_STATE_MACHINE_SOURCES`（4 个已实现状态机）；新增状态机必须
   同步加进该清单，否则不被此闸门覆盖——这一点未机器化（清单本身是人工维护的）。
3. 铁律 1 的「无反向依赖」仍是人工 AST 扫描；`tests/architecture` / import-linter 未落地。
