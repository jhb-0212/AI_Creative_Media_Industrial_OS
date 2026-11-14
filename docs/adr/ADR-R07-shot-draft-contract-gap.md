# ADR-R07：镜头域未打通的原因是 ShotDraft 缺对象契约

状态：已识别，未实施（2026-11-14）
关联：ADR-032（AI 拆镜输出默认草案）、31.6 Action Registry、C.3 新增领域对象数据契约

## 结论

`SHOT.DRAFT.CREATE` 现在**不能**按「已实现」登记。不是运行时能力不够，而是契约层
缺一个对象定义：动作声明 `resource_type=ShotDraft`，但对象 schema 里没有
`ShotDraft`，只有 `ShotSpec` / `ShotVisualPlan` 等相邻对象。

在这种情况下写代码只有两种结局：
1. 自己发明字段 → 与后续 C.3 正式定义冲突，必然返工；
2. 把 ShotDraft 硬映射到 ShotVisualPlan → 把「草案」与「已确认的视觉方案」混为一谈，
   正好违反 ADR-032「AI 拆镜输出默认草案、人工确认前不得写正式对象」。

两者都是拍脑袋，所以本轮不做，先把缺口钉死。

## 证据（file:line）

- `contracts/action_registry/action_registry.yaml:63-73`
  `SHOT.DRAFT.CREATE`：resource_type `ShotDraft`，scope `project/episode/scene`，
  LOW，ACTIVE。
- `contracts/openapi/openapi.draft.yaml:245-278`
  `/actions/shot.draft.create` 的请求体只引用 `ActionRequestEnvelope`，
  **没有** ShotDraft 的字段 schema；响应是通用 `ActionAccepted`。
- `contracts/schemas/object_schemas.schema.json`
  `$defs` 中存在 `ShotSpec`、`ShotVisualPlan`、`ContinuityConstraint`、
  `ConstraintPack` 等，但**没有** `ShotDraft`。
  （核验脚本：`python -c "import json;...;print([k for k in defs if 'Shot' in k])"`
  输出 `['ShotProductionProfile','ShotResourceAllocation','ShotBudgetLedger',
  'ShotTierChangeRequest','ReverseShotSegment','ShotVisualPlan','ShotSpec',
  'ContinuityConstraint','ConstraintPack']`。）

即：契约三件套（动作台账 / OpenAPI / 对象 schema）里，第三件缺了这一项。
现有 `scripts/contract_check.py` 校验的是「ACTIVE action 要么实现、要么带 reason
登记未实现」，不校验 resource_type 是否在 schema 中有定义——所以这条缺口能通过
CI，但它不是「契约自洽」，只是「没检查」。

## 决策

1. **不实现** `SHOT.DRAFT.CREATE`，在 `contracts/runtime/action_implementations.yaml`
   保持 `not_implemented`，reason 指向本 ADR。
2. **先补契约**：按 C.3 新增领域对象数据契约补 `ShotDraft` 定义（至少需要：
   归属键 project_id/episode_ref/scene_ref、镜头序号、草案内容引用、来源
   （Agent/人工）、DRAFT→CONFIRMED/SUPERSEDED 的状态集合、通用实体信封字段），
   再实现运行时。
3. **补一条闸门**（下一轮）：`contract_check` 增加「ACTIVE action 的
   resource_type（按 `/` 拆分）必须在 object_schemas 中有定义，或显式登记豁免」。
   否则同类缺口会继续以其它域的形式出现。

## 影响

- 镜头域仍是 0 个已实现 action；`SHOT.REVISION.CONFIRM`、`SHOTSPEC.LOCK` 依赖
  ShotDraft/ShotSpec 的版本与审批语义，同样不能先做。
- 已实现的 8 个 action 不受影响（它们的 resource_type 在 schema 中有定义）。
