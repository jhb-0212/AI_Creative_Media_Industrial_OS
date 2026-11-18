# Phase 0 契约冻结包清单

> 本文件由 `scripts/freeze_manifest.py --write` 生成，**禁止手改**。
> 手改会被 `--check` 的清单自指哈希校验当场判定为篡改。

基线文档：`docs/baseline/AI影视工业化生产平台V2.0.5.md`（489607 字节）
文档版本：`V205-R14`（版本号按项目约束冻结，不随修订升号）
冻结时刻仓库快照：`dfc5bd8`
基线 SHA-256：`9a9940c5523c2c9626a410fa82b0ccbc195d6fe1bf3e985395e35706101ee7f7`

本清单把 31.11 声明的十一类契约文件与磁盘实际文件逐项对齐：每份文件给出类目、
字节数、SHA-256 与对应正文章节，可直接复核。第八类即本清单自身，因自指不登记
自身哈希，其哈希见同目录 `phase0_manifest.md.sha256`，由生成器在写入后计算。

## 文件清单

| 正文类目 | 文件 | 字节 | SHA-256 | 对应章节 |
|---|---|---|---|---|
| ① 对象 Schema | `contracts/schemas/object_schemas.schema.json` | 227319 | `2511e28d1f293fd220df813cf39cd86080e1ce4969479a7edc5a9ee379aa60ed` | 8.x 对象契约 / C.3 数据契约 |
| ② OpenAPI 草案 | `contracts/openapi/openapi.draft.yaml` | 113540 | `90aaff4ed065afec075d272c0f62d4ae02713f46822757c7bf0de556dc5b5c72` | 31.6（由 Action Registry 派生） |
| ③ 状态机 | `contracts/state_machines/state_machines.yaml` | 25697 | `cadd98eca7b9719a9269872857497fec6d4bedb5f648f77df3e6e29842ca259f` | 31.5 / 8.6 / 32.6 / 33.3 / 34.7 |
| ④ Action Registry | `contracts/action_registry/action_registry.yaml` | 43424 | `07629e3e699203c7c62b1c73e8616a9352861b34d279d90c676297af8d840e8a` | 31.6 唯一注册表 |
| ⑤ 权限矩阵 | `contracts/permission/permission_matrix.yaml` | 4212 | `c45a96cbf790568527b6a42a06fc4e20d5228f166b2d8e98d3abce7dd5709f6c` | C.5 / 31.6 |
| ⑥ 预算与成本口径 | `contracts/budget/budget_caliber.yaml` | 2511 | `29e1d7d470f115ff41b07daaebfb198ccea9246316a2dd4b43a28b52596a0aef` | C.2 / 34.6 |
| ⑦ 指标字典 | `contracts/metrics/metric_dictionary.yaml` | 7567 | `fb909bccf1da14c74efaad77d77cf73d86c34126b41052def1d0185e4ea4f8cb` | 31.10 / 34.13 |
| ⑧ 冻结包清单 | `contracts/phase0/phase0_manifest.md` | — | 自指不登记，见 `phase0_manifest.md.sha256` | 31.11 契约证据清单 |
| ⑨ 枚举注册表 | `contracts/enums/enum_registry.yaml` | 15356 | `7833d4cdf9d0ada7dace78650f38a30d5416abd1464d40936163578e4ea5e9cc` | 31.5 / C.7 |
| ⑩ ADR 主索引 | `docs/adr/adr_index.yaml` | 33011 | `24cca2103e93296eae56d8118b15d0eeb5eb6a52a9f2ec44ff50d1f49d7c27ff` | 31.12 / 32.13 / 33.15 |
| ⑪ 阶段表 | `contracts/phases/phase_map.yaml` | 4158 | `77e88f57afd82eaa5caf9c4f73f485c33bfe8b125ca94529c05eb90c1788eeb3` | 31.2 唯一阶段总表 |

## 已批准修订

- `V205-R14-A1`
- `V205-R14-A2`
- `V205-R14-A3`
- `V205-R14-A4`
- `V205-R14-A5`

契约名称全集必须精确等于「基线清单 ∪ 已批准修订」，由
`scripts/freeze_manifest.py --check` 逐名称判定（不是只看数量）。

## 校验命令

```bash
python scripts/freeze_manifest.py --check   # 基线未变 + 派生未私改 + 演进有据
python scripts/contract_check.py            # 派生之间的交叉一致性
```

## 写入口径

1. 只改基线文档 → 必须新修订升版，并重新派生全部产物后 `--write`；
2. 只加业务契约（Action / Schema / 状态机）→ 先在 `contract_amendments.yaml`
   登记理由与审批人，再改派生文件，最后 `--write`；
3. 顺序反了（先改派生文件）→ `--check` 报「未登记新增」，不得用 `--write` 掩盖；
4. 旧机制清理：不得保留任何手写的数量副本（`total_actions` 等必须与条目数一致）。
