# 双体系深度对账：DB 角色 / RLS / 授权链

> 日期：2026-11-19
> 性质：**纯对账，未改动任何代码**。real merge 演练已 abort，集成分支干净。
> 目的：量化 main(D1~D5) 与 feat/identity-data-ownership(Phase 1) 两套体系的差异与改造成本，供底座选型决策。
> 关联：`2026-11-19-identity-data-ownership-merge-reconciliation.md`（归并总方案）

---

## 0. 执行摘要

原归并方案假设「冲突是机械的，底座以 main 为准 + 提取分支增量」。**深度对账推翻了这个假设**：

- 两套体系不是「新旧」，而是**不同维度的演进**——main 在凭证治理/迁移治理上更完整，分支在授权粒度/域 Identity 划分上更完整。
- 冲突深入**迁移链 revision ID**、DB 角色名、RLS 会话变量、Principal 结构四个层面，无法通过 merge 机械消解。
- 但两套体系**可以共存的方向存在**：以分支的 8 域 + 项目级 scope 为授权底座，保留 main 的凭证/审计/契约治理成果——代价是重写 main 的 D3/D4 迁移与相关代码（约 22 文件），并给 19 条分支迁移重建 revision 链使其挂到 main head。

**推荐：以分支授权模型为底座（选型 B），但要吸收 main 的凭证治理与契约治理，不是原样采用分支。** 理由见 §5。

---

## 1. 维度一：DB 角色体系

| 项 | main（D4 / ADR-R25） | 分支（R16/R27+ / ADR-R16） |
| --- | --- | --- |
| 角色数量 | 2 业务域 + app + relay | **8 业务域**（含 outbox_relay / identity） |
| 角色名来源 | **配置项**（`get_settings()` 动态读，`POSTGRES_*_USER`） | **代码常量**（`svc_<domain>`，枚举绑定） |
| 角色名示例 | `project_planning_role` / `asset_service_role` | `svc_project_planning` / `svc_asset` / `svc_generation_execution` / `svc_qc_governance` / `svc_workflow` / `svc_identity` |
| 类型表达 | `str` 域标识 + `dict[str,str]` 映射 | `StrEnum DatabaseDomain` + `MappingProxyType` 不可变映射（强类型、fail-closed） |
| API | `domain_role(str)` / `domain_roles()` / `domain_role_names()` / `app_role_name()` / `relay_role_name()` | `role_for(DatabaseDomain)` / `all_business_roles()` / `DatabaseDomain.*` |
| 授权策略 | **有** `ALTER DEFAULT PRIVILEGES`（新表自动继承） | **明确不设** DEFAULT PRIVILEGES（新表必须显式 GRANT，忘授权立刻红） |
| app_role 归宿 | 保留为遗留角色，部分 REVOKE | `c2d3e4f5a6b7` 最终 `DROP app_role`（断回退路） |
| 设计哲学 | 角色名可配置（运维友好） | 域身份强类型 + 失败即关闭（安全优先） |

**结论**：两套 API 完全不兼容（一个吃 `str` 一个吃 `StrEnum`，函数名全不同），且授权策略相反。分支的角色体系覆盖更多域——尤其是 `generation_execution` / `qc_governance` / `workflow` / `identity`，这些正是 Phase 1 新增的域，main 的 2 域体系**无法表达**这些域的身份。

被依赖面：
- main 角色 API：22 文件（6 packages/scripts + 4 tests + 12 migrations）
- 分支角色 API：36 文件（含 16 个含 `svc_*` 字面量；覆盖 4 workers + 3 apps + 14 migrations + 多测试）

---

## 2. 维度二：RLS 与身份授权链

| 项 | main | 分支 |
| --- | --- | --- |
| RLS 组织变量 | `app.current_organization_id` | `app.current_org_id` |
| RLS 项目变量 | 无 | `app.current_projects`（`_PROJECT_SCOPE_SEPARATOR = ","`） |
| 作用域粒度 | 租户 + 组织 | **租户 + 组织 + 项目**（项目级 scope） |
| Principal.organization_id | `UUID \| None`（可选） | `UUID`（必填） |
| Principal.project_ids | 无 | `frozenset[str]`，含通配符 `PROJECT_SCOPE_ALL = "*"` |
| 项目访问判定 | 无 | `can_access_project()` / `has_all_projects_scope()` |
| 引用面 | `current_organization_id`：2 文件 | `current_org_id`/`current_projects`：15 文件 |

**结论**：分支的授权模型严格强于 main——它实现了 main 完全没有的**项目级隔离**。main 的 D3 只做组织级 RLS。这不是可以「取 main 舍分支」的地方：若以 main 为底座，分支的 15 个文件里的项目级 scope 逻辑全部要重写或丢弃，等于**倒退授权能力**。

---

## 3. 维度三：迁移链（最硬障碍）

- main：34 个迁移，head 链 `... → e9f0a1b2c3d4 → c1a2b3d4e5f6`（D2 事实表 → D3 RLS → D4 最小权限）。
- 分支：19 个独有迁移，自成一条链：
  `a1b2c3d4e5f6`(拆角色) → `d3e4f5a6b7c8`(组织项目归属) → `e7f8a9b0c1d2`(项目 RLS) → `b1a2c3d4e5f6`/`b8c9d0e1f2a3`/`d5e6f7a8b9c0`(shot_planning) → `a5b6...`/`b8c9...`/`c9d0...`/`d0e1...`(generation) → `d6e7...`/`a2b3...`(delivery) → `e8f9...`(QC governance) → `f0a1...`(workflow) → `f1a2...`(QC review)。

**两个致命问题**：

1. **revision ID 直接冲突**：`b3c4d5e6f7a8`（asset_status_transitions）和 `b7c8d9e0f1a2`（partition_protection_and_grant_baseline）在两边都存在但内容不同。Alembic 不允许两个迁移用同一 revision ID——必须重命名其一并修正所有 down_revision 引用。
2. **链的根基冲突**：分支 Phase 1 所有表迁移都挂在 `c2d3e4f5a6b7`(拆角色) 这条链上，而该迁移与 main 的 `c1a2b3d4e5f6`(最小权限) 解决同一问题、角色名/策略相反。两条链无法首尾相接。

**结论**：要保住分支的 Phase 1 表迁移，必须为这 19 条迁移**重建 revision 链**——把根从 `c2d3e4f5a6b7` 改挂到 main head `c1a2b3d4e5f6`，并处理 2 个 revision ID 冲突，同时决定角色体系归属（决定 `c2d3...` 保留还是替换）。

---

## 4. 维度四：各自独占的治理资产（都不能丢）

**main 独占（须保留）**：
- kid 公钥内容指纹（KEYS-4）、JWKS 轮换、归属链根节点解析（SCOPE-1）
- 基线单一事实源（ADR-R22）+ 游离副本 casefold 判定
- 状态迁移事实表（ADR-R23）、组织 RLS（ADR-R24）、最小权限（ADR-R25）
- ADR-R27~R26 的镜头契约/状态枚举治理（我此前合并的 R20/R21）
- 869 个测试 + 四道闸门全绿的验证基线

**分支独占（须保留）**：
- `services/generation_execution` / `services/shot_planning` / delivery 域（58 文件核心业务代码）
- 8+ Phase 1 表迁移、ADR-R27~R39、7 份取证证据
- 项目级 scope 授权（15 文件）、8 域角色 Identity

---

## 5. 改造成本量化与推荐

### 方案 A：以 main 为底座，把分支适配到 main

- 需改：分支 36 个角色依赖文件 + 15 个 RLS 变量文件 + 19 条迁移重建链 + 2 revision 冲突 + 58 业务文件里的域角色引用 ≈ **90~130 文件**
- 后果：分支的项目级 scope 能力无处安放（main Principal 无 `project_ids`），要么倒退授权模型，要么反过来把分支的 Principal/RLS 移植进 main——那实际上就变成了方案 B。
- 风险：**倒退授权能力**，且工作量最大。

### 方案 B：以分支为底座，吸收 main 治理成果（推荐）

- 需改：
  1. main 的 22 个角色依赖文件 → 适配到 `DatabaseDomain` 8 域体系（角色名改 `svc_*`、API 改 `role_for`）；
  2. main 的 2 个 RLS 变量文件 → 统一到 `current_org_id` + `current_projects`；
  3. main 的 D3/D4 迁移（`e9f0a1b2c3d4`/`c1a2b3d4e5f6`）→ 与分支 `c2d3e4f5a6b7` 合并为一套，保留 main 的审计事实表但用分支角色名；
  4. 分支 19 条迁移重建 revision 挂到合并后的 head；2 个 revision 冲突重命名；
  5. main 的凭证治理（kid/JWKS/审计/基线单一源）→ 平移到分支体系，这部分**代码冲突少**（分支缺的是 main 的后半段治理，非重叠实现）。
- 工作量：约 **22 + 2 + 2 + 19 + 2 ≈ 47 文件**的核心改造，小于方案 A。
- 优势：保住**更完整的授权模型**（8 域 + 项目级 scope），且 main 的凭证/审计治理成果平移即可，不倒退任何能力。

### 方案 C：维持分裂，并行开发

- 成本为零，但 Phase 1 永远进不了 main，42 提交持续贬值，冲突面继续扩大。**不推荐**。

**推荐 B**。核心理由：分支的授权模型严格包含 main 的能力（8 域 ⊃ 2 域，项目级 ⊃ 组织级），而 main 的治理成果是正交的、可平移的。选 B 是「取长」，选 A 是「削足适履」。

---

## 6. 风险与边界

- 本报告只做静态对账，**未验证 B 方案改造后能否全绿**——尤其迁移链重建（§3）是高风险操作，需在实际执行时用干净 PG 逐条验证 `upgrade head → downgrade → upgrade`。
- ADR 编号对撞（R16~R26，11 编号 / 22 文档）在 B 方案下同样存在：分支侧 R16~R26 需重编号到 R40~R50（原方案阶段 1 不变）。
- Phase 1 出口本身不成立（A3/B1/B2/C6/D1 阻塞）——选 B 只解决「资产进 main 且授权模型更强」，不等于 Phase 1 验收通过。
- 两个 revision ID 冲突（b3c4…/b7c8…）必须在迁移链重建时一并解决，否则 Alembic 无法启动。

---

## 7. 复现命令

```bash
B=origin/feat/identity-data-ownership
# 角色体系
cat packages/shared/db_roles.py
git show $B:packages/shared/db_roles.py
grep -rlnE "domain_role|app_role_name" --include=*.py packages services tests data/migrations
git grep -lE "DatabaseDomain|role_for|all_business_roles" $B -- '*.py'

# RLS / Principal
grep -rln "current_organization_id" --include=*.py packages services tests
git grep -lE "current_org_id|current_projects" $B -- '*.py'
git show $B:packages/identity/principal.py | grep -nE "project_ids|can_access_project"

# 迁移链
git diff --name-only $(git merge-base origin/main $B) $B -- data/migrations/versions/
```
