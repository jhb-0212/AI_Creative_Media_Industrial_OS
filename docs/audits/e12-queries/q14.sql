WITH r AS (
  SELECT oid, rolname FROM pg_roles
   WHERE left(rolname,4)='svc_' OR rolname LIKE '%_role'
      OR rolname IN ('afs_owner','afs_migrator','ops_admin'))
SELECT 'SCHEMA' AS objtype, r.rolname, n.nspname AS object,
       concat_ws('', CASE WHEN has_schema_privilege(r.oid,n.oid,'USAGE')  THEN 'U' END,
                      CASE WHEN has_schema_privilege(r.oid,n.oid,'CREATE') THEN 'C' END) AS privs
  FROM r CROSS JOIN pg_namespace n
 WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
UNION ALL
SELECT 'TABLE/PARTITION', r.rolname, n.nspname||'.'||c.relname,
       concat_ws('', CASE WHEN has_table_privilege(r.oid,c.oid,'SELECT')     THEN 'S' END,
                      CASE WHEN has_table_privilege(r.oid,c.oid,'INSERT')     THEN 'I' END,
                      CASE WHEN has_table_privilege(r.oid,c.oid,'UPDATE')     THEN 'U' END,
                      CASE WHEN has_table_privilege(r.oid,c.oid,'DELETE')     THEN 'D' END,
                      CASE WHEN has_table_privilege(r.oid,c.oid,'TRUNCATE')   THEN 'T' END,
                      CASE WHEN has_table_privilege(r.oid,c.oid,'REFERENCES') THEN 'R' END,
                      CASE WHEN has_table_privilege(r.oid,c.oid,'TRIGGER')    THEN 'G' END)
  FROM r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
UNION ALL
SELECT 'VIEW/MATVIEW', r.rolname, n.nspname||'.'||c.relname,
       concat_ws('', CASE WHEN has_table_privilege(r.oid,c.oid,'SELECT') THEN 'S' END,
                      CASE WHEN has_table_privilege(r.oid,c.oid,'INSERT') THEN 'I' END,
                      CASE WHEN has_table_privilege(r.oid,c.oid,'UPDATE') THEN 'U' END,
                      CASE WHEN has_table_privilege(r.oid,c.oid,'DELETE') THEN 'D' END)
  FROM r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind IN ('v','m') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
UNION ALL
SELECT 'SEQUENCE', r.rolname, n.nspname||'.'||c.relname,
       concat_ws('', CASE WHEN has_sequence_privilege(r.oid,c.oid,'USAGE')  THEN 'U' END,
                      CASE WHEN has_sequence_privilege(r.oid,c.oid,'SELECT') THEN 'S' END,
                      CASE WHEN has_sequence_privilege(r.oid,c.oid,'UPDATE') THEN 'W' END)
  FROM r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind='S' AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
UNION ALL
SELECT 'FUNCTION', r.rolname,
       n.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||')',
       CASE WHEN has_function_privilege(r.oid,p.oid,'EXECUTE') THEN 'X' ELSE '' END
  FROM r CROSS JOIN pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND p.prokind='f'
UNION ALL
SELECT 'DATABASE', r.rolname, current_database(),
       concat_ws('', CASE WHEN has_database_privilege(r.oid,current_database(),'CONNECT')   THEN 'C' END,
                      CASE WHEN has_database_privilege(r.oid,current_database(),'TEMPORARY') THEN 'T' END,
                      CASE WHEN has_database_privilege(r.oid,current_database(),'CREATE')    THEN 'X' END)
  FROM r
 ORDER BY 1,2,3;
