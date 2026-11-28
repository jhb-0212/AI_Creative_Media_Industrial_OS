# ADR-R46 修订建议稿（D4 整改后）

> 状态：**草案，待 Owner 签署**。本文件只给出逐条替换文本与依据，不构成签署事实。
> 依据：本轮本机 Docker 实测（`NEW-MACHINE / 2026-11-28`）+ 代码读证。
> 生效方式：Owner 逐条勾选后由维护者并入 `docs/adr/ADR-R46-data-constrained-downgrade.md`。

## 0. 本轮实测基线（替换文中一切旧数字）

| 项 | 旧文档口径 | 本轮实测 |
|---|---|---|
| 身份 DDL 迁移 | 41 | 41（不变） |
| 矩阵不一致 revision | 28 条快照 / 25 asymmetric | 25 asymmetric |
| 快照条数与 `diff` | 28 条，**全部 `{}`**（生成器丢弃明细 + 硬覆盖，见 §3.2） | **25 条，24 条实测空差异、1 条实测非空** |
| `restore_required` | 0 | **1**（`e6a7b8c9d0e1`） |
| `unproven_blocking` | 0 | **1**（`f2b3c4d5e6a7`） |
| Registry | exit 2 BLOCKED-ENV | **clean=true（exit 0）** |
| 身份 DDL 基线 | `role_create 9 / grant 54 / role_alter 9`（含未提交 strip 补丁口径） | **`role_create 5 / role_alter 5 / grant 49 / revoke 76 / default_privs 31 / role_drop 1`（311530c + 本轮修复）** |

---

## 1. 正文 §6「后果」追加一段

**替换**（原文保留，末尾追加）：

> 6.1 本 ADR 的验收数字必须以**当轮实测**为准，不得沿用上一轮文档中的计数。凡文档出现
> 「N 条快照、全部 `{}`」这类表述，须同时给出生成器与输入文件路径，否则视为未经测量的声明。
> 6.2 身份 DDL 的度量口径必须覆盖 `role_drop`。缺该类别时，迁移销毁角色不会被计数，
> 「只许减不许增」的基线对最危险的破坏性动作失明（D4-10 实测：`classify_sql('DROP ROLE x')` 返回 `{}`）。

## 2. A.1 身份事实源

**A.1.1 追加**：

> 1.1 `role_specs.py` 是唯一事实源，但「事实源」不等于「执行者」。截至本轮，迁移链中仍有
> 5 个 revision 在执行 `CREATE ROLE`（`a0b1c2d3e4f5`、`a5b6c7d8e9f0`、`e0f1a2b3c4d5`、
> `e6a7b8c9d0e1`、`8f3b2c1d4e5a`），即事实源与执行者尚未收敛。该状态与 A.2.2 冲突，
> 必须作为待整改项登记，不得按「已收敛」叙述。

## 3. A.2 Migration 职责边界

**A.2.2 替换为**：

> 2. Migration 不得创建、删除、修改或重建基础角色；角色的创建、属性、口令与成员关系
> 一律由 bootstrap 依 `role_specs.py` 执行。迁移对角色只允许两种动作：**断言存在**
> （缺失即带修复指令失败）或**断言退役**（不存在即通过）。

**A.2.3 替换为**：

> 3. 退役（`DROP ROLE`）**不属于**迁移的常规职责。历史上 `f2b3c4d5e6a7` 曾在 `upgrade()`
> 中执行 `DROP ROLE`，配置依赖守卫与显式授权恢复；本轮将其登记为 `unproven_blocking`，
> 待 Owner 在「移出迁移（推荐）」与「签署单向安全契约」之间裁决
> （`docs/audits/2026-11-28-wp04-f2b3-owner-packet.md`）。裁决前该 exception 不得被引用为
> 其他迁移的先例。

**A.2.9 追加**：

> 9. 「迁移自建角色、降级不删角色」在实测中表现为 `restore_required`（D 态多出角色），
> 不是 `no_restore_proven`。本轮 `e6a7b8c9d0e1` 即为此类：P 无 `qc_worker`、D 有。
> 该类差异只能靠删除迁移中的建角色动作消除，不能靠改分类消除。

## 4. A.3 退役身份恢复

**A.3.5 替换为**：

> 5. 恢复行为的审计痕迹当前仅为同事务内 `RAISE WARNING`（服务器日志级），**不是**持久化
> 审计记录。另需注意：`CREATE ROLE` 是**集群级**副作用，隔离库降级后角色仍留在集群。
> 本轮实测即因此触发 Registry 漂移（`qc_worker` 存在于集群 → `clean=false`），
> 须由操作者在证据采集后显式清理并复跑 Registry。该副作用必须写入操作手册。

## 5. A.4 双向可逆性验收

**A.4.2 替换为**：

> 2. 三类材料定义与现状：
>    - **原始 P/U/D 快照**：采集脚本输出（`runs/2026-11-28/sweep-all.results.json`），含 `diff`、
>      `diff_p_to_u`（中间态）与 `diff_u_to_d_group_count`；
>    - **规范化差异结果**：`docs/audits/2026-11-27-wp04-bidirectional-snapshot.json`，
>      **25 条全部来自实测**，其中 24 条 `diff` 为空、1 条非空；
>    - **审计归档材料**：ADR 与证据索引，只描述比较结论，不替代原始快照。
>    生成器为 `docs/audits/2026-11-28-wp04_build_snapshot_v2.py`：只接受结构化实测结果，
>    缺字段即失败，**不含任何按 revision 的分支或硬覆盖**。

**A.4.3 替换为**：

> 3. `no_restore_proven` 必须以规范化差异为空为证据。本轮起快照 `diff` 由生成器从实测结果
> 直转，不允许人工填写；历史上「生成器恒置 `diff:{}` + 末尾硬覆盖 `f8c9d0e1a2b3`」的做法
> 已作为 P0 缺陷记录，禁止复用。

**A.4.4 保留，追加**：

> 4.1 `restore_required` 与 `unproven_blocking` 均不得靠文档、基线或分类表更新解除；
> 只能靠改代码后重跑同一往返并把实测结果写入快照来解除。

**A.4.5 替换为**：

> 5. `intentional_one_way` 必须有安全契约与 Owner 裁决；当前 **0 条**。若 Owner 选择
> f2b3 的「选项 B」，该条将成为第 1 条，并须同时满足 `cluster_wide_zero_authorization_verified=True`。

**A.4.7 替换为**：

> 7. 分类、快照覆盖率与实测结果必须一致，且覆盖率声明须给出 revision 清单与来源文件。
>    本轮覆盖：矩阵 25 条 asymmetric 全部有实测条目（`sweep-all.results.json`，25/25），
>    无「快照含链外 revision」的漂移项。

## 6. A.5 集群级证明边界

**追加 5**：

> 5. Registry 的 `clean=true` 只证明「已声明角色在**当前连接库所属集群**的状态符合注册表」，
> 不证明其他集群、其他库的角色状态。

## 7. A.6 验收要求

**A.6.4 替换为**：

> 4. 双向矩阵无未解除阻断。**当前状态：阻断 2 项**——
>    `e6a7b8c9d0e1`（实测 `restore_required`：降级后多出 `qc_worker`）、
>    `f2b3c4d5e6a7`（`unproven_blocking`：迁移内销毁集群角色，待 Owner 裁决）。
>    因此 A.6.4 尚未满足，D5 不得准入。

**A.6.1 追加**：

> 1.1 三闸门须在**同一 HEAD、同一环境、同一次运行**内取原始日志并归档；本轮三闸门结果：
>     Registry `exit 0 clean=true`、身份 DDL 基线 `exit 0 PASS`、矩阵 `exit 1`（上述 2 项）。

## 8. A.7 明确不代表

**追加**：

- 不代表迁移链已停止创建角色（5 个 revision 仍在 `CREATE ROLE`）；
- 不代表 `restore_required` / `unproven_blocking` 已被解除；
- 不代表 D4 通过或 D5 已准入；
- 不代表本 ADR 已由 Owner 签署。

## 9. §8 待裁决点（更新）

原三项保留，新增第四项：

> 4. `f2b3c4d5e6a7` 的处置：A（退役移出迁移，推荐）/ B（保留并签署单向安全契约）。
>    以及其前置：是否将 `cluster_wide_zero_authorization_verified` 认可为 `True`。
