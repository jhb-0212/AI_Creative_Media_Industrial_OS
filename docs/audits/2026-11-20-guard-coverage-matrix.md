# 受控例外逐表覆盖矩阵（13 张守卫表）

- 日期：2026-11-20
- 目标：把「受控例外已实现」从 **S1 结构存在** 推进到 **13/13 行为可证**
- 环境：`localhost:15444` 克隆库 `t3cov_lab`（模板 `ai_film_industrial`），用完即删
- 结果：**65/65 通过，0 FAIL，3 条 N/A**（另有 3 条待裁决缺口）

## 1. 为什么这一轮必须做

`verify-exception` 的 36/36 里，端到端只覆盖了 3 张表（`asset_versions` / `gate_decisions` /
`qc_reports`），其余 10 张只有结构级证据 S1：

> S1 的判据是「守卫函数体里出现了 `ownership_repair_allows`」。

这句话**不证明**三件必须成立的事：

1. 接入点是否在被无条件 `RAISE EXCEPTION` 之前（放在后面 = 永远不会走到）；
2. 返回值是否真的决定走向（`IF ... THEN RETURN NEW;` 写错成函数调用即恒假）；
3. 每张表的 **行标识列** 是否与守卫实际取值一致（`qc_reports` 是 `qc_report_id`，
   其余是 `entity_id`；抄错一列会让例外对该表永久失效，而 S1 依然全绿）。

这三件事都只能在**真实行上跑一遍**才知道。所以本轮补的不是「更多用例」，
而是「同一命题在 13 张表上逐表成立」。

## 2. 方法（以及刻意不做的两件事）

| 决策 | 理由 |
| --- | --- |
| 夹具声明式（`guard_fixtures.py`：列 → SQL 字面量） | 若在验证脚本里写 13 个 `xxx_insert()`，脚本就变成 13 份手抄 DDL，表结构一变即静默漂移 |
| 内容列**由系统目录推导** | 手写 13 个「内容列名」= 手抄白名单。推导规则：非归属列、不在守卫白名单、不被 CHECK/FK/UNIQUE 引用、类型可生成确定不同的值 |
| 不把 N/A 记成 PASS | 「按设计不建外键」与「验证通过」是两件事；混在一起通过率就失真 |

用例口径（每条都断言 SQLSTATE 或影响行数，不看「没报错」）：

| 用例 | 前置 | 动作 | 期望 |
| --- | --- | --- | --- |
| N1 | 无令牌 | 改归属列 | `P0001` |
| N2 | 令牌 + 映射 + 成员 | 改归属列 | `UPDATE 1` **且回读值已变** |
| N3 | 令牌 + 映射 + 成员 | 改内容列 | `P0001` |
| N4 | 令牌 + 映射 + **非成员** | 改归属列 | `P0001`（**不是** `42501`） |
| N5 | 同上 + 目标归属不存在 | 改归属列 | `23503`（例外不是外键后门） |

前置步骤：seed `proj-1`/`proj-2` → 执行 `a9b8`（建归属外键）→ 补第二个组织 →
授表权限 → 执行 `b0c9`。**授权必须在 `b0c9` 之前**，因为该迁移按真实表权限动态收敛
`EXECUTE`；反序会让 N4 从「守卫拒绝」漂移成「权限拒绝」。

## 3. 结果

| 表 | 类别 | N1 | N2 | N3 | N4 | N5 |
| --- | --- | --- | --- | --- | --- | --- |
| asset_service.asset_versions | 逐列白名单 | PASS | PASS | PASS | PASS | PASS |
| asset_service.delivery_manifests | 逐列白名单 | PASS | PASS | PASS | PASS | PASS |
| asset_service.qc_reports | 无条件 RAISE | PASS | PASS | PASS | PASS | **N/A** |
| generation_execution.generation_tasks | 逐列白名单 | PASS | PASS | PASS | PASS | PASS |
| generation_execution.production_routes | 逐列白名单（两列归属） | PASS | PASS | PASS | PASS | PASS |
| generation_execution.shot_route_selections | 无条件 RAISE | PASS | PASS | PASS | PASS | PASS |
| qc_governance.gate_decisions | 无条件 RAISE | PASS | PASS | PASS | PASS | PASS |
| qc_governance.qc_review_sessions | 逐列白名单 | PASS | PASS | PASS | PASS | PASS |
| shot_planning.constraint_packs | 逐列白名单 | PASS | PASS | PASS | PASS | PASS |
| shot_planning.shot_specs | 逐列白名单 | PASS | PASS | PASS | PASS | **N/A** |
| shot_planning.shot_visual_plans | 逐列白名单 | PASS | PASS | PASS | PASS | PASS |
| workflow.approval_flows | 无条件 RAISE | PASS | PASS | PASS | PASS | PASS |
| workflow.approval_steps | 无条件 RAISE | PASS | PASS | PASS | PASS | **N/A** |

两个容易出错的地方被这轮实测抓出来（都不是产品缺陷，是**测试自身的假阴性**）：

1. `organization_id` 是 `uuid`，asyncpg 返回 `UUID` 对象，与字符串直接比较**永远不等**——
   把「其实成功了」判成失败。改为按字符串比较。
2. `qc_reports.asset_version_id` 上有既存外键，夹具里填「长得像 ID 的假值」→ 造行直接 `23503`，
   整表 5 条用例全部静默跳过（第一版就是这样，5 条只剩 1 条失败记录）。改成显式
   `REF_OVERRIDES` 指向真实父行，缺引用就在夹具层报错而不是在断言层误判。

## 4. 三个发现

### F1（已证）例外放行 ≠ 绕过外键

10 张有归属外键的表，在四条件齐备、例外已放行的前提下，把 `project_id` /
`organization_id` 改为不存在的目标，**全部 `23503`**。这说明 D-10.2 的分层是真实的：
层 2 例外只解「不可改写」这一条，层 1 外键仍然独立生效，两者不是同一把锁的两种说法。

### F2（已证）角色条件是唯一变量，且权限收敛正确

13/13 的非成员用例都返回 `P0001`（守卫拒绝），没有任何一条是 `42501`（权限拒绝）。
这条同时验证了 `REVOKE PUBLIC EXECUTE` 之后「按真实表授权动态补 `EXECUTE`」的收敛策略
是对的：非成员拿得到函数执行权（否则测到的是权限），但拿不到放行结果。

### F3（新增，待裁决）三张守卫表不在归属台账内，其中一张属**范围缺口**

守卫表（13）与归属台账（13 表 + `projects`）不是同一集合：

- 交集 **10** 张：既有外键又有守卫，N5 可执行；
- `shot_specs` / `approval_steps`：`a9b8` 的 `DERIVED_NO_FK` **显式**列为「刻意不建」
  （归属由更近的父对象派生）——这是设计，不是遗漏；
- **`qc_reports`：既不在 `PROJECT_LEVEL`/`ORG_LEVEL`，也不在 `DERIVED_NO_FK`**，
  但它有 `project_id`、有不可变守卫、且被 `b0c9` 纳入了受控例外。

第三条是新的：它意味着 `qc_reports.project_id` 在**任何**归属闭环之外——既没有外键
保护 I-1，也不在 14 行台账里被要求 `VALIDATE`。实测已复现：四条件齐备时该列可被
改成任意不存在的项目，数据库不拦（`UPDATE 1`）。

这不是 `b0c9` 引入的缺陷（`qc_reports` 从来就没有外键），但**受控例外把它从「完全不可改」
变成「可被工单改、且不校验目标」**，性质变了。三个选项需所有者裁决：

| 选项 | 后果 |
| --- | --- |
| A. 把 `qc_reports` 纳入台账（补三列外键 + `NOT VALID` → `VALIDATE`） | 闭环从 14 张变 15 张，`a9b8` 需增一步 |
| B. 显式列入 `DERIVED_NO_FK` 并写明派生来源 | 台账不变，但「不建外键」从默认变成声明 |
| C. 维持现状 | 必须接受「该表归属不受数据库约束」，并写入残余风险 |

**不得默认选 C 而不登记**——这正是「文档没说 = 没人管」的典型形态。

## 5. 边界

- 未激活任何迁移（两层仍在 `data/migrations/pending/`），未在分支链上 `upgrade`；
- 未做逐行映射、未执行任何 `VALIDATE`；14 表闭环仍 **0/14**；
- 未动 `ai_film_verify` / `ai_film_industrial` / 生产 / 模板库 / 15432；
- 未改 ADR v1.4.6（仍 `2eaa1f85…f0a70`）；本轮证据待并入下一版：
  ① 附录 J.2「覆盖不完整」→ 13/13 已覆盖；② 新增残余风险 #28（无外键表在例外下的 I-1）；
  ③ F3 的选项 A/B/C 待裁决。

## 6. 复现

```bash
cd docs/audits/mig-recon
python verify_guard_coverage.py --port 15444 \
  --guard-migration ../../../data/migrations/pending/feat-ownership/b0c9d8e7f6a5_ownership_repair_exception.py \
  --fk-migration    ../../../data/migrations/pending/feat-ownership/a9b8c7d6e5f4_ownership_fk_backfill.py
```

产物：`data/migrations/pending/feat-ownership/verify-coverage.{md,json}`。
断言装置与行为断言共用 `lab.py`；`verify_exception.py` 已同步切到同一装置并重跑
**36/36**，证明这次抽取行为等价（不是「改了脚本让结果好看」）。
