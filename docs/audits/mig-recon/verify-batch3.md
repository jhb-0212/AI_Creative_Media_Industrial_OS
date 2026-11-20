# 归属外键第三批（g7b8c9d0e1f2 / episodes）验证结果

- 执行时间：2026-11-20T17:16:37+08:00
- 目标：`localhost:15444` 克隆库 `t3b3_lab`（模板 `ai_film_industrial`），用完即删
- 基线：克隆后回退到 `f8a9b0c1d2e3` 已应用（撤掉批次三），P0 记录撤了哪几层
- 对照行：真实项目三元组从库中现取，不硬编码（夹具失效会让 23503 语义漂移）
- 结果：**15/15**

| # | 阶段 | 用例 | 结果 | 说明 |
| --- | --- | --- | --- | --- |
| P0 | 基线 | 回退到 f8a9b0c1d2e3 已应用的状态（撤掉 3 层） | PASS | i9d0e1f2a3b4/h8c9d0e1f2a3/g7b8c9d0e1f2 |
| P1 | 前提 | 基线里 episodes 归属外键尚不存在（否则测的不是本迁移） | PASS | exists=False |
| P2 | 前提 | 库里取到一行 ACTIVE 项目作为对照 | PASS | proj-alpha / 0001 / 0011 |
| G1 | 对照 | 真实项目 + 同租户组织可正常插入（约束未建立前） | PASS | INSERT 0 1 |
| C1 | 缺口 | 建约束前：episodes 可指向不存在的项目 | PASS | INSERT 0 1 |
| U1 | 升级 | 执行 upgrade_steps()（3 步） | PASS | 无异常 |
| S1 | 结构 | 三列外键已建且为 NOT VALID（存量未扫） | PASS | exists=True convalidated=False |
| N1 | 拦截 | 约束建立后：指向不存在的项目一律 23503 | PASS | 23503: insert or update on table "episodes" violates foreign key constraint "fk_episodes_project_tenant_org" |
| N2 | 拦截 | 同一约束不误伤真实项目（否则 N1 的 23503 是别的原因） | PASS | INSERT 0 1 |
| V1 | 验证 | 有存量脏行时 VALIDATE 必须 23503（证明它会真扫数据） | PASS | 23503: insert or update on table "episodes" violates foreign key constraint "fk_episodes_project_tenant_org" |
| V3 | 验证 | 清掉脏行后 VALIDATE 通过（成功路径可达） | PASS | VALIDATE OK |
| I1 | 幂等 | 重复 upgrade 不重复建约束 | PASS | count=1 |
| D1 | 降级 | 执行 downgrade_steps()（1 步） | PASS | 无异常 |
| D2 | 降级 | 外键已删净 | PASS | exists=False |
| D3 | 降级 | 降级后同一脏写入恢复为可成功（语义可逆） | PASS | INSERT 0 1 |

## 口径提醒

- `G1`/`C1` 断言写入**成功**，`N1` 断言 `23503`，`N2` 断言真实项目仍可写。
  只报 N1 不报 N2，「拦住了」也可能是「什么都写不进去」。
- `V1` 断言 `VALIDATE` **失败**：若它通过，说明 VALIDATE 没扫存量，台账 PASS 是假的。
- `D3` 断言脏写入恢复成功：证明 D2 删的确实是这条约束，而不是删了个别的。
- 三列外键只保「目标真实 + 租户组织一致」。对 `episodes` 而言父行就是 `projects`，
  故此处「一致」与「存在」等价；对 `qc_reports` 那类父行不同的表仍不等价（I-1 未闭合半边）。
