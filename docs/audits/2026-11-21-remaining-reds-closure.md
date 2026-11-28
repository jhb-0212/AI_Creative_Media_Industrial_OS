# 剩余红灯清零：导入循环 / 测试环境 / ACL 漂移 / DANGLING

> 日期：2026-11-21　验证实例：`afs-step-feat` @ 15444
> 分叉库：`ai_film_industrial` → `m3b4c5d6e7f8`　main 链测试库：`ai_film_api_tests` → `c1a2b3d4e5f6`
> 生产 `15432` / `ai_film_verify` **未触碰**

## 一、为什么这四个红灯要一起修

上一轮收口把四类问题如实留在报告里，而不是改判据洗白。本轮逐项追到根因后发现它们**不是四个孤立故障**：

- 导入循环来自包的 eager 初始化；
- 测试红灯来自把 main 链 API 测试指向了分叉链数据库；
- ACL 漂移来自同一个分叉库缺少 main 链的 `audit` 对象；
- DANGLING 来自契约已定的 `ReworkTask` 从未建表。

共同形态是**两套体系混在一处，各自缺一半**。所以修法是让两套体系各自自洽，再补上唯一真正缺失的业务对象；不是给四个红灯各贴一个 if。

## 二、R3 导入期循环：从包初始化上消除，不靠 import 顺序

`packages.shared.authorization.__init__` 在导入期 eager import 三个子模块。检查器把「子模块 → 祖先包」算作边（父包初始化确实执行），于是必然得到
`包 → ddl → registry → 包`。

改成 PEP 562 惰性门面：`__init__` 只保留符号表与 `__getattr__`，导入期零子模块加载。这保留原有 14 个门面符号与 `__all__`，但依赖图变成单向叶子结构。

- `scripts/architecture_check.py`：R1~R5 全 0 违规
- `tests/architecture`：29/29
- 门面行为：`from packages.shared.authorization import load_registry` 正常，未知符号抛 `AttributeError`

## 三、827 项单测全绿：分两套库，不迁移就改测试断言

`.env` 原本指向 `15432`（未运行）+ `16379`（未监听），先把测试引向验证实例后又暴露第二层问题：
`15444/ai_film_industrial` 是分叉链，`audit` schema 不存在、`ReworkTask` 未建、RLS 已是组织双维度；而 `tests/unit` 中的 API/DB 用例属于 main 链体系。

对齐角色名与口令后仍有 67 项红灯，代表用例报 `new row violates row-level security policy "project_scope_projects"`——这是**策略语义不同**，不是环境抖动。

处理：在同一实例新建 `ai_film_api_tests`，从空库重放 main 链全部迁移到 `c1a2b3d4e5f6`，让 `.env` 指向它。分叉库的归属验证继续用 `ai_film_industrial`。两套体系不再互相借用对象。

| 阶段 | 结果 |
| --- | --- |
| 原始 `.env`（15432 / 16379） | 44 FAILED + 45 ERROR |
| 只对齐验证实例角色 | 67 FAILED / ERROR（RLS 策略语义不匹配） |
| main 链测试库 | **827 passed, 1 warning** |

**没有修改任何测试断言去迁就分叉库。**

## 四、ACL 漂移 9 → 0：补对象，再用生成器收敛

分叉库 9 项漂移中，2 项其实是 `audit.status_transitions` **表不存在**被误读成「权限为空」。处理方式：

1. 从 main 链 `d8e9f0a1b2c3` 迁移提取审计 schema/表/函数/策略/RLS 语句（跳过两条与分叉库重复的 TRUNCATE 守卫触发器）；
2. 对两个域执行 `converge_statements(role, domain)` 全量收敛——没有手写一条 `GRANT`；
3. 实测终态：整表权限、列级权限、`PUBLIC` 与默认权限全部与矩阵相等，**漂移 0**。

main 链测试库曾尝试补分叉链的 `scenes`，但会牵出 `repair_row_allowed` 等整条分叉守卫链；已回退该尝试，测试库保持纯 main 链。`scenes` 对它**不适用**，而不是「权限缺失」。

## 五、DANGLING 归零：批次九建真表

`shots.rework_basis_ref → workflow.rework_tasks` 是真实对象引用。上一轮明确拒绝把它改成 NonRef——那会把「未实施」永久洗白。

`m3b4c5d6e7f8` 按契约与同域模式落地：

- `ReworkTask` 契约 required 字段齐全，状态 CHECK 与状态机六态一致；
- `entity_id = rework_task_id` 由数据库 CHECK 守（与 `scenes` 同一条身份单一铁律）；
- 归属三列 + 复合外键 → `projects`，五条 RLS 策略与 `approval_tickets` 同构；
- `shots` 侧外键是**复合**的：`(project_id, rework_basis_ref) → (project_id, rework_task_id)`。单列会放行「拿别的项目返工单号给自己回退」，正是验收要堵的暗门；
- 落地顺序：建目标键 → `NOT VALID` 外键 → 存量订正 → `VALIDATE`。

同步改动：台账新增 A 行（35 行）、`OWNERSHIP_STACK` 与 `assert_rewound` 各单列本批次对象、一致性闸门给新表四列分类（业务键 / 软引用 / 多态）、覆盖闸门给审计事实表新增带机检前提的豁免。

### 变异证据（证明不是给现状背书）

| 变异 | 预期 | 实测 |
| --- | --- | --- |
| 克隆库撤掉 `rework_tasks` 与外键 | 一致性闸门 DANGLING > 0、台账 FAIL | DANGLING 1，台账 `workflow.rework_tasks 未建立（表不存在）` |
| 跨项目借用他项目的 `rework_task_id` | 23503 且回读未变 | 23503，回读未变 |
| 撤掉新外键后重放同一条写 | 成功 | `UPDATE 1`，回读生效（证明拦截者确是外键） |

## 六、自检自身的根因修复

新增台账行后 `acceptance_ledger --selftest` 由 5/5 掉到 4/5：`ledger_pending` 期望 4 PASS，实测 5。根因不是台账错，而是自检期望依赖**硬编码表名清单** `STRUCTURAL_TABLES`——「哪些约束在建表时就已 validated」又是一份会腐化的清单。

改为在「已建约束、尚未 VALIDATE」场景中，从 `pg_constraint` 实读 `convalidated` 的外键名集合，按台账行的约束名比对。新增多少张表都不用改自检；且这个集合在 VALIDATE 之前取自独立事实源，不来自被测脚本自己的输出。自检恢复 5/5。

## 七、全量验证

| 闸门 | 结果 |
| --- | --- |
| 契约一致性 `contract_check.py` | 全 PASS（DATA-6 亦 PASS） |
| 架构 `architecture_check.py` / `tests/architecture` | R1~R5 全 0 / 29/29 |
| 全量 `tests/unit` | **827 passed** |
| 归属台账（真实库 / 自检） | 35/35 / 5/5 |
| 覆盖完整性（真实库 / 自检） | 8/8 / 全 PASS |
| 归属一致性（真实库 / 自检） | FAIL 0、DANGLING 0 / 5/5 |
| 逐表覆盖 / 受控例外 / 授权 | 70/70 / 37/37 / 11/11 |
| RLS 行为验收 | 10/10 |
| 批次二~七、批次九 | 16/16、15/15、21/21、26/26、13/13、10/10、8/8 |
| 目标库 ACL 漂移 | 0 |

## 八、如实保留

- **授权 G9**：E6 是升级时的一次性收敛，新建写角色需重跑收敛语句才有函数 EXECUTE。这是「授权随权限变化」的固有代价，已作为待裁决缺口单列；当前库内无累积漂移。名单式授权只会把缺口换成静默跳过，不采用。
- **既有产品债未动**：`GateInstance` 无 schema（A8）、`SHOT.STATUS.CHANGE` 押后（`SHOT.REVISION.CONFIRM` 未实现，`shots.shot_revision_id NOT NULL`）。
- **测试库与分叉库是两套体系**：`.env` 指向 main 链测试库；归属验证脚本连分叉库。混用会重新制造本轮的根因。

## 九、可复现命令

```bash
# 一次性重建 main 链测试库（15444 实例，绝不指向 15432）
# CREATE DATABASE ai_film_api_tests;
POSTGRES_PORT=15444 POSTGRES_PASSWORD=<ROTATED-2026-11-28> POSTGRES_DB=ai_film_api_tests \
  ./.venv/Scripts/python.exe -m alembic upgrade head

# 全量单测（.env / .env.example 已指向 15444/ai_film_api_tests）
./.venv/Scripts/python.exe -m pytest tests/unit -o addopts="" -q

# 归属链闸门
POSTGRES_PROJECT_PLANNING_USER=svc_project_planning POSTGRES_ASSET_SERVICE_USER=svc_asset \
  ./.venv/Scripts/python.exe docs/audits/mig-recon/verify_batch9.py
```
