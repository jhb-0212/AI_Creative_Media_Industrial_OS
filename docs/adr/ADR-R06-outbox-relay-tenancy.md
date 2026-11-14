# ADR-R06：Outbox 事件投递的租户边界（待决策）

状态：**待决策**（本文只做对比，不改变代码）
范围：`outbox.outbox_events` 的消费端（`workers/*` 目前均为 `.gitkeep`，无实现）

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

## 需要拍板的只有一句

> 平台级 relay 是否允许持有跨租户读取权限？

答「不允许」→ 走 B；答「允许，但只限 relay 角色」→ 走 A，并在 ADR 里写明
该角色的凭据管理、网络隔离与审计要求。
