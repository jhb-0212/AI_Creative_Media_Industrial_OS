# T3 脏状态验证：原始结果（机器生成，勿手改）

- 实验库：`t3_lab`（模板 `ai_film_industrial`，端口 15444，分支链已重放到 head）
- 对照：端口 15443（主支链 head），仅用于可空性/MATCH SIMPLE 对照，事务内已 ROLLBACK
- 步骤总数 49，PASS 49，FAIL 0
- 状态记法：`fk2=t/f` 约束已校验/未校验（f 即 NOT VALID）；`assert=O/D`、`guard=O/D` 表示哨兵触发器与内容守卫处于启用/临时关闭；`-` 表示对象不存在

## 1. 七类场景覆盖

| 场景 | 覆盖步骤 | 该场景要判定的事实 |
| --- | --- | --- |
| 1 正常归属 | `B1 → B2` | 完全匹配的资产可写入，且干净数据上外键可建 |
| 2 孤儿项目 | `C2 → C3 → C10 → C12` | 无约束时孤儿可写入；建约束时被拒而非静默删；修复后方可 VALIDATE |
| 3 租户错配 | `D2 → D3` | 两列外键即可拦截 project+tenant 不一致 |
| 4 组织错配 | `E1 → E2 → E4` | 两列外键放行、三列外键拦截 —— 两列方案的结构性盲区 |
| 5 legacy-unassigned 哨兵 | `F1 → F2 → F9` | 触发器只挡魔法值、不查存在性；历史哨兵行阻塞外键，必须人工映射 |
| 6 NULL / MATCH SIMPLE | `G1 + 可空性审计` | 分支侧归属列全 NOT NULL，放行路径列级不可达；主支 organization_id 可空则可绕过 |
| 7 NOT VALID 分阶段 | `C4 → C5 → C7 → C12` | 跳过历史扫描但新写入仍受约束；未清脏即 VALIDATE 会失败且状态保持 f |

## 2. 逐步结果

| 阶段 | 步骤 | 期望 | 实际 SQLSTATE | 行数(前→后) | 行哈希(前→后) | 状态(前 → 后) | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A0 | 分支底座直接建两列外键 | 42830 | `42830` | 0→0 | `-`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| A1 | 补 organizations 表（镜像 main 的 DDL） | 成功 | `OK` | 0→0 | `-`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| A2 | 写入两个组织（T1/O1、T2/O2） | 成功 | `OK` | 0→0 | `-`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| A3 | projects 补 (entity_id, tenant_id) 唯一键 | 成功 | `OK` | 0→0 | `-`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| A4 | projects 补三列唯一键 | 成功 | `OK` | 0→0 | `-`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| A5 | projects → organizations 复合外键 | 成功 | `OK` | 0→0 | `-`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| A6 | 写入项目 P1（属 T1/O1） | 成功 | `OK` | 0→0 | `-`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| B1 | 插入项目/租户/组织完全匹配的资产 | 成功 | `OK` | 0→1 | `-`→`44d9ac49` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| B2 | 干净数据下建两列外键 | 成功 | `OK` | 1→1 | `44d9ac49`→`44d9ac49` | `fk2=- fk3=- assert=O guard=O` → `fk2=t fk3=- assert=O guard=O` | PASS |
| C1 | 撤约束以注入脏数据 | 成功 | `OK` | 1→1 | `44d9ac49`→`44d9ac49` | `fk2=t fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| C2 | 注入孤儿资产（project 不存在） | 成功 | `OK` | 1→2 | `44d9ac49`→`a6088a0e` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| C3 | 含孤儿时直接建两列外键 | 23503 | `23503` | 2→2 | `a6088a0e`→`a6088a0e` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| C4 | 改用 NOT VALID 分阶段添加 | 成功 | `OK` | 2→2 | `a6088a0e`→`a6088a0e` | `fk2=- fk3=- assert=O guard=O` → `fk2=f fk3=- assert=O guard=O` | PASS |
| C5 | NOT VALID 下再写孤儿 | 23503 | `23503` | 2→2 | `a6088a0e`→`a6088a0e` | `fk2=f fk3=- assert=O guard=O` → `fk2=f fk3=- assert=O guard=O` | PASS |
| C6 | NOT VALID 下写合规资产 | 成功 | `OK` | 2→3 | `a6088a0e`→`25a9dba8` | `fk2=f fk3=- assert=O guard=O` → `fk2=f fk3=- assert=O guard=O` | PASS |
| C7 | 脏数据未修就 VALIDATE | 23503 | `23503` | 3→3 | `25a9dba8`→`25a9dba8` | `fk2=f fk3=- assert=O guard=O` → `fk2=f fk3=- assert=O guard=O` | PASS |
| C8 | 就地修复：孤儿映射到真实项目 | P0001 | `P0001` | 3→3 | `25a9dba8`→`25a9dba8` | `fk2=f fk3=- assert=O guard=O` → `fk2=f fk3=- assert=O guard=O` | PASS |
| C9 | 维护窗口：临时关闭内容守卫 | 成功 | `OK` | 3→3 | `25a9dba8`→`25a9dba8` | `fk2=f fk3=- assert=O guard=O` → `fk2=f fk3=- assert=O guard=D` | PASS |
| C10 | 守卫关闭期间执行同一修复 | 成功 | `OK` | 3→3 | `25a9dba8`→`7d0c022d` | `fk2=f fk3=- assert=O guard=D` → `fk2=f fk3=- assert=O guard=D` | PASS |
| C11 | 重新启用内容守卫 | 成功 | `OK` | 3→3 | `7d0c022d`→`7d0c022d` | `fk2=f fk3=- assert=O guard=D` → `fk2=f fk3=- assert=O guard=O` | PASS |
| C12 | 修复后 VALIDATE | 成功 | `OK` | 3→3 | `7d0c022d`→`7d0c022d` | `fk2=f fk3=- assert=O guard=O` → `fk2=t fk3=- assert=O guard=O` | PASS |
| D0 | 撤约束 | 成功 | `OK` | 3→3 | `7d0c022d`→`7d0c022d` | `fk2=t fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| D1 | 清空资产表 | 成功 | `OK` | 3→0 | `7d0c022d`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| D2 | 注入租户错配行 | 成功 | `OK` | 0→1 | `-`→`970521dd` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| D3 | 含租户错配时建两列外键 | 23503 | `23503` | 1→1 | `970521dd`→`970521dd` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| D4 | 就地改租户以图绕过 | P0001 | `P0001` | 1→1 | `970521dd`→`970521dd` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| E0 | 清空资产表 | 成功 | `OK` | 1→0 | `970521dd`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| E1 | 注入组织错配行 | 成功 | `OK` | 0→1 | `-`→`8f970667` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| E2 | 两列外键能否发现组织错配 | 成功 | `OK` | 1→1 | `8f970667`→`8f970667` | `fk2=- fk3=- assert=O guard=O` → `fk2=t fk3=- assert=O guard=O` | PASS |
| E3 | 撤两列约束 | 成功 | `OK` | 1→1 | `8f970667`→`8f970667` | `fk2=t fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| E4 | 改用三列外键 | 23503 | `23503` | 1→1 | `8f970667`→`8f970667` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| E5 | 就地修复组织归属 | P0001 | `P0001` | 1→1 | `8f970667`→`8f970667` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| E6 | 维护窗口：关闭内容守卫 | 成功 | `OK` | 1→1 | `8f970667`→`8f970667` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=D` | PASS |
| E7 | 守卫关闭期间修复组织归属 | 成功 | `OK` | 1→1 | `8f970667`→`05b9b8e2` | `fk2=- fk3=- assert=O guard=D` → `fk2=- fk3=- assert=O guard=D` | PASS |
| E8 | 重新启用内容守卫 | 成功 | `OK` | 1→1 | `05b9b8e2`→`05b9b8e2` | `fk2=- fk3=- assert=O guard=D` → `fk2=- fk3=- assert=O guard=O` | PASS |
| E9 | 修复后建三列外键 | 成功 | `OK` | 1→1 | `05b9b8e2`→`05b9b8e2` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=t assert=O guard=O` | PASS |
| F0 | 清空资产表 | 成功 | `OK` | 1→0 | `05b9b8e2`→`-` | `fk2=- fk3=t assert=O guard=O` → `fk2=- fk3=t assert=O guard=O` | PASS |
| F0b | 撤掉 E9 残留约束 | 成功 | `OK` | 0→0 | `-`→`-` | `fk2=- fk3=t assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| F1 | 触发器是否拦哨兵值的新写入 | P0001 | `P0001` | 0→0 | `-`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| F2 | 触发器是否校验项目真实存在 | 成功 | `OK` | 0→1 | `-`→`f2ce0c5e` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| F3 | 临时关闭哨兵触发器（仅这一个） | 成功 | `OK` | 1→1 | `f2ce0c5e`→`f2ce0c5e` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=D guard=O` | PASS |
| F4 | 还原历史哨兵行 | 成功 | `OK` | 1→2 | `f2ce0c5e`→`0250ec7d` | `fk2=- fk3=- assert=D guard=O` → `fk2=- fk3=- assert=D guard=O` | PASS |
| F5 | 维护窗口：关闭内容守卫 | 成功 | `OK` | 2→2 | `0250ec7d`→`0250ec7d` | `fk2=- fk3=- assert=D guard=O` → `fk2=- fk3=- assert=D guard=D` | PASS |
| F6 | 把幽灵项目行映射到真实项目 | 成功 | `OK` | 2→2 | `0250ec7d`→`47e3094d` | `fk2=- fk3=- assert=D guard=D` → `fk2=- fk3=- assert=D guard=D` | PASS |
| F7 | 重新启用内容守卫 | 成功 | `OK` | 2→2 | `47e3094d`→`47e3094d` | `fk2=- fk3=- assert=D guard=D` → `fk2=- fk3=- assert=D guard=O` | PASS |
| F8 | 重新启用哨兵触发器 | 成功 | `OK` | 2→2 | `47e3094d`→`47e3094d` | `fk2=- fk3=- assert=D guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| F9 | 含历史哨兵行时建两列外键 | 23503 | `23503` | 2→2 | `47e3094d`→`47e3094d` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| G0 | 清空资产表 | 成功 | `OK` | 2→0 | `47e3094d`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |
| G1 | project_id 置 NULL 是否可写入 | 23502 | `23502` | 0→0 | `-`→`-` | `fk2=- fk3=- assert=O guard=O` → `fk2=- fk3=- assert=O guard=O` | PASS |

## 3. 步骤意图与判定依据

- **A0 分支底座直接建两列外键**：被引用侧无 (entity_id, tenant_id) 唯一约束 → 约束物理不可建；是目标键缺失，不是数据脏
- **A1 补 organizations 表（镜像 main 的 DDL）**：分支链整张表不存在（阻塞 B1 与 L2→L1 外键）
- **A3 projects 补 (entity_id, tenant_id) 唯一键**：阻塞 A8；主支已有同名约束
- **A4 projects 补三列唯一键**：三列外键的被引用目标；分支 projects.organization_id 为 NOT NULL
- **A5 projects → organizations 复合外键**：打通 L2→L1；分支侧两列均 NOT NULL，故 MATCH SIMPLE 无放行口
- **B1 插入项目/租户/组织完全匹配的资产**：场景 1 正向
- **B2 干净数据下建两列外键**：补齐前置后可建 → 反证 A0 的 42830 是目标键缺失，不是数据脏
- **C1 撤约束以注入脏数据**：历史库通常无约束，脏数据正由此产生
- **C2 注入孤儿资产（project 不存在）**：场景 2：无约束时孤儿写入成功——归属事实链断裂的实证
- **C3 含孤儿时直接建两列外键**：外键预检识别孤儿并拒绝，且不静默删数据（行数不变）
- **C4 改用 NOT VALID 分阶段添加**：场景 7：NOT VALID 跳过历史扫描，新写入仍受约束
- **C5 NOT VALID 下再写孤儿**：场景 7 关键：历史脏数据未清时，新写入同样被拦
- **C6 NOT VALID 下写合规资产**：合规写入不被误伤
- **C7 脏数据未修就 VALIDATE**：NOT VALID 不是长期豁免；失败后约束保持 convalidated=f
- **C8 就地修复：孤儿映射到真实项目**：**分支结构事实**：project_id 属不可变内容列，就地 UPDATE 被守卫拒绝（ADR-R11）
- **C9 维护窗口：临时关闭内容守卫**：单语句：即使后续失败也不会把守卫留在关闭态（下一阶段重置会重建）
- **C10 守卫关闭期间执行同一修复**：同一语句、同一数据，仅守卫状态不同 → 证明 C8 的 P0001 唯一来源是守卫
- **C12 修复后 VALIDATE**：约束转为已验证（fk2=t）
- **D0 撤约束**：TRUNCATE 不触发行级触发器，故可作为阶段重置；守卫拦不住 TRUNCATE，属运维权限面的边界
- **D2 注入租户错配行**：项目 P1 属 T1，资产写 T2；组织 O2 属 T2，故唯一违规点是 project+tenant 组合
- **D3 含租户错配时建两列外键**：场景 3：租户错配被拒
- **D4 就地改租户以图绕过**：复证：归属列全部不可就地改写，脏数据只能前向新建版本或走维护窗口
- **E0 清空资产表**：上一阶段未留约束
- **E1 注入组织错配行**：项目 P1 属 O1，资产写 O2；租户同为 T1
- **E2 两列外键能否发现组织错配**：**预期成功**：两列只比 project+tenant → 组织错配被放行，这是两列方案的盲区
- **E3 撤两列约束**：准备三列方案
- **E4 改用三列外键**：三列把 organization_id 纳入比较 → 组织错配被拒
- **E5 就地修复组织归属**：organization_id 同样是不可变内容列
- **E7 守卫关闭期间修复组织归属**：只改这一行，影响行数 1→1、哈希变化可见
- **E9 修复后建三列外键**：合法数据上无副作用
- **F0 清空资产表**：E9 残留的 fk3 不拦 TRUNCATE
- **F0b 撤掉 E9 残留约束**：隔离变量：本阶段只让触发器起作用，不让外键抢答
- **F1 触发器是否拦哨兵值的新写入**：场景 5：project_id 取哨兵值 'legacy-unassigned'，分支的 assert_asset_versions_project_assigned 触发器应拒
- **F2 触发器是否校验项目真实存在**：**预期成功**：只认哨兵字符串，不查存在性 → 触发器不是归属约束，只挡一个魔法值
- **F3 临时关闭哨兵触发器（仅这一个）**：单语句：即使后续插入失败，也不会把触发器留在关闭态
- **F4 还原历史哨兵行**：真实历史行由触发器上线前写入，此处等价复现
- **F6 把幽灵项目行映射到真实项目**：幽灵行可修；哨兵行不在本步修复，留给 F9 证明必须人工决策
- **F9 含历史哨兵行时建两列外键**：哨兵值必须人工映射为真实项目，禁止自动当真实项目
- **G1 project_id 置 NULL 是否可写入**：场景 6：该列 NOT NULL → 分支侧 MATCH SIMPLE 放行路径在列级即不可达

## 4. 错误明细（仅非 OK 行，含完整首行报错）

- **A0 分支底座直接建两列外键** `42830` — there is no unique constraint matching given keys for referenced table "projects"
- **C3 含孤儿时直接建两列外键** `23503` — insert or update on table "asset_versions" violates foreign key constraint "fk_asset_versions_project_tenant"
- **C5 NOT VALID 下再写孤儿** `23503` — insert or update on table "asset_versions" violates foreign key constraint "fk_asset_versions_project_tenant"
- **C7 脏数据未修就 VALIDATE** `23503` — insert or update on table "asset_versions" violates foreign key constraint "fk_asset_versions_project_tenant"
- **C8 就地修复：孤儿映射到真实项目** `P0001` — asset_versions.project_id 为不可变内容列，禁止改写（ADR-R11）
- **D3 含租户错配时建两列外键** `23503` — insert or update on table "asset_versions" violates foreign key constraint "fk_asset_versions_project_tenant"
- **D4 就地改租户以图绕过** `P0001` — asset_versions.tenant_id 为不可变内容列，禁止改写（ADR-R11）
- **E4 改用三列外键** `23503` — insert or update on table "asset_versions" violates foreign key constraint "fk_asset_versions_project_tenant_org"
- **E5 就地修复组织归属** `P0001` — asset_versions.organization_id 为不可变内容列，禁止改写（ADR-R11）
- **F1 触发器是否拦哨兵值的新写入** `P0001` — 新写入必须带真实 project_id（legacy-unassigned 仅用于迁移前的历史数据）
- **F9 含历史哨兵行时建两列外键** `23503` — insert or update on table "asset_versions" violates foreign key constraint "fk_asset_versions_project_tenant"
- **G1 project_id 置 NULL 是否可写入** `23502` — null value in column "project_id" of relation "asset_versions" violates not-null constraint

## 5. 归属列可空性审计（分支侧全库）

共 62 个归属列；`is_nullable` 全为 `NO` 时，复合外键的 MATCH SIMPLE 放行路径不可达。

| 表 | 列 | is_nullable |
| --- | --- | --- |
| `asset_service.asset_versions` | organization_id | NO |
| `asset_service.asset_versions` | project_id | NO |
| `asset_service.asset_versions` | tenant_id | NO |
| `asset_service.delivery_manifests` | organization_id | NO |
| `asset_service.delivery_manifests` | project_id | NO |
| `asset_service.delivery_manifests` | tenant_id | NO |
| `asset_service.qc_reports` | organization_id | NO |
| `asset_service.qc_reports` | project_id | NO |
| `asset_service.qc_reports` | tenant_id | NO |
| `auth.revoked_tokens` | tenant_id | NO |
| `generation_execution.generation_tasks` | organization_id | NO |
| `generation_execution.generation_tasks` | project_id | NO |
| `generation_execution.generation_tasks` | tenant_id | NO |
| `generation_execution.production_routes` | organization_id | NO |
| `generation_execution.production_routes` | tenant_id | NO |
| `generation_execution.shot_route_selections` | organization_id | NO |
| `generation_execution.shot_route_selections` | project_id | NO |
| `generation_execution.shot_route_selections` | tenant_id | NO |
| `outbox.outbox_events` | tenant_id | NO |
| `outbox.outbox_events_default` | tenant_id | NO |
| `outbox.outbox_events_y202611` | tenant_id | NO |
| `outbox.outbox_events_y202612` | tenant_id | NO |
| `project_planning.episodes` | organization_id | NO |
| `project_planning.episodes` | project_id | NO |
| `project_planning.episodes` | tenant_id | NO |
| `project_planning.organizations` | organization_id | NO |
| `project_planning.organizations` | tenant_id | NO |
| `project_planning.projects` | organization_id | NO |
| `project_planning.projects` | tenant_id | NO |
| `project_planning.script_revisions` | organization_id | NO |
| `project_planning.script_revisions` | project_id | NO |
| `project_planning.script_revisions` | tenant_id | NO |
| `qc_governance.gate_decisions` | organization_id | NO |
| `qc_governance.gate_decisions` | project_id | NO |
| `qc_governance.gate_decisions` | tenant_id | NO |
| `qc_governance.qc_review_sessions` | organization_id | NO |
| `qc_governance.qc_review_sessions` | project_id | NO |
| `qc_governance.qc_review_sessions` | tenant_id | NO |
| `shot_planning.constraint_packs` | organization_id | NO |
| `shot_planning.constraint_packs` | project_id | NO |
| `shot_planning.constraint_packs` | tenant_id | NO |
| `shot_planning.shot_drafts` | organization_id | NO |
| `shot_planning.shot_drafts` | project_id | NO |
| `shot_planning.shot_drafts` | tenant_id | NO |
| `shot_planning.shot_revisions` | organization_id | NO |
| `shot_planning.shot_revisions` | project_id | NO |
| `shot_planning.shot_revisions` | tenant_id | NO |
| `shot_planning.shot_specs` | organization_id | NO |
| `shot_planning.shot_specs` | project_id | NO |
| `shot_planning.shot_specs` | tenant_id | NO |
| `shot_planning.shot_visual_plans` | organization_id | NO |
| `shot_planning.shot_visual_plans` | project_id | NO |
| `shot_planning.shot_visual_plans` | tenant_id | NO |
| `workflow.approval_flows` | organization_id | NO |
| `workflow.approval_flows` | project_id | NO |
| `workflow.approval_flows` | tenant_id | NO |
| `workflow.approval_steps` | organization_id | NO |
| `workflow.approval_steps` | project_id | NO |
| `workflow.approval_steps` | tenant_id | NO |
| `workflow.approval_tickets` | organization_id | NO |
| `workflow.approval_tickets` | project_id | NO |
| `workflow.approval_tickets` | tenant_id | NO |

- 可空归属列数量：**0**

## 6. 对照：主支 organization_id 可空（已回滚）

- projects.organization_id is_nullable：`YES`
- organizations 表行数：`0`
- 反证：organization_id 非空且不存在：`23503（insert or update on table "projects" violates foreign key constraint "fk_projects_organization_tenan）`
- 正证：organization_id 置 NULL：`成功（MATCH SIMPLE 跳过校验：组织表 0 行仍写入成功）`

## 7. 最终状态

- 状态：`fk2=- fk3=- assert=O guard=O`
- 资产表最终行数：0
