# ADR-R06：Outbox 事件投递的租户边界（待决策）

状态：**已决策：方案 B**（2026-11-14，用户拍板「不允许平台级 relay 持有跨租户读取权限」）
范围：`outbox.outbox_events` 的消费端（`workers/relay/` 已落地，其余 `workers/*` 仍为占位）

## 背景（以仓库现状为准）

- `outbox.outbox_events` 已有 `tenant_id NOT NULL` + RLS 策略，策略依据
  `app.current_tenant_id`（`packages/shared/rls.py:28`）。
- 应用连接角色 `app_role` 是 **NOBYPASSRLS**，只有 `INSERT / SELECT / UPDATE`
  权限（`data/migrations/versions/9e3b4c5d6e7f_*.py`、`d4e5f6a7b8c9_*.py`）。
- 写入侧 `record_event()` 不 commit，随业务工作单元一起提交
  （`packages/shared/outbox_writer.py:44`），保证「状态改了就一定有事件」。
- 迁移由超级用户 `postgres` 执行，应用运行时不用超级用户。

于是消费端立刻撞上一个二选一：**谁来读「所有租户」的待投递事件**。

## 方案 A：平台级 relay + 专用 BYPASSRLS 角色

单进程用一个新角色 `relay_role`（`BYPASSRLS`，只授予 `outbox` schema 的
`SELECT/UPDATE`）读全表，按 `published_at IS NULL` 分批投递并回写投递时间。

| 维度 | 评价 |
|---|---|
| 实现成本 | 低。一条迁移建角色授权，一个循环即可 |
| 运维成本 | 低。单进程、单游标、可水平扩展为「租约 + SKIP LOCKED」 |
| 顺序语义 | 天然按 `aggregate_id`/时间全局有序，易实现单分区有序 |
| 爆炸半径 | **大**。该角色一旦被误用/被攻破，可跨租户读全部审计负载 |
| 与决策 5 的关系 | **回退**。B/`8204ac5` 那轮正是把「跨租户读取」堵死在数据库层；再造一个跨租户读通道，等于把堵住的洞换个门开 |

## 方案 B：按租户循环消费，复用现有 RLS 机制

relay 用**与 API 相同的 `app_role`**，每个租户执行一次
`SET LOCAL app.current_tenant_id`，再在本租户上下文内 claim 一批事件。
跨租户能力只保留「列举租户 ID」这一点点，且由一个窄接口提供。

| 维度 | 评价 |
|---|---|
| 实现成本 | 中。需要「租户枚举」来源 + 每租户一次 claim 查询 |
| 运维成本 | 中。租户数增长后是 N 次小查询/轮；可用「每轮只扫活跃租户」缓解 |
| 顺序语义 | 租户内有序；跨租户无序（对审计投递无影响） |
| 爆炸半径 | **小**。relay 出 bug 也读不到别的租户，RLS 仍是最后一道防线 |
| 与决策 5 的关系 | 一致。RLS 不只约束 API，也约束后台消费者 |

租户枚举的做法（B 的必要配套）：迁移里建一个 `SECURITY DEFINER` 函数
`outbox.list_active_tenants()`，**只返回 tenant_id 列表**，不返回任何负载，
仅授权给 `app_role`。它不是后门：泄露的只是「有哪些租户」，而不是「租户里
发生了什么」。

## 建议

**选 B。** 理由不是「B 更优雅」，而是 A 会把平台最贵的一次投入（把越权
堵在数据库层）换成运维便利。租户枚举的代价是可控的一次性设计，而
BYPASSRLS 角色的代价是永久性的暴露面。

只有在下面任一条件成立时才应翻回 A：

1. 租户数达到「每轮 N 次查询成为瓶颈」的量级（例如 >500 活跃租户且事件
   吞吐 >1000/s），且已无法用分区/批量 claim 缓解；
2. 业务上要求**跨租户全局有序**（目前审计投递没有这个语义需求）；
3. 出现了必须跨租户聚合的消费方（分析型数仓），此时应走「脱敏导出」而不是
   让在线 relay 持特权。

## 若选 B，落地清单（供后续排期）

1. 迁移：`outbox.list_active_tenants()`（SECURITY DEFINER，仅 SELECT 权限授予 `app_role`）。
2. `workers/relay/` 实现：租户循环 → `SET LOCAL` → `SELECT ... WHERE published_at IS NULL
   ORDER BY occurred_at LIMIT n FOR UPDATE SKIP LOCKED` → 投递 → 回写 `published_at`。
3. 投递语义定为 **至少一次**，幂等键用 `event_id`；投递与回写不在同一事务时，
   必须允许重投（消费方按 `event_id` 去重）。
4. 指标：待投递积压数、单轮耗时、失败重试数（复用 `packages/shared/metrics.py`）。
5. 回归：多租户集成测试——租户 X 的 relay 不得读到租户 Y 的事件（与
   `tests/integration/test_rls_isolation.py` 同一判据）。

## 决策

> 平台级 relay **不允许**持有跨租户读取权限。

按方案 B 落地。租户枚举走 `outbox.list_active_tenants()`（`SECURITY DEFINER`，
只返回 `tenant_id / pending_count / oldest_occurred_at`，`REVOKE FROM PUBLIC`
后仅授 `app_role`）；relay 用 `app_role` 逐租户 `SET LOCAL` 后消费。

## 落地记录（2026-11-14）

- 迁移 `e5f6a7b8c9d0`：建枚举函数并收回 PUBLIC 权限；`alembic upgrade head` 已应用。
- `workers/relay/consumer.py`：租户内 `FOR UPDATE SKIP LOCKED` claim → 处理器 →
  回写 `published_at`（带 `published_at IS NULL` 前置条件）。
- `workers/relay/handlers.py`：按 `event_type` 细分的处理器注册表（当前为空）；
  统一外发由 `__main__` 装配的兜底处理器承担。
- `workers/relay/webhook.py`（下游）：`OUTBOX_WEBHOOK_URL` 为空 → 没有兜底 →
  事件不 ack、积压可见；配了就真的外发，带 `Idempotency-Key: event_id`、
  `X-Outbox-Signature = HMAC-SHA256(secret, "{timestamp}.{body}")`。
  配 URL 不配密钥时进程**启动即失败**（拒绝未签名外发）。
- 指标：`outbox_relay_events_total{event_type,result}`、`outbox_relay_backlog`、
  `outbox_relay_run_errors_total{stage}`（不按租户打标签，避免基数爆炸）。
- 编排：compose 增 `relay` 服务（同镜像、同 app_role、`migrate` 成功后启动、
  healthcheck 显式关闭，因为它没有 HTTP 端口）；镜像与 CI 增 relay 入口冒烟。
- 测试 `tests/integration/test_outbox_relay.py`（4 条）：单租户上下文只读到本租户
  事件、无处理器不 ack、处理器失败不 ack、枚举函数只返回元数据。
- 验证：`ruff=0`、`mypy=0`（63 files，workers 已纳入 CI）、`pytest` 全绿、
  `contract_check=0`、`alembic check=0`。
- 本地实跑：`python -m workers.relay --once` → `tenants=1, delivered=0,
  unrouted=100`（当前池里只有历史遗留未投递事件，且没有注册处理器——符合预期）。

## 未做 / 已知边界

1. **Kafka 未接**：当前没有真实消费者，先建 broker/topic 只会得到一个没人读的
   通道。已接 webhook（配置即启用），满足「事件真正能出去且可验签」。
2. 消费方去重存储（按 `event_id`）未提供：至少一次语义下由收方保证幂等，
   幂等键已在请求头上给出。
3. 单轮只领一批（默认 100/租户），积压大时需要多轮或多副本；靠 `SKIP LOCKED`
   保证多副本不重复投递。
4. 每轮 `N` 次小查询（N=有积压的租户数）；租户量级上千时应改为分区或批量 claim。
