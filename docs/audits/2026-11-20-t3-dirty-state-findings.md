# T3 脏状态验证：结论与对补齐迁移的约束

- 执行时间：2026-11-20
- 前置裁决：**批准 T3 脏状态验证，且应先于迁移编写**（本文件只产出结论与设计约束，**不含任何迁移代码**）
- 机器原始结果：`docs/audits/mig-recon/t3-dirty-state.md`（49 步，PASS 49 / FAIL 0）
- 驱动脚本：`docs/audits/mig-recon/t3_dirty_state.py`
- 结构化输出：`docs/audits/mig-recon/t3-dirty-state.json`

---

## 0. 这个实验回答什么，不回答什么

回答：**归属约束在「历史库已有脏数据」这一真实前置状态下，能不能建、建了拦什么、拦不住什么、脏数据怎么修**。

不回答：迁移文件怎么写、Revision 怎么排、生产数据有多少脏行。这三件事都需要另一个输入（生产环境取证），本轮裁决已明确生产状态「未知」。

---

## 1. 环境与可复现性

| 项 | 值 |
| --- | --- |
| 实验库 | `t3_lab`（由 `CREATE DATABASE ... TEMPLATE ai_film_industrial` 克隆，端口 15444 = step-feat，分支链已重放到 head） |
| 模板态 | `projects` 0 行、`asset_versions` 0 行、**`organizations` 表整张不存在** |
| 对照 | 端口 15443 = step-main（主支链 head），仅用于可空性对照，全部操作在事务内 `ROLLBACK` |
| 隔离 | 两个容器均为独立集群，与 `ai_film_verify` / `ai_film_industrial`（15432）/ 生产无任何连接 |
| 可重跑 | 脚本每次 `DROP DATABASE t3_lab` 后重建，不依赖上一次残留 |

**上一轮 `aborted` 的根因已定位并根治**：`reseed()` 里 `TRUNCATE project_planning.organizations` 在该表尚未创建时抛 `UndefinedTableError`，该异常不是 `PostgresError` 子类，直接穿透 `main()` 导致整脚本中断、无任何输出。修法是结构性的：**组织表的创建与写入都变成 A 阶段的显式步骤**，起点的 `reset_base()` 只清空模板中确实存在的两张表；同时 `run()` 增加非 `PostgresError` 的兜底记录（记 `EXC` 而非中断），使「脚本自身故障」与「数据库拒绝」在结果里可区分。

---

## 2. 七类场景结论

| 场景 | 关键步骤 | 实测 | 结论 |
| --- | --- | --- | --- |
| 1 正常归属 | B1→B2 | `OK`，干净数据上 FK 建成（`fk2=t`） | 归属完整时无副作用，约束可正常上线 |
| 2 孤儿项目 | C2→C3→C10→C12 | C3 `23503`、C12 `OK` | 建约束时**拒绝而非静默删数据**（C3 前后行数不变）；修完才能 VALIDATE |
| 3 租户错配 | D2→D3 | `23503` | 两列外键 `(project_id, tenant_id)` 即可拦 |
| 4 **组织错配** | E1→E2→E4 | E2 `OK`（**放行**）、E4 `23503`（拦截） | **两列外键存在结构性盲区**；三列 `(project_id, tenant_id, organization_id)` 才能闭合 |
| 5 哨兵值 | F1→F2→F9 | F1 `P0001`、F2 `OK`、F9 `23503` | 触发器只挡一个魔法字符串；历史哨兵行会**阻塞外键建立**，必须人工映射 |
| 6 NULL / MATCH SIMPLE | G1 + 可空性审计 | `23502`；62 个归属列可空数 = 0 | 分支侧放行路径**列级不可达**；主支可达（见 §3.5） |
| 7 NOT VALID 分阶段 | C4→C5→C7→C12 | C4 `OK(fk2=f)`、C5 `23503`、C7 `23503`、C12 `OK(fk2=t)` | NOT VALID 只跳过历史扫描，**新写入仍受约束**；脏未清时 VALIDATE 必失败且状态保持 `f` |

---

## 3. 六项结构发现（对迁移设计的直接输入）

### 3.1 目标键缺失是 42830 的唯一原因，与数据脏无关

`A0` 在空表上直接建两列外键即得 `42830 — there is no unique constraint matching given keys for referenced table "projects"`。补上 `uq_projects_entity_tenant` 后（A3），同样的语句在 B2 成功。

**含义**：补齐迁移的第一步不是清洗数据，是**先把被引用侧的 4 个目标键建出来**（`organizations` 表、`(entity_id, tenant_id)`、`(entity_id, tenant_id, organization_id)`、`projects→organizations` 外键）。在目标键之前做任何数据清洗都是无效工时。

### 3.2 两列外键无法发现组织错配 —— 这是必须放弃两列方案的实证

`E1` 写入 `project_id=proj-1`（属 T1/O1）、`tenant_id=T1`、`organization_id=O2` 的资产；`E2` 在**该行存在时**建两列外键**成功**（`fk2=t`）。换成三列后 `E4` 立即 `23503`。

**含义**：`fk_asset_versions_project_tenant` 这个在主支已存在并被 `convalidated=t` 的约束，**从不校验组织归属**。它在主支「看起来是归属闭环」，实际只闭环了 project+tenant 两维。补齐迁移若沿用两列，会把一个已被实验证明无效的约束当作达标标志。

### 3.3 脏数据无法就地修复 —— 不可变守卫（ADR-R11）与归属补齐直接冲突

| 步骤 | 语句 | 结果 |
| --- | --- | --- |
| C8 | `UPDATE asset_versions SET project_id = 'proj-1' WHERE entity_id='asset-orphan'` | `P0001 — asset_versions.project_id 为不可变内容列，禁止改写（ADR-R11）` |
| C10 | 同一语句，仅在 `DISABLE TRIGGER trg_asset_versions_guard` 期间 | `OK` |
| D4 / E5 | 改 `tenant_id` / `organization_id` | 同样 `P0001` |
| C12 | 维护窗口修完后 `VALIDATE` | `OK`（`fk2=t`） |

C8 与 C10 是**同一语句、同一数据、唯一差异是守卫状态**，因此 `P0001` 的唯一来源被隔离确认。

**含义（本轮最重要的可执行结论）**：
- 归属修复**不可能**在正常业务路径上完成，因为归属列恰恰是不可变内容列。
- 修复只有两条合法路径：①**维护窗口**（成对 `DISABLE TRIGGER` / `ENABLE TRIGGER` + `UPDATE` + `VALIDATE`）；②**前向新建版本**（把脏行标 `SUPERSEDED`，新写一条归属正确的版本）。
- ②保留了不可变性，但会改变资产身份与下游引用，成本远高于①。**这一点需要裁决，不在本实验范围内。**

### 3.4 触发器不是归属约束，历史哨兵行是硬阻塞

- `F1`：新写入 `project_id='legacy-unassigned'` → `P0001`（触发器拦住）。
- `F2`：新写入 `project_id='ghost-project'`（不存在的项目）→ **`OK`**（触发器放行）。

**含义**：`assert_asset_versions_project_assigned` 只做一次字符串比较，**不做存在性、不查租户、不查组织**。它降低「新增魔法值」的概率，但对已经存在的历史哨兵行毫无作用——`F9` 证明只要有一行 `legacy-unassigned`，两列外键就建不成（`23503`）。**哨兵行必须逐行人工决策映射到哪个真实项目，任何自动映射都是伪造归属事实。**

### 3.5 分支侧 MATCH SIMPLE 放行路径不可达；主支可达且被 RLS 显式容忍

分支侧全库 **62 个归属列（`tenant_id` / `organization_id` / `project_id`）`is_nullable` 全为 `NO`**，可空数 = 0。而主支 `projects.organization_id` 为 `YES`：

| 主支对照（已回滚） | 实测 |
| --- | --- |
| `projects.organization_id` 可空 | `YES` |
| `organizations` 表行数 | `0` |
| 反证：`organization_id` 非空且不存在 | `23503` |
| 正证：`organization_id` 置 `NULL` | **成功**（组织表一行都没有，项目照样写入） |

主支 `tenant_isolation_projects` 策略为 `tenant_id = current_tenant AND (organization_id IS NULL OR organization_id = current_organization)`——**`NULL` 组织被策略显式放行**。

**含义**：
- 分支侧「14 表复合外键」不存在 MATCH SIMPLE 漏网问题，因为三列全 NOT NULL，任一列 `NULL` 的路径在列级就被 `23502` 挡掉（G1 实证）。
- 主支侧反过来：`organization_id IS NULL` 是**可达状态**，且被 RLS 当作「不属于任何组织、对所有同租户可见」。合并时若把主支的 NULL 组织行迁入分支的 NOT NULL 列，会在迁移中途 `23502` 失败——这不是脏数据，是**两侧可空性契约相反**（已在 14 表归属矩阵中记为前置冲突）。
- 分支的 NOT NULL 是更强的约束，方向正确；冲突解决方向应是「收敛到分支」，而不是把分支放宽。

### 3.6 追加型守卫拦不住 TRUNCATE

阶段重置用的是 `TRUNCATE asset_versions CASCADE`——它**不触发行级触发器**，因此 `trg_asset_versions_guard`（禁止 DELETE、禁止改写内容列）完全不生效。

**含义**：`asset_versions` 的「追加型不可变」只在 DML 层成立，在 DDL/运维权限层不成立。表属主或超级用户可一键清空。这属于权限面而非约束面，但审计结论不应声称「资产不可删除」——准确表述是「**经 DML 不可删除**」。

---

## 4. 对补齐迁移的约束（设计输入，非迁移代码）

按实验证据可确定的顺序与硬约束：

1. **第 1 步必须建目标键**：`organizations` 表 → `uq_projects_entity_tenant` → `uq_projects_entity_tenant_org` → `projects→organizations` 外键。缺任一，外键语句必 `42830`。
2. **归属外键一律三列**：`(project_id, tenant_id, organization_id)`。两列方案已被 E2/E4 证明有组织盲区。
3. **外键分两段上**：先 `NOT VALID`（不锁历史、不拒合法新写入），清脏后再 `VALIDATE`。C4/C5/C7/C12 给出完整生命周期。
4. **脏数据治理必须是独立、可中止、可重跑的阶段**，且必须显式成对 `DISABLE/ENABLE TRIGGER`（顺序：`DISABLE guard` → `UPDATE` → `ENABLE guard` → `VALIDATE`）。任一步失败不得把守卫留在关闭态。
5. **哨兵行需要人工决策清单**：先 `SELECT ... WHERE project_id='legacy-unassigned'` 出清单，逐行决定映射目标或标记 `SUPERSEDED`，不做自动映射。
6. **可空性冲突先裁决**：主支 `organization_id` 可空 vs 分支 NOT NULL，必须在迁移里显式处理 NULL 行（补组织 or 拒绝），不能靠 `NOT VALID` 掩盖——`23502` 不受 `NOT VALID` 影响。
7. **不假设「约束建好即闭环」**：R11 要求不变量放在最可靠层。E2 已证明「数据库约束存在」与「不变量成立」不是一回事，验收标准必须写**拦截用例**（如 E1 那行必须被拒），而不是写「约束已创建」。

---

## 5. 诚实边界：本轮未验证、不予结论

| 未覆盖项 | 原因 | 影响 |
| --- | --- | --- |
| 14 张表逐表验证 | 只以 `asset_versions` 为代表性表跑通全链路 | 其余 13 表结构同构但未逐表实证；结论不能外推为「14 表均已验证」 |
| 并发写入下 `NOT VALID` / `VALIDATE` 的锁行为 | 单会话串行实验 | `VALIDATE` 的锁级别与耗时未知，大表上线窗口需另测 |
| `ON DELETE` 行为（建约束后删项目） | 未设计步骤 | 级联/拒绝语义未定，主支侧 `episodes` 已有引用关系，需补 |
| 生产脏行数量与分布 | 生产状态「未知」（既定裁决） | 无法给出治理工时与窗口长度 |
| 修复路径 ①/② 的选择 | 需裁决，非实验可定 | 直接决定迁移形态 |
| `TRUNCATE` 权限收紧方案 | 属权限治理，非本实验范围 | 「经 DML 不可删除」的表述边界需在验收标准中固化 |

---

## 6. 与既有结论的一致性检查

| 既有结论 | 本次是否一致 | 说明 |
| --- | --- | --- |
| 分支 `asset_versions` **无任何外键** | 一致 | A0 的 `42830` 与模板态 `\d` 均无外键 |
| 分支无 `organizations` 表 | 一致 | 全库 `table_name='organizations'` 查询为空，A1 需手工建 |
| 分支 `projects.organization_id` NOT NULL、主支可空 | 一致 | 14 表归属矩阵的前置冲突，本次在运行时复证 |
| 触发器 `asset_versions_project_assigned` 只拦哨兵、不校验归属 | 一致并强化 | 原判「部分缓解非归属约束」；本次给出正反两证（F1 拦、F2 放） |
| 主支 11 项独有含「组织 RLS + FORCE 收敛」 | 一致并补充 | 补充：该策略把 `organization_id IS NULL` 显式放行，是收敛未完成的残留口 |
| Phase 1 未验收 | 一致 | 本次不改变该判定 |

---

## 7. 产物

| 文件 | 内容 |
| --- | --- |
| `docs/audits/mig-recon/t3_dirty_state.py` | 驱动脚本（49 步，参数化单语句，逐语句捕获 SQLSTATE） |
| `docs/audits/mig-recon/t3-dirty-state.md` | 机器生成的原始结果（含逐步 SQL、行数、行哈希、约束/触发器状态） |
| `docs/audits/mig-recon/t3-dirty-state.json` | 同上结构化版本，供后续引用 |
| `docs/audits/2026-11-20-t3-dirty-state-findings.md` | 本文件：结论与设计约束 |

四容器（15441/15442/15443/15444）保持运行，未销毁；实验仅在 `t3_lab` 克隆库内进行，模板库与两分支 head 库未被修改。
