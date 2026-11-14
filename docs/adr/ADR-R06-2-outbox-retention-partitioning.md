# ADR-R06-2：Outbox 保留期与分区归档

状态：已决策并落地（2026-11-14）
关联：ADR-R06-outbox-relay-tenancy.md（消费端按租户循环，RLS 不破）

## 问题

`outbox.outbox_events` 是 append-only 审计事件流：业务写入只增，relay 只把
`published_at` 标成非空，**没有任何删除路径**。后果可预期：

1. 表随业务线性增长，`SELECT ... WHERE published_at IS NULL` 的全表扫描成本越来越高；
2. 索引膨胀后 relay 的 `FOR UPDATE SKIP LOCKED` 延迟上升，直接拉长投递时延；
3. 真到需要清理时，只剩 `DELETE` 一条路——大事务、锁表、与主库写入互相拖累，
   而这恰恰是最容易在故障时被草率执行的操作。

「以后拿 DELETE 删」不是方案，是把一次可预期的容量事故留给未来的人。

## 决策

### 1. 按 `occurred_at` 做 PostgreSQL 原生范围分区（按月）

- 分区键选 `occurred_at` 而不是 `published_at`：事件的时间归属是写入时确定的，
  不会因为投递延迟改变；保留策略也天然按「事件发生时间」而非「投递时间」生效。
- 按月而非按周：单月事件量远小于需要频繁管理的量级，周分区只会把分区数量
  变成新的运维负担（50 个分区/年 → 太多 DDL、太多文件）。
- 保留期到期 = `DETACH PARTITION`，不是 `DELETE` 行。DETACH 是元数据操作，
  **不重写数据、不产生长事务**；detach 出来的表是完整独立表，可离线 `pg_dump`
  搬去冷备，再 `DROP TABLE` 释放空间。

### 2. 主键从 `(event_id)` 变为 `(event_id, occurred_at)`

PostgreSQL 硬约束：分区表的唯一约束必须包含分区键。代价与判断：

- 代价：`event_id` 的唯一性只在**单个分区内**成立，跨分区不再由数据库强制；
- 判断：`event_id` 是 uuid4，跨分区碰撞概率可忽略；且消费端幂等键本来就是
  `event_id`，重复投递仍以业务侧幂等为准（outbox 语义是至少一次，不是唯一一次）。
- 反过来选「不分区、靠唯一索引」会把上面所有问题原样留下，不值得。

### 3. 权限与 RLS 口径不变：应用侧无 DDL、无 DELETE

- `app_role` 仍只有父表（分区表）的 `SELECT/INSERT/UPDATE`；分区子表不单独授权，
  应用永远通过父表名访问。
- RLS 策略建在父表上，PostgreSQL 会把父表策略应用到所有分区；`FORCE ROW LEVEL
  SECURITY` 不需要——表 owner 是迁移角色，`app_role` 不是 owner。
- 分区创建/摘除需要 DDL 权限，因此**维护动作不放在应用路径里**，用迁移角色
  （与 `migrate` 服务同一个 DSN）执行。给 `app_role` 开放 `CREATE/DROP` 才是真正的
  权限倒退。

### 4. 维护是显式操作，默认 dry-run

`python -m workers.outbox_maintenance`：

- 默认只打印「将创建哪些分区、将摘除哪些分区」，不执行；
- `--apply` 才执行创建；`--drop-detached` 才在摘除后删除分区表；
- **未投递事件保护**：某分区里只要还有 `published_at IS NULL` 的行，就拒绝摘除
  并给出明确提示（先把 relay 排空，或延长保留期）。宁可保留数据，也不丢未投递事件。
- `--ahead`（默认 3 个月）保证未来月份已存在，避免写入落到 default 分区。

### 5. Alembic 必须忽略分区子表

分区子表在 `pg_class` 里是独立表，而 ORM 元数据里没有它们。若不处理，
`alembic check` 会认为「库里有元数据没有的表」并提议 `drop_table`，把分区当成
漂移。`data/migrations/env.py` 的 `include_object` 因此显式排除
`outbox_events_y*` 与 `outbox_events_default`（分区属于物理布局，不属于领域模型）。

## 未做（明确不假装做了）

- **对象存储冷备未实现**：`S3_*` 配置当前为空，仓库里没有可用的对象存储客户端。
  本阶段归档 = `DETACH`（数据完整保留为独立表）+ 运维用 `pg_dump` 搬走。
  接入对象存储后，应在 detach 之后、drop 之前补自动导出与校验（另开 ADR/R 项）。
- **分区裁剪依赖查询条件带 `occurred_at`**：relay 的查询按 `published_at IS NULL`
  + 租户过滤，会扫描所有「仍有未投递事件」的分区。事件量上去后需要再加
  `occurred_at` 上界条件才吃到裁剪收益；当前规模不做，避免提前引入时间窗口语义
  （漏投比慢投严重）。
