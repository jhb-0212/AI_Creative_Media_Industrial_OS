# S1 正式认可记录（R42 实施授权口径）

> 日期：2026-11-25 ｜ 性质：**Owner 表态的落纸**，不是新一份审查结论 ｜
> 登记：台账修订 `V205-R42-S1-AUTHORIZATION-ACK` ｜ 清单条目：`V-BLOCK-R42-IMPLEMENTATION-AUTH`

## 0. 一句话

Owner 于 2026-11-25 明确认可 S1 口径：**ADR-R42 正文保持历史原文不动；R42 实施状态以
2026-11-23 实施对账记录与 2026-11-24 授权对账记录为事实源**。该认可只确认「原始架构决策 /
后续实施授权 / 实际实施状态」三层的记录口径，不构成对未列事项的追认、不新增技术与业务授权、
不预授权未来变更。

此前状态是「有条件认可」（`2026-11-24-r42-authorization-reconciliation.md` §8.1），条件补齐后
Owner 未再表态，故 `[V-BLOCKED]` 一直成立。本记录是该表态的落点。

## 1. Owner 原话（逐字，不改写）

> 我明确认可 S1：ADR-R42 正文保持历史原文不动；R42 实施状态以 2026-11-23 实施对账记录及
> 2026-11-24 授权对账记录为事实源。该认可只确认原始架构决策、后续实施授权、实际实施状态三层
> 的记录口径，不构成对未列事项的追认，不新增技术或业务授权，也不预授权未来变更。
> PR-G-2、PR-G-3 的追认范围仍以 `V205-R42-P2-RATIFY` 为准。
> 同意按该口径解除 `V-BLOCK-R42-IMPLEMENTATION-AUTH`。

## 2. 四项前置的当前落点（为什么这一句已经足够）

`2026-11-24` 记录 §8.1 把解除条件定成四条可核验的前置，不是形容词：

| # | 前置 | 落点 | 状态 |
|---|---|---|---|
| P1 | 决策 / 授权 / 实施三层分列 | 同一记录 §9.1 的三层表（L1 / L2 / L3），并声明三层是**单向**关系 | 已闭环 |
| P2 | 授权时间、范围、PR、未完成事项逐项可追溯 | 同一记录 §9.2 逐项清单（C1–C7 / D2–D6 / PR-G-1..4，每行带授权时间、来源、提交、剩余事项） | 已闭环 |
| P3 | 附可核验的代码与测试证据 | §2.2 提交清单 + 迁移 id + 测试文件；本 PR 的 CI run 为旁证 | 已闭环 |
| P4 | 不得以「已合并」反推「已获批」 | §9.1 明确 L3 不产生授权效力；PR-G-2 / PR-G-3 的追认由 `V205-R42-P2-RATIFY` 单独承担 | 已落实 |

四项都不是本轮新补的，本轮补的只有一件事：**Owner 的表态本身**。这也是它此前一直
`BLOCKED` 的唯一原因——缺的不是证据，是一句话。

## 3. 落了什么

- 台账：`V205-R42-S1-AUTHORIZATION-ACK`（`approved_by` = Owner，`approved_at` = 2026-11-25，
  `source_commits` 指向本记录的落地提交）；
- 清单：`V-BLOCK-R42-IMPLEMENTATION-AUTH` 改 `LIFTED`，`lifted_scope: DECISION`，
  并保留 `remaining_gate`（见 §4）；
- 计数：**8 条 → blocked 0 / lifted 8**。清单首次没有仍阻塞的条目。

## 4. 这个认可不是什么（读出这四条才算读懂）

1. **不是「R42 全部实施已获授权」**：授权范围就是 §9.2 那张逐项表，表外的项不在其中；
2. **不是对未列事项的追认**：表外若有已实施的内容，仍需逐项追认（PR-G-2 / PR-G-3 就是这么补的）；
3. **不是 P2 范围的扩大**：PR-G-2、PR-G-3 的追认范围仍以 `V205-R42-P2-RATIFY` 为准，
   本记录不改变其边界；
4. **不是未来变更的预授权**：新的技术或业务变更仍要单独授权；
5. **不是「代码已合并 ⇒ 授权成立」**：P4 仍然成立，方向不可反。

另有一条容易读过头：`lifted_scope: DECISION` 而不是 `ALL`。解除的是「等 Owner 一句明确认可」，
这一句到了，所以解除；但「R42 之外、表之外的实施」仍需各自授权，故必须带 `remaining_gate`。
把它写成 `ALL`，等于宣称「R42 这件事整体放行」，那是这个认可没有说过的话。

## 5. 复核命令

```bash
python scripts/verification_blocks.py --check          # 8 条：blocked 0 / lifted 8
python scripts/verification_blocks.py --status lifted  # 逐条看 lifted_by / lift_evidence
python scripts/freeze_manifest.py --check              # 台账未破坏冻结面（54 条修订）
```

## 6. 同日三次计数变化（串读顺序）

| 时点 | 记录 | 计数 |
|---|---|---|
| 上午 | `2026-11-25-tc-decision-block-reconciliation.md` §7 | blocked 2 / lifted 6 |
| 下午 | `2026-11-25-freeze-resolution-origin-investigation.md` §8（TC 对账记录 §10 同步说明） | blocked 1 / lifted 7 |
| 晚 | 本记录 §3 | blocked 0 / lifted 8 |

三份记录各自标注时点，互不回改。任何一份单独引用都会给出当时正确、现在过期的数字；
当前状态以 `contracts/phase0/verification_blocks.yaml` 为准。
