# P2 `[V-BLOCKED]` 解除复核记录（2026-11-25）

> 性质：**技术复核记录**，不是授权文件。授权已由 `V205-R42-P2-RATIFY` 与
> 上游审计 §13 落纸；本记录只回答「技术验收是否通过」。
> 触发条件：`V205-R42-P2-RATIFY` 第 3 条要求「解除须另行复核代码、契约派生、
> 迁移升级与降级、真实 PostgreSQL 测试与恢复故障场景，且复核结果单独落纸」。
> 取证方式：逐项实读源码 / 迁移 / 契约文件，命令与输出对照记录；未运行的一律标
> `NOT RUN`。本轮所有项均已实际运行。

---

## 1. 一页结论

| 项 | 结果 |
|---|---|
| 复核前状态 | **不通过**：`ABANDONED` 在真实库上不可写，PR-G-3 的放弃路径必然失败 |
| 缺陷性质 | 迁移漏同步 `ck_artifact_manifests_status`，ORM 三值 / 库里两值 |
| 为什么既有闸门全绿 | `alembic check` 走 autogenerate，SQLAlchemy **不比较 CHECK 约束** |
| 修复 | 新增迁移 `e7f8a9b0c1d2` + 两条新用例（漂移检测、行为验收） |
| 复核后状态 | **通过**（判据逐项见 §4） |
| `[V-BLOCKED]` | **本轮不自行摘除**。技术维度已具备解除条件，是否解除属 Owner 裁决（见 §6） |

---

## 2. 阻断级发现：`ABANDONED` 写不进真实库

### 2.1 事实

```
ORM 声明 (ArtifactManifestStatus 全集) : ABANDONED, PREPARED, STORED_VERIFIED
数据库 ck_artifact_manifests_status    : PREPARED, STORED_VERIFIED   ← 缺 ABANDONED
```

约束由 `e1f2a3b4c5d6`（建表）用两值常量创建；`a7b8c9d0e1f2`（PR-G）加了
`media_type` 列，却没有同步这个约束。ORM 的 `status_check(...)` 按枚举生成三值，
因此「新库 `create_all` 通过、正式库写入失败」。

真实库上的直接证据（修复前）：

```
asyncpg.exceptions.CheckViolationError: new row for relation "artifact_manifests"
violates check constraint "ck_artifact_manifests_status"
```

### 2.2 影响面：不是边角，是恢复链条的核心故障路径

两条**生产**调用路径都会撞上：

| 位置 | 场景 |
|---|---|
| `services/production_planning/service/generation_service.py:232` | T1 判定「对象确定不存在 / 读回校验失败」后放弃意图 |
| `workers/generation/executor.py:408` | 恢复流程判定该 attempt 不可复用后放弃意图 |

失败后果不是「报个错」：意图永远停在 `PREPARED`，每次恢复都会重新尝试、
可能重复计费，而「已放弃」这个事实无法落纸。这正好击穿 PR-G 存在的理由。

### 2.3 为什么所有既有闸门都绿

| 闸门 | 为什么看不见 |
|---|---|
| `alembic check` | autogenerate **不比较 CHECK 约束**（SQLAlchemy 已知限制） |
| `contract_check` | 比对的是契约文件之间、契约与代码常量之间，不读数据库 catalog |
| 静态单测（4 例） | 全部作用在领域对象上，不落库 |
| 真实 PG 集成测试 | **没有任何一条写过 `ABANDONED`**——缺的是用例，不是断言错 |
| CI | 上述闸门的合集，因此同样失明 |

结论：缺口不在「某条断言判错」，而在「没有这项能力」。

### 2.4 修复

- **新增迁移 `e7f8a9b0c1d2`**（`down_revision = c5d6e7f8a9b0`）：drop 后按
  `status_check_sql` 重建三值约束。**不改 `a7b8c9d0e1f2`**——它已在本地与 CI
  执行过，改历史迁移会让「已升级的库」与「新升级的库」同一 revision 不同结构。
- **降级不静默改数据**：`downgrade` 先统计 `ABANDONED` 行，非零则抛带计数的
  `RuntimeError`。理由是这些行是「意图已被放弃」的既有事实，不能为了让迁移
  通过而被抹成 `PREPARED`。
- 本 PR 的 CI 往返深度随之从 `-1` 改为 `-2`：`-1` 只撤最新一格，加了迁移却仍写
  `-1`，上一个迁移（`c5d6e7f8a9b0` 的分区撤销）就再也没有运行时证据。

### 2.5 防呆：把判据建起来，而不是只修这一次

同一形态的坑在 `generation_tasks` 上**已经出现过一次**——
`test_asset_registration_constraints` 的 sample fixture 末尾那段「把在途任务复位，
否则 CI 的 `downgrade -1` 过不去」的注释就是它的痕迹。修一次不够。

| 新增 | 作用 |
|---|---|
| `tests/integration/test_status_constraint_drift.py` | ORM 声明的 `ck_*_status` 允许集合 vs 数据库实际逐表比对，双向（少值 / 多约束） |
| `tests/integration/test_artifact_manifest_abandoned_pg.py` | 三值可写 + 第四值仍被拒（23514）+ 拒绝后不残留半截写入 |
| `packages/shared/orm_registry.py` | ORM 模块清单收成唯一入口，`env.py` 与漂移断言共用；漏导一个域会让漂移断言对该域静默失明，故必须一份清单 |

**负向验证**（证明断言不空转）：

- 修复前跑漂移断言 → 红，报错精确到
  `production_planning.artifact_manifests: ORM [ABANDONED, PREPARED, STORED_VERIFIED] / DB [PREPARED, STORED_VERIFIED]`；
- `alembic downgrade -1` 退到修复前再跑行为用例 → 红，
  `CheckViolationError: ... violates check constraint "ck_artifact_manifests_status"`；
- 恢复 `upgrade head` → 两条均绿。

---

## 3. 一处判断更正：回填覆盖面

上一轮把「`_BACKFILL` 只覆盖 7 种小写扩展名」写成了残余风险，并举例
`.gif` / `.mov` / `.PNG` 会让 `NOT NULL` 收紧失败。本轮实读后**更正**：

```
ck_artifact_manifests_locator_sha256  convalidated = true
regex 允许扩展名 = (bin|jpg|mp4|png|wav|webm|webp)   ← 与回填映射逐项重合
现存 572 行：572 行全部匹配该 regex
```

也就是说，回填**不可能遗漏合法行**：不合法的 locator 根本进不了这张表。
原判断成立的前提是「locator 可为任意字符串」，而该前提被上游约束排除了。

**修正后的残余风险**是另一个形态：如果将来 locator 的允许扩展名集合扩大，而
回填映射未同步，`NOT NULL` 收紧会在迁移期失败。这是「迁移与契约扩展名集合的
一致性」问题，与 §2 的 status 漂移同源。建议的判据（本轮未做，登记为观察项）：
比较 `EXT_BY_MEDIA_TYPE` 的键集合与 `sql_locator_check` 正则的扩展名集合，
以及 `a7b8c9d0e1f2` 的回填分支集合。

---

## 4. 逐项判据（全部实际运行）

| # | 判据 | 命令 / 证据 | 结果 |
|---|---|---|---|
| 1 | `media_type` 是 T0 持久化事实 | ORM `mapped_column(String(64), nullable=False)`；`ArtifactManifest.__post_init__` 非空校验；`generation_service.py:168-175`（T0 与 locator 推导值比对）、`:265-269`（T2 与意图比对）；`registration_fingerprint.py:70` 入指纹 | PASS |
| 2 | 契约派生完整 | `object_schemas.schema.json`：`media_type` 在 `required` 且有说明；`status.enum` 含 `ABANDONED`；`state_machines.yaml:999-1004` 边与 `frozen_states`；`enum_registry.yaml:26` | PASS |
| 3 | 迁移升降级 | `alembic upgrade head` → `downgrade -2` → `upgrade head` 全过；回滚后核对角色属性、RLS、双触发器、分区授权形态 | PASS |
| 4 | 迁移与 ORM 一致 | `alembic check` → `No new upgrade operations detected`；`test_status_constraint_drift` 全 19 张表逐项一致 | PASS（修复后） |
| 5 | 真实 PG 行为 | `pytest tests` **1740 passed / 1 skipped**；含 ABANDONED 落库与未知状态拒绝 | PASS |
| 6 | 恢复故障场景 | `test_storage_failure_keeps_prepared_evidence_no_registration`：存储失败后意图停在 `PREPARED`、不登记、不发登记请求、无回执；`test_retry_recovers_after_transient_failure_without_second_asset`：瞬时故障恢复不产生第二个资产 | PASS |
| 7 | 状态机边界 | `test_manifest_abandoned_is_frozen_terminal`、`test_manifest_stored_verified_cannot_be_abandoned`、`test_manifest_requires_media_type` | PASS |
| 8 | 闸门 | `contract_check` 38 项全 PASS；`freeze_manifest --check` 49 条修订、十一类产物哈希一致（无需 `--write`）；`ruff` / `mypy` 206 文件全绿 | PASS |

---

## 5. 已知差额（不因本次修复消失）

| 项 | 事实 | 为什么保留 |
|---|---|---|
| `audit_ref` 不校验非空 | `abandon(audit_ref)` 参数必填，`Entity._advance_version` 直接赋值，`abandon("")` 被接受 | 「放弃必须有证据」目前是调用方纪律 + 状态机边界，不是字段级约束 |
| `artifact_manifests` 非 append-only | 授权层无 DELETE 限制、无拒绝删除触发器；本文的测试清理就是靠 `DELETE` 完成 | 「不删除原意图行」由状态机冻结终态与调用纪律承担 |
| 状态机 registration_status | `state_machines.yaml` 的 ArtifactManifest 条目为 `PENDING_PHASE1_REVIEW` | 属另一条治理线，不在 P2 范围 |
| CI 往返深度人工同步 | `-2` 与「本分支新增 2 个迁移」对应；下次加迁移忘记调大会静默退化 | 已写入 CI 注释，无自动化约束 |
| 偶发 ACL 红灯 | `test_append_only_tables_reject_row_mutation` 曾单次失败，后续多轮全绿 | 根因未定位，**不销项**；与本节其它项无关 |

---

## 6. `[V-BLOCKED]` 的当前状态

本轮复核把阻塞理由从「技术复核未做」推进到「复核已完成」。按 Owner 2026-11-25
的口径，解除条件已具备：

| 条件 | 状态 |
|---|---|
| 代码 | 复核通过（§4 第 1、7 项） |
| 契约派生 | 复核通过（第 2 项） |
| 迁移升级与降级 | 复核通过（第 3、4 项） |
| 真实 PostgreSQL 测试 | 复核通过（第 5 项） |
| 恢复故障场景 | 复核通过（第 6 项） |

**但本记录不摘除 `[V-BLOCKED]`。** 理由与 `V205-R42-P2-RATIFY` 一致：授权与验收
是两层纸，复核结论不能自行变成放行结论。解除动作须 Owner 一句话，并在落纸时
写明「解除依据是本记录 §4，且 §5 的五项差额作为已知边界继续挂账」。

需要特别指出：本次复核**推翻了自己上一轮的判断**——上一轮说「主要实现已完成，
剩余是挂账」，实际上 `ABANDONED` 在真实库上根本不可写。这不是措辞偏差，是
「复核没做就下结论」的直接代价。因此 §4 的每一项都附了可复跑的命令，而不是
「已核对」四个字。
