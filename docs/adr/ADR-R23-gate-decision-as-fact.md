# ADR-R23：Gate 裁决是不可改写的事实；描述性契约主体用可校验台账解析

- 编号：ADR-R23
- 状态：已采纳
- 相关包：D（Phase 1 主链与交付闸门）
- 契约依据：正文 7.4（Gate 通用契约）/ 7.5（Gate 0—4）/ 34.5（CREATOR_GATE 与工业 Gate 隔离）；
  `action_registry.yaml#PRODUCTION.GATE.APPROVE`（CRITICAL，`ACT-GATE-NEG-001`）；
  `object_schemas.schema.json#GateDecision`；`ADR-R22`（交付清单 APPROVE 的前置）

## 背景

`PRODUCTION.GATE.APPROVE` 是契约里唯一的 Gate 动作，运行时此前没有任何落点。
这意味着"Gate4 PASS"从未在平台上真实发生过，而 `DeliveryManifest.APPROVE/RELEASE`
又以它为前提。补链时遇到两个不能靠写更多 `if` 解决的问题：

1. **裁决是可改行还是不可改事实**：若允许 UPDATE，那么"当时批的是什么"不再可信，
   Gate 通过率、交付合法性、事后追责全部失去锚点；
2. **契约的允许主体不是角色**：`allowed_principals: '对应 Gate 授权人类职责主体'`
   是描述性短语。解析器按 `/` 切分后得到一个字面角色名，任何令牌都不可能命中——
   动作"已实现"却无人能调用，而所有静态闸门仍全绿。

## 决策

### 1. 裁决是事实，不是可改的表

- `GateDecisionRepository` 只有 `add` / `get_by_id` / `find_by_idempotency_key` /
  `list_by_instance`，**没有 save**；
- 数据库只授 `svc_qc_governance` `SELECT, INSERT`，并加 append-only 触发器兜住
  超级用户与迁移角色；
- `status` 恒为 `RECORDED`；`GateDecisionStatus` 只有一个取值，不是状态机。
  契约 7.4 的「输入变化 → 原 Gate 标记 STALE」描述的是 **Gate 实例**的有效性，
  把它写进裁决行就等于允许改写历史。"当前是否通过"是派生问题：取同一实例最新
  APPROVED，再比对其 `constraint_snapshot_id` 与当前快照（尚未实现，见第 6 节）。

裁决可以重做，做法是**新建一条**（新快照 + 新理由 + 新证据）；轨迹因此天然完整。

### 2. 两条红线压到数据库 CHECK 上

- `decided_by_subject_type <> 'agent'`：授权层拦的是"发起请求主体"，落库的是
  "被记为决策人的主体"。两者必须同时成立，否则手工插入一条 Agent 裁决就能
  绕开全部应用代码。该列是内部列，契约 `GateDecision` 没有它，不进响应。
- `decision <> 'APPROVED' OR jsonb_array_length(evidence_refs) > 0`：缺证据不得 PASS。
  否决/退回不强制证据，理由由 `reason_code` + `comment` 承载。

`decided_by` 只能从已验签 `Principal` 取得：`GateDecision.record()` 不接受该参数，
路由请求体也不接受该字段。身份伪造在构造阶段就无路可走。

### 3. 四种结论，三个事件名

契约按"动作"登记（`PRODUCTION_GATE_APPROVE` 一个动作），事实却有
APPROVED / REJECTED / RETURNED 三种结论。事件按结论分开登记，否则"否决"会被
记成 `APPROVED`，污染 Gate 通过率的分子分母——这类审计污染事后几乎无法还原。

事件名**刻意以字面量出现在三个发出分支**，不抽成 dict 再动态取值：契约闸门靠
字面量扫描发现"代码发了未登记事件"，映射表会让扫描器失明，而失明的闸门比没有
闸门更危险。

`OVERRIDDEN` 是第四种结论：契约 7.4 列了它，但没有"覆盖了哪一条决策"的引用字段。
枚举保留该值（`enum_parity` 要求逐值相等），领域层拒绝其进入，登记为债务。

### 4. 描述性主体的运行时解析：有原文指纹的台账，不是猜测

新增 `contracts/runtime/principal_bindings.yaml`，把 `PRODUCTION.GATE.APPROVE`
的描述性原文显式解析为 `导演`：

- **不改冻结契约**。契约修订需走正式流程；这是运行时解释，不是改原文。
- **逐字校验原文**。契约原文变化而台账没跟上时，加载期 fail-fast（结构化
  `INTERNAL_ERROR`），绝不按旧解释继续放行；`contract_check.check_principal_bindings`
  独立再校验一次，不依赖运行时恰好被走到。
- **最小授权**。取契约中其它 CRITICAL 生产审批动作（脚本锁定、镜头规格锁定、
  约束包锁定、资产版本审批）共同包含的「导演」，不擅自扩大到制片/QC/交付负责人。
- **必须可解除**：契约 7.5 为各 Gate 定义职责主体或引入 CapabilityToken 后删除绑定。

### 5. W2 职责分离缺口的窄口径豁免

`decide()` 对审批动作缺 `applicant_subject_id` 时返回 ALLOW 并把
`separation_of_duties` 记进 `deferred_checks`。授权接线闸门 W2 用 AST 禁止这种
"永久 deferred"，它对 Gate 动作报红——这是正确的报警。

但冻结契约里 Gate 链**没有申请人载体**：无 GATE.REQUEST 动作、`GateDecision`
无 applicant 字段、无 Gate 实例提交记录。从请求体收一个字符串当申请人，等于把
"谁提请的"交给调用方声明，会制造假的审计事实，比显式登记缺口危险。

因此给 W2 增加**按 (文件, 动作) 精确匹配**的豁免，必填 `reason` /
`unblocks_when` / `compensating_control`，并新增测试保证它不是陈旧条目。
补偿性控制在 Gate 的领域/数据库测试中被真实断言（Agent 主体三层拒绝、
`reviewers` 必须包含决策人）。

## 闸门证据

| 证据 | 证明什么 |
| --- | --- |
| `tests/unit/test_gate_decision_domain.py` | APPROVED 缺证据拒绝、Agent 拒绝、OVERRIDDEN 不可进入、身份来自 Principal、to_dict 字段集 == 契约 |
| `tests/unit/test_gate_service.py` | 幂等（同 id / 同键）、内容冲突 409、并发路径同判定、三种事件名、成功才发事件 |
| `tests/unit/test_gate_api.py` | 导演可批、制片/Agent/其它角色 403、跨项目 403、伪造身份/时间无效、重放只发一次事件 |
| `tests/integration/test_gate_decision_immutability.py` | UPDATE/DELETE 被触发器拒绝（含超级用户）、CHECK 三条、唯一索引、只授 SELECT/INSERT、RLS 双策略 |
| `tests/permission/test_principal_bindings.py` | 解析生效、原文漂移 fail-fast、闸门独立发现台账漂移 |
| `tests/permission/test_authorization_wiring.py` | W2 豁免必须精确、理由完备、非陈旧 |

## 未闭合（诚实登记）

1. **逐 Gate 授权主体**：当前统一绑定「导演」；契约 7.5 未给 Gate 0—4 与 A2
   子 Gate 各自的职责主体，细化需契约修订。
2. **申请人与批准人分离**：W2 精确豁免已登记；解锁条件见第 5 节。
3. **Gate 实例解析与 STALE 派生**：没有 GateInstance 对象与"当前约束快照"来源，
   无法判断一条历史 APPROVED 是否仍代表当前输入。
4. **多人会签 / 审批顺序**：契约 7.4 要求，`reviewers` 只是参与人记录，不构成签名证据。
5. **跨域证据有效性**：只校验 `evidence_refs` 非空，未校验被引用对象真实存在。
6. **补录历史裁决**：`decided_at` 由服务端取当前时间，不能指定过去时间。
7. **交付清单 APPROVE / RELEASE**：前置的 Gate 决策链已落地，但这两个动作本身
   仍未实现（缺 PictureLock、终检报告、权利证据的跨域校验与 DeliveryPackage）。
8. **重建执行器 / 各 Worker**：`workers/*` 仍是空目录。
