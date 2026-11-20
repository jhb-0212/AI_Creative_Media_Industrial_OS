# pending/feat-ownership —— 归属约束补齐（**【2026-11-20 已在 integration 分支激活】**）

## 当前状态（先看这一节）

| 项 | 状态 |
| --- | --- |
| 迁移是否已上链 | **是**。五个迁移已提交到 `integration/phase1-identity-data-ownership`（`4278971` / `256ce6e` / `2c1037c`） |
| 目标库是否已执行 | **是**。`localhost:15444/ai_film_industrial` 从 `c4e5f6a7b8d9` 升到 `e3f4a5b6c7d8` |
| 14 表闭环 | **14/14 PASS**（`convalidated` 全 `t`，违规行数全 0） |
| RLS 行为验收 | **10/10 PASS** |
| 生产库 | **未触碰**（`15432` / `ai_film_verify` / 模板库均未操作） |
| 阶段 3 | **仍不放行**——本轮是数据与约束落地，不是放行评审 |

执行报告：`docs/audits/2026-11-20-quarantine-project-activation.md`
例外工单：`docs/audits/2026-11-20-workorder-WO-OWN-2026-1120-001.md`

### 为什么本目录仍然保留

本目录是**源**，`integration` 分支的 `data/migrations/versions/` 是**激活副本**。
两者内容一致，都保留，原因不是懒得删：

- main 工作区 checkout 的是 main 链（head `c1a2b3d4e5f6`），这些迁移的
  `down_revision` 指向**分支链**。放进 main 的 `versions/` 会立刻造出第二个 head，
  使 `alembic upgrade head` 变成二义性命令，甚至被人 `alembic merge` 掉，
  从而在 main 链上凭空插入一批针对分支表的 DDL。
- 因此 main 侧的源只能放 `pending/`；激活必须在分支 worktree 内完成。

**不要**把本目录的文件复制进 main 的 `versions/`——这是刻意保留的隔离，不是遗漏。

## 为什么当初放在 `pending/` 而不是 `data/migrations/versions/`

Alembic 的 `versions/` 是**整目录发现**：任何带 `down_revision` 的 `.py` 都会被加载并参与
建链。本迁移的 `down_revision = c4e5f6a7b8d9` 是**分支链 head**，而主工作区 checkout 的是
main（`c1a2b3d4e5f6`）。一旦放进主工作区的 `versions/`：

- 主支上会同时存在两个 head（`c1a2b3d4e5f6` 与分支 head）；
- `alembic upgrade head` 立即变成二义性命令而报错；
- 更糟的是有人可能 `alembic merge` 掉它，从而在 main 链上凭空插入一批针对分支表的 DDL。

所以激活在 `integration` 分支的 worktree 内完成，主工作区只保留源。

## 文件

| 文件 | 作用 |
| --- | --- |
| `a9b8c7d6e5f4_ownership_fk_backfill.py` | organizations 目标键 + projects 复合唯一键 + 14 条归属外键（NOT VALID）；`upgrade_steps()` / `downgrade_steps()` 是唯一事实源 |
| `b0c9d8e7f6a5_ownership_repair_exception.py` | 13 个不可变守卫接入受控例外 + 映射表 + 判定函数 + 专用角色 |
| `c1d2e3f4a5b7_quarantine_project_rls.py` | D-10.3 隔离项目：判定函数 + 终态 CHECK + 18 条 RESTRICTIVE 策略 + 审计角色 |
| `d2e3f4a5b6c7_repair_role_grants.py` | 受控例外的运行前提：表权限 + RLS 行级可见性 + 两个判定函数的 EXECUTE |
| `e3f4a5b6c7d8_repair_rls_bypass.py` | 19 条 scope 策略的 repair 旁路（**侵入既有 RLS**，须评审）+ 原表达式备份表 |
| `verify_on_clone.py` | 在克隆库上跑 a9b8 的 upgrade / VALIDATE / 新写入语义 / downgrade，13 项断言 |
| `docs/audits/mig-recon/verify_exception.py` | b0c9 的行为验证（结构 / 判定 / 端到端 / 降级 / 幂等），36 项 |
| `docs/audits/mig-recon/verify_guard_coverage.py` | 13 张守卫表**逐表**矩阵，证明每张表都真的接通例外通道 |
| `docs/audits/mig-recon/seed_legacy_dirty.py` | 在外键建立**之前**造存量脏数据（4 类脏态 + 干净对照） |
| `docs/audits/mig-recon/repair_ownership.py` | 逐行探测 → R1/R2/R3 判定 → 受控例外订正 → 断言 + 回读 |
| `docs/audits/mig-recon/rls_acceptance.py` | D-10.3 要求 6 的 RLS 行为验收（10 用例） |
| `docs/audits/mig-recon/guard_fixtures.py` | 13 张表的**最小合法行**声明式夹具 |
| `docs/audits/mig-recon/lab.py` | 验证脚本共用的克隆 / 连接 / 断言 / 三态记录装置 |

`upgrade()` / `downgrade()` 只是把 `upgrade_steps()` / `downgrade_steps()` 逐条交给
`op.execute()`。验证脚本 import 的是同一份步骤列表——**验证的就是迁移要执行的那批 SQL，
不是「照着 SQL 另写一遍」**。

## 激活过程中被数据库打回的五个缺口（务必先读，避免重复踩）

1. **`@dataclass` 迁移无法被 Alembic 加载**：Alembic 用 `module_from_spec` 不登记
   `sys.modules`，dataclass 解析注解时回查得到 `None`。已改 `NamedTuple`。
   验证脚本曾手动补 `sys.modules` 绕过它，于是「13/13 通过」掩盖了「alembic 读不进来」。
2. **`afs_data_repair` 零表权限**：例外装好了却没有主体能用。
3. **RLS 先于守卫 → `UPDATE 0` 不报错**（D-10.2.3 陷阱 b 的实景）。
4. **跨组织订正数学上不可能**：`project_scope_*` 是 RESTRICTIVE，
   USING 要旧 org、WITH CHECK 要新 org，同会话无解 → 必须给 repair 一条绑映射表的旁路。
5. **降级无法用正则还原**：PG 把 `entity_id::text` 规范化为 `(entity_id)::text`，
   括号无法平衡 → 改成「升级时备份原表达式，降级逐字还原」。

另有一处顺序依赖：`ownership_repair_map_match` 缺 `EXECUTE`（`b0c9` 的 E5 只授 `allows`，
E6 的按表收敛在「角色当时无表权限」时跳过它）。


## 验证结果（2026-11-20，真 PostgreSQL 16.15，克隆库）

```
补齐迁移克隆库验证：13/13 通过
D1   14 条归属外键全部建成为 NOT VALID
D1b  organizations 表与 3 个唯一键就位
D2   空数据下 VALIDATE 全部成功（14/14）
D2b  VALIDATE 后 convalidated 全为 true
D2c  重复执行 upgrade 幂等
D3a  脏行存在时 upgrade 仍成功（NOT VALID 不扫描历史行）
D3b  新写入指向不存在项目 → 23503 被拒（NOT VALID 对新写入仍生效）
D3c  归属正确的写入不被误伤
D3d  组织错配写入被 23503 拒绝（三列外键的真实收益）
D3e  脏行未清时 VALIDATE 必然 23503
D4a  downgrade 后约束数回到基线（54 → 71 → 54）
D4b  downgrade 后 organizations 表被移除
D4c  无同名约束残留
```

复跑：

```bash
python data/migrations/pending/feat-ownership/verify_on_clone.py
```

脚本自建 `mig_lab_clean` / `mig_lab_dirty` / `mig_lab_down` 三个库并在结束时删除。
模板库 `ai_film_industrial`、main 链、`ai_film_verify`、生产**均不触碰**。

## 两个迁移的分工与执行顺序

```
c4e5f6a7b8d9（分支链 head）
  └─ a9b8c7d6e5f4  层 1：归属外键 NOT VALID（本目录第一个文件）
       └─ b0c9d8e7f6a5  层 2：不可变守卫的受控例外（本目录第二个文件）
```

| 迁移 | 作用 | 验证 |
| --- | --- | --- |
| `a9b8c7d6e5f4` | 建 `organizations` + 4 个目标唯一键 + 14 条归属外键（`NOT VALID`） | `verify_on_clone.py` 13/13 |
| `b0c9d8e7f6a5` | 13 个不可变守卫接入受控例外；建 `afs_data_repair`、映射表、判定函数 | `verify_exception.py` 36/36 |

### 逐表覆盖（2026-11-20 补）

`verify_exception.py` 的端到端只覆盖 3 张表；其余 10 张仅有「函数体里出现
`ownership_repair_allows`」这一条结构证据，**不证明接入点位置正确、返回值真的决定走向、
行标识列取值正确**。`verify_guard_coverage.py` 用声明式夹具在真实行上逐表跑：

```
逐表覆盖矩阵：65/65 通过（N/A 3，待裁决缺口 3）
N1 无令牌改归属列      → 13/13 P0001
N2 令牌+映射+成员      → 13/13 UPDATE 1 且回读值已变
N3 令牌+映射+成员改内容列 → 13/13 P0001
N4 令牌+映射+非成员    → 13/13 P0001（不是 42501）
N5 例外放行后改到不存在的目标 → 10/10 23503；3 张无归属外键 → N/A（缺口单列）
```

**N/A 的 3 张表不是通过**：`shot_specs` / `approval_steps` 在 `a9b8` 的 `DERIVED_NO_FK`
中显式列为刻意不建；`qc_reports` 则**既不在台账也不在任何「刻意不建」清单里**——
它把归属排除在闭环之外却没有任何声明，属范围缺口，需所有者裁决（详见
`docs/audits/2026-11-20-guard-coverage-matrix.md` §4 F3）。

**顺序不可颠倒**：层 1 是「新写入归属合法」的保护，层 2 是「已确认归属不被越权改写」的保护，
D-10.2 要求前者先立；但两者对同一批表生效，先跑 `a9b8` 再加例外，语义最清晰。

## 激活前置（四条，缺一不可）

1. **ADR-R40 复审确认**（当前有效文本 v1.4.6，含 §13 D-10 三项裁决、D-10.2 读法裁定
   与 D-10.2.3 的实现陷阱），且阶段 3 被放行——本迁移是阶段 3 的输入，不是它的替代。
2. **`D-10` 落地链就绪**（见 `docs/audits/2026-11-20-ownership-repair-path-decision.md`）：
   两个迁移都**不含任何数据修复**。存量脏行的订正必须走
   **层 2 受控例外**（工单令牌 + 映射行 + `afs_data_repair` 角色，四条件），
   **不得以「维护窗口禁用触发器」的方式执行**——该方式已在 ADR D-10.2 撤回并否决。
3. **为 `afs_data_repair` 授予表权限**（迁移**刻意不做**）：订正执行角色需在被订正表上具备
   **`SELECT` 与 `UPDATE`**。只授 `UPDATE` 会在 `WHERE` 子句处得到 `42501`，
   看起来像「守卫拒绝」而实际是权限缺失，极易误判。
   是否以表 owner 身份执行属运维决策，迁移不替人决定。
4. **`organizations` 表归属确认**：`a9b8` 会在分支谱系**新建**该表并从 `projects.organization_id`
   物化成行。若届时决定改走「main 谱系重放」（organizations 已存在），则阶段 1 的
   O1~O4 必须整段删除，只保留 K1/K2 与阶段 3~5。

## 受控例外的运行形态（订正时）

```sql
BEGIN;
SET LOCAL ROLE afs_data_repair;                                  -- 条件 1
SET LOCAL app.ownership_repair_ticket = 'TCK-2026-0042';         -- 条件 2
UPDATE asset_service.asset_versions                              -- 条件 3：只改归属三列
   SET project_id = 'proj-real'
 WHERE entity_id = 'av-...';                                     -- 条件 4：映射表有匹配行
GET DIAGNOSTICS affected = ROW_COUNT;   -- 必须断言 = 待修行数，不能只看有无异常
UPDATE project_planning.ownership_repair_map
   SET applied_at = now()
 WHERE table_name = 'asset_service.asset_versions'
   AND ticket_ref = 'TCK-2026-0042' AND applied_at IS NULL;      -- 盖章后例外自动关闭
COMMIT;
```

> **两条实测教训**（详见 `docs/audits/2026-11-20-ownership-repair-exception-implementation.md`）：
> ① 目标表多启用 RLS，越权写入会退化为 `UPDATE 0` 而非报错——**只看「没报错」会误判成功**，
> 必须断言影响行数并回读值；② 判定函数必须 `SECURITY INVOKER`，写成 `SECURITY DEFINER`
> 会让 `current_user` 变成函数 owner，角色条件静默失效。

## 激活方式

```bash
# 在分支谱系工作区（feat）而非 main 上执行
git mv data/migrations/pending/feat-ownership/a9b8c7d6e5f4_ownership_fk_backfill.py \
       data/migrations/versions/
alembic heads          # 必须只输出一个 head：a9b8c7d6e5f4
alembic upgrade head
```

`verify_on_clone.py` 不跟随迁移移动，留在 `pending/feat-ownership/` 作为回归脚本；
它靠 `HERE.glob("*_ownership_fk_backfill.py")` 定位迁移文件，移动后需把路径改为
`data/migrations/versions/`。

## 已知边界（不掩盖）

| 项 | 状态 |
| --- | --- |
| `organizations` 未启用 RLS | **刻意**。分支用 `app.current_org_id`、main 用 `app.current_organization_id`，两套 GUC 契约未统一；此时套任一方的策略都会静默返回 0 行或漏隔离。且该表当前对新角色无任何 GRANT，读取面本身是关闭的。登记为债务，不在本迁移临时加策略。 |
| `organizations.name` 是占位值 | 形如 `(unnamed) <uuid>`。分支从未存过组织名，编一个「像真的」名字等于伪造业务事实；占位值至少可被一眼识别并批量替换。 |
| 14 条外键全部 `NOT VALID` | 这是「先立层 1 写入保护」而非「已闭环」。闭环判据是 `VALIDATE CONSTRAINT`，须在存量订正（走层 2 受控例外）完成后逐条执行，见裁决文档与 ADR §13 D-10。 |
| 并发写入下的锁行为未测 | 克隆库是空库串行执行，`ADD CONSTRAINT NOT VALID` 的锁级别与大表耗时未测量。 |
| `ON DELETE` 语义未设计 | 全部默认 `NO ACTION`。级联/置空语义需与 Gate/DeliveryPackage 生命周期一并裁定。 |
