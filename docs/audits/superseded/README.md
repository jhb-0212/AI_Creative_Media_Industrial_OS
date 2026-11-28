# 已作废的身份迁移清单（2026-11-27 修复前生成）

- 文件：`2026-11-27-identity-migration-inventory.pre-regeneration.json`
- 状态：**SUPERSEDED，禁止作为矩阵输入**
- 原因：
  1. 生成于 #24 降级改写之前，`8f3b2c1d4e5a` 的 downgrade 仍记录旧的 `GRANT ... ON ALL TABLES`；
  2. `b8c9d0e1f2a3`、`f2b3c4d5e6a7` 当前已无身份操作，旧清单仍计入；
  3. `f6a7b8c9d1e2` 存在动态 SQL 展开失败；
  4. 含 35 处 `*_dev_password` 明文占位值（开发默认口令，非生产密钥，但仍需擦除）。
- 处置：保留原文件作为历史证据，访问范围限于审计追溯；当前矩阵与快照一律使用重新生成的
  `docs/audits/2026-11-27-identity-migration-inventory.json`。
- 重新生成工具：`scripts/build_identity_inventory.py`（Alembic offline `as_sql` + AST 回退 + 口令擦除）。
