# 运行时可复现清单（Phase 1 验收 A1）

> 本文件由 `scripts/runtime_inventory.py` 生成，**请勿手工编辑**。
> `tests/unit/test_runtime_inventory.py` 会断言它与当前代码逐行一致——
> 手改会在下一次测试里被覆盖，加字段后忘记重新生成则会测试失败。

## 1. 版本基线

| 项 | 值 | 来源 |
|---|---|---|
| 平台版本 | `2.0.5` | `Settings.app_version`（契约 V2.0.5） |
| Python 要求 | `>=3.12` | `pyproject.toml` |
| 依赖锁文件 | `uv.lock`，sha256 `c603ac8ae828b5e809968d2baedd2f21ce6cfca49fa8ebff5e461a5cb48beede`（484341 字节） | 仓库 |
| 项目定义 | `pyproject.toml`，sha256 `e8a54473971c8b99597f17f17a8b7ce58b43d29f8bd3b726c3423e365b85ddae` | 仓库 |
| 迁移脚本 | 38 个 | `data/migrations/versions/` |
| 镜像版本标签 | `2.0.5` | `Dockerfile.api` 的 `ARG APP_VERSION`（写入 OCI `image.version` 标签） |

## 2. 基础镜像

| 镜像引用 | 阶段 | 按 digest 固定 |
|---|---|---|
| `ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58` | builder | 是 |
| `ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58` | runtime | 是 |

全部基础镜像已按 digest 固定，重建时基础层字节一致。

## 3. 配置项（环境变量）

变量名为字段名大写。`敏感` 列表示该值按密钥处理（回显与日志均脱敏）；
`生产必填` 列表示 `APP_ENV=production` 下为空则拒绝启动。

| 环境变量 | 类型 | 默认值 | 敏感 | 生产必填 |
|---|---|---|---|---|
| `APP_ENV` | str | `development` |  |  |
| `APP_VERSION` | str | `2.0.5` |  |  |
| `LOG_LEVEL` | str | `INFO` |  |  |
| `POSTGRES_HOST` | str | `localhost` |  |  |
| `POSTGRES_PORT` | int | `15432` |  |  |
| `POSTGRES_USER` | str | `postgres` |  |  |
| `POSTGRES_PASSWORD` | SecretStr | （密钥，默认空） | 是 | 是 |
| `POSTGRES_DB` | str | `ai_film_industrial` |  |  |
| `POSTGRES_APP_PASSWORD` | SecretStr | （密钥，不回显） | 是 | 是 |
| `REDIS_URL` | SecretStr | （密钥，不回显） | 是 |  |
| `RATE_LIMIT_ENABLED` | bool | `True` |  |  |
| `RATE_LIMIT_PER_MINUTE` | int | `120` |  |  |
| `RATE_LIMIT_WINDOW_SECONDS` | int | `60` |  |  |
| `OUTBOX_RELAY_BATCH_SIZE` | int | `100` |  |  |
| `OUTBOX_RELAY_INTERVAL_SECONDS` | float | `5.0` |  |  |
| `OUTBOX_WEBHOOK_URL` | str | （空字符串） |  |  |
| `OUTBOX_WEBHOOK_SECRET` | SecretStr | （密钥，默认空） | 是 |  |
| `OUTBOX_WEBHOOK_TIMEOUT_SECONDS` | float | `5.0` |  |  |
| `OUTBOX_WEBHOOK_CIRCUIT_FAILURE_THRESHOLD` | int | `5` |  |  |
| `OUTBOX_WEBHOOK_CIRCUIT_RECOVERY_SECONDS` | float | `30.0` |  |  |
| `GENERATION_LEASE_SECONDS` | float | `300.0` |  |  |
| `ERRORS_CATALOG_PUBLIC` | bool | `False` |  |  |
| `ERRORS_CATALOG_TOKEN` | SecretStr | （密钥，默认空） | 是 |  |
| `S3_ENDPOINT_URL` | str | （空字符串） |  |  |
| `S3_ACCESS_KEY_ID` | SecretStr | （密钥，默认空） | 是 |  |
| `S3_SECRET_ACCESS_KEY` | SecretStr | （密钥，默认空） | 是 |  |
| `S3_BUCKET` | str | `ai-film-assets` |  |  |
| `TEMPORAL_HOST` | str | `localhost` |  |  |
| `TEMPORAL_PORT` | int | `7233` |  |  |
| `MQ_URL` | SecretStr | （密钥，默认空） | 是 |  |
| `MODEL_GATEWAY_BILLING_MODE` | str | `USER_OWN_KEY` |  |  |
| `MODEL_GATEWAY_API_KEY` | SecretStr | （密钥，默认空） | 是 |  |
| `IDENTITY_ISSUER` | str | `ai-film-identity` |  |  |
| `IDENTITY_AUDIENCE` | str | `ai-film-api` |  |  |
| `IDENTITY_ACCESS_TOKEN_TTL_SECONDS` | int | `900` |  |  |
| `IDENTITY_PROCESS_ROLE` | Literal | `api` |  |  |
| `IDENTITY_PRIVATE_KEY_PEM` | SecretStr | （密钥，默认空） | 是 |  |
| `IDENTITY_PUBLIC_KEY_PEM` | SecretStr | （密钥，默认空） | 是 |  |
| `IDENTITY_JWKS_FILE` | str | （空字符串） |  |  |
| `IDENTITY_JWKS_REFRESH_SECONDS` | int | `0` |  |  |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | str | （空字符串） |  |  |
| `OTEL_EXPORTER_OTLP_INSECURE` | bool | `False` |  |  |
| `OTEL_SERVICE_NAME` | str | `ai-film-industrial-platform` |  |  |

## 4. 启动期校验（配置错误一律拒绝启动，不留到运行期）

| 校验 | 说明 |
|---|---|
| `_enforce_identity_process_role` | 进程角色与密钥必须自洽：API 无签发私钥，issuer 必有私钥。 |
| `_reject_empty_secrets_in_production` | 生产环境（app_env=production）下，关键密钥为空时拒绝启动。 |
| `_require_webhook_secret_with_url` | 配了 webhook URL 却不配密钥时拒绝启动（所有环境，不只生产）。 |

## 5. 重建步骤（从空环境的唯一入口）

```bash
uv sync --frozen --extra dev          # 严格按 uv.lock，漂移即失败
python -m alembic upgrade head        # 建库结构（当前 head 见迁移目录最大 revision）
python -m uvicorn apps.api.main:app   # 或 docker compose -f infra/compose/docker-compose.yml up
```

容器构建：`docker build -f infra/docker/Dockerfile.api -t ai-film-industrial-api:<tag> .`
