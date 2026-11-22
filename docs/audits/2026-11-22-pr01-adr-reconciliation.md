# PR-01 ADR 与架构事实源对账（只读核验）

> 依据：《开发执行总纲与 PR 验收规范 V1.0》第四章 PR-01
> 性质：只读对账，不裁决、不改 ADR 状态。结论中需 Owner 裁决项明确标记 BLOCKED。

## 1. ADR 现状（实测）

| 项 | 值 | 证据 |
| --- | --- | --- |
| 索引登记总数 | 118 条（ADR-001 … ADR-118） | `docs/adr/adr_index.yaml` `total_registered: 118` |
| 物理文件 | 31 个 .md | `docs/adr/` 实测 |
| 去重后的 R 编号 | 24 个（R06–R41，含 R40） | 文件名去重 |
| 编号缺口 | `number_gaps: []`（索引自述无缺口） | `docs/adr/adr_index.yaml` |
| 状态字段 | **无**（索引无 ACCEPTED/PROPOSED/SUPERSEDED 状态矩阵） | 索引全文 |
| 决策字段 | 大量 `decision: ''`（118 条中仅少数带正文决策摘要） | 索引 entries |

## 2. 同号重复与版本漂移

| 现象 | 明细 | 风险 |
| --- | --- | --- |
| 正文编号重复 | 索引 `duplicate_ids` 登记 ADR-101、ADR-001、ADR-021 等（`same_wording: true`） | 同号异文件，需权威位声明 |
| 文件多版本并存 | `ADR-R40-unified-authorization-model.md` + `.v1.4.3` / `.v1.4.4` / `.v1.4.5` / `.v1.4.6` / `.v1.4.7`（6 个文件） | 存在陈旧版本被误读为现行版本的风险 |
| 文件多版本并存 | `ADR-R27-scene-shot-runtime-entry.md` + `.PROPOSED.md` | 提议态与现行态并存 |
| 拆分编号 | `ADR-R06-outbox-relay-tenancy.md` + `ADR-R06-2-outbox-retention-partitioning.md` | 编号后缀约定需固化 |

## 3. 权威架构文件清单（事实源定位）

| 事实源 | 权威路径 | 权威性 |
| --- | --- | --- |
| 架构主文档 | `docs/baseline/AI影视工业化生产平台V2.0.5.md`（489580B，SHA256 已核） | 唯一架构基线 |
| 阶段总表 | `contracts/phases/phase_map.yaml`（源自 31.2） | 唯一权威阶段表 |
| Action 注册 | `contracts/action_registry/action_registry.yaml`（100 actions） | 唯一权威 |
| OpenAPI 草案 | `contracts/openapi/openapi.draft.yaml` | 派生视图 |
| 对象 Schema | `contracts/schemas/object_schemas.schema.json`（84 defs） | 唯一权威 |
| 状态机 | `contracts/state_machines/state_machines.yaml` | 唯一权威 |
| 状态分类 | `contracts/enums/status_field_taxonomy.yaml` + `status_layers.yaml` | 唯一权威 |
| 枚举 | `contracts/enums/enum_registry.yaml` | 唯一权威 |
| 权限 | `contracts/permission/permission_matrix.yaml` + `authorization/authorization_registry.yaml` | 唯一权威 |
| 事件 | `contracts/runtime/event_catalog.yaml` | 唯一权威 |
| 运行时实现 | `contracts/runtime/action_implementations.yaml` | 唯一权威 |
| 修订登记 | `contracts/phase0/contract_amendments.yaml`（A1–A23） | 冻结后唯一合法变更入口 |

## 4. 状态矩阵（结论）

| 维度 | 结论 |
| --- | --- |
| ADR 清单 | 已存在（118 注册 / 24 去重编号 / 31 文件） |
| ADR 状态矩阵 | **缺失**——索引无状态字段，无 ACCEPTED/SUPERSEDED/IMPLEMENTED 矩阵 |
| 重复编号核查 | 已登记（duplicate_ids），但未到「合并/指认权威」步骤 |
| 失效引用清单 | **缺失**——索引无失效/悬空引用检查 |
| 冲突决策清单 | 部分（state_machines.yaml `conflict_rulings` / `conflict` 分区存在） |
| 权威架构文件清单 | 本报告 §3 已给出 |

## 5. BLOCKED（需 Owner/架构负责人裁决）

1. **ADR-xxx（正文编号）↔ ADR-Rxx（文件编号）的映射规则**未定义：正文 118 条与 24 个 R 文件不是一一对应，需裁定映射口径。
2. **ADR-R40 的 6 个版本文件**哪个是现行版：需收敛为单现行文件 + 归档历史版本。
3. **ADR 状态矩阵**是否补建：建议每个 R 文件头部固化 `status:` 与 `superseded_by:`，需 Owner 批准后执行。
4. **ADR-R27 的 .PROPOSED 后缀**：该提案是否已被 R28 取代，需裁定。

## 6. 进入 PR-02 的门禁结论

PR-01 交付即本报告。BLOCKED 四项不影响 PR-02（契约事实源对账）——契约权威关系在 §3 已定位，可直接开展。但 BLOCKED 未裁决前，不允许将「ADR 状态矩阵缺失」当作已收敛。
