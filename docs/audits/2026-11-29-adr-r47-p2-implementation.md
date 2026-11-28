# ADR-R47 P2 实施记录（2026-11-29）

状态：**已实施**（实现方转录，非 Owner 手写签名；Owner 的口头授权与「全部按最好的方案做」
是本次实施依据，记录本身不代签任何新的裁决）。

## 1. 授权与范围

- Owner 于 2026-11-29 明确授权 P2：契约变更 + 数据库迁移 + 代码实现 + 冻结重签。
- 范围严格限定在 `docs/proposals/generation-spec-contract-p1.md` §1–§11 的已裁定项；
  不重开 ADR-R44/R47 的既有决策（不建提示词库 / 不建 Prompt 对象存储 / 不建
  `ConstraintSnapshot` / 不建版本继承树 / Worker 不二次派生 / 不回写 ShotSpec）。
- 不改 `temporalio` 依赖、`temporal_host/port`、`uv.lock`。

## 2. 落点

| 层 | 文件 | 内容 |
|---|---|---|
| 契约 | `contracts/schemas/object_schemas.schema.json` | 新增 `GenerationSpec`（99→100）；`GenerationTask` 移除 `prompt_ref` |
| 契约 | `contracts/enums/status_field_taxonomy.yaml` | `GenerationSpec` = `envelope_only / immutable_record` |
| 契约 | `contracts/runtime/schema_field_signatures.yaml` | 重签 100 对象 |
| 迁移 | `data/migrations/versions/a9b0c1d2e3f4_generation_spec_r47.py` | 建表 + 任务列 + CHECK + 复合 FK + 索引 + RLS + 矩阵授权 |
| 授权 | `packages/shared/db_grants.py` | `generation_specs: SELECT INSERT`；`shot_specs` 列级只读 |
| 领域 | `services/production_planning/domain/generation_spec.py` | 内容寻址身份、规范 JSON、派生、`ShotSpecFacts` |
| 策略 | `packages/shared/size_policy.py`、`generation_template.py` | 版本化尺寸策略与提示词模板（代码常量，摘要钉值） |
| 持久化 | `repository/orm.py`、`repository/sqlalchemy_repos.py` | ORM + 规格仓储 + 冲突复用 + 锁定规格读取 |
| 服务 | `service/generation_service.py` | 同事务派生/绑定、自报 `genspec:` 拒绝、幂等冲突、媒体类型映射 |
| API | `api/generation_routes.py` | `prompt_ref` 可选；新增 `shot_spec_id` / `negative_prompt` / `generation_parameters` |
| Worker | `workers/generation/spec_resolver.py`、`executor.py`、`__main__.py` | 规格解析六步校验与失败码；执行器按规格取 prompt/媒体/尺寸/模型 |

## 3. 在授权范围内由实现方决定的取舍

1. **SizePolicy 与模板不建表**：两者是策略投影而非业务事实，建表会多出一个领域对象并
   触发完整的 schema/taxonomy/权限/冻结成本，且「可以被 UPDATE」正是裁决禁止的行为。
   改为代码内版本化常量：`policy_id + policy_version`、`template_version` 进身份域并随
   规格冻结；内容摘要由 `tests/unit/test_generation_spec.py` 钉值，内容变化必须升版本。
2. **模板 v1 采用最小确定性映射**：固定顺序拼接 `camera_path / subject_paths /
   shot_size_changes / aspect_ratio`，空字段省略。文案质量不是本阶段验收项，但
   「同输入同字节」是；提升文案必须发新版本。
3. **`generation_parameters` 键白名单收窄为 `guidance_scale / seed / steps`**：满足
   Owner 的「键白名单、≤16 键、仅 string/整数/布尔」约束；扩键必须同步契约
   `propertyNames` 与领域白名单（两处都改才会通过闸门）。
4. **`provider` 由模型适配器引用前缀派生**，`media_type` 由路线类型显式映射；未知类型
   一律 fail-closed，不落默认值。

## 4. 验证证据（本机，2026-11-29）

- 干净库：`p2-spec-db`（postgres:16，端口 15446，库 `ai_film_industrial`）。
  `bootstrap_identity.py --roles` → `alembic upgrade head` 成功；
  `alembic downgrade -1` → `alembic upgrade head` 成功；`alembic check`：无漂移。
- `scripts/contract_check.py`：全 PASS（100 个对象结构、字段签名、状态分类 100）。
- `scripts/freeze_manifest.py --check`：重签后 PASS（见 §5）。
- `pytest tests/unit/test_generation_spec.py`：11 passed；
  `pytest tests/integration/test_generation_spec_pg.py`：2 passed（复合 FK、绑定 CHECK、
  状态哨兵 CHECK）。
- `ruff check`：PASS。

## 5. 冻结重签

修订条目 `V205-R47-P2-GENERATION-SPEC` 登记于 `contracts/phase0/contract_amendments.yaml`
（`additions.schema_objects: GenerationSpec`、`counts_after.schema_objects: 100`），
随后 `freeze_manifest.py --write` 重签。重签前的漂移（对象 schema 变更、未登记新增）
均已由该修订解释，不是「改清单转绿」。

## 6. 残余风险与未纳入项

- **P3 外部前置未解除**：真实网关五要素仍为空；本次没有任何真实网关调用，也没有
  `SUCCEEDED` 的真实回执。
- **Worker 复核 ShotSpec 依赖跨域列级只读**：生产角色对 `shot_specs` 仅 10 列只读；
  未来 ShotSpec 新增渲染字段时，需要同步扩展列清单与模板版本，否则表现为
  `spec-shot-spec-mismatch`（fail-closed，不是静默降级）。
- **历史内联任务的 CHECK 用 `NOT VALID`**：升级库的历史行不被追溯判定；新行仍受约束。
  数据清理后可 `VALIDATE CONSTRAINT` 收紧，属后续运维动作。
- **DRAFT 试生成仍不支持**（Owner 裁决 A-2 的「现阶段」边界未变）。
