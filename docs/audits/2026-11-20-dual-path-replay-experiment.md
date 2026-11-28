# 双路径重放实验：主支与功能分支迁移路径的隔离重放与结构对账

> **实验依据**：所有者 2026-11-20 裁决「有条件批准独立 PostgreSQL 实例上的双路径重放实验；
> 不批准升级 `ai_film_verify`」。
>
> **输入清单**：`docs/audits/2026-11-20-fork-inventory-frozen.md`（10 个分叉 revision，五列 + 双侧 SHA256）。
>
> **不构成**：IP-3 通过、ADR-R40 阶段 3 放行、任何迁移文件或 Revision 的改写许可、
> 任何角色的退役许可。也**不还原**历史执行来源（见 §9）。

## 0. 结论摘要

| # | 结论 | 证据强度 |
| --- | --- | --- |
| R1 | `ai_film_industrial` 的结构与「main 链全量重放」**逐字节一致**（唯一差异是 `pg_dump` 随机 token） | **强**（结构层可复核） |
| R2 | 共同段 9 个冲突点在两侧重放的快照**逐字段完全一致** | **强**（实验证明，非推断） |
| R3 | 两支最终态**结构性不可合并**：表 14 vs 33、角色 4 vs 8、schema 集合不同 | **强** |
| R4 | 两个 `b3c4d5e6f7a8` 是**两个不同功能**抢了同一 ID，不是同一功能的两种实现 | **强** |
| R5 | 分支**从未实现**主支的 4 个尾部安全收敛节点（不是「实现不同」，是「没有」） | **强** |
| R6 | T3 的角色属性差异在**干净库上不可观测**（走 `CREATE` 分支不触发） | **强**（实验证明 + 源码） |

一句话：**合并之所以不能做，不是因为 10 个 revision 冲突需要二选一，而是因为分支缺了主支最后 4 个安全收敛节点，且归属模型换了另一套。**

## 1. 实验设计

| 项 | 取值 |
| --- | --- |
| 实例数 | **4 个独立 PostgreSQL 容器**（各自独立集群，非同一集群内多库） |
| 实例 A / B（head 对账） | `localhost:15441`（main）、`localhost:15442`（feat） |
| 实例 C / D（步进快照） | `localhost:15443`（main）、`localhost:15444`（feat） |
| 服务器版本 | 16.15（Debian）— 与现网 15432 一致 |
| 起点 | `8d8d59a91012`（两支均存在） |
| 主支路径 | `main` @ `5352d1979421f41bfb9943774378671cfe94de71`，34 个 revision，单头 `c1a2b3d4e5f6` |
| 分支路径 | `origin/feat/identity-data-ownership` @ `59b13af73640d10b81703b514202173976c432c5`，40 个 revision，单头 `c4e5f6a7b8d9` |
| 重放方式 | `alembic upgrade <rev>`，代码取自对应提交的 `git worktree`（`PYTHONPATH` 指向 worktree，已核实导入的是 worktree 内模块） |
| 口令 | 全部为非默认值，避免触发（或绕过）开发默认口令守卫 |
| 写操作范围 | 仅 4 个一次性容器；**未触碰** `ai_film_industrial`(15432)、`ai_film_verify`、生产 |

**为什么必须 4 个实例而不是 2 个**：角色是**集群级**对象。在已重放过一遍的实例上补跑，
`CREATE ROLE` 会变成 `ALTER ROLE`，观测不到「首次创建」分支——而这恰好是 T3 差异所在。
实例 A/B 用于 head 对账（各自只跑一次），实例 C/D 用于步进快照（同样只跑一次，不重置复用）。

## 2. R1：重放保真度——`industrial` 就是主支链的产物

对 `ai_film_industrial`(15432) 与实例 A(15441) 各做 `pg_dump --schema-only --no-owner --no-privileges`：

```
industrial.sql     1203 行
replay-main.sql    1203 行
diff 结果：仅 2 行不同，均为 pg_dump 的随机 \restrict / \unrestrict token
```

**这是本轮最强的单条证据**：一个运行了近一个月的库，其结构（表、列、约束、索引、触发器、
函数、Policy、分区）与「从空库按 main 链重放 34 个 revision」的结果**逐字节相同**。

推论（严格）：
- 主支迁移链是**可复现的**，重放不产生漂移；
- `ai_film_industrial` **不存在任何可观测的分支结构残留**——若有分支迁移在其中执行过，
  其独有对象（4 个域 schema、6 张转移登记表、`organization_id` 列）必然出现在 dump 里，实际一个都没有。

ACL 层同样吻合（表/序列显式授权，`grantee` 维度）：

| 角色 | `industrial` | `replay-main` | `replay-feat` |
| --- | --- | --- | --- |
| `app_role` | 3 对象 / SELECT+USAGE | 3 / SELECT+USAGE | — |
| `asset_service_role` | 18 / INSERT+SELECT+UPDATE+USAGE | 18 / 同 | — |
| `outbox_relay_role` | 5 / SELECT+USAGE | 5 / 同 | — |
| `project_planning_role` | 23 / INSERT+SELECT+UPDATE+USAGE | 23 / 同 | — |
| `svc_asset` … `svc_workflow`（8 个） | **0（零授权）** | **不存在** | 4~21 对象不等 |

`industrial` 的库级 ACL 与 main 重放**逐行一致**；而 8 个 `svc_*` 在该库**零授权**。

> 这解释了一个此前只能列「来源不可证」的事实：`svc_*` 角色存在（集群级），
> 但 `c2d3e4f5a6b7`（建八域角色 + 授权）的**授权效果**在 `ai_film_industrial` 完全不存在。
> 因此可判定：**该库没有执行过 `c2d3` 的授权部分**；角色的物理存在只能说明「集群上曾有某个库/某次执行建过它们」，
> 具体来源仍不可证（§9）。

## 3. R2：共同段 9 个冲突点，两侧逐字段一致

在实例 C/D 上按链顺序推进，每到一个冲突 revision 立即取样（schema 集合、表与列数、约束清单、
角色属性、表级授权摘要、成员边）。两侧同名快照 **10 个**，其中 9 个（共同段）**逐字段完全一致**：

| 快照 | 表数 | 角色数 | 授权对象数 | 两侧一致 |
| --- | --- | --- | --- | --- |
| `9e3b4c5d6e7f` | 5 | 2 | 16 | 是 |
| `d4e5f6a7b8c9` | 5 | 2 | 15 | 是 |
| `e5f6a7b8c9d0` | 5 | 2 | 15 | 是 |
| `f6a7b8c9d1e2` | 8 | 2 | 24 | 是 |
| `c1d2e3f4a5b6` | 11 | 2 | 37 | 是 |
| `b7c8d9e0f1a2` | 11 | 2 | 26 | 是 |
| `b5c6d7e8f9a0` | 11 | 2 | 23 | 是 |
| `c6d7e8f9a0b1` | 12 | 2 | 24 | 是 |
| `a1b2c3d4e5f6` | 12 | 2 | 24 | 是 |
| `zz-head` | **14 vs 33** | **4 vs 8** | **49 vs 96** | **否** |

**这 9 个「是」把两处结论从推断升级为实验证据**：

- **T2（7 处角色名来源）**：在 `POSTGRES_APP_USER=app_role`（该字段默认值）前提下，
  两侧产出**完全相同**的表、约束、角色属性与授权。等价性成立，但**仅在该前提下成立**（见冻结清单 §1.2）。
- **T4（`b7c8` 的 helper 调用点）**：主支「不传 `roles`、用默认」与分支「显式传 `roles=(APP_ROLE,)`」
  在默认配置下产出**同一结果**。因此 T4 的风险**不在效果**，而在**组合**：
  一侧的迁移配另一侧的 helper 会 `TypeError`（冻结清单 §3.2），与效果等价无关。

副产物：`d4e5f6a7b8c9`（撤销 app_role 对 outbox 的 DELETE）与 `b7c8d9e0f1a2`（权限基线反转）
的效果在步进快照中直接可见——授权对象数 16→15、37→26。这是「迁移确实执行了它声称的事」的旁证。

## 4. R3：最终态的结构性差异

| 维度 | main @ `c1a2b3d4e5f6` | feat @ `c4e5f6a7b8d9` |
| --- | --- | --- |
| business schema | `asset_service`、`audit`、`auth`、`outbox`、`project_planning` | `asset_service`、`auth`、`outbox`、`project_planning`、**`generation_execution`、`qc_governance`、`shot_planning`、`workflow`** |
| `audit` schema | **有**（`status_transitions` + 4 约束/触发器） | **无** |
| `organizations` 表 | **有**（`uq_organizations_owner_tenant`） | **无** |
| `projects` 组织外键 | **有**（`fk_projects_organization_tenant`） | **无** |
| `asset_versions` | 21 列，**有** `fk_asset_versions_project_tenant` | 22 列（多 `organization_id`），**无任何外键** |
| `qc_reports` | 14 列 | **16 列**（多 `organization_id`、`project_id` 冗余归属） |
| `episodes` | 有 `ck_episodes_status` + `fk_episodes_project` | **两者皆无** |
| 状态取值约束 | `ck_asset_versions_status`（主支 c7d8） | `ck_asset_versions_status_registered`（分支 b3c4）+ 6 张 `*_status_transitions` 登记表 |
| 角色 | 4 个旧应用角色 | 8 个 `svc_*` |
| 表总数 | 14 | 33 |

**两条与审查报告互为印证的实测**：

1. 分支的 `asset_versions` 只有 `project_id` 字符串列，**没有** `(project_id, tenant_id) → projects(entity_id, tenant_id)`
   复合外键。数据库无法保证「资产的项目真实存在」或「与资产同租户」——审查报告 P1-3 的第一条被实验证实。
2. 分支的 `qc_reports` 多出 `organization_id`、`project_id` 两列，而它们与父资产归属之间**没有数据库约束**，
   项目级 RLS 却直接读这两列。审查报告 P1-3 的第二条同样被证实。

## 5. R4：两个 `b3c4d5e6f7a8` 是两个不同功能

| | main `b3c4d5e6f7a8_asset_project_ownership` | feat `b3c4d5e6f7a8_asset_status_transitions` |
| --- | --- | --- |
| 创建物 | `asset_versions.project_id` + `fk_asset_versions_project_tenant` | `asset_service.asset_status_transitions`（2 列，播种合法边）+ `ck_asset_versions_status_registered` + 改写 `asset_versions_guard()` |
| 解决的问题 | 资产归属哪个项目（跨 schema 复合外键） | 状态只能沿已登记边推进（状态机守卫） |
| 是否依赖对方 | 不依赖 | 不依赖 |

**关键纠正**：审查报告称分支 `b3c4` 含「状态转移**事实表**」。实测不符——它建的是
`(from_status, to_status)` 两列的**规则登记表**（播种「哪些转移合法」），不是记录「发生过什么转移」的历史事实表。
后者由 main 的 `d8e9f0a1b2c3` 建在 `audit.status_transitions`。

因此两者**不是同一功能的两种实现，可以共存**。这直接支持所有者「保留 main 版历史身份、
分支状态转换能力用唯一新 Revision 前向补齐」的裁决——补齐时不需要在两者间做减法。

## 6. R5：分支缺的是「4 个安全收敛节点」，不是「4 个不同实现」

主支独有 11 个 revision，分两组：

**A 组（7 个，归属与角色拆分——分支用 `c2d3`/`d3e4`/`e7f8` 另做了一套）**

`9f1a2b3c4d5e`（organizations + 归属外键）、`7a8b9c0d1e2f`（relay 角色）、
`8f3b2c1d4e5a`（域角色拆分）、`ae8724980e0b`（分区授权缺口）、`b9e1c2d3f4a5`（relay 列级 UPDATE 收口）、
`c1f2e3d4a5b6`（回收 app_role 对 outbox 写权限）、`d2e3f4a5b6c7`（组织租户复合约束 + organizations RLS）

**B 组（4 个，尾部安全收敛——分支完全没有对应物）**

| Revision | 作用 | 分支有无对应 |
| --- | --- | --- |
| `c7d8d9e0f1a2` | `ck_*_status` 状态取值约束 | 仅 `asset_versions` 有分支配的等价物；`episodes`/`projects`/`script_revisions` **无** |
| `d8e9f0a1b2c3` | `audit` schema + 状态迁移事实表 | **无** |
| `e9f0a1b2c3d4` | 组织维度 RLS + `FORCE` | **无**（且无 organizations 表，无从谈起） |
| `c1a2b3d4e5f6` | 业务域角色表级最小权限（DATA-6） | **无** |

这是「不能以 merge 方式合并」的结构性原因：**A 组可以按归属模型重做，B 组是缺口**。
若以功能分支覆盖主支，会丢掉 `audit` 事实表、`FORCE RLS`、`episodes`/`projects` 的状态约束；
若以主支覆盖分支，会丢掉 14 个业务域迁移。两条都不是「解决冲突」，而是「丢东西」。

## 7. R6：T3 在干净库上不可观测（一个必须说清的方法论限制）

`9e3b4c5d6e7f` 两侧的 `ALTER ROLE` 语义不同：

- main：`ALTER ROLE ... LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD ...` + `reject_development_default_password(...)`
- feat：`ALTER ROLE ... PASSWORD ...`（不收敛属性，无口令守卫）

但**在全新集群上两者的快照完全一致**——因为角色不存在时走的是 `CREATE ROLE` 分支，
两支的 `CREATE` 语句相同，`ALTER` 分支根本没被执行。

**所以「T3 效果不等价」这一结论不能由本次重放实验证伪或证实**，它依赖的是：
1. 逐句源码比对（`ALTER` 语句差异确定）；
2. PostgreSQL 语义：`ALTER ROLE ... NOBYPASSRLS` 会把存量 `BYPASSRLS=t` 收敛为 `f`，而 `ALTER ROLE ... PASSWORD` 不会。

要实验验证，必须**构造脏前置状态**（先建一个 `BYPASSRLS=t` 的 `app_role`，再跑 `9e3b`）。
本轮未做，列为后续可选验证项。**这是「重放不能证明历史」的一个具体例证**：干净重放会掩盖只对存量状态生效的差异。

## 8. 对整合方案的直接含义

| 裁决项 | 本实验的支持程度 |
| --- | --- |
| 以 main 为整合基底 | **支持**。main 链可复现（R1），且含 B 组 4 个安全收敛节点（R5） |
| 分支能力「用唯一新 Revision 前向补齐」 | **支持且可行**。`b3c4` 两侧是不同功能，可共存（R4）；但补齐顺序受归属模型约束（R3：分支资产无复合外键、QC 有冗余归属） |
| 不整侧胜出 | **必要**。任一侧覆盖另一侧都会丢东西（R5） |
| 8 个 `svc_*` 保留 | **支持**。它们在 `industrial` 零授权，不构成现存权限风险；在 feat 重放中服务于 4 个新域（R1/R3） |
| 不升级 `ai_film_verify` | **支持**。它当前结构是「main 链第 30/34 步」，是有价值的历史参照；升级即销毁该参照 |

**补齐时必须先解决的三件事**（本实验新增的约束，此前未列）：

1. **归属模型先行**：分支的 14 个域迁移依赖 `d3e4`（`organization_id` 全实体 + `project_id`），
   而 `d3e4` 的 `organization_id = tenant_id` 与主支的 `organizations` 表冲突。先定归属模型，才能定补齐顺序。
2. **复合外键补建**：分支的 14 个域表在无 `fk_asset_versions_project_tenant` 的前提下建立，
   补齐时该外键可能因存量数据不满足而失败——需要前向数据校验，不能直接 `ADD CONSTRAINT`。
3. **`qc_reports` 冗余归属的处置**：分支的 `organization_id`/`project_id` 两列若保留，
   必须补「与父资产一致」的约束或触发器；若删除，需先改所有读取这两列的 RLS 策略。

## 9. 本实验**不能**判定的（严格边界）

| 问题 | 为什么不能 |
| --- | --- |
| `industrial` 当年执行的是 main 版还是 feat 版 `b3c4` | 结构只反映**最终态**。分支 `b3c4` 若要被后续 main 迁移完全覆盖，理论上也可能（但 `audit` 表由 `d8e9` 建，无法区分来源）。`alembic_version` 只存 ID |
| 8 个 `svc_*` 由谁、何时、在哪个库创建 | 集群级对象，无 `pg_roles` 创建时间（`pg_authid` 不记），无审计日志。只能确认「`industrial` 未执行其授权部分」 |
| 生产及其他环境执行过什么 | 本机证据不得外推 |
| T3 在真实存量角色上的后果 | 干净重放走 `CREATE` 分支，未触发 `ALTER`（§7） |

> 结论表述必须停在「结构效果与哪一侧一致」，不能升级为「历史上执行了哪一份文件」。

## 10. 复现

```bash
# 1) 建 4 个一次性实例（各自独立集群）
docker run -d --name afs-replay-main -e POSTGRES_PASSWORD=<ROTATED-2026-11-28> -e POSTGRES_DB=ai_film_industrial -p 15441:5432 postgres:16
docker run -d --name afs-replay-feat -e POSTGRES_PASSWORD=<ROTATED-2026-11-28> -e POSTGRES_DB=ai_film_industrial -p 15442:5432 postgres:16

# 2) 固定提交的 worktree（避免工作区污染）
git worktree add --detach /tmp/afs-replay/main 5352d197
git worktree add --detach /tmp/afs-replay/feat 59b13af7

# 3) 重放（环境变量须含非默认口令）
cd /tmp/afs-replay/main && \
  POSTGRES_HOST=localhost POSTGRES_PORT=15441 POSTGRES_DB=ai_film_industrial \
  POSTGRES_USER=postgres POSTGRES_PASSWORD=<ROTATED-2026-11-28> \
  POSTGRES_APP_PASSWORD=replay_app_pw POSTGRES_RELAY_PASSWORD=replay_relay_pw \
  POSTGRES_PROJECT_PLANNING_PASSWORD=replay_pp_pw POSTGRES_ASSET_SERVICE_PASSWORD=replay_as_pw \
  PYTHONPATH=/tmp/afs-replay/main python -m alembic -c alembic.ini upgrade head

# 4) 结构对账
pg_dump -h localhost -p 15441 -U postgres -d ai_film_industrial \
  --schema-only --no-owner --no-privileges > replay-main.sql

# 5) 步进快照（务必用全新集群，勿复用已重放实例）
python docs/audits/mig-recon/step_snapshot.py main 15443 /tmp/afs-replay/main docs/audits/mig-recon/steps/main
python docs/audits/mig-recon/step_snapshot.py feat 15444 /tmp/afs-replay/feat docs/audits/mig-recon/steps/feat
```

**产物**：

| 文件 | 内容 |
| --- | --- |
| `docs/audits/mig-recon/snapshots/{industrial,replay-main,replay-feat}.sql` | 三份 `pg_dump --schema-only` |
| `docs/audits/mig-recon/steps/{main,feat}/*.json` | 逐 revision 快照（schema/表/约束/角色/授权/成员边） |
| `docs/audits/mig-recon/step_snapshot.py` | 步进快照脚本 |
| `docs/audits/mig-recon/build_fork_inventory.py` | 冻结清单生成脚本 |

## 11. 清理

4 个容器均为一次性实例，实验后可整体销毁（`docker rm -f afs-replay-main afs-replay-feat afs-step-main afs-step-feat`）。
本轮**未删除**，以便复审者复跑核对；销毁前不产生任何对外影响，且与 15432 集群完全隔离。

> 注意：`docker rm -f` 会连同匿名卷一并丢弃；若需保留快照，先取走 `snapshots/` 与 `steps/`。
