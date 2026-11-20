# feat/identity-data-ownership 归并对账报告

> 日期：2026-11-19
> 性质：**对账与方案，未改动任何代码/契约**。本报告供所有者决策，不自行执行归并。
> 对象分支：`origin/feat/identity-data-ownership` @ `59b13af`（42 提交 / 357 文件 / +49160 -1394）
> 基线：`origin/main` @ `5352d19`（PR #14~#20 已合并）
> 共同祖先：`cac04e3`（2026-11-16，PR #9）

---

## 1. 一句话结论

**这条分支不能按常规 merge/rebase 归并。** 它与 main 之间存在 git 层 74 处硬冲突 +
git 检测不到的 ADR 编号语义对撞（R16~R26 共 11 个编号、22 份文档语义完全不同）。
但它同时携带 Phase 1 主体实现（generation_execution / shot_planning / 交付链 / 7 份取证证据）——
这是不可丢弃的真实资产。**根因不是「冲突多」，而是两条线在分叉后各自独立推进了同一套编号空间与同一批底座文件。**

---

## 2. 对账事实（均可复现）

### 2.1 时间线：不是并行，是分叉后单向停滞

| 项 | 值 |
| --- | --- |
| 共同祖先 | `cac04e3` 2026-11-16 PR #9 |
| 分支提交区间 | 2026-11-16 ~ 2026-11-17（停在 11-17） |
| main 提交区间 | 2026-11-17 ~ 2026-11-19（PR #14~#20 继续推进） |

分支并非与 main 并行演进，而是停在 11-17；main 在其后完成了 R20/R21 状态枚举治理、
D1~D5 四大根基重构。因此**冲突的方向是单向的**：main 是「更新的同一批文件」，分支是「较早的另一套语义」。

### 2.2 三方改动集

| 集合 | 文件数 |
| --- | --- |
| main 相对祖先改动 | 208 |
| 分支相对祖先改动 | 357 |
| **双方都改（重叠）** | **91** |
| main 独占 | 117 |
| 分支独占 | 266 |

`git merge-tree` 精确模拟结果：**238 个冲突条目**，其中硬冲突 74 处
（content 71 / modify-delete 2 / add-add 1），其余为可自动合并的重叠改动。
冲突集中在底座文件：`object_schemas.schema.json`、`state_machines.yaml`、`enum_registry.yaml`、
`action_registry.yaml`、`permission_matrix.yaml`、`contract_amendments.yaml`、`phase0_manifest.md`、
`packages/shared/{database,rls,db_roles,config}.py`、三个 service 的 repository/service/domain 等。

### 2.3 最危险的信号：ADR 编号空间对撞（git 检测不到）

R06~R15 两边同名同内容（SAME），但从 R16 起，两边**占用了同一批编号，语义完全不同**：

| 编号 | main 侧 | 分支侧 |
| --- | --- | --- |
| R16 | ShotBreakdownDraft 契约 | 数据库角色按域拆分 |
| R17 | ShotRevision 契约 | 项目范围隔离 |
| R18 | Shot 实体契约 | 统一授权决策 |
| R19 | Scene 实体契约 | 签名密钥边界 |
| R20 | 状态枚举单一事实源 | 数据不变量下沉数据库 |
| R21 | 双 status 字段权威 | 仓储错误语义 |
| R22 | 基线单一事实源 | 交付清单即证据 |
| R23 | 状态迁移事实表 | Gate 决策即事实 |
| R24 | 组织范围 RLS | 交付审批消费 |
| R25 | 最小权限 DB 授权 | 发布包治理 |
| R26 | Agent Harness 架构 | Capability Token 运行时强制 |

因文件名后缀不同，git 只报 1 处 add/add；但语义上是 **11 个编号 / 22 份决策记录身份对撞**。
直接合并会产生「同一个 R20 有两个权威定义」，等价于契约身份体系的分叉——这正是 R4「一个事实一个权威源」要杜绝的。

### 2.4 分支独占的真实资产（不可丢弃）

- 完整域：`services/generation_execution/*`、`services/shot_planning/*`、`delivery_*`（service / domain / api）
- 迁移：generation task / lease / route、shot planning spec pack、delivery manifest 等 8+ 个
- ADR：R27~R37 + R39（镜头域拆分、生成执行、单一路线、租约、QC 复查、阶段门禁等）
- 证据：`docs/evidence/phase1-*.md` / `.json` 共 7 份（环境、迁移回滚、备份恢复、韧性、B2 负向、C4、C5、覆盖审计）
- 集成/负向测试：G3 审计抽样、主链 e2e、生成路由等

### 2.5 分支自己的出口判定：Phase 1 出口不成立

`docs/evidence/phase1-coverage-audit.md` §10 机械推导结论：

| 组 | 实测 | 结论 |
| --- | --- | --- |
| A | A1/A2/A4 PASS，**A3 MISSING**（业务链零 span） | ✗ |
| B | B1 MISSING（15 动作无端点）、B2 PARTIAL（AGENT-NEG-004 无载体）、B3/B4 PASS | ✗ |
| C | C1~C5 PASS、C6 PARTIAL（用例就位取证待跑） | ✗ |
| D | D1 PARTIAL（10/42 状态机）、D2 PASS | ✗ |
| F | F1 PASS（ADR-R37）；F2/F3 MISSING 已登记 | ✓ 条件成立 |

**Phase 1 出口不成立**，五组有阻塞项，B/E 是零到一缺口。所以这条分支即便原样合并，也不是「完成态」。

---

## 3. 根因判定

不是「分支太久没合并」这种表层原因，而是三条结构性根因：

1. **共享底座文件缺少所有权边界**：`object_schemas` / `state_machines` / `action_registry` /
   `permission_matrix` 等十一类冻结产物，两条线都在直接改，没有「谁负责合入」的单一入口。
2. **ADR 编号是无主的全局序列**：没有分配机构，两条线独立取号必然对撞。
3. **Phase 1 主体实现与 D1~D5 根基重构并行**：同一批 service/domain/repository 文件，
   一边在补业务实现，一边在重构底座，语义天然冲突。

---

## 4. 归并方案（推荐路径：分层重放，不整体 merge）

**原则：先保住 split-brain 两边的权威，再按「底座 → 契约 → 域实现 → 证据」顺序分层重放；
全程不覆盖、不静默消解任何一边的事实。**

### 阶段 0：冻结现状（决策前置，约 10 分钟）

1. 给分支打保护标签 `backup/identity-data-ownership-59b13af`，防止后续操作丢历史。
2. 本地建只用于分析的 worktree，**不动 main**。
3. 确认无开放 PR 依赖该分支（已核实：无）。

### 阶段 1：解决 ADR 编号对撞（必须先做，否则后面全是错的）

- 为分支侧 R16~R26 重新编号到空闲区间。main 已占到 R26，分支侧 ADR-R27~R39 又已占用，
  故分支侧原 R16~R26 建议整体迁移到 **R40~R50**（连续、无歧义），并按映射表同步：
  - 每份 ADR 文件名、文内编号、`docs/adr/adr_index.yaml`、`contract_amendments.yaml` 引用、
    代码注释中的编号引用全部更新；
  - 保留一份 `R16~R26 编号对撞映射表`随方案归档，使历史提交里的编号引用可追溯（不篡改历史提交，
    只在当前树里重映射，重映射本身作为一次显式修订登记）。
- **这一阶段只改编号，不改任何语义内容**，可由脚本机械完成 + 人工复核映射表。

### 阶段 2：底座层冲突的定向消解（71 content 冲突的主战场）

对每个冲突文件逐一判定「保留 main / 保留分支 / 需要人工融合」三类，判据：

- **契约十一类产物 + `packages/shared` 底座**：以 main 为准（D1~D5 是更新、更完整的重构），
  分支侧对这些文件的改动若含 Phase 1 必需能力，则**提取该能力为增量补丁**重放到 main 之上，
  而不是回退 main。
- **service/domain/repository**：以「哪边的改动承载了对方没有的业务事实」为准；
  main 的乐观锁/RLS/最小权限语义必须保留，分支的状态机/归属语义按需融合。
- 每处判定写进本报告附录的决策表（文件 / 冲突类型 / 采用 / 理由 / 是否需补测试），
  做到可审计、可回滚。

### 阶段 3：Phase 1 独占资产平移（低冲突，266 分支独占文件大部分可直接带入）

- 整域复制 `generation_execution` / `shot_planning` / `delivery_*` 与 8+ 迁移到 main；
- 重放 ADR-R27~R39（分支侧原本就比 main 高，无需重编号）与 7 份证据文档；
- 每个迁移在干净的 PG 上跑 `upgrade head → downgrade → upgrade` 验证（本地已具备 compose PG+Redis 环境）。

### 阶段 4：统一验证（全绿才允许合入 main）

- 四道闸门：`freeze_manifest --check` / `contract_check` / `architecture_check` / `gen_status_contracts --check`；
- `ruff` / `mypy` / `alembic check`；
- 全量 `pytest`（当前 main 869 passed，归并后须 ≥ 此数且无失败）；
- Phase 1 负向：`pytest -m agent_negative`；
- 冻结清单：因改了契约产物须走修订流程重签（新增一条修订，登记编号重映射与归并来源）。

### 阶段 5：合入

- 归并结果作为一个新 PR 提交（**不在旧分支上原地 merge**，避免污染备份标签）；
- CI 四项全绿 + 逐条处理评审意见后再合并；合并后清理旧分支。

---

## 5. 不推荐的路径及原因

| 路径 | 为什么不推荐 |
| --- | --- |
| 直接 `git merge` | 74 硬冲突 + 11 个 ADR 编号语义对撞，产出不可信的契约身份体系 |
| `rebase` 42 提交到 main | 逐提交重放同一批底座文件，冲突次数≈提交数×重叠文件，比 merge 更痛苦且同样不解决 ADR 对撞 |
| cherry-pick 部分提交 | 会拆散提交间的语义依赖（如 ADR 与实现、迁移与 service），产出半截功能 |
| 丢弃分支只保留 main | 丢失 Phase 1 主体实现与全部证据，等于否定已做的工作 |
| 原样合并后补 ADR | 编号对撞已写入冻结产物与索引，后补成本更高且难以追溯 |

---

## 6. 风险与边界

- **本报告未执行任何归并动作**，main 与分支均未改动；临时分析文件已清理，工作区干净。
- 阶段 1 的编号重映射是全局标识符变更，必须同步所有引用；遗漏一处即产生悬空引用，
  由 `contract_check` 的引用目标闸门兜底。
- 阶段 2 的 71 处 content 冲突需要逐处业务判断，**不能机械「选 main」**——
  分支侧可能含有 main 没有的 Phase 1 必需语义（如交付清单审批消费）。
- Phase 1 出口本身不成立（§2.5），归并只解决「资产进入 main」，**不等于 Phase 1 验收通过**；
  A3 / B1 / B2 / C6 / D1 等阻塞项需独立推进。

---

## 7. 复现命令

```bash
# 三方改动集
base=$(git merge-base origin/main origin/feat/identity-data-ownership)
git diff --name-only $base origin/main | sort -u
git diff --name-only $base origin/feat/identity-data-ownership | sort -u

# 冲突精确模拟
git merge-tree --write-tree origin/main origin/feat/identity-data-ownership

# ADR 编号对撞
git ls-tree -r --name-only origin/main docs/adr/ | grep -oE 'ADR-R[0-9]+' | sort -u
git ls-tree -r --name-only origin/feat/identity-data-ownership docs/adr/ | grep -oE 'ADR-R[0-9]+' | sort -u

# 出口判定
git show origin/feat/identity-data-ownership:docs/evidence/phase1-coverage-audit.md | sed -n '131,150p'
```
