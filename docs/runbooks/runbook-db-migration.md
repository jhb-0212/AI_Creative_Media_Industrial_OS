# Runbook：数据库迁移失败 / 卡死

适用：`alembic upgrade head` 报错、长时间不返回、`migrate` 容器非 0 退出、`api` 因
`depends_on: service_completed_successfully` 没起来、`alembic check` 报结构漂移。

> 铁律：**先止血、再取证、最后修**。不要在没有备份的情况下 `downgrade`，也不要用
> `stamp` 假装迁移成功——那会让后续迁移在错误的基线上执行。

## 0. 先判断影响面（30 秒）

```bash
cd infra/compose
docker compose ps                 # migrate 是否 exited(1)、api 是否根本没起
docker compose logs --tail=100 migrate
docker compose logs --tail=50 api
```

两种局面处置不同：

| 局面 | 含义 | 处置 |
|---|---|---|
| `migrate` 失败、`api` 未启动 | 半旧表结构**没有接流量**，风险可控 | 走第 2 节 |
| `migrate` 失败但 `api` 仍在跑 | 有人绕过了依赖条件（手工起服务/热更新镜像） | 先 `docker compose stop api` 止血，再走第 2 节 |
| `migrate` 长时间不返回 | 大概率锁等待或长事务 | 走第 1 节 |

## 1. 诊断：到底是哪一类

### 1.0 先确认"你连的是哪个库"（30 秒，必做）

本机曾出现同一端口上的两个 Postgres 实例：`127.0.0.1:15432` 落到本地旧集群
（`alembic_version` 落后、甚至没有 `asset_service` schema），`localhost:15432`
落到 Docker 的正式库。**用 `-h 127.0.0.1` 排查会看到"迁移没跑"的假世界**，
接下来所有判断都是错的。

```powershell
# 1) 谁在监听 15432
Get-NetTCPConnection -LocalPort 15432 -State Listen |
  Select-Object LocalAddress,LocalPort,OwningProcess

# 2) 只看 revision（必须与代码 head 一致；Docker 正常只有一个 head）
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic heads

# 3) 直接确认 revision（固定用 localhost，不要用 127.0.0.1）
docker compose exec -T postgres psql -U postgres -d ai_film_industrial -c "select version_num from alembic_version;"
```

判据：
- 出现多个监听地址（如 `127.0.0.1` 由本地 `postgres.exe` 提供）→ 先按第 5 节停用旧集群，
  再排查迁移；
- `alembic current` 与 `/readyz` 的 `schema_revision` 不一致 → 应用连的不是迁移目标库。

### 1.1 迁移版本状态

```bash
docker compose run --rm migrate alembic current      # 库当前 revision
docker compose run --rm migrate alembic heads        # 代码侧 head，正常只有一个
```

- `current` 落后于 `heads`：迁移没跑完。
- `heads` 出现**多个 head**：分支合并时漏了 `alembic merge`，先修 head 再谈其他。
- `current` 比代码新：有人跑过未提交的迁移，必须先对齐代码与库。

### 1.2 是不是锁住了（PostgreSQL）

```sql
-- 谁在等锁、等谁的锁
SELECT a.pid, a.usename, a.state, a.wait_event_type, a.wait_event,
       now() - a.query_start AS waited,
       left(a.query, 120) AS query
FROM pg_stat_activity a
WHERE a.datname = 'ai_film_industrial' AND a.state <> 'idle'
ORDER BY a.query_start;

-- 阻塞关系（blocked -> blocking）
SELECT blocked.pid   AS blocked_pid,
       blocking.pid  AS blocking_pid,
       left(blocked.query, 80)  AS blocked_query,
       left(blocking.query, 80) AS blocking_query
FROM pg_locks blocked
JOIN pg_locks blocking
  ON blocking.locktype = blocked.locktype
 AND blocking.database IS NOT DISTINCT FROM blocked.database
 AND blocking.relation IS NOT DISTINCT FROM blocked.relation
 AND blocking.page IS NOT DISTINCT FROM blocked.page
 AND blocking.tuple IS NOT DISTINCT FROM blocked.tuple
 AND blocking.transactionid IS NOT DISTINCT FROM blocked.transactionid
 AND blocking.pid <> blocked.pid
WHERE NOT blocked.granted;
```

常见原因：某个长事务（如大表回填、`SELECT ... FOR UPDATE` 未提交）持有表锁；
`ALTER TABLE ... SET NOT NULL` 需要 `ACCESS EXCLUSIVE`，只要有人读就等。

处置优先级：
1. 让**持有锁**的业务事务先结束（部署窗口内停写，比杀连接干净）；
2. 确认是无主会话才 `SELECT pg_terminate_backend(<pid>);`，并在事件记录里写明杀了谁；
3. 大表迁移必须放低峰窗口。当前最重的两个：
   - `b2c3d4e5f6a7`：outbox 加列 → 全表回填 → `SET NOT NULL` → 建索引；
   - `f6a7b8c9d1e2`：outbox 改建月分区，单事务内全量搬迁（旧表 → 分区父表）。
   两者都会产生长事务与大量 WAL，迁移 docstring 已标注维护提示。

### 1.3 口令 / DSN 类失败

症状：`invalid interpolation syntax`、`password authentication failed`。

- `data/migrations/env.py` 用**超级用户** DSN（`build_migration_url()`，读
  `POSTGRES_USER/POSTGRES_PASSWORD`），并对 URL 里的 `%` 做了 `%%` 转义。
  报 `invalid interpolation` 说明又有人把未转义的 URL 塞回 `config.set_main_option`。
- `app_role` 是 NOBYPASSRLS 且**没有 DDL 权限**，迁移不能用它跑；报权限错误先看
  `POSTGRES_USER` 是否被误改成 `app_role`。
- 口令含 `@ : / 空格`：编码由 `quote(safe="")` 负责，不要手工拼 DSN。

## 2. 回滚（唯一被允许的降级路径）

**先备份**，再降级。迁移里存在不可逆的数据清理（例如 `6a7b8c9d0e1f` 删除孤儿
`script_revisions`），降级只还原结构、不还原数据。

```powershell
# 1) 备份（库级冷备到本地 D 盘，不要放系统盘；路径用绝对路径）
$stamp  = Get-Date -Format "yyyyMMdd_HHmm"
$dump   = "D:\backup\ai_film_$stamp.dump"
New-Item -ItemType Directory -Force (Split-Path $dump) | Out-Null
docker compose exec -T postgres pg_dump -U postgres -d ai_film_industrial -Fc > $dump

# 2) 记录校验和（没有校验和的备份等于没备份）
Get-FileHash -Algorithm SHA256 $dump | Format-List
(Get-Item $dump).Length

# 3) 停业务（避免回滚期间继续写入不兼容结构）
docker compose stop api relay

# 4) 单步降级 / 降到指定 revision
docker compose run --rm migrate alembic downgrade -1
docker compose run --rm migrate alembic downgrade <revision_id>

# 5) 确认没有残留漂移
docker compose run --rm migrate alembic check
```

禁止事项：
- **不要** `alembic stamp head` 跳过失败迁移（基线错位，后续迁移会在错误前提上执行）；
- **不要**手工 `DROP/CREATE` 表来「修」迁移，ORM 元数据与库会立即漂移；
- **不要**在 `migrate` 未成功时手工起 `api`，等于让半旧结构接流量。

## 3. 修复后复验

```bash
docker compose run --rm migrate alembic current && docker compose run --rm migrate alembic check
docker compose up -d api relay
curl -fsS localhost:${API_HOST_PORT:-8000}/readyz      # 期望 200 + database=up
```

本地完整验证（五道闸门）：

```powershell
& ".\.venv\Scripts\python.exe" scripts\contract_check.py
& ".\.venv\Scripts\python.exe" -m ruff check .
& ".\.venv\Scripts\python.exe" -m mypy packages services apps workers
& ".\.venv\Scripts\python.exe" -m pytest tests/unit tests/integration tests/permission -q
& ".\.venv\Scripts\python.exe" -m alembic check
```

## 4. 事件记录（必须留痕）

写进 incident 记录：失败 revision、卡住的 PID 与持锁语句、是否降级及降到哪个
revision、备份文件位置与校验和、恢复时间点。**没有记录的「修好了」等于没修**——
下一个人遇到同样的锁等待，还得从零猜。

## 5. 同端口旧集群处置（先确认归属，再动手）

`127.0.0.1:15432` 上可能还跑着一个本地集群（如 `D:\pgcluster15432`）。它与 Docker
的库**同名不同结构**，是排查时最大的假信息源。处置前先确认它归谁用：

```powershell
# 找到监听 15432 的本地 postgres 进程与其服务/数据目录
$pid15432 = (Get-NetTCPConnection -LocalPort 15432 -State Listen |
  Where-Object LocalAddress -eq "127.0.0.1").OwningProcess
Get-CimInstance Win32_Process -Filter "ProcessId = $pid15432" |
  Select-Object ProcessId, CommandLine
Get-CimInstance Win32_Service |
  Where-Object { $_.PathName -like "*pgcluster15432*" } |
  Select-Object Name, State, StartMode
```

处置选项（按风险从低到高）：
1. **只改排查习惯**：所有 psql 固定 `-h localhost`，`/readyz` 看 `schema_revision`；
2. **改端口**：把旧集群 `port` 改为未占用的值并重启其服务（影响依赖它的其它项目）；
3. **停服务**：确认无人使用后 `Stop-Service <name>`，并把 `StartMode` 改为 `Manual`。

不要直接删数据目录：那不是"清理环境"，是销毁别人的库。
