# 归属闭环验收台账（机器生成，勿手改）

- 目标库：`ai_film_industrial` @ `localhost:15444`
- 采集时间：2026-11-20 15:17:33
- 判据：`PASS ⇔ 约束存在 ∧ convalidated ∧ 违规行数 = 0`（ADR-R40 §13 D-10.1 / 附录 J.2）
- **整体判定：PASS（17/17 闭环）**

| # | 表 | 类型 | 目标约束 | 约束存在 | convalidated | 违规行数 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `generation_execution.production_routes` | B | `fk_production_routes_organization_tenant` | 是 | true | 0 | PASS |
| 2 | `generation_execution.generation_tasks` | A | `fk_generation_tasks_project_tenant_org` | 是 | true | 0 | PASS |
| 3 | `generation_execution.shot_route_selections` | A | `fk_shot_route_selections_project_tenant_org` | 是 | true | 0 | PASS |
| 4 | `shot_planning.shot_drafts` | A | `fk_shot_drafts_project_tenant_org` | 是 | true | 0 | PASS |
| 5 | `shot_planning.shot_revisions` | A | `fk_shot_revisions_project_tenant_org` | 是 | true | 0 | PASS |
| 6 | `shot_planning.shot_visual_plans` | A | `fk_shot_visual_plans_project_tenant_org` | 是 | true | 0 | PASS |
| 7 | `shot_planning.constraint_packs` | A | `fk_constraint_packs_project_tenant_org` | 是 | true | 0 | PASS |
| 8 | `shot_planning.shot_specs` | A | `fk_shot_specs_project_tenant_org` | 是 | true | 0 | PASS |
| 9 | `asset_service.delivery_manifests` | A | `fk_delivery_manifests_project_tenant_org` | 是 | true | 0 | PASS |
| 10 | `asset_service.asset_versions` | A | `fk_asset_versions_project_tenant_org` | 是 | true | 0 | PASS |
| 11 | `asset_service.qc_reports` | A | `fk_qc_reports_project_tenant_org` | 是 | true | 0 | PASS |
| 12 | `qc_governance.gate_decisions` | A | `fk_gate_decisions_project_tenant_org` | 是 | true | 0 | PASS |
| 13 | `qc_governance.qc_review_sessions` | A | `fk_qc_review_sessions_project_tenant_org` | 是 | true | 0 | PASS |
| 14 | `workflow.approval_flows` | A | `fk_approval_flows_project_tenant_org` | 是 | true | 0 | PASS |
| 15 | `workflow.approval_tickets` | A | `fk_approval_tickets_project_tenant_org` | 是 | true | 0 | PASS |
| 16 | `workflow.approval_steps` | A | `fk_approval_steps_project_tenant_org` | 是 | true | 0 | PASS |
| 17 | `project_planning.projects` | ROOT | `fk_projects_organization_tenant` | 是 | true | 0 | PASS |

**隔离区占比**（#25 防垃圾桶）：projects 共 3 行，隔离项目 1 个，比值 0.333333。
该比值只应随时间下降；持续上升须触发复审，不得当作正常态。

> 本台账**不接受人工填写**。任一行为 `待验证 PENDING_VALIDATE` 时，整体即未闭环；
> 不得以「迁移已执行」「约束已创建」替代该项判定（D-10.1 四条不可接受结果）。
