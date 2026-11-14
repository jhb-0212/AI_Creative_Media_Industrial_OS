# Runbook：租户数据泄漏 / RLS 失效

适用信号（任一命中即按 P0 处理）：

- 任一接口返回了**不属于当前租户**的项目/分集/剧本；
- `pg_policies` 里 `tenant_isolation_*` 策略缺失，或表上 `relrowsecurity = false`；
- `app_role` 在**没有租户上下文**时能读到行（这等价于 RLS 没生效）；
- relay 把 A 租户的事件投给了 B 租户的下游；
- 审计/日志里出现「一个 trace_id 内跨了两个 tenant_id」。

> 铁律：**先熔断、再取证、最后修**。取证窗口一旦被重启或重启服务冲掉，就无法证明
> 影响面。所以下面第一步是**停业务、不停数据库**。

## 1. 熔断（目标 3 分钟内完成）

```bash
cd infra/compose
docker compose stop api relay          # 停掉对外读写与事件外发
docker compose ps                      # 确认 postgres 仍在跑（取证现场在库里）
```

- **不要重启 postgres**：会话、慢查询、锁等待、当前 `app.current_tenant_id` 设置
  都在内存里，重启即销毁现场。
- 如果有前置网关/负载均衡，同时把入口切到 503，避免残留连接继续打进来。
- relay 必须停：它是唯一会**主动把数据送出边界**的组件，泄漏期间它每 5 秒都在放大
  影响面（`OUTBOX_RELAY_INTERVAL_SECONDS`）。

## 2. 取证（只读，先不要改任何东西）

### 2.1 数据库侧

```sql
-- ① RLS 是否还开着、策略是否还在
SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled, c.relforcerowsecurity
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('project_planning', 'outbox') AND c.relkind = 'r'
ORDER BY 1;

SELECT schemaname, tablename, policyname, cmd, qual
FROM pg_policies
WHERE policyname LIKE 'tenant_isolation%';

-- ② 受影响行的证据快照（按需要改成具体 tenant / 表）
SELECT tenant_id, count(*) FROM project_planning.projects GROUP BY 1 ORDER BY 2 DESC;

-- ③ 当前会话是否残留租户设置
SELECT pid, usename, state, left(query, 100) AS query
FROM pg_stat_activity
WHERE datname = 'ai_film_industrial' AND usename = 'app_role';
```

期望状态：四张表（`projects` / `episodes` / `script_revisions` /
`outbox_events`）`rls_enabled = true`，且各有 `tenant_isolation_<table>` 策略；
`outbox_events` 的策略名为 `tenant_isolation_outbox_events`。

### 2.2 应用侧

```bash
docker compose logs --since=1h api   | tee /backup/incident-api.log
docker compose logs --since=1h relay | tee /backup/incident-relay.log
```

按 `trace_id` 聚合，找出「同一请求触碰多个租户」的调用链。relay 侧关注
`outbox_relay_no_downstream`、`outbox_relay_run_errors_total{stage=...}` 与一次运行
内出现的多个 tenant_id。

### 2.3 外发侧（如果配了 webhook）

webhook 每次投递都带 `Idempotency-Key`（= `event_id`）与
`HMAC-SHA256(secret, "{timestamp}.{body}")` 签名。核对下游存档里的
`tenant_id` 与 `event_id` 是否与本地 outbox 记录一致——**下游只认签名，不认租户**，
所以一旦发错，下游无法自纠，只能靠这里比对。

### 2.4 备份现场

```bash
docker compose exec -T postgres \
  pg_dump -U postgres -d ai_film_industrial -Fc \
  > /backup/incident_$(date +%Y%m%d_%H%M).dump
```

## 3. 轮换（假定「已经泄漏」而不是「可能泄漏」）

按影响面从大到小：

| 凭证 | 动作 |
|---|---|
| `app_role` 口令 | `ALTER ROLE app_role WITH PASSWORD '<新口令>';` 并更新密钥系统中的 `POSTGRES_APP_PASSWORD` |
| webhook 签名密钥 | 换 `OUTBOX_WEBHOOK_SECRET`，通知下游同步；旧密钥立即作废 |
| JWT 签名密钥对 | 换 `IDENTITY_PRIVATE_KEY_PEM` / `IDENTITY_PUBLIC_KEY_PEM`（RS256），旧 JWKS 下线 |
| 限流状态 | 视需要清理 Redis 键（`rate_limit:*`），避免熔断期间的放行被继续沿用 |

## 4. 修复与验证（修完必须留下负向测试）

SQL 侧修复示例（缺策略时）：

```sql
ALTER TABLE project_planning.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_planning.projects FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_projects ON project_planning.projects
  USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
```

`FORCE ROW LEVEL SECURITY` 是必须的：不加的话，表 owner 仍然绕过策略。

用 `app_role` 实测（这是唯一的验收标准，看代码不算）：

```sql
-- 无租户上下文：必须 0 行
SELECT count(*) FROM project_planning.projects;

-- 注入租户后：只应看到本租户行
BEGIN;
SET LOCAL app.current_tenant_id = '<tenant-uuid>';
SELECT count(*) FROM project_planning.projects;
COMMIT;
```

回归闸门：

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_rls_isolation.py tests/unit/test_outbox.py -q
& ".\.venv\Scripts\python.exe" -m pytest tests/integration -q
& ".\.venv\Scripts\python.exe" scripts\contract_check.py
```

## 5. 恢复顺序

```bash
docker compose up -d api relay        # migrate 已在升序链里，无需单独跑
curl -fsS localhost:${API_HOST_PORT:-8000}/readyz
```

观察 15 分钟：
- `http_requests_total{status_code=...}` 是否出现异常 4xx/5xx；
- `outbox_relay_backlog` 是否被消化（停 relay 期间积压必然升高）；
- `outbox_relay_run_errors_total{stage=...}` 是否不再增长。

## 6. 事后（不可跳过）

1. 写 incident 记录：泄漏窗口起止、受影响 tenant 列表、外发了哪些 event_id、
   轮换了哪些凭证、恢复时间点；
2. 把这次失效**做成负向测试**并进 CI——修好没测试等于随时复发；
3. 如果根因是「某条路径没用 app_role / 用了 BYPASSRLS」，把该路径从架构上收敛，
   不要只补一处判断（打补丁会留下第二扇门）。
