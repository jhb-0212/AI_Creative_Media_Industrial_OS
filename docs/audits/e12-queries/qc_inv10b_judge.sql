WITH o AS (SELECT oid FROM pg_roles WHERE rolname='afs_owner'),
     bad AS (SELECT count(*) AS n FROM pg_auth_members m, o
              WHERE m.roleid = o.oid AND m.admin_option
                AND NOT EXISTS (SELECT 1 FROM pg_roles u WHERE u.oid=m.member AND u.rolsuper))
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM o) THEN 'FAIL: afs_owner 不存在（真空真陷阱，禁止记为通过）'
            WHEN (SELECT n FROM bad) > 0     THEN 'FAIL: 存在非超级用户的 admin_option 边'
            ELSE 'PASS' END AS inv_10b,
       EXISTS (SELECT 1 FROM o) AS role_exists,
       (SELECT n FROM bad)      AS offending_edges;
