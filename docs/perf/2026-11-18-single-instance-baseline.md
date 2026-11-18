# 单实例基线压测实测记录（2026-11-18）

> 状态：**首轮实测已完成，首期阈值多数未达标**。
> 本文档记录的是**本机单进程**的证据，不能外推为生产容量结论。
> 阈值口径见 `docs/perf/performance-acceptance-plan.md` 第 2 节。

## 1. 为什么要有这份记录

`performance-acceptance-plan.md` 此前只有「脚本、矩阵、阈值、checklist」，
没有一行实测数字。第三轮审查（F09）对它的判定是「无压测证据」——这个判定
在本文档之前依然成立，因为**方案存在 ≠ 证据存在**。

本轮补了两件事：

1. `scripts/perf/load_test.py` 增加 `--write-path` 模式。此前脚本只能 GET，
   而阈值表要求 `POST /assets` 的 P99——两者矛盾，导致 S5 结构性无法闭合。
2. 在真实进程栈（uvicorn + PostgreSQL 16 + Redis）上跑了四条路径，记录实测值。

## 2. 环境（这决定了数据能说明什么）

| 项 | 值 |
|---|---|
| 运行形态 | 单 uvicorn 进程（无 gunicorn 多 worker、无多实例） |
| 数据库 | 本地 PostgreSQL 16，验证库 `ai_film_verify` |
| 缓存/限流 | 本地 Redis 7 |
| 压测客户端 | 与 API **同一台机器**，与 API 争抢 CPU |
| 操作系统 | Windows 11 |
| 并发 | 20（客户端上限 `max_connections=20`） |
| 每路径请求数 | 500 |
| 限流 | 写路径压测时抬到 1,000,000/分钟（否则测的是限流器） |

**必须说明的局限**：单机 + 同机压测客户端 + 单进程，三个因素都会压低吞吐。
这里的数字衡量的是「本机基线」，不是「平台容量」。

## 3. 实测数据

| 路径 | 阈值（首期） | P50 | P95 | P99 | QPS | 错误率 | 判定 |
|---|---|---|---|---|---|---|---|
| `GET /readyz` | P99 < 50ms，QPS ≥ 500 | 92.5ms | 266.1ms | **352.1ms** | 160.8 | 0.00% | **未达标** |
| `GET /assets/{id}` | P99 < 200ms | 98.0ms | 171.9ms | **200.1ms** | 176.2 | 0.00% | **边界**（超 0.1ms） |
| `POST /assets` | P99 < 500ms，错误率 < 1% | 159.9ms | 310.1ms | **562.3ms** | 107.6 | **0.00%** | P99 **未达标**，错误率达标 |
| 429 背压 | 按阈值返回，无 5xx 雪崩 | — | — | — | — | — | **达标** |

背压那一行是单独验证的：限流设为 120/分钟时，500 个写请求得到
`{201: 119, 429: 381}`，无 5xx、无静默堆积。这说明限流器按配置生效，
且拒绝方式符合「按阈值返回 429」的要求。

## 4. 三个实测才发现的问题

### 4.1 限流后端不可达时，每个请求固定阻塞 1 秒（无熔断）

首次压测的写请求稳定在 **1041ms**，连 401、403 也是 1.01s，而 `/health` 只要 2ms。

根因是 `.env` 里 `REDIS_URL=redis://localhost:16379/0`（Docker 映射端口），
而本地 Redis 在 6379。`RateLimiter.from_settings()` 配了
`socket_connect_timeout=1.0`，于是每个请求都等满 1 秒才走 fail-open 分支。

配置错误本身是环境问题，但它暴露了一个**真实的架构弱点**：

> 限流器只在单次请求内 fail-open，没有任何短路/熔断。Redis 不可达时，
> 每个请求都要付一次完整的 connect_timeout，QPS 从数百掉到个位数。
> 指标 `RATE_LIMIT_BACKEND_ERRORS` 会涨，但没有机制让它停止尝试。

生产上 Redis 抖动一次，全站每个请求 +1s。这不是「降级」，是「降级实现得比不降级更糟」。
按「禁止补丁式演进」的口径，这里需要的是熔断（连续失败 N 次后短路 + 半开探测），
不是把 timeout 从 1.0 调到 0.1。

**未在本轮修改**：熔断策略涉及阈值、半开探测周期、与限流语义的关系，
属设计决策，需要单独定，不在 S5「复验」范围内顺手改。

修正 Redis 地址后，写请求从 1041ms 降到 105ms（单请求）。

### 4.2 `/readyz` 每次真探测双池 + Redis，成本约 90ms

探针 P99 352ms 的原因不是框架开销，而是实现：每次 `/readyz` 都实际探测
`project_planning` 池、`asset_service` 池与 Redis（见 main.py 的双池 checks）。
单次调用约 90ms（P50 92.5ms），20 并发下 P99 放大到 352ms。

阈值 50ms 按「探针只做轻量检查」设定，与当前实现不匹配。两条路：
缓存探测结果（1 秒 TTL）或把双池探测移到独立的深度健康检查端点。
无论选哪条，都需要先定「探针要回答什么问题」——这也是设计决策。

### 4.3 压测写入的数据**删不掉**（ADR-R11 触发器）

`asset_versions` 是追加型不可变表，DELETE 被触发器拒绝，**连超级用户也被拒**：

```
asset_versions 是追加型不可变表，禁止删除（ADR-R11）
```

脚本原先会打印「清理 SQL」，照它执行必然失败。已修正为明确警告：

> 写路径压测只能对专用库或专用租户执行，不可对生产库压。

这条约束比它看起来重要：如果没人实测，下一个人会以为「压完能清干净」，
对生产库压一次就永久留下一批不可删的脏数据。

## 5. 结论

1. **首期阈值在本机单实例下多数未达标**：探针 P99 超标 7 倍，写路径 P99 超标 12%，
   只读路径恰好卡在边界。不宣称「性能验收通过」。
2. **错误率与背压行为达标**：0% 错误、429 按配置返回、无 5xx 雪崩。
3. **基线 P50 偏高（88–160ms）** 无法归因于本机环境之外的因素，
   需要目标环境复测才能定论——不能拿本机数字下生产容量结论。
4. 4.1 与 4.2 是**结构性**问题，不是调参问题，需各自的设计决策后才能修。

## 6. 复现步骤

```bash
# 1. 起服务（需公钥；写路径压测才需要后面的限流抬升）
IDENTITY_PUBLIC_KEY_PEM="$(cat perf_pub.pem)" \
REDIS_URL=redis://localhost:6379/0 \
RATE_LIMIT_PER_MINUTE=1000000 \
POSTGRES_DB=ai_film_verify \
uv run uvicorn apps.api.main:app --host 127.0.0.1 --port 8081

# 2. 探针
uv run python scripts/perf/load_test.py --probe-only \
    --url http://127.0.0.1:8081 --concurrency 20 --requests 500

# 3. 只读（需具备 ASSET.VERSION.READ 的 token；Git Bash 下注意 MSYS 路径转换）
MSYS2_ARG_CONV_EXCL='*' uv run python scripts/perf/load_test.py \
    --url http://127.0.0.1:8081 --path "/assets/<id>" --token "$TOKEN" \
    --concurrency 20 --requests 500

# 4. 写路径（只对专用库执行——写入的数据不可删）
uv run python scripts/perf/load_test.py --write-path \
    --url http://127.0.0.1:8081 --project-id <perf-project> --token "$TOKEN" \
    --concurrency 20 --requests 500
```
