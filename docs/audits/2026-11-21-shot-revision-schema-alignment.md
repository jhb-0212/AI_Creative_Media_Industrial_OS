# Shot 域根因前置：修订表对齐契约 + 三条真实引用边落地

> 日期：2026-11-21　验证实例：`afs-step-feat` @ 15444
> 分叉库：`ai_film_industrial` → `o5c6d7e8f9a0`　生产 `15432` / `ai_film_verify` **未触碰**

## 一、为什么说这是 Shot 域的根因，而不是又加一层

上一轮报告把 `SHOT.REVISION.CONFIRM` 未实现登记为"镜头域断头"的直接原因。
本轮实库核对（`shot_planning.shot_revisions` 只有 21 列）发现更靠前的一层：

- 契约 `ShotRevision.required` 有 30+ 字段，表里 `description` / `narrative_intent` /
  `risk` / `intent_ref` / `confirmed_by` 等 20 个**根本不存在**；
- 表里留下的 `shot_description` / `intent_summary` / `revision` / `episode_ref` /
  `scene_ref` 是草案期列名，不在契约对象里；
- 旧列 `episode_ref` / `scene_ref` 与已回填的 `episode_id` / `scene_id` 同时存在，
  是两套可以互相矛盾的"同一件事"。

在这种表上直接写 `CONFIRM` 服务，只会得到"服务报成功、库里没有契约要求的事实"——
比不做更坏。ADR-R17 §缺口 2 其实早已裁定"没有运行时，也没有修订表"，本轮是把
这个登记了很久的根因真正消掉，而不是在症状处加 if。

## 二、批次十（`n4b5c6d7e8f9`）：表对齐契约

### 存量夹具不编造引用

唯一存量行 `rev-gt-mismatch` 是为外键验收造的夹具（`created_by='backfill'`），
不是人类确认的修订。而 `script_revisions` / `constraint_packs` 两表均 0 行，
没有真实锚点可指。若给引用列填一个 `LEGACY_*` 假 id 再建外键，就是**造假的
引用事实**；若干脆设成普通可空列，则是偷偷放宽契约的 required。

采用第三条路：引用列（剧本依据 / 约束包）保持 nullable，但用**带机检前提的
CHECK** 约束新写入——

```sql
ck_sr_basis_script:
  confirmed_by = 'LEGACY_BACKFILL' OR basis_script_revision_id IS NOT NULL
ck_sr_constraint_pack:
  (applicability = 'BOUND' AND pack_id IS NOT NULL)
  OR (applicability = 'NOT_APPLICABLE' AND pack_id IS NULL)
```

存量行的 `confirmed_by='LEGACY_BACKFILL'` 哨兵明确标记"这不是人类确认修订"，
任何下游若把它当 Gate 1 准入依据，会先读到这个哨兵。前提可机检、可失效：
新写入去掉哨兵就必须提供真实依据。这正是"豁免必须带可机检前提"的落地。

其余 20 列补齐后 `NOT NULL` + 12 条枚举/结构 CHECK，草案期三列退役。
所有约束直接建成 validated（存量回填后已满足），不伪造"待验证"状态。

### 变异证据（9/9）

- B1/B2 契约 required 落表且 NOT NULL 口径双向一致（身份投影 `shot_revision_id`
  是唯一显式例外，与 `Scene.scene_id` 同一条铁律）；
- B3 14 条 CHECK 全部 convalidated；
- B4 存量夹具按真实旧值映射 + 哨兵留痕；
- B5 非法枚举 `L9` 被 CHECK 拒且回读未变；B6 必填写 NULL 被拒且回读未变；
- B7 按契约全字段写入，影响行数 1 且回读生效；
- B9 新写入清空剧本依据被 `ck_sr_basis_script` 拒（仅 legacy 可空）；
- B8 downgrade 恢复草案列、upgrade 可重放。

## 三、批次十一（`o5c6d7e8f9a0`）：三条真边不是 NonRef

新增列让归属闸门报 6 条 UNCLASSIFIED，逐条核过后分两类：

| 类别 | 列 | 处置 |
| --- | --- | --- |
| 真实归属边 | `basis_script_revision_id` → ScriptRevision | 建四列复合外键 |
| 真实归属边 | `constraint_pack_id` → ConstraintPack | 建四列复合外键 |
| 真实归属边 | `supersedes_shot_revision_id` → 自表 | 建自引用四列复合外键 |
| 合法非边 | `intent_ref` / `source_draft_ref` | `embedded_object`（jsonb 内嵌 id+版本） |
| 合法非边 | `location_ref` | `soft_pointer`（外部主数据，不决定归属） |

把三条真边登记成 NonRef 就是把"跨项目借用剧本依据/约束包/上一版"从判据里删掉，
属于洗白。故先给两个目标表补四列唯一键（单列主键证明不了"同项目同租户同组织"），
再挂 `NOT VALID` 复合外键 → 存量订正 → `VALIDATE`。

### 变异证据（6/6）

V3 合法引用写入影响行数 1 且回读生效；V4 用另一项目的真实剧本版本（同租户同组织）
借用，被 `fk_sr_basis_script` 以 23503 拒绝且回读未变；V5 不存在的版本同样被拒；
V6 外键可逆。

## 四、全量验证（本轮复跑）

| 闸门 | 结果 |
| --- | --- |
| 批次十 / 批次十一 | 9/9、6/6 |
| 契约 `contract_check.py` / 架构 | 全 PASS / R1~R5 全 0 |
| 全量 `tests/unit` | 827 passed |
| 归属一致性（真实库） | FAIL 0、DANGLING 0、EXEMPT 3、PASS 26 |
| 批次六 / 批次七 / 批次九 | 13/13、10/10、8/8 |
| 验收台账 / ACL / 守卫覆盖 | 全 PASS / 漂移 0 / 全 PASS |

批次七按设计要求模板库停在"批次六基线"；分叉库已推进到批次十一，故用一次性
回退库（`rewind_to(j0e1)`）作前提补跑 10/10，未改脚本前提。

## 五、如实保留

- **Shot 运行时入口仍未落地**：`SHOT.REVISION.CONFIRM` / `SHOT.STATUS.CHANGE`
  仍是 `not_implemented`。本轮消除的是"没有可用修订表"这一前置根因；运行时
  全栈（领域 / 仓储 / 服务 / API / 授权 / 契约登记）是紧接的下一步。表已就绪，
  不会再出现"服务声称成功、库没有事实"的假绿。
- **草案列 `episode_ref` / `scene_ref` 暂留**：批次五回填逻辑曾引用，本批不删以
  避免范围扩张；它们已被登记为 `soft_pointer` 非边，归属由 `episode_id` /
  `scene_id` 承担。
- **`GateInstance` 无 schema / G9 授权边界**：仍是既有显式债务，本轮未涉及。

## 六、可复现命令

```bash
./.venv/Scripts/python.exe docs/audits/mig-recon/verify_batch10.py
./.venv/Scripts/python.exe docs/audits/mig-recon/verify_batch11.py
./.venv/Scripts/python.exe docs/audits/mig-recon/verify_ownership_consistency.py \
  --db ai_film_industrial --port 15444
```
