# ADR-R20：数据不变量下沉到数据库（状态转移、枚举取值、事件正文）

- 编号：ADR-R20
- 状态：已采纳
- 相关包：C（数据不变量）
- 相关文档：`docs/audits/2026-11-16-review-round3-response.md` 第 13、14 条、
  ADR-R11（资产内容不可变）、ADR-R16（按域数据库角色）、ADR-R19（私钥边界）

## 背景

三条不变量此前只有一条真正成立：

| 不变量 | 状态 | 事实 |
|---|---|---|
| 资产内容列不可改 | **成立** | 触发器逐列比对（ADR-R11） |
| 资产 `status` 只能沿状态机推进 | **不成立** | `status` 在触发器白名单里，`UPDATE ... SET status='GOLD'` 可跳过 QC 与人工审核 |
| 事件正文不可改 | **已成立但无断言** | B1 角色拆分后业务角色只有 `SELECT, INSERT`，投递域只有 4 个投递状态列的列级 `UPDATE`——但没有闸门守着，下一轮加列会悄悄变回可改 |
| 代码枚举 ↔ 契约枚举一致 | **不成立** | `AssetStatus` 只有 5 个值，契约登记 10 个；全仓闸门全绿，因为没有闸门比对枚举 |

共同点：**约束存在于声明里，执行靠"没人会那么写"**。审查报告的判据是"数据库层能不能拦住"，
而不是"代码里有没有那一行 if"。

## 决策

### 1. 状态转移集合落成数据表，由触发器执行

```
asset_service.asset_status_transitions(from_status, to_status)   ← 迁移播种，应用角色只读
asset_service.asset_versions_guard()                             ← BEFORE UPDATE 逐条判定
```

- `status` 变化时查表：命中则放行，未命中即 `RAISE EXCEPTION`；
- `status` 仍留在"非内容列"里（否则合法转移会被内容不可变规则挡住），但它的每一次变化
  都要过转移判定——这才是"内容不可改、状态只能沿边改"的完整表达；
- 表对所有业务角色 `GRANT SELECT`，**不授予任何写权限**。触发器是 `SECURITY INVOKER`，
  读不到参考表就会把"非法转移"误报成"权限不足"；而能改转移表的人等于能给自己放行，
  所以写权限只留给迁移。

**为什么是表而不是写死在函数体**：契约补登旁支来源边（REJECTED / REWORK / SUPERSEDED /
ARCHIVED）时只需插一行数据；且"允许哪些转移"变成可 `SELECT` 的事实，运维不必读 PL/pgSQL。

**为什么边只有 4 条**：契约正文写"可进入 REJECTED / QUARANTINED / REWORK / SUPERSEDED /
ARCHIVED"，但没给来源状态。按 31.5「未登记转移一律拒绝」，实现不能凭空造边。这 5 个状态
的不可达性已登记在 `contracts/runtime/state_machine_gaps.yaml`，由 `contract_check` 双向校验
（登记了却已可达 → 报陈旧债务）。

### 2. 枚举取值由 CHECK 约束限定，且枚举本身纳入机械比对

- `asset_versions.status` 加 CHECK，只接受契约登记的 10 个值（`BANANA` 写不进去）；
- `AssetStatus` 补齐 5 个旁支状态。**这是必要的**：枚举是「数据可表示的集合」，不是
  「当前流程能到达的集合」。缺值时读到契约允许的状态会抛 `ValueError`（对外 500），
  而旁支状态是契约允许存在的数据（例如合规隔离出的资产）；
- 新增台账 `contracts/runtime/enum_bindings.yaml` + 闸门 `check_enum_parity`：
  - 每个绑定的代码枚举必须与契约状态**逐值相等**（缺值与多值都报红）；
  - `services/*/domain/enums.py` 里每个枚举类必须在 bindings 或 exemptions 之一
    （新增枚举不登记 = 报红，否则"忘登记"等于"免检"）；
  - 豁免必须写 `reason`，且登记的实现类必须真实存在（陈旧豁免报红）。

**为什么用 CHECK 而不是引用一张取值表**：信封列 `status` 由 `EntityMixin` 提供，被
Project / Episode / ScriptRevision 共用；给它挂一个指向资产域参考表的外键，会把这四个域
绑成一条隐式依赖。取值集合来自契约枚举，变更本来就要走契约修订，"再写一条迁移"可接受。

**迁移字面量 vs 代码枚举**：迁移里的取值与边是**字面量快照**（迁移不能 import 代码，
否则同一个 revision 在不同 commit 下得到不同的库）。第三份副本的一致性由
`tests/unit/test_enum_parity.py` 守——它比对迁移字面量与当前枚举/转移表。

### 3. 事件正文不可改：把"当前恰好如此"变成"改坏了会红"

B1 已经把每个业务角色的 `outbox.outbox_events` 授权改成 `SELECT, INSERT`，投递域只有
`published_at` / `attempts` / `next_attempt_at` / `dead_lettered_at` 四列的列级 `UPDATE`。
包 C 不新增授权，只补断言：

- `test_only_relay_role_has_any_update_on_events`：除投递域外，任何业务角色对事件表**没有**
  任何 `UPDATE`（表级与列级都查）；
- `test_relay_role_cannot_update_event_fact_columns`：投递域拿到的列里不含任何事实列
  （`payload` / `event_type` / `aggregate_*` / `tenant_id` / `event_id` / `occurred_at`）。

查系统目录而不是"跑一次 UPDATE 试试"：逐个试写只能证明"我试的那列写不进去"，
证明不了"下次加的列也写不进去"。

## 配套闸门

| 闸门 | 守什么 | 位置 |
|---|---|---|
| `test_unregistered_transition_is_rejected` | `GENERATED → GOLD` 被拒（含超级用户直连） | `tests/integration/test_asset_status_transitions.py` |
| `test_registered_transition_is_allowed` | 已登记边仍可走（拒绝不是"整列冻结"） | 同上 |
| `test_status_outside_contract_enum_is_rejected` | 契约外取值写不进库（CHECK） | 同上 |
| `test_side_branch_status_cannot_be_entered` | 旁支状态"可表示但不可进入" | 同上 |
| `test_application_role_cannot_edit_transition_table` | 业务角色不能改转移表（不能自我放行） | 同上 |
| `test_transition_table_matches_code_state_machine` | 库里的边 == 代码转移表 | 同上 |
| `check_enum_parity` | 代码枚举 ↔ 契约枚举逐值相等；枚举类必须登记或豁免 | `scripts/contract_check.py` |
| `test_enum_parity.py` | 迁移字面量 == 代码枚举/转移表；闸门本身在当前树上通过 | `tests/unit/test_enum_parity.py` |

## 后果

**行为变化**：

1. 任何绕过服务层的状态写入（直连 SQL、运维脚本、未来的新仓储实现）在未登记边上会被数据库拒绝。
   此前这类写入是"能成功但审计无法解释"。
2. `AssetStatus` 从 5 个值扩到 10 个。`_APPROVABLE_STATES` / `_REPORTABLE_STATES` 等
   白名单不变，因此**流程可达性没有变化**——扩的是"能读出来的数据"，不是"能进的流程"。
3. 新增表 `asset_service.asset_status_transitions` 已声明为 ORM 模型（只读参考数据），
   因此它在 `alembic check` 的比对范围内，schema 被手工改坏能被发现。

**新增的诚实登记**：

- 旁支状态仍然**不可达**（契约没给来源边），这 5 条债务不变；本 ADR 只是让"不可达"
  与"可表示"两件事各自有据可查。
- CHECK 约束的值集合是迁移快照，与枚举的一致性靠测试守，**不是**数据库层的自动派生。
  若有人只改枚举不加迁移，`pytest` 会红（而非 CI 静默通过）。
- 事件正文不变量只覆盖 `outbox.outbox_events`。分区子表由 `protect_partition_statements`
  回收直连权限（B1），但"未来新建分区是否自动受保护"依赖分区维护流程，仍无闸门。

## 验证

- `pytest tests` 442 passed（包 C 共新增 24 条，其中本 ADR 覆盖 12 条：状态转移 7 条 +
  枚举一致性 3 条 + 事件授权断言 2 条）
- `alembic upgrade head` → `alembic check` 无漂移
- 反向验证：删掉 `AssetStatus.ARCHIVED` 后 `contract_check` 立即报
  `[FAIL] AssetStatus 缺契约登记的状态: ['ARCHIVED']`；往 `domain/project.py` 加
  `from sqlalchemy import text` 后 R6 报红
- `ruff` / `mypy` / `architecture_check`（R1–R6 全 0 违规）/ `freeze_manifest` /
  `contract_check` 全绿
