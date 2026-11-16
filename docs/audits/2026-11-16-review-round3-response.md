# 第三轮深度审查应答（基线 7e1c1ad）

- 审查对象：`AI_Creative_Media_Industrial_OS`，Git HEAD `7e1c1ad`（PR #8 合并后）
- 权威基线：`docs/baseline/AI影视工业化生产平台V2.0.5.md`（V205-R14）
- 审查结论：**不通过工程基线验收，Phase 1 不得进入正式验收**
- 本应答结论：**接受该结论**。本轮只闭合 F1 的机制与 F6 的一部分，F2/F3/F4/F5 仍未闭合，
  不得因闸门变绿而改变「未通过验收」的判断。

原始审查报告文本由项目所有者持有（未随仓库提交）。本文按报告章节逐条应答，
每条给出「报告判断 / 独立核实结果 / 处置」，核实以仓库实际代码与契约为准。

---

## 一、核实结果总表

| # | 报告主张 | 独立核实 | 处置 |
|---|---|---|---|
| 1 | README 自造五条铁律，与 §1.3.2 不是一一对应 | **成立**。README 原第 3 条把「高并发/高流量」单列，第 5 条自造「工业级最佳实践」，把「禁止补丁式演进」挤没了 | 已修：README 回归 §1.3.2 原文口径，高并发降为技术质量指标单列 |
| 2 | README 过度宣称「每一条都在 CI 里跑」 | **成立**。跨模块写表、数据所有权、Capability、密钥扫描均无 CI 判定 | 已修：闸门表增「覆盖边界」列，新增「未覆盖范围」诚实登记 |
| 3 | Action 81 vs 基线 75；OpenAPI 81；Schema 79 vs 76 | **成立**，且状态机同样漂移 35→38 | 已修：冻结基线清单 + 修订登记 + `freeze_manifest` 三层判定 |
| 4 | 六个新增 Action 未进权威基线 | **成立**（EPISODE.CREATE、SCRIPT.LOCK.REQUEST、ASSET.VERSION.READ、PROJECT.READ、EPISODE.READ、SCRIPT.READ） | 已修：以修订 `V205-R14-A1` 追认登记，标注 `retroactive: true` |
| 5 | 三个新增 Schema 未逐个定性 | **成立**：`Project`、`Episode`、`ScriptRevision`，均为 Phase 1 主链对象 | 同上，随 A1 登记 |
| 6 | 缺 `phase0_manifest.md` / `.sha256` / 主文档；冻结产物分散；`adr_index.yaml` 位置不符 | **成立**（清单与主文档原在仓库外 `Desktop/AI制作工作台/`，`edit_work/freeze_review/` 派生脚本已不存在） | 已修：主文档进 `docs/baseline/`，清单进 `contracts/phase0/`，`--write/--check` 可复现 |
| 7 | `contract_check` 的 PASS 不能证明契约冻结通过 | **成立**。它只做派生交叉校验（81↔81），无基线概念 | 已修：职责分离，基线一致性归 `freeze_manifest`，`contract_check` 只做派生一致性 |
| 8 | 架构闸门未覆盖服务内分层（domain 不得依赖 FastAPI/SQLAlchemy） | **成立**，现仅覆盖顶层六层 | 未修（列入包 B） |
| 9 | 未防止跨模块直接写表；单一 `app_role` 跨 Schema 授权 | **成立**。`9e3b4c5d6e7f` 对每个 Schema `GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES` | **已修（B1，`9e23d72`）**：按域拆四个角色 + 四池，越权写表在 PostgreSQL 层被拒（实测 `has_table_privilege` 矩阵）；`app_role` 已 `DROP`；R4 闸门禁止业务模块自选连接身份。见 `docs/adr/ADR-R16-database-role-per-domain.md` |
| 10 | 仅基础 RBAC，无 Capability Token 运行时校验 | **成立**。`rbac.py` 判定为 `principal.roles & allowed_roles(action_key)` | 未修（列入包 B） |
| 11 | 同租户不同项目未隔离，`Project` 无 `organization_id` | **成立**。`Project` 领域模型无组织归属字段，租户 RLS ≠ 项目级授权 | **已修（B3）**：归属链进契约（修订 `V205-R14-A2`）与数据模型；五张项目级表上加 RESTRICTIVE 策略，越权读写由 PostgreSQL 拒绝；Principal 携带组织与项目范围，注入点唯一（R5 闸门）。见 `docs/adr/ADR-R17-project-scope-isolation.md`、`tests/unit/test_project_scope.py` |
| 12 | API 仍须持有身份签发私钥 | **成立**。`identity_private_key_pem` 在 `_PRODUCTION_REQUIRED_SECRETS` 中，api 与 relay 同镜像同配置 | 未修（列入包 B） |
| 13 | 资产不可覆盖只保护内容，`status` 可被应用角色直接改 | **成立**。触发器白名单为 `status / entity_version / updated_at / audit_ref` | 未修（列入包 C） |
| 14 | Outbox 仅收回 `DELETE`，原始事件内容仍可 `UPDATE` | **成立**。`d4e5f6a7b8c9` 只 `REVOKE DELETE` | 未修（列入包 C） |
| 15 | API 与 Outbox Relay 共用 `app_role` | **成立**。compose 中 api / relay / outbox-maintenance 共用 `POSTGRES_APP_USER=app_role` | **已修（B1）**：relay 用 `svc_outbox_relay`（仅事件 SELECT + 投递状态列级 UPDATE）；`POSTGRES_APP_USER` 配置项已删除——用户名可配等于权限边界可配 |
| 16 | Service 层直接 import `IntegrityError` | **成立**。`asset_service.py:26` 起 4 个 service 依赖 SQLAlchemy 异常 | 未修（列入包 B/C 交界） |
| 17 | `tests/contract`、`tests/e2e`、`tests/resilience` 只有 `.gitkeep` | **成立** | 已修：三个空壳目录删除，README/结构文档改为「不存在」 |
| 18 | README 称 `pytest tests`，CI 只跑子集 | **成立** | 已修：CI 改为 `pytest tests`，口径统一 |
| 19 | Phase 1 主链未完成；ACTIVE 36 / 已实现 12 | **成立**。台账 `implemented: 12` / `not_implemented: 25` | 未修（包 D），但 README 已把四个状态的区别写清 |
| 20 | Creator Mode 未提前启用 | **成立**，符合基线 | 保持 |
| 21 | 交付包含 `.env` / `.venv` / `.git` / 缓存；`.env` 约 1253 字节 | **成立**（仓库内 `.env` 未被 git 跟踪，但打包脚本无排除规则） | 未修（列入包 D 的发布闸门）；**建议按已暴露处理并轮换凭证** |

---

## 二、本轮已闭合：F1（契约冻结生命周期）

### 2.1 修的不是数字，是断掉的权威链

原状态：

```
V2.0.5 权威（75/75/76/35）  ──X（无记录）──▶  派生文件（81/81/79/38）
                                              │
                                       contract_check 81↔81 → PASS
```

`contract_check` 的 PASS 是真的，只是它回答的问题不是「是否符合 V2.0.5」。
这属于 §1.3.2 第 3 条「禁止补丁式演进」的典型后果：每次为上线改一点派生文件，
单次都像无害小改，累积后与权威源失去可追溯关系。

现在的链路（ADR-R15）：

```
docs/baseline/…V2.0.5.md            ← 权威源（随仓库交付，字节数+SHA-256 受校验）
        │
contracts/phase0/frozen_inventory.yaml    ← 冻结时刻名称全集（75/76/35）
        │
contracts/phase0/contract_amendments.yaml ← 已批准增删及理由（A1：+6/+3/+3）
        │
派生文件 ─────────────────────────────┘
        ↓
contracts/phase0/phase0_manifest.md (+ .sha256)   ← 十一类产物字节数+哈希
        ↓
scripts/freeze_manifest.py --check                ← 三层判定
```

三层判定，各自堵一种绕过手法：

| 层 | 判定 | 堵住的绕过 |
|---|---|---|
| 权威源 | 基线正文字节数 + SHA-256 | 偷改正文却沿用旧清单 |
| 产物 | 十一类文件字节数 + 哈希 + 清单自指哈希 | 改内容不重签；重签后再手改清单 |
| 演进 | 实际名称全集 == 基线 ∪ 已批准修订（双向精确） | 「删 6 加 6」凑数量；`--write` 洗白 |

### 2.2 变异验证（不是「写完就绿」）

在真实仓库上制造漂移，确认闸门真的报红（均已还原）：

```
# 变异 1：注入未登记 Action SHOT.MAGIC
[FAIL] ④ Action Registry 内容已变更但清单未重签：…action_registry.yaml（实际 a22d170bd79b3ee4…）
[FAIL] Action 存在未登记新增 ['SHOT.MAGIC']：必须先登记契约修订再改派生文件
[FAIL] OpenAPI 操作数 81 ≠ Action 数 82（派生关系断裂）

# 变异 2：删掉已登记的 EPISODE.READ，再跑 --write 重签清单试图掩盖
[FAIL] Action 缺少基线/登记项 ['EPISODE.READ']：冻结内容不得静默删除
[FAIL] Action 修订登记陈旧 ['EPISODE.READ']：实际已不存在，必须删条目或补实现
[FAIL] OpenAPI 操作数 81 ≠ Action 数 80（派生关系断裂）
```

变异 2 是重点：**`--write` 不能把漂移洗成合规**。它只按实际状态重签清单，
不判断该不该签；判断权在修订登记与人工审批。

新增 21 条测试（`tests/unit/test_freeze_manifest.py`）覆盖：未登记新增（Action/Schema）、
基线项缺失、陈旧登记、重复登记、清单篡改、正文漂移、清单缺失、`total_actions` 与条目数不符、
OpenAPI 脱钩、修订缺字段、修订坏形状、id 重复、CLI 不发 traceback。

### 2.3 为什么不是「再加一个校验函数」

报告的核心要求是「让系统本身不存在绕过路径」，而不是「让脚本显示更多 PASS」。
本轮的三处设计选择都按这个标准做：

1. **不硬编码 75**：基线数字进 `frozen_inventory.yaml` 作为快照，判定用集合运算。
   下次修订只改数据，不改代码——否则就是补丁。
2. **不做自动修复**：`--write` 只重签，不写修订记录。让机器自动登记契约修订，
   等于让 AI 自批契约，直接违反「AI 不绕过审批」。
3. **不与 `contract_check` 合并**：两者回答不同问题（是否符合基线 / 派生之间是否自洽），
   合并后任一侧放宽都会掩盖另一侧。职责分开、各自单一实现，符合「一条规则一个权威实现」。

---

## 三、本轮已闭合：F6 的一部分

| 项 | 处理 |
|---|---|
| README 自造铁律 | 回归 §1.3.2 五条原文，技术质量指标单列 |
| README 过度宣称 | 闸门表增「覆盖边界」列 + 新增「未覆盖范围」表 |
| 空壳测试目录 | 删除 `tests/contract`、`tests/e2e`、`tests/resilience`；结构文档标注「不存在」 |
| CI 与 README 口径不一致 | CI 改为 `pytest tests` |
| 冻结产物不可复现 | 主文档、清单、修订登记入仓，`--check` 可复现 |
| 阶段状态表述 | 明确「Phase 1 未通过验收」，写清「契约 ACTIVE ≠ 已实现 ≠ 集成通过 ≠ 生产验收」 |
| `.env` / `.venv` / `.git` 进包 | **未修**：打包脚本仍在仓库外，无排除规则。**建议先按已暴露轮换凭证** |
| 169 个文件显示修改实为 CRLF/LF 差异 | **部分修**：`contracts/**` 与 `docs/baseline/*.md` 已随本轮完成行尾归一化（哈希判定要求跨平台同字节，否则闸门随平台偶发失败）。代价是这几个文件的 diff 变成整文件级——但这笔账必须现在还：先把被哈希的文件稳定住，再做其余目录的一次性 `--renormalize`，否则归一化本身会把真实改动淹没。**代码目录的 CRLF 归一化仍未做**（包 D） |

---

## 四、报告中有两处需要修正的事实

应答不等于全盘接受，以下两条与仓库实际不符，按核实结果修正：

**（1）`permission_matrix.yaml` 从 35786 字节缩到 4212 字节，不是内容丢失。**
这是一次**有意的单一事实源重构**：删掉了 75 条 `action_key` 级副本，改为
`action_matrix: {derived_from: action_registry.yaml, total: 81}`，避免同一授权事实两处维护。
报告把它当作「契约内容缺失」的迹象，实际应归入 F1 的**另一半**——
重构本身合理，但没有更新冻结清单、没有留修订记录，因此同样不可追溯。

**（2）`adr_index.yaml` 的「位置不符」需要基线出处。**
冻结清单（`phase0_manifest.md`）对十一类文件只登记文件名与章节，未规定目录；
`adr_index.yaml` 位于 `docs/adr/` 是仓库既有约定。若基线另有明确路径要求，
请给出章节号，我按基线搬迁；在此之前按「口径待澄清」处理，不计入缺陷。

（另：报告提到「环境缺 structlog 导致测试阻塞」——这是 ZIP 内 `.venv` 为 Windows 形态
所导致，属交付包形态问题（第 21 条），不是源码问题。本机用仓库自身 `.venv` 可跑全量 330 用例。）

---

## 五、本轮自查新发现（报告未提及）

**`uv.lock` 把每个依赖的下载 URL 硬编码为清华镜像，CI 镜像构建因此随网络抖动偶发失败。**

`uv.lock` 中每条记录形如
`source = { registry = "https://pypi.tuna.tsinghua.edu.cn/simple" }`，
`--frozen` 会按锁文件里的 URL 下载。GitHub Actions 的 runner 在美国机房，
访问国内镜像不稳定：本 PR 的 `image-build` 首次运行即在
`redis-8.1.0` 下载处超时失败（重试 3 次后 exit 1），重跑同一提交则 44 秒通过。

这不是代码缺陷，但属于 F6 同类问题——**构建可复现性依赖了地域性网络条件**。
更麻烦的是它的表现形态：偶发红 → 被当成"网络问题"重跑 → 真的回归也会被同样忽略。

处置建议（不与本 PR 混做，避免锁文件巨大 diff 淹没契约改动）：

1. 用默认 PyPI 源重新生成 `uv.lock`（需要联网 `uv lock`）；
2. 或在 Dockerfile 构建期显式覆盖索引并验证 `--frozen` 仍校验哈希；
3. 无论哪种，都应让"镜像构建失败"不再有"重跑一下就好"的处置习惯。

---

## 六、未闭合的四项：为什么不在这轮修

F2（数据所有权）/ F3（身份与授权）/ F4（数据不变量）/ F5（Phase 1 主链）的共同点是：
**必须改数据库授权、领域模型与运行时授权入口**，不是契约层能解决的。
按报告要求「一次整体重构，而不是继续修补」，这四项按下表分四个包推进，
每个包都以「绕过路径被封闭 + 负向测试能证明」为验收，而不是「文件写完」。

| 包 | 对应根因 | 核心动作 | 验收标准（缺一不通过） |
|---|---|---|---|
| **A 契约生命周期** | F1、F5（部分） | 本轮已完成机制 | 已达成：基线/产物/演进三层判定 + 变异验证 + 21 条测试 |
| **B 身份、安全与数据所有权** | F2、F3 | ① 拆数据库运行角色（api / relay / worker / migration），按 Schema 授权；② 统一授权决策入口（Principal + Action + Resource + Context → ALLOW/DENY），Capability 真正执行；③ 建立组织→项目→资源归属；④ 私钥出 API 进程，公钥走 JWKS | 跨模块写表在**数据库层**被拒（不是 Python 层约定）；跨项目读取有拒绝测试；API 进程配置中不存在私钥；Agent 无法通过 CRITICAL Gate 有测试 |
| **C 数据不变量** | F4 | ① 资产状态迁移改由受控函数/触发器承担，应用角色不得直接 `UPDATE status`；② Outbox 拆「事件事实（只追加）」与「投递状态（relay 可更新）」；③ 领域异常与 ORM 异常在 Repository 适配器收口 | 绕过业务服务的直接写被数据库拒绝；事件正文不可改有测试；Service 层不再 import `IntegrityError` |
| **D Phase 1 主链 + 交付闸门** | F5、F6 | ① 按 31.3 主链补齐 AI 拆镜→人工确认→ShotSpec→QC→Gate→DeliveryManifest；② 发布包排除 `.env`/`.venv`/缓存并做密钥扫描；③ 压测与故障演练证据 | 主链有端到端证据；Action `ACTIVE` 与 `implemented` 台账一致；发布包含密钥扫描结果 |

**顺序建议**：B → C → D。理由：B 决定「谁能写哪个 Schema」，C 的状态保护要建在
B 的角色边界之上；先做 C 会在旧角色模型上再叠一层，属补丁式演进。
A 已完成，作为后续三个包的判定基础（每次契约变动都要走 A 的流程）。

**不做的事**（与报告一致）：不再新增 Manager / Policy Engine / Guard / Registry；
不新增第二套架构扫描器；不为凑数量删除已上线的读写接口。

---

## 七、本轮交付清单

| 类型 | 文件 |
|---|---|
| 新增（权威源） | `docs/baseline/AI影视工业化生产平台V2.0.5.md`（489607 字节，SHA-256 `9a9940c5…`） |
| 新增（冻结生命周期） | `contracts/phase0/frozen_inventory.yaml`、`contract_amendments.yaml`、`phase0_manifest.md`、`phase0_manifest.md.sha256` |
| 新增（闸门） | `scripts/freeze_manifest.py` |
| 新增（测试） | `tests/unit/test_freeze_manifest.py`（21 条） |
| 新增（文档） | `docs/adr/ADR-R15-contract-freeze-lifecycle.md`、本文 |
| 修改 | `README.md`（铁律回归 + 闸门边界 + 未覆盖范围 + 阶段状态） |
| 修改 | `.github/workflows/ci.yml`（新增冻结步骤；pytest 口径统一为 `tests`） |
| 修改 | `Makefile`（`freeze-check`） |
| 修改 | `docs/architecture/repository-structure.md`（空壳测试目录说明） |
| 删除 | `tests/contract/.gitkeep`、`tests/e2e/.gitkeep`、`tests/resilience/.gitkeep` |

---

## 八、结论

- 审查结论「不通过工程基线验收」**接受且不申辩**；
- F1 的**机制**已闭合（可复现、可验证、能区分漂移类型），F6 闭合一部分；
- F2 / F3 / F4 / F5 **未闭合**，已在 README「未覆盖范围」与本文第六节显式登记，
  不以闸门全绿冒充达标；
- 下一轮从包 B 开始。在包 B/C 完成前，本项目仍不具备 Phase 1 正式验收条件。
