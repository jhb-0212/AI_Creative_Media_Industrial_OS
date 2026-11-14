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

```bash
# 1) 备份（库级，冷备到对象存储或本地 D 盘，不要放系统盘）
docker compose exec -T postgres \
  pg_dump -U postgres -d ai_film_industrial -Fc \
  > /backup/ai_film_$(date +%Y%m%d_%H%M).dump

# 2) 停业务（避免回滚期间继续写入不兼容结构）
docker compose stop api relay

# 3) 单步降级 / 降到指定 revision
docker compose run --rm migrate alembic downgrade -1
docker compose run --rm migrate alembic downgrade <revision_id>

# 4) 确认没有残留漂移
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
