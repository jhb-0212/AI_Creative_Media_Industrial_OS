# ADR-R28：审批目标资源归属的跨域只读端口

- 状态：已接受（Phase 1 实现侧 ADR，随代码提交生效）
- 日期：2026-11-17
- 关联：`ADR-R16`（数据库运行身份按业务域拆分）、`ADR-R24`（§4 未闭合项 1 的同源问题：
  跨域只读校验需要结构化的端口，而不是域间直读）、`ADR-R27`（镜头域独立后新增两类可解析资源）、
  第四轮审查报告 R4-2（跨项目审批绕过）
- 涉及：`services/workflow/domain/ports.py`、`services/workflow/service/approval_service.py`、
  `services/workflow/api/approval_routes.py`、`apps/api/resource_ownership.py`、`apps/api/main.py`、
  `tests/conftest.py`

## 1. 背景

R4-2 修复时已登记一条架构债务：`WORKFLOW.START_APPROVAL` 发起票据时会校验
目标动作状态 / 资源类型 / 发起人对票据项目的权限，但**不校验目标资源本身**——
resource_id 可以是不存在的资源、或属于别的项目的资源。把「审批」建立在
幽灵资源上，整条审批链就退化成橡皮图章。

当时不做的理由是结构性的：workflow 的数据库角色（`svc_workflow`）读不到
asset / shot_planning 的表（ADR-R16 角色隔离），而直接 import 对方仓储违反
R2（14.2 数据所有权：模块间只经 API/命令 + 事件交互）。缺一个不破坏所有权
边界就能问「这个资源属于哪个项目」的通道。

## 2. 决策：六边形端口 + 组合根适配器

**workflow 域定义端口，组合根提供实现**：

- **端口**（`services/workflow/domain/ports.py`）：
  `ResourceOwnershipPort.can_resolve(resource_type)` +
  `resolve(tenant_id, organization_id, resource_type, resource_id) -> ResourceOwnership | None`，
  `ResourceOwnership` 只含 `project_id` 一个字段——归属解析输出的是
  「归属结论」这个最小事实，不是业务数据，不构成数据泄漏通道。
- **适配器**（`apps/api/resource_ownership.py`）：登记式路由
  `resource_type → (DatabaseDomain, 只读仓储类)`，当前覆盖
  DeliveryManifest（asset 域）与 ShotDraft / ShotRevision / ShotSpec /
  ShotVisualPlan / ConstraintPack（shot_planning 域）。查询用**所有者域
  自己的连接池与数据库身份**（`svc_asset` / `svc_shot_planning`）——读永远
  经过所有者，workflow 不获得任何他域表权限；跨服务 import 只出现在
  组合根（R1 允许 apps → services）。
- **装配**：`apps/api/main.py` lifespan 在池就绪后挂
  `app.state.resource_ownership_port`；`approval_routes` 经 `Request.app.state`
  取端口，缺失即 fail-closed（INTERNAL_ERROR，拒绝发起）。测试侧由
  conftest 的 `db_pools` 镜像同一装配，生产与测试走同一实现。

## 3. 服务层失败语义（fail-closed，错误码与既有域口径一致）

| 情形 | 结果 |
|---|---|
| 资源类型无解析器 | 422 `VALIDATION_ERROR`（「查不了」≠「查不到」，明确拒绝盲发） |
| 资源在租户内不存在 | 404 `NOT_FOUND` |
| 归属项目 ≠ 票据声明项目 | 422 `VALIDATION_ERROR`（与 shot/asset 域归属不一致同口径） |
| 端口未装配 | 500 `INTERNAL_ERROR`（进程装配不完整，拒绝服务） |

归属解析按**租户全域**注入 RLS 上下文（项目范围 `*`）：只有看得见跨项目行，
才能区分「不存在」与「在别的项目」；适配器身份是 `ownership-resolver`
探针（AGENT 主体、无角色、无能力凭证），不是任何请求主体的代理。

校验顺序：绑定一致性 → Phase Gate → 资源类型 → **归属校验（新增）** →
`authorize`（动作级 + 资源级）。归属校验在授权判定之前，因为它是请求
事实校验，不依赖发起人身份。

## 4. 为什么不是其他方案

- **workflow 直读他域表**：违反 ADR-R16（角色隔离）+ R2（架构闸门直接红）。
- **事件驱动异步校验**：票据是同步写入的决策对象，「先发票据、事件到了再作废」
  让无效票据在窗口期内真实存在，比拒绝发起更糟。
- **把归属塞进 action_registry 静态登记**：归属是运行时事实（资源随时新建），
  静态表只会造成第二份真相。
- **PLATFORM_SERVICE_DEPENDENCIES 白名单放行 identity_access 装配适配器**：
  该白名单的唯一理由是认证装配；把业务数据读取塞进去会把例外变成后门。
  组合根（apps/api）才是这类跨域装配的正确位置。

## 5. 闸门证据

| 性质 | 证明方式 |
|---|---|
| 幽灵资源拒绝 | `tests/unit/test_approval_service.py::test_start_rejects_resource_missing`（404）|
| 跨项目拒绝 | 单测 `test_start_rejects_cross_project_resource` + 集成 `tests/unit/test_approval_api.py::test_cross_project_resource_is_rejected`（真实 RLS 下两域真实仓储）|
| 无解析器 fail-closed | `test_start_rejects_unresolvable_resource_type` |
| 主链不误伤 | `test_start_passes_when_resource_owned_by_ticket_project` + 全部既有审批 API 集成测试（真实种子清单后全绿）|
| 边界合规 | `architecture_check` R1/R2 0 违规（适配器只在 apps/api）|

## 6. 未闭合项（诚实登记）

1. **ADR-R24 §4 项 1**（交付清单引用的证据对象存在性校验）与本端口同源：
   待 PictureLock / 权利证据 / 终检报告对象落地后，在 `_RESOLVERS` 登记对应
   类型即可复用同一通道，无需新机制。
2. **解析器覆盖面**：只有审批绑定涉及的资源类型需要解析器；新增审批绑定
   时若资源类型未登记，会在发起时被 fail-closed 拒绝（不会静默放行），
   补登记即可。
3. **编排域（workflow）自身的资源**（如 Gate 实例）暂无审批绑定，未登记
   解析器；需要时按第 2 条处理。
