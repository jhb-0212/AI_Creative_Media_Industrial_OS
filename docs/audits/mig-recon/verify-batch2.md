# 归属外键第二批（f8a9b0c1d2e3）验证结果

- 执行时间：2026-11-20T17:16:32+08:00
- 目标：`localhost:15444` 克隆库 `t3b2_lab`（模板 `ai_film_industrial`），用完即删
- 基线：克隆模板后**回退到 `e3f4a5b6c7d8` 已应用**（撤掉批次三），P0 记录撤了哪几层，P1 断言批次二约束确实不在——前提由脚本声明，不靠模板偶然状态
- 结果：**16/16**

| # | 阶段 | 用例 | 结果 | 说明 |
| --- | --- | --- | --- | --- |
| P0 | 基线 | 回退到 e3f4a5b6c7d8 已应用的状态（撤掉 4 层） | PASS | i9d0e1f2a3b4/h8c9d0e1f2a3/g7b8c9d0e1f2/f8a9b0c1d2e3 |
| P1 | 前提 | 基线里批次二的三条外键尚不存在（否则测的不是本迁移） | PASS | asset_service.qc_reports=False; shot_planning.shot_specs=False; workflow.approval_steps=False |
| P2 | 前提 | 夹具组织存在（外键目标真实，否则 23503 会被误读为拦截） | PASS | organizations=1 |
| C1 | 缺口 | 建约束前：qc_reports 可指向不存在的项目 | PASS | INSERT 0 1 |
| C1ss | 缺口 | 建约束前：shot_planning.shot_specs 可指向不存在的项目 | PASS | INSERT 0 1 |
| C1as | 缺口 | 建约束前：workflow.approval_steps 可指向不存在的项目 | PASS | INSERT 0 1 |
| U1 | 升级 | 执行 upgrade_steps()（7 步） | PASS | 无异常 |
| S1 | 结构 | 三条三列外键已建且均为 NOT VALID（存量未扫） | PASS | count=3/3 convalidated=[False] |
| N1 | 拦截 | 约束建立后：三表新写入不存在的项目一律 23503 | PASS | qc_reports=23503: insert or update on table "qc_rep; shot_specs=23503: insert or update on table "shot_s; approval_steps=23503: insert or update on table "approv |
| V1 | 验证 | 有存量脏行时 VALIDATE 必须 23503（证明它会真扫数据） | PASS | 23503: insert or update on table "qc_reports" violates foreign key constraint "fk_qc_reports_project_tenant_org" |
| V2 | 验证 | 另两表的脏行同样让 VALIDATE 失败（三表都真扫） | PASS | shot_specs=23503: insert or update on table "shot_s; approval_steps=23503: insert or update on table "approv |
| V3 | 验证 | 清掉脏行后三表 VALIDATE 全部通过（成功路径可达） | PASS | qc_reports=VALIDATE OK; shot_specs=VALIDATE OK; approval_steps=VALIDATE OK |
| I1 | 幂等 | 重复 upgrade 不重复建约束 | PASS | 3→3 |
| D1 | 降级 | 执行 downgrade_steps()（3 步） | PASS | 无异常 |
| D2 | 降级 | 三条外键已删净 | PASS | asset_service.qc_reports=False; shot_planning.shot_specs=False; workflow.approval_steps=False |
| D3 | 降级 | 降级后同一脏写入恢复为可成功（语义可逆） | PASS | INSERT 0 1 |

## 口径提醒

- `C1` 与 `D3` 断言「脏写入**成功**」：这是缺口与可逆性的证据，不是缺陷复现后又消失。
- `V1/V2` 断言 `VALIDATE` **失败**：若它通过，说明 VALIDATE 没扫存量，台账上的 PASS 就是假的。
- `V3` 断言 `VALIDATE` 成功路径可达；清理脏行走 `session_replication_role=replica` 关守卫，因为守卫本就禁止 DELETE（E8 已证），这里不是被测行为。
- 三列外键只保「目标真实且组织一致」，**不保与父行的 project 一致**——后者需复合外键指向父行或触发器，不在本迁移范围。
