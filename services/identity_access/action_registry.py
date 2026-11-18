"""Action Registry 运行时加载器（RBAC + Capability 的单一事实源）。

设计要点（P0-3 收口 + R1 单一事实源 + 防呆）：
- ``contracts/action_registry/action_registry.yaml`` 是 action 定义的唯一权威源；
  本模块只读取并缓存，绝不在代码里复制角色/能力清单（避免二次维护造成
  权限矩阵与代码不一致）。
- ``allowed_principals`` 的机器可读约定（正文 31.6 主表口径）：
    * ``+`` 分隔「会签席位」（seat），席位之间是 **AND**——每个席位必须由
      **不同真实主体**分别签署；
    * ``/`` 分隔**同一席位内**的候选角色，席位内是 **OR**——任一角色可担此席位；
    * 括号内为资质/范围说明（如 ``Agent（授权范围内）``），解析时剥离。
  优先级是 ``/`` 先结合、``+`` 后组合，因为正文里的 ``内部 QC/权利职责`` 是一个
  整体候选组：
    * ``版权/法务授权人`` → 1 个席位 {版权, 法务授权人}：单主体 OR，普通 RBAC；
    * ``交付负责人+内部 QC/权利职责`` → 2 个席位 {交付负责人} / {内部 QC, 权利职责}：
      交付负责人与（内部 QC 或 权利职责）两方会签。
  此前的实现只按 ``/`` 切分，于是 ``交付负责人+内部 QC`` 被当成**一个角色名**，
  任何主体都不可能持有它——既不是会签，也不是 OR，是一个静默失效的假配置。
  含多个席位的动作在单请求授权模型下无法完成会签，``authorize`` 会显式拒绝
  （见 ``rbac.authorize``），而不是随机不匹配。
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
def parse_principal_seats(text: str) -> tuple[frozenset[str], ...]:
    """把 ``allowed_principals`` 解析为「会签席位」序列（纯函数，可单测）。

    语法见模块 docstring：``+`` 分隔席位（AND），``/`` 分隔席位内候选角色（OR），
    ``/`` 先结合。实现顺序因此是「先按 ``+`` 切席位，再在席位内按 ``/`` 切候选」。

    返回值的长度就是会签方数：
    - 长度 1 → 单主体普通 RBAC（任一候选角色即可）；
    - 长度 ≥2 → 需要 N 个**不同主体**分别签署，单请求无法完成。

    空文本或全是空白 → 返回空 tuple（调用方 fail-closed）。
    """
    seats: list[frozenset[str]] = []
    for seat_text in text.split("+"):
        candidates: set[str] = set()
        for token in seat_text.split("/"):
            normalized = _PAREN_RE.sub("", token).strip()
            if normalized:
                candidates.add(normalized)
        # 空席位（如 "A++B" 的中间段）是契约书写错误，不能当成「无要求」吞掉。
        if candidates:
            seats.append(frozenset(candidates))
    return tuple(seats)


@cache
def parse_allowed_principals(text: str) -> frozenset[str]:
    """所有席位的候选角色并集（展示/审计投影，**不用于**授权判定）。

    保留此函数是为了让权限矩阵、诊断输出与既有断言仍能列出「这个动作涉及哪些
    角色」；授权判定一律走 :func:`parse_principal_seats`，因为并集丢掉了
    「哪些角色必须由不同主体分别担任」这一关键信息。
    """
    return frozenset().union(*parse_principal_seats(text))


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
    """返回指定 action 涉及的全部候选角色（并集投影，不表达会签要求）。

    未注册 action_key 转结构化内部错误。授权判定请用 :func:`principal_seats`。
    """
    return parse_allowed_principals(action_definition(action_key).allowed_principals)


def principal_seats(action_key: str) -> tuple[frozenset[str], ...]:
    """返回指定 action 的会签席位序列；未注册 action_key 转结构化内部错误。

    长度为 1 表示单主体即可执行（普通 RBAC）；长度 ≥2 表示需要多个不同主体
    分别签署同一动作，当前授权链不支持，``authorize`` 会显式拒绝。
    """
    return parse_principal_seats(action_definition(action_key).allowed_principals)
