"""描述性 allowed_principals 的运行时解析（单一解释点，漂移即拒绝）。

为什么需要这一层：
冻结契约里 `PRODUCTION.GATE.APPROVE` 的允许主体写的是「对应 Gate 授权人类职责
主体」——一句描述，不是一个角色名。通用解析把它当字面角色后，没有任何令牌能
命中，于是"动作已实现"与"动作可调用"之间出现了一条静态闸门看不见的裂缝。

本模块把这种解释收敛成一个**有原文指纹的数据绑定**：
- 只认台账里登记的动作；没登记的一律按契约原文处理（不改变既有行为）；
- 原文与契约不一致时 fail-fast，绝不"按旧解释继续放行"——那等于换了授权范围
  却没有任何人签字；
- 角色集合非空且去重，避免台账写空导致全员禁止却难以定位。

依赖方向：`action_registry` → 本模块 → `contracts/runtime`。本模块不依赖
任何服务、数据库或 Web 框架，可被契约闸门与单测直接复用。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from packages.errors.error_codes import DomainError, ErrorRegistry

_BINDINGS_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "runtime"
    / "principal_bindings.yaml"
)


@lru_cache(maxsize=1)
def load_principal_bindings() -> dict[str, dict[str, Any]]:
    """加载台账并做结构校验（损坏即 fail-fast，不返回半份可用配置）。"""
    if not _BINDINGS_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(_BINDINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # YAML 解析异常类型不固定，统一转结构化错误
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"principal_bindings 解析失败: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail="principal_bindings 顶层必须是映射",
        )
    bindings = data.get("bindings") or {}
    if not isinstance(bindings, dict):
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail="principal_bindings.bindings 必须是映射",
        )
    for action_key, entry in bindings.items():
        if not isinstance(entry, dict):
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail=f"principal_bindings 条目必须是映射: {action_key!r}",
            )
        for field in ("contract_text", "roles", "reason", "unblocks_when"):
            if not entry.get(field):
                raise DomainError(
                    ErrorRegistry.INTERNAL_ERROR,
                    detail=f"principal_bindings[{action_key}] 缺字段 {field}",
                )
        if not isinstance(entry["roles"], list) or not all(
            isinstance(role, str) and role.strip() for role in entry["roles"]
        ):
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail=f"principal_bindings[{action_key}].roles 必须是非空角色字符串列表",
            )
    return bindings


def resolve_principals_text(action_key: str, contract_text: str) -> str:
    """把契约原文解析成可机读的角色文本；未登记的动作原样返回。

    `contract_text` 必须与台账登记逐字相等。不等说明契约改了而解释没跟上：
    此时**拒绝加载**（而不是继续用旧角色），因为授权范围可能已经被契约改写。
    """
    binding = load_principal_bindings().get(action_key)
    if binding is None:
        return contract_text
    if str(binding["contract_text"]) != contract_text:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=(
                f"动作 {action_key} 的 allowed_principals 契约原文已变化，"
                "principal_bindings 解释已过期；请先更新台账（不得直接沿用旧角色）: "
                f"台账 {binding['contract_text']!r} != 契约 {contract_text!r}"
            ),
        )
    # 输出格式沿用通用解析约定（'/' 为 OR），保持 AllowedPrincipals 解析单一实现。
    return "/".join(dict.fromkeys(str(role).strip() for role in binding["roles"]))


__all__ = ["load_principal_bindings", "resolve_principals_text"]
