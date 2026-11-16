"""审批绑定台账的运行时读取（AND/OR 语义的唯一配置源）。

与 `packages.identity.principal_bindings` 同口径：台账是数据，不是代码里的
分支；结构损坏即 fail-fast，契约原文漂移由服务层用 ActionSpec 再核对一次。
本模块只读文件，不依赖任何服务、Web 框架或数据库。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from packages.errors.error_codes import DomainError, ErrorRegistry

# 本文件位于 <repo>/packages/approval_bindings.py（packages 根，不是子目录），
# parents[1] 才是仓库根。写 parents[2] 会指到仓库外，台账静默读不到并 fail-closed。
_PATH = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "runtime"
    / "approval_bindings.yaml"
)


@dataclass(frozen=True)
class ApprovalGroupBinding:
    name: str
    roles: frozenset[str]


@dataclass(frozen=True)
class ApprovalBinding:
    action_key: str
    contract_text: str
    mode: str
    groups: tuple[ApprovalGroupBinding, ...]
    reason: str
    unblocks_when: str


@lru_cache(maxsize=1)
def load_approval_bindings() -> dict[str, ApprovalBinding]:
    if not _PATH.exists():
        return {}
    try:
        data = yaml.safe_load(_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR, detail=f"approval_bindings 解析失败: {exc}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("bindings"), dict):
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR, detail="approval_bindings 结构损坏"
        )
    result: dict[str, ApprovalBinding] = {}
    for action_key, raw in data["bindings"].items():
        raw = raw or {}
        groups = tuple(
            ApprovalGroupBinding(
                name=str(group["name"]),
                roles=frozenset(str(role) for role in group["roles"]),
            )
            for group in raw.get("groups") or []
        )
        result[str(action_key)] = ApprovalBinding(
            action_key=str(action_key),
            contract_text=str(raw.get("contract_text", "")),
            mode=str(raw.get("mode", "")),
            groups=groups,
            reason=str(raw.get("reason", "")),
            unblocks_when=str(raw.get("unblocks_when", "")),
        )
    return result


def binding_for(action_key: str) -> ApprovalBinding:
    """取目标动作的审批绑定；未登记即拒绝（fail-closed）。"""
    binding = load_approval_bindings().get(action_key)
    if binding is None:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=f"动作 {action_key} 没有审批绑定，不能发起通用审批",
        )
    return binding


__all__ = ["ApprovalBinding", "ApprovalGroupBinding", "binding_for", "load_approval_bindings"]
