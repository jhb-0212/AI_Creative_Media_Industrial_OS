-- 数据库迁移对账：只读取证脚本
--
-- 约束：本文件只含 SELECT / 目录查询。禁止出现 DDL、DML、alembic 命令。
-- 执行：psql -U postgres -d ai_film_industrial -f probe.sql
-- 输出：逐段比对 docs/evidence/db-reconciliation/RECONCILIATION.md 的表格。
--
-- 为什么用超级用户连接取证：RLS 策略与 FORCE 标志对超级用户不生效，
-- 读侧反而能看见全部策略与全部行的元数据；写侧本脚本一次都不做。

\echo '=== 1. 迁移版本（唯一的「库执行到哪」的事实源）==='
SELECT version_num FROM public.alembic_version;

\echo '=== 2. schema 清单（分支独有域是否存在）==='
SELECT nspname FROM pg_namespace
WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
ORDER BY 1;

\echo '=== 3. 表/分区：属主、RLS、FORCE ==='
SELECT n.nspname AS schema, c.relname AS object,
       CASE c.relkind WHEN 'p' THEN 'partitioned' ELSE 'table' END AS kind,
       pg_get_userbyid(c.relowner) AS owner,
       c.relrowsecurity AS rls, c.relforcerowsecurity AS forced
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
  AND c.relkind IN ('r','p')
ORDER BY 1,2;

\echo '=== 4. 角色（集群级对象：两个库共享，不能各自无脑执行）==='
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolbypassrls
FROM pg_roles
WHERE rolname NOT LIKE 'pg\_%'
ORDER BY 1;

\echo '=== 5. 表级 ACL（relacl）==='
SELECT n.nspname||'.'||c.relname AS object, c.relacl::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
  AND c.relkind IN ('r','p')
ORDER BY 1;

\echo '=== 6. 列级 ACL（attacl）——跨域只读与 relay 列级 UPDATE 的真实落点 ==='
SELECT n.nspname||'.'||c.relname||'.'||a.attname AS column_object, a.attacl::text
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE a.attacl IS NOT NULL AND n.nspname NOT LIKE 'pg_%'
ORDER BY 1;

\echo '=== 7. schema 级 ACL（USAGE 是表权限的前置条件）==='
SELECT nspname, nspacl::text FROM pg_namespace
WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
ORDER BY 1;

\echo '=== 8. 默认权限（未来建的表的授权口令）==='
SELECT pg_get_userbyid(d.defaclrole) AS owner_role, n.nspname AS schema,
       d.defaclobjtype, d.defaclacl::text
FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
ORDER BY 1,2,3;

\echo '=== 9. RLS 策略（USING / WITH CHECK 是否都写了）==='
SELECT schemaname, tablename, policyname, cmd,
       qual IS NOT NULL AS has_using, with_check IS NOT NULL AS has_check
FROM pg_policies ORDER BY 1,2,3;

\echo '=== 10. 约束类型分布（外键/检查/唯一是否有真实约束）==='
SELECT n.nspname AS schema, c.relname AS table, con.contype,
       count(*) AS n
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT LIKE 'pg_%'
GROUP BY 1,2,3 ORDER BY 1,2,3;

\echo '=== 11. 越权扫描：业务角色是否持有 DELETE / TRUNCATE / REFERENCES ==='
SELECT r.rolname, n.nspname||'.'||c.relname AS object,
       has_table_privilege(r.rolname, c.oid, 'DELETE') AS can_delete,
       has_table_privilege(r.rolname, c.oid, 'TRUNCATE') AS can_truncate
FROM pg_roles r
CROSS JOIN pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE r.rolname IN ('app_role','project_planning_role','asset_service_role','outbox_relay_role')
  AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
  AND c.relkind IN ('r','p')
  AND (has_table_privilege(r.rolname, c.oid, 'DELETE')
       OR has_table_privilege(r.rolname, c.oid, 'TRUNCATE'))
ORDER BY 1,2;
