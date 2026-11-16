"""签发私钥边界测试（包 B4）。

要求：**签发私钥不进入 API 进程，出现即拒绝启动**。

这条要求此前只有一句文档（"私钥仅用于签发，身份服务持有"），而生产配置校验
把私钥列为**必填**——文档与配置互相矛盾，实际部署必然按配置走。本文件把要求
变成四条可执行断言：

1. 配置层：``role=api`` 带私钥 → 构造即失败；``role=issuer`` 无私钥 → 构造即失败。
2. 入口层：API 入口的守卫函数遇到私钥配置 → 抛错并给出修复指引。
3. 结构层：签发端由私钥派生公钥，不要求单独配置（两者不匹配在结构上不可能）。
4. 分发层：验签方可只凭 JWKS 文档构建密钥仓储（公钥分发路径可用）。
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError

from packages.identity.guards import (
    SigningKeyPresentInApiProcessError,
    assert_no_signing_key_in_api_process,
)
from packages.identity.issuer import TokenIssuer
from packages.identity.jwks import PublicKeyStore
from packages.identity.keys import generate_signing_key
from packages.identity.principal import SubjectType
from packages.shared.config import Settings

_PRIVATE_PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "placeholder-for-guard-test\n"
    "-----END PRIVATE KEY-----\n"
)


def test_api_role_rejects_signing_private_key() -> None:
    """``IDENTITY_PROCESS_ROLE=api`` 配置了私钥 → 拒绝启动（不是警告）。"""
    with pytest.raises(ValidationError) as exc:
        Settings(
            identity_process_role="api",
            identity_private_key_pem=SecretStr(_PRIVATE_PEM),
        )
    assert "不得配置签发私钥" in str(exc.value)


def test_issuer_role_requires_signing_private_key() -> None:
    """``role=issuer`` 却没有私钥 → 拒绝启动（否则进程能起来但一条令牌都签不了）。"""
    with pytest.raises(ValidationError) as exc:
        Settings(identity_process_role="issuer")
    assert "必须配置" in str(exc.value)


def test_api_entrypoint_guard_blocks_private_key() -> None:
    """入口守卫：带私钥的配置直接抛错，且错误文案包含修复指引。

    用 ``model_construct`` 绕过配置校验，模拟"某条路径构造出了带私钥的 Settings"
    ——守卫必须独立成立，不能只在配置校验存在时才有效。
    """
    settings = Settings.model_construct(
        identity_private_key_pem=SecretStr(_PRIVATE_PEM),
    )
    with pytest.raises(SigningKeyPresentInApiProcessError) as exc:
        assert_no_signing_key_in_api_process(settings)
    assert "IDENTITY_PUBLIC_KEY_PEM" in str(exc.value)


def test_api_entrypoint_guard_allows_public_key_only() -> None:
    """只有公钥的配置通过守卫（正常形态）。"""
    settings = Settings.model_construct(
        identity_private_key_pem=SecretStr(""),
    )
    assert_no_signing_key_in_api_process(settings)


def test_api_entrypoint_actually_enforces_both_directions() -> None:
    """入口必须真的执行两个方向——否则上面的测试证明的只是"函数能用"。

    - 有私钥 → 拒绝启动；
    - 没公钥 → 拒绝启动（``get_public_key_store`` 在 lifespan 里被显式调用）。
    两条都写在 ``apps/api/main.py`` 的 lifespan 里，缺一条就会出现
    "函数写好了但没人调用"的假闭环。
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "apps" / "api" / "main.py"
    ).read_text(encoding="utf-8")
    assert "assert_no_signing_key_in_api_process(settings)" in source
    lifespan_body = source.split("async def lifespan", 1)[1].split("app = FastAPI", 1)[0]
    assert "get_public_key_store()" in lifespan_body


def test_issuer_derives_public_key_so_mismatch_is_impossible() -> None:
    """签发端由私钥派生公钥：签出的令牌能用自己的公钥验签。"""
    key = generate_signing_key(kid="current")
    issuer = TokenIssuer.from_private_pem(
        key.private_pem, kid="current", issuer="iss", audience="aud"
    )

    token = issuer.issue(
        subject_id="user-1",
        tenant_id="00000000-0000-0000-0000-000000000001",
        roles=["导演"],
        subject_type=SubjectType.HUMAN.value,
    )

    from packages.identity.jwt_core import decode_token

    principal = decode_token(
        token, public_pem=issuer.signing_key.public_pem, issuer="iss", audience="aud"
    )
    assert principal.subject_id == "user-1"


def test_jwks_file_is_a_valid_public_distribution_path(tmp_path: object) -> None:
    """验签方可只凭 JWKS 文档构建密钥仓储（公钥分发不需要私钥在场）。"""
    key = generate_signing_key(kid="current")
    issuer = TokenIssuer.from_private_pem(
        key.private_pem, kid="current", issuer="iss", audience="aud"
    )
    path = tmp_path / "jwks.json"  # type: ignore[operator]
    path.write_text(json.dumps(issuer.public_jwks), encoding="utf-8")

    store = PublicKeyStore.from_jwks_file(path)

    assert store.public_pem("current") is not None
    assert b"PRIVATE" not in (store.public_pem("current") or b"")
