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

> **后写指针（2026-11-25，同日）**：Owner 已裁决解除，裁决内容、边界与落点见 §7。
> 上表末行是复核完成时点的表述，保留不改——复核方无权自我放行，这句话在解除后仍然成立。

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

> **后写指针（2026-11-25，同日）**：上表第 4 行的**前提本身记错了数**——本分支新增的是
> **3** 个迁移（`a7b8c9d0e1f2` / `c5d6e7f8a9b0` / `e7f8a9b0c1d2`），不是 2 个，`-2` 当场
> 就已经少撤一格。该项已收口（深度改为派生），见 §8。其余四项不动。

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

> **后写指针（2026-11-25，同日）**：这句话已由 Owner 兑现，解除记录见 §7。
> 本段保留原文，因为它是「复核不能自我放行」的边界声明，不是对最终状态的判断。

需要特别指出：本次复核**推翻了自己上一轮的判断**——上一轮说「主要实现已完成，
剩余是挂账」，实际上 `ABANDONED` 在真实库上根本不可写。这不是措辞偏差，是
「复核没做就下结论」的直接代价。因此 §4 的每一项都附了可复跑的命令，而不是
「已核对」四个字。

---

## 7. 解除记录（Owner 裁决，2026-11-25）

| 项 | 内容 |
|---|---|
| 裁决 | Owner 2026-11-25 一句话「解除」 |
| 解除对象 | P2（PR-G-2 `media_type` 事实化 / PR-G-3 `ABANDONED` 终态）的 `[V-BLOCKED]` |
| 技术依据 | 本记录 §4 八项判据，全部实际运行并通过（无 `NOT RUN`） |
| 挂账边界 | 本记录 §5 五项差额，解除后**继续挂账、不销项** |
| 裁决落纸 | `contract_amendments.yaml` → `V205-R42-P2-BLOCK-LIFT`（`retroactive: false`，`approved_at: 2026-11-25`，`source_commits` → `5442e0a`，6 条残余风险） |
| 契约计数 | 未变：Action 107 / OpenAPI 107 / Schema 99 / 状态机 56 |
| 运行时 | 未改一行代码；本条是状态登记，不是变更 |

### 7.1 解除的确切含义

- 解除的是「P2 这两项实施的技术验收未完成」这一条阻塞，**不是**「`[V-BLOCKED]` 这个标记本身」。
- 解除依据是**修复之后**的判据（`e7f8a9b0c1d2` 补齐 `ck_artifact_manifests_status`），
  不是修复之前。若沿用上一轮「主要实现已完成」的结论直接解除，解除的是一个假象——
  那两条生产放弃路径仍然会撞 23514。
- 判定「已解除」的证据是可复跑的：§4 的八项命令、§3 末尾登记的「扩展名集合一致性」观察项、
  §5 的挂账清单，而不是本段这几行字。

### 7.2 解除**不**包含什么

| 不包含 | 说明 |
|---|---|
| 不销 §5 的五项差额 | `audit_ref` 无非空校验、`artifact_manifests` 非 append-only、状态机条目 `PENDING_PHASE1_REVIEW`、CI 往返深度人工同步、偶发 ACL 红灯根因未定位——解除不触碰其中任何一项 |
| 不预授权未来同类扩充 | 再出现新字段、新终态，或又一次「加了列没同步 CHECK」，仍须单独复核 + 单独裁决 |
| 不覆盖其它 `[V-BLOCKED]` | TC-1~TC-5 技术选型、上游「实施是否获单独授权」等条目各走各的裁决路径 |
| 不改 ADR-R42 正文 | ADR 保持历史原文不动（这是 S1 认可的治理方式），实施状态以审计记录 + 台账为准 |
| 不改变 P2-RATIFY 的追认边界 | 追认只覆盖 PR-G-2 / PR-G-3 两项，解除同样只覆盖这两项 |

### 7.3 为什么解除必须由 Owner 落这一句话

复核可以回答「技术验收是否通过」，但不能回答「是否放行」。两者若合并，
下次复核不通过时，也会有人用「复核人认为通过」把它替代掉——§2 的缺陷正是这样漏过去的：
所有闸门都绿，缺的是「有人真的去写那条会失败的用例」。P4 守的就是这条线，
因此授权纸（`V205-R42-P2-BLOCK-LIFT`）与技术纸（本记录 §4）分开落。

### 7.4 落点与未改动的范围

| 对象 | 动作 |
|---|---|
| 本记录 | 追加 §7；§1 表末行与 §6 末段加后写指针，**原文保留不改** |
| `docs/audits/2026-11-24-r42-authorization-reconciliation.md` | 追加 §14（解除记录）；§13.3 加后写指针，**§13 原文保留** |
| `contract_amendments.yaml` | 新增 `V205-R42-P2-BLOCK-LIFT`（`retroactive: false`，6 条残余风险） |
| `V205-R42-P2-RATIFY` / `V205-R15-A36` / `V205-R42-C7B-IMPL` / `V205-R42-D69-IMPL` | **未改**（历史条目冻结）。它们仍写着「`[V-BLOCKED]` 不解除 / P2 未签署，`[V-BLOCKED]` 不变」，读台账必须合并读——该代价已写进新条目的残余风险 |
| 代码 / 迁移 / 契约派生文件 | **未改一行** |

**当前状态：P2 的 `[V-BLOCKED]` 于 2026-11-25 解除，依据 §4，边界为 §5。**

---

## 8. §5 第 4 项收口：往返深度改为派生（2026-11-25）

> 追加于文末。§5 是复核当时的挂账清单，本节记录其中「CI 往返深度人工同步」一项
> 的收口；其余四项保持挂账。

### 8.1 事实：那个常量当场就已经少了

```
$ git diff --name-status dadbb17 HEAD -- data/migrations/versions
A  data/migrations/versions/a7b8c9d0e1f2_artifact_manifest_media_type.py
A  data/migrations/versions/c5d6e7f8a9b0_dead_letter_replays.py
A  data/migrations/versions/e7f8a9b0c1d2_artifact_manifest_abandoned_status.py
```

三个，不是两个。CI 写 `-2`，于是只执行 `e7f8a9b0c1d2` 与 `c5d6e7f8a9b0` 的
`downgrade()`；`a7b8c9d0e1f2` 的降级在 CI 里**一次都没跑过**。

这不是笔误，是手抄常量的必然结果：它连「当时本分支加了几条」都对不上，而 CI 依然全绿。
同一个坑此前已经踩过一次（`-1` 时代漏掉 `c5d6e7f8a9b0` 的分区撤销）。

### 8.2 修法：深度从 merge-base 派生，取不到就红

| 改动 | 内容 |
|---|---|
| `scripts/migration_roundtrip_depth.py`（新增） | N = `git diff --name-status <merge-base>..HEAD -- data/migrations/versions` 的 **A + M** 数；下限 1；出现 **D** 直接失败（迁移链不允许删条目）；T/U 等异常状态不猜；**拿不到 merge-base 非零退出**，不退回默认值 |
| `.github/workflows/ci.yml` | `DEPTH="$(uv run python scripts/migration_roundtrip_depth.py)"` → `alembic downgrade "-${DEPTH}"`；checkout 改 `fetch-depth: 0`（浅克隆取不到共同祖先，派生脚本会红） |
| `tests/engineering/test_migration_roundtrip_depth.py`（新增） | 9 例：A/M/D/R/异常状态逐项、取不到 merge-base 必须抛错、增量为 0 时取下限 1、**workflow 若被改回字面量 `downgrade -2` 会红**、真 git 仓库只数本分支增量 |

为什么下限是 1 而不是 0：`-0` 不是合法往返；在 main 上跑时增量为 0，但仍要保住
「回滚后还能重建 head」这条证据。

为什么用 merge-base 而不是 `origin/main..HEAD`：分支落后 main 时，后者会把 main 上
别人的迁移算进本分支增量，深度虚高且说不清来由。

### 8.3 证据（本轮实跑）

```
$ python scripts/migration_roundtrip_depth.py
3

$ alembic downgrade -3
  e7f8a9b0c1d2 -> c5d6e7f8a9b0
  c5d6e7f8a9b0 -> a7b8c9d0e1f2
  a7b8c9d0e1f2 -> e4f5a6b7c8d9
$ alembic upgrade head
  e4f5a6b7c8d9 -> a7b8c9d0e1f2 -> c5d6e7f8a9b0 -> e7f8a9b0c1d2
```

`a7b8c9d0e1f2` 的降级是**首次**有运行时证据，通过。也就是说这一项的缺陷是
「没跑过」，不是「跑不过」——这两者必须分清，否则会把「覆盖面缺口」误记成「代码 bug」。

负向验证：把 workflow 临时改回 `alembic downgrade -2`，守卫用例变红（报错直接指出
缺 `alembic downgrade "-${DEPTH}"`），改回后转绿。断言不空转。

### 8.4 边界

- **只收口 §5 第 4 项。** 其余四项不动：`audit_ref` 无非空校验与 `artifact_manifests`
  非 append-only 是 Owner 追认时已接受的边界（改它们须单独裁决），状态机条目
  `PENDING_PHASE1_REVIEW` 属另一条治理线，偶发 ACL 红灯根因仍未定位。
- 台账 `V205-R42-P2-BLOCK-LIFT` 的残余风险第 4 条（「CI 往返深度靠人工同步」）是
  解除当时的正确表述，冻结不改；本项收口以本节为准，读账须串读。
- 本节不改契约、不改运行时行为：新增的是一个 CI 脚本与一组断言。

---

## 9. 台账 `D69-IMPL` 三项残余风险收口（2026-11-25）

> 追加于文末。§5 是 P2 复核当时的挂账清单；本节处理的是**另一条线上**的三项：
> `V205-R42-D69-IMPL` 的 `residual_risks` 第 1/2/3 条。两者不可混读——§5 的五项
> 在本节一个字都没动。

### 9.1 三条各自是一句「没做」，不是「做不到」

| # | 台账原文 | 实际性质 |
|---|---|---|
| 1 | `result` 的 FAILED 枚举当前不可达；CHECK 保留两个值是为了将来多步重放不必改表 | 表里允许一个**任何代码路径都写不进**的值 |
| 2 | 观测面缺失：没有「重放次数 / 死信存量」指标 | 后半句**当时就已过时**：`outbox_relay_dead` 早就在；缺的只是「重放次数」 |
| 3 | 只有写入口（重放），没有死信查询端点 | 契约里没有查询动作，「可查」仍靠只读 SQL，且「可查」曾被当成已交付 |

第 2 条值得单独指出：台账写这句话时，`packages/shared/metrics.py` 里已经有
`OUTBOX_RELAY_DEAD`（复核 R2 加的）。这不是谁的记忆出错，而是**台账里的
现状描述天然会过期**，而它一旦冻结就没人回头改——所以本节不复述台账，只记录
这次实际补了什么、依据是什么。

### 9.2 三项的做法与理由

**(1) `result` 收窄为单值 `SUCCEEDED`**（迁移 `f8a9b0c1d2e3`）

保留 FAILED 的代价不是「多一个值」，而是读代码的人会得出「失败路径漏写了」的结论，
去补一条本不该存在的分支，或在别处 import 它做判定。补路径也不可行：重放是
「锁行 → 条件清标记 → 写审计」的单事务，而 `dead_letter_replays` 是 append-only，
「先占位再改写终值」根本不可实现——为让一个值可达而破坏 append-only，是把判据
迁就枚举。

表达式 `result = 'SUCCEEDED'` 由 `packages/shared/outbox.py` 的常量统一提供，
ORM 与迁移共用一份文本，避免两处手写漂移。迁移的前置检查写成 SQL `DO` 块而不是
Python 侧 `scalar_one()`：迁移会被 `boundary_assertions` 以 op 替身回放，Python 侧
查库会把一条授权断言打成执行异常。

**(2) 观测面：补「重放次数」，并区分存量与动作**

- `outbox_dead_letter_replays_total{event_type}`（Counter）：只在**提交成功之后**
  才 +1。放在服务层看似更「领域内」，但服务返回时事务还没提交——提交失败会让重放
  整体回滚而计数器不回滚，那样「重放次数」会把失败记成成功，且无处纠正。
- 口径写死：只计成功（拒绝路径的证据在结构化日志与 4xx 指标里，混进来会让
  「这个月重放了多少条」永远算不对）；唯一标签是 `event_type`（契约有限集），
  不打 operator（人员维度单调增长，且不该用于阈值）。
- 告警 `OutboxDeadLettersPresent`（`outbox_relay_dead > 0` 持续 30m）：死信不会自愈；
  用 30m 排除正常处置窗口，用 warning 而不是 critical —— 一条毒事件就足以触发，
  长期 critical 会让人对这条规则脱敏。

**(3) 死信查询端点 `OUTBOX.DEAD_LETTER.READ`**（`GET /outbox/dead-letters`）

- 新动作登记后计数为 **Action 108 / OpenAPI 108**（原 107/107），Schema 99、状态机 56 不变。
- 只读、独立席位语义：查询用于**定位**，重放才是**处置**；并成一个动作会让
  「只想看看有哪些死信」也必须持有处置权。
- 只返回运维元数据（`event_id` / `event_type` / `attempts` / `dead_lettered_at` /
  `occurred_at`），**不返回 payload**：payload 里有业务数据与跨域引用，整批吐到运维
  列表等于把「可读运维元数据」放大成「可读业务事件内容」。
- 键集分页（`(dead_lettered_at, event_id)` 倒序 + 游标），不用 OFFSET：死信是正在
  被处置的队列，翻页期间行会消失，OFFSET 会因此漏行；只按时间做锚点则会在
  「同一时刻批量进死信」时跨页丢行。
- 新增部分索引 `ix_outbox_dead_letter`（迁移 `f9b0c1d2e3f4`）：死信只占全表极小
  比例，普通复合索引会让每次事件写入都为极少数行付维护代价；建在父表上，分区
  索引会自动级联到已有与未来分区。
- runbook 的手工 SQL 重放段落已删除并替换为端点调用：那段 SQL **不落任何审计**，
  留着手册等于教人绕过 D6-9 的审计链。

### 9.3 证据（本轮实跑）

```
$ python scripts/migration_roundtrip_depth.py
5
$ alembic downgrade -1 → upgrade head     # f8a9b0c1d2e3 / f9b0c1d2e3f4 各自往返通过
$ SELECT pg_get_constraintdef(... 'ck_dead_letter_replays_result')
CHECK (((result)::text = 'SUCCEEDED'::text))
```

- `pytest tests`：**1800 收集 / 1799 passed / 1 skipped**，0 fail 0 error（JUnit 计数，exit 0）。
  新增 50 例：查询单元 19、查询集成 12、阻塞清单 17、重放指标 1、result 约束 1。
- `contract_check` 全 PASS（含新增第 13 项「阻塞清单」判据）。
- `ruff` 全绿；`mypy`（CI 口径）215 文件无问题。

### 9.4 边界

- **不销 §5 的五项**：本节与那五项无关，读账须分开。
- **不改历史条目**：`V205-R42-D69-IMPL` 的 `residual_risks` 第 1/2/3 条是记录当时的
  正确表述，冻结不回改；本节是新事实。代价是台账必须串读到本条对应的新修订
  `V205-R42-D69-RESIDUAL-CLOSURE`。
- **不预授权**：新增只读动作是**这一次**的契约变更，不构成「以后可以随手加端点」。

---

## 10. §5 第 5 项（偶发 ACL 红灯）的根因调查（2026-11-25，仍不销项）

追加。§5 的五项在本节**一项都没销**，本节只回答「那一项查到哪一步了」。

调查记录：`2026-11-25-acl-flaky-root-cause.md`。三句话：

1. **原始失败日志不存在**。近 300 次 CI 运行（239 次 / 失败 37 次）里，2026-11-20 之后
   9 次失败逐条核到失败步骤（mypy 3、ruff 1、alembic check 2、downgrade roundtrip 1、
   两个 unit 台账用例 1、前序步骤失败 2），**没有一次**停在
   `test_append_only_tables_reject_row_mutation`。没日志 ⇒ 不能宣称已定位，本项维持挂账。
2. **不确定性已被收敛**：该断言读的是 `information_schema.role_table_grants` 的行集，
   观察者身份是判据的一部分（以 `app_role` 观察同一查询返回 0 行，实测）；套件内不存在
   改 ACL 的语句、也没有并行执行，因此在同一 revision 的库上它是确定性的。
   「偶发」只剩三个出口，各有可判别的指纹（`实际 []` / 少一项 / 多一项），见调查记录 §3。
3. **顺带发现两件事，都不在本轮修**：① `ALTER DEFAULT PRIVILEGES` 给未来对象满量 DML，
   而 append-only 收口只对现存对象做一次 `REVOKE`——任何重建这两张表的迁移都会静默恢复
   `UPDATE`/`DELETE`；② 该用例在落后于 head 的库上（本机 `b1c2d3e4f5a6`）仍然绿，
   说明它绿不代表本分支的迁移产出了这个口径。四条改法（角色相对判据、断言观察者前提、
   收口与默认权限同源、断言库在 head）都属「改则须单独裁决」，见调查记录 §6。

与 §7.2 的关系：§7.2 说「不销 §5 的五项」，本节不改那句话——它仍是当前表述。
