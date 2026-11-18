# 性能验收方案与故障演练（F09）

> 状态：**已有首轮本机单实例实测（2026-11-18），首期阈值多数未达标**。
> 实测数据与三个结构性发现见
> [`2026-11-18-single-instance-baseline.md`](./2026-11-18-single-instance-baseline.md)。
> 本方案仍不声称「性能验收通过」：阶段 B（多实例扩展）与阶段 C（故障注入）
> 尚未执行，且阶段 A 的数字来自单机环境，不能外推为生产容量。
>
> 历史：本文档最初只有「脚本、矩阵、阈值、checklist」，没有一行实测数字——
> 那个状态已被判定为「无压测证据」。本轮补上了证据，也补上了
> `--write-path` 模式（此前脚本只读，与阈值表要求的写路径 P99 直接矛盾）。

## 1. 背景与定位

第三轮审查（F09）指出：代码已具备连接池、Redis 限流、Outbox 重试、熔断、
OpenTelemetry、Prometheus 指标等基础设施，但**没有压测与故障演练实测证据**。
`pool_size=10 / max_overflow=20` 只是单进程连接池参数，不代表平台能承受
每秒 1000 次请求。

本方案不声称「压测已通过」，而是把「该测什么、怎么测、什么算通过」落到
可执行产物，使压测从「想起来才做」变成「有标准可依」。

## 2. 压测目标与验收阈值

阈值是**首期基线**，不是最终容量承诺；达到后按业务增长重新标定。

| 指标 | 验收阈值（首期） | 说明 | 2026-11-18 本机实测 |
|------|-----------------|------|--------------------|
| 探针 GET /readyz | P99 < 50ms，QPS ≥ 500/实例 | 验证基座吞吐，排除框架开销 | P99 352ms / QPS 161 ❌ |
| 只读 GET /assets/{id} | P99 < 200ms，错误率 < 0.1% | 验证 RLS 查询 + 鉴权路径 | P99 200.1ms / 0% ⚠️ 边界 |
| 写 POST /assets | P99 < 500ms，错误率 < 1% | 验证事务 + Outbox + 乐观锁 | P99 562ms / 0% ❌ 错误率达标 |
| 并发上限 | 无 5xx 雪崩，限流按阈值返回 429 | 验证背压，不静默堆积 | 119×201 + 381×429 ✅ |

上表最后一列是**单机单进程**数据（压测客户端与 API 同机），只用于登记当前
基线，不作为通过依据；目标环境复测前，这四行的判定都不改变。

## 3. 压测矩阵

按「单实例基线 → 多实例扩展 → 故障注入」三阶段递进：

### 阶段 A：单实例基线
- 目标：确认单进程在 pool_size=10/max_overflow=20 下的真实吞吐。
- 步骤：
  1. `docker compose up -d postgres redis migrate api`（本地/单机编排）；
  2. 跑探针压测：`python scripts/perf/load_test.py --probe-only --concurrency 50 --requests 5000`；
  3. 跑只读压测：`python scripts/perf/load_test.py --path /assets/{id} --token <token>`；
  4. **跑写路径压测**（事务 + Outbox + 乐观锁的唯一证据来源）：
     `python scripts/perf/load_test.py --write-path --project-id <perf-project> --token <token>`；
  5. 记录 QPS / P50 / P95 / P99 / 错误率，对照上表阈值。

- **写路径压测的两条硬前提**（2026-11-18 实测确认）：
  1. 必须把 `RATE_LIMIT_PER_MINUTE` 抬到远高于压测请求数，否则测出来的是
     限流器而不是系统（默认 120/分钟，500 请求会有 76% 得到 429）。
  2. 必须对**专用库或专用租户**执行：`asset_versions` 有 ADR-R11 触发器禁止
     DELETE（超级用户同样被拒），写入的压测数据**无法清理**。


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
