-- 目录全量导出（只读）：用于把「revision 声明」与「库内实际结构」逐项对上。
-- 用法：psql -d <db> -f q_catalog.sql
-- 输出为 unaligned + '|' 分隔，便于机器比对；不要手工改写结果。
\pset pager off
\pset format unaligned
\pset fieldsep '|'
\pset tuples_only on

\echo === 1 身份与迁移版本
SELECT 'identity', current_database(), current_setting('server_version'), current_user;
SELECT 'alembic_version', version_num FROM public.alembic_version;

\echo === 2 schema
SELECT 'schema', nspname
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
ORDER BY 1, 2;

\echo === 3 表与分区（含 RLS 状态与 owner）
SELECT 'table', n.nspname, c.relname, c.relkind,
       pg_get_userbyid(c.relowner), c.relrowsecurity, c.relforcerowsecurity
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
ORDER BY 2, 3;

\echo === 4 列
SELECT 'column', table_schema, table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema NOT LIKE 'pg\_%' AND table_schema <> 'information_schema'
ORDER BY 2, 3, ordinal_position;

\echo === 5 约束
SELECT 'constraint', n.nspname, c.relname, con.conname, con.contype,
       pg_get_constraintdef(con.oid)
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
ORDER BY 2, 3, 4;

\echo === 6 索引
SELECT 'index', schemaname, tablename, indexname
FROM pg_indexes
WHERE schemaname NOT LIKE 'pg\_%' AND schemaname <> 'information_schema'
ORDER BY 2, 3, 4;

\echo === 7 触发器
SELECT 'trigger', n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid)
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
ORDER BY 2, 3, 4;

\echo === 8 函数（含 SECDEF / owner / search_path / ACL）
SELECT 'function', n.nspname, p.proname,
       pg_get_function_identity_arguments(p.oid),
       p.prosecdef, pg_get_userbyid(p.proowner),
       coalesce(array_to_string(p.proconfig, ','), ''),
       coalesce(p.proacl::text, 'NULL(=默认 PUBLIC EXECUTE)')
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
ORDER BY 2, 3, 4;

\echo === 9 RLS 策略
SELECT 'policy', schemaname, tablename, policyname, permissive, roles::text, cmd,
       coalesce(qual, ''), coalesce(with_check, '')
FROM pg_policies
ORDER BY 2, 3, 4;

\echo === 10 序列
SELECT 'sequence', schemaname, sequencename
FROM pg_sequences
ORDER BY 2, 3;

\echo === 11 默认权限
SELECT 'default_acl', pg_get_userbyid(d.defaclrole),
       coalesce(n.nspname, '-'), d.defaclobjtype, d.defaclacl::text
FROM pg_default_acl d
LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
ORDER BY 2, 3, 4;

\echo === 12 表级授权（实际 ACL，按角色展开）
SELECT 'table_grant', table_schema, table_name, grantee, string_agg(privilege_type, ',' ORDER BY privilege_type)
FROM information_schema.role_table_grants
WHERE table_schema NOT LIKE 'pg\_%' AND table_schema <> 'information_schema'
GROUP BY 1, 2, 3, 4
ORDER BY 2, 3, 4;
