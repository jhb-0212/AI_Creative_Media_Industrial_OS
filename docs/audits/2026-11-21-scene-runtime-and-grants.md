# Scene 运行时闭环、父链裁定与授权收敛（2026-11-21）

- 范围：`Project.COMPLETED` 冻结、父链冻结传递、`BLOCKED` 归档裁定、Scene 运行时入口、
  DATA-6 授权断言、ADR-R27 转正。
- 目标库：`localhost:15444/ai_film_industrial`。**未触碰** `15432` 与 `ai_film_verify`。
- 裁决来源：项目所有者「按工程铁律改就行」——证据优先、单一事实源、fail-closed、
  不得用「有无异常」代替可失败判据。

## 0. 结论

这轮修的不是四个孤立待办，而是同一类根因的四个面：**规则写在文档或注释里，
执行点却缺失或位置错误**。

1. `Project.COMPLETED` 判为冻结：状态图无回路，放行建集数只会产生永远推不动的孤儿数据。
2. 父链冻结逐级传递：只判直接父会被「上层归档、下层 ACTIVE」绕过。
3. `BLOCKED` 不阻断归档，但归档后不得推进到 `IN_PRODUCTION`；阻断集合收进单源函数。
4. Scene 从「有 schema、有表、零动作」变成可创建、可读、可推进；Shot 押后且理由可验证。
5. 授权断言从「语句多重集相等」改为「按角色 × 表 × 权限折叠后的终态相等」，
   分支链上的授权迁移必须参与回放。

## 1. 决策与理由

| 待裁定 | 裁定 | 根因判据 |
| --- | --- | --- |
| `Project.COMPLETED` 是否冻结 | 是 | 出边只到 ARCHIVED，没有回到 ACTIVE 的路；允许建子对象会制造合法但永久停滞的行 |
| 父链冻结是否传递 | 是 | 历史数据/上层先归档会让 Episode 仍 ACTIVE；只判直接父是同型「只拦一半」 |
| `BLOCKED` 是否阻断归档 | 否 | A 侧放行由 B 侧「归档后不得推进 IN_PRODUCTION」兜底；`IN_PRODUCTION` 不享受此豁免，因为它归档那一刻缺口已发生 |
| `SHOT.STATUS.CHANGE` | 押后 | `shots.shot_revision_id NOT NULL` 指向未实现的 `SHOT.REVISION.CONFIRM`；先登记只会得到永远 404 的「已实现动作」 |

`SCENE.READ` 是裁定过程中补入的：只给创建、不给读取，会产生写进去查不到的对象，
故障时也无法观察。它与 `SCENE.STATUS.CHANGE` 都归因于 A13，不是偷偷扩范围。

## 2. 本轮发现并修复的真实缺陷

### 2.1 幂等顺序与注释相反

`SceneService.create_scene` 原先在 `_lock_ancestors_for_child_write` 内就做了冻结判定，
而幂等回读排在其后——与代码注释声明的顺序相反。后果是：父项目归档后重放一个
当时成功过的创建请求会被误拒，调用方永远无法确认首次是否成功。

修法是拆开两步、把顺序显式写在调用点：

```text
输入校验 → 锁 Project → 锁 Episode → 幂等回读 → 组织键校验 → 冻结判定 → INSERT
```

锁必须先于冻结判定以关掉并发窗口；幂等必须先于冻结判定以保证重试语义。
新增 `test_idempotency_runs_before_freeze_guard` 用哨兵守卫做变异式顺序断言。

### 2.2 内存仓储持有领域对象同一引用

`InMemorySceneRepository` 原先存的是调用方传入的对象引用。状态推进会原地改
`entity_version`，乐观锁于是拿改后版本与改前期望值比较，必然假冲突——真实数据库
没有这种别名问题。改为存浅拷贝，并让读也返回拷贝，行为与 DB 的 detached entity 一致。

### 2.3 DATA-6 看不见分支链授权迁移

断言把「授权面 == 矩阵」实现成语句多重集比对，而扫描集合只按主链位置派生。
批次八的 `REVOKE ALL ON project_planning.scenes FROM PUBLIC` 等语句在 `pending/`
分支链里，根本没进回放，于是表现为「迁移少执行 3 条」，且没有任何前兆。

修法是两条同时做：

- 回放链扩展到 `pending/feat-ownership/`；
- 判据改为「折叠到角色 × 对象 × 权限后的终态相等」，不再要求历史语句逐条相同。

后者才是根治：迁移是时间胶囊，新表由新迁移授权，历史语句的并集不等于当前矩阵是
正常状态；只有终态必须相等。八组变异（去掉批次八、超额 DELETE、整 schema 授权、
PUBLIC 残留、默认权限未撤、未声明表、默认权限被授、列级越界）全部报红，基线 PASS。

## 3. 变更清单（按层）

| 层 | 文件 | 作用 |
| --- | --- | --- |
| 领域 | `domain/enums.py` / `domain/scene.py` / `domain/state_machine.py` | `SceneStatus`、`Scene`（`scene_id` 是 `entity_id` 投影）、`SCENE_LIFECYCLE`；`COMPLETED` 入冻结态 |
| 仓储 | `repository/scene_repo.py` / `scene_sqlalchemy_repo.py` / `in_memory_repo.py` / `orm.py` | Scene 端口与 ORM；内存仓储改存快照 |
| 服务 | `service/scene_service.py` / `script_service.py` | 父链双锁双冻结、幂等优先、组织键 fail-closed、`SCENE_CREATED` 同事务 |
| API | `api/scene_routes.py` / `apps/api/main.py` | 三条动作挂 `require_role` / `require_resource_role` |
| 身份 | `packages/identity/ownership.py` | Scene 经 `project_id` 一跳收敛归属 |
| 契约 | A13 + 五个派生文件 + `object_schemas.schema.json` | 先登记后改；SC-1/2/3/5 从 RUNTIME_PENDING 改为 RUNTIME_ENFORCED |
| 迁移 | `k1f2…_shot_archive_blocking_states.py`、`l2a3…_scene_grants.py` | 阻断集合单源；`scenes` 权限收敛 |
| 闸门 | `scripts/boundary_assertions.py` / `scripts/contract_check.py` | DATA-6 终态判定；Scene 纳入状态机一致性检查 |
| 验证 | `verify_batch7.py`、`test_scene_runtime.py`、`test_shot_archive_scope.py` 等 | 行为 + 变异 + 台账可失败性 |

## 4. 验证结果

### 4.1 契约与单测

| 项 | 结果 |
| --- | --- |
| `scripts/contract_check.py` | 全 PASS（含 ACTION/OpenAPI 87、Scene 状态机 3 条、DATA-6、事件 15、安全边界 23） |
| Scene 运行时单测 | **18/18 PASS** |
| 纯单测（排除 15 个依赖测试库/认证后端的 API 集成文件） | **730/730 PASS** |
| 全量 `tests/unit` | 44 FAILED / 45 ERROR，全部落在 15 个连库文件；根因 `ConnectionRefusedError`，认证后端不可达后 fail-closed 返回 401。**未将这些红灯记为通过，也未为此放宽产品权限或改测试断言。** |

### 4.2 归属栈与迁移行为

| 项 | 结果 |
| --- | --- |
| 闭环台账 | 34/34；自检 5 态可区分未建立/待验证/存量违规/归属不一致/闭环 |
| 覆盖完整性 | 8/8；自检 3/3（目录侧、台账侧、基线回退均能被点名） |
| 归属一致性自检 | 5/5；真实库仍有 1 条已登记 DANGLING（见 §5） |
| 逐表覆盖 | 70/70，N/A 0 |
| 受控例外 | 37/37 |
| 授权完整性 | 11/11，缺口 1（G9：升级时一次性收敛的固有边界） |
| RLS 验收 | 10/10 |
| 批次二 / 三 / 四 / 五 / 六 / 七 | 16/16、15/15、21/21、26/26、13/13、10/10 |

批次七因模板库已被推进到批次七/八之后，脚本按设计拒绝在错误基线上运行。
本轮没有修改脚本前提，而是新建 `tmp_b7_baseline` 克隆并回退到 `j0e1f2a3b4c5`
（实际撤掉 `l2a3`、`k1f2` 两层），在其上得到 10/10。

### 4.3 变异验证（证明闸门会 FAIL）

| 验证 | 结果 |
| --- | --- |
| 移除 `PROJECT_LIFECYCLE.assert_child_creation_allowed` 后跑归档父项目用例 | `DID NOT RAISE DomainError` —— 用例确实依赖该守卫 |
| 哨兵替换两级守卫 | 两个哨兵都被触发；顺序哨兵证明幂等先于冻结 |
| DATA-6 八组变异 | 全部报红，基线 PASS |
| 批次七 S5/S6 双向变异 | 把 BLOCKED 加进函数→归档被拒；清空函数→IN_PRODUCTION 归档成功 |
| 台账四种坏状态 | 失败行可精确定位到具体表与约束 |

## 5. 已知红灯与遗留（不掩盖）

1. **归属一致性 DANGLING 1 条**：`shots.rework_basis_ref → workflow.rework_tasks`。
   `ReworkTask` 有契约、无运行时表。此前报告已显式登记；本轮没有把「不是边」写成豁免。
2. **架构检查 1 处导入期循环**：`packages.shared.authorization → .ddl → .registry → authorization`。
   本轮未触碰这些文件，属既有债，不因本轮改绿。
3. **目标库 ACL 漂移 9 项**（相对当前授权矩阵，均为历史积累，非本轮引入）：
   - `audit.status_transitions`：`svc_project_planning` / `svc_asset` 均缺 INSERT、SELECT；
   - `auth.revoked_tokens`：两角色均缺 SELECT；
   - `project_planning.episodes` / `projects` / `script_revisions`：`svc_project_planning` 多 DELETE；
   - `project_planning.organizations`：`svc_project_planning` 缺 SELECT；
   - `project_planning.projects` 列级：三列 SELECT 实际为空。
   批次八只收敛 `scenes`，实测其 ACL 已与矩阵一致；其余 9 项必须单独立项，不能靠
   「这次没改到」当作闭环。
4. **Shot 域仍断头**：`SHOT.REVISION.CONFIRM` 与 `SHOT.STATUS.CHANGE` 需成对落地。
5. **历史报告处理**：`2026-11-20-scene-shot-tables.md` 与
   `2026-11-21-parent-freeze-and-archive-guard.md` 仅在顶部加「后续状态」，正文未改。

## 6. 结论

Scene 的第一环已经闭环，且闭环判据全部可失败：契约先行、父链双冻结、幂等顺序、
组织键 fail-closed、状态推进事实与授权终态都有运行证据。
本轮不宣称 Shot 域完成，也不把 9 项 ACL 漂移与两条既有红灯洗成通过。
