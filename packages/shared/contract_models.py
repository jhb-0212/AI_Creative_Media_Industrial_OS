"""由契约生成 FastAPI 响应模型（contract → response_model 单一事实源）。

为什么不在路由里手写 Pydantic 响应模型：
- 手写等于把 object_schemas 的字段抄一遍，抄完就开始漂移（审计 P2-4：
  openapi 里 0 处 tenant_id，实际响应带 tenant_id；路由没有 response_model，
  OpenAPI 只能显示 object）。
- 这里从 `contracts/schemas/object_schemas.schema.json` 直接生成模型：
  契约加字段 → 模型自动多一个字段；契约删字段 → 响应里多余字段会被
  FastAPI 过滤掉（Pydantic 默认忽略多余键），不会把内部字段漏出去。

类型映射刻意保守：JSON Schema 的 array/object 统一映射到 `list[Any]` /
`dict[str, Any]`，不在此处重复表达业务元素类型（那属于契约校验的职责，
见 contract_check.check_schema_objects）。
"""

from __future__ import annotations

import json
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, cast

from pydantic import create_model

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contracts" / "schemas" / "object_schemas.schema.json"

_JSON_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list[Any],
    "object": dict[str, Any],
}


@lru_cache(maxsize=1)
def _definitions() -> dict[str, Any]:
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    definitions = data.get("definitions", data.get("$defs", {}))
    return cast(dict[str, Any], definitions)


def _annotation_for_type(json_type: object) -> Any:
    """把 JSON Schema 的 type（字符串或字符串数组）映射为 Python 注解。"""
    if isinstance(json_type, list):
        types = [_JSON_TYPE_MAP.get(str(item)) for item in json_type]
        if None in types and type(None) not in types:
            types.append(type(None))
        non_any = [t for t in types if t is not None and t is not Any]
        if not non_any:
            return Any
        union = non_any[0]
        for extra in non_any[1:]:
            union = union | extra
        if type(None) in types:
            union = union | None
        return union
    return _JSON_TYPE_MAP.get(str(json_type), Any)


@cache
def contract_model(name: str) -> type[Any]:
    """按契约定义生成响应模型；契约缺该定义时直接抛错（不静默放行）。"""
    definitions = _definitions()
    if name not in definitions:
        raise KeyError(f"object_schemas 中不存在对象定义: {name}")

    spec = definitions[name]
    properties: dict[str, Any] = spec.get("properties") or {}
    required = set(spec.get("required") or [])

    fields: dict[str, Any] = {}
    for prop_name, prop_spec in properties.items():
        spec = prop_spec or {}
        base = _annotation_for_type(spec.get("type"))
        if prop_name in required:
            fields[prop_name] = (base, ...)
        else:
            # 契约里可选字段没有 required，但也没有显式 null 联合；
            # 响应里业务真的会给 None（例如 audit_ref 未审批时），
            # 因此注解必须是 `T | None`，否则 response_model 会把合法响应判死。
            py_type = Any if base is Any else base | None
            fields[prop_name] = (py_type, None)

    return cast(type[Any], create_model(name, **fields))
