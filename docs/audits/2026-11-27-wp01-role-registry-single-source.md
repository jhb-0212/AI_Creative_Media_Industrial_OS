# WP-01：角色事实源统一到 `role_specs.py`

- 日期：2026-11-27
- 分支 / worktree：`feat/identity-lifecycle-gov`（`C:\Users\EDY\Desktop\AI制作工作台\AI_Creative_Media_Industrial_OS-identity-gov`）
- 基线：`c4b9a52`（`refactor/identity-bootstrap` 稳定基线）
- 裁决依据：用户 2026-11-27 裁决「`role_specs.py` 是唯一事实源，不新增独立 `identity_registry.yaml`」
- 状态：代码已落地、闸门已过、**未提交**（本轮不授权提交）

## 1. 结论

角色定义的唯一事实源是 **`packages/shared/role_specs.py`**：Bootstrap 读它创建角色，CI 读它生成预期状态与 catalog 对账。审计报告只能输出**生成物**，不得反向成为第二份定义。

不新建 `identity_registry.yaml` 的理由（避免两份事实源各自通过局部校验、行为却不一致）：

| 若引入 YAML | 后果 |
| --- | --- |
| Python 定义创建与属性，YAML 定义生命周期与权限 | 两份"角色定义"，改一处漏一处 |
| Bootstrap 读 Python、CI 读 YAML | 闸门绿而运行时错，或反之 |
| 生成物需要人维护 | 生成物一旦手工改过，就变成事实源 |

## 2. 落实内容

### 2.1 `RoleSpec` 新增字段（全部为显式字段，不靠注释约定）

| 字段 | 取值域 | 说明 |
| --- | --- | --- |
| `name` | 角色名 | 数据库角色名 |
| `role_type` | `LOGIN` / `NOLOGIN`（派生 property） | 由 `login` 派生，避免两处可写 |
| `lifecycle_owner` | `bootstrap` / `migration` / `governance` | **本轮 20 个角色全部 `bootstrap`** |
| `lifecycle_state` | `active` / `deprecated` / `retired` | 已退役的 `qc_worker` **不得**声明为 live |
| `login` | bool | 是否允许登录 |
| `superuser` / `createdb` / `createrole` / `bypassrls` | bool | 提权面，`spec_problems` 对 True 一律报缺陷 |
| `inherit` | bool | 缺省 True；False 视为缺陷（会造成角色不可预期降权） |
| `member_of` | tuple[str, ...] | 允许的成员关系，方向是 `GRANT <group> TO <login>` |
| `database_domains` | tuple[str, ...] | 角色所属数据库域；`svc_*` 占位为空 |
| `deletion_policy` | `bootstrap_retire_only` / … | **迁移不得删除**，退役只能走 bootstrap/运维 |
| `adr_refs` | tuple[str, ...] | 关联架构决策（当前留空，不编造） |
| `migration_refs` | tuple[str, ...] | 历史迁移引用，由 `_LIFECYCLE_REFS` 派生 |

关键派生：

- `deletion_policy = bootstrap_retire_only` + property `can_be_dropped_by_migration = False` —— 迁移作者读代码即可知道"这不是你能删的东西"。
- `_with_lifecycle()` 统一派生域归属、成员关系、`migration_refs`、`adr_refs`，避免逐条硬编码错位。

### 2.2 校验与生成物

| 能力 | 位置 | 行为 |
| --- | --- | --- |
| 声明自检 | `role_specs.spec_problems()` | 提权属性、`inherit=False`、NOLOGIN 配口令、非法生命周期取值、角色名重复 → 返回问题列表 |
| 预期状态生成 | `role_specs.registry_payload()` | 输出 JSON 可序列化的 20 行，供 CI 与审计报告使用 |
| 对账工具 | `scripts/identity_registry.py` | `--check` 与活库 catalog 对账；`--dump` 生成审计用 JSON；`--include-undeclared` 显式允许"库里有、声明里没有"的差异项 |

`registry_payload()` 的定位写死在文档与脚本里：**它是投影，不是事实源**。任何人改生成物都会被下一次 `--dump` 覆盖。

### 2.3 闸门实测（worktree，`afs_gov_head` = 干净 head 库）

```
$ python scripts/identity_registry.py --check
{"declared": 20, "checked": 20, "problems": [], "clean": true}

$ python scripts/audit_identity_ddl.py --check
[PASS] 身份 DDL 未超出基线（只减不增）
```

`audit_identity_ddl` 的基线文件在 WP-02 之后**重新生成过一次**：`role_create` 9 → 6、`role_alter` 9 → 6，其余 11 类不变。理由不是"为了让闸门变绿"，而是基线账本的定义是"与当前迁移树精确相等"（`test_baseline_matches_current_migration_tree` 就是这么判的）；三处 `CREATE/ALTER ROLE` 已被移除，账本必须跟着降。`--check` 的口径仍是"只减不增"，未来再有人加身份 DDL 会立刻红。

## 3. 消费者与边界

| 主体 | 读什么 | 写什么 |
| --- | --- | --- |
| Bootstrap（`scripts/bootstrap_identity.py --roles`） | `role_specs()` | 创建/对齐角色属性与口令 |
| Migration | 只断言存在（`assert_role_present`） | 自己的对象与授权，**不**碰角色生命周期 |
| CI | `role_specs()` → 预期状态 | 与 catalog 比对，不写库 |
| 审计报告 | `identity_registry.py --dump` | 只出生成物 |

## 4. 已知边界（不掩盖）

1. `svc_*` 8 个 NOLOGIN 占位在集群里**真实存在**但仓库零引用，`born 为集群手工产物`。本轮把它们登记为声明项（`login=False`、无口令、无域），不等于已接入运行；接入需要独立裁决。
2. `role_specs.py` 里 `_ops_password / _relay_password / _app_password` 已成**历史语义保留**（WP-02 后无调用）。保留而非删除的理由：它们是"密码从哪来"的唯一文字证据；删除会让人误以为这些角色从不需要口令。是否有更清晰的表达方式，留给下一轮评审。
3. 成员关系方向曾写反（把 `GRANT operator TO login` 记成 login → operator 的成员是 login 的成员），导致对账 1 条误报。修正后 `problems=[]`；这不是"调参让闸门变绿"，而是把方向按 `pg_auth_members` 语义摆正。
4. 对账只覆盖**角色属性、成员关系、口令是否已设**这三层。授权面（schema/表/函数 ACL、默认权限、策略）由 `dump_identity_surface.py` 与 `audit_identity_ddl.py` 覆盖，两条闸门不可互相替代。

## 5. 本轮不做

- 不新增 YAML Registry。
- 不把审计生成物纳入版本控制（生成物在 `docs/audits/`，仍按用户要求保持未跟踪）。
- 不改 `ai_film_industrial`（见 WP-INDUSTRIAL-LINEAGE）。
