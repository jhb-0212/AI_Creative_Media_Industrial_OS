# TC-1~TC-5 决策阻塞对账：裁决已完成，执行门槛未解除

> 日期：2026-11-25 ｜ 范围：`contracts/phase0/verification_blocks.yaml`（对账前 7 条 BLOCKED，
> 对账后 2 条 BLOCKED / 6 条 LIFTED）
> 裁决登记：`V205-R44-TC-DECISION-BLOCK-LIFT`（`contract_amendments.yaml`）
> 复跑入口：`python scripts/verification_blocks.py --check`

## 1. 这份记录处理什么、不处理什么

**处理**：阻塞清单里有 5 条（TC-1~TC-5）记的是「等 Owner 在选项中裁定其一」，而 Owner 已于
2026-11-23 裁定并签署。清单没有同步，导致「还有 7 条阻塞」这个当前状态是错的。本条修正状态。

**不处理**：技术栈切换、PoC 执行、ADR-R44 正文、冻结基线。自始至终一个字都没动，也不需要动——
因为**裁决从来没有授权过这些**。

一句话口径：**清单问的是「还在等什么决定」，ADR 答的是「决定了什么、之后不许做什么」。两者
此前被读成同一件事，于是「已裁决」被当成「仍等待裁决」。**

## 2. 事实：清单与 ADR 的原文对照

| 清单条目 | 清单的 `unblocks_when`（原文） | ADR-R44 §4 的裁决栏 | 是否满足 |
|---|---|---|---|
| `V-BLOCK-TC-1` | Owner 在三选项中裁定其一 | **(b) 仅新流程引入 Temporal，R42 不迁** | 满足 |
| `V-BLOCK-TC-2` | 裁定 (a) 维持 16 或 (b) 立项 18 | **(b) 先做 PG18.7 全链 PoC，验证前继续 PG16** | 满足 |
| `V-BLOCK-TC-3` | 裁定 (a) 维持 Redis 7 或 (b) 独立 Valkey PoC | **(b) 独立 Valkey PoC（9.1.4 基准 / 9.2.0 单独测试），保留 redis-py，不切生产默认** | 满足 |
| `V-BLOCK-TC-4` | 裁定 (a) Storage Port 增量接入或 (b) 维持本地 FS | **(a) 通过现有 Storage Port 增量接入 S3，先做 S3/SeaweedFS 对比 PoC** | 满足 |
| `V-BLOCK-TC-5` | 裁定 (a) 物理拆分不改归属链或 (b) 维持现状 | **(a) 独立领域与 Schema，归属仍走 Scene→Episode→Project** | 满足 |

五项全部落在清单自己列的选项内，没有一项需要重新讨论。清单的解除条件写得是「Owner 裁定」，
不是「Owner 裁定后由复核方批准」——所以这五条的阻塞在 2026-11-23 就已经消失，不是今天才消失。

### 2.1 签署证据（不是转述，是可核对的原文位置）

- `docs/adr/ADR-R44-technology-and-infrastructure-baseline.md` §4「所有者裁决（七项，2026-11-23 签署）」
  表格 5 行；标题行本身即签署事实。
- 同文件 §8 生效条件表：条件 1「Owner 对七项 TC 逐项签署裁决」= **满足**；条件 4「若裁决涉及
  契约/迁移变更，冻结清单重签」= **N/A**（"七项裁决均为方向/PoC 授权，无一直接改契约或迁移"）。
  这一条正是「本次解除不需要重签冻结基线」的依据，而不是我事后判断。
- 同文件 §9.1「授权」：Owner 于 2026-11-23 指令「TC-1~7 七项是最优状态就签署」，采纳七项推荐裁决。
- 落地提交：`b6ab0aa9f564b1efddbfad4f3ba2b753e529aa61`（`docs(adr): 签署 ADR-R44 七项技术裁决（PROPOSED → DECIDED）`，
  改 `ADR-R44-VERSION.txt` / ADR 正文 / 决策矩阵三份）。
- 机读视图：`docs/architecture/technology-decision-matrix.md` 七项 `裁决状态 = DECIDED`、
  `PoC/立项状态 = NOT RUN`。

## 3. 判定：解除的是「决策阻塞」，不是「执行门槛」

清单此前把两层混在一条 `gap` 里。这次拆开，并且把「没解除的那层」写进条目本身
（`remaining_gate`），而不是靠读者记得去看 ADR。

| 条目 | 决策层 | 执行层（本次明确保留） | 来源 |
|---|---|---|---|
| TC-1 | 已裁决（b） | 不得迁移 R42 的业务事务/回执/幂等/补偿权威；全面迁移须另走 R42 正式修订 + 冻结重签 | ADR-R44 §4 TC-1 执行约束 |
| TC-2 | 已裁决（b） | PoC 在隔离库执行、禁挂 PG16 旧数据目录；切换须 TECH-01 五项全过 + ADR 修订 + 冻结重签 | §4 TC-2；§5.1 |
| TC-3 | 已裁决（b） | 先固化 `redis:7` 实际版本与 Digest；禁止同时换服务端/客户端/限流算法；PoC 不改 Compose 默认 | §4 TC-3；§5.2 |
| TC-4 | 已裁决（a） | 复用 `ObjectStoragePort`、不加 delete；Generation 入口对非 local 保持 fail-closed、禁止失败回落；切换须 TECH-04 七项全过 | §4 TC-4；§5.4 |
| TC-5 | 已裁决（a） | 必须先有 SHOT-01 归属裁决/迁移/无双写方案，不得直接搬代码；无双写且真实 PG 数据对账通过才切换 | §4 TC-5 |

现行技术栈因此**一项都没变**：PostgreSQL 16、Redis 7、`local://` 本地 FS、Outbox Saga、
无 Temporal 服务、无独立 Broker。PoC/立项状态仍是 `NOT RUN`——`NOT RUN` 不等于 `PASS`，
这次解除也不把它变成 `PASS`。

## 4. 为什么这不能读成「批准技术栈切换」

ADR-R44 §4.1 的原句是「**本次签署的是技术方向与 PoC 授权，不是技术栈切换**」，并明写：

> 任何信号若显示「裁决已签 ⇒ 可直接改生产配置」，即为误读，违反 D2/D5。

§4.2 又把两层做成一张表：七项裁决状态全 `DECIDED`，PoC/立项执行状态全 `NOT RUN`。
切换前四道门槛缺一即维持现行基线：真实交互 + 故障注入的 `[E5]` 运行证据、独立 ADR 修订 +
Owner 签署、冻结清单/契约派生文件同步重签、不与领域重构同 PR。

本次解除**只删掉「还在等裁决」这一句**，四道门槛一条不动，仍在 ADR 与决策矩阵里生效。

## 5. 为什么 S1（R42 实施授权口径）不随之解除

本条容易被误读，所以把反证写全——三处独立证据都指向「不在范围」：

1. **ADR-R44 §9.1 签署范围**（原文）：「**不在本次范围**：R42 实施授权口径（BOOT-01 登记的
   BLOCKED 项，仍待 Owner 单独裁决）、C7 QC 手动入队端点边界、D6 人工重放入口——三项保持
   原判定，本 ADR 不处理。」
2. **`technology-decision-matrix.md` 签署信息**同一句：「不在本次裁决范围（维持原判定）：
   R42 实施授权口径（BLOCKED）……」
3. **`contract_amendments.yaml` → `V205-R42-P2-BLOCK-LIFT` 残余风险**：P2 解除不覆盖 S1；
   且 `docs/audits/2026-11-24-r42-authorization-reconciliation.md` §14.2 已声明「不覆盖」。

结论：TC 解除不构成 S1 的签署证据，也不构成「S1 已被默示认可」。S1 仍按原口径等 Owner
明确认可「ADR-R42 正文保持历史原文不动、实施状态以 2026-11-23 实施对账 + 2026-11-24 授权
对账为事实源」——「有条件认可」不能被写成无条件正式认可。

## 6. 计数对账：17 / 19 / 20 / 23–25 是四个口径，不是一个事实的四个版本

原始冻结决议（`AFS-P0-FREEZE-2026-11-13-01`）的「引用 N 处」此前出现多个数字。逐项查清如下，
**结论是「17」没错，但它带一个隐含前提**；而 23/25 是检索范围被污染的结果。

### 6.1 可复跑的口径表

命令统一为 `git grep -c "AFS-P0-FREEZE-2026-11-13-01" <rev>`（只数 tracked 文件，逐文件求行数后求和）：

| 口径 | rev | 行 | 文件 | 说明 |
|---|---|---|---|---|
| 首次写下「17」的提交，含该审计自身 | `cd2a7ec` | 19 | 6 | 该审计自己贡献 2 行（状态引用 + 计数句） |
| 同上，**排除该审计自身** | `cd2a7ec` | **17** | 5 | 与原文「命中 17 处」精确吻合 |
| 当前 HEAD | `57bcc46` | **20** | 7 | = 17 + 2（该审计自身）+ 1（本清单自身） |
| 含 `backups/` 的 Nov17 legacy 基线副本 | 工作区 | 25 | 8 | `backups/` 被 gitignore，副本自身 5 行 |
| 含 `.pytest-basetemp` 测试产物 | 工作区 | 数百 | 数十 | 测试夹具复制了 `state_machines.yaml`/`object_schemas` |

**所以「17」的真实口径是：`git grep`、只数 tracked 文件、且排除写这句话的文档自身。**
这不是错，是一个没有写下来的前提。它在 `cd2a7ec` 上可精确复现（19 − 2 = 17），
在 HEAD 上则是 20，差额的两项都能点到具体文件。

### 6.2 上一轮口头说的「23」无法复现

必须如实记：我此前说的 23，既不是 20 也不是 25，在任一 rev 上都复现不出来——那是未记录口径的
估算。可复现的相邻值只有 20（tracked）与 25（含 `backups/` 副本）。**故 23 不作为基线**，
本记录以 20 为当前基线，并在清单里登记 `as_of_commit` 与命令，而不是只写一个数。

### 6.3 为什么这个数根本不该当判据

引用数是随审计增长而单调上升的量：写一份新审计、在清单里提一次，它就 +1。把它写进标题却不写
口径，等于埋一个必然漂移的断言——`17` 变 `20` 的原因里，有 3 行正是**记录这件事本身**带来的。
所以清单改为：标题不再带数字，数字移入 `reference_baseline`（带 `as_of_commit` + `command`）。
真正的缺口是「**原件 0 份**」，那个数不会因为谁写文档而变。

## 7. 状态口径（本次修正后）

| 分类 | 条目 | 状态 | 说明 |
|---|---|---|---|
| 已裁决、已同步解除（执行门槛保留） | TC-1~TC-5 | `LIFTED` + `lifted_scope: DECISION` | 决策不欠；PoC 仍是 `NOT RUN`，切换仍受四道门槛 |
| 仍待 Owner 提供或确认权威证据 | 原始冻结决议原件 | `BLOCKED` | 两条路径：交原件/正式副本，或指定项目外存档为权威并给可核验凭据 |
| 仍待 Owner 明确认可 | R42 实施授权口径（S1） | `BLOCKED` | 「有条件认可」≠ 正式认可；P2 解除与 TC 解除均不覆盖 |
| 已追认、已技术复核、已单独解除 | P2（`media_type` / `ABANDONED`） | `LIFTED` + `lifted_scope: ALL` | 见 `V205-R42-P2-BLOCK-LIFT` 与复核记录 §4 |

清单计数随之变为 **8 条：blocked 2 / lifted 6**。

两条读法必须同时成立，缺一条就会得出相反结论：

- **P2 已解除 ≠ 全局冻结治理完成**（还有 2 条）；
- **TC 已裁决 ≠ 相关技术可投产**（PoC 全 `NOT RUN`，四道门槛一条未过）。

## 8. 边界与代价

- **不改 ADR-R44 正文**，不改决策矩阵，不改任何技术栈配置，不动冻结清单。落纸范围是四份
  改动加本记录：`verification_blocks.yaml`（登记状态与门槛）、`contract_amendments.yaml`
  （裁决 `V205-R44-TC-DECISION-BLOCK-LIFT`）、`scripts/verification_blocks.py` 与
  `tests/unit/test_verification_blocks.py`（判据与防呆，见 §8.1）。清单是登记状态，不是裁决。
- **清单不复制裁决正文**。`remaining_gate.summary` 是索引，权威在 ADR §4 执行约束栏；
  两处若出现分歧，以 ADR 为准并修正清单。
- **台账合并读成本**：`V205-R42-D69-RESIDUAL-CLOSURE` 的 reason 写着「8 条：blocked 7 / lifted 1」，
  那是 2026-11-25 登记当时的正确表述，冻结不回改。只读该条会得出「仍有 7 条阻塞」的错误结论，
  必须与本记录及 `V205-R44-TC-DECISION-BLOCK-LIFT` 串读。该代价已写进新条目的残余风险。
- **不预授权**：本条不构成「以后 TC 类条目可自行标 LIFTED」的先例。凡 `lifted_scope: DECISION`
  的条目，必须在 ADR 里存在已签署的裁决栏，并由 `lifted_by` 指到真实台账修订。
- **冻结重签的时点**：本次不重签（ADR-R44 §8 条件 4 = N/A）。真正需要重签的是**将来某次切换**，
  那一份 ADR 上执行，不是这里。

### 8.1 判据与测试同步改了什么，为什么必须一起改

只把五个 `status` 从 `BLOCKED` 改成 `LIFTED` 是不够的——那样改完闸门仍是绿的，看不出任何区别。

**判据侧**（`scripts/verification_blocks.py`）新增三条，都是「状态与字段必须互证」：

1. `lifted_scope` 必填且只认 `ALL` / `DECISION`；
2. `DECISION` 必须带 `remaining_gate`（`summary` + `source`，`source` 必须是存在的文件），
   否则「已裁决」会被读成「已放行」；`ALL` 反过来不得带 `remaining_gate`，否则状态自相矛盾；
3. `lift_evidence` 文档正文必须**提到** `lifted_by` 那个 id。此前只校验 id 在台账里存在，
   于是可以写一条台账 + 一份不提它的证据文档，两处指针各自成立、互相不认。

**测试侧**（`tests/unit/test_verification_blocks.py`）拆了白名单。原来只有一份 `_REQUIRED_KINDS`，
它把「必须存在」和「必须仍阻塞」压成一条断言。后果是：TC-1~TC-5 合法解除时，唯一能过测试的
做法是把这五个 id **从白名单删掉**——而删掉之后，「它们还在清单里」这件事就再没人检查了。
现在拆成两条：`_REQUIRED_IDS`（八条都必须在，不管状态）与 `_MUST_STAY_BLOCKED`（本轮仍必须
阻塞的两条）。解除一条阻塞要显式改白名单，漏登记一条阻塞同样要显式改。

这三条是**针对本轮已发生的失真**加的，不是通用加固：本轮的真实故障是「状态与字段脱钩」——
裁决签了、清单没动，而闸门因为没有对应判据，两天里一直是绿的。

## 9. 复跑

```
python scripts/verification_blocks.py --check         # 状态/字段/指针一致性
python scripts/verification_blocks.py --status blocked # 应只剩 2 条
python scripts/verification_blocks.py --id V-BLOCK-TC-1
git grep -c "AFS-P0-FREEZE-2026-11-13-01" 57bcc46      # 20 行 / 7 文件
python scripts/contract_check.py                       # 含第 13 项「阻塞清单」
```

落纸顺序不可颠倒：**先台账修订 → 再证据文档（正文提到该 id）→ 最后改清单状态**。
反过来做，中间任何一个时刻跑判据都会红，且红的原因会被误读成「清单写错了」，
而真实原因是「裁决还没有落纸」。
