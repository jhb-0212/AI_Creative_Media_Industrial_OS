# ADR-R08：模块执行闭环的定义与首次落地（AssetVersion + QCReport）

- 状态：Accepted（本机验证通过；未提交到远端）
- 日期：2026-11-14
- 关联提交：R06 保留期闭环（`7bb8925`）、ADR-R07（`5263d14`）、契约资源闸门（`93eef5e`）
- 关联契约：`contracts/action_registry/action_registry.yaml:140-172`、
  `contracts/state_machines/state_machines.yaml:43-50`、
  `contracts/schemas/object_schemas.schema.json:1887`（AssetVersion）、
  `contracts/schemas/object_schemas.schema.json:6212`（QCReport）

## 1. 决策：什么叫「执行闭环」

从本 ADR 起，新增模块必须七环齐全，缺一环视为未完成（不是"部分完成"）：

| # | 环节 | 强制证据（缺一即不算闭环） |
|---|------|--------------------------|
| 1 | 契约定义 | `action_registry.yaml` 有 action（含 `scope`/`risk`/`allowed_principals`/`audit_event`），`object_schemas` 有对象定义，`state_machines.yaml` 有状态机 |
| 2 | 领域实现 | `domain/` 纯 Python 值对象 + 状态机，不 import 框架、不 import 其他业务模块 |
| 3 | 权限校验 | 路由挂 `require_role("<ACTION_KEY>")`；契约闸门 `check_implemented_permission_wiring()` 校验"端点存在但没有授权"这一沉默缺口 |
| 4 | 数据持久化 | 独立 schema + 迁移 + RLS + 乐观锁列；`alembic check` 无漂移 |
| 5 | 事件驱动 | 状态变更与审计事件同事务登记（Outbox），事件类型 = 契约 `audit_event` |
| 6 | 自动化测试 | 纯领域单测 + service 单测（fake 仓储）+ 真实数据库 API 集成测试，含**负向用例**（403/409/422） |
| 7 | 质量验收 | `contract_check.py`、`ruff`、`mypy`、`pytest`、`alembic check` 五道闸门全绿 |

### 为什么把"授权落点"做成闸门而不是口头约定

运行时覆盖闸门只证明"端点存在"，不证明"端点受控"。一个忘写 `require_role`
的端点照样能调用，而 CI 全绿 —— 这正是"声明 ≠ 执行"的沉默缺口类别。
故新增 `scripts/contract_check.py:612` 的
`check_implemented_permission_wiring()`：对每个 implemented action，
断言其台账 `module` 文件存在且源码含 `require_role("<KEY>")`
（`:639`），并已挂入 `main()`（`:672`）。

诚实边界：这是源码文本级校验，只证明"键被引用"，不能证明该装饰器挂在正确
的路由函数上；真正的证明是集成测试里的 403 用例（见 `tests/unit/test_asset_api.py`）。

## 2. 首次落地：资产域（AssetVersion + QCReport）

选定理由：资产是百集生产的主干数据，`services/asset_service` 原本是空壳；
且 `AssetVersion` / `QCReport` 在 `object_schemas` 有完整定义，`AssetVersion`
主链状态机可完整解析（不是 ADR-R07 那种"无 schema 只能拒绝猜测"的模块）。

三个动作（全部 HIGH/MEDIUM 风险，均落 `require_role`）：

| 动作 | 路由 | 允许主体（契约原文） | 落点 |
|------|------|--------------------|------|
| `ASSET.VERSION.CREATE` | `POST /assets` | `Worker` / `Model Adapter` | `services/asset_service/api/asset_routes.py:86` |
| `QC.REPORT.CREATE` | `POST /assets/{asset_version_id}/qc-reports` | `QC Worker` / `受信检测服务` | `asset_routes.py:112` |
| `ASSET.VERSION.APPROVE` | `POST /assets/{asset_version_id}/approve` | `授权审核人` / `QC` / `导演按职责` | `asset_routes.py:136` |

闭环证据（file:line）：

- 领域与状态机：`services/asset_service/domain/state_machine.py:21-33`，
  只登记 4 条有来源边的转移；`GOLD` 无出边（`:27`）。
- 用例规则：`services/asset_service/service/asset_service.py:64/150/235`
  （幂等 create、首个报告推进 `GENERATED→TECHNICAL_QC`、
  批准时先落 `HUMAN_REVIEW` 再 `APPROVED`、职责分离、证据强制）。
- 持久化：`data/migrations/versions/c1d2e3f4a5b6_create_asset_service_tables.py`
  （`asset_versions` 单主键 `entity_id`：`:71`；`qc_reports` 以 FK 指向它：`:90`；
  两表 RLS + `app_role` 授权）。
- 路由挂载：`apps/api/main.py:66`。
- 测试：`tests/unit/test_asset_domain.py`（纯规则）、
  `tests/unit/test_asset_service.py`（fake 仓储）、
  `tests/unit/test_asset_api.py`（真实 PostgreSQL + 真实 JWT，
  含 403 / 409 / 422 三条负向用例、e2e 生命周期换会话直读回验证 `entity_version=4`）。

## 3. 已识别但**拒绝猜测**的缺口（按 31.5「未登记一律拒绝」）

### G1 旁支状态没有来源边

`state_machines.yaml:45` 只写"可进入 REJECTED / QUARANTINED / REWORK /
SUPERSEDED / ARCHIVED"，`:46` 明确"分支进入各旁支的具体前置条件正文未逐条给出"。
解析器只能得到正链 4 条边（旁支无 `X→REJECTED` 形式来源）。
因此运行时无法进入旁支——这不是遗漏，是**按契约边界拒绝猜测**。
补齐方式：契约正文补登记旁支来源边后，`ASSET_ALLOWED_TRANSITIONS` 随契约扩边，
闸门 `check_state_machines` 会强制实现与契约一致。

### G2 缺读能力：没有 `ASSET.VERSION.READ`

`action_registry` 里不存在该动作，`openapi.yaml` 也无 GET 端点。
按"未登记端点"闸门（`check_runtime_contract_coverage`），私加 `GET /assets/{id}`
会被判 FAIL。因此**当前资产查询只能靠数据库直读**，运行时无读路径。
这是能力缺口，不是实现瑕疵；补法唯一：先登记契约动作（含 scope/risk/审计），再实现。
对百集生产这是优先级最高的下一步（生产系统不能只能写不能读）。

### G3 `allowed_principals` 不可机读：带限定语的主体名

`action_registry.yaml:156` 写 `授权审核人/QC/导演按职责`。
`parse_allowed_principals()`（`services/identity_access/action_registry.py:40-52`）
只按 `/` 切分并剥离括号内容，故得到集合 `{"授权审核人","QC","导演按职责"}`。
`导演按职责` 是"导演按职责"的语义限定，不是任何 JWT 里的真实角色字符串——
于是"导演"实际上无法批准，而"角色名叫 `导演按职责`"的人才能批准。
本 ADR 不做文本猜测（例如擅自剥掉"按职责"），集成测试改用契约里明确的
`QC` 角色批准（`tests/unit/test_asset_api.py:49-53` 注释已说明）。
根治方向：契约把该字段改为结构化列表（`principals: [{role: 导演, condition: 按职责}]`
或独立 `role` + `qualifier` 字段），解析器随之升级，闸门校验"每个主体名必须能在
身份域角色字典里找到"。

### G4 `GOLD` 在运行时不可达

`ASSET.VERSION.*` 只有 CREATE / APPROVE，没有"晋升 GOLD"动作。
`APPROVED→GOLD` 边虽在转移表内，但无端点触发，GOLD 实际不可达。
需要契约层拍板：补 `ASSET.VERSION.PROMOTE_GOLD`（含准入条件：GOLD 配额/终审人），
或明确 GOLD 由离线治理任务推进。在此之前不补私有端点（否则又变成未登记端点）。

## 4. 影响

- 正面：资产域从空壳变成可调用、可审计、可回滚验证的闭环模块；
  授权落点闸门把"忘挂权限"从人工 review 风险降为 CI 失败。
- 负面/成本：多了一个 schema、两张表、三条 HIGH 风险动作和一条新闸门；
  读能力缺失会让前端/调度端暂时只能写不能查（G2）。
- 不做的事：不为旁支状态造猜测边；不为读能力走私端点；不把带限定语的
  principal 静默改名。

## 5. 后续（按优先级）

1. 契约登记 `ASSET.VERSION.READ`（含分页/租户过滤口径）→ 实现 GET → 补负向测试。
2. G3 契约字段结构化 + 解析器升级 + 闸门校验主体名可解析。
3. G1 旁支来源边补登记 → 扩转移表 → 补旁支用例（REJECTED 需理由与证据）。
4. G4 GOLD 晋升动作或离线治理任务的明确归属。
