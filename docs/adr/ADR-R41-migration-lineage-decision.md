# ADR-R41：数据库迁移谱系边界裁定

- 状态：DECIDED（事实边界裁定；不激活任何迁移、不改任何库）
- 日期：2026-11-21
- 触发：第三轮工程审查 P1-1「正式数据库谱系还需要统一裁定」
- 关联：ADR-R28（Scene/Shot 运行时）、ADR-R40（统一授权模型，PROPOSED）、
  A18 契约修订（V205-R14-A18，登记两套 Schema 并存）
- 不影响：本裁定**不**合并迁移链、**不**改 `down_revision`、**不**触碰任何数据库；
  它只确定「哪条是部署事实、另一条是什么、证据如何引用」。

---

## 1. 裁定（项目所有者 2026-11-21 拍板）

| 编号 | 议题 | 裁定 |
|---|---|---|
| D1 | 最终正式数据库模型 | **main 正式链**（`data/migrations/versions/`，单 schema `project_planning`）是**唯一部署事实源**。部署数据库只采用这条链的 Schema 与迁移历史。 |
| D2 | pending 归属治理链的定位 | `data/migrations/pending/feat-ownership/`（分 schema `shot_planning` / `workflow` / `generation_execution`，13 个迁移 `a9b8`→`m3b4`）定位为**已完成并激活的归属治理事实源与验证载体**，在 `integration` 分支与 `ai_film_industrial` 库上运行；不是待合并进 main 的代码，也不是被废弃的实验。 |
| D3 | 两条链是否合并 | **禁止合并**。两链 schema 结构不同、`down_revision` 互不隶属；把 pending 文件复制进 main 的 `versions/` 会立刻造出第二个 alembic head，使 `alembic upgrade head` 二义化，并可能在 main 链上误插入针对分 schema 表的 DDL。隔离是刻意设计，不是遗漏。 |
| D4 | 两套结构是否同时部署 | **禁止**。部署库只能是 D1 的单 schema 结构。`ai_film_industrial`（分 schema）是验证库，不是生产/准生产实例；`15432 / ai_film_verify` 生产库从未被本链触碰。 |
| D5 | 验收证据如何引用 | 分叉库的 35/35 归属闭环证据**只证明 D2 链上的事实**，不得直接当作 D1 正式链的上线证明。凡要在正式链主张同一不变量，必须在 D1 链的库（`ai_film_api_tests`）上用同一判据重跑，并把结果作为独立证据登记。 |

---

## 2. 已核实事实（裁定依据，非推测）

| 事实 | 证据 |
|---|---|
| main 正式链 head = `b1c2d3e4f5a6`（A13 Scene/Shot 五表 + 复合外键升级），跑在 `ai_film_api_tests` | `data/migrations/versions/`；无迁移以 `b1c2` 为 `down_revision`；`.env:17` |
| 正式链 schema 为单 `project_planning`，运行时归属解析指向 `project_planning.shots` / `project_planning.rework_tasks` | `packages/identity/ownership.py` 的 `_RESOLVE_SQL` |
| pending 链已在 `integration` 分支激活，目标库 `ai_film_industrial` 推进到 `m3b4c5d6e7f8`，归属闭环 35/35 PASS | `data/migrations/pending/feat-ownership/README.md`；`docs/audits/mig-recon/acceptance-ledger.md` |
| 归属验收台账引用的是分 schema：`shot_planning.shots` / `workflow.rework_tasks` / `generation_execution.*` | `docs/audits/mig-recon/acceptance-ledger.md` 归属表清单 |
| 两链迁移文件的 `down_revision` 指向各自链的 head，互不隶属 | 两链迁移文件头 |
| A18 修订已登记「两套 schema 并存」为残余风险 | `contracts/phase0/contract_amendments.yaml` V205-R14-A18 |

**关键区分**：报告指出的「35/35 不能直接等同于 main 正式链已验收」成立，但成因不是"pending 链未激活的实验"——恰恰相反，它**已在另一分支激活并验收**；问题是两套事实源的**适用范围不同**，引用时必须带限定词，不能跨链挪用。

---

## 3. 边界后果（必须遵守）

1. **代码侧**：运行时仓储、身份归属解析、API 路由一律以 D1 单 schema 为准；不得因分叉库有 35/35 就在 main 代码里引用 `shot_planning.*` / `workflow.*`。
2. **闸门侧**：两链的验证脚本/台账必须在标题与产物里标注所属链名（D1: `ai_film_api_tests` / D2: `ai_film_industrial`），禁止出现无主语的「PASS 35/35」。
3. **迁移侧**：任何新增归属约束进入正式链，必须走 D1 链的新迁移（如已落地的 `b1c2d3e4f5a6`），不得从 pending 复制。
4. **证据侧**：把分叉链验收结果写进正式链的生产准入材料时，必须同时说明 D5 的限定；否则视为把一条链的证据伪装成另一条的。

---

## 4. 何时重新开裁

出现下列任一情形，本裁定需重新评审（不自动失效，但必须重开）：
- 决定将分 schema 治理能力正式并入部署模型（需设计完整的合并迁移与存量数据迁移方案，而非复制文件）；
- `ai_film_industrial` 的验收被证明可无歧义映射到 D1 结构并获正式链复验通过；
- 目标环境部署方案变更，要求两套结构同时存在。

---

## 5. 本裁定的局限

- 它是**事实边界裁定**，不是技术统一方案。两链最终是否会物理统一仍未决，本裁定只保证"在统一之前不误用证据、不制造双 head"。
- 它不改变 Phase 1 生产准入状态（仍不通过，见生产准入清单）；也不代表 ADR-R40 阶段 3 放行。
