# ADR-R21：仓储异常语义与「服务层不依赖驱动」

- 编号：ADR-R21
- 状态：已采纳
- 相关包：C（数据不变量）
- 相关文档：`docs/audits/2026-11-16-review-round3-response.md` 第 8、16 条、ADR-R20（数据不变量）

## 背景

四个 service 直接 `from sqlalchemy.exc import IntegrityError`。表面上只是"分层不够干净"，
实际造成了三个后果：

1. **一个异常表达两件事。** `IntegrityError` 同时被用来表示：
   - 唯一约束冲突（并发重复提交）→ 正确处置是**幂等返回既有记录**；
   - 乐观锁冲突（`entity_version` 被推进）→ 正确处置是**基于新版本重试**。

   处置方式相反，却只能靠"捕获后回查"猜是哪一种。猜错不会报错——它会把别人的修改
   结果当成本次请求的结果返回。
2. **两条实现语义不等价。** SQL 实现在乐观锁冲突时抛 `ConcurrentModificationError`，
   而内存实现（`InMemoryProjectRepository`）对**两种**情况都抛 `IntegrityError`。
   单测跑内存实现、集成测试跑 SQL 实现，于是单测的通过不能代表行为一致。
3. **语义泄漏到 API。** 驱动异常没有错误码，逃出服务层就是 500。业务层本该说
   "这是重复提交"或"这是并发冲突"。

还有一个更弱的地方：审查第 8 条指出服务内部（`domain/`、`service/`）完全没有分层闸门——
顶层闸门只看 `services → packages` 的方向，服务内部是"法外之地"，所以上面这些写法
能一直存在而没人被拦。

## 决策

### 1. 异常语义下沉，翻译收敛在仓储层

```
数据库驱动异常 →（仓储层：packages/shared/persistence.py）→ 领域异常 →（服务层）→ 结构化错误码
```

异常类型定义在 `packages/errors/repository_errors.py`（**不依赖任何驱动**）：

| 异常 | 含义 | 正确处置 | 错误码 |
|---|---|---|---|
| `DuplicateEntityError` | 同一请求被重复提交（唯一约束兜住竞态） | 幂等：内容一致则返回既有记录 | `IDEMPOTENCY_CONFLICT` |
| `ConcurrentModificationError` | 两个不同请求并发改同一实体 | 重试：基于最新版本重算 | `CONCURRENT_MODIFICATION` |
| `ReferenceViolationError` | 引用不存在的对象（外键悬空） | 拒绝：调用方参数错误 | `VALIDATION_ERROR` |
| `IntegrityViolationError` | 其它完整性冲突（CHECK / NOT NULL / 触发器） | 报警：实现缺陷 | `INTERNAL_ERROR` |

翻译按 SQLSTATE：`23505` → duplicate，`23503` → reference，其余一律 `IntegrityViolation`。
**未识别的状态码不静默**：约束冲突被当成成功时，数据不变量就失去了最后的观测点。

**为什么异常不放在 `packages/shared/persistence.py` 一起**：翻译函数必须 `import sqlalchemy`
（它要读驱动异常的 SQLSTATE），而服务层要捕获这些异常。放在一起会让"捕获一个领域异常"
顺带把驱动拉进服务层，等于绕开刚立的规矩。依赖方向固定为
`errors ← shared.persistence ← repository ← service`。

### 2. 闸门 R6：`domain/` 与 `service/` 不得 import 框架/驱动

`scripts/architecture_check.py::check_service_internal_layering`：扫描
`services/<domain>/{domain,service}/**`，出现 `fastapi` / `starlette` / `sqlalchemy` / `asyncpg`
即报红。

- **不覆盖 `api/` 与 `repository/`**：这两层本来就是接触 HTTP 与 SQL 的地方；
- 豁免必须**逐条**（`文件 → 理由` 映射），当前为空。刻意不提供"目录级白名单"——
  后者一旦存在，整个目录就永久免检。

## 配套闸门

| 闸门 | 守什么 | 位置 |
|---|---|---|
| `test_unique_violation_maps_to_duplicate_entity` | 23505 → `DuplicateEntityError`（幂等入口） | `tests/unit/test_repository_errors.py` |
| `test_foreign_key_violation_maps_to_reference_violation` | 23503 → 参数错误而非内部错误 | 同上 |
| `test_unknown_integrity_violation_is_internal_error` | 未识别约束冲突不静默 | 同上 |
| `test_duplicate_and_concurrency_are_finally_distinct` | 两种冲突必须是不同异常 + 不同错误码 | 同上 |
| `test_in_memory_save_raises_concurrency_conflict` | 内存实现与 SQL 实现语义一致 | 同上 |
| R6 `check_service_internal_layering` | service/domain 层 0 处框架依赖 | `scripts/architecture_check.py` |

## 后果

**行为变化**：

1. 服务层捕获的异常类型变了（`IntegrityError` → `DuplicateEntityError`）。这**不会**改变
   对外错误码：翻译保留了原来的码（`IDEMPOTENCY_CONFLICT` / `CONCURRENT_MODIFICATION`）。
2. `InMemoryProjectRepository.save` 的冲突从 `IntegrityError` 变为
   `ConcurrentModificationError`——这是修正，不是破坏：它与 SQL 实现原本就抛的异常一致了。
3. `tests/unit/test_asset_service.py` 的"并发"用例此前是**假绿**：快速路径（先查后写）
   直接命中既有记录返回，`add` 从未执行，"并发回查"那段代码没被覆盖。现在让第一次
   `get_by_id` 返回 `None`，用例才真正走到 `add` 冲突 → 回查 → 幂等返回。

**新增的诚实登记**：

- R6 只覆盖 `services/` 内部，`apps/` 与 `workers/` 的内部分层仍未覆盖（`apps` 是薄层，
  风险低；`workers` 尚无代码）。
- 翻译只处理 PostgreSQL 的 SQLSTATE。换驱动（例如换回 psycopg 同步驱动）时 `pgcode`
  同样可用，但**其它数据库**的错误码完全不同——本项目只支持 PostgreSQL，这一点在
  `packages/shared/database.py` 已有基线约束，不在本 ADR 重复。

## 验证

- `pytest tests` 442 passed（本 ADR 覆盖 12 条：`tests/unit/test_repository_errors.py` 7 条 +
  R6 闸门正负向 5 条）
- 反向验证：往 `services/project_planning/domain/project.py` 加一行
  `from sqlalchemy import text`，`architecture_check` 立即报
  `[FAIL] R6 服务内分层 | ... domain/ 层 import 了 sqlalchemy`
- `grep -rn "IntegrityError" services/` 只剩仓储层评论与 `persistence.py` 的翻译实现
- `ruff` / `mypy` / `architecture_check`（R1–R6 全 0）/ `freeze_manifest` /
  `contract_check` / `alembic check` 全绿
