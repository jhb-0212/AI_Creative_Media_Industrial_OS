# 归属栈角色授权完整性验证（名单式授权删除后）

- 执行时间：2026-11-21T09:36:59+08:00
- - 目标：`localhost:15444` 克隆库 `gx_lab`（模板 `ai_film_industrial`），用完即删
- - 迁移源：`data\migrations\pending\feat-ownership`
- - 受守卫表：13 张（清单取自 `b0c9.GUARD_ORDER`，不手写）
- 结果：**11/11**

| # | 阶段 | 用例 | 结果 | 说明 |
| --- | --- | --- | --- | --- |
| G0 | 基线 | 回退归属栈至激活前基线（10 层） | PASS | projects 3→0，asset_versions 3→0；归属三列外键=0; 两列组织外键=0; 例外映射表=0; 例外判定函数=0; RLS 表达式备份表=0; 批次二归属外键=0; 批次三归属外键=0; 批次五新表=0; 批次五收敛边=0; 批次六归档守卫=0; 批次四父表唯一键=0; 批次四复合外键=0 |
| G1 | 中间态 | 两个判定函数已创建（b0c9 之后） | PASS | allows=41738 map=41737 |
| G2 | 中间态 | 此刻的写者均已持有函数 EXECUTE | PASS | 无漂移 |
| G3 | 终态 | 归属栈全部应用（10 层，末层 j0e1f2a3b4c5） | PASS | 10 层 |
| G4 | 终态 | 不变量：对受守卫表有写权限者必持两函数 EXECUTE（37 个 角色×表） | PASS | 零漂移 |
| G4b | 终态 | 写者矩阵非空 | PASS | 7 个角色：afs_data_repair, postgres, svc_asset, svc_generation_execution, svc_qc_governance, svc_shot_planning… |
| G5 | 终态 | `afs_data_repair` 对两函数均有 EXECUTE（11 张表） | PASS | 全部具备（历史坑：曾只授 allows 不授 map_match） |
| G6 | 终态 | PUBLIC 无两函数 EXECUTE（撤 PUBLIC 生效） | PASS | allows=False map_match=False |
| G7 | 非恒真 | 只授表权限的新角色**不会**自动获得函数 EXECUTE | PASS | allows=False map_match=False → E6 的绿不是 PG 默认行为 |
| G8 | 非恒真 | 执行 E6 同款语句后该角色获得两函数 EXECUTE | PASS | allows=True map_match=True |
| G10 | 降级 | 归属栈逆序降级后判定函数已移除（无悬空 ACL） | PASS | allows_oid=None |

## 待裁决缺口（N/A 背后的真实问题，不得并入下一轮「已修」）

| # | 事项 | 缺口 |
| --- | --- | --- |
| G9 | E6 是升级时的一次性收敛，不是持续授权 | 升级后新建的写角色需重跑收敛语句才能获得 EXECUTE，否则其写入得到 42501。本脚本 G7 已实证该缺口存在；G8 证明修复语句有效。这属「授权随权限变化」的固有代价，名单式授权无法消除（它只是把缺口换成了静默跳过）。 当前库内无累积漂移。 |

## 口径提醒

- 本报告证明的命题是「**归属栈重放后，授权完整且无漂移**」，不是「目标库当前的 ACL 也验过了」；后者应以目标库实查为准。
- G4 的主断言是不变量而非清单：**有表写权限 ⇒ 有函数 EXECUTE**。违反它的后果是保护语义漂移（`P0001` 变 `42501`），不是少一个 GRANT。
- G7/G8 是非恒真证明：若 G4 全绿只是 PG 默认行为，G7 会失败。两条必须一起看。
- G9 不是失败项，是**已被实证的边界**：E6 一次性收敛，新角色需重跑。
- 角色名一律从 `pg_roles` / `information_schema` 实读，脚本内不出现默认角色名字面量（`app_role` 那段被删正是因为这一点，判据见 `tests/unit/test_db_role_names.py`）。
