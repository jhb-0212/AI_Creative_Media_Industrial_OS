# ADR-R33：生成产出登记为资产版本 + Phase 1 主链端到端闭环（主链段 4）

状态：已接受（2026-11-17）
关联：ADR-R32（GenerationTask 与执行环）、ADR-R31（generation_execution 域）、
ADR-R28（跨域只读端口范式）、ADR-R20（数据库守卫）、ADR-R22（交付清单作为证据）、
ADR-R24（交付审批消费）、契约 A6（GenerationTask 冻结）

## 1. 背景与问题

ADR-R32 把主链生成环做成了"选路 → 受理 → 执行 → 终态"的闭环，但留下三条
显式债务，其中第 3 条是 **worker 产出未联动 ASSET.VERSION.CREATE**：执行
成功的任务只在 ``result_ref`` 里留了一个只有适配器认识的字符串
（``genresult://<task>/attempt-<n>``），资产域没有任何对象承载这个产出。
后果是主链在"生成"与"QC/批准/交付"之间**断开**：

- 资产域无对象 → QC.REPORT.CREATE 无对象可依附（404）；
- 交付清单要求资产状态 ∈ {APPROVED, GOLD} → 无对象可引用；
- 任务终态 ``result_ref`` 指向字符串而不是对象 → "成功"无法被下游消费，
  也无法回答"这条资产是哪次生成产出的"。

同时，Phase 1 的验收口径（``contracts/phases/phase_map.yaml``）要求核心主链
端到端成立，而此前**没有任何一条测试**把 13 个动作按真实约束串起来：
每个域各自有测试，闸门全绿，但"链"本身没有人验证过。

## 2. 决策

### 2.1 产出描述：``GenerationOutput``（domain/output.py）

适配器协议从"返回一个字符串"改为返回不可变的产出描述：

| 字段 | 语义 | 为什么需要 |
| --- | --- | --- |
| ``artifact_ref`` | 生成侧产出引用（供应商作业 id / 结果 URI） | 追溯"是哪次产出" |
| ``sha256`` | 产出内容摘要（**由适配器给出**） | 资产域完整性校验 + 幂等判定 |
| ``storage_ref`` | 资产域可定位地址 | AssetVersion 必填项 |
| ``asset_type`` | 资产类型 | AssetVersion 必填项 |

**摘要必须由产出者给出**。若让 worker 对引用字符串自己算一个 SHA-256，摘要
就只证明"有人算过一次"，不再证明内容——这是把完整性校验退化成形式校验。
Phase 1 的确定性适配器对确定性产出的内容标识取 SHA-256，可复现；真实适配器
落地后摘要来自供应商返回的实际内容，协议不变。

### 2.2 登记端口：``GenerationOutputRegistrar``（domain/ports.py）

generation 域只定义"把产出登记为资产版本"这一个动作，实现放在 worker 层
（``workers/generation/asset_registrar.py``）：

- **跨域写必须经过所有者域的身份**：适配器自建 asset_service 角色的会话 +
  RLS 上下文，再调用资产域自己的应用服务（``AssetService.create_version``）。
  generation 域不认识 asset 表，asset 域也不认识 GenerationTask；
- 端口契约写明**幂等**：同一任务重复登记必须返回同一资产版本。执行环
  "登记成功、终态提交失败 → 下一轮重跑"的恢复路径完全依赖这一条。

**幂等键是确定性资产版本号** ``av_<generation_task_id>``：重跑必然算出同一
id，资产域按"同 id 同内容"幂等返回既有版本，不会新增版本、更不会覆盖
（产出不可覆盖是资产域红线）。重试链是新任务 → 天然新版本，不需要额外去重。

### 2.3 执行环改成四段事务（workers/generation/executor.py）

```
1) 领取        generation 域事务（乐观版本控制裁决并发）
2) 适配器执行  事务外（供应商调用不得持有数据库连接）
3) 产出登记    asset 域事务（资产角色 + RLS）
4) 终态        generation 域事务，result_ref := 资产版本号
```

第 3 步必须在第 4 步之前：终态要写入产出对象引用，先终态后登记会让"成功的
任务指向一个不存在的资产"。

**登记失败 → FAILED**，不是 SUCCEEDED：登记不上资产意味着这次生成没有被
交付的产物，记成功会让"任务成功但没有东西"成为合法状态，主链会静静断开。
失败分类写进 ``audit_ref``（``adapter:`` / ``output_registration:`` 前缀），
事后能区分"供应商没做出来"与"做出来了但没落地"。

**``result_ref`` 语义收敛**：段 3 是适配器自证的 URI，段 4 起是**产出对象
引用**（资产版本号）。主链终态必须指向可交付对象；生成产物 URI 保留在
资产的 ``storage_ref.uri`` 里，追溯关系变成双向可查。

### 2.4 权利凭据：``rights_manifest_ref`` 扩展列（迁移 c9d0e1f2a3b4）

资产批准有一条已实现的硬约束：``rights_manifest_ref`` 非空，否则拒绝
（无权利证据不得批准）。机器产出的资产若在受理时不携带权利凭据，**永远
进不了批准链**——主链会在"批准"这一步死掉。

因此受理入口接受可选的 ``rights_manifest_ref``，作为实现扩展列落到
``generation_tasks``（契约 A6 的 ``additionalProperties: true`` 允许，
不进 required；同 ``retry_of`` / ``result_ref`` 的既有范式）：

- 可空：缺失是**合法状态**——产出照常登记，只是不可批准（fail-closed）；
  加 NOT NULL 会逼受理端伪造一个权利引用，那比留空危险；
- **不可变**：受理时定型的准入事实事后不可改写。守卫触发器按"白名单之外
  一律不可变"实现，新列无需改触发器即自动受保护；
- rights_governance 域落地后，本列由 RightsManifest 引用替换，引用面不变。

### 2.5 生成准入补一条契约明写的规则（7.9）

7.9：**只有 ACTIVE 可作为生成输入**。此前受理只校验约束包的"存在 + 归属"，
不校验状态——一条写在契约里、运行时无人执行的规则。

实现方式刻意避开两种坏解法：①消费域照着对方状态枚举写 if（换一次状态名
生成侧就静默失效）；②在组合根猜规则。采用的是**所有者域判定 + 消费域只认
结论**：``RefOwnership`` 增加 ``eligible: bool | None``，由组合根按"契约明写
的规则"登记评估器（当前只有 ``ConstraintPack → ACTIVE``），generation 只在
``eligible is False`` 时拒绝。

``None`` 表示"契约没有为该类型写明状态准入规则"，**不表示默认放行**：
ShotSpec / ShotVisualPlan 是否应有状态准入，契约没有写，故不判定——不猜规则
（猜出来的规则比没有规则更危险：它会让错误前提在测试里全绿）。

### 2.6 端到端证据（tests/integration/test_main_chain_e2e.py）

按 phase_map 的 Phase 1 core_production 顺序串起 13 个动作，全部走真实
入口（HTTP + JWT + Capability + RLS + 域角色）：

```
PROJECT.CREATE → EPISODE.CREATE → SCRIPT.IMPORT → SHOT.DRAFT.CREATE(Agent)
→ SHOT.REVISION.CONFIRM(人类) → ShotSpec + ShotVisualPlan → CONSTRAINT.PACK.CREATE
→ CONSTRAINT.PACK.LOCK → PRODUCTION.ROUTE.SELECT → GENERATION.REQUEST
→ worker 执行环（产出登记）→ QC.REPORT.CREATE → ASSET.VERSION.APPROVE
→ PRODUCTION.GATE.APPROVE → DELIVERY.MANIFEST.CREATE
```

两处负向断言**插在正向路径的同一测试里**，证明闸门在链上生效而不是只存在于
别的文件：DRAFT 约束包不得作为生成输入（7.9）；GENERATED 资产不得进交付清单。

审计链按 aggregate 逐个核对（11 个聚合），资产链要求四条事件齐全
（CREATED → QC_REPORT_CREATED → HUMAN_REVIEW_ENTERED → APPROVED）：
只有"最终 APPROVED"不足以证明人工复核真的发生过。

## 3. 诚实登记的债务（不允许伪装通过）

1. **交付清单止于 DRAFT**：``DELIVERY.MANIFEST.APPROVE`` 的契约主体是
   "交付负责人 **+** 内部 QC/权利职责"（AND 语义），联合审批流程未实现，
   权限层按 fail-closed 拒绝；``RELEASE`` 依赖 APPROVE。因此主链的最后一环
   是"清单草案 + 服务端校验和"，不是"已放行交付"。
2. **PictureLock / RightsManifest 对象未建模**：清单的 ``picture_lock_ref``
   在 E2E 中引用本次 Gate4 APPROVED 裁决（真实产物），``rights_evidence_refs``
   引用受理时声明的权利凭据字符串。两者都不是"已验证的权利事实"——真正的
   权利校验在交付域 D3「消费前权利校验」（ADR-R24），随 rights_governance
   域落地。
3. **预算/配额数值校验仍缺位**（R32 债务 1 不变）：本段只沿用结构性准入。
4. **真实供应商适配仍缺位**（R32 债务 2 不变）：确定性适配器是诚实替身。
5. **生成准入的状态规则只覆盖契约明写的一项**：见 §2.5；其余类型不判。
6. **``rights_manifest_ref`` 未进契约正文**：它是实现扩展列。解除条件——
   契约修订把"生成产出的权利凭据引用"写进 GenerationTask（或明确"产出权利
   一律由资产域登记时补"），届时本列改名为契约字段或删除。

## 4. 后果

- 正面：主链第一次**端到端**成立（建项目 → 交付清单 DRAFT），且"生成成功
  = 资产存在"成为不可绕过的结构事实；生成准入补齐 7.9 一条；执行环的
  失败分类可区分两种失败。
- 代价/风险：worker 进程现在持有两个域连接池（generation + asset），
  退出时两个都要释放（已实现）；``result_ref`` 语义变化是**行为变更**，
  旧数据里仍是 URI 形态——解析方不得再假设它是 ``genresult://`` 串
  （当前唯一解析方是测试）。
- 计数不变：状态机 41、事件 26（本段不新增状态机与事件）；新增迁移 1 个
  （``c9d0e1f2a3b4``）、端口 1 个、测试文件 2 个。
