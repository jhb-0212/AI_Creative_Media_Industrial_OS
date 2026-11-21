SELECT x.rolname,
       (r.oid IS NOT NULL) AS role_exists,
       COALESCE(r.rolcanlogin,false)  AS canlogin,
       COALESCE(r.rolsuper,false)     AS super,
       COALESCE(r.rolbypassrls,false) AS bypassrls,
       COALESCE(r.rolcreatedb,false)  AS createdb,
       COALESCE(r.rolcreaterole,false) AS createrole,
       COALESCE(r.rolinherit,false)   AS inherit_attr,
       (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) AS member_of_count,
       (SELECT count(*) FROM pg_auth_members m WHERE m.roleid = r.oid) AS is_granted_to_count,
       (SELECT count(*) FROM pg_auth_members m WHERE m.roleid = r.oid AND m.admin_option) AS admin_edges_held
  FROM (VALUES ('afs_owner'),('afs_migrator'),('ops_admin')) x(rolname)
  LEFT JOIN pg_roles r ON r.rolname = x.rolname;
