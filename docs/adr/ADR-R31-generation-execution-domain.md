# ADR-R31: generation_execution 域落地与 PRODUCTION.ROUTE.SELECT 实现

- 状态：Accepted（2026-11-17）
- 关联：ADR-R16（按域最小权限数据库角色）、ADR-R20（数据不变量下沉）、
  ADR-R27（镜头域拆分范式）、ADR-R28（跨域只读端口）、契约修订
  V205-R14-A6（对象契约追认）与 V205-R14-A7（14.1/14.2 矩阵修订）

## 背景

Phase 1 主链"单一路线生成"是最后缺口（31.3）：拆镜→确认→规格/视觉方案/
约束包已闭环（ADR-R27），资产/QC/Gate 与交付 D1–D4 已闭环，唯独
PRODUCTION.ROUTE.SELECT 没有运行时落点——选路这件事在平台上从未真实发生。
A6 已补齐 GenerationTask / ProductionRoute 的对象契约；本 ADR 登记
generation_execution 域的落地决策与选路语义。

## 决策

### 1. 独立 schema + 独立数据库角色（ADR-R16 范式）

`generation_execution` schema + `svc_generation_execution` 角色
（packages/shared/db_roles.py 新增 GENERATION_EXECUTION）。不复用资产域
角色的理由：路线目录/生成任务是"生成流水线"职责，资产域角色代表"资产
事实"；共用身份会让任务执行路径同时握有资产写入权，数据所有权边界在
数据库层失效。迁移 a5b6c7d8e9f0 建角色并精确授权（SELECT/INSERT/UPDATE，
不授 DELETE；outbox SELECT/INSERT 用于事务性审计事件）。

### 2. ProductionRoute 是平台能力目录，不是项目资源

A6 冻结字段集里没有 project_id——路线是租户级适配器能力条目。RLS：
租户隔离 + 组织限制性策略（无项目列）。选路时的项目上下文由授权层
ResourceRef(project_id) 绑定（capability token 的项目范围判定），
不落在路线条目上。

### 3. 状态机：UNVERIFIED→VERIFIED 唯一登记边，VERIFIED 终态

依据基线既有规则（16.1 未验证能力不得进入生产路由；16.2 仅可对实际
通过验证的能力标 VERIFIED；动作矩阵前置条件"路线须处于 VERIFIED"），
不新造语义。VERIFIED 无出边：验证是"对已提交证据完成验证"的事实记录，
基线未给撤销语义——不猜契约，不造撤销边。

**诚实登记**：VERIFIED 的标定入口是适配器验证工作流（与
EXTERNAL_TOOL.ADAPTER.VERIFY 同源规则：集成工程负责人验证 + 安全/QC
独立复核），动作矩阵未给路线级状态变更 action——本域**不造 HTTP 入口**，
路线目录由运维播种。contract_check 的转移入口债务启发式（非 CREATE/READ
动作即视为状态变更入口）会因 PRODUCTION.ROUTE.SELECT 已实现而放行；
该动作实际不改路线状态，此启发式偏差在此显式说明，不被当作验证入口
已补上的证据。

### 4. 选路语义：选择是"使用事实"，不是状态变更

PRODUCTION.ROUTE.SELECT 校验顺序：存在（404）→ VERIFIED
（ACT-ROUTE-NEG-001，422）→ 授权层 capability/项目范围（require_action +
authorize）。选中发出契约登记的审计事件 PRODUCTION_ROUTE_SELECTED
（payload 与 event_catalog 一致），不改路线状态。选中结果的持久化载体
是 GenerationTask.route_id（主链段 3 落地）；本段只产出受理回执 +
审计事件。

### 5. rights 判定：不伪造，诚实留待债务收口

动作前置条件"满足 rights/capability"的 rights 侧可校验载体
（RightsManifest/RightsCase，ADR-R24 债务）尚未建模。本实现不伪造
rights 判定：capability 由授权层强制；rights 消费闸在交付域 D3
「消费前权利校验」承担（已实现的转移守卫）。权利域落地后，若契约要求
选路时前置校验，再按 ADR-R28 跨域只读端口接入。

## 后果

- 正向：主链选路入口真实存在并带全量负向测试；数据所有权新增一域，
  分区回收名单经 all_business_roles() 自动覆盖。
- 债务登记（不进 gap 文件、随实现收口）：GenerationTask 契约已冻结
  （A6），域实现随主链段 3；14.2 矩阵 Generation Execution 行随本次
  A7 修订入正文。
- 测试：单测（聚合/状态机/服务 fake/迁移字面量 parity）+ 集成（真实
  DB 鉴权选路、outbox 事件、守卫触发器、跨域角色隔离）。
