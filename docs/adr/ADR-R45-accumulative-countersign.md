# ADR-R45：累积式会签事实与运行时入口（D2b · 已裁决 DECIDED）

- 状态：**DECIDED（项目所有者 2026-11-24 签署三项裁定；定版指纹见 `ADR-R45-VERSION.txt`）**
- 日期：2026-11-24
- 触发：`#13` 累积式会签长期 blocked；`#35` 状态治理三组债务（G-B/G-A/G-C）全部销项后，裁定包 `2026-11-24-status-governance-ruling-packet.md §7-E` 的前置条件「先解开 #35」已满足。
- 关联：ADR-R23（`status_transitions` 事实表，D2a 已落地）；ADR-R40（统一授权模型）；`contracts/security_boundary.yaml` AUTH-5；ADR-R15（契约修订与冻结重签流程）。
- 指纹：正文**不内嵌自身哈希**。定稿 → 一致性校验 → 采集指纹 → 登记 `ADR-R45-VERSION.txt`（append-only，四元口径）。
- 计数口径：ADR 记录决策不记录进度。计数一律指向 `contracts/phase0/contract_amendments.yaml` 与各台账，正文不内嵌。

---

## 1. Why（为什么需要这份 ADR）

ADR-R23 的 D2a 关闭了「迁移可追溯」：每次状态推进写一行 `audit.status_transitions`。但 §D2-e 明确撤回了一个错误结论——**会签不能借那张表表达**：

- `status_transitions` 记录的是「推进**已经**发生」，一行 = 一次推进，`CHECK (from_status <> to_status)` 已把 no-op 行堵死；
- 会签需要的是「推进**之前**，某席位签了字」——这是**另一类事实**，有独立生命周期（可撤回、可被消费、可能凑不齐）。

于是 `contracts/security_boundary.yaml` 的 AUTH-5 `residual_risk` 把缺口从泛称「审批事实契约」收紧到两项具体依赖：

1. `audit.approval_signatures`——「推进前某席位已签署」的独立事实表；
2. 目标动作的运行时入口——支持多主体**分次**签署、凑齐后放行。

在此之前，多席位动作在 `rbac.py:90-106` 一律 fail-closed 显式拒绝并说明「不支持多主体累积审批」。这是**有意为之**而非缺陷——它保证了「不静默降级」，但会签功能本身不可用，`#13` 因此维持 blocked。

本 ADR 裁决 D2b：落地这两项依赖，关闭 AUTH-5 的功能缺口。**D2a 的事实表不改动、不复用、不混表**（§5）。

## 2. 所有者裁决（三项，2026-11-24）

| # | 裁决 | 边界含义 |
|---|---|---|
| R1 | **本批范围 = 地基 + 一条闭环** | ADR + `approval_signatures` 表 + 一条动作的累积签署闭环 + 契约登记 + 负向测试。历史读接口 `AUDIT.STATUS.HISTORY.READ` **留后续批次**，本批不新增。 |
| R2 | **目标动作 = `DELIVERY.MANIFEST.APPROVE`** | 作为第一个接入累积会签的动作（席位定义来自 Action Registry 的 `交付负责人+内部 QC/权利职责`）。 |
| R3 | **签署状态集 = `SIGNED / REVOKED / CONSUMED`** | 不做超时失效（基线无有效期口径，不臆造）。凑齐按「每席位一个有效签署 + 跨席位主体互不相同」判定。 |

## 3. 决策

### D3-a 独立事实表：`audit.approval_signatures`（append-only 事件日志）

一行 = 一次**签署生命周期事件**，而非一条可变状态的签署记录。三态 `SIGNED / REVOKED / CONSUMED` 作为 `event_kind` 落值，当前「某席位是否有效签署」由事件**派生**，不做原地 UPDATE。这与 D2a 一致：append-only + 派生，不变量交给数据库而非「记得写对」。

```sql
CREATE TABLE audit.approval_signatures (
    signature_id    uuid          PRIMARY KEY,
    tenant_id       uuid          NOT NULL,
    organization_id uuid          NULL,
    resource_type   varchar(64)   NOT NULL,   -- 与 status_transitions / 实体 entity_id 同型
    resource_id     varchar(64)   NOT NULL,
    action_key      varchar(64)   NOT NULL,   -- 目标动作，如 DELIVERY.MANIFEST.APPROVE
    seat_index      integer       NOT NULL,   -- principal_seats 的下标（1-based）
    subject         varchar(128)  NOT NULL,   -- 签署主体，取自 Principal（不可伪）
    event_kind      varchar(16)   NOT NULL,   -- SIGNED / REVOKED / CONSUMED
    authority_ref   varchar(128)  NULL,       -- Capability Token jti
    evidence_refs   varchar(64)[] NOT NULL DEFAULT '{}',  -- 证据引用，不存快照
    occurred_at     timestamptz   NOT NULL DEFAULT now(),
    event_id        uuid          NOT NULL,   -- 对应 outbox 事件（提交时校验存在）

    CONSTRAINT uq_signature_event
        UNIQUE (tenant_id, resource_type, resource_id, action_key, seat_index, subject, event_kind),
    CONSTRAINT ck_signature_kind
        CHECK (event_kind IN ('SIGNED','REVOKED','CONSUMED')),
    CONSTRAINT ck_signature_seat_positive
        CHECK (seat_index >= 1)
);
```

五条设计要点，每条对应一个「不做就会重演」的失败形态：

**1. 唯一约束含 `event_kind` 与 `subject`，不含时间。** 同一主体对同一席位只能签一次（重复 SIGN 撞唯一约束）；撤销/消费是不同 `event_kind` 的新行，不覆盖原签署。若把时间列纳入唯一键，重复提交会写出两条 SIGNED，凑齐判定被「同席位多主体」的假象污染。

**2. `subject` 必须取自 Principal，不进请求体。** 与 `worker_id`/`approver` 同类判据（记忆铁律 R43）：凡进入准入或职责分离判定的字段必须不可伪。请求体里可留 `operator_ref` 做溯源，但它不参与任何比较；进入 `subject` 的值只来自 `get_principal` 注入的 Principal，模型层 `extra="forbid"`。

**3. 跨席位「不同主体」是运行时判据，靠单一入口 + AST 白名单保证。** 数据库唯一约束只能保证「同席位同主体不重复签」，无法表达「seat 1 的 subject ≠ seat 2 的 subject」（SQL 无跨行跨列 exclusion）。因此凑齐判定收敛为唯一纯函数 `evaluate_countersignature()`，签字入口收敛为唯一服务 `CountersignatureService`；边界断言用 AST 查 `CountersignatureService` 的写入调用点白名单，出现第二条路径即报红。这与 D2a「每域一条推进函数 + `transition_to` 调用点白名单」同源——**不是靠数调用次数，而是白名单能点名越界者**。

**4. `event_id` 非空 + 提交时约束触发器校验事件存在。** 与 D2a 完全同法（分区表上 `event_id` 单独不唯一，外键建不出来）。`SECURITY DEFINER` + `SET search_path`，同时比 `event_id` 与 `tenant_id`——只比前者会让租户 A 的签署指向租户 B 的事件，两租户的会签链被缝在一起。

**5. append-only 三层守卫 + RLS enable&force。** 行级 `BEFORE UPDATE OR DELETE`、语句级 `BEFORE TRUNCATE`（TRUNCATE 不触发行级触发器）、提交时事件校验；所有业务角色 `REVOKE UPDATE/DELETE/TRUNCATE`；`ENABLE` **且** `FORCE ROW LEVEL SECURITY`。缺任何一条，表还在但不变量不在（同 D2a DATA-5 判据）。

### D3-b 凑齐判定：每席位恰好一个有效签署，且跨席位主体互不相同

```
有效签署(seat) := 存在该 seat 的 SIGNED 行，
                  且不存在同 (seat, subject) 的更晚 REVOKED 行，
                  且不存在该 (resource, action) 的 CONSUMED 行
凑齐 := 对 principal_seats(action_key) 的每个 seat，恰好一个有效签署，
        且所有有效签署的 subject 两两不同
```

- 「每席位恰好一个」：多于一个有效签署（哪怕来自不同主体）也是异常——说明撤销流程被绕过，必须报红而非取第一个；
- 「跨席位主体互不相同」：这正是 `+`（AND 会签）与 `/`（OR 候选）的语义落点。单主体签满所有席位 = 会签被静默降级为单人授权，必须拒绝；
- 判据从 `principal_seats()` 派生席位集合，**不手写席位清单**（新增/改动动作席位时自动跟随，清单不会自己举手）。

### D3-c 消费与防重放：凑齐后写 `CONSUMED`，整组签署一次性失效

放行时为每个已签席位写一条 `CONSUMED`（与 SIGNED 同 `seat_index`/`subject`，不同 `event_kind`），与放行事件同事务。消费后：凑齐判定因 `CONSUMED` 存在而恒为 false，同一组签署无法被二次用于批准——**防重放不依赖「记得删除签署」**（append-only 表也删不掉）。

### D3-d 运行时入口：签署服务 + 交付审批接入

- 新增 `CountersignatureService`（`services/identity_access/countersign.py`）：`sign()` / `revoke()` / `evaluate()` / `consume()`，唯一写入点；`sign()` 校验签署主体角色 ∈ 目标席位候选（单席位 RBAC），角色不符 fail-closed；
- `DELIVERY.MANIFEST.APPROVE` 从「单请求 fail-closed 拒绝」改为「经签署服务凑齐后放行」：`rbac.authorize()` 的多席位分支保留（直接单请求调用仍拒绝，语义不变），新增签署流程专用入口，凑齐后由服务记录 `CONSUMED` 并触发放行事件；
- 签署入口复用 `get_principal` 注入的 Principal 与 `SqlOwnershipResolver` 的归属解析，不另造身份/归属判定。

### D3-e 契约演进走既有流程（不直接改派生文件）

新增 `ApprovalSignature` 对象 Schema、`ApprovalSignature` 三态状态机，先登记 `V205-R45` 修订，再派生、再 `freeze_manifest --write` 重签、再 `contract_check`/`architecture_check`/`boundary_assertions` 复验。顺序反了 `freeze --check` 报「未登记新增」——这是 ADR-R15 设计的行为，不绕。

**本批不新增 `DELIVERY.MANIFEST.SIGN`/`REVOKE` 动作**（CORRECTION-1）：签署服务是内部服务，尚无 API 端点调用它（交付清单业务域按 §5 不在本批落地）。此时登记动作只会制造新的「已登记未实现」债务——与 G-C 清理的正是同一类。动作登记推迟到交付清单业务域落地、有真实端点调用 `CountersignatureService` 时同批进行；本批签署路径的授权由服务内的席位角色校验（`_validate_seat_role`，角色取自 Principal 不可伪）+ Action Registry 派生的席位集合保证，不靠新增 action_key。

## 4. 数据边界要求（与 D2a 同源，不重复实现）

| 项 | 口径 |
|---|---|
| Schema | `audit`（与 `status_transitions` 同 schema，不同表，不混数据） |
| 写入授权 | 有 `DELIVERY.MANIFEST.*` 动作的域角色：`USAGE ON SCHEMA audit` + `INSERT, SELECT`；授权由 `packages/shared/db_grants.py` 矩阵声明，迁移只执行 |
| 变更授权 | 所有业务角色 `REVOKE UPDATE, DELETE, TRUNCATE`；三层触发器二次兜底 |
| RLS | `ENABLE` 且 `FORCE`，按 `app.current_tenant_id` 隔离（未注入租户 → fail-closed） |
| 组织维度 | 随 D3 统一接入，本 ADR 只落值不落第二套策略（同 D2a） |
| 保留期 | 不设（审计事实长期保留） |

## 5. 不做什么

- **不碰 `audit.status_transitions`**：D2a 事实表已定版，本 ADR 不改其结构、守卫、写入点；两类事实分开存放。
- **不做超时失效 / `EXPIRED`**（R3）：基线无有效期口径，臆造一个只会制造第二处可配置事实源。
- **不做历史读接口 `AUDIT.STATUS.HISTORY.READ`**（R1）：留后续批次，本批不新增动作。
- **不落地交付清单业务域**：`DELIVERY.MANIFEST.CREATE/RELEASE` 仍在未落地清单；本批关闭的是「累积签署机制」，Manifest 的业务状态推进属下游，不借本 ADR 夹带。
- **不回填历史**：迁移前的签署不存在，不编造。
- **不改 `rbac.authorize()` 的多席位拒绝语义**：它保证「不静默降级」，必须保留；本批是在其旁开一条显式的累积签署路径，不是放宽它。

## 6. 残余风险

1. **跨席位不同主体靠运行时 + AST 白名单，非数据库硬约束**：SQL 无跨行 exclusion；白名单按「文件 + 类」判定，反射/字符串拼接调用需代码评审兜底（同 D2a DATA-5 残险）。
2. **交付清单业务副作用未落地**：会签凑齐后写 `CONSUMED` + 放行事件，但 Manifest 状态推进到 APPROVED 的下游消费者尚未实现；本批交付的是「会签机制可用且可测」，不是「交付审批端到端可用」。
3. **组织隔离依赖 D3**：子资源组织归属当前落 NULL，会签事实不可用于按组织维度查子资源历史（登记债务，同 D2a）。
4. **运维绕过面**：表所有者可用 `ALTER TABLE … DISABLE TRIGGER` 或 `session_replication_role = replica` 关守卫（运维权限范畴，非应用路径）。

## 7. 生效条件（签署时逐条核销）

- [x] 所有者三项裁决（R1/R2/R3）已签署（2026-11-24）
- [x] 权威事实源 `security_boundary.yaml` AUTH-5 `residual_risk` 已收紧到两项具体依赖（D2a 已完成部分保留）
- [ ] `approval_signatures` 迁移 + ORM + 授权 + RLS 落地，负向用例全绿
- [ ] 累积签署服务 + 交付审批接入落地，凑齐/防重放/不同主体负向用例全绿
- [ ] 契约登记 + 派生 + 冻结重签 + 边界断言全绿
- [ ] CI 四项全绿并合并 main

## 8. 签署记录（append-only）

| 日期 | 事项 | 证据 |
|---|---|---|
| 2026-11-24 | 三项裁决签署（地基+一条闭环 / DELIVERY.MANIFEST.APPROVE / SIGNED-REVOKED-CONSUMED） | 本会话所有者授权；`docs/audits/2026-11-24-status-governance-ruling-packet.md §7-E` 前置条件满足 |
| 2026-11-24 | 定版指纹 | `ADR-R45-VERSION.txt`（append-only，四元口径） |
