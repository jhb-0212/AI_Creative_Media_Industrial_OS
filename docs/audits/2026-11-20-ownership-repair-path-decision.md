# 归属脏数据修复路径 ①/② 裁决（附受控例外 runbook）

**文档性质**：裁决建议 + 可执行 runbook。**不含迁移代码**（迁移见
`data/migrations/pending/feat-ownership/`），**不含数据修改**（本轮零写生产/模板库）。

**为什么要单独裁这一条**：`NOT VALID` 让「先建约束」变成可能，但它只是个中间态。
中间态能否收敛到 `VALIDATE`，取决于「存量脏行怎么修」——而这恰恰被 ADR-R11 的
不可变列守卫挡住了。这不是实现细节，它决定迁移形态、决定阶段 3 的放行条件。

**取证来源**：T3 脏状态实验（E13，49 步全绿）+ 本轮补齐迁移克隆库验证（13 项全绿，
见 `data/migrations/pending/feat-ownership/README.md`）。全部为真 PostgreSQL 16.15 实测，
非从源码推断。

---

## 1. 裁决摘要

| 方案 | 能否达成 `VALIDATE`（约束真闭环） | 是否改写历史 | 主要成本 | 结论 |
| --- | --- | --- | --- | --- |
| **① 就地订正（走受控例外）** | **能** | 是（受控、留痕） | 需逐行人工映射 + `afs_data_repair` 角色 + 工单令牌；**不摘守卫** | **采纳** |
| ② 前向新建版本（旧行标 `SUPERSEDED`） | **不能** | 否 | 改变资产身份、波及下游引用、历史行仍在且仍违规 | **不足以单独成立** |
| ③ 永久 `NOT VALID`（不修数据） | 不能，且**永不打算**达成 | 否 | 无 | 不采纳：等于把「约束」降级为「新写入过滤网」，历史归属永不被证明 |
| ④ ②+① 组合（新版本 + 旧行仍修） | 能 | 是 | ①的成本 + ②的成本 | 仅当业务上确实需要新版本时才叠加，不是替代 ① |

> **一句话结论**：选 **①**，但**不许摘掉守卫**。D-10 后的执行序是
> **「建键 → 挂 `NOT VALID`（层 1 写入保护）→ 守卫常开 + 受控例外（层 2 写入保护）→
> 存量订正走受控例外 → `VALIDATE` → 14 行台账全 PASS」**。
>
> 【2026-11-20 所有者裁决后修订】本文件 §4 与 §5.3 的原稿建议「维护窗口内
> `DISABLE TRIGGER`」，**已按 D-10.2 撤回**：禁守卫期间保护是被整体摘掉的，而受控例外
> 只对「映射表里的那一行 + 归属三列」开一道可审计的窄门。修订后的内容见 §4/§5.3，
> 撤回理由与四重条件见 `ADR-R40 v1.4.5 §13 D-10.2 / D-10.2.1`。
> 【v1.4.5 补】所有者已裁定 D-10.2 的「守卫」= **层 1 归属写入保护（外键）**；
> 层 2 不可变守卫**不得禁用**。本文档旧稿中「维护窗口」一词仅指**业务低峰时段**，
> 不含「禁触发器」含义。

---

## 2. 为什么 ② 单独无法成立（三条硬证据，可复算）

### 2.1 前向新建不改变旧行的归属列，旧行仍在表里

`VALIDATE CONSTRAINT` 扫描**整张表**。新增一条归属正确的新版本，并不会让旧行的
`project_id = 'legacy-unassigned'` 变得可解析。实测（D3e）：

```
ALTER TABLE asset_service.asset_versions VALIDATE CONSTRAINT fk_asset_versions_project_tenant_org;
-- ERROR: insert or update on table "asset_versions" violates foreign key constraint
-- DETAIL: Key (project_id, tenant_id, organization_id)=(ghost-project, …) is not present in table "projects".
```

换言之：② 解决的是**内容演进**（哪一版是当前版本），不是**归属事实错误**（这一行属谁）。
两者语义不同，不能互相顶替。

### 2.2 `NOT VALID` 不标记脏行，时间越久越查不出来

约束处于 `NOT VALID` 时，PG **不会记录**哪些行违规。要定位只能逐表比对：

```sql
SELECT count(*) FROM <table> t
LEFT JOIN project_planning.projects p
       ON p.entity_id = t.project_id
      AND p.tenant_id = t.tenant_id
      AND p.organization_id = t.organization_id
WHERE p.entity_id IS NULL;
```

所以「先永久 `NOT VALID`，以后再修」在工程上是不可收敛的：脏行集合只会在一次次业务
写入与迁移中变大，而没有任何机制提醒还有多少。这是「④ 不采纳」的实质理由，不是洁癖。

### 2.3 ② 的成本落在下游，而不只是本表

分支侧的资产被这些对象引用：`qc_reports.asset_version_id`、`delivery_manifests.asset_refs[]`、
`shot_specs`/`generation_tasks` 的血统字段。把旧版本标 `SUPERSEDED` 会改变「当前有效版本」
的判定，进而改变 QC 结论指向哪一版。修一次归属，代价是重算一条链路。

---

## 3. 分支为什么会自锁：顺序错误

T3 实测（C8 / C10）：

| 步骤 | 语句 | 结果 |
| --- | --- | --- |
| C8 | `UPDATE asset_versions SET project_id='proj-1' WHERE entity_id='…'` | `P0001 不可变内容列（ADR-R11）` |
| C10 | **同一语句、同一数据**，仅在 `DISABLE TRIGGER trg_asset_versions_guard` 期间 | `OK` |

唯一差异是守卫状态 ⇒ `P0001` 的来源被隔离确认。也就是说：**分支先把归属列设成
不可变内容列，之后才发现归属数据是错的**，于是「修数据」这件事在自己的守卫面前不合法。

这不是某一行数据的偶发问题，是**部署顺序的结构性后果**。根治方式只有一条：
**不可变守卫必须晚于数据订正上线**。守卫本身没错（它是 append-only 的正确实现），
错的是它上线的时点。

---

## 4. 落地时序（阶段化，每阶段一个可核闸门）

【按 D-10.2 修订】原稿把「不可变守卫」排在最后，等于默认存在一段「无守卫」的窗口。
所有者已明确拒绝该次序。修订后**两层写入保护都在任何订正之前完成**，
订正本身走守卫提供的受控例外（守卫始终在线，从未被禁用）。

| 阶段 | 动作 | 事务形态 | 闸门（不过则中止） |
| --- | --- | --- | --- |
| S0 | 建目标键（organizations + 3 个唯一键） | 迁移，可回滚 | `upgrade` 成功且 `downgrade` 可逆 |
| S1 | **层 1 写入保护**：14 条归属外键 `NOT VALID` | 迁移，可回滚 | 新写入违规被 `23503` 拒（D3b/D3d 已证） |
| S2 | **层 2 写入保护**：不可变守卫改造为「受控例外」，四重条件生效 | 迁移，可回滚 | 负向用例：非映射行改归属仍 `P0001`；非归属列改内容仍 `P0001`；无工单令牌仍 `P0001` |
| S3 | **只读**盘点：逐表统计违规行数、按违规形态分组 | 只读 | 得到「违规总数 + 可唯一映射 / 需人工判断 / 无法判断」三档数量 |
| S4 | 人工映射入库（工单化，逐行） | 小事务，可回滚 | 映射覆盖 100% 待修行，且无一行自动推断 |
| S5 | 订正执行：置工单令牌 → 改归属三列 → 校验行数 → 盖章 `applied_at` | 单事务 + `lock_timeout` | 影响行数严格等于映射条数；盖章后例外自动关闭 |
| S6 | `VALIDATE CONSTRAINT`（逐条，独立事务） | 每条一个事务 | 该条 `convalidated = true` |
| S7 | **验收台账**（附录 J.2） | 只读 | 14 行全 PASS；否则整体「未通过」 |

> **为什么 S2 能排在订正之前**：受控例外不是「关掉守卫」，而是让守卫自己判断
> 「这一行、这三列、这张工单」是否被授权。因此 S1/S2 完成后，**任何不符合归属规则的
> 新写入都进不来，任何未被工单授权的历史改写也进不来**——这正是所有者要求的
> 「不能出现没有保护的写入窗口」。
>
> S6 的 `VALIDATE` 取 `SHARE UPDATE EXCLUSIVE` 锁，**不阻塞读写**，因此可以逐条、
> 分多次、在业务时段执行；不需要与 S5 同一个窗口。

---

## 5. 订正 runbook（S3~S6 的可执行模板）

### 5.1 S3：映射表（不是迁移，不进 `versions/`）

```sql
CREATE TABLE IF NOT EXISTS project_planning.ownership_repair_map (
    id             bigserial PRIMARY KEY,
    table_name     text        NOT NULL,   -- 例 'asset_service.asset_versions'
    entity_id      text        NOT NULL,
    old_project_id text,
    new_project_id text        NOT NULL,
    old_organization_id uuid,
    new_organization_id uuid,
    reason         text        NOT NULL,   -- 判定依据，不接受 'fix'
    ticket_ref     text        NOT NULL,   -- 工单号，可回溯到人
    applied_at     timestamptz,
    UNIQUE (table_name, entity_id)
);
```

**为什么不复用 `audit.status_transitions`**：那是**状态转移事实表**，记录
`from_status → to_status`。归属订正是「历史数据订正」，不是状态变更，混写会让
状态机审计出现无法解释的转移。留痕落在 `ownership_repair_map`（含工单号与 reason）
+ `applied_at`，语义清晰且可追人。

### 5.2 S3 的硬规则

- **禁止自动推断**：`legacy-unassigned` 不是真实项目（T3 F9 证明只要一行这样的行，
  两列外键就 `23503`）。把哨兵值自动映射到「最像的项目」= 伪造归属事实。
- 三档处置，逐行由业务定：
  1. **迁移到真实项目**——有外部证据（工单、历史日志、人工确认）；**须走例外工单**；
  2. **移入隔离项目（默认）**——确实无法判定但必须保留资产：每个组织一个真实项目行
     `entity_id = 'quarantine-' || organization_id`（详见 D-10.3 七条要求），
     保留行的原 `organization_id` 不变，只改 `project_id` 指向隔离项目。
     这是「承认不知道」，不是猜；
  3. **删除**——仅在确认无下游引用且业务批准时；**须走例外工单**。注意经 DML 删不掉
     （append-only），删除同样只能走 S5 的受控例外，且必须有删前备份。
- 一张表修完才开下一张，避免一个大事务锁住全部资产表。

### 5.3 S5：订正执行（单事务，走受控例外，**不摘守卫**）

【按 D-10.2 修订】不再 `DISABLE TRIGGER`。守卫始终在线，只对「映射表里那一行 + 归属三列 +
本工单」放行。四重条件**同时**满足才通过（缺一即 `P0001`）：

| # | 条件 | 为什么需要它 |
| --- | --- | --- |
| ① | 会话角色是 `afs_data_repair` 成员 | **GUC 不可伪造这一条**，是本机制的安全锚点；绕开 D-8 已接受的「GUC 可伪造」边界 |
| ② | 会话设置 `app.ownership_repair_ticket` | 把「这一次改动」绑定到一张工单，而不是「这段时间都能改」 |
| ③ | 映射表存在 `table_name + entity_id` 匹配、`applied_at IS NULL`、`ticket_ref` 等于该令牌的行 | 把豁免面收窄到**具体行**；不在映射表里的行一律照旧 `P0001` |
| ④ | 本次变更只涉及 `project_id` / `organization_id` / `tenant_id` | 归属订正不得成为改内容的通道；其余内容列仍然不可变 |

```sql
BEGIN;
SET LOCAL lock_timeout = '5s';                       -- 拿不到锁就失败，不排长队
SET LOCAL statement_timeout = '300s';
SET LOCAL app.ownership_repair_ticket = 'OPS-2026-1120-001';   -- 条件②

-- 改映射表里列出的行，并在同一 DO 块内用 GET DIAGNOSTICS 断言影响行数。
-- 断言必须绑定真实 ROW_COUNT：用「事后 count(applied_at)」反推会恒等于 expected
-- （真空真陷阱），等于没断言。
DO $$
DECLARE expected integer; affected integer;
BEGIN
    SELECT count(*) INTO expected
      FROM project_planning.ownership_repair_map
     WHERE table_name = 'asset_service.asset_versions' AND applied_at IS NULL;

    IF expected = 0 THEN
        RAISE EXCEPTION 'S5 中止：没有待修复行，空跑无意义';
    END IF;

    UPDATE asset_service.asset_versions t
       SET project_id      = m.new_project_id,
           organization_id = COALESCE(m.new_organization_id, t.organization_id)
      FROM project_planning.ownership_repair_map m
     WHERE m.table_name = 'asset_service.asset_versions'
       AND m.applied_at IS NULL
       AND t.entity_id = m.entity_id
       AND t.project_id IS NOT DISTINCT FROM m.old_project_id;

    GET DIAGNOSTICS affected = ROW_COUNT;

    IF affected <> expected THEN
        RAISE EXCEPTION
            'S5 行数不符：待修 % 行、实修 % 行。差集说明映射与真实数据不一致，须重新盘点，不得放行',
            expected, affected;
    END IF;
END $$;

-- 盖章：例外随即自动失效（条件③ 不再成立），守卫恢复全量不可变
UPDATE project_planning.ownership_repair_map
   SET applied_at = now()
 WHERE table_name = 'asset_service.asset_versions' AND applied_at IS NULL;
COMMIT;
```

#### 5.3.1 守卫的受控例外入口（设计参考，**未实施**）

守卫函数需在原有「逐列比对 → `RAISE`」之前插入一段例外判定。**这不是本轮要提交的
代码**，而是 D-10.2 要求实现的形态，写在 runbook 里是为了让「四重条件」有可核对的落点：

```sql
-- 仅供设计核对，未在任何库执行
DECLARE
    ticket text := current_setting('app.ownership_repair_ticket', true);
    allowed boolean := false;
BEGIN
    IF ticket IS NOT NULL AND pg_has_role(current_user, 'afs_data_repair', 'member') THEN
        SELECT EXISTS (
            SELECT 1 FROM project_planning.ownership_repair_map m
             WHERE m.table_name = 'asset_service.asset_versions'
               AND m.entity_id  = OLD.entity_id
               AND m.applied_at IS NULL
               AND m.ticket_ref = ticket
        ) INTO allowed;
    END IF;

    IF allowed THEN
        -- 条件④：只允许归属三列变化，其他内容列仍按原逻辑拒绝
        IF (to_jsonb(OLD) - 'project_id' - 'organization_id' - 'tenant_id')
           IS DISTINCT FROM
           (to_jsonb(NEW) - 'project_id' - 'organization_id' - 'tenant_id') THEN
            RAISE EXCEPTION '受控例外只允许变更归属三列（工单 %）', ticket;
        END IF;
        RETURN NEW;   -- 放行这一行；其余所有行仍走原不可变判定
    END IF;
    -- …原有逐列不可变与状态转移判定…
END;
```

> 关键点：**例外是守卫内部的一个分支，不是守卫的关闭**。`RETURN NEW` 只对这一行生效，
> 下一行走的还是完整判定。这比 `DISABLE TRIGGER` 强的地方不是「更严格」，
> 而是**失败模式不同**：禁守卫时若有意外语句，保护整个不在；受控例外时保护始终在，
> 只是对一行开了可审计的窄门。


> 两个细节决定这条断言真有约束力：① `GET DIAGNOSTICS affected = ROW_COUNT` 必须
> **紧跟** `UPDATE`，中间隔任何语句都会拿到上一条语句的行数；② `AND t.project_id
> IS NOT DISTINCT FROM m.old_project_id` 是「乐观核对」——若盘点之后业务又改过该行，
> 它不会命中，`affected` 就会小于 `expected` 并中止，避免覆盖别人刚写入的事实。
>
> 同一类缺陷在本项目有前例：E12 发现 INV 查询「返回 0 行看起来像通过」，根因同样是
> 「判定式没有绑定到真实观测值」。**断言不绑定观测值 = 没有断言。**


### 5.4 S6：逐条 VALIDATE（不与 S5 同窗口）

```sql
ALTER TABLE asset_service.asset_versions
  VALIDATE CONSTRAINT fk_asset_versions_project_tenant_org;
-- 不阻塞读写（SHARE UPDATE EXCLUSIVE）；成功即 convalidated = true
```

判定：

```sql
SELECT conrelid::regclass AS tbl, conname, convalidated
  FROM pg_constraint
 WHERE conname = ANY (ARRAY[
   'fk_projects_organization_tenant',
   'fk_generation_tasks_project_tenant_org',
   'fk_asset_versions_project_tenant_org'
   /* …14 条 */ ])
 ORDER BY 1, 2;
-- 判据：14 行且 convalidated 全为 true
```

---

## 6. 不可回滚的部分（必须提前接受）

| 动作 | 可回滚？ | 说明 |
| --- | --- | --- |
| 建约束 / `NOT VALID` / `VALIDATE` | 是 | DDL 可逆；`VALIDATE` 撤不回「已验证」这件事，但可用 `DROP CONSTRAINT` 退回 |
| 受控例外内的 `UPDATE` | **否** | 事务回滚只覆盖未提交部分；已提交的订正在 `ownership_repair_map` 里只能再修一次 |
| 删除脏行 | **否**（除非有备份） | 且受 append-only 约束，删除路径本身就需要受控例外，风险最高，放最后 |
| `SUPERSEDED` 标记 | 否（语义上） | 一旦下游按新语义读，回退会改变 QC 结论指向 |

因此 runbook 要求：**S5 之前必须对受影响表做逻辑备份**
（`pg_dump -t <table>` 或 `CREATE TABLE <table>_bak_<date> AS SELECT * FROM <table>`），
并在工单里登记备份位置。这是唯一能让 S5「事实上可回滚」的手段。

---

## 7. 诚实边界（本轮不主张的结论）

| 项 | 状态 |
| --- | --- |
| 生产库实际脏行数量与分布 | **未知**。既定裁决为「生产状态未知」，本轮未连接生产。S2 是必经的只读取证步骤，不是可选优化。 |
| 并发写入下的锁行为 | **未测**。全部实验在空库串行执行，`DISABLE TRIGGER` 的 `ACCESS EXCLUSIVE` 在实际流量下的等待时间未量化。 |
| 大表 `VALIDATE` 耗时 | **未测**。只验证了「能通过」，没测「要多久」。 |
| 受控例外在并发下的行为（同一行被两张工单同时改） | **未测**。设计上由「映射行 `applied_at IS NULL` + 行锁 + 乐观核对」三重拦住，但没有在真实并发下跑过。 |
| 各表违规行的业务含义 | **不在本轮职权内**。S3 的每一行都需要业务判断；本文只给机制，不给映射。 |

---

## 8. 三项裁决已下（D-10），以下转为「待实现」而非「待确认」

所有者于 2026-11-20 作出三项裁决，已写入 `ADR-R40 v1.4.4 §13 D-10`：

| 原待确认项 | 裁决 | 对本文件的影响 |
| --- | --- | --- |
| 是否采纳 ①；是否接受永久 `NOT VALID` | **有条件采纳 ①；拒绝永久豁免**，终态必须 14 表闭环 | §2.3「③ 永久 `NOT VALID`」从「不采纳的建议」升为**裁决明确的禁止项** |
| 部署顺序 | **拒绝「守卫晚于数据订正」**；写入保护必须先行 | §4 全表重排为 S0~S7；§5.3 由 `DISABLE TRIGGER` 改为**受控例外**；撤回原建议 |
| 哨兵行默认口径 | **默认移入隔离项目**；迁真实项目/删除走例外工单 | §5.2 第 2 项改为每组织一个 `quarantine-<organization_id>` 真实项目行 |

**仍待实现（本轮明确未做，不得表述为已完成）**：

1. 守卫函数的受控例外改造（§5.3.1 仅设计参考，**未提交、未执行**）。
2. 隔离项目的 RLS 策略（业务侧不可见 / 不可写）。
3. `afs_data_repair` 专用角色的创建与窗口期授予/回收。
4. 14 行台账的**真库实采**：脚本已可用（`acceptance_ledger.py`，自检证明可区分四种状态），
   但当前无障碍的库上跑出的必然是「未建立」，不构成验收。
5. 生产库脏行盘点（S3）——生产状态仍为「未知」，未连接。

---

## 9. 与本裁决相关的产物（本轮已交付）

| 产物 | 位置 |
| --- | --- |
| 补齐迁移（已写完、已验证、未激活） | `data/migrations/pending/feat-ownership/a9b8c7d6e5f4_ownership_fk_backfill.py` |
| 克隆库验证脚本与结果 | `data/migrations/pending/feat-ownership/verify_on_clone.py`、同目录 `README.md` |
| T3 脏状态实验（本文证据来源） | `docs/audits/2026-11-20-t3-dirty-state-findings.md`、`docs/audits/mig-recon/t3-dirty-state.{md,json}` |
| 14 表归属矩阵（迁移设计输入） | `docs/audits/2026-11-20-ownership-model-and-14-table-matrix.md` |

**本裁决不进 ADR-R40 v1.4.3**：该版已于本轮定版，指纹登记在
`docs/adr/ADR-R40-VERSION.txt`，任何正文改动都会使指纹失效。若要写入 ADR，
须以 v1.4.4 承接（或按 §13 决策记录体例编号 D-10）。
