# ADR-R11：幂等键作用域、逐跳事件与内容列不可变

- 状态：已实施（S3）
- 日期：2026-11-14
- 关联：审计 `docs/audits/2026-11-14-execution-loop-audit.md` P1-1 / P1-2 / P1-4；
  ADR-R08（执行闭环）；ADR-R10（权限基线）

## 1. 背景：三处"声明成立、事实不成立"

审计用运行时复现确认了三个问题，共同点都是**代码行为与文档声明相反**：

1. **P1-1 幂等键跨资产串号**：`create_qc_report` 只要 `qc_report_id` 命中就返回
   既有报告，不校验它属于哪个资产。给 A 建报告 `shared-report-id`，再把同一 id
   提交给 B，B 会拿到 A 的质检结论——错误证据直接进入批准链。
2. **P1-2 两跳只发一条事件**：批准从 `TECHNICAL_QC` 经 `HUMAN_REVIEW` 到
   `APPROVED`，状态迁移了两次、`entity_version` 推进了两版，但只发一条
   `ASSET_VERSION_APPROVED`，还带一个 `human_review_in_same_call=true` 的补偿
   字段。事后审计无法证明人工复核真的发生过。
3. **P1-4 内容列可被原地改写**：仓储 `save()` 用
   `_orm_update_values()`（除主键与 `entity_version` 外全部列）整列回写，
   每次状态迁移都把 `sha256/storage_ref/...` 一起 UPDATE；授权层只挡住了
   `app_role`，迁移角色/运维直连仍然能改。

## 2. 决策

### 2.1 幂等键的作用域 = (资源类型, 聚合 id)

`qc_report_id` 的幂等语义是"同一资产版本的同一份报告重复提交"，不是"全库唯一
的 id 命中即可复用"。命中已存在报告时，必须校验 `asset_version_id` 一致；
不一致返回 `409 IDEMPOTENCY_CONFLICT`，不返回他人报告、不静默改写。

理由：幂等返回的前提是"这是同一个事实的重复提交"。跨资产命中不是重复提交，
是两个不同事实撞了同一个键——此时返回旧结果等于伪造证据。

### 2.2 一次状态迁移 = 一条事件，事件记录相邻两状态

批准链路拆成两条事件：

| 事件 | from_status | to_status | 关键字段 |
|---|---|---|---|
| `ASSET_VERSION_HUMAN_REVIEW_ENTERED` | `TECHNICAL_QC` | `HUMAN_REVIEW` | `reviewed_by` / `qc_report_count` |
| `ASSET_VERSION_APPROVED` | `HUMAN_REVIEW` | `APPROVED` | `approved_by` / `created_by` / `qc_report_count` |

- 去掉 `human_review_in_same_call`：它是"补偿声明"，事件本身已经证明复核发生。
- `from_status` 记录**相邻**来源状态，而不是整条调用链的起点。审计侧用
  `from_status`/`to_status` 可以逐跳复算，不需要知道调用栈。
- 事件与迁移同事务：先改状态再补事件（或反之）都会留下半成品，禁止。

### 2.3 事件进契约，双向校验

新增 `contracts/runtime/event_catalog.yaml`：每个事件的 `aggregate_type`、
发出文件、下游可依赖的最小 payload 字段集、契约引用。

闸门 `check_event_catalog()` 做四件事：

1. 代码里出现的 `event_type="..."` 必须在契约里登记（禁止私发事件）；
2. 契约里登记的事件必须真被发出（禁止台账陈旧）；
3. `emitted_by` 必须指向真正发出该事件的文件（禁止落点漂移）；
4. 已实现且非 `.READ` 的 action，其 `audit_event` 必须已登记；还没落地的
   （当前是 `PROJECT_CREATED` / `EPISODE_CREATED`）必须在
   `contracts/runtime/event_emission_gaps.yaml` 显式登记原因与解除条件。

第 4 条的意义：它把"动作有、事件无"从隐性变成红线。`.READ` 动作是否写访问
审计由接入层决定，不在本闸门强制——这一条写在这里，是为了让豁免有明确边界，
而不是靠闸门"恰好没查"。

### 2.4 内容列不可变：语句白名单 + 结构触发器，双层

- **语句层**（`sqlalchemy_asset_repo.save`）：只写 `status`、`updated_at`、
  `audit_ref` 三列，内容列不进入 SET，减少写放大、避免"顺手改内容"。
- **结构层**（迁移 `d1e2f3a4b5c6`）：`asset_versions` 的 `BEFORE UPDATE`
  逐列比对，除上述三列 + `entity_version` 外任何列变化即拒绝；`BEFORE DELETE`
  拒绝。`qc_reports` 的 `BEFORE UPDATE OR DELETE` 一律拒绝。

为什么触发器是必要的而不是冗余：

- 列级 `REVOKE UPDATE (...)` 只对特定角色生效，超级用户/迁移角色照样能改；
- 触发器对所有角色生效（含超级用户）；
- 触发器逐列扫描 `pg_attribute`，**新增内容列自动纳入保护**，不维护列清单，
  不会随模型加列而漏保护（这是"创建即保护"在行级的对应实现）。

## 3. 落地物

| 文件 | 变更 |
|---|---|
| `services/asset_service/service/asset_service.py` | 幂等键校验资产归属（409）；批准逐跳发事件，去掉补偿字段 |
| `services/project_planning/service/script_service.py` | `SCRIPT_SUPERSEDED` payload 补 `tenant_id`（与其余事件口径一致） |
| `services/asset_service/repository/sqlalchemy_asset_repo.py` | `save()` 改白名单三列 |
| `packages/shared/versioning.py` | `_orm_update_values` 增加"不可变域禁用"警示 |
| `data/migrations/versions/d1e2f3a4b5c6_asset_immutability_triggers.py` | 两个保护函数 + 两个触发器（可降级） |
| `contracts/runtime/event_catalog.yaml` | 新增，9 个事件的契约 |
| `contracts/runtime/event_emission_gaps.yaml` | 新增，2 条显式缺口 |
| `scripts/contract_check.py` | 新增 `check_event_catalog()` 并接入 main |
| `tests/unit/test_asset_service.py` | 跨资产幂等拒绝；逐跳事件顺序与 payload |
| `tests/unit/test_contract_check.py` | 事件闸门 4 条负向 + 1 条正向 |
| `tests/integration/test_asset_immutability.py` | 触发器行为（含超级用户、状态列仍可写） |

## 4. 验证

单测（fake 仓储，不碰库）：

- 同一 report_id 给 A 再给 B → `IDEMPOTENCY_CONFLICT`，B 仍 `GENERATED`，
  `QC_REPORT_CREATED` 只有 1 条；
- 批准后事件序列末尾两条依次为 `HUMAN_REVIEW_ENTERED` / `APPROVED`，
  `from_status` 分别为 `TECHNICAL_QC` / `HUMAN_REVIEW`；
- 事件闸门：未登记事件、陈旧条目、`emitted_by` 漂移、动作事件缺失无缺口，
  四种情况全部报红；一致时通过。

集成（真库，迁移角色）：

```sql
UPDATE asset_service.asset_versions SET sha256 = repeat('f', 64) WHERE ...;  -- 拒绝
DELETE FROM asset_service.asset_versions WHERE ...;                          -- 拒绝
UPDATE asset_service.asset_versions SET updated_at = updated_at WHERE ...;   -- 允许
UPDATE asset_service.qc_reports SET findings = '[]'::jsonb WHERE ...;        -- 拒绝
DELETE FROM asset_service.qc_reports WHERE ...;                              -- 拒绝
```

## 5. 影响面与代价

- **事件数量变化**：一次从 `TECHNICAL_QC` 发起的批准由 1 条变 2 条。下游若按
  "每个资产一条批准事件"做统计会多算，必须按 `event_type` 区分；契约里已写明。
- **payload 变化**：删除 `human_review_in_same_call`。任何按该字段判断"是否
  同调用"的消费方都要改为读两条事件的 `from_status`。
- **写路径**：`save()` 的 SET 列变少，写入更轻；若未来新增**可变**状态列
  （如 `rejection_reason`），必须同时改仓储白名单与触发器允许列表，否则会被
  触发器挡住——这是有意的摩擦：加可变列必须显式决策。

## 6. 未做（明确登记，不假装完成）

- payload 字段的**静态**校验：当前闸门只校验事件"登记了哪些字段"，不逐字比对
  代码里 payload 的键。要做需要 AST 级提取，留作后续（风险：字段漂移只能靠
  代码评审发现）。
- `.READ` 动作的访问审计：本闸门不强制，是否落 audit_access 事件需要与合规口径
  一起定，未擅自加。
- `PROJECT_CREATED` / `EPISODE_CREATED` 仍不发出：根因是 Project/Episode 域
  没有 service 层与状态变更入口（审计 P2-1），事件已登记为缺口，修复 P2-1 时
  一并关闭。
- `qc_reports` 的 `INSERT` 仍开放给 `app_role`（这是必要的：QC Worker 必须能
  写证据）；不可变约束由触发器保证，不靠撤权。
