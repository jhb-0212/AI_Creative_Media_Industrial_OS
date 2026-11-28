# `alembic check` 的检测边界：E1–E7 实测、结构权威来源与 28 表覆盖统计

> 日期：2026-11-26 ｜ 性质：**实验与只读调查报告，不实施、不改代码**
> 授权：Owner「按 1 → 2 → 3 → 4 顺序推进；第 4 项只做治理现状调查」；本轮追加「先补齐 28 张 RLS 表的全量覆盖统计，暂不提交报告」
> 前序：`2026-11-26-c4-head-gate-and-c1-dedup.md`（§6 已知盲区）、`2026-11-26-acl-flaky-evidence-recovery.md`
> 环境：PostgreSQL **16.15 (Debian)**，容器 `afs-step-feat`，端口 15444
> 增补：**§9 为第二轮只读调查**（读 catalog + 读测试源码），§8 据此改写为「已证实事实 / 尚待设计方案」两段

## 0. 结论先行

| # | 结论 | 证据 |
|---|---|---|
| 1 | `alembic check` **只检测 SQLAlchemy metadata 能表达的对象**：索引、外键（表/列/PK/UNIQUE 同理）。对 **CHECK 约束、RLS Policy、表级权限、触发器一律静默通过** | E1–E5 |
| 2 | **「`alembic check` 盲」≠「项目无防护」。** 四类盲区里有三类**已被其它判据兜住**，且能准确报出差异 | E2b/E3/E4/E5 复核 |
| 3 | 兜底分两种形态，风险截然不同：<br>**规律级结构判据**（`ck_*_status`、ACL 矩阵）→ 新增对象自动纳入；<br>**逐表行为判据**（Policy、Trigger）→ 覆盖取决于该表有没有业务测试 | §5 |
| 4 | **实证出真实空白**：删掉 `qc_governance.gate_decisions` 的租户策略后，集成测试 **239 条全绿**——该表没有任何测试引用，租户隔离已消失而无人察觉 | E7 |
| 5 | **版本号对齐 ≠ 结构正确**：`upgrade head` 在 `current == head` 时是**空操作**（exit 0、无输出、偏差仍在）；`downgrade -1` 因对象缺失而**失败**（exit 1）。两条标准路径都不能修复偏差 | E6 |
| 6 | **不建议进入结构指纹设计阶段**。事实源已存在（catalog + 迁移链 + 统一模型入口），缺的是**规律级判据**，不是第二份需人工同步的清单 | §8 |
| 7 | **28 张 RLS 表的有效行为覆盖率为 9/28 = 32.1%**（直接 8/28 = 28.6%，读侧 6/28 = 21.4%）。19 张无有效行为覆盖的表里，**17 张已有某种结构判据**——13 张读**迁移文本**、1 张只有 `FORCE` 静态判据、3 张读 catalog；只有 2 张完全没有。缺口不是「没有判据」，而是「已有判据看不见数据库」 | §9 |

## 1. 实验方法与基线

### 1.1 隔离措施

| 项 | 做法 |
|---|---|
| 实例 | 项目容器 `afs-step-feat`（独立 PostgreSQL 16.15），非生产、非验证库 |
| 基线库 | `afs_struct_probe_base`，**新建**，从空库 `alembic upgrade head` |
| 单变量保证 | 每个实验前 `CREATE DATABASE afs_probe_<id> TEMPLATE afs_struct_probe_base` **克隆全新基线**，实验后 `DROP ... WITH (FORCE)`。不做「连续破坏后再判断」 |
| 既有库 | `ai_film_api_tests`、`ai_film_api_tests_stale_20261126`、`ai_film_head_verify`、`ai_film_industrial`、`lab_*` **一律未连接、未改动** |

### 1.2 基线事实

```
仓库 head             = f9b0c1d2e3f4
afs_struct_probe_base  = f9b0c1d2e3f4
alembic check（基线）  = exit 0，末行 "No new upgrade operations detected."
```

基线对象基数（用于量化「删掉一个对象」）：

| 对象 | 数量 |
|---|---|
| 索引 | 130 |
| 约束 | 135（CHECK 48 / FK 28 / PK 31 / 触发器约束 2 / UNIQUE 26） |
| RLS 策略 | 28（分布在 28 张表，每表 1 条） |
| 非内部触发器 | 16 |

### 1.3 一个方法论坑（已修正）

首次取基线退出码用了 `alembic check | tail -6; echo $?`，拿到的是 **`tail` 的 0**，不是 alembic 的。本报告全部退出码改为 `cmd > log 2>&1; echo $?` 取得，并与输出联合判定。

另：Windows 下 `alembic check` 失败返回 **-1**，`subprocess` 以无符号 32 位呈现为 `4294967295`。下文统一写作 **-1（4294967295）**。

## 2. 检测能力矩阵（实测）

| 对象类型 | `alembic check` | 退出码 | 输出特征 |
|---|---|---|---|
| 索引 | **能发现** | -1（4294967295） | `FAILED: New upgrade operations detected: [('add_index', ...)]` |
| 外键约束 | **能发现** | -1（4294967295） | `FAILED: ... [('add_fk', ForeignKeyConstraint(...))]` |
| CHECK 约束 | **静默通过** | 0 | `No new upgrade operations detected.` |
| RLS Policy | **静默通过** | 0 | 同上 |
| 表级权限（GRANT/REVOKE） | **静默通过** | 0 | 同上 |
| 触发器 | **静默通过** | 0 | 同上 |
| 表 / 列 / 类型 | 未测（机制上 Alembic 支持） | — | 不在本轮实验范围，**不宣称** |

**必须写清的一句**：`alembic check` 报的是「**模型与数据库的差异**」，不是「数据库完整」。它在 metadata 表达能力之内精确，之外是**结构性沉默**——不是「碰巧没发现」，而是**根本不参与比较**。

## 3. 逐项实验记录（破坏层）

每项均为：克隆基线 → 破坏单一对象 → 跑 `alembic check` → 丢弃库。

### E1｜删除索引

| 字段 | 内容 |
|---|---|
| 数据库 / 基线 | `afs_probe_e1` / `f9b0c1d2e3f4` |
| 目标 | `outbox.ix_outbox_dead_letter`（部分索引，迁移 `f9b0c1d2e3f4` 创建） |
| 破坏 SQL | `DROP INDEX outbox.ix_outbox_dead_letter` |
| 破坏前 → 后 | `['ix_outbox_dead_letter']` → `[]`（生效） |
| `alembic check` | **exit -1**，`FAILED: New upgrade operations detected: [('add_index', Index('ix_outbox_dead_letter', Column('dead_lettered_at', ...), Column('event_id', ...)))]` |
| 判定 | **发现差异** |

### E2a｜删除外键约束

| 字段 | 内容 |
|---|---|
| 数据库 | `afs_probe_e2a` |
| 目标 | `asset_service.qc_reports.qc_reports_asset_version_id_fkey` |
| 破坏 SQL | `ALTER TABLE asset_service.qc_reports DROP CONSTRAINT qc_reports_asset_version_id_fkey` |
| 破坏前 → 后 | `['qc_reports_asset_version_id_fkey']` → `[]`（生效；该表仅此一个 FK） |
| `alembic check` | **exit -1**，`FAILED: ... [('add_fk', ForeignKeyConstraint(...))]` |
| 判定 | **发现差异** |

### E2b｜删除 CHECK 约束

| 字段 | 内容 |
|---|---|
| 数据库 | `afs_probe_e2b` |
| 目标 | `production_planning.artifact_manifests.ck_artifact_manifests_status`（迁移 `e7f8a9b0c1d2` 修复过的那个） |
| 破坏 SQL | `ALTER TABLE production_planning.artifact_manifests DROP CONSTRAINT ck_artifact_manifests_status` |
| 破坏前 → 后 | `['ck_artifact_manifests_locator_sha256', 'ck_artifact_manifests_status']` → `['ck_artifact_manifests_locator_sha256']`（生效） |
| `alembic check` | **exit 0**，`No new upgrade operations detected.` |
| 判定 | **未发现差异** |

### E3｜删除 RLS Policy（`qc_reports`）

| 字段 | 内容 |
|---|---|
| 数据库 | `afs_probe_e3` |
| 目标 | `asset_service.qc_reports.tenant_isolation_qc_reports` |
| 破坏 SQL | `DROP POLICY tenant_isolation_qc_reports ON asset_service.qc_reports` |
| 破坏前 → 后 | `['tenant_isolation_qc_reports']` → `[]`（生效；`relrowsecurity` 仍为 `true`） |
| `alembic check` | **exit 0**，`No new upgrade operations detected.` |
| 判定 | **未发现差异** |

### E4｜REVOKE 表级权限

| 字段 | 内容 |
|---|---|
| 数据库 | `afs_probe_e4` |
| 目标 | `asset_service_role` 对 `asset_service.qc_reports` 的 `SELECT` |
| 破坏 SQL | `REVOKE SELECT ON asset_service.qc_reports FROM asset_service_role` |
| 破坏前 → 后 | `['INSERT', 'SELECT']` → `['INSERT']`（生效） |
| `alembic check` | **exit 0**，`No new upgrade operations detected.` |
| 判定 | **未发现差异** |

### E5｜删除触发器

| 字段 | 内容 |
|---|---|
| 数据库 | `afs_probe_e5` |
| 目标 | `asset_service.qc_reports.trg_qc_reports_guard`（append-only 守卫） |
| 破坏 SQL | `DROP TRIGGER trg_qc_reports_guard ON asset_service.qc_reports` |
| 破坏前 → 后 | `['trg_qc_reports_guard', 'trg_qc_reports_no_truncate']` → `['trg_qc_reports_no_truncate']`（生效） |
| `alembic check` | **exit 0**，`No new upgrade operations detected.` |
| 判定 | **未发现差异** |

### 四态判定的口径

| 态 | 判定条件 | 本轮出现 |
|---|---|---|
| 发现差异 | exit ≠ 0 且输出含 `New upgrade operations detected` | E1、E2a |
| 未发现差异 | exit = 0 且输出 `No new upgrade operations detected` | E2b、E3、E4、E5 |
| 检查执行失败 | exit ≠ 0 但无 autogenerate 差异标记（异常/Traceback） | 无 |
| 实验无法成立 | 破坏 SQL 本身报错 | 无（六项 `destroy_error` 均为 `None`） |

**没有把「命令失败」记成「成功拦截」**：E1/E2a 的 -1 是检出差异的正常非零退出；E2b–E5 的 0 是「检查跑通了但没比这类对象」。

## 4. 关键复核：盲区是否真的无人看守

只测「`alembic check` 静默」会得出过于悲观的结论。本轮对每个盲区都追加了**「其它判据是否响」**的实测——在同一破坏状态下跑相关测试。

| 实验 | 破坏对象 | `alembic check` | 追加跑的判据 | 结果 |
|---|---|---|---|---|
| E2b-v | `ck_artifact_manifests_status` | 静默（0） | `test_status_constraint_drift.py` | **1 failed** |
| E3-v | `qc_reports` 租户策略 | 静默（0） | `tests/integration`（全量） | **2 failed / 237 passed** |
| E4-v | `asset_service_role` 的 SELECT | 静默（0） | `test_least_privilege_grants.py` | **1 failed / 14 passed** |
| E5-v | `trg_qc_reports_guard` | 静默（0） | `tests/integration`（全量） | **1 failed / 238 passed** |
| **E7** | `gate_decisions` 租户策略 | 静默（0） | `tests/integration`（全量） | **239 passed（全绿）** |

### 4.1 E2b-v：`alembic check` 看不见的 CHECK，被结构判据精确抓到

```
E   AssertionError: ORM 声明了 status 约束，数据库里却没有同名约束：
E       - production_planning.artifact_manifests
E     （迁移漏建，或约束被改名）
FAILED tests/integration/test_status_constraint_drift.py::test_status_constraints_match_orm_declaration
```

这条判据的形式值得注意：正则 `^ck_[a-z0-9_]+_status$` **扫全库**，与 ORM 逐值比对——**规律级**，新增表自动纳入。

### 4.2 E3-v：删策略被业务测试发现，但报的是「写不进去」

删掉 `qc_reports` 的策略后，表仍 `relrowsecurity = true` 但已无任何策略 → 行级默认拒绝。结果是：

```
FAILED tests/integration/test_qc_inspector_locator_e2e.py::test_string_locator_produces_qc_report
FAILED tests/integration/test_qc_inspector_locator_e2e.py::test_missing_content_produces_rejected_report
2 failed, 237 passed
```

**没有一条判据说「缺策略」**——是 e2e 路径在插入 `qc_reports` 时被 RLS 拒绝。这是**行为判据**，不是结构判据。

### 4.3 E4-v：ACL 矩阵判据报出精确差异

```
[表级] asset_service_role 对 asset_service.qc_reports: 实际=['INSERT']，矩阵=['INSERT', 'SELECT']
FAILED tests/integration/test_least_privilege_grants.py::test_domain_role_privileges_match_matrix
```

**规律级**：期望值来自 `db_grants` 矩阵，不是硬编码用例。

### 4.4 E5-v：删 guard 触发器被行为测试发现

```
FAILED tests/integration/test_asset_immutability.py::test_qc_report_update_and_delete_are_rejected
1 failed, 238 passed
```

### 4.5 E7：真实空白 —— 删一张没有测试引用的表的策略，全绿

`qc_governance.gate_decisions` 在全仓 `tests/` 中**零引用**（grep 为空）。删掉其 `tenant_isolation_gate_decisions` 策略后：

```
目标策略: tenant_isolation_gate_decisions
删除后 policy: [] | relrowsecurity: True
239 passed
```

**这是本轮最有价值的负面结论**：同一类破坏（删租户策略），在被业务测试覆盖的表上会红（E3-v），在没有测试引用的表上**全绿**。

## 5. 覆盖形态分析：两种兜底，两种风险

| 盲区类型 | `alembic check` | 现有兜底 | **兜底形态** | 新增对象是否自动覆盖 |
|---|---|---|---|---|
| CHECK（`ck_*_status`） | 静默 | `test_status_constraint_drift` | **规律级结构判据** | **是**（正则扫全库） |
| CHECK（非 status，如 `locator_sha256`） | 静默 | 无 | — | — |
| RLS Policy | 静默 | 部分 e2e 行为测试 | **逐表行为判据** | **否**（E7 实证） |
| 表级 / 列级 ACL | 静默 | `test_least_privilege_grants` | **规律级结构判据**（矩阵驱动） | **是** |
| 触发器 | 静默 | `test_asset_immutability` 等 | **逐表行为判据** | **否**（未验证的表是否存在覆盖，取决于是否有业务测试） |

**这个区分是本报告的核心**：

- **规律级**判据的事实源是 catalog 里的**规律**（「凡开 RLS 且属业务表 → 必须有策略」），不是对象清单。新增表自动纳入，维护成本为零。
- **逐表行为**判据的事实源是**具体业务路径**。它证明「防护真的生效」（比结构判据更强），但只能覆盖被测到的表。**未被测到的表，破坏是静默的**——E7 已实证。

因此真正的问题不是「`alembic check` 有盲区」，而是：**28 张开了 RLS 的表里，有多少张有行为判据覆盖？** 本轮起初只验证了 `qc_reports`（有）与 `gate_decisions`（无）两个端点；全量统计已在 §9 补做，结论是 **9/28 有有效行为覆盖，19 张没有**——E7 不是孤例。

## 6. E6：版本号对齐后，标准恢复手段是否有效

| 步骤 | 命令 | exit | 结果 |
|---|---|---|---|
| 1 | `DROP INDEX outbox.ix_outbox_dead_letter` | — | 索引消失 |
| 2 | `alembic check` | **-1** | `FAILED: ... [('add_index', ...)]`，问题被检出 |
| 3 | **`alembic upgrade head`** | **0** | **stdout 为空**；stderr 仅 `Context impl` / `transactional DDL`；**索引未恢复** |
| 4 | `alembic check` | **-1** | 与步骤 2 **完全相同** |
| 5 | `alembic downgrade -1` | **1** | 进入 `Running downgrade f9b0c1d2e3f4 -> f8a9b0c1d2e3` 后抛异常（降级要 `DROP INDEX`，而索引已不存在） |
| 6 | `alembic upgrade head`（再） | 0 | 空操作 |
| 7 | `alembic check`（往返后） | **-1** | 仍报同一 `add_index` |

**结论（实测）**：

1. `current == head` 时 `alembic upgrade head` 是**空操作**——它只看版本表，不看结构。**exit 0 在这里不代表「已修复」，只代表「没有需要执行的迁移」**。
2. `downgrade` 假设结构完好（其 `drop_index` / `drop_table` 依赖对象存在），因此**偏差会让降级失败**。
3. 唯一可靠恢复路径是**重建库**（从零 `upgrade head`）。这也是 CI 每次都能绿的原因，同时说明 **CI 从结构上无法发现「版本对、结构错」**。

## 7. 结构权威来源分析（第 2 项）

按裁决要求，先分开两种权威：

| 概念 | 定义 | 本项目载体 |
|---|---|---|
| **期望结构权威** | 「目标结构应该是什么」 | 迁移链（`data/migrations/versions`）+ ORM 模型（`Base.metadata`，经 `packages.shared.orm_registry.load_all_orm_models()` **唯一入口**加载） |
| **实际结构事实** | 「库里真实存在什么」 | PostgreSQL catalog（`pg_class` / `pg_constraint` / `pg_policy` / `pg_trigger` / `acl*`） |

三条判断：

1. **迁移链可以承担期望结构权威**——`env.py` 的 `target_metadata` 指向同一份 `Base.metadata`，模型加载有唯一入口，不存在第二份模型清单。这一点是现状，不需要新建。
2. **`alembic check` 是两者的部分比较**，比较范围 = metadata 可表达的对象子集。它的正确用途是「模型漂移检测」，**不是**「结构完整性证明」。
3. **「实际结构符合期望」无法由迁移链自证**（E6 已证）。它能被判据证明，但这些判据必须**直接读 catalog**，不能读版本表。

第 2 项的答案：**不需要引入新的结构清单**。需要补的是「**直接读 catalog** 的规律级判据」——注意这里不是从零开始：§8.1 F1/F2 证明 Policy 已经有一条规律级判据，只是它读迁移文本；E7 与 §9 说明这条判据在「版本号对、结构错」的场景里是盲的。补齐的含义是**给既有判据换一层输入**，不是新建一套体系。

## 8. 是否需要进入结构指纹设计（第 3 项结论）

**建议：暂不进入。** 逐条对应裁决提出的六个问题：

| 裁决的问题 | 本轮答案 |
|---|---|
| 1. 指纹覆盖哪些对象？ | 要覆盖 E2b–E5 四类盲区，指纹必须含 CHECK 表达式、Policy `qual` / `with_check`、ACL、触发器定义。**这恰是规范化最不稳定的部分** |
| 2. 哪些对象无法稳定规范化？ | CHECK 与 Policy 的表达式经 PG 重写后形态随版本变化（`test_status_constraint_drift` 文件头已记录这个教训：逐字比较全为假红） |
| 3. 版本差异会不会误报？ | 会。本轮为 PG 16.15；跨大版本（15→16）表达式渲染差异会造成大面积假红 |
| 4. 新迁移如何更新指纹？ | 必须人工重算并提交——这就是「第二份需同步的事实源」 |
| 5. 谁维护第二份定义？ | 未定，且与项目「单一事实源」原则直接冲突 |
| 6. 指纹与迁移不一致时谁是权威？ | 无法回答。这是引入指纹后**必然**产生的第二权威冲突 |

### 8.1 已证实的事实（可据以决策）

| # | 事实 | 证据 |
|---|---|---|
| F1 | **Policy 的规律级结构判据已经存在**，不是空白：`scripts/boundary_assertions.py::scope_org_rls_enforced` 对 21 张业务表逐条校验「策略名 / 租户谓词 / 组织谓词形态（列 or 父项目存在性）/ 两跳相关性」，并对 23 张表校验 `FORCE` | `_ORG_RLS_TABLES`（21）、`_ORG_RLS_FORCED`（23）；`tests/unit/test_boundary_org_rls.py` 有 22 条负向用例证明判据会报红 |
| F2 | **该判据的输入是迁移文本回放，不是数据库**：`_policy_statements()` 只 `read_text` + `_rendered_migration_statements()`，不建立任何连接 | 代码路径；本轮实跑 `scope_org_rls_enforced() → (True, '')` |
| F3 | 因此 **E7 的「删策略全绿」与该判据 PASS 可以同时成立**——它们回答的不是同一个问题（「迁移里写了什么」vs「库里现在有什么」）。这不是判据写错了，是**输入选错了层** | E7 + F2 |
| F4 | 应检集合**不是手写清单**：`_tenant_scoped_business_tables()` 从 `db_grants.DOMAINS` 派生，并与登记表双向比对（未登记/陈旧登记都报红）。「加表即漏检」这一历史缺陷已修 | `evaluate_org_rls` 双向差集；`test_unregistered_business_table_is_reported` / `test_stale_registration_is_reported` |
| F5 | **行为覆盖与结构覆盖是两张不同的网，且不重合**：9 张有行为覆盖，19 张没有；19 张中 17 张有某种结构判据（13 迁移文本 + 1 仅 `FORCE` + 3 catalog），2 张（`outbox.outbox_events` 父表、`outbox.dead_letter_replays`）连结构判据也没有 | §9 |
| F6 | **同为 `alembic check` 盲区的触发器，行为覆盖是 14/16**，远高于 RLS 策略的 9/28。缺口不是「数据库对象普遍缺证据」，而是「**可见性**很少被当作断言目标」（写得进/删不掉有测试，看得见/看不见没有） | §9.5 |

### 8.2 尚待设计的方案（未实施，未选型）

按 F1–F3，**不需要新建一套判据体系**，需要的是给既有判据加一个读 catalog 的孪生检查。可选形态有三种，成本与稳健性递增：

| 形态 | 判据 | 优点 | 已知风险 |
|---|---|---|---|
| C1 存在性 | 凡 `relrowsecurity = true` 的表，`pg_policies` 中必须有策略，且策略名/命令/角色与登记一致 | 表达式无关，零规范化风险；直接覆盖 E7 | 不校验谓词内容：策略被替换成 `USING (true)` 也能过 |
| C2 形态 | 在 C1 之上，要求 `qual` 同时引用 `tenant_id` 与 `current_setting('app.current_tenant_id')` | 能挡住「空策略」「只写组织谓词」；仍是子串级判据 | 表达式改写（括号/别名/`::text`）可能假红 |
| C3 等价 | 用 `packages/shared/rls.py` 的谓词生成器重新生成期望表达式，与 `pg_get_expr` 结果做**结构等价**比较 | 与单一事实源同源，最强 | 需处理 PG 版本间的 deparse 差异（§8 六问的 Q2/Q3 正是这条）；维护成本最高 |

三点约束（尚未设计，仅记边界）：

1. **应检集合必须从 catalog 派生**（`relrowsecurity = true` 且属于业务 schema），不能新增第二份表清单——F4 已经把这条教训固化在迁移文本侧，catalog 侧必须同构。
2. **`outbox` 分区不能靠「逐表列举」**：分区是动态建的，判据必须按 `pg_inherits` 派生（现有 `test_every_outbox_partition_is_protected` 已是这个形态，可直接复用其取数）。
3. **判据不能替代行为测试**：C1–C3 只能证明「策略在」，不能证明「策略对」。§9 的 19 张缺口表里，`gate_decisions`、`shots`、`rework_tasks` 等属于**业务上确实没有跨租户读路径**的表，是否要补行为测试取决于该表是否会进入跨租户查询，不能按覆盖率数字机械补齐（见 §9.6）。

**下一步不属于本轮**：C1/C2/C3 选型、catalog 孪生检查的实现、以及是否为 19 张表补行为测试，都需要 Owner 单独裁决。本轮只交事实与选项。

## 9. 28 张 RLS 表的全量覆盖统计（本轮补做）

> 性质：**只读调查**（读 catalog + 读测试源码），不改代码、不改测试、不做新的破坏实验。
> 统计对象：基线库 `afs_struct_probe_base` 中 `relrowsecurity = true` 的 **28 张表**（含 `outbox.outbox_events` 父表与 3 个分区，与 §1.2 的「28 条策略 / 28 张表」口径一致）。

### 9.1 判定方法：不看 grep，看「断言会不会因策略缺失而变化」

「测试里出现表名」与「测试了租户隔离」之间有四道容易走错的关口，逐条设卡：

| 关口 | 反例（本轮实际发现） | 处置 |
|---|---|---|
| ① 表名出现 ≠ 对该表执行 | `tests/unit/test_r16_route_wiring.py` 里的 `production_routes` 是**模块名**，不是表 | 只认 `SQL/ORM 实际指向该表` |
| ② 对该表执行 ≠ 断言隔离 | `test_asset_registration_constraints.py` 大量读写 `asset_registration_receipts`，但断言的是 append-only 与 CHECK | 断言必须关于**可见性 / 越权拒绝** |
| ③ 断言隔离 ≠ 测到 RLS | `test_other_tenant_dead_letters_are_invisible` 的列表 SQL **自带** `WHERE tenant_id = :tenant_id` | 查询带显式租户谓词 ⇒ 策略缺失也照样绿，**不计入** |
| ④ 走管理连接 ≠ 测到 RLS | 多数 `asset_registration_*` / `artifact_manifest_*` 用例用 `admin_engine`（超级用户绕过 RLS） | 只认**域角色连接**下的断言 |

**分类定义**（与裁决一致，仅把「方向」显式化）：

| 类 | 定义 | 操作化判据 |
|---|---|---|
| **A 直接行为覆盖** | 有专门断言验证该表的跨租户/跨组织隔离或越权拒绝 | 断言读的是该表；SQL 无显式租户谓词；删策略会改变结果 |
| **B 间接行为覆盖** | 无专门隔离断言，但该表在真实 PG 路径中被执行，且**删策略会使测试失败** | 实测（E3-v）或可由断言结构推断 |
| **C 仅结构覆盖** | 只有「RLS 开启 / 策略存在 / 定义正确 / ACL 矩阵」类断言，无运行时可见性证据 | 区分**判据读的是迁移文本还是 catalog**（本轮关键区分） |
| **D 无有效覆盖** | 既无结构判据、也无 RLS 行为证据 | — |

**证据等级**：`实测`（E3/E7 真删过策略）> `推断`（读断言结构，判断删策略后结果会变）> `仅结构`。本轮未对 28 张表逐张做删除实验——那会把「统计」变成「扩大实验范围」，且 8 张 A 类的「删策略后 42501 变 23503 / 0 变 1」是确定性推断，不需要逐张重跑。

**形态代号**（策略 `qual` 的四种形态，全部由 `packages/shared/rls.py` 的生成器产生）：

| 代号 | 形态 | 表数 |
|---|---|---|
| T | 仅 `tenant_id = current_setting(...)` | 8（含 4 张 outbox） |
| O | 租户 + `organization_id IS NULL OR = current_organization_id` | 2 |
| P | 租户 + `EXISTS (projects p WHERE p.entity_id = 本表.project_id AND p.tenant_id = 本表.tenant_id)` | 17 |
| Q | 租户 + 两跳 `qc_reports → asset_versions → projects` | 1 |

### 9.2 覆盖矩阵（28 行）

| # | schema.table | RLS | 策略（1 条 / 形态） | 对应测试与用例 | 覆盖行为（方向） | 类 | 证据 / 不足 |
|---|---|---|---|---|---|---|---|
| 1 | asset_service.asset_registration_receipts | ✓ FORCE | `tenant_isolation_asset_registration_receipts`（P） | `test_asset_registration_constraints.py::test_receipt_is_append_only_even_for_superuser` 等 5 例；`test_asset_registration_fault_injection.py` | 无隔离断言（全部 admin 连接） | C | 迁移文本判据覆盖策略形态；行为层 RLS 未参与 |
| 2 | asset_service.asset_versions | ✓ FORCE | `tenant_isolation_asset_versions`（P） | `test_ownership_fk_constraints.py::test_asset_cannot_reference_cross_tenant_project`（层 1 = 42501） | 跨租户写被拒（写侧） | **A** | 删策略后 42501 → 23503，用例会红（推断）；无读侧断言 |
| 3 | asset_service.qc_reports | ✓ FORCE | `tenant_isolation_qc_reports`（Q） | `test_qc_inspector_locator_e2e.py::test_string_locator_produces_qc_report`、`::test_missing_content_produces_rejected_report` | 删策略 → 插入被 RLS 拒 → 2 failed（**E3-v 实测**） | **B** | 实测可检出，但断言目标是「报告没生成」，不是隔离；无读侧 |
| 4 | audit.approval_signatures | ✓ FORCE | `tenant_isolation_approval_signatures`（T） | `test_accumulative_countersign.py` 5 例（append-only / 唯一 / 事件存在） | 无 | C | **只有 FORCE 静态判据，策略形态无判据**；文件头第 6 条宣称「跨租户写入被 RLS 拒绝」但**没有对应用例**（见 9.4-①） |
| 5 | audit.status_transitions | ✓ FORCE | `tenant_isolation_status_transitions`（T） | `test_status_transition_fact.py::test_cross_tenant_insert_rejected_by_rls` | 跨租户写被拒（写侧） | **A** | 断言文案含 `row-level security`；删策略后写入会成功（推断） |
| 6 | budget_quota.budget_ledger | ✓ FORCE | `tenant_isolation_budget_ledger`（T） | `test_budget_settlement_invariants.py` 6 例（缺预留 / 负额 / 并发） | 无（数据全部同租户） | C | 迁移文本判据；`_SETTLED_TOTAL` 无租户谓词，但同租户数据下删策略不改变结果 |
| 7 | budget_quota.shot_budget_requests | ✓ FORCE | `tenant_isolation_shot_budget_requests`（P） | **无** | 无 | C | 全仓 `tests/` 零引用；仅迁移文本判据 |
| 8 | outbox.dead_letter_replays | ✓ FORCE | `tenant_isolation_dead_letter_replays`（T） | `test_outbox_dead_letter_replay_pg.py`（append-only / ACL）、`test_outbox_ops_db_roles.py`（ACL） | 无（列表 SQL 自带 `tenant_id`；重放服务显式比对 `row.tenant_id != tenant_id`） | **D** | 无结构判据、无 RLS 行为；ACL 层另有覆盖（append-only by grant） |
| 9 | outbox.outbox_events（父表） | ✓ | `tenant_isolation_outbox_events`（T） | `test_tenant_isolation_invariants.py::test_outbox_parent_still_fails_closed_without_tenant` | 无租户上下文时为 0 行 | C | **空表即通过**，不构成充分证据；父表不在分区结构判据的循环内 |
| 10 | outbox.outbox_events_default | ✓ | 同上（T，分区继承父表策略名） | `test_tenant_isolation_invariants.py::test_every_outbox_partition_is_protected` | `relrowsecurity` + 策略名（catalog，结构） | C | 只验存在性，不验 `qual` |
| 11 | outbox.outbox_events_y202611 | ✓ | 同上（T） | 同上 | 同上 | C | 同上 |
| 12 | outbox.outbox_events_y202612 | ✓ | 同上（T） | 同上 | 同上 | C | 同上 |
| 13 | production_planning.artifact_manifests | ✓ FORCE | `tenant_isolation_artifact_manifests`（P） | `test_asset_registration_constraints.py::test_manifest_rejects_cross_tenant_project`（admin，23503）、`test_artifact_manifest_abandoned_pg.py` | 无（跨租户用例测的是复合外键） | C | 迁移文本判据；行为层走 admin，RLS 未参与 |
| 14 | production_planning.generation_tasks | ✓ FORCE | `tenant_isolation_generation_tasks`（P） | `test_qc_task_recovery_pg.py::test_cross_tenant_task_is_invisible` | 无（`GenerationFactsLookup` 显式带 `tenant_id` 三元组过滤） | C | 迁移文本判据；404 由应用层过滤保证（见 9.4-③） |
| 15 | production_planning.production_routes | ✓ FORCE | `tenant_isolation_production_routes`（P） | 无（`test_r16_route_wiring.py` 命中的是**模块名**） | 无 | C | 表级零引用；仅迁移文本判据 |
| 16 | project_planning.constraint_packs | ✓ FORCE | `tenant_isolation_constraint_packs`（P） | `test_constraint_pack_boundaries.py::test_constraint_packs_are_invisible_across_tenants` | 跨租户读为 0（读侧） | **A** | SQL 无租户谓词，删策略即读到 1（推断） |
| 17 | project_planning.episodes | ✓ FORCE | `tenant_isolation_episodes`（P） | `test_org_scope_parity.py::test_child_resource_follows_parent_scope`；`test_ownership_fk_constraints.py::test_episode_must_reference_existing_project` | 他组织不可见（读侧）+ 悬空父项目写入 42501（写侧） | **A** | 读侧用组织维度、租户恒定；写侧验的是父项目可见性 |
| 18 | project_planning.organizations | ✓ FORCE | `tenant_isolation_organizations`（O） | `test_ownership_fk_constraints.py::test_organizations_are_tenant_isolated` | 跨租户读为 0（读侧） | **A** | 组织上下文两次注入同一值，把「不可见」逼到租户维度——设计上避免了假阳性 |
| 19 | project_planning.projects | ✓ FORCE | `tenant_isolation_projects`（O） | `test_rls_isolation.py::test_cross_tenant_insert_rejected`、`::test_cross_tenant_rows_invisible_and_not_writable`；`test_org_scope_parity.py::test_matrix_verdict_matches_between_layers` | 读侧（A 看不到 B）+ 写侧（42501）+ 越权 UPDATE 0 行 | **A** | 唯一同时有读、写、UPDATE 三种形态的表 |
| 20 | project_planning.rework_tasks | ✓ FORCE | `tenant_isolation_rework_tasks`（P） | 无（仅在 `test_scene_rls_isolation.py::_NEW_TABLES` 结构清单中） | 无（该清单只断言 RLS/FORCE 开启） | C | 迁移文本判据；「进了清单」不等于「被测过行为」 |
| 21 | project_planning.scenes | ✓ FORCE | `tenant_isolation_scenes`（P） | `test_scene_rls_isolation.py::test_scenes_are_invisible_across_tenants`、`::test_scenes_are_invisible_across_organizations`、`::test_cross_tenant_scene_insert_is_rejected` | 跨租户读 0 / 跨组织读 0 / 跨租户写 42501 | **A** | 三层证据（catalog + 读过滤 + WITH CHECK）齐备，是全仓最完整的单表证据 |
| 22 | project_planning.script_revisions | ✓ FORCE | `tenant_isolation_script_revisions`（P） | `tests/unit/test_composite_fk.py`（复合外键）；`test_least_privilege_grants.py`（DELETE 拒） | 无（非隔离） | C | 迁移文本判据；`test_rls_isolation.py` 只在文件头注释里提到这张表 |
| 23 | project_planning.shot_breakdown_drafts | ✓ FORCE | `tenant_isolation_shot_breakdown_drafts`（P） | 无（仅 `_NEW_TABLES` 结构清单） | 无 | C | 迁移文本判据 |
| 24 | project_planning.shot_revisions | ✓ FORCE | `tenant_isolation_shot_revisions`（P） | `test_shot_revision_concurrency.py`（单租户并发） | 无 | C | 迁移文本判据；并发用例不切租户 |
| 25 | project_planning.shot_specs | ✓ FORCE | `tenant_isolation_shot_specs`（P） | `test_shot_spec_boundaries.py::test_shot_specs_are_invisible_across_tenants` | 跨租户读为 0（读侧） | **A** | 播种走 admin、读取走域角色，谓词无租户列（推断可检出） |
| 26 | project_planning.shots | ✓ FORCE | `tenant_isolation_shots`（P） | 仅 `planning_seed.py` 播种 + `_NEW_TABLES` 结构清单 | 无 | C | 播种走管理连接；无任何域角色读断言 |
| 27 | qc_governance.gate_decisions | ✓ FORCE | `tenant_isolation_gate_decisions`（P） | 无（零引用）；**E7 删策略后 239 passed** | 无 | C | 迁移文本判据 PASS，库内策略已缺失仍全绿（E7 实测）——§8 F2/F3 的实证 |
| 28 | qc_governance.qc_tasks | ✓ FORCE | `tenant_isolation_qc_tasks`（P） | `test_qc_task_recovery_pg.py::test_cross_tenant_task_is_invisible`（未读 `qc_tasks`）、`test_qc_worker_concurrency_pg.py` | 无 | C | 迁移文本判据；跨租户 404 的读取目标是 `generation_tasks` |

### 9.3 覆盖率汇总

| 指标 | 计算 | 结果 |
|---|---|---|
| **有效行为覆盖率**（A+B） | (8+1)/28 | **32.1%** |
| **直接行为覆盖率**（A） | 8/28 | **28.6%** |
| 读侧断言覆盖率 | 6/28（constraint_packs、episodes、organizations、projects、scenes、shot_specs） | **21.4%** |
| 仅结构覆盖（C） | 18/28 | 64.3% |
| 无有效覆盖（D） | 1/28 | 3.6% |

**方向拆分**（回应「写入失败 ≠ 读取隔离」）：A 类 8 张中，**只有 3 张同时有读侧与写侧断言**（projects、episodes、scenes）；`asset_versions`、`status_transitions` 只有写侧；`qc_reports`（B）只有「插入被拒」这一形态。**「跨租户读为 0」这一最强形态只覆盖 6 张表。**

**结构覆盖的实际分布**（与行为覆盖是两个不同的网）：

| 结构证据形态 | 表数 | 覆盖对象 |
|---|---|---|
| 迁移文本判据（`scope_org_rls_enforced`，策略形态） | 21 | `_ORG_RLS_TABLES` 全部业务表（含 gate_decisions / qc_tasks / shots 等） |
| 仅 `FORCE` 静态判据（无策略形态） | 2 | audit.approval_signatures、audit.status_transitions |
| catalog 结构判据（分区） | 3 | outbox_events_default / y202611 / y202612 |
| 无任何 RLS 结构判据 | 2 | outbox.outbox_events（父表）、outbox.dead_letter_replays |

交叉后得到本轮最关键的一张表：

| | 有行为覆盖（A+B = 9） | 无行为覆盖（C+D = 19） |
|---|---|---|
| **有结构判据** | 9 张（8 迁移文本 + 1 仅 `FORCE`） | **17 张**（13 迁移文本 + 1 仅 `FORCE` + 3 catalog） |
| **无结构判据** | 0 | 2 张（`outbox.outbox_events` 父表、`outbox.dead_letter_replays`） |

即：**19 张没有行为覆盖，其中 17 张并非「没有判据」，而是判据看不见数据库**（13 张读迁移文本、1 张只有 `FORCE` 静态判据、3 张只读 catalog 存在性）。这正是把「补判据」与「补测试」分开决策的依据。

### 9.4 四个高信息量个例

**① 文档承诺 ≠ 证据：`audit.approval_signatures`**
文件头列了 6 条保证，第 6 条是「跨租户写入被 RLS 拒绝」，但文件内 5 个用例（`test_sign_revoke_consume_append_only`、`test_duplicate_signature_is_rejected`、`test_cross_seat_same_subject_is_rejected`、`test_consumed_group_cannot_be_reused`、`test_signature_without_event_rejected_at_commit`）没有一条构造跨租户写入。**这比「没有测试」更难发现**：docstring 会被后来者当作已覆盖的依据。

**② 命名是「跨租户不可见」，实现却在应用层：`outbox.dead_letter_replays`**
`test_other_tenant_dead_letters_are_invisible` 与 `test_cross_tenant_dead_letter_is_invisible` 都以「跨租户」命名，但列表查询 `_DEAD_LETTERS_FIRST_PAGE` 自带 `WHERE tenant_id = :tenant_id`，重放 `lock()` 之后服务层又显式 `if row.tenant_id != tenant_id` → **删掉 RLS 策略，两个用例仍然全绿**。这是裁决里「测试引用表名 ≠ 测试了租户隔离」的教科书样本。

**③ 404 由应用过滤保证：`qc_governance.qc_tasks` / `production_planning.generation_tasks`**
`test_cross_tenant_task_is_invisible` 断言 404，但事实读取 `GenerationFactsLookup.lookup()` 的 `select` 里写着 `GenerationTaskModel.tenant_id == tenant_id`。取不到行 → 404，**与 RLS 无关**。该用例是有效的**越权防护**证据，但不是 RLS 证据——两者不可互相替代。

**④ 已有规律判据，仍挡不住 E7：`qc_governance.gate_decisions`**
它就在 `_ORG_RLS_TABLES` 里，`scope_org_rls_enforced` 对它的策略名、租户谓词、父项目存在性逐条校验且 PASS；同时 E7 把库里的策略删掉后 239 条集成测试全绿。**判据与事实之间隔着一层输入**：判据读的是迁移文本（§8 F2）。

### 9.5 旁证：16 个触发器的覆盖映射（不计入 28 的分母）

| 触发器 | 表 | 行为测试 | 覆盖 |
|---|---|---|---|
| `trg_asset_registration_receipts_immutable` | receipts | `test_receipt_is_append_only_even_for_superuser` | 行为 |
| `trg_asset_registration_receipts_no_truncate` | receipts | 无（`test_catalog_facts_are_readable_and_true` 只断言触发器存在） | 结构 |
| `trg_asset_versions_guard` | asset_versions | `test_asset_version_delete_is_rejected`、`test_asset_content_column_update_is_rejected` | 行为 |
| `trg_asset_versions_no_truncate` | asset_versions | 无 | 结构 |
| `trg_qc_reports_guard` | qc_reports | `test_qc_report_update_and_delete_are_rejected` | 行为 |
| `trg_qc_reports_no_truncate` | qc_reports | `test_owner_cannot_truncate_existing_append_only_tables` | 行为 |
| `trg_approval_signatures_event_exists` | approval_signatures | `test_signature_without_event_rejected_at_commit` | 行为 |
| `trg_approval_signatures_guard` | approval_signatures | `test_sign_revoke_consume_append_only` | 行为 |
| `trg_approval_signatures_no_truncate` | approval_signatures | 同上（TRUNCATE 分支） | 行为 |
| `trg_status_transitions_event_exists` | status_transitions | `test_event_link_must_be_same_tenant`、`test_transition_without_event_is_rejected_at_commit` | 行为 |
| `trg_status_transitions_guard` | status_transitions | `test_append_only_blocks_update_and_delete` | 行为 |
| `trg_status_transitions_no_truncate` | status_transitions | `test_owner_cannot_truncate_fact_table` | 行为 |
| `trg_budget_ledger_settlement_guard` | budget_ledger | `test_db_rejects_settlement_pointing_at_missing_reserve`、`test_db_rejects_negative_amount` | 行为 |
| `trg_dead_letter_replays_no_mutation` | dead_letter_replays | `test_audit_rows_are_append_only` | 行为 |
| `trg_dead_letter_replays_no_truncate` | dead_letter_replays | 同上（TRUNCATE 分支） | 行为 |
| `trg_constraint_packs_shots` | constraint_packs | `test_shots_must_exist_in_the_same_project` | 行为 |

**14/16 有行为测试**。这个对比很重要：同样是 `alembic check` 看不懂的对象，触发器几乎条条有行为证据，而 RLS 策略只有 9/28。差别不在「对象类型难测」，而在**测试习惯**——「写不进去 / 删不掉」是显式断言目标，「看不见」很少是。

### 9.6 决策对照

| 裁决给出的情形 | 本轮数据 | 是否命中 |
|---|---|---|
| 大部分表已有有效行为覆盖 | 9/28（32.1%） | 否 |
| 大量表只有结构覆盖或没有覆盖 | 19/28（67.9%） | **是** |
| 多张表存在相同的 Policy 设计规律 | 28 张只有 4 种形态，其中 17 张是同一 P 形态、由同一生成器产出 | **是** |
| Policy 规则并不统一 | 统一（4 种形态，均含租户谓词） | 否 |

同时命中第 2、3 行 ⇒ 优先研究「从 catalog 推导统一判据」（§8.2 的 C1/C2/C3），而不是逐表补测试。但必须同时记住 §8.2 的第 3 条约束：

- **不能为了覆盖率给 19 张表机械各加一条测试**：`shots`、`rework_tasks`、`shot_breakdown_drafts` 等表在生产路径上是否会出现跨租户查询，是需要单独判断的业务事实；给它们硬造跨租户用例，测的是测试自己。
- **也不能为了省事引入一条无法表达各域语义的通用断言**：`organizations` / `projects` 的组织列形态与子表的父项目存在性形态是**两种语义**（§9.1 的 O 与 P），一条 `USING (true)` 级别的通用断言会把两者都放过（C1 的已知缺口）。

---

## 10. 复现步骤

```bash
# 0. 前提：容器 afs-step-feat 运行（端口 15444）
# 1. 建基线（一次）
CREATE DATABASE afs_struct_probe_base;
POSTGRES_DB=afs_struct_probe_base python -m alembic upgrade head
POSTGRES_DB=afs_struct_probe_base python -m alembic check        # 期望 exit 0

# 2. 单变量实验（以 E3 为例）
CREATE DATABASE afs_probe_e3 TEMPLATE afs_struct_probe_base;
psql -d afs_probe_e3 -c "DROP POLICY tenant_isolation_qc_reports ON asset_service.qc_reports"
POSTGRES_DB=afs_probe_e3 python -m alembic check                 # 记录真实 exit
POSTGRES_DB=afs_probe_e3 python -m pytest tests/integration -q   # 追加判据复核
DROP DATABASE afs_probe_e3 WITH (FORCE)
```

编排脚本（实验用，**未入仓**）：`%TEMP%\struct_probe.py`（E1–E5）、`%TEMP%\struct_probe_e6.py`（E6）。
结构化结果：`%TEMP%\struct_probe_results.json`、`%TEMP%\struct_probe_e6.json`。

**保留的库**：`afs_struct_probe_base`（复现基线，可 `DROP DATABASE` 清理）。
临时库 `afs_probe_*` **已全部删除**（含 e2b_verify / e3run / e4run / e5run / e7）。

## 11. 边界与未做什么

1. **未修改任何代码**：迁移、`env.py`、ORM Registry、测试、契约、冻结面全部未动。
2. **未引入**任何结构校验框架或结构清单。
3. **未触碰** P2 剩余项、`PENDING_PHASE1_REVIEW`、原始 ACL 红灯议题。
4. **未改变** `c122daf` / `f29a46f` 的验收记录。
5. **未做**历史状态重放实验（仍按 Owner 裁决暂缓）。
6. **未测**表/列/类型的增删，矩阵中该行标为「未测」而非「能发现」。
7. **§9 的 28 表统计是只读调查**：读 catalog + 读测试源码，**没有**为统计新建/修改任何数据库，**没有**对 28 张表逐张做删除实验；A 类「删策略会红」是断言结构推断，只有 `qc_reports`（E3-v）与 `gate_decisions`（E7）是实测。
8. **§9.5 的触发器映射未改变分母**：16 个触发器单独统计，不与 28 张 RLS 表混算。
