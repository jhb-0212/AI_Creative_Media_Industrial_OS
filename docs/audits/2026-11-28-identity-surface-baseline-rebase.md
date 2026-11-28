# 身份面基线重定（2026-11-28）：从「手工集群快照」改为「干净安装真相」

## 1. 为什么必须重定

`data/migrations/identity_surface_baseline.json` 是 `tests/integration/test_identity_surface_pg.py`
的唯一判据：活库九层身份面（角色/成员/属主/schema ACL/表 ACL/列 ACL/默认权限/函数 ACL/策略）
必须与它逐条相等。

这份基线的历史来源是**一台手工维护过的集群**（2026-11-19 取证时那 8 个 `svc_*` 角色已存在且
`LOGIN` / `INHERIT`），而迁移链从来不创建它们（GRANT 语句对不存在的角色是 `IF EXISTS` 静默
跳过）。也就是说：基线描述的不是「跑完迁移链会得到什么」，而是「那台机器上恰好有什么」。

2026-11-27 的身份声明把 8 个 `svc_*` 收敛为 **NOLOGIN + NOINHERIT**（理由见
`packages/shared/role_specs.py`：它们零授权、无人连接、无口令来源，是可登录壳身份，与已退役的
`qc_worker` 同类风险面）。声明是目标态，`bootstrap_identity.py --roles` 负责把集群拉过去。

两边一合，基线与任何**干净安装**都不可能相等：干净安装由 bootstrap 建角色，`svc_*` 必然是
NOINHERIT。这不是回归，是「基线没跟上声明」。

## 2. 实测差异（重定前）

在干净集群（`postgres:16`，仅 `postgres` 角色）上按新顺序执行：

```
scripts/bootstrap_identity.py --roles     # 40 条语句
alembic upgrade head                      # → f2b3c4d5e6a7
```

`dump_identity_surface.py --check-baseline` 报 **16 条**差异，全部落在 `roles` 一层，且全部是
同一形态：8 个 `svc_*` 的**最后一个属性**（`INHERIT`）由 `true` 变 `false`，其余七个属性
（LOGIN / SUPERUSER / CREATEDB / CREATEROLE / BYPASSRLS …）完全一致。

```
roles 仅存在于左：["svc_asset", false, false, false, false, false, true, true]
roles 仅存在于右：["svc_asset", false, false, false, false, false, true, false]
…（其余 7 个 svc_* 同形）
```

除 `roles` 外，另外八层差异 **0 条**——成员关系、属主、四类 ACL、默认权限、策略都没有变。

## 3. 做了什么

用干净安装的真实快照重写基线（`dump_identity_surface.py --dump`，不手工编辑）：

```
--dump → runs/2026-11-28/surface-clean-install.json → 覆盖 data/migrations/identity_surface_baseline.json
```

重定后 `--check-baseline` 立即 `[PASS] 与基线差异 0 条`，`test_identity_surface_pg.py` 通过。

**基线的语义从此明确写死为**：`bootstrap_identity.py --roles` + `alembic upgrade head` 的产物。
将来任何一次身份声明变更，都必须由 bootstrap 收敛后再重采基线，而不是靠手工改 JSON 对齐。

## 4. 边界与不可做的事

1. 本次重定**没有**放宽任何授权面：八层 ACL 差异为 0，`svc_*` 只是从「可登录壳」变成
   「不可登录声明」，是收紧而不是放松。
2. 不得用「重采基线」掩盖迁移造成的意外差异：差异必须能逐条解释到某次声明或某条迁移。
   本次差异只有一条形态（`INHERIT true→false`，8 行）。
3. 生产集群（`ai-film-postgres`，15432）仍是旧身份面（`svc_*` LOGIN/INHERIT）。它**不在**
   本次变更范围：生产要到下一次受控 `bootstrap --roles` 才会收敛；在那之前，
   用生产快照跑本测试会红——这是预期的，且必须红。

   > **2026-11-28 复核更正**（原文保留不改）：上句关于生产 `svc_*` 的事实判断来自
   > 重定前的**手工快照**，实测不成立。对 15432 真库跑 `verify_role_attributes.py`
   > 得 20 角色 / 0 问题（`clean: true`），`svc_*` 现为 `NOLOGIN` 且 `INHERIT=true`，
   > 与声明一致；完整身份面 `--check-baseline` 只有 22 条差异，全部属于
   > `quarantine` 归档 schema/表（D4-9 退役动作的产物），无其他漂移。
   > 证据与结论见 `docs/audits/2026-11-28-production-identity-surface-verification.md`。

## 5. 证据

- `runs/2026-11-28/wire-clean-e2e.log`：干净集群 `upgrade` 先失败（`role "app_role" is missing`）→
  bootstrap `[OK] roles：40 条语句` → `upgrade` 成功到 `f2b3c4d5e6a7`。
- `runs/2026-11-28/baseline-diff.txt`：重定前 16 条差异（roles 一层，8 行，INHERIT 位）。
- `runs/2026-11-28/surface-clean-install.json`：新快照原始导出。
- `runs/2026-11-28/baseline-diff2.txt`：重定后 `[PASS] 差异 0 条`。
- `runs/2026-11-28/retest-two.log`：`test_identity_surface_pg.py` + `test_merge_readiness.py` 24 passed。
