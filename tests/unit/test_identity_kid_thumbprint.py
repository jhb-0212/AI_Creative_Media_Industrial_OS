"""kid 必须是「公钥内容的指纹」，而不是角色标签（RFC 7638）。

根因（本轮发现）：kid 曾被硬编码为字面量 ``"current"``（签发侧
``services/identity_issuance``、验签侧 ``services/identity_access``、开发引导
``packages/identity/bootstrap`` 三处）。而 ``KeyStore`` 以 ``{kid: key}`` 建索引，
两把密钥同名会**静默互相覆盖**——于是提案里「KeyStore 已支持多 kid，轮换调度
属运维面」这句 residual 在实现层没有入口：运维即便注入新密钥对，也造不出第二个
可区分的 kid，验签方按 kid 缓存会一直命中旧公钥。

本文件锁定改后的不变量：
1. kid 与 RFC 7638 官方测试向量逐字节一致（不是「看起来像哈希」就算数）；
2. 同一把公钥在任何进程/任何时刻算出的 kid 相同（确定性，纯函数）；
3. 不同公钥必然得到不同 kid（轮换可表达）；
4. 签发侧（令牌头部 kid）与分发侧（JWKS kid）与验签侧（仓储索引 kid）同源，
   且**没有任何一方需要配置 kid 字符串**；
5. kid 不得混入 kid/use/alg 等非必需成员（否则 kid 无法自证，轮换即漂移）。
"""

from __future__ import annotations

import base64

import pytest
from jwt import get_unverified_header

from packages.identity.jwks import KeyStore, PublicKeyStore
from packages.identity.jwt_core import encode_token
from packages.identity.keys import (
    SigningKey,
    _thumbprint_from_numbers,
    generate_signing_key,
    load_signing_key,
    public_jwk_from_pem,
    public_key_thumbprint,
)

# RFC 7638 §3.1 官方示例的 n / e 与期望指纹。用官方向量而不是「自己算一遍再比
# 自己」，否则实现错了测试也会跟着错。
RFC7638_N = (
    "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7a"
    "PFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl9"
    "3lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHz"
    "u6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksIN"
    "HaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
)
RFC7638_E = "AQAB"
RFC7638_THUMBPRINT = "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def _b64url_to_int(value: str) -> int:
    padded = value + "=" * (-len(value) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(padded), "big")


def test_thumbprint_matches_rfc7638_test_vector() -> None:
    """指纹实现必须与官方测试向量一致——kid 是要给对方重算的，差一个字节即失配。"""
    assert _thumbprint_from_numbers(_b64url_to_int(RFC7638_N), _b64url_to_int(RFC7638_E)) == (
        RFC7638_THUMBPRINT
    )


def test_thumbprint_is_of_public_key_content_only() -> None:
    """kid 只由公钥决定：同一把密钥重新加载、不同进程计算，结果必须相同。"""
    key = generate_signing_key()

    assert public_key_thumbprint(key.public_pem) == key.kid
    reloaded = load_signing_key(key.private_pem, key.public_pem)
    # 重新加载同一对 PEM，必须得到同一个 kid——否则重启即换标识。
    assert reloaded.kid == key.kid


def test_different_keys_get_different_kids() -> None:
    """轮换的前提：两把密钥必须可区分。同 kid 会让 KeyStore 索引静默覆盖。"""
    first = generate_signing_key()
    second = generate_signing_key()

    assert first.kid != second.kid


def test_key_entrypoints_expose_no_kid_parameter() -> None:
    """任何构造入口都不得接受 kid——能传就能造出「头 A / 索引 B」的令牌。

    这条是**结构断言**而非行为断言：kid 一旦可以传入，调用方即使算错也不会
    立刻报错，而是签出一批验不过的令牌（评审 P2 指出的正是这一点）。
    把入口参数面钉死，这类错误才在写代码时就不可表达。
    """
    import dataclasses
    import inspect

    for fn in (generate_signing_key, load_signing_key, public_jwk_from_pem):
        assert "kid" not in inspect.signature(fn).parameters, fn.__name__
    assert "kid" not in inspect.signature(PublicKeyStore.__init__).parameters
    assert "kid" not in inspect.signature(KeyStore.public_pems).parameters
    # SigningKey 也没有 kid 字段：它是派生属性，不是可赋值状态。
    assert "kid" not in {f.name for f in dataclasses.fields(SigningKey)}


def test_jwk_kid_is_always_the_thumbprint() -> None:
    """JWK 里的 kid 恒为指纹；指纹输入不含 kid/use/alg（RFC 7638 必需成员）。"""
    key = generate_signing_key()

    jwk = public_jwk_from_pem(key.public_pem)
    assert jwk["kid"] == key.kid == public_key_thumbprint(key.public_pem)
    # 多传一个 kid 只能得到 TypeError：覆盖入口已被删除，不是靠「缺省就对了」。
    with pytest.raises(TypeError):
        public_jwk_from_pem(key.public_pem, "explicit-kid")  # type: ignore[call-arg]


def test_keystore_rejects_duplicate_kid() -> None:
    """重复 kid 必须显式失败，而不是「后注册的赢」变成隐性事实。"""
    key = generate_signing_key()

    with pytest.raises(ValueError, match="重复 kid"):
        KeyStore([key, key])


def test_load_signing_key_rejects_mismatched_pair() -> None:
    """私钥与公钥张冠李戴必须在加载时失败——否则会签出全平台验不过的令牌。"""
    key = generate_signing_key()
    other = generate_signing_key()

    with pytest.raises(ValueError, match="不是同一对密钥"):
        load_signing_key(key.private_pem, other.public_pem)


def test_token_header_kid_equals_jwks_kid_and_verifier_index() -> None:
    """三方同源：令牌头 kid == JWKS kid == 验签仓储索引 kid，且无需任何 kid 配置。"""
    key = generate_signing_key()
    key_store = KeyStore([key])

    token = encode_token(
        key_store.current,
        subject_id="subject-1",
        tenant_id="tenant-1",
        roles=["producer"],
        issuer="test-issuer",
        audience="test-audience",
    )

    token_kid = get_unverified_header(token)["kid"]
    jwks_kid = key_store.jwks()["keys"][0]["kid"]
    # 验签方只交出公钥，kid 由仓储内部算出——调用方连「提供 kid」这个动作都没有。
    verifier = PublicKeyStore([key.public_pem])

    assert token_kid == jwks_kid == key.kid
    assert verifier.public_pem(token_kid) == key.public_pem
