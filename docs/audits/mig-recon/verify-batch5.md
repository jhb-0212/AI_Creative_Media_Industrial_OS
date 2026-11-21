# 批次五（i9d0e1f2a3b4）建表与接边 — 行为验证

- 执行时间：2026-11-21T12:10:37+08:00
- 目标：`localhost:15444` 克隆库 `t5_lab`（模板 `ai_film_industrial`），用完即删
- 基线：克隆后回退到 `h8c9d0e1f2a3` 已应用（撤掉批次五）
- 迁移：`data/migrations/pending/feat-ownership/i9d0e1f2a3b4_scene_shot_tables.py`
- 结果：**26/26**

| # | 阶段 | 用例 | 结果 | 说明 |
| --- | --- | --- | --- | --- |
| P0a | 基线 | 清空业务行（避免上一轮遗留的正式修订挡住回退） | PASS | projects 3→0，asset_versions 3→0 |
| P0 | 基线 | 回退到 h8c9d0e1f2a3 已应用（撤掉 5 层） | PASS | m3b4c5d6e7f8/l2a3b4c5d6e7/k1f2a3b4c5d6/j0e1f2a3b4c5/i9d0e1f2a3b4 |
| P1 | 前提 | 基线里 shots / scenes 均不存在（否则测的不是本迁移） | PASS | 命中 0 张 |
| P2 | 前提 | 两个同组织项目 + 各自一集 | PASS | b5-pa / b5-pb |
| G1 | 缺口 | 建表前：shot_id 写一个不存在的镜头，插入成功（裸 varchar 无人管） | PASS | INSERT 0 1 |
| U1 | 升级 | 执行 upgrade_steps()（35 步） | PASS | 无异常 |
| S1 | 结构 | 9 条收敛边全部建立（缺一条即建了表白建） | PASS | 缺失=无 |
| S2 | 结构 | 接边的外键为 NOT VALID（存量未扫，新写入即生效） | PASS | NOT VALID=6 条（新表自建的三条天然 valid） |
| S3 | 结构 | shots 备 (project_id, shot_id) 唯一键（复合边的目标键） | PASS | count=1 |
| N1 | 拦截 | 接边后：指向不存在的镜头一律 23503 | PASS | 23503: insert or update on table "shot_visual_plans" violates foreign key constraint "fk_shot_visual_plans_shot_proje |
| V1 | 验证 | 存量 ghost 行让 VALIDATE 必须 23503（不是走过场） | PASS | 23503: insert or update on table "shot_visual_plans" violates foreign key constraint "fk_shot_visual_plans_shot_proje |
| V2a | 验证 | 清掉本批夹具与任何模板遗留合成行，清完无残留 | PASS | 退场 gt-mismatch 0 行，残留夹具 0 行 |
| V2 | 验证 | 清掉 ghost 行后全部 VALIDATE 通过（成功路径可达） | PASS | 失败=无 |
| C0 | 约束 | 同事务创建 Shot 与其首个 ShotRevision（互指，靠 DEFERRABLE） | PASS | COMMIT 成功 |
| C1 | 约束 | 强制立即检查时，指向缺失修订的 Shot 必须 23503（证明约束真在） | PASS | 23503 |
| C2 | 约束 | 镜头挂到**另一个真实项目**的场次：23503（一致性，不是存在性） | PASS | 23503: insert or update on table "shots" violates foreign key constraint "fk_shots_scene_project" |
| C3 | 约束 | 未知 status（IN_REVIEW 刻意不在五态内）必须 23514 | PASS | 23514: new row for relation "shots" violates check constraint "ck_shots_status" |
| C4 | 约束 | BLOCKED 无 blocked_reason 必须 23514 | PASS | 23514: new row for relation "shots" violates check constraint "ck_shots_blocked_reason" |
| C5 | 约束 | 非 BLOCKED 携带 blocked_reason **也**必须 23514（双向，不是单向） | PASS | 23514: new row for relation "shots" violates check constraint "ck_shots_blocked_reason" |
| C6 | 约束 | 同项目重复 shot_code 必须 23505（ADR-R19 的纸面约束落地） | PASS | 23505: duplicate key value violates unique constraint "uq_shots_project_code" |
| C7 | 约束 | entity_id ≠ shot_id 必须 23514（信封主键与业务身份合一） | PASS | 23514: new row for relation "shots" violates check constraint "ck_shots_single_identity" |
| R1 | RLS | 隔离项目的镜头对业务角色不可见（与既有归属表同形） | PASS | 业务可见 1 / 总行 2（应少 1） |
| Q1 | 闸门 | 一致闸门里 shots / scenes 相关边不再报 DANGLING | PASS | PASS=20 NO_FK=0 FK_NOT_COMPOSITE=0 PENDING=2 DANGLING=1（DANGLING 应只剩 gate_instances 相关） |
| D1 | 降级 | 执行 downgrade_steps()（19 步） | PASS | 无异常 |
| D2 | 降级 | 新表、新列、新边全部消失（对称，不留悬空引用） | PASS | 表=0 列=0 边=0 |
| D3 | 降级 | 降级后同一跨项目/幽灵写入恢复成功（约束确实已撤） | PASS | INSERT 0 1 |

## 口径提醒

- `G1` 断言幽灵镜头写入**成功**：这是缺口的正面复现。它若失败，说明别处的约束先拦住了，
  用例证明不了「`shot_id` 是裸字符串」这件事。
- `N1` 与 `G1` 必须成对：只有 N1 时，「全都不让写」也能让它通过。
- `V1` 断言 `VALIDATE` **失败**：成功即说明没扫存量，台账的 PASS 是假的。
- `C1` 是 `C0` 的反证：`DEFERRABLE` 只是把检查推迟到提交，不是取消检查。
  强制立即检查而目标缺失仍能提交，说明约束根本不存在。
- `C2` 的两个项目都真实且同组织，故 `23503` 只能来自「一致性」，
  排除了「项目/场次不存在」这个更平庸的解释。
- `C5` 与 `C4` 成对：只测单向时，「非 BLOCKED 也能带原因」不会被发现，
  于是「为什么卡过」与「现在卡不卡」变成两个可以互相矛盾的字段。
- `R1` 比较的是**业务角色实读行数**，不是策略是否存在：RLS 的失败方式是静默返回 0 行，
  看 `pg_policies` 有记录完全不能证明隔离生效。
- `D3` 断言降级后写入**恢复成功**：只检查约束消失不足以证明撤掉的是这件事。
