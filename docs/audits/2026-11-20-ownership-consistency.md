# I-1「一致」半边补齐（批次四 `h8c9d0e1f2a3`）

日期：2026-11-20 ｜ 目标库：`ai_film_industrial` @ `localhost:15444` ｜ head：`h8c9d0e1f2a3`

## 一、修的是什么

I-1「归属指向的实体真实且一致」有两半，前四轮只做了**前半**：

| 半边 | 判据 | 此前状态 |
| --- | --- | --- |
| 真实 | 本行 `project_id` 指向的 `projects` 行存在，且 org/tenant 与它一致 | 18 行台账全 PASS |
| 一致 | 本行 `project_id` 与**它的父行**相同 | **无任何约束、无任何闸门** |

上半边全绿时，下面这件事完全合法：

```
qc_reports(qc-1).asset_version_id → asset_versions(av-ref-qc-1) ∈ proj-alpha
qc_reports(qc-1).project_id                              = 隔离项目
```

两个项目都真实、组织也一致，三列外键一路放行。于是同一条 QC 事实在
「按资产溯源」与「按项目统计」两个口径下归属不同——**一个事实两个答案**。

这不是推演：本库唯一一行 `qc_reports` 就是这种形态（由激活阶段按 R2「无法判定
真实归属」移入隔离区产生）。所以缺口是可复现的，不是理论风险。

## 二、判据：一条，不是一堆特例

「本行与父行一致」的充分条件：

> 存在一个复合外键，其**子列集合 ⊇ `{父引用列, project_id}`**，`confrelid` 指向父表，且 `convalidated`。

为什么不需要连 `tenant_id` / `organization_id` 一起要求：

1. 外键把 `project_id` 钉到父行的 `project_id` ⇒ 两者指向 `projects` 的**同一行**；
2. 两边各自的 `(project_id, tenant_id, organization_id) → projects` 三列外键又把这个
   project 钉到唯一一组 `(tenant, org)` 上；
3. ⇒ tenant/org 一致性**传递成立**，不必在复合外键里重复。

这也解释了为什么 `script_revisions` 早就用 `(project_id, episode_ref) → episodes`
两列外键解决了同一问题——本批只是把这个形状推广到其余边，不是发明新机制。

**反向情形同样要抓**：若某列已有**单列**外键（只引 `entity_id`，不含 `project_id`），
它看起来「有保护」，实际只保父行存在。本库 `qc_reports.asset_version_id` 正是如此。
判定为 `FK_NOT_COMPOSITE`，处置是**替换**而不是并列新增——两条键并存时，弱的那条
会被读成「只要它就行」。

## 三、缺口盘点（闸门实读，40 条候选列）

| 状态 | 条数 | 含义 |
| --- | --- | --- |
| `PASS` | 1 | 仅 `script_revisions.episode_ref`（早已做对） |
| `FK_NOT_COMPOSITE` | 1 | `qc_reports.asset_version_id`——有外键但只保存在 |
| `NO_FK` | 9 | 7 条项目级边 + 2 条组织级边（路线库） |
| `DANGLING` | 9 | 父表 `shots` / `scenes` / `gate_instances` **在库中不存在** |
| `UNCLASSIFIED` | 0 | 派生集全部被分类覆盖 |

`DANGLING` 的性质必须说清：它们是**设计上真实存在**的归属边，只是目标表尚未创建
（镜头域运行时未落地）。不能归入「不是边」——那是把「未实施」永久洗白。本批不修
它们，但闸门把它们逐条点名并单列，因此「其余全绿」不会被读成「保护完备」。

## 四、改了什么

### 4.1 迁移 `h8c9d0e1f2a3_ownership_consistency_fk.py`（18 步升级 / 20 步降级）

1. **父表补唯一键**（外键要有可引用的目标；缺键时 `ADD FOREIGN KEY` 直接 42830，
   与数据脏无关——空表上也报）：
   `asset_versions` / `shot_revisions` / `qc_reports` / `approval_flows` / `approval_tickets`
   各加 `(身份列, project_id, tenant_id, organization_id)` 唯一键；
   `production_routes` 加 `(entity_id, organization_id)`。
2. **删除被取代的单列外键**（`qc_reports_asset_version_id_fkey`、
   `fk_generation_execution_shot_route_selections_route_id`）。
3. **挂复合外键 `NOT VALID`**：10 条（8 条项目级 + 2 条组织级）。

**迁移不做数据订正**，三段职责分离：迁移（结构）→ `repair_ownership.py`（走例外的存量
订正）→ `acceptance_ledger.py`（闭环判定）。把订正塞进迁移会让迁移依赖不同库的脏数据
分布（不可复现），且守卫一拦整批 DDL 回滚，看起来像「结构变更失败」。

### 4.2 订正：`qc-1` 二次订正，走受控例外（新工单 `WO-OWN-2026-1120-002`）

`repair_ownership.py` 新增规则 **R4**：`project_id` 与**父行**不一致时，以父行为准。

- 方向的理由：父子关系里有方向性。QC 报告挂在资产版本上，资产版本归属哪个项目就是
  哪个项目；报告自己的 `project_id` 只是冗余副本。副本与源冲突时改副本，不改源。
- 边清单从 `verify_ownership_consistency.CLASSIFICATION` 取，**不另写一份**。闸门与订正
  必须对「哪些是归属边」有同一个答案，否则会出现「闸门报 FAIL、订正说无事可做」。
- **R4 优先于 R1~R3**：同一行若既有父行可推导、自身 project 又不合法，R2 会说「移隔离区」、
  R4 会说「改成父行项目」。父行是强事实源，能推导就不该退化成隔离。

执行结果：`UPDATE 1 · 回读一致 · 变更列=['project_id']`。

顺带修了工具的两个缺陷：
- **工单号写死**会让同一行的第二次订正必然撞 `uq_ownership_repair_map(表, 行, 工单)`。
  改为 `--ticket` 可传，默认值保留兼容。复用旧工单等于用旧授权做新操作。
- 主键读取抽成 `single_pk()`，两处（真实半边 / 一致半边）共用，避免在「复合主键怎么处理」
  上分叉。

### 4.3 台账 18 → 28 行

新增一档 `A2`（一致半边），判据同时要求**父行存在** ∧ **归属相同**——只判后者会让
「父行被删」的子行算合规（JOIN 不上就没有可比的父值）。

报告分两节渲染并各自给出小计，因为两类不变量混在一张表里时，读者会把「真实 18 行全绿」
读成「保护完备」，而一致半边就在旁边空着。

### 4.4 装置修正：`rewind_to` 不再「反向应用」未应用的层

模板库停在 `g7b8`、批次四尚未应用时，回退批次四会执行它的 downgrade，而 downgrade 里
的 `ADD CONSTRAINT` 会去**重建**一条早已存在的单列外键 → `DuplicateObject`。

判据不能由 lab 猜：多数层的 downgrade 全是 `DROP ... IF EXISTS`，误执行看不出差别，
错误会潜伏到某一层恰含 `ADD CONSTRAINT` 时才炸。改为**由迁移层自己声明**
`applied_probe_sql()`：探针为假 ⇒ 跳过。没有声明的层按「已应用」处理，旧层语义不变。

## 五、验证结果

| 装置 | 结果 | 关键项 |
| --- | --- | --- |
| `verify_ownership_consistency.py` | 目标库 **PASS 11 / DANGLING 9 / 其余 0** | 40 条候选列全部被分类 |
| 同上 `--selftest` | **4/4** | S0 修复前必红且原因精确（含 `FK_NOT_COMPOSITE`）；S1 未分类被点名；S2 目标表不存在报 DANGLING；S3 组织级父表走组织级判定 |
| `verify_batch4.py` | **21/21** | C1 跨项目挂载**成功**（缺口正面复现）；S2 弱键已消失；V1 有存量违规时 VALIDATE 必败；D4 降级后可写；Q1 与闸门一致 |
| `acceptance_ledger.py` | **28/28** | 真实 18/18 + 一致 10/10 |
| 同上 `--selftest` | **5/5** | 新增 `inconsistent` 态：真实全真、仅一致被破坏 → 恰好 1 行 FAIL 且**正是** `qc_reports` |
| `verify_ownership_closure.py` | **8/8** | C7 28 行约束全在；C8 全部 `convalidated` |
| `verify_exception.py` | **37/37** | 例外通道未受本批影响 |
| `verify_guard_coverage.py` | **70/70（N/A 0）** | 13 张守卫表全部接通且仍受外键约束 |
| `verify_grants.py` | **11/11（N/A 0，缺口 1）** | 8 层自动识别；G9 仍为已知边界 |
| `rls_acceptance.py` | **10/10** | 订正后 `qc-1` 已离开隔离区，A3 的「未改」符合预期 |
| `verify_batch2 / batch3` | **16/16 / 15/15** | 前批无回归 |
| `scripts/contract_check.py` | **PASS** | CRED-3 未复现 |

## 六、仍未解决（不得当作已闭环）

1. **9 条 `DANGLING`**：`shots` / `scenes` / `gate_instances` 三张表在运行时**不存在**。
   所有 `shot_id` / `scene_id` / `gate_instance_id` 仍是裸字符串，写一个不存在的 ID
   不会报错。修复属「镜头域运行时落地」（建表 + ORM + service + action），不是一次迁移能完成。
   **在此之前，这 9 条边没有任何保护，且不会因为其它边全绿而变得安全。**
2. **ADR 未记录本批**。ADR 记录决策不记录进度，本批是否构成 D-10 之外的新决策
   （把「一致」提升为验收必备项）需所有者确认；如确认，再走 ADR 版本流程。
3. **`afs_audit` 的 SELECT 范围**仍未逐表论证（上轮遗留）。

## 七、复现命令

```bash
cd docs/audits/mig-recon
P=<repo>/data/migrations/pending/feat-ownership

python verify_ownership_consistency.py --db ai_film_industrial --port 15444
python verify_ownership_consistency.py --selftest
python verify_batch4.py --migration "$P/h8c9d0e1f2a3_ownership_consistency_fk.py"
python acceptance_ledger.py --db ai_film_industrial --port 15444
python acceptance_ledger.py --selftest
python verify_ownership_closure.py --db ai_film_industrial --port 15444
python repair_ownership.py --port 15444 --plan          # 应为「无违规行」
```
