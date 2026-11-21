# 归属模型与 14 表归属类型矩阵（补齐迁移的前置设计，不含迁移代码）

> **【后续状态 · 2026-11-20 追加】** 本文的「14 表」是**当时的范围判定**，不是最终范围。
> 逐表覆盖矩阵后来发现 `qc_reports` / `shot_specs` / `approval_steps` 三张表
> 有归属列却无归属外键（前者属漏列，后两者被本文判为「派生级、刻意不建」），
> 受控例外可把它们改到不存在的项目。所有者裁定补齐，见
> `docs/audits/2026-11-20-ownership-fk-batch2.md`。**正文不改写**——它记录的是当时的判断，
> 改写它会让「为什么后来变了」失去依据；范围与计数一律以 `docs/audits/mig-recon/acceptance-ledger.md` 为准。

**文档性质**：设计前置材料。本轮**不写迁移、不改 ORM、不建约束**。

**为什么先做这个而不是先写迁移**：所有者裁决——「现在最重要的不是把 14 张表的外键补上，
而是先确认这 14 张表到底应该继承哪一种归属事实。这是根因」。本文件即回答这个根因问题：
逐表给出「权威父实体 / 归属字段 / 引用键 / 唯一约束 / 外键关系 / 冗余字段处置」，
并列出写迁移之前必须先解掉的阻塞项。

**取证来源**：`afs-step-feat`（分支链全量重放到 head，`alembic_version = c4e5f6a7b8d9`）
与 `afs-step-main`（主支链全量重放，`c1a2b3d4e5f6`）的 `information_schema` / `pg_constraint` 实测，
不是从源码推断。

---

## 1. 一个必须先承认的结构缺口：租户层没有外键锚点

| 层 | 对象 | 引用锚点 | 现状 |
| --- | --- | --- | --- |
| L0 租户 | `tenant_id uuid` | **无 `tenants` 表** | 两个集群实测：`relname IN ('tenants','tenant')` 计数均为 **0** |

这意味着：**`tenant_id` 在数据库里没有可引用的目标**，租户边界只能靠
RLS + GUC（`app.current_tenant_id`）在查询时施加，无法用外键在写入时阻止「编造一个不存在的租户」。

这不是本轮要修的问题，但必须写进模型，否则会误以为「加了项目复合外键 = 归属闭环」。
真实闭环只有三段：`组织 → 项目 → 域资源`，租户一致性由**同列传递**保证
（项目外键带上 `tenant_id`，于是资源的 `tenant_id` 必须等于项目的 `tenant_id`），
而「租户本身是否存在」仍无约束。**这是已知残余缺口，不是设计遗漏。**

---

## 2. 权威模型（以 main 已落地的三层为准）

```
L0 租户（无表，仅 RLS 边界）
  └─ L1 组织  project_planning.organizations(organization_id PK, tenant_id)
  │            UNIQUE (organization_id, tenant_id)          ← 让 L2 可复合引用
  │            ※ 仅 main 有；feat 侧**整张表不存在**
  └─ L2 项目  project_planning.projects(entity_id PK varchar, tenant_id, organization_id)
  │            UNIQUE (entity_id, tenant_id)                ← 让 L3 可复合引用（main 有，feat 无）
  │            FK (organization_id, tenant_id) → organizations(organization_id, tenant_id)  ← main 有
  └─ L3 域资源
       ├─ 项目级：必须能证明「该项目存在」且「与我同租户」（可选：与我同组织）
       ├─ 组织级：直接引用组织，不强行造项目
       └─ 派生级：归属由更近的父对象派生，本表不重复保存归属事实
```

**两条原则来自 main 的既有裁定，本轮不重新讨论**：

1. **组织归属从项目派生，不在域资源上冗余保存**。证据：main 的 `asset_versions` 与
   `episodes` 的字段清单**都没有 `organization_id`**；`qc_reports` 连 `project_id` 都没有，
   只保留 `asset_version_id`，归属全靠父对象派生。
2. **不无条件给所有表加三列复合外键**。所有者原话：「这个原则应当检查是否适用于其他项目级资源，
   而不是无条件给 14 张表全部加上三列复合外键。」

---

## 3. 两种写法，以及为什么单列不够

### 写法 A：两列外键（main 现行）

```sql
FOREIGN KEY (project_id, tenant_id) REFERENCES project_planning.projects(entity_id, tenant_id)
```

**保证**：项目存在；资源与项目同租户。
**不保证**：资源上的 `organization_id` 与项目所属组织一致——如果域资源自己还存了 `organization_id`（feat 的 14 张表**全部**存了），
它可以指向另一个组织，而两列外键**看不见**这个字段。

### 写法 B：三列外键（本项目建议，针对保留 `organization_id` 的表）

```sql
-- 先给项目加第三个唯一键
ALTER TABLE project_planning.projects
  ADD CONSTRAINT uq_projects_entity_tenant_org UNIQUE (entity_id, tenant_id, organization_id);

-- 域资源用三列复合外键，把「组织一致性」也纳入数据库约束
ALTER TABLE <schema>.<table>
  ADD CONSTRAINT fk_<table>_project_tenant_org
  FOREIGN KEY (project_id, tenant_id, organization_id)
  REFERENCES project_planning.projects(entity_id, tenant_id, organization_id);
```

**保证**：项目存在 + 同租户 + 组织与项目一致。**「组织错配」从"写入时无人拦"变成"写入即失败"，
且不需要删列、不需要触发器。**

> 关键判断：feat 的 14 张表都带 `organization_id`，而项目级资源**本不该带**这个字段（main 的原则 1）。
> 但**删列是破坏性变更**，且 feat 的 RLS 策略按 `organization_id` 过滤。
> 因此本轮建议：**保留列，用三列外键约束其取值**——既不破坏现有策略，又堵住错配。
> 若后续 IP-4 决定收敛到 main 的「不存 organization_id」，则三列外键可随之降为两列（演进路径是通的）。

### 三条硬前提（缺一不可）

| 前提 | main | feat | 不满足的后果 |
| --- | --- | --- | --- |
| `organizations` 表存在且有 `UNIQUE (organization_id, tenant_id)` | ✓ | **✗ 表都不存在** | 组织级资源无引用目标；三列/两列外键都建不了 |
| `projects` 有 `UNIQUE (entity_id, tenant_id)` | ✓ | **✗ 无** | `ERROR 42830: there is no unique constraint matching given keys for referenced table` |
| `projects.organization_id` 非空 | **✗ 可空**（实测 `is_nullable=YES`） | ✓ `NOT NULL` | 可空时外键按 `MATCH SIMPLE` **整体跳过校验**：把 `organization_id` 置 NULL 即可绕开组织归属约束（见 T3 场景 6） |

> **这条前提在 main 上是不成立的**。`projects.organization_id` 可空 + 外键 `(organization_id, tenant_id)`
> 意味着：**一条 `organization_id IS NULL` 的项目记录可以完全绕过组织归属校验**。
> 这是 main 现有的一个真实缺口，不是 feat 引入的。补齐迁移时若照抄 main 的约束而不加 `NOT NULL`，
> 会把同一个缺口复制到 14 张表上。**建议：feat 侧新建三列外键时，`projects.organization_id` 已是 `NOT NULL`，
> 可直接沿用；main 侧的可空性问题单独登记，不在本轮改。**

**结论：14 张表的归属外键在 feat 底座上物理不可建**，这不是数据脏，是**目标键不存在**。
写迁移之前必须先在 feat 谱系补 `organizations` + 两个唯一约束，否则第一条 `ADD CONSTRAINT` 就会以 42830 失败。

---

## 4. 14 表归属类型矩阵

**分类定义**：

- **A 项目级**：归属事实在本表（`project_id` 是权威），需要项目外键；
- **B 组织级**：没有 `project_id`，归属到组织，需要组织外键；
- **C 派生级**：存在更近的父对象（如 `ticket_id`、`shot_revision_id`），归属应经父派生，本表的 `project_id` 属冗余。

**约束命名**沿用 main 风格 `fk_<table>_<target>`（main 已有 `fk_asset_versions_project_tenant`、`fk_projects_organization_tenant`）。

| # | 表 | 类型 | 权威父实体 | 现有归属字段 | 目标引用键 | 所需唯一约束（被引用侧） | 目标外键 | 冗余字段处置 | 阻塞 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `generation_execution.production_routes` | **B 组织级** | Organization | `tenant_id`, `organization_id`（**无 `project_id`**） | `(organization_id, tenant_id)` | `organizations` 已有 `UNIQUE (organization_id, tenant_id)`（main） | `fk_production_routes_organization_tenant` | 无 | feat 无 `organizations` 表 |
| 2 | `generation_execution.generation_tasks` | A 项目级 | Project | `tenant_id`, `organization_id`, `project_id` | `(project_id, tenant_id, organization_id)` | `projects` 需新增三列唯一键 | `fk_generation_tasks_project_tenant_org` | `organization_id` 保留，由三列外键约束 | 三列唯一键缺失；`shot_id`/`route_id` 悬空（见 §5） |
| 3 | `generation_execution.shot_route_selections` | A 项目级 | Project | `tenant_id`, `organization_id`, `project_id` | `(project_id, tenant_id, organization_id)` | 同上 | `fk_shot_route_selections_project_tenant_org` | 同上 | 已有 `fk_…_route_id` + `ux_shot_route_selection_effective(tenant_id, project_id, shot_id)`，可作参照 |
| 4 | `shot_planning.shot_drafts` | A 项目级 | Project | `tenant_id`, `organization_id`, `project_id` | `(project_id, tenant_id, organization_id)` | 同上 | `fk_shot_drafts_project_tenant_org` | 同上 | 业务父实为 Episode，但只有 `episode_ref`（字符串），无 FK 目标 |
| 5 | `shot_planning.shot_revisions` | A 项目级 | Project | `tenant_id`, `organization_id`, `project_id`, `shot_id` | `(project_id, tenant_id, organization_id)` | 同上 | `fk_shot_revisions_project_tenant_org` | 同上 | **`shot_id` 无目标表**（`shots` 未实现）；`project_id` 目前是唯一可用的归属锚点，不能判为冗余 |
| 6 | `shot_planning.shot_specs` | **C 派生级** | ShotRevision | `shot_revision_id`, `shot_id`, `project_id`, `tenant_id`, `organization_id` | `(shot_revision_id)` → `shot_revisions(entity_id)` | `shot_revisions` 需 `UNIQUE (entity_id)`（PK 已够） | `fk_shot_specs_shot_revision` | **`project_id` 冗余**：可经 revision 派生；建议先建父 FK，暂不为 `project_id` 建外键 | 若先给 `project_id` 建外键，会固化「两套归属可独立漂移」——正是 `qc_reports` 的旧问题 |
| 7 | `shot_planning.shot_visual_plans` | A 项目级 | Project | `tenant_id`, `organization_id`, `project_id`, `shot_id` | `(project_id, tenant_id, organization_id)` | 三列唯一键 | `fk_shot_visual_plans_project_tenant_org` | 同上 | `shot_id` 悬空 |
| 8 | `shot_planning.constraint_packs` | A 项目级 | Project | `tenant_id`, `organization_id`, `project_id`, `scene_id`(可空) | `(project_id, tenant_id, organization_id)` | 三列唯一键 | `fk_constraint_packs_project_tenant_org` | 同上 | `scene_id` 可空且无目标表（Scene 已建模未实施）；`shot_ids` 为数组，无法用外键 |
| 9 | `asset_service.delivery_manifests` | A 项目级 | Project | `tenant_id`, `organization_id`, `project_id` | `(project_id, tenant_id, organization_id)` | 三列唯一键 | `fk_delivery_manifests_project_tenant_org` | 同上 | `asset_refs` 为数组，无外键 |
| 10 | `qc_governance.gate_decisions` | A 项目级 | Project | `tenant_id`, `organization_id`, `project_id`, `gate_instance_id` | `(project_id, tenant_id, organization_id)` | 三列唯一键 | `fk_gate_decisions_project_tenant_org` | 同上 | `gate_instance_id` 无目标表（GateInstance 未实现） |
| 11 | `qc_governance.qc_review_sessions` | A → C（可分两步） | Project（现）／AssetVersion（目标） | `tenant_id`, `organization_id`, `project_id`, `asset_version_id` | 第一步 `(project_id, tenant_id, organization_id)`；第二步 `(asset_version_id)` | 三列唯一键；`asset_versions` PK 已够 | 第一步 `fk_qc_review_sessions_project_tenant_org`；第二步 `fk_qc_review_sessions_asset_version` | 建好父 FK 后，`project_id`/`organization_id` 可降为派生（与 main 的 `qc_reports` 一致） | `asset_version_id` 当前**无外键**，先补它成本更低、收益更直接 |
| 12 | `workflow.approval_flows` | A 项目级 | Project | `tenant_id`, `organization_id`, `project_id` | `(project_id, tenant_id, organization_id)` | 三列唯一键 | `fk_approval_flows_project_tenant_org` | 同上 | `action_key`/`resource_type`+`resource_id` 是多态引用，不能建外键 |
| 13 | `workflow.approval_tickets` | A 项目级 | Project | `tenant_id`, `organization_id`, `project_id`, `flow_id` | `(project_id, tenant_id, organization_id)`；另加 `(flow_id)` | 三列唯一键；`approval_flows` PK 已够 | `fk_approval_tickets_project_tenant_org` + `fk_approval_tickets_flow` | 同上 | `flow_id` 无外键 |
| 14 | `workflow.approval_steps` | **C 派生级** | ApprovalTicket | `ticket_id`, `project_id`, `tenant_id`, `organization_id` | `(ticket_id)` → `approval_tickets(entity_id)` | PK 已够 | `fk_approval_steps_ticket` | **`project_id` 冗余**：票已有项目归属；步骤再存一份即可独立漂移 | 已有 `uq_workflow_approval_steps_one_vote_per_group`，注意别与新 FK 冲突 |

---

## 5. 矩阵的四个统计结论

**5.1 有 13 张表带 `project_id`，1 张不带。**

`production_routes`（路线是组织级资产，被多个项目共享）——这与 `shot_route_selections` 引用它、
且选择结果带 `project_id` 的设计自洽。**不能因为"别的表都有"就给它补 `project_id`。**

**5.2 14 张表全部带 `organization_id`，而 main 的域资源不带。**

对照证据：main 的 `asset_versions`（22 列）、`episodes`（14 列）字段清单里**没有 `organization_id`**。
这 14 张表的 `organization_id` 来自分支的「`organization_id = tenant_id` 单组织租户基线」，
是**把组织层级压成租户标识**的产物。⇒ 三列外键正是为「保留列但要约束它」而设计的解法。

**5.3 归属外键 0 条，业务引用外键只有 1 条。**

| 类别 | 现状 |
| --- | --- |
| `(project_id, tenant_id)` 类归属外键 | **0 条**（14 张表全部缺失） |
| `(organization_id, tenant_id)` 类归属外键 | **0 条** |
| 业务引用外键 | **1 条**：`shot_route_selections.route_id → production_routes.entity_id` |

**5.4 悬空业务引用（有列、无目标表、无外键）—— 这部分不属于「归属」问题，不能混进来一起补**

| 列 | 出现在 | 目标 | 状态 |
| --- | --- | --- | --- |
| `shot_id` | `generation_tasks`、`shot_route_selections`、`shot_revisions`、`shot_specs`、`shot_visual_plans`（5 张） | `Shot` | ADR-R16/R17 已建模（`Shot` 独立实体），**运行时未实施** |
| `shot_revision_id` | `shot_specs` | `shot_revisions` | 目标表存在，**可直接建 FK** |
| `ticket_id` / `flow_id` | `approval_steps` / `approval_tickets` | `approval_tickets` / `approval_flows` | 目标表存在，**可直接建 FK** |
| `asset_version_id` | `qc_review_sessions` | `asset_versions` | 目标表存在，**可直接建 FK** |
| `gate_instance_id` | `gate_decisions` | `GateInstance` | 未实现 |
| `scene_id`（可空） | `constraint_packs` | `Scene` | 已建模未实施 |

> 这张表是**给迁移划边界用的**：本轮裁决只授权讨论「归属」，把 5 类悬空引用混进来会
> 让一次迁移承担两种性质的风险。**归属先立、引用后补**，两条线分开。

---

## 6. 与 main 现有资源的对照（模型自洽性验证）

若模型正确，main 已落地的资源应当**恰好**符合它：

| 现有对象 | 模型预测 | 实测 | 是否自洽 |
| --- | --- | --- | --- |
| `projects` | L2，有 `UNIQUE (entity_id, tenant_id)` + `FK (organization_id, tenant_id) → organizations` | 两条都在 | ✓ |
| `episodes` | A 项目级，`FK (project_id)` 存在、**无** `organization_id` | `fk_episodes_project` 存在；字段无 `organization_id` | ✓（注：`episodes` 用的是**单列** `project_id` 外键，未带 `tenant_id`——这是 main 已有的不一致，登记为遗留项，不在本轮改） |
| `asset_versions` | A 项目级，`FK (project_id, tenant_id)` 两列、无 `organization_id` | `fk_asset_versions_project_tenant` 存在；字段无 `organization_id` | ✓ |
| `qc_reports` | C 派生级，只有 `asset_version_id`，无 `project_id`/`organization_id` | 14 列，仅有 `asset_version_id` + `tenant_id` | ✓ |

**一条必须登记的遗留不一致**：`episodes` 用的是单列 `project_id → projects(entity_id)`，
**没有**带 `tenant_id`，所以「剧集与项目同租户」在数据库层**没有被强制**。
它不违反模型（模型不要求所有表都用复合键），但它是 main 内部唯一一处「项目级资源只用单列外键」，
补齐迁移时若以 `episodes` 为范式，会把同样的漏洞复制 14 份。**建议以 `asset_versions` 为范式，不以 `episodes` 为范式。**

---

## 7. 写迁移之前必须先解的 5 项阻塞（按顺序）

| # | 阻塞 | 为什么必须先解 | 解除判据 |
| --- | --- | --- | --- |
| B1 | feat 谱系没有 `organizations` 表 | 组织级外键（#1）与三列外键的引用目标都不存在 | `project_planning.organizations` 在 feat 链上存在，且带 `UNIQUE (organization_id, tenant_id)` |
| B2 | feat 的 `projects` 无 `UNIQUE (entity_id, tenant_id)` | 任何项目外键都会 42830 失败 | 唯一键存在（实测：当前直接 `ADD CONSTRAINT` 会失败，见 T3 场景 0） |
| B3 | `projects.organization_id` 可空性在两侧相反 | main **可空** ⇒ 其组织外键可被 `NULL` 绕过；feat **NOT NULL** ⇒ 可直接沿用 | 明确 `NOT NULL`；main 侧缺口单独登记（实测值见 §3 前提表） |
| B4 | feat 存量 `organization_id = tenant_id` 的回填语义 | 若历史项目真实组织 ≠ 租户，三列外键会拒绝历史数据 | 逐行核对「项目所属组织」与「资源上的 organization_id」是否一致；不一致比例与处置方案须先出证据 |
| B5 | `legacy-unassigned` 哨兵值 | 不是真实项目，建外键必然失败；**不能自动当真实项目** | 明确映射规则（迁移到真实项目 / 移入隔离区 / 删除），且需人工确认 |

**B4 与 B5 正是 T3 实验要量化验证的对象**，因此执行顺序是：T3 取证 → 按证据定 B4/B5 处置 → 再写迁移。

---

## 8. 本文件明确不做的事

| 不做 | 原因 |
| --- | --- |
| 写迁移文件 / 建约束 / 改 ORM | 所有者裁决「不批准开始写补齐迁移」 |
| 删 `organization_id` 列 | 破坏性变更，且 feat 的 RLS 策略依赖它；演进路径已保留（三列 → 两列） |
| 给 14 张表无条件加三列外键 | 所有者明确反对；本文件已区分 A/B/C 三类（2 张为 C 派生级、1 张为 B 组织级） |
| 补 `shot_id` 等悬空引用 | 属另一条线（业务引用），不是归属问题 |
| 讨论 `episodes` 单列外键的整改 | main 的遗留项，改动会波及主支已有对象，需单独裁决 |

---

## 9. 复核方式

```bash
# 14 张表归属列（应看到 13 张有 project_id、全部有 organization_id）
psql -p 15444 -U postgres -d ai_film_industrial -c "
SELECT table_schema||'.'||table_name, column_name
FROM information_schema.columns
WHERE table_name IN ('production_routes','generation_tasks','shot_drafts','shot_revisions','shot_specs',
  'shot_visual_plans','constraint_packs','delivery_manifests','shot_route_selections','gate_decisions',
  'qc_review_sessions','approval_flows','approval_tickets','approval_steps')
  AND column_name IN ('project_id','organization_id','tenant_id')
ORDER BY 1,2;"

# 现有约束（应看到：14 张表只有 1 条业务引用外键）
psql -p 15444 -U postgres -d ai_film_industrial -c "
SELECT n.nspname||'.'||c.relname, con.conname, pg_get_constraintdef(con.oid)
FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relname IN (...) AND con.contype IN ('f','u');"
```
