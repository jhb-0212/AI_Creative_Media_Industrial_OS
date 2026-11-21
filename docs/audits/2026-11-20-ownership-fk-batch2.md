# 归属外键第二批：三张派生表补齐三列外键（2026-11-20）

> 迁移：`f8a9b0c1d2e3_ownership_fk_batch2.py`（`down_revision = e3f4a5b6c7d8`）
> 目标库：`localhost:15444/ai_film_industrial`，`e3f4a5b6c7d8 → f8a9b0c1d2e3`
> 决策者裁定：**A —— 三张表都补三列外键**（不采用「声明派生不建约束」）

## 1. 这一批解决的是什么

逐表覆盖矩阵（`verify_guard_coverage.py`）在收尾时留下 3 条 N/A + 3 条待裁决缺口，
指向同一个事实：**三张表有归属列、有不可变守卫、也被受控例外覆盖，但没有归属外键。**

| 表 | 归属列 | 归属外键 | 在 14 行台账里 |
| --- | --- | --- | --- |
| `asset_service.qc_reports` | `project_id` / `organization_id` / `tenant_id` | 无 | **不在**（既不在 `PROJECT_LEVEL`，也不在 `DERIVED_NO_FK`） |
| `shot_planning.shot_specs` | 同上 | 无 | 不在（`DERIVED_NO_FK`「刻意不建」） |
| `workflow.approval_steps` | 同上 | 无 | 不在（同上） |

`qc_reports` 是**漏列**：它既没被纳入，也没被显式排除——一个既不在「做了」也不在
「没做，理由如下」清单里的对象，是最容易被两轮评审都跳过的形态。

后果是可实测的，不是理论风险：受控例外放行之后，`UPDATE ... SET project_id='ghost-project'`
**返回 `UPDATE 1` 并成功**——归属指向一个根本不存在的项目，数据库一声不响。
这正是 I-1（归属指向的实体真实且一致）未获保护的直接证据。

## 2. 为什么选「补外键」而不是「声明派生」

派生的说法在语义上成立：`qc_reports → asset_version`、`shot_specs → shot_revision`、
`approval_steps → ticket` 都能推出 project。但**数据库层无法强制派生**：

- `project_id` 是已落库的事实列，不是视图表达式。派生关系只能靠应用层维护；
- 一旦这一列写错，父行推出来的值与本行存的值开始分叉，同一个事实就有了两个源——
  违反 R1「一个事实一个权威源」；
- 而且「声明派生」并不能阻止受控例外把它改成任意值，缺口只是从「数据库不拦」
  变成「文档说它不该被改」。

补三列外键的代价很小（目标唯一键 `uq_projects_entity_tenant_org` 由 `a9b8` 建好，
其中两张是空表），收益是**错的归属根本写不进去**。派生逻辑仍留在应用层。

所以本迁移**明确撤销** `a9b8.DERIVED_NO_FK` 对 `shot_specs` / `approval_steps` 的处置
（`a9b8` 的字节不动——它已应用，改它等于改历史；撤销动作记录在迁移常量
`RETIRED_DERIVED_NO_FK` 与本文件里）。

## 3. 做了什么

1. **迁移本体**：三条 `NOT VALID` 三列外键，命名沿用 `fk_<table>_project_tenant_org`
   （台账与覆盖矩阵按名字识别，不另起一套）。含两类前置断言：
   * `projects` 三列唯一键存在——缺失时报 `42830`，而 `42830` 的根因是「目标键缺失」，
     与数据脏无关，必须提前分开；
   * 三表归属列存在且 `NOT NULL`——可空的三列外键会退化成「NULL 即跳过」，等于没约束。
2. **闭环**：`acceptance_ledger.py --validate` 逐行 `VALIDATE CONSTRAINT`，
   三行全部通过，台账 **17/17**。
3. **台账**：`14 → 17` 行。这不是把分母做大——补充的三张都是真实存在、带归属列的表；
   原先的 14 掩盖了它们。
4. **覆盖矩阵**：`13 表 × 5 用例`，N5（例外放行后仍受外键约束）原先有 3 条 N/A，
   现全部转为实测 `23503`。矩阵 **70/70，N/A 0，待裁决缺口 0**。

## 4. 证据

| 证据 | 结果 | 位置 |
| --- | --- | --- |
| 批次二行为验证 | **15/15** | `docs/audits/mig-recon/verify-batch2.{md,json}` |
| 逐表覆盖矩阵 | **70/70**（N/A 0） | `docs/audits/mig-recon/verify-coverage.{md,json}` |
| 归属闭环台账 | **17/17 PASS** | `docs/audits/mig-recon/acceptance-ledger.{md,json}` |
| 台账四态自检 | **4/4 OK** | `acceptance_ledger.py --selftest` |
| 受控例外验证 | **37/37** | `docs/audits/mig-recon/verify-exception.{md,json}` |

`verify_batch2.py` 的关键断言（都是「会让恒真检查变红」的那种）：

- **C1**：约束建立**之前**，把 `project_id` 指向不存在的项目 → 写入**成功**（缺口可复现）；
- **N1**：约束建立**之后**（哪怕 `NOT VALID`）→ 三表一律 `23503`；
- **V1/V2**：存量有脏行时 `VALIDATE CONSTRAINT` **必须失败**（`23503`）。若它通过，
  说明 VALIDATE 根本没扫数据，台账上的 PASS 就是假的；
- **V3**：清掉脏行后 VALIDATE 通过（成功路径可达，不是只会失败）；
- **D3**：降级后同一脏写入**恢复为可成功**——证明 D2 删的确实是那条约束，
  而不是「碰巧删了个别的」。

`lab.OWNERSHIP_STACK` 同步从 5 层扩到 6 层，`assert_rewound()` 增加一项
「批次二归属外键=0」。漏掉这一层的话，回退后基线仍带着这三条约束，
而旧断言照样全绿——装置会静默偏离。

## 5. 没有解决的问题（不许当成已解决）

三列外键 `(project_id, tenant_id, organization_id) → projects(...)` 只保证
**目标项目真实存在，且租户与组织跟它一致**。

它**不保证本行的 `project_id` 与父行的 `project_id` 相同**：

- `qc_reports` 的父行是 `asset_versions`，两者 project 不一致时三列外键照样放行；
- `shot_specs` 的父行是 `shot_revisions`，同理；
- `approval_steps` 的父行是 `approval_tickets`，同理。

即 I-1 的「真实」半边已由本批覆盖，「**一致**」半边仍未覆盖。补齐方式需要
复合外键指向父行的 `(id, project_id, tenant_id, organization_id)`（要求父表有对应唯一键），
或等价触发器。这是独立一轮的议题，本迁移不假装解决。

## 6. 执行记录

| 步骤 | 命令 | 结果 |
| --- | --- | --- |
| 克隆验证 | `verify_batch2.py --migration .../f8a9b0c1d2e3_*.py` | 15/15（库 `t3b2_lab`，用完即删） |
| 应用 | `alembic upgrade head`（`POSTGRES_PORT=15444`） | `e3f4a5b6c7d8 → f8a9b0c1d2e3` |
| 迁移后立刻读台账 | `acceptance_ledger.py --db ai_film_industrial --port 15444` | **14/17**，三行 `待验证 PENDING_VALIDATE` |
| 闭环 | `acceptance_ledger.py ... --validate` | 三行 `VALIDATE 通过` → **17/17** |
| 覆盖矩阵 | `verify_guard_coverage.py --fk-migration ... --batch2-migration ...` | 70/70，N/A 0 |

第 3 步是刻意保留的记录：迁移刚跑完时台账**必须是 14/17 而不是 17/17**。
若那一刻就全绿，说明台账把「约束已建」当成了「已闭环」，`NOT VALID` 就成了永久豁免。
