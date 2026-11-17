# A1 环境可复现 · 验证记录（2026-11-17）

审计对象：`Phase1-实现验收清单.md` A1

> 通过标准：从空环境按清单可重建出同一版本，版本号与基线一致（V2.0.5）。
> 证据要求：镜像摘要、依赖锁文件、配置清单。

## 1. 结论

| A1 子项 | 状态 | 证据 |
|---|---|---|
| 依赖锁文件 | ✅ | `uv.lock`（484341 字节，sha256 `c603ac8a…`），构建时 `uv sync --frozen`，漂移即失败 |
| 配置清单 | ✅ | `docs/evidence/phase1-runtime-inventory.md`（**由代码生成**，43 个配置字段 / 11 个敏感字段 / 3 条启动期校验），`tests/unit/test_runtime_inventory.py` 6 例断言其与代码一致 |
| 镜像摘要 | ✅ 基础镜像已固定；业务镜像已构建并自检 | 见 §3、§4、§5 |
| 版本号与基线一致 | ✅ | `Settings.app_version` = `pyproject.version` = Dockerfile `ARG APP_VERSION` = `2.0.5`，三处由测试对齐；镜像 OCI 标签实测为 `2.0.5` |

**本轮修掉的一个真实缺陷**：业务镜像此前没有声明 `org.opencontainers.image.version`，
等于**继承基础镜像的标签**——实测值为 `0.9.30-python3.12-bookworm-slim`（uv 的版本）。
一个声称自己是 uv 0.9.30 的业务镜像，在制品库、漏洞扫描与事故复盘里都会被认错版本。
现在 Dockerfile 用 `ARG APP_VERSION=2.0.5` 写入 OCI 标签，并由
`test_app_version_matches_contract_baseline` 钉住它与 `pyproject.toml` 同值。

## 2. 版本基线

| 项 | 值 |
|---|---|
| 平台版本 | `2.0.5`（契约 V2.0.5 / `AFS-ARCH-MASTER-V2.0.5`） |
| Python | `>=3.12`（容器内实测 `3.12.12`） |
| 迁移脚本 | 38 个（head = `f1a2b3c4d5e6`，见 A2 演练证据） |

完整清单见 `docs/evidence/phase1-runtime-inventory.md`（生成物，勿手改）。

## 3. 基础镜像（按 digest 固定）

| 镜像 | digest（多架构索引） | 说明 |
|---|---|---|
| `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` | `sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58` | builder 与 runtime 两阶段**使用同一 digest** |

**为什么必须固定**：只写 `:tag` 时 tag 可变，同一个 Dockerfile 今天和明天会构建出
两个不同的运行时，A1 的"重建出同一版本"在此项被固定前不成立。

**为什么用索引 digest 而不是某平台 digest**：索引（manifest list）会按架构分发，
x86 与 arm64 都能构建，同时字节仍被固定。

更新流程（写在 Dockerfile 注释里，避免下一个人靠猜）：

```bash
docker pull ghcr.io/astral-sh/uv:python3.12-bookworm-slim
docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/astral-sh/uv:python3.12-bookworm-slim
# 把新 digest 换进两处 FROM（必须同时改：venv 的解释器路径依赖同源镜像）
python scripts/runtime_inventory.py --write
```

## 4. 构建产物

```bash
docker build -f infra/docker/Dockerfile.api -t ai-film-industrial-api:2.0.5 .
```

| 项 | 值 |
|---|---|
| 镜像标签 | `ai-film-industrial-api:2.0.5` |
| 镜像 ID | `sha256:eacdb10abb0a90ea01a7f61b1e9aadea98524b813f24a60b365eb7cf3bbd69d0` |
| 镜像大小 | 600126375 字节（约 572 MiB） |
| `org.opencontainers.image.version` | `2.0.5`（修复前为 `0.9.30-python3.12-bookworm-slim`） |
| 构建时依赖解析 | `uv sync --frozen`（严格按 `uv.lock`，任何漂移直接失败） |

## 5. 运行时自检（构建成功 ≠ 能跑）

**① 无配置时 fail-closed**（这条同样是证据：镜像里没有可用的默认密钥）：

```
services.identity_access.IdentityConfigurationError: 验签公钥缺失：必须配置
IDENTITY_PUBLIC_KEY_PEM 或 IDENTITY_JWKS_FILE。API 进程不允许用签发私钥派生公钥
（B4：私钥不进 API 进程）。
Application startup failed. Exiting.
```

**② 注入公钥后正常服务**：

| 检查 | 实测输出 |
|---|---|
| `GET /health` | `{"status":"ok","service":"api","version":"2.0.5"}` |
| `python -V` | `Python 3.12.12` |
| `command -v uv` | 不存在（Dockerfile 约束 2：交付层不携带构建工具） |
| `id -u` | `10001`（非 root 运行） |

复现命令：

```bash
PEM=$(python -c "from cryptography.hazmat.primitives import serialization as s; \
from cryptography.hazmat.primitives.asymmetric import rsa as r; \
print(r.generate_private_key(public_exponent=65537,key_size=2048).public_key()\
.public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo).decode())")
docker run -d --rm --name a1check -e IDENTITY_PUBLIC_KEY_PEM="$PEM" -p 18000:8000 \
  ai-film-industrial-api:2.0.5
curl -fsS http://127.0.0.1:18000/health
docker rm -f a1check
```

## 6. 配置清单的防漂移机制

`docs/evidence/phase1-runtime-inventory.md` 不是手写文档，而是
`scripts/runtime_inventory.py` 从 `Settings.model_fields` 与 `Dockerfile.api` 渲染的结果。

| 断言 | 守的失败面 |
|---|---|
| 磁盘文件 == 当前代码渲染 | 文档过期（加了字段没重新生成） |
| 每个 `Settings` 字段都出现在清单 | 新配置项被漏记，部署方不知道要配 |
| 密钥字段行不含口令内容 | 清单被附进发布包时泄露默认口令 |
| 每个 `FROM` 都带 `@sha256:` | 有人加回浮动 tag，A1 前提被悄悄破坏 |
| 三处版本号同值 | 镜像标签与基线脱节（本轮修掉的那类缺陷） |
| 两次渲染逐字节相同 | 清单混入时间戳/commit，无法被比对 |

**关键设计**：清单记录的是**代码里的默认值**（`field.default`），不是当前 `.env`
实例化后的值。否则同一份文档在不同环境渲染出不同内容——A1 的证据自己就会破坏
"可复现"。

## 7. 本记录不声称什么

1. **不声称 bit-for-bit 可复现**。固定了基础镜像 digest 与依赖锁文件，
   但 `docker build` 本身会写入构建时间等元数据，两次构建的镜像 ID 不保证相同
   （本轮两次构建 ID 已不同：`97a1768a…` → `eacdb10a…`，因 Dockerfile 变更）。
   可复现性的准确口径是：**输入固定（锁文件 + 基础镜像 digest）+ 构建路径固定 +
   运行时自检通过**，不是字节级同一。
2. **镜像摘要不是仓库 manifest digest**。本镜像是本地构建、未推送，
   记录的是本地 image ID。推送到制品库后应改记 registry 的 manifest digest。
3. **未在 CI 中重建比对**。当前证据是一次人工构建记录；"每次提交都重建"需要
   CI 流水线与制品库，仓库里没有。
4. **单架构验证**。基础镜像用索引 digest 保留了多架构能力，但本轮只在
   linux/amd64 上构建与运行；arm64 未实测。
5. **`/readyz` 未验证**。它要连真实数据库；本记录只验证了 `/health` 与进程约束。
   数据库连通性由 A2/A4 演练（真实 PostgreSQL）覆盖。
