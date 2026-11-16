"""Action Registry 运行时加载器（授权的单一事实源）。

设计要点（P0-3 收口 + R1 单一事实源 + B2 决策化）：

位置说明：本模块放在 ``packages/identity`` 而不是 ``services/identity_access``，
因为授权是**横切能力**而不是某个领域服务的私有能力——业务服务（service 层）
也要能在拿到资源后调用授权判定，而 ``services → services`` 的直接依赖是被
R2 禁止的。契约加载本身是纯读取（无 HTTP、无数据库），天然属于 packages。

- ``contracts/action_registry/action_registry.yaml`` 是动作元数据的唯一权威源；
  本模块只读取并缓存，绝不在代码里复制角色清单或风险等级（避免二次维护造成
  权限矩阵与代码不一致）。
- **加载完整的 ActionSpec**：``scope`` / ``risk_level`` / ``token_required`` /
  ``approval_requirement`` 此前被丢弃，导致契约里 81 条动作的作用范围与风险
  等级从未被运行时使用。现在它们全部进入决策对象（见
  ``packages/identity/authorization.py``）。
- ``allowed_principals`` 的机器可读约定：``/`` 分隔多个「可选角色」（OR 语义），
  括号内为资质/范围说明（如 ``Agent（授权范围内）``），解析时剥离括号。
  含 ``+`` / ``、`` 的条目表示「多主体共同审批」（AND 语义），**不拆**：
  拆开等于把 AND 读成 OR（任意一方可单独批准），是审批链上最危险的误读。
- 未注册 action_key 一律 fail-fast 转 ``DomainError(INTERNAL_ERROR)``（结构化
  信封，绝不静默放行，也不把裸异常抛成无信封 500）。
"""

from __future__ import annotations

import re
from functools import cache, lru_cache
from pathlib import Path

import yaml

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.authorization import ActionSpec, RiskLevel

# 仓库根 → contracts/action_registry/action_registry.yaml。
# 本文件位于 <repo>/packages/identity/action_registry.py，parents[2] 即仓库根。
_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "action_registry"
    / "action_registry.yaml"
)

# 匹配全角/半角括号及其内容（资质说明），如「Agent（授权范围内）」「Worker（最小权限）」。
_PAREN_RE = re.compile(r"[（(][^（）()]*[)）]")

# 契约里必填的动作字段（缺一即视为契约损坏，拒绝启动而不是带病运行）。
_REQUIRED_FIELDS = (
    "action_key",
    "resource_type",
    "scope",
    "risk_level",
    "allowed_principals",
)


@cache
def parse_allowed_principals(text: str) -> frozenset[str]:
    """把 ``allowed_principals`` 文本解析为可选角色集合（纯函数，可单测）。

    - 按 ``/`` 切分（OR 语义）。
    - 剥离括号内资质说明：``Agent（授权范围内）`` → ``Agent``。
    - 去空白、去空项。
    - **不按 ``+`` 切分**：那是联合审批（AND），拆开就变成"任一方可单独批"。
    """
    roles: set[str] = set()
    for token in text.split("/"):
        normalized = _PAREN_RE.sub("", token).strip()
        if normalized:
            roles.add(normalized)
    return frozenset(roles)


@lru_cache(maxsize=1)
def _load_registry() -> dict[str, ActionSpec]:
    """加载并缓存 action_key -> ActionSpec 映射（fail-fast）。

    加载期所有损坏（文件缺失 / 语法错误 / 顶层非映射 / actions 非列表 /
    条目非 dict / 缺关键字段 / 风险等级非法 / 重复 action_key）一律转
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
        registry: dict[str, ActionSpec] = {}
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError(f"action_registry 存在非映射条目: {action!r}")
            missing = [field for field in _REQUIRED_FIELDS if not action.get(field)]
            if missing:
                raise ValueError(
                    f"action_registry 条目缺字段 {missing}: {action.get('action_key')!r}"
                )
            key = str(action["action_key"])
            if key in registry:
                raise ValueError(f"action_registry 存在重复 action_key: {key}")
            try:
                risk_level = RiskLevel(str(action["risk_level"]))
            except ValueError as exc:
                raise ValueError(
                    f"action_registry 风险等级非法: {key} = {action['risk_level']!r}"
                ) from exc
            principals_text = str(action["allowed_principals"])
            registry[key] = ActionSpec(
                action_key=key,
                resource_type=str(action["resource_type"]),
                scope=str(action["scope"]),
                risk_level=risk_level,
                allowed_principals=parse_allowed_principals(principals_text),
                token_required=bool(action.get("token_required", False)),
                approval_requirement=str(action.get("approval_requirement", "")),
                allowed_principals_text=principals_text,
            )
        return registry
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"action_registry 加载失败: {exc}",
        ) from exc


def action_spec(action_key: str) -> ActionSpec | None:
    """返回动作元数据；未注册返回 ``None``。

    刻意不在这里抛异常：判定函数需要把"未注册"表达成一个**可审计的拒绝决策**
    （``ACTION_UNREGISTERED``），而不是一条没有决策记录的 500。调用方
    （``services.identity_access.policy``）负责把它转成 403/500。
    """
    return _load_registry().get(action_key)


def all_action_specs() -> tuple[ActionSpec, ...]:
    """返回全部动作元数据（供契约级别的穷举测试与启动自检使用）。"""
    return tuple(_load_registry().values())


def allowed_roles(action_key: str) -> frozenset[str]:
    """返回指定 action 允许的角色集合；未注册 action_key 转结构化内部错误。

    保留给"只关心角色集合"的调用方（如测试与审计导出）；授权判定请走
    ``action_spec`` + ``decide``，不要只比角色。
    """
    spec = action_spec(action_key)
    if spec is None:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR, detail=f"未注册的 action_key: {action_key}"
        )
    return spec.allowed_principals
