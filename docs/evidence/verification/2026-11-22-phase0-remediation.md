# 核验电池（时点证据）

> 生成时间：2026-11-21T16:59:59Z（UTC）　提交：`eb72a54`　工作树：洁净
> 本文件由 `scripts/verification_battery.py` 生成，**禁止手工编辑**。
> 本文件只陈述事实与证据等级，不含合规判断；合规结论见同日审计报告。

## 1. 核验总表

| 编号 | 类别 | 核验对象 | 核验状态 | 证据等级 | 证据位置 | 未完成事项 |
|---|---|---|---|---|---|---|
| C1.1 | C1 基准文件完整性 | V2.0.5 基线正文 | `[V-VERIFIED]` | `[E2]` | docs/baseline/AI影视工业化生产平台V2.0.5.md 与 contracts/phase0/frozen_inventory.yaml:baseline | 只证明文件字节未变，不证明其内容正确或已被人类批准 |
| C2.1 | C2 冻结清单与修订台账 | 契约冻结校验 | `[V-VERIFIED]` | `[E3]` | D:\Users\9527\Desktop\AI_Creative_Media_Industrial_OS\.venv\Scripts\python.exe scripts/freeze_manifest.py --check | 本项只证明检查器自身的判定域，不覆盖运行期行为 |
| C2.2 | C2 冻结清单与修订台账 | 修订台账只追加 | `[V-VERIFIED]` | `[E2]` | contracts/phase0/contract_amendments.yaml vs HEAD | 只比对 id 序列，不比对条目内容：改写正文但不改 id 的形态本判据看不到 |
| C2.3 | C2 冻结清单与修订台账 | 修订条数口径 | `[V-VERIFIED]` | `[E2+E3]` | contracts/phase0/contract_amendments.yaml / freeze_manifest 输出 | 只比对条数，不比对条目内容（内容一致性由 C2.1 与 C3.1 承担） |
| C3.1 | C3 契约一致性 | 契约派生一致性 | `[V-VERIFIED]` | `[E3]` | D:\Users\9527\Desktop\AI_Creative_Media_Industrial_OS\.venv\Scripts\python.exe scripts/contract_check.py | 本项只证明检查器自身的判定域，不覆盖运行期行为 |
| C4.1 | C4 Manifest 哈希 | 冻结清单自指哈希 | `[V-VERIFIED]` | `[E2]` | contracts/phase0/phase0_manifest.md.sha256:1 | 侧车与正文同源生成（--write 同时刷新），因此同谋篡改无法被本判据发现 |
| C5.1 | C5 静态与结构检查 | ruff 全仓 lint | `[V-VERIFIED]` | `[E3]` | D:\Users\9527\Desktop\AI_Creative_Media_Industrial_OS\.venv\Scripts\ruff.exe check . | 本项只证明检查器自身的判定域，不覆盖运行期行为 |
| C5.2 | C5 静态与结构检查 | mypy 类型检查 | `[V-VERIFIED]` | `[E3]` | D:\Users\9527\Desktop\AI_Creative_Media_Industrial_OS\.venv\Scripts\mypy.exe packages services apps workers | 本项只证明检查器自身的判定域，不覆盖运行期行为 |
| C5.3 | C5 静态与结构检查 | 分层依赖校验 | `[V-VERIFIED]` | `[E3]` | D:\Users\9527\Desktop\AI_Creative_Media_Industrial_OS\.venv\Scripts\python.exe scripts/architecture_check.py | 本项只证明检查器自身的判定域，不覆盖运行期行为 |
| C6.1 | C6 数据库迁移 | 迁移链与数据库版本 | `[V-VERIFIED]` | `[E5]` | alembic heads / alembic current（真实 PostgreSQL） | 只证明版本一致，不证明 downgrade 可逆（往返验证需单独执行） |
| C6.2 | C6 数据库迁移 | 模型与迁移无漂移 | `[V-VERIFIED]` | `[E5]` | alembic check（真实 PostgreSQL） | 只比对 ORM 模型与迁移，不校验数据层约束是否真的生效 |
| C7.1 | C7 自动化测试与核心链路 | 单元、工程判据、架构判据 | `[V-VERIFIED]` | `[E4]` | pytest tests\architecture tests\engineering tests\unit | 执行通过只证明本层用例覆盖的行为，不证明生产环境可靠 |
| C7.2 | C7 自动化测试与核心链路 | 集成（真实 PG）、韧性、权限负向 | `[V-VERIFIED]` | `[E4+E5]` | pytest tests\integration tests\permission tests\resilience | 执行通过只证明本层用例覆盖的行为，不证明生产环境可靠 |
| C7.3 | C7 自动化测试与核心链路 | 端到端主链（真实 PG + ASGI） | `[V-VERIFIED]` | `[E4+E5]` | pytest tests\e2e | 只覆盖主链的已实现段；剧本→拆镜→ShotSpec→ConstraintPack 这一段由逐域用例证明，端到端链路尚未串起 |

## 2. 逐条证据

- `[V-VERIFIED][E2] C1.1 V2.0.5 基线正文：证据：docs/baseline/AI影视工业化生产平台V2.0.5.md 与 contracts/phase0/frozen_inventory.yaml:baseline；限制：只证明文件字节未变，不证明其内容正确或已被人类批准`
  - 详情：bytes=489580（登记 489580）; sha256=b922a1e1c6e5c43b…（登记 b922a1e1c6e5c43b…）
- `[V-VERIFIED][E2] C4.1 冻结清单自指哈希：证据：contracts/phase0/phase0_manifest.md.sha256:1；限制：侧车与正文同源生成（--write 同时刷新），因此同谋篡改无法被本判据发现`
  - 详情：actual=2b8d44442587fb66… declared=2b8d44442587fb66…
- `[V-VERIFIED][E2] C2.2 修订台账只追加：证据：contracts/phase0/contract_amendments.yaml vs HEAD；限制：只比对 id 序列，不比对条目内容：改写正文但不改 id 的形态本判据看不到`
  - 详情：HEAD 23 条 / 当前 23 条
- `[V-VERIFIED][E3] C5.1 ruff 全仓 lint：证据：D:\Users\9527\Desktop\AI_Creative_Media_Industrial_OS\.venv\Scripts\ruff.exe check .；限制：本项只证明检查器自身的判定域，不覆盖运行期行为`
- `[V-VERIFIED][E3] C5.2 mypy 类型检查：证据：D:\Users\9527\Desktop\AI_Creative_Media_Industrial_OS\.venv\Scripts\mypy.exe packages services apps workers；限制：本项只证明检查器自身的判定域，不覆盖运行期行为`
- `[V-VERIFIED][E3] C3.1 契约派生一致性：证据：D:\Users\9527\Desktop\AI_Creative_Media_Industrial_OS\.venv\Scripts\python.exe scripts/contract_check.py；限制：本项只证明检查器自身的判定域，不覆盖运行期行为`
- `[V-VERIFIED][E3] C5.3 分层依赖校验：证据：D:\Users\9527\Desktop\AI_Creative_Media_Industrial_OS\.venv\Scripts\python.exe scripts/architecture_check.py；限制：本项只证明检查器自身的判定域，不覆盖运行期行为`
- `[V-VERIFIED][E3] C2.1 契约冻结校验：证据：D:\Users\9527\Desktop\AI_Creative_Media_Industrial_OS\.venv\Scripts\python.exe scripts/freeze_manifest.py --check；限制：本项只证明检查器自身的判定域，不覆盖运行期行为`
- `[V-VERIFIED][E2+E3] C2.3 修订条数口径：证据：contracts/phase0/contract_amendments.yaml / freeze_manifest 输出；限制：只比对条数，不比对条目内容（内容一致性由 C2.1 与 C3.1 承担）`
  - 详情：闸门报告 23 条 / 台账实际 23 条
- `[V-VERIFIED][E5] C6.1 迁移链与数据库版本：证据：alembic heads / alembic current（真实 PostgreSQL）；限制：只证明版本一致，不证明 downgrade 可逆（往返验证需单独执行）`
  - 详情：head=d7e8f9a0b1c2 current=d7e8f9a0b1c2
- `[V-VERIFIED][E5] C6.2 模型与迁移无漂移：证据：alembic check（真实 PostgreSQL）；限制：只比对 ORM 模型与迁移，不校验数据层约束是否真的生效`
- `[V-VERIFIED][E4] C7.1 单元、工程判据、架构判据：证据：pytest tests\architecture tests\engineering tests\unit；限制：执行通过只证明本层用例覆盖的行为，不证明生产环境可靠`
  - 详情：passed=1106 failed=0
- `[V-VERIFIED][E4+E5] C7.2 集成（真实 PG）、韧性、权限负向：证据：pytest tests\integration tests\permission tests\resilience；限制：执行通过只证明本层用例覆盖的行为，不证明生产环境可靠`
  - 详情：passed=126 failed=0
- `[V-VERIFIED][E4+E5] C7.3 端到端主链（真实 PG + ASGI）：证据：pytest tests\e2e；限制：只覆盖主链的已实现段；剧本→拆镜→ShotSpec→ConstraintPack 这一段由逐域用例证明，端到端链路尚未串起`
  - 详情：passed=3 failed=0

## 3. 关键指标（从闸门输出解析，未经人工转录）

| 指标 | 值 | 来源 |
|---|---|---|
| `C7.1.failed` | 0 | C7.1 |
| `C7.1.passed` | 1106 | C7.1 |
| `C7.2.failed` | 0 | C7.2 |
| `C7.2.passed` | 126 | C7.2 |
| `C7.3.failed` | 0 | C7.3 |
| `C7.3.passed` | 3 | C7.3 |
| `actions_total` | 100 | C3.1 |
| `active_actions` | 56 | C3.1 |
| `amendments_count` | 23 | C2.2 |
| `amendments_count_reported` | 23 | C2.1 |
| `baseline_bytes` | 489580 | C1.1 |
| `baseline_sha256` | b922a1e1c6e5c43b0c7e6ee30b5f036b7919fcc946935594f225b90bf441945e | C1.1 |
| `event_gaps` | 0 | C3.1 |
| `events_emitted` | 29 | C3.1 |
| `events_total` | 29 | C3.1 |
| `implemented_actions` | 35 | C3.1 |
| `invariants_debt` | 0 | C3.1 |
| `invariants_partial` | 0 | C3.1 |
| `invariants_total` | 23 | C3.1 |
| `invariants_verified` | 23 | C3.1 |
| `manifest_sha256` | 2b8d44442587fb661fe90f41cdb7361f029aa91e4ab99854a57662ece68affb1 | C4.1 |
| `migration_current` | d7e8f9a0b1c2 | C6.1 |
| `migration_head` | d7e8f9a0b1c2 | C6.1 |
| `registered_not_implemented` | 21 | C3.1 |
| `schemas_total` | 84 | C3.1 |
| `state_machines_compared` | 11 | C3.1 |
| `traceability_debt` | 0 | C2.2 |
| `transition_edges_compared` | 47 | C3.1 |
| `transition_entry_debt_edges` | 3 | C3.1 |

## 4. 类别覆盖完备性

- C1 基准文件完整性：1 条探针
- C2 冻结清单与修订台账：3 条探针
- C3 契约一致性：1 条探针
- C4 Manifest 哈希：1 条探针
- C5 静态与结构检查：3 条探针
- C6 数据库迁移：2 条探针
- C7 自动化测试与核心链路：3 条探针

- 完备性判定：PASS（七类均有探针，无未登记类别）
