# 归属「一致」闸门（机器生成，勿手改）

- 目标库：`ai_film_industrial` @ `localhost:15444`
- 派生候选列：**54** 条（`pg_attribute` 实读）
- 判定：`PASS ⇔ 存在含 project_id 的复合外键 ∧ 指向父表 ∧ convalidated`

| 状态 | 条数 |
| --- | --- |
| UNCLASSIFIED | 0 |
| DANGLING | 0 |
| NO_FK | 0 |
| FK_NOT_COMPOSITE | 0 |
| PENDING_VALIDATE | 0 |
| EXEMPT | 3 |
| PASS | 23 |

**整体判定：PASS**

## EXEMPT（3）

| 表 | 列 | 父表 | 说明 |
| --- | --- | --- | --- |
| `qc_governance.gate_decisions` | `gate_instance_id` | `-` | PARENT_NO_SCHEMA｜`GateInstance` 无 schema——`contracts/phase0/contract_amendments.yaml`（V205-R14-A8 residual_risks）已把它登记为「下一个同型缺口」。无契约的表不能凭空建，否则造出来的是新的无约束事实源，比留一条已知缺口更坏。（前提成立：契约无 GateInstance 且库中无该表） |
| `shot_planning.shot_drafts` | `scene_ref` | `-` | DRAFT_INTERNAL｜`ShotBreakdownDraft` 契约把场次放在 `scene_breakdowns[]` 数组内，**没有**顶层场次引用字段；本列是草案期内部标识，不是对象引用。若将来契约补了顶层 `scene_id`，该豁免自动失效并要求建边。（前提成立：契约确认该对象无顶层场次引用字段） |
| `shot_planning.shots` | `cancellation_ref` | `-` | AUTH_RECORD_UNMODELED｜取消授权所指向的「授权记录」对象**尚未建模**——`contract_amendments.yaml` 明载「专用 Approval 对象仍未建模（见 ADR-R18）」。契约要求该列在 CANCELLED 时必填，但没有任何对象可供外键指向。若将来建模为 Approval 或复用 GateDecision，此豁免自动失效。（前提成立：契约无 AuthorizationRecord / Approval，无可指向的授权对象） |

## PASS（23）

| 表 | 列 | 父表 | 约束 |
| --- | --- | --- | --- |
| `asset_service.qc_reports` | `asset_version_id` | `asset_service.asset_versions` | `fk_qc_reports_asset_version_project` |
| `generation_execution.generation_tasks` | `route_id` | `generation_execution.production_routes` | `组织级边：fk_generation_tasks_route_org` |
| `generation_execution.generation_tasks` | `shot_id` | `shot_planning.shots` | `fk_generation_tasks_shot_project` |
| `generation_execution.shot_route_selections` | `route_id` | `generation_execution.production_routes` | `组织级边：fk_shot_route_selections_route_org` |
| `generation_execution.shot_route_selections` | `shot_id` | `shot_planning.shots` | `fk_shot_route_selections_shot_project` |
| `project_planning.scenes` | `episode_id` | `project_planning.episodes` | `fk_scenes_episode_project` |
| `project_planning.script_revisions` | `episode_ref` | `project_planning.episodes` | `fk_script_revisions_episode` |
| `qc_governance.qc_review_sessions` | `asset_version_id` | `asset_service.asset_versions` | `fk_qc_review_sessions_asset_version_project` |
| `qc_governance.qc_review_sessions` | `qc_report_ref` | `asset_service.qc_reports` | `fk_qc_review_sessions_qc_report_project` |
| `shot_planning.constraint_packs` | `scene_id` | `project_planning.scenes` | `fk_constraint_packs_scene_project` |
| `shot_planning.shot_drafts` | `episode_ref` | `project_planning.episodes` | `fk_shot_drafts_episode_project` |
| `shot_planning.shot_revisions` | `episode_id` | `project_planning.episodes` | `fk_shot_revisions_episode_identity` |
| `shot_planning.shot_revisions` | `episode_ref` | `project_planning.episodes` | `fk_shot_revisions_episode_project` |
| `shot_planning.shot_revisions` | `scene_id` | `project_planning.scenes` | `fk_shot_revisions_scene_project` |
| `shot_planning.shot_revisions` | `shot_id` | `shot_planning.shots` | `fk_shot_revisions_shot_project` |
| `shot_planning.shot_specs` | `shot_id` | `shot_planning.shots` | `fk_shot_specs_shot_project` |
| `shot_planning.shot_specs` | `shot_revision_id` | `shot_planning.shot_revisions` | `fk_shot_specs_shot_revision_project` |
| `shot_planning.shot_visual_plans` | `shot_id` | `shot_planning.shots` | `fk_shot_visual_plans_shot_project` |
| `shot_planning.shots` | `rework_basis_ref` | `workflow.rework_tasks` | `fk_shots_rework_basis` |
| `shot_planning.shots` | `scene_id` | `project_planning.scenes` | `fk_shots_scene_project` |
| `shot_planning.shots` | `shot_revision_id` | `shot_planning.shot_revisions` | `fk_shots_current_revision` |
| `workflow.approval_steps` | `ticket_id` | `workflow.approval_tickets` | `fk_approval_steps_ticket_project` |
| `workflow.approval_tickets` | `flow_id` | `workflow.approval_flows` | `fk_approval_tickets_flow_project` |
