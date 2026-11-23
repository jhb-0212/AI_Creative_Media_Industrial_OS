# 正式链归属验收台账（机器生成，勿手改）

- 目标库：`ai_film_api_tests` @ `localhost:15444`（**main 正式链**，非分叉库）
- 判据：`PASS ⇔ 项目真实 ∧ 租户一致 ∧ 数据干净 ∧ 相关外键已 VALIDATE`
- 目标集合：从 `information_schema` / `pg_constraint` **实读派生**，不抄清单
- **整体判定：PASS**
  - 「真实」+「租户一致」：15/15 通过
  - 「一致」（复合外键同项目）：6/6 通过

> 与分叉库台账（`acceptance-ledger.md`，35/35）不可互推：两库谱系不同。
> 分叉库是「三列外键 + 冗余 organization_id」；正式链是「两列外键 + 组织只在 projects」。
> ADR-R41 D5 禁止跨链挪用证据。

## 一、「真实」与「租户一致」

| # | 表 | tenant_id | 直接外键 | 项目真实可达 | 依据 | 覆盖 tenant | validated | 孤儿 | 租户不一致 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `asset_service.asset_versions` | 是 | `fk_asset_versions_project_tenant` | 是 | 直接 ← fk_asset_versions_project_tenant | 是 | true | 0 | 0 | PASS |
| 2 | `budget_quota.shot_budget_requests` | 是 | `fk_shot_budget_requests_project_tenant` | 是 | 直接 ← fk_shot_budget_requests_project_tenant | 是 | true | 0 | 0 | PASS |
| 3 | `production_planning.generation_tasks` | 是 | `fk_generation_tasks_project_tenant` | 是 | 直接 ← fk_generation_tasks_project_tenant | 是 | true | 0 | 0 | PASS |
| 4 | `production_planning.production_routes` | 是 | `fk_production_routes_project_tenant` | 是 | 直接 ← fk_production_routes_project_tenant | 是 | true | 0 | 0 | PASS |
| 5 | `project_planning.constraint_packs` | 是 | `fk_constraint_packs_project_tenant` | 是 | 直接 ← fk_constraint_packs_project_tenant | 是 | true | 0 | 0 | PASS |
| 6 | `project_planning.episodes` | 是 | `fk_episodes_project_tenant` | 是 | 直接 ← fk_episodes_project_tenant | 是 | true | 0 | 0 | PASS |
| 7 | `project_planning.rework_tasks` | 是 | `fk_rework_tasks_project_tenant` | 是 | 直接 ← fk_rework_tasks_project_tenant | 是 | true | 0 | 0 | PASS |
| 8 | `project_planning.scenes` | 是 | `fk_scenes_project_tenant` | 是 | 直接 ← fk_scenes_project_tenant | 是 | true | 0 | 0 | PASS |
| 9 | `project_planning.script_revisions` | 是 | `fk_script_revisions_project_tenant` | 是 | 直接 ← fk_script_revisions_project_tenant | 是 | true | 0 | 0 | PASS |
| 10 | `project_planning.shot_breakdown_drafts` | 是 | `fk_shot_breakdown_drafts_project_tenant` | 是 | 直接 ← fk_shot_breakdown_drafts_project_tenant | 是 | true | 0 | 0 | PASS |
| 11 | `project_planning.shot_revisions` | 是 | `fk_shot_revisions_project_tenant` | 是 | 直接 ← fk_shot_revisions_project_tenant | 是 | true | 0 | 0 | PASS |
| 12 | `project_planning.shot_specs` | 是 | `fk_shot_specs_project_tenant` | 是 | 直接 ← fk_shot_specs_project_tenant | 是 | true | 0 | 0 | PASS |
| 13 | `project_planning.shots` | 是 | `fk_shots_project_tenant` | 是 | 直接 ← fk_shots_project_tenant | 是 | true | 0 | 0 | PASS |
| 14 | `qc_governance.gate_decisions` | 是 | `fk_gate_decisions_project_tenant` | 是 | 直接 ← fk_gate_decisions_project_tenant | 是 | true | 0 | 0 | PASS |
| 15 | `qc_governance.qc_tasks` | 是 | `fk_qc_tasks_project_tenant` | 是 | 直接 ← fk_qc_tasks_project_tenant | 是 | true | 0 | 0 | PASS |

## 二、「一致」半边：复合外键保证本行与父行同项目

| # | 子表 | 父表 | 约束 | 列映射 | validated | 悬空行 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `project_planning.scenes` | `project_planning.episodes` | `fk_scenes_episode` | `project_id=project_id, episode_id=entity_id` | true | 0 | PASS |
| 2 | `project_planning.script_revisions` | `project_planning.episodes` | `fk_script_revisions_episode` | `project_id=project_id, episode_ref=episode_ref` | true | 0 | PASS |
| 3 | `project_planning.shot_revisions` | `project_planning.shots` | `fk_shot_revisions_shot` | `project_id=project_id, shot_id=entity_id` | true | 0 | PASS |
| 4 | `project_planning.shot_specs` | `project_planning.shot_revisions` | `fk_shot_specs_revision` | `project_id=project_id, shot_revision_id=entity_id` | true | 0 | PASS |
| 5 | `project_planning.shots` | `project_planning.rework_tasks` | `fk_shots_rework_basis` | `project_id=project_id, rework_basis_ref=entity_id` | true | 0 | PASS |
| 6 | `project_planning.shots` | `project_planning.scenes` | `fk_shots_scene` | `project_id=project_id, scene_id=entity_id` | true | 0 | PASS |

## 三、可复算的复验命令

```sql
-- 租户一致性（以 episodes 为例，任一张含 tenant_id 的表同理）
SELECT count(*) FROM project_planning.episodes e
  JOIN project_planning.projects p ON p.entity_id = e.project_id
 WHERE e.tenant_id <> p.tenant_id;

-- 孤儿行（无任何归属路径的表才会 > 0）
SELECT count(*) FROM qc_governance.qc_tasks q
 WHERE NOT EXISTS (
   SELECT 1 FROM project_planning.projects p WHERE p.entity_id = q.project_id);
```

## 四、自检

`--selftest` 会插入一行「project_id 指向租户 A 的项目、tenant_id 写租户 B」的
episode，然后回滚。它必须使「租户不一致行数」≥ 1；否则说明本台账的判定是恒真的。
