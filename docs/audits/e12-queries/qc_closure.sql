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
