# 镜头域契约闭合提案（Shot domain contract closure）

状态：第一步已实施（2026-11-14，闸门口径已扩到 ACTIVE 并用显式债务承接；第 2~4 步阻塞于正文）
关联：ADR-032（AI 拆镜输出默认草案）、ADR-R07（ShotDraft 缺对象契约）
适用范围：仅契约层与闸门口径；不含运行时实现代码

## 一、结论

ADR-R07 记录的是「ShotDraft 无对象 schema」这**一个**缺口。本轮按 ACTIVE 全量复核后，
实际有 **9 个**资源类型既没有对象 schema、也没有豁免登记：

| 资源类型 | 引用它的 ACTIVE 动作 |
|---|---|
| `ShotDraft` | `SHOT.DRAFT.CREATE` |
| `ShotRevision` | `SHOT.REVISION.CONFIRM` |
| `ShotBudgetRequest` | `BUDGET.SHOT.REQUEST`、`BUDGET.SHOT.APPROVE` |
| `BudgetLedger` | `BUDGET.SHOT.APPROVE` |
| `GenerationTask` | `GENERATION.REQUEST` |
| `ProductionRoute` | `PRODUCTION.ROUTE.SELECT` |
| `CanonRevision` | `CANON.CHANGE.APPROVE` |
| `RightsCase` | `RIGHTS.STATUS.CHANGE` |
| `RightsManifest` | `RIGHTS.STATUS.CHANGE` |

已豁免的 4 个是核心信封对象：`Project`、`Episode`、`ScriptRevision`、`ScriptLock`。

## 二、根因

`scripts/contract_check.py` 的 `check_implemented_resource_schemas()` 只校验**已实现**动作
（当前 12 个）的资源类型。于是 ACTIVE 但未实现的 action 存在一条双向盲区：

- 「未实现」一侧由 `action_implementations.yaml` 的 reason 覆盖，闸门放行；
- 「已实现」一侧才检查 schema，而它们根本没实现，闸门不看。

ADR-R07 已指出「这条缺口能通过 CI，但它不是契约自洽，只是没检查」，
本轮实测证明受影响的不是一条，而是九条——**同一根因的九次复制**。

## 三、修复顺序（不做等价替换、不加第九个豁免掩盖）

1. **先改闸门口径**：把「只查已实现」改为「查所有 ACTIVE」，并保留两条附加规则：
   - 已实现动作的资源类型必须落在 schema（豁免仅限核心信封对象）；
   - 豁免条目若不被任何已实现动作引用即视为过期并报红（沿用现有规则）。
   改完后 CI 会立刻报出上述 9 条，缺口由静默变为显性。
2. **主链必需的两个对象补 schema**：`ShotDraft`、`ShotRevision`（见第四节）。
3. **补两个动作**，否则状态机没有入口（见第五节）。
4. **其余 7 个按域批量处理**，不得一次性全塞豁免：业务对象（`GenerationTask`、
   `ProductionRoute`、`ShotBudgetRequest`、`BudgetLedger`、`CanonRevision`、`RightsCase`、
   `RightsManifest`）都是可校验的业务对象，应补 schema；豁免只留给核心信封对象这一既有先例。

## 四、ShotDraft 字段契约（draft-07 片段）

命名遵循现有 schema 惯例：业务标识用 `<对象>_id`，引用用 `<对象>_ref` / `_refs`，
通用信封为 10 项（`entity_id`、`entity_type`、`schema_version`、`entity_version`、`status`、
`owner_module`、`created_at`、`created_by`、`updated_at`、`audit_ref`）。

```json
"ShotDraft": {
  "type": "object",
  "required": [
    "shot_draft_id", "project_id", "episode_id", "basis_script_revision_id",
    "shot_index", "source_type", "status",
    "entity_id", "entity_type", "schema_version", "entity_version",
    "owner_module", "created_at", "created_by", "updated_at", "audit_ref"
  ],
  "properties": {
    "shot_draft_id": {"type": "string", "description": "草案标识，创建后不可改"},
    "project_id": {"type": "string"},
    "episode_id": {"type": "string"},
    "scene_id": {"type": "string", "description": "可为空：剧本尚未切分场景时不填"},
    "shot_index": {"type": "integer", "minimum": 1, "description": "在所属 episode/scene 内的镜头序号"},
    "basis_script_revision_id": {"type": "string", "description": "本草案所依据的剧本版本；剧本变更后必须产生新草案，不得原地改写"},
    "source_type": {"type": "string", "enum": ["AGENT", "HUMAN", "IMPORT"]},
    "source_refs": {"type": "array", "items": {"type": "string"}, "description": "来源证据：Agent 任务引用、剧本片段定位、导入文件哈希"},
    "draft_payload": {"type": "object", "description": "拆镜草案内容（画面描述/景别/预估时长/台词/动作），不允许包含 locked_dimensions"},
    "draft_summary": {"type": "string", "description": "一句话摘要，供列表与人工确认界面使用"},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "模型置信度；HUMAN 来源时为空"},
    "confirmed_by": {"type": "string", "description": "CONFIRMED 时必填；不得为 Agent 主体"},
    "confirmed_at": {"type": "string", "format": "date-time"},
    "confirmed_shot_revision_id": {"type": "string", "description": "CONFIRMED 时必填，且必须等于同一次确认调用创建的 ShotRevision 标识"},
    "reject_reason": {"type": "string", "description": "REJECTED 时必填"},
    "superseded_by": {"type": "string", "description": "SUPERSEDED 时必填，指向取代它的新草案"},
    "status": {"type": "string", "enum": ["DRAFT", "CONFIRMED", "REJECTED", "SUPERSEDED"]},
    "entity_id": {"type": "string"},
    "entity_type": {"type": "string", "enum": ["ShotDraft"]},
    "schema_version": {"type": "integer"},
    "entity_version": {"type": "integer"},
    "owner_module": {"type": "string"},
    "created_at": {"type": "string", "format": "date-time"},
    "created_by": {"type": "string"},
    "updated_at": {"type": "string", "format": "date-time"},
    "audit_ref": {"type": "string"}
  }
}
```

### 状态机

```
DRAFT ──CONFIRM──> CONFIRMED ──SUPERSEDE──> SUPERSEDED(终态)
  │
  ├──REJECT──> REJECTED(终态)
  └──SUPERSEDE──> SUPERSEDED(终态)
```

- `CONFIRMED` 不是终态：后续草案可取代它（旧草案置 `SUPERSEDED`），但 `CONFIRMED` 记录本身不被覆盖。
- `REJECTED`、`SUPERSEDED` 为终态，按 31.5「终态不得原地恢复」，恢复走新建草案。

### 三条不变式（比字段更重要）

1. **确认即绑定**：`CONFIRMED` 必须携带 `confirmed_shot_revision_id`，且与同一次确认事务内创建的
   `ShotRevision` 标识相等；不一致即拒绝。
2. **正式对象的前置**：任何写 `ShotSpec` / `ShotVisualPlan` 的动作，前置必须是存在对应的
   `CONFIRMED` `ShotDraft`（ADR-032 的运行时落点）。人工直建路径如需保留，必须显式登记为
   独立动作并留审计，不得靠"没有草案也能写"默认放行。
3. **草案不越权定约束**：`draft_payload` 不得包含 `locked_dimensions`——锁定维度属于
   `ConstraintPack`，草案只能提出候选，不能自带约束。

## 五、缺失的确认动作（比字段更硬的缺口）

`SHOT.DRAFT.CREATE` 只产生 `DRAFT`。按 31.5「未登记转移一律拒绝」，
`DRAFT → CONFIRMED / REJECTED` 若没有对应 action，运行时根本无路可走——
ADR-R07 只提了对象 schema，遗漏了这一半。

建议新增两项（沿用现有 action 表格式）：

```yaml
- action_key: 'SHOT.DRAFT.CONFIRM'
  resource_type: 'ShotDraft'
  scope: 'project/episode/shot'
  risk_level: 'MEDIUM'
  token_required: true
  allowed_principals: '导演/制片'
  approval_requirement: '仅人工确认；Agent 不得确认自己产出的草案；必须携带 confirmed_shot_revision_id'
  audit_event: 'SHOT_DRAFT_CONFIRMED'
  phase: 'Phase 1'
  status: 'ACTIVE'

- action_key: 'SHOT.DRAFT.REJECT'
  resource_type: 'ShotDraft'
  scope: 'project/episode/shot'
  risk_level: 'LOW'
  token_required: false
  allowed_principals: '导演/制片'
  approval_requirement: '必须填写 reject_reason；驳回后不得原地改回 DRAFT'
  audit_event: 'SHOT_DRAFT_REJECTED'
  phase: 'Phase 1'
  status: 'ACTIVE'
```

## 六、闸门改造的负向用例（补进 tests/contract）

1. ACTIVE 动作引用未定义且未豁免的资源类型 → 报红（当前会命中上述 9 条）。
2. 已实现动作引用「仅登记未实现」的资源类型 → 报红（防止先实现后补契约）。
3. 豁免条目不被任何已实现动作引用 → 报红（沿用现有规则）。
4. 状态机中出现未登记转移 → 报红（沿用 31.5）。
5. 把 `ShotDraft` 伪造成已实现但 schema 缺失 → 报红（沿用 ADR-R07 已有用例）。

## 七、须与正文对齐的待确认项

本机未找到基线正文《AI影视工业化生产平台V2.0.5.md》，以下 5 点在实施前须逐条核对正文，
不得以本提案代替正文口径：

1. 8.x 拆镜条款是否已给草案状态集（若正文另有 `DISCARDED` 等状态，以正文为准）。
2. 正文对 `ShotRevision` 与 `ShotSpec` 的从属关系是否已有箭头（本提案按
   `ShotDraft → ShotRevision → ShotSpec` 的链式理解起草）。
3. 草案是否允许人工直建（若有，需对应独立动作与审计事件）。
4. `episode_id` 在正文中的正式名称与是否为必填归属键。
5. `draft_payload` 是否允许承载台词原文（涉及版权与原文保护，正文 26.x 有相关约束）。

## 八、落地记录（第一步：闸门口径）

**已实施（2026-11-14）**

1. `scripts/contract_check.py` 新增 `check_active_resource_schemas()` 并挂进 `main()`：
   - 口径从「已实现动作」扩到「全部 ACTIVE 动作」（当前 37 个 / 27 个资源类型）；
   - 缺口必须落在 `contracts/runtime/resource_schema_gaps.yaml`（每项含
     `referenced_by`、`reason`、`contract_ref`、`unblocks_when`），**未新增豁免条目**；
   - 双向校验：未登记缺口、已有 schema 仍挂债务（陈旧）、债务无人引用（孤儿）、
     字段缺失、`referenced_by` 与契约实际引用不一致，均报红。
2. 债务台账按实测登记 9 个类型（ShotDraft、ShotRevision、ShotBudgetRequest、
   BudgetLedger、GenerationTask、ProductionRoute、CanonRevision、RightsCase、
   RightsManifest），其中 `ShotBudgetRequest` 被 `BUDGET.SHOT.REQUEST` 与
   `BUDGET.SHOT.APPROVE` 两个 ACTIVE 动作引用，故为 10 组引用。
3. 新增 6 条单测（正向 + 清空债务报红 + 陈旧债务报红 + 孤儿债务报红 +
   缺字段报红 + `referenced_by` 撒谎报红）。

**未实施（阻塞原因）**：第 2~4 步（补 `ShotDraft`/`ShotRevision` schema、新增
`SHOT.DRAFT.CONFIRM`/`SHOT.DRAFT.REJECT`、其余 7 个业务对象补 schema）需要基线正文
《AI影视工业化生产平台V2.0.5.md》（AFS-ARCH-MASTER-V2.0.5）。该正文当前不在本机，
只有 V1.2 与本提案；按第七节「不得以提案代替正文口径」，本轮不写猜测字段。
正文到位后按第八节顺序执行即可，闸门会自动把「补了 schema 仍挂债务」判为陈旧。
