# Runbook：限流后端（Redis）降级与熔断

适用：Redis 不可达/超时、`rate_limit_backend_errors_total` 持续增长、
`rate_limit_circuit_state` 出现 `open`、请求延迟出现固定量级的周期性台阶。
实现入口：`packages/shared/rate_limit.py`、`packages/shared/circuit_breaker.py`。
实测依据：`docs/perf/2026-11-18-single-instance-baseline.md` §3.3。

## 0. 先记住三条红线

1. **限流是 fail-open，不是 fail-closed。** Redis 不可用时放行请求并标记
   `degraded=True`，不拒绝流量。这是刻意的：限流是保护措施，不是授权判定，
   把保护措施的故障升级成「全站 503」是拿可用性换一个次要目标。
2. **redis 不进 `api` 的 `depends_on`，也不进就绪链。** 正因为 fail-open，
   Redis 属于「降级依赖」而不是「启动依赖」；写进 `depends_on` 会让一次限流
   后端抖动变成编排层面的 API 不可用，与降级设计正好相反。
3. **不要为了「让限流生效」在故障期重启 API。** 重启会清空熔断器状态，
   让每个实例重新付出「连接超时」的代价，反而放大故障。

## 1. 降级契约长什么样

| 状态 | 行为 | 每请求代价 |
|---|---|---|
| CLOSED（正常） | 正常 `INCR`/`EXPIRE` 判定 | 一次 Redis 往返 |
| 连续失败达到阈值 → OPEN | 一个网络包都不发，直接放行并置 `degraded` | 纯本地判定 |
| OPEN 满冷却期 → HALF_OPEN | 只放**一个**探测请求（单飞），其余继续放行 | 一次往返（仅探测） |
| 探测成功 → CLOSED / 失败 → OPEN | 恢复或重新短路 | 同上 |

关键判据**不是「API 不阻塞」**——修复前每请求固定阻塞 1.02s 也满足这句话。
真正的判据是「**故障期每请求开销不随故障时长增长**」：

> 实测（本机，2026-11-18）：前 3 次各约 1.02s（真连、等超时），
> 第 4 次起 **12.6ms**（熔断短路），冷却期满第 9 次回到 1.03s（半开探测），
> 探测失败后第 10 次再回 12.9ms。完整数据见基线文档 §3.3。

## 2. 参数与调优边界

| 配置项 | 默认 | 含义 |
|---|---|---|
| `rate_limit_enabled` | `true` | 关掉即退化为全部放行（仅用于无 Redis 的本地单测） |
| `rate_limit_per_minute` | `120` | 固定窗口内允许的请求数 |
| `rate_limit_window_seconds` | `60` | 固定窗口长度 |
| `rate_limit_circuit_failure_threshold` | `3` | 连续失败多少次进入 OPEN；`<=0` 关闭熔断 |
| `rate_limit_circuit_recovery_seconds` | `30.0` | OPEN 冷却期，之后转 HALF_OPEN |

调优口径：**门槛低于超时次数才有意义**。若 `failure_threshold` 偏大，
故障期会有较多请求各自等满超时；偏小则网络抖动一下就被判为故障。
`3` 是针对「Redis 客户端默认超时约 1s」选的，超时时间改了要同步复核。

## 3. 判断当前处于哪一档（30 秒）

```bash
# 熔断器当前状态（1=当前态）
curl -s localhost:8000/metrics | grep rate_limit_circuit_state

# 真实失败次数（只在「尝试过并失败」时递增，熔断期不增）
curl -s localhost:8000/metrics | grep rate_limit_backend_errors_total

# 放行/拒绝分布
curl -s localhost:8000/metrics | grep rate_limit_decisions_total
```

| 观测 | 含义 | 处置 |
|---|---|---|
| `circuit_state{state="closed"}=1` | 正常 | 无事 |
| `state="open"=1` + `backend_errors` 不再增长 | 已短路，正在保护自己 | 修 Redis，勿重启 API |
| `state="half_open"=1` 反复回 `open` | Redis 仍未恢复 | 继续修 Redis |
| `backend_errors` 快速增长但 `state` 仍 `closed` | 未达阈值或熔断被关闭（`threshold<=0`） | 检查配置是否被人为调大 |

## 4. 处置步骤

1. **确认是 Redis 侧问题，不是限流逻辑**：`docker compose ps redis`、
   `redis-cli -p 16379 ping`。容器内应用走服务名 `redis:6379`，宿主走 `16379`。
2. **止血（可选）**：若 Redis 短期无法恢复且需要临时关闭限流以免噪声告警，
   设 `RATE_LIMIT_ENABLED=false` 并滚动重启——注意这会**完全放弃限流**，
   只在确认上游有网关级限流时才可接受。
3. **恢复**：Redis 起来后无需干预，冷却期满自动 HALF_OPEN → CLOSED。
   若想立即恢复，重启**单个**实例即可（会清空该实例熔断状态）。
4. **取证**：保留 `backend_errors_total` 的增长率与熔断状态时间线，
   用于判断是否需要调整阈值。

## 5. 已知残余风险（不得省略）

- **fail-open 期间平台实际不限流。** 若 Redis 长时间不可用，限流失效，
  上游若无网关限流，存在被打爆的可能。这是显式接受的权衡，不是疏漏；
  需要更严格语义时应在上层（网关/WAF）补，而不是把限流改成 fail-closed。
- **固定窗口限流的固有突刺**：窗口边界处允许两倍速率，属算法特性。
- **进程内熔断器不跨实例共享**：每个 API 实例独立判定，N 个实例会各自
  付出前几次超时代价。水平扩展后该代价随实例数线性增长，需在阶段 B
  （多实例压测）中实测确认可接受。
