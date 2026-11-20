SELECT r.rolname, n.nspname||'.'||c.relname AS object,
       concat_ws('',
         CASE WHEN has_table_privilege(r.oid,c.oid,'SELECT')     THEN 'S' END,
         CASE WHEN has_table_privilege(r.oid,c.oid,'INSERT')     THEN 'I' END,
         CASE WHEN has_table_privilege(r.oid,c.oid,'UPDATE')     THEN 'U' END,
         CASE WHEN has_table_privilege(r.oid,c.oid,'DELETE')     THEN 'D' END,
         CASE WHEN has_table_privilege(r.oid,c.oid,'TRUNCATE')   THEN 'T' END,
         CASE WHEN has_table_privilege(r.oid,c.oid,'REFERENCES') THEN 'R' END,
         CASE WHEN has_table_privilege(r.oid,c.oid,'TRIGGER')    THEN 'G' END) AS privs,
       c.relkind
  FROM pg_roles r
  CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
   AND (r.rolname LIKE 'svc_%' OR r.rolname LIKE '%_role'
        OR r.rolname IN ('afs_owner','afs_migrator','ops_admin'))
   AND (has_table_privilege(r.oid,c.oid,'SELECT') OR has_table_privilege(r.oid,c.oid,'INSERT')
     OR has_table_privilege(r.oid,c.oid,'UPDATE') OR has_table_privilege(r.oid,c.oid,'DELETE')
     OR has_table_privilege(r.oid,c.oid,'TRUNCATE') OR has_table_privilege(r.oid,c.oid,'REFERENCES')
     OR has_table_privilege(r.oid,c.oid,'TRIGGER'))
 ORDER BY 1,2;
