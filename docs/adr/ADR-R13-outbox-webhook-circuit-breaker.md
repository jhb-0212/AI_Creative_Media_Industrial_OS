# ADR-R13：Outbox Webhook 端点熔断（与事件级退避/死信分层）

- 状态：已接受（R9 落地）
- 日期：2026-11-16
- 关联：`docs/audits/2026-11-16-code-and-iron-law-review.md` R9；
  ADR-R06-2（outbox 分区/保留期）、ADR-R12（死信与令牌吊销）
- 替代：无（此前 README 铁律 3 列「熔断」但无代码实体，见「背景」）

## 1. 背景

README 铁律 3 把「熔断」列为硬要求，但仓库长期没有 `CircuitBreaker` 组件：
对外 webhook 只有「5s 超时 + 指数退避 + 死信」（`workers/relay/webhook.py`、
`consumer.py::_FAIL`）。2026-11-16 审查（R9）把这一项列为 P3 缺口：

> 下游 webhook 持续故障时，每个事件仍会独立尝试（最多 3 次）直到死信；
> 没有「连续失败→快速失败」的熔断，故障期间会持续放大对外请求。

这是两个不同层次的问题，之前被「退避 + 死信」掩盖了：

| 层次 | 解决什么 | 现有实现 |
|---|---|---|
| **事件级** | 单条事件失败后不再无限重试 | 指数退避（10s×2^n，上限 640s）+ `max_attempts` 后死信 |
| **端点级** | 下游整体故障时，**新**事件不再逐条撞墙 | 此前缺失 → 本 ADR 补齐 |

退避/死信管的是「同一条事件的失败生命周期」；熔断管的是「下游健康度」——
两者正交，不互相替代。下游持续 503 时，退避+死信会让每条新事件都白撞 3 次墙，
熔断则在第 N 次连续失败后直接快速失败，把对外请求量从「每事件 3 次」压到
「每事件 0 次 + 一个半开探测」。

## 2. 决策

### 2.1 引入进程内三态熔断器（`packages/shared/circuit_breaker.py`）

标准熔断器状态机：

- **CLOSED**：正常放行；连续失败达 `failure_threshold`（默认 5）→ OPEN；
- **OPEN**：快速失败（不真正外发）；`recovery_timeout`（默认 30s）后 → HALF_OPEN；
- **HALF_OPEN**：只放行**一个**探测请求；成功回 CLOSED、失败回 OPEN 并重新计时。

接入点：`WebhookDispatcher.__call__` 在发请求前先 `breaker.allow()`，OPEN 时抛
`WebhookCircuitOpenError`（`WebhookDeliveryError` 子类，对 relay 语义不变——
不 ack、走退避）。真实失败/成功分别 `record_failure()`/`record_success()`。

### 2.2 进程内、无共享存储

熔断状态只在本 relay 副本内维护。**刻意不做** Redis/分布式锁共享状态：

- 目标不是「全副本对下游健康度有一致判定」，而是「每个副本都不持续放大请求」；
  下游故障时每个副本独立进入 OPEN 已经达成这个目标。
- 分布式熔断需要引入共享状态 + 心跳 + 一致性协调，复杂度换来的收益在当前
  规模（内部平台、单一 webhook 端点）不成立。
- 跨副本的全局观测由 Prometheus 聚合 `outbox_webhook_circuit_state` 指标 + 告警
  承担，不在应用层做汇总。

### 2.3 时钟注入（可测试性）

`CircuitBreaker` 接受 `clock: Callable[[], float]`，默认 `time.time`。测试注入
假时钟即可确定性驱动「冷却期满 → 半开探测」时序，不依赖真实 `sleep`。
惰性迁移：OPEN 到 HALF_OPEN 的迁移在**下次 `allow()` 调用时**判定，不起定时器。

### 2.4 半开探测的原子性

`allow()` 从读状态到改状态之间无 `await`，asyncio 单线程内天然原子；「只放行
一个探测」用布尔标志 `_probe_in_flight` 实现，探测未出结果前其余请求继续快速失败。

### 2.5 配置与开关

`packages/shared/config.py` 新增：

- `outbox_webhook_circuit_failure_threshold`（默认 5，0 = 关闭熔断）；
- `outbox_webhook_circuit_recovery_seconds`（默认 30.0）。

`threshold <= 0` 时 `circuit_enabled=False`，熔断器退化为「永远放行」——与
`RateLimiter` 的退化约定一致，未配置/显式关闭时不改变调用方代码。

## 3. 明确不做（本次边界，登记取舍）

- **不做分布式熔断**（见 2.2）。
- **不做按 endpoint 的多实例熔断表**：当前只有单一 webhook 端点（单值配置），
  熔断器按「一个 dispatcher 一个 breaker」绑定；未来多端点时再引入
  `endpoint → CircuitBreaker` 映射（指标已预留，但端点标签仍不加，避免高基数）。
- **不做自适应阈值/半开探测窗口**：固定阈值 + 固定冷却期足够当前规模，
  自适应调节会把「行为随负载变化」引入排障面（与 `outbox_relay_batch_size`
  的固定参数决策一致）。
- **不改 relay 的失败语义**：熔断快速失败仍走 `_FAIL` 退避/死信路径，
  事件不丢弃、不改变「至少一次」投递契约。

## 4. 落地物

- `packages/shared/circuit_breaker.py`：`CircuitBreaker` + `CircuitState`。
- `workers/relay/webhook.py`：`WebhookDispatcher` 接入熔断，新增
  `WebhookCircuitOpenError`。
- `workers/relay/__main__.py`：熔断参数从 config 透传。
- `packages/shared/metrics.py`：`OUTBOX_WEBHOOK_CIRCUIT_STATE`（三值 Gauge）。
- `packages/shared/config.py`：两个熔断配置项。
- 测试：`tests/unit/test_circuit_breaker.py`（状态机）+ `test_outbox_webhook.py`
  （熔断接入路径）。

## 5. 演进路线

1. 多端点时引入 `endpoint → CircuitBreaker` 映射（当前单端点，不预建）。
2. 若下游故障模式需要全局一致（多副本共享熔断），评估 Redis 共享状态。
3. 半开探测可加「探测成功阈值」（连续 K 次成功才 CLOSED，避免抖动），
   当前「一次成功即 CLOSED」对单一 webhook 足够。
