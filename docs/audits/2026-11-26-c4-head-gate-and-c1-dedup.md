# C4 集成测试 head 闸门 + C1 重复 ACL 用例清理：实施与验证

> 日期：2026-11-26 ｜ 授权：Owner 六项裁决（A1–A6） ｜ 范围：C1 / C2 / C3 / C4
> 结论边界：本批次解决的是「测试环境可信度」与「判据覆盖完整性」。
> **不**宣称定位了原始偶发 ACL 红灯——那一条仍挂账，见 §7。

## 1. 授权与本批改动面

| 项 | 裁决 | 本批是否改动代码 |
|---|---|---|
| C1 | A：删除重复用例（不换判据） | 是（删 1 条用例 + 1 个专用 fixture；补三类覆盖前提） |
| C2 | 撤销 | 否 |
| C3 | 撤销 | 否 |
| C4 | 采纳：提升为集成测试全局前提 | 是（新增 2 个测试基础设施文件 + 1 个判定单测） |
| A5 | 采纳：C4 → C1 | 按此顺序执行 |
| A6 | 不允许豁免开关 | 未引入任何环境变量/跳过开关 |

未触碰：已应用迁移、`db_grants.DOMAIN_GRANTS` 的授权集合、outbox 分区默认权限判据、
P2 §5 另两项、`PENDING_PHASE1_REVIEW`、冻结面。

## 2. C4：集成测试的数据库版本前置条件

### 2.1 机制

| 文件 | 职责 |
|---|---|
| `tests/integration/_db_head.py` | 纯判定：`repository_heads()` 从 `alembic.ini` 的 `script_location` 派生仓库 head；`check_database_at_head(current, heads)` 集合相等否则抛 `DatabaseHeadMismatchError`；`read_current_revisions()` 读 `alembic_version`（表不存在 → 空集，视为未迁移） |
| `tests/integration/conftest.py` | `pytest_pyfunc_call`（`tryfirst`）在**测试函数体执行前**判定；会话内只连一次库；不满足即 `pytest.fail` |
| `tests/unit/test_db_head_gate.py` | 7 条：相等 / 多 head 顺序无关 / 落后 / 超前 / 分叉 / 空库 / 仓库 head 可读 |

三个设计选择及理由：

1. **用 hook 而不是 autouse fixture**：fixture 失败落在 setup 阶段，报告是
   `ERROR`，容易被读成「环境没起来」，而真实原因是「环境起来了、版本不对」。
   `pytest_pyfunc_call` 是 firstresult 入口，判定失败时默认实现不会接管，
   测试函数体确实不执行，报告是 `FAILED`。
2. **比集合而不是单值**：`alembic_version` 支持多行（多 head），落后 / 超前 /
   分叉 / 空库都归入同一结论。
3. **没有豁免开关**：`ALLOW_STALE_DB` / `SKIP_HEAD_CHECK` 一律不引入。需要历史
   结构时另立迁移兼容性测试（独立库 + 显式目标 revision）。

### 2.2 失败证据（真实落后库，改动前后同一条命令）

环境：`ai_film_api_tests`，`alembic current = b1c2d3e4f5a6`，`alembic heads = f9b0c1d2e3f4`。

改动前：

```
$ pytest tests/integration/test_tenant_isolation_invariants.py -q
....                                                                     [100%]
```

四条全绿——库落后 20 余个版本，用例仍然绿，这就是「绿得没道理」。

改动后：

```
$ pytest tests/integration/test_tenant_isolation_invariants.py -q
FAILED ...::test_every_outbox_partition_is_protected
FAILED ...::test_direct_partition_access_is_denied_for_app_role
FAILED ...::test_outbox_parent_still_fails_closed_without_tenant
FAILED ...::test_append_only_tables_reject_row_mutation
```

报错正文（每条相同，节选）：

```
集成测试的全局前提不满足：被测库与仓库迁移 head 不一致。
  当前库 revision：b1c2d3e4f5a6
  仓库 head　　　：f9b0c1d2e3f4

处理方式（在仓库根目录执行）：
    alembic upgrade head
```

报告类型是 `FAILED`（不是 `ERROR`），且测试主体未执行：判定发生在
`pytest_pyfunc_call` 的 tryfirst 实现里，抛错后默认实现不会调用 `item.runtest()`。

### 2.3 通过证据（从零建库到 head）

新建空库 `ai_film_head_verify`，从零 `alembic upgrade head` 成功（`f9b0c1d2e3f4 (head)`）。

```
$ POSTGRES_DB=ai_film_head_verify pytest tests/integration/test_tenant_isolation_invariants.py \
      tests/integration/test_least_privilege_grants.py -q
...................                                                      [100%]
```

```
$ POSTGRES_DB=ai_film_head_verify pytest tests/integration -q
...（239 条）...                                                          [100%]
```

同一个闸门在干净库上放行，说明它不是「一律拦」而是「按事实拦」。

## 3. C1：删除重复用例之前的覆盖核查

裁决要求：删除前必须确认表级 ACL、列级 ACL、角色继承三类授权路径仍有覆盖；
不得因为删掉重复测试而让「仅列级 GRANT」与「组角色继承」失去覆盖。

### 3.1 删除前的三类路径盘点

| 授权路径 | 判据位置 | 删除前状态 |
|---|---|---|
| 表级（`pg_class.relacl`） | `test_least_privilege_grants.py::test_domain_role_privileges_match_matrix` | 有，但只按 `grantee = 域角色` 过滤，看不见继承 |
| 列级（`pg_attribute.attacl`） | 仅 `test_cross_domain_read_is_column_scoped`（单表） | 有，但只覆盖 `asset→project_planning.projects`；append-only 表上的列级越权无人判 |
| 角色继承（`pg_auth_members`） | 无 | **缺失** |

同时确认：被删除用例的行为断言已有更强覆盖——
`test_qc_reports_are_append_only`（UPDATE 被拒）与
`test_asset_role_cannot_delete_asset_versions`（DELETE 被拒），两者都保留 `rollback`。

### 3.2 补齐方式（同一份矩阵、同一批查询，不新建框架）

`test_domain_role_privileges_match_matrix` 一次完成三类对账：

- 表级：`_TABLE_ACL`；
- 列级：`_COLUMN_ACL`，期望来自 `declared_column_privileges(domain)`；
- 继承：`_ROLE_MEMBERSHIPS` + `_privilege_sources()` 求成员闭包，闭包内所有来源的
  权限并起来再与矩阵比较。

期望值仍只有一份（`db_grants` 矩阵），观察者仍是已断言 `rolsuper` 的
`admin_sessionmaker`（`tests/conftest.py:193-202`），没有新增第三个查询口径。

### 3.3 两条盲区的实测（干净验证库，实验后已清理）

**实验 A：仅列级 UPDATE**

```sql
GRANT UPDATE (findings) ON asset_service.qc_reports TO asset_service_role;
```

```
E  [列级] asset_service_role 对 asset_service.qc_reports:
   实际列=['findings'] 权限=['UPDATE']，矩阵列=无 权限=无
FAILED ...::test_domain_role_privileges_match_matrix
```

按 §6-1 原方案换成 `has_table_privilege` 时，这条会返回 `false`（表级无授权）
——即越权从「会红」变成「会绿」。这就是不采纳 C1-B 的直接证据。

**实验 B：组角色继承**

```sql
CREATE ROLE afs_acl_probe NOLOGIN;
GRANT SELECT, UPDATE ON asset_service.qc_reports TO afs_acl_probe;
GRANT afs_acl_probe TO asset_service_role;
```

```
E  [表级] asset_service_role 对 asset_service.qc_reports:
   实际=['INSERT', 'SELECT', 'UPDATE']，矩阵=['INSERT', 'SELECT']
FAILED ...::test_domain_role_privileges_match_matrix
```

清理并确认恢复：

```
probe role dropped, remaining = 0
$ pytest tests/integration/test_least_privilege_grants.py -q
...............                                                          [100%]
```

### 3.4 删除内容

`tests/integration/test_tenant_isolation_invariants.py`：

- 删除 `test_append_only_tables_reject_row_mutation`（原 189–235 行）；
- 删除只被它使用的 `asset_engine` fixture 与 `build_asset_service_database_url` import；
- 文件头 docstring 改为指向新的判据归属，不留「为什么少了一条」的疑问。

删除后的判据归属：

| 事实 | 判据 |
|---|---|
| 授权集合是否正确（表级 / 列级 / 继承） | `test_domain_role_privileges_match_matrix`（矩阵派生、双向） |
| 越权是否真的被拒 | `test_qc_reports_are_append_only`、`test_asset_role_cannot_delete_asset_versions`、`test_project_role_cannot_delete` 等 |
| 默认权限是否收敛 | `test_no_default_privileges_on_business_schemas` |
| 结构是否正确 | 迁移链 + `alembic check` + Schema 验收 |

## 4. C2 / C3：撤销的依据

- **C2**：`admin_sessionmaker` 已断言 `rolsuper`；被删用例是 `admin_engine` 唯一
  的「读 ACL 但无前提断言」消费者。C1-A 后该路径消失，再给 `admin_engine` 加断言
  属于给一条即将不存在的用例加固。
- **C3**：`c1a2b3d4e5f6` 已用「授权矩阵 + 收敛器」根治默认权限，业务 schema
  实测 fail-closed（新建表零权限）。再写一条 `ALTER DEFAULT PRIVILEGES … REVOKE`
  会形成第二处权限声明。**未改动任何权限矩阵与已应用迁移。**

## 5. 回归结果

| 项 | 命令 | 结果 |
|---|---|---|
| Lint | `ruff check .` | All checks passed |
| 契约冻结 | `scripts/freeze_manifest.py --check` | PASS（基线 V205-R15 未变 / 54 条修订 / 附件 1 份） |
| 契约一致性 | `scripts/contract_check.py` | PASS（24 条不变量；提案状态 implemented；阻塞清单 8 条 blocked 0） |
| 分层 | `scripts/architecture_check.py` | PASS（R1–R5 零违规） |
| 阻塞清单 | `scripts/verification_blocks.py --check` | PASS（8 条：blocked 0 / lifted 8） |
| 集成测试（干净 head 库） | `POSTGRES_DB=ai_film_head_verify pytest tests/integration -q` | 239 passed |
| 全量测试（本地库已重建到 head） | `pytest tests -q --junit-xml=…` | **1832 条：failures 0 / errors 0 / skipped 1**（93s） |
| 类型检查 | `mypy apps services packages workers adapters` | Success: no issues found in 215 source files |

### 5.1 本地测试库的重建（环境事实）

`ai_film_api_tests` 迁移前处于**版本表与实际结构不一致**的状态：

```
alembic_version = b1c2d3e4f5a6
pg_namespace    = asset_service, audit, auth, outbox, project_planning, public
                  （缺 production_planning / budget_quota / qc_governance，
                    而 e0f1a2b3c4d5 本应创建它们）

alembic upgrade head →
  sqlalchemy.exc.DBAPIError: schema "production_planning" does not exist
  [SQL: ALTER DEFAULT PRIVILEGES IN SCHEMA production_planning
        REVOKE ALL ON TABLES FROM project_planning_role]
```

即这个库不是「落后」，而是「版本表声称执行过的迁移，其结构不在库里」。
按裁决要求的流程处置：**改名保留**（数据未删）后重建同名库。

```
ALTER DATABASE ai_film_api_tests RENAME TO ai_film_api_tests_stale_20261126;
CREATE DATABASE ai_film_api_tests;
alembic upgrade head   → f9b0c1d2e3f4 (head)
```

改名后的旧库保留在原实例，需要核对历史数据时可连它；确认无用后由 Owner 自行删除。

## 6. 本批次**没有**解决的问题

1. **原始偶发 ACL 红灯未定位**。原始日志不存在；本批次只消除「判据自身的不确定性」
   与「测试环境不可信」。这两个结论不能推出「那次红灯已经解释」。
2. **闸门只校验版本表，不校验结构**。若有人手工 `alembic stamp` 或手工删 schema，
   `alembic_version` 仍可能等于 head，闸门会放行。本次实际遇到的正是这种库
   （见 §5.1），闸门拦住它是因为版本号也确实落后；「版本号对、结构错」的组合
   仍是盲区，属另一个议题（结构指纹/`alembic check` 的职责边界）。
3. **`tests/unit` 下存在依赖数据库结构的用例**（`test_asset_api.py`、
   `test_identity_from_principal.py`、`test_resource_scope_api.py`）。它们不在
   `tests/integration` 目录，本批次的前置条件不覆盖它们；实测在干净 head 库上
   38 条全绿，在损坏库上红。是否把闸门扩展到这类用例，属新议题，未顺手扩大范围。
4. **非迁移角色建表的默认权限边界**、**P2 §5 另两项**、**`PENDING_PHASE1_REVIEW`**
   按裁决各自独立。
