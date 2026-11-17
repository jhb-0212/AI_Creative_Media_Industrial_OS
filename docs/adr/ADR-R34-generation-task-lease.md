# ADR-R34：生成任务租约、崩溃接管与并发领取（Phase 1 可靠性证据）

状态：已接受（2026-11-17）
关联：ADR-R32（GenerationTask 执行环）、ADR-R20（状态机守卫）、
ADR-R33（产出登记）、基线 9.4（重试与故障分类）、12.6（运行可靠性目标）、
`docs/evidence/phase1-resilience.md`（实测证据）

## 1. 背景与问题

契约三处明写同一件事：

- 9.4 表：**Worker 崩溃 → 租约到期后重新领取 → 幂等重试**；
- 12.6 表：**Worker 崩溃恢复**的目标是「任务租约到期后 5 分钟内重新调度」；
- `state_machines.yaml` 的 GenerationTask 注：all_sources 含「9.4 重试与故障
  分类（Worker 崩溃重新领取、失败为终结态）」。

段 3（ADR-R32）实现执行环时只有 PENDING 扫描：领取后进程崩溃，任务停在
`RUNNING` 且无人再取——`list_pending` 只看 PENDING。这不是"缺一个优化"，
而是**契约声明过的故障恢复能力不存在**。同时，压测暴露第二个问题：并发
worker 各自 `list_pending(16)` 再逐个乐观领取，读到的是同一个"最早 16 条"
窗口，6 个 worker 抢 24 个任务产生 **113 次幂等跳过**（正确但空转）。

## 2. 决策

### 2.1 租约是数据事实，不是内存标记（迁移 d0e1f2a3b4c5）

`generation_tasks` 增两列（实现扩展列，契约 additionalProperties 允许）：

- `lease_owner`：当前持有者（`hostname:pid`）；
- `lease_expires_at`：租约到期时刻。

租约列进入守卫**可变列白名单**：租约是运行态元数据，天然要被接管与续租
改写。**这不是放宽不可变面**——准入事实（project/shot/route/引用/attempt/
retry_of）仍在白名单之外，事后改写照旧被拒（既有负向测试覆盖）。

存量 `RUNNING` 行回填 `now() + 5 分钟`：迁移瞬间在跑的旧 worker 没有续租
能力，给一个窗口而不是立刻判死；窗口过后由新 worker 接管。

### 2.2 接管是**状态不变**的 CAS，不是状态转移

`take_over` 只改租约列 + `entity_version`，状态保持 `RUNNING`。理由：

- 租约与状态是正交事实。把"租期"塞进状态机（如 `LEASE_EXPIRED`）等于把
  运营元数据说成业务阶段，且会引入一条 `RUNNING→RUNNING` 的假转移；
- 并发安全由**谓词 CAS** 提供：`UPDATE ... WHERE status='RUNNING' AND
  lease_expires_at < now`，谓词与写入在同一条语句里求值。多个 worker 同时
  看到同一批过期任务时只有一个命中——先读后写的 check-then-act 做不到这点。

`started_at` 不被接管改写：首次开始执行是既成事实；接管次数若需度量化，
从审计事件统计，不覆盖时间戳。

### 2.3 终态写入带租约栅栏

`succeed` / `fail` 前校验 `lease_owner == 当前 worker`。为什么版本号不够：

- 执行环在写终态前会**重读**任务（拿最新版本）。若只靠乐观锁版本，僵尸
  worker 重读后拿到的是接管后的新版本，照样写得进去；
- 结果就是"旧执行的结果覆盖新执行"，而且以**合法状态转移**的形态发生，
  审计上看不出异常。真实供应商适配下两次执行的产出不同，这不是理论风险。

被栅栏挡下的终态写入记 `skipped`（不是 failed）：另一个持有者会收尾。

### 2.4 批量领取改用 `FOR UPDATE SKIP LOCKED`

`claim_pending` 用一条 `UPDATE ... WHERE entity_id IN (SELECT ... FOR UPDATE
SKIP LOCKED LIMIT n)` 把"挑选"与"占有"合成原子动作，与 outbox relay
（ADR-R06）同范式。实测把并发争抢的幂等跳过从 **113 → 0**，吞吐从
**40.8 → 57.4 任务/秒**（同机同规模，见证据报告）。

### 2.5 单一状态推进路径

删除 `GenerationService.claim/succeed/fail`（段 3 遗留、无调用方的第二套
推进路径）。保留它等于保留一条**绕过租约栅栏**的写口。生命周期只在
`GenerationTaskLifecycle`。

### 2.6 配置

`GENERATION_LEASE_SECONDS`（默认 300 秒）：崩溃到恢复的最长延迟约等于
`lease + 一个轮询间隔`，直接决定 12.6 的 5 分钟目标能否成立。调大它就是
调大恢复延迟。

## 3. 证据（可复现）

`python scripts/resilience_check.py` → `docs/evidence/phase1-resilience.json`：

| 场景 | 断言的不变量 | 实测 |
| --- | --- | --- |
| 6 worker × 24 任务排空 | succeeded == 24 且 asset_versions == 24 | 24/24，57.4 任务/秒，0 次跳过 |
| 20 并发受理 | 一请求一任务一事件 | 20/20，p95 447ms（同机） |
| 崩溃 6 任务 → 租约到期接管 | reclaimed == 6 且资产仍为 6 | 1.16s 恢复（SLO 300s） |
| 处理器成功、ack 前崩溃 | 不丢 + 重投有界 | 投递 2 次、发布 1 次、0 丢失 |

## 4. 诚实登记的债务（不允许伪装通过）

1. **执行中续租（heartbeat）未实现**：Phase 1 确定性适配器亚秒级，租约
   300s 远大于执行时间，因此不续租也不会误判。接真实供应商适配时**必须**
   在执行循环里周期性 `renew`，否则长调用会被当成崩溃接管——那时"接管"
   会变成重复计费。解除条件：`model_gateway` 首次接入。
2. **接管不查询上游作业状态**：9.4 附注要求"先查询 Provider Job"。确定性
   适配器无外部作业可查；真实适配器接管前必须先查供应商侧状态，否则同一
   作业会被提交两次。
3. **没有覆盖 RPO/RTO**：12.6 里 Project DB / 对象存储 / Workflow Runtime
   的备份恢复演练不在本 ADR 范围（需要独立环境与故障注入），本 ADR 只覆盖
   Worker 崩溃与 Outbox 崩溃重投。
4. **Outbox 投递延迟 SLO 未测**：验证的是"崩溃窗口不丢不重"的语义，
   P95 ≤ 30s 的延迟目标需要持续生产流量与时间窗口，单机快照测不出来。
5. **压测规模有限**：24 任务 / 6 worker / 20 并发，证明行为正确与退化趋势，
   不构成容量承诺（12.6 明确要求容量测试后按成本/风险校准）。
6. **租约为空的 RUNNING 行不会被回收**：查询要求 `lease_expires_at` 非空。
   迁移已回填存量；若未来有历史数据绕过迁移写入 RUNNING，需要一次性运维
   脚本补回填，而不是让扫描逻辑"猜"。

## 5. 后果

- 正面：契约 9.4 / 12.6 的 Worker 崩溃恢复从"声明"变成"可运行、可测、
  有数字"；并发领取不再空转；僵尸执行无法覆盖新执行。
- 代价：`generation_tasks` 多两列与一个部分索引；worker 必须携带唯一
  `worker_id`（缺省共用标识会让栅栏失效，已在执行环 docstring 写明）。
- 计数不变：状态机 41、事件 26（本 ADR 不新增状态与事件）；新增迁移 1 个、
  域模块 1 个（`domain/lease.py`）、测试 3 个文件、脚本 1 个、证据 2 份。
