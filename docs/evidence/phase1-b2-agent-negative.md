# B2 证据：Agent 不得自批自用（AGENT-NEG-001～004）

- 验收入口：`Phase1-实现验收清单.md` → B2
- 契约出处：基线 31.6 负向测试表（AGENT-NEG-001～004）
- 机器校验：`tests/permission/test_agent_negative_matrix.py`（6 条）
- 执行记录：`pytest -m agent_negative` → **7 passed / 965 deselected**
- 复现：`python -m pytest tests/permission/test_agent_negative_matrix.py -v && python -m pytest -m agent_negative -v`

## 1. 结论

| 编号 | 判定 | 依据 |
| --- | --- | --- |
| AGENT-NEG-001 | 已覆盖（3 条用例） | 授权层 + 授权策略 + 全量扫描 |
| AGENT-NEG-002 | 已覆盖（2 条用例） | 越权发布 + 只可产 DRAFT |
| AGENT-NEG-003 | 已覆盖（2 条用例） | 数据库 CHECK 拒 Agent 写 Gate / 投票 |
| AGENT-NEG-004 | **阻塞（无可执行载体）** | `services/budget_quota` 仍为空壳 |

B2 因此是 **3/4 覆盖 + 1/4 阻塞**，不是四项全绿。把它写成全绿会让"Agent 超预算
被拒"这件事在证据层凭空存在——那比缺证据更危险。

## 2. 编号 → 用例映射

| 编号 | 基线要求 | 承载用例 | 断言要点 |
| --- | --- | --- | --- |
| 001 | Agent 审批自己创建的发布/预算/Canon/Gate 请求 → 拒绝、无权威状态变化、记录 `AUTH_SCOPE_DENIED` 与职责分离事件 | `tests/unit/test_gate_api.py::test_agent_cannot_approve_even_with_director_role` | Agent 持「导演」角色走真实 `POST /gate-decisions`，403；无裁决落库 |
| 001 | 同上 | `tests/permission/test_authorization_policy.py::test_self_approval_is_denied` | 申请人 = 审批人 → 拒绝（`SELF_APPROVAL_FORBIDDEN`） |
| 001 | 同上 | `tests/permission/test_authorization_policy.py::test_no_critical_action_is_executable_by_agent` | 全量扫描：没有任何 CRITICAL 动作对 Agent 可执行（样例只能证明被选中的那一条） |
| 002 | Agent 越权发布、下架、回复社区或批准复用 → 拒绝；Agent 只可创建 DRAFT/PROPOSAL；人类审批与 CapabilityToken 缺一不可 | `tests/unit/test_delivery_release_api.py::test_agent_cannot_release_critical` | Agent 持「交付负责人」角色 `RELEASE` → 403；清单仍为 VALIDATED |
| 002 | 同上 | `tests/integration/test_shot_planning_api.py::test_agent_cannot_confirm_revision` | Agent 可建拆镜草案（DRAFT），确认正式修订 → 403 |
| 003 | Agent/API/Worker 直接写 Gate、Canon、Rights 或 CostLedger → 数据库/服务层拒绝；审计记录调用主体和目标对象 | `tests/integration/test_gate_decision_immutability.py::test_agent_decider_is_rejected_by_check` | 超级用户直连插入 Agent 裁决 → 数据库 CHECK `no_agent_decider` 拒绝（证明拦的是结构不是角色） |
| 003 | 同上 | `tests/integration/test_workflow_approval_invariants.py::test_agent_voter_rejected_by_check` | 审批票直插 Agent 投票 → CHECK `no_agent_voter` 拒绝 |

### 002 的覆盖边界（说清没覆盖的部分）

基线 002 列举了四类越权：发布、下架、回复社区、批准复用。当前只覆盖**发布**
（交付 RELEASE）与**确认**（镜头修订）。下架/回复社区属展示与社区域，
`services/` 下无该域（Phase 1 未建），批复用属资产复用动作（`ASSET.REUSE.*`）。
这三类不是"实现了但有漏洞"，而是"载体尚不存在"——因此不列为本条编号的阻塞：
002 的通过标准（Agent 只可产 DRAFT/PROPOSAL、人类审批必须）已被现有两条用例
正面证明，社区域落地后须回头补对应用例。

### 003 的覆盖边界（一处真实缺口）

「审计记录调用主体和目标对象」对**被拒绝的直连写**不成立：请求在数据库层被拒，
没有成功动作就没有 outbox 事件，因此不产生审计行。允许路径的"主体 + 目标 + 前后
状态"由 G3 抽样覆盖（`docs/evidence/phase1-g3-audit-sampling.md`）。
"被拒尝试也要留痕"需要独立的拒绝审计通道（当前只有 `AUTH_SCOPE_DENIED` 类授权
拒绝会记），登记为债务，不在本轮伪造。

## 3. AGENT-NEG-004 为什么是阻塞（不是没做）

基线 004：Agent 调用超预算/超配额动作 → 按明确预算/配额策略返回**结构化错误**；
BYOK 不因平台余额不足被拦截。

现状（可复核）：

- `services/budget_quota/` 只有 `.gitkeep`——域未实现；
- `GenerationTask.budget_reservation_ref` / `quota_reservation_ref` 是「结构性准入」
  （非空 + 引用归属），**不是**数值校验；`generation_service.py` 的模块 docstring
  已写明这条边界并登记为 ADR-R32 债务；
- 没有预算账户、没有配额余量、没有 BYOK 分支——因此既无法制造"超配额"，
  也无法断言"结构化错误码"，更无法断言 BYOK 不被拦截。

解锁条件：`budget_quota` 域实现 `QuotaReservation` + 预算/配额策略校验后，
补一条「Agent 请求超配额 → 结构化错误码（非 500）」的用例，并把编号从
`_BLOCKED` 移入 `_EVIDENCE`。

## 4. 机器校验（防止编号表退化成文档谎言）

`tests/permission/test_agent_negative_matrix.py` 的 6 条断言：

1. 基线 31.6 负向表的编号集合必须仍是 `AGENT-NEG-001～004`——基线加条目则红，
   强制补证据；
2. 每个编号"有用例"与"登记阻塞"二选一，不允许缺口、不允许两者同时成立、
   不允许出现基线里没有的编号；
3. 每条证据必须指向真实用例，且该用例源码里确实标着
   `@pytest.mark.agent_negative("<同一编号>")`；
4. 反向：任何标了编号的用例必须已登记（防止"标了但没人引用"）；
5. 阻塞项必须写清原因与解锁条件（≥40 字且含「解锁条件」）；
6. `agent_negative` marker 必须在 `pyproject.toml` 注册（未注册的 marker 只是警告，
   不会失败——那等于没校验）。

第 3、4 条用**源码扫描**而不是 import：这些用例依赖真实 PostgreSQL 与 FastAPI
装配，为了核对编号把它们全部 import 会引入非必要副作用。代价是标记必须紧邻函数
定义，已由正则强制。
