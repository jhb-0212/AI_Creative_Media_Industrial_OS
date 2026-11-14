"""Action Registry 运行时加载器（RBAC 的单一事实源）。

设计要点（P0-3 收口 + R1 单一事实源 + 防呆）：
- ``contracts/action_registry/action_registry.yaml`` 是 ``allowed_principals``
  的唯一权威源；本模块只读取并缓存，绝不在代码里复制角色清单（避免二次
  维护造成权限矩阵与代码不一致）。
- ``allowed_principals`` 的机器可读约定：``/`` 分隔多个「可选角色」（OR 语义），
  括号内为资质/范围说明（如 ``Agent（授权范围内）``），解析时剥离括号。
  含 ``+`` 的条目表示「多主体共同审批」，不是单一角色，简单 RBAC 不匹配
  （fail-closed；这类 CRITICAL 动作后续由 ABAC/双人审批落地）。
- 未注册 action_key 一律 fail-fast 转 ``DomainError(INTERNAL_ERROR)``（结构化
  信封，绝不静默放行，也不把裸异常抛成无信封 500）。
"""

from __future__ import annotations

import re
from functools import cache, lru_cache
from pathlib import Path

import yaml

from packages.errors.error_codes import DomainError, ErrorRegistry

# 仓库根 → contracts/action_registry/action_registry.yaml。
# 本文件位于 <repo>/services/identity_access/action_registry.py，
# parents[2] 即仓库根。
_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "action_registry"
    / "action_registry.yaml"
)

# 匹配全角/半角括号及其内容（资质说明），如「Agent（授权范围内）」「Worker（最小权限）」。
_PAREN_RE = re.compile(r"[（(][^（）()]*[)）]")


@cache
def parse_allowed_principals(text: str) -> frozenset[str]:
    """把 ``allowed_principals`` 文本解析为可选角色集合（纯函数，可单测）。

    - 按 ``/`` 切分（OR 语义）。
    - 剥离括号内资质说明：``Agent（授权范围内）`` → ``Agent``。
    - 去空白、去空项。
    """
    roles: set[str] = set()
    for token in text.split("/"):
        normalized = _PAREN_RE.sub("", token).strip()
        if normalized:
            roles.add(normalized)
    return frozenset(roles)


@lru_cache(maxsize=1)
def _load_registry() -> dict[str, str]:
    """加载并缓存 action_key -> allowed_principals 映射（fail-fast）。"""
    if not _CONTRACT_PATH.exists():
        raise FileNotFoundError(f"契约缺失: {_CONTRACT_PATH}")
    data = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    registry: dict[str, str] = {}
    for action in data.get("actions", []):
        key = action.get("action_key")
        principals = action.get("allowed_principals")
        if not key or not principals:
            raise ValueError(f"action_registry 存在缺关键字段的条目: {action!r}")
        if key in registry:
            raise ValueError(f"action_registry 存在重复 action_key: {key}")
        registry[key] = principals
    return registry


def allowed_roles(action_key: str) -> frozenset[str]:
    """返回指定 action 允许的角色集合；未注册 action_key 转结构化内部错误。"""
    principals = _load_registry().get(action_key)
    if principals is None:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR, detail=f"未注册的 action_key: {action_key}"
        )
    return parse_allowed_principals(principals)
