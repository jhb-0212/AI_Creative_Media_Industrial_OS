# ADR-R17：归属链与项目/组织级行隔离

- 状态：已接受
- 日期：2026-11-16
- 关联：审查第三轮第 11 条「同租户不同项目未隔离，Project 无 organization_id」；
  铁律 2「不屎山代码」、铁律 3「禁止补丁式演进」、铁律 5「防呆 + 自动化约束」；
  `docs/adr/ADR-R10-partition-rls-and-grant-baseline.md`（租户 RLS 的前置）、
  `docs/adr/ADR-R16-database-role-per-domain.md`（域角色 = 谁能写哪张表，
  本 ADR = 谁能看/写哪些行）
- 迁移：`d3e4f5a6b7c8_add_organization_and_project_ownership.py`（加列与回填）、
  `e7f8a9b0c1d2_add_project_scope_rls.py`（限制性策略）
- 契约修订：`contracts/phase0/contract_amendments.yaml` → `V205-R14-A2`

## 1. 背景

审查第三轮第 11 条成立，而且它是**最容易被误判为已修**的一条：

> 同租户不同项目未隔离，`Project` 领域模型无组织归属字段，
> 租户 RLS ≠ 项目级授权。

之前的防线只有一条 RLS 策略：

```sql
USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
```

它回答的是「哪家客户」。但一个租户（客户）内部还有两层结构：组织（制作主体）
与项目。缺了这两层，后果不是抽象的「隔离不够」，而是三条具体事实：

- `asset_versions` **没有任何归属字段**。资产是悬空的：数据库里没有任何一列
  能回答「这个资产是谁的」，于是同租户内 A 项目的主体可以读、写 B 项目的资产，
  并且这不是漏洞利用，是正常调用就能做到；
- `qc_reports` 同样悬空，检测证据可以跨项目漂流，事后审计无法回答
  「这份结论属于哪个项目」；
- `Project` 没有 `organization_id`，而 Action Registry 里大量动作把 scope
  写成 `tenant+organization`——**契约声明了范围，数据里却没有承载它的列**。
  声明与实现之间没有任何东西能发现这个缺口。

## 2. 决策

把归属链补进数据模型，并把拒绝逻辑放在数据库层。三段缺一不可。

### 2.1 归属链（数据侧）

| 层 | 承载列 | 表 |
|---|---|---|
| 租户（客户） | `tenant_id` | 全部业务表 |
| 组织（制作主体） | `organization_id` | `projects` / `episodes` / `script_revisions` / `asset_versions` / `qc_reports` |
| 项目 | `project_id`（`projects` 用自身 `entity_id` 作项目标识） | 同上 |

五个列的关系是**继承**而不是各自为政：组织由服务端从 Principal 写入；资源的
组织与项目跟随父对象（QC 证据的组织取自它所证明的**资产**，不取调用方身份——
从调用方取会在"跨组织误配"时产生一条归属自相矛盾的证据）。

### 2.2 拒绝（数据库侧）

在既有租户策略之上，为五张表各加一条**限制性（RESTRICTIVE）**策略：

```sql
AS RESTRICTIVE FOR ALL
USING (
    organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
    AND (
        current_setting('app.current_projects', true) = '*'
        OR <project_column> = ANY (
            string_to_array(NULLIF(current_setting('app.current_projects', true), ''), ',')
        )
    )
)
WITH CHECK (同表达式)
```

三个刻意的选择：

1. **限制性而不是再加一条宽松策略**。PostgreSQL 对同一命令的多条 permissive
   策略取「或」——再加一条宽松策略等于把租户隔离**放宽**成
   「租户匹配 或 项目匹配」，这是会把安全网撕开的写法。RESTRICTIVE 与既有
   策略取「与」，语义正好是「租户隔离 且 归属在范围内」，因此本迁移不改既有
   策略，只做加法。
2. **`SET LOCAL` 注入，不设默认值**。GUC 未设置时表达式为 NULL，策略判否：
   **未注入即看不到任何行**。不写"没声明就放行"的默认值——那正是历史上最常见
   的越权来源。空范围（令牌没声明任何项目）同样判否。
3. **`*` 的单位是组织，不是租户**。组织是客户内部的责任边界，跨组织连
   「全部项目」也不放行。把 `*` 定义成租户级会让租户管理员天然越权到别的
   制作主体，而这件事没有任何告警会提示你。

### 2.3 上下文注入（唯一写入点）

`packages/shared/rls.py::set_request_context` 是唯一允许写这三个 GUC 的地方，
取值全部来自 Principal（令牌 claim），并新增架构闸门 R5：

- `set_config(...)` 与 `SET LOCAL ...` 两种写法都算违规；
- 策略定义里 `current_setting(...)` 读 GUC 不算违规（读值 ≠ 写值）；
- 文档字符串不算违规（否则注释会被迫含糊其辞，没人再解释机制）。

理由很直接：**谁能写 `app.current_projects`，谁就能给自己扩权**。若业务代码
可以顺手设置它，那么"越权在数据库层被拒"只在"没人这么写"的前提下成立——
那不是约束，是约定。

### 2.4 写入入口的可解释拒绝

数据库会拦，但把 42501 直接抛成 500 不叫防呆。两处写入入口给出显式判定：

| 入口 | 判定 | 结果 |
|---|---|---|
| `PROJECT.CREATE` | 必须持有本组织全部项目范围 | 403 `AUTH_SCOPE_DENIED` |
| `ASSET.VERSION.CREATE` | 请求体的 `project_id` 必须在范围内 | 403 `AUTH_SCOPE_DENIED` |
| `EPISODE.CREATE` / `SCRIPT.IMPORT` | 归属校验先查项目可见性 | 404 `NOT_FOUND` |

**为什么新建项目要求 `*`**：新项目的 id 不属于任何既有范围。若放行，调用方
写进去之后自己也读不回来（自锁）；若为此放宽 `WITH CHECK`，就等于允许
「往看不见的项目里写数据」。两害相权，落回"新建归属根需要组织级授权"。

**为什么错误码不统一**（403 与 404 并存）：不是疏漏，是可解释性差异。
`project_id` 由调用方提供时不需要掩饰存在性，403 更准确；而按 id 载入资源时
必须不确认存在性，只能与"不存在"同码。资产域还有一层约束：它**没有**项目表的
读权（ADR-R16 按域授权），因此也无从判断项目是否存在——想校验就得先破一次
域边界，那笔账更贵。

## 3. 验证（实测，不是声称）

### 3.1 策略行为（以域角色直连数据库，绕开应用层）

```text
scope=''          → projects 可见 0 行      （空范围 fail-closed）
scope='proj_x'    → projects 可见 1 行
scope='*'         → projects 可见 1291 行
越界 INSERT        → ERROR: new row violates row-level security policy
                     "project_scope_episodes" for table "episodes"
```

### 3.2 反向验证（证明测试不是假绿）

把 `projects` 的 RLS 临时 `DISABLE ROW LEVEL SECURITY` 后，
`tests/unit/test_project_scope.py` 的跨项目/跨组织/空范围三条用例**立即变红**；
重新 `ENABLE` 后全绿。闸门若不能因策略缺失而红，它证明的就不是策略。

### 3.3 覆盖矩阵

| 场景 | 用例 |
|---|---|
| 跨组织读他人项目 | `test_project_of_other_org_is_invisible` |
| 同组织跨项目读 | `test_scoped_token_cannot_read_other_project` |
| 同组织跨项目建集数 | `test_scoped_token_cannot_hide_behind_episode_ownership` |
| 跨项目建资产 | `test_asset_create_in_other_project_is_403` |
| 跨项目读资产 | `test_asset_of_other_project_is_invisible` |
| 跨项目交 QC 证据 | `test_qc_report_of_other_project_is_invisible` |
| 空范围 | `test_empty_scope_sees_nothing` |
| 项目级用户新建项目 | `test_project_creation_requires_org_wide_scope` |
| 业务代码自行设上下文 | `test_service_setting_rls_context_is_reported`（R5） |

## 4. 刻意不做的三件事

1. **不做应用层逐路由校验**。「每个 handler 记得查一次」是最容易漏的形态：
   新增一个路由就多一个漏点，漏了不会有任何症状。这类不变量只有放在数据库层
   才不需要靠记性。
2. **不跨域校验项目是否存在**（资产域不读 `project_planning`）。跨域读权被
   ADR-R16 封死，为了一次存在性校验而重新开一条跨域读通道，等于用边界换提示。
   归属正确性由「写入时 scope 必须命中」保证：能写进某个 `project_id` 的前提
   是调用方对该项目有授权，而授权集合由身份服务按成员关系签发。
3. **不做 FORCE ROW LEVEL SECURITY**。表 owner 是超级用户，FORCE 在此拓扑下
   不产生额外约束（只约束非超级用户 owner）。真正的防呆是「应用绝不以
   owner/superuser 连接」，由 `build_database_url(domain)` 固定域角色保证。

## 5. 存量数据与残留风险

- `asset_versions` / `qc_reports` 的历史行以 `legacy-unassigned` 哨兵值回填
  `project_id`；迁移无法凭空知道 165 条历史资产属于哪个项目。哨兵值**不可用于
  新写入**（`BEFORE INSERT` 触发器拒绝），历史行需要业务侧人工认领后才能进入
  项目级授权范围。
- `organization_id` 在存量行上按 `tenant_id` 回填（单组织租户基线）。多组织
  租户需要一次显式的组织重分配作业，不在本次范围。
- 回填必须临时 `DISABLE TRIGGER USER`：`asset_versions` 的内容列受 ADR-R11
  不可变触发器保护，连"补一列组织归属"都会被拦——首次执行即被拦，正好是
  触发器有效的证据。
- 迁移回退：`alembic downgrade c2d3e4f5a6b7` 可回退这两步（已在本地库实测
  往返）。注意 PostgreSQL 角色是**集群级**对象，若同集群还有别的库持有这些
  角色的授权，`c2d3e4f5a6b7` 的回退会在 `DROP ROLE` 处拒绝——这是集群级约束，
  迁移 docstring 已写明。
