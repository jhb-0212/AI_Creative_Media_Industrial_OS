# 密钥标识与归属事实的两处结构性收口（2026-11-18）

> 本文记录在 P1-05（身份签发物理拆分）落地后的联调中发现、并按根因根治的两处
> 缺口。两者都不是「断言写少了」，而是**事实来源缺位**：
> 密钥标识靠人写、归属判定靠请求体自证。
> 每条都给出「根因 → 治理 → 可复现证据」，未做的写在「本轮不做」。

## 0. 结论

| 项 | 根因（一句话） | 治理 | 证据 |
| --- | --- | --- | --- |
| kid 是字面量 `"current"` | 密钥标识被当成角色标签，由人赋值；`KeyStore` 以 kid 为键 → 两把密钥同名即静默覆盖 | kid 改为公钥内容指纹（RFC 7638），三方各自独立计算 | 契约 `KEYS-4`；`tests/unit/test_identity_kid_thumbprint.py`（5 项）；运行态三处一致 |
| 创建项目时组织归属自证 | 归属解析表 `_RESOLVE_SQL` 缺链根 `Organization`，只能拿 principal 租户 + 请求体拼一条必然自洽的路径；跨租户靠外键兜底，被映射成 409 | 登记 `Organization` 解析（RLS 下跨租户查不到 → 404），路由改用数据库事实 | `tests/unit/test_resource_scope_api.py` 2 项；`test_authorize.py` 结构守卫；容器内 404/403/201 |

闸门：`ruff` / `mypy`（103 文件）/ `pytest`（742 passed, 0 failed）/ `contract_check`
（17 条不变量，断言验证 17、partial 0、debt 0）/ `architecture_check`（R1–R5 零违规）/
`freeze_manifest --check` 全绿。

## 1. KEYS-4：kid 必须是公钥内容指纹，不能是配置项

**根因**：`services/identity_issuance/__init__.py` 两处、`packages/identity/bootstrap.py`
一处，都把 kid 硬编码为字符串 `"current"`。而 `KeyStore.__init__` 以
`{k.kid: k for k in keys}` 建索引——**两把密钥都叫 `"current"` 会静默互相覆盖**。

后果不是「报错」而是「静默错」：运维注入新密钥对后，签名令牌头部仍是 `"current"`，
验签方按 kid 查公钥永远命中旧键；同时提案里写的「KeyStore 已支持多 kid，轮换调度属
运维面」在实际实现层**没有入口**——门被焊死了，文档却写着门开着。

**治理**（把标识变成可验证的事实，而不是一个需要人来对齐的字符串）：

- `packages/identity/keys.py` 新增 `public_key_thumbprint()`：按 RFC 7638 对
  `{"e","kty","n"}` 规范序列化后取 SHA-256、base64url 无填充。它是 kid 的**唯一实现**。
- `generate_signing_key()` / `load_signing_key()` 不再接受 `kid` 参数（调用方无法
  再「指定一个标识」），kid 恒等于自身公钥指纹。
- 签发方（令牌头部）、分发方（JWKS 文档）、验签方（公钥仓储索引）三处各自从同一把
  公钥独立计算，**不需要任何一方配置 kid**；不再存在「配置不一致」这一类故障。
- 契约新增不变量 `KEYS-4`（`contracts/security_boundary.yaml`），断言
  `keys.kid_content_addressed`（AST 级检查：不得再出现字面量 kid 赋值）。

**评审拦截（PR #15 P2）与二次加固**：第一版只把 kid 的**缺省值**改成指纹，
三个入口（`generate_signing_key` / `load_signing_key` / `public_jwk_from_pem`）
仍保留 `kid` 形参，`PublicKeyStore` 仍接受 `{kid: pem}`——等于把「写死」换成了
「可绕过」，缺陷形态没变。二次加固改为**让非法状态无法表达**：

- `SigningKey.kid` 从字段改为 `cached_property`（由 `public_pem` 现场算），
  dataclass 字段只剩 `private_pem` / `public_pem`；
- 三个构造函数与 `PublicKeyStore.__init__` 全部删除 `kid` 形参；
- `KeyStore.public_keys()` → `public_pems()`（返回列表），验签仓储只收公钥、
  索引键内部算——调用方连「提供 kid」这个动作都不存在；
- 断言从「查字符串标识符」升级为「查函数签名 + 查类字段」：`_parameter_names()`
  用 AST 取形参名，`_annotated_fields()` 取可赋值字段名。上一版正是只查了标识符
  才漏掉这个缺口。

**证据**：

- `test_thumbprint_matches_rfc7638_test_vector`：用 RFC 7638 §3.1 官方示例密钥，
  输出与规范给出的 `NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs` **完全一致**；
- `test_token_header_kid_equals_jwks_kid_and_verifier_index`：三方取值同源；
- `test_generate_signing_key_derives_kid_from_content` / `test_two_keys_never_share_kid`：
  两把真实密钥的 kid 必不相同（旧的 `"current"` 语义下这是不可能的）；
- 容器实测（重建镜像后）：

  ```
  identity /health         signing_kid = UDjmyn5F…
  JWKS keys[0].kid         = UDjmyn5F…
  宿主机按 public.pem 自算  = UDjmyn5F…
  宿主机签令牌 → API 验签   POST /projects → 201
  ```

  验签方只持公钥、且没有从私钥派生公钥的入口（`PublicKeyStore.from_key_store` 已在
  P1-05 删除），三方仍能得到同一个 kid——这是内容派生的直接含义。

## 2. 归属链的根节点：`Organization` 必须可解析

**根因**：`packages/identity/ownership.py` 的 `_RESOLVE_SQL` 登记了 Project / Episode /
ScriptRevision / AssetVersion / QCReport，**唯独漏了归属链的根 `Organization`**。

于是「创建项目时指定组织归属」这条路径无法用数据库事实回答「该组织是否在我这个
租户内」，只能拿 `principal.tenant_id` + 请求体 `organization_id` 拼一条
`OwnershipPath`——这条路径**必然自洽**（租户来自 principal，恒等于 principal 的租户），
组织这一环等于自证。跨租户创建于是要等数据库复合外键报错才被发现，而 Service 把
`ReferentialIntegrityError` 统一映射为 `CONCURRENT_MODIFICATION`（409，`retryable: true`）。

**这个错误码是有害的**：把「无权 / 不可见」报成「稍后重试即可」，调用方会重试，
排障者会去查并发，而真实原因是越权。归属不可见必须与不存在同形（404），这是
`ownership` 模块已经定死的语义——缺的只是让根节点能被解析。

**治理**：

- `_RESOLVE_SQL` 增加 `"Organization"`（`project_id` 恒 NULL，链根无上级）。
  RLS 保证跨租户查不到 → `None` → `authorize` 转 404，与其余资源同形。
- `create_project` 不再拼路径，改为按 `resource_id` 解析：
  组织不可见 → 404；同租户跨组织 → 403 `AUTH_SCOPE_DENIED`；一致 → 放行。
  数据库复合外键保留为纵深防御，但不再承担「授权判定」的职责。

**证据**（容器内实测，同一组密钥签发的三种令牌）：

| 场景 | 修前 | 修后 |
| --- | --- | --- |
| 跨租户引用组织（token tenant≠0000，目标组织属 0000） | 409 `CONCURRENT_MODIFICATION`（retryable） | **404 `NOT_FOUND`** |
| 同租户跨组织（org claim=A，请求体=B，B 真实存在） | 403 | 403 `AUTH_SCOPE_DENIED` |
| 归属一致 | 201 | 201 |

- `tests/unit/test_resource_scope_api.py`：新增跨租户 404（显式断言「不得是 409」）与
  跨组织 403 两项；
- `tests/unit/test_authorize.py::test_ownership_resolver_registers_organization`：
  结构守卫，未登记类型会 fail-closed 抛 `INTERNAL_ERROR`，因此「返回 None」即证明已登记；
- `tests/unit/test_project_api.py::test_create_project_in_other_organization_rejected`：
  补播种 `_ORG_B`。**这不是放宽判定**——该用例的语义是「同租户、存在但不同的组织」，
  旧实现不查组织是否存在，漏播种也能碰巧得到 403；现在未播种会正确地落到 404，
  测到的就不是本条要证明的语义了。

## 3. 本轮不做

- **多公钥并行验签**：验签进程当前只读单把 `IDENTITY_PUBLIC_KEY_PEM`，轮换过渡期内
  无法同时验旧 kid 的令牌。kid 改为内容派生后，多 kid 的实现入口已存在（不再同名
  覆盖），但「多公钥注入 + JWKS 回源缓存」属运维面，登记在 `KEYS-4.residual_risk`。
- **JWKS 端点限流**：暴露面由网络层控制，与 P1-05 的 residual 一致。
- **OpenAPI 补 404 声明**：`openapi.draft.yaml` 是由 Action 派生的信封式草案
  （`/actions/project.create`），并非 REST `/projects` 的实际契约；把 404 写进
  信封草案会制造第二事实源。资源不可见的响应语义以 `packages/identity/ownership.py`
  模块 docstring 为唯一权威。
