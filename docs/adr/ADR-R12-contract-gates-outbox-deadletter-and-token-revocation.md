# ADR-R12：契约闸门行为化、Outbox 死信、幂等与令牌吊销

- 状态：已接受（S4/S5/S6 落地）
- 日期：2026-11-14
- 关联：`docs/audits/2026-11-14-execution-loop-audit.md` P1-3、P2-1~4、P3-1~6；
  ADR-R06-2（outbox 分区/保留期）、ADR-R10（权限基线）、ADR-R11（事件契约与不可变）

## 1. 背景

审计把"声明自洽、事实缺失"列为结构性根因：schema 闸门只判非空、响应字段靠手写、
relay 失败无终点、幂等键可复用、jti 无法吊销。S4/S5/S6 的目标不是逐条打补丁，而是
把这几类"看起来有、实际没有"的地方变成可执行、可失败、可复现的约束。

## 2. 决策

### 2.1 Schema 闸门从"数量"改为"行为"（P1-3）

`scripts/contract_check.py::check_schema_objects` 现在必须同时满足：

1. 每个定义 `type=object` 且 `properties` 非空；
2. `required` 是列表且每项都在 `properties`；
3. 每个属性有形状（type/$ref/anyOf/oneOf/allOf/enum/const），`type` 必须是合法
   JSON 类型（支持 `["string","null"]` 这类联合）；
4. 内部 `$ref` 必须能解析（悬空报红）；
5. 已实现对象（`AssetVersion`、`QCReport`）用真实 `to_dict()` 做双向比对：
   契约 required 必须全部出现，运行时多出的键必须在 properties 内。

**为什么用真实对象而不是静态解析**：`to_dict()` 的字段集由多段逻辑拼出来，
静态阅读会漏掉条件分支；用最小合法探针对象跑一遍，才是"运行时真的会输出什么"。

### 2.2 契约修正：`audit_ref` 显式可空

67 个对象的 `audit_ref` 原声明为 `type: string`，但领域信封在未发生审批动作时
就是 `None`。这不是运行时错误，而是契约过度承诺。修正为 `["string","null"]`：
键仍然 required（永远出现），值允许为空（表示"尚无审计引用"）。

**影响**：响应模型生成器支持联合类型；闸门支持类型数组。没有放宽必填语义。

### 2.3 响应模型由契约生成（P2-4 / P2-3）

`packages/shared/contract_models.py::contract_model(name)` 从 object_schemas 动态
生成 Pydantic 模型；资产四个路由挂 `response_model`，GET 改走只读 UoW。
`Entity._envelope_dict()` 与 `QCReport.to_dict()` 移除 `tenant_id` / 内部时间戳等
契约未声明字段。

**取舍**：契约加字段 → 响应自动多字段；契约删字段 → FastAPI 过滤多余键。
手写模型必然再次漂移，因此不再手写。

### 2.4 Outbox 重试与死信（P2-2）

`outbox_events` 增 `attempts`、`next_attempt_at`、`dead_lettered_at`（父表加列
自动下传分区）。claim 条件加 `dead_lettered_at IS NULL AND next_attempt_at <= now()`；
失败按 `10s × 2^attempts`（上限 640s）退避，默认 5 次后写死信；指标新增
`result="dead"`。死信不删除，runbook 给巡检 SQL 与迁移角色重放语句。

**为什么死信不删**：事件是审计事实，"投递放弃"本身需要可查；删除会把一次
失败变成一次数据丢失。

### 2.5 幂等键只能复用于同一请求（P3-1）

剧本、项目、分集在同 `entity_id` 命中时比对内容；不一致一律
`409 IDEMPOTENCY_CONFLICT`。此前"命中即返回旧对象"会让调用方拿到 201 而数据
从未写入。

### 2.6 jti 吊销（P3-2）

新增 `auth.revoked_tokens`（jti 主键、expires_at 可清理）；app_role 仅 SELECT，
写入走 `scripts/revoke_token.py`（迁移角色）。认证依赖验签成功后查吊销，缺 jti
直接 401（无法吊销 = 无法止损）；查询失败 fail-closed。

**未做 HTTP logout**：action_registry 无该动作，凭空加端点等于猜契约。

### 2.7 测试污染、运维权限、生产密钥（P3-3/P3-5/P3-6）

- `test_unit_of_work.py` 用迁移角色自清理 `UoWTest` 事件；迁移清理存量 9 条；
- `outbox.maintenance_runs` 收回 app_role 全部权限（该表无租户键，加 RLS 是假隔离）；
- 生产必需密钥只保留 DB 口令与身份 PEM；S3/MQ/模型网关无实现，强制配置只会
  逼迫填假值。

## 3. 落地物

| 类别 | 文件 |
|---|---|
| 契约/闸门 | `scripts/contract_check.py`、`contracts/schemas/object_schemas.schema.json` |
| 响应模型 | `packages/shared/contract_models.py`、`services/asset_service/api/asset_routes.py` |
| 序列化 | `packages/shared/entity.py`、`services/asset_service/domain/qc_report.py` |
| 事件 | `contracts/runtime/event_catalog.yaml`、`event_emission_gaps.yaml`、`project_service.py`、`episode_service.py` |
| Outbox | `workers/relay/consumer.py`、`packages/shared/outbox.py`、`runbook-outbox-retention.md` |
| 吊销 | `packages/identity/token_revocation.py`、`revoked_token_model.py`、`scripts/revoke_token.py`、`services/identity_access/auth_dependency.py` |
| 迁移 | `e2f3a4b5c6d7`、`f3a4b5c6d7e8`、`a4b5c6d7e8f9`、`b5c6d7e8f9a0`、`c6d7e8f9a0b1` |
| 测试 | `test_contract_check.py`（4 条负向）、`test_outbox_relay_retry.py`、`test_project_lifecycle_events.py`、`test_auth_revocation_and_grants.py`、`test_unit_of_work.py`、`test_config_secrets.py` |

## 4. 验证

- `contract_check`：Schema 76 定义结构通过、运行时字段一致 2 对象、事件 10/缺口 0；
- `ruff` 0；`mypy` 90 files 0；`pytest` 272 passed；`alembic check` 无漂移；
- 迁移已 upgrade 到 `c6d7e8f9a0b1`；S4 迁移与 S5 迁移在实际库执行成功。

## 5. 未做项（显式）

1. 新迁移未逐条 `downgrade` 往返（S3 迁移做过）；数据结构简单，但按红线仍应
   在发布前对 `b5c6d7e8f9a0`/`c6d7e8f9a0b1` 做一次往返演练。
2. `auth.revoked_tokens` 的过期行清理目前靠 CLI `--prune-expired` 手动/排程，
   未接维护 worker。
3. 事件 payload 仍只做静态字段登记，未做运行时 payload 形状校验（ADR-R11 已列）。
4. Project/Episode 的 HTTP 状态变更入口仍不存在（只补了创建事件）；动作登记
   需等正文口径，不猜契约。
