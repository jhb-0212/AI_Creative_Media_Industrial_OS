# ADR-R19：签发私钥移出 API 进程，验签方只持有公钥

- 编号：ADR-R19
- 状态：已采纳
- 相关包：B4（身份与安全）
- 相关文档：`docs/audits/2026-11-16-review-round3-response.md`、ADR-R18（统一授权决策入口）

## 背景

红线写的是「私钥仅用于签发，身份服务持有；API 层只拿公钥验签」。实际上有三处让它无法成立：

1. **生产配置把私钥列为必填。** `_PRODUCTION_REQUIRED_SECRETS` 里有
   `identity_private_key_pem`，于是每个 API 实例都**必须**挂载签发私钥才能通过生产校验。
   这不是"配错了"，而是把最危险的部署形态写成了合规要求。
2. **`KeyStore` 是"进程内多私钥注册表"。** 它同时提供签发（`current`）与验签（`get`）能力，
   任何持有 `KeyStore` 的进程都能签发任意主体的令牌——包括 CRITICAL 动作的审批人。
3. **验签方拿不到公钥时，回退到"由私钥派生"。** `PublicKeyStore.from_key_store(get_key_store())`
   这条路径让"无私钥的验签"成了可选优化，而不是结构约束。

三处叠加的后果：API 被攻破时，攻击面从"读数据"扩大到"伪造任意身份"；私钥轮换要求
重启全部 API 实例；"签发/验签分离"只存在于文档里。

## 决策

**把签发能力收敛为一个必须显式持有私钥才能构造的对象，并让"API 进程出现私钥"成为
一个可判定的启动错误。**

```
packages/identity/issuer.py   TokenIssuer：唯一持有私钥的组件（无全局单例、无隐式路径）
packages/identity/jwks.py     PublicKeyStore：只装公钥（结构上装不下私钥）+ 可从 JWKS 文档构建
packages/identity/guards.py   assert_no_signing_key_in_api_process：进程角色守卫
packages/shared/config.py     identity_process_role=api|issuer 的角色自洽校验
services/identity_access      get_public_key_store()（只读公钥）/ get_issuer()（只 issuer 进程）
```

四条具体决定：

**① 删除 `KeyStore` 与 `from_key_store`。** 前者是签发+验签合一的注册表，后者是"验签进程
借用签发仓储"的桥。多 kid 的合法需求（轮换期验签旧令牌）由 `PublicKeyStore` 承载——它只装
`kid → 公钥 PEM`，装不下私钥。删除比"标记废弃"更彻底：留下的类型就是一个可以被误用的入口。

**② 公钥由私钥派生（`public_pem_from_private`），不再要求单独配置。** 旧设计要求私钥、公钥
分别配置，两者不匹配时验签**全量**失败，而失败现象是"令牌被篡改"——排查方向完全错误。
派生让不匹配在结构上不可能发生。

**③ 配置缺失时拒绝启动，而不是生成临时密钥。** `get_public_key_store()` 在既无
`IDENTITY_PUBLIC_KEY_PEM` 也无 `IDENTITY_JWKS_FILE` 时抛 `IdentityConfigurationError`。
临时密钥意味着重启即全量失效、多副本互不认可，是最糟的失败模式。

**④ 两道校验，不是重复。** `Settings` 的进程角色校验在任何进程构造配置时生效（含 worker、
脚本）；`guards.assert_no_signing_key_in_api_process` 在 API 入口再判一次并给出**部署层面**
的修复指引（拔掉 secret 挂载，而不是改字段）。少任何一道都会留下"某个入口没走校验"的可能。

**为什么守卫放在 lifespan 而不是模块导入时**：导入期校验会误伤"只需要路由表"的用法
（OpenAPI 导出、`contract_check`、路由单测）——它们因为一个与自身无关的密钥配置而失败。
lifespan 是"这个进程要开始接流量"的唯一时刻，配置缺失必须在这里挡下；而"只读路由表"的
工具不会经过它。测试用一个薄 `TestClient` 就能同时证明两个方向。

## 配套闸门

| 闸门 | 守什么 | 位置 |
|---|---|---|
| `test_api_role_rejects_signing_private_key` | `role=api` + 私钥 → 构造 `Settings` 即失败 | `tests/unit/test_identity_private_key_boundary.py` |
| `test_issuer_role_requires_signing_private_key` | `role=issuer` 无私钥 → 拒绝（否则进程能起来但一条令牌都签不了） | 同上 |
| `test_api_entrypoint_guard_blocks_private_key` | 守卫独立成立（用 `model_construct` 绕过配置校验后仍然拦得住） | 同上 |
| `test_api_entrypoint_actually_enforces_both_directions` | 守卫**真的被调用**——源码级断言，防"函数写好了但没人调用"的假闭环 | 同上 |
| `test_jwks_file_is_a_valid_public_distribution_path` | 验签方可只凭 JWKS 文档构建仓储（公钥分发不需要私钥在场） | 同上 |
| `test_identity_public_store.py` 全量 | JWKS 往返、拒绝非 RS256、无私钥入口、缺公钥拒绝启动 | `tests/unit/test_identity_public_store.py` |

反向验证：把 `identity_process_role` 校验里 `api` 分支的 `raise` 注释掉，
`test_api_role_rejects_signing_private_key` 立即变红。

## 后果

**行为变化（有意为之）**：

1. 生产部署**不再能把私钥挂进 API 容器**——挂了就起不来。运维需要新增一个
   `IDENTITY_PROCESS_ROLE` 环境变量并为签发进程单独部署（或在本进程用 issuer 角色签发）。
2. `IDENTITY_PRIVATE_KEY_PEM` 从生产必填清单**移除**，改为"只有 issuer 角色必填"。
   `.env.example` 与 `infra/compose/docker-compose.yml` 已同步。
3. `services/identity_access.get_key_store()` 已删除。任何外部调用点会得到 `ImportError`
   而不是"悄悄继续用私钥派生"。
4. 令牌签发入口由 `issue_token(...)`（薄封装）与 `TokenIssuer.issue(...)`
   （显式对象）提供；测试通过 `signing_key=` 注入固定密钥，不依赖进程内全局状态。

**新增的诚实登记**：

- **没有独立部署的签发进程。** `IDENTITY_PROCESS_ROLE=issuer` 目前只是一个配置角色，
  仓库内没有 issuer 的容器、没有 `/token` 端点、没有 JWKS 发布端点。也就是说：
  API 进程**不能**再签发令牌，但"谁来签发"尚未实现——当前令牌由测试与脚本签发。
  这是包 D（Phase 1 主链）之前的已知空档，**不是**可以忘掉的技术债。
- **JWKS 文档目前靠挂载文件分发**，没有 HTTP 端点。轮换需要重新挂载并重启 API，
  "不停机轮换"只是结构上可行（多 kid 共存），流程上未验证。
- **没有密钥指纹/版本的可观测性。** 验签失败时不知道是 kid 未命中、算法不符还是签名错，
  日志里只有一句"验签失败"。这对线上排障是不够的（列入后续可观测性项）。

## 验证

- `pytest tests` 418 passed（较 B2 净增 9 条：新增
  `test_identity_private_key_boundary.py` 7 条 + `test_identity_public_store.py` 2 条；
  另对 `test_config_secrets.py`、`test_identity_jwt.py` 中依赖旧 `KeyStore` 的用例做了改造）
- `ruff` / `mypy 97 files` / `architecture_check`（R1–R5 全 0 违规）/ `freeze_manifest` /
  `contract_check` / `alembic check` 全绿
- 手工核验（四种部署形态，均以 `apps/api/main.py` 为入口真实启动一次）：

  | 配置 | 结果 | 拦截点 |
  |---|---|---|
  | `role=api` + 私钥 | 起不来 | `Settings` 校验（早于 lifespan） |
  | `role=api` + 仅公钥 | `STARTED` + `/readyz` 200 | — |
  | `role=api` + 无任何密钥 | 起不来 | lifespan 的 `get_public_key_store()` |
  | `role=issuer` + 私钥 | 起不来 | lifespan 的 `assert_no_signing_key_in_api_process` |

  第四行是有意为之：**进程身份由入口决定，不由环境变量决定**。把 `role` 改成 `issuer`
  并不能让私钥进入 API 入口——否则这个开关本身就成了绕过手段。真正的签发进程不是
  跑 `apps/api/main.py` 的进程（该进程尚未实现，见「后果」）。
