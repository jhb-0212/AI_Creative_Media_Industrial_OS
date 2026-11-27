# 双向语义闸门：`reversibility_matrix.py --check --snapshot`

- 日期：2026-11-27
- 变更：把"P → upgrade → downgrade → D"的实测差异接入正式闸门，不再只做人工 triage。
- 输入：迁移语句盘点 JSON（`2026-11-27-identity-migration-inventory.json`）+ 往返快照 JSON（默认 `2026-11-27-wp04-fix-round.json`）。

## 1. 闸门回答的四个问题

| 方向 | 由谁回答 | 判据 |
|---|---|---|
| upgrade 引入了什么 | 源码集合比对 `build_matrix()` | 每条语句解析为 `(动作, 范围, 对象, 角色, 权限集, grant option)`；不对称即列出 finding |
| downgrade 是否撤回了升级引入的变化 | 源码配对 + 快照 | 升级 `GRANT` ↔ 降级 `REVOKE` 集合相等；否则 `restore_required` / `unproven_blocking` |
| downgrade 是否凭空创造父态不存在的东西 | 快照 `diff` | `no_restore_proven` 要求 `diff` 为空；非空且未被登记规则排除 → 阻断 |
| P 与 D 是否语义一致 | 快照 verdict | `no_restore_proven` 即 P==D；`restore_required` 即不一致且未修 |

## 2. 分类语义（闸门强制，不是注释）

- `no_restore_proven`：P 与 D 无语义差异。**`diff` 必须为空**，或只剩已登记的排除项。
- `restore_required`：存在应恢复未恢复的差异 → 阻断。
- `intentional_one_way`：必须有 `safety_contract` 字段（非空），否则阻断；没有证据的单向变化不得登记。
- `unproven_blocking`：阻断，不自动放行（#24 当前状态）。
- 任何非法/缺失 verdict、缺 `parent` 的条目 → 视为 `unclassified` 并阻断。

## 3. 排除规则（白名单必须逐条、可说明）

| 规则名 | 允许排除的内容 | 边界 |
|---|---|---|
| `pg_toast_oid` | `pg_toast` / `pg_toast_<oid>` 的 OID 抖动 | 只匹配 `pg_toast` 前缀；TOAST 名称里的 OID 无身份语义 |
| `acl_item_order` | 同一对象 ACL 的**条目顺序** | 只允许 `[acl]` 组内、且已按 `role=PRIV` 规范化排序后的顺序差异 |

规则实现是 `SNAPSHOT_IGNORE_RULES` 常量 + 逐条 `ignored: [{rule, item}]` 登记；**未知规则名不会生效**，差异仍会阻断。`test_snapshot_ignore_rules_are_narrow` 断言规则集合恰好是这两条。

## 4. 覆盖率规则

- 源码矩阵里任何仍标 `restore_required` 的 revision，必须在快照里出现且 verdict ∈
  {`no_restore_proven`, `intentional_one_way`}，否则阻断（防止"源码改回绿、库没验证"）。
- 快照里的 revision 必须存在于迁移链，否则阻断（防止数据源漂移）。
- 源码里 `unclassified` 的不对称项 → 阻断。

## 5. CLI 与输出

```
python scripts/reversibility_matrix.py --check \
  --inventory docs/audits/2026-11-27-identity-migration-inventory.json \
  --snapshot  docs/audits/2026-11-27-wp04-fix-round.json
```

输出 JSON 增加 `snapshot` 段：`counts`、`ignored`、`ignore_rules`、`blocks`、`source`。
当前读数：

```
classified={'no_restore_proven': 27, 'restore_required': 0, 'intentional_one_way': 0, 'unproven_blocking': 1}
[SNAPSHOT] counts={'no_restore_proven': 10, 'restore_required': 0, 'intentional_one_way': 0, 'unproven_blocking': 0} ignored=0
[BLOCKED] 待补证据: 8f3b2c1d4e5a
```

## 6. 已知边界

- 快照由 `%TEMP%\wp04_triage.py` 生成（一次性库，成功即删），不是仓库内可复现的 CI 步骤；
  CI 接入需要把"建库 → 迁移 → 快照 → 删库"做成脚本，当前未做。
- 快照的 `diff` 目前覆盖：对象存在性、ACL（有效权限集口径）、索引定义与名称、表列结构；
  未覆盖：触发器启用状态、RLS 策略表达式、函数定义的逐字比较、数据行级语义。
  这些应在下一轮扩展 triage 的采集面，而不是靠 `no_restore_proven` 一句话带过。
- 源码集合比对基于盘点 JSON（由 `scripts/downgrade_inventory.py` 生成），盘点本身需要与新迁移同步重跑。

## 7. 回归

`tests/unit/test_reversibility_matrix.py`（20 项）覆盖：集合对称/不对称、`ALL` 覆盖子集、
对象或角色被删只记 note、`RETIRED_ROLES` 重建、10 条快照闸门规则（未解释差异阻断、
已登记排除放行、未知规则不生效、`safety_contract` 必填、覆盖率、链外 revision 阻断）。

## 覆盖率三分法（补充规则）

闸门区分三种口径，禁止互相替代：

| 口径 | 判定 | 不满足时 |
| --- | --- | --- |
| 分类覆盖率 | 每条 asymmetric revision 都有 `class` | 阻断：`源码集合比对未分类` |
| 快照覆盖率 | 每条 asymmetric revision 在 snapshot 中有 P→upgrade→downgrade→D 记录 | 阻断：`缺少 P/D 往返快照证据` |
| 双向检查 | upgrade 变化 / downgrade 恢复 / 额外状态 / 父态一致 四项均有判定 | 任一非空即阻断 |

`restore_required` 需要快照 `verdict=no_restore_proven|intentional_one_way` 才算证明修复；静态分类不能让它变绿。

`SNAPSHOT_IGNORE_RULES` 只允许收紧到已证明无语义影响的差异（当前仅 `pg_toast_oid`、`acl_item_order`）；未知差异不得靠新增忽略项通过。每条忽略规则命中次数在 `--check` 输出中可审计（当前 `ignored=0`）。

`unproven_blocking` 永远阻断，不因裁决或授权自动降级为通过。
