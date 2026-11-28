# C1/C2/C3 选型验证与 19 张缺口表访问路径审查

> 日期：2026-11-26 ｜ 性质：**研究与方案裁决材料，不含代码实现**
> 授权：Owner「批准进入 C1/C2/C3 选型研究，优先验证 C3；不批准立即实现。19 张缺口表不批量补测，先做真实访问路径审查」
> 前序：`2026-11-26-alembic-check-detection-boundary.md`（`62bf243`，§8.2 三方案定义、§9 覆盖统计）
> 环境：PostgreSQL **16.15**（`afs-step-feat`，15444）+ **18.7**（`local-pg18`，5434）；所有实验从 `afs_struct_probe_base` 克隆，实验库已删除
> 边界：未改代码、迁移、`rls.py`、权限矩阵、`boundary_assertions.py`、测试、冻结面；不改变 `62bf243` 验收状态

## 0. 结论先行

| # | 结论 | 证据 |
|---|---|---|
| 1 | **推荐 C3（结构等价）为目标、C1（存在性）为基础不变量，不采用 C2 作为最终判据**——与 Owner 预设方向一致，且已被实测支撑 | §2、§3 |
| 2 | C3 的比较方式**可靠**：把期望谓词经 PostgreSQL `pg_get_expr` 规范化后与 catalog 实际策略比较，空白/冗余括号/`CAST` 写法差异被自动消除；语义差异（GUC 名大小写、省略 `true`）被正确检出 | §2.1 |
| 3 | **迁移回放可作 C3 期望侧事实源（对原始生成器方案的调整），无需第二份清单**：**22/28 张** P/Q/T 类策略回放期望与 catalog 实际 deparse 后逐字相等；**另 6 张尚未完成期望表达式提取**，属提取器覆盖问题而非架构障碍 | §0.1、§2.3 |
| 4 | **跨版本兼容（限 26 条共有策略）**：PG16.15 与 PG18.7 共有的 **26 条**策略 `pg_get_expr` 输出 **26/26 逐字一致、0 差异**；改写实验两版本一致。**不等于 28/28 跨版本验证** | §0.1、§2.2 |
| 5 | C3 能检出 C1/C2 都漏的 `USING (true)` 危险替换；配合「业务表全集」集合派生还能检出意外关闭 RLS（D4）与 RESTRICTIVE 变更（D5） | §3 |
| 6 | **19 张无覆盖表里只有 1 张存在实质高风险：`qc_governance.qc_tasks`**——查询无租户谓词、HTTP 端点无资源级授权、RLS 是唯一租户边界，而现有 404 测试不覆盖它 | §4.3 |
| 7 | E7（`gate_decisions`）全绿的根因是**没有运行中的 SELECT 路径**，不是「RLS 多余」；当前无实质越权，但 `get_by_id` 是无租户谓词的死代码，一旦接线即成风险 | §4.2 |
| 8 | 不批准 19 张机械补测：按真实路径分级后，**优先补测「用户可达 + RLS 为唯一/主要边界」的表（P0 = `qc_tasks`）**，其余按威胁模型逐表裁决 | §5 |

### 0.1 提交前必须保留的四项限定口径（Owner 裁决）

1. **C3 等价验证的分母是 22/28**：28 张 RLS 表中，22 张完成「迁移回放期望 → catalog 实际」的 deparse 等价验证；**另外 6 张尚未完成期望表达式提取**（2 张 audit + 4 张 outbox），不是 28/28 已验证。
2. **PG 版本验证的分母是 26/26，且只覆盖共有策略**：PG16.15 与 PG18.7 的 **26 条共有策略** `pg_get_expr` 输出逐字一致、0 差异；**不等于 28/28 跨版本验证**。PG18 PoC 库停在旧 revision，缺 `approval_signatures` 与 `dead_letter_replays` 两条，属样本差异而非版本差异。两组结果的**分母与验证范围不同，不得混称**。
3. **期望侧事实源是对原始 C3 方案的调整**：`packages/shared/rls.py` 只生成租户/组织两个基础谓词，**不生成完整 P/Q 父项目 `EXISTS` 谓词**。本轮证明的是「**复用现有迁移链回放作为期望侧，再与 catalog 比较**」，**不是**「`rls.py` 能独立生成全部 28 张表的完整期望策略」。这是对原始 C3 生成器方案的事实性调整，实施设计必须按调整后的事实源表述。
4. **对象集合的构成与豁免依据**：28 张 = 21 张 `db_grants.DOMAINS` 业务表 + 2 张 audit 表 + 1 张 `outbox.dead_letter_replays` + 4 张 `outbox_events`（父表 + 3 个 `pg_inherits` 动态发现的分区）。`outbox.maintenance_runs` 是**有意豁免项**，依据为 ADR-R10 §93（仅运维元数据、无租户字段，保留现状）、ADR-R12 §76（该表无租户键，加 RLS 是假隔离）、ADR-R40 §498（授权矩阵登记）与迁移 `b5c6d7e8f9a0`（实际 REVOKE app_role）。**当前没有专门的机器可读 RLS 豁免台账**；§3.2/§6.1 中「登记豁免」是实施阶段的前置治理要求，不是本轮已存在的机制。

`qc_tasks` 另有以下限定：**不得把 `generation_tasks` 的 404 测试算作 `qc_tasks` 的覆盖**；不因 RLS 策略存在就认定已有充分测试证据；**本轮没有对 `qc_tasks` 做删策略实测，不能推断删策略必然造成数据泄漏**——实际后果需区分「策略删除后的默认拒绝」「策略被错误放宽」「连接身份是否为超级用户/域角色」「租户上下文是否注入」四种状态后另行裁决。`gate_decisions` 当前只有 INSERT 生产路径、GET 相关代码未接线，**未来可能的风险不得写成当前已发生的漏洞**。

## 1. C1 / C2 / C3 定义与本次验证口径

| 方案 | 判据 | 本次验证内容 |
|---|---|---|
| **C1 存在性** | 凡应启用 RLS 的表，`pg_policies` 中须有策略，名称/命令/角色与登记一致 | 验证它能发现 D1（删策略）、D4（关 RLS），但**不能**发现 D2（`USING true`） |
| **C2 子串形态** | 检查 `qual` 是否含租户列与 `current_setting` 子串 | 验证它会被 D2 欺骗（策略名与子串都还在）——**不作为最终判据** |
| **C3 结构等价** | 期望谓词与 `pg_get_expr(polqual)` 经同一 deparse 管线后**结构等价** | 验证比较可靠性、版本边界、字段完整性、对象集合完整性 |

## 2. C3 可行性的实测验证

### 2.1 比较方式可靠性：用 PG deparse 消除语法噪声

**方法**：把期望谓词与实际策略都放进 PostgreSQL 的表达式存储位置，用 `pg_get_expr` 取回规范化文本，再比较——不做裸字符串相等。

生成器原始输出（`packages/shared/rls.py:49-77`）：

```
tenant = tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
org    = (organization_id IS NULL OR organization_id = NULLIF(current_setting('app.current_organization_id', true), '')::uuid)
```

PG deparse 后（两版本一致）：

```
(tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid)
((organization_id IS NULL) OR (organization_id = (NULLIF(current_setting('app.current_organization_id'::text, true), ''::text))::uuid))
```

**改写实验结果**（`%TEMP%\c3_equiv_results.json`）：

| 改写 | deparse 结果 | 判定 |
|---|---|---|
| 原始 | 基准形态 | — |
| 前后空白 / 多余括号 | **与基准逐字相同** | 正确归一（无假红） |
| `CAST(... AS uuid)` 替代 `::uuid` | **与基准逐字相同** | 正确归一 |
| GUC 名改大写 `APP.CURRENT_TENANT_ID` | **不同**（保留大写） | 正确检出（语义确实不同） |
| 省略 `current_setting(..., true)` 的 `true` | **不同**（deparse 为 `current_setting('...'::text)`） | 正确检出（未定义时行为不同） |

**结论**：deparse 规范化能消除纯语法差异，同时保留真实语义差异。**它不是「无脑归一」，这正是 C3 比 C2 可靠的原因**。

一个工程陷阱（已踩并修正）：**CHECK 约束不能承载含 `EXISTS` 子查询的 P/Q 类谓词**（PG 禁止），必须用 POLICY USING 或 VIEW 做 deparse。另一陷阱：规范化函数若返回 `None` 被当作空串比较，会产生「假绿」——本轮第一版脚本即如此，已修正为显式 `NORMALIZE_FAILED`。

### 2.2 版本边界：PG16.15 vs PG18.7

**项目实际支持的版本**（事实源）：
- 生产与 CI 基线：`postgres:16`（`.github/workflows/ci.yml:20`、`infra/compose/docker-compose.yml:71`），实测 16.15。
- PG18.7 是**未获批切换的候选**：ADR-R44 §4.1/D2 明定「现行基线维持 PG16」，TC-2 `LIFTED` 仅解除决策阻塞，`remaining_gate` 要求 TECH-01 五项全过 + 独立 ADR 修订 + 冻结重签（`contracts/phase0/verification_blocks.yaml:171-197`）。TECH-01 PoC 已 PASS 但**不构成切换授权**（`docs/audits/2026-11-23-tech01-pg18-poc.md:6,125`）。

**实测**：两版本共有的 **26 条策略**，`pg_get_expr(polqual)` 输出 **26/26 逐字一致、0 差异**；§2.1 的全部改写实验在两版本上结果一致。PG18 PoC 库缺 `approval_signatures` 与 `dead_letter_replays`（库停在旧 revision），属样本差异非版本差异。

**结论**：C3 的 deparse 方案在 16.15 与 18.7 的**26 条共有策略**上都成立，**不等于 28/28 跨版本验证**。但**需登记为持续义务**：若将来 PG 大版本升级，`pg_get_expr` 渲染变化须在升级 PoC 中重验（这正是 Owner 要求的「不假定 16.15 自动适用其他版本」）。

### 2.3 期望侧事实源：复用迁移回放，不新增清单

**方法**：调用现有 `scripts/boundary_assertions.py::_policy_statements()`（按 alembic 版本链回放迁移，取最终 `CREATE POLICY` 语句），提取 USING 表达式作为期望侧；经 §2.1 的 deparse 规范化后与 catalog 比较（`%TEMP%\c3_full_equiv2.json`）。

**结果**：27 条回放候选中，**22 张表 MATCH（deparse 后逐字相等）**，6 张 `NO_EXPECTED`：

| 类别 | 表 | 原因 |
|---|---|---|
| audit 2 张 | `approval_signatures`、`status_transitions` | 策略由早期迁移的手写形态创建，正则未匹配 |
| outbox 4 张 | `outbox_events` 父表 + 3 分区 | `CREATE POLICY` 跨行书写（如 `b2c3d4e5f6a7:47-57`），正则 `ON (\w+)\.(\w+)` 遇换行失败 |

这 6 张是**提取器工程问题**（扩展正则/按 `op.execute` 多行 SQL 提取即可），不是 C3 架构不可行；且其期望仍可从单一事实源取得（迁移文本），**不需要第二份手工清单**。

**重要事实缺口（对原始 C3 生成器方案的调整）**：`packages/shared/rls.py` 只生成租户/组织两个基础谓词，**不生成完整 P/Q 父项目 `EXISTS` 谓词**；18 张 P 形态表的父项目 `EXISTS` 由各迁移的 `_visibility()` 函数（如 `e1f2a3b4c5d6:106-115`）拼接。本轮证明的架构是「**期望侧复用迁移链回放，再与 catalog 比较**」，不是「`rls.py` 可独立生成全部 28 张表的完整期望策略」。**若只拿 `rls.py` 的两个基础谓词做期望，会漏掉 P/Q 类的组织维度**，实施设计不得把二者混称。

### 2.4 C3 对 Owner 五项要求的逐条应答

| Owner 要求 | 应答 |
|---|---|
| ① 比较方式可靠（非裸字符串） | ✅ 已验证 deparse 规范化，见 §2.1 |
| ② 版本边界明确 | ✅ 16.15/18.7 实测 26/26 一致，见 §2.2；升级须重验 |
| ③ 检查内容完整（qual/with_check/cmd/roles/permissive） | ✅ 全部字段可从 `pg_policy` 读取；实测 `polcmd`/`polroles`/`polpermissive`/`with_check` 形态（`%TEMP%\c3_equiv_results.json` field_shapes），D5 验证 RESTRICTIVE 可检出 |
| ④ 对象集合完整（含意外关闭 RLS） | ⚠️ 见 §3.2——需从「业务表全集 − 登记豁免」派生，不能只查 `relrowsecurity = true` |
| ⑤ 不新增第二份清单 | ✅ 期望复用迁移回放，集合复用 `db_grants.DOMAINS` + `pg_inherits` 派生 |

**第 ④ 项是唯一需要额外设计约束的点**，详见 §3.2。

## 3. C1/C2/C3 检出能力对照（单变量破坏实验）

每项从 `afs_struct_probe_base` 克隆新库，破坏单一对象后取 catalog 快照，与基线比对。结果（`%TEMP%\c3_detect_results.json`）：

| 场景 | 破坏 | catalog 变化 | C1 | C2 | C3 |
|---|---|---|---|---|---|
| D1 | 删策略 | `policy_exists=False`、`qual_changed=True` | ✅ 发现 | ✅ 发现 | ✅ 发现 |
| **D2** | **`USING (true)` 替换** | 策略仍在、字段未变、**仅 `qual` 变** | ❌ **漏** | ❌ **漏**（子串仍在） | ✅ **发现** |
| D3 | 删租户谓词、留父项目 EXISTS | 仅 `qual` 变 | ❌ 漏 | ⚠️ 部分（租户子串不在） | ✅ 发现 |
| **D4** | **意外关闭 RLS** | `rls_changed=True`、策略仍在 | ⚠️ 取 `relrowsecurity=true` 则漏 | ⚠️ 同左 | ✅ 发现（若集合为全表） |
| D5 | 改 RESTRICTIVE | `permissive` 变、`qual` 亦变 | ❌ 漏 | ❌ 漏 | ✅ 发现 |

**D2 是决定性场景**：它正是 Owner 指出的「C1 发现不了、C2 会误判安全」的危险替换，只有 C3 的结构等价能抓到。这支撑「C1 作基础不变量、C3 作完整判据，不采用 C2」的推荐。

### 3.1 分区发现的陷阱

`pg_inherits` 同时包含**索引继承**关系（如 `ix_outbox_tenant` → `outbox_events_default_tenant_id_idx`）。直接按 `pg_inherits` 派生会把索引当成表。必须加 `c.relkind IN ('r','p')` 过滤。实测过滤后得到 3 个真实分区，各有 1 条同名策略且 `relrowsecurity=t`。

### 3.2 检查对象集合的正确公式

| 派生方式 | 结果 | 问题 |
|---|---|---|
| `db_grants.DOMAINS` 业务表（21 张） | 漏 7 张（2 audit + 1 dead_letter + 4 outbox） | 不覆盖全部 RLS 表 |
| 业务 schema 全表（`relkind r/p`） | 29 张，多抓 `outbox.maintenance_runs` | 该表有意无 RLS，依据见下 |
| **业务 schema 全表 − 登记豁免** | **28 张，与基线一致** | ✅ 推荐；豁免依据必须显式登记，不硬编码 |

最终集合 = 21 张 `DOMAINS` 业务表 + 2 张 audit 表 + `outbox.dead_letter_replays` + `outbox_events` 父表 + 3 个经 `pg_inherits` 动态发现的分区 = **28**。新增表自动进入，删策略或关 RLS 都会报红。

**`maintenance_runs` 豁免依据来源**：ADR-R10 §93（仅运维元数据、无租户字段，保留现状）、ADR-R12 §76（无租户键、加 RLS 是假隔离）、ADR-R40 §498（授权矩阵登记）、迁移 `b5c6d7e8f9a0`（P3-5 实际 REVOKE `app_role` 全部权限）。**注意：当前仓库没有专门的机器可读 RLS 豁免台账**；把这张表从「业务全表」中排除的登记机制，是 C1/C3 实施阶段的**前置治理要求**，不是本轮已存在的事实。

## 4. 19 张缺口表的真实访问路径与风险分级

**方法**：只读审查服务仓储、API 路由、worker、执行身份与上下文注入（不运行测试、不改代码）。证据均带文件:行号。

**口径区分**：§9 的 A/B/C/D 是「RLS 行为测试覆盖」；本节是「运行可达性/实质越权风险」。**C 类（仅结构覆盖）≠ 无运行路径**——多数表有完整 API，只是缺隔离断言。

### 4.1 三分类汇总

以下严格对应 `62bf243` §9.2 的 **19 张 C+D 类表**（不含 A/B 类），合计 19：

| 分类 | 表 | 数量 |
|---|---|---|
| **用户可达（高）** | `budget_ledger`、`shot_budget_requests`；`rework_tasks`、`script_revisions`、`shot_breakdown_drafts`、`shot_revisions`、`shots`、`generation_tasks`、`production_routes`；`outbox_events`（父）、`dead_letter_replays`（有 HTTP 写端点）；`gate_decisions`、`qc_tasks` | 13 |
| **仅内部服务（中）** | `artifact_manifests`（Worker/Relay）、`asset_registration_receipts`（Relay）、3 个 outbox 分区 | 5 |
| **无运行路径（低）** | `approval_signatures`（服务类存在但无路由、无 worker 调用） | 1 |

注：`audit.status_transitions` 在 §9.2 属 **A 类**（`test_cross_tenant_insert_rejected_by_rls`，域角色写入侧有负向断言），只是没有读侧断言——它**不在**这 19 张里。其运行路径风险为高（状态推进端点极广），但这是另一个维度的问题，不并入本节缺口统计。

### 4.2 四个指定问题的答复

**① `audit.approval_signatures`（低）**：迁移 `e4f5a6b7c8d9:165-173` 确实 ENABLE+FORCE RLS，结构上「域角色 + 已注入上下文 + 跨租户 INSERT」会被拒；但真实写入路径（`services/identity_access/countersign.py:154-170`）目前只被集成测试实例化，无 HTTP 路由、无 worker 调用。跨租户防护实际由 Principal 取 tenant（`:210-221`）、服务校验（`:112-129`）、唯一约束 + SECURITY DEFINER 触发器（同时比对 `event_id` 与 `tenant_id`）承担。**处置：文档第 6 条承诺应改为「结构上由 RLS + 唯一约束保证，当前无运行路径」——补真实验证或纠正承诺，二者择一，由 Owner 裁决。**

**② `qc_governance.gate_decisions`（可达高 / 实质低）**：生产路径只有 INSERT（`services/qc_governance/repository/sqlalchemy_repo.py:93-98` ← `gate_service.py:16-68`），经 `gate_routes.py:58-95` 写入，身份 `qc_governance_role`，tenant 来自验签 Principal + 父 Project 归属判定（`:70-82`）。唯一 SELECT `get_by_id()`（`:100-105`，**无 tenant 谓词**）只被 `scripts/verify_r16_persistence.py:178` 调用，未接线。**E7 全绿的根因是没有运行中的 SELECT，删 USING 策略不影响 INSERT**——不是 RLS 多余。潜在条件风险：若未来新增 GET 端点复用 `get_by_id`，RLS 将成为唯一防线，届时 E7 变成真实静默越权。

**③ `outbox.dead_letter_replays`（可达高 / 实质低）**：唯一 DML 是 INSERT（`services/outbox_ops/repository/dead_letter_repo.py:195-196`），由 `POST /outbox/dead-letters/{event_id}/replay` 触发（`outbox_ops_routes.py:141-169`）。列表 SQL 自带 `WHERE tenant_id = :tenant_id`（`dead_letter_repo.py:51-76`），重放 `lock()` 后服务层显式 `if row.tenant_id != tenant_id → 404`（`dead_letter_service.py:205-219`）。**数据库层隔离是合理纵深防御，但不是当前唯一防线**。真正需要 RLS 兜底的是 `outbox_events` 的 `lock()`/`UPDATE`（无应用层租户谓词）。

**④ `qc_governance.qc_tasks` 与 `production_planning.generation_tasks`（结论不同，必须分开）**：
- `generation_tasks`：`GenerationFactsLookup.lookup()` 的 SQL 显式带 `tenant_id == tenant_id` 三元组（`generation_facts_lookup.py:51-55`），404 由应用层保证，**不依赖 RLS**——是有效越权证据，但非 RLS 证据。
- **`qc_tasks`：`get_by_id`/`find_by_generation_task`/`next_claimable_for_claim`/`list_expired` 全部无租户谓词**（`qc_task_repo.py:125-165`）；5 个 HTTP 端点中 4 个 PROCESS 端点只有动作级 `require_role`，无资源级授权（`qc_task_routes.py:207-287`）；`_RESOLVE_SQL` 无 `QCTask` 键（`packages/identity/ownership.py:97-236`）。**RLS 是该表唯一租户边界**；现有 `test_cross_tenant_task_is_invisible` 读的是 `generation_tasks`，**不得算作 `qc_tasks` 的覆盖**，也不因策略存在就认定已有充分测试证据。QC worker 的 `_make_scope`（`workers/qc/__main__.py:70-93`）不注入租户上下文（fail-closed）。**本轮未做 `qc_tasks` 删策略实测**：删除后的实际后果须区分默认拒绝、策略错误放宽、连接身份（域角色/超级用户）与租户上下文注入状态，**不直接推断必然泄漏**。

### 4.3 风险分级结论

**唯一实质高风险：`qc_governance.qc_tasks`**——用户可达、RLS 为唯一边界、现有测试不覆盖。这是**风险分级**判断，不是「删策略必然泄漏」的实测结论；后果仍取决于策略删除后的默认拒绝、错误放宽形态、连接身份与租户上下文状态。其余高可达性表（预算、规划、状态事实）都有域角色 + 上下文注入 + 应用层授权的多重防护，缺的是「删策略能报红」的行为证据，而非当前的越权漏洞。`gate_decisions` 当前只有 INSERT 生产路径、GET 代码未接线，**未来接线后的潜在风险不等于当前已发生的漏洞**。

## 5. 需要新增行为测试的具体对象与边界（**建议，未批准**）

按 Owner「不按 19 张数字机械补齐」的要求，以下按真实路径给出候选，**均不实施**：

| 优先级 | 对象 | 测试目的 | 验证边界 |
|---|---|---|---|
| **P0** | `qc_tasks` 的 `get_by_id` / `find_by_generation_task` / `next_claimable_for_claim` | **实测**域角色下策略删除/错误放宽后的真实可见性，不预设必然泄漏 | 域角色连接 + 无显式租户谓词的查询 + 区分默认拒绝 / 错误放宽 / 连接身份 / 上下文注入四种状态后再定断言 |
| P1 | `budget_ledger` / `shot_budget_requests` | 证明写钱路径的租户隔离不只剩应用层 | 域角色 + 跨租户父项目写入/读取断言 |
| P1 | `production_routes` / `generation_tasks` / `rework_tasks` / `shot_*` 系列（用户可达 7 张） | 证明对外 CRUD 的 RLS 真生效 | 每表 1 条「域角色下跨租户读为 0」即可，**不逐表复制模板**；优先选已有 e2e 路径的表复用 |
| P2 | `artifact_manifests` / `asset_registration_receipts` | 内部 Saga 路径， Worker 已显式注入租户 | 只在威胁模型要求时补（如 Relay 身份误配场景） |
| 不补 | `gate_decisions` / `dead_letter_replays` / `approval_signatures` / outbox 分区 | 无运行 SELECT 或已有应用层兜底 | 改为在 C3 结构判据中覆盖；`gate_decisions` 的 `get_by_id` 应标记「接线前必须补 RLS 测试」 |

**明确不做**：给每张表机械加一条测试；引入抹平 O（组织列）与 P（父项目存在性）两种语义的通用断言；用 admin 连接写「隔离测试」（那是假证据）。

## 6. 推荐实施方案与剩余风险

### 6.1 推荐方案

**C3 为目标、C1 为基础不变量**，分两步（**均需 Owner 另行批准后才实施**）：

1. **C1 基础不变量**（先落地，成本低）：从「业务 schema 全表 − 登记豁免」派生集合，校验每张应检表 `relrowsecurity=true`（D4）、策略存在且名/命令/角色与登记一致（D1）。事实源：`db_grants.DOMAINS` + `pg_inherits`（过滤索引）+ 契约登记的豁免表。
2. **C3 完整判据**（在 C1 之上）：复用 `_policy_statements()` 迁移回放作为期望侧，把 6 张 NO_EXPECTED 的提取器补齐；把期望与 catalog 实际策略都经 `pg_get_expr` 规范化，比较 `qual`、`with_check`、`polcmd`、`polroles`、`polpermissive`。规范化失败必须报 `NORMALIZE_FAILED`，不得当空串"匹配"（§2.1 的假绿陷阱）。

**不采用 C2 作为最终判据**：子串命中不等于语义正确（D2 实证）。

### 6.2 剩余风险与不能自动验证的事项

1. **规范化函数返回 None 的假绿**：实施时必须有负向测试（删策略/改谓词必须报红），否则「判据通过」无意义——本轮第一版脚本就踩过这个坑。
2. **6 张表提取器覆盖**：迁移回放的正则需补跨行 `op.execute` 与手写形态，否则 C3 对它们失效（但不影响 C1）。
3. **PG 升级的 deparse 漂移**：16.15/18.7 当前一致，但大版本升级须在升级 PoC 重验 C3；这是持续义务，不是一次性结论。
4. **C3 证明的是「策略在且形态对」，不能证明「策略语义在真实数据上隔离正确」**——`USING (true)` 能过 C1、被 C3 挡住，但更隐蔽的逻辑错误（如组织谓词写反）仍需行为测试。判据与行为测试互补，不可互相替代。
5. **`qc_tasks` 的实质风险不是结构判据能完全覆盖的**：即使 C3 上线，仍需 P0 行为测试证明 RLS 真生效——因为它的 HTTP 端点无资源级授权，RLS 是唯一边界。
6. **运行时身份分离**：`workers/qc`、`workers/generation` 的会话不注入租户上下文（fail-closed），测试靠 conftest 补齐；生产若误配身份（超级用户）会绕过全部 RLS（`rls.py:245-272` 有启动期拦截，但 worker 未必走同一入口）——此项属身份治理，不在本方案范围。

### 6.3 本轮边界声明

- 未修改任何代码、迁移、`env.py`、ORM Registry、`rls.py`、`boundary_assertions.py`、权限矩阵、测试、契约或冻结面。
- 未改变 `62bf243` 的验收状态；本报告为新增审计文档，不进入冻结产物哈希范围。
- 实验只用独立克隆库（`afs_c3_*`，已全部删除）与只读 catalog 查询；既有业务库、验证库、生产库一律未连接。
- 未批准事项（编写正式测试、改 `boundary_assertions.py`、改 `rls.py`/迁移/权限矩阵、改冻结面/P2/`PENDING_PHASE1_REVIEW`）一律未执行。
- 实验脚本与原始 JSON 保留在 `%TEMP%`（`c3_equiv_probe.py`、`c3_detect_probe.py`、`c3_full_equiv2.py` 及对应结果），未入仓。

## 7. 复现要点

```bash
# 版本
docker exec afs-step-feat psql -U postgres -Atc "select version()"   # 16.15
docker exec local-pg18  psql -U postgres -Atc "select version()"     # 18.7

# 单变量实验：从干净基线克隆
psql -U postgres -c "CREATE DATABASE afs_c3_d2 TEMPLATE afs_struct_probe_base"
psql -U postgres -d afs_c3_d2 -c "DROP POLICY tenant_isolation_qc_reports ON asset_service.qc_reports; \
  CREATE POLICY tenant_isolation_qc_reports ON asset_service.qc_reports USING (true)"
psql -U postgres -d afs_c3_d2 -c "SELECT pg_get_expr(polqual,polrelid) FROM pg_policy WHERE polrelid='asset_service.qc_reports'::regclass"
psql -U postgres -c "DROP DATABASE afs_c3_d2 WITH (FORCE)"
```
