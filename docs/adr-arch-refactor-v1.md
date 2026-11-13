# AI 影视工业化生产平台 V2.0.5 —— 架构重构方案（ADR-ARCH-REFACTOR-V1）

> 状态：**已定稿（决策已确认，进入实施）**
> 触发：第三轮系统性审查报告（2026-11-13）
> 定位：本文档是后续所有重构工作的唯一纲领，取代"逐项修补"的开发节奏。
> 原则：审查报告 R1-R10 硬性准则为最高约束；"不要打补丁，要系统性根治"为总纲。

---

## 0. 结论先行

当前工程存在四类根因级问题，必须按依赖顺序重构，**在根基解决前停止新增业务功能**：

| 优先级 | 根因 | 一句话本质 |
| --- | --- | --- |
| P0-1 | 契约多事实源 | 同一事实在多处手工维护，靠比对脚本制造"虚假一致性" |
| P0-2 | 数据模型无约束 | 归属关系只靠 Service 查询校验，数据库不变量为零 |
| P0-3 | 身份治理零落地 | 权限只是 YAML 声明 + 字符串比较，运行时无鉴权内核 |
| P0-4 | 资源生命周期错位 | 用全局状态 + 事件循环重建硬扛连接池，把测试问题带进生产 |

保留项：模块化单体路线、PostgreSQL 独立 Schema、数据所有权设计、乐观锁方向（eb51910）、依赖锁定与 Docker 交付。

---

## 1. 重构总原则（硬性约束，逐条可核验）

| 编号 | 原则 | 对应审查禁令 | 验收方式 |
| --- | --- | --- | --- |
| R1 | 一个事实只有一个权威来源 | 同配置多文件维护 | 契约文件间无重复字段 |
| R2 | 不变量放在最可靠的层级保证 | 仅 Service 重复校验 | 数据库 FK/UK/Check 落地 |
| R3 | 先处理根因，再改代码 | 加 if/try/except | 每步先写根因说明再动手 |
| R4 | 消除重复机制 | 新旧并存、平行实现 | 旧机制物理删除 |
| R5 | 抽象必须有实际边界价值 | 为目录好看加 Manager | 无空壳抽象 |
| R6 | 安全必须真实执行 | 配置声明代替运行时鉴权 | 负向测试真实调接口 |
| R7 | 状态变化可追溯 | 单字段覆盖历史 | 不可变事件/审批事实表 |
| R8 | 并发有明确一致性保证 | 查询后写入解决竞争 | 条件更新/唯一约束兜底 |
| R9 | 测试验证真实行为 | 只验配置文件结构 | 测试调用真实端点/真实 DB |
| R10 | 每次重构验证旧机制已清理 | 只增不减 | 删除的代码有 diff 佐证 |

---

## 2. 已确认的六大架构决策（本轮定稿）

以下六条决策由产品/架构负责人确认，作为重构的权威技术选型，后续实现不得偏离。

### 决策 1：契约派生方式 —— Pydantic v2 为绝对核心（Code-First）

- **拒绝手写两份 Schema**（一份 Python 字典、一份 JSON Schema）。
- FastAPI 中强制使用 **Pydantic v2 模型定义底层数据结构**。
- 通过 `app.openapi()` 自动导出 JSON Schema，用 CI 跑 `openapi-generator` 自动生成前端（TypeScript）与测试端 SDK。
- **收益**：彻底消灭前后端"字段拼写不一致"的 bug；代码即文档、代码即契约，单点修改全局生效。

### 决策 2：ScriptRevision 冗余 project_id —— 坚决不冗余，用复合外键防越权

- 保持 **3NF**，`script_revisions` 表只存 `episode_id`。
- `episodes` 表主键设计为 `(project_id, episode_ref)` 复合主键（或唯一索引）。
- `script_revisions` 通过 `(project_id, episode_ref)` **复合外键**强绑定 Episode。
- **收益**：数据库直接锁死"跨项目乱挂载"，无需在代码里写一堆 if-else 校验。

### 决策 3：审计与审批 —— Transactional Outbox（事务发件箱）

- 建一张 `outbox_events` 表（event_id、聚合根 ID、event_type、payload、occurred_at）。
- 状态变更与审计事件**在同一数据库事务**中原子完成（例：导演审批通过 → ①更新 script_revisions 为 LOCKED；②插入 SCRIPT_LOCKED 到 outbox_events）。
- 后续后台 Worker 读 Outbox，异步发送到 Kafka/RabbitMQ 或写入分析型审计表。
- **收益**：业务状态与审计事件绝对原子；主库轻量 + 100% 可追溯。

### 决策 4：身份来源 —— RS256 非对称 JWT + 强制 JWKS 缓存

- 自签 JWT，但**必须 RS256**（私钥签发、公钥验证）。
- FastAPI 层实现 **JWKS 缓存**（按 `kid` 动态拉取，或定时刷新）。
- Authorization Service 持有私钥（离线/高安全区），API 网关/路由层只拿公钥验签。
- Token Payload 必须含短期强制过期（如 15 分钟）+ 唯一 `jti`。
- **收益**：杜绝 HS256 密钥泄露导致全盘崩溃；Key 轮换对业务层无感。

### 决策 5：租户/组织字段 —— 必加 + PostgreSQL RLS（行级安全）

- 所有核心表加 `tenant_id`。
- 配置 **RLS 策略**：`USING (tenant_id = current_setting('app.current_tenant_id')::uuid)`。
- FastAPI 数据库 Session 依赖解析 JWT 提取 tenant_id，每次查询前 `SET LOCAL app.current_tenant_id = '...'`。
- **收益**：多租户终极防呆，研发写 `SELECT * FROM projects` 也只会返回当前租户数据，越权漏洞按死在数据库层。

### 决策 6：契约收敛步骤 —— 先产出"三剑客"再写代码

动手写业务代码前，必须产出三份设计文件（纳入 `docs/architecture/` 版本控制）：
1. **DBML**（Database Markup Language）：ER 图，涵盖所有核心实体、外键、索引。
2. **OpenAPI（YAML）**：API 路由、Request/Response 结构。
3. **Mermaid 状态图**：ScriptRevision 等核心实体的完整状态流转。

---

## 3. 四大根基的重构方案

### 3.1 契约事实源（P0-1）

**现状问题（已核实）**：
- `action_registry.yaml` 与 `permission_matrix.yaml` 的 `action_matrix` 有 10 个共享字段完全重复。
- 状态机三重定义：`state_machines.yaml` + `domain/state_machine.py` + `contract_check.py`。
- OpenAPI 的 `/actions/*` 路径与 FastAPI 实际路由无映射。
- Schema 校验只查"定义非空"。

**根治方案（结合决策 1）**：
- **Pydantic v2 为运行时唯一数据结构源**，OpenAPI/JSON Schema 一律由 `app.openapi()` 派生，不再手写。
- 状态机单一源：`state_machines.yaml` 为权威，`domain/state_machine.py` 运行时加载或生成后标记"生成物勿手改"。
- `contract_check.py` 从"比对两套手写定义"改为"校验生成物与权威源一致"。

### 3.2 数据模型（P0-2）

**现状问题（已核实）**：ORM 与迁移中零 `ForeignKey`、零 `CheckConstraint`。

**根治方案（结合决策 2/3/5）**：

```
Project (1) ──┬── (N) Episode (1) ── (N) ScriptRevision
              │       ↑ 复合主键(project_id, episode_ref)  ↑ 复合外键
              └── (N) outbox_events（审计事件，append-only）
```

- **复合外键**：`Episode` 主键 `(project_id, episode_ref)`；`ScriptRevision` 通过 `(project_id, episode_ref)` 复合外键绑定，**删除冗余 project_id**。
- **tenant_id**：所有核心表加 `tenant_id`，配置 RLS。
- **Outbox**：`audit_ref` 单字段覆盖 → 独立不可变 `outbox_events` 表。
- **统一信封**：C.3.1 信封字段收敛到统一 mixin（ORM）+ 统一 dataclass 基类（领域）。

### 3.3 身份治理（P0-3）

**现状问题（已核实）**：`services/identity_access/` 空目录；`requested_by == approved_by` 冒充职责分离；Project 无 tenant 字段。

**根治方案（结合决策 4/5）**：
- RS256 JWT + JWKS 缓存。
- Principal 从认证上下文（JWT）解析，不是从请求体字符串。
- `tenant_id` 从 JWT claim 提取，注入 RLS 上下文。
- 职责分离是运行时判定（真实主体比对），非客户端自由填字符串。

### 3.4 事务与资源生命周期（P0-4）

**现状问题（已核实）**：`database.py` 用 `_engine_loop` 检测事件循环变化动态重建全局引擎。

**根治方案**：
- FastAPI Lifespan 管理引擎/SessionFactory（`app.state`）。
- 删除 `_engine_loop`、`_reset_engine_for_loop` 等动态重建机制。
- 测试用独立引擎（conftest 注入），生产代码零测试感知。

---

## 4. 分步实施计划（按依赖顺序，每步收口验证后再下一步）

| 步骤 | 内容 | 交付物 | 验收 |
| --- | --- | --- | --- |
| Step 0 | 落 ADR + 三剑客 | docs/architecture/*.dbml/openapi/mermaid | 三文件齐备 |
| Step 1 | 契约事实源收敛 | Pydantic 单一源；状态机单一源 | R1/R10 |
| Step 2 | 数据模型重建 | 复合外键 + 统一信封 + outbox + tenant_id + RLS + 迁移 | R2/R7/R8 |
| Step 3 | 身份内核落地 | RS256 JWT + JWKS + Principal | R6 |
| Step 4 | 资源生命周期重构 | Lifespan 管理引擎；删动态重建 | R4/R10 |
| Step 5 | 端点接入鉴权 | 所有写入口走统一授权 | R6/R9 |

**执行约束**：
- 每步开始前，先写该步的"根因 → 方案 → 验收"三行说明（R3）。
- 每步完成后，跑全量测试 + 删除旧机制的 diff 佐证（R10）。

---

## 5. 明确不做的事（本期）

- 不新增 Agent、模型网关、资产、QC 等业务模块。
- 不新增平行工具层（LockManager/VersionManager/AuditManager/IdempotencyManager）。
- 不把鉴权做成"自由文本别名适配"式的伪决策引擎。
- 不在数据模型无约束的情况下继续叠加 Service 归属检查。
