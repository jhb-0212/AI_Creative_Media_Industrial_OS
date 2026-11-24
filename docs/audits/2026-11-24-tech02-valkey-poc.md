# TECH-02 Valkey PoC 核验报告

- 日期：2026-11-24
- 依据：ADR-R44 §5.2（Valkey，五项通过条件）、§4.1「裁决 ≠ 切换」、F-2/F-3
- 性质：append-only 审计报告；只登记 PoC 事实，不构成技术栈切换授权
- 证据目录：`docs/audits/tech02-valkey-poc/`（取证脚本 `run_poc.py` + 运行日志 `run.log`）
- 证据等级口径：`docs/standards/verification-and-audit-annotation-standard.md`

## 0. 结论摘要

| 条件 | ADR §5.2 要求 | 结论 | 标注 |
|---|---|---|---|
| ① | redis-py 客户端 + 连接/INCR/EXPIRE/超时/重连/连接池关闭/熔断/高并发计数 | PASS | `[V-VERIFIED][E4+E5]` |
| ② | 9.1.4 基准、9.2.0 独立测试各跑同一矩阵 | PASS | `[V-VERIFIED][E5]` |
| ③ | 模拟不可用：fail-open + 熔断语义准确 + 指标可见 | PASS | `[V-VERIFIED][E5]` |
| ④ | 评估切换限流窗口计数重置空档 + 写明运维窗口 | PASS | `[V-VERIFIED][E5+E1]` |
| ⑤ | 无授权语义回退 | PASS | `[V-VERIFIED][E2]` |

五项全过；取证装置经变异证伪（见 §3），正常态 17/17 断言 PASS，变异态精确报 FAILED。

## 1. 前置取证（决定 PoC 边界，不可跳过）

| 项 | 实测事实 | 证据 |
|---|---|---|
| 被测服务端 | Valkey **9.1.4**（6380，digest `3ab0eca823eb…`）、**9.2.0**（6382，digest `1c2b25b09f35…`），`INFO valkey_version` 实测一致并自报 `redis_version:7.2.4` | `[V-VERIFIED][E5]` |
| 当前生产后端 | `redis:7` 镜像实际为 **Redis CE 7.4.12**（镜像 ID `2e3eb11462c4…`，`redis-server --version` 实测） | `[V-VERIFIED][E5]` |
| 迁移兼容性边界 | 按 F-3，Redis CE 7.4+ 数据文件与 Valkey 不兼容 | `[V-VERIFIED][E1]` |

**PoC 边界**：本轮只验证协议/命令层与 `RateLimiter` 真实用法的兼容（INCR + 首次 EXPIRE 固定窗口），**不声称 RDB/AOF 数据文件平滑迁移**——7.4.12 → Valkey 若日后切换需另定数据迁移方案（如双写/重建计数），不得据本报告推断可平滑迁移。

## 2. 逐项取证结果

### ① redis-py 客户端 + 全套计数/降级能力 `[V-VERIFIED][E4+E5]`
- 客户端：Redis-Py **8.1.0**（现有依赖，**未新增/未改**依赖锁）。
- 每个版本实测：版本指纹、`INCR`→`EXPIRE(30)`→`TTL=30`、100 并发 `INCR` 精确计数=100、`RateLimiter(limit=3)` 连续 5 次 `allowed=[T,T,T,F,F]`、`aclose()` 后连接池 `in_use=0`。
- 连接池关闭判据说明：Redis-Py 8.x 的 `aclose()` 释放连接资源，之后再 `ping` 会惰性重建——这是正常语义，不作为「永久禁用」判据（初版判据误判此项，已修正为 in_use 归零）。

### ② 双版本同一矩阵 `[V-VERIFIED][E5]`
- 9.1.4 与 9.2.0 执行完全相同的断言集（脚本 `ENDPOINTS` 两版本循环），各 5 项全 PASS。
- 不混用：两版本用各自的客户端连接、各自清理测试键（`poc:` 前缀）。

### ③ 不可用注入：fail-open + 熔断 + 指标 `[V-VERIFIED][E5]`
- 指向无监听端口 6399（1s 连接超时）：两次 `acquire` 均 `allowed=True, degraded=True`（fail-open 正确）。
- `rate_limit_backend_errors_total` +2（后端故障可观测）。
- `CircuitBreaker(failure_threshold=2)` 连续失败后 `state=open`；OPEN 期一次 `acquire` 耗时 0.000s（零网络包，短路正确，非 1s 超时）。
- `rate_limit_circuit_state{state="open"}=1.0`（熔断状态指标可见；初版漏接 `state_observer` 导致误报，已按生产 `from_settings()` 接线修正）。

### ④ 限流窗口切换空档 `[V-VERIFIED][E5+E1]`
- 实测固定窗口语义：key 窗口内值=1，`EXPIRE(2)` 到期后值=None → 计数归零确认。
- **运维窗口结论**：切换后端瞬间，旧后端已累计的窗口计数不会迁移到新后端，新后端从 0 起算。最坏情况下，一个窗口周期（默认 `rate_limit_window_seconds`，见配置）内，个别调用方可获得「旧桶已用尽、新桶重新计数」的额外额度。
- 处置建议（PoC 层面，不改生产）：切换应选择在**窗口边界附近**执行，并在此期间加强 `http_requests_total` 429 比例监控；该空档是固定窗口方案的固有属性，不是 Valkey 引入的新问题（Redis→Redis 重启同样存在）。

### ⑤ 无授权语义回退 `[V-VERIFIED][E2]`
- `RateLimiter.acquire()` 仅用调用方传入的 `identity` 字符串（中间件按令牌指纹/IP 构造）计数，不读取 `Principal`、不参与鉴权判定（AST/源码核对）。
- 限流为保护性设施、fail-open 明示于 `ports` 语义；引入 Valkey 不改变任何授权判定路径。

## 3. 取证装置自检（证明 PASS 非装置失明）

| 检查 | 做法 | 结果 |
|---|---|---|
| 正常态 | `run_poc.py` 全量 | `[PASS] 17/17`，`POC_RESULT PASS` |
| 变异证伪 | `TECH02_SELFTEST_MUTANT=1` 把 9.2.0 端点指向坏端口 | `[FAIL]` 精确定位 `①② 9.2.0 版本指纹`（连接不可用 TimeoutError），`POC_RESULT FAIL`，无 ERROR 逃逸 |
| 装置缺陷修正 | 初版坏状态抛未捕获异常（ERROR）与 FAILED 混淆 | 已修：连接失败记为 FAILED 断言，ERROR/FAILED 可区分 |

变异点跟着被测事实走（换真实端点，非改判据期望值），符合「修装置不修产品」的判据——前两次 FAIL（连接池关闭、熔断指标）是装置判据错误，已修正；变异 FAIL 证明装置对真实故障敏感。

## 4. 边界与红线（重申）

- 取证装置在 `docs/audits/tech02-valkey-poc/`，**未改** `packages/`、`adapters/`、`pyproject.toml`、Compose、冻结清单、`.env`；测试键 `poc:` 前缀用后清理。
- 本次改动后 `freeze_manifest --check`、`contract_check`、`architecture_check` 全 PASS（EXIT=0）。
- **裁决 ≠ 切换**：现行生产基线仍是 Redis 7 + redis-py。本报告是 PoC 授权范围内的取证，不构成切换批准。正式切换须：PoC 全过 + 独立 ADR 修订 + 冻结重签 + 数据迁移方案（因 F-3 不兼容不可省略）。

## 5. 待裁决 / 缺口

- `[V-UNVERIFIED]` Redis CE 7.4.12 → Valkey 的 RDB/AOF 数据文件迁移可行性（本 PoC 明确不在范围；按 F-3 判定为不兼容，需独立方案）。
- 长稳/容量/故障恢复自动化不在本 PoC 范围（§5.2 未要求）；单次运行证据不覆盖生产可靠性。
