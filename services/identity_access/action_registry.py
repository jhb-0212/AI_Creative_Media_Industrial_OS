"""Action Registry 运行时加载器（RBAC + Capability 的单一事实源）。

设计要点（P0-3 收口 + R1 单一事实源 + 防呆）：
- ``contracts/action_registry/action_registry.yaml`` 是 action 定义的唯一权威源；
  本模块只读取并缓存，绝不在代码里复制角色/能力清单（避免二次维护造成
  权限矩阵与代码不一致）。
- ``allowed_principals`` 的机器可读约定：``/`` 分隔多个「可选角色」（OR 语义），
  括号内为资质/范围说明（如 ``Agent（授权范围内）``），解析时剥离括号。
  含 ``+`` 的条目表示「多主体共同审批」，不是单一角色，简单 RBAC 不匹配
  （fail-closed；这类 CRITICAL 动作后续由 ABAC/双人审批落地）。
- ``token_required`` / ``scope`` 是 Capability 授权的契约字段：加载器不再丢弃
  它们，而是随 action 定义一起缓存，供统一授权决策入口消费（F02 根治——
  让「契约声明了 token_required」与「运行时真的校验了」对齐，而非死声明）。
- 未注册 action_key 一律 fail-fast 转 ``DomainError(INTERNAL_ERROR)``（结构化
  信封，绝不静默放行，也不把裸异常抛成无信封 500）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ActionDefinition:
    """一条 action 的完整运行时定义（RBAC + Capability 判定所需字段）。

    从 ``action_registry.yaml`` 的条目投影而来；只保留运行时授权真正消费的
    字段，避免把整个 YAML 条目（含 audit_event / negative_test_desc 等声明
    性字段）塞进运行时热点路径。
    """

    action_key: str
    resource_type: str
    scope: str
    token_required: bool
    allowed_principals: str


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
def _load_registry() -> dict[str, ActionDefinition]:
    """加载并缓存 action_key -> ActionDefinition 映射（fail-fast）。

    加载期所有损坏（文件缺失 / 语法错误 / 顶层非映射 / actions 非列表 /
    条目非 dict / 缺关键字段 / 重复 action_key）一律转
    ``DomainError(INTERNAL_ERROR)``（结构化信封，与模块 docstring 承诺一致），
    绝不裸抛 FileNotFoundError / ValueError / TypeError 冒泡成无信封 500。
    """
    try:
        if not _CONTRACT_PATH.exists():
            raise FileNotFoundError(f"契约缺失: {_CONTRACT_PATH}")
        data = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(
                f"action_registry 解析结果不是映射: {type(data).__name__}"
            )
        actions = data.get("actions")
        if not isinstance(actions, list):
            raise ValueError(
                f"action_registry.actions 必须是列表，实际: {type(actions).__name__}"
            )
        registry: dict[str, ActionDefinition] = {}
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError(
                    f"action_registry 存在非映射条目: {action!r}"
                )
            key = action.get("action_key")
            principals = action.get("allowed_principals")
            if not key or not principals:
                raise ValueError(
                    f"action_registry 存在缺关键字段的条目: {action!r}"
                )
            if key in registry:
                raise ValueError(f"action_registry 存在重复 action_key: {key}")
            registry[key] = ActionDefinition(
                action_key=key,
                resource_type=str(action.get("resource_type", "")),
                scope=str(action.get("scope", "")),
                token_required=bool(action.get("token_required", False)),
                allowed_principals=str(principals),
            )
        return registry
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"action_registry 加载失败: {exc}",
        ) from exc


def action_definition(action_key: str) -> ActionDefinition:
    """返回指定 action 的完整运行时定义；未注册转结构化内部错误。"""
    definition = _load_registry().get(action_key)
    if definition is None:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR, detail=f"未注册的 action_key: {action_key}"
        )
    return definition


def allowed_roles(action_key: str) -> frozenset[str]:
    """返回指定 action 允许的角色集合；未注册 action_key 转结构化内部错误。"""
    return parse_allowed_principals(action_definition(action_key).allowed_principals)
