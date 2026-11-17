# Phase 1 验收证据 · C5「核心 QC」

- 验收项：Phase 1 验收清单 C5（契约出处 21.1、QCReviewSession 状态机 / C.7、
  31.5 裁决 ④、4.3 QC 严重级别、21.2 人机分工）
- 通过标准：**一次含缺陷的 QC 全流程；缺陷绑定 `QCIssue` 必填项；
  `RESOLVED→CLOSED` 跳关被拒绝（须经 GATE_ELIGIBLE）**
- 实现与决策：`docs/adr/ADR-R36-qc-review-session.md`（契约修订 V205-R14-A10）
- 取证日期：2026-11-17
- 取证环境：本仓库 `docker compose` 提供的真实 PostgreSQL（`localhost:15432`），
  真实 JWT + Capability + RLS + 域角色（`svc_qc_governance` / `svc_asset`），
  无 mock；数据库守卫用例另用迁移角色（超级用户）直连，证明拦的是结构而非权限

## 1. 证据清单

### 1.1 缺陷九项必填（21.1）

| # | 证据 | 位置（测试用例） | 断言要点 |
| --- | --- | --- | --- |
| E1 | 缺必填项即拒，且能定位是第几条 | `tests/integration/test_qc_review_api.py::test_missing_required_issue_field_is_rejected` | 422 `VALIDATION_ERROR`，正文含 `findings[1]` 与 `evidence`；第二条缺陷缺 evidence |
| E2 | 空白串等同于没填 | `tests/unit/test_qc_review_domain.py::TestQCIssueValidation::test_blank_required_field_is_rejected` | `shot_id="   "` → `missing_fields` 含 `shot_id`（`None`/`""`/纯空白三态同口径） |
| E3 | 缺任一字符串必填项都报字段名 | `::TestQCIssueValidation::test_missing_required_field_is_rejected_with_field_name` | `missing_fields == ["evidence"]`、`issue_index == 2` |
| E4 | 检测器多给的字段不被裁剪 | `tests/integration/test_qc_review_api.py::test_response_keeps_detector_extra_fields`、`tests/unit/test_qc_review_domain.py::TestQCIssueValidation::test_detector_extra_fields_are_preserved` | 响应 `findings[0].confidence == 0.72`；`timecode` 原样保留 |
| E5 | 缺陷级别不含 `BLOCKER` | `tests/unit/test_qc_review_parity.py::test_issue_severity_matches_4_3` | 取值集合 == `{CRITICAL, MAJOR, MINOR, OBSERVATION}`；`BLOCKER` 属 7.6 返工单级别 → 422 |
| E6 | 契约外 severity 被拒并回报可选值 | `::TestQCIssueValidation::test_unknown_severity_is_rejected_with_allowed_values` | `severity="BLOCKER"` → 422，detail 含 `CRITICAL` |

### 1.2 含缺陷的 QC 全流程（C.7 状态链）

| # | 证据 | 位置 | 断言要点 |
| --- | --- | --- | --- |
| E7 | 开启会话落 OPEN + 缺陷落库 + 审计事件同事务 | `tests/integration/test_qc_review_api.py::test_open_session_registers_findings_and_emits_event` | 201；outbox 有 1 条 `QC_REVIEW_OPENED`，payload 的 `issue_count == 2`、`status == "OPEN"`、`created_by == "qc_reviewer"` |
| E8 | 全链走通到 CLOSED | `::test_full_chain_reaches_closed_and_jump_is_denied` | `IN_REVIEW → DECISION_PENDING → RESOLVED → GATE_ELIGIBLE → CLOSED`，每步 200 且响应 `status` 逐跳正确 |
| E9 | **跳关拒绝**（31.5 裁决 ④） | 同上 + `::test_jump_from_in_review_to_resolved_is_denied` | `RESOLVED→CLOSED` → 409 `STATE_TRANSITION_DENIED`，`details.allowed_targets == ["GATE_ELIGIBLE"]`；`IN_REVIEW→RESOLVED` → 409，`allowed_targets == ["DECISION_PENDING"]` |
| E10 | 结论未齐不得进 DECISION_PENDING | `::test_decision_pending_requires_every_issue_decided` | 只判 i1 → 422，正文列出未裁决的 i2（前置条件 = 422，不是 409） |
| E11 | 证据不足不得进 RESOLVED | `tests/unit/test_qc_review_domain.py::TestSessionLifecycle::test_resolved_requires_no_unresolved_evidence` | `NEEDS_MORE_EVIDENCE` → `unresolved_issue_ids == ["i1"]` |
| E12 | 同一报告不能有两条复核链 | `::test_same_report_open_is_idempotent_and_conflict_is_rejected` | 同内容重复提交 → 同一 `session_id`；改缺陷集 → 409 `IDEMPOTENCY_CONFLICT` |
| E13 | 一份报告只能开一次（结构层） | 迁移 `f1a2b3c4d5e6` | `uq_qc_governance_qc_review_sessions_report` UNIQUE `(tenant_id, project_id, qc_report_ref)` |

### 1.3 结论只追加、身份不可伪造、红线不可豁免

| # | 证据 | 位置 | 断言要点 |
| --- | --- | --- | --- |
| E14 | 改判 = 追加，最新一条生效 | `::test_decisions_are_append_only_and_latest_wins` | `decisions` 长度为 2、顺序为 `[CONFIRM_DEFECT, FALSE_POSITIVE]`；领域层 `latest_decisions()["i1"]` 为后者 |
| E15 | RESOLVED 之后拒绝新结论 | `tests/unit/test_qc_review_domain.py::TestSessionLifecycle::test_resolved_session_rejects_new_decisions` | 409 `STATE_TRANSITION_DENIED` |
| E16 | 请求体塞 `decided_by` 无效 | `::test_decided_by_cannot_be_forged_in_body` | 落库 `decided_by == "qc_reviewer"`（来自令牌），请求体伪造值被忽略 |
| E17 | AI 不能单独裁决（21.2） | `::test_agent_cannot_decide` | Agent 主体带齐「QC 审核人/导演」角色 → 403 `AUTH_SCOPE_DENIED` |
| E18 | CRITICAL 不得豁免（4.3 / 21.1） | `::test_red_line_issue_cannot_be_waived` | 带齐授权与理由仍 422，正文含「不得以任何理由豁免」 |
| E19 | 非 CRITICAL 豁免需授权+理由 | `tests/unit/test_qc_review_domain.py::TestReviewDecision::test_waiver_requires_authority_and_reason`、`::test_non_critical_waiver_is_allowed_with_authority` | 缺一 → 422；齐备 → 落 `waiver_authority` |
| E20 | `PENDING` 不是结论 | `::TestReviewDecision::test_pending_is_not_a_decision` | 422（用它推进等于把未裁决算成已裁决） |
| E21 | 会话外缺陷不得下结论 | `::TestSessionLifecycle::test_unknown_issue_decision_is_rejected` | 422，detail 含「不在本次会话内」 |

### 1.4 跨域引用与授权

| # | 证据 | 位置 | 断言要点 |
| --- | --- | --- | --- |
| E22 | 报告不存在 → 404 | `::test_ghost_report_is_not_found` | 404 `NOT_FOUND`（不区分"不存在"与"不可见"，防跨租户探测） |
| E23 | 报告归属不符 → 422 | `::test_report_from_other_project_is_rejected` | 报告属别的项目 → 422，正文含「不符」 |
| E24 | 报告绑定版本不符 → 422 | `::test_report_version_mismatch_is_rejected` | 资产版本不一致 → 422，正文含「资产版本」 |
| E25 | 未授权角色不得开启会话 | `::test_unauthorized_role_cannot_open_session` | 角色「制片」→ 403 `AUTH_SCOPE_DENIED`（fail-closed） |

### 1.5 并发与数据库层独立兜底（ADR-R20）

| # | 证据 | 位置 | 断言要点 |
| --- | --- | --- | --- |
| E26 | 并发裁决按 `entity_version` CAS | `::test_concurrent_decide_conflicts_on_version` | 两个会话读到同一版本，先写者成功、后写者 → `CONCURRENT_MODIFICATION` |
| E27 | 直连 SQL 跳关被拒 | `::test_db_guard_blocks_unregistered_transition` | 超级用户 `UPDATE ... SET status='CLOSED' WHERE status='RESOLVED'` → 触发器「非法状态转移」 |
| E28 | 已登记边仍可通行 | `::test_db_guard_allows_registered_transition` | `RESOLVED→GATE_ELIGIBLE` 直连 SQL 成功（否则"拦住了"可以靠整列冻结伪造） |
| E29 | 复查输入不可改写 | `::test_db_guard_blocks_findings_rewrite` | 改 `findings` → 「不可变内容列，禁止改写」 |
| E30 | 会话不可删除 | `::test_db_guard_blocks_delete` | `DELETE` → 「禁止物理删除」 |
| E31 | 契约外状态写不进 | `::test_db_rejects_unregistered_status_on_insert` | INSERT `status='ARCHIVED'` → 违反 `ck_qc_governance_qc_review_sessions_status_registered` |
| E32 | 空会话不得存在 | `::test_db_rejects_empty_findings_on_insert` | INSERT `findings='[]'` → 违反 `..._findings_not_empty` |
| E33 | 四副本一致性 | `tests/unit/test_qc_review_parity.py`（11 例） | 契约链展开边集 == 领域表 == 迁移字面量；枚举 == enum_registry == ORM 字面量；`RESOLVED→CLOSED` 在**四份副本**里都不存在；`WAIVE_WITH_AUTHORITY` 三份取值集合都有它、三份转移边都没有它 |

## 2. 复现命令

```bash
docker compose -f infra/compose/docker-compose.yml up -d
python -m alembic upgrade head

# E1、E4、E7—E10、E12、E14、E16—E18、E22—E32（真实库、真实鉴权）
pytest tests/integration/test_qc_review_api.py -v

# E2、E3、E5、E11、E15、E19—E21（纯领域规则，无外部依赖）
pytest tests/unit/test_qc_review_domain.py -v

# E33（四副本一致性）
pytest tests/unit/test_qc_review_parity.py -v
```

实测结果（2026-11-17）：三个文件合并运行 **57 passed in 4.44s**
（集成 22 / 领域 24 / 一致性 11）。全仓 `pytest -q` 亦全绿；
`ruff check .`、`mypy packages services apps workers`、`architecture_check.py`、
`contract_check.py`、`alembic check` 均通过。

## 3. 为什么这些断言足以覆盖 C5

C5 的措辞里有两个容易被跳过的成分：**"含缺陷"** 与 **"跳关被拒绝"**。

- **"含缺陷"** 不是"跑一次 QC"，而是"每条缺陷都能回答 21.1 的九个问题"。
  只断言"会话创建成功"会漏掉最关键的一条：缺陷是不是**可复核**的。E1—E6
  覆盖必填项与取值（含"缺一即拒"与"多给不裁"），E7 证明它真的落库并进了审计。
- **"跳关被拒绝"** 若只在服务层验证，证明的只是"我们的代码记得检查"。E27
  用**超级用户直连 SQL**（绕过全部应用代码与角色权限）仍然被触发器拒绝——
  这才证明它是一条结构事实。E28 是配套的反向断言：如果哪天守卫把整列冻死，
  E27 依然绿而 E28 会红。
- **"一次全流程"** 要求链上的每一步都真实走过：E8 逐跳断言 6 个状态，
  E10/E11 断言"不能跳过前置条件"，E9 断言"不能跳过裁决"。
  只跑正向路径的用例无法区分"状态机正确"与"状态机只实现了正向"。

另外三条口径容易被漏掉，也各有证据：

1. **结论的历史不可改写**（E14/E15）：QC 的价值全在"谁在何时判了什么"这条链上，
   覆盖式更新会让它永久丢失；
2. **身份不能由客户端声明**（E16）：能填就是身份伪造，与 GateDecision 同口径；
3. **AI 不得单独裁决**（E17）：21.2 的分工如果只写在文档里，Agent 拿着人类角色
   就能下结论——E17 证明它在领域层就被拦。

## 4. 证据边界（不声称的部分）

1. **不覆盖返工闭环**：`QCIssue` 未建独立表，派发 `ReworkTask`、修复后复核关闭
   均未实现（ADR-R36 §3 债务 1、8）。因此本证据的"全流程"止于会话 `CLOSED`，
   不含"缺陷被修复"这一段。
2. **不覆盖多人冲突裁决**：`reviewers` 是参与人记录，不是会签；C.7 note 的
   "多人冲突触发裁决"未实现（ADR-R36 §3 债务 2/3）。E26 证明的是**同一实体
   并发写**的乐观锁，不是"两个复核人结论不一致"的裁决机制。
3. **不覆盖 Gate 放行**：`GATE_ELIGIBLE` 只表示"证据齐备、可以进 Gate"。
   Gate 0—4 的决策记录属 C6（`tests/unit/test_gate_api.py`），本证据不声称
   "QC 通过的资产可以被批准"。
4. **不覆盖读接口**：动作矩阵无 QC 复查的 READ 动作，本轮不暴露读端点
   （ADR-R36 §3 债务 5），因此会话只能在开启/裁决响应中观察到。
5. **并发是单进程内 asyncio 并发**：E26 采取"两个会话读同一版本、各写一次"的
   确定性构造，而不是"并发发两个 HTTP 请求看谁输"——后者取决于事件循环调度，
   在快机器上可能两笔都成功（第一笔提交后第二笔才读到新版本），用例会时红时绿。
   跨进程/跨实例的并发机制相同（CAS 在库里），但本段未单独做多进程压测。
6. C5 通过**不**意味着 Phase 1 其余验收项通过。
