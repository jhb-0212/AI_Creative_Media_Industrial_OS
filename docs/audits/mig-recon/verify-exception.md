# 受控例外迁移验证结果（b0c9d8e7f6a5）

- 执行时间：2026-11-21T12:10:28+08:00
- 目标：`localhost:15444` 克隆库 `t3x_lab`（模板 `ai_film_industrial`），用完即删
- 结果：**37/37**

| # | 阶段 | 用例 | 结果 | 说明 |
| --- | --- | --- | --- | --- |
| B-1 | 基线 | 回退归属栈至激活前基线（13 层） | PASS | projects 3→0，asset_versions 3→0; 归属三列外键=0; 两列组织外键=0; 例外映射表=0; 例外判定函数=0; RLS 表达式备份表=0; 批次二归属外键=0; 批次三归属外键=0; 批次五新表=0; 批次五收敛边=0; 批次六归档守卫=0; 批次七阻断集合函数=0; 批次九返工表=0; 批次九返工引用外键=0; 批次四父表唯一键=0; 批次四复合外键=0 |
| B0 | 基线 | 记录 13 个守卫函数定义哈希 | PASS | 13 个 |
| B1 | 基线 | 升级前：改归属列一律被拒 | PASS | AV=P0001; GD=P0001; QR=P0001; AVC=P0001 |
| U1 | 升级 | 执行 upgrade_sqls()（23 条） | PASS | 无异常 |
| S1 | 结构 | 13 个守卫均已注入接入点 | PASS | 13/13 |
| S2 | 结构 | 判定函数为 INVOKER、读映射表函数为 DEFINER 且固定 search_path | PASS | allows.secdef=False match.secdef=True match.cfg=search_path=pg_catalog, project_planning |
| S3 | 结构 | 映射表唯一键与 reason 非空校验存在 | PASS | ['ck_ownership_repair_map_reason_len', 'uq_ownership_repair_map'] |
| S4 | 结构 | 待处理部分索引存在 | PASS | count=1 |
| S5 | 结构 | PUBLIC 对两个函数均无 EXECUTE（附录 E #15） | PASS | allows=False match=False |
| S6 | 结构 | 专用角色存在且 NOLOGIN | PASS | canlogin=False |
| F1 | 判定 | 无工单令牌 → false | PASS | got=False |
| F2 | 判定 | 无令牌时一切为 false（映射无关） | PASS | got=False |
| F3 | 判定 | 工单不匹配 → false | PASS | got=False |
| F4 | 判定 | 四条件满足 → true | PASS | got=True |
| F5 | 判定 | 变更含非归属列 → false（内容列不受例外保护） | PASS | got=False |
| F6 | 判定 | 无任何变更 → false | PASS | got=False |
| F7 | 判定 | 映射行已盖章 → false | PASS | got=False |
| F8 | 判定 | 非 afs_data_repair 成员 → 不放行（false 或权限拒绝） | PASS | got=False |
| F9 | 判定 | 13 张表的 table_name 匹配全部成立 | PASS | 13/13 [] |
| E1 | 端到端 | 类别 A（asset_versions）四条件满足 → 改成功且值真的变了 | PASS | UPDATE 1 now=proj-2 |
| E2 | 端到端 | 无令牌改另一行 → P0001（不是权限错误） | PASS | P0001: asset_versions.project_id 为不可变内容列，禁止改写（ADR-R11） |
| E3 | 端到端 | 有令牌但改内容列 → P0001 | PASS | P0001: asset_versions.sha256 为不可变内容列，禁止改写（ADR-R11） |
| E4 | 端到端 | 归属列+内容列同时改 → P0001（绕过尝试被拒） | PASS | P0001: asset_versions.sha256 为不可变内容列，禁止改写（ADR-R11） |
| E5a | 端到端 | 探针角色的表权限已就位（否则 E5 测到的是权限拒绝） | PASS | update=True usage=True |
| E5 | 端到端 | 非成员：其余三条件全满足仍被拒 → P0001（拦在守卫而非权限） | PASS | P0001: gate_decisions 是 append-only Gate 裁决事实，禁止 UPDATE （裁决不可改写，修正须新建一条裁决并附新证据） |
| E6 | 端到端 | 类别 B（gate_decisions 无条件 RAISE）四条件满足 → 改成功 | PASS | UPDATE 1 now=proj-2 |
| E7 | 端到端 | 键列为 qc_report_id 的表也能正确匹配 → 改成功 | PASS | UPDATE 1 now=proj-2 |
| E8 | 端到端 | DELETE 不受例外保护 → 仍 P0001 | PASS | P0001: asset_versions 是追加型不可变表，禁止删除（ADR-R11） |
| E9 | 端到端 | 同令牌同表但该行无映射 → P0001（例外按行，不按表） | PASS | P0001: asset_versions.project_id 为不可变内容列，禁止改写（ADR-R11） |
| E10 | 端到端 | 同一映射行：成员执行成功、非成员执行被拒（角色是唯一变量） | PASS | UPDATE 1 |
| D0 | 降级 | 执行 downgrade_sqls()（16 条） | PASS | 无异常 |
| D1 | 降级 | 13 个守卫定义逐字恢复为升级前基线 | PASS | 不一致=[] |
| D2 | 降级 | 两个函数均已删除 | PASS | allows_null=True match_null=True |
| D3 | 降级 | 映射表已删除 | PASS | is_null=True |
| Q1 | 语义等价 | 降级后 4 类操作的状态码与消息与升级前逐字一致 | PASS | pre={'AV': 'P0001', 'GD': 'P0001', 'QR': 'P0001', 'AVC': 'P0001'} post={'AV': 'P0001', 'GD': 'P0001', 'QR': 'P0001', 'AVC': 'P0001'} |
| I1 | 幂等 | 重复升级被前置断言拒绝 | PASS | P0001: 受控例外函数已存在：本迁移已执行过或谱系不符 |
| I2 | 幂等 | 二次降级可执行（无残留冲突） | PASS | 无异常 |

## 口径提醒

- 本报告证明的命题是「**从激活前基线出发，b0c9 被正确应用、且可安全降级**」，不是「激活后的目标库也验过了」。B-1 在克隆库内先清行业务行再逆序回退五层迁移，因此每条用例都跑在真正的激活前结构上。
- 激活后的 RLS 层行为（隔离项目可见性、静默 `UPDATE 0`、审计只读、盖章后失效）由 `rls_acceptance.py` 负责，**两个脚本互不替代**。
- `E1/E6/E7/E10` 断言「`UPDATE 1` 且回读值已变」：只断言「没报错」会被静默 0 行伪装成成功。
- `E5` 断言 `P0001` 而非 `42501`：若得到 42501，说明测到的是权限而不是守卫。
- 模板库 `ai_film_industrial` 已不再是干净的激活前基线（内含指向隔离项目的行），B-1 的清行量已在明细里如实记录，不假设它干净。
