# 性能验收方案与故障演练（F09）

> 状态：本方案落地了**可复现的压测入口与验收阈值**，但真实压测证据仍需在目标
> 环境执行。本文档把「无压测证据」从一句诚实声明，升级为「有脚本、有矩阵、
> 有阈值、有 checklist」的可执行验收方案。

## 1. 背景与定位

第三轮审查（F09）指出：代码已具备连接池、Redis 限流、Outbox 重试、熔断、
OpenTelemetry、Prometheus 指标等基础设施，但**没有压测与故障演练实测证据**。
`pool_size=10 / max_overflow=20` 只是单进程连接池参数，不代表平台能承受
每秒 1000 次请求。

本方案不声称「压测已通过」，而是把「该测什么、怎么测、什么算通过」落到
可执行产物，使压测从「想起来才做」变成「有标准可依」。

## 2. 压测目标与验收阈值

阈值是**首期基线**，不是最终容量承诺；达到后按业务增长重新标定。

| 指标 | 验收阈值（首期） | 说明 |
|------|-----------------|------|
| 探针 GET /readyz | P99 < 50ms，QPS ≥ 500/实例 | 验证基座吞吐，排除框架开销 |
| 只读 GET /assets/{id} | P99 < 200ms，错误率 < 0.1% | 验证 RLS 查询 + 鉴权路径 |
| 写 POST /assets | P99 < 500ms，错误率 < 1% | 验证事务 + Outbox + 乐观锁 |
| 并发上限 | 无 5xx 雪崩，限流按阈值返回 429 | 验证背压，不静默堆积 |

## 3. 压测矩阵

按「单实例基线 → 多实例扩展 → 故障注入」三阶段递进：

### 阶段 A：单实例基线
- 目标：确认单进程在 pool_size=10/max_overflow=20 下的真实吞吐。
- 步骤：
  1. `docker compose up -d postgres redis migrate api`（本地/单机编排）；
  2. 跑探针压测：`python scripts/perf/load_test.py --probe-only --concurrency 50 --requests 5000`；
  3. 跑只读压测：`python scripts/perf/load_test.py --path /assets/{id} --token <token>`；
  4. 记录 QPS / P50 / P95 / P99 / 错误率，对照上表阈值。

### 阶段 B：多实例扩展
- 目标：验证 API 水平扩展后，连接池总量与 PostgreSQL `max_connections` 不互相
  拖垮（单实例 pool=10 上限 30，N 实例时 `max_connections` 必须 ≥ N×30 + 余量）。
- 步骤：
  1. `docker compose up --scale api=3`；
  2. 重复阶段 A 压测，对比吞吐是否线性（近似）增长；
  3. 观察 PG `max_connections` 与连接池排队，确认无连接耗尽。

### 阶段 C：故障注入
- 目标：验证降级/熔断/重试在故障下不雪崩。
- 场景清单（每项都记录「恢复时间 RTO」）：
  - [ ] Redis 宕机：限流应 fail-open 且 `rate_limit_backend_errors_total` 递增，API 不阻塞；
  - [ ] PG 主库短暂不可用：连接池报错后应能重连（pool_recycle=1800），不永久假死；
  - [ ] Outbox Relay 停摆：事件积压，恢复后应重放且不丢、不重（幂等）；
  - [ ] Webhook 目标超时：熔断器打开，快速失败而非无限重试；
  - [ ] 高并发下乐观锁冲突：`CONCURRENT_MODIFICATION` 可重试语义，不静默丢写。

## 4. 可观测性对照

压测全程对照 Prometheus 指标（`infra/monitoring/prometheus/outbox-rules.yml`
已含 Outbox 告警规则），确认「压测结论」与「指标曲线」一致，而非只看脚本输出：

- `http_request_duration_seconds`：延迟分位；
- `rate_limit_rejections_total`：限流命中；
- `outbox_relay_*`：Relay 吞吐与积压；
- `rate_limit_backend_errors_total`：限流后端降级计数。

## 5. 验收报告模板

每轮压测产出一份报告，至少含：

```
- 环境：实例数 / PG 版本 / 连接池参数 / 数据规模
- 压测：脚本 / 并发 / 请求数 / 时长
- 结果：QPS / P50 / P95 / P99 / 错误率 / 状态码分布
- 对照：上表阈值，逐项 PASS/FAIL
- 结论：达标 / 未达标 + 瓶颈定位（连接池 / 慢查询 / 锁竞争 / 队列背压）
```

## 6. 与「未覆盖范围」的关系

本文档落地后，README「高并发/故障恢复」一栏从「无压测证据」改为「有可复现
压测方案，实测证据见 docs/perf/ 下验收报告」。但**方案落地 ≠ 压测通过**——
在目标环境跑出报告前，该项仍应诚实标注「待实测」。
