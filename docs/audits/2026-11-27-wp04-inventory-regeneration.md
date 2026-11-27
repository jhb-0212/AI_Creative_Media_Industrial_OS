# WP-04：身份迁移清单重建与矩阵复验（2026-11-27，D4 整改）

## 1. 处置旧清单

旧文件 `docs/audits/2026-11-27-identity-migration-inventory.json` 已移入
`docs/audits/superseded/2026-11-27-identity-migration-inventory.pre-regeneration.json`，
并附 `README.md` 说明作废原因（生成于 #24 修复前、矩阵输入失效、含开发口令占位值）。
旧文件保留为历史证据，不再作为矩阵输入。

## 2. 重建方法

工具：`scripts/build_identity_inventory.py`

- 对每条 revision 的 `upgrade` / `dowgrade` 使用 Alembic offline `as_sql=True`
  真实执行迁移函数，捕获其将发出的 SQL；
- offline 被 `op.get_bind()` 查询阻断时：
  - 该迁移自基线 `c4b9a52` 以来未修改 → 沿用旧清单同 revision/direction 的语句，
    标 `CARRIED_FORWARD`（文件未变，语句等价）；
  - 已修改 → 不沿用旧语句，标 `UNPROVEN`；
- 生成时对 `f2b3c4d5e6a7` 设置 `AUTHORIZE_RETIRED_ROLE_RESTORE=f2b3c4d5e6a7`，
  仅用于让退役恢复分支可静态展开，不连接数据库、不改库；
- 归档前擦除 `PASSWORD '<...>'` 与连接串口令。

元数据（写入 JSON）：

| 项 | 值 |
| --- | --- |
| `head_sha` | 当前 HEAD（生成时） |
| `base_commit` | `c4b9a52415258060f4310c24a245234b8b852511` |
| `generated_at` | 生成时本地时区时间戳 |
| `generator` | `scripts/build_identity_inventory.py（Alembic offline as_sql + AST 回退）` |
| `scan_scope` | 全链 `upgrade`/`downgrade` 的 `op.execute` 语句 |

## 3. 生成结果

- rows：824
- 含身份语句的 revision：41
- `expansion_failures`：12 条，集中在依赖 `op.get_bind()` 的 revision
- 标 `UNPROVEN` 的 revision：`6a7b8c9d0e1f`、`f6a7b8c9d1e2`、`b7c8d9e0f1a2`、`b3c4d5e6f7a8`、`c7d8e9f0a1b2`、`c1a2b3d4e5f6`、`a13b0c1d2e3f`、`b1c2d3e4f5a6`、`c3d4e5f6a7b8`、`e7f8a9b0c1d2`
- `CARRIED_FORWARD`：6 个 direction（未修改迁移、offline 被阻断）
- 脱敏：`_dev_password` 明文 0 处；`PASSWORD '***REDACTED***'` 23 处

未修改的迁移不伪装成“重新抽取”：其语句来自旧清单 carry-forward，原因是 offline
展开被 `op.get_bind()` 阻断；文件自基线以来未修改，语句等价，已在 JSON 的
`notes.carried_forward` 说明。

## 4. 矩阵复验：FAIL，新阻断项

```
$ python scripts/reversibility_matrix.py --check
[MATRIX] revisions=41 asymmetric=25 classified={'no_restore_proven': 27, ...} unclassified=1
[MATRIX][FAIL] 未分类不对称 revision: 9e3b4c5d6e7f
exit=1
```

失败原因（`9e3b4c5d6e7f_create_app_role_and_grants.py`）：

- `upgrade` 在 `project_planning` / `outbox` 上执行
  `ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES`
  与 `GRANT USAGE, SELECT ON SEQUENCES`；
- `downgrade` 只撤销 `ALL TABLES` / `ALL SEQUENCES` 与 schema `USAGE`，
  **没有撤销 `ALTER DEFAULT PRIVILEGES`**；
- 旧算法把“降级 `DROP ROLE`”视为覆盖全部授权，因此该不对称此前被掩盖；
  本轮迁移不再创建/删除 `app_role` 后，默认权限残留成为真实不对称。

快照覆盖：`9e3b4c5d6e7f` **不在** 28 条双向快照中，因此没有 P/D 证据可证明
`no_restore_proven`。这属于“覆盖缺口 + 真实不对称”，不是口径问题。

## 5. 结论与边界

- 旧清单已作废并保留；新清单已脱敏、带元数据、覆盖状态显式；
- 矩阵在**当前源码 + 新清单**下不再是 28/28，而是 25 asymmetric + 1 unclassified；
- 未修改 migration 代码、未修改分类、未修改排除规则、未修改 28 条快照与 baseline；
- 因此 D4 仍不通过，WP-04 重新出现技术阻断，需 Owner 决策：
  1. 修复 `9e3b4c5d6e7f` 的 downgrade（补 `ALTER DEFAULT PRIVILEGES … REVOKE`），随后做 P/D 真实往返并纳入快照；
  2. 或由 Owner 以安全契约证明“默认权限残留可接受”并显式分类（需证据，不得只改分类）。
