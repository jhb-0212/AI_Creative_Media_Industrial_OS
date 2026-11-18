# ADR-R24：组织维度 RLS，以及「RLS 是否真的生效」的启动期验证

- 状态：**已实施（D3a，2026-11-18）**
- 关联：核验报告 §7 **N-03**（P1）、不变量 **SCOPE-1/SCOPE-2/SCOPE-3**、`contracts/security_boundary.yaml`
- 前序：ADR-R22（基线单一事实源）、ADR-R23（状态迁移事实表）

## 1. 问题

数据库只隔离租户，不隔离组织。实测（`project_planning_role` + 单租户上下文）：
同租户内可读 **51 个组织、1584 个项目**，组织边界 100% 由应用层 `authorize` 判定。

11 条既有策略一字不差，都长这样：

```sql
USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
```

## 2. 根因

**不是漏写策略，是策略缺输入。** `Principal` 里早有组织信息（`org` claim），
但没有任何代码把它注入数据库会话。缺一个维度，就只能写出一个维度的策略——
写十条也一样。

这与「授权决策输入不完整」（R-A）是同一根因在数据库层的投影：应用层拿全了输入，
数据库层没有。于是任何**绕过 `authorize` 的访问**（新端点忘了挂依赖、Worker 直查、
脚本直连）都会让组织边界静默失效，且数据库不会拒绝。

第二个根因是**没有验证「RLS 生效」这件事本身**：
- 所有业务表 `relforcerowsecurity = false`，表所有者是 `postgres`——**超级用户**。
  超级用户与 `BYPASSRLS` 角色永远绕过行级安全，`FORCE` 对它们也是无效的；
- 没有任何启动检查拒绝「业务进程以超级用户连库」。DSN 误配成 `postgres` 时，
  RLS **整体静默失效**，而「权限看起来都通」——这类失效无法在运行期被发现。

## 3. 决策

### D3a-1 组织上下文与租户上下文同点注入

`packages/shared/rls.py` 的 `build_tenant_uow_dependency` 是唯一的会话上下文注入点，
在注入 `app.current_tenant_id` 的同一处注入 `app.current_organization_id`（取自
`Principal.organization_id`）。不新增依赖、不新增一层装配——上下文有两个维度，
但注入点仍然只有一个。

**未注入时必须是 fail-closed**：`NullIF(current_setting(..., true), '')::uuid` 为
`NULL`，`org = NULL` 恒为 `NULL`（非真），于是只能看到 `organization_id IS NULL` 的行。
这与 `assert_ownership_scope` 对「无 org claim 的主体」的判定逐字一致。

### D3a-2 策略谓词与 `authorize` 同源

应用层的规则（`packages/identity/ownership.py::assert_ownership_scope`）是：

| 资源组织 | 主体有 org claim | 判定 |
| --- | --- | --- |
| `NULL`（个人租户） | 任意 | 允许 |
| 非空 | 同为该组织 | 允许 |
| 非空 | 无 claim 或不同组织 | 拒绝 |

「拒绝」在两层有两个形态，而不是两套规则：数据库层是**行不可见**（API 表现为
404），应用层 `assert_ownership_scope` 仍保留一条 `AUTH_SCOPE_DENIED`（403）分支。
后者在现有调用路径上**恒不触发**——解析器受策略约束，只能返回本作用域可见的行，
于是「他组织资源」在解析阶段就已与「不存在」同形。保留它是防「有人绕过解析器、
拿请求体拼归属路径」的 fail-closed 断言，不是第二条规则（详见 §7）。

数据库谓词必须逐格等价：

```sql
tenant_id = current_tenant
AND (organization_id IS NULL OR organization_id = current_org)
```

注意不能写成 `organization_id = NULL::uuid OR ...`：`x = NULL` 恒为 `NULL`（非真），
个人租户行会整片消失。这是 SQL 三值逻辑的坑，不是笔误。

两个实现，一条规则。为避免「两套规则各自漂移」，谓词由
`packages/shared/rls.py::organization_scope_predicate()` 生成（迁移直接调用它，不手抄 SQL），
并由**一致性用例**用同一张判定矩阵分别打应用层与数据库层，断言两层裁决相同
（`tests/integration/test_org_scope_parity.py`）。只写其中一层，另一层漂移时无人知道。

### D3a-3 子资源用 EXISTS 传递，不复制组织规则

`episodes` / `script_revisions` / `asset_versions` / `qc_reports` 没有 `organization_id`
列（归属经 project 派生，冗余存储会产生第二事实源——`scope.asset_ownership_chain`
已禁止）。它们的谓词用**父表存在性**表达：

```sql
EXISTS (SELECT 1 FROM project_planning.projects p
        WHERE p.entity_id = <本表>.project_id AND p.tenant_id = <本表>.tenant_id)
```

关键点：`projects` 自己的策略会在子查询内生效，于是「组织规则」只写了一次，
子表通过传递获得它。代价是每行一次索引探测（`projects.entity_id` 为主键）；
收益是**组织判定不可能在子表处成为另一份副本**。

`asset_service_role` 对 `projects` 只有列级 `SELECT (entity_id, organization_id,
tenant_id)`（迁移 `b3c4d5e6f7a8`），谓词恰好只用这三列——不需要放宽授权。

`organizations` 表自身：`organization_id = current_org`。它的「组织列」是自己的
主键，不是从父级继承——这是唯一一处规则形态不同，理由在语义而非实现。

### D3a-4 `FORCE ROW LEVEL SECURITY`，并说清它对超级用户无效

业务表与证据表全部 `ENABLE` + `FORCE`。必须写明：**`FORCE` 对超级用户和
`BYPASSRLS` 角色无效**（PostgreSQL 语义），而当前表所有者是 `postgres`（超级用户）。
所以 `FORCE` 在这里只是「防止将来把所有权转给一个非 BYPASSRLS 角色后 RLS 静默失效」
的保险，**它不构成本次的防线**。真正的防线是下一条。

### D3a-5 启动期验证「RLS 真的会生效」，而不是假设它生效

`packages/shared/rls.py` 增加 `verify_rls_enforcement(engine, *, app_env)`：
在进程启动时以**即将用于业务查询的同一条连接**问数据库两个问题：

1. `SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user`
   → 任一为真即拒绝启动（超级用户/BYPASSRLS 连库，RLS 不生效）；
2. 取一张策略表的 `relrowsecurity` 与 `pg_get_userbyid(relowner)`
   → 未启用 RLS，或**当前角色就是表所有者且未 FORCE**，即拒绝启动。

拒绝发生在启动期而不是运行期：运行期拒绝意味着「请求已经在跑，安全属性未知」。
非生产环境同样执行检查，但只在 `app_env == production` 时把失败升级为拒绝启动
（开发环境用 `postgres` 连库是既有事实，改成硬失败会让本地开发无法启动；
这条差异必须写在代码里，而不是靠不在生产跑测试来保证）。

### D3a-6 契约登记

新增不变量 **SCOPE-4**「组织维度的数据库强制」：组织边界必须在数据库层执行，
且与 `authorize` 同源、有启动期生效验证。断言 `scope.org_rls_enforced` 由迁移回放
（复用 ADR-R23 的 `_rendered_migration_sql`）+ 对 `rls.py` 的结构检查组成；
`runtime_evidence` 指向一致性用例与启动检查用例。

断言本身也要能被证明会报红：`tests/unit/test_boundary_org_rls.py` 把取数与判据
分开（`evaluate_org_rls` 收构造语句），先证「合规样本零问题」，再逐项破坏
（缺策略、手抄谓词漏 `IS NULL`、子表自己造组织列、丢租户谓词、缺 FORCE、
业务代码自行注入、注入点缺组织维度、启动检查未接线），每项只改一处并断言
恰好该条报红。没有这组用例，「断言永远 PASS」与「边界真的守住了」不可区分。

## 4. 迁移与回滚

单条迁移 `e9f0a1b2c3d4_organization_scope_rls.py`：

1. 对 6 张表 `DROP POLICY` + `CREATE POLICY`（同名，便于排障时一眼认出是哪张表）；
2. 6 张表 `FORCE ROW LEVEL SECURITY`；
3. `downgrade()` 逐表恢复单维策略并取消 FORCE。

`outbox.outbox_events` **不在本次范围**：Relay 需要跨租户枚举（迁移 `e5f6a7b8c9d0`
的窄通道就是为此而建），给它加组织维度会把「投递基础设施」和「业务授权」耦合。
事件总线的作用域是租户，这是设计选择，不是遗漏。

`audit.status_transitions` **暂不加组织维度**（见 §6 债务 1），理由不是「不重要」，
而是「现在加会错」：子资源的组织归属尚未落值（ADR-R23 §7.4），此时加
`organization_id IS NULL OR ...` 的谓词，等于把「未落值的子资源事实」判给所有组织可见。

## 5. 验收

| # | 判据 | 形式 |
| --- | --- | --- |
| 1 | 无组织上下文：只能看到 `organization_id IS NULL` 的行 | 负向用例（计数对比） |
| 2 | 组织 A 上下文：只能看到组织 A + 个人行，看不到组织 B | 负向用例（计数对比） |
| 3 | 子资源（episodes/assets）随父项目一同收敛 | 负向用例 |
| 4 | `organizations` 表只返回本组织 | 负向用例（对应实测的 51 行） |
| 5 | 应用层裁决 == 数据库层裁决（6 格矩阵：资源组织 × 主体组织） | 一致性用例 `test_org_scope_parity.py` |
| 6 | 超级用户 / BYPASSRLS / 表所有者连库 → 生产拒绝启动，非生产告警放行 | `test_rls_enforcement.py`（构造探测结果） |
| 7 | 既有用例不因收紧而失败 | 全量回归（792 项，见 §7.3） |

第 7 条本身就是检验：测试装配若绕过生产路径（自己 `SET LOCAL` 而不走依赖），
收紧后必然报红——这正是「测试是否真按生产路径跑」的证据。本轮实测确实红了
15 条，全部是测试播种代码「只注租户、不注组织」，不是实现缺陷（§7.3）。

## 6. 残余风险（登记，不假装已解决）

1. **表所有者仍是超级用户**：`FORCE` 当前无效（§D3a-4）。把所有权转给独立的
   `NOSUPERUSER` owner 角色需要部署侧新增凭证与 `ALTER TABLE ... OWNER TO` 的运维流程，
   属部署变更，列为 D3b。解除条件：owner 角色落地 + §D3a-5 的第 2 项检查在生产
   观测到下不再依赖「所有者不是业务角色」这一巧合。
2. **审计事实的组织维度**：`audit.status_transitions` 仍只按租户隔离。解除条件：
   子资源（Episode / ScriptRevision / AssetVersion）的组织归属在写入时落值
   （依赖 D3 的归属解析接入写入路径），随后加同名谓词。
3. **组织归属只支持单一组织**：`Principal.organization_id` 是单值。多组织用户需要
   令牌携带「当前组织」（而非组织列表），多值会引入「以谁的视角查」的歧义。
   当前选择是单值 + 切换组织需换令牌，属产品决策，不是技术限制。
4. **策略表达式无法被单元测试直接断言**：`pg_policies.qual` 是文本。一致性用例
   测的是**行为**（同矩阵下两层裁决相同），不是文本——这正是它比文本断言可靠的原因。

## 7. 实施中暴露并收敛的三个问题

这一节记录的不是「顺手改的」，而是三类**只有真跑起来才会暴露**的问题。它们都
不属于 D3a 的设计范围，但会被 D3a 触发，留着就是新的「文档说 A、代码做 B」。

### 7.1 组织边界下沉后，403 退化为 404（响应语义必须跟着改）

**事实**：收紧后 `test_resource_scope_api.py` 与 `test_project_api.py` 共 **10 处**
「跨组织读/改 → 403」断言全部变成 404。

**根因**：不是回归，而是边界下沉的必然后果。解析器 `SqlOwnershipResolver` 在策略
约束下**根本查不到**他组织的行，于是「无权访问」与「不存在」在解析阶段就无法区分。
若为了保住 403 而放宽解析（比如按请求体拼归属路径），就把组织这一环变成了自证——
正是整改前 `_assert_organization_scope` 的缺陷。

**改法**：
- 用户可见语义定为 **404 / NOT_FOUND**（`packages/identity/ownership.py` 模块
  docstring 写死真值表，不再逐处决定）；
- 保留 `AUTH_SCOPE_DENIED`（403）分支，定位改为「绕过解析器时的 fail-closed 断言」，
  并在 docstring 里说明它在现有路径上恒不触发——**规则只裁决一次**（数据库策略），
  应用层这段是等价性断言；
- 一致性用例只比较**裁决**（允许/拒绝），不比较响应形态，形态差异在用例 docstring
  里写明理由。

### 7.2 RLS 拒绝写入（42501）必须有端口语义，不能落到 500

**事实**：`test_missing_project_translates_to_referential_integrity_error` 期望
`ReferentialIntegrityError`（23503），实际拿到 RLS 的 `42501`。

**根因**：父项目不在当前组织作用域时，子表 INSERT 的 `WITH CHECK` 先被
`projects` 的组织谓词拒绝——**外键根本没机会报错**。而 5 个适配器只
`except IntegrityError`，`ProgrammingError`（42501 属于该类）直接穿透成 500。

更危险的是反向可能：42501 也被「缺少 GRANT」使用。若一律当成「引用不成立」，
部署漏授权会伪装成业务数据问题；若一律当成编程错误，跨组织写入会变成 500 而不是
明确的拒绝。

**改法**（`packages/shared/port_errors.py`）：
- `classify_write_error()` 按 SQLSTATE 分档：`23505` → 可幂等重复；
  `23xxx` → 不可幂等引用失败；`42501` **且消息含 `row-level security`** → 归入
  引用失败（父实体不在作用域）；其余 42501（缺 GRANT）、42601、42P01 → 返回
  `None` 原样上抛——部署/代码缺陷不能被翻译成业务失败；
- 适配器捕获类型从 `IntegrityError` 扩到 `DBAPIError` 并调用统一分类器，
  分类器返回 `None` 时 re-raise 原始异常（不是包一层）。

这条不只修一次测试：它把「数据库层拒绝」的三种来源（唯一、引用、安全）在端口
契约里彻底分开，服务层不必知道 SQLSTATE。

### 7.3 15 条红全部是测试装配缺陷，不是实现缺陷

| 现象 | 根因 | 改法 |
| --- | --- | --- |
| 播种组织行报 `new row violates row-level security policy for table "organizations"` | `organizations` 自身策略要求会话组织 == 该行，播种却只注租户 | `set_test_scope(session, org_id)` |
| 子表插入被 42501 拒（composite FK / ownership FK / port contract） | 父项目组织的上下文未注入 | 同上，按父项目组织播种 |
| 跨组织断言 403 变 404 | §7.1 | 断言改为 404 + NOT_FOUND |

关键判断：**没有一条需要放宽策略**。若某条用例只有放宽 RLS 才能过，说明它测的
不是生产路径——这正是 §5 第 7 条要检验的东西。

### 7.4 降级告警写成 `extra={...}`，字段被压进一个包（可观测性静默失效）

**事实**：§D3a-5 的非生产告警首次写成
`logger.warning("rls_enforcement_degraded", extra={...})`，实际输出是
`{"extra": {"role": "...", "problems": [...]}, "event": "..."}`。

**根因**：`extra=` 是 stdlib `logging` 的参数，structlog 把关键字参数当**字段**用；
写成 `extra` 只是一次「包了一层」的重命名，日志里仍然有字，人眼看不出问题。
但按字段检索（`role="postgres"`、`problems:*`）就一条也匹配不到——可观测性故障
恰好发生在唯一该报警的那次启动上。

**改法**：改为平铺关键字参数，与仓内其余 30 处调用一致；单测用 `capfd` 断言
`"role": "postgres"` 出现在输出里，而不只是事件名出现。

**顺带的陷阱**：该用例初版用 `caplog` 断言。structlog `PrintLogger` 直接写 stdout，
不经过 stdlib logging，`caplog` 永远收不到——无论实现有没有告警这条断言都红，
是「假阴性测试」。改用 `capfd`（fd 级捕获）后才真正验到。
