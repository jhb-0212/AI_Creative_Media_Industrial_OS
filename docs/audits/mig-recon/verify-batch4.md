# 归属「一致」外键（h8c9d0e1f2a3）验证结果

- 执行时间：2026-11-20T16:17:25+08:00
- 目标：`localhost:15444` 克隆库 `t3b4_lab`（模板 `ai_film_industrial`），用完即删
- 基线：克隆后回退到 `g7b8c9d0e1f2` 已应用（撤掉批次四），P0 记录撤了哪几层
- 对照：两个真实项目从库中现取且同租户同组织——**两者都真实**才是「一致」命题的前提
- 结果：**21/21**

| # | 阶段 | 用例 | 结果 | 说明 |
| --- | --- | --- | --- | --- |
| P0 | 基线 | 回退到 g7b8c9d0e1f2 已应用（撤掉 0 层） | PASS | 模板已在该状态 |
| P1 | 前提 | 基线里批次四的约束均不存在（否则测的不是本迁移） | PASS | 命中 0 个 |
| P2 | 前提 | 取到同组织下两个真实项目 | PASS | A=proj-alpha B=proj-beta org=0011 |
| P3 | 前提 | 插入一行属于 A 的资产版本 | PASS | INSERT 0 1 |
| G1 | 对照 | 项目一致时可正常插入（约束未建前） | PASS | INSERT 0 1 |
| C1 | 缺口 | 建约束前：报告挂在 A 的资产上、项目写成 B，依然插入成功 | PASS | INSERT 0 1 |
| U1 | 升级 | 执行 upgrade_steps()（18 步） | PASS | 无异常 |
| S1 | 结构 | 复合外键已建且为 NOT VALID（存量未扫） | PASS | exists=True convalidated=False |
| S2 | 结构 | 被取代的单列外键已删除（不是两条键并存） | PASS | count=0 |
| S3 | 结构 | 父表四列唯一键已建（复合外键的可引用目标） | PASS | count=1 |
| N1 | 拦截 | 约束建立后：跨项目挂载一律 23503 | PASS | 23503: insert or update on table "qc_reports" violates foreign key constraint "fk_qc_reports_asset_version_project" |
| N2 | 拦截 | 同一约束不误伤项目一致的写入（否则 N1 是别的原因） | PASS | INSERT 0 1 |
| V0 | 验证 | 存量中存在「真实但不一致」的行（VALIDATE 的必然后果） | PASS | 违规行 2 条 |
| V1 | 验证 | 有「真实但不一致」存量行时 VALIDATE 必须 23503 | PASS | 23503: insert or update on table "qc_reports" violates foreign key constraint "fk_qc_reports_asset_version_project" |
| V3 | 验证 | 清掉脏行后 VALIDATE 通过（成功路径可达） | PASS | VALIDATE OK |
| I1 | 幂等 | 重复 ADD CONSTRAINT 必须失败（42P07/42710） | PASS | 42P07 |
| Q1 | 闸门 | 升级后一致闸门无 NO_FK / FK_NOT_COMPOSITE（DANGLING 不计） | PASS | NO_FK=0 FK_NOT_COMPOSITE=0 PENDING=9 DANGLING=9 |
| D1 | 降级 | 执行 downgrade_steps()（20 步） | PASS | 无异常 |
| D2 | 降级 | 批次四的复合外键与父表唯一键已删净 | PASS | 残留=0 |
| D3 | 降级 | 被取代的单列外键已恢复（回退到 g7b8 时刻等价） | PASS | count=1 |
| D4 | 降级 | 降级后同一跨项目写入恢复成功（语义可逆） | PASS | INSERT 0 1 |

## 口径提醒

- `C1` 断言跨项目写入**成功**：这是缺口的正面复现，不是缺陷复现脚本本身有 bug。
  若 C1 失败，说明三列外键先拦住了，用例实为「项目不存在」，证明不了「一致」缺失。
- `N2` 与 `N1` 必须成对：只有 N1 时，「全都写不进去」也能让 N1 通过。
- `S2` 断言弱单列外键**已消失**：两条键并存时会掩盖「弱键保不住一致」这件事。
- `V1` 断言 VALIDATE **失败**：它若通过，说明没扫存量，台账 PASS 是假的。
- `I1` 断言重复执行**失败**：迁移不假装幂等——`ADD CONSTRAINT` 本就没有
  `IF NOT EXISTS`，把它包装成静默成功会掩盖「约束唯一存在」这个事实。
- `Q1` 把本迁移与闸门对齐：迁移建完的边，闸门必须认；不认就是两者规则漂移。
- `DANGLING` 不计入 Q1：`shots` / `scenes` / `gate_instances` 表尚未创建，
  属镜头域运行时未落地，无法由外键修复，已在一致闸门的报告里单列。
