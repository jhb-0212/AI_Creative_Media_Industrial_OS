SELECT r.rolname, n.nspname||'.'||c.relname AS object,
       has_table_privilege(r.oid,c.oid,'SELECT')     AS sel,
       has_table_privilege(r.oid,c.oid,'INSERT')     AS ins,
       has_table_privilege(r.oid,c.oid,'UPDATE')     AS upd,
       has_table_privilege(r.oid,c.oid,'DELETE')     AS del,
       has_table_privilege(r.oid,c.oid,'TRUNCATE')   AS trun,
       has_table_privilege(r.oid,c.oid,'REFERENCES') AS ref,
       has_table_privilege(r.oid,c.oid,'TRIGGER')    AS trg
  FROM pg_roles r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
   AND (left(r.rolname,4)='svc_' OR r.rolname LIKE '%_role'
        OR r.rolname IN ('afs_owner','afs_migrator','ops_admin'))
 ORDER BY 1,2;
