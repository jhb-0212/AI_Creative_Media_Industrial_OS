# 归属「一致」闸门（机器生成，勿手改）

- 目标库：`ai_film_industrial` @ `localhost:15444`
- 派生候选列：**40** 条（`pg_attribute` 实读）
- 判定：`PASS ⇔ 存在含 project_id 的复合外键 ∧ 指向父表 ∧ convalidated`

| 状态 | 条数 |
| --- | --- |
| UNCLASSIFIED | 0 |
| DANGLING | 9 |
| NO_FK | 0 |
| FK_NOT_COMPOSITE | 0 |
| PENDING_VALIDATE | 0 |
| PASS | 11 |

**整体判定：FAIL**

## DANGLING（9）

| 表 | 列 | 父表 | 说明 |
| --- | --- | --- | --- |
| `generation_execution.generation_tasks` | `shot_id` | `shot_planning.shots` | 生成任务服务于某个镜头；目标表在库中不存在 |
| `generation_execution.shot_route_selections` | `shot_id` | `shot_planning.shots` | 路线选择是针对某个镜头做的；目标表在库中不存在 |
| `qc_governance.gate_decisions` | `gate_instance_id` | `qc_governance.gate_instances` | 裁决是对某个 Gate 实例作出的；目标表在库中不存在 |
| `shot_planning.constraint_packs` | `scene_id` | `shot_planning.scenes` | 约束包作用于某个场次；目标表在库中不存在 |
| `shot_planning.shot_drafts` | `scene_ref` | `shot_planning.scenes` | 分镜草案按场次组织；目标表在库中不存在 |
| `shot_planning.shot_revisions` | `scene_ref` | `shot_planning.scenes` | 镜头修订按场次组织；目标表在库中不存在 |
| `shot_planning.shot_revisions` | `shot_id` | `shot_planning.shots` | 修订属于某个镜头身份；目标表在库中不存在 |
| `shot_planning.shot_specs` | `shot_id` | `shot_planning.shots` | 规格属于某个镜头身份；目标表在库中不存在 |
| `shot_planning.shot_visual_plans` | `shot_id` | `shot_planning.shots` | 视觉方案属于某个镜头身份；目标表在库中不存在 |

## PASS（11）

| 表 | 列 | 父表 | 约束 |
| --- | --- | --- | --- |
| `asset_service.qc_reports` | `asset_version_id` | `asset_service.asset_versions` | `fk_qc_reports_asset_version_project` |
| `generation_execution.generation_tasks` | `route_id` | `generation_execution.production_routes` | `组织级边：fk_generation_tasks_route_org` |
| `generation_execution.shot_route_selections` | `route_id` | `generation_execution.production_routes` | `组织级边：fk_shot_route_selections_route_org` |
| `project_planning.script_revisions` | `episode_ref` | `project_planning.episodes` | `fk_script_revisions_episode` |
| `qc_governance.qc_review_sessions` | `asset_version_id` | `asset_service.asset_versions` | `fk_qc_review_sessions_asset_version_project` |
| `qc_governance.qc_review_sessions` | `qc_report_ref` | `asset_service.qc_reports` | `fk_qc_review_sessions_qc_report_project` |
| `shot_planning.shot_drafts` | `episode_ref` | `project_planning.episodes` | `fk_shot_drafts_episode_project` |
| `shot_planning.shot_revisions` | `episode_ref` | `project_planning.episodes` | `fk_shot_revisions_episode_project` |
| `shot_planning.shot_specs` | `shot_revision_id` | `shot_planning.shot_revisions` | `fk_shot_specs_shot_revision_project` |
| `workflow.approval_steps` | `ticket_id` | `workflow.approval_tickets` | `fk_approval_steps_ticket_project` |
| `workflow.approval_tickets` | `flow_id` | `workflow.approval_flows` | `fk_approval_tickets_flow_project` |

## DANGLING 的性质与处置边界

这些是**设计上真实存在**的归属边，目标表（`shots` / `scenes` / `gate_instances`）
在库中尚未创建。不能靠外键修复，也不能归入「不是边」——归入即等于把
「运行时未实施」永久洗白。正确处置是补建目标表，属镜头域运行时落地范畴。

在此之前，这些列仍是**裸字符串**：写一个不存在的 `shot_id` 不会报错。
这一点必须留在报告里，不得因为「其它都 PASS」而被读成保护完备。
