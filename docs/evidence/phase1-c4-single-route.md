# Phase 1 验收证据 · C4「单一路线生成」

- 验收项：Phase 1 验收清单 C4（契约出处 31.2 Phase 1 核心生产主链、预算口径）
- 通过标准：**并发发起两条路线时，首发主链拒绝第二条路线并给出结构化错误，
  不得静默排队**
- 实现与决策：`docs/adr/ADR-R35-shot-route-selection.md`（契约修订 V205-R14-A9）
- 取证日期：2026-11-17
- 取证环境：本仓库 `docker compose` 提供的真实 PostgreSQL（`localhost:15432`），
  真实 JWT + Capability + RLS + 域角色，无 mock

## 1. 证据清单

| # | 证据 | 位置（测试用例） | 断言要点 |
| --- | --- | --- | --- |
| E1 | 并发两条路线，恰好一条成功 | `tests/integration/test_generation_route_api.py::test_concurrent_two_routes_exactly_one_wins` | `sorted(status) == [200, 409]`；409 的 `details.effective_route_id` == 成功那条；库里 `count(selections)=1`、`count(events)=1` |
| E2 | 唯一约束独立于服务层存在 | `::test_unique_constraint_rejects_second_row_without_service` | 直连 SQL 插入同 (tenant, project, shot) 的第二行 → 违反 `ux_shot_route_selection_effective`；库里仍只有 1 行 |
| E3 | 顺序选择第二条路线被拒 | `::test_second_route_rejected_with_structured_error` | 409 `ROUTE_ALREADY_SELECTED`、`category=CONFLICT`、`retryable=false`、details 含既有路线/选择 id/请求路线；无副作用（1 行 1 事件） |
| E4 | 同路线幂等 | `::test_reselect_same_route_is_idempotent` | 第二次 200、`selection_id` 相同、`idempotent_replay=true`；仍 1 行 1 事件 |
| E5 | 主链内联负向（选路面 + 受理面） | `tests/integration/test_main_chain_e2e.py`（选路步骤与生成受理步骤之间） | 第二条路线 409 `ROUTE_ALREADY_SELECTED`；在第二条路线上受理生成 409 `ROUTE_NOT_EFFECTIVE`；随后正向受理 200 |
| E6 | 受理侧单一路线 | `tests/unit/test_generation_task_service.py::test_non_effective_route_rejected`、`::test_route_without_effective_selection_rejected` | 未选路 409；路线与生效路线不一致 409（`details.effective_route_id`）；不发受理事件 |
| E7 | 生效路线不可改写 | `tests/integration/test_generation_route_api.py::test_effective_route_cannot_be_rewritten_or_deleted` | 应用角色：`permission denied`；迁移角色（超级用户）：触发器 `禁止改写` / `不可物理删除` |

## 2. 复现命令

```bash
docker compose -f infra/compose/docker-compose.yml up -d
python -m alembic upgrade head

# E1–E4、E7（含并发 gather 用例）
pytest tests/integration/test_generation_route_api.py -v

# E5（主链端到端，含两条内联负向断言）
pytest tests/integration/test_main_chain_e2e.py -v

# E6（受理侧单一路线语义）
pytest tests/unit/test_generation_task_service.py -v
```

实测结果（2026-11-17）：`test_generation_route_api.py` 10 passed in 1.50s；
`test_main_chain_e2e.py` 1 passed；`test_generation_task_service.py` 15 passed。

## 3. 为什么这些断言足以覆盖 C4

C4 的措辞里有两个容易被跳过的词：**并发** 与 **结构化错误**。

- "并发"意味着"先查有没有生效路线、再插"这条路必然失败——两个请求都会查到
  "没有"。所以证据不能只有 E3（顺序拒绝），必须有 E1（并发裁决）与 E2
  （约束本身的存在）。E2 是 E1 的根因：如果哪天约束被删掉，E1 可能因时序
  巧合偶尔通过，E2 会稳定变红。
- "结构化错误"意味着调用方要能**编程处理**，不能靠 message 文本。E3 断言了
  错误码、类别、可重试标记与 `details` 里的既有路线/选择 id。
- "不得静默排队"在实现上等于"不得自动改路"：`ROUTE_ALREADY_SELECTED` 是终态
  结论，没有后台重试、没有覆盖写（E7 证明连特权会话都改不了）。

另有一条口径容易被漏掉：C4 若只守选路入口，**同一镜头仍可在两个路线上各
受理一次生成**（ADR-R33 §3 债务 7 的后半句）。E5/E6 就是这条的负向证据。

## 4. 证据边界（不声称的部分）

1. **并发是单进程内 asyncio 并发**：两个请求走各自独立数据库连接（真实连接级
   并发），裁决者是数据库唯一约束。跨进程/跨实例并发机制相同（约束在库里），
   但本段**未**单独做多进程压测——此项属任务 ①（压测与故障恢复）的延伸范围。
2. 本证据**不覆盖**路线变更（换路线）：契约未给动作，当前一律拒绝
   （ADR-R35 §3 债务 1），因此没有"变更成功后的一致性"证据。
3. 本证据**不覆盖** rights/capability 的路线准入：那部分仍依赖授权层与交付域
   D3（ADR-R24 债务），选路入口只校验 VERIFIED（ACT-ROUTE-NEG-001）。
4. C4 通过**不**意味着 Phase 1 其余验收项通过：C5（QCIssue 缺陷闭环）与 A3
   （trace 全链路）仍无证据，见 ADR-R33 §4。
