# ADR-R18：授权判定收敛为单一决策入口（Principal + Action + Resource + Context）

- 编号：ADR-R18
- 状态：已采纳
- 相关包：B2（身份与授权）
- 相关文档：`docs/audits/2026-11-16-review-round3-response.md` 第 12 条、ADR-R16（按域数据库角色）、ADR-R17（项目范围隔离）

## 背景

包 B1/B3 之后，"谁能读到哪些行"已经在数据库层被强制。但"谁能执行某个动作"仍然只有一种判定：

```python
if not (principal.roles & allowed_principals):
    raise 403
```

这条判定有三个结构性缺口，且都不在"少写一行校验"的层面：

1. **契约的大部分内容没有被运行时使用。** Action Registry 的 81 条动作，每条都带
   `scope` 与 `risk_level`；加载器只读了 `allowed_principals`，其余字段读完即弃。
   契约描述的是一个系统，代码执行的是另一个系统。
2. **红线「Agent 不得自批自用」没有代码执行。** 令牌里已有 `sub_type` 字段
   （B3 之前就加了），但没有任何一处判定读它。一个 `sub_type=agent` 且角色名为
   「导演」的令牌，可以走任何一条角色匹配的动作。
3. **职责分离写了两份。** 资产审批与剧本锁定审批各自写了一行
   `if creator == approver: raise STATE_TRANSITION_DENIED`，错误码与文案都不一致，
   审计无法按理由码统一计数；新增第三条第审批链时会写第三份。

## 决策

引入 `ActionSpec` + 纯函数 `decide()` + 单一强制执行点 `authorize()`，分三层、依赖单向：

```
packages/identity/authorization.py   纯决策（无 IO）：Principal + ActionSpec + Resource + Context → Decision
packages/identity/action_registry.py 契约加载：YAML → ActionSpec（含 scope / risk_level）
packages/identity/enforcement.py     强制执行点：决策 + 审计留痕 + 拒绝转 403 信封
services/identity_access/deps.py     依赖工厂：Depends(require_action("X"))
```

**为什么把授权判定放进 `packages` 而不是 `services/identity_access`**：业务服务在
加载资源后要执行资源级判定（例如资产批准前需要知道资产的组织、项目、创建人），
而 `services → services` 的直接依赖被 R2 闸门禁止。授权是横切能力，不是某个领域
服务的私有能力。反过来，`services/identity_access` 只保留"认证器装配"
（Principal 解析、吊销检查、域 UoW），职责边界因此更窄。

**判定顺序（首个命中的拒绝胜出，fail-closed）**：

| # | 条件 | 理由码 |
|---|---|---|
| 1 | 动作未在契约注册 | `ACTION_UNREGISTERED` |
| 2 | 契约未声明允许主体 | `NO_ALLOWED_PRINCIPAL` |
| 3 | 要求多主体共同审批（`A+B` 的 AND 语义） | `JOINT_APPROVAL_UNSUPPORTED` |
| 4 | 角色不相交 | `ROLE_MISMATCH` |
| 5 | Agent 执行 CRITICAL | `AGENT_CRITICAL_FORBIDDEN` |
| 6 | 申请人 == 批准人 | `SELF_APPROVAL_FORBIDDEN` |
| 7 | 资源存在但组织/项目归属未知 | `RESOURCE_UNRESOLVED` |
| 8 | 组织不匹配 | `ORGANIZATION_MISMATCH` |
| 9 | 项目不在授权范围（含空范围） | `PROJECT_SCOPE_DENIED` |
| — | 以上皆过 | `ROLE_AND_SCOPE_VERIFIED` / `SCOPE_DEFERRED_TO_RLS` |

**关于"此刻无法判定的检查"**：依赖阶段（HTTP 依赖）拿不到具体资源——创建类动作的
资源还不存在，按 id 读取要查库才知道归属，审批类动作的申请人也在库里。决策对象
把这两类"未执行"如实分成两栏：

- `deferred_dimensions`：范围维度（组织/项目）交给 PostgreSQL 行级策略（ADR-R17）；
- `deferred_checks`：检查项（当前只有 `separation_of_duties`）交给业务层在资源加载后执行。

这样"检查了什么"与"交给谁检查"都能被审计读到。反过来，若把"拿不到资源"写成
`ALLOW` 而不记录，ALLOW 就退化成"角色对上了"的同义词；若写成 `DENY`，所有列表
查询和创建接口都会 403——两种做法都不可用。

**为什么 `+` 不拆开**：`A+B` 是"三方共同审批"（AND），`A/B` 是"任一即可"（OR）。
把 `+` 也按分隔符拆成角色集合，会让任意一方单独批准——这是审批链上后果最严重的
一种误读。当前没有联合审批流程，因此这类动作一律拒绝（`JOINT_APPROVAL_UNSUPPORTED`），
而不是"按 OR 放行后再补"。

## 配套闸门

| 闸门 | 守什么 | 位置 |
|---|---|---|
| `test_no_action_is_open_to_principal_without_roles` | 81 条动作对无角色主体**全部**拒绝 | `tests/permission/test_authorization_policy.py` |
| `test_no_critical_action_is_executable_by_agent` | 10 条 CRITICAL 动作对**角色完全匹配**的 Agent 全部拒绝 | 同上 |
| W1 动作必须注册 | `require_action("X")` 的 X 必须在契约里（拼错在 CI 报红，而非首个请求 500） | `tests/permission/test_authorization_wiring.py` |
| W2 审批动作必须带审批对象 | 每处针对审批动作的 `authorize(...)` 必须传 `applicant_subject_id`（少传=静默跳过职责分离） | 同上 |
| W3 不得自行比对角色 | `services/` `apps/` `workers/` 内出现 `principal.roles` 即报红 | 同上 |

W1–W3 是 AST 静态检查，不是运行时抽查——它们针对的失守形态（拼错动作名、
复制一套角色判定、忘记传申请人）在功能测试里都不会暴露。

## 后果

**行为变化（有意为之，已同步更新测试）**：

1. 越权写入从 404 变成 403。此前集数登记靠"RLS 过滤后查不到项目"间接拒绝，
   返回 404；现在授权层直接拒绝。403 同样不确认资源存在性（范围外的 id 无论存在
   与否都是 403），但不再把"授权拒绝"伪装成"资源不存在"——运维排查时能区分
   "路径写错"和"越权"。两条写入路径（集数/资产）口径因此统一。
2. 自批从 409 变成 403。职责分离是"谁能批"的问题，不是状态机的合法转移问题。
   `STATE_TRANSITION_DENIED` 仍用于"流程不完整"（未申请就批准）。

**新增的诚实登记**：

- CRITICAL 动作**当前没有任何一条接入 HTTP 路由**（12 条已接入动作里最高为 HIGH）。
  因此 Agent 规则只有策略层证明，没有接口级证明；主链接入交付/放行动作时必须补。
- `deferred_dimensions` 的履行完全依赖 RLS 策略在对应表上存在。新表若忘记加策略，
  读类动作会变成"角色匹配即放行"。这一条由 ADR-R17 的"五张表策略文本必须一致"
  与本 ADR 的 deferred 记录共同约束，但没有自动化闸门能证明"新表也加了策略"。
- 联合审批（`+`）未实现，相关 3 条 CRITICAL 动作在当前系统里**不可执行**。
  这是刻意的：宁可不可用，不可被单方批准。

## 验证

- `pytest tests` 409 passed（新增 28 条：规则 17 条 + 契约穷举 3 条 + 接线闸门 4 条 + 强制执行点 2 条 + 适配 2 条）
- `ruff` / `mypy 95 files` / `architecture_check`（R1–R5 全 0 违规）/ `freeze_manifest` / `contract_check` 全绿
- 反向验证方式（可复用）：把 `decide` 中 Agent 分支注释掉后
  `test_no_critical_action_is_executable_by_agent` 立即变红——证明该测试不是假绿。
