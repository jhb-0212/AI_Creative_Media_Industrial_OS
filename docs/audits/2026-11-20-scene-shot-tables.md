# 镜头域与场次域运行时落地（批次五 i9d0e1f2a3b4）

- 日期：2026-11-20
- 范围：建 `project_planning.scenes` / `shot_planning.shots`；给各域 `shot_id` / `scene_id` 接边；收敛 `shot_revisions` 的草案/正式分层
- 目标库：`ai_film_industrial` @ `localhost:15444`（head `i9d0e1f2a3b4`）
- 结论：一致闸门 **DANGLING 9 → 0**（原 9 条全部消除），另余 1 条**新建表才第一次进入视野**的边；台账 **34/34** 闭环

---

## 一、这件事为什么不能靠「再修一轮」解决

此前的 9 条 `DANGLING` 全部指向三张**在库里根本不存在的表**：

| 目标表 | 边数 |
| --- | --- |
| `shot_planning.shots` | 5 |
| `scenes`（契约层已建模，运行时全无） | 3 |
| `qc_governance.gate_instances` | 1 |

给不存在的表建外键，工程上无解。所以它不属于「归属约束没补齐」，而是**镜头域与场次域的运行时从未落地**。这一条必须说清：继续在归属迁移上加补丁，永远补不上这三张表。

九列的实际数据是 **2 行非空**（`generation_tasks.shot_id` 1 行、`gate_decisions.gate_instance_id` 1 行），其余七列全空。也就是说，建表在数据层面的代价几乎为零——卡住的从来不是数据量，是「该不该建」。

## 二、分叉的真相与我们原先的判断不同

此前把运行时 `episode_ref` / `scene_ref` 与契约 `episode_id` / `scene_id` 的差异判为「既有分叉」。逐字段核对后，结论要改：

| 层 | 契约字段 | 说明 |
| --- | --- | --- |
| `ShotBreakdownDraft`（草案） | `episode_ref`，**可空** | 全剧拆解（`breakdown_scope.level = PRODUCTION`）时本来就没有单集 |
| `Shot` / `Scene` / `ShotRevision`（正式） | `episode_id` / `scene_id` | 身份键，指向目标表的 `entity_id` |

**草案层用业务编码、正式层用身份，是契约刻意的分层，不是混乱。** 唯一真正的偏差落在 `shot_revisions`：它把草案的 `_ref` 直接搬进了正式修订表，同时丢掉了 `episode_id` 与 `scene_id` 两列。批次五补上，`_ref` 保留为来源追溯，但**不再是归属事实**。

这也解释了 ADR-R19 里所有者早已裁定的外键形状：

```sql
FOREIGN KEY (project_id, scene_id) REFERENCES scene (project_id, scene_id)
UNIQUE (project_id, shot_code)
```

本批次照此执行，不另行设计。

## 三、`gate_instances` 为什么没有建

`GateInstance` **没有任何 schema**。`contracts/phase0/contract_amendments.yaml`（V205-R14-A8 `residual_risks`）已把它登记为「下一个同型缺口」。

凭空建一张无契约的表，等于给系统造一个新的、无约束的事实源——**这比留一条已知缺口更坏**。因此这 1 条不建，闸门用带可机检前提的豁免承接：

```
EXEMPT｜PARENT_NO_SCHEMA
前提：契约仍无 GateInstance 定义 ∧ 库中仍无 gate_instances 表
```

前提一旦不成立（有人补了契约、或凭空建了表），豁免**自动失效**并退回 `DANGLING`。自检 S4 专门证明这一点：在克隆库里凭空建一张 `gate_instances`，该列立刻从 `EXEMPT` 变 `DANGLING`。

## 四、`Shot` 与首个 `ShotRevision` 的循环引用

契约要求两者在同一事务创建（ADR-R18）：`Shot.shot_revision_id` 指向首个修订，`ShotRevision.shot_id` 指向镜头，两边都 NOT NULL。若外键立即检查，事务内先插的一方必然找不到对方。

处置：两条边都设为 `DEFERRABLE INITIALLY DEFERRED`。只设一条时，约束检查顺序会依赖 INSERT 顺序，而「先建哪个」不该是调用方要关心的事。

**关键：延迟不等于不检查。** 验证装置的 `C1` 是这一点的反证——在同一事务里插一条指向缺失修订的 `Shot`，再 `SET CONSTRAINTS ALL IMMEDIATE`，必须 `23503`。它若提交成功，说明约束根本不存在。

## 五、验证结果

| 装置 | 结果 | 证明什么 |
| --- | --- | --- |
| `verify_batch5.py` | **26/26** | 缺口可复现（G1）、接边后同一条写入被拦（N1）、跨项目挂载 `23503`（C2）、`VALIDATE` 非恒真（V1）、`DEFERRABLE` 真在检查（C1）、状态与护栏双向（C3~C7）、RLS 与既有表同形（R1）、降级对称（D1~D3） |
| `verify_ownership_consistency.py` | **PASS 22 / EXEMPT 3 / DANGLING 1**，自检 **5/5** | 50 条派生候选列全部被认领；豁免带可机检前提且会自动失效 |
| `acceptance_ledger.py` | **34/34**，自检 **5/5** | 新增 6 行（2 张表 × 真实/一致两档）全部闭环；五态判定互不相同，失败行可精确定位 |
| `verify_ownership_closure.py` | 8/8 | 台账之外没有别的归属表 |
| `verify_guard_coverage.py` | 70/70 | 逐表例外通道仍接通 |
| `verify_exception.py` | 37/37 | 受控例外行为不变 |
| `verify_grants.py` | 11/11 | 不变量「有表写权限 ⇒ 有函数 EXECUTE」成立（N/A 0） |
| `rls_acceptance.py` | 10/10 | 隔离项目对业务不可见、对审计可见 |
| `verify_batch2/3/4` | 16/16 · 15/15 · 21/21 | 前四批行为未被破坏 |
| `scripts/contract_check.py` | PASS | 契约一致 + 代码未写死角色名 |

### 目标库逐条 `VALIDATE`

8 条后加外键中 **7 条一次通过**；`fk_generation_tasks_shot_project` 被一行挡住，处置见下节。补完父行后 8/8 全部 `convalidated`。

## 六、那行挡路的合成脏数据

`generation_execution.generation_tasks / gt-mismatch` 是 `seed_legacy_dirty.py` 为验证「组织错配可被订正」造的样例。该脚本只在归属外键**建立之前**可运行，产物属上一阶段遗留。

它的 `shot_id = 'shot-gt-mismatch'` 是插入模板自动生成的假值，从来不是它要证明的命题的一部分（该命题已由工单 `WO-OWN-2026-1120-001` 完成、`repair_ownership` 已验证）。但批次五挂上外键后，这行使 `VALIDATE` **永久失败**——而「永远验证不了」与「未验证标记为通过」同样不是闭环。

两条常规处置都被不变量挡住了：

1. **删除** → 守卫直接拒绝：`generation_execution.generation_tasks 业务对象不可物理删除`。这是 ADR-R11 不可变守卫在正常工作，未绕过。
2. **改 `shot_id`** → 它不在受控例外允许修改的归属三列内，同样会被守卫拒绝。

故采用第三条：**补齐它引用的父行链**（`episodes` → `scenes` → `shots` → `shot_revisions`），让引用真实成立，而不动那行本身。证据见 `docs/audits/mig-recon/gt-mismatch-parent-backfill.json`。

> 这 4 行是补齐引用所必需的最小占位。镜头域创建入口（`SHOT.REVISION.CONFIRM`）落地时应由正式流程替换——已在证据文件里登记为待办。

## 七、顺带修掉的三个装置缺陷

同一类根因：**清单/集合只列了「当前用得上的那些」**。

1. **`lab.OWNERSHIP_STACK` 缺第五层** → `rewind_to` 会先撤批次四，撞 `uq_shot_revisions_entity_project_tenant_org` 的依赖（`shots.fk_shots_current_revision`）。报文说的是「批次四撤不动」，根因却是「批次五没进栈」。
2. **`acceptance_ledger.SELFTEST_MIGRATIONS` 只有 5 条**，跳过 `b0c9` / `c1d2` / `d2e3` / `e3f4`。当时看不出问题——台账的判定 SQL 一条也不依赖那几层。批次五的 RLS 策略调用 `repair_row_allowed` 之后立刻炸出来了。
3. **各批次接口不一致**（有的 `upgrade_steps()` 返回 `Step`、有的 `upgrade_sqls()` 返回 `str`）→ 统一走 `lab.upgrade_sqls`。

另有两处判定口径修正：

- **新建表的外键无法处于 `NOT VALID`**：它写在 `CREATE TABLE` 里，DDL 不允许建表时声明 `NOT VALID`（没有历史行可豁免）。台账自检的 `pending` 态因此不能断言「全部待验证」，改为「4 行结构通过 + 30 行待验证」，并在代码里说明这是 DDL 的事实而非验收宽松。
- **列尚未增加 ≠ 0 条违规**：批次五之前 `shot_revisions.episode_id` 不存在，判定 SQL 会抛 `UndefinedColumn`。报 0 会让台账在列都不存在的库上看起来干净，故显式判「不可计算」。

## 八、仍未闭环

| # | 事项 | 性质 |
| --- | --- | --- |
| 1 | `shots.rework_basis_ref → workflow.rework_tasks` 报 `DANGLING` | `ReworkTask` **有契约、无运行时表**。这是**新建表才第一次进入视野**的边——以前 `shots` 不存在，没人问过它指向谁。不登记为「不是边」，否则把「未实施」洗白 |
| 2 | `GateInstance` 无 schema | 已登记（V205-R14-A8），闸门用可机检豁免承接 |
| 3 | `shots` / `scenes` 的**创建入口**未定 | ADR-R19 明载「须在 Scene 运行时设计时一并裁定」。本批次**刻意不建 service / action**，不替所有者裁定「确认拆镜即建立正式场次」 |
| 4 | `afs_audit` 的 SELECT 范围 | 沿用上轮遗留，未逐表论证 |
| 5 | ADR 是否记录本批次 | 待所有者确认口径（建议记「一致」提升为验收必备项 + 豁免须带可机检前提） |

## 九、一句话结论

**原 9 条 DANGLING 已全部消除**：5 条接 `shots`、2 条接 `scenes`、2 条由新的 `scene_id` / `episode_id` 替代。剩下的 1 条是新表带来的、指向 `ReworkTask`（有契约无运行时），与 `GateInstance` 同类，不能用「建表」消灭，只能用「实施」消灭。
