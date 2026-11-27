# WP-03：`qc_worker` 退役与恢复语义审查（只读）

- 日期：2026-11-27
- 对象：`data/migrations/versions/f2b3c4d5e6a7_qc_worker_identity_retirement.py`（第 64 位，当前 head）
- 授权：只读调查与设计，**未改代码**

## 1. 事实

| 项 | 事实 |
| --- | --- |
| upgrade | 用一条 DO 块完成"数依赖 → 有依赖就带条数 `RAISE` → 无依赖才 `DROP ROLE`"，与当前 head 一致：`qc_worker` 在集群内有依赖时迁移显式失败，不静默跳过 |
| downgrade | `CREATE ROLE qc_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '<settings.postgres_qc_worker_password>'`（不存在才建，存在则 `ALTER ROLE` 对齐），再 `REVOKE operator FROM qc_worker` |
| 口令来源 | 运行时 `get_settings()`，不是角色声明的常量 |
| 仓库连接点 | 无：`workers/qc` 用的是 `qc_governance_role` + `asset_service_role` 池；没有 DSN、没有入口、没有池工厂 |
| 集群现状 | `qc_worker` 已不存在；也不在 WP-01 的 20 个声明角色内 |

## 2. 语义拆分：这不是"删了不该删的角色"，而是"退役身份能否被降级复活"

| 问题 | 判断 |
| --- | --- |
| upgrade 删 `qc_worker` 对不对？ | **对**。它是空壳登录身份、零依赖、零连接点；补授权只会造出无人使用却可登录的权限面。依赖检查 + 显式失败也是对的 |
| downgrade 重建它，时间胶囊语义上对不对？ | **语义上成立**：`f2b3c4d5e6a7` 之前的真实状态就有一个可登录空壳，回退要回到那个状态 |
| 那问题在哪？ | 重建出来的身份有**三个不受登记约束的东西**：①登录口令来自运行时 settings（第二事实源）；②它不在 WP-01 声明集里，`identity_registry --check` 会把它当成"库里有、声明里没有"的未声明角色；③"已退役"这件事没有任何机器可读记录，下一轮谁看到 `qc_worker` 出现在集群里都可能以为它该在 |

一句话：**时间胶囊是对的，"退役"这件事却只活在提交信息和文档里。**

## 3. 三个可选处置（待裁决）

### A. 登记"退役身份"维度（推荐）

- 在 `role_specs.py` 增加 `RETIRED_ROLES`（独立于 20 个 live 声明），每条记录 `name / retired_in / retired_reason / recreation_policy`：
  ```
  qc_worker: retired_in="f2b3c4d5e6a7", recreation_policy="downgrade_only"
  ```
- `identity_registry --check` 按此区分：`qc_worker` 存在 → 只在 `alembic_version < f2b3c4d5e6a7` 时算合法，否则报"已退役身份复活"。
- 迁移代码不改；退役事实从"人类记忆"变成"机器判据"。

代价：声明集从 20 变 20 + N（retired 单独一节），需要同步 `--dump` 生成物结构。

### B. downgrade 失败关闭（不推荐单独使用）

- downgrade 不重建，直接 `RAISE EXCEPTION '降级需要恢复已退役身份，请运维执行 scripts/retire_legacy_roles.py --restore qc_worker'`。
- 好处：不再产生可登录的幽灵身份。
- 坏处：**主动破坏可逆性**。head 的 downgrade 变成"必须人工介入"，与 WP-04 的"状态语义可逆"目标直接冲突。

### C. 重建但降权（折中）

- downgrade 建 `NOLOGIN` 空壳（无口令），让"身份存在"成立、登录面不恢复。
- 坏处：与 `f2b3c4d5e6a7` 之前的状态**不等价**（原来是 LOGIN + 口令），会污染 WP-04 的往返比对基线。

**建议 A**，并在 A 之上加一条降级告警：downgrade 成功后把"已重建退役身份 `qc_worker`（LOGIN）"写进迁移日志，让操作者看得见自己刚刚恢复了一个退役凭证面。

## 4. 若采纳 A，验收判据

| 判据 | 做法 |
| --- | --- |
| 退役事实可机读 | `RETIRED_ROLES` 含 `qc_worker`，`retired_in = f2b3c4d5e6a7` |
| 未声明角色不再"静默合法" | `--check` 在 `alembic_version = head` 时对 `qc_worker` 报错；在 `< f2b3c4d5e6a7` 时不报 |
| 重建行为可观测 | downgrade 输出一行 WARNING/INFO，明确角色名与登录属性 |
| 不回退到"给它补授权" | 仍保留"零连接点"证据：`workers/qc` 池装配不含它 |
| 口令来源单一 | 若保留重建，口令只从 settings 取；不允许在迁移里写常量或默认值 |

## 5. 本轮结论

- 不把 `qc_worker` 混进 WP-02 的三处整改：两者病根不同（一个是"迁移越权拥有 bootstrap 角色"，一个是"退役身份的恢复语义"）。
- 本轮**未改** `f2b3c4d5e6a7`：处置方案需先裁决；在 A 落地前，任何"为了闸门变绿"的改动都会把可逆性换成另一种失真。
