# KEYS-1：身份签发服务独立部署单元拆分方案

> 状态：**S1/S2/S3/S4 已落地**（2026-11-18）。S1 签发方迁至 `services/identity_issuance`；
> S2 移除 `PublicKeyStore.from_key_store` 派生桥；S3 新增独立进程 `workers/identity`
> （持私钥 + `/.well-known/jwks.json`，不提供 token 端点）；S4 契约登记 KEYS-3 +
> 负向测试（`tests/unit/test_identity_process.py`、`test_identity_public_store.py`）。
> 未做（明确不在本轮）：密钥自动轮换调度、token 签发 HTTP 端点、OIDC 完整协议。
>
> 关联不变量：`contracts/security_boundary.yaml` KEYS-1 / KEYS-2 / KEYS-3 / DATA-1
> 前置：F01-F05 + DATA-1/DATA-2 六阶段重构已收敛
> 定位：本轮是「最后一个结构性 residual_risk」的根治，不是 Phase 1 核心生产闭环的推进。

---

## 1. 根因（不是补丁，是结构性缺口）

KEYS-1 的 residual_risk 原文：

> 身份签发服务仍以进程内组件（`services/identity_access.issue_token/get_key_store`）
> 而非独立部署单元存在；私钥边界已按 process_role 收口，但签发方进程尚未物理拆出。

根因拆解——**私钥边界靠「配置约定」隔离，而不是靠「部署单元」隔离**：

| 现状 | 问题 |
| --- | --- |
| `get_key_store()`（持私钥）与 `get_public_key_store()`（纯公钥）**同处一个模块** `services/identity_access/__init__.py`，`lru_cache` 单例共享 | API 进程 `import` 一下 `issue_token` 就能拿到私钥，只靠「API 进程清单不含私钥」的**约定**挡，而不是结构挡 |
| 私钥的隔离层是 `process_role` 配置字段（F05 已做） | 配置隔离是「声明」，运行时隔离是「部署」——红线明令禁止「用配置声明代替运行时鉴权/隔离」 |
| `apps/api/main.py` 只挂 4 个业务 router，**没有 JWKS 端点**（`__init__.py` docstring 声称「JWKS 端点对外暴露公钥」但实际不存在） | 验签方（API/网关）要轮换密钥，无正规公钥分发通道 |
| `issue_token` 只被 `tests/conftest.py` 调用，生产无签发入口 | 签发逻辑存在但未物理独立，也无独立进程承载 |

**结论**：F05 已经正确地把「私钥清单」按进程分列（配置层已收口），但**签发方和验签方仍编译进同一个镜像、同一个进程**。要彻底根治，必须把「持私钥的签发逻辑」从「只拿公钥的验签逻辑」在**进程边界**上切开——就像 relay 已经从 API 里拆成独立 `workers/relay` 进程一样。

---

## 2. 目标与成功判据

**目标**：让「签发私钥只存在于身份签发进程」成为**部署事实**而非配置约定。

**成功判据（可验证）**：
1. 存在独立进程 `workers/identity`（`python -m workers.identity`），持有私钥、负责签发；
2. API 进程的**代码路径上不存在任何能拿到私钥的 import**（验签只用 `PublicKeyStore`，结构上无私钥字段）；
3. API 进程经 JWKS 端点（HTTP）获取公钥验签，而非经 `from_key_store` 从私钥派生；
4. 新增契约不变量 `KEYS-3`：签发方与验签方进程物理分离，私钥不出签发进程；
5. 负向测试证明：API 进程 dump 内存拿不到私钥；identity 进程才能签发。

---

## 3. 现状盘点（关键事实）

### 3.1 已具备（可复用，无需重造）
- **进程维度已有**：`ProcessRole = Literal["api", "relay", "outbox_maintenance", "identity"]`，`identity` 角色已定义（`packages/shared/config.py:24`）。
- **密钥矩阵已分列**：`_PRODUCTION_REQUIRED_SECRETS_BY_PROCESS` 已把 `identity_private_key_pem` 只配给 identity 角色（F05）。
- **独立进程范式已有**：`workers/relay/__main__.py` 是成熟参照——`python -m workers.relay` + `get_process_settings("relay")` + 不起 HTTP 只循环。
- **纯公钥验签类已有**：`PublicKeyStore`（`packages/identity/jwks.py:55`）结构上**不含私钥字段**，验签路径本该只用它。
- **docker-compose 编排已有**：relay/outbox-maintenance 都是「同镜像 + 不同 entrypoint」的独立服务，identity 照抄即可。

### 3.2 缺口（本方案要补的）
| 缺口 | 说明 |
| --- | --- |
| `get_key_store`/`issue_token` 在 `services/identity_access` 里 | 签发逻辑与验签装配同层，未按进程物理分离 |
| 无 JWKS HTTP 端点 | docstring 声称有，实际 `apps/api/main.py` 未挂 |
| API 验签走 `get_public_key_store()` | 生产应经 JWKS/静态公钥配置，开发才 `from_key_store` 派生 |
| 无 identity 独立进程 | docker-compose 无 identity service |

---

## 4. 拆分设计

> **已拍板决策（2026-11-18）**：
> 1. JWKS 端点挂 **identity 进程**（公钥唯一权威源在签发方）。
> 2. 签发方代码迁到 **`services/identity_issuance`**（签发是身份领域的一部分，属 services 内核层，而非 worker 壳）。
> 3. identity 进程首期**最小闭环**：持私钥 + JWKS 端点，不新增 token 签发 HTTP 端点。

### 4.1 目标进程拓扑

```
┌─────────────────────────────────────────────────────────┐
│  镜像 ai-film-api:local（同一镜像，不同 entrypoint）      │
│                                                         │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐
│  │ api      │   │ relay    │   │ identity │   │ outbox_maint │
│  │ 持公钥    │   │ 无密钥    │   │ 持私钥    │   │ 无密钥        │
│  │ 验签      │   │ 消费事件  │   │ 签发      │   │ DDL 维护      │
│  └────┬─────┘   └──────────┘   └────┬─────┘   └──────────────┘
│       │                              │                        │
│       └────── JWKS(公钥) ────────────┘                        │
└─────────────────────────────────────────────────────────┘
```

- **identity 进程**：持私钥，负责签发令牌；对外提供（可选）JWKS 端点供验签方拉公钥。
- **api 进程**：只拿公钥验签；公钥来源 = 静态配置 `IDENTITY_PUBLIC_KEY_PEM`（生产）或经 JWKS 拉取。
- **relay/outbox_maintenance**：维持现状（不碰身份密钥）。

### 4.2 模块归属（消除「签发/验签同层」）

把 `services/identity_access/__init__.py` 里的**签发方函数**迁出，落到身份签发领域模块 `services/identity_issuance`：

| 现状 | 目标归属 | 说明 |
| --- | --- | --- |
| `get_key_store()`（持私钥） | `services/identity_issuance/`（签发领域） | 签发方专属，API 不 import |
| `issue_token()` | 同上 | 签发入口，只被 identity 进程/测试调用 |
| `get_signing_key()` | 同上 | 同上 |
| `get_public_key_store()`（纯公钥） | 保留在 `services/identity_access`（验签装配层） | API 用，结构上无私钥 |

**分层映射（遵循 architecture_check 六层规则）**：
- `services/identity_issuance`：签发领域逻辑，属 services 内核层（只依赖 packages）。
- `workers/identity`：进程壳，`import services.identity_issuance`，起 HTTP 暴露 JWKS。
- `apps/api`：验签方，只 import `services/identity_access`，**禁止 import `services/identity_issuance`**。

> 关键红线（新增架构闸门规则）：API 进程的 import 图里**不得出现**
> `services.identity_issuance`、`get_key_store`、`issue_token`、`get_signing_key`、
> `SigningKey`。验签装配只用 `PublicKeyStore`（`kid → 公钥 PEM`，无私钥字段）。
> 这条规则必须落到 `scripts/architecture_check.py` 的 LAYER_RULES，而非靠人工自觉。

### 4.3 JWKS 端点补全（契约对齐，不猜）

现状 docstring 声称有 JWKS 端点但实际没有——这是「文档与实现漂移」。本方案补一个**只读 JWKS 端点**（验签方拉公钥的标准通道），但这**属于契约变更**，须按治理流程登记：

- 新增 action？**不需要**——JWKS 是匿名只读的密钥分发端点，不是业务动作，不进入 Action Registry（类比 `/metrics`、`/readyz` 属于系统面）。
- 但需要在 openapi.draft.yaml 登记路径 + 在 contract_amendments.yaml 登记（新增端点属契约演进）。

> 待你拍板的点：JWKS 端点挂在 **identity 进程**（签发方暴露公钥）还是 **api 进程**？
> 推荐：挂在 identity 进程（公钥的唯一权威源在签发方），api 进程启动时/按需从 identity 拉 JWKS。

### 4.4 验签方公钥来源（api 进程）

当前 `get_public_key_store()` 生产从 `IDENTITY_PUBLIC_KEY_PEM` 构建（已正确），开发才 `from_key_store`（从私钥派生，这是**开发场景的越界残留**）。目标：

| 场景 | 公钥来源 | 说明 |
| --- | --- | --- |
| 生产 | 静态 `IDENTITY_PUBLIC_KEY_PEM`（密钥系统注入） | 已正确，保持 |
| 开发/测试 | 从 JWKS 拉取，或显式配置公钥 | **去掉 `from_key_store` 派生**，消除「API 经私钥派生公钥」的越界 |

> 关键：`PublicKeyStore.from_key_store` 这个类方法（`jwks.py:79`）是「验签方从签发密钥仓储派生」的桥，它是**开发期单进程的临时耦合**。拆分后应废弃或仅限 identity 进程内测试使用。

### 4.5 docker-compose 新增 identity 服务

照抄 relay 编排：
```yaml
identity:
  build: { context: ../.., dockerfile: infra/docker/Dockerfile.api }
  image: ai-film-api:local
  command: ["python", "-m", "workers.identity"]
  environment:
    <<: *db-env
    APP_ENV: ${APP_ENV:-development}
    # identity 进程持私钥（生产由密钥系统注入，这里仅占位）
    IDENTITY_PRIVATE_KEY_PEM: ${IDENTITY_PRIVATE_KEY_PEM:-}
    IDENTITY_PUBLIC_KEY_PEM: ${IDENTITY_PUBLIC_KEY_PEM:-}
  depends_on:
    migrate: { condition: service_completed_successfully }
  restart: unless-stopped
```

---

## 5. 拆分步骤（分阶段，每阶段可验证）

### S1：签发方模块物理迁出（不新增端点）
- 新建 `services/identity_issuance/`，把 `get_key_store`/`issue_token`/`get_signing_key` 迁入。
- `services/identity_access/__init__.py` 只保留 `get_public_key_store`（验签）。
- conftest 的 `issue_token` import 路径更新为 `services.identity_issuance`。
- 验证：`architecture_check` 新增规则——apps 层禁止 import `services.identity_issuance`。

### S2：废弃 `from_key_store` 派生桥
- `PublicKeyStore.from_key_store` 标记废弃或仅限 identity 进程/测试使用。
- API 验签公钥只来自静态配置 / JWKS，不再从私钥派生。

### S3：identity 独立进程 + docker-compose 编排
- 新建 `workers/identity/__main__.py`（`python -m workers.identity`）：import `services.identity_issuance`，起 HTTP 暴露 JWKS 端点（最小闭环）。
- docker-compose 加 identity service（同镜像 + 不同 entrypoint）。

### S4：契约登记 + 负向测试
- `contract_amendments.yaml` 登记 KEYS-3 新不变量（签发/验签进程物理分离）+ JWKS 端点。
- `security_boundary.yaml` 新增 KEYS-3，KEYS-1 residual_risk 从「待物理拆出」改为「已拆出」。
- 负测：API 进程 dump 内存无 `SigningKey`/私钥；identity 进程能签发。

### S5：真实 PG / 全量验证
- 复用现有 Docker 环境跑全量测试 + 闸门。

---

## 6. 明确不做（边界，防止范围膨胀）

- **不新增 token 签发 HTTP 端点**（`issue_token` 的 HTTP 化属另一契约决策，需另立方案；当前只做「进程物理分离」这个根因）。
- **不改 token 格式 / claim / 算法**（RS256 已定，本轮不动）。
- **不做密钥轮换编排**（KeyStore 已支持多 kid，轮换调度是运维面，非本轮）。
- **不做 OAuth/OIDC 完整协议**（只做 JWKS 公钥分发这一最小闭环）。

---

## 7. 决策结论（已拍板）

1. **JWKS 端点挂载位置**：✅ 挂 identity 进程（公钥唯一权威源在签发方）。
2. **签发模块落点**：✅ `services/identity_issuance`（签发是身份领域，属 services 内核层；`workers/identity` 只是进程壳）。
3. **identity 进程首期职责**：✅ 最小闭环（持私钥 + JWKS 端点，不新增 token 签发端点）。

---

## 8. 影响面与风险

| 影响 | 说明 |
| --- | --- |
| conftest 测试 import 路径 | `issue_token` 迁移后更新，测试继续用（测试可 import 签发方，测试是「签发方」语义） |
| `test_identity_public_store.py` | 验证 `get_public_key_store` 仍可用，需确认不依赖私钥派生 |
| 无环境风险 | 纯代码结构拆分 + docker 编排，不碰数据迁移 |

**残余风险**：密钥轮换、token 端点、OIDC 属后续演进，不在本轮。
