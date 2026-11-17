# 深度审查报告六根因 · 最终实证核对（2026-11-17）

- 原审查对象：`AI_Creative_Media_Industrial_OS`，HEAD `7e1c1ad`
- 当前核对对象：同一仓库 `feat/identity-data-ownership` 分支，HEAD `2facf5bc92c2125fd72abc9c2551df0deed4f82a`
- 远端：`origin/feat/identity-data-ownership` 与本地同步
- 核对方式：只读盘点旧包 + 读源码/契约 + 跑全量闸门 + 逐条对照六根因
- 结论口径：**机制闭合 ≠ Phase 1 验收通过**。五项根因已形成结构性闭环；F5（Phase 1 主链）仍只闭合到交付审批/放行段，不得据此宣布正式验收通过。

---

## 1. 总判定

| 根因 | 报告中的问题 | 当前判定 | 关键证据 |
|---|---|---|---|
| F1 契约生命周期 | 派生文件与冻结基线脱钩，`contract_check` 自洽但无权威源 | **已闭合** | `scripts/freeze_manifest.py --check` 三层判定；修订 A1/A2；21 条变异测试 |
| F2 数据所有权 | 单一 `app_role` 跨模块写表；API 持有签发私钥 | **已闭合** | 按域数据库角色 + RLS；R1–R6 全 0；私钥边界四形态测试 |
| F3 身份与授权 | 只有 RBAC 角色交集；无真实 Capability；跨项目隔离缺 | **运行时已闭合** | 统一 `decide()` + Capability Token + 项目范围收窄 + jti 撤销；仍有契约未登记的 HTTP 签发/升级端点债务 |
| F4 数据不变量 | 资产 `status` 可绕过服务直改；Outbox 事实可 `UPDATE`；异常语义混用 | **已闭合** | 数据库状态转移表、列级授权、领域异常翻译、枚举双向闸门 |
| F5 Phase 1 主链 | 主链与交付闸门未落地 | **部分闭合** | D1–D4 已闭合 CREATE / Gate / 联合审批 / 交付审批消费；但 ACTIVE 39 中 implemented 17，22 项仍登记未实现 |
| F6 发布包治理 | zip 含 `.env` / `.venv` / `.git` / 缓存，无密钥扫描 | **已闭合** | commit-only 归档、双闸门、确定性清单、CI `--check`；旧包应视为已暴露 |

**总体结论**：原报告「未通过工程基线验收 / 需要架构收敛」的判断在当时成立；当前工作区已完成结构性收敛，
但 Phase 1 仍不具备正式验收条件。剩余缺口已显式登记，没有被闸门全绿掩盖。

---

## 2. F1：契约生命周期

### 报告主张

冻结基线是 75 Action / 76 Schema / 35 状态机；派生文件变成 81 / 79 / 38。
`contract_check` 只证明派生文件互相一致，无法证明仍符合 V2.0.5。

### 处置与证据

- 主文档进入 `docs/baseline/`，冻结清单进入 `contracts/phase0/`，新增
  `contracts/phase0/contract_amendments.yaml` 逐条记录 A1/A2 修订。
- `freeze_manifest.py --check` 做三层判定：
  1. 基线正文 SHA-256；
  2. 十一类产物 SHA-256 + 清单自指哈希；
  3. 实际名称集合 == 基线 ∪ 已批准修订（双向精确）。
- `--write` 只重签，不自动审批；变异验证证明「删掉已登记项再重签」仍会报红。

当前命令输出：

```text
[PASS] 契约冻结：基线 V205-R14 未变（9a9940c5523c2c96…） / 十一类产物哈希一致 /
       演进有据（3 条修订：Action 83 = 75+8，Schema 80 = 76+4，状态机 39 = 35+4）
```

判定：**已闭合**。`contract_check` 与 `freeze_manifest` 职责分离，不再互相替代。

---

## 3. F2：数据所有权与密钥边界

### 报告主张

所有业务模块共用一个 `app_role`，可跨 Schema 写表；API 与签发共用配置，API 进程持有私钥。

### 处置与证据

- 数据库按域拆角色/连接池：PROJECT_PLANNING / ASSET / QC_GOVERNANCE / WORKFLOW /
  IDENTITY / OUTBOX_RELAY，越权写在 PostgreSQL 层被拒。
- R4 闸门禁止业务模块自选连接身份；R5 保证 RLS 上下文只有一个注入点。
- `IDENTITY_PROCESS_ROLE=api|issuer` 分离；API 进程配置签发私钥即拒绝启动；
  `TokenIssuer` 必须显式持私钥才能构造；验签只认公钥 PEM / JWKS。

当前闸门输出：

```text
[PASS] 分层依赖：6 层规则 / R1 方向 0 违规 / R2 跨服务 0 违规 /
       R3 导入期循环 0 处 / R4 数据库身份旁路 0 处（装配点 6 个） /
       R5 RLS 上下文仅由 packages.shared.rls 注入 /
       R6 domain/service 层 0 处框架依赖
```

判定：**已闭合**。残留债务是「独立 issuer 部署与 JWKS 发布端点未实现」，属部署形态债务，
不是「API 能签令牌」的反向突破；已由 `test_identity_private_key_boundary` 钉住。

---

## 4. F3：身份、ABAC 与 Capability Token

### 报告主张

"RBAC" 实为角色集合交集；`scope` / `risk_level` 契约字段没进运行时；Agent 可通过 CRITICAL；
没有 Capability Token，跨项目隔离不可证明；职责分离散落多份。

### 处置与证据

**B2（统一决策）**：`Principal + Action + Resource + Context → AuthorizationDecision`，
首个命中的拒绝胜出。规则覆盖：未注册动作、允许主体、联合审批 AND 语义、
角色、Agent/CRITICAL、自批、租户/组织/项目范围。

**B3（项目归属）**：组织/项目进入 Principal 与数据库 RESTRICTIVE 策略；
capability 只能收窄，带 `*` 但 principal 非组织级范围时仍拒绝。

**F3（本次）**：Capability Token 进入运行时：

- 独立 audience `ai-film-capability`，访问令牌不能重放；
- 默认 5 分钟、上限 15 分钟，issuer 进程签名，API 只验公钥；
- `X-Capability-Token` 验签后挂到 Principal，仍由同一个 `decide()` 消费；
- 校验主体/租户/组织/动作/项目范围，按 `jti` 走 denylist 撤销；
- 审计决策新增 `capability_jti`，不记录 token 原文。

关键测试：

```text
tests/permission/test_authorization_policy.py   统一决策正/负向 + 契约穷举
tests/unit/test_capability_token.py             签发/验签/过期/重放/撤销/HTTP 强制
tests/unit/test_project_scope.py                同租户跨项目隔离
```

判定：**运行时已闭合**。仍需诚实登记：

1. `POST /authorization/capabilities` 与 `/escalation-requests` 未进冻结 Action Registry，
   本轮不猜契约、不新增端点；
2. `nonce` 已签发并验签，但没有 `auth.consumed_capabilities` 唯一约束，
   「一次性」尚未在数据库层执行；
3. `budget_scope` / `max_amount` / `resource_quota` 被保留并进入审计，
   实际扣减仍属于预算/配额命令职责。

---

## 5. F4：数据不变量

### 报告主张

状态可被直接 `UPDATE status` 绕过；Outbox 事件正文可改；`IntegrityError` 泄漏到 service 层；
枚举值在契约/代码/迁移三处漂移无人发现。

### 处置与证据

- `asset_status_transitions` / `delivery_manifest_status_transitions` 落库，
  应用角色只读；触发器校验每条未登记边，超级用户直连也被拒。
- Outbox 事实列 append-only；只有 relay 角色拿到 4 个投递状态列的列级 `UPDATE`。
- 仓储异常翻译到领域异常：`DuplicateEntityError` /
  `ConcurrentModificationError` / `ReferenceViolationError` /
  `IntegrityViolationError`；service 层不再 import SQLAlchemy 异常。
- `contract_check` 双向比对代码枚举与契约，三类字面量漂移都报红。

当前输出节选：

```text
[PASS] AssetVersion 状态机一致：4 条转移
[PASS] DeliveryManifest 状态机一致：3 条转移
[PASS] ApprovalTicket 状态机一致：2 条转移
[PASS] 枚举一致性：5 个绑定逐值相等 / 显式豁免 9 项
```

判定：**已闭合**。

---

## 6. F5：Phase 1 主链与交付闸门

### 报告主张

AI 拆镜→人工确认→ShotSpec→QC→Gate→DeliveryManifest 主链未落地；
`ACTIVE` 只是契约状态，不等于已实现、集成通过或生产验收。

### 已闭合的段落

| 提交 | 内容 | 证据 |
|---|---|---|
| `ecd682b` (D1) | `DELIVERY.MANIFEST.CREATE` → `POST /delivery-manifests`；append-only 证据引用 | `test_delivery_api.py` / `test_delivery_manifest_immutability.py` |
| `085db0c` (D2) | `PRODUCTION.GATE.APPROVE` → `POST /gate-decisions`；不可改写事实 | `test_gate_api.py` / `test_gate_decision_immutability.py` |
| `3ed618a` (D3) | 通用审批工作流；AND/OR 收敛；Outbox 解耦 | `test_approval_service.py` / `test_workflow_approval_invariants.py` |
| `a3e7dc2` (D4) | 交付清单 APPROVE 事件消费 + RELEASE 授权放行；职责分离 | `test_delivery_approval_service.py` / `test_delivery_release_api.py` / `test_delivery_approval_consumption.py` |

### 仍未闭合

当前 `contract_check` 输出：

```text
[PASS] 运行时 ↔ 契约覆盖：ACTIVE 39 个（已实现 17 / 登记未实现 22）
[PASS] 状态可达性：6 个已实现状态机；显式登记不可达 10 项
[PASS] 状态转移入口债务：显式登记 2 项
```

仍有 22 条 ACTIVE 动作处于「契约已激活、实现已登记但未提供」状态；
`workers/generation`、`workers/qc`、`workers/notifications` 仍为占位；
PictureLock / 权利证据 / 终检报告的跨域存在性校验尚未接入；
delivery worker 的租户发现尚需人工配置。

判定：**部分闭合**。D4 闭合的是「Gate → 审批 → 交付放行」这一段，
不是整条 Phase 1 主链；因此不能宣布 Phase 1 通过验收。

---

## 7. F6：发布包治理

### 旧包只读盘点

| 项 | 旧 ZIP |
|---|---:|
| 文件大小 | 116,578,395 字节 |
| 条目 | 14,914 |
| `.git/` | 272 |
| `.venv/` | 14,172 |
| `__pycache__/` | 3,068 |
| `.env` / `.env.*` | 2 |
| `.workbuddy/` | 5 |

旧包含本地 `.env`，其中存在数据库 / S3 / 模型网关等凭据键。无论值是否只是本机占位，
都应视为**已暴露面**：非占位值必须轮换。

### 新机制

- `git archive <resolved-commit>`：只含受版本控制文件，本地 `.env`/`.venv`/缓存物理上不可能进包；
- 写包前依次跑禁止路径闸门和密钥扫描；命中只记路径/行号/规则/指纹，不记明文；
- 已知测试夹具走 `release_secret_allowlist.yaml`，必须写路径+规则+指纹+理由；
- ZIP 顺序/时间戳/权限固定；`RELEASE_MANIFEST.json` 记录 commit 与逐文件 SHA-256；
- `--verify` 独立验证；`--check` 供 CI 构建临时包后即验证，不落盘源码。

当前产物（gitignored）：

```text
dist/ai-creative-media-industrial-os.zip
commit=2facf5bc92c2  files=362
sha256=6d407e4cf1d8d90e4fd57dd268ab62526233b3321b4029b3577e84a0187765ca
```

判定：**已闭合**。压测与故障演练证据不在此闸门覆盖范围内。

---

## 8. 当前全量闸门（最终提交 2facf5b）

| 闸门 | 命令 | 结果 |
|---|---|---|
| 风格 | `ruff check .` | All checks passed |
| 类型 | `mypy packages services apps workers` | Success，133 source files |
| 测试 | `pytest tests` | 662 collected，全绿 |
| 架构 | `python scripts/architecture_check.py` | R1–R6 全部 0 违规 |
| 冻结 | `python scripts/freeze_manifest.py --check` | 基线未变，产物哈希一致，3 条修订 |
| 契约 | `python scripts/contract_check.py` | 83 actions / 80 schemas / 19 events / 枚举一致 |
| 发布 | `python scripts/release_package.py --check` | 362 files，临时包验证通过 |
| 迁移 | `alembic check` | No new upgrade operations detected |

`contract_check` 中的 `PENDING_PHASE1_REVIEW` 警告是刻意保留的：它表示状态机虽与代码一致，
但仍等 Phase 1 验收复核固化，不能被当成错误消除。

---

## 9. 提交与远端

```text
2facf5b feat(F3): Capability Token 运行时强制（双凭证验签、统一决策消费、范围收窄与撤销）
c2c9104 fix(F6): 登记发布治理测试夹具到密钥放行清单（路径+规则+指纹+理由）
961a8c1 feat(F6): 发布包治理落地（commit 来源唯一、禁止路径与密钥扫描、确定性清单）
a3e7dc2 feat(D4): 交付清单审批消费闭环（APPROVE 事件驱动 / RELEASE 授权放行）
3ed618a feat(D3): 通用审批工作流引擎落地（AND/OR 收敛、Outbox 解耦、并发可控）
085db0c feat(D2): Gate 决策链落地（不可改写的事实；描述性契约主体可校验解析）
ecd682b feat(D1): 交付清单 CREATE 落地（append-only 证据引用，不声明未发生的事实）
30241f4 feat(data): 状态机与枚举取值下沉为数据库约束，仓储异常语义拆分（ADR-R20/R21）
719fd0c feat(auth): 签发私钥移出 API 进程，验签方只持有公钥（ADR-R19）
```

以上提交均已推送到 `origin/feat/identity-data-ownership`，工作树干净。

---

## 10. 下一步（不做假闭环）

按优先级，Phase 1 正式验收前仍需：

1. 补齐剩余 22 条已登记未实现的 ACTIVE 动作，或明确降级/移出 ACTIVE 的契约修订；
2. 接入 generation / QC / notifications worker，闭合主链端到端证据；
3. 落地方向性债务：PictureLock / 权利证据 / 终检报告存在性校验；
4. Capability HTTP 签发/升级端点需先走契约修订，再实现；nonce 一次性落库去重独立成包；
5. 压测与故障演练证据（高并发/高流量/高标准的可观测项）；
6. 轮换旧 ZIP 中可能暴露的非占位凭据。

这些事项不属于「再加一个校验函数」能解决的问题；每项都应有契约、边界、负向测试与证据。
