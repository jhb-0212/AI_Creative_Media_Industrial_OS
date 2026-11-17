# ADR-R26：Capability Token 进入统一授权决策，而不是新增第二套鉴权层

- 状态：Accepted（F3 处置）
- 日期：2026-11-17
- 关联：基线 §10.5、§C.5 / §C.5.1；ADR-R18（统一授权决策）；ADR-R19（签发私钥边界）

## 1. 背景

B2/B3 之后，`authorize()` 已经把角色、风险等级、Agent 红线、职责分离、组织与项目
范围收敛到单一决策入口。但契约里还有一条要求没有运行时对应物：

> RBAC 决定角色可申请的动作集合；ABAC 决定动作在组织/项目/资源/时间上的适用范围；
> Capability Token 将一次授权收敛为短时、限范围、可撤销的执行能力。

`Action Registry` 的 83 条动作全部 `token_required=true`，负向测试也钉住了这一点；
但运行时只检查访问令牌。结果是「长时身份令牌 + 角色」就能覆盖所有动作，
短时授权、单次范围、撤销链路都不存在。

## 2. 决策

### 2.1 两类令牌，两个受众

| 令牌 | 回答的问题 | 默认有效期 | audience |
|---|---|---:|---|
| Access Token | 你是谁、属于哪个租户/组织、能看到哪些项目 | 15 分钟 | `ai-film-api` |
| Capability Token | 这一次允许对哪些资源做哪些动作 | 5 分钟（上限 15 分钟） | `ai-film-capability` |

两者都用 RS256，按 `kid` 从同一个**纯公钥**仓储取公钥；audience 不同，
访问令牌无法被当作 capability 重放。签发私钥仍只在 issuer 进程，
API 进程结构上不能自签（ADR-R19）。

Capability Token 通过独立请求头 `X-Capability-Token` 传递，不塞进 Bearer 字段，
避免两类凭证被同一个解析器混用。

### 2.2 认证层只做验签与装配，不做授权判断

`build_principal_dependency()` 依次完成：

1. 验签访问令牌，构造 `Principal`；
2. 验签 capability，检查其 `jti` 是否在吊销表；
3. 用 `dataclasses.replace()` 把 `Capability` 挂到 `Principal` 上。

这一步**不**判断动作是否允许。授权仍只有 `packages/identity/authorization.py::decide()`
一个入口；capability 只是新的输入维度，不是第二套 Policy Engine。

### 2.3 `decide()` 里的新规则

在角色匹配、Agent CRITICAL 红线与职责分离之后，统一决策新增：

| 条件 | 理由码 |
|---|---|
| `token_required` 动作缺 capability | `CAPABILITY_MISSING` |
| capability 主体与访问令牌主体不一致 | `CAPABILITY_SUBJECT_MISMATCH` |
| capability 租户/组织与访问令牌不一致 | `CAPABILITY_TENANT_MISMATCH` / `CAPABILITY_ORGANIZATION_MISMATCH` |
| 动作不在 `allowed_actions` 或在 `denied_actions` | `CAPABILITY_ACTION_DENIED` |
| 资源项目不在 capability 的 `project_scope` | `CAPABILITY_PROJECT_SCOPE_DENIED` |

**不可扩大 scope** 是硬规则：capability 带 `*` 但 principal 本身不是组织级范围时，
`allows_project()` 返回拒绝。capability 只能收窄，不能把项目级主体抬成组织级主体。

组织/项目维度在依赖阶段仍然如实记为 `deferred_dimensions`，交给 PostgreSQL RLS；
capability 没有声明项目时不会在依赖阶段一律 403，否则列表查询与按 id 读取会全部失效。
拿到资源后，业务层再次调用同一个 `authorize(..., resource=...)`，capability 的项目
范围才会被真正核对。

### 2.4 撤销

capability 的 `jti` 与访问令牌共用 `auth.revoked_tokens` denylist。
现有 `scripts/revoke_token.py` 无需改动即可撤销 capability；检查失败一律 fail-closed。
过期返回 `CAPABILITY_EXPIRED`，撤销返回 `CAPABILITY_REVOKED`，都在审计里可计数。

### 2.5 签发边界

`CapabilityIssuer` 必须显式持有私钥才能构造；`services/identity_access.issue_capability()`
支持测试注入私钥，生产路径则调用 `get_capability_issuer()`（只对 issuer 进程可用）。
签发参数要求显式的 `subject_id / tenant_id / organization_id / allowed_actions /
project_scope / task_id / workflow_run_id / policy_version`；缺省即空集，不做「没写就是全权」。

## 3. 明确未实现（不 pretend）

这些属于基线要求，但当前契约没有对应动作或表，本轮不靠猜实现：

1. **HTTP 签发 / 升级端点**：`POST /authorization/capabilities` 与
   `POST /authorization/escalation-requests` 不在已冻结 Action Registry 中；
   凭空加动作需要走契约修订流程。当前由 issuer 进程内的签发对象提供能力。
2. **一次性 nonce 落库去重**：token 内含 `nonce`，但没有 `auth.consumed_capabilities`
   唯一约束，因此「同一凭证能否复用一次以上」尚未在数据库层执行。
3. **预算 / 配额实际扣减**：`budget_scope` / `max_amount` / `resource_quota` 被验签、
   保留并进入审计，但真正的扣减仍属于预算与配额命令的职责，不在授权层重复实现。
4. **PictureLock / 权利证据 / 终检报告的跨域存在性校验**：属于 D4 已登记的债务，
   不因引入 capability 而自动闭合。

## 4. 后果与验证

- 所有业务 API 客户端现在必须同时带 Bearer 与 `X-Capability-Token`；测试夹具
  `make_test_headers()` 统一走真实双凭证链路，而不是绕过认证。
- 审计决策新增 `capability_jti`，可把拒绝/放行与 Authorization Service 的签发记录关联；
  不记录 token 原文（红线「密钥不落日志」）。
- 负向证明：`tests/unit/test_capability_token.py` 覆盖 audience 重放、过期、主体不符、
  缺头、撤销；`tests/permission/test_authorization_policy.py` 覆盖同一 `decide()` 里的
  租户/组织/动作/项目范围越权。
