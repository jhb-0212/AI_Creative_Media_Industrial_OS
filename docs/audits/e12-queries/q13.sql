WITH RECURSIVE t AS (
  SELECT c.oid, n.nspname||'.'||c.relname AS object, c.relkind, c.relacl, c.relowner, NULL::text COLLATE "C" AS parent
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE c.relkind='p' AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
  UNION ALL
  SELECT ch.oid, n.nspname||'.'||ch.relname, ch.relkind, ch.relacl, ch.relowner, t.object
    FROM t JOIN pg_inherits i ON i.inhparent=t.oid
           JOIN pg_class ch ON ch.oid=i.inhrelid
           JOIN pg_namespace n ON n.oid=ch.relnamespace)
SELECT COALESCE(t.parent,'<parent>') AS parent, t.object, t.relkind,
       COALESCE(pg_get_userbyid(a.grantee),'PUBLIC') AS grantee, a.privilege_type,
       has_table_privilege('app_role', t.oid, 'SELECT') AS app_role_select,
       has_table_privilege('asset_service_role', t.oid, 'SELECT') AS asset_service_role_select
  FROM t CROSS JOIN LATERAL aclexplode(COALESCE(t.relacl, acldefault('r'::"char", t.relowner))) a
 ORDER BY 1,2,4,5;
