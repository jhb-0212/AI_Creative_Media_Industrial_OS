SELECT m.roleid::regrole AS granted_role, m.member::regrole AS member_role,
       m.admin_option, u.rolsuper AS member_is_super
  FROM pg_auth_members m JOIN pg_roles u ON u.oid = m.member
 WHERE m.roleid = (SELECT oid FROM pg_roles WHERE rolname='afs_owner');
