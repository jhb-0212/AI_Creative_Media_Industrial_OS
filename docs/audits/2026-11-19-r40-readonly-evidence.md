# ADR-R40 只读取证原始输出存档（E0~E12）

> 采集时间：2026-11-19 12:31:46 +0800 起，分四批追加（E0~E8 / E9 / E10 / E11）
> 性质：**纯只读 SELECT 查询**。未执行任何 INSERT/UPDATE/DELETE/DDL/GRANT/REVOKE/SET ROLE；未执行 upgrade/downgrade。
> 连接：本地 ai-film-postgres @ 15432，用户 postgres（读取 pg_catalog，非业务连接身份）。
> 用途：附录 C.2/C.3/C.5 的可追溯证据。每条命令与其原始输出一并留存。
> 说明：所有查询均为 SELECT；has_*_privilege 展示的是有效权限取证，负向行为测试须另用真实业务域连接在阶段 6 执行。
> **独立交付物**：本文件不依赖仓库其他文件即可复核（命令 + 原始输出成对留存，不需重跑即可核对结论）。
> 对应 ADR-R40 **v1.4.2**；第五轮复审判定「E0~E10 未随附件提供」后，本文件作为**独立交付物**提交。
>
> **E12 的完整原始输出**另存为机器可读文件 `docs/audits/2026-11-19-r40-e12-raw.txt`（1184 行，
> `sha256 = d8f57d1b…d7e79847`）。本文 E12 节给摘要 + 关键片段；如需逐行核对，用 raw 文件比对。

## 目录（按批次）

| 批次 | 小节 | 内容 | 对应 ADR 正文 |
| --- | --- | --- | --- |
| 第一批 | E0 | 环境、版本、非模板库列表 | §6.1 |
| 第一批 | E1 | 两库 `alembic_version` | C.2 第 5 列 |
| 第一批 | E2 | 角色属性 + 成员关系（含 PG16 `ADMIN/INHERIT/SET` + grantor） | §5.1 / INV-6b / INV-10 / INV-10b |
| 第一批 | E3 | Schema/Table/Sequence/Function/View/MV 的 OWNER | INV-1 / C.3.6 |
| 第一批 | E4 | RLS Policy 完整定义 + `relforcerowsecurity` | C.3.1 / C.3.2 |
| 第一批 | E5 | ACL / PUBLIC / `pg_default_acl` / `aclexplode` | C.3.3 / C.3.4 |
| 第一批 | E7 / E7b | 约束、触发器（含函数签名）、索引 | C.3.6 |
| 第一批 | E6 | 六个迁移文件 SHA256 + Git blob | C.1 / C.2 第 3/4 列 |
| 第二批 | E8 | 活跃连接 + 运行中程序引用 | §6.5 表B |
| 第三批 | E9 | 证据范围核定：集群级 vs 数据库级 catalog | §6.1.1 |
| 第三批 | E10.0~E10.11 | 第四轮补充取证（成员边三选项、nspowner、逐对象 OWNER、函数签名+SECDEF+proconfig、ACL 展开、有效权限矩阵、约束/触发器/索引、Git 固定 commit、列类型） | §5.3 / C.5 / D.5 |
| **第四批** | **E11.0~E11.7** | **GUC 数组语义与真实键类型**（解析正确性、`project_id` 真实类型、`uuid[]` 失败现场、`text[]` 边界、真实表 0 行 vs 684 行对比、分区 `relkind` 缺口、outbox/audit 列结构） | **§4.2 / D.2.2 / D.6.0c / C.5.3 ⑨** |
| **第五批** | **E12.0~E12.10** | **补齐查询的首次实跑**：基础设施角色属性（`role_exists`）、INV-10b 合取式判定（PASS/FAIL）、原始违规边、递归成员闭包、⑨ 两库、⑬ 分区父子 ACL（含 collation 缺陷修复）、⑭ 全对象类型有效权限、⑮ 零权限补集矩阵、两库漂移精确值 | **§5.3(INV) / C.5.2 / C.5.3 ⑨⑬⑭⑮ / §7.0.4** |

> **阅读提示**：两份输出最关键——**E11.4**：同一上下文下 v1.4.1 表达式返回 **0 行**、v1.4.2 返回 **684 行**；
> **E12.1/E12.2**：三个基础设施角色 `role_exists=f`，同一条不变量在原始查询下返回 0 行（像通过）、
> 在合取式判定下返回 **FAIL**。前者证明「解析必须对」，后者证明「0 行不等于合规」。

## E0. 环境与非业务库确认
\$ psql --version
psql (PostgreSQL) 16.15
\$ SELECT current_database(), version();
 current_database |                                                       version                                                        
------------------+----------------------------------------------------------------------------------------------------------------------
 postgres         | PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit
(1 row)

\$ 非模板数据库列表（postgres 系统库存在但不参与业务对账）
      datname       | datistemplate | datallowconn 
--------------------+---------------+--------------
 ai_film_industrial | f             | t
 ai_film_verify     | f             | t
 postgres           | f             | t
(3 rows)


## E1. 两个业务库各自的 Alembic Revision
\$ [ai_film_verify] SELECT version_num FROM alembic_version;
 version_num  
--------------
 b3c4d5e6f7a8
(1 row)

\$ [ai_film_industrial] SELECT version_num FROM alembic_version;
 version_num  
--------------
 c1a2b3d4e5f6
(1 row)


## E2. 角色完整属性 + 成员关系（含 INHERIT/SET/ADMIN OPTION，PG16）
### [ai_film_verify] 角色属性
\$ SELECT rolname,rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE ...;
         rolname          | rolcanlogin | rolsuper | rolbypassrls | rolcreatedb | rolcreaterole | rolinherit 
--------------------------+-------------+----------+--------------+-------------+---------------+------------
 app_role                 | t           | f        | f            | f           | f             | t
 asset_service_role       | t           | f        | f            | f           | f             | t
 outbox_relay_role        | t           | f        | f            | f           | f             | t
 postgres                 | t           | t        | t            | t           | t             | t
 project_planning_role    | t           | f        | f            | f           | f             | t
 svc_asset                | t           | f        | f            | f           | f             | t
 svc_generation_execution | t           | f        | f            | f           | f             | t
 svc_identity             | t           | f        | f            | f           | f             | t
 svc_outbox_relay         | t           | f        | f            | f           | f             | t
 svc_project_planning     | t           | f        | f            | f           | f             | t
 svc_qc_governance        | t           | f        | f            | f           | f             | t
 svc_shot_planning        | t           | f        | f            | f           | f             | t
 svc_workflow             | t           | f        | f            | f           | f             | t
(13 rows)

\$ pg_auth_members（group/member/grantor + admin/inherit/set option）
      group_role      | member_role | admin_option | inherit_option | set_option | grantor  
----------------------+-------------+--------------+----------------+------------+----------
 pg_read_all_settings | pg_monitor  | f            | t              | t          | postgres
 pg_read_all_stats    | pg_monitor  | f            | t              | t          | postgres
 pg_stat_scan_tables  | pg_monitor  | f            | t              | t          | postgres
(3 rows)

### [ai_film_industrial] 角色属性
\$ SELECT rolname,rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE ...;
         rolname          | rolcanlogin | rolsuper | rolbypassrls | rolcreatedb | rolcreaterole | rolinherit 
--------------------------+-------------+----------+--------------+-------------+---------------+------------
 app_role                 | t           | f        | f            | f           | f             | t
 asset_service_role       | t           | f        | f            | f           | f             | t
 outbox_relay_role        | t           | f        | f            | f           | f             | t
 postgres                 | t           | t        | t            | t           | t             | t
 project_planning_role    | t           | f        | f            | f           | f             | t
 svc_asset                | t           | f        | f            | f           | f             | t
 svc_generation_execution | t           | f        | f            | f           | f             | t
 svc_identity             | t           | f        | f            | f           | f             | t
 svc_outbox_relay         | t           | f        | f            | f           | f             | t
 svc_project_planning     | t           | f        | f            | f           | f             | t
 svc_qc_governance        | t           | f        | f            | f           | f             | t
 svc_shot_planning        | t           | f        | f            | f           | f             | t
 svc_workflow             | t           | f        | f            | f           | f             | t
(13 rows)

\$ pg_auth_members（group/member/grantor + admin/inherit/set option）
      group_role      | member_role | admin_option | inherit_option | set_option | grantor  
----------------------+-------------+--------------+----------------+------------+----------
 pg_read_all_settings | pg_monitor  | f            | t              | t          | postgres
 pg_read_all_stats    | pg_monitor  | f            | t              | t          | postgres
 pg_stat_scan_tables  | pg_monitor  | f            | t              | t          | postgres
(3 rows)


## E3. 所有对象 OWNER：Schema / Table / Sequence / Function / View / MV
### [ai_film_verify] Schema owner
     nspname      |       owner       
------------------+-------------------
 asset_service    | postgres
 auth             | postgres
 outbox           | postgres
 project_planning | postgres
 public           | pg_database_owner
(5 rows)

\$ 业务对象 owner by type
      objtype      |     nspname      |  owner   | count 
-------------------+------------------+----------+-------
 partitioned_table | outbox           | postgres |     1
 sequence          | outbox           | postgres |     1
 table             | asset_service    | postgres |     2
 table             | auth             | postgres |     1
 table             | outbox           | postgres |     4
 table             | project_planning | postgres |     4
 table             | public           | postgres |     1
(7 rows)

\$ Function owner + prosecdef（SECURITY DEFINER 清单）
    nspname    |       proname        |  owner   | security_definer | provolatile | args 
---------------+----------------------+----------+------------------+-------------+------
 asset_service | asset_versions_guard | postgres | f                | v           | 
 asset_service | qc_reports_guard     | postgres | f                | v           | 
 outbox        | list_active_tenants  | postgres | t                | s           | 
(3 rows)

### [ai_film_industrial] Schema owner
     nspname      |       owner       
------------------+-------------------
 asset_service    | postgres
 audit            | postgres
 auth             | postgres
 outbox           | postgres
 project_planning | postgres
 public           | pg_database_owner
(6 rows)

\$ 业务对象 owner by type
      objtype      |     nspname      |  owner   | count 
-------------------+------------------+----------+-------
 partitioned_table | outbox           | postgres |     1
 sequence          | outbox           | postgres |     1
 table             | asset_service    | postgres |     2
 table             | audit            | postgres |     1
 table             | auth             | postgres |     1
 table             | outbox           | postgres |     4
 table             | project_planning | postgres |     4
 table             | public           | postgres |     1
(8 rows)

\$ Function owner + prosecdef（SECURITY DEFINER 清单）
    nspname    |            proname             |  owner   | security_definer | provolatile | args 
---------------+--------------------------------+----------+------------------+-------------+------
 asset_service | asset_versions_guard           | postgres | f                | v           | 
 asset_service | qc_reports_guard               | postgres | f                | v           | 
 audit         | assert_transition_event_exists | postgres | t                | v           | 
 audit         | reject_mutation                | postgres | f                | v           | 
 audit         | reject_truncate                | postgres | f                | v           | 
 outbox        | list_active_tenants            | postgres | t                | s           | 
(6 rows)


## E4. RLS Policy 完整定义（polname/roles/cmd/permissive/USING/WITH CHECK + relforcerowsecurity）
### [ai_film_verify] Policy 逐条定义
    schemaname    |       tablename       |            policyname             | roles  | cmd | permissive |                                          using_expr                                          | with_check 
------------------+-----------------------+-----------------------------------+--------+-----+------------+----------------------------------------------------------------------------------------------+------------
 asset_service    | asset_versions        | tenant_isolation_asset_versions   | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) | 
 asset_service    | qc_reports            | tenant_isolation_qc_reports       | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) | 
 outbox           | outbox_events         | tenant_isolation_outbox_events    | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) | 
 outbox           | outbox_events_default | tenant_isolation_outbox_events    | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) | 
 outbox           | outbox_events_y202611 | tenant_isolation_outbox_events    | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) | 
 outbox           | outbox_events_y202612 | tenant_isolation_outbox_events    | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) | 
 project_planning | episodes              | tenant_isolation_episodes         | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) | 
 project_planning | organizations         | tenant_isolation_organizations    | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) | 
 project_planning | projects              | tenant_isolation_projects         | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) | 
 project_planning | script_revisions      | tenant_isolation_script_revisions | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) | 
(10 rows)

\$ 表 RLS 开关与 FORCE
     nspname      |        relname        | rls_enabled | rls_forced 
------------------+-----------------------+-------------+------------
 asset_service    | asset_versions        | t           | f
 asset_service    | qc_reports            | t           | f
 outbox           | outbox_events         | t           | f
 outbox           | outbox_events_default | t           | f
 outbox           | outbox_events_y202611 | t           | f
 outbox           | outbox_events_y202612 | t           | f
 project_planning | episodes              | t           | f
 project_planning | organizations         | t           | f
 project_planning | projects              | t           | f
 project_planning | script_revisions      | t           | f
(10 rows)

### [ai_film_industrial] Policy 逐条定义
    schemaname    |       tablename       |             policyname              | roles  | cmd | permissive |                                                                                                                 using_expr                                                                                                                 | with_check 
------------------+-----------------------+-------------------------------------+--------+-----+------------+--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------+------------
 asset_service    | asset_versions        | tenant_isolation_asset_versions     | public | ALL | PERMISSIVE | ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND (EXISTS ( SELECT 1                                                                                                                      +| 
                  |                       |                                     |        |     |            |    FROM project_planning.projects p                                                                                                                                                                                                       +| 
                  |                       |                                     |        |     |            |   WHERE (((p.entity_id)::text = (asset_versions.project_id)::text) AND (p.tenant_id = asset_versions.tenant_id)))))                                                                                                                        | 
 asset_service    | qc_reports            | tenant_isolation_qc_reports         | public | ALL | PERMISSIVE | ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND (EXISTS ( SELECT 1                                                                                                                      +| 
                  |                       |                                     |        |     |            |    FROM (asset_service.asset_versions a                                                                                                                                                                                                   +| 
                  |                       |                                     |        |     |            |      JOIN project_planning.projects p ON ((((p.entity_id)::text = (a.project_id)::text) AND (p.tenant_id = a.tenant_id))))                                                                                                                +| 
                  |                       |                                     |        |     |            |   WHERE (((a.entity_id)::text = (qc_reports.asset_version_id)::text) AND (a.tenant_id = qc_reports.tenant_id)))))                                                                                                                          | 
 audit            | status_transitions    | tenant_isolation_status_transitions | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid)                                                                                                                                               | 
 outbox           | outbox_events         | tenant_isolation_outbox_events      | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid)                                                                                                                                               | 
 outbox           | outbox_events_default | tenant_isolation_outbox_events      | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid)                                                                                                                                               | 
 outbox           | outbox_events_y202611 | tenant_isolation_outbox_events      | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid)                                                                                                                                               | 
 outbox           | outbox_events_y202612 | tenant_isolation_outbox_events      | public | ALL | PERMISSIVE | (tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid)                                                                                                                                               | 
 project_planning | episodes              | tenant_isolation_episodes           | public | ALL | PERMISSIVE | ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND (EXISTS ( SELECT 1                                                                                                                      +| 
                  |                       |                                     |        |     |            |    FROM project_planning.projects p                                                                                                                                                                                                       +| 
                  |                       |                                     |        |     |            |   WHERE (((p.entity_id)::text = (episodes.project_id)::text) AND (p.tenant_id = episodes.tenant_id)))))                                                                                                                                    | 
 project_planning | organizations         | tenant_isolation_organizations      | public | ALL | PERMISSIVE | ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND ((organization_id IS NULL) OR (organization_id = (NULLIF(current_setting('app.current_organization_id'::text, true), ''::text))::uuid))) | 
 project_planning | projects              | tenant_isolation_projects           | public | ALL | PERMISSIVE | ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND ((organization_id IS NULL) OR (organization_id = (NULLIF(current_setting('app.current_organization_id'::text, true), ''::text))::uuid))) | 
 project_planning | script_revisions      | tenant_isolation_script_revisions   | public | ALL | PERMISSIVE | ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND (EXISTS ( SELECT 1                                                                                                                      +| 
                  |                       |                                     |        |     |            |    FROM project_planning.projects p                                                                                                                                                                                                       +| 
                  |                       |                                     |        |     |            |   WHERE (((p.entity_id)::text = (script_revisions.project_id)::text) AND (p.tenant_id = script_revisions.tenant_id)))))                                                                                                                    | 
(11 rows)

\$ 表 RLS 开关与 FORCE
     nspname      |        relname        | rls_enabled | rls_forced 
------------------+-----------------------+-------------+------------
 asset_service    | asset_versions        | t           | t
 asset_service    | qc_reports            | t           | t
 audit            | status_transitions    | t           | t
 outbox           | outbox_events         | t           | f
 outbox           | outbox_events_default | t           | f
 outbox           | outbox_events_y202611 | t           | f
 outbox           | outbox_events_y202612 | t           | f
 project_planning | episodes              | t           | t
 project_planning | organizations         | t           | t
 project_planning | projects              | t           | t
 project_planning | script_revisions      | t           | t
(11 rows)


## E5. ACL / PUBLIC / 默认权限（pg_default_acl 只证明配置；aclexplode 展开实际对象 ACL）
### [ai_film_verify] 默认权限 pg_default_acl
     nspname      | defaclrole | defaclobjtype |                                             defaclacl                                             
------------------+------------+---------------+---------------------------------------------------------------------------------------------------
 asset_service    | postgres   | S             | asset_service_role=rU/postgres
 asset_service    | postgres   | r             | asset_service_role=arwd/postgres
 outbox           | postgres   | r             | outbox_relay_role=r/postgres | project_planning_role=ar/postgres | asset_service_role=ar/postgres
 project_planning | postgres   | S             | project_planning_role=rU/postgres
 project_planning | postgres   | r             | project_planning_role=arwd/postgres
(5 rows)

\$ 业务表实际 ACL 展开 aclexplode（grantee=0 显示 PUBLIC；acldefault 用 ::\"char\" 适配 PG16）
     nspname      |        relname        |        grantee        | privilege_type 
------------------+-----------------------+-----------------------+----------------
 asset_service    | asset_versions        | asset_service_role    | INSERT
 asset_service    | asset_versions        | asset_service_role    | SELECT
 asset_service    | asset_versions        | asset_service_role    | UPDATE
 asset_service    | asset_versions        | postgres              | DELETE
 asset_service    | asset_versions        | postgres              | INSERT
 asset_service    | asset_versions        | postgres              | REFERENCES
 asset_service    | asset_versions        | postgres              | SELECT
 asset_service    | asset_versions        | postgres              | TRIGGER
 asset_service    | asset_versions        | postgres              | TRUNCATE
 asset_service    | asset_versions        | postgres              | UPDATE
 asset_service    | qc_reports            | asset_service_role    | INSERT
 asset_service    | qc_reports            | asset_service_role    | SELECT
 asset_service    | qc_reports            | postgres              | DELETE
 asset_service    | qc_reports            | postgres              | INSERT
 asset_service    | qc_reports            | postgres              | REFERENCES
 asset_service    | qc_reports            | postgres              | SELECT
 asset_service    | qc_reports            | postgres              | TRIGGER
 asset_service    | qc_reports            | postgres              | TRUNCATE
 asset_service    | qc_reports            | postgres              | UPDATE
 auth             | revoked_tokens        | asset_service_role    | SELECT
 auth             | revoked_tokens        | postgres              | DELETE
 auth             | revoked_tokens        | postgres              | INSERT
 auth             | revoked_tokens        | postgres              | REFERENCES
 auth             | revoked_tokens        | postgres              | SELECT
 auth             | revoked_tokens        | postgres              | TRIGGER
 auth             | revoked_tokens        | postgres              | TRUNCATE
 auth             | revoked_tokens        | postgres              | UPDATE
 auth             | revoked_tokens        | project_planning_role | SELECT
 outbox           | maintenance_runs      | postgres              | DELETE
 outbox           | maintenance_runs      | postgres              | INSERT
 outbox           | maintenance_runs      | postgres              | REFERENCES
 outbox           | maintenance_runs      | postgres              | SELECT
 outbox           | maintenance_runs      | postgres              | TRIGGER
 outbox           | maintenance_runs      | postgres              | TRUNCATE
 outbox           | maintenance_runs      | postgres              | UPDATE
 outbox           | outbox_events         | app_role              | SELECT
 outbox           | outbox_events         | asset_service_role    | INSERT
 outbox           | outbox_events         | asset_service_role    | SELECT
 outbox           | outbox_events         | outbox_relay_role     | SELECT
 outbox           | outbox_events         | postgres              | DELETE
 outbox           | outbox_events         | postgres              | INSERT
 outbox           | outbox_events         | postgres              | REFERENCES
 outbox           | outbox_events         | postgres              | SELECT
 outbox           | outbox_events         | postgres              | TRIGGER
 outbox           | outbox_events         | postgres              | TRUNCATE
 outbox           | outbox_events         | postgres              | UPDATE
 outbox           | outbox_events         | project_planning_role | INSERT
 outbox           | outbox_events         | project_planning_role | SELECT
 outbox           | outbox_events_default | asset_service_role    | INSERT
 outbox           | outbox_events_default | asset_service_role    | SELECT
 outbox           | outbox_events_default | outbox_relay_role     | SELECT
 outbox           | outbox_events_default | postgres              | DELETE
 outbox           | outbox_events_default | postgres              | INSERT
 outbox           | outbox_events_default | postgres              | REFERENCES
 outbox           | outbox_events_default | postgres              | SELECT
 outbox           | outbox_events_default | postgres              | TRIGGER
 outbox           | outbox_events_default | postgres              | TRUNCATE
 outbox           | outbox_events_default | postgres              | UPDATE
 outbox           | outbox_events_default | project_planning_role | INSERT
 outbox           | outbox_events_default | project_planning_role | SELECT
 outbox           | outbox_events_y202611 | asset_service_role    | INSERT
 outbox           | outbox_events_y202611 | asset_service_role    | SELECT
 outbox           | outbox_events_y202611 | outbox_relay_role     | SELECT
 outbox           | outbox_events_y202611 | postgres              | DELETE
 outbox           | outbox_events_y202611 | postgres              | INSERT
 outbox           | outbox_events_y202611 | postgres              | REFERENCES
 outbox           | outbox_events_y202611 | postgres              | SELECT
 outbox           | outbox_events_y202611 | postgres              | TRIGGER
 outbox           | outbox_events_y202611 | postgres              | TRUNCATE
 outbox           | outbox_events_y202611 | postgres              | UPDATE
 outbox           | outbox_events_y202611 | project_planning_role | INSERT
 outbox           | outbox_events_y202611 | project_planning_role | SELECT
 outbox           | outbox_events_y202612 | asset_service_role    | INSERT
 outbox           | outbox_events_y202612 | asset_service_role    | SELECT
 outbox           | outbox_events_y202612 | outbox_relay_role     | SELECT
 outbox           | outbox_events_y202612 | postgres              | DELETE
 outbox           | outbox_events_y202612 | postgres              | INSERT
 outbox           | outbox_events_y202612 | postgres              | REFERENCES
 outbox           | outbox_events_y202612 | postgres              | SELECT
 outbox           | outbox_events_y202612 | postgres              | TRIGGER
 outbox           | outbox_events_y202612 | postgres              | TRUNCATE
 outbox           | outbox_events_y202612 | postgres              | UPDATE
 outbox           | outbox_events_y202612 | project_planning_role | INSERT
 outbox           | outbox_events_y202612 | project_planning_role | SELECT
 project_planning | episodes              | postgres              | DELETE
 project_planning | episodes              | postgres              | INSERT
 project_planning | episodes              | postgres              | REFERENCES
 project_planning | episodes              | postgres              | SELECT
 project_planning | episodes              | postgres              | TRIGGER
 project_planning | episodes              | postgres              | TRUNCATE
 project_planning | episodes              | postgres              | UPDATE
 project_planning | episodes              | project_planning_role | DELETE
 project_planning | episodes              | project_planning_role | INSERT
 project_planning | episodes              | project_planning_role | SELECT
 project_planning | episodes              | project_planning_role | UPDATE
 project_planning | organizations         | postgres              | DELETE
 project_planning | organizations         | postgres              | INSERT
 project_planning | organizations         | postgres              | REFERENCES
 project_planning | organizations         | postgres              | SELECT
 project_planning | organizations         | postgres              | TRIGGER
 project_planning | organizations         | postgres              | TRUNCATE
 project_planning | organizations         | postgres              | UPDATE
 project_planning | organizations         | project_planning_role | DELETE
 project_planning | organizations         | project_planning_role | INSERT
 project_planning | organizations         | project_planning_role | SELECT
 project_planning | organizations         | project_planning_role | UPDATE
 project_planning | projects              | postgres              | DELETE
 project_planning | projects              | postgres              | INSERT
 project_planning | projects              | postgres              | REFERENCES
 project_planning | projects              | postgres              | SELECT
 project_planning | projects              | postgres              | TRIGGER
 project_planning | projects              | postgres              | TRUNCATE
 project_planning | projects              | postgres              | UPDATE
 project_planning | projects              | project_planning_role | DELETE
 project_planning | projects              | project_planning_role | INSERT
 project_planning | projects              | project_planning_role | SELECT
 project_planning | projects              | project_planning_role | UPDATE
 project_planning | script_revisions      | postgres              | DELETE
 project_planning | script_revisions      | postgres              | INSERT
 project_planning | script_revisions      | postgres              | REFERENCES
 project_planning | script_revisions      | postgres              | SELECT
 project_planning | script_revisions      | postgres              | TRIGGER
 project_planning | script_revisions      | postgres              | TRUNCATE
 project_planning | script_revisions      | postgres              | UPDATE
 project_planning | script_revisions      | project_planning_role | DELETE
 project_planning | script_revisions      | project_planning_role | INSERT
 project_planning | script_revisions      | project_planning_role | SELECT
 project_planning | script_revisions      | project_planning_role | UPDATE
 public           | alembic_version       | asset_service_role    | SELECT
 public           | alembic_version       | postgres              | DELETE
 public           | alembic_version       | postgres              | INSERT
 public           | alembic_version       | postgres              | REFERENCES
 public           | alembic_version       | postgres              | SELECT
 public           | alembic_version       | postgres              | TRIGGER
 public           | alembic_version       | postgres              | TRUNCATE
 public           | alembic_version       | postgres              | UPDATE
 public           | alembic_version       | project_planning_role | SELECT
(137 rows)

\$ 有效权限 has_*_privilege（业务域角色对 asset_service.asset_versions；含 TRUNCATE）
         rolname          | sel | ins | upd | del | trunc 
--------------------------+-----+-----+-----+-----+-------
 app_role                 | f   | f   | f   | f   | f
 asset_service_role       | t   | t   | t   | f   | f
 outbox_relay_role        | f   | f   | f   | f   | f
 project_planning_role    | f   | f   | f   | f   | f
 svc_asset                | f   | f   | f   | f   | f
 svc_generation_execution | f   | f   | f   | f   | f
 svc_identity             | f   | f   | f   | f   | f
 svc_outbox_relay         | f   | f   | f   | f   | f
 svc_project_planning     | f   | f   | f   | f   | f
 svc_qc_governance        | f   | f   | f   | f   | f
 svc_shot_planning        | f   | f   | f   | f   | f
 svc_workflow             | f   | f   | f   | f   | f
(12 rows)

### [ai_film_industrial] 默认权限 pg_default_acl
 nspname | defaclrole | defaclobjtype |                                             defaclacl                                             
---------+------------+---------------+---------------------------------------------------------------------------------------------------
 outbox  | postgres   | r             | outbox_relay_role=r/postgres | project_planning_role=ar/postgres | asset_service_role=ar/postgres
(1 row)

\$ 业务表实际 ACL 展开 aclexplode（grantee=0 显示 PUBLIC；acldefault 用 ::\"char\" 适配 PG16）
     nspname      |        relname        |        grantee        | privilege_type 
------------------+-----------------------+-----------------------+----------------
 asset_service    | asset_versions        | asset_service_role    | INSERT
 asset_service    | asset_versions        | asset_service_role    | SELECT
 asset_service    | asset_versions        | asset_service_role    | UPDATE
 asset_service    | asset_versions        | postgres              | DELETE
 asset_service    | asset_versions        | postgres              | INSERT
 asset_service    | asset_versions        | postgres              | REFERENCES
 asset_service    | asset_versions        | postgres              | SELECT
 asset_service    | asset_versions        | postgres              | TRIGGER
 asset_service    | asset_versions        | postgres              | TRUNCATE
 asset_service    | asset_versions        | postgres              | UPDATE
 asset_service    | qc_reports            | asset_service_role    | INSERT
 asset_service    | qc_reports            | asset_service_role    | SELECT
 asset_service    | qc_reports            | postgres              | DELETE
 asset_service    | qc_reports            | postgres              | INSERT
 asset_service    | qc_reports            | postgres              | REFERENCES
 asset_service    | qc_reports            | postgres              | SELECT
 asset_service    | qc_reports            | postgres              | TRIGGER
 asset_service    | qc_reports            | postgres              | TRUNCATE
 asset_service    | qc_reports            | postgres              | UPDATE
 audit            | status_transitions    | asset_service_role    | INSERT
 audit            | status_transitions    | asset_service_role    | SELECT
 audit            | status_transitions    | postgres              | DELETE
 audit            | status_transitions    | postgres              | INSERT
 audit            | status_transitions    | postgres              | REFERENCES
 audit            | status_transitions    | postgres              | SELECT
 audit            | status_transitions    | postgres              | TRIGGER
 audit            | status_transitions    | postgres              | TRUNCATE
 audit            | status_transitions    | postgres              | UPDATE
 audit            | status_transitions    | project_planning_role | INSERT
 audit            | status_transitions    | project_planning_role | SELECT
 auth             | revoked_tokens        | asset_service_role    | SELECT
 auth             | revoked_tokens        | postgres              | DELETE
 auth             | revoked_tokens        | postgres              | INSERT
 auth             | revoked_tokens        | postgres              | REFERENCES
 auth             | revoked_tokens        | postgres              | SELECT
 auth             | revoked_tokens        | postgres              | TRIGGER
 auth             | revoked_tokens        | postgres              | TRUNCATE
 auth             | revoked_tokens        | postgres              | UPDATE
 auth             | revoked_tokens        | project_planning_role | SELECT
 outbox           | maintenance_runs      | postgres              | DELETE
 outbox           | maintenance_runs      | postgres              | INSERT
 outbox           | maintenance_runs      | postgres              | REFERENCES
 outbox           | maintenance_runs      | postgres              | SELECT
 outbox           | maintenance_runs      | postgres              | TRIGGER
 outbox           | maintenance_runs      | postgres              | TRUNCATE
 outbox           | maintenance_runs      | postgres              | UPDATE
 outbox           | outbox_events         | app_role              | SELECT
 outbox           | outbox_events         | asset_service_role    | INSERT
 outbox           | outbox_events         | asset_service_role    | SELECT
 outbox           | outbox_events         | outbox_relay_role     | SELECT
 outbox           | outbox_events         | postgres              | DELETE
 outbox           | outbox_events         | postgres              | INSERT
 outbox           | outbox_events         | postgres              | REFERENCES
 outbox           | outbox_events         | postgres              | SELECT
 outbox           | outbox_events         | postgres              | TRIGGER
 outbox           | outbox_events         | postgres              | TRUNCATE
 outbox           | outbox_events         | postgres              | UPDATE
 outbox           | outbox_events         | project_planning_role | INSERT
 outbox           | outbox_events         | project_planning_role | SELECT
 outbox           | outbox_events_default | asset_service_role    | INSERT
 outbox           | outbox_events_default | asset_service_role    | SELECT
 outbox           | outbox_events_default | outbox_relay_role     | SELECT
 outbox           | outbox_events_default | postgres              | DELETE
 outbox           | outbox_events_default | postgres              | INSERT
 outbox           | outbox_events_default | postgres              | REFERENCES
 outbox           | outbox_events_default | postgres              | SELECT
 outbox           | outbox_events_default | postgres              | TRIGGER
 outbox           | outbox_events_default | postgres              | TRUNCATE
 outbox           | outbox_events_default | postgres              | UPDATE
 outbox           | outbox_events_default | project_planning_role | INSERT
 outbox           | outbox_events_default | project_planning_role | SELECT
 outbox           | outbox_events_y202611 | asset_service_role    | INSERT
 outbox           | outbox_events_y202611 | asset_service_role    | SELECT
 outbox           | outbox_events_y202611 | outbox_relay_role     | SELECT
 outbox           | outbox_events_y202611 | postgres              | DELETE
 outbox           | outbox_events_y202611 | postgres              | INSERT
 outbox           | outbox_events_y202611 | postgres              | REFERENCES
 outbox           | outbox_events_y202611 | postgres              | SELECT
 outbox           | outbox_events_y202611 | postgres              | TRIGGER
 outbox           | outbox_events_y202611 | postgres              | TRUNCATE
 outbox           | outbox_events_y202611 | postgres              | UPDATE
 outbox           | outbox_events_y202611 | project_planning_role | INSERT
 outbox           | outbox_events_y202611 | project_planning_role | SELECT
 outbox           | outbox_events_y202612 | asset_service_role    | INSERT
 outbox           | outbox_events_y202612 | asset_service_role    | SELECT
 outbox           | outbox_events_y202612 | outbox_relay_role     | SELECT
 outbox           | outbox_events_y202612 | postgres              | DELETE
 outbox           | outbox_events_y202612 | postgres              | INSERT
 outbox           | outbox_events_y202612 | postgres              | REFERENCES
 outbox           | outbox_events_y202612 | postgres              | SELECT
 outbox           | outbox_events_y202612 | postgres              | TRIGGER
 outbox           | outbox_events_y202612 | postgres              | TRUNCATE
 outbox           | outbox_events_y202612 | postgres              | UPDATE
 outbox           | outbox_events_y202612 | project_planning_role | INSERT
 outbox           | outbox_events_y202612 | project_planning_role | SELECT
 project_planning | episodes              | postgres              | DELETE
 project_planning | episodes              | postgres              | INSERT
 project_planning | episodes              | postgres              | REFERENCES
 project_planning | episodes              | postgres              | SELECT
 project_planning | episodes              | postgres              | TRIGGER
 project_planning | episodes              | postgres              | TRUNCATE
 project_planning | episodes              | postgres              | UPDATE
 project_planning | episodes              | project_planning_role | INSERT
 project_planning | episodes              | project_planning_role | SELECT
 project_planning | episodes              | project_planning_role | UPDATE
 project_planning | organizations         | postgres              | DELETE
 project_planning | organizations         | postgres              | INSERT
 project_planning | organizations         | postgres              | REFERENCES
 project_planning | organizations         | postgres              | SELECT
 project_planning | organizations         | postgres              | TRIGGER
 project_planning | organizations         | postgres              | TRUNCATE
 project_planning | organizations         | postgres              | UPDATE
 project_planning | organizations         | project_planning_role | SELECT
 project_planning | projects              | postgres              | DELETE
 project_planning | projects              | postgres              | INSERT
 project_planning | projects              | postgres              | REFERENCES
 project_planning | projects              | postgres              | SELECT
 project_planning | projects              | postgres              | TRIGGER
 project_planning | projects              | postgres              | TRUNCATE
 project_planning | projects              | postgres              | UPDATE
 project_planning | projects              | project_planning_role | INSERT
 project_planning | projects              | project_planning_role | SELECT
 project_planning | projects              | project_planning_role | UPDATE
 project_planning | script_revisions      | postgres              | DELETE
 project_planning | script_revisions      | postgres              | INSERT
 project_planning | script_revisions      | postgres              | REFERENCES
 project_planning | script_revisions      | postgres              | SELECT
 project_planning | script_revisions      | postgres              | TRIGGER
 project_planning | script_revisions      | postgres              | TRUNCATE
 project_planning | script_revisions      | postgres              | UPDATE
 project_planning | script_revisions      | project_planning_role | INSERT
 project_planning | script_revisions      | project_planning_role | SELECT
 project_planning | script_revisions      | project_planning_role | UPDATE
 public           | alembic_version       | asset_service_role    | SELECT
 public           | alembic_version       | postgres              | DELETE
 public           | alembic_version       | postgres              | INSERT
 public           | alembic_version       | postgres              | REFERENCES
 public           | alembic_version       | postgres              | SELECT
 public           | alembic_version       | postgres              | TRIGGER
 public           | alembic_version       | postgres              | TRUNCATE
 public           | alembic_version       | postgres              | UPDATE
 public           | alembic_version       | project_planning_role | SELECT
(142 rows)

\$ 有效权限 has_*_privilege（业务域角色对 asset_service.asset_versions；含 TRUNCATE）
         rolname          | sel | ins | upd | del | trunc 
--------------------------+-----+-----+-----+-----+-------
 app_role                 | f   | f   | f   | f   | f
 asset_service_role       | t   | t   | t   | f   | f
 outbox_relay_role        | f   | f   | f   | f   | f
 project_planning_role    | f   | f   | f   | f   | f
 svc_asset                | f   | f   | f   | f   | f
 svc_generation_execution | f   | f   | f   | f   | f
 svc_identity             | f   | f   | f   | f   | f
 svc_outbox_relay         | f   | f   | f   | f   | f
 svc_project_planning     | f   | f   | f   | f   | f
 svc_qc_governance        | f   | f   | f   | f   | f
 svc_shot_planning        | f   | f   | f   | f   | f
 svc_workflow             | f   | f   | f   | f   | f
(12 rows)


## E7. 状态转移/约束/触发器/索引精确对象名（C.3 可计算验收所需）
### [ai_film_verify] 约束 pg_constraint
     nspname      |             conname              | contype |                tbl                |                                                      def                                                       
------------------+----------------------------------+---------+-----------------------------------+----------------------------------------------------------------------------------------------------------------
 asset_service    | asset_versions_pkey              | p       | asset_service.asset_versions      | PRIMARY KEY (entity_id)
 asset_service    | fk_asset_versions_project_tenant | f       | asset_service.asset_versions      | FOREIGN KEY (project_id, tenant_id) REFERENCES project_planning.projects(entity_id, tenant_id)
 asset_service    | qc_reports_asset_version_id_fkey | f       | asset_service.qc_reports          | FOREIGN KEY (asset_version_id) REFERENCES asset_service.asset_versions(entity_id)
 asset_service    | qc_reports_pkey                  | p       | asset_service.qc_reports          | PRIMARY KEY (qc_report_id)
 outbox           | maintenance_runs_pkey            | p       | outbox.maintenance_runs           | PRIMARY KEY (id)
 outbox           | outbox_events_default_pkey       | p       | outbox.outbox_events_default      | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_pkey               | p       | outbox.outbox_events              | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_y202611_pkey       | p       | outbox.outbox_events_y202611      | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_y202612_pkey       | p       | outbox.outbox_events_y202612      | PRIMARY KEY (event_id, occurred_at)
 project_planning | episodes_pkey                    | p       | project_planning.episodes         | PRIMARY KEY (entity_id)
 project_planning | fk_episodes_project              | f       | project_planning.episodes         | FOREIGN KEY (project_id) REFERENCES project_planning.projects(entity_id)
 project_planning | fk_projects_organization_tenant  | f       | project_planning.projects         | FOREIGN KEY (organization_id, tenant_id) REFERENCES project_planning.organizations(organization_id, tenant_id)
 project_planning | fk_script_revisions_episode      | f       | project_planning.script_revisions | FOREIGN KEY (project_id, episode_ref) REFERENCES project_planning.episodes(project_id, episode_ref)
 project_planning | organizations_pkey               | p       | project_planning.organizations    | PRIMARY KEY (organization_id)
 project_planning | projects_pkey                    | p       | project_planning.projects         | PRIMARY KEY (entity_id)
 project_planning | script_revisions_pkey            | p       | project_planning.script_revisions | PRIMARY KEY (entity_id)
 project_planning | uq_organizations_owner_tenant    | u       | project_planning.organizations    | UNIQUE (organization_id, tenant_id)
 project_planning | uq_projects_entity_tenant        | u       | project_planning.projects         | UNIQUE (entity_id, tenant_id)
 project_planning | ux_episode_ref_per_project       | u       | project_planning.episodes         | UNIQUE (project_id, episode_ref)
(19 rows)

\$ 触发器查询见下方 E7b 修正版（原查询因别名冲突失败，已移除）
\$ 索引 pg_index（关键 schema）
     nspname      |          tbl          |                       idx                       | indisunique | indisprimary 
------------------+-----------------------+-------------------------------------------------+-------------+--------------
 asset_service    | asset_versions        | asset_versions_pkey                             | t           | t
 asset_service    | asset_versions        | ix_asset_service_asset_versions_project         | f           | f
 asset_service    | asset_versions        | ix_asset_service_asset_versions_tenant_id       | f           | f
 asset_service    | asset_versions        | ix_asset_service_asset_versions_type_sha256     | f           | f
 asset_service    | qc_reports            | ix_asset_service_qc_reports_asset_version_id    | f           | f
 asset_service    | qc_reports            | ix_asset_service_qc_reports_tenant_id           | f           | f
 asset_service    | qc_reports            | qc_reports_pkey                                 | t           | t
 outbox           | maintenance_runs      | ix_outbox_maintenance_runs_started              | f           | f
 outbox           | maintenance_runs      | maintenance_runs_pkey                           | t           | t
 outbox           | outbox_events         | ix_outbox_aggregate                             | f           | f
 outbox           | outbox_events         | ix_outbox_published                             | f           | f
 outbox           | outbox_events         | ix_outbox_tenant                                | f           | f
 outbox           | outbox_events         | outbox_events_pkey                              | t           | t
 outbox           | outbox_events_default | outbox_events_default_aggregate_id_idx          | f           | f
 outbox           | outbox_events_default | outbox_events_default_pkey                      | t           | t
 outbox           | outbox_events_default | outbox_events_default_published_at_idx          | f           | f
 outbox           | outbox_events_default | outbox_events_default_tenant_id_idx             | f           | f
 outbox           | outbox_events_y202611 | outbox_events_y202611_aggregate_id_idx          | f           | f
 outbox           | outbox_events_y202611 | outbox_events_y202611_pkey                      | t           | t
 outbox           | outbox_events_y202611 | outbox_events_y202611_published_at_idx          | f           | f
 outbox           | outbox_events_y202611 | outbox_events_y202611_tenant_id_idx             | f           | f
 outbox           | outbox_events_y202612 | outbox_events_y202612_aggregate_id_idx          | f           | f
 outbox           | outbox_events_y202612 | outbox_events_y202612_pkey                      | t           | t
 outbox           | outbox_events_y202612 | outbox_events_y202612_published_at_idx          | f           | f
 outbox           | outbox_events_y202612 | outbox_events_y202612_tenant_id_idx             | f           | f
 project_planning | episodes              | episodes_pkey                                   | t           | t
 project_planning | episodes              | ix_project_planning_episodes_project_id         | f           | f
 project_planning | episodes              | ix_project_planning_episodes_tenant_id          | f           | f
 project_planning | episodes              | ux_episode_ref_per_project                      | t           | f
 project_planning | organizations         | ix_project_planning_organizations_tenant_id     | f           | f
 project_planning | organizations         | organizations_pkey                              | t           | t
 project_planning | organizations         | uq_organizations_owner_tenant                   | t           | f
 project_planning | projects              | ix_project_planning_projects_tenant_id          | f           | f
 project_planning | projects              | projects_pkey                                   | t           | t
 project_planning | projects              | uq_projects_entity_tenant                       | t           | f
 project_planning | script_revisions      | ix_project_planning_script_revisions_project_id | f           | f
 project_planning | script_revisions      | ix_project_planning_script_revisions_tenant_id  | f           | f
 project_planning | script_revisions      | script_revisions_pkey                           | t           | t
 project_planning | script_revisions      | ux_script_active_version                        | t           | f
(39 rows)

### [ai_film_industrial] 约束 pg_constraint
     nspname      |                conname                | contype |                tbl                |                                                                                                                                                                                      def                                                                                                                                                                                       
------------------+---------------------------------------+---------+-----------------------------------+--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
 asset_service    | asset_versions_pkey                   | p       | asset_service.asset_versions      | PRIMARY KEY (entity_id)
 asset_service    | ck_asset_versions_status              | c       | asset_service.asset_versions      | CHECK (((status)::text = ANY ((ARRAY['APPROVED'::character varying, 'ARCHIVED'::character varying, 'GENERATED'::character varying, 'GOLD'::character varying, 'HUMAN_REVIEW'::character varying, 'QUARANTINED'::character varying, 'REJECTED'::character varying, 'REWORK'::character varying, 'SUPERSEDED'::character varying, 'TECHNICAL_QC'::character varying])::text[])))
 asset_service    | fk_asset_versions_project_tenant      | f       | asset_service.asset_versions      | FOREIGN KEY (project_id, tenant_id) REFERENCES project_planning.projects(entity_id, tenant_id)
 asset_service    | qc_reports_asset_version_id_fkey      | f       | asset_service.qc_reports          | FOREIGN KEY (asset_version_id) REFERENCES asset_service.asset_versions(entity_id)
 asset_service    | qc_reports_pkey                       | p       | asset_service.qc_reports          | PRIMARY KEY (qc_report_id)
 audit            | ck_status_transition_advances         | c       | audit.status_transitions          | CHECK (((from_status)::text <> (to_status)::text))
 audit            | ck_status_transition_version_positive | c       | audit.status_transitions          | CHECK ((entity_version >= 1))
 audit            | status_transitions_pkey               | p       | audit.status_transitions          | PRIMARY KEY (transition_id)
 audit            | trg_status_transitions_event_exists   | t       | audit.status_transitions          | TRIGGER DEFERRABLE INITIALLY DEFERRED
 audit            | uq_status_transition_version          | u       | audit.status_transitions          | UNIQUE (tenant_id, resource_type, resource_id, entity_version)
 outbox           | maintenance_runs_pkey                 | p       | outbox.maintenance_runs           | PRIMARY KEY (id)
 outbox           | outbox_events_default_pkey            | p       | outbox.outbox_events_default      | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_pkey                    | p       | outbox.outbox_events              | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_y202611_pkey            | p       | outbox.outbox_events_y202611      | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_y202612_pkey            | p       | outbox.outbox_events_y202612      | PRIMARY KEY (event_id, occurred_at)
 project_planning | ck_episodes_status                    | c       | project_planning.episodes         | CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'ARCHIVED'::character varying, 'DRAFT'::character varying])::text[])))
 project_planning | ck_projects_status                    | c       | project_planning.projects         | CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'ARCHIVED'::character varying, 'COMPLETED'::character varying, 'DRAFT'::character varying, 'ON_HOLD'::character varying])::text[])))
 project_planning | ck_script_revisions_status            | c       | project_planning.script_revisions | CHECK (((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'LOCKED'::character varying, 'LOCK_PENDING'::character varying, 'SUPERSEDED'::character varying])::text[])))
 project_planning | episodes_pkey                         | p       | project_planning.episodes         | PRIMARY KEY (entity_id)
 project_planning | fk_episodes_project                   | f       | project_planning.episodes         | FOREIGN KEY (project_id) REFERENCES project_planning.projects(entity_id)
 project_planning | fk_projects_organization_tenant       | f       | project_planning.projects         | FOREIGN KEY (organization_id, tenant_id) REFERENCES project_planning.organizations(organization_id, tenant_id)
 project_planning | fk_script_revisions_episode           | f       | project_planning.script_revisions | FOREIGN KEY (project_id, episode_ref) REFERENCES project_planning.episodes(project_id, episode_ref)
 project_planning | organizations_pkey                    | p       | project_planning.organizations    | PRIMARY KEY (organization_id)
 project_planning | projects_pkey                         | p       | project_planning.projects         | PRIMARY KEY (entity_id)
 project_planning | script_revisions_pkey                 | p       | project_planning.script_revisions | PRIMARY KEY (entity_id)
 project_planning | uq_organizations_owner_tenant         | u       | project_planning.organizations    | UNIQUE (organization_id, tenant_id)
 project_planning | uq_projects_entity_tenant             | u       | project_planning.projects         | UNIQUE (entity_id, tenant_id)
 project_planning | ux_episode_ref_per_project            | u       | project_planning.episodes         | UNIQUE (project_id, episode_ref)
(28 rows)

\$ 触发器查询见下方 E7b 修正版（原查询因别名冲突失败，已移除）
\$ 索引 pg_index（关键 schema）
     nspname      |          tbl          |                       idx                       | indisunique | indisprimary 
------------------+-----------------------+-------------------------------------------------+-------------+--------------
 asset_service    | asset_versions        | asset_versions_pkey                             | t           | t
 asset_service    | asset_versions        | ix_asset_service_asset_versions_project         | f           | f
 asset_service    | asset_versions        | ix_asset_service_asset_versions_tenant_id       | f           | f
 asset_service    | asset_versions        | ix_asset_service_asset_versions_type_sha256     | f           | f
 asset_service    | qc_reports            | ix_asset_service_qc_reports_asset_version_id    | f           | f
 asset_service    | qc_reports            | ix_asset_service_qc_reports_tenant_id           | f           | f
 asset_service    | qc_reports            | qc_reports_pkey                                 | t           | t
 audit            | status_transitions    | ix_status_transitions_resource_history          | f           | f
 audit            | status_transitions    | status_transitions_pkey                         | t           | t
 audit            | status_transitions    | uq_status_transition_version                    | t           | f
 outbox           | maintenance_runs      | ix_outbox_maintenance_runs_started              | f           | f
 outbox           | maintenance_runs      | maintenance_runs_pkey                           | t           | t
 outbox           | outbox_events         | ix_outbox_aggregate                             | f           | f
 outbox           | outbox_events         | ix_outbox_published                             | f           | f
 outbox           | outbox_events         | ix_outbox_tenant                                | f           | f
 outbox           | outbox_events         | outbox_events_pkey                              | t           | t
 outbox           | outbox_events_default | outbox_events_default_aggregate_id_idx          | f           | f
 outbox           | outbox_events_default | outbox_events_default_pkey                      | t           | t
 outbox           | outbox_events_default | outbox_events_default_published_at_idx          | f           | f
 outbox           | outbox_events_default | outbox_events_default_tenant_id_idx             | f           | f
 outbox           | outbox_events_y202611 | outbox_events_y202611_aggregate_id_idx          | f           | f
 outbox           | outbox_events_y202611 | outbox_events_y202611_pkey                      | t           | t
 outbox           | outbox_events_y202611 | outbox_events_y202611_published_at_idx          | f           | f
 outbox           | outbox_events_y202611 | outbox_events_y202611_tenant_id_idx             | f           | f
 outbox           | outbox_events_y202612 | outbox_events_y202612_aggregate_id_idx          | f           | f
 outbox           | outbox_events_y202612 | outbox_events_y202612_pkey                      | t           | t
 outbox           | outbox_events_y202612 | outbox_events_y202612_published_at_idx          | f           | f
 outbox           | outbox_events_y202612 | outbox_events_y202612_tenant_id_idx             | f           | f
 project_planning | episodes              | episodes_pkey                                   | t           | t
 project_planning | episodes              | ix_project_planning_episodes_project_id         | f           | f
 project_planning | episodes              | ix_project_planning_episodes_tenant_id          | f           | f
 project_planning | episodes              | ux_episode_ref_per_project                      | t           | f
 project_planning | organizations         | ix_project_planning_organizations_tenant_id     | f           | f
 project_planning | organizations         | organizations_pkey                              | t           | t
 project_planning | organizations         | uq_organizations_owner_tenant                   | t           | f
 project_planning | projects              | ix_project_planning_projects_tenant_id          | f           | f
 project_planning | projects              | projects_pkey                                   | t           | t
 project_planning | projects              | uq_projects_entity_tenant                       | t           | f
 project_planning | script_revisions      | ix_project_planning_script_revisions_project_id | f           | f
 project_planning | script_revisions      | ix_project_planning_script_revisions_tenant_id  | f           | f
 project_planning | script_revisions      | script_revisions_pkey                           | t           | t
 project_planning | script_revisions      | ux_script_active_version                        | t           | f
(42 rows)

## E7b. 触发器查询修正（f=pg_proc, ns=pg_namespace）
### [ai_film_verify] 触发器（修正版）
  tbl_schema   |      tbl       |       trigger_name       | fn_owner | tgenabled |   fn_schema   |       fn_name        
---------------+----------------+--------------------------+----------+-----------+---------------+----------------------
 asset_service | asset_versions | trg_asset_versions_guard | postgres | O         | asset_service | asset_versions_guard
 asset_service | qc_reports     | trg_qc_reports_guard     | postgres | O         | asset_service | qc_reports_guard
(2 rows)

### [ai_film_industrial] 触发器（修正版）
  tbl_schema   |        tbl         |            trigger_name             | fn_owner | tgenabled |   fn_schema   |            fn_name             
---------------+--------------------+-------------------------------------+----------+-----------+---------------+--------------------------------
 asset_service | asset_versions     | trg_asset_versions_guard            | postgres | O         | asset_service | asset_versions_guard
 asset_service | asset_versions     | trg_asset_versions_no_truncate      | postgres | O         | audit         | reject_truncate
 asset_service | qc_reports         | trg_qc_reports_guard                | postgres | O         | asset_service | qc_reports_guard
 asset_service | qc_reports         | trg_qc_reports_no_truncate          | postgres | O         | audit         | reject_truncate
 audit         | status_transitions | trg_status_transitions_event_exists | postgres | O         | audit         | assert_transition_event_exists
 audit         | status_transitions | trg_status_transitions_guard        | postgres | O         | audit         | reject_mutation
 audit         | status_transitions | trg_status_transitions_no_truncate  | postgres | O         | audit         | reject_truncate
(7 rows)


## E6. 六个冲突迁移文件：完整 SHA256 + Git blob 对象身份（无省略号）
$ main 侧（工作区文件 sha256sum + git hash-object）
\$ sha256sum data/migrations/versions/b3c4d5e6f7a8_asset_project_ownership.py
fbc0da26e3caf0fc99ef8464fb780d231451ed83362a2bf004f94a50382a4823 *data/migrations/versions/b3c4d5e6f7a8_asset_project_ownership.py
\$ git hash-object data/migrations/versions/b3c4d5e6f7a8_asset_project_ownership.py
dbc51ce794d95cd62245edde52ab7f5ca3cb64dd
\$ sha256sum data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py
7af954b50a7757f81859a5aeab77b6a32a8cb96483958d5a026a97cda3272369 *data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py
\$ git hash-object data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py
dbae1fc43715b33c6334a86e25debb7a915f69ed
\$ sha256sum data/migrations/versions/9e3b4c5d6e7f_create_app_role_and_grants.py
0dd3d2fddef8b3f6c943f64845caf8ed2dd5ba94bd7ba31012307f01bc34f29b *data/migrations/versions/9e3b4c5d6e7f_create_app_role_and_grants.py
\$ git hash-object data/migrations/versions/9e3b4c5d6e7f_create_app_role_and_grants.py
3b5b6753dc4876622e0a74a5fa0f84e91f8fe145
$ 分支侧（origin/feat/identity-data-ownership，git show | sha256sum + git rev-parse）
\$ git show origin/feat/identity-data-ownership:data/migrations/versions/b3c4d5e6f7a8_asset_status_transitions.py | sha256sum
0d1c44d24f10d3b231a29457fd5968c35b8a71b43a446ae0a2d15957f7a6e606 *-
\$ git rev-parse origin/feat/identity-data-ownership:data/migrations/versions/b3c4d5e6f7a8_asset_status_transitions.py
8899ef436376c211af3b91a403e7d482a9afa89d
\$ git show origin/feat/identity-data-ownership:data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py | sha256sum
2a8d7d2395c5d936b6e6be2be393c437a781f664c68d0f26773d7fe210f27c8f *-
\$ git rev-parse origin/feat/identity-data-ownership:data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py
816a0f1c337601d36b2309646622c208cc56df81
\$ git show origin/feat/identity-data-ownership:data/migrations/versions/9e3b4c5d6e7f_create_app_role_and_grants.py | sha256sum
57c3d775ac07e90a8ecce53e39449f226c3cd68d38f9c984f2d474130ad81497 *-
\$ git rev-parse origin/feat/identity-data-ownership:data/migrations/versions/9e3b4c5d6e7f_create_app_role_and_grants.py
41faff78a58e068775db8befa6745c8d5bc99691

## 证据有效性说明
- E7 的触发器查询曾因别名冲突（`column p.proname does not exist`）失败，已移除并以 **E7b 修正版**替代；E7b 中触发器查询为有效证据。
- 当前存档所有命令均为成功的只读 SELECT 查询或 git/sha256sum 只读命令，无失败输出残留。
- 所有数据库查询均为 SELECT；未执行任何写操作、SET ROLE、upgrade/downgrade。

## E8. §6.5 依赖盘点补充取证：活跃连接 + 运行中程序引用（2026-11-19 13:22，只读）

> 承接 ADR §6.5「verify 混跑角色依赖盘点」中此前只有框架、无取证的「活跃连接」「运行中程序」两项。
> 用途：风险 #9「verify 混跑角色来源未定」的依赖侧证据。**注意**：依赖盘点 ≠ 来源取证——
> 本项只能证明「当前无依赖」，不能证明「9e3b 某版本是否执行」（创建来源仍不可证，按保守原则不推定）。

\\$ 两库当前活跃连接按角色（backend_type=client backend）
\$ [ai_film_verify] SELECT usename,application_name,state,count(*) FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY 1,2,3;
 usename  | application_name | state  | count 
----------+------------------+--------+-------
 postgres | psql             | active |     1
(1 row)

\$ [ai_film_industrial] SELECT usename,application_name,state,count(*) FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY 1,2,3;
 usename  | application_name | state  | count 
----------+------------------+--------+-------
 postgres | psql             | active |     1
(1 row)

\$ 运行时代码（packages/workers/api/infra）中 svc_* 字面量引用计数：0（grep -rIl，排除 __pycache__）
  结论：main 运行时 config.py 仅配置 main 4 角色（app_role/outbox_relay_role/project_planning_role/asset_service_role）；
        8 个 svc_* 在运行时代码/迁移/测试中 0 引用，仅出现于 docs 下 3 份本次治理文档。
\$ 代码引用分布统计（grep -rIl svc_）：packages=0 workers=0 api=0 infra=0 data/migrations=0 tests=0 scripts=0 docs=3

**E8 结论**：两库当前无业务角色活跃会话（仅本次 postgres/psql 取证连接）；8 个 svc_* 无任何运行时/迁移/测试代码引用。
§6.5 六项盘点现全部有证据：拥有对象(无)/成员关系(空)/活跃连接(无)/遗留 GRANT(见 E5)/OWNER 依赖(postgres)/运行中程序(无引用)。
依赖侧指向「可退役」，但**创建来源仍不可证**，退役仍须走 §7 Expand-Contract 经所有者裁定，不在本轮执行。

## E9. 证据范围核定：集群级 role catalog vs 数据库级对象 catalog（2026-11-19 13:45，只读）

> 承接第四轮补充复审：PostgreSQL 的 role 是集群级对象，不是每库独立。本项只读验证并留证。
> 均为 SELECT；未执行任何写操作。

### E9.1 shared catalog 标记（relisshared=t 即集群级）
\$ SELECT relname, relisshared FROM pg_class WHERE relname IN (pg_authid, pg_auth_members, pg_database);
     relname     | relisshared 
-----------------+-------------
 pg_authid       | t
 pg_database     | t
 pg_auth_members | t
(3 rows)


### E9.2 两库 pg_roles 业务角色（应完全一致 = 同一批）
$ [ai_film_industrial] SELECT count(*) FROM pg_roles WHERE rolname LIKE 'svc_%' OR rolname LIKE '%_role';
 biz_roles 
-----------
        12
(1 row)

$ [ai_film_industrial] 8 个 svc_* 名称串
svc_asset,svc_generation_execution,svc_identity,svc_outbox_relay,svc_project_planning,svc_qc_governance,svc_shot_planning,svc_workflow
$ [ai_film_verify] SELECT count(*) FROM pg_roles WHERE rolname LIKE 'svc_%' OR rolname LIKE '%_role';
 biz_roles 
-----------
        12
(1 row)

$ [ai_film_verify] 8 个 svc_* 名称串
svc_asset,svc_generation_execution,svc_identity,svc_outbox_relay,svc_project_planning,svc_qc_governance,svc_shot_planning,svc_workflow

### E9.3 两库 pg_auth_members 条数（应一致）
$ [ai_film_industrial] SELECT count(*) FROM pg_auth_members;
 members 
---------
       3
(1 row)

$ [ai_film_verify] SELECT count(*) FROM pg_auth_members;
 members 
---------
       3
(1 row)


### E9.4 数据库级对象 catalog（应不同 = 库级）
$ [ai_film_industrial] relkind r/p 计数 / pg_policy / schema
 biz_tables | policies | schemas 
------------+----------+---------
         14 |       11 |       6
(1 row)

$ [ai_film_verify] relkind r/p 计数 / pg_policy / schema
 biz_tables | policies | schemas 
------------+----------+---------
         13 |       10 |       5
(1 row)


**E9 结论**：pg_authid/pg_auth_members/pg_database 的 relisshared=t（集群级）；两库业务角色均 12、svc_* 名称逐字相同、pg_auth_members 均 3 条 → 同一批共享身份，不可按库重复计数。两库对象级事实不同（表 14/13、policy 11/10、schema 6/5）→ 库级，分库记录正确。

## E10. 第四轮复审补充取证（2026-11-19 14:20，纯只读 SELECT）

> 承接第四轮复审对 C.5 的缺口清单：集群级只采一次 / 逐对象 OWNER 名称 / 函数签名 / 全量 ACL /
> 全量有效权限 / 约束·触发器·索引完整定义 / Git 固定 commit。
> 性质：全部为 SELECT；未执行任何 DDL/GRANT/REVOKE/SET ROLE；未输出数据库口令。

### E10.0 集群级事实（只采集一次，不对两库重复计数）
$ SELECT version(), current_database();
                                                       version                                                        | current_database 
----------------------------------------------------------------------------------------------------------------------+------------------
 PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit | postgres
(1 row)

$ SELECT system_identifier FROM pg_control_system();   -- 实例身份
  system_identifier  
---------------------
 7707510806710833190
(1 row)

$ SELECT relname, relisshared FROM pg_class WHERE relname IN ('pg_authid','pg_auth_members','pg_database');
     relname     | relisshared 
-----------------+-------------
 pg_authid       | t
 pg_database     | t
 pg_auth_members | t
(3 rows)

$ 全部业务相关角色属性（集群级，一份）
         rolname          | rolcanlogin | rolsuper | rolbypassrls | rolcreatedb | rolcreaterole | rolinherit | rolreplication | rolconnlimit 
--------------------------+-------------+----------+--------------+-------------+---------------+------------+----------------+--------------
 app_role                 | t           | f        | f            | f           | f             | t          | f              |           -1
 asset_service_role       | t           | f        | f            | f           | f             | t          | f              |           -1
 outbox_relay_role        | t           | f        | f            | f           | f             | t          | f              |           -1
 postgres                 | t           | t        | t            | t           | t             | t          | t              |           -1
 project_planning_role    | t           | f        | f            | f           | f             | t          | f              |           -1
 svc_asset                | t           | f        | f            | f           | f             | t          | f              |           -1
 svc_generation_execution | t           | f        | f            | f           | f             | t          | f              |           -1
 svc_identity             | t           | f        | f            | f           | f             | t          | f              |           -1
 svc_outbox_relay         | t           | f        | f            | f           | f             | t          | f              |           -1
 svc_project_planning     | t           | f        | f            | f           | f             | t          | f              |           -1
 svc_qc_governance        | t           | f        | f            | f           | f             | t          | f              |           -1
 svc_shot_planning        | t           | f        | f            | f           | f             | t          | f              |           -1
 svc_workflow             | t           | f        | f            | f           | f             | t          | f              |           -1
(13 rows)

$ 全部成员关系（集群级，含 PG16 三选项 + grantor）
      group_role      | member_role | admin_option | inherit_option | set_option | grantor  
----------------------+-------------+--------------+----------------+------------+----------
 pg_read_all_settings | pg_monitor  | f            | t              | t          | postgres
 pg_read_all_stats    | pg_monitor  | f            | t              | t          | postgres
 pg_stat_scan_tables  | pg_monitor  | f            | t              | t          | postgres
(3 rows)

$ 数据库列表 + datacl 展开（含 PUBLIC）
      datname       |  owner   |     datacl     
--------------------+----------+----------------
 ai_film_industrial | postgres | <NULL=default>
 ai_film_verify     | postgres | <NULL=default>
 postgres           | postgres | <NULL=default>
(3 rows)

      datname       |     grantee     | privilege_type 
--------------------+-----------------+----------------
 ai_film_industrial | postgres        | CONNECT
 ai_film_industrial | postgres        | CREATE
 ai_film_industrial | postgres        | TEMPORARY
 ai_film_industrial | unknown (OID=0) | CONNECT
 ai_film_industrial | unknown (OID=0) | TEMPORARY
 ai_film_verify     | postgres        | CONNECT
 ai_film_verify     | postgres        | CREATE
 ai_film_verify     | postgres        | TEMPORARY
 ai_film_verify     | unknown (OID=0) | CONNECT
 ai_film_verify     | unknown (OID=0) | TEMPORARY
 postgres           | postgres        | CONNECT
 postgres           | postgres        | CREATE
 postgres           | postgres        | TEMPORARY
 postgres           | unknown (OID=0) | CONNECT
 postgres           | unknown (OID=0) | TEMPORARY
(15 rows)


### E10.1 Schema OWNER + nspacl（数据库级，分库）
$ [ai_film_industrial] nspname/owner/nspacl
     nspname      |       owner       |                                                                    nspacl                                                                    
------------------+-------------------+----------------------------------------------------------------------------------------------------------------------------------------------
 asset_service    | postgres          | postgres=UC/postgres | asset_service_role=U/postgres
 audit            | postgres          | postgres=UC/postgres | project_planning_role=U/postgres | asset_service_role=U/postgres
 auth             | postgres          | postgres=UC/postgres | project_planning_role=U/postgres | asset_service_role=U/postgres
 outbox           | postgres          | postgres=UC/postgres | app_role=U/postgres | outbox_relay_role=U/postgres | project_planning_role=U/postgres | asset_service_role=U/postgres
 project_planning | postgres          | postgres=UC/postgres | project_planning_role=U/postgres | asset_service_role=U/postgres
 public           | pg_database_owner | pg_database_owner=UC/pg_database_owner | =U/pg_database_owner
(6 rows)

$ [ai_film_verify] nspname/owner/nspacl
     nspname      |       owner       |                                                                    nspacl                                                                    
------------------+-------------------+----------------------------------------------------------------------------------------------------------------------------------------------
 asset_service    | postgres          | postgres=UC/postgres | asset_service_role=U/postgres
 auth             | postgres          | postgres=UC/postgres | project_planning_role=U/postgres | asset_service_role=U/postgres
 outbox           | postgres          | postgres=UC/postgres | app_role=U/postgres | outbox_relay_role=U/postgres | project_planning_role=U/postgres | asset_service_role=U/postgres
 project_planning | postgres          | postgres=UC/postgres | project_planning_role=U/postgres | asset_service_role=U/postgres
 public           | pg_database_owner | pg_database_owner=UC/pg_database_owner | =U/pg_database_owner
(5 rows)


### E10.2 逐对象 OWNER（含对象名，非聚合计数）
$ [ai_film_industrial] relkind/ schema.object / owner
   objtype   |              object               |  owner   
-------------+-----------------------------------+----------
 partitioned | outbox.outbox_events              | postgres
 sequence    | outbox.maintenance_runs_id_seq    | postgres
 table       | asset_service.asset_versions      | postgres
 table       | asset_service.qc_reports          | postgres
 table       | audit.status_transitions          | postgres
 table       | auth.revoked_tokens               | postgres
 table       | outbox.maintenance_runs           | postgres
 table       | outbox.outbox_events_default      | postgres
 table       | outbox.outbox_events_y202611      | postgres
 table       | outbox.outbox_events_y202612      | postgres
 table       | project_planning.episodes         | postgres
 table       | project_planning.organizations    | postgres
 table       | project_planning.projects         | postgres
 table       | project_planning.script_revisions | postgres
 table       | public.alembic_version            | postgres
(15 rows)

$ [ai_film_verify] relkind/ schema.object / owner
   objtype   |              object               |  owner   
-------------+-----------------------------------+----------
 partitioned | outbox.outbox_events              | postgres
 sequence    | outbox.maintenance_runs_id_seq    | postgres
 table       | asset_service.asset_versions      | postgres
 table       | asset_service.qc_reports          | postgres
 table       | auth.revoked_tokens               | postgres
 table       | outbox.maintenance_runs           | postgres
 table       | outbox.outbox_events_default      | postgres
 table       | outbox.outbox_events_y202611      | postgres
 table       | outbox.outbox_events_y202612      | postgres
 table       | project_planning.episodes         | postgres
 table       | project_planning.organizations    | postgres
 table       | project_planning.projects         | postgres
 table       | project_planning.script_revisions | postgres
 table       | public.alembic_version            | postgres
(14 rows)


### E10.3 函数：完整签名 + OWNER + SECURITY DEFINER + proacl 展开 + proconfig
$ [ai_film_industrial] 函数清单（含 pg_get_function_identity_arguments，可区分重载）
    nspname    |            proname             | args |  owner   | security_definer | provolatile |           proconfig            
---------------+--------------------------------+------+----------+------------------+-------------+--------------------------------
 asset_service | asset_versions_guard           |      | postgres | f                | v           | <none>
 asset_service | qc_reports_guard               |      | postgres | f                | v           | <none>
 audit         | assert_transition_event_exists |      | postgres | t                | v           | search_path=pg_catalog, outbox
 audit         | reject_mutation                |      | postgres | f                | v           | <none>
 audit         | reject_truncate                |      | postgres | f                | v           | <none>
 outbox        | list_active_tenants            |      | postgres | t                | s           | search_path=outbox, pg_catalog
(6 rows)

$ [ai_film_industrial] 函数 proacl 展开（NULL 表示默认=EXECUTE TO PUBLIC，已用 acldefault 展开）
                  func                  |      grantee      | privilege_type 
----------------------------------------+-------------------+----------------
 asset_service.asset_versions_guard()   | postgres          | EXECUTE
 asset_service.asset_versions_guard()   | unknown (OID=0)   | EXECUTE
 asset_service.qc_reports_guard()       | postgres          | EXECUTE
 asset_service.qc_reports_guard()       | unknown (OID=0)   | EXECUTE
 audit.assert_transition_event_exists() | postgres          | EXECUTE
 audit.assert_transition_event_exists() | unknown (OID=0)   | EXECUTE
 audit.reject_mutation()                | postgres          | EXECUTE
 audit.reject_mutation()                | unknown (OID=0)   | EXECUTE
 audit.reject_truncate()                | postgres          | EXECUTE
 audit.reject_truncate()                | unknown (OID=0)   | EXECUTE
 outbox.list_active_tenants()           | app_role          | EXECUTE
 outbox.list_active_tenants()           | outbox_relay_role | EXECUTE
 outbox.list_active_tenants()           | postgres          | EXECUTE
(13 rows)

$ [ai_film_verify] 函数清单（含 pg_get_function_identity_arguments，可区分重载）
    nspname    |       proname        | args |  owner   | security_definer | provolatile |           proconfig            
---------------+----------------------+------+----------+------------------+-------------+--------------------------------
 asset_service | asset_versions_guard |      | postgres | f                | v           | <none>
 asset_service | qc_reports_guard     |      | postgres | f                | v           | <none>
 outbox        | list_active_tenants  |      | postgres | t                | s           | search_path=outbox, pg_catalog
(3 rows)

$ [ai_film_verify] 函数 proacl 展开（NULL 表示默认=EXECUTE TO PUBLIC，已用 acldefault 展开）
                 func                 |      grantee      | privilege_type 
--------------------------------------+-------------------+----------------
 asset_service.asset_versions_guard() | postgres          | EXECUTE
 asset_service.asset_versions_guard() | unknown (OID=0)   | EXECUTE
 asset_service.qc_reports_guard()     | postgres          | EXECUTE
 asset_service.qc_reports_guard()     | unknown (OID=0)   | EXECUTE
 outbox.list_active_tenants()         | app_role          | EXECUTE
 outbox.list_active_tenants()         | outbox_relay_role | EXECUTE
 outbox.list_active_tenants()         | postgres          | EXECUTE
(7 rows)


### E10.4 表/分区/序列/视图/物化视图 ACL 展开（含 PARTITION 与 PARTITION 直连）
$ [ai_film_industrial] 全部业务对象 ACL（含 PUBLIC；NULL 用 acldefault 展开）
              object               |   objtype   |        grantee        | privilege_type 
-----------------------------------+-------------+-----------------------+----------------
 asset_service.asset_versions      | table       | asset_service_role    | INSERT
 asset_service.asset_versions      | table       | asset_service_role    | SELECT
 asset_service.asset_versions      | table       | asset_service_role    | UPDATE
 asset_service.asset_versions      | table       | postgres              | DELETE
 asset_service.asset_versions      | table       | postgres              | INSERT
 asset_service.asset_versions      | table       | postgres              | REFERENCES
 asset_service.asset_versions      | table       | postgres              | SELECT
 asset_service.asset_versions      | table       | postgres              | TRIGGER
 asset_service.asset_versions      | table       | postgres              | TRUNCATE
 asset_service.asset_versions      | table       | postgres              | UPDATE
 asset_service.qc_reports          | table       | asset_service_role    | INSERT
 asset_service.qc_reports          | table       | asset_service_role    | SELECT
 asset_service.qc_reports          | table       | postgres              | DELETE
 asset_service.qc_reports          | table       | postgres              | INSERT
 asset_service.qc_reports          | table       | postgres              | REFERENCES
 asset_service.qc_reports          | table       | postgres              | SELECT
 asset_service.qc_reports          | table       | postgres              | TRIGGER
 asset_service.qc_reports          | table       | postgres              | TRUNCATE
 asset_service.qc_reports          | table       | postgres              | UPDATE
 audit.status_transitions          | table       | asset_service_role    | INSERT
 audit.status_transitions          | table       | asset_service_role    | SELECT
 audit.status_transitions          | table       | postgres              | DELETE
 audit.status_transitions          | table       | postgres              | INSERT
 audit.status_transitions          | table       | postgres              | REFERENCES
 audit.status_transitions          | table       | postgres              | SELECT
 audit.status_transitions          | table       | postgres              | TRIGGER
 audit.status_transitions          | table       | postgres              | TRUNCATE
 audit.status_transitions          | table       | postgres              | UPDATE
 audit.status_transitions          | table       | project_planning_role | INSERT
 audit.status_transitions          | table       | project_planning_role | SELECT
 auth.revoked_tokens               | table       | asset_service_role    | SELECT
 auth.revoked_tokens               | table       | postgres              | DELETE
 auth.revoked_tokens               | table       | postgres              | INSERT
 auth.revoked_tokens               | table       | postgres              | REFERENCES
 auth.revoked_tokens               | table       | postgres              | SELECT
 auth.revoked_tokens               | table       | postgres              | TRIGGER
 auth.revoked_tokens               | table       | postgres              | TRUNCATE
 auth.revoked_tokens               | table       | postgres              | UPDATE
 auth.revoked_tokens               | table       | project_planning_role | SELECT
 outbox.maintenance_runs           | table       | postgres              | DELETE
 outbox.maintenance_runs           | table       | postgres              | INSERT
 outbox.maintenance_runs           | table       | postgres              | REFERENCES
 outbox.maintenance_runs           | table       | postgres              | SELECT
 outbox.maintenance_runs           | table       | postgres              | TRIGGER
 outbox.maintenance_runs           | table       | postgres              | TRUNCATE
 outbox.maintenance_runs           | table       | postgres              | UPDATE
 outbox.maintenance_runs_id_seq    | sequence    | app_role              | SELECT
 outbox.maintenance_runs_id_seq    | sequence    | app_role              | USAGE
 outbox.maintenance_runs_id_seq    | sequence    | asset_service_role    | USAGE
 outbox.maintenance_runs_id_seq    | sequence    | outbox_relay_role     | USAGE
 outbox.maintenance_runs_id_seq    | sequence    | postgres              | SELECT
 outbox.maintenance_runs_id_seq    | sequence    | postgres              | UPDATE
 outbox.maintenance_runs_id_seq    | sequence    | postgres              | USAGE
 outbox.maintenance_runs_id_seq    | sequence    | project_planning_role | USAGE
 outbox.outbox_events              | partitioned | app_role              | SELECT
 outbox.outbox_events              | partitioned | asset_service_role    | INSERT
 outbox.outbox_events              | partitioned | asset_service_role    | SELECT
 outbox.outbox_events              | partitioned | outbox_relay_role     | SELECT
 outbox.outbox_events              | partitioned | postgres              | DELETE
 outbox.outbox_events              | partitioned | postgres              | INSERT
 outbox.outbox_events              | partitioned | postgres              | REFERENCES
 outbox.outbox_events              | partitioned | postgres              | SELECT
 outbox.outbox_events              | partitioned | postgres              | TRIGGER
 outbox.outbox_events              | partitioned | postgres              | TRUNCATE
 outbox.outbox_events              | partitioned | postgres              | UPDATE
 outbox.outbox_events              | partitioned | project_planning_role | INSERT
 outbox.outbox_events              | partitioned | project_planning_role | SELECT
 outbox.outbox_events_default      | table       | asset_service_role    | INSERT
 outbox.outbox_events_default      | table       | asset_service_role    | SELECT
 outbox.outbox_events_default      | table       | outbox_relay_role     | SELECT
 outbox.outbox_events_default      | table       | postgres              | DELETE
 outbox.outbox_events_default      | table       | postgres              | INSERT
 outbox.outbox_events_default      | table       | postgres              | REFERENCES
 outbox.outbox_events_default      | table       | postgres              | SELECT
 outbox.outbox_events_default      | table       | postgres              | TRIGGER
 outbox.outbox_events_default      | table       | postgres              | TRUNCATE
 outbox.outbox_events_default      | table       | postgres              | UPDATE
 outbox.outbox_events_default      | table       | project_planning_role | INSERT
 outbox.outbox_events_default      | table       | project_planning_role | SELECT
 outbox.outbox_events_y202611      | table       | asset_service_role    | INSERT
 outbox.outbox_events_y202611      | table       | asset_service_role    | SELECT
 outbox.outbox_events_y202611      | table       | outbox_relay_role     | SELECT
 outbox.outbox_events_y202611      | table       | postgres              | DELETE
 outbox.outbox_events_y202611      | table       | postgres              | INSERT
 outbox.outbox_events_y202611      | table       | postgres              | REFERENCES
 outbox.outbox_events_y202611      | table       | postgres              | SELECT
 outbox.outbox_events_y202611      | table       | postgres              | TRIGGER
 outbox.outbox_events_y202611      | table       | postgres              | TRUNCATE
 outbox.outbox_events_y202611      | table       | postgres              | UPDATE
 outbox.outbox_events_y202611      | table       | project_planning_role | INSERT
 outbox.outbox_events_y202611      | table       | project_planning_role | SELECT
 outbox.outbox_events_y202612      | table       | asset_service_role    | INSERT
 outbox.outbox_events_y202612      | table       | asset_service_role    | SELECT
 outbox.outbox_events_y202612      | table       | outbox_relay_role     | SELECT
 outbox.outbox_events_y202612      | table       | postgres              | DELETE
 outbox.outbox_events_y202612      | table       | postgres              | INSERT
 outbox.outbox_events_y202612      | table       | postgres              | REFERENCES
 outbox.outbox_events_y202612      | table       | postgres              | SELECT
 outbox.outbox_events_y202612      | table       | postgres              | TRIGGER
 outbox.outbox_events_y202612      | table       | postgres              | TRUNCATE
 outbox.outbox_events_y202612      | table       | postgres              | UPDATE
 outbox.outbox_events_y202612      | table       | project_planning_role | INSERT
 outbox.outbox_events_y202612      | table       | project_planning_role | SELECT
 project_planning.episodes         | table       | postgres              | DELETE
 project_planning.episodes         | table       | postgres              | INSERT
 project_planning.episodes         | table       | postgres              | REFERENCES
 project_planning.episodes         | table       | postgres              | SELECT
 project_planning.episodes         | table       | postgres              | TRIGGER
 project_planning.episodes         | table       | postgres              | TRUNCATE
 project_planning.episodes         | table       | postgres              | UPDATE
 project_planning.episodes         | table       | project_planning_role | INSERT
 project_planning.episodes         | table       | project_planning_role | SELECT
 project_planning.episodes         | table       | project_planning_role | UPDATE
 project_planning.organizations    | table       | postgres              | DELETE
 project_planning.organizations    | table       | postgres              | INSERT
 project_planning.organizations    | table       | postgres              | REFERENCES
 project_planning.organizations    | table       | postgres              | SELECT
 project_planning.organizations    | table       | postgres              | TRIGGER
 project_planning.organizations    | table       | postgres              | TRUNCATE
 project_planning.organizations    | table       | postgres              | UPDATE
 project_planning.organizations    | table       | project_planning_role | SELECT
 project_planning.projects         | table       | postgres              | DELETE
 project_planning.projects         | table       | postgres              | INSERT
 project_planning.projects         | table       | postgres              | REFERENCES
 project_planning.projects         | table       | postgres              | SELECT
 project_planning.projects         | table       | postgres              | TRIGGER
 project_planning.projects         | table       | postgres              | TRUNCATE
 project_planning.projects         | table       | postgres              | UPDATE
 project_planning.projects         | table       | project_planning_role | INSERT
 project_planning.projects         | table       | project_planning_role | SELECT
 project_planning.projects         | table       | project_planning_role | UPDATE
 project_planning.script_revisions | table       | postgres              | DELETE
 project_planning.script_revisions | table       | postgres              | INSERT
 project_planning.script_revisions | table       | postgres              | REFERENCES
 project_planning.script_revisions | table       | postgres              | SELECT
 project_planning.script_revisions | table       | postgres              | TRIGGER
 project_planning.script_revisions | table       | postgres              | TRUNCATE
 project_planning.script_revisions | table       | postgres              | UPDATE
 project_planning.script_revisions | table       | project_planning_role | INSERT
 project_planning.script_revisions | table       | project_planning_role | SELECT
 project_planning.script_revisions | table       | project_planning_role | UPDATE
 public.alembic_version            | table       | asset_service_role    | SELECT
 public.alembic_version            | table       | postgres              | DELETE
 public.alembic_version            | table       | postgres              | INSERT
 public.alembic_version            | table       | postgres              | REFERENCES
 public.alembic_version            | table       | postgres              | SELECT
 public.alembic_version            | table       | postgres              | TRIGGER
 public.alembic_version            | table       | postgres              | TRUNCATE
 public.alembic_version            | table       | postgres              | UPDATE
 public.alembic_version            | table       | project_planning_role | SELECT
(150 rows)

$ [ai_film_verify] 全部业务对象 ACL（含 PUBLIC；NULL 用 acldefault 展开）
              object               |   objtype   |        grantee        | privilege_type 
-----------------------------------+-------------+-----------------------+----------------
 asset_service.asset_versions      | table       | asset_service_role    | INSERT
 asset_service.asset_versions      | table       | asset_service_role    | SELECT
 asset_service.asset_versions      | table       | asset_service_role    | UPDATE
 asset_service.asset_versions      | table       | postgres              | DELETE
 asset_service.asset_versions      | table       | postgres              | INSERT
 asset_service.asset_versions      | table       | postgres              | REFERENCES
 asset_service.asset_versions      | table       | postgres              | SELECT
 asset_service.asset_versions      | table       | postgres              | TRIGGER
 asset_service.asset_versions      | table       | postgres              | TRUNCATE
 asset_service.asset_versions      | table       | postgres              | UPDATE
 asset_service.qc_reports          | table       | asset_service_role    | INSERT
 asset_service.qc_reports          | table       | asset_service_role    | SELECT
 asset_service.qc_reports          | table       | postgres              | DELETE
 asset_service.qc_reports          | table       | postgres              | INSERT
 asset_service.qc_reports          | table       | postgres              | REFERENCES
 asset_service.qc_reports          | table       | postgres              | SELECT
 asset_service.qc_reports          | table       | postgres              | TRIGGER
 asset_service.qc_reports          | table       | postgres              | TRUNCATE
 asset_service.qc_reports          | table       | postgres              | UPDATE
 auth.revoked_tokens               | table       | asset_service_role    | SELECT
 auth.revoked_tokens               | table       | postgres              | DELETE
 auth.revoked_tokens               | table       | postgres              | INSERT
 auth.revoked_tokens               | table       | postgres              | REFERENCES
 auth.revoked_tokens               | table       | postgres              | SELECT
 auth.revoked_tokens               | table       | postgres              | TRIGGER
 auth.revoked_tokens               | table       | postgres              | TRUNCATE
 auth.revoked_tokens               | table       | postgres              | UPDATE
 auth.revoked_tokens               | table       | project_planning_role | SELECT
 outbox.maintenance_runs           | table       | postgres              | DELETE
 outbox.maintenance_runs           | table       | postgres              | INSERT
 outbox.maintenance_runs           | table       | postgres              | REFERENCES
 outbox.maintenance_runs           | table       | postgres              | SELECT
 outbox.maintenance_runs           | table       | postgres              | TRIGGER
 outbox.maintenance_runs           | table       | postgres              | TRUNCATE
 outbox.maintenance_runs           | table       | postgres              | UPDATE
 outbox.maintenance_runs_id_seq    | sequence    | app_role              | SELECT
 outbox.maintenance_runs_id_seq    | sequence    | app_role              | USAGE
 outbox.maintenance_runs_id_seq    | sequence    | asset_service_role    | USAGE
 outbox.maintenance_runs_id_seq    | sequence    | outbox_relay_role     | USAGE
 outbox.maintenance_runs_id_seq    | sequence    | postgres              | SELECT
 outbox.maintenance_runs_id_seq    | sequence    | postgres              | UPDATE
 outbox.maintenance_runs_id_seq    | sequence    | postgres              | USAGE
 outbox.maintenance_runs_id_seq    | sequence    | project_planning_role | USAGE
 outbox.outbox_events              | partitioned | app_role              | SELECT
 outbox.outbox_events              | partitioned | asset_service_role    | INSERT
 outbox.outbox_events              | partitioned | asset_service_role    | SELECT
 outbox.outbox_events              | partitioned | outbox_relay_role     | SELECT
 outbox.outbox_events              | partitioned | postgres              | DELETE
 outbox.outbox_events              | partitioned | postgres              | INSERT
 outbox.outbox_events              | partitioned | postgres              | REFERENCES
 outbox.outbox_events              | partitioned | postgres              | SELECT
 outbox.outbox_events              | partitioned | postgres              | TRIGGER
 outbox.outbox_events              | partitioned | postgres              | TRUNCATE
 outbox.outbox_events              | partitioned | postgres              | UPDATE
 outbox.outbox_events              | partitioned | project_planning_role | INSERT
 outbox.outbox_events              | partitioned | project_planning_role | SELECT
 outbox.outbox_events_default      | table       | asset_service_role    | INSERT
 outbox.outbox_events_default      | table       | asset_service_role    | SELECT
 outbox.outbox_events_default      | table       | outbox_relay_role     | SELECT
 outbox.outbox_events_default      | table       | postgres              | DELETE
 outbox.outbox_events_default      | table       | postgres              | INSERT
 outbox.outbox_events_default      | table       | postgres              | REFERENCES
 outbox.outbox_events_default      | table       | postgres              | SELECT
 outbox.outbox_events_default      | table       | postgres              | TRIGGER
 outbox.outbox_events_default      | table       | postgres              | TRUNCATE
 outbox.outbox_events_default      | table       | postgres              | UPDATE
 outbox.outbox_events_default      | table       | project_planning_role | INSERT
 outbox.outbox_events_default      | table       | project_planning_role | SELECT
 outbox.outbox_events_y202611      | table       | asset_service_role    | INSERT
 outbox.outbox_events_y202611      | table       | asset_service_role    | SELECT
 outbox.outbox_events_y202611      | table       | outbox_relay_role     | SELECT
 outbox.outbox_events_y202611      | table       | postgres              | DELETE
 outbox.outbox_events_y202611      | table       | postgres              | INSERT
 outbox.outbox_events_y202611      | table       | postgres              | REFERENCES
 outbox.outbox_events_y202611      | table       | postgres              | SELECT
 outbox.outbox_events_y202611      | table       | postgres              | TRIGGER
 outbox.outbox_events_y202611      | table       | postgres              | TRUNCATE
 outbox.outbox_events_y202611      | table       | postgres              | UPDATE
 outbox.outbox_events_y202611      | table       | project_planning_role | INSERT
 outbox.outbox_events_y202611      | table       | project_planning_role | SELECT
 outbox.outbox_events_y202612      | table       | asset_service_role    | INSERT
 outbox.outbox_events_y202612      | table       | asset_service_role    | SELECT
 outbox.outbox_events_y202612      | table       | outbox_relay_role     | SELECT
 outbox.outbox_events_y202612      | table       | postgres              | DELETE
 outbox.outbox_events_y202612      | table       | postgres              | INSERT
 outbox.outbox_events_y202612      | table       | postgres              | REFERENCES
 outbox.outbox_events_y202612      | table       | postgres              | SELECT
 outbox.outbox_events_y202612      | table       | postgres              | TRIGGER
 outbox.outbox_events_y202612      | table       | postgres              | TRUNCATE
 outbox.outbox_events_y202612      | table       | postgres              | UPDATE
 outbox.outbox_events_y202612      | table       | project_planning_role | INSERT
 outbox.outbox_events_y202612      | table       | project_planning_role | SELECT
 project_planning.episodes         | table       | postgres              | DELETE
 project_planning.episodes         | table       | postgres              | INSERT
 project_planning.episodes         | table       | postgres              | REFERENCES
 project_planning.episodes         | table       | postgres              | SELECT
 project_planning.episodes         | table       | postgres              | TRIGGER
 project_planning.episodes         | table       | postgres              | TRUNCATE
 project_planning.episodes         | table       | postgres              | UPDATE
 project_planning.episodes         | table       | project_planning_role | DELETE
 project_planning.episodes         | table       | project_planning_role | INSERT
 project_planning.episodes         | table       | project_planning_role | SELECT
 project_planning.episodes         | table       | project_planning_role | UPDATE
 project_planning.organizations    | table       | postgres              | DELETE
 project_planning.organizations    | table       | postgres              | INSERT
 project_planning.organizations    | table       | postgres              | REFERENCES
 project_planning.organizations    | table       | postgres              | SELECT
 project_planning.organizations    | table       | postgres              | TRIGGER
 project_planning.organizations    | table       | postgres              | TRUNCATE
 project_planning.organizations    | table       | postgres              | UPDATE
 project_planning.organizations    | table       | project_planning_role | DELETE
 project_planning.organizations    | table       | project_planning_role | INSERT
 project_planning.organizations    | table       | project_planning_role | SELECT
 project_planning.organizations    | table       | project_planning_role | UPDATE
 project_planning.projects         | table       | postgres              | DELETE
 project_planning.projects         | table       | postgres              | INSERT
 project_planning.projects         | table       | postgres              | REFERENCES
 project_planning.projects         | table       | postgres              | SELECT
 project_planning.projects         | table       | postgres              | TRIGGER
 project_planning.projects         | table       | postgres              | TRUNCATE
 project_planning.projects         | table       | postgres              | UPDATE
 project_planning.projects         | table       | project_planning_role | DELETE
 project_planning.projects         | table       | project_planning_role | INSERT
 project_planning.projects         | table       | project_planning_role | SELECT
 project_planning.projects         | table       | project_planning_role | UPDATE
 project_planning.script_revisions | table       | postgres              | DELETE
 project_planning.script_revisions | table       | postgres              | INSERT
 project_planning.script_revisions | table       | postgres              | REFERENCES
 project_planning.script_revisions | table       | postgres              | SELECT
 project_planning.script_revisions | table       | postgres              | TRIGGER
 project_planning.script_revisions | table       | postgres              | TRUNCATE
 project_planning.script_revisions | table       | postgres              | UPDATE
 project_planning.script_revisions | table       | project_planning_role | DELETE
 project_planning.script_revisions | table       | project_planning_role | INSERT
 project_planning.script_revisions | table       | project_planning_role | SELECT
 project_planning.script_revisions | table       | project_planning_role | UPDATE
 public.alembic_version            | table       | asset_service_role    | SELECT
 public.alembic_version            | table       | postgres              | DELETE
 public.alembic_version            | table       | postgres              | INSERT
 public.alembic_version            | table       | postgres              | REFERENCES
 public.alembic_version            | table       | postgres              | SELECT
 public.alembic_version            | table       | postgres              | TRIGGER
 public.alembic_version            | table       | postgres              | TRUNCATE
 public.alembic_version            | table       | postgres              | UPDATE
 public.alembic_version            | table       | project_planning_role | SELECT
(145 rows)


### E10.5 全量有效权限（12 角色 × 全部业务表；只列有任一权限的行，未列出=全 f）
$ [ai_film_industrial] role × object：S/I/U/D/TRUNC/REF/TRIG
        rolname        |              object               | privs 
-----------------------+-----------------------------------+-------
 asset_service_role    | asset_service.asset_versions      | SIU
 asset_service_role    | asset_service.qc_reports          | SI
 asset_service_role    | audit.status_transitions          | SI
 asset_service_role    | auth.revoked_tokens               | S
 asset_service_role    | outbox.outbox_events_default      | SI
 asset_service_role    | outbox.outbox_events_y202611      | SI
 asset_service_role    | outbox.outbox_events_y202612      | SI
 asset_service_role    | public.alembic_version            | S
 outbox_relay_role     | outbox.outbox_events_default      | S
 outbox_relay_role     | outbox.outbox_events_y202611      | S
 outbox_relay_role     | outbox.outbox_events_y202612      | S
 project_planning_role | audit.status_transitions          | SI
 project_planning_role | auth.revoked_tokens               | S
 project_planning_role | outbox.outbox_events_default      | SI
 project_planning_role | outbox.outbox_events_y202611      | SI
 project_planning_role | outbox.outbox_events_y202612      | SI
 project_planning_role | project_planning.episodes         | SIU
 project_planning_role | project_planning.organizations    | S
 project_planning_role | project_planning.projects         | SIU
 project_planning_role | project_planning.script_revisions | SIU
 project_planning_role | public.alembic_version            | S
(21 rows)

$ [ai_film_verify] role × object：S/I/U/D/TRUNC/REF/TRIG
        rolname        |              object               | privs 
-----------------------+-----------------------------------+-------
 asset_service_role    | asset_service.asset_versions      | SIU
 asset_service_role    | asset_service.qc_reports          | SI
 asset_service_role    | auth.revoked_tokens               | S
 asset_service_role    | outbox.outbox_events_default      | SI
 asset_service_role    | outbox.outbox_events_y202611      | SI
 asset_service_role    | outbox.outbox_events_y202612      | SI
 asset_service_role    | public.alembic_version            | S
 outbox_relay_role     | outbox.outbox_events_default      | S
 outbox_relay_role     | outbox.outbox_events_y202611      | S
 outbox_relay_role     | outbox.outbox_events_y202612      | S
 project_planning_role | auth.revoked_tokens               | S
 project_planning_role | outbox.outbox_events_default      | SI
 project_planning_role | outbox.outbox_events_y202611      | SI
 project_planning_role | outbox.outbox_events_y202612      | SI
 project_planning_role | project_planning.episodes         | SIUD
 project_planning_role | project_planning.organizations    | SIUD
 project_planning_role | project_planning.projects         | SIUD
 project_planning_role | project_planning.script_revisions | SIUD
 project_planning_role | public.alembic_version            | S
(19 rows)


### E10.6 关键约束完整定义（pg_get_constraintdef）
$ [ai_film_industrial] 约束清单（业务 schema）
     nspname      |                conname                | contype |                tbl                |                                                                                                                                                                                   definition                                                                                                                                                                                   
------------------+---------------------------------------+---------+-----------------------------------+--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
 asset_service    | asset_versions_pkey                   | p       | asset_service.asset_versions      | PRIMARY KEY (entity_id)
 asset_service    | ck_asset_versions_status              | c       | asset_service.asset_versions      | CHECK (((status)::text = ANY ((ARRAY['APPROVED'::character varying, 'ARCHIVED'::character varying, 'GENERATED'::character varying, 'GOLD'::character varying, 'HUMAN_REVIEW'::character varying, 'QUARANTINED'::character varying, 'REJECTED'::character varying, 'REWORK'::character varying, 'SUPERSEDED'::character varying, 'TECHNICAL_QC'::character varying])::text[])))
 asset_service    | fk_asset_versions_project_tenant      | f       | asset_service.asset_versions      | FOREIGN KEY (project_id, tenant_id) REFERENCES project_planning.projects(entity_id, tenant_id)
 asset_service    | qc_reports_asset_version_id_fkey      | f       | asset_service.qc_reports          | FOREIGN KEY (asset_version_id) REFERENCES asset_service.asset_versions(entity_id)
 asset_service    | qc_reports_pkey                       | p       | asset_service.qc_reports          | PRIMARY KEY (qc_report_id)
 audit            | ck_status_transition_advances         | c       | audit.status_transitions          | CHECK (((from_status)::text <> (to_status)::text))
 audit            | ck_status_transition_version_positive | c       | audit.status_transitions          | CHECK ((entity_version >= 1))
 audit            | status_transitions_pkey               | p       | audit.status_transitions          | PRIMARY KEY (transition_id)
 audit            | trg_status_transitions_event_exists   | t       | audit.status_transitions          | TRIGGER DEFERRABLE INITIALLY DEFERRED
 audit            | uq_status_transition_version          | u       | audit.status_transitions          | UNIQUE (tenant_id, resource_type, resource_id, entity_version)
 auth             | revoked_tokens_pkey                   | p       | auth.revoked_tokens               | PRIMARY KEY (jti)
 outbox           | maintenance_runs_pkey                 | p       | outbox.maintenance_runs           | PRIMARY KEY (id)
 outbox           | outbox_events_default_pkey            | p       | outbox.outbox_events_default      | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_pkey                    | p       | outbox.outbox_events              | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_y202611_pkey            | p       | outbox.outbox_events_y202611      | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_y202612_pkey            | p       | outbox.outbox_events_y202612      | PRIMARY KEY (event_id, occurred_at)
 project_planning | ck_episodes_status                    | c       | project_planning.episodes         | CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'ARCHIVED'::character varying, 'DRAFT'::character varying])::text[])))
 project_planning | ck_projects_status                    | c       | project_planning.projects         | CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'ARCHIVED'::character varying, 'COMPLETED'::character varying, 'DRAFT'::character varying, 'ON_HOLD'::character varying])::text[])))
 project_planning | ck_script_revisions_status            | c       | project_planning.script_revisions | CHECK (((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'LOCKED'::character varying, 'LOCK_PENDING'::character varying, 'SUPERSEDED'::character varying])::text[])))
 project_planning | episodes_pkey                         | p       | project_planning.episodes         | PRIMARY KEY (entity_id)
 project_planning | fk_episodes_project                   | f       | project_planning.episodes         | FOREIGN KEY (project_id) REFERENCES project_planning.projects(entity_id)
 project_planning | fk_projects_organization_tenant       | f       | project_planning.projects         | FOREIGN KEY (organization_id, tenant_id) REFERENCES project_planning.organizations(organization_id, tenant_id)
 project_planning | fk_script_revisions_episode           | f       | project_planning.script_revisions | FOREIGN KEY (project_id, episode_ref) REFERENCES project_planning.episodes(project_id, episode_ref)
 project_planning | organizations_pkey                    | p       | project_planning.organizations    | PRIMARY KEY (organization_id)
 project_planning | projects_pkey                         | p       | project_planning.projects         | PRIMARY KEY (entity_id)
 project_planning | script_revisions_pkey                 | p       | project_planning.script_revisions | PRIMARY KEY (entity_id)
 project_planning | uq_organizations_owner_tenant         | u       | project_planning.organizations    | UNIQUE (organization_id, tenant_id)
 project_planning | uq_projects_entity_tenant             | u       | project_planning.projects         | UNIQUE (entity_id, tenant_id)
 project_planning | ux_episode_ref_per_project            | u       | project_planning.episodes         | UNIQUE (project_id, episode_ref)
 public           | alembic_version_pkc                   | p       | alembic_version                   | PRIMARY KEY (version_num)
(30 rows)

$ [ai_film_verify] 约束清单（业务 schema）
     nspname      |             conname              | contype |                tbl                |                                                   definition                                                   
------------------+----------------------------------+---------+-----------------------------------+----------------------------------------------------------------------------------------------------------------
 asset_service    | asset_versions_pkey              | p       | asset_service.asset_versions      | PRIMARY KEY (entity_id)
 asset_service    | fk_asset_versions_project_tenant | f       | asset_service.asset_versions      | FOREIGN KEY (project_id, tenant_id) REFERENCES project_planning.projects(entity_id, tenant_id)
 asset_service    | qc_reports_asset_version_id_fkey | f       | asset_service.qc_reports          | FOREIGN KEY (asset_version_id) REFERENCES asset_service.asset_versions(entity_id)
 asset_service    | qc_reports_pkey                  | p       | asset_service.qc_reports          | PRIMARY KEY (qc_report_id)
 auth             | revoked_tokens_pkey              | p       | auth.revoked_tokens               | PRIMARY KEY (jti)
 outbox           | maintenance_runs_pkey            | p       | outbox.maintenance_runs           | PRIMARY KEY (id)
 outbox           | outbox_events_default_pkey       | p       | outbox.outbox_events_default      | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_pkey               | p       | outbox.outbox_events              | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_y202611_pkey       | p       | outbox.outbox_events_y202611      | PRIMARY KEY (event_id, occurred_at)
 outbox           | outbox_events_y202612_pkey       | p       | outbox.outbox_events_y202612      | PRIMARY KEY (event_id, occurred_at)
 project_planning | episodes_pkey                    | p       | project_planning.episodes         | PRIMARY KEY (entity_id)
 project_planning | fk_episodes_project              | f       | project_planning.episodes         | FOREIGN KEY (project_id) REFERENCES project_planning.projects(entity_id)
 project_planning | fk_projects_organization_tenant  | f       | project_planning.projects         | FOREIGN KEY (organization_id, tenant_id) REFERENCES project_planning.organizations(organization_id, tenant_id)
 project_planning | fk_script_revisions_episode      | f       | project_planning.script_revisions | FOREIGN KEY (project_id, episode_ref) REFERENCES project_planning.episodes(project_id, episode_ref)
 project_planning | organizations_pkey               | p       | project_planning.organizations    | PRIMARY KEY (organization_id)
 project_planning | projects_pkey                    | p       | project_planning.projects         | PRIMARY KEY (entity_id)
 project_planning | script_revisions_pkey            | p       | project_planning.script_revisions | PRIMARY KEY (entity_id)
 project_planning | uq_organizations_owner_tenant    | u       | project_planning.organizations    | UNIQUE (organization_id, tenant_id)
 project_planning | uq_projects_entity_tenant        | u       | project_planning.projects         | UNIQUE (entity_id, tenant_id)
 project_planning | ux_episode_ref_per_project       | u       | project_planning.episodes         | UNIQUE (project_id, episode_ref)
 public           | alembic_version_pkc              | p       | alembic_version                   | PRIMARY KEY (version_num)
(21 rows)


### E10.7 触发器 + 触发器函数完整签名 + 启用状态
$ [ai_film_industrial] 非内部触发器
  tbl_schema   |        tbl         |               tgname                | tgenabled |            trigger_function            | fn_owner |                                                                                                trigger_def                                                                                                
---------------+--------------------+-------------------------------------+-----------+----------------------------------------+----------+-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
 asset_service | asset_versions     | trg_asset_versions_guard            | O         | asset_service.asset_versions_guard()   | postgres | CREATE TRIGGER trg_asset_versions_guard BEFORE DELETE OR UPDATE ON asset_service.asset_versions FOR EACH ROW EXECUTE FUNCTION asset_service.asset_versions_guard()
 asset_service | asset_versions     | trg_asset_versions_no_truncate      | O         | audit.reject_truncate()                | postgres | CREATE TRIGGER trg_asset_versions_no_truncate BEFORE TRUNCATE ON asset_service.asset_versions FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_truncate()
 asset_service | qc_reports         | trg_qc_reports_guard                | O         | asset_service.qc_reports_guard()       | postgres | CREATE TRIGGER trg_qc_reports_guard BEFORE DELETE OR UPDATE ON asset_service.qc_reports FOR EACH ROW EXECUTE FUNCTION asset_service.qc_reports_guard()
 asset_service | qc_reports         | trg_qc_reports_no_truncate          | O         | audit.reject_truncate()                | postgres | CREATE TRIGGER trg_qc_reports_no_truncate BEFORE TRUNCATE ON asset_service.qc_reports FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_truncate()
 audit         | status_transitions | trg_status_transitions_event_exists | O         | audit.assert_transition_event_exists() | postgres | CREATE CONSTRAINT TRIGGER trg_status_transitions_event_exists AFTER INSERT ON audit.status_transitions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit.assert_transition_event_exists()
 audit         | status_transitions | trg_status_transitions_guard        | O         | audit.reject_mutation()                | postgres | CREATE TRIGGER trg_status_transitions_guard BEFORE DELETE OR UPDATE ON audit.status_transitions FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation()
 audit         | status_transitions | trg_status_transitions_no_truncate  | O         | audit.reject_truncate()                | postgres | CREATE TRIGGER trg_status_transitions_no_truncate BEFORE TRUNCATE ON audit.status_transitions FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_truncate()
(7 rows)

$ [ai_film_verify] 非内部触发器
  tbl_schema   |      tbl       |          tgname          | tgenabled |           trigger_function           | fn_owner |                                                                            trigger_def                                                                             
---------------+----------------+--------------------------+-----------+--------------------------------------+----------+--------------------------------------------------------------------------------------------------------------------------------------------------------------------
 asset_service | asset_versions | trg_asset_versions_guard | O         | asset_service.asset_versions_guard() | postgres | CREATE TRIGGER trg_asset_versions_guard BEFORE DELETE OR UPDATE ON asset_service.asset_versions FOR EACH ROW EXECUTE FUNCTION asset_service.asset_versions_guard()
 asset_service | qc_reports     | trg_qc_reports_guard     | O         | asset_service.qc_reports_guard()     | postgres | CREATE TRIGGER trg_qc_reports_guard BEFORE DELETE OR UPDATE ON asset_service.qc_reports FOR EACH ROW EXECUTE FUNCTION asset_service.qc_reports_guard()
(2 rows)


### E10.8 索引完整定义（indexdef）
$ [ai_film_industrial] 业务索引
      schema      |          tbl          |                   index_name                    |                                                                              index_def                                                                               
------------------+-----------------------+-------------------------------------------------+----------------------------------------------------------------------------------------------------------------------------------------------------------------------
 asset_service    | asset_versions        | asset_versions_pkey                             | CREATE UNIQUE INDEX asset_versions_pkey ON asset_service.asset_versions USING btree (entity_id)
 asset_service    | asset_versions        | ix_asset_service_asset_versions_project         | CREATE INDEX ix_asset_service_asset_versions_project ON asset_service.asset_versions USING btree (project_id)
 asset_service    | asset_versions        | ix_asset_service_asset_versions_tenant_id       | CREATE INDEX ix_asset_service_asset_versions_tenant_id ON asset_service.asset_versions USING btree (tenant_id)
 asset_service    | asset_versions        | ix_asset_service_asset_versions_type_sha256     | CREATE INDEX ix_asset_service_asset_versions_type_sha256 ON asset_service.asset_versions USING btree (asset_type, sha256)
 asset_service    | qc_reports            | ix_asset_service_qc_reports_asset_version_id    | CREATE INDEX ix_asset_service_qc_reports_asset_version_id ON asset_service.qc_reports USING btree (asset_version_id)
 asset_service    | qc_reports            | ix_asset_service_qc_reports_tenant_id           | CREATE INDEX ix_asset_service_qc_reports_tenant_id ON asset_service.qc_reports USING btree (tenant_id)
 asset_service    | qc_reports            | qc_reports_pkey                                 | CREATE UNIQUE INDEX qc_reports_pkey ON asset_service.qc_reports USING btree (qc_report_id)
 audit            | status_transitions    | ix_status_transitions_resource_history          | CREATE INDEX ix_status_transitions_resource_history ON audit.status_transitions USING btree (tenant_id, resource_type, resource_id, occurred_at)
 audit            | status_transitions    | status_transitions_pkey                         | CREATE UNIQUE INDEX status_transitions_pkey ON audit.status_transitions USING btree (transition_id)
 audit            | status_transitions    | uq_status_transition_version                    | CREATE UNIQUE INDEX uq_status_transition_version ON audit.status_transitions USING btree (tenant_id, resource_type, resource_id, entity_version)
 auth             | revoked_tokens        | ix_auth_revoked_tokens_expires_at               | CREATE INDEX ix_auth_revoked_tokens_expires_at ON auth.revoked_tokens USING btree (expires_at)
 auth             | revoked_tokens        | revoked_tokens_pkey                             | CREATE UNIQUE INDEX revoked_tokens_pkey ON auth.revoked_tokens USING btree (jti)
 outbox           | maintenance_runs      | ix_outbox_maintenance_runs_started              | CREATE INDEX ix_outbox_maintenance_runs_started ON outbox.maintenance_runs USING btree (started_at)
 outbox           | maintenance_runs      | maintenance_runs_pkey                           | CREATE UNIQUE INDEX maintenance_runs_pkey ON outbox.maintenance_runs USING btree (id)
 outbox           | outbox_events         | ix_outbox_aggregate                             | CREATE INDEX ix_outbox_aggregate ON ONLY outbox.outbox_events USING btree (aggregate_id)
 outbox           | outbox_events         | ix_outbox_published                             | CREATE INDEX ix_outbox_published ON ONLY outbox.outbox_events USING btree (published_at)
 outbox           | outbox_events         | ix_outbox_tenant                                | CREATE INDEX ix_outbox_tenant ON ONLY outbox.outbox_events USING btree (tenant_id)
 outbox           | outbox_events         | outbox_events_pkey                              | CREATE UNIQUE INDEX outbox_events_pkey ON ONLY outbox.outbox_events USING btree (event_id, occurred_at)
 outbox           | outbox_events_default | outbox_events_default_aggregate_id_idx          | CREATE INDEX outbox_events_default_aggregate_id_idx ON outbox.outbox_events_default USING btree (aggregate_id)
 outbox           | outbox_events_default | outbox_events_default_pkey                      | CREATE UNIQUE INDEX outbox_events_default_pkey ON outbox.outbox_events_default USING btree (event_id, occurred_at)
 outbox           | outbox_events_default | outbox_events_default_published_at_idx          | CREATE INDEX outbox_events_default_published_at_idx ON outbox.outbox_events_default USING btree (published_at)
 outbox           | outbox_events_default | outbox_events_default_tenant_id_idx             | CREATE INDEX outbox_events_default_tenant_id_idx ON outbox.outbox_events_default USING btree (tenant_id)
 outbox           | outbox_events_y202611 | outbox_events_y202611_aggregate_id_idx          | CREATE INDEX outbox_events_y202611_aggregate_id_idx ON outbox.outbox_events_y202611 USING btree (aggregate_id)
 outbox           | outbox_events_y202611 | outbox_events_y202611_pkey                      | CREATE UNIQUE INDEX outbox_events_y202611_pkey ON outbox.outbox_events_y202611 USING btree (event_id, occurred_at)
 outbox           | outbox_events_y202611 | outbox_events_y202611_published_at_idx          | CREATE INDEX outbox_events_y202611_published_at_idx ON outbox.outbox_events_y202611 USING btree (published_at)
 outbox           | outbox_events_y202611 | outbox_events_y202611_tenant_id_idx             | CREATE INDEX outbox_events_y202611_tenant_id_idx ON outbox.outbox_events_y202611 USING btree (tenant_id)
 outbox           | outbox_events_y202612 | outbox_events_y202612_aggregate_id_idx          | CREATE INDEX outbox_events_y202612_aggregate_id_idx ON outbox.outbox_events_y202612 USING btree (aggregate_id)
 outbox           | outbox_events_y202612 | outbox_events_y202612_pkey                      | CREATE UNIQUE INDEX outbox_events_y202612_pkey ON outbox.outbox_events_y202612 USING btree (event_id, occurred_at)
 outbox           | outbox_events_y202612 | outbox_events_y202612_published_at_idx          | CREATE INDEX outbox_events_y202612_published_at_idx ON outbox.outbox_events_y202612 USING btree (published_at)
 outbox           | outbox_events_y202612 | outbox_events_y202612_tenant_id_idx             | CREATE INDEX outbox_events_y202612_tenant_id_idx ON outbox.outbox_events_y202612 USING btree (tenant_id)
 project_planning | episodes              | episodes_pkey                                   | CREATE UNIQUE INDEX episodes_pkey ON project_planning.episodes USING btree (entity_id)
 project_planning | episodes              | ix_project_planning_episodes_project_id         | CREATE INDEX ix_project_planning_episodes_project_id ON project_planning.episodes USING btree (project_id)
 project_planning | episodes              | ix_project_planning_episodes_tenant_id          | CREATE INDEX ix_project_planning_episodes_tenant_id ON project_planning.episodes USING btree (tenant_id)
 project_planning | episodes              | ux_episode_ref_per_project                      | CREATE UNIQUE INDEX ux_episode_ref_per_project ON project_planning.episodes USING btree (project_id, episode_ref)
 project_planning | organizations         | ix_project_planning_organizations_tenant_id     | CREATE INDEX ix_project_planning_organizations_tenant_id ON project_planning.organizations USING btree (tenant_id)
 project_planning | organizations         | organizations_pkey                              | CREATE UNIQUE INDEX organizations_pkey ON project_planning.organizations USING btree (organization_id)
 project_planning | organizations         | uq_organizations_owner_tenant                   | CREATE UNIQUE INDEX uq_organizations_owner_tenant ON project_planning.organizations USING btree (organization_id, tenant_id)
 project_planning | projects              | ix_project_planning_projects_tenant_id          | CREATE INDEX ix_project_planning_projects_tenant_id ON project_planning.projects USING btree (tenant_id)
 project_planning | projects              | projects_pkey                                   | CREATE UNIQUE INDEX projects_pkey ON project_planning.projects USING btree (entity_id)
 project_planning | projects              | uq_projects_entity_tenant                       | CREATE UNIQUE INDEX uq_projects_entity_tenant ON project_planning.projects USING btree (entity_id, tenant_id)
 project_planning | script_revisions      | ix_project_planning_script_revisions_project_id | CREATE INDEX ix_project_planning_script_revisions_project_id ON project_planning.script_revisions USING btree (project_id)
 project_planning | script_revisions      | ix_project_planning_script_revisions_tenant_id  | CREATE INDEX ix_project_planning_script_revisions_tenant_id ON project_planning.script_revisions USING btree (tenant_id)
 project_planning | script_revisions      | script_revisions_pkey                           | CREATE UNIQUE INDEX script_revisions_pkey ON project_planning.script_revisions USING btree (entity_id)
 project_planning | script_revisions      | ux_script_active_version                        | CREATE UNIQUE INDEX ux_script_active_version ON project_planning.script_revisions USING btree (project_id, episode_ref) WHERE ((status)::text <> 'SUPERSEDED'::text)
(44 rows)

$ [ai_film_verify] 业务索引
      schema      |          tbl          |                   index_name                    |                                                                              index_def                                                                               
------------------+-----------------------+-------------------------------------------------+----------------------------------------------------------------------------------------------------------------------------------------------------------------------
 asset_service    | asset_versions        | asset_versions_pkey                             | CREATE UNIQUE INDEX asset_versions_pkey ON asset_service.asset_versions USING btree (entity_id)
 asset_service    | asset_versions        | ix_asset_service_asset_versions_project         | CREATE INDEX ix_asset_service_asset_versions_project ON asset_service.asset_versions USING btree (project_id)
 asset_service    | asset_versions        | ix_asset_service_asset_versions_tenant_id       | CREATE INDEX ix_asset_service_asset_versions_tenant_id ON asset_service.asset_versions USING btree (tenant_id)
 asset_service    | asset_versions        | ix_asset_service_asset_versions_type_sha256     | CREATE INDEX ix_asset_service_asset_versions_type_sha256 ON asset_service.asset_versions USING btree (asset_type, sha256)
 asset_service    | qc_reports            | ix_asset_service_qc_reports_asset_version_id    | CREATE INDEX ix_asset_service_qc_reports_asset_version_id ON asset_service.qc_reports USING btree (asset_version_id)
 asset_service    | qc_reports            | ix_asset_service_qc_reports_tenant_id           | CREATE INDEX ix_asset_service_qc_reports_tenant_id ON asset_service.qc_reports USING btree (tenant_id)
 asset_service    | qc_reports            | qc_reports_pkey                                 | CREATE UNIQUE INDEX qc_reports_pkey ON asset_service.qc_reports USING btree (qc_report_id)
 auth             | revoked_tokens        | ix_auth_revoked_tokens_expires_at               | CREATE INDEX ix_auth_revoked_tokens_expires_at ON auth.revoked_tokens USING btree (expires_at)
 auth             | revoked_tokens        | revoked_tokens_pkey                             | CREATE UNIQUE INDEX revoked_tokens_pkey ON auth.revoked_tokens USING btree (jti)
 outbox           | maintenance_runs      | ix_outbox_maintenance_runs_started              | CREATE INDEX ix_outbox_maintenance_runs_started ON outbox.maintenance_runs USING btree (started_at)
 outbox           | maintenance_runs      | maintenance_runs_pkey                           | CREATE UNIQUE INDEX maintenance_runs_pkey ON outbox.maintenance_runs USING btree (id)
 outbox           | outbox_events         | ix_outbox_aggregate                             | CREATE INDEX ix_outbox_aggregate ON ONLY outbox.outbox_events USING btree (aggregate_id)
 outbox           | outbox_events         | ix_outbox_published                             | CREATE INDEX ix_outbox_published ON ONLY outbox.outbox_events USING btree (published_at)
 outbox           | outbox_events         | ix_outbox_tenant                                | CREATE INDEX ix_outbox_tenant ON ONLY outbox.outbox_events USING btree (tenant_id)
 outbox           | outbox_events         | outbox_events_pkey                              | CREATE UNIQUE INDEX outbox_events_pkey ON ONLY outbox.outbox_events USING btree (event_id, occurred_at)
 outbox           | outbox_events_default | outbox_events_default_aggregate_id_idx          | CREATE INDEX outbox_events_default_aggregate_id_idx ON outbox.outbox_events_default USING btree (aggregate_id)
 outbox           | outbox_events_default | outbox_events_default_pkey                      | CREATE UNIQUE INDEX outbox_events_default_pkey ON outbox.outbox_events_default USING btree (event_id, occurred_at)
 outbox           | outbox_events_default | outbox_events_default_published_at_idx          | CREATE INDEX outbox_events_default_published_at_idx ON outbox.outbox_events_default USING btree (published_at)
 outbox           | outbox_events_default | outbox_events_default_tenant_id_idx             | CREATE INDEX outbox_events_default_tenant_id_idx ON outbox.outbox_events_default USING btree (tenant_id)
 outbox           | outbox_events_y202611 | outbox_events_y202611_aggregate_id_idx          | CREATE INDEX outbox_events_y202611_aggregate_id_idx ON outbox.outbox_events_y202611 USING btree (aggregate_id)
 outbox           | outbox_events_y202611 | outbox_events_y202611_pkey                      | CREATE UNIQUE INDEX outbox_events_y202611_pkey ON outbox.outbox_events_y202611 USING btree (event_id, occurred_at)
 outbox           | outbox_events_y202611 | outbox_events_y202611_published_at_idx          | CREATE INDEX outbox_events_y202611_published_at_idx ON outbox.outbox_events_y202611 USING btree (published_at)
 outbox           | outbox_events_y202611 | outbox_events_y202611_tenant_id_idx             | CREATE INDEX outbox_events_y202611_tenant_id_idx ON outbox.outbox_events_y202611 USING btree (tenant_id)
 outbox           | outbox_events_y202612 | outbox_events_y202612_aggregate_id_idx          | CREATE INDEX outbox_events_y202612_aggregate_id_idx ON outbox.outbox_events_y202612 USING btree (aggregate_id)
 outbox           | outbox_events_y202612 | outbox_events_y202612_pkey                      | CREATE UNIQUE INDEX outbox_events_y202612_pkey ON outbox.outbox_events_y202612 USING btree (event_id, occurred_at)
 outbox           | outbox_events_y202612 | outbox_events_y202612_published_at_idx          | CREATE INDEX outbox_events_y202612_published_at_idx ON outbox.outbox_events_y202612 USING btree (published_at)
 outbox           | outbox_events_y202612 | outbox_events_y202612_tenant_id_idx             | CREATE INDEX outbox_events_y202612_tenant_id_idx ON outbox.outbox_events_y202612 USING btree (tenant_id)
 project_planning | episodes              | episodes_pkey                                   | CREATE UNIQUE INDEX episodes_pkey ON project_planning.episodes USING btree (entity_id)
 project_planning | episodes              | ix_project_planning_episodes_project_id         | CREATE INDEX ix_project_planning_episodes_project_id ON project_planning.episodes USING btree (project_id)
 project_planning | episodes              | ix_project_planning_episodes_tenant_id          | CREATE INDEX ix_project_planning_episodes_tenant_id ON project_planning.episodes USING btree (tenant_id)
 project_planning | episodes              | ux_episode_ref_per_project                      | CREATE UNIQUE INDEX ux_episode_ref_per_project ON project_planning.episodes USING btree (project_id, episode_ref)
 project_planning | organizations         | ix_project_planning_organizations_tenant_id     | CREATE INDEX ix_project_planning_organizations_tenant_id ON project_planning.organizations USING btree (tenant_id)
 project_planning | organizations         | organizations_pkey                              | CREATE UNIQUE INDEX organizations_pkey ON project_planning.organizations USING btree (organization_id)
 project_planning | organizations         | uq_organizations_owner_tenant                   | CREATE UNIQUE INDEX uq_organizations_owner_tenant ON project_planning.organizations USING btree (organization_id, tenant_id)
 project_planning | projects              | ix_project_planning_projects_tenant_id          | CREATE INDEX ix_project_planning_projects_tenant_id ON project_planning.projects USING btree (tenant_id)
 project_planning | projects              | projects_pkey                                   | CREATE UNIQUE INDEX projects_pkey ON project_planning.projects USING btree (entity_id)
 project_planning | projects              | uq_projects_entity_tenant                       | CREATE UNIQUE INDEX uq_projects_entity_tenant ON project_planning.projects USING btree (entity_id, tenant_id)
 project_planning | script_revisions      | ix_project_planning_script_revisions_project_id | CREATE INDEX ix_project_planning_script_revisions_project_id ON project_planning.script_revisions USING btree (project_id)
 project_planning | script_revisions      | ix_project_planning_script_revisions_tenant_id  | CREATE INDEX ix_project_planning_script_revisions_tenant_id ON project_planning.script_revisions USING btree (tenant_id)
 project_planning | script_revisions      | script_revisions_pkey                           | CREATE UNIQUE INDEX script_revisions_pkey ON project_planning.script_revisions USING btree (entity_id)
 project_planning | script_revisions      | ux_script_active_version                        | CREATE UNIQUE INDEX ux_script_active_version ON project_planning.script_revisions USING btree (project_id, episode_ref) WHERE ((status)::text <> 'SUPERSEDED'::text)
(41 rows)


### E10.9 Git 取证身份固定（完整 commit SHA，非可移动分支名）
$ main = 5352d1979421f41bfb9943774378671cfe94de71 ; branch = 59b13af73640d10b81703b514202173976c432c5
$ git rev-parse main / origin/feat/identity-data-ownership
5352d1979421f41bfb9943774378671cfe94de71
59b13af73640d10b81703b514202173976c432c5
$ main 侧：在固定 commit 5352d1979421f41bfb9943774378671cfe94de71 上计算 sha256 + blob
-- data/migrations/versions/b3c4d5e6f7a8_asset_project_ownership.py
sha256: fbc0da26e3caf0fc99ef8464fb780d231451ed83362a2bf004f94a50382a4823
blob:   dbc51ce794d95cd62245edde52ab7f5ca3cb64dd
-- data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py
sha256: 0caf8be5f0bd19c10d7c8349c24be9532547e1925c1a55df99951a5a9317630c
blob:   dbae1fc43715b33c6334a86e25debb7a915f69ed
-- data/migrations/versions/9e3b4c5d6e7f_create_app_role_and_grants.py
sha256: 76e330519a95d2981b1b3b414294d3c207d62ace3406bb529ba63f2b31af3418
blob:   3b5b6753dc4876622e0a74a5fa0f84e91f8fe145
$ 分支侧：在固定 commit 59b13af73640d10b81703b514202173976c432c5 上计算 sha256 + blob
-- data/migrations/versions/b3c4d5e6f7a8_asset_status_transitions.py
sha256: 0d1c44d24f10d3b231a29457fd5968c35b8a71b43a446ae0a2d15957f7a6e606
blob:   8899ef436376c211af3b91a403e7d482a9afa89d
-- data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py
sha256: 2a8d7d2395c5d936b6e6be2be393c437a781f664c68d0f26773d7fe210f27c8f
blob:   816a0f1c337601d36b2309646622c208cc56df81
-- data/migrations/versions/9e3b4c5d6e7f_create_app_role_and_grants.py
sha256: 57c3d775ac07e90a8ecce53e39449f226c3cd68d38f9c984f2d474130ad81497
blob:   41faff78a58e068775db8befa6745c8d5bc99691

### E10.10 说明：proacl 展开中的 OID=0 即 PUBLIC
> psql 以 `unknown (OID=0)` 显示 `pg_get_userbyid(0)`；grantee=0 在 PostgreSQL 中就是 PUBLIC。
> 因此 E10.3 中带 `unknown (OID=0)` 的行 = **PUBLIC 拥有 EXECUTE**（默认 ACL 展开的结果）。

### E10.11 关键表列与类型（RLS scope 维度必须由真实列决定）
$ [ai_film_industrial] 列清单（asset_versions / outbox_events / status_transitions / projects）
            object            |                                                                                                                                                                                                                                                                                                              columns                                                                                                                                                                                                                                                                                                               
------------------------------+------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
 asset_service.asset_versions | entity_id:character varying, entity_type:character varying, schema_version:integer, entity_version:integer, status:character varying, owner_module:character varying, tenant_id:uuid, created_at:timestamp with time zone, created_by:character varying, updated_at:timestamp with time zone, audit_ref:character varying, asset_type:character varying, sha256:character varying, storage_ref:jsonb, source_asset_version_ids:jsonb, rights_manifest_ref:character varying, adapter_id:character varying, tool_product:character varying, operator_ref:character varying, version:character varying, project_id:character varying
 audit.status_transitions     | transition_id:uuid, tenant_id:uuid, organization_id:uuid, resource_type:character varying, resource_id:character varying, entity_version:integer, from_status:character varying, to_status:character varying, owner_action:character varying, actor_subject:character varying, authority_ref:character varying, evidence_refs:ARRAY, occurred_at:timestamp with time zone, event_id:uuid
 outbox.outbox_events         | event_id:uuid, aggregate_type:character varying, aggregate_id:character varying, tenant_id:uuid, event_type:character varying, payload:jsonb, occurred_at:timestamp with time zone, published_at:timestamp with time zone, attempts:integer, next_attempt_at:timestamp with time zone, dead_lettered_at:timestamp with time zone
 project_planning.projects    | entity_id:character varying, entity_type:character varying, schema_version:integer, entity_version:integer, status:character varying, owner_module:character varying, created_at:timestamp with time zone, created_by:character varying, updated_at:timestamp with time zone, audit_ref:character varying, project_name:character varying, project_tier:character varying, tenant_id:uuid, organization_id:uuid
(4 rows)

$ [ai_film_verify] 列清单（asset_versions / outbox_events / status_transitions / projects）
            object            |                                                                                                                                                                                                                                                                                                              columns                                                                                                                                                                                                                                                                                                               
------------------------------+------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
 asset_service.asset_versions | entity_id:character varying, entity_type:character varying, schema_version:integer, entity_version:integer, status:character varying, owner_module:character varying, tenant_id:uuid, created_at:timestamp with time zone, created_by:character varying, updated_at:timestamp with time zone, audit_ref:character varying, asset_type:character varying, sha256:character varying, storage_ref:jsonb, source_asset_version_ids:jsonb, rights_manifest_ref:character varying, adapter_id:character varying, tool_product:character varying, operator_ref:character varying, version:character varying, project_id:character varying
 outbox.outbox_events         | event_id:uuid, aggregate_type:character varying, aggregate_id:character varying, tenant_id:uuid, event_type:character varying, payload:jsonb, occurred_at:timestamp with time zone, published_at:timestamp with time zone, attempts:integer, next_attempt_at:timestamp with time zone, dead_lettered_at:timestamp with time zone
 project_planning.projects    | entity_id:character varying, entity_type:character varying, schema_version:integer, entity_version:integer, status:character varying, owner_module:character varying, created_at:timestamp with time zone, created_by:character varying, updated_at:timestamp with time zone, audit_ref:character varying, project_name:character varying, project_tier:character varying, tenant_id:uuid, organization_id:uuid
(3 rows)


---

## E11 GUC 数组语义与真实键类型（v1.4.2 第五轮复审新增）

> 采集目的：第五轮复审指出 D.6.0c 的 `string_to_array` 解析错误。追问根因时需回答两个问题：
> ① 正确的解析方式是什么；② **`uuid[]` 这个逻辑类型本身是否成立**。
> 采集方式：全部为 `SELECT`（纯表达式 + 只读表扫描，事务内 `BEGIN READ ONLY` + `ROLLBACK`）。
> **未创建/修改任何对象，未执行 GRANT/REVOKE，未写入业务表**。会话级 `set_config(..., is_local=true)` 随事务回滚失效。
> 实例：`ai_film_industrial`，PostgreSQL 16.15（Debian），端口 15432。
> **本节含 3 条预期报错的输出**（E11.2 变体 C、E11.3 空串、E11.3 畸形文本）——这是**负向探针**，
> 目的是证明「非法输入会被拒绝而不是被静默吞掉」，**不是采集失败**。全档其余小节均为零报错。

### E11.0 实例与采集身份

```sql
SELECT version();
```
```
PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit
```

### E11.1 解析语义：`string_to_array` vs 原生数组输入（纯表达式）

```sql
-- A. v1.4.1 生成器的做法
SELECT string_to_array('{11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222}', ',') AS wrong_split;
```
```
wrong_split
{"{11111111-1111-1111-1111-111111111111","22222222-2222-2222-2222-222222222222}"}
```
> 两个元素分别以 `{` 结尾/开头——花括号残留在元素内，与任何 `project_id` 值都不相等。

```sql
-- B. 原生数组输入语法（正确）
SELECT '{11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222}'::uuid[] AS native_parse;
```
```
native_parse
{11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222}
```

```sql
-- C. 元素转为 text 后与 varchar 列同域比较
SELECT '{11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222}'::uuid[]::text[] AS to_text_array;
```
```
to_text_array
{11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222}
```

```sql
SELECT '11111111-1111-1111-1111-111111111111'::varchar
       = ANY ('{11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222}'::uuid[]::text[]) AS varchar_vs_textarr;
```
```
varchar_vs_textarr
t
```

### E11.2 真实键类型：`uuid[]` 假设不成立（决定性证据）

```sql
SELECT table_schema, table_name, column_name, data_type, character_maximum_length
  FROM information_schema.columns
 WHERE column_name IN ('project_id','organization_id','tenant_id')
   AND table_schema IN ('asset_service','project_planning','outbox','audit')
 ORDER BY 1,2,3;
```
```
 table_schema     | table_name        | column_name     | data_type         | character_maximum_length
------------------+-------------------+-----------------+-------------------+--------------------------
 asset_service    | asset_versions    | project_id      | character varying | 64
 asset_service    | asset_versions    | tenant_id       | uuid              |
 asset_service    | qc_reports        | tenant_id       | uuid              |
 audit            | status_transitions| organization_id | uuid              |
 audit            | status_transitions| tenant_id       | uuid              |
 outbox           | outbox_events     | tenant_id       | uuid              |
 outbox           | outbox_events_default | tenant_id   | uuid              |
 outbox           | outbox_events_y202611 | tenant_id   | uuid              |
 outbox           | outbox_events_y202612 | tenant_id   | uuid              |
 project_planning | episodes          | project_id      | character varying | 64
 project_planning | organizations     | organization_id | uuid              |
 project_planning | organizations     | tenant_id       | uuid              |
 project_planning | projects          | organization_id | uuid              |
 project_planning | projects          | tenant_id       | uuid              |
 project_planning | script_revisions  | project_id      | character varying | 64
```
> 关键：**`project_id` 是 `character varying(64)`，不是 `uuid`**；`tenant_id`/`organization_id` 才是 `uuid`。
> 这说明「项目 GUC 用 `uuid[]`、租户/组织 GUC 用 `uuid`」的对称假设从一开始就不成立。

```sql
SELECT count(*) AS rows_total, count(DISTINCT tenant_id) AS tenants, count(DISTINCT project_id) AS projects
  FROM asset_service.asset_versions;
```
```
 rows_total | tenants | projects
------------+---------+----------
        971 |       2 |      288
```

```sql
SELECT project_id, count(*) FROM asset_service.asset_versions GROUP BY 1 ORDER BY 2 DESC LIMIT 5;
```
```
 project_id                       | count
----------------------------------+-------
 project-1                        |   684
 2140479b0e494e6baf17ea8f4a938224 |     1
 dc8a7f4fabe0459c912c71c26afb0e9c |     1
 8913928d5de94db59ae9b76d709ce3c5 |     1
 b4feb9a7591b4b76b99608ec31afa6ec |     1
```

```sql
SELECT count(*) FILTER (WHERE project_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-') AS uuid_like,
       count(*) AS total
  FROM asset_service.asset_versions;
```
```
 uuid_like | total
-----------+-------
         0 |   971
```
> **971 行中 0 行是 UUID 形态**：取值形如 `project-1`（684 行）与 32 位无连字符 hex。
> 因此 `app.current_projects` 若声明为 `uuid[]`，注入 `project-1` 必然 22P02（见变体 C）。

```sql
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conrelid='asset_service.asset_versions'::regclass AND contype='f';
```
```
 conname                         | pg_get_constraintdef
---------------------------------+---------------------------------------------------------------
 fk_asset_versions_project_tenant | FOREIGN KEY (project_id, tenant_id)
                                   REFERENCES project_planning.projects(entity_id, tenant_id)
```
> 项目身份的引用列是 `projects.entity_id`，同为 `varchar(64)`：
```sql
SELECT data_type, character_maximum_length FROM information_schema.columns
 WHERE table_schema='project_planning' AND table_name='projects' AND column_name IN ('entity_id','project_id');
SELECT entity_id FROM project_planning.projects ORDER BY 1 LIMIT 3;
```
```
 data_type         | character_maximum_length
-------------------+--------------------------
 character varying | 64

 entity_id
----------------------------------
 002b20d084f74f6080546e62bec146cb
 002c6cbba5bc4b41999babe49445c158
 003c6d20135f4c9facc63a9b99ddda71
```

**变体 C：`uuid[]` 假设的失败现场**

```sql
SELECT 'project-1'::varchar = ANY ('{project-1}'::uuid[]::text[]) AS matched;
```
```
ERROR:  invalid input syntax for type uuid: "project-1"
LINE 1: ...st_probe' AS variant, 'project-1'::varchar = ANY ('{project-...
                                                             ^
```
> 这是 `22P02`。它证明：**只要把 `project-1` 放进 `uuid[]`，无论后续怎么转 text 都会先在这一步失败**。
> 所以 v1.4.1 的问题不是「解析函数选错」，而是**逻辑类型与真实键类型不符**。

### E11.3 `text[]` 的边界行为（fail-closed 三态）

```sql
SELECT current_setting('app.current_projects', true) AS unset_guc,
       pg_typeof(current_setting('app.current_projects', true)) AS t;
```
```
 unset_guc |   t
-----------+-------
           | text
```
> GUC 未设置 ⇒ `NULL`（不是空串）。据此 `= ANY(NULL)` 为 NULL ⇒ 0 行（fail-closed）。

```sql
SELECT (NULL::text[]) IS NULL AS null_arr_is_null,
       ('{}'::text[]) AS empty_arr,
       ('11111111-1111-1111-1111-111111111111'::varchar = ANY ('{}'::text[])) AS empty_any;
```
```
 null_arr_is_null | empty_arr | empty_any
------------------+-----------+-----------
 t                | {}        | f
```
> 空集合 `'{}'` ⇒ `false` ⇒ 0 行（**不等于"全部可见"**）。

```sql
SELECT count(*) FROM (VALUES ('11111111-1111-1111-1111-111111111111'::varchar)) v(pid)
 WHERE pid = ANY (NULL::text[]);
```
```
 count
-------
     0
```
> NULL 数组 ⇒ 谓词为 NULL ⇒ 0 行。fail-closed 由 PG 三值逻辑天然保证，**不需要额外函数**。

```sql
SELECT ''::text[];
```
```
ERROR:  malformed array literal: ""
LINE 1: SELECT ''::text[];
               ^
DETAIL:  Array value must start with "{" or dimension information.
```

```sql
SELECT '{project-1'::text[];
```
```
ERROR:  malformed array literal: "{project-1"
LINE 1: SELECT '{project-1'::text[];
               ^
```
> 畸形文本直接报错，**不会被静默转成空集合**——与 §4.2「非法即拒」一致。

```sql
SELECT '{a,b}'::text[] AS three_elements;
SELECT '{"a,b"}'::text[] AS one_element;
```
```
 three_elements
----------------
 {a,b}

 one_element
-------------
 {"a,b"}
```
> 只有**含逗号**的元素才需要引号。而 §4.2 的元素编码约束 `^[A-Za-z0-9_-]{1,64}$` 排除了 `,` `{` `}` `"` `\`，
> 因此编码器产出的数组文本**永不需要转义**，原生解析无歧义。

### E11.4 真实表上的效果对比（决定性验收证据）

```sql
BEGIN READ ONLY;
SELECT set_config('app.current_projects', '{project-1}', true) AS injected;

-- A. v1.4.1 生成器产物
SELECT 'A_v1.4.1_string_to_array' AS variant, count(*) AS rows_visible
  FROM asset_service.asset_versions
 WHERE project_id = ANY (string_to_array(current_setting('app.current_projects', true), ',')::text[]);

-- B. v1.4.2 修正产物
SELECT 'B_v1.4.2_native_array' AS variant, count(*) AS rows_visible
  FROM asset_service.asset_versions
 WHERE project_id = ANY (current_setting('app.current_projects', true)::text[]);
ROLLBACK;
```
```
BEGIN
    injected
---------------
 {project-1}

        variant          | rows_visible
-------------------------+--------------
 A_v1.4.1_string_to_array |            0

        variant         | rows_visible
------------------------+--------------
 B_v1.4.2_native_array   |          684
```
> **同一上下文、同一张表、同一批数据**：v1.4.1 的表达式返回 **0 行**（合法项目完全不可见——正是复审预言的
> 「严重时表现为合法项目也查不到」），v1.4.2 的表达式返回 **684 行**（与 `project-1` 的实际行数一致）。
> 这条对比是本轮最硬的证据：它同时否证了旧解析方式与 `uuid[]` 假设。

### E11.5 分区结构与 `relkind` 缺口（支撑 C.5.3 ⑨ 修正）

```sql
SELECT c.relname, c.relkind, pg_get_expr(c.relpartbound,c.oid) AS bound
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='outbox' AND c.relname LIKE 'outbox_events%' ORDER BY 1;
```
```
        relname             | relkind |                        bound
----------------------------+---------+------------------------------------------------------
 outbox_events              | p       |
 outbox_events_default      | r       | DEFAULT
 outbox_events_default_...  | i       |            (三个索引，省略)
 outbox_events_pkey         | I       |
 outbox_events_y202611      | r       | FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00')
 outbox_events_y202612      | r       | FOR VALUES FROM ('2026-12-01 00:00:00+00') TO ('2027-01-01 00:00:00+00')
```
> 父表是 `relkind='p'`（partitioned），子分区是 `relkind='r'`，父表主键是 `relkind='I'`（partitioned index）。
> **v1.4.1 的 `WHERE c.relkind='r'` 会把父表整行漏掉**，因此无法复现 C.3.4 认定的「父表有 `app_role` SELECT、
> 三分区没有」。v1.4.2 已改为 `IN ('r','p')` 并新增 ⑬ 分区父子 ACL 对照查询。

### E11.6 outbox / audit 的列结构（支撑 D.2.2 业务裁定）

```sql
SELECT table_name, ordinal_position, column_name, data_type
  FROM information_schema.columns
 WHERE table_schema='outbox' AND table_name='outbox_events' ORDER BY 2;
```
```
 1  event_id         | uuid
 2  aggregate_type   | character varying
 3  aggregate_id     | character varying
 4  tenant_id        | uuid
 5  event_type       | character varying
 6  payload          | jsonb
 7  occurred_at      | timestamp with time zone
 8  published_at     | timestamp with time zone
 9  attempts         | integer
10  next_attempt_at  | timestamp with time zone
11  dead_lettered_at | timestamp with time zone
```
> **无 `project_id` 列；有 `payload jsonb`** —— 结构上 payload 可装任意内容，而 RLS 只能到租户级。
> 这就是 D.2.2 裁定 1（payload 封闭白名单 + 消费者回源）的事实依据：不是"漏了 project_id"，
> 而是**该表在设计上是传输层，业务内容必须在契约层禁止进入**。

```sql
SELECT table_name, ordinal_position, column_name, data_type
  FROM information_schema.columns
 WHERE table_schema='audit' AND table_name='status_transitions' ORDER BY 2;
```
```
 1  transition_id     | uuid
 2  tenant_id         | uuid
 3  organization_id   | uuid
 4  resource_type     | character varying
 5  resource_id       | character varying
 6  entity_version    | integer
 7  from_status       | character varying
 8  to_status         | character varying
 9  owner_action      | character varying
10  actor_subject     | character varying
11  authority_ref     | character varying
12  evidence_refs     | ARRAY
13  occurred_at       | timestamp with time zone
14  event_id          | uuid
```
> **无 `project_id` 列；全部为迁移元数据，无业务正文** —— 支撑 D.2.2 裁定 2（有意为租户+组织级）。

### E11.7 E11 结论（可独立复核的三句话）

1. **解析**：`string_to_array` 对 PostgreSQL 数组文本是错误解析（E11.1 A）；正确方式是原生数组输入语法
   `::<type>[]`（E11.1 B/C）。
2. **类型**：`uuid[]` 与真实键类型不符（E11.2，0/971 行为 UUID 形态，`project-1` 触发 22P02）；
   逻辑类型必须是 `text[]`。
3. **效果**：同上下文下旧表达式 0 行、新表达式 684 行（E11.4）——这不是"更好"，是"能否看见数据"的区别。

> 局限（必须写明）：以上结论均在 **`postgres` 超级用户会话**下取得，**不构成安全验证**；
> RLS 行为的真实验证必须在阶段 6 用真实域角色连接执行（§12 E1），且必须区分 RLS 测试主体（有 SELECT）
> 与 ACL 测试主体（无权限）（D.6.0c 派生 2a/2b）。

---

## E12 补齐查询的首次实跑（⑨⑬⑭⑮ + 递归闭包 + 角色属性）

> 采集目的：第五轮复审指出 v1.4.1 的有效权限查询漏分区父表、缺递归闭包与全对象类型有效权限。
> v1.4.2 补齐了查询（⑨ 改 `IN ('r','p')`、新增 ⑬⑭⑮、C.5.2 补递归闭包），**但从未跑过**。
> 本节是这些新查询的**首次真实执行**，用于回答「补齐后的查询能否复现文档自己认定的事实」。
>
> 采集方式：纯 `SELECT`。`BEGIN READ ONLY` 未使用（全部为 catalog 查询，无事务外副作用）；
> **未执行** GRANT/REVOKE/DDL/SET ROLE/upgrade/downgrade，未写入任何业务表。
>
> **完整原始输出**（1184 行，机器可读）另存为独立文件：
> `docs/audits/2026-11-19-r40-e12-raw.txt`，`sha256 = d8f57d1ba259447d21e7f9e0d6d367e5ca2ef5789edb9f1b78406fc9d7e79847`。
> 本节只做**摘要 + 关键完整片段**；两处不一致即为裁剪，可用 raw 文件逐行核对。

### E12.0 采集身份与边界

```sql
SELECT current_database(), current_user, version();
SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1;
```
```
postgres | postgres | PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu, 64-bit
datname: ai_film_industrial | ai_film_verify | postgres
```

### E12.1 基础设施角色属性核查（关键发现：三个角色都不存在）

```sql
SELECT x.rolname, (r.oid IS NOT NULL) AS role_exists, COALESCE(r.rolcanlogin,false) AS canlogin,
       COALESCE(r.rolsuper,false) AS super, COALESCE(r.rolbypassrls,false) AS bypassrls,
       COALESCE(r.rolcreatedb,false) AS createdb, COALESCE(r.rolcreaterole,false) AS createrole,
       COALESCE(r.rolinherit,false) AS inherit_attr,
       (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) AS member_of_count,
       (SELECT count(*) FROM pg_auth_members m WHERE m.roleid = r.oid) AS is_granted_to_count,
       (SELECT count(*) FROM pg_auth_members m WHERE m.roleid = r.oid AND m.admin_option) AS admin_edges_held
  FROM (VALUES ('afs_owner'),('afs_migrator'),('ops_admin')) x(rolname)
  LEFT JOIN pg_roles r ON r.rolname = x.rolname;
```
```
   rolname    | role_exists | canlogin | super | bypassrls | createdb | createrole | inherit_attr | member_of_count | is_granted_to_count | admin_edges_held
--------------+-------------+----------+-------+-----------+----------+------------+--------------+-----------------+---------------------+------------------
 ops_admin    | f           | f        | f     | f         | f        | f          | f            |               0 |                   0 |                0
 afs_owner    | f           | f        | f     | f         | f        | f          | f            |               0 |                   0 |                0
 afs_migrator | f           | f        | f     | f         | f        | f          | f            |               0 |                   0 |                0
```

**这是本轮最重要的负面结论**：三个基础设施目标角色在集群中**一条记录都没有**。
注意后四列全是 `0` —— 一个不存在的角色，在「成员边数为 0」「被授予数为 0」「持有 admin_option 数为 0」
这些指标上**与合规状态完全无法区分**。

### E12.2 INV-10b 的合取式判定（v1.4.2 新增查询，首次实跑）

```sql
WITH o AS (SELECT oid FROM pg_roles WHERE rolname='afs_owner'),
     bad AS (SELECT count(*) AS n FROM pg_auth_members m, o
              WHERE m.roleid = o.oid AND m.admin_option
                AND NOT EXISTS (SELECT 1 FROM pg_roles u WHERE u.oid=m.member AND u.rolsuper))
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM o) THEN 'FAIL: afs_owner 不存在（真空真陷阱，禁止记为通过）'
            WHEN (SELECT n FROM bad) > 0     THEN 'FAIL: 存在非超级用户的 admin_option 边'
            ELSE 'PASS' END AS inv_10b,
       EXISTS (SELECT 1 FROM o) AS role_exists,
       (SELECT n FROM bad)      AS offending_edges;
```
```
                       inv_10b                       | role_exists | offending_edges
----------------------------------------------------+-------------+-----------------
 FAIL: afs_owner 不存在（真空真陷阱，禁止记为通过） | f           |               0
```

> 对比 E12.3（同事实的原始查询，返回 **0 行**）可看出差异：原始形态「0 行」极易被读成「无违规 = 通过」；
> 合取式判定把同一事实输出为 **FAIL**。两者查的是同一件事，结论相反——差别只在**是否显式要求角色存在**。
> 因此 v1.4.2 规定所有 INV 判定式必须写成 `role_exists(:role) AND <不变量谓词>`。

### E12.3 原始违规边查询（展示真空真的形态）

```sql
SELECT m.roleid::regrole AS granted_role, m.member::regrole AS member_role,
       m.admin_option, u.rolsuper AS member_is_super
  FROM pg_auth_members m JOIN pg_roles u ON u.oid = m.member
 WHERE m.roleid = (SELECT oid FROM pg_roles WHERE rolname='afs_owner');
```
```
 granted_role | member_role | admin_option | member_is_super
--------------+-------------+--------------+-----------------
(0 rows)
```

### E12.4 递归成员关系传递闭包（集群级，全角色）

```sql
WITH RECURSIVE g AS (
  SELECT m.roleid, m.member, m.admin_option, m.inherit_option, m.set_option, m.grantor,
         1 AS depth, ARRAY[m.roleid, m.member] AS path, false AS is_cycle
    FROM pg_auth_members m
  UNION ALL
  SELECT g.roleid, m.member, m.admin_option, m.inherit_option, m.set_option, m.grantor,
         g.depth + 1, g.path || m.member, m.member = ANY(g.path)
    FROM g JOIN pg_auth_members m ON m.roleid = g.member
   WHERE NOT g.is_cycle)
SELECT rg.rolname AS granted_role, ru.rolname AS member_role, g.depth,
       g.admin_option AS admin, g.inherit_option AS inherit_opt, g.set_option AS set_opt,
       pg_get_userbyid(g.grantor) AS grantor, g.is_cycle,
       array_to_string(ARRAY(SELECT pg_get_userbyid(x) FROM unnest(g.path) x), ' -> ') AS chain
  FROM g JOIN pg_roles rg ON rg.oid = g.roleid
         JOIN pg_roles ru ON ru.oid = g.member
 WHERE left(rg.rolname,3) <> 'pg_' AND left(ru.rolname,3) <> 'pg_'
 ORDER BY 1,2,3;
```
```
 granted_role | member_role | depth | admin | inherit_opt | set_opt | grantor | is_cycle | chain
--------------+-------------+-------+-------+-------------+---------+---------+----------+-------
(0 rows)
```
> 集群内**没有任何非系统角色的成员关系**（main 4 角色之间也无成员边）。
> 因此「传递闭包」在当前状态与「直接边列表」同为 0，**该查询尚不具备判别力**——
> 这也意味着 INV-6/INV-6b 目前只能判「未建立」，不能判「已满足」。

### E12.5 ⑨ 全量有效权限 —— 修正后成功复现分区父表差异

`ai_film_industrial`：
```
        rolname        |              object               | privs | relkind
-----------------------+-----------------------------------+-------+---------
 app_role              | outbox.outbox_events              | S     | p
 asset_service_role    | asset_service.asset_versions      | SIU   | r
 asset_service_role    | asset_service.qc_reports          | SI    | r
 asset_service_role    | audit.status_transitions          | SI    | r
 asset_service_role    | auth.revoked_tokens               | S     | r
 asset_service_role    | outbox.outbox_events              | SI    | p
 asset_service_role    | outbox.outbox_events_default      | SI    | r
 asset_service_role    | outbox.outbox_events_y202611      | SI    | r
 asset_service_role    | outbox.outbox_events_y202612      | SI    | r
 asset_service_role    | public.alembic_version            | S     | r
 outbox_relay_role     | outbox.outbox_events              | S     | p
 outbox_relay_role     | outbox.outbox_events_default      | S     | r
 outbox_relay_role     | outbox.outbox_events_y202611      | S     | r
 outbox_relay_role     | outbox.outbox_events_y202612      | S     | r
 project_planning_role | audit.status_transitions          | SI    | r
 project_planning_role | auth.revoked_tokens               | S     | r
 project_planning_role | outbox.outbox_events              | SI    | p
 project_planning_role | outbox.outbox_events_default      | SI    | r
 project_planning_role | outbox.outbox_events_y202611      | SI    | r
 project_planning_role | outbox.outbox_events_y202612      | SI    | r
 project_planning_role | project_planning.episodes         | SIU   | r
 project_planning_role | project_planning.organizations    | S     | r
 project_planning_role | project_planning.projects         | SIU   | r
 project_planning_role | project_planning.script_revisions | SIU   | r
 project_planning_role | public.alembic_version            | S     | r
(25 rows)
```

**结论：#19 修复有效。** `app_role` 只在 `relkind='p'` 的父表出现，三个分区均无 —— 这正是 C.3.4 认定的
「父表有 `app_role` SELECT、三个分区没有」。v1.4.1 的 `relkind='r'` 会把这**唯一一行**漏掉。

### E12.6 ⑬ 分区父子 ACL 对照（首次实跑即暴露该查询自身有缺陷）

⑬ 的原始写法在 PG16 上**直接报错**：

```
ERROR:  recursive query "t" column 6 has collation "default" in non-recursive term but collation "C" overall
LINE 2: ...lname AS object, c.relkind, c.relacl, c.relowner, NULL::text...
HINT:  Use the COLLATE clause to set the collation of the non-recursive term.
```
> 原因：递归项 `t.object` 来自 `nspname||'.'||relname`，属 `"C"` collation；非递归项的 `NULL::text`
> 属 `default`。两者不一致即报错。**修法：`NULL::text COLLATE "C"`**（已回写 ADR C.5.3 ⑬）。
> 这条说明：**文档里的查询不跑一遍，无法确认它可执行**——即使语法看起来完全正常。

修正后 `ai_film_industrial`（49 行，节选首末）：
```
        parent        |            object            | relkind |        grantee        | privilege_type | app_role_select | asset_service_role_select
----------------------+------------------------------+---------+-----------------------+----------------+-----------------+---------------------------
 <parent>             | outbox.outbox_events         | p       | app_role              | SELECT         | t               | t
 <parent>             | outbox.outbox_events         | p       | asset_service_role    | INSERT         | t               | t
 <parent>             | outbox.outbox_events         | p       | asset_service_role    | SELECT         | t               | t
 <parent>             | outbox.outbox_events         | p       | outbox_relay_role     | SELECT         | t               | t
 <parent>             | outbox.outbox_events         | p       | project_planning_role | INSERT         | t               | t
 <parent>             | outbox.outbox_events         | p       | project_planning_role | SELECT         | t               | t
 outbox.outbox_events | outbox.outbox_events_default | r       | asset_service_role    | INSERT         | f               | t
 outbox.outbox_events | outbox.outbox_events_default | r       | asset_service_role    | SELECT         | f               | t
 outbox.outbox_events | outbox.outbox_events_default | r       | outbox_relay_role     | SELECT         | f               | t
 outbox.outbox_events | outbox.outbox_events_default | r       | project_planning_role | INSERT         | f               | t
 outbox.outbox_events | outbox.outbox_events_default | r       | project_planning_role | SELECT         | f               | t
 outbox.outbox_events | outbox.outbox_events_y202611 | r       | asset_service_role    | INSERT         | f               | t
 outbox.outbox_events | outbox.outbox_events_y202611 | r       | asset_service_role    | SELECT         | f               | t
 outbox.outbox_events | outbox.outbox_events_y202611 | r       | outbox_relay_role     | SELECT         | f               | t
 outbox.outbox_events | outbox.outbox_events_y202612 | r       | asset_service_role    | INSERT         | f               | t
 outbox.outbox_events | outbox.outbox_events_y202612 | r       | asset_service_role    | SELECT         | f               | t
 outbox.outbox_events | outbox.outbox_events_y202612 | r       | outbox_relay_role     | SELECT         | f               | t
(49 rows；完整输出见 raw 文件 E12.7/E12.8)
```

**两项必须记录的事实**：
1. `app_role_select` 在父表为 `t`、在**三个分区全部为 `f`** —— 分区父子 ACL 不对称**精确复现**。
2. `app_role` 在三个分区的 ACL 中**一条记录都没有**（不是「有权限但被过滤」，而是**完全没有 ACL 项**）。
   这意味着直接访问分区时 `app_role` 无任何权限；经父表访问时的行为取决于 PG 的分区权限解析，
   **必须由 E1 用真实角色连接实测**，不能由 catalog 推断。

### E12.7 ⑭ 全对象类型最终有效权限（汇总）

| 对象类型 | `ai_film_industrial` 行数 / 有权限 | `ai_film_verify` 行数 / 有权限 |
| --- | --- | --- |
| SCHEMA | 72 / 23 | 60 / 21 |
| TABLE / PARTITION | 168 / 25 | 156 / 23 |
| VIEW / MATVIEW | **0 / 0** | **0 / 0** |
| SEQUENCE | 12 / 4 | 12 / 4 |
| FUNCTION | 72 / 62 | 36 / 26 |
| DATABASE | 12 / 12 | 12 / 12 |

> 行数 = 对象数 × 角色数（12 个角色：main 4 + `svc_*` 8）。
> **VIEW/MATVIEW 两库均为 0**：本 ADR 范围内**不存在视图/物化视图**，故「视图层有效权限」是空集，
> 既不应宣称「已验证」也不应宣称「存在风险」。
> **SEQUENCE 只有 1 个**（`outbox.maintenance_runs_id_seq`）× 12 角色 = 12 行，其中 4 个角色有权限
> （`app_role` 为 `US`，其余三个 main 角色为 `U`）。
> **FUNCTION**：`ai_film_industrial` 6 个函数、`ai_film_verify` 3 个；72 行中 62 行有 `X`，
> 原因是 `CREATE FUNCTION` 默认授予 `PUBLIC` EXECUTE（对应附录 E #15）。

表权限分布（⑭ 的 `TABLE/PARTITION` 有权限行）：
```
ai_film_industrial: 25 行  {app_role:1, asset_service_role:9, outbox_relay_role:4, project_planning_role:11}
ai_film_verify    : 23 行  {app_role:1, asset_service_role:8, outbox_relay_role:4, project_planning_role:10}
```
**`svc_*` 在其中一行都不出现** —— 与 ⑮ 的结论一致。

### E12.8 ⑮ 零权限补集布尔矩阵（决定性：svc_* 全 f）

```sql
SELECT r.rolname, n.nspname||'.'||c.relname AS object,
       has_table_privilege(r.oid,c.oid,'SELECT') AS sel, ... 'TRIGGER' AS trg
  FROM pg_roles r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind IN ('r','p') AND ...
 ORDER BY 1,2;
```
```
ai_film_industrial: 矩阵行=169，涉及 svc_* = 8 个，svc_* 存在任一 "t" 的「角色×对象」组合数 = 0
ai_film_verify    : 矩阵行=157，涉及 svc_* = 8 个，svc_* 存在任一 "t" 的「角色×对象」组合数 = 0
```

> 这是比 E10.5 更强的证据：E10.5 只输出「有权限的行」，`svc_*` 不出现，**无法区分「零权限」与「查询漏了」**；
> ⑮ 输出完整 f/t 矩阵，**8 个 `svc_*` × 全部表的 7 项权限全为 `f`**，是显式的「为零」证明。
>
> 附带事实：`svc_*` 在 **DATABASE** 层面有 `CT`（CONNECT + TEMPORARY），所以「零权限」只指**表级权限**，
> 不指连接能力。表述必须精确到「表权限为零」，不能说「`svc_*` 无任何权限」。

### E12.9 两库漂移的精确值（`project_planning` 四表的 DELETE）

| 库 | `project_planning_role` 对 `episodes` / `organizations` / `projects` / `script_revisions` |
| --- | --- |
| `ai_film_industrial` | `SIU` —— **无 DELETE** |
| `ai_film_verify` | `SIUD` —— **含 DELETE** |

⑨ 的两份输出可直接对照：
```
[ai_film_industrial] project_planning_role | project_planning.projects | SIU  | r
[ai_film_verify]     project_planning_role | project_planning.projects | SIUD | r
```

> 这是 §7.0.4「DROP 必须双库验证」的**活证据**：同一角色在两个库的有效权限不同。
> 只查 `ai_film_industrial` 会得出「DELETE 已撤销」的结论，而 `ai_film_verify` 仍有 DELETE。
> 另：`ai_film_verify` 的 ⑨ 输出中**没有 `audit.status_transitions` 行**（industrial 有），
> 说明 verify 库的 audit schema 授权状态与 industrial 不同，需在 IP-4 逐库校准。

### E12.10 本轮新增结论（四条，均不得记为「已解决」）

1. **三个基础设施角色不存在**（E12.1）：`afs_owner` / `afs_migrator` / `ops_admin` 的 `role_exists=f`。
   INV-1/5/6/6b/8/9/10/10b **全部无法判定满足**，只能判「未建立」。
2. **INV 的真空真缺陷已实测证实**（E12.2/E12.3）：同一事实，原始查询 0 行、合取式查询 FAIL。
   已在 §5.3 把判定式改为 `role_exists AND <谓词>`，并在 C.5.2 增加 PASS/FAIL 判定查询。
3. **⑬ 查询本身有 collation 缺陷**（E12.6）：文档里的查询**不经实跑无法确认可执行**。已修并回写。
4. **两库权限漂移有精确值**（E12.9）：verify 库 `project_planning` 四表仍含 DELETE。
   「单库取证即可下结论」被否证。

> 局限（必须写明）：本节全部输出在 **`postgres` 超级用户会话**下取得。
> 超级用户受 `BYPASSRLS`/`rolsuper` 影响，`has_table_privilege` 的结果**不代表真实角色连接时的行为**；
> RLS 与 ACL 的行为验证必须在阶段 6 用真实域角色连接执行（§12 E1，区分 2a/2b 主体）。
> 本节只证明「catalog 状态是什么」，不证明「运行时行为如何」。
