# ADR-R22：交付清单是 append-only 的证据引用，不是可编辑的文档

- 编号：ADR-R22
- 状态：已采纳
- 相关包：D（Phase 1 主链与交付闸门）
- 契约依据：`ADR-067` / `ADR-083`「交付 Manifest 必须可追溯、可校验、可重建」；
  `action_registry.yaml#DELIVERY.MANIFEST.CREATE`；`state_machines.yaml#DeliveryManifest`；
  `object_schemas.schema.json#DeliveryManifest`（负向用例 `ACT-DELIVERY-NEG-002`）

## 背景

`DELIVERY.MANIFEST.CREATE` 在契约里是 `status: ACTIVE` 的动作，运行时却没有任何落点：
`contract_check` 只能证明「登记为未实现」，头号风险不是"少一个端点"，而是**这条动作
在被实现的那一天会不会把红线一起打开**。三条红线同时压在这个对象上：

1. **版本不可变**：状态机契约写「每个 Manifest 版本不可变」，修正只能新建 `manifest_id`；
2. **质量不可暗降**：清单一旦 `VALIDATED` 就是对客户的交付依据，未审批的资产不得出现；
3. **不声明未发生的事实**：Phase 1 验收要求「不声明未发生的 Gate PASS」。

交付清单的诱惑是把它做成"一张能改的表"：加个 `PUT /delivery-manifests/{id}`，
补个 `status` 字段让客户端写。那样三条红线会同时失效，而且失效是静默的——
所有既有闸门仍然全绿。

## 决策

### 1. 端口层没有 `save`，权限层没有 `UPDATE`

`DeliveryManifestRepository` 只有 `add` / `get_by_id`；数据库只给资产域
`GRANT SELECT, INSERT`，并加 append-only 触发器兜住超级用户与迁移角色
（与 `qc_reports` 同口径）。

这不是"提供了 save 但要求调用方别用"——**去掉它在端口层面就不存在**。
约束能被执行的前提是它有地方可执行，而不是有一份写在注释里的约定。

### 2. 校验和由服务端计算，只覆盖可重建内容

`compute_manifest_checksum` 是纯函数，覆盖 `asset_refs`（排序后）、
`spec_ref` / `qc_final_ref` / `picture_lock_ref` / `rights_evidence_refs`（排序后）、
`rebuild_recipe`。**不含** `manifest_id`、时间戳、决策人。

理由：重建一次交付时 `manifest_id` 与时间戳必然不同，把它们纳入哈希等于让
「用清单重建一次，哈希应与原件一致」这条验收要求**永远无法成立**。

请求体刻意不接受 `checksum`：校验和若可由调用方提交，"哈希一致"就只是
一句自证。实测见 `tests/unit/test_delivery_api.py::test_checksum_is_computed_by_server`
（塞 `checksum="0"*64` 也不生效）。

### 3. `rebuild_recipe` 的类型以契约为准（string），代价显式登记

契约把 `rebuild_recipe` 声明为 `string`，实现就收字符串，不做"结构更好所以改成
对象"的擅自升级——那会让契约与运行时出现第二种真相。

代价要说清：平台无法对配方内部做规范化，因此"同一份交付算出同一哈希"要求
**配方字符串本身是确定性的**，这是产出方的责任。平台能做且已做的是：
拒绝空配方、把配方原文逐字纳入哈希（改一个空格就换一个哈希，见
`test_recipe_text_is_hashed_verbatim_without_normalisation`）。

若将来需要结构化配方，那是契约修订流程的事（`freeze_manifest` 会要求
"演进有据"），不在这里单方面放开类型。

### 4. 只引用已成立的证据，且只可能产生 DRAFT

- `asset_refs` 逐个校验：必须存在、必须属于本项目、状态必须是 `APPROVED` / `GOLD`。
  放 `GENERATED` / `TECHNICAL_QC` 进来，等于允许未审资产出现在对客户的清单上；
- 领域对象 `__post_init__` 直接拒绝非 `DRAFT` 状态：`VALIDATED` / `RELEASED`
  必须走 `DELIVERY.MANIFEST.APPROVE` / `RELEASE`，那两个动作依赖 Gate 决策链，
  尚未实现，已在 `action_implementations.yaml` 登记为未实现并写明原因；
- **`gate_pass` 类字段在契约里根本不存在**，因此"不声明未发生的 Gate PASS"
  是字段集缺项保证的，不是靠代码自觉。清单里只有证据引用
  （`qc_final_ref` / `picture_lock_ref` / `rights_evidence_refs`）——
  引用只能指向已存在的证据，无法表达"Gate4 已通过"这种结论。

### 5. 幂等键复用必须是同一请求，并发路径不享有豁免

同 `manifest_id` 同内容 → 返回既有清单且不重复发事件（重复提交不是新事实）；
同 `manifest_id` 不同内容 → `IDEMPOTENCY_CONFLICT`（409），因为那是**改写已发出的
交付清单**。仓储抛 `DuplicateEntityError` 的并发路径回查后走**同一个**内容比对函数，
不允许"并发成了绕过口"。两条路径各有一条测试。

### 6. 项目隔离列存在但不进响应

表上有 `project_id`，契约对象没有这个字段。它只服务 `project_scope_*` 限制性策略
（与既有五张项目级资源表同一份表达式）——缺这一列，同租户跨项目的清单泄漏
就没有拒绝依据。`to_dict()` 刻意不输出它，`contract_check` 的序列化探针会以
「响应出现契约未声明字段」报红（已反向验证）。

## 闸门

| 性质 | 证明方式 |
|---|---|
| 版本不可变 | `tests/integration/test_delivery_manifest_immutability.py`：`UPDATE` / `DELETE` 被触发器拒绝，**含超级用户直连** |
| 状态可表示但不可乱写 | 同上：契约外取值被 `ck_delivery_manifests_status_registered` 拒绝；8 个契约取值逐一可插入 |
| 只授 SELECT/INSERT | 同上：系统目录断言（`information_schema.table_privileges`），只看应用角色 |
| RLS 双策略 | 同上：跨租户、同租户跨项目均读不到 |
| 校验和可重建 | `tests/unit/test_delivery_manifest_domain.py`：排序不敏感、配方逐字参与、`rebuild_projection` 复算一致 |
| 只生成 DRAFT / 证据必须成立 | `tests/unit/test_delivery_service.py`、`tests/unit/test_delivery_api.py` |
| 响应字段集 == 契约 | `contract_check` 序列化探针（`DeliveryManifest` 已登记） |
| 事件已登记且真被发出 | `event_catalog.yaml#DELIVERY_MANIFEST_CREATED`（双向校验） |
| 枚举三方一致 | `enum_bindings.yaml#DeliveryManifest` → `DeliveryManifestStatus` |

## 诚实登记（未闭合项）

1. **审批与放行未实现**：`DELIVERY.MANIFEST.APPROVE` / `RELEASE` 依赖 Gate4 PASS、
   PictureLock、权利证据与终检报告齐备。本 ADR 只让 CREATE 落地，两个动作在
   `action_implementations.yaml` 中仍是 `not_implemented` 并写了原因。
   当前清单**永远停在 DRAFT**，这是刻意的，不是遗漏。
2. **不验证证据本身的有效性**：清单引用 `qc_final_ref` / `picture_lock_ref` /
   `rights_evidence_refs` 时只做非空校验，不校验被引用对象是否真实存在。
   跨域校验属于 `APPROVE` 的职责（届时这些对象都要可查）。
3. **状态机只实现了"起点"，转移表还没有**：`DeliveryManifestStatus` 的 8 个取值全部
   可表示（CHECK 允许），但运行时**没有**转移表——`DRAFT → VALIDATING → …` 的边由
   审批动作落地时才建立。因此它不在 `contracts/runtime/state_machine_gaps.yaml`
   的登记范围内（该台账只登记**已实现状态机**的不可达状态），本对象的缺口以
   `action_implementations.yaml` 的 `not_implemented`（APPROVE / RELEASE）表达。
   这是两类不同维度的债务，不能互相替代：前者是"有转移表但某些状态无来源边"，
   后者是"连转移表都还没有入口来驱动"。
4. **配方字符串的确定性靠产出方**：见决策 3。
5. **无"重建"执行器**：`rebuild_projection()` 只给出重建所需的输入与哈希，
   真正跑一次渲染重建需要 `workers/delivery` 与模型网关，仍为空目录。

### 债务状态核对（2026-11-17，ADR-R30 同期收口）

- **未闭合项 3 已过时（存档保留）**：转移表由迁移 `a2b3c4d5e6f7` 建立，
  `APPROVE` / `RELEASE` 的运行时入口已由 ADR-R24（D3 通用审批引擎 +
  D4 审批事件消费）闭环落地，清单可推进至 RELEASED。本条仅存档当时状态，
  不再代表现状。
- **未闭合项 5 部分演进，债务保持登记**：`workers/delivery` 已落地，但
  当前只承载审批事件消费（D4）；重建执行器的剩余前置是
  `services/model_gateway`（仍为 `.gitkeep` 空壳）与生成侧闭环
  （GENERATION.REQUEST 未实现）。解除条件不变：模型网关接入 +
  单一路线生成可执行后，`workers/delivery` 扩展重建用例。
- **未闭合项 2 机制演进**：ADR-R28 落地了登记式跨域只读端口
  （`CompositeResourceOwnershipPort`），证据对象存在性校验的**接入机制**
  已就绪；剩余前置收敛为"被引用的证据对象本身落地"（权利证据所属的
  rights_governance 仍为空壳，见 ADR-R24 §4 同步核对）。
