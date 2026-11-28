---
doc_type: proposal
status: implemented
updated_at: 2026-11-26
authoritative_sources:
  - docs/audits/2026-11-25-acl-flaky-root-cause.md
  - docs/audits/2026-11-26-c4-head-gate-and-c1-dedup.md
  - packages/shared/db_grants.py
  - tests/integration/test_least_privilege_grants.py
  - tests/integration/test_tenant_isolation_invariants.py
open_items: []
---

# ACL 四条改法的裁决材料（**已裁决、已实施**）

> 日期：2026-11-26 ｜ 提出：Agent（不自裁） ｜ 裁决人：项目 Owner
> 依据：`docs/audits/2026-11-25-acl-flaky-root-cause.md` §6
> 状态：Owner 已就 A1–A6 逐条裁决（见 §7），C1/C4 已实施，C2/C3 按裁决撤销。
> 实施证据：`docs/audits/2026-11-26-c4-head-gate-and-c1-dedup.md`。

## 0. 为什么需要这份材料

§6 的四条建议写在 2026-11-25，是对「`test_append_only_tables_reject_row_mutation`
偶发红灯」的调查结论。今天复核时发现三处事实与 §6 原文不一致：一条已被别的
迁移做完、一条按原方案实施会引入新漏报、一条的覆盖面已被更强的现成测试取代。

**如果不先纠正，批准的就是一个已经完成的改动（重复抽象）和一个会削弱判据的改动。**
所以本材料先把事实摆开，再给建议形态与执行顺序。

---

## 1. §6 原文（逐字，`2026-11-25-acl-flaky-root-cause.md` 第 125–141 行）

```markdown
## 6. 建议（须单独裁决，本轮不实施）

1. **判据换成角色相对函数**：`has_table_privilege('asset_service_role',
   'asset_service.qc_reports', 'UPDATE')`。它与观察者无关，而且把「经角色继承得到的权限」
   也算进去——现在的目录视图看不见继承，是另一种漏报。
   一个实现细节：文本形式需要能解析表名，用 `app_role` 连接执行会撞
   `permission denied for schema asset_service`；要用 OID 形式或在 owner/superuser 连接上跑。
2. **断言观察者前提**。conftest 的 `admin_sessionmaker` 写了「前提必须被验证而不是假设」
   并断言 `rolsuper`；本模块的 `admin_engine` 只有 docstring 说「迁移角色连接」，没有任何
   断言。同一份前提在仓库里一处被验证、一处被假设，正好落在最需要它的位置。
3. **收口与默认权限同源**：`ALTER DEFAULT PRIVILEGES` 不该给 append-only 表满量 DML，
   否则 M3 迟早变成一条真红灯。
4. **断言库在 head**：把这个用例的结论与自己声称的前提绑死；否则 §5 那种「绿得没道理」
   会一直存在。

四条都改判据/改迁移，属「改则须单独裁决」的范围（`2026-11-25-p2-release-review.md` §8.4
的同一口径），本轮一条都不做。
```

---

## 2. 三处必须更正的事实（先看这节，再决定批什么）

### 2.1 §6-3 已经做完了——按原文实施即重复抽象

`c1a2b3d4e5f6_least_privilege_business_schema.py`（DATA-6 / N-06）已经处理了同一个根因，
docstring 原文写着：

> **默认权限把同一个根因留给了「下一次建表」**。`8f3b2c1d4e5a` 同时给两个域角色授了
> `ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES`。
> `b7c8d9e0f1a2` 的「默认零授权」只 REVOKE 了 `app_role`——域角色是后建的，于是
> 「新表自动获得全量 DML」这条旧机制在新角色上原样复发……只 REVOKE 现有表，等于把
> 缺陷留给未来。

落地形态不是「再补一条 REVOKE」，而是**授权矩阵 + 收敛器**：

| 层 | 位置 | 作用 |
|---|---|---|
| 声明 | `packages/shared/db_grants.py:134`（`DOMAIN_GRANTS`） | 每张表的权限集与 `write_path`，矩阵是唯一事实源 |
| 执行 | `db_grants.py:666`（`converge_statements`） | 生成 `REVOKE ALL` + 按矩阵 `GRANT`，并 `REVOKE` 业务 schema 的默认权限（641–650 行） |
| 迁移 | `c1a2b3d4e5f6` | 执行上述语句 |
| 运行时证据 | `test_least_privilege_grants.py:129`、`:238`、`:280` | 真实 ACL == 矩阵（双向）、业务表不对 PUBLIC 授权、业务 schema 无默认权限 |

**实测（干净库，`alembic upgrade head`）**：

```
F1. 以迁移角色新建 asset_service.t_future 后
    information_schema.role_table_grants（grantee=asset_service_role）→ 0 行
    以 asset_service_role 实际访问 t_future → ERROR: permission denied for table t_future
```

即业务 schema 已经是 **fail-closed**：新表默认零权限，忘记显式 GRANT 会立刻报错。
§6-3 想做的那件事，方向与结论都对，但**已经落地**。

**顺带更正 §3.3 的 M3 判据**：`pg_default_acl` 里确实有一条
`asset_service_role=ar/postgres`，但它挂在 **`outbox` schema** 上
（`postgres / r / outbox / outbox_relay_role=r, project_planning_role=ar, asset_service_role=ar`），
是 `ae8724980e0b` 有意给「未来分区」继承的 `INSERT+SELECT`，不是业务 schema 的残留。
干净库与本地库的这条记录逐字相同，**不是手工改动**。
所以 §3 表格里 M3「重建后多到全量 `['DELETE','INSERT','SELECT','UPDATE']`」
在业务 schema 上**不成立**，该行判据需作废。

### 2.2 §6-1 按原文实施会引入一个新的漏报

`has_table_privilege` 只看**表级**授权。实测（临时库）：

```
G1. 仅 `GRANT SELECT (a) ON t_col TO member_role` 后
    has_table_privilege(member_role, t_col, 'SELECT')        = false
    has_any_column_privilege(member_role, t_col, 'SELECT')   = true
    information_schema.role_table_grants                      = 0 行
    information_schema.role_column_grants                     = 1 行
```

对 append-only 而言，「列级 UPDATE」是比「整表 UPDATE」更隐蔽的越权：

- 旧判据（`role_table_grants` 集合相等）：只有列级 UPDATE 时得空集 ≠ 期望集 → **红**（报因不准，但会红）；
- 新判据（`has_table_privilege('UPDATE')`）：返回 `false` → **绿**（看起来安全）。

**换判据会把一个会红的场景变成会绿的场景。** 若采用 §6-1，必须同时判列级
（仓库已有现成查询：`test_least_privilege_grants.py:47` 的 `_COLUMN_ACL`）。

顺带确认 §6-1 的实现细节是对的：文本形式在低权限角色上确实不可用——
`SET ROLE app_role; has_table_privilege(..., 'asset_service.qc_reports', 'UPDATE')`
→ `ERROR: permission denied for schema asset_service`；
OID 形式（先 `pg_class` 取 oid）在同一身份下正常返回。仓库也已有 OID 形式先例：
`test_least_privilege_grants.py:194` 的 `has_schema_privilege(r.oid, n.oid, 'USAGE')`。

### 2.3 §6-1/§6-2 的对象是一条**重复**且**最弱**的判据

`test_append_only_tables_reject_row_mutation`（`test_tenant_isolation_invariants.py:189-235`）
做了两件事，两件都已有更强的覆盖：

| 该用例的部分 | 已有的覆盖 | 强弱对比 |
|---|---|---|
| 207–226：授权口径集合相等 | `test_least_privilege_grants.py:129` `test_domain_role_privileges_match_matrix` | 已有：期望来自 `db_grants` 矩阵（不是硬编码）、双向比对（少授/多授/未声明表上持权）、`admin_sessionmaker` 已断言 `rolsuper`（`conftest.py:193-202`） |
| 228–235：UPDATE qc_reports 被拒 | `:357` `test_qc_reports_are_append_only`；`:346` `test_asset_role_cannot_delete_asset_versions` | 已覆盖，且各自保留 `rollback` |

也就是说：**原用例是三份判据里最弱的一份**——
期望值硬编码在用例里（与矩阵两处声明，会漂移）、判据有观察者依赖、
看不见角色继承（实测：靠组成员获得的 SELECT，`role_table_grants` 返回 0 行，
`has_table_privilege` 返回 true）。

按铁律「不叠加重复抽象」，正确动作是在这两者中选一个，**不是给最弱的那份加判据**。

---

## 3. 四条改法的建议形态

### C1（对应 §6-1）｜处置：**改为「删除重复用例」，不换判据**

**首选方案 C1-A**：删除 `test_append_only_tables_reject_row_mutation`（189–235 行），
append-only 的口径与行为分别交给已有两处：

- 口径：`test_domain_role_privileges_match_matrix`（矩阵派生、双向）；
- 行为：`test_qc_reports_are_append_only` / `test_asset_role_cannot_delete_asset_versions`。

附带确认（已核）：该文件 `admin_engine` 仍被 125/166 行的 outbox 分区用例使用，
删掉本条不会留下死 fixture。

**备选方案 C1-B**（若你要求保留这条用例的位置）：按 §6-1 换判据，并**必须**补两项：

1. 判据用 OID 形式（先 `pg_class` 取 oid），使观察者中立；
2. 期望值改为从 `db_grants.declared_privileges(domain)` 派生，不在用例里硬编码；
3. 增判列级：`pg_attribute.attacl` 上不得有该角色对 append-only 表的 UPDATE/DELETE。

C1-B 等于把已有那份判据的写法复制第二遍——这是我建议 C1-A 的理由。

- 影响面（C1-A）：删 47 行测试，无生产代码改动，无迁移。
- 验收：`pytest tests/integration/test_least_privilege_grants.py -q` 全绿；
  `grep -c "role_table_grants" tests/integration/test_tenant_isolation_invariants.py` 由 2 降为 1
  （剩下的是 outbox 分区那条，见 §5 边界）；
  故意 `GRANT UPDATE ON asset_service.qc_reports` 后，矩阵比对用例必须红（实测过该路径）。
- 回退：`git revert` 单提交即可。

### C2（对应 §6-2）｜处置：**C1-A 下自动消失；C1-B 下必须做**

`admin_engine`（67–73 行）没有前提断言，而 `admin_sessionmaker` 有。这个不对称本身是
缺陷——但它的**唯一受害者**就是 C1 这条用例。C1-A 之后，该文件读 `role_table_grants`
的只剩 115 行的 outbox 分区用例；那条的期望是「app_role 无任何直连权限」，
**空集判据看不见继承**（同样是漏报，但属另一条不变量，见 §5）。

- 建议：C1-A 下不单独做 C2；若要给 `admin_engine` 加断言，理由应写成
  「分区用例的前提」，不要借 ACL 的名义。
- 若你选 C1-B：C2 必须做，断言内容为「当前启用角色是 superuser 或
  `pg_has_role(current_user, :asset_role, 'MEMBER')` 或该表的 grantor」——
  即「这个身份能看见它要断言的行」。

### C3（对应 §6-3）｜处置：**撤销，不实施**

见 §2.1：`c1a2b3d4e5f6` + `db_grants` 矩阵已完成，并有 `test_no_default_privileges_on_business_schemas`
（`:280`）守着。再写一条 `ALTER DEFAULT PRIVILEGES ... REVOKE` 属于重复抽象，
而且会与矩阵产生**第二处声明**。

唯一残留的真问题是反向的：`db_grants` 的默认权限撤销只对 `DOMAIN_GRANTS` 里的域生效，
`FOR ROLE` 缺省是「执行迁移的角色」。实测确认这一语义成立：

```
B4. postgres 设的 default ACL 不作用于 app_role 建的表 → 该表上 asset_service_role 0 行
D.  pg_default_acl: postgres / r / asset_service_role=arwd/postgres
```

含义：**若将来有人用非迁移角色建表，新表拿到的不是「全量 DML」而是「零权限」**，
同样会 permission denied。这不是越权，是另一种 fail-closed；是否要为它加断言，
属新议题，不在本次四项内。

### C4（对应 §6-4）｜处置：**保留，但改位置：从单条用例提升为集成测试的全局前提**

§5 的结论（「它绿也不一定说明本分支对」）对**所有**集成测试成立，不只这条 ACL 用例。
所以断言写在用例内部只修一处，写在 `tests/integration/conftest.py` 才是根因级。

- 改法：session 级 autouse fixture，读 `alembic_version.version_num` 与
  `ScriptDirectory.from_config(Config("alembic.ini")).get_heads()`，不等即失败，
  报错文案直接给动作：
  `本库 revision=<x>，仓库 head=<y>；集成测试要求二者相等，先 alembic upgrade head`。
- 影响面：**只在集成测试**。本地库落后时，`pytest tests/integration` 会在首个用例前
  整体失败——这是有意的 fail-fast，但会改变现有本地工作流（当前可在落后库上跑出
  「大部分绿」的假象）。unit 测试不受影响。
- 验收：本地库停在 `b1c2d3e4f5a6` 时跑集成测试 → 失败信息含两个 revision 与动作；
  `alembic upgrade head` 后 → 全绿。CI 不受影响（CI 先迁移）。
- 风险：若有人需要「在落后库上验证某个历史行为」，这条路会被堵。可通过显式
  `--no-header` 类的开关豁免，但那等于给闸门开后门——建议不加，由你在批准时定。

---

## 4. 执行顺序（与 §6 编号不同，**需你裁决**）

§6 的编号是「论证顺序」，不是「依赖顺序」。按依赖应为：

```
C4（库在 head）→ C1（删重复用例）→ C2（仅 C1-B 需要）    ；C3 不实施
```

理由：

1. **C4 先行**：§5 已证明「库落后时这条用例仍然绿」。在这个前提下删/改判据，
   验收结论不可信——你无法区分「删了之后仍然绿」是因为判据正确还是因为环境不对。
2. **C1 在 C2 之前**：C1-A 会让 C2 的对象消失；先做 C2 等于给一条即将删除的用例加固。
3. **C3 不进序列**：已由 `c1a2b3d4e5f6` 完成，任何「再补一条 REVOKE」都是在
   `db_grants` 之外制造第二处声明。

若你要求「严格按 §6 编号」，那么执行顺序是 1→2→3→4，但 C1 会被迫走 C1-B
（因为 C3 会变成重复实现），且 C1-B 的验收必须在 C4 之前完成——
这意味着「用一个前提未验证的判据去验收另一个改动」，我不建议。

---

## 5. 禁止触碰的边界（本批次的硬约束）

1. **不改 `c1a2b3d4e5f6` 或任何已应用的迁移**。它已上线，改它就是改历史。
2. **不改 `db_grants.DOMAIN_GRANTS` 的任何权限集**。矩阵是授权面的唯一声明；
   改它等于改授权，属独立裁决。
3. **不动 outbox 分区那条判据**（`test_tenant_isolation_invariants.py:115`）。
   它有同样的「空集看不见继承」问题，但属不同不变量，混进来会让本批次无法收口。
4. **不动 P2 §5 的另两项**（`audit_ref` 非空校验、`artifact_manifests` 非 append-only）
   与 `PENDING_PHASE1_REVIEW`。按你 2026-11-26 的分流，它们各自独立。
5. **不通过放宽测试、加重试、改断言文案来让红灯变绿**。若某条用例在新判据下变红，
   先判「是判据错了还是库错了」，把结论写进记录再改。
6. **不动 `docs/baseline/` 与冻结面**（本批次无契约变更）。
7. **不宣称「已定位原始偶发」**。原始日志不存在（§1），本批次只消除「判据本身的不确定性」，
   不消除「当时那次红灯」——那条仍挂账。

---

## 6. 裁决清单（Owner 已逐条批注，2026-11-26）

| 编号 | 事项 | 我的建议 | 裁决 |
|---|---|---|---|
| A1 | C1 走 C1-A（删重复用例）还是 C1-B（换判据 + 补列级） | C1-A | **A：删除重复用例** |
| A2 | C2 是否随 C1-A 一并撤销 | 是 | **撤销** |
| A3 | C3 是否撤销（不做默认权限改动） | 撤销 | **撤销** |
| A4 | C4 是否提升为集成测试全局前提（会改变本地落后库的工作流） | 是 | **采纳** |
| A5 | 执行顺序是否采用 C4 → C1（与 §6 编号不同） | 采用 | **采纳** |
| A6 | 若选 C4，落后库上是否允许豁免开关 | 不允许 | **不允许** |

裁决理由的口径：判据归属于正确的事实源（授权集合 → 权限矩阵；越权拒绝 →
行为负向测试；结构正确 → 迁移与 Schema 验收），不为「就近可见」再建一份硬编码
期望值、独立观察者和独立查询口径。

---

## 7. 实施记录（2026-11-26）

### 7.1 落点

| 项 | 落点 | 结果 |
|---|---|---|
| C4 | `tests/integration/conftest.py`、`tests/integration/_db_head.py`、`tests/unit/test_db_head_gate.py` | 会话级一次判定；落后/超前/分叉/空库一律 `FAILED`，测试主体不执行；无豁免开关 |
| C1（删除） | `tests/integration/test_tenant_isolation_invariants.py` | 删除 `test_append_only_tables_reject_row_mutation` 及其专用 `asset_engine` fixture |
| C1（覆盖前提） | `tests/integration/test_least_privilege_grants.py` | 同一份矩阵对账扩展到表级 + 列级 + 角色继承（`pg_auth_members` 闭包展开） |
| C2 | 不改动 | `admin_sessionmaker` 已断言 `rolsuper`（`tests/conftest.py:193-202`），不重复增加断言 |
| C3 | 不改动 | 默认权限收敛只由 `db_grants.DOMAIN_GRANTS → converge_statements` 承担 |

C1 的覆盖前提不是「顺手加测试」：删除前逐项核对三类授权路径，并实测确认原判据
的两条盲区（仅列级 `GRANT`、组角色继承）在删除后会失去覆盖，因此先把矩阵对账
补成三类齐全，再删重复用例。实测证据见 `docs/audits/2026-11-26-c4-head-gate-and-c1-dedup.md` §3。

### 7.2 与 §6 原文的三处差异（不是照搬）

1. **C3 撤销**：`c1a2b3d4e5f6` 已完成同一根因，业务 schema 已 fail-closed；
   再补 `ALTER DEFAULT PRIVILEGES … REVOKE` 会形成第二处权限声明。
2. **C1 不换判据**：`has_table_privilege` 看不见列级，按原文实施会把
   「仅列级 UPDATE」从会红变成会绿（§2.2 实测）。
3. **C4 改位置**：从单条用例内部提升为集成测试目录级前提（§5 的结论对所有
   集成测试成立，不只 ACL 用例）。

### 7.3 仍然挂账（不得并入本次结论）

- **原始偶发 ACL 红灯未定位**。原始日志不存在；本次只消除了「判据自身的不确定性」
  与「测试环境不可信」，没有解释当时那一次红灯。
- **非迁移角色建表的默认权限边界**：实测该路径是「零权限」而非「全量 DML」，
  属 fail-closed 的另一种形态；是否要加断言另立议题，未混入本批次。
- **`PENDING_PHASE1_REVIEW`** 与 P2 §5 另两项（`audit_ref` 非空校验、
  `artifact_manifests` 非 append-only）不在本次授权范围。
