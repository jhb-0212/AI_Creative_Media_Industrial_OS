# ADR-R32：GenerationTask 落地与生成任务执行环（GENERATION.REQUEST，主链段 3）

状态：已接受（2026-11-17）
关联：ADR-R31（generation_execution 域与 ProductionRoute）、ADR-R28（跨域只读
端口）、ADR-R20（状态机守卫）、契约修订 V205-R14-A6（GenerationTask 冻结）

## 1. 背景与问题

Phase 1 主链"单一路线生成"中，PRODUCTION.ROUTE.SELECT（段 2）解决了
**选路**，但 GENERATION.REQUEST（受理生成任务）仍是 not_implemented：契约
（A6）已冻结 GenerationTask 对象，运行时没有任务表、没有受理入口、没有
执行环。主链生成环缺受理与执行两环。

## 2. 决策

### 2.1 数据层（迁移 b8c9d0e1f2a3）

- `generation_execution.generation_tasks` 表（A6 冻结字段集）+ 转移表 +
  ADR-R20 守卫触发器：PENDING→RUNNING→SUCCEEDED/FAILED 唯一登记边集；
  SUCCEEDED/FAILED 终态无出边（失败为终结态，重试以新任务承载，9.4）。
- **不可变内容列白名单刻意收紧**：result_ref/started_at/completed_at 是
  转移伴随元数据（可变）；attempt/retry_of 与全部准入引用列
  （project/shot/route/shot_spec_ref/visual_plan_ref/constraint_snapshot_ref/
  budget_reservation_ref/quota_reservation_ref）登记后不可改写——受理时
  定型的准入事实是 ACT-GEN-NEG-001 的防抵赖面，事后改写等于伪造准入。
- RLS：租户隔离 + 项目限制性策略（生成任务是项目级资源，d5e6f7a8b9c0
  同口径）；角色沿用 svc_generation_execution（域已存在，不新建角色）。

### 2.2 受理准入（GenerationService.request_generation）

1. 路线存在 + VERIFIED（与 PRODUCTION.ROUTE.SELECT 同源语义，404/422）；
2. shot_spec_ref / visual_plan_ref（及非哨兵 constraint_snapshot_ref）
   **存在且属于请求项目**——经 `RefOwnershipPort` 跨域只读解析（ADR-R28
   机制复用：读经过所有者域自己的数据库身份；generation 定义自己的端口
   协议，组合根做显式类型转换，不靠两个域名义类型的结构巧合耦合）；
3. constraint_snapshot_ref 接受 NOT_APPLICABLE 哨兵（31.3：无锁定约束镜头）；
4. retry_of 必须指向同项目同镜头的 FAILED 终态任务，新任务 attempt+1；
5. 受理发 GENERATION_REQUESTED 审计事件（事件目录双向校验，随实现登记）。

### 2.3 执行环（workers/generation）

- worker 用受信编排上下文（GenerationExecutorContext，与 delivery consumer
  同纪律：范围是授权，来自配置，不从任务行自称）；
- 领取（PENDING→RUNNING）以**乐观版本控制**裁决（复用共享
  conditional_update；两个实例并发领取同一任务只有一个成功，失败方幂等
  跳过）；适配器执行在事务外（未来供应商调用不持数据库连接）；
- `GenerationAdapter` 协议隔离执行后端：Phase 1 用确定性适配器（不调
  外部供应商，result_ref 由任务标识派生，可复现回归）；失败统一走
  RUNNING→FAILED 终态（`fail_task_ids` 显式失败注入）。

### 2.4 服务拆分（接口隔离）

`GenerationService`（受理，API 用）与 `GenerationTaskLifecycle`（领取/
成功/失败，worker 用）分离：worker 不需要路线仓储、引用归属端口和
outbox 写入端——把受理依赖强塞给 worker 会逼出 fail-closed 空实现，
依赖即负担。

## 3. 诚实登记的债务（不允许伪装通过）

1. **预算/配额数值校验未实现**（budget_quota 域空壳）：本段只强制
   budget_reservation_ref / quota_reservation_ref 的结构性准入（非空 +
   引用面）；**数值级**校验（不可超卖、租约有效期、余额扣减、
   QuotaReservation/BudgetLedger 状态推进）需要 budget_quota 域。伪造
   "预算通过"判定违反质量红线——债务显式登记，budget_quota 域实现后
   把数值校验插入受理入口，引用面保持不变。
2. **真实供应商适配未接入**（model_gateway 域空壳）：确定性适配器是
   Phase 1 的诚实替身；真实适配随 model_gateway 落地后接入同一协议，
   执行环不因适配器更换而改变（ADR-R22 重建执行器同源债务）。
3. **ASSET.VERSION.CREATE 联动未打通**：worker 产出目前只登记
   result_ref 引用，不创建资产版本（跨域写入属资产域，属主链段 4
   E2E 的衔接点）。
   **状态更新（2026-11-17，ADR-R33）**：已闭合。执行环改为四段事务，产出经
   ``GenerationOutputRegistrar`` 端口在资产域事务内登记为 AssetVersion，
   ``result_ref`` 收敛为资产版本号（确定性幂等键 ``av_<task_id>``）。本段
   「债务 3」保留为历史记录，不再作为未闭合项计数。

## 4. 后果

- 正面：主链生成环"选路 → 受理 → 执行 → 终态"闭环，失败/重试/守卫/
  并发领取全链路可测；准入事实防抵赖下沉数据库层。
- 代价：预算数值校验与真实供应商适配仍缺位（§3 显式登记）；GenerationTask
  状态机登记使状态机总数 40→41、事件总数 25→26。
