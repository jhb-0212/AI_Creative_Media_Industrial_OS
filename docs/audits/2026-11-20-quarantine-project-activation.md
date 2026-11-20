# D-10 落地执行报告：三层激活 · 隔离项目 · 逐行订正 · 14 表闭环

> 执行时间：2026-11-20
> 目标库：`localhost:15444/ai_film_industrial`（分支链 head，**非生产**）
> 起始版本：`c4e5f6a7b8d9` → 终止版本：`e3f4a5b6c7d8`
> 授权来源：所有者本轮会话明确授权「激活 a9b8+b0c9、建隔离项目与 RLS、逐行映射与 VALIDATE」

---

## 一、结论先行

| 项 | 结果 |
| --- | --- |
| 迁移激活 | **3 层 + 2 层补充**，共 5 个迁移上链并提交（`4278971` / `256ce6e` / `2c1037c`） |
| 隔离项目 | 按组织真实物化 **1 个**（`quarantine-…0011`，`ARCHIVED`） |
| 存量订正 | **5 行**，全部 `UPDATE 1` 且回读一致；映射表 5 条全部盖章 |
| 14 表台账 | **14/14 PASS**，`convalidated` 全为 `t`，违规行数全为 0 |
| RLS 行为验收 | **10/10 PASS**（业务不可见/不可写、审计可见/可写、盖章后例外失效） |
| 阶段 3 | **仍不放行**——本轮做的是数据与约束落地，不是放行评审 |

**一句话**：闭环达成了，但它是在**合成存量数据**上达成的；真实生产数据一行都没动过，
`15232`/`ai_film_verify`/模板库全部未触碰。

---

## 二、为什么必须再造一遍脏数据

第一次激活时库是空的（所有业务表 0 行）。空库上 `VALIDATE` 会**平凡通过**，
台账立刻 14/14——但这只证明「约束语法正确」，完全不证明「存量订正链路可用」。
那样的 14/14 是恒真指标。

所以执行路径改为真实时序：

```
downgrade → c4e5f6a7b8d9
   ↓ 造存量脏数据（4 类脏态 + 干净对照）
upgrade a9b8（NOT VALID 外键，不扫存量 → 成功）
   ↓
upgrade b0c9（守卫接入例外）
   ↓
upgrade c1d2（隔离项目 + RLS）
   ↓
先证明 VALIDATE **会失败**（4 张表 23503，10 张通过）
   ↓
逐行映射 + 受控例外订正（5 行）
   ↓
VALIDATE 全部 14 条 → 通过
   ↓
台账 14/14
```

关键在「先证明会失败」这一步：如果跳过它，后面的 14/14 无法排除「判定恒真」。

### 造出的四类脏态

| 场景 | 表 | 脏点 | 订正规则 | 结果 |
| --- | --- | --- | --- | --- |
| 哨兵 | `asset_versions` / `qc_reports` | `project_id='legacy-unassigned'` | R2 → 隔离项目 | ✅ 2 行 |
| 幽灵 | `gate_decisions` | `project_id='ghost-project'` | R2 → 隔离项目 | ✅ 1 行 |
| 组织错配 | `generation_tasks` | 项目属 org-1，行写 org-2 | R1 → 以项目真实 org 为准 | ✅ 1 行 |
| 组织级孤儿 | `production_routes` | `organization_id=org-2`（组织不存在） | R3 → 唯一真实组织 | ✅ 1 行 |
| 干净对照 | `asset_versions` | 归属正确 | 不动 | ✅ 未误伤 |

> 造数脚本 `docs/audits/mig-recon/seed_legacy_dirty.py`。
> 哨兵两行走 `session_replication_role=replica` 插入——因为库里另一层
> `assert_*_project_assigned` 断言（**只覆盖 2 张表**）在基线就拒绝 `legacy-unassigned`。
> 那是模拟「断言建立之前就存在的历史行」，不是绕过不可变守卫（守卫只拦 UPDATE/DELETE）。

---

## 三、五个实测缺口（本轮最有价值的部分）

这五条都不是推理出来的，是**执行时被数据库打回来的**。它们的共同特征是：
静态检查全绿、验证脚本也全绿，但真实路径走不通。

### 缺口 1：迁移无法被 Alembic 加载（`@dataclass`）

- **现象**：`alembic heads` 抛 `AttributeError: 'NoneType' object has no attribute '__dict__'`。
- **根因**：`a9b8` 用 `@dataclass(frozen=True)` 定义 `Step`。Alembic 用
  `importlib.util.module_from_spec` 加载迁移，**不先登记 `sys.modules`**；
  dataclass 解析注解时回查 `sys.modules[cls.__module__]` 得到 `None`。
- **为什么之前没发现**：`verify_on_clone.py` 手动补了 `sys.modules[name] = mod`，
  绕过该问题后跑出 13/13。**「验证脚本能加载」掩盖了「alembic 读不进来」。**
- **修法**：`Step` 改 `NamedTuple`（构造与属性访问不变，不触发回查）。
- **防止复发**：迁移的加载路径必须用 `alembic heads`/`upgrade` 验证，不能只用自建脚本。

### 缺口 2：受控例外没有任何主体能用（零表权限）

- **现象**：`UPDATE` 直接 `42501 permission denied`。
- **根因**：`b0c9` 建了 `afs_data_repair` 角色、映射表、判定函数，
  但 `information_schema.role_table_grants` 里该角色**零行**。
- **修法**：`d2e3` 授 `SELECT, UPDATE`（不含 `INSERT`/`DELETE`：订正不造行不删行）。
  `SELECT` 不可省——`UPDATE ... WHERE` 的 WHERE 列需要它（D-10.2.3 陷阱 c）。
- **防止复发**：授权与机制分开迁移；`R0` 前置断言缺表/缺角色直接中止。

### 缺口 3：`UPDATE 0` 冒充成功（RLS 先于守卫）

- **现象**：影响 0 行，**不抛异常**。
- **根因**：`project_scope_*` 是 **`RESTRICTIVE`**（不是 PERMISSIVE），
  要求 `app.current_org_id` 与 `app.current_projects`；修复会话没有这两个上下文。
- **修法**：`d2e3` 加 `repair_row_scope`（PERMISSIVE），条件绑映射表
  「工单令牌 + 未盖章」。例外范围精确到**行**。
- **为什么不用 `BYPASSRLS`**：那会把爆炸半径从「工单列出的行」放大到「全库所有租户」，
  与 D-10.2.1 的最小豁免面直接冲突。
- **防止复发**：所有写入用例断言 `影响行数 = 预期` **且回读**，`UPDATE 0` 判失败。

### 缺口 4：跨组织订正数学上不可能（RESTRICTIVE 的双向夹逼）

- **现象**：缺口 3 修完后，组织错配那一行仍无法订正。
- **根因**：`project_scope_*` 是 RESTRICTIVE。把 `organization_id` 从 org-2 改到 org-1：
  - `current_org_id=org-2` → USING 通过，WITH CHECK 要求新行仍是 org-2 → **拒**；
  - `current_org_id=org-1` → WITH CHECK 通过，USING 看不见该行 → **`UPDATE 0`**。
  两条路都堵死，不是参数没调对。
- **修法**：`e3f4` 给 19 条 scope 策略追加 `repair_row_allowed()` 分支
  （INVOKER + PL/pgSQL 显式短路 + 判据仍是映射表）。
- **侵入性**：**修改了分支既有的 19 条策略**——这是本轮唯一对既有 RLS 设计的侵入，
  原条件逐字保留，只在末尾追加 OR 分支。

### 缺口 5：降级无法还原（正则做不了括号平衡）

- **现象**：`降级失败：project_scope_projects 的表达式未能还原`。
- **根因**：PostgreSQL 把 `entity_id::text` 规范化成 `(entity_id)::text`，
  且整体多包一层括号。用正则剥掉追加分支无法处理括号平衡。
- **修法**：`e3f4` 升 `rls_policy_backup` 表，**升级时备份原表达式**，降级逐字还原；
  还原后断言「不应残留任何含旁路分支的策略」。
- **附带教训**：新增迁移必须实测降级，不能只测升级。

### 另有一处顺序依赖的授权缺口（并入缺口 2 的迁移）

`ownership_repair_map_match` 缺 `EXECUTE`：`b0c9` 的 `E5` 只授了 `allows`，
`E6`「按真实表授权动态收敛」只遍历**已有表写权限**的角色——当时 `afs_data_repair`
还没有任何表权限，于是被跳过。结果是 `allows` 可执行、它内部调用的 `map_match` 不可执行。
这类缺口只有真正跑一次订正才会显形。

---

## 四、隔离项目落地情况（D-10.3 七条要求）

| # | 要求 | 落地 | 实测证据 |
| --- | --- | --- | --- |
| 1 | 真实存在的项目记录 | ✅ `entity_id='quarantine-'||org_id` | 1 行，`(project_id,tenant_id,org)` 三列外键 VALIDATE 通过 |
| 2 | 明确隔离属性与生命周期 | ✅ `status='ARCHIVED'`、`project_name='数据隔离项目（非业务）'`、`created_by='(system) ownership-migration'` | B4：改回 `ACTIVE` 被 `CheckViolationError` 拒，回读仍 `ARCHIVED` |
| 3 | 不被业务查询当成真实数据 | ✅ 18 条 RESTRICTIVE 策略 | A1：业务主体在 `current_projects='*'` 下仍返回 0 行 |
| 4 | 不开放给业务写入 | ✅ 写侧 RESTRICTIVE WITH CHECK | A3：`UPDATE 0` 且值未变；A4：`InsufficientPrivilegeError` |
| 5 | 保留原始标识与变更记录 | ✅ 映射表存 `old_*`/`new_*`/`reason`/`ticket_ref`/`applied_at` | 5 条映射，`old_project_id` 保留 `legacy-unassigned` 原值 |
| 6 | 验证外键 + RLS 行为 | ✅ 台账 14/14 + RLS 10/10 | 全部断言影响行数/返回行数，不以「无异常」为判据 |
| 7 | 迁移/删除走例外工单 | ✅ `WO-OWN-2026-1120-001` | 本轮不含删除、不含迁入真实项目 |

### 一处必须登记的偏差：`project_tier`

D-10.3 要求 2 写「`project_tier` 取专用值」。但冻结契约的 `ProjectTier` 枚举只有 `S/A/B`，
写入 `'Q'` 会让**项目读取接口在反序列化阶段抛错**（隔离项目在 API 层不可读），
且契约已冻结哈希。

本轮取 `project_tier='B'`，隔离语义由 `entity_id` 前缀 + `status='ARCHIVED'` + `project_name`
三个可查事实共同表达，判定收敛到 `is_quarantine_project()` 一个函数。

**需 ADR 下一版澄清**：「专用值」是指新增枚举值（须走契约修订），还是指可识别的固定组合。

---

## 五、台账结果与它的**盲区**（不可省略的限定）

```
整体判定：PASS（14/14 闭环）
```

但必须同时说明三件事，否则这个 14/14 会被误用：

### 5.1 只有 5 行涉及真实数据，9 行是空表

| 行 | 表 | 行数 | 实质含义 |
| --- | --- | --- | --- |
| 1 | `projects` | 3 | 2 真实 + 1 隔离 |
| 2 | `asset_versions` | 3 | 含哨兵与干净对照 |
| 3 | `gate_decisions` | 1 | 幽灵项目已订正 |
| 4 | `generation_tasks` | 1 | 组织错配已订正 |
| 5 | `production_routes` | 2 | 组织孤儿已订正 |
| 6–14 | 其余 9 张 | **0** | 空表上 `VALIDATE` 平凡通过 |

空表通过是**正确**结果，但它不构成「这张表的存量数据曾被修复过」的证据。

### 5.2 `qc_reports` 不在台账里，却确实被订正过

它有 `project_id`、有不可变守卫、被纳入受控例外，但 `a9b8` 的 `PROJECT_LEVEL`
未含它、`DERIVED_NO_FK` 也未列入——**它不在任何归属闭环内**。

后果：本轮的 `qc-1` 哨兵行**被订正了**（`legacy-unassigned` → 隔离项目，`UPDATE 1` + 回读一致），
**但它的订正不体现在 14/14 里**。也就是说：

> **台账 14/14 ≠ 全库没有脏归属。**

这是上一轮发现、本轮再次被实际数据证实的范围缺口，仍需所有者裁决
（A 纳入台账 / B 显式列入 `DERIVED_NO_FK` / C 登记为残余风险）。

### 5.3 台账的「违规行数=0」是逐表计算的

所以「13 张表都干净、只有 1 张脏」时它会精确报 13/14，不会整表白判。
这一点已由 `--selftest` 四态证明，不是本轮新结论。

---

## 六、RLS 行为验收（10/10）

| 用例 | 断言 | 结果 |
| --- | --- | --- |
| A1 | 业务查隔离项目（`current_projects='*'`）→ 0 行 | 0 行 |
| A2 | 业务查隔离区哨兵行 → 0 行 | 0 行 |
| A3 | 业务改哨兵行归属 → 影响 0 行**且回读未变** | 0 行 / 未变 |
| A4 | 业务向隔离项目新增 → 被 RLS 拒 | `InsufficientPrivilegeError` |
| B1 | 审计（`include_quarantine='on'`）查隔离项目 → 1 行 | 1 行 |
| B2 | 审计查哨兵行 → 1 行 | 1 行 |
| B3 | 审计改隔离项目 → **影响 1 行** | 1 行 |
| B4 | 改回 `ACTIVE` → 被 CHECK 拒，回读仍 `ARCHIVED` | `CheckViolationError` / `ARCHIVED` |
| C1 | 工单盖章后 repair 看不见该行 | 0 行（映射 5 条均已盖章） |
| D1 | 干净对照对业务仍可见 | 1 行 |

C1 是这套机制最容易被忽略的性质：映射行 `applied_at` 一旦非空，
**可见性与可改性同时失效**。不测它，就无法区分「例外按需生效」与「例外永久开着」。

---

## 七、边界与未做的事

| 事项 | 状态 | 说明 |
| --- | --- | --- |
| 生产库 `15432` | **未触碰** | 本轮只在 `15444` 分支链克隆库执行 |
| `ai_film_verify` / 模板库 | **未触碰** | — |
| 真实业务数据订正 | **未发生** | 库内数据全部为合成；生产存量修复需单独授权与维护窗口 |
| 阶段 3 放行 | **未放行** | 本轮是数据与约束落地，不是放行评审 |
| `qc_reports` 纳入台账 | **待裁决** | 见 5.2，A/B/C 三选一 |
| ADR 状态更新 | **未做** | v1.4.6 仍写「14 表闭环 0/14」，与当前 14/14 不一致，需下一版更正 |
| 四容器（15441–15444） | **保持运行** | 按所有者要求 |

---

## 八、产物清单

**迁移（`data/migrations/pending/feat-ownership/`，同时已入 integration 分支 `versions/`）**

| 文件 | 职责 | 提交 |
| --- | --- | --- |
| `a9b8c7d6e5f4_ownership_fk_backfill.py` | 4 目标键 + 14 条归属外键（`NOT VALID`） | `4278971` |
| `b0c9d8e7f6a5_ownership_repair_exception.py` | 13 表守卫接入受控例外 | `4278971` |
| `c1d2e3f4a5b7_quarantine_project_rls.py` | 隔离项目 + 18 条 RESTRICTIVE 策略 + CHECK + 审计角色 | `256ce6e` |
| `d2e3f4a5b6c7_repair_role_grants.py` | 表权限 + RLS 行级可见性 + 函数 EXECUTE | `2c1037c` |
| `e3f4a5b6c7d8_repair_rls_bypass.py` | 19 条 scope 策略的 repair 旁路 + 原表达式备份 | `2c1037c` |

**脚本（`docs/audits/mig-recon/`）**

| 文件 | 用途 |
| --- | --- |
| `seed_legacy_dirty.py` | 在**外键建立之前**造存量脏数据（4 类脏态 + 对照） |
| `repair_ownership.py` | 探测 → R1/R2/R3 判定 → 受控例外订正 → 断言 + 回读 |
| `rls_acceptance.py` | D-10.3 要求 6 的 RLS 行为验收（10 用例） |
| `acceptance_ledger.py` | 14 行台账（本轮实采 14/14） |
| `lab.py` / `guard_fixtures.py` | 克隆库实验装置与 13 表声明式夹具 |

**报告**

- 本文件：`docs/audits/2026-11-20-quarantine-project-activation.md`
- 例外工单：`docs/audits/2026-11-20-workorder-WO-OWN-2026-1120-001.md`
- 台账：`docs/audits/mig-recon/acceptance-ledger.{md,json}`

**备份**

- `backups/pre-activate-15444-20261120-143254.dump`（基线）
- `backups/post-activate-3layer-20261120-143647.dump`（三层激活后）

---

## 九、复现方式

```bash
# 1) 造存量脏数据（须先 downgrade 到 c4e5f6a7b8d9）
python docs/audits/mig-recon/seed_legacy_dirty.py --port 15444

# 2) 升级五层（在 integration 分支 worktree 内）
cd <worktree> && POSTGRES_PORT=15444 POSTGRES_PASSWORD=... \
  python -m alembic upgrade head

# 3) 证明 VALIDATE 此刻会失败
#    （对 4 张脏表应 23503，其余应成功）

# 4) 逐行订正
python docs/audits/mig-recon/repair_ownership.py --port 15444 --plan   # 先看判定
python docs/audits/mig-recon/repair_ownership.py --port 15444          # 执行

# 5) 逐条 VALIDATE

# 6) 出账与 RLS 验收
python docs/audits/mig-recon/acceptance_ledger.py --port 15444
python docs/audits/mig-recon/rls_acceptance.py --port 15444
```
