# 父冻结落地 + 归档不变量（批次六）

> 日期：2026-11-21　目标库：`ai_film_industrial` @ 15444　head：`j0e1f2a3b4c5`
> 证据：`docs/audits/mig-recon/verify_parent_freeze_lock.json`（9/9）、
> `verify_parent_freeze_e2e.json`（3/3）、`verify-batch6.json`（13/13）、
> `acceptance-ledger.json`（34/34 + 2 条不变量）

## 0. 一句话

把「归档意味着这一支不再长了」从文档语义变成三处可执行断言：父状态判定、
并发正确性、跨表「不得存在」。三处都做了**反证**（未修时能 FAIL），不是只看绿灯。

---

## 1. `FOR KEY SHARE` 为什么等于没加锁

ADR-R27 的初稿方案写「`lock_for_child_write`（`FOR KEY SHARE`）……与归档侧
`FOR UPDATE` 天然相遇」。两处都不成立：全仓没有 `FOR UPDATE`（归档走乐观锁
条件 UPDATE）；即使有，`FOR KEY SHARE` 只与「改键值/删除」冲突，
**不与改非键列的 UPDATE 冲突**。

实测（`verify_parent_freeze_lock.py`，裸 asyncpg + 真实表）：

| 用例 | 命题 | 结果 |
|---|---|---|
| H1 | `FOR KEY SHARE` 挡不住 `UPDATE SET status` | **畅通**（初稿方案失效的反证） |
| H2 | `FOR SHARE` 挡得住 `UPDATE SET status` | 阻塞（`55P03`） |
| H3 | `FOR SHARE` 挡得住**乐观锁条件 UPDATE**（`WHERE entity_version=?`） | 阻塞 |
| H5 | 两个创建方同时 `FOR SHARE` 互不阻塞 | 畅通（不串行化并发创建） |
| H6 | `FOR SHARE` 不阻塞子表 INSERT（外键取 KEY SHARE，与 SHARE 不冲突） | 畅通 |
| H8 | 真实表 + 真实 RLS 策略 + `svc_project_planning`：锁到 1 行并挡住归档 | 锁定 1 行，阻塞 |
| H9 | 错误租户上下文下 `FOR SHARE` 返回 **0 行且不报错** | 0 行 |

**H9 是设计约束而不是趣闻**：RLS 会把「看不见」变成「锁不到」，语句照样成功。
所以 service 里的 `if parent is None: raise NOT_FOUND` 是必需的 fail-closed；
若写成 `if parent is not None: 检查冻结`，锁与检查会**一起消失**。

## 2. 等锁结束后读到的是新值还是旧快照

这一条最容易漏，且漏了以后查不出来：若创建方阻塞结束拿到旧快照（`ACTIVE`），
冻结判定会放行，闸门全绿、数据已错。

| 用例 | 隔离级别 | 结果 |
|---|---|---|
| H4 | READ COMMITTED（项目默认，未显式设置） | 挂起后读到 **ARCHIVED**（重检生效） |
| H7 | REPEATABLE READ | `40001 could not serialize access`（fail-closed，可接受） |

装置第一版把归档方写成 `ROLLBACK`，测出 H4/H7 双红。那是**装置缺陷**：
回滚后读到 `ACTIVE` 本来就对，用它判「重检」等于把命题换成了「回滚是否生效」。
修正为 `COMMIT` 后全绿。记在这里是因为「先红后修」必须说清红的是谁。

## 3. 端到端：SQLAlchemy 路径上的锁是真的

上面的 9/9 证明的是**数据库语义**，不是**本仓库调用路径**。中间隔着会话事务边界：
若会话落在 autocommit 上，`FOR SHARE` 语句结束即释放，所有代码看起来都对。

`verify_parent_freeze_e2e.py`（真实 engine + 真实仓储方法 + 两个会话）：

| 用例 | 命题 | 结果 |
|---|---|---|
| R1 | `get_by_id_for_child_write` 读到 ACTIVE 且会话在事务中 | PASS |
| R2 | 持锁期间另一会话乐观锁归档 → `55P03` | PASS |
| R3 | 持锁方提交后归档成功；新带锁读读到 ARCHIVED | PASS |

R2 的判定用 SQLSTATE `55P03` 而不是异常类名：SQLAlchemy 会用方言层包装
asyncpg 异常，类名变成 `AsyncAdapt_asyncpg_dbapi.Error`，「看起来不是锁超时」。

## 4. `SCRIPT.IMPORT` 为什么不塞进共用函数

`_assert_episode_ownership` 有 3 个调用点：1 个创建（导入）、2 个审批推进
（`LOCK.REQUEST` / `LOCK.APPROVE`）。在共用函数上加冻结判定，会把
「已归档集数下完成既有审批」一起拒掉——审批是对既有事实的补记，不是新建内容。

故新增 `_assert_episode_open_for_child_write`：带锁读 + 冻结判定，
**只给创建路径用**。仓储侧新增 `get_by_project_ref_for_child_write`
（抽象方法，6 个实现），刻意不给默认实现——有默认值就等于允许某个适配器
悄悄退回无锁读，而没有闸门会发现。

行为测试 3 条 + **两次变异**：

| 变异 | 预期 | 实际 |
|---|---|---|
| M1 摘掉冻结判定 | 只有「归档拒绝」变红 | `F..`（两条对照保持绿） |
| M2 退回无锁读 | 两条创建路径变红（`locked_reads==0`），审批路径保持绿 | 命中 |

M2 证明 `locked_reads` 这个断言不是装饰：它专门防「判定还在但锁没了」。

## 5. `SECURITY DEFINER` 的裁定——推翻本文早期判断

早期判断写：「`INVOKER` 就够，复合外键保证子行同项目，RLS 过滤不掉。」
这条推理**只覆盖 RLS 层，漏了表权限层**。

实测：

| 事实 | 值 |
|---|---|
| `svc_project_planning` 对 `shot_planning.shots` | **零权限**（`role_table_grants` 无此行） |
| `svc_shot_planning` 对 `project_planning.scenes` | 零权限 |
| 函数用 `INVOKER` | `42501 permission denied for schema shot_planning` |
| 函数用 `DEFINER` | `P0001`（业务拒绝，含镜头数） |

后果不是「只挡住不该归档的」，而是**所有归档一律失败**——功能整体不可用，
且报错像业务规则拒绝（与受控例外陷阱③同型）。补授权不是解法：那会让项目规划域
直接读镜头域原始表，破坏数据所有权边界，且是永久性扩散。

与受控例外看似矛盾，实则同一判据：**判定授权用 `INVOKER`，检查数据用 `DEFINER`**。
前者 `DEFINER` 会让 `pg_has_role` 恒真、授权被绕过；后者不判角色、不返回数据、
只 `RAISE`，`DEFINER` 只让它看得更全（更严格地拒绝）。

`SET search_path` 是 `DEFINER` 的强制配套；既有 13 个守卫都没设（既存隐患，
本批次不改它们，新函数一律设）。

变异反证：把 `DEFINER` 改成 `INVOKER`，P1 从 `P0001` 变成
`42501 permission denied for schema shot_planning`——证明 P1 真的在测这条。

## 6. 必须双向

只拦归档侧会被对称路径绕过：

    归档场次（被拦）→ 先把镜头推进 IN_PRODUCTION（无人拦）→ 再归档

故两条：A 侧 `scenes` 归档时查子行；B 侧 `shots` 进入 `IN_PRODUCTION`
（含 `INSERT` 直插）时查父行。

B 侧有一处**刻意放行**：父行此刻查不到时不拒。同一事务内「先建场次后建镜头」
是合法路径（外键可能未检查），父行缺失由 `fk_shots_scene_project` 兜底成 `23503`；
在那里拒会把正常路径一起打死（F4 钉住）。

范围只含 `IN_PRODUCTION`：`NOT_STARTED`/`BLOCKED` 不拦（砍掉未开拍的场次是合法操作）。
`BLOCKED` 归所有者裁定，当前不拦，F5 与迁移注释各留一枚钉子。

## 7. 台账：可复算，不是「触发器存在」

`acceptance_ledger.collect_invariants` 增两条，三态 PASS/FAIL/N-A（N-A 不计通过率、
单列待裁决）：

1. 归档场次残留 `IN_PRODUCTION` 镜头的**违例行数**（期望 0）
2. 守卫函数缺失数（期望 0）

反证（I2）：卸守卫 → 造一条「归档场次 + 在制镜头」→ 只重装守卫不修数据
（模拟存量违例）→ 违例行数 = 1 且判定 FAIL。两条各自独立：函数缺失不会让
违例行数假 FAIL，数据违例也不会让缺失数假 FAIL。

> 「守卫函数存在」只说明装置没被删；**不变量成立**由违例行数证明。两者不可互替。

## 8. 装置缺陷三处（都是「清单只列当前用得上的」）

| 缺陷 | 后果 | 修法 |
|---|---|---|
| 造数用写死的 INSERT 列清单 | 表加列后报 `null value in column ...`，看起来像「表结构变了」 | `lab.insert_minimal_row` 从 `pg_attribute` 派生，类型覆盖不到就报错不猜 |
| `OWNERSHIP_STACK` 未含批次六 | 回退后残留函数，下次 `CREATE OR REPLACE` 静默覆盖，测到旧实现 | 补 `j0e1f2a3b4c5` + `assert_rewound` 增一项「批次六归档守卫=0」 |
| `SELFTEST_MIGRATIONS` 未含批次六 | 自检在缺层的前提上求值 | 补一条 |

另修一处**会伪造结论**的：占位值用字符串传给 `timestamptz` 会抛 `DataError`
（`expected a datetime...`），看起来像表结构问题，实际是夹具类型错。改为真类型对象。

## 9. 验证汇总

| 装置 | 结果 |
|---|---|
| `verify_parent_freeze_lock.py` | 9/9 |
| `verify_parent_freeze_e2e.py` | 3/3 |
| `verify_batch6.py` | 13/13（含 `DEFINER`→`INVOKER` 变异） |
| 台账 `--db ai_film_industrial` | 34/34 + 2 条不变量 PASS |
| 台账 `--selftest` | 5/5 场景 |
| 一致闸门 | PASS 22 / EXEMPT 3 / DANGLING 1 |
| 一致闸门 `--selftest` | 5/5 |
| batch2 / batch3 / batch4 / batch5 | 16/16 · 15/15 · 21/21 · 26/26 |
| 逐表覆盖 | 70/70 |
| 受控例外 | 37/37 |
| 授权 | 11/11（缺口 1 = E6 遗留） |
| 闭环 | 8/8 |
| `contract_check` | 退出码 0（22 PASS，84 action 不变） |
| 单元测试（15 个相关文件） | 170 项通过 |

未跑：`tests/unit/test_script_api.py`、`test_unit_of_work.py` 等 18 个依赖
15432 + Redis 的用例——**生产容器未启动，且红线不允许向 15432 写测试数据**。
这些用例失败于 `401 AUTH_UNAUTHENTICATED / 无法校验令牌吊销状态`，
发生在业务代码之前，与本轮改动无关。

## 10. 未闭环

| # | 事项 | 性质 |
|---|---|---|
| 1 | `SCENE.CREATE` / `SCENE.STATUS.CHANGE` / `SHOT.STATUS.CHANGE` 三条动作 | ADR-R27 Q1/Q2/Q4，需所有者裁定后才落 A13 |
| 2 | `shots.rework_basis_ref → rework_tasks` DANGLING | `ReworkTask` 有契约无表 |
| 3 | `GateInstance` 无 schema | V205-R14-A8 已登记 |
| 4 | `BLOCKED` 是否纳入归档不变量 | 所有者保留裁定 |
| 5 | 父链传递性：Project 归档后仍可在其下 Episode 导入剧本 | 当前只验直接父，未递归 |
| 6 | 既有 13 个守卫函数未设 `search_path` | 既存隐患，不在本批次范围 |
| 7 | `afs_audit` SELECT 范围逐表论证 | 未做 |

第 5 项需要说明清楚：`SCRIPT.IMPORT` 的父是 Episode，只看 Episode 是否归档。
若 Project 已归档但其下 Episode 仍是 ACTIVE，导入仍会成功。这属「归档不变量未
沿父链传递」，是真实缺口，未在本轮假装覆盖。
