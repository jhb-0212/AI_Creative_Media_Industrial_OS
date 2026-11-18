# ADR-R23：状态迁移事实表（审批可追溯的第一类事实）

- 状态：已采纳，待实施（2026-11-18）
- 关联：N-02、ADR-R07（状态机单一事实源）、ADR-R11（不可变性与授权基线）、ADR-R06-2（outbox 保留期与分区）、ADR-R22（基线事实源单一化）
- 上游依据：`docs/audits/2026-11-18-full-repo-baseline-and-root-cause-audit.md` §7 N-02
- 触发的契约演进：`V205-R14-A12`（新增 `StatusTransition` 对象 Schema，待登记）

## 1. 问题的实质：审批被建模成「一次 HTTP 请求的副作用」

当前一次状态迁移留三处痕：

| 落点 | 性质 | 会不会消失 |
| --- | --- | --- |
| 实体 `audit_ref` | 单字段赋值 `f"{owner_action}:{actor}"` | **下一次迁移就覆盖** |
| outbox 事件 | 完整 payload（from/to/actor/action/证据计数） | **保留期到就 DETACH 归档** |
| — | 无第三处 | 数据库 12 张表里没有 `approvals` / `status_transitions` / `audit_log` |

事实本身是齐的——`AssetService._advance()` 里 from_status、to_status、actor、owner_action、
qc_report_count 全都有，只是**唯一持久落点是一张会归档的表**。

于是「谁在什么证据下批准了什么」这个问题，在保留期之后**不可查询**，只能翻冷备 dump。
而实体上的 `audit_ref` 是自由字符串 `ASSET.STATUS.CHANGE:alice`——它连「哪一次」都说不清。

## 2. 根因

不是「忘了建表」，是**建模层级错位**：

- ADR-R17/R18 明确「刻意不另造审批子对象，避免第三份事实源」——这个取舍在**审批只有一次、
  且只需要当下可见**的前提下成立；
- 但它把「可追溯性」整体押在了事件流上，而事件流的生命周期由**保留期**决定，
  不由「审计需要」决定；
- 结果是一个**结构性**结论：AUTH-5（累积式会签）不是排期问题，是模型里没有落点。

判据很简单：**事实的生命周期必须由事实的用途决定，不能由承载它的传输机制的保留策略决定。**

## 3. 决策

### D2-a 新增唯一一张迁移事实表：`audit.status_transitions`

一张表，一类事实：**「某个资源在某个版本上，从 A 状态推进到了 B 状态」**。

```sql
CREATE TABLE audit.status_transitions (
    transition_id   uuid        PRIMARY KEY,
    tenant_id       uuid        NOT NULL,
    organization_id uuid        NULL,          -- 全局资源（无组织归属）为 NULL
    resource_type   text        NOT NULL,      -- 对象类型，与 Schema $defs 同名
    resource_id     uuid        NOT NULL,
    entity_version  integer     NOT NULL,      -- 推进完成后的乐观锁版本
    from_status     text        NOT NULL,
    to_status       text        NOT NULL,
    owner_action    text        NOT NULL,      -- Action Registry 的 action_key
    actor_subject   text        NOT NULL,      -- 谁触发的
    authority_ref   text        NULL,          -- 授权凭据 jti（谁授的权），可空
    evidence_refs   text[]      NOT NULL DEFAULT '{}',  -- 证据对象 id 列表，不是摘要
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    event_id        uuid        NULL,          -- 对应 outbox 事件（事件是通知，不是历史）

    CONSTRAINT uq_status_transition_version
        UNIQUE (tenant_id, resource_type, resource_id, entity_version)
);
```

四个设计要点，每条都对应一个「不做就会重演」的失败形态：

**1. 唯一约束绑在 `entity_version` 上，而不是绑在「时间」上。**
乐观锁已经在 `save(entity, expected_version)` 里保证「同一版本只推进一次」；把它抬到事实表，
就使「并发两次审批产生两条记录」**结构上不可能**——第二个写者拿不到同一个 version，
唯一约束先于任何应用层判断失败。这比在 Service 里加 `if` 强，因为它不依赖谁记得加。

**2. 只存 `evidence_refs`，不存证据快照。**
存 `qc_report_count=3` 之类的快照，等于给「当时有几份 QC 报告」造了第二个事实源，
而 `qc_reports` 本身已经 append-only 不可变（ADR-R11），随时可重新计数。
**能引用就不要复制**——复制出来的数字一定会和源漂移。

**3. `event_id` 互指：事件从「唯一历史」降级为「通知下游」。**
写事务内先生成 `event_id`，再写 transition 与 outbox 事件，两条记录互相指向。
归档 outbox 分区不再影响审计可查询性；反过来，事件表也不再被要求承担历史职责。

**4. append-only 用触发器兜底，不是只 REVOKE。**
REVOKE 只对业务角色生效，迁移角色/超级用户照样能改（`d1e2f3a4b5c6` 的同一结论）。
`BEFORE UPDATE OR DELETE` 一律 `RAISE EXCEPTION`，对所有角色生效。

### D2-b 写入点收敛到既有的唯一 choke point

不新增 `TransitionRecorder` / `AuditService` 之类的平行层。每个域**本来就有**一个统一推进函数：

| 域 | 唯一推进点 | 当前职责 |
| --- | --- | --- |
| asset_service | `AssetService._advance()` | 守卫求值 → 乐观锁迁移 → 同事务写事件 |
| project_planning | 同形态推进函数 | 同上 |

在这一个函数里增加一步「写 transition 行」，顺序固定为：

```
求值守卫 → （生成 event_id）→ 乐观锁 save(entity, expected) → 写 transition → 写 outbox 事件
```

三者同一事务：任一步失败，状态、事实、事件一起回滚。这样得到的不变量是
**「有状态推进 ⇒ 必有 transition 行」**，而且只有一条路径能产生它。

### D2-c `audit_ref` 降级为指针

`audit_ref` 不再接受 `f"{action}:{actor}"` 这类自由字符串，改为**最近一条 transition 的 `transition_id`**。

- 保留字段（信封 C.3.1 有它，删除会动契约面），但语义从「凭据摘要」变成「指针」；
- 由 `_advance_version()` 统一赋值，**不允许任何其他位置写它**（新增边界断言，见 §6）；
- 指针失效即报错：读到的 `audit_ref` 必须能在 `status_transitions` 里找到，
  否则说明有人绕过唯一路径写了状态。

### D2-d 契约演进走既有流程（不直接改派生文件）

新增 `StatusTransition` 对象 Schema 属于「新增 Schema 对象」，必须先登记 `V205-R14-A12`：

```
contract_amendments.yaml 登记 A12（schema_objects: StatusTransition）
    → object_schemas.schema.json 新增 $defs
    → freeze_manifest --write 重签
    → contract_check / architecture_check 复验
```

顺序反了 `freeze_manifest --check` 会报「未登记新增」——这正是 ADR-R15 设计的行为，不绕。

### D2-e 本 ADR 只做 D2a；读接口与会签拆到 D2b（并修正一处此前的判断）

**纠正核验报告的结论**：报告 §N-02 写「会签在此表上表达为同一 `(resource, from, to)` 下多条
席位签署记录，凑齐后放行——AUTH-5 的 unblocks_when 因此可以关闭」。**这个说法不成立，撤回。**

- 本表记录的是「推进**已经**发生」，一行 = 一次推进；
- 会签需要的是「推进**之前**，某个席位签了字」——这是**另一类事实**，有独立的生命周期
  （可能被驳回、可能超时失效、可能凑不齐），塞进本表只能靠 `to_status = from_status` 的
  no-op 行或 `kind` 判别列来表达，那是用一张表存两类不变量；
- 因此 AUTH-5 的 `unblocks_when` 改写为：**需 `audit.approval_signatures` 表（D2b 单独 ADR）**，
  本表只关闭「迁移可追溯」，不关闭会签。

**宁可承认多一张表，也不把两类事实挤进一张表**——后者正是「用配置/判别列代替建模」。

读接口同理：需要新动作 `AUDIT.STATUS.HISTORY.READ`（进 Action Registry 属于修订），
随 D2b 一并提交所有者裁定，不在本 ADR 里偷偷新增动作。

## 4. 数据边界要求（与 D3 同源，但不重复实现）

| 项 | 本 ADR 口径 |
| --- | --- |
| Schema | 新建 `audit`，与 `outbox` 同为「跨域写入、单一所有者」的证据 schema |
| 写入授权 | `project_planning_role` / `asset_service_role`：`USAGE ON SCHEMA audit` + `INSERT`（对齐二者对 `outbox.outbox_events` 的既有授权形态，实测为 INSERT+SELECT） |
| 变更授权 | 所有业务角色 `REVOKE UPDATE, DELETE, TRUNCATE`；触发器二次兜底 |
| RLS | `ENABLE` **且** `FORCE ROW LEVEL SECURITY`，按 `app.current_tenant_id` 隔离（不重蹈「只 enable 不 force」的覆辙） |
| 组织维度 | 组织列随 D3 统一接入策略，本 ADR 不单独写第二套组织判定 |
| 保留期 | **不设**。审计事实按合规要求长期保留；分区策略待数据量触阈值时按 ADR-R06-2 同法处理，现在不做 |

## 5. 不做什么

- **不回填历史**：迁移前发生的状态推进没有 transition 记录，这是既成事实。不编造、不用 outbox
  残余数据拼接「看起来完整」的历史——假历史比缺历史更有害。缺口在 §7 显式登记。
- **不新增审批子对象**：`approval_signatures` 是 D2b 的事，本 ADR 不预建空表。
- **不改状态机语义**：表是观测点，不是规则源。合法边仍只由 `state_machines.yaml` +
  域转移表决定（ADR-R07 / ADR-R20）。
- **不删除 outbox**：事件仍是下游通知与投递重试的载体，保留期不变。

## 6. 实施清单（D2a）

| # | 内容 | 验收 |
| --- | --- | --- |
| 1 | 迁移：建 `audit` schema + 表 + 唯一约束 + 索引 `(tenant_id, resource_type, resource_id, occurred_at)` | `alembic upgrade head` 在真实 PG 通过 |
| 2 | 迁移：append-only 触发器（`BEFORE UPDATE OR DELETE` → `RAISE`） | 负向用例：UPDATE/DELETE 均抛异常，含以表所有者身份 |
| 3 | 迁移：权限（INSERT/SELECT 给两个域角色，REVOKE 变更权）+ RLS enable & force | `information_schema.role_table_grants` 实测与设计一致 |
| 4 | 契约：登记 A12 → `StatusTransition` Schema → `freeze_manifest --write` | 四闸门全绿；`freeze --check` 报「Action 84 = 75+9 / Schema 84 = 76+8」 |
| 5 | 写入路径：两个域的推进函数增加 transition 写入（同事务、先取 event_id） | 正向用例：一次推进 → 一行 transition + 一条事件，互指 |
| 6 | `audit_ref` 改为 `transition_id` | 断言：`audit_ref` 赋值点唯一且可解析 |
| 7 | 边界断言 `data.transition_fact_table` | 迁移缺表/缺触发器/缺唯一约束/缺 RLS force 任一 → 报红 |
| 8 | 负向用例组 | 并发同版本双写 → 唯一约束冲突；事务失败 → 状态/事实/事件同时回滚；归档 outbox 后历史仍可查 |

**第 6 条的可执行判据**（AST，不查字符串）：
`audit_ref` 的赋值只允许出现在 `packages/shared/entity.py::_advance_version`；
其他 `.py` 里出现 `self.audit_ref =` 或 `audit_ref=` 传参即失败。

## 7. 残余风险

1. **历史不可回填**：迁移前的推进记录不存在。若合规要求历史审计，只能从冷备 outbox dump 另行
   重建并标注「重建非原始」，不得混入本表。
2. **无限增长**：append-only + 不设保留期 = 表持续增长。按当前业务量（迁移前 0 行）风险可忽略，
   触阈值时按 ADR-R06-2 的分区法处理，不提前引入分区复杂度。
3. **组织隔离依赖 D3**：本表组织列目前只落值、不参与策略；跨组织可见性仍由应用层保证，
   直到 D3 统一接入 RLS 组织维度。
4. **D2b 未立项**：`approval_signatures` 与读接口需所有者裁定新动作；在此之前，
   「会签」与「按资源查历史」都不可用（但**不是**静默缺失——AUTH-5 的 `unblocks_when` 已指向 D2b）。

## 8. 为什么这是「根治」而不是「加一张表」

判断标准不是「有没有新表」，而是「问题会不会以同一形态再出现」：

| 同类问题 | 本 ADR 是否覆盖 |
| --- | --- |
| 审批证据随保留期消失 | 是：事实表不设保留期，超出 outbox 生命周期 |
| 并发审批产生双记录 | 是：唯一约束绑 `entity_version`，冲突先于应用层判断 |
| 有人绕过审批直接改状态 | 是：`audit_ref` 指针 + 断言，绕过即指针解析失败 |
| 证据被二次录入后漂移 | 是：只存 `evidence_refs`，不存快照 |
| 状态历史需要另一个「日志服务」 | 否（正确）：`audit_log` 若是操作日志就是另一类事实，本 ADR 不建 |
