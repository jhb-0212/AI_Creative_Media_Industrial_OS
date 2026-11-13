"""验签侧密钥边界单元测试（决策 4：签发与验签分离）。

验收点：API 进程使用的验签仓储**结构上不可能**持有私钥——
不是「约定不打印」，而是类型里根本没有私钥字段。
"""

from packages.identity.jwks import KeyStore, PublicKeyStore
from packages.identity.keys import generate_signing_key
from services.identity_access import get_public_key_store


def test_public_store_carries_public_material_only() -> None:
    """从签发仓储派生的公钥视图只含公钥，JWKS 不含私钥参数（d/p/q）。"""
    key = generate_signing_key()
    signing_store = KeyStore([key])
    public_store = PublicKeyStore.from_key_store(signing_store)

    assert public_store.public_pem(key.kid) == key.public_pem
    assert public_store.jwks() == signing_store.jwks()

    jwk = public_store.jwks()["keys"][0]
    for private_param in ("d", "p", "q", "dp", "dq", "qi"):
        assert private_param not in jwk
    assert not hasattr(public_store, "current")


def test_runtime_verify_store_is_public_only() -> None:
    """运行时验签依赖使用的仓储必须是 PublicKeyStore，且没有签发入口。"""
    store = get_public_key_store()
    assert isinstance(store, PublicKeyStore)
    assert not hasattr(store, "current")
    assert not hasattr(store, "get")
