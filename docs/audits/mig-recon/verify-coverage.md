# 逐表覆盖矩阵（受控例外，13 张守卫表）

- 执行时间：2026-11-20T15:42:56+08:00
- 目标：`localhost:15444` 克隆库 `t3cov_lab`（模板 `ai_film_industrial`），用完即删
- 归属外键：已建（a9b8c7d6e5f4_ownership_fk_backfill.py）
- 批次二：已建（f8a9b0c1d2e3_ownership_fk_batch2.py）
- 结果：**70/70**

| # | 阶段 | 用例 | 结果 | 说明 |
| --- | --- | --- | --- | --- |
| B0 | 前置 | 回退归属栈至激活前基线（6 层） | PASS | projects 3→0，asset_versions 3→0; 归属三列外键=0; 两列组织外键=0; 例外映射表=0; 例外判定函数=0; RLS 表达式备份表=0; 批次二归属外键=0 |
| U0 | 前置 | 执行归属外键迁移（21 步） | PASS | projects 已 seed，O0 通过 |
| U0b | 前置 | 归属外键已建（NOT VALID 亦对新写入生效） | PASS | 三列外键=12 |
| U0c | 前置 | 批次二三条归属外键已建（7 步） | PASS | 命中=3/3 |
| U1 | 前置 | 执行受控例外迁移（23 条） | PASS | 无异常 |
| asset_versions.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: asset_versions.project_id 为不可变内容列，禁止改写（ADR-R11） |
| asset_versions.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| asset_versions.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: asset_versions.schema_version 为不可变内容列，禁止改写（ADR-R11） |
| asset_versions.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: asset_versions.project_id 为不可变内容列，禁止改写（ADR-R11） |
| asset_versions.N5 | 外键 | 例外放行后仍受 fk_asset_versions_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "asset_versions" violates foreign key constraint "fk_asset_versions_project_tenant_o |
| delivery_manifests.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: delivery_manifests.project_id 为不可变内容列，禁止改写（每个 Manifest 版本不可变） |
| delivery_manifests.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| delivery_manifests.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: delivery_manifests.schema_version 为不可变内容列，禁止改写（每个 Manifest 版本不可变） |
| delivery_manifests.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: delivery_manifests.project_id 为不可变内容列，禁止改写（每个 Manifest 版本不可变） |
| delivery_manifests.N5 | 外键 | 例外放行后仍受 fk_delivery_manifests_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "delivery_manifests" violates foreign key constraint "fk_delivery_manifests_project_ |
| qc_reports.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: qc_reports 是追加型检测证据，禁止 UPDATE（ADR-R11） |
| qc_reports.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| qc_reports.N3 | 内容列 | 有令牌但改内容列 detector_versions → P0001 | PASS | P0001: qc_reports 是追加型检测证据，禁止 UPDATE（ADR-R11） |
| qc_reports.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: qc_reports 是追加型检测证据，禁止 UPDATE（ADR-R11） |
| qc_reports.N5 | 外键 | 例外放行后仍受 fk_qc_reports_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "qc_reports" violates foreign key constraint "fk_qc_reports_project_tenant_org" |
| generation_tasks.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: generation_execution.generation_tasks.project_id 为不可变内容列，禁止改写 |
| generation_tasks.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| generation_tasks.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: generation_execution.generation_tasks.schema_version 为不可变内容列，禁止改写 |
| generation_tasks.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: generation_execution.generation_tasks.project_id 为不可变内容列，禁止改写 |
| generation_tasks.N5 | 外键 | 例外放行后仍受 fk_generation_tasks_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "generation_tasks" violates foreign key constraint "fk_generation_tasks_project_tena |
| production_routes.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: generation_execution.production_routes.organization_id 为不可变内容列，禁止改写 |
| production_routes.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 organization_id 回读为新值 | PASS | UPDATE 1 now=00000000-0000-0000-0000-000000000012 |
| production_routes.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: generation_execution.production_routes.schema_version 为不可变内容列，禁止改写 |
| production_routes.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: generation_execution.production_routes.organization_id 为不可变内容列，禁止改写 |
| production_routes.N5 | 外键 | 例外放行后仍受 fk_production_routes_organization_tenant 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "production_routes" violates foreign key constraint "fk_production_routes_organizati |
| shot_route_selections.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: generation_execution.shot_route_selections 为受理事实（生效路线），禁止改写（单一路线生成，V205-R14-A9） |
| shot_route_selections.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| shot_route_selections.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: generation_execution.shot_route_selections 为受理事实（生效路线），禁止改写（单一路线生成，V205-R14-A9） |
| shot_route_selections.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: generation_execution.shot_route_selections 为受理事实（生效路线），禁止改写（单一路线生成，V205-R14-A9） |
| shot_route_selections.N5 | 外键 | 例外放行后仍受 fk_shot_route_selections_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "shot_route_selections" violates foreign key constraint "fk_shot_route_selections_pr |
| gate_decisions.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: gate_decisions 是 append-only Gate 裁决事实，禁止 UPDATE （裁决不可改写，修正须新建一条裁决并附新证据） |
| gate_decisions.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| gate_decisions.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: gate_decisions 是 append-only Gate 裁决事实，禁止 UPDATE （裁决不可改写，修正须新建一条裁决并附新证据） |
| gate_decisions.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: gate_decisions 是 append-only Gate 裁决事实，禁止 UPDATE （裁决不可改写，修正须新建一条裁决并附新证据） |
| gate_decisions.N5 | 外键 | 例外放行后仍受 fk_gate_decisions_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "gate_decisions" violates foreign key constraint "fk_gate_decisions_project_tenant_o |
| qc_review_sessions.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: qc_governance.qc_review_sessions.project_id 为不可变内容列，禁止改写 |
| qc_review_sessions.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| qc_review_sessions.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: qc_governance.qc_review_sessions.schema_version 为不可变内容列，禁止改写 |
| qc_review_sessions.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: qc_governance.qc_review_sessions.project_id 为不可变内容列，禁止改写 |
| qc_review_sessions.N5 | 外键 | 例外放行后仍受 fk_qc_review_sessions_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "qc_review_sessions" violates foreign key constraint "fk_qc_review_sessions_project_ |
| constraint_packs.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: shot_planning.constraint_packs.project_id 为不可变内容列，禁止改写（ADR-R27/C2） |
| constraint_packs.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| constraint_packs.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: shot_planning.constraint_packs.schema_version 为不可变内容列，禁止改写（ADR-R27/C2） |
| constraint_packs.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: shot_planning.constraint_packs.project_id 为不可变内容列，禁止改写（ADR-R27/C2） |
| constraint_packs.N5 | 外键 | 例外放行后仍受 fk_constraint_packs_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "constraint_packs" violates foreign key constraint "fk_constraint_packs_project_tena |
| shot_specs.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: shot_planning.shot_specs.project_id 为不可变内容列，禁止改写（ADR-R27/C2） |
| shot_specs.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| shot_specs.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: shot_planning.shot_specs.schema_version 为不可变内容列，禁止改写（ADR-R27/C2） |
| shot_specs.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: shot_planning.shot_specs.project_id 为不可变内容列，禁止改写（ADR-R27/C2） |
| shot_specs.N5 | 外键 | 例外放行后仍受 fk_shot_specs_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "shot_specs" violates foreign key constraint "fk_shot_specs_project_tenant_org" |
| shot_visual_plans.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: shot_planning.shot_visual_plans.project_id 为不可变内容列，禁止改写（ADR-R27/C2） |
| shot_visual_plans.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| shot_visual_plans.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: shot_planning.shot_visual_plans.schema_version 为不可变内容列，禁止改写（ADR-R27/C2） |
| shot_visual_plans.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: shot_planning.shot_visual_plans.project_id 为不可变内容列，禁止改写（ADR-R27/C2） |
| shot_visual_plans.N5 | 外键 | 例外放行后仍受 fk_shot_visual_plans_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "shot_visual_plans" violates foreign key constraint "fk_shot_visual_plans_project_te |
| approval_flows.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: approval_flows 是 append-only 审批事实，禁止 UPDATE（修正须追加新事实） |
| approval_flows.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| approval_flows.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: approval_flows 是 append-only 审批事实，禁止 UPDATE（修正须追加新事实） |
| approval_flows.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: approval_flows 是 append-only 审批事实，禁止 UPDATE（修正须追加新事实） |
| approval_flows.N5 | 外键 | 例外放行后仍受 fk_approval_flows_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "approval_flows" violates foreign key constraint "fk_approval_flows_project_tenant_o |
| approval_steps.N1 | 无令牌 | 改归属列 → P0001（守卫仍在） | PASS | P0001: approval_steps 是 append-only 审批事实，禁止 UPDATE（修正须追加新事实） |
| approval_steps.N2 | 例外放行 | 四条件齐备 → UPDATE 1 且 project_id 回读为新值 | PASS | UPDATE 1 now=proj-2 |
| approval_steps.N3 | 内容列 | 有令牌但改内容列 schema_version → P0001 | PASS | P0001: approval_steps 是 append-only 审批事实，禁止 UPDATE（修正须追加新事实） |
| approval_steps.N4 | 非成员 | 非成员即使令牌+映射齐备 → P0001（不是 42501） | PASS | P0001: approval_steps 是 append-only 审批事实，禁止 UPDATE（修正须追加新事实） |
| approval_steps.N5 | 外键 | 例外放行后仍受 fk_approval_steps_project_tenant_org 约束 → 23503（非 0 行静默） | PASS | 23503: insert or update on table "approval_steps" violates foreign key constraint "fk_approval_steps_project_tenant_o |

## 口径提醒

- 本矩阵证明的命题是「**从激活前基线出发**，13 张表各自都真的接通了受控例外」。B0 在克隆库内先清行业务行再逆序回退五层迁移，故 U0/U1 重放的是真实迁移体，而不是在一个已激活的库上做假升级。
- 激活后的 RLS 层行为由 `rls_acceptance.py` 负责，本矩阵不覆盖 RLS 分支。
- N2 断言 `UPDATE 1` **且回读值已变**：`UPDATE 0` 也算失败（RLS 会静默过滤）。
- N4 断言 `P0001` 而非 `42501`：若得到 42501，说明测到的是权限而不是守卫。
- 内容列由系统目录推导，不由脚本手写；表结构变化会改变探针列，但不会让用例失声。
- N/A 不是通过：它表示该用例的前置条件按设计不存在，因此**不计入通过率**，其背后的缺口单列在「待裁决缺口」。
