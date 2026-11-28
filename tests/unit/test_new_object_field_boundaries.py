"""R42 新对象的字段边界（第四批）。

两条不变量必须同时成立：

1. ``entity_id`` 是领域唯一身份，数据库代理键 ``id`` 不进契约；
2. ``project_id`` 是归属事实，必须与实体一起出现在契约里（ORM/DB 早已有该列，
   漏在 Schema 会让「契约能说清身份、说不清归属」）。

这里直接读权威 Schema，而不是复用字段签名台账——台账是派生视图，拿它自证
会把漂移一起签进去。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "object_schemas.schema.json"
)
_OBJECTS = ("ArtifactManifest", "AssetRegistrationReceipt")


@pytest.fixture(scope="module")
def definitions() -> dict[str, dict]:
    payload = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return payload["definitions"]


@pytest.mark.parametrize("name", _OBJECTS)
def test_entity_id_is_identity_and_db_id_is_not_in_contract(
    definitions: dict[str, dict], name: str
) -> None:
    definition = definitions[name]
    properties = definition["properties"]
    assert "entity_id" in definition["required"]
    assert "entity_id" in properties
    assert "唯一" in properties["entity_id"]["description"]
    assert "id" not in definition["required"]
    assert "id" not in properties


@pytest.mark.parametrize("name", _OBJECTS)
def test_project_id_and_strict_boundary(definitions: dict[str, dict], name: str) -> None:
    definition = definitions[name]
    assert "project_id" in definition["required"]
    assert "project_id" in definition["properties"]
    assert definition["additionalProperties"] is False
