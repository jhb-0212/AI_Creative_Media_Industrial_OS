"""身份签发进程（workers/identity）——KEYS-1 S3 的物理进程落点。

进程职责（最小闭环，与 KEYS-1 决策 3 一致）：
- 持**签发私钥**（部署事实：私钥只注入本进程）；
- 对外提供 JWKS（公钥权威分发通道），供 API/网关按 kid 取公钥验签；
- **不**提供 token 签发 HTTP 端点（那属另一份契约决策，本进程只解决
  「签发私钥不出签发进程」这个根因）。

为什么需要它（根因）：此前签发逻辑虽已迁到 services/identity_issuance、
验签方结构上零依赖私钥（R4 闸门强制），但两者仍编译进**同一个镜像、同一个
进程**——私钥边界靠「进程配置清单」这一约定挡，而红线禁止「用配置声明代替
运行时隔离」。本进程让「私钥只在身份进程内」成为部署事实。

分层：workers 只能依赖 packages/services（architecture_check R2/R1），
因此本进程壳 import services.identity_issuance 是合法方向；apps 层被 R4
禁止 import 签发模块，验签方只能拿公钥。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from packages.shared.config import get_process_settings
from packages.shared.logging import get_logger, setup_logging
from services.identity_issuance import get_key_store

# 进程身份在**入口导入时**声明并校验：identity 角色要求私钥 + 公钥，
# 生产缺任一即拒绝启动（fail-closed）。这是 KEYS-1/KEYS-2 的落点，不是
# 靠部署方记得设置环境变量。
settings = get_process_settings("identity")
setup_logging(settings.log_level)
logger = get_logger("workers.identity")

app = FastAPI(
    title="AI Creative Media Industrial OS — Identity",
    version=settings.app_version,
    description="身份签发进程：持私钥，提供 JWKS 公钥分发（不提供 token 端点）",
)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """存活探针：只证明进程能应答与密钥可用，不触碰外部依赖。

    密钥不可用（开发环境随机生成失败 / 生产配置错误）时构造 KeyStore 会抛错，
    此处即表现为 500——「身份进程没有可用签名密钥」必须显式暴露，而不是
    起一个空转的进程让验签方拿到空 JWKS。
    """
    key_store = get_key_store()
    return {
        "status": "ok",
        "service": "identity",
        "version": settings.app_version,
        "signing_kid": key_store.current.kid,
    }


@app.get("/.well-known/jwks.json", tags=["system"], include_in_schema=False)
async def jwks() -> JSONResponse:
    """JWKS（仅公钥）——验签方获取/轮换公钥的唯一权威通道。

    不进 OpenAPI 业务契约（与 /health、/metrics 同属系统面）：OpenAPI 草案
    由 Action Registry 派生，操作数必须等于业务 Action 数（freeze_manifest
    的 parity 校验）；JWKS 是密钥分发，不是业务动作，登记进去会破坏
    「OpenAPI == Actions」这条派生关系。
    """
    document = get_key_store().jwks()
    # 明确禁止缓存：轮换后验签方必须能立刻取到新公钥；缓存策略交给调用方
    # 的本地 kid 缓存（未命中 kid 时回源），而不是让中间层缓存旧文档。
    return JSONResponse(
        content=document,
        headers={"Cache-Control": "no-store"},
    )
