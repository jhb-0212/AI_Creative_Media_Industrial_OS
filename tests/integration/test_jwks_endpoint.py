"""JWKS 发布端点集成测试（ADR-R29）。

验证 /.well-known/jwks.json 的对外契约：
- 200 返回非空 keys，且与验签路径**同一来源**（同源是轮换自动生效的前提）；
- Cache-Control / ETag 头存在（HTTP 缓存语义）；
- If-None-Match 命中时 304（消费方按标准语义刷新，不重复传输）。
"""

from __future__ import annotations

import hashlib
import json

from httpx import AsyncClient

from services.identity_access import get_public_key_store

_JWKS_PATH = "/.well-known/jwks.json"


async def test_jwks_published_matches_verification_store(
    client: AsyncClient,
) -> None:
    """发布的公钥集与验签仓储同源：不存在两个版本的公钥事实。"""
    resp = await client.get(_JWKS_PATH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    document = resp.json()
    assert document == get_public_key_store().jwks()
    # 测试夹具的签名密钥 kid 固定为 current（tests/__init__ 约定）。
    kids = [entry["kid"] for entry in document["keys"]]
    assert "current" in kids


async def test_jwks_has_cache_headers(client: AsyncClient) -> None:
    """携带 Cache-Control 与 ETag：消费方可按标准 HTTP 缓存语义刷新。"""
    resp = await client.get(_JWKS_PATH)
    assert "public" in resp.headers["cache-control"]
    assert "max-age=" in resp.headers["cache-control"]
    assert resp.headers["etag"].startswith('"sha256:')


async def test_jwks_conditional_request_returns_304(client: AsyncClient) -> None:
    """If-None-Match 命中当前 ETag 时返回 304，不重复传输文档。"""
    first = await client.get(_JWKS_PATH)
    etag = first.headers["etag"]
    # ETag 与文档内容绑定：可从响应体独立复算（发布与验签同源的自证）。
    body = json.dumps(
        first.json(), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert etag == f'"sha256:{hashlib.sha256(body).hexdigest()}"'

    second = await client.get(_JWKS_PATH, headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""


async def test_jwks_mismatched_etag_returns_200(client: AsyncClient) -> None:
    """If-None-Match 不命中（轮换后旧 ETag）时返回 200 + 新文档。"""
    stale = '"sha256:0000000000000000000000000000000000000000000000000000000000000000"'
    resp = await client.get(_JWKS_PATH, headers={"If-None-Match": stale})
    assert resp.status_code == 200
    assert resp.json()["keys"]
