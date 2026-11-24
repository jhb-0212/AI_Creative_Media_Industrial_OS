# Phase 0 契约冻结包清单

> 本文件由 `scripts/freeze_manifest.py --write` 生成，**禁止手改**。
> 手改会被 `--check` 的清单自指哈希校验当场判定为篡改。

基线文档：`docs/baseline/AI影视工业化生产平台V2.0.5.md`（489580 字节）
文档版本：`V205-R15`（版本号按项目约束冻结，不随修订升号）
冻结时刻仓库快照：`dfc5bd8`
基线 SHA-256：`b922a1e1c6e5c43b0c7e6ee30b5f036b7919fcc946935594f225b90bf441945e`

本清单把 31.11 声明的十一类契约文件与磁盘实际文件逐项对齐：每份文件给出类目、
字节数、SHA-256 与对应正文章节，可直接复核。第八类即本清单自身，因自指不登记
自身哈希，其哈希见同目录 `phase0_manifest.md.sha256`，由生成器在写入后计算。

## 文件清单

| 正文类目 | 文件 | 字节 | SHA-256 | 对应章节 |
|---|---|---|---|---|
| ① 对象 Schema | `contracts/schemas/object_schemas.schema.json` | 318439 | `8dd9923114ae0faae929d38e9c8d5043028865e80cd6926811da9413af7b5319` | 8.x 对象契约 / C.3 数据契约 |
| ② OpenAPI 草案 | `contracts/openapi/openapi.draft.yaml` | 139315 | `419568058560926fa9a07be0aba6cd7e2e3cbf9daa17ec8677bac4867f82a43a` | 31.6（由 Action Registry 派生） |
| ③ 状态机 | `contracts/state_machines/state_machines.yaml` | 68503 | `947372930811f91071475655b09f306920f52e0f5e4758d32c902482de29d1e6` | 31.5 / 8.6 / 32.6 / 33.3 / 34.7 |
| ④ Action Registry | `contracts/action_registry/action_registry.yaml` | 55959 | `6ccfa5f7c85b0c894f439f6547ead5ec50445ab8263ea78e51623fdd23767ffc` | 31.6 唯一注册表 |
| ⑤ 权限矩阵 | `contracts/permission/permission_matrix.yaml` | 4213 | `ea343622c08f233d11c91420c914f46fa5f8fd984c9b23140cb29821b447ce6b` | C.5 / 31.6 |
| ⑥ 预算与成本口径 | `contracts/budget/budget_caliber.yaml` | 2511 | `29e1d7d470f115ff41b07daaebfb198ccea9246316a2dd4b43a28b52596a0aef` | C.2 / 34.6 |
| ⑦ 指标字典 | `contracts/metrics/metric_dictionary.yaml` | 7567 | `fb909bccf1da14c74efaad77d77cf73d86c34126b41052def1d0185e4ea4f8cb` | 31.10 / 34.13 |
| ⑧ 冻结包清单 | `contracts/phase0/phase0_manifest.md` | — | 自指不登记，见 `phase0_manifest.md.sha256` | 31.11 契约证据清单 |
| ⑨ 枚举注册表 | `contracts/enums/enum_registry.yaml` | 6548 | `8dd371aefffd05b36d30d5b945ba9a6c488b51b6e975590ff295500dcc4a8f05` | 31.5 / C.7 |
| ⑩ ADR 主索引 | `docs/adr/adr_index.yaml` | 33011 | `24cca2103e93296eae56d8118b15d0eeb5eb6a52a9f2ec44ff50d1f49d7c27ff` | 31.12 / 32.13 / 33.15 |
| ⑪ 阶段表 | `contracts/phases/phase_map.yaml` | 4221 | `ee03d60c4008aff193cc19c06f96fbe96f9976d9bb7c4a1b6af7f80c6199528e` | 31.2 唯一阶段总表 |

## 已批准修订

- `V205-R14-A1`
- `V205-R14-A2`
- `V205-R14-A3`
- `V205-R14-A4`
- `V205-R14-A5`
- `V205-R14-A6`
- `V205-R14-A7`
- `V205-R14-A8`
- `V205-R14-A9`
- `V205-R14-A10`
- `V205-R14-A11`
- `V205-R14-A12`
- `V205-R14-A13`
- `V205-R14-A14`
- `V205-R14-A15`
- `V205-R14-A16`
- `V205-R14-A17`
- `V205-R14-A18`
- `V205-R14-A19`
- `V205-R14-A20`
- `V205-R14-A21`
- `V205-R14-A22`
- `V205-R14-A23`
- `V205-R15-A24`
- `V205-R15-A25`
- `V205-R15-A26`
- `V205-R15-A27`
- `V205-R15-A28`
- `V205-R15-A29`
- `V205-R15-A30`
- `V205-R15-A31`
- `V205-R15-A32`
- `V205-R15-A33`
- `V205-R15-A34`
- `V205-R15-A35`
- `V205-R42-T2-FINGERPRINT`
- `V205-R43-BOUNDARY-HARDENING`
- `V205-R35-GB-FIELD-VALUE-SETS`
- `V205-R35-GA-FIRST-FOUR`
- `V205-R35-GA-SECOND-FOUR`
- `V205-R35-GA-THIRD-TEN`
- `V205-R35-GC`
- `V205-R45`

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
