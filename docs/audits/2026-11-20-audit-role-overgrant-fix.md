# 审计角色过度授权与名单式授权的根因修复

- 日期：2026-11-20
- 触发：`scripts/contract_check.py` 报 `[FAIL] CRED-3 ... b0c9: 字符串字面量含默认角色名 'app_role'`
- 结论：一个契约违规牵出**两条**真实缺陷——一条安全（审计角色可改业务数据）、一条功能（12 张表的写权限是假的）。均已根治，并新增一条不变量断言把这类缺陷变成可执行检查。

---

## 一、为什么会发现

上一轮完成归属栈激活与批次二后，只跑了 `verify_exception` / `verify_guard_coverage` / `acceptance_ledger` / `rls_acceptance` 四套装置，**没有跑 `contract_check.py`**。这次补跑，第 12 项授权 Registry 通过，但既有闸门里冒出一条：

```
[FAIL] CRED-3 断言未通过（db.role_names_from_config）:
       data/migrations/pending/feat-ownership/b0c9d8e7f6a5_ownership_repair_exception.py:
       字符串字面量含默认角色名 'app_role'
```

`CRED-3` 的判据在 `tests/unit/test_db_role_names.py`：角色名是配置（`POSTGRES_APP_USER` 等），
写死进代码后，部署方改名会得到「建出没人用的角色」或「GRANT 目标不存在导致升级失败」，
两种都不会被读代码发现。

**流程缺口**：归属栈属于「迁移 + 授权」双面改动，验收清单缺了契约校验这一项。
四套装置都在验「迁移行为」，没有一套验「工程规约」。已在 README 补清单（见 §六）。

---

## 二、缺陷 1：名单式授权，三个坑叠在一起

`b0c9` 的 E5 原文：

```python
for role in ("app_role", "afs_data_repair"):
    ... IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
          GRANT USAGE ON SCHEMA project_planning TO ...;
          GRANT EXECUTE ON FUNCTION ...ownership_repair_allows... TO ...;
```

注释写的是「守卫对一切写者生效，故须显式授予所有可能写入这些表的主体」，
并声称「未列出的角色写入时会得到 42501 —— 这是**有意的**」。

实测这条名单**在本环境从未生效过**，且三个坑互不重叠：

| # | 坑 | 事实 |
| --- | --- | --- |
| 1 | 角色名不存在 | 库里没有 `app_role`；真实角色是 `svc_asset` / `svc_project_planning` / `svc_qc_governance` / `svc_shot_planning` / `svc_workflow` / `svc_generation_execution` / `svc_identity` / `svc_outbox_relay` 八个域服务角色。`IF EXISTS` 把它**静默跳过**——注释里的「已显式授权」是错觉 |
| 2 | 只授一半 | 名单只授 `ownership_repair_allows`，没授它内部调用的 `ownership_repair_map_match`。函数级 EXECUTE 不传递，守卫走到 `map_match` 仍 42501 |
| 3 | 时序错位 | `afs_data_repair` 由 `d2e3`（更晚）创建并授表权限，E5 执行时 `IF EXISTS` 看不到它，同样静默跳过 |

而注释里那句「未列出的角色得到 42501 是有意的」，被紧随其后的 E6 **直接否定**：

```
# E6 按实际表授权收敛，消除「保护语义漂移」：
#   硬编码角色名无法穷举，因此改为「谁有权写这些表，谁就有权调用判定函数」。
```

两段注释互相矛盾——这就是「补丁叠在补丁上」留下的痕迹：E6 是为取代 E5 而写的，
但 E5 没被删除，于是**同一件事有两个事实源，其中一个还是错的**。

### 修法

删除 E5 的角色循环，`REVOKE PUBLIC` 保留，授权收敛为两条**由事实驱动**的路径：

- **E6**：任何对受守卫表持 `UPDATE/INSERT/DELETE` 的主体，升级时即刻获得（穷举的替代）；
- **d2e3**：`afs_data_repair` 在**获得表权限之后**显式补授两个函数，时序名实相符。

判据是「它真的能写这些表」，不是「它在名单里」。

---

## 三、缺陷 2：把「隔离项目可写」扩散成「18 张表可写」

`d2e3` 的 R3 原文：

```python
for schema, table in AUDIT_TARGETS:          # 18 张表
    GRANT SELECT, UPDATE ON TABLE ... TO afs_audit
# 理由：「D-10.3 要求 6 的验收需要『审计主体可见/可写』这一侧；只授 SELECT 无法构造『可写』用例」
```

对回 ADR 原文（`ADR-R40-unified-authorization-model.v1.4.7.md` §D-10.3）：

- **要求 4**：「**不得作为普通业务项目开放给业务写入**」→ `WITH CHECK`：仅审计主体可写；
- **要求 6**：「业务主体不可见/不可写、审计主体可见/**可写**」。

两条讲的都是**隔离项目**，而隔离项目是 `project_planning.projects` 里的一行。
「可写」的范围是**这一张表**，不是「审计能看见的每一张表」。

而验收用例本身也只用了两张表：B1/B3/B4 全部在 `projects`，B2 在 `asset_versions` 但**只读**。
也就是说 18 张表里的 17 张 UPDATE **没有任何用例使用**——授权范围比需求大了 17 张表。

### 这件事的真实代价

不只是「权限给多了」，它造成了一个**当前就存在的功能故障**：

R3 在 `d2e3` 执行，而函数 EXECUTE 由 `b0c9` 的 E6 授予（更早）。
`afs_audit` 在 E6 跑的时候还没有表权限，于是被跳过；到 R3 拿到 UPDATE 时，E6 已经跑完了。

结果：**`afs_audit` 对 12 张受守卫表有 UPDATE 权限，却没有判定函数的 EXECUTE**。
它真去写，守卫调 `ownership_repair_allows()` 时直接 `42501`。这是「有钥匙没门」——

- 「权限在」是假象：`information_schema` 里看得见 UPDATE；
- 「功能不可用」是事实：写入必然失败，且失败码是 `42501` 而非 `P0001`，
  运维会以为是权限配置问题，实际是守卫通道没接通。

另外 6 张非守卫表（`projects` / `episodes` / `script_revisions` / `shot_drafts` /
`shot_revisions` / `approval_tickets`）的 UPDATE 是**真能用**的——审计身份可以改业务数据。
这与「审计只读」的语义、与项目红线「审计不可篡改」直接冲突。

### 修法

R3 按 `AUDIT_WRITABLE` 常量区分，把需求边界显式写出来：

| 表 | 权限 | 依据 |
| --- | --- | --- |
| `project_planning.projects` | `SELECT, UPDATE` | D-10.3 要求 4/6 的唯一落点（隔离项目是这张表里的一行） |
| 其余 17 张 | `SELECT` | 审计「能看见」；要求 3/4 的业务侧不可见由 RESTRICTIVE 策略保证，与审计写权限无关 |

`projects` 不在 13 张受守卫表内，因此它的 UPDATE 不经过 `ownership_repair_allows`，
不存在 `42501` 风险——这正是把「可写」限定在这一张表上的另一个好处。

---

## 四、新装置：把「授权漂移」变成不变量

两条缺陷都不是读代码能发现的：一个依赖 `pg_roles` 的运行时内容，一个依赖**执行顺序**。
所以新增 `docs/audits/mig-recon/verify_grants.py`，在克隆库里把六层重放一遍，
再用 `has_function_privilege` 从系统目录**实读**，断言一条不变量而不是某几个角色的清单：

> 对任一受守卫表持有 `UPDATE/INSERT/DELETE` 的主体，必须同时持有两个判定函数的 `EXECUTE`。

违反它的后果不是「少个 GRANT」，而是**保护语义漂移**：同一个非法归属改写，
升级前 `P0001`（守卫拒绝），升级后 `42501`（权限拒绝），按 `P0001` 写的处理逻辑静默失效。

### 证据（11/11）

| # | 用例 | 结果 |
| --- | --- | --- |
| G0 | 回退归属栈至激活前基线（6 层，7 项断言归零） | PASS |
| G1 | 两个判定函数已创建（b0c9 之后） | PASS |
| G2 | 中间态写者均已持有函数 EXECUTE | PASS |
| G3 | 归属栈六层全部应用 | PASS |
| **G4** | **不变量：37 个 角色×表，零漂移** | **PASS**（修前 49 个 / 12 条漂移） |
| G4b | 写者矩阵非空（否则 G4 平凡通过） | PASS（7 个角色） |
| G5 | `afs_data_repair` 对两函数均有 EXECUTE（11 张表） | PASS |
| G6 | PUBLIC 无两函数 EXECUTE（撤 PUBLIC 生效） | PASS |
| G7 | 只授表权限的新角色**不会**自动获得函数 EXECUTE | PASS（非恒真证明） |
| G8 | 执行 E6 同款语句后获得 EXECUTE | PASS（缺口可修） |
| G10 | 六层逆序降级后函数已移除（无悬空 ACL） | PASS |

G7 是非恒真的关键：若 G4 全绿只是 PG 默认行为，G7 会失败。两条必须一起看。

### 已知边界（G9，如实登记，不并入「已修」）

**E6 是升级时的一次性收敛，不是持续授权。** 升级后新建的写角色需重跑收敛语句才能获得
EXECUTE，否则其写入得到 `42501`（G7 已实证该缺口存在，G8 证明修复语句有效）。

这是「授权随权限变化」的固有代价，名单式授权消除不了它——名单只是把缺口换成了**静默跳过**，
从「看得见的失败」退化成「看不见的缺失」。当前库内无累积漂移，但新增写角色时必须重跑。

---

## 五、目标库对齐（本次为手工动作，必须留痕）

两处修复改了已应用的迁移源码（`b0c9` / `d2e3`）。这两层只在 `localhost:15444/ai_film_industrial`
执行过，**未进入生产、未发布**，故按「未发布迁移允许源码修正」处理。
但目标库不会因此自动变化，需一次等价对齐——方向是**收紧**，不影响任何现存功能
（G4 已证明被收走的 12 张表 UPDATE 本来就不可用）。

```sql
DO $do$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY[
    'project_planning.episodes','project_planning.script_revisions',
    'generation_execution.generation_tasks','generation_execution.shot_route_selections',
    'shot_planning.shot_drafts','shot_planning.shot_revisions','shot_planning.shot_specs',
    'shot_planning.shot_visual_plans','shot_planning.constraint_packs',
    'asset_service.asset_versions','asset_service.delivery_manifests','asset_service.qc_reports',
    'qc_governance.gate_decisions','qc_governance.qc_review_sessions',
    'workflow.approval_flows','workflow.approval_tickets','workflow.approval_steps'
  ] LOOP
    EXECUTE format('REVOKE UPDATE ON TABLE %s FROM afs_audit', t);
  END LOOP;
END $do$;
```

对齐后核对（`afs_audit` 的非 `SELECT` 权限应只剩一行）：

```
            tbl            | privilege_type
---------------------------+----------------
 project_planning.projects | UPDATE
```

**这条手工动作是当前唯一「源码与库」的差异点。** 新建库跑迁移会直接得到正确状态；
已建的库靠上面这段 SQL 对齐。未做进迁移是因为「收紧一条从未生效的授权」若写成新迁移，
会与 `d2e3` 的正确版本冲突（同一事实两个源），反而制造新的漂移面。

---

## 六、验收清单（本轮补上，防止再漏）

归属栈任何改动后，六项全绿才算完成：

| 项 | 命令 | 本轮结果 |
| --- | --- | --- |
| 工程规约 | `.venv/Scripts/python.exe scripts/contract_check.py` | **PASS**（CRED-3 已修） |
| 契约单测 | `pytest tests/unit/test_db_role_names.py tests/unit/test_authorization_registry.py` | PASS |
| 例外行为 | `verify_exception.py --migration <b0c9>` | 37/37 |
| 逐表覆盖 | `verify_guard_coverage.py --guard/--fk/--batch2` | 70/70（N/A 0、缺口 0） |
| **授权完整性** | `verify_grants.py --versions-dir <versions>` | **11/11**（零漂移） |
| 闭环台账 | `acceptance_ledger.py --db … --validate` + `--selftest` | 17/17，自检 4/4 |
| RLS 行为 | `rls_acceptance.py` | 10/10 |

前两项此前不在清单内，是本次缺陷漏检的直接原因。

---

## 七、本轮边界（不假装解决）

- **I-1 的「一致」半边仍未成立**：三列外键只保「目标真实且组织一致」，
  不保「本行 `project_id` 与父行相同」。需复合外键指向父行或触发器，属下一轮。
- **E6 一次性收敛的边界**（§四 G9）是设计取舍，不是待修缺陷，但必须在使用侧知晓。
- `afs_audit` 的 `SELECT` 范围仍是 18 张表。这是「审计能看见隔离区哨兵行」的要求 3 所需，
  本轮不动；但「审计到底该看见哪些表」尚未逐表论证，属范围问题而非缺陷。
- 阶段 3（身份治理放行）**仍不放行**。
