# ADR-R25：域角色的表级最小权限（授权面 == 动作面）

- 状态：**已实施（D4，2026-11-19）**
- 关联：N-06、DATA-1（模块级角色隔离）、DATA-6（本 ADR 新增的不变量）、ADR-R10（授权基线）、
  ADR-R24（组织维度 RLS，同一轮 D3）
- 上游依据：`docs/audits/2026-11-18-full-repo-baseline-and-root-cause-audit.md` §7 N-06
- 触发的契约演进：`contracts/security_boundary.yaml` 新增 **DATA-6**（不变量 21 → 22）

## 1. 问题的实质：数据库授的权比契约声明的动作多

运行库实测（`information_schema.role_table_grants`）：

```
project_planning_role  project_planning.projects         SELECT/INSERT/UPDATE/DELETE
project_planning_role  project_planning.organizations    SELECT/INSERT/UPDATE/DELETE
project_planning_role  project_planning.episodes         SELECT/INSERT/UPDATE/DELETE
project_planning_role  project_planning.script_revisions SELECT/INSERT/UPDATE/DELETE
```

而 `contracts/action_registry` 里**没有任何 `ORGANIZATION.*` 动作，也没有任何 `*.DELETE`
动作**——删除语义是 `ARCHIVED` / `SUPERSEDED` 状态，不是物理删除。也就是说数据库授予
的权限面比契约声明的动作面更宽，宽出来的那部分：

- 无授权入口（`organizations` 可被 INSERT/DELETE，却没有对应的动作与审计事件）；
- 与红线相悖（物理删除 + 重新插入，比「复活终态」更彻底地绕过状态机）；
- 无审计（没有任何业务动作会产生这类写入，因此日志里也不会有）。

## 2. 根因：授权从来没有被声明过（两层，缺一层都修不掉）

**第一层：用「表清单」代替「动作清单」。** `8f3b2c1d4e5a` 用

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA project_planning TO role
```

表达「这个域能读写自己的 schema」。于是权限面由**表清单**决定：新增一张表自动多一份
全量 DML，不需要任何人决定；而动作清单里根本没有 DELETE，两者从未被要求对齐。
`_revoke_append_only_overreach` 只对资产域的两张表做了点对点收紧——因为当时只修了被
点名的对象，于是「资产域收紧了、项目域没收紧」这种不对称本身就是补丁式修复的痕迹。

**第二层：默认权限把同一个根因留给了「下一次建表」。**

```sql
ALTER DEFAULT PRIVILEGES IN SCHEMA project_planning
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO role;
```

`b7c8d9e0f1a2` 已经做过一次「默认零授权」的反转，但它只 REVOKE 了 `app_role`；域角色
是后建的，于是同一形态在新角色上**原样复发**（`pg_default_acl` 实测：两个域角色对
业务 schema 都持 DELETE + INSERT + SELECT + UPDATE，序列持 USAGE/SELECT）。

只 REVOKE 现有表等于把缺陷留给未来：下次建表，DELETE 又回来了，而且**验证时看不见**
（表还不存在）。

## 3. 决策

### D4-a 授权矩阵是唯一声明处：`packages/shared/db_grants.py`

每条声明是 `(schema, table, privileges, write_path, source[, columns])`，其中 `write_path`
**必填**——说不出「谁在什么时候写它」，就不该有写权限。这是「权限 == 已实现动作所需的
最小集合」的可执行形式，而不是一句注释。

矩阵内容（最终态）：

| 域 | 表 | 权限 | 依据 |
| --- | --- | --- | --- |
| project_planning | projects | SELECT/INSERT/UPDATE | 创建 + 状态推进 + 归属解析；无 DELETE（ARCHIVED） |
| project_planning | episodes | SELECT/INSERT/UPDATE | 同上 |
| project_planning | script_revisions | SELECT/INSERT/UPDATE | 提交修订 + 锁定审批；修订历史不可物理删除 |
| project_planning | organizations | **SELECT** | 契约无 `ORGANIZATION.*` 动作；组织是平台级归属根 |
| asset_service | asset_versions | SELECT/INSERT/UPDATE | 登记 + 状态推进；无 DELETE（资产不可覆盖） |
| asset_service | qc_reports | SELECT/INSERT | 质检结论是证据，写入即定稿 |
| asset_service | project_planning.projects | **SELECT (entity_id, organization_id, tenant_id)** | 跨域只读，列级 |
| 两域共享 | audit.status_transitions | SELECT/INSERT | append-only 事实 |
| 两域共享 | outbox.outbox_events | SELECT/INSERT | 同事务投递（分区授权归 `partition_guard`） |
| 两域共享 | auth.revoked_tokens | SELECT | 吊销检查（fail-closed） |
| 两域共享 | public.alembic_version | SELECT | `/readyz` 读 schema revision |

**跨域只读必须列级**：授整表 SELECT 会让项目域未来新增的列自动对资产域可见——那不是
「最小权限」，只是「此刻恰好相同」。列级授权在 `pg_attribute.attacl`，与整表授权是
两份 ACL，断言必须分别比对（混在一起比会让列级永远对不上）。

### D4-b 收敛迁移是执行器，不是第二份声明

`c1a2b3d4e5f6` 的 `upgrade()` 只有一句循环：

```python
for statement in converge_statements(role, domain):
    op.execute(statement)
```

每张表先 `REVOKE ALL` 再按矩阵 `GRANT`，并撤销两个业务 schema 的默认权限（TABLES 与
SEQUENCES 各一条）。**为什么是整表重置而不是差量收紧**：差量式的结果取决于历史——
「现在缺什么就补什么」永远无法回答「矩阵之外还有什么」。整表重置让结果只取决于矩阵。
域角色是专用角色，重置不会伤到别人的授权。

迁移另加一条运行时护栏：收敛后枚举业务 schema 的真实表，存在矩阵未声明的表直接失败。
ORM 覆盖判据看不见迁移建的表，这层护栏补上。

### D4-c 判据：少授与多授都算失败

| # | 判据 | 拦住的失效 |
| --- | --- | --- |
| 1 | 本域 schema 的每张 ORM 表都在矩阵里 | 新表静默无权限（跑到线上才 permission denied） |
| 2 | 矩阵声明的表都真实存在 | 陈旧声明（收敛迁移 GRANT 失败） |
| 3 | 回放收敛迁移的语句 == 矩阵生成 | 矩阵改了迁移没跟上；迁移里手写一份「看起来一样」的 DDL |
| 4 | 收敛之后的迁移不得出现手写 `GRANT` | 下一次有人用 `GRANT … ON ALL TABLES` 图省事 |
| 5 | 业务 schema 无域角色默认权限 | 「未来建表自动带权限」——N-06 的第二层根因 |

第 4 条的判据是 AST 级**非 docstring 字符串字面量**：迁移的文档字符串会写「`GRANT … ON
ALL TABLES` 是错的」，把它算成违规等于「文档里不许提旧实现」，下一个人会为了过闸门
删掉说明（与 `evaluate_role_name_literals` 踩过的坑同型）。

第 4 条按 **alembic 版本链**划界（`ScriptDirectory`），不按文件名/字典序：迁移 id 是随机
前缀，字典序与执行序无关。历史迁移因此天然豁免，不需要维护白名单。

## 4. 三层证据（互不替代）

1. **静态**（`data.role_grants_least_privilege`）：覆盖上面 5 条判据，合并前就能拦住；
2. **迁移**：矩阵落地；`upgrade → downgrade → upgrade` 往返通过（回滚恢复旧口径，
   包括存量列级授权，而不是「什么都不做」）；
3. **运行时**（`tests/integration/test_least_privilege_grants.py`）：读
   `pg_class.relacl` / `pg_attribute.attacl` / `pg_default_acl` 与矩阵逐项比对，
   并用 `DELETE`/`INSERT organizations`/`UPDATE qc_reports`/`SELECT project_name`
   的 42501 证明越权 DML 真的被拒。

静态看不见「部署被手工改过」，实测看不见「迁移下一步会授什么」——两者缺一都留盲区。

## 5. 被这一决策触发的连带修正

**A. 测试播种必须换连接（26 条用例）。** 组织行原本用业务连接播种，收紧后
`INSERT INTO project_planning.organizations` 直接 42501。这不是「测试挡路」，而是
边界的真实形态：契约里没有组织创建动作，业务域角色本就不该能建组织。播种改走
`admin_sessionmaker`（超级用户），并在 helper 文档里写清「它对应的是尚不存在的
管理面路径」。**若为让播种通过而放宽授权，就是用测试需求反过来削弱生产的最小权限。**

**B. 删除语义的可测性变了。** `test_rls_isolation.py` 原先断言「A 上下文 DELETE B 的行 →
0 行受影响」，这是 RLS 过滤的证据。域角色没有 DELETE 之后，这条路径无角色可走：改为
断言 42501（更强——即使策略被误改成放行，也没有删除能力）。代价是「RLS 对 DELETE 的
过滤」不再可测，已登记为残余风险；若将来落地删除动作，需在该动作的 ADR 里补测。

**C. `REVOKE ALL ON <table>` 会一并撤销列级授权**（本地实测：`attacl` 被清空）。
因此收敛迁移无需单独 `REVOKE ... (columns)`；但回滚时**不能**把列级授权放大成整表
DML——`GRANT DELETE (col)` 是非法语句（PG：`invalid privilege type DELETE for column`），
这条报错恰好说明「列级」与「整表」不能共用同一套权限字面量。

## 6. 残余风险

1. **表所有者仍是超级用户**（D3b）：owner 路径不受本不变量约束，也无法用授权约束。
   `FORCE ROW LEVEL SECURITY` 对超级用户无效，这一点已在 ADR-R24 §D3a-4 说明。
2. **`information_schema.role_table_grants` 会过滤不可见角色的授权**：实测一律读
   `pg_class.relacl` / `pg_attribute.attacl`（授权的存储位置），否则「少授」会被误判成
   「无授权」。
3. **`GRANT USAGE ON SCHEMA` 不在矩阵内**：schema 级 USAGE 不含任何数据权限，且
   `public` schema 的 USAGE 本来就是 PUBLIC 默认。矩阵只管表级/列级，这一取舍写在此处
   以免下一个人以为漏了。
4. **删除动作落地后需补测 RLS 过滤**（见 §5 B）。

## 7. 验收

| # | 判据 | 证据 |
| --- | --- | --- |
| 1 | 真实 ACL == 矩阵 | `test_domain_role_privileges_match_matrix` |
| 2 | 业务 schema 无默认权限 | `test_no_default_privileges_on_business_schemas` |
| 3 | DELETE 被拒（三张表参数化） | `test_project_role_cannot_delete` |
| 4 | organizations 只读（INSERT/UPDATE/DELETE） | `test_organizations_are_read_only` |
| 5 | 跨域只读列级生效（读 `project_name` 被拒） | `test_asset_role_cannot_read_unscoped_project_columns` |
| 6 | 断言会报红（5 类构造输入各报一条） | `tests/unit/test_boundary_role_grants.py` |
| 7 | 迁移往返一致 | `alembic downgrade -1 && upgrade head` 实测 |
| 8 | 既有用例不因收紧而失败 | 全量回归 838 passed |
