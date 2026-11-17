"""身份签发进程（workers/identity）的进程契约测试（KEYS-3）。

验收点（对应 contracts/security_boundary.yaml 的 KEYS-3）：
1. 身份进程能独立启动（import 即完成进程身份声明 + 密钥校验）；
2. JWKS 端点只发布公钥，响应文本中不出现任何私钥材料；
3. 签发能力（get_key_store，持私钥）只出现在身份进程这一侧——API 侧
   验签仓储结构上没有私钥（由 tests/unit/test_identity_public_store.py 断言），
   架构闸门 R4 负责阻止 apps 层 import 签发模块。

这些断言不需要真实 PostgreSQL：身份进程不连业务库（只持密钥、发公钥）。
"""

from __future__ import annotations

import json

from httpx import ASGITransport, AsyncClient

from services.identity_issuance import get_key_store
from workers.identity.app import app


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://identity")


async def test_health_reports_signing_kid() -> None:
    """存活探针报告当前签发 kid（证明进程内密钥可用，而非空转）。"""
    async with await _client() as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "identity"
    assert body["signing_kid"]


async def test_jwks_exposes_public_keys_only() -> None:
    """JWKS 只含公钥参数（kty/kid/n/e），不含私钥参数（d/p/q/dp/dq/qi）。"""
    async with await _client() as client:
        resp = await client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"

    document = resp.json()
    assert document["keys"], "JWKS 不能为空：验签方需要公钥"
    for jwk in document["keys"]:
        assert jwk["kty"] == "RSA"
        assert jwk["kid"]
        for private_param in ("d", "p", "q", "dp", "dq", "qi"):
            assert private_param not in jwk, f"JWKS 泄露私钥参数 {private_param}"


async def test_jwks_is_the_authority_for_the_signing_key() -> None:
    """JWKS 的公钥与被签发令牌所用私钥同源（kid 对齐，轮换不脱节）。"""
    async with await _client() as client:
        resp = await client.get("/.well-known/jwks.json")
    kids = {jwk["kid"] for jwk in resp.json()["keys"]}
    assert get_key_store().current.kid in kids


async def test_private_key_material_never_in_http_response() -> None:
    """响应文本不得含私钥 PEM 片段（红线：密钥不落日志/不落响应）。"""
    private_pem = get_key_store().current.private_pem.decode("utf-8")
    # 取私钥正文的一段非公开片段做比对（PEM 头本身是公开格式，不能当指纹）。
    fingerprint = private_pem.splitlines()[1][:32]
    assert fingerprint

    async with await _client() as client:
        jwks_text = (await client.get("/.well-known/jwks.json")).text
        health_text = (await client.get("/health")).text
    assert fingerprint not in jwks_text
    assert fingerprint not in health_text
    # 额外确认 JWKS 是合法 JSON 且没有意外把 PEM 塞进字段。
    assert "BEGIN" not in json.dumps(json.loads(jwks_text))
