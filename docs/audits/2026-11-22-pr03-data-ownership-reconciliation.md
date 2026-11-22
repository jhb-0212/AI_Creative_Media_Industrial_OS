# PR-03 数据所有权与迁移对账（只读核验）

> 依据：《开发执行总纲与 PR 验收规范 V1.0》第四章 PR-03
> 性质：只读对账；未执行真实 PostgreSQL 的项目一律标注 NOT RUN，不视为通过。

## 1. Schema 清单（实测迁移链）

| Schema | 用途 | 所有权 | 证据 |
| --- | --- | --- | --- |
| `project_planning` | Project/Episode/Script/Scene/Shot/ShotDraft/ShotRevision/ShotSpec/ConstraintPack/ReworkTask | project_planning 域 | 迁移 `8d8d59a91012` 起 |
| `asset_service` | AssetVersion / QCReport（append-only） | asset_service 域 | 迁移 + 14 表矩阵 |
| `outbox` | Outbox 事件分区 | relay / 平台 | 迁移 `7c8d9e0f1a2b` |
| `audit` | 状态迁移事实（status_transitions） | 平台 | ADR-R23 |
| `public` | alembic_version（就绪探针只读） | 平台 | 迁移 `8f3b2c1d4e5a` |

> 迁移 `8f3b2c1d4e5a_split_domain_roles.py` 注释自述「四个 schema 的权限」，域角色按 schema 写隔离。

## 2. 表所有权矩阵（关键表，非全量）

| 表 | 所有域 | 写角色 | 约束要点 |
| --- | --- | --- | --- |
| projects / episodes / scripts / scenes / shots | project_planning | project_planning_role | 复合归属（project_id 前缀）+ RLS |
| shot_breakdown_drafts / shot_revisions / shot_specs / constraint_packs / rework_tasks | project_planning | project_planning_role | 版本不可变 + 单 ACTIVE 唯一索引 |
| asset_versions / qc_reports | asset_service | asset_service_role | append-only + 版本不可覆盖 |
| outbox_events | 平台 | outbox_relay_role | 分区 + 保留期 |
| status_transitions | 平台 | app_role | 迁移事实不可变 |

## 3. ORM 对账

- `packages/shared/orm.py` 为共享 ORM 基座；各域 `repository/orm.py` 声明本域映射。
- 所有权模型与 14 表矩阵审计：`docs/audits/2026-11-20-ownership-model-and-14-table-matrix.md`（含 `information_schema`/`pg_constraint` 实测）。
- 结论：ORM 映射与迁移表集合一致；无第二套 ORM 事实源。

## 4. 正式迁移链

| 项 | 值 | 证据等级 |
| --- | --- | --- |
| 版本文件 | 38 个 | 实测 |
| head | `d7e8f9a0b1c2`（constraint_pack_runtime_tables） | 实测 |
| current | `d7e8f9a0b1c2` | 仓库电池 C6.1（未本机复跑 → NOT RUN） |
| 链唯一性 | 单 head（历史存在分支 `7a8b9c0d1e2f` down_revision=`9f1a2b3c4d5e6`，已合并收敛） | 实测文件 |
| 升级/降级验证 | **NOT RUN**（本地无 PostgreSQL） | — |

## 5. 数据库权限矩阵

| 角色 | 授权范围 | 证据 |
| --- | --- | --- |
| project_planning_role | 仅写 project_planning schema | `8f3b2c1d4e5a` |
| asset_service_role | 仅写 asset_service schema | `8f3b2c1d4e5a` |
| app_role | 两业务域 DQL + alembic_version 只读 | `8f3b2c1d4e5a` |
| outbox_relay_role | outbox 消费/发布 | `7a8b9c0d1e2f` |

## 6. 约束矩阵（关键不变量由数据库保证）

| 约束 | 表 | 类型 |
| --- | --- | --- |
| uq_constraint_pack_version | constraint_packs | 唯一 |
| uq_constraint_pack_single_active | constraint_packs | 部分唯一（同包至多一个 ACTIVE） |
| script/episode 复合外键 | scripts/episodes | 复合 FK（`6a7b8c9d0e1f`） |
| shot 复合归属 CHECK/FK | shots | 归属约束 |
| trg_constraint_packs_shots | constraint_pack↔shot | 触发器兜底 |
| RLS（tenant_id） | 全域业务表 | 行级安全 |

## 7. 数据迁移风险清单

| 级别 | 风险 | 状态 |
| --- | --- | --- |
| P1 | Shot/ReworkTask 复合归属约束负向写入未本机复跑 | 归 PR-06 真实 PG |
| P1 | 8 处跨域调用点（asset_service / project_planning → identity_access）未裁决 | 归 PR-04 |
| P2 | packages/shared → packages/identity 分层方向未裁决 | 归 PR-04 |
| P2 | 迁移升级/降级未本机验证 | NOT RUN |
| P0 | 未发现 | — |

## 8. 结论

数据所有权明确、迁移链唯一、域角色按 schema 最小授权——PR-03 的可读侧通过。真实 PostgreSQL 侧（迁移链重放、约束负向、降级）为本机环境所限标 NOT RUN，须在 PR-06 以真实 PG 补齐，不得以本报告替代。
