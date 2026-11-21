# A-009 整改 · 修订来源追溯矩阵（证据映射，非台账修改）

- 日期：2026-11-22
- 审计对象：`contracts/phase0/contract_amendments.yaml` 的 21 条 `traceability_debt`
- 目的：把「债务」从「空 source_commits」落实为「逐条可复核的来源提交映射」
- 性质：**只读证据产物，不修改台账**。是否回填 `source_commits`、是否删除债务条目，
  由下一轮整改在独立分支执行，并走 `freeze_manifest --check` 复核。

## 0. 判定口径

每条修订的落地提交必须同时满足以下至少两项，才记为「已定位」（LOCATED）：

1. 提交消息明文写出修订编号（如 `A13`、`A21`）或对应 ADR 编号；
2. 提交 diff 实际包含该修订声明的契约/代码增量（用 `git log -S <关键串>` 命中）；
3. 提交时间与修订 `recorded_at` / `approved_at` 的时间线顺序合理。

只满足「提交消息相似」不满足 diff 内容的，不记为已定位——这正是 A-009 禁止的「凭
Commit Message 相似认定」。

## 1. 追溯矩阵

| 修订 | 落地提交 | 提交主题（摘要） | 佐证级别 |
|------|---------|------------------|----------|
| A2 | `a262a45` | feat(roles): 数据库角色拆分 + 进程密钥拆分 + 状态推进入口（F03/F05/F01） | 提交消息 + diff 明写 PROJECT.STATUS.CHANGE / EPISODE.STATUS.CHANGE |
| A3 | `8de1215` | feat(asset): F06 补 ASSET.STATUS.CHANGE 授权闭环 | 提交消息明写「A3 修订登记」 |
| A4 | `f79a40c` | feat(contract): 拆镜草案对象契约 ShotBreakdownDraft | 提交消息明写 ADR-R16 |
| A5 | `2192af9` | feat(contract): 镜头修订对象契约 ShotRevision | 提交消息明写 ADR-R17 |
| A6 | `07951cc` | feat(contract): 稳定逻辑镜头对象契约 Shot | 提交消息明写 ADR-R18 |
| A7 | `74a7497` | refactor(contract): 镜头字段归属裁决 | 提交消息明写「A7」 |
| A8 | `73ec613` | feat(contract): 场次聚合根契约 Scene | 提交消息明写「A8 / ADR-R19」 |
| A9 | `f81850a` | feat(contract): 状态枚举单一事实源与四层闸门 | 提交消息明写 ADR-R20 |
| A10 | `91215fd` | feat(contract): 双 status 字段权威归属与信封哨兵 | 提交消息明写 ADR-R21 |
| A11 | `0af85f7`、`37aee5f` | fix(contract): 补齐漏抄转移边（两轮评审） | 提交消息明写「A11」「PR #14 评审」 |
| A12 | `db7038d` | feat(audit): 状态迁移事实表落地 | 提交消息明写 D2 / ADR-R23 |
| A13 | `29b842a` | feat(project-planning): A13 Scene/Shot 运行时落地 | 提交消息明写「登记 V205-R14-A13 / A14」 |
| A14 | `29b842a` | （同 A13，同一提交内的对账根治段） | 提交消息明写「对账根治（A14，两处静默偏离 + 一处遗漏）」 |
| A15 | `460f1be` | fix(planning): SC-5 归档改父行锁重判，23505 映射逐路径收敛到 409 | 提交消息明写 SC-5 |
| A16 | `279fcf1`（实现）+ `d55ac41`（补登记） | Scene 运行时入口——创建/读取/状态推进 | 实现落于归属治理线 `279fcf1`；条目由 `d55ac41` 补登记为 A16 |
| A17 | `f7c0486`（实现）+ `d55ac41`（补登记） | Shot 域闭环——REVISION.CONFIRM + STATUS.CHANGE | 实现落于 `f7c0486`；条目由 `d55ac41` 补登记为 A17 |
| A18 | `8dfe25c` | fix(second-review): 第二轮审查整改——三个 P1 业务不变量 | 提交消息明写「第二轮审查整改」 |
| A19 | `07e304c` | feat(planning): 归档即冻结子集合——四条创建路径接线 | 提交消息明写 |
| A20 | `84940ef` | feat(contracts): 冻结策略进入状态机契约 | 提交消息明写 |
| A21 | `f064fb5` | feat(contracts): ShotSpec 契约补全 | 提交消息明写「A21」 |
| A22 | `20be170` | feat(shot-planning): ShotSpec 运行时落地 | 提交消息明写「A22」 |

## 2. 追溯过程中发现的、必须如实记录的事实

### 2.1 A14 与 A13 共用同一提交

`29b842a` 的提交消息明确写着「登记 V205-R14-A13 / A14」，且「对账根治（A14）」段
完整描述了 A14 的三项整改（编号唯一域收敛 / 身份单一下沉 / SC-5 归档前置条件）。

因此 A14 的落地提交与 A13 相同，是 `29b842a`。这不是推断，是提交消息的明文。

**回填时的处置**：A14 的 `source_commits` 应填 `['29b842a']`，与 A13 相同。两条修订
共享同一提交是历史事实，不能因为「看着重复」而给 A14 编造另一个提交。

### 2.2 台账修订编号存在历史重排

追溯发现，台账修订编号在 WIP 整合提交 `d55ac41` 发生了一次结构性重排：

- `d55ac41` 之前（`d55ac41^`）：台账只有 **A1–A15 共 15 条**，其中
  - A13 = 「Scene / Shot / ShotRevision / ShotBreakdownDraft / ReworkTask 运行时入口」
    （正式链版本，落地于 `29b842a`）
  - A14 = 「A13 定级复核：编号唯一域收敛…」（落地于 `29b842a`）
  - A15 = 「SC-5 并发窗口根治」（落地于 `460f1be`）
- `d55ac41`：**新增** A16（Scene 运行时入口）与 A17（Shot 域闭环）两条条目，
  把归属治理线的 Scene/Shot 运行时落地作为独立修订补登记。

这意味着：A16/A17 的 `retroactive: true` 标注是准确的——它们的**动作已被 A13 覆盖**
（SCENE.CREATE 等动作在 `29b842a` 就已在 action_registry 出现），A16/A17 是在
双线整合阶段补登记的、承载归属治理线决策理由与治理增量的修订。

### 2.3 A16/A17 的「实现提交」与「登记提交」分离

- A16 的实现（Scene 运行时 + 动作 + grants）落在 `279fcf1`（归属治理线），
  但该提交当时把它登记为 `V205-R14-A13`（旧编号）。
- A17 的实现（Shot 域闭环）落在 `f7c0486`，当时登记为 `V205-R14-A14`（旧编号）。
- 最终编号 A16/A17 由 `d55ac41` 在整合时补登记确立。

**回填时的处置**：A16 的 `source_commits` 应体现「实现 + 补登记」两条事实。
按台账字段语义（source_commits = 落地提交），核心实现提交是 `279fcf1`；
补登记提交 `d55ac41` 是台账自身的变更，不属「落地提交」。
建议 A16 填 `['279fcf1']`、A17 填 `['f7c0486']`，并把「编号曾为 A13/A14、
由 d55ac41 重排为 A16/A17」的历史事实写入条目 `reason` 或新增 `history` 字段，
而非塞进 `source_commits`。

## 3. 无法定位的条目

**无**。21 条债务全部有可复核的落地提交（见矩阵）。没有任何一条需要「编造 SHA」
或「接受残余风险」。

## 4. 下一步（不在此文档执行）

1. 在独立分支 `fix/a009-amendment-traceability` 上，按矩阵回填 21 条
   `source_commits`（A14 与 A13 同填 `29b842a`；A16/A17 分别填 `279fcf1` / `f7c0486`）。
2. 同步从 `traceability_debt` 清单删除这 21 条（`freeze_manifest` 双向校验会要求
   「补齐即删条目」，否则报陈旧债务）。
3. 保持 A1 的 `retroactive: true` 与 A16/A17 的 `retroactive: true` 不变。
4. 运行 `python scripts/freeze_manifest.py --check` 与 `python scripts/contract_check.py`
   确认闸门仍全绿、债务计数归零。
5. 更新 `baseline_ref.revision`（若回填本身不算新修订，则无需变更；若视为一条
   治理修订，须按 R14 规则登记 V205-R15）。

> 本矩阵是证据映射，供下一轮整改与独立复核使用。它本身不改变任何冻结事实。
