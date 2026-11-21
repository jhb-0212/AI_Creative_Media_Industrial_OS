# ADR-R27：Scene / Shot 运行时入口与父链冻结

状态：已裁定；Scene 运行时已实施；Shot 前置根因（修订表对齐契约 + 三条引用边）已于 2026-11-21 落地（批次十/十一），Shot 运行时入口仍押后
关联：ADR-R19（Scene 契约）、ADR-R18（Shot 契约）、ADR-R11（不可变守卫）、ADR-R23（状态推进事实）、ADR-R25（最小权限）
草案：`docs/adr/ADR-R27-scene-shot-runtime-entry.PROPOSED.md`（原件保留，不回改）
契约修订：`V205-R14-A13`（先登记，后改派生文件）

## 结论

1. **`SCENE.CREATE` 镜像 `EPISODE.CREATE`**：以 `project_id + episode_ref` 寻址父集，
   不扩展 `SHOT.REVISION.CONFIRM` 的 `resource_type`。确认拆镜与建立正式场次是两种
   职责，前者为 HIGH 风险且主体为导演/镜头负责人，后者与集数登记同属编剧/制片范畴；
   把一个动作包成两个权限等于悄悄取消职责分离。
2. **`SCENE.STATUS.CHANGE` 采用单动作 + 目标状态作参数**：转移合法性只由
   `SCENE_LIFECYCLE` 判定，不按边拆动作。`DRAFT / ACTIVE / ARCHIVED` 三条边全部归它，
   `ARCHIVED` 无出边，终态不可复活。
3. **追加 `SCENE.READ`**：只开写入口会得到一个写进去查不到的对象，也无法在故障时观察；
   与 `EPISODE.READ` 对称是入口自洽的最低要求，不是范围扩张。
4. **`SHOT.STATUS.CHANGE` 本轮不登记、不实现**：`shots.shot_revision_id NOT NULL`
   且指向 `shot_revisions`，而 `SHOT.REVISION.CONFIRM` 仍是未实现动作；此时登记状态变更
   只会得到「永远 404 的已实现动作」。Shot 域必须以 `CONFIRM + STATUS.CHANGE` 成对落地。
5. **父链冻结是逐级不变量**：新增子对象前按 `Project → Episode → Scene` 自顶向下取锁，
   并逐级判定冻结；只判直接父会让「上层已封存、下层还在长」悄悄成立。
6. **`Project.COMPLETED` 纳入冻结态**：判据不是「终态自然冻结」，而是状态图给不出
   `COMPLETED → ACTIVE` 的回路——内容已定稿，再建子对象只会产生永远推不动的孤儿行。
   冻结态不等于终态别名：`DRAFT / ACTIVE / ON_HOLD` 仍可建子对象。
7. **`BLOCKED` 不阻断场次归档**，但归档后镜头不得再推进到 `IN_PRODUCTION`。A 侧放行
   的正当性完全由 B 侧守卫兜底；`IN_PRODUCTION` 不能享受同一豁免——它归档那一刻缺口
   已经发生。阻断集合收敛为单源函数，守卫与触发器不再各自写状态字面量。

## 为什么这些是决策而不是进度

草案列出的是「缺入口」，真正危险的是缺口的**静默性**：

- 84 个动作里没有 `SCENE.*`，三张闸门全绿——因为反向完备性只遍历已登记的状态机；
- 批次五建成表与 12 条外键后，26 项行为验证证明了约束正确，却没有一项证明这张表能用；
- 只判直接父、只判归档侧、阻断集合在四处各写一遍——三类「只拦一半」在正常路径上
  都不会报错，失效形态是安静的。

所以本 ADR 记录的是**判据**：谁有权创建、哪条边归哪个动作、哪一层负责拦、
什么条件下允许例外。具体计数与行级状态不在这里维护，分别查
`contract_amendments.yaml` 与 `docs/audits/mig-recon/acceptance_ledger.md`。

## 顺序不变量（不可交换）

```text
输入校验 → 锁 Project → 锁 Episode → 幂等回读 → 组织键校验 → 冻结判定 → INSERT
```

- 锁必须在冻结判定之前，否则「读的时候未冻结、写的时候已冻结」仍有并发窗口；
- 幂等必须在冻结判定之前，否则归档后重放一个当时成功过的请求会变成 409，
  调用方永远无法确认首次是否成功；
- 加锁顺序固定自顶向下；反向取锁与只锁 Project 的 `EPISODE.CREATE` 相遇可能死锁。

`Scene.scene_id` 是 `entity_id` 的只读投影，不是第二份字段。两套标识各自生成就是
两套身份；数据库 CHECK 只能拦写入时不一致，拦不住代码里本来是两份。

## 归档不变量的落点

「归档场次下不得有在制镜头」是跨表存在性不变量，**不放在服务层再查一遍**：
服务层只能证明「查的瞬间成立」，且绕开服务层的写入不会被拦。判定放在数据库
守卫里，由 A/B 两侧共同构成：

- A 侧（`UPDATE scenes.status → ARCHIVED`）：存在 `IN_PRODUCTION` 镜头即拒绝；
- B 侧（`UPDATE shots.status → IN_PRODUCTION`）：父场次已归档即拒绝。

阻断集合由 `shot_planning.shot_archive_blocking_states()` 单源提供；
`BLOCKING_STATES ∪ NON_BLOCKING_STATES` 必须等于契约全部 Shot 状态且不交。
新增状态时两侧都红，逼出显式裁定；默认「不算在制」是最坏选择。

## 授权与闸门

- 授权面由 `db_grants.py` 矩阵单一维护，迁移必须显式声明 `tables=` 快照。
  迁移是时间胶囊：新表由新迁移授权，语句并集才等于矩阵。
- DATA-6 的判据按「折叠到角色 × 表 × 权限后的终态」比较，不再拿语句多重集硬比；
  分支链上的授权迁移必须参与回放，否则新表的 `REVOKE ALL ... FROM PUBLIC` 静默缺席。
- `Scene` 运行时转移表纳入 `contract_check.py` 的一致性检查；此前闸门全绿是正确行为
  （反向完备性只遍历已登记对象），入口落地后不再登记就是漏检。

## 影响

- 契约：先登记 `V205-R14-A13`，再同步 action registry / OpenAPI / permission matrix /
  event catalog / action implementations；未改动任何状态机边。
- 领域：`SceneStatus` / `Scene` / `SCENE_LIFECYCLE`；`PROJECT_LIFECYCLE.frozen_states`
  纳入 `COMPLETED`；`SCRIPT.IMPORT` 与 `SceneService` 均做父链双冻结。
- 运行时：`scene_sqlalchemy_repo.py` / `scene_service.py` / `scene_routes.py` /
  `SceneModel`，以及 `ownership.py` 的 Scene 归属解析。
- 迁移：批次七（阻断集合单源 + 裁定）与批次八（`scenes` 权限收敛）。
- 验证：`verify_batch7.py`、`test_scene_runtime.py`、`test_script_parent_freeze.py`、
  `test_shot_archive_scope.py`、`test_boundary_role_grants.py`。

## 未闭合（显式登记，不是遗漏）

1. Shot 域仍无创建/推进入口：`SHOT.REVISION.CONFIRM` 与 `SHOT.STATUS.CHANGE` 必须成对落地。
   （前置已就绪：批次十让 `shot_revisions` 对齐契约 schema、批次十一给三条引用边建复合外键；
   详见 `docs/audits/2026-11-21-shot-revision-schema-alignment.md`。运行时全栈仍是下一步。）
2. `shots.rework_basis_ref → workflow.rework_tasks` 的 DANGLING 已消除（批次九建表 + 复合外键）。
3. `GateInstance` 无 schema；`afs_audit` 的 SELECT 范围尚未逐项论证。
4. 目标验证库相对主链仍有 9 项 ACL 漂移（详见本轮审计报告），属历史积累，未在本 ADR 范围内消化。
5. `Scene` 的 `registration_status` 仍为 `PENDING_PHASE1_REVIEW`：本轮补入口，不改状态集。
