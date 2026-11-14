# ADR-R08：模块执行闭环的定义与首次落地（AssetVersion + QCReport）

- 状态：Accepted（本机验证通过；含一次契约扩展「ASSET.VERSION.READ」，见 G2）
- 日期：2026-11-14
- 关联提交：R06 保留期闭环（`7bb8925`）、ADR-R07（`5263d14`）、契约资源闸门（`93eef5e`）
- 关联契约：`contracts/action_registry/action_registry.yaml:140-180`、
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

四个动作（均落 `require_role`；READ 为后补的契约扩展，见 G2）：

| 动作 | 路由 | 允许主体（契约原文） | 落点 |
|------|------|--------------------|------|
| `ASSET.VERSION.CREATE` | `POST /assets` | `Worker` / `Model Adapter` | `services/asset_service/api/asset_routes.py:86` |
| `QC.REPORT.CREATE` | `POST /assets/{asset_version_id}/qc-reports` | `QC Worker` / `受信检测服务` | `asset_routes.py:112` |
| `ASSET.VERSION.APPROVE` | `POST /assets/{asset_version_id}/approve` | `授权审核人` / `QC` / `导演（按职责）` | `asset_routes.py:136` |
| `ASSET.VERSION.READ` | `GET /assets/{asset_version_id}` | `授权审核人` / `QC` / `导演（按职责）` / `制片` | `asset_routes.py:151` |

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

## 3. 缺口处置：两个已关闭、两个仍拒绝猜测

### G1 旁支状态没有来源边（**仍未关闭，按 31.5 拒绝猜测**）

`state_machines.yaml:45` 只写"可进入 REJECTED / QUARANTINED / REWORK /
SUPERSEDED / ARCHIVED"，`:46` 明确"分支进入各旁支的具体前置条件正文未逐条给出"。
解析器只能得到正链 4 条边（旁支无 `X→REJECTED` 形式来源）。
因此运行时无法进入旁支——这不是遗漏，是**按契约边界拒绝猜测**。
补齐方式：契约正文补登记旁支来源边后，`ASSET_ALLOWED_TRANSITIONS` 随契约扩边，
闸门 `check_state_machines` 会强制实现与契约一致。

### G2 缺读能力：`ASSET.VERSION.READ`（**已关闭**）

原状：契约无该动作 → 按"未登记端点"闸门不能私加 GET → 资产只能写不能读。
处置：按现有 READ 范式（`PROJECT.READ` / `EPISODE.READ` / `SCRIPT.READ` 同构），
在契约三处同步登记，再实现——顺序不可颠倒，避免出现"端点先于契约"的既成事实：

- `contracts/action_registry/action_registry.yaml:162-171`（ACTIVE / LOW /
  `allowed_principals: 授权审核人/QC/导演（按职责）/制片` / 审计事件
  `ASSET_VERSION_READ` / 负向用例 `ACT-ASSET-NEG-003`），`total_actions` 80→81。
- `contracts/openapi/openapi.draft.yaml`（`/actions/asset.version.read`，GET，
  `x-afs-*` 字段与注册表逐项一致）。
- `contracts/runtime/action_implementations.yaml`（`GET /assets/{asset_version_id}`
  → `services/asset_service/api/asset_routes.py`）。
- 实现：`asset_routes.py:149-155`（`require_role("ASSET.VERSION.READ")`）；
  `service/asset_service.py` 的 `get_version` 为纯读（不改版本号、不写事件）。
- 边界：只返回版本信封与 `storage_ref` 引用，**不签发存储凭证**；租户外资产
  与不存在统一返回 `NOT_FOUND`，避免用 403/404 差异探测他人租户资产
  （`tests/unit/test_asset_api.py::test_read_missing_asset_is_404`）。

诚实标注：这是一次**契约扩展**（owner 明确要求补齐读能力），不是从正文推导
出来的既有条目；因此记录在本 ADR 而非静默改表。若后续正文（8.5）给出不同的
读口径，以正文为准并回改本条目。

### G3 `allowed_principals` 不可机读：带限定语的主体名（**已关闭**）

原状：`导演按职责`（无括号）被 `parse_allowed_principals()` 原样保留，得到
现实中不存在的角色名，真实「导演」无法批准。
处置：契约改写为 `导演（按职责）`——括号内是资质限定，解析器按既有规则剥离，
角色名回落为 `导演`；语义不变，只是让限定语回到它本来就该在的括号里。
回归锁：`tests/unit/test_asset_permissions.py`（4 个资产动作的主体名必须落在
已知角色集合内、APPROVE 解析结果必须恰为 `{授权审核人, QC, 导演}`、READ 不得
包含只写角色 `Worker`）；集成测试的批准主体已切回真实「导演」角色。

根治方向：契约把该字段改为结构化列表（`principals: [{role: 导演, condition: 按职责}]`
或独立 `role` + `qualifier` 字段），解析器随之升级，闸门校验"每个主体名必须能在
身份域角色字典里找到"。

### G4 `GOLD` 在运行时不可达

`ASSET.VERSION.*` 只有 CREATE / APPROVE，没有"晋升 GOLD"动作。
`APPROVED→GOLD` 边虽在转移表内，但无端点触发，GOLD 实际不可达。
需要契约层拍板：补 `ASSET.VERSION.PROMOTE_GOLD`（含准入条件：GOLD 配额/终审人），
或明确 GOLD 由离线治理任务推进。在此之前不补私有端点（否则又变成未登记端点）。

## 4. 影响

- 正面：资产域从空壳变成可读写、可审计、可回滚验证的闭环模块；
  授权落点闸门把"忘挂权限"从人工 review 风险降为 CI 失败。
- 负面/成本：多了一个 schema、两张表、四个动作和一条新闸门；
  READ 使动作总数 80→81（ACTIVE 36→37），台账与权限派生视图计数同步更新。
- 不做的事：不为旁支状态造猜测边；不把带限定语的主体名静默改名；
  不把 READ 做成"先上线端点、后补契约"。

## 5. 后续（按优先级）

1. G1 旁支来源边补登记 → 扩转移表 → 补旁支用例（REJECTED 需理由与证据）。
2. G4 GOLD 晋升动作，或明确 GOLD 由离线治理任务推进（当前不可达，已登记）。
3. G3 彻底结构化：`allowed_principals` 改为 `role` + `qualifier` 字段，
   解析器与闸门随之升级（当前修复只覆盖括号形式，文本仍是自由格式）。
4. READ 的列表/分页口径：当前仅单版本读取；批量查询需契约另立动作，
   不得在 GET 上加隐式过滤参数绕过动作登记。
