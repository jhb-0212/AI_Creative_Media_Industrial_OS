# 归属闭环的「覆盖完整性」：`episodes` 缺口与覆盖闸门

- 日期：2026-11-20
- 范围：`project_planning.episodes` 的归属外键；`docs/audits/mig-recon/verify_ownership_closure.py`
- 前置：归属栈六层已在 `localhost:15444/ai_film_industrial` 执行到 `f8a9b0c1d2e3`，台账 17/17
- 结论：补第七层 `g7b8c9d0e1f2`，台账 → **18/18**；新增覆盖闸门，**8/8**，双向自检通过

---

## 一、事实：一张「例外通道建好了，约束却没建」的表

`project_planning.episodes` 有 `project_id`，但**没有任何归属外键**。而它并不是被遗忘
在角落里的一张表——恰恰相反，整套修数机制都是围着它建的：

| 层 | 对 `episodes` 做了什么 | 缺什么 |
| --- | --- | --- |
| `c1d2` | 建 `project_scope_episodes`（RESTRICTIVE）× 1 | — |
| `d2e3` | `KEY_COLS` 收录，授 `afs_data_repair` 对该表的 `SELECT, UPDATE` | — |
| `e3f4` | `SCOPED_POLICIES` 收录，装上 RLS 旁路 | — |
| 归属约束 | **无** | `fk_episodes_project_tenant_org` |

也就是说：「它的 `project_id` 可能会指错、需要订正」这个前提被完整实现了——权限有了、
旁路有了、行键映射有了——**唯独「什么算指错」从未被定义为约束**。于是修数角色可以把
它改到任意字符串，数据库一声不响。

实测（`verify_batch3.py` 的 `C1`，在回退到 `f8a9` 的克隆库上）：

```
INSERT INTO project_planning.episodes (..., project_id, ...)
VALUES (..., 'ghost-project', ...);   -- INSERT 0 1
```

`ghost-project` 在 `projects` 里不存在，插入成功。

### 与 `qc_reports` 是同一类，不是同一个错误

`qc_reports`（上一轮）既不在 `a9b8.PROJECT_LEVEL`，也不在 `a9b8.DERIVED_NO_FK`。
`episodes` 同样两者都不在。两次都不是「判断错了该不该建」，而是**从未进入判断**：
建表、建 RLS、建守卫、装例外这四轮全部跳过它，台账 17 行也从不包含它。

一个既不在「做了」也不在「不做，理由如下」清单里的对象，不会举手。所以这一轮真正
该修的不是这张表，而是**「清单是否完整」这件事没有判据**。

---

## 二、判据：集合相等，且豁免必须可机检

`verify_ownership_closure.py` 把覆盖完整性变成两个集合的相等：

```
含 project_id 的表          ==  台账 A 类行 ∪ 豁免
含 organization_id 且无 project_id 的表  ==  台账 B/ROOT 行 ∪ 豁免
```

三条设计约束：

1. **台账一侧从 `acceptance_ledger.ledger_rows()` 取，不另写一份**。否则闸门自己
   就成了第二个事实源，而「一个事实一个权威源」（R1）正是它要守的规则。
2. **目录一侧从 `pg_attribute` 实读，不读迁移常量**。常量是「我以为有哪些表」，
   目录是「实际有哪些表」；两者不一致时以实际为准，并让人看见差异。
3. **豁免不是免费通行证**。每条豁免必须携带一段**返回 bool 的 SQL**，前提失效即转红：

| 豁免 | 类型 | 理由 | 机检前提 |
| --- | --- | --- | --- |
| `script_revisions` | derived | project 归属由 `(project_id, episode_ref) → episodes` 复合外键强制，父行一致则本行一致 | 该复合外键存在 ∧ 列序为 `(project_id, episode_ref)` ∧ `convalidated` |
| `organizations` | root | 归属链终点，唯一上一级是 `projects → organizations` | `projects → organizations` 外键存在且已验证 |

`script_revisions` 这条值得单独说：**它不是「不建」，而是「用父行的约束代替自己的」**。
再给它挂一条三列外键会让同一事实出现第二个可被违反的入口。但这个替代关系此前只存在于
口头——现在它是一段会在父行约束被删时自动变红的 SQL。

### 闸门的非恒真证明（`--selftest`）

第一版自检写成「基线库上应当报出 ≥10 张未覆盖」，跑出来只有 1 张。原因是 **C1 判的是
「表在不在清单里」，与约束存不存在无关**——我用 A 的失败去证明 B 有效，前提就错了。
改成两侧各证一次，每侧构造一个「必须变红」的输入：

| 用例 | 构造 | 必须的结果 |
| --- | --- | --- |
| S0 | 清行 + 逆序回退七层 | 七项断言全归零 |
| S1 | 在克隆库建 `public.zz_synthetic_owned(project_id)` | C1 把它报为未覆盖（证明目录侧真读 `pg_attribute`） |
| S2 | 同一基线库上查台账 17 行的约束 | C7 报出全部 17 条缺失（证明台账侧真读 `pg_constraint`） |

两条都红，闸门才是真的在比对两个独立来源。

---

## 三、修法：第七层迁移 `g7b8c9d0e1f2`

`project_planning.episodes` 挂 `(project_id, tenant_id, organization_id) → projects
(entity_id, tenant_id, organization_id)`，命名沿用 `fk_<table>_project_tenant_org`，
`NOT VALID`，与前三批同口径。

**单列一批而不并入 `f8a9`** 的理由：`f8a9` 已应用，改它等于改历史（与 `a9b8` 同一原则）。
批次是「已发生的事实」，新增约束只能以新的一层出现。

**对本表而言 I-1 的两半同时闭合**：父行就是 `projects` 本身，所以「目标真实」与
「与父行一致」等价。这一点与 `qc_reports`（父行是 `asset_versions`）不同，别外推。

台账 17 → 18 行。迁移跑完的那一刻台账是 **17/18**（`episodes` 为 `PENDING_VALIDATE`），
`--validate` 之后才 18/18。这个中间态被刻意保留并记录：若那一刻直接全绿，说明把
「约束已建」当成了「已闭环」——D-10.1 明令禁止的那件事。

---

## 四、顺带修掉的三处装置缺陷（同一根因）

`verify_batch2.py` 原本写着「直接克隆模板、不回退，因为模板恰好停在这个状态」，
并断言「模板上批次二的三条外键不存在」。`g7b8c9d0e1f2` 一应用，这个断言必然失败——
**它把「模板此刻恰好在哪」当成了前提**，与上一轮 `23503` 事故同一根因。

改为 `lab.rewind_to(keep)`：每批脚本显式声明「我的增量前提是 `keep` 这一层」，
由 `lab` 负责把其后各层撤掉。脚本表达的是**被测命题的前提**，不是**环境的偶然状态**。
`verify_batch2` 因此由 15 项变成 16 项（新增 `P0`，记录撤了哪几层）。

同时把 `lab.py`、`verify_grants.py` 里写死的「六层」全部去掉——层数每一批都在涨，
写进 docstring 就会在下一批变成假陈述（本轮已经从五层涨到七层两次）。层数以
`OWNERSHIP_STACK` 为准。

### 4.3 第三处硬编码清单：`repair_ownership.py` 的订正范围

查 `episodes` 波及面时发现，订正脚本的 `SCOPE` 是一张写死的 16 行清单，**不含
`episodes`**，而它的注释还写着「覆盖范围**大于**台账」——实际是**小于**。

后果是两个装置对同一事实给出相反结论：`episodes` 若有脏行，台账会因 `VALIDATE`
失败而变红，而 `repair_ownership.py --plan` 会报「没有可订正的行」。运维看到后者
只会认为台账在误报。

改为从 `acceptance_ledger.ledger_rows()` 派生范围，行键从 `pg_index` 主键实读：

- 范围与台账**不可能再分叉**（台账是闭环唯一判据，订正跟着它走）；
- 顺手消掉「12 张用 `entity_id`、`qc_reports` 用 `qc_report_id`」这类手工差异——
  实跑确认 `qc_report_id` 被自动取到；
- 复合主键**显式报错**而不是猜一个列名；
- `projects` 进 `SCOPE_EXCLUDED` 并写明理由：改一个项目的 `organization_id` 等于
  把它的全部资产搬家，属业务决策，不能由脚本按规则推导。

这三处（闭环清单、批测试基线、订正范围）病根是同一个：**清单不会自己举手**。
凡「有哪些表 / 有哪些层 / 有哪些行」这类集合，都必须从系统目录或唯一权威源派生，
不能写死。

---

## 五、验证证据

| 装置 | 结果 | 说明 |
| --- | --- | --- |
| `verify_ownership_closure.py` | **8/8** | C1 是本轮唯一由红转绿的用例（红时精确报出 `episodes`） |
| 同上 `--selftest` | **S0/S1/S2 全 PASS** | 两侧各被证伪一次 |
| `acceptance_ledger.py`（目标库） | **18/18** | `--validate` 前为 17/18，中间态已截图记录 |
| 同上 `--selftest`（四态） | **4/4 OK** | nomig 0/18、pending 0/18+18 待验证、clean 18/18、dirty 17/18 |
| `verify_batch3.py` | **15/15** | 含 G1 对照行与 N2「不误伤」——只报 23503 无法区分「拦住了」和「什么都写不进去」 |
| `verify_batch2.py` | **16/16** | 新增 P0；改用 `rewind_to` 后基线可复现 |
| `verify_exception.py` | **37/37** | B-1 现在回退 7 层，且「批次三归属外键=0」单列一项 |
| `verify_guard_coverage.py` | **70/70** | N/A 0、待裁决缺口 0（`episodes` 无守卫，不进该矩阵） |
| `verify_grants.py` | **11/11**（缺口 1） | 缺口为 G9：E6 一次性收敛，属已知边界 |
| `rls_acceptance.py` | **10/10** | 未受影响 |
| `repair_ownership.py --plan` | **0 行待订正** | 范围改从台账派生后覆盖 **17 张表**（含 `episodes`），`qc_reports` 行键自动取到 |
| `scripts/contract_check.py` | **PASS** | 23 条不变量 / 8 域 7 资源 16 策略 / 提案状态 |

目标库 `15444/ai_film_industrial`：`alembic_version = g7b8c9d0e1f2 (head)`；四个容器与
`15432` 全程未触碰。

---

## 六、仍未解决（不许当成已解决）

1. **I-1 的「一致」半边，除 `episodes` 外仍未闭合**。三列外键只保「目标真实 + 租户组织
   一致」，不保本行 `project_id` 与父行相同。`qc_reports`（父 `asset_versions`）、
   `shot_specs`（父 `shot_revision`）、`approval_steps`（父 `ticket`）都属这一类。
   需要复合外键指向父行 `(id, project_id, tenant_id, organization_id)`，或等价的触发器。
   前置条件：父表要有 `(id, project_id, tenant_id, organization_id)` 唯一键——多数还没有。
2. **E6 的一次性收敛**（`verify_grants` G9）：升级后新建的写角色不会自动获得判定函数
   `EXECUTE`，其写入得到 `42501`（不是 `P0001`，运维容易误判为权限配错）。
3. **`afs_audit` 的 SELECT 范围仍是 18 张表**，尚未逐表论证「审计到底该看见哪些」。
4. **阶段 3 仍不放行**。本轮是数据与约束落地，不是放行评审。
5. **【本轮自己引入的不一致】** ADR-R40 v1.4.7 附录 J1 写着「清单见
   `a9b8.OWNERSHIP_FKS` + `f8a9.OWNERSHIP_FKS` **两处常量**」。批次三加了第三处，
   这句话现在不完整。

   这是上一轮刚定的通则（**ADR 记录决策，不记录当前进度**）在同一份文档里的重犯：
   把「有几处常量」这种随每批增长的枚举写进了冻结文本。正确写法是「清单以各归属迁移
   模块的 `OWNERSHIP_FKS` 常量为准（逐批累积）」，不带数目。

   v1.4.7 已定版·文本冻结，不改原件字节。修法留到下一次实质修订，届时**一并**把
   恒定枚举清干净（不只是这一句）。在此之前，以本条与台账为准。

---

## 七、口径

- 本闸门只回答「**有没有被纳入**」，不回答「纳入的约束够不够强」。它与 `C8`
  （存在 ∧ `convalidated`）及台账的「违规行数=0」是三个正交维度，缺一不可。
- 「18/18」不是把分母做大：17 张含 `project_id` 的表里，此前有 1 张完全在闭环之外。
  台账行集必须能逐行对应一条真实约束，凑数没有意义。
- 迁移仍为 `NOT VALID` → `VALIDATE` 两步，**终态判据是台账每一行 PASS**，不是
  「约束已创建」。`NOT VALID` 只是过渡，不构成豁免。
