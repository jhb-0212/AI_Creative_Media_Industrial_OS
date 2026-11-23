# 问题清单与解决办法（Problems & Fixes Ledger）

> **定位**：给人看的「问题 → 根因 → 解决办法」台账，用于回顾与检索。
> 它不是进度记录，也不替代另外两样东西：
> - 给 AI / 自动化遵守的决策与铁律 → `.workbuddy/memory/MEMORY.md`
> - 防问题复发的**可执行闸门** → `scripts/merge_readiness.py` + CI `Merge readiness` 步骤
>
> **维护约定**：新问题按类别追加、编号唯一；每条必须写清「现象 / 根因 / 解决办法」，
> 尽量附证据（提交号 / 测试 / 报告）。只写「已修复」而不写根因的条目等于没写——
> 清单的价值在于让人遇到相似症状时能认出同一个病根。

覆盖时段：2026-11-13 ~ 2026-11-23（Shot 域闭环、双线整合、收口 PR #22、自检闸门 PR #23、
第二轮审查整改 PR #24、第三/四轮审查 P1-01~P1-08 整改 PR #38~#44）

---

## 一、统计概览

| 类别 | 条数 | 典型症状 | 代表问题 |
|---|---:|---|---|
| A. 并发与锁语义 | 4 | 并发下状态被改写、重检失效、锁"看不见" | `FOR KEY SHARE` 挡不住归档 |
| B. 归属、授权与 RLS | 5 | 越权静默成功 / 42501 像业务拒绝 / 跨项目借用 | DEFINER/INVOKER 用反 |
| C. 双线整合与契约治理 | 5 | 平行实现、编号冲突、绿灯测死代码 | 96 vs 88 动作、A13/A14 重编号 |
| D. 迁移与数据落地 | 4 | 迁移在混合态中止、悬空引用、错基线跑绿 | a13b0 遇残留唯一键 42710 |
| E. 验证装置与闸门 | 6 | 闸门给现状背书、清单漏项、mock 契约不符 | `CODE_DIRS` 自抄漏 4 目录 |
| F. 代码质量的静默缺陷 | 6 | 不报错但行为错 / 负债累积 | 字典重复键静默覆盖、122 项 lint 债 |
| G. 工程流程与环境 | 6 | 直推被拒、网络通道失败、基线游离副本、SHA 一致性未验收、YAML 重复键 | GH006 / BLOCKED-ENV / safe_load 静默覆盖 |
| H. 第三/四轮审查（P1 系列） | 6 | 权限/引用看着齐全、第一次真实调用才炸 | 表权限在但 schema 无 USAGE / SQL 表名没跟分域迁移 |
| I. 验收取证与自检 | 3 | 判据比实现更容易错、清单漏项、自检把环境错误当证据 | 闭包判据用可空外键 / `--basetemp` 落 `C:\tmp` |
| J. 归属约束整改闭环 | 2 | 归属「看着管住了」其实是策略不是约束；修完自检前提被改掉 | 策略当外键用 / 自检断言坏数据可写入 |
| **合计** | **47** | | |

**出现频次最高的三类根因**（占绝大多数条目）：
1. **判据/前提本身错了** —— 不是代码写错，是"用什么判断对错"错了（锁类型、DEFINER、语句多重集、错基线）。
2. **清单类事实人工维护** —— 清单不会自己举手，漏掉的条目不报错，只是不在那里。
3. **两套事实源并存** —— 契约、schema、服务、测试各维护一份真相，直到合并点集中爆发。

---

## 二、问题明细

### A. 并发与锁语义

#### A1 `FOR KEY SHARE` 挡不住归档（实测否掉原方案）
- **现象**：方案中「创建前 `FOR KEY SHARE` 锁父，与归档侧 `FOR UPDATE` 天然相遇」两处都不成立。
- **根因**：① 全仓根本没有 `FOR UPDATE`，归档走乐观锁条件 UPDATE；② PostgreSQL 语义上 `FOR KEY SHARE` 只与「改键值 / 删除」冲突，**不与 `UPDATE SET status` 冲突**。用它是完全没加锁，并发窗口原样保留。
- **解决办法**：改用 `FOR SHARE`（`with_for_update(read=True)`）。它挡 `UPDATE SET status` 和乐观锁条件 UPDATE，却不阻塞两个创建方之间、也不阻塞子表 INSERT（外键只取 KEY SHARE）——精确到"只挡归档"。实测 H2/H3/H5/H6/H8 覆盖。
- **证据**：ADR-R27；提交 `241568c`。

#### A2 等锁之后读到新值还是旧快照
- **现象**：归档方提交后，创建方重检父状态——读到的若是旧快照 `ACTIVE`，冻结判定放行，闸门全绿而数据已错。
- **根因**：隔离级别决定等锁后的读结果，方案没考虑。
- **解决办法**：RC 下读到 `ARCHIVED`（重检生效）；RR 下 40001（fail-closed）。两种都可接受，唯独"RC 下读到 ACTIVE"不可接受。装置 H4/H7 专门钉住这一点。
- **证据**：提交 `241568c`。

#### A3 RLS 错租户下 `FOR SHARE` 静默返回 0 行
- **现象**：错租户查父行时锁不到行，但语句**不报错**，照样成功返回。
- **根因**：RLS 把「看不见」变成「锁不到」，`SELECT ... FOR SHARE` 命中 0 行不是异常。
- **解决办法**：`if parent is None: raise` 是必需的 fail-closed，不能把"查不到"当成"没有并发冲突"。实测 H9。
- **证据**：提交 `241568c`。

#### A4 验证装置把归档方写成 `ROLLBACK`，重检用例双红
- **现象**：H4/H7 两条本该验证"等锁后重检"的用例失败。
- **根因**：装置缺陷——回滚后读到 `ACTIVE` 本来就对，用它判重检等于**换了被测命题**。
- **解决办法**：归档方改 `COMMIT` 后全绿。教训：**修装置还是修产品，判据是"被测命题有没有被换掉"**。
- **证据**：提交 `241568c`。

---

### B. 归属、授权与 RLS

#### B1 DEFINER / INVOKER 用反，归档全挂 42501
- **现象**：跨表归档守卫用 `INVOKER` 时全部 `42501`（功能整体不可用，报错还长得像业务拒绝）；改 `DEFINER` 后变成正常的 `P0001` 业务拒绝。
- **根因**：`svc_project_planning` 对 `shot_planning.shots` 零权限，`svc_shot_planning` 对 `scenes` 也零权限。旧记忆里"判定函数写 DEFINER 会让 `pg_has_role` 恒真"只适用于**授权判定**，被错误地泛化到所有函数。
- **解决办法**：判据改为"函数在判什么"——**判定授权用 INVOKER**（否则 `current_user` 变 owner、恒真），**检查数据用 DEFINER**（不判角色、不返回数据、只 `RAISE`，只会看得更全）；DEFINER 必配 `SET search_path`。补授权不是解法——那等于让域间直读原始表，永久破坏所有权边界。
- **证据**：提交 `241568c`。

#### B2 跨表不变量只拦一侧，被对称路径绕过
- **现象**：只在「归档时查子行」设防，仍可先把镜头推进 `IN_PRODUCTION`（无人拦）再归档。
- **根因**：不变量是跨表的，只守卫一条写入路径。
- **解决办法**：双向判定。B 侧有一处刻意放行——父行此刻查不到时不拒（同事务「先建场次后建镜头」合法，父行缺失由外键兜底 23503）。台账用可复算违例行数（能 FAIL），不认"触发器存在"。
- **证据**：提交 `241568c`；变异验证"卸守卫造违例再装守卫 ⇒ 违例行数=1 且 FAIL"。

#### B3 ACL 对账把「对象不存在」当成「权限为空」
- **现象**：分叉库 9 项 ACL 漂移里 2 项是 `audit.status_transitions` **表不存在**，却被判成"少授权"，诱导人手写 GRANT 补一张不存在的表。
- **根因**：对账前没有先判对象是否存在。
- **解决办法**：先 `to_regclass` / `pg_class` 派生真实存在的表；缺表单列 `[N-A]` 并带可机检前提，只对比存在的对象。main 测试库尝试补 `scenes` 会牵出整条分叉守卫链，已回退——测试库不含分叉对象属"不适用"。
- **证据**：提交 `f7c0486`、`241568c`。

#### B4 两套授权 / RLS 体系混库 → 67 红灯
- **现象**：`.env` 指向分叉库时 tests/unit 的 API/DB 用例报 `project_scope_projects` RLS 拒绝，共 67 红灯。
- **根因**：分叉库 RLS 已是「组织 × 租户」双维度，而 main 链测试假设的是单维度；两套体系语义不同，**改测试断言去迁就就是掩盖**。
- **解决办法**：在同一实例新建 `ai_film_api_tests` 空库，从空重放 main 链到 `c1a2b3d4e5f6`，`.env` 指向它；分叉库继续专管归属验证。**没有改一条测试断言**。
- **证据**：提交见当日"剩余红灯清零"段落。

#### B5 单列外键放行跨项目借用返工授权
- **现象**：`shots.rework_basis_ref` 若用单列外键指向返工任务，A 项目可拿 B 项目的授权单号回退。
- **根因**：单列引用无法约束"同一项目"这一归属前提。
- **解决办法**：建目标表 `workflow.rework_tasks`（DANGLING 只能靠建表消除），外键改为**复合** `(project_id, rework_basis_ref)`。撤表 ⇒ DANGLING 1 + 台账 FAIL；撤外键 ⇒ 跨项目写成功——两侧变异都验证过。
- **证据**：批次九 `m3b4c5d6e7f8`。

---

### C. 双线整合与契约治理

#### C1 本地 main 与远端 feat 是平行实现，架构互斥
- **现象**：远端 `feat/a13-scene-shot-runtime`（5 提交）与本地 main 都实现了同一批 Scene/Shot 对象，硬冲突在三处：迁移（远端一张 `a13b0` 建 5 表进 `versions/` 正式链 vs 本地 14 个 pending 迁移分 schema，起点是另一条链）、契约编号（两边都占 A13/A14）、服务（草案链+SC-5 vs 父链冻结，`episode_id` vs `episode_ref`）。
- **根因**：同一业务能力没有单一权威入口，两条线各自维护一份真相。
- **解决办法**：用户裁定"真合并重做架构"，以**远端为骨架**统一运行时；本地 A13/A14 修订重编号为 A16/A17（retroactive），计数重述为 96；本地 pending 归属迁移保留为"未激活的替代治理线"（不是死 schema，不作为红灯）。两个源分支原样保留作证据。
- **证据**：WIP `d55ac41` → `f1df27f`。

#### C2 21 个合并冲突 + 冻结清单漂移
- **现象**：契约 96 vs 88 动作；本地占用了远端 A13/A14 编号；`freeze_manifest --check` 出现 4 项哈希漂移。
- **根因**：编号没有全局分配机制；A14 修订已登记但未重签冻结清单。
- **解决办法**：契约取远端 96 动作超集，本地修订重编号；状态机补回 `frozen_states` / `initial_states`；冻结漂移按 A14 流程 `--write` 重签（是"已修订未重签"，不是私改），14/17 条修订全部有据。
- **证据**：`f1df27f`；`f7c0486`。

#### C3 17 个红灯其实在测死代码
- **现象**：整合后 24 个失败里 17 个来自本地 Scene/Shot 测试。
- **根因**：`scene_routes` / `shot_routes` 及其 SQLAlchemy 仓储是**未挂载、未被契约引用、端点已被 `shot_planning_routes` 覆盖**的死代码岛；运行时早已统一到远端，分裂只在测试层。
- **解决办法**：删死代码岛（4 个文件）；本地 `test_shot_runtime.py` 测旧 API，删除；本地 Scene 测试保留并适配统一服务。mypy 25 错 → 0（127 文件），是根治不是加 ignore。
- **教训**：合并时先判断"运行时是否真的分裂"，别在两套实现间反复切基底。

#### C4 两套 SceneService 寻址口径不兼容
- **现象**：治理线用 `episode_ref` + 服务层父链冻结；A13 线用 `episode_id` + DB 守卫/RLS。
- **解决办法**：`SceneService` 重写为**单一权威**，兼容两条创建路径：位置参数前 4 个保持 A13 口径，`project_repository` 作为第 5 可选依赖追加，两条线装配代码都无需重排；治理线补父链逐级冻结、组织键从父项目派生、幂等先于冻结、Project→Episode 固定锁序；A13 的 SC-5 归档排他锁内重判保留。`DuplicateEntityError` 按"是否提供 entity_id"精确区分幂等重放 / 编号占用 / 跨集撞号。
- **证据**：`f1df27f`。

#### C5 幂等与冻结的顺序写错
- **现象**：注释说"幂等先于冻结"，代码却把冻结塞进父链加锁函数，实际先冻后幂等。
- **根因**：注释与实现脱节。
- **解决办法**：固定顺序 **锁父链 → 幂等回读 → 组织键 → 冻结判定 → INSERT**。锁早于冻结关并发窗口；幂等早于冻结，否则归档后重放一个当时成功的请求会变 409，调用方永远无法确认首次结果。用哨兵守卫写成会失败的变异式单测。
- **证据**：当日"收口"段落。

---

### D. 迁移与数据落地

#### D1 a13b0 在混合态库上中止整条迁移链
- **现象**：`a13b0c1d2e3f` 无条件建 `uq_episodes_entity_project`，遇治理链残留的同名唯一键（列序不同但唯一性等价 `entity_id,project_id`）即 42710。
- **根因**：迁移假设"从零建干净库"，没假设"库可能停在任意中间态"。
- **解决办法**：改为 catalog 实判后幂等创建；同名但**列集不等价则 fail-closed** 拒绝；downgrade 对称，不删非本迁移创建的约束。
- **教训**：混合残留库上的正式链迁移必须能幂等升级。报告产物不写进 `versions/`（会让 Alembic 目录变事实源）。
- **证据**：`f1df27f`。

#### D2 冻结态判据被当成"终态别名"
- **现象**：默认"非终态即不冻结"会造出合法却永远推不动的孤儿子对象。
- **根因**：终态与冻结态是交叉关系——`Project.COMPLETED` 非终态（终态是 ARCHIVED）但应冻结。
- **解决办法**：判据改为**状态图有无回路**：`COMPLETED` 只有 `→ARCHIVED` 一条出边、无回 `ACTIVE` 的路，即契约已认为内容不再变。`Lifecycle.frozen_states` 刻意不默认取 `terminal_states()`，避免新状态静默继承没人想过的语义。
- **证据**：`241568c`。

#### D3 验证库推进到新批次后，旧脚本按设计拒绝运行
- **现象**：验证库推进到批次七/八后，`verify_batch7` 拒绝在错误基线上运行。
- **根因**：脚本自带前提检查（正确行为）。
- **解决办法**：新建克隆并回退到脚本声明的 `keep` 层，而不是放宽脚本前提。**在错基线上跑出的绿比红更危险**。同理 `lab.prepare_pre_activation_baseline()` 清行 → 逆序回退 → `assert_rewound()`，不过必须 raise。
- **证据**：当日"剩余红灯清零"段落。

#### D4 造数写死 INSERT 列清单，表一加列就炸
- **现象**：造数失败，报错却像表结构问题。
- **根因**：清单只列当前用得上的列。同类事故：`OWNERSHIP_STACK` / `SELFTEST_MIGRATIONS` 漏批次（函数型迁移不进栈 → 回退残留函数 → 下次 `CREATE OR REPLACE` 静默覆盖，测到旧实现）。
- **解决办法**：`lab.insert_minimal_row` 从 `pg_attribute` 派生列；两个栈各补批次六。判据一律从 `pg_catalog` / 台账派生并可复算。
- **证据**：`241568c`。

---

### E. 验证装置与闸门

#### E1 自检脚本自己抄了第二份目录清单
- **现象**：第一版 `merge_readiness.py` 硬编码 `CODE_DIRS`，当场漏了 `artifacts/backups/contracts/dist` 4 个目录，被自己的 R0 防呆报红。
- **根因**：**病根在写闸门的人身上重现**——清单人工维护必然腐烂。
- **解决办法**：删除自抄清单，改为从 `architecture_check` 导入 `LAYERS` / `NON_LAYER_DIRS` 单一事实源；R0 只针对"含 `.py` 却未登记"的目录，不含代码的产物目录直接跳过。
- **证据**：`02da048` → PR #23。

#### E2 闸门没有负向测试 = 删条目也绿
- **现象**：如果闸门只断言"仓库现状 0 发现"，把检查条目删掉照样 PASS。
- **根因**：只有正向断言，没有证明闸门能 FAIL。
- **解决办法**：12 项负向测试——仓库现状 0 发现 + 逐条在临时目录构造坏状态，断言能 FAIL 并点名具体位置。原则：**闸门必须先于修复运行**（先在坏状态跑红并点名，再修）。
- **证据**：`tests/engineering/test_merge_readiness.py`。

#### E3 闸门 mock 与真实接口不符
- **现象**：写法正确的迁移在静态回放时崩在 `'list' has no 'all'`。
- **根因**：`boundary_assertions._EmptyResult.scalars()` 返回裸 list，与真实 SQLAlchemy `ScalarResult` 接口不符。
- **解决办法**：补 `_EmptyScalarResult`，忠实模拟 `.all()` / 迭代 / `.first()`。测试替身必须遵守真实对象的契约。
- **证据**：`f1df27f`。

#### E4 内存仓储持有领域对象的同一引用
- **现象**：状态推进原地改 `entity_version`，仓储若持有同一对象，乐观锁拿改后版本比改前期望值，必然假冲突。
- **根因**：测试替身没有遵守真实库的 detached-entity 语义。
- **解决办法**：仓储存快照、读也返回快照；uuid 列 asyncpg 返回 `UUID` 对象须 `str()` 再比。
- **证据**：当日"收口"段落。

#### E5 DATA-6 用「语句多重集」做判据，本身即错
- **现象**：分支链授权迁移不在主链扫描范围，表现为"少执行 3 条"，8 组变异骗过检查。
- **根因**：迁移是时间胶囊，历史语句并集 ≠ 当前矩阵是正常状态。
- **解决办法**：判据改为**折叠到角色 × 对象 × 权限后的终态相等**；分支链授权迁移必须进回放，否则新表的 REVOKE 静默缺席。8 组变异全部报红，基线 PASS。
- **证据**：`241568c`。

#### E6 夹具填「像 ID 的假值」撞既存外键 → 整表用例静默跳过
- **现象**：测试看似通过，实际整批用例没跑起来。
- **根因**：假 ID 撞上真实外键约束，setup 失败被吞。
- **解决办法**：夹具值必须明确区别于真实标识；自检不许"用 A 的失败证明 B 有效"，两侧各证伪一次；三态 PASS / FAIL / **N-A**，N/A 不计通过率且单列待裁决缺口。
- **证据**：工程通则（多次出现）。

---

### F. 代码质量的静默缺陷

#### F1 归属解析字典重复键，旧值指向已删 schema
- **现象**：`_RESOLVE_SQL` 里 `Scene` / `Shot` 各定义两次，Python 字面量后键覆盖前键——旧 `Shot` 指向已删除的 `shot_planning.shots`。运行时恰好用 A13 段，功能没坏，但死代码掩盖整合真相且触发 F601。
- **根因**：双线整合后旧段未清理；字典重复键**不报错**。
- **解决办法**：删旧段、保留 A13 权威段，并把旧注释里的三列外键 / I-2 守卫决策依据合并进保留段。新闸门 D1（AST 字典重复键）正是此事故的直接回归。
- **证据**：`f71a4e9` → PR #22。

#### F2 导入期循环
- **现象**：检查器报包 → ddl → registry → 包的循环依赖。
- **根因**：`authorization/__init__` eager import 子模块（把子模块→祖先包算成边）。
- **解决办法**：改 PEP 562 惰性门面——包初始化零加载，只留符号表 + `__getattr__`，API 不变。不是调 import 顺序、也不是关规则。
- **证据**：`241568c`；R3 检查 0 违规。

#### F3 非状态版本变更绕过唯一写入口
- **现象**：Shot 版本指针前移若直接 `repository.save()`，闸门报红——"指针变了、事件没写"。但指针前移不是状态迁移，事实表有 `from_status <> to_status`，不能伪造 `status_transitions`。
- **根因**：唯一入口只覆盖状态迁移，没覆盖非状态的版本变更。
- **解决办法**：新增 `service/transition.py::save_versioned_entity()`——领域 mutation + 乐观锁 save + 事件写入同序列，显式登记进唯一入口白名单并注明与 `advance_status` 的分工。服务层不得再直接 `save()` / `transition_to()`。
- **证据**：`f7c0486`。

#### F4 122 项 lint 债在从未过 CI 的分支上累积
- **现象**：本地 main 全仓 ruff 122 红，而基线 `origin/main` 全绿；CI 的 `lint-and-test` job 跑 `ruff check .`，PR 必红。
- **根因**：这些文件由本地治理线提交 `279fcf1` 引入（`docs/audits` + `data/migrations/pending`），该线从未推 main、从未过 CI，是质量盲区。生产代码零告警。
- **解决办法**：全部真修（未用导入 / 重复 noqa / import 排序 / dict 字面量 / 单字母变量改名 / 解包语法）；`ASYNC240` 是 CLI 启动期一次性路径解析，加**带理由**的 noqa；**不靠排除目录掩盖**（那是质量暗降）。RUF005 自动修复把多行压成 400 字符单行，手工恢复多行格式——拒绝 autofix 屎山。
- **证据**：`157520b`；PR #22 CI 4/4 全绿。

#### F5 测试把 `str` 混入枚举当错误示例
- **现象**：用例红了，但错的是测试不是实现。
- **根因**：`EpisodeStatus` 是 `(str, Enum)`，`"ARCHIVED" == EpisodeStatus.ARCHIVED` 为真，裸字符串**不会**导致冻结静默失效。
- **解决办法**：改用真正拼错的值 `"ARCHIVEDD"`，并把"str 混入枚举的宽松性"单钉一条用例，注明校验防的是拼写而非类型。
- **证据**：`241568c`。

#### F6 校验防的是什么要写清，别把宽松性当 bug
- **现象**：同类于 F5——把语言的宽松等价当成实现缺陷，制造假红灯。
- **解决办法**：任何"校验/守卫"类测试，先确认它防的是**拼写**还是**类型**还是**归属**，用能真正触发该风险的样本，而不是看似可疑实则等价的值。
- **证据**：工程通则。

---

### G. 工程流程与环境

#### G1 直推 main 被 GH006 拒绝
- **现象**：`git push origin main` 被拒（main 受保护，要求 4 项 status checks：lint-and-test / contract-check / permission-negative-tests / image-build）。
- **根因**：受保护分支的 fail-closed 正确行为。
- **解决办法**：**不改保护规则、不强推、不绕过**。从 main 建 PR 分支推送、开 PR，让 CI 真实跑 4 项检查，全绿后合并。合并用 merge commit 保留完整历史线。本地验证用 `uv run` 对齐 CI 的锁定工具链（曾核对 ruff 版本一致 0.17.5，避免白修）。
- **证据**：PR #22 `c609094`、PR #23 `b870d3b`。

#### G2 合并后 tree sha 一致性确认
- **现象**：担心"CI 绿的到底是不是我验证的那批代码"。
- **解决办法**：用 `git rev-parse main^{tree}` 与 GitHub API 查远端 HEAD 的 tree sha 对比；PR #22 两者均为 `0fdfe3e`，逐字节一致。合并提交两父 = 原远端 main + 本地 main，确认整合完整合入。
- **证据**：PR #22 收口。

#### G3 git HTTPS 通道 502，gh API 通道正常
- **现象**：`git fetch` / `git pull` 持续 502（代理 CONNECT 失败），而 `gh` CLI 走自己的 API 通道正常。
- **根因**：HTTPS 代理问题，不是合并失败。
- **解决办法**：用 `gh api` 查远端 HEAD 与 tree sha 确认交付；本地 `origin/main` 跟踪引用暂过期属正常（合并提交对象还没 fetch 到本地，`update-ref` 会失败——**不强行制造引用**）。代理恢复后一次 `git fetch origin main` 自动对齐对象 + 引用，再 `git merge --ff-only`。
- **证据**：PR #22 收口 → 代理开启后已对齐，三方 SHA 一致。

#### G4 根目录基线正文游离副本导致本地闸门红灯、CI 却全程绿
- **现象**：仓库根目录长期存在未跟踪文件 `AI影视工业化生产平台V2.0.5.md`（与 `docs/baseline/` 权威基线同名、hash 不同）。本地 `freeze_manifest --check` 判 FAIL，但 PR #22/#23 的 CI 从未报——因为它是未跟踪文件，干净 checkout 里根本不存在。
- **根因**：判据是「同名即歧义，不看内容」——读者按文件名找权威源，两份同名正文让"哪份受校验"变成需要考古的问题；下一个人可能改了没受校验的那份而 CI 全绿。该文件正是 `freeze_manifest.py` 注释里 **N-01 教训**的活标本（曾因手工回填副本导致同一件事出现 75 / 83 / 84 三个数字，并引用了不存在的 ADR）。
- **解决办法**：正文修订只能改 `docs/baseline/` 唯一权威路径，改后重新派生、重签清单；**游离副本必须删除**。（该文件非本次产物，此前保留待裁定；本次验证暴露其真实后果——如确认删除即可让本地 frozen 闸门转绿。）
- **证据**：`freeze_manifest.py:227-260` N-01；本次落盘后的 `--check` 实测 FAIL。

#### G5 SHA 三方一致性未达成时标记 BLOCKED-ENV，禁止伪造一致
- **现象**：PR #24 合并后远端 main = 合并提交 `196a6e3`（GitHub API 权威确认，CI 4/4 全绿），但 `git fetch origin main` 持续 `502 CONNECT tunnel failed` / `schannel: server closed abruptly`，本地 `origin/main` 停在 `b870d3b`。
- **验收状态**：**BLOCKED-ENV**——不是 PASS，也不是代码 FAIL。
  - **已确认**：`github.com` 通道受阻（curl 实测 000，代理放行 `api.github.com` 200）；API 返回的 `verification.payload` 是规范化文本，不是原始 Git commit object，不能替代 fetch；未伪造提交对象、未强改本地引用。
  - **尚未确认**：本地提交与远程 main 的**对象级一致性**；`origin/main` 是否准确指向目标提交；工作区是否基于最新远程基线。tree sha 相等（`99c10ef`）只证明文件树内容一致，**不证明 commit 对象一致**。
- **工程裁决（用户裁定）**：① 不接受人工重建提交对象绕过 fetch（SHA 由完整原始字节决定，签名/头/消息任一细节都影响结果）；② 不修改本地引用（`update-ref`、强制重置一律禁止）；③ 不标 PASS、不标代码 FAIL，保留 `BLOCKED-ENV`；④ 不阻塞不依赖最新远程对象的只读审查，但**禁止据此进行需要最新 main 基线的合并、变基、发布裁决**。
- **解除条件**：网络恢复后重跑 fetch，核对 API 的 main SHA、本地 `origin/main`、目标合并提交三者相等，且提交对象可读、父提交关系正确、工作区干净，方可解除。恢复命令：`git fetch origin main && git merge --ff-only origin/main`（本地 `8dfe25c` 是 `196a6e3` 的第二父，可干净快进）。
- **证据**：PR #24 收口；穷举签名头部格式变体重建对象，SHA 均不等于 `196a6e3`，已停止尝试且未伪造引用。
- **解除记录（2026-11-21，网络恢复后）**：**验收状态 BLOCKED-ENV → PASS**。按解除条件逐项实测并满足：
  ① `git fetch origin main` 成功（curl `github.com`=200），`origin/main` 由 `b870d3b` 更新到 `196a6e3`；
  ② 三方 SHA 相等——API main / 本地 `origin/main` / 目标合并提交均为 `196a6e3b8cb358967365e30ebee808f224587ce0`；
  ③ 合并提交对象可读（`git cat-file -t`=commit，gpgsig 签名完整），两父 = `b870d3b` + `8dfe25c`，父关系正确；
  ④ 工作区用 `stash → merge --ff-only → stash pop` 保持干净，本地 `main` 快进到 `196a6e3`，G5 文档改动完整恢复、stash 清空。
  全程未重建对象、未 `update-ref`、未强推——一致性来自真实 fetch，不是人为制造。
- **机器判定落地（第三轮审查 P2-02）**：G5 从人工 runbook 升级为可执行闸门。
  `merge_readiness.py --git` 新增 `check_remote_baseline()`：真实 fetch（失败即
  `BLOCKED-ENV`，绝不输出 PASS）、三方 SHA 核对（API 远端 / `origin/main` / HEAD）、
  目标提交对象可读且结构合法。闸门引入三态退出码：`0=PASS / 1=代码缺陷 /
  2=BLOCKED-ENV`；环境阻塞不记作代码 FAIL，但同样阻止合并决策。负向测试覆盖
  5 个判据分支 + 2 个 main 三态退出码（`test_merge_readiness.py`）。

#### G6 YAML 重复映射键静默覆盖（第三轮审查 P2-01）
- **现象**：PR #24 删过 `contract_amendments.yaml` 末尾重复的 `revocations` 块，但
  `contract_check.py` / `freeze_manifest.py` 都用 `yaml.safe_load`——重复键静默取后键，
  同类问题会在下一份契约 YAML 重演。`merge_readiness.py` 的 D1 只查 Python 字典重复键，
  不覆盖 YAML。
- **根因**：`yaml.safe_load` 对重复映射键不报错、不警告（与 D1 的 Python 字典重复键同病）。
- **解决办法**：新建 `packages/shared/yaml_strict.py` 作为治理 YAML 的**唯一严格载入事实源**
  （拒绝顶层/嵌套/序列内显式重复键，保留合法 `<<` merge-key 语义），闸门与运行时共用：
  `contract_check`（全部契约 YAML）、`freeze_manifest`、`action_registry`、授权 `registry`、
  `boundary_assertions`、`gen_status_contracts`、两份审计取证脚本统一切换。
- **证据**：8 项 loader 正负向测试（`test_yaml_strict.py`）；端到端负向——向
  `budget_caliber.yaml` 注入重复 `source` 键，`contract_check` 报 FAIL 并点名文件+行号+键，
  恢复后转绿、文件零 diff。

---

### H. 第三/四轮审查整改（P1-01~P1-06，2026-11-22~23）

这一批的共同特征是：**所有静态信号都是绿的，功能整体不可用**。四条里有三条是
「权限/引用看着齐全，第一次真实调用才炸」，因此都被补成了派生式闸门。

#### H1 表权限齐全、schema 没有 USAGE（R16 三域写路径全 500）
- **现象**：`production_planning_role` / `budget_quota_role` / `qc_governance_role` 对
  `outbox.outbox_events` 有 INSERT，但写事件时报 `42501 permission denied for schema outbox`。
  于是 `GENERATION.*` 推不动状态、`BUDGET.SHOT.APPROVE` 连同事务一起回滚、`GATE.*` 同理——
  R16-D3 的 MVP 链在库里没有任何一条可写路径。
- **根因**：PostgreSQL 里表权限（`pg_class.relacl`）与 schema USAGE（`pg_namespace.nspacl`）是
  两道独立的门。授权矩阵只声明了前者，`converge_statements` 又只硬编码补了
  「本域 + `project_planning`」。`8f3b2c1d4e5a` 手写的 `GRANT USAGE` 覆盖的是**当时**
  存在的两个域角色，矩阵接管授权面之后新建的域天生缺共享 schema 的 USAGE——每建一个域复发一次。
  更麻烦的是授权守卫只比 `relacl`，`nspacl` 在结构上不在它视野内。
- **解决办法**：`referenced_schemas()` 从矩阵**派生** USAGE 集合（「他在这个 schema 下有
  表权限 ⇒ 他需要 USAGE」），删掉硬编码分支；迁移 `b8c9d0e1f2a3` 补存量（升级全量幂等，
  降级只撤三个被修复域，不碰 `8f3b2c1d4e5a` 授给老域的部分）；新增 schema 级守卫
  `test_domain_role_has_usage_on_every_referenced_schema`，判据集合同样派生。
- **证据**：守卫先在坏状态跑红并点名 3 域 × 3 schema（附「矩阵在此有表权限」的证据），
  修复后转绿；迁移 `downgrade -1` → `upgrade head` 回环并逐域比对 `has_schema_privilege`。
  契约 A33。

#### H2 资源级授权 SQL 的表名没跟着分域迁移（42P01 → 端点 500）
- **现象**：`ShotBudgetRequest` / `GenerationTask` / `GateDecision` / `ProductionRoute`
  的资源级授权在解析归属时抛 `42P01 relation "project_planning.shot_budget_requests"
  does not exist`，审批端点**整体不可用**。
- **根因**：`packages/identity/ownership.py` 的 `_RESOLVE_SQL` 是手写 SQL，R16 分域迁移
  （`a0b1c2d3e4f5`）把四张表搬到自有 schema 后没人同步它。服务层单测全绿，因为单测不经授权依赖。
  「SQL 里的表名不会自己举手，它只是不存在而已」。
- **解决办法**：改四处 schema；新增 `tests/integration/test_ownership_resolve_targets.py`
  ——从源码正则抽 `FROM|JOIN a.b`、逐个 `to_regclass` 判存在、再真跑一遍（探针值 → `None`）。
- **证据**：把 `shot_budget_requests` 临时改回坏表名 → 两条用例报红（`BAD_STATE_EXIT=1`），
  恢复后 `EXIT=0`。契约 A34。

#### H3 租约持有者与审批人来自请求体（身份是自称的）
- **现象**：任何人只要知道别人的 `worker_id` 就能续租、终结甚至写出 `PASSED` 判定；
  申请人填一个别人的名字即可通过「自批拒绝」。
- **根因**：把身份当参数。`lease_owner` 的唯一写入源是请求体，而它又是
  `require_lease_owner` 的唯一判据 ⇒ 判定等于「谁知道这个名字，谁就是持有者」。
  `approver` 同理，是 `approver != requested_by` 的唯一输入。两者都比「拒绝」更糟：
  它们在库里留下**看不出错**的审计记录，事后复核也查不出来。
- **解决办法**：四个请求模型删字段 + `extra="forbid"`（旧调用方 422，而不是被静默忽略后
  以为身份生效），路由改传 `principal.subject_id`。`operator_ref` 保留在请求体并写明理由：
  它只做溯源、不参与任何授权比较，判据是「不可伪的必要性」而非字段名。
- **证据**：`test_identity_from_principal.py`（12 项）——模型层拒绝、OpenAPI 契约
  `additionalProperties=false`、AST 派生不变量（凡 `principal.<attr>` 填充的槽位，
  请求模型不得声明同名字段）、真库端到端（伪造字段 422 且任务状态一字未变；
  换一个同样是 Worker 的令牌续租被拒）。契约 A34。

#### H4 账本结算逐笔判定（合计可以到预留的 2 倍）
- **现象**：`RESERVE 1000 → RELEASE 1000 → REFUND 1000`，每笔都合法，合计 2000。
  账本是追加式流水、没有余额列，超支只能靠求和发现，而原来没有任何地方求和。
- **根因**：`_settle` 判的是「本次金额 ≤ 预留」，不是「累计 ≤ 预留」。只在服务层求和也不行：
  两个并发事务各自读到 0，各写一笔。部分唯一索引只挡「同 kind 重复」，挡不住一 RELEASE + 一 REFUND。
- **解决办法**：库层触发器 `budget_ledger_settlement_guard`，按 `reservation_ref` 取
  `pg_advisory_xact_lock` 串行化后重新求和。用咨询锁而非 `FOR UPDATE`：`budget_quota_role`
  对账本只有 SELECT/INSERT，为了加锁把账本改成可 UPDATE 等于用不变量换锁。
  错误码用 `23514` 而非 `P0001`（后者不被 `port_errors` 翻译，会冒成 500）。
- **证据**：6 项集成（超额/等于边界/预留不存在/负金额/并发时用 `pg_locks` 证明 T2 在等锁）
  + 3 项单测；迁移 `c4d5e6f7a8b9` 已做升降回环。契约见 A32 之后的记录。

#### H5 跨域提交顺序：待检项先于生成成功
- **现象**：生成 Worker 在 `finish` 之后、`commit` 之前用独立 QC 会话写 `qc_tasks` 并立即提交。
  生成域提交失败就留下一条指向不存在生成任务的待检项。
- **根因**：跨域写入排在本地提交之前，且 `qc_tasks.generation_task_id` 是跨域引用、没有物理外键，
  数据库拦不住。
- **解决办法**：改成事件驱动——`GENERATION_SUCCEEDED` 与终态同事务进 outbox，relay 消费才入队
  （事件存在 ⟺ 终态已提交）。payload 补产物两字段与归属两字段，让事件自足；
  `MalformedEventError` 刻意不继承 `DomainError`，否则载荷违约会被当业务冲突 ack 掉、进不了死信。
- **证据**：`test_relay_handlers.py` 13 项（四字段逐个缺失、幂等、不吞违约、无依赖不注册）；
  另外在 P1-01 rebase 时发现 `process_one` 里残留的旧直接入队调用会让缺陷复活，已删除。契约 A31。

#### H6 测试装置为了「能重跑」去删 append-only 历史（被库层拦下）
- **现象**：端到端用例第一版用固定任务 id，第二次运行因任务已是 `RUNNING` 假失败；
  修法是「先把 `audit.status_transitions` 里上次的记录删掉」——结果被
  `RestrictViolation: append-only 表禁止 DELETE` 拦住（连超级用户也拦）。
- **根因**：装置依赖上一次运行的副作用。而「删掉历史审计事实让测试能重跑」正是红线
  禁止的操作，只是它伪装成了测试装置需求。
- **解决办法**：每次运行生成新任务 id（项目行固定、任务 id 随机），装置自然幂等，
  一行历史事实都不用动。
- **证据**：`tests/unit/test_identity_from_principal.py` 连跑两次均全绿。

#### H7 正式链归属约束缺失（一张「35/35 闭环」的台账掩盖了它）
- **现象**：分叉库 `ai_film_industrial` 的归属台账 35/35 全 PASS，看起来归属治理已经完备。
  对正式链 `ai_film_api_tests` 重新取证后：15 张含 `project_id` 的表里只有 2 张通过，
  5 张（`shot_budget_requests` / `generation_tasks` / `production_routes` /
  `gate_decisions` / `qc_tasks`）**完全没有归属路径**，`project_id` 可以指向任意字符串。
- **根因**：两套库是不同谱系（分叉库三列外键含 `organization_id`，正式链两列外键、
  组织只在 `projects`），而验收证据被跨链挪用了一次。缺口本身则是 R16 分域迁移只搬了表、
  没搬归属约束——表和约束分两次做，后一次没人记得。
- **解决办法**：写 `main_chain_ownership_ledger.py`，目标集合从 `information_schema` /
  `pg_constraint` **实读派生**，判据为「项目真实 ∧ 租户一致 ∧ 数据干净 ∧ 已 VALIDATE」。
  「项目真实」走**传递闭包**（`scenes` 没有直接指向 projects 的外键，但经
  `episodes` 可达），并要求路径全程 NOT NULL——复合外键在任一列为 NULL 时完全不校验。
- **证据**：`docs/audits/mig-recon/main-chain/ownership-ledger.md`（13/15 未通过）；
  `--selftest` 插入一行租户不一致的 episode 后计数 = 1，证明判定不是恒真。

#### H8 类型检查的覆盖面靠手写清单（新落地的层天然没有保护）
- **现象**：`adapters/` 落地了网关与存储的真实实现，而 CI 的 mypy 仍是
  `packages services apps workers`，注释还写着「adapters 仍为占位」。新代码一行类型检查都没跑过。
- **根因**：清单写在两个地方（CI workflow 与 `merge_readiness.py`），新增一层时两边都不报错，
  它们只是**漏掉**。漏掉的层没有任何信号——这正是「清单不会自己举手」的又一例。
- **解决办法**：`scripts/mypy_targets.py` 从 `architecture_check.LAYERS` 派生，按「实际含 .py」
  过滤（`agents/` 只有 `.gitkeep`，对它跑 mypy 会得到「no .py files」的非零退出码，看起来像类型错误）。
  CI 与本地闸门都调同一个派生函数，不再各写一份。
- **证据**：`tests/unit/test_mypy_targets.py` 4 项，含「新建一个有 .py 的层必须自动出现」；
  派生结果 `apps services packages workers adapters`，全仓 191 个文件类型检查通过。

#### H9 自检把「环境错误」当成「闸门有效」（这只差一步就变成假证据）
- **现象**：为了证明守卫有效，用 `subprocess` 跑「注入坏状态 → 跑测试 → 恢复」。
  三组都得到 `exit=1`，看起来完美。实际上 pytest 的 `--basetemp=/tmp/x` 在 Windows 上被
  解析成 `C:\tmp\x`（不存在），每个用例都是 **ERROR（夹具失败）**，不是 FAILED（断言失败）。
  真正的原因：Git Bash 会把 `/tmp/x` 参数转换成 `C:/Users/.../Temp/x`，而 Python
  `subprocess` 直接传参**不转换**。
- **根因**：只看退出码。`ERROR` 与 `FAILED` 的退出码都是 1，而它们证明的是相反的事情——
  一个说「环境没跑起来」，一个说「守卫抓住了坏状态」。这与「用 A 的失败证明 B 有效」是同一类错误。
- **解决办法**：把自检固化为 `tests/unit/test_guard_effectiveness.py`，判据改成三条同时成立：
  `exit != 0` ∧ 输出含 `FAILED` ∧ **不含 `ERROR`**，且失败的必须是**指定的那条**用例。
  basetemp 改用 `mkdtemp()` 生成的绝对路径。
- **证据**：8 条坏状态（扩白名单、去符号链接判定、5xx 归成拒绝、只信 content-length、
  不校验 URL 协议、prompt_ref 当正文、可重试归类、缺存储根目录）逐条命中各自用例。

### J. 归属约束整改闭环（D2 修复，2026-11-23）

H7 记的是「缺口被发现」，本节记的是「补缺口时新踩到的两件事」——它们都不在 H7 的视野里。

#### H10 归属判定只写进 RLS 策略、没写成外键（「看着管住了」的典型形态）
- **现象**：`qc_tasks` 等 5 张 R16 分域表的 RLS 策略里写着
  `USING (tenant_scope AND EXISTS (SELECT 1 FROM project_planning.projects p
  WHERE p.entity_id = ...project_id AND p.tenant_id = ...tenant_id))`
  ——与租户复合外键**语义等价**，读起来像归属已被管住；但 `pg_constraint` 里一条
  外键都没有，台账判 `FAIL_NO_PATH`。
- **根因**：把「策略」与「约束」当成同一件事。四点上不同：
  ① 策略只在 RLS 生效的连接上起作用，超管 / 迁移连接 / `pg_dump` 恢复 / 新角色都不经过；
  ② 破坏策略报 `42501`（看起来像「你没权限」），破坏外键报 `23503`
  （`ReferentialIntegrityError`，可翻译成业务 4xx）——用策略守归属，会让「数据错」
  在调用方眼里变成「权限不足」；
  ③ 策略若只写 `USING`、且该表另有更宽策略，判定可能根本不进 `WITH CHECK`，
  越权写被静默过滤成 `UPDATE 0`；
  ④ 策略是 `pg_policies` 里的字符串表达式，无法被机器判定「约束已 VALIDATE」——
  验收因此不可机检，而不可机检的判据只能靠人记得去看。
- **解决办法**：迁移 `c9d0e1f2a3b4` 给 13 张表补 `(project_id, tenant_id) -> projects`
  复合外键（目标键 `uq_projects_entity_tenant` 一直在，故无需新建唯一键）。
  构造收敛到 `packages.shared.orm.project_tenant_fk`，约束名由表名派生；
  3 张原本有单列外键的表**替换**而非并存（复合蕴含单列，并存即两份事实源）。
- **证据**：台账 15/15 PASS；15 条 `fk_*_project_tenant` 全部 `convalidated`；
  `alembic check` 无漂移（说明 ORM 声明与迁移逐字一致）。

#### H11 修复动作把自检的前提改掉了（旧断言在修复后必然失败，会被误读成「判据失效」）
- **现象**：修复前 `--selftest` 的判据是「插入一行租户不一致的 episode，违规计数必须 = 1」。
  复合外键补上后，同一行写入被 `23503` 拒绝，计数恒为 0——自检报失败，
  而那与「台账判据坏了」看起来完全一样。
- **根因**：断言的是「坏数据写得进去」（缺口存在的证据），而不是「判据能识别坏数据」。
  被测事实变了、断言没跟着变。此时的默认处置往往是「把自检放宽」，也就是**删掉证据**
  让红灯变绿——这正是最该避免的方向。
- **解决办法**：改成**两侧各证伪一次**。
  A 侧：约束在位 ⇒ 同一行必须被**外键**拒绝，且只认 `23503`
  （不接受 RLS 的 `42501`——那只证明别的东西在拦）；
  B 侧：事务内 `DROP CONSTRAINT` ⇒ 同一行能写入且违规计数 ≥ 1（证明计数不是恒 0），
  最后整体回滚，DDL 与数据一并撤销。
  约束名从 `pg_constraint` 实读，不写死——写死的名字在重命名后会让自检报「找不到约束」，
  与「约束真的不存在」无法区分。
- **证据**：`--selftest` 输出 `A=True / B=1`；`alembic downgrade -1` 回到修复前状态后，
  新闸门 `test_every_project_scoped_table_has_tenant_covering_fk` 精准点名 **13 张表**，
  与台账缺口清单逐字对应。

---

## 三、避坑总则（把上面的根因浓缩成可执行的判断）

1. **判据先行**：改代码前先问"我用什么判断它对了"。锁类型、DEFINER/INVOKER、终态相等、可复算违例行数——历史上翻车几乎都是判据错，不是实现错。
2. **清单一律派生**：`pg_attribute` / `pg_constraint` / `information_schema` / 台账 / 既有检查器的常量——凡人工维护的清单必漏，漏了不报错。
3. **沉默失败要主动举手**：字典重复键、`UPDATE 0`、`FOR SHARE` 返回 0 行、假 ID 撞外键致用例跳过——凡"没报错"都要补一条"确实生效"的断言（影响行数 + 回读值）。
4. **闸门先于修复**：先在坏状态跑红并点名，再修；闸门必须有负向测试，否则删条目也绿。
5. **一个能力一个权威**：合并时先判断运行时是否真的分裂，别在两套实现间反复切基底；第二套实现只能是短暂死代码，且合并时必须删除。基线正文同理——唯一权威路径 `docs/baseline/`，同名副本即歧义，必须删除（G4）。
6. **迁移写成任意中间态可幂等升级**：不假设干净库；同名约束 catalog 实判，列集不等价 fail-closed。
7. **不接 CI 的分支是债务区**：长生命周期分支至少接 lint + 契约检查，否则债只在合并点还。
8. **不绕过治理机制**：保护分支被拒就走 PR；质量负债真修或带理由豁免，不排除目录、不强推、不改保护规则。
9. **验证装置改前提 = 换命题**：修装置还是修产品，看被测命题有没有被换掉；错基线上跑出的绿比红更危险。
10. **无证据不宣称一致，无通道不伪造一致**：本地 SHA 三方一致性拿不到就标 `BLOCKED-ENV`，不标 PASS、不标代码 FAIL。tree sha 相等 ≠ commit 对象一致；API 的规范化 payload ≠ 原始 commit object；不重建对象、不强改引用。`BLOCKED-ENV` 只放行不依赖最新远程对象的只读审查，不放行需要最新 main 基线的合并/变基/发布（G5，已落地为 `merge_readiness --git` 机器判定）。
11. **同语义的静默覆盖要有唯一载入入口**：Python 字典重复键（D1）与 YAML 重复映射键（G6）是同一种病——后者覆盖前者、不报错。凡是"多份声明合并成一份"的格式，必须有一个统一且 fail-closed 的解析入口（本仓为 `packages/shared/yaml_strict.py`），不能让 `safe_load` 散落各处；闸门要能注入反例真实报红点名。

---

## 四、这些教训对应的自动化防线

| 防线 | 防住的问题 |
|---|---|
| `scripts/merge_readiness.py`（D1/D2/D0/R0） | F1 字典重复键、同名定义、装饰器语法、E1 清单腐烂、C/E 覆盖面 |
| `scripts/merge_readiness.py --full` | F4 lint 债、G1 CI 盲区（编排 ruff/mypy/contract/arch/freeze） |
| `scripts/merge_readiness.py --git` | G1/G3 合并前工作树洁净 + 分支新鲜度 |
| CI `Merge readiness` 步骤（contract-check job） | 每个 PR 验证闸门本身没坏 |
| `tests/engineering/test_merge_readiness.py`（12 项） | E2 闸门给现状背书 |
| `verify_grants.py` / ACL 对账（`to_regclass`） | B1/B3 授权函数 EXECUTE 派生、对象不存在误判 |
| ADR-R27 / 台账 `collect_invariants` | A1~A4 锁语义、B2 双向不变量、D2 冻结态 |
| 分支保护 + PR CI 4 项 | F4/G1 未过 CI 的代码进主干 |
| `test_domain_role_has_usage_on_every_referenced_schema` | H1 只授表不授 schema USAGE（`nspacl` 不在表级对账视野内） |
| `tests/integration/test_ownership_resolve_targets.py` | H2 手写 SQL 的表名没跟分域迁移（`to_regclass` 实读 + 真跑一遍） |
| `tests/unit/test_identity_from_principal.py` | H3 身份自称（AST 派生：principal 填充的槽位不得由请求体声明） |
| `budget_ledger_settlement_guard`（库层触发器） | H4 累计结算超预留（服务层求和挡不住并发） |
| `tests/unit/test_relay_handlers.py` | H5 跨域提交顺序（事件存在 ⟺ 终态已提交） |
| `test_every_project_scoped_table_has_tenant_covering_fk` | H7/H10 归属外键覆盖率（集合从 `information_schema` 派生 + 断言 `convalidated` + 拒绝冗余单列外键） |
| `test_project_scoped_rows_have_consistent_tenant` | H7 孤儿行 / 租户不一致行必须为 0（用超管连接数，否则 RLS 静默过滤恒得 0） |
| `main_chain_ownership_ledger.py --selftest`（A/B 两侧） | H11 自检断言了「坏数据可写入」而非「判据能识别坏数据」 |

> 文档与闸门的关系：本清单记录"问题是什么、为什么、怎么解"；闸门把其中可机检的部分
> 固化成"以后再犯就报红"。新增问题若可机检，应同时补一条闸门而不是只加文档条目。

---

## 五、第二轮外部审查补充（同日整改）

一次只读的外部工程审查在最新 main（PR #22/#23 已合并）上又发现 8 项问题，
全部核实属实并整改。这一轮的方法论价值在于：**审查对象是"已经全绿"的仓库**——
CI 4/4、契约闸门、冻结校验全通过，但业务事实仍有缺口。教训是「CI 绿灯 ≠ 业务不变量成立」。

| 编号 | 问题 | 根因 | 解决办法 | 证据 |
|---|---|---|---|---|
| R1 | Shot 取消授权真实性 | 代码采信任意 `cancellation_ref` 字符串写入 `authority_ref`，违背 A14 已登记的 fail-closed 裁定 | 服务层入口 + 领域守卫双层拒绝；存量 CANCELLED 对象仍可加载（历史可读、不再新增）；审计事实不再写不可解析引用 | `shot_service.py` / `domain/shot.py`；新负向测试证明「字段齐全也拒绝」 |
| R2 | ReworkTask 执行人身份 | `owner` 来自请求体，与可信 Principal 的复核人比较时可被自报值绕过职责分离 | 服务签名删 owner 入参、内部用可信 `created_by` 派生（认领制）；职责分离统一走 `require_different_subject` | `rework_task_service.py` / `domain/rework_task.py`；`test_rework_task_service.py` |
| R3 | Shot-ReworkTask 跨项目归属 | 正式链 a13b0 是单列外键；复合外键只在未激活的 pending 链 | 新增正式迁移 b1c2d3e4f5a6（catalog 实判幂等、NOT VALID→VALIDATE），把正式链升级为复合外键；ORM + a13b0 同步；真库验证跨项目写入被拒 | 真库双向验证（同项目成功/跨项目拒绝）+ downgrade 往返；SCOPE-2 断言负向验证 |
| R4 | ADR-R40 编号冲突 | 两份不同决策共用 R40，`§2/§3.3` 指 runtime、`附录 D.x` 指授权模型 | scene-shot 文档重编号 R28，逐条判断引用归属后更新（R40 专属授权模型）；按 A14 登记 A18 并重签冻结 | `ADR-R28-*`；18 条修订有据 |
| R5 | 合并闸门 git 方向写反 | `origin/main..branch` 数的是「领先」却报成「落后」，且命令失败静默跳过 | 改为 `{branch}..origin/main`；远端引用缺失/命令失败 fail-closed；4 情形真实 git 仓库负向测试 | `merge_readiness.py::check_git`；领先/落后/脏/缺失四测试 |
| R6 | `--full` 冒充完整 CI | 只跑 ruff/mypy/contract/arch/freeze，却命名「CI 全口径」 | 改用诚实命名「静态与契约检查」，输出与文档明确 pytest/DB/镜像仍以 CI 为准 | `merge_readiness.py` help/输出、Makefile target |
| R7 | SC-5 并发测试用 sleep | `asyncio.sleep(0.2)` + `assert not pending.done()` 只证明协程未完成，不证明数据库在等锁 | 改用 `pg_stat_activity` 的 `wait_event_type='Lock'` 真实证据（独立 observer 连接轮询 backend pid） | `test_scene_archive_concurrency.py::_assert_waiting_on_lock` |
| R8 | 冻结基线游离副本 | 根目录未跟踪同名副本使本地 freeze 永久 FAIL，而 CI 干净 checkout 从不报 | 判据「同名即歧义、不看内容」；备份到 `backups/` 后移除副本（可恢复），freeze 转绿 | `freeze_manifest.py::check_stray_baseline_copies` N-01 |

**本轮额外收获的两条通用教训**：
- **CI 绿不等于事实成立**：4 项检查全过的 main 上，三个 P1 业务不变量仍不成立。可机检的边界要补断言，不可机检的业务语义要显式裁定并留痕。
- **外部审查要逐条核实而非盲信**：本轮 8 条全部属实，但核实过程本身发现更深事实——P1-01 的正确答案是「代码违背已登记裁定」而非「裁定缺失」，这让修复从「新建授权对象」收敛到「恢复 fail-closed」，改动面小了一个数量级。

**同步治理**：登记 A18 修订时顺手删除了 `contract_amendments.yaml` 末尾重复的
`revocations: []`（YAML 后键覆盖前键的同类静默缺陷，正是 D1 要防的病）。
