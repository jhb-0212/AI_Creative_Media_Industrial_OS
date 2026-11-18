# ADR-R23：状态迁移事实表（审批可追溯的第一类事实）

- 状态：已采纳，待实施（2026-11-18；同日按评审 R1–R5 修订）
- 关联：N-02、ADR-R07（状态机单一事实源）、ADR-R11（不可变性与授权基线）、ADR-R06-2（outbox
  保留期与分区）、ADR-R22（基线事实源单一化）
- 上游依据：`docs/audits/2026-11-18-full-repo-baseline-and-root-cause-audit.md` §7 N-02
- 触发的契约演进：`V205-R14-A12`（新增 `StatusTransition` 对象 Schema，待登记）
- 修订记录：R1 枚举真实写入路径（原稿误称每域已有唯一 choke point）；R2 `event_id` 改非空并由
  延迟约束触发器保证事件存在；R3 遗留 `audit_ref` 处理；R4 `resource_id` 与实体同型；
  R5 补 `TRUNCATE` 守卫（含既有两表同缺口）

## 1. 问题的实质：审批被建模成「一次 HTTP 请求的副作用」

当前一次状态迁移留三处痕：

| 落点 | 性质 | 会不会消失 |
| --- | --- | --- |
| 实体 `audit_ref` | 单字段赋值 `f"{owner_action}:{actor}"` | **下一次迁移就覆盖** |
| outbox 事件 | 完整 payload（from/to/actor/action/证据计数） | **保留期到就 DETACH 归档** |
| — | 无第三处 | 数据库 12 张表里没有 `approvals` / `status_transitions` / `audit_log` |

事实本身是齐的——各域推进函数里 from_status、to_status、actor、owner_action、qc_report_count
全都有，只是**唯一持久落点是一张会归档的表**。

于是「谁在什么证据下批准了什么」这个问题，在保留期之后**不可查询**，只能翻冷备 dump。
而实体上的 `audit_ref` 是自由字符串——实测库里存在 `ASSET.STATUS.CHANGE:director_1`、
`ASSET.VERSION.APPROVE:director_3`，它连「哪一次」都说不清。

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
    transition_id   uuid         PRIMARY KEY,
    tenant_id       uuid         NOT NULL,
    organization_id uuid         NULL,          -- 全局资源（无组织归属）为 NULL
    resource_type   text         NOT NULL,      -- 对象类型，与 Schema $defs 同名
    resource_id     varchar(64)  NOT NULL,      -- 与实体表 entity_id 同型（见要点 2）
    entity_version  integer      NOT NULL,      -- 推进完成后的乐观锁版本
    from_status     text         NOT NULL,
    to_status       text         NOT NULL,
    owner_action    text         NOT NULL,      -- Action Registry 的 action_key
    actor_subject   text         NOT NULL,      -- 谁触发的
    authority_ref   text         NULL,          -- 授权凭据 jti（谁授的权），可空
    evidence_refs   text[]       NOT NULL DEFAULT '{}',  -- 证据对象 id 列表，不是摘要
    occurred_at     timestamptz  NOT NULL DEFAULT now(),
    event_id        uuid         NOT NULL,      -- 对应 outbox 事件（非空，见要点 3）

    CONSTRAINT uq_status_transition_version
        UNIQUE (tenant_id, resource_type, resource_id, entity_version),
    CONSTRAINT ck_status_transition_advances
        CHECK (from_status <> to_status)
);
```

五个设计要点，每条都对应一个「不做就会重演」的失败形态：

**1. 唯一约束绑在 `entity_version` 上，而不是绑在「时间」上。**
乐观锁已经在 `save(entity, expected_version)` 里保证「同一版本只推进一次」；把它抬到事实表，
就使「并发两次审批产生两条记录」**结构上不可能**——第二个写者拿不到同一个 version，
唯一约束先于任何应用层判断失败。这比在 Service 里加 `if` 强，因为它不依赖谁记得加。

配套 `CHECK (from_status <> to_status)`：本表只记录**真实推进**。这样「用 no-op 行表示会签」
这条退路在结构上被关掉——会签是另一类事实（D2-e），不能借本表的形状蒙混过去。

**2. `resource_id` 与实体表同型（`varchar(64)`），不是 `uuid`。**
`packages/shared/orm.py` 的 `entity_id` 是 `Mapped[str] = mapped_column(String(64), …)`，
基线 §8.2 允许「UUID 或等效全局唯一 ID」。若本表写成 `uuid`，任何非 UUID 形态的合法标识
在写事实行时都会失败——事实表反过来限制了它观测的对象，这是因果倒置。取与实体完全相同的
表示，让「同一个 id 在两处是同一个值」成为类型层面的默认。

**3. `event_id` 非空，且由延迟约束触发器保证事件真实存在。**
原稿把 `event_id` 设为可空，但同一份文档又要求「迁移与事件同事务、互指」。可空 + 域角色持有
`INSERT` = 允许写出「有迁移、没事件」的行，制度靠自觉。

先看**为什么不能用外键**（实测，不是推断）：

```
outbox_events_pkey :: PRIMARY KEY (event_id, occurred_at)
分区键            :: RANGE (occurred_at)
```

分区表上的外键要求被引用列上存在唯一约束，且该约束必须包含分区键——`event_id` 单独不唯一，
所以 `status_transitions.event_id → outbox_events.event_id` 这个外键**建不出来**。

因此用**延迟约束触发器**（`AFTER INSERT … DEFERRABLE INITIALLY DEFERRED`）在**提交时**校验
事件存在，不存在则 `RAISE EXCEPTION`。三个收益：

- 与 `NOT NULL` 合起来，把「迁移事实没有对应事件」变成结构上不可能，而不是靠写入顺序；
- 因为是**提交时**校验，写入顺序不再重要（先事件后事实、先事实后事件都合法），于是少了一条
  「必须记得先写哪个」的口头规则——规则一旦需要人记，早晚会破；
- 校验函数以 `SECURITY DEFINER` 由 `audit` schema owner 持有：否则调用方在 `outbox` 上的
  RLS 可见性会让「事件确实存在」被误判为不存在，把正确写入拒掉。

**4. 只存 `evidence_refs`，不存证据快照。**
存 `qc_report_count=3` 之类的快照，等于给「当时有几份 QC 报告」造了第二个事实源，
而 `qc_reports` 本身已经 append-only 不可变（ADR-R11），随时可重新计数。
**能引用就不要复制**——复制出来的数字一定会和源漂移。

**5. append-only 用触发器兜底，且必须覆盖 `TRUNCATE`。**
REVOKE 只对业务角色生效，迁移角色/表所有者照样能改。原稿只写 `BEFORE UPDATE OR DELETE`——
**漏了 `TRUNCATE`**：`TRUNCATE` 不触发行级触发器，一个持有 `TRUNCATE` 的迁移角色可以把整段
审计历史清空而触发器毫无反应。所以是两条：

```sql
-- 行级：改/删单行
CREATE TRIGGER trg_status_transitions_guard
    BEFORE UPDATE OR DELETE ON audit.status_transitions
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();

-- 语句级：清空整表（TRUNCATE 只支持 FOR EACH STATEMENT）
CREATE TRIGGER trg_status_transitions_no_truncate
    BEFORE TRUNCATE ON audit.status_transitions
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_truncate();
```

**同型缺口在既有代码里也存在（实测，非推测）**：`d1e2f3a4b5c6` 给 `asset_service.qc_reports`
与 `asset_versions` 装的守卫只有 `BEFORE UPDATE OR DELETE`，两张表都能被 `TRUNCATE` 清空。
本 ADR 的实施清单把这两张表一并补齐——不允许「新表按新标准、旧表按旧标准」，
那等于承认同一类不变量有两套强度。

### D2-b 写入点收敛：先承认现在是 5 条路径，再收敛成每域 1 条

原稿写「每个域本来就有唯一推进函数」，**这句是错的**。实测全部持久化迁移路径：

| # | 路径 | 位置 | 形态 |
| --- | --- | --- | --- |
| 1 | `AssetService._advance()` | `asset_service.py:443` | 守卫 → 乐观锁 save → 写事件 |
| 2 | `AssetService.create_qc_report()` 内联 | `asset_service.py:282–293` | 同样四步**复制一遍**（payload 需 `qc_report_id`） |
| 3 | 项目推进 | `project_service.py:178` | 同上 |
| 4 | 集数推进 | `episode_service.py:189` | 同上 |
| 5 | 剧本推进 | `script_service.py:337` | 同上 |

根因也就清楚了：**四步序列被复制了 5 次**，因为每处 payload 不同。若把「写 transition 行」
按这 5 处铺开，就是把已经犯过的错误（同一规则多份实现）再犯一遍。所以记录点必须建在
**收敛之后**的单点上，收敛本身是 D2-b 的**前置步骤**：

**D2-b-0（前置）每域收敛为一个推进函数**

- `asset_service`：`_advance()` 增加 `extra_payload: Mapping[str, Any] | None = None`，
  `create_qc_report()` 改为「登记报告 → 调 `_advance(..., event_type="QC_REPORT_CREATED",
  extra_payload={"qc_report_id": …})`」；「已在 TECHNICAL_QC、只发事件不迁移」的分支抽成
  `_record_event()`——该分支**没有状态推进**，不属于本表，单独留一个出口是诚实的。
- `project_planning`：新增域内单一 `advance()`（`service/transition.py`），三个 service 共用，
  实体与 payload 走参数。它**不是新的 Manager**：无状态、无缓存、无业务判断，只承载
  「守卫 → 乐观锁 → 事实 → 事件」这一条固定序列。

收敛后，本表的写入只在两个函数里发生（每域一个），于是不变量成立：
**「有状态推进 ⇒ 必有 transition 行」，且只有一条路径能产生它。**

**防止第 6 条路径偷偷长出来**：边界断言用 AST 查 `transition_to(` 的调用点白名单
（`asset:_advance`、`project_planning:transition.advance`），出现第三处即报红。
这比「统计调用次数」可靠——白名单能指出是谁越界，计数只会说「多了一个」。

### D2-c `audit_ref` 降级为指针，并明确遗留值的处理

`audit_ref` 不再接受 `f"{action}:{actor}"` 这类自由字符串，改为**最近一条 transition 的
`transition_id`**，序列化形如 `tr:<uuid>`。

**遗留值必须区别对待，否则升级即误报（原稿的 P1 级缺陷）**：迁移前实体上的 `audit_ref`
是自由串（实测 `ASSET.STATUS.CHANGE:director_1`），§5 又明确不回填历史。若要求「每个非空
`audit_ref` 都必须能在表里解析到」，这些合法历史记录会被判为损坏、读操作失败。

判定规则（自描述，不用时间戳——`updated_at` 可被业务改、时钟不可靠）：

| `audit_ref` | 解释 | 读侧行为 |
| --- | --- | --- |
| `NULL` | 从未推进 | 不解析 |
| `tr:<uuid>` | D2a 之后的指针 | **必须解析成功**，否则报「有人绕过唯一路径写了状态」 |
| 其他任意串 | D2a 之前的遗留凭据摘要 | 标记为 `legacy`，**不解析、不判错** |

写侧由 AST 断言封闭：`audit_ref` 的赋值只允许出现在 `packages/shared/entity.py::_advance_version`，
因此 D2a 之后不可能再产生新的遗留串。遗留值的「新鲜度」由写侧保证，不由读侧猜测。

另一处必须说清的语义：`status_transitions` 里存在某条 `transition_id`，而实体 `audit_ref`
已指向**更新的**一条——这是正常历史（指针只指最近一次），不是错误。只有「`tr:` 前缀解析失败」
才是异常。

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

- 本表记录的是「推进**已经**发生」，一行 = 一次推进（且 `CHECK (from_status <> to_status)`
  已经把 no-op 行堵死）；
- 会签需要的是「推进**之前**，某个席位签了字」——这是**另一类事实**，有独立的生命周期
  （可能被驳回、可能超时失效、可能凑不齐）；
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
| 变更授权 | 所有业务角色 `REVOKE UPDATE, DELETE, TRUNCATE`；触发器二次兜底（含 TRUNCATE） |
| RLS | `ENABLE` **且** `FORCE ROW LEVEL SECURITY`，按 `app.current_tenant_id` 隔离（不重蹈「只 enable 不 force」的覆辙） |
| 组织维度 | 组织列随 D3 统一接入策略，本 ADR 不单独写第二套组织判定 |
| 保留期 | **不设**。审计事实按合规要求长期保留；分区策略待数据量触阈值时按 ADR-R06-2 同法处理，现在不做 |

## 5. 不做什么

- **不回填历史**：迁移前发生的状态推进没有 transition 记录，这是既成事实。不编造、不用 outbox
  残余数据拼接「看起来完整」的历史——假历史比缺历史更有害。遗留 `audit_ref` 按 D2-c 标记处理，
  不反推 transition 行。
- **不新增审批子对象**：`approval_signatures` 是 D2b 的事，本 ADR 不预建空表。
- **不改状态机语义**：表是观测点，不是规则源。合法边仍只由 `state_machines.yaml` +
  域转移表决定（ADR-R07 / ADR-R20）。
- **不删除 outbox**：事件仍是下游通知与投递重试的载体，保留期不变。

## 6. 实施清单（D2a）

| # | 内容 | 验收 |
| --- | --- | --- |
| 0 | **前置**：收敛写入路径为每域 1 条（D2-b-0），含 `_advance` 的 `extra_payload` 与 project_planning 共用 `advance()` | 5 条路径 → 2 条；AST 白名单断言（`transition_to` 调用点）通过；既有用例全绿（行为不变） |
| 1 | 迁移：建 `audit` schema + 表 + 唯一约束 + `CHECK(from<>to)` + 索引 `(tenant_id, resource_type, resource_id, occurred_at)` | `alembic upgrade head` 在真实 PG 通过 |
| 2 | 迁移：三条守卫——行级 `UPDATE/DELETE`、语句级 `TRUNCATE`、延迟约束触发器（提交时校验 `event_id` 存在） | 负向用例：UPDATE / DELETE / TRUNCATE 均抛异常（**含以表所有者身份**）；无事件的事实行在提交时失败 |
| 3 | 迁移：**补齐既有两表**的 `TRUNCATE` 守卫（`asset_service.qc_reports`、`asset_service.asset_versions`） | 负向用例：两张表 TRUNCATE 均被拒 |
| 4 | 迁移：权限（INSERT/SELECT 给两个域角色，REVOKE 变更权含 TRUNCATE）+ RLS enable & force | `information_schema.role_table_grants` 实测与设计一致 |
| 5 | 契约：登记 A12 → `StatusTransition` Schema → `freeze_manifest --write` | 四闸门全绿；`freeze --check` 报「Action 84 = 75+9 / Schema 84 = 76+8」 |
| 6 | 写入路径：两个推进函数增加 transition 写入（同事务；`event_id` 先生成） | 正向用例：一次推进 → 一行 transition + 一条事件，互指 |
| 7 | `audit_ref` 改为 `tr:<transition_id>` 指针，读侧按 D2-c 三态解释 | 用例：legacy 串不被判错且标记 `legacy`；`tr:` 解析失败即报错 |
| 8 | 边界断言 `data.transition_fact_table` | 迁移缺表/缺触发器（含 TRUNCATE）/缺唯一约束/缺 RLS force 任一 → 报红 |
| 9 | 负向用例组 | 并发同版本双写 → 唯一约束冲突；事务失败 → 状态/事实/事件同时回滚；归档 outbox 分区后历史仍可查 |

**第 7 条的可执行判据**（AST，不查字符串）：
`audit_ref` 的赋值只允许出现在 `packages/shared/entity.py::_advance_version`；
其他 `.py` 里出现 `self.audit_ref =` 或 `audit_ref=` 传参即失败。

## 7. 残余风险

1. **历史不可回填**：迁移前的推进记录不存在。若合规要求历史审计，只能从冷备 outbox dump 另行
   重建并标注「重建非原始」，不得混入本表。
2. **遗留 `audit_ref` 不可解析**：升级后旧实体的 `audit_ref` 是自由串，按 D2-c 标记为 `legacy`，
   不能用于追溯「哪一次」。这是缺历史的可见形态，不是静默失败。
3. **无限增长**：append-only + 不设保留期 = 表持续增长。按当前业务量（迁移前 0 行）风险可忽略，
   触阈值时按 ADR-R06-2 的分区法处理，不提前引入分区复杂度。
4. **组织隔离依赖 D3**：本表组织列目前只落值、不参与策略；跨组织可见性仍由应用层保证，
   直到 D3 统一接入 RLS 组织维度。
5. **`TRUNCATE` 守卫的剩余绕过面**：触发器对表所有者与超级用户同样生效，但后者可用
   `ALTER TABLE … DISABLE TRIGGER` 或 `SET session_replication_role = replica` 关掉。
   这不是应用路径，属运维权限范畴：生产迁移角色凭据不进业务容器（F05 已落地），DDL 变更走评审。
   登记为已知边界，不假装已被应用层防住。
6. **D2b 未立项**：`approval_signatures` 与读接口需所有者裁定新动作；在此之前，
   「会签」与「按资源查历史」都不可用（但**不是**静默缺失——AUTH-5 的 `unblocks_when` 已指向 D2b）。

## 8. 为什么这是「根治」而不是「加一张表」

判断标准不是「有没有新表」，而是「问题会不会以同一形态再出现」：

| 同类问题 | 本 ADR 是否覆盖 |
| --- | --- |
| 审批证据随保留期消失 | 是：事实表不设保留期，超出 outbox 生命周期 |
| 并发审批产生双记录 | 是：唯一约束绑 `entity_version`，冲突先于应用层判断 |
| 有人绕过审批直接改状态 | 是：`audit_ref` 指针 + 写侧 AST 断言 + 读侧三态判定 |
| 记录点按 5 处铺开、漏一处就静默失效 | 是：先收敛为每域 1 条，再用 AST 白名单封住第 6 条 |
| 事实行有迁移没事件 | 是：`event_id NOT NULL` + 提交时约束触发器（外键在分区表上不可用，已实测） |
| 整表被 TRUNCATE 清空 | 是：语句级触发器，且既有两表同缺口一并补齐 |
| 证据被二次录入后漂移 | 是：只存 `evidence_refs`，不存快照 |
| 状态历史需要另一个「日志服务」 | 否（正确）：`audit_log` 若是操作日志就是另一类事实，本 ADR 不建 |
