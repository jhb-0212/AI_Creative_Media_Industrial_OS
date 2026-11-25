# Runbook：Outbox 保留期维护与归档

适用：`outbox.outbox_events` 的月分区创建、保留期摘除、归档与清理。
决策依据：`docs/adr/ADR-R06-2-outbox-retention-partitioning.md`
实现入口：`workers/outbox_maintenance`（CLI），容器内为 `outbox-maintenance`。

## 0. 先记住三条红线

1. **不要用 `DELETE` 清 outbox**。删行会重写表、拉长事务、和 relay 抢锁；保留期
   只走 `DETACH`，要释放空间再 `DROP` 已归档的独立表。
2. **不要给 `app_role` 开 DDL/DELETE**。维护用迁移角色（与 `migrate` 同一 DSN）。
3. **没有可校验副本不许删**。`--drop-detached` 必须配 `--archive-dir`，否则工具
   直接拒绝；不要绕过它手工 `DROP`。
4. **新建分区必须"创建即保护"**。`--apply` 会自动执行（RLS + 租户策略 +
   回收 app_role 直连权，见 ADR-R10）；手工建分区只建表不保护，等于把跨租户
   审计数据暴露给任何直连 SQL，且不变量测试会立刻报红。

## 1. 常规巡检（1 分钟）

```sql
-- 上次维护是什么时候、结果如何（没有 ok 就是异常）
SELECT started_at, finished_at, applied, created, detached, refused, status, error
FROM outbox.maintenance_runs
ORDER BY started_at DESC
LIMIT 5;

-- 分区清单与行数（父表名访问，app_role 也可读）
SELECT c.relname, pg_total_relation_size(c.oid) AS bytes
FROM pg_inherits i
JOIN pg_class c ON c.oid = i.inhrelid
JOIN pg_class p ON p.oid = i.inhparent
JOIN pg_namespace n ON n.oid = p.relnamespace
WHERE n.nspname = 'outbox' AND p.relname = 'outbox_events'
ORDER BY c.relname;

-- 未投递积压（会阻止摘除，也会让 relay 变慢）
SELECT count(*) FROM outbox.outbox_events WHERE published_at IS NULL;

-- 死信（P2-2）：attempts 达上限、relay 已放弃投递的事件（必须人工看）
-- 常规处置走 GET /outbox/dead-letters（见 §4）；本 SQL 用于端点不可用时的
-- 应急排障，或在库里核对端点返回的结果。
SELECT event_id, event_type, aggregate_type, aggregate_id,
       attempts, next_attempt_at, dead_lettered_at
FROM outbox.outbox_events
WHERE dead_lettered_at IS NOT NULL
ORDER BY dead_lettered_at DESC;

-- 退避等待中（正常现象，到 next_attempt_at 会自动重试，不要手工改）
SELECT count(*) FROM outbox.outbox_events
WHERE published_at IS NULL AND dead_lettered_at IS NULL
  AND next_attempt_at > now();

-- 分区保护巡检（应全部 rls=true，且 app_role 无任何直连授权）
SELECT c.relname, c.relrowsecurity
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'outbox' AND c.relname LIKE 'outbox_events%' AND c.relkind = 'r'
ORDER BY c.relname;

SELECT table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'app_role' AND table_schema = 'outbox'
  AND table_name LIKE 'outbox_events_y%';
```

判据：
- 最近 26 小时内没有 `status='ok'` 的行 → 维护没在跑，按第 5 节排查；
  出现 `status='refused'` → 有分区因未投递被拒绝摘除，属**预期保护**，先排空 relay。
- `refused > 0` 且 `published_at IS NULL` 持续增长 → 是 relay 故障，不是保留期问题。

## 2. 标准执行顺序（本地/容器都适用）

```powershell
# 1) 只看计划（默认 dry-run，不改任何东西）
.\.venv\Scripts\python.exe -m workers.outbox_maintenance --retain-months 6

# 2) 归档 + 摘除 + 删除（三件事一个事务链，导出失败即中止）
.\.venv\Scripts\python.exe -m workers.outbox_maintenance `
  --retain-months 6 --apply `
  --archive-dir "D:\backup\outbox" --drop-detached
```

容器内：

```bash
docker compose --profile ops run --rm outbox-maintenance \
  python -m workers.outbox_maintenance --retain-months 6 \
  --apply --archive-dir /archive --drop-detached
```

顺序不可拆：**归档 → DETACH → RENAME → DROP**。默认只 `RENAME` 为
`outbox_events_archived_yYYYYMM` 保留为独立表；`--drop-detached` 才真正释放空间。

## 3. 归档校验（必须做，否则等于没归档）

每次产出三个文件：`<分区>.csv.gz`、`<分区>.sha256`、`<分区>.manifest.json`。

```powershell
# 摘要与归档时记录的一致？
Get-FileHash -LiteralPath "D:\backup\outbox\outbox_events_y202601.csv.gz" -Algorithm SHA256
Get-Content -LiteralPath "D:\backup\outbox\outbox_events_y202601.sha256"

# manifest 里的行数与 CSV 实际行数一致？
.\.venv\Scripts\python.exe -c "import gzip,csv;r=list(csv.reader(gzip.open(r'D:\backup\outbox\outbox_events_y202601.csv.gz','rt',encoding='utf-8')));print(len(r)-1)"
```

判定：两处 sha256 相同 **且** manifest `rows` = CSV 数据行数 = 摘除前该分区行数，
才算归档有效。任何一项不一致：停止后续删除，保留 `archived_*` 表与归档文件排查。

## 4. 部分失败怎么处理

| 现象 | 含义 | 处置 |
|---|---|---|
| `REFUSED ... 仍有 N 条未投递事件` | 保护机制生效 | 查 relay 是否在跑；排空后重跑，或临时调大 `--retain-months` |
| `REFUSED ... S3 归档尚未接入` | `--archive-dir` 写成了 `s3://` | 改用本地目录；S3 需要先接凭证（见 ADR 未做项） |
| `拒绝删除 ... 必须同时给 --archive-dir` | 安全默认 | 补归档目录，不要手工 DROP |
| 导出中途异常 | 事务未提交，分区未摘除 | 看错误修环境（磁盘/权限）后重跑；已产出的半截文件不可信，删掉重来 |
| `dead_lettered_at` 非空的积压 | 事件已投递失败到上限（毒事件/下游长期不可用） | 用 `GET /outbox/dead-letters` 查 event_type 与 attempts；修复下游后用 `POST /outbox/dead-letters/{event_id}/replay` 处置。**不要**手工 SQL 改三列（见下） |

死信处置：只用端点，不要手工 SQL
--------------------------------

```bash
# 1) 定位（只读；按进入死信时间倒序，游标分页）
GET /outbox/dead-letters?limit=50
#    → items[].event_id / event_type / attempts / dead_lettered_at / occurred_at
#    → next_cursor 非空表示还有下一页，原样回传 cursor 参数继续翻

# 2) 确认下游已修复后，逐条放行（写；调用者身份与理由落审计表）
POST /outbox/dead-letters/{event_id}/replay
{"reason": "下游 5xx 已修复，确认可投递"}
```

为什么手册不再给手工 SQL（此前这一段写的是 `UPDATE ... SET dead_lettered_at = NULL`）：
手工改库**不落任何审计**——操作者、理由、重放前的失败次数全部消失，等于把
「谁在什么时候放行了一条本该被丢弃的事件」变成不可回答的问题。端点把这三件事
写进 append-only 的 `outbox.dead_letter_replays`，并且顺带做了租户校验、
状态校验与并发互斥。手工 SQL 只应作为端点不可用时的应急手段，且必须在事后
补记（who / why / 哪些 event_id）。

`replay` 只是把事件交回正常领取，不等于已投递：之后仍走 relay 的
claim → handler → ack，失败会重新进死信。只重放已确认下游能处理的事件；
同一 `event_type` 连续多次进死信，先修上游再批量重放。

观测：`outbox_relay_dead`（当前死信数）持续 > 0 超过 30 分钟会触发
`OutboxDeadLettersPresent`；重放成功次数看 `outbox_dead_letter_replays_total`。
存量不降而重放次数上涨，说明新进速度快于处置——先查上游。


## 5. 常驻维护没跑（新鲜度告警）

1. 查容器状态：`docker compose --profile ops ps outbox-maintenance`；
   常驻模式（`--every-hours 24`）应长期 Up，`restart: unless-stopped` 只兜进程级崩溃。
2. 查最近一轮错误：`SELECT status, error FROM outbox.maintenance_runs ORDER BY id DESC LIMIT 1;`
3. 手工补跑一次 dry-run 确认连接与 DDL 权限；
4. 补跑 `--apply` 后核对 `maintenance_runs` 新增 `ok` 行，再看分区清单是否补齐未来 3 个月。

DDL 卡住（`ALTER TABLE ... DETACH` 等锁）时：先查 `pg_locks`/`pg_stat_activity`
的阻塞对（见 `runbook-db-migration.md` 第 1 节），**不要**重启 postgres，
也不要 `pg_terminate_backend` 打掉持有业务写锁的连接——那会把一次等待变成一次回滚。

## 6. 无法回滚的部分

`DROP TABLE` 之后数据库层面不可恢复，唯一副本是归档目录里的 `CSV.gz`。
需要恢复某月数据时：按 manifest 校验归档 → `CREATE TABLE` 临时表 →
`COPY` 导入 → 核对行数 → 再决定是否 `ATTACH PARTITION` 回父表。
归档文件本身必须搬到冷备（对象存储/离线盘）；只留在本机等于把容量问题换了个位置。
