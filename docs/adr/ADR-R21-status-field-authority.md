# ADR-R21：双 status 字段的权威归属与信封哨兵

状态：已实施（契约层，2026-11-18）
关联：ADR-R20（状态枚举单一事实源）、ADR-R19（引用目标闸门）、8.11 / 8.13 / 21.1 / 32.4 正文权威段、C.3.1 通用信封
上游依据：项目所有者 2026-11-18 对 ADR-R20 台账的评审指令——「允许债务存在，不允许债务隐形；允许暂时不定义状态，不允许多个事实源同时拥有状态」。

## 一、先纠正一个统计口径

评审意见里的表格写的是「已有 Schema enum 5 / lifecycle_gap 19 / envelope_only 18 = 42」。
按当前 PR #14 分支实测，这个口径把**迁移前**的数字当成了迁移后的分类：

| 口径 | 评审表格 | 当前实测 |
| --- | --- | --- |
| state_machines 对象 | 42 | 43（本 ADR 补登 PrevizVersion） |
| schema 中带 `status` 的对象 | 未列 | 76 |
| lifecycle（有权威状态机且已建模） | 5 | 40 |
| lifecycle_gap（有裸 status、无状态机） | 19 | 19 |
| envelope_only（占位 status） | 18 | 17（PrevizVersion 归位） |
| schema 中 `status.enum` 的对象 | 5 | 57（40 生命周期 + 17 哨兵） |

「5」是 ADR-R20 记录的迁移前既有 enum 数（Scene / Shot / ShotBreakdownDraft / ShotRevision / Task），
不是当前分类。分类对象集与状态机对象集也不是同一个集合：42 个状态机对象里有 3 个没有 schema
（PublicationReview / CreatorShareLink / Pattern），而 19 + 17 个带 status 的对象根本不在状态机里。

## 二、四个双 status 对象的逐项裁定

裁定遵循所有者给出的三条口径：同一事实选唯一权威字段、不同过程保留各自语义、投影/兼容字段不得当权威。

| 对象 | 裁定 | 权威写入位置 | 非权威字段处理 |
| --- | --- | --- | --- |
| `ExternalToolAdapterManifest` | 同一生命周期事实（口径 1） | `lifecycle_status` | 信封 `status` 退役为哨兵；完整值集未由正文给出，登记 `value_set_status: pending_owner_ruling` |
| `ModelInvocation` | 三个正交业务过程（口径 2） | `submission_status` / `result_status` / `billing_status` | 三者不合并、不排序成一条链；信封 `status` 退役为哨兵 |
| `PrevizVersion` | 正文已给出权威六态（口径 1） | `status` | `purity_check_status` 是检查结果维度，`approved_at`/`approved_by` 是锁定证据；两者都不是第二套生命周期 |
| `QCIssue` | 审阅裁决不是生命周期链（口径 1） | `human_decision` / `disposition` | 信封 `status` 退役为哨兵；缺陷流转归 `ReworkTask.status`，不归 QCIssue |

裁定全文与逐项 `unblocks_when` 在 `contracts/enums/field_authority.yaml`，闸门校验其中每个字段
都真实存在、权威与非权威集合不重叠、四项论证齐全。`state_machines` 新增 `transitions_gap`
字段：允许状态来自正文，但正文没逐条授权的边不登记、不推定。

## 三、信封哨兵：堵住「无生命周期 ≠ 任意字符串」

C.3.1 要求 `status` 必填且未知枚举必须拒绝。此前把对象归为 `envelope_only` 后仍保留裸
`string`，等于一边承认没有生命周期语义，一边允许任意字符串写入——这是所有者点名的红线。

现在 `status_layers.envelope_only_sentinel: NO_LIFECYCLE` 是唯一合法值：

- 它不是生命周期状态，只表示「本对象没有领域生命周期，权威事实在别的字段上（或无）」；
- `gen_status_contracts.py` 给 17 个 `envelope_only` 对象的 `status` 生成单值 enum；
- `schema_enum_problems()` 把「裸 string」「多写生命周期值」都判为 FAIL；
- `taxonomy_problems()` 新增 `unblocks_when` 必填，17 个对象逐个写清哪项领域决策才能销项。

`lifecycle_gap` 不收 enum：它的生命周期是已知的，但权威状态集尚未登记，强行造 enum 等同于
用假值替换缺口。它继续以带完整债务字段的裸字符串存在，由台账双向校验。

## 四、非生命周期维度的边界

`status_layers.non_lifecycle_dimensions` 新增四组登记：适配器生命周期（REGISTERED / VERIFIED /
REVOKED）、模型调用三字段（目前只有 SUBMISSION_UNKNOWN 是正文点名值）、白模纯净度
（NOT_RUN / PASSED）、QC 人工结论（五类）。凡正文只给叙述未给字面值的（暂停/回滚/退役、
纯净度不合格），`value_set_status: pending_owner_ruling`，禁止自造 FAILED/SUSPENDED 之类枚举。

## 五、验收

- 四闸门全绿；`pytest` 全量通过，新增 9 个 ADR-R21 负向/正向用例；
- 状态机 43 对象 / 258 状态 / 174 转移；分类 40 lifecycle + 17 envelope_only + 19 lifecycle_gap；
- 信封 `status` 裸字符串在 17 个对象上全部消除（哨兵收口），`lifecycle_gap` 19 个仍为显式债务；
- 缺失 Schema 对象（PublicationReview / CreatorShareLink / Pattern / SharedReferenceAsset）继续单列
  `schema_gaps`，不因不在枚举迁移范围而被当作已闭合。

## 六、未闭合

- 四个对象的权威字段值集大多仍待所有者裁定（field_authority.unblocks_when 已写明条件）；
- PrevizVersion 的 5 条未授权转移边仍登记在 `transitions_gap`；
- 19 个 `lifecycle_gap` 与缺失 Schema 对象的独立建模，按所有者确认的顺序另行推进。
