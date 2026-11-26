"""安全边界不变量的**可执行断言**注册表。

为什么需要这个模块（根因，不是又一层校验）
------------------------------------------------------------
在本次整改前，``contracts/security_boundary.yaml`` 的每条不变量都靠人工填写
``status: satisfied`` 来销项，而 ``scripts/contract_check.py`` 只校验 YAML 的
结构自洽（字段齐备、id 不重复、status 枚举合法、债务子块不残留），**从不校验
``fact_source`` 指向的代码是否真的成立**。

后果是实测出来的，不是推测出来的：

* ``AUTH-1``（授权决策单一入口）标记 ``satisfied``，但组织级授权判定实际手写在
  ``services/project_planning/api/project_routes.py`` 的路由层，``episode_routes.py``
  连这段都没有；
* ``KEYS-1``（进程密钥最小权限）标记 ``satisfied``，但它的不变量文句只覆盖
  「签发私钥」，完全没覆盖「数据库凭证」，于是 identity 进程继承 postgres
  超级用户口令这件事对闸门是隐形的。

也就是说：``status`` 是自我声明，闸门只能证明「有人写了 satisfied」，不能证明
「事实成立」。第三轮、第二轮审查反复从同一条缝里挖出同类 P1，机制就在这里。

本模块的定位
------------------------------------------------------------
每条不变量改为声明 ``assertion: <id>``，由闸门**执行**注册表里的函数判定成立与否；
``status: satisfied`` 被禁止书写（只有 ``debt`` / ``partial`` 允许手写，且必须带
原因与解除条件）。这样「已满足」不再是一个可以被编辑的字符串。

两级证据，各管各的事实，不互相冒充：

1. ``assertion``（本模块）：**无需外部依赖的结构性/接线断言**——机制存在、被接线、
   在预期位置生效。它回答「这个约束有没有在代码里真实落地」。
2. ``runtime_evidence``（YAML 字段）：指向真实行为的 pytest 节点。闸门校验节点
   真实存在（防指向空气），由 CI 的 ``pytest tests`` 在真实 PostgreSQL 上执行。
   它回答「这个约束在运行时真的拦得住」。

断言函数必须返回 ``(bool, str)``：False 时第二项是给人看的失败原因（指出缺什么、
在哪），不允许返回裸 False——闸门报错信息是这条链路唯一的可读出口。

新增/修改不变量的顺序（与 security_boundary.yaml 维护规则 2 一致）：
先在本模块写好断言 → 再改 YAML 引用它 → 最后改实现让断言由红转绿。
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.shared.lifecycle import Lifecycle
from packages.shared.yaml_strict import load_yaml, load_yaml_file

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_PATH = ROOT / "contracts"

# (是否通过, 失败原因)；通过时原因为空串。
AssertionResult = tuple[bool, str]
Assertion = Callable[[], AssertionResult]

_REGISTRY: dict[str, Assertion] = {}


def assertion(assertion_id: str) -> Callable[[Assertion], Assertion]:
    """把函数登记为指定 id 的断言（模块导入即完成注册）。"""

    def _register(func: Assertion) -> Assertion:
        if assertion_id in _REGISTRY:
            raise RuntimeError(f"断言 id 重复注册: {assertion_id}")
        _REGISTRY[assertion_id] = func
        return func

    return _register


def get_assertion(assertion_id: str) -> Assertion | None:
    """按 id 取断言；未注册返回 None（由闸门报「断言未实现」）。"""
    return _REGISTRY.get(assertion_id)


def registered_ids() -> tuple[str, ...]:
    """已注册断言 id（供闸门提示可用集合，便于排查拼写错误）。"""
    return tuple(sorted(_REGISTRY))


# --------------------------------------------------------------------------- #
# 读取工具：只读源码文本 / AST，尽量不 import 业务模块（避免闸门被业务依赖拖垮）
# --------------------------------------------------------------------------- #
def _read(rel_path: str) -> str:
    path = ROOT / rel_path
    if not path.exists():
        raise FileNotFoundError(f"断言依赖的文件不存在: {rel_path}")
    return path.read_text(encoding="utf-8")


def _tree(rel_path: str) -> ast.Module:
    return ast.parse(_read(rel_path), filename=rel_path)


def _param_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = node.args
    positional = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    return {a.arg for a in positional}


def _parameter_names(rel_path: str, symbol: str) -> set[str]:
    """取函数（或类的 ``__init__``）的形参名集合。

    用于「入口不得接受某个参数」这类**结构断言**：字符串搜索会把注释、文档
    一并算进去，而这里只认真正的形参——判据才与意图一致。
    """
    for node in ast.walk(_tree(rel_path)):
        if isinstance(node, ast.ClassDef) and node.name == symbol:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and sub.name == "__init__":
                    return _param_names(sub)
            return set()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == symbol:
            return _param_names(node)
    raise AssertionError(f"{rel_path} 中找不到 {symbol}（断言目标被重命名或删除）")


def _annotated_fields(rel_path: str, class_name: str) -> set[str]:
    """取数据类里**带注解的字段名**（不含方法与派生属性）。"""
    for node in ast.walk(_tree(rel_path)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                sub.target.id
                for sub in node.body
                if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name)
            }
    raise AssertionError(f"{rel_path} 中找不到类 {class_name}")


def _string_literals(rel_path: str) -> set[str]:
    """源码中出现的**字符串字面量**集合（AST 级，注释天然不计入）。

    与 ``"x" in _read(path)`` 的区别：后者会把注释与文档里的追述也算成违规，
    判据就从「代码里还有硬编码」退化成「文档里不许提旧实现」。这里取字面量
    精确值，注释被 AST 丢弃，docstring 虽被收集但因不**等于**目标值而不命中。
    """
    return {
        node.value
        for node in ast.walk(_tree(rel_path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _module_string_lists(rel_path: str, name: str) -> list[str] | None:
    """读取模块级赋值的字符串序列（元组/列表）字面量；找不到返回 None。

    用于「列清单」类不变量：注释里提到某列不算数，必须在真实数据结构里。
    """
    for node in ast.walk(_tree(rel_path)):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = node.value
        if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            continue
        return [
            elt.value
            for elt in value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
    return None


def _dict_of_name_tuples(rel_path: str, name: str) -> dict[str, set[str]] | None:
    """读取 ``{role: ((field, env), ...)}`` 形状的模块级常量（config 的必需密钥表）。

    返回 ``{role: {field, ...}}``；形状不符返回 None，由断言报「结构不可解析」。
    """
    for node in ast.walk(_tree(rel_path)):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            return None
        table: dict[str, set[str]] = {}
        # AST 字面量字典里 keys/values 长度天然一致（同一 Dict 节点），
        # strict=True 把「不一致就该失败」写成断言，而不是靠 zip 静默截断。
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(value, ast.Tuple):
                return None
            fields = set()
            for pair in value.elts:
                if not isinstance(pair, ast.Tuple) or len(pair.elts) != 2:
                    return None
                first = pair.elts[0]
                if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                    return None
                fields.add(first.value)
            table[str(key.value)] = fields
        return table
    return None


def _require(cond: bool, message: str) -> AssertionResult:
    return (True, "") if cond else (False, message)


def _status_value(status: object) -> str:
    """状态值统一取 ``.value``；已是字符串时原样返回。

    与 ``packages/shared/lifecycle.py`` 同口径：断言比对的是「值」而不是
    「枚举对象」，否则同一份边表换个写法就会被判成不一致。
    """
    return str(getattr(status, "value", status))


def _iter_python_files(
    roots: tuple[str, ...], *, root: Path | None = None
) -> list[str]:
    """遍历若干目录下的 ``.py`` 相对路径（按字典序，保证报错顺序稳定）。"""
    base = ROOT if root is None else root
    files: list[str] = []
    for root_name in roots:
        files.extend(
            path.relative_to(base).as_posix()
            for path in sorted((base / root_name).rglob("*.py"))
        )
    return files


def _injects_organization_from_principal(rel_path: str) -> bool:
    """该文件是否把 ``principal.organization_id`` 传给 ``set_session_scope``。

    查 AST 而不是查文本：``organization_id=principal.organization_id`` 出现在
    注释或文档里也能被字符串搜索命中，而这里要的是「真的作为实参传进去」。
    """
    for node in ast.walk(_tree(rel_path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "set_session_scope":
            continue
        for keyword in node.keywords:
            if keyword.arg != "organization_id":
                continue
            value = keyword.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "organization_id"
                and isinstance(value.value, ast.Name)
                and value.value.id == "principal"
            ):
                return True
    return False


# --------------------------------------------------------------------------- #
# 1. 授权边界
# --------------------------------------------------------------------------- #
@assertion("auth.rbac_capability_enforced")
def auth_rbac_capability_enforced() -> AssertionResult:
    """统一入口对「声明 token_required 的动作」必须真的校验 Capability。

    行为断言（不是文本断言）：同一角色、同一动作，无 caps 必须被拒、有 caps 必须放行。
    只以「角色对但无能力 → 拒绝」为通过条件，因为那正是此前缺失的一环；
    「有能力 → 放行」用来排除「一律拒绝」的假通过。
    """
    from uuid import uuid4

    from packages.errors.error_codes import DomainError
    from packages.identity.principal import Principal
    from services.identity_access.action_registry import (
        _load_registry,
        principal_seats,
    )
    from services.identity_access.rbac import authorize

    probe = None
    for key, definition in _load_registry().items():
        # 只挑「单席位」动作做探针：多席位动作要求多方会签，authorize 会
        # 无条件拒绝（会签机制未实现），用它做探针会让断言假通过。
        seats = principal_seats(key)
        if definition.token_required and len(seats) == 1:
            probe = (key, definition.resource_type, sorted(seats[0])[0])
            break
    if probe is None:
        return (False, "找不到可用于探针的 token_required 动作（契约可能已改）")

    action_key, resource_type, role = probe
    tenant = uuid4()

    without_caps = Principal(
        subject_id="assertion-probe", tenant_id=tenant, roles=frozenset({role})
    )
    try:
        authorize(action_key, without_caps)
        return (
            False,
            f"角色具备但缺少 Capability 时动作 {action_key} 未被拒绝："
            "token_required 仍是死声明",
        )
    except DomainError:
        pass

    with_caps = Principal(
        subject_id="assertion-probe",
        tenant_id=tenant,
        roles=frozenset({role}),
        capabilities=frozenset({f"{resource_type}:*"}),
    )
    try:
        authorize(action_key, with_caps)
    except DomainError as exc:
        return (
            False,
            f"持有 {resource_type}:* 能力时动作 {action_key} 仍被拒绝："
            f"Capability 匹配规则与契约不一致（{exc}）",
        )
    return (True, "")


@assertion("auth.separation_enforced")
def auth_separation_enforced() -> AssertionResult:
    """职责分离必须比对真实主体，且不得误判合法审批。"""
    from packages.errors.error_codes import DomainError
    from packages.identity.separation import require_different_subject

    try:
        require_different_subject(
            actor="agent-1",
            counterpart="agent-1",
            action_label="ASSET.VERSION.APPROVE",
            actor_label="批准人",
            counterpart_label="创建人",
        )
    except DomainError:
        pass
    else:
        return (False, "同一主体自批未被拒绝：职责分离失效")

    try:
        require_different_subject(
            actor="agent-2",
            counterpart="agent-1",
            action_label="ASSET.VERSION.APPROVE",
            actor_label="批准人",
            counterpart_label="创建人",
        )
    except DomainError as exc:
        return (False, f"不同主体审批被误拒：{exc}")
    return (True, "")


@assertion("auth.countersignature_not_downgraded")
def auth_countersignature_not_downgraded() -> AssertionResult:
    """会签（多席位）语义不得被静默降级成单人授权。

    背景：``allowed_principals`` 用 ``/`` 表达「同一席位任选其一」、``+`` 表达
    「多个席位必须由不同主体分别签署」。旧解析器只按 ``/`` 切分，把
    ``交付负责人+内部 QC`` 当成一个角色名——那个角色永远不存在，于是判定永远
    否决，报错却是「缺少角色」，把「双人会签」掩盖成「权限没配好」。

    本断言要求三件事同时成立（缺一即为可被欺骗）：
    1. 契约里含 ``+`` 的动作确实被解析成 ≥2 个席位；
    2. 只持有一个席位的角色也**必须被拒绝**，且错误语义指向「会签/多主体」；
    3. 单席位动作仍能正常放行——排除「一律拒绝」造成的假通过。
    """
    from uuid import uuid4

    from packages.errors.error_codes import DomainError
    from packages.identity.principal import Principal
    from services.identity_access.action_registry import (
        _load_registry,
        principal_seats,
    )
    from services.identity_access.rbac import authorize

    tenant = uuid4()

    # 找一个真实的「多席位」动作作为探针；找不到就说明契约已无会签要求，
    # 此时本断言不适用（不算失败，但必须显式说明，不能静默通过）。
    countersign: tuple[str, frozenset[str]] | None = None
    for key, definition in _load_registry().items():
        if "+" not in definition.allowed_principals:
            continue
        seats = principal_seats(key)
        if len(seats) < 2:
            return (
                False,
                f"动作 {key} 的 allowed_principals 含 '+' 却只解析出 "
                f"{len(seats)} 个席位：会签语义已被降级",
            )
        countersign = (key, seats[0])
        break
    if countersign is None:
        return (True, "")

    action_key, first_seat = countersign
    role = sorted(first_seat)[0]
    single_seat_principal = Principal(
        subject_id="assertion-countersign",
        tenant_id=tenant,
        roles=frozenset({role}),
        capabilities=frozenset({"*"}),
    )
    try:
        authorize(action_key, single_seat_principal)
    except DomainError as exc:
        if "会签" not in exc.detail:
            return (
                False,
                f"动作 {action_key} 已拒绝但错误语义未指向会签（实际：{exc.detail}）",
            )
    else:
        return (
            False,
            f"动作 {action_key} 要求多方会签，却被单个角色 {role} 放行——"
            "会签被静默降级为单人授权",
        )

    # 反向探针：单席位动作必须仍能放行，否则「一律拒绝」也能骗过上面的检查。
    for key, definition in _load_registry().items():
        seats = principal_seats(key)
        if len(seats) == 1 and not definition.token_required:
            probe_role = sorted(seats[0])[0]
            principal = Principal(
                subject_id="assertion-single",
                tenant_id=tenant,
                roles=frozenset({probe_role}),
                capabilities=frozenset({"*"}),
            )
            try:
                authorize(key, principal)
            except DomainError as exc:
                return (False, f"单席位动作 {key} 被误拒：{exc.detail}")
            return (True, "")
    return (True, "")


@assertion("auth.single_entry_covers_resource")
def auth_single_entry_covers_resource() -> AssertionResult:
    """授权入口必须接收资源归属输入，且业务路由不得自行判定归属。

    两件事一起断言，因为它们是同一个事实的两面：入口没有资源参数，路由就只能
    手写；路由还在手写，说明入口仍未真正承担资源级判定。

    检查项：
    1. ``authorize`` 的签名包含资源形参；
    2. ``services/*/api/*.py`` 中不出现 ``organization_id`` 比较；
    3. 不出现路由层构造 ``AUTH_SCOPE_DENIED``。
    """
    signature_ok = _authorize_has_resource_param()
    offenders = _api_layer_ownership_checks()
    if not signature_ok and not offenders:
        return (
            False,
            "authorize 未接收资源参数，且路由层也没有归属判定——"
            "资源级授权环节整体缺失",
        )
    if not signature_ok:
        return (
            False,
            "authorize 仍只接收 (action_key, principal)，资源级判定无处安放；"
            f"当前由路由层手写：{', '.join(offenders)}",
        )
    if offenders:
        return (
            False,
            "授权入口已支持资源参数，但以下路由仍自行判定归属（必须收敛到统一入口）："
            f"{', '.join(offenders)}",
        )
    return (True, "")


def _authorize_has_resource_param() -> bool:
    """``authorize`` 是否声明了资源形参（名称不限，但必须是显式参数）。"""
    path = "services/identity_access/rbac.py"
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "authorize"
        ):
            params = {arg.arg for arg in node.args.args}
            params |= {arg.arg for arg in node.args.kwonlyargs}
            return bool(params & {"resource", "resource_ref", "ownership"})
    raise AssertionError(f"{path} 中找不到 authorize 定义")


_API_SCOPE_PATTERNS = (
    re.compile(r"organization_id\s*[!=]="),
    re.compile(r"_assert_organization_scope"),
    re.compile(r"AUTH_SCOPE_DENIED"),
)


def _api_layer_ownership_checks() -> list[str]:
    """扫描业务路由层，返回仍在自行做归属/授权判定的文件列表。"""
    offenders: list[str] = []
    for path in sorted((ROOT / "services").glob("*/api/*.py")):
        code_lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        if any(p.search(line) for p in _API_SCOPE_PATTERNS for line in code_lines):
            offenders.append(path.relative_to(ROOT).as_posix())
    return offenders


# 已实现状态机的登记表：对象 → 代码落点。
# 只登记 contract_check._STATE_MACHINE_SOURCES 里的四个对象：没实现状态机的
# 对象没有边表可判，硬判会得到「找不到边表」的假红。
_LIFECYCLE_SOURCES: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "Project",
        "services.project_planning.domain.state_machine",
        "PROJECT_LIFECYCLE",
        "ALLOWED_TRANSITIONS",
        "services/project_planning/domain/project.py",
        "Project",
    ),
    (
        "Episode",
        "services.project_planning.domain.state_machine",
        "EPISODE_LIFECYCLE",
        "EPISODE_ALLOWED_TRANSITIONS",
        "services/project_planning/domain/episode.py",
        "Episode",
    ),
    (
        "ScriptRevision",
        "services.project_planning.domain.state_machine",
        "SCRIPT_LIFECYCLE",
        "SCRIPT_ALLOWED_TRANSITIONS",
        "services/project_planning/domain/script.py",
        "ScriptRevision",
    ),
    (
        "AssetVersion",
        "services.asset_service.domain.state_machine",
        "ASSET_LIFECYCLE",
        "ASSET_ALLOWED_TRANSITIONS",
        "services/asset_service/domain/asset_version.py",
        "AssetVersion",
    ),
    # A13（ADR-R28）：同一条事务链上的五个对象，所有权判定逐个执行。
    (
        "Scene",
        "services.project_planning.domain.state_machine",
        "SCENE_LIFECYCLE",
        "SCENE_ALLOWED_TRANSITIONS",
        "services/project_planning/domain/scene.py",
        "Scene",
    ),
    (
        "Shot",
        "services.project_planning.domain.state_machine",
        "SHOT_LIFECYCLE",
        "SHOT_ALLOWED_TRANSITIONS",
        "services/project_planning/domain/shot.py",
        "Shot",
    ),
    (
        "ShotRevision",
        "services.project_planning.domain.state_machine",
        "SHOT_REVISION_LIFECYCLE",
        "SHOT_REVISION_ALLOWED_TRANSITIONS",
        "services/project_planning/domain/shot_revision.py",
        "ShotRevision",
    ),
    (
        "ShotBreakdownDraft",
        "services.project_planning.domain.state_machine",
        "SHOT_DRAFT_LIFECYCLE",
        "SHOT_DRAFT_ALLOWED_TRANSITIONS",
        "services/project_planning/domain/shot_draft.py",
        "ShotBreakdownDraft",
    ),
    (
        "ReworkTask",
        "services.project_planning.domain.state_machine",
        "REWORK_TASK_LIFECYCLE",
        "REWORK_TASK_ALLOWED_TRANSITIONS",
        "services/project_planning/domain/rework_task.py",
        "ReworkTask",
    ),
)


@dataclass(frozen=True)
class LifecycleSpec:
    """一个状态机的判定输入（由 wrapper 收集，纯函数只消费）。"""

    object_name: str
    lifecycle: Lifecycle[Any]  # 各域边表（共享层形态，域间无类型耦合）
    exported_allowed: object  # 模块导出的邻接集投影（契约闸门读它）
    aggregate_path: str  # 聚合根源码路径（相对 root）
    aggregate_class: str  # 聚合根类名
    registry: dict[str, str]  # action_key -> status
    contract_edges: frozenset[tuple[str, str]]  # 契约登记的 (from, to)


@assertion("lifecycle.edge_ownership")
def lifecycle_edge_ownership() -> AssertionResult:
    """状态推进的准入条件必须挂在转移边上，且**所有权被执行**（不变量 LIFECYCLE-1）。

    本断言有两个来源，覆盖的是同一件事的两个层：

    * P1-02（资产域）：证据门槛与职责分离必须长在边上。长在用例方法里，第二条
      推进路径就会漏——实测 ``change_status`` 只给 GOLD 加门槛，``APPROVED``
      没有，通用接口可绕过审批。
    * N-07（跨域）：所有权必须由聚合根**执行**，不能只是写进审计事实的字符串。
      整改前资产域执行、项目域只记录；同一句不变量两种落实方式，就有一种是假的。
      实测假的那一半：任何调用方带着看起来对的 action 就能走任意已登记的边，
      审计表记下的「谁走的」是自称而非判定。

    判据见 ``evaluate_lifecycle_edge_ownership``（纯函数，供负向测试喂构造数据，
    证明规则真的会拒绝，而不是「碰巧现在没问题」）与 ``_asset_edge_specific``
    （P1-02 的资产域回归防线）。
    """
    registry = _action_registry_statuses()
    if not registry:
        return (False, "动作注册表为空或无法读取：所有者将无从校验")

    contract_objects = _contract_state_machines()
    specs: list[LifecycleSpec] = []
    for (
        object_name,
        module_path,
        lifecycle_var,
        allowed_var,
        aggregate_path,
        aggregate_class,
    ) in _LIFECYCLE_SOURCES:
        try:
            module = _import_domain_module(module_path)
        except Exception as exc:  # pragma: no cover - 导入失败即实现损坏
            return (False, f"无法导入 {module_path}：{exc!r}")
        lifecycle = getattr(module, lifecycle_var, None)
        allowed = getattr(module, allowed_var, None)
        if lifecycle is None or allowed is None:
            return (
                False,
                f"{module_path} 缺少 {lifecycle_var} / {allowed_var}："
                "边表未收敛到共享形态，所有权无从判定",
            )
        contract_edges = contract_objects.get(object_name)
        if contract_edges is None:
            return (False, f"state_machines.yaml 中未找到 {object_name} 的转移边")
        specs.append(
            LifecycleSpec(
                object_name=object_name,
                lifecycle=lifecycle,
                exported_allowed=allowed,
                aggregate_path=aggregate_path,
                aggregate_class=aggregate_class,
                registry=registry,
                contract_edges=contract_edges,
            )
        )

    problems = evaluate_lifecycle_edge_ownership(tuple(specs), root=ROOT)
    problems.extend(_asset_edge_specific(specs))
    return _require(not problems, "；".join(problems))


def evaluate_lifecycle_edge_ownership(
    specs: tuple[LifecycleSpec, ...], *, root: Path
) -> list[str]:
    """纯判定：返回边所有权不成立的位置。

    五条判据，覆盖「形态」「事实」「执行」三层：

    1. **投影同源**：模块导出的邻接集必须等于边表派生出的投影。手写的
       ``{source: {target}}`` 与边表可以同时存在且不一致——那时契约闸门比对的是
       邻接集，边表成了没人看的第二份真相。
    2. **所有者是真动作**：每条边的 ``owner_action`` 必须在
       ``action_registry.yaml`` 里存在且状态为 ACTIVE。拼错一个字母的 action
       仍然「非空」，靠肉眼发现不了，靠它准入则等于永远拒绝。
    3. **边在契约登记**：每条边必须能在 ``state_machines.yaml`` 的该对象
       ``transitions`` 里找到（31.5：未登记转移一律拒绝；实现也不许造边）。
    4. **聚合根接收 action**：``transition_to`` 必须有 ``action`` 形参。没有它，
       所有权判定只能靠服务层「记得先查」，而「记得」不是机制。
    5. **action 真的参与判定**：``transition_to`` 必须把 ``action`` 传给
       ``edge_owned_by``。只加参数不用它，等于把准入降级成装饰——这条判据是
       「看起来已经修好了」与「真的修好了」的分界线。
    """
    problems: list[str] = []
    if not specs:
        return ["没有任何状态机被登记：断言会无条件通过，等于没有"]

    for spec in specs:
        lifecycle = spec.lifecycle
        edges = tuple(lifecycle.edges)
        if not edges:
            problems.append(f"{spec.object_name}：边表为空，所有权无从判定")
            continue

        # 判据 1：投影同源
        derived = lifecycle.allowed_transitions()
        exported = _normalize_allowed_map(spec.exported_allowed)
        if derived != exported:
            problems.append(
                f"{spec.object_name}：导出的邻接集与边表派生结果不一致"
                f"（导出 {_describe_allowed(exported)} / 派生 {_describe_allowed(derived)}）"
                " —— 存在两份真相"
            )

        for edge in edges:
            source = _status_value(edge.source)
            target = _status_value(edge.target)
            owner = edge.owner_action

            # 判据 2：所有者是真动作
            status = spec.registry.get(owner)
            if status is None:
                problems.append(
                    f"{spec.object_name} 边 {source}->{target} 的所有者 "
                    f"{owner!r} 不在动作注册表"
                )
            elif status != "ACTIVE":
                problems.append(
                    f"{spec.object_name} 边 {source}->{target} 的所有者 "
                    f"{owner!r} 状态为 {status}，不可作为准入凭据"
                )

            # 判据 3：边在契约登记
            if (source, target) not in spec.contract_edges:
                problems.append(
                    f"{spec.object_name} 边 {source}->{target} 未在 "
                    "state_machines.yaml 登记（实现不得造边，31.5）"
                )

        # 判据 4 + 5：聚合根真的用 action 判所有权
        problems.extend(
            _evaluate_aggregate_entry(
                spec.aggregate_path, spec.aggregate_class, spec.object_name, root
            )
        )

    return problems


def _asset_edge_specific(specs: Sequence[LifecycleSpec]) -> list[str]:
    """P1-02 的资产域回归防线（跨域不变量之外，资产域还有自己的具体形状）。

    三条都比「边有所有者」更具体：审批边的所有者必须正是 APPROVE 动作、
    该边必须带守卫、服务层不得再持有证据判定代码。前两条防止「边表结构对了但
    审批边被改派给通用动作」，第三条防止「守卫留在边上、又在服务里抄了一份」。
    """
    from services.asset_service.domain import state_machine as sm

    problems: list[str] = []
    approved = sm.edge_for(sm.AssetStatus.HUMAN_REVIEW, sm.AssetStatus.APPROVED)
    if approved is None:
        problems.append("资产域缺少 HUMAN_REVIEW -> APPROVED 边")
    else:
        if approved.owner_action != "ASSET.VERSION.APPROVE":
            problems.append(
                f"APPROVED 边的所有者为 {approved.owner_action}："
                "通用状态接口可绕过审批（P1-02 复发）"
            )
        if not approved.guards:
            problems.append("APPROVED 边没有任何准入守卫：审批无凭据也能通过")

    service = _read("services/asset_service/service/asset_service.py")
    leaked = [
        marker
        for marker in ("if not asset.rights_manifest_ref", "report_count == 0")
        if marker in service
    ]
    if leaked:
        problems.append(f"资产服务层仍持有证据判定代码（两套规则）: {leaked}")
    return problems


def _evaluate_aggregate_entry(
    rel_path: str, class_name: str, object_name: str, root: Path
) -> list[str]:
    """聚合根的 ``transition_to`` 必须接收并**使用** action（判据 4 + 5）。"""
    path = root / rel_path
    if not path.exists():
        return [f"{object_name}：聚合根文件不存在 {rel_path}"]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    if cls is None:
        return [f"{object_name}：{rel_path} 中未找到类 {class_name}"]
    func = next(
        (
            node
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "transition_to"
        ),
        None,
    )
    if func is None:
        return [f"{object_name}：{class_name} 未定义 transition_to"]

    params = [arg.arg for arg in func.args.args] + [
        arg.arg for arg in func.args.kwonlyargs
    ]
    if "action" not in params:
        return [
            f"{object_name}：{class_name}.transition_to 未接收 action 形参——"
            "边所有权只能靠调用方「记得先查」，那不是机制（N-07）"
        ]

    uses_action = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "edge_owned_by"
        and any(isinstance(arg, ast.Name) and arg.id == "action" for arg in node.args)
        for node in ast.walk(func)
    )
    if not uses_action:
        return [
            f"{object_name}：{class_name}.transition_to 接收 action 却未传给 "
            "edge_owned_by——参数存在但不参与准入，等于装饰（N-07）"
        ]
    return []


def _action_registry_statuses() -> dict[str, str]:
    """读动作注册表：action_key -> status（所有者必须在这里且 ACTIVE）。"""
    path = CONTRACTS_PATH / "action_registry" / "action_registry.yaml"
    if not path.exists():
        return {}
    data = load_yaml_file(path) or {}
    result: dict[str, str] = {}
    for item in data.get("actions", []) or []:
        key = item.get("action_key")
        if isinstance(key, str):
            result[key] = str(item.get("status", ""))
    return result


def _contract_state_machines() -> dict[str, frozenset[tuple[str, str]]]:
    """读契约状态机：object -> {(from, to)}。"""
    path = CONTRACTS_PATH / "state_machines" / "state_machines.yaml"
    if not path.exists():
        return {}
    data = load_yaml_file(path) or {}
    result: dict[str, frozenset[tuple[str, str]]] = {}
    for entry in data.get("objects", []) or []:
        name = entry.get("object")
        if not isinstance(name, str):
            continue
        edges = {
            (str(t["from"]), str(t["to"]))
            for t in entry.get("transitions", []) or []
            if isinstance(t, dict) and "from" in t and "to" in t
        }
        result[name] = frozenset(edges)
    return result


def _import_domain_module(module_path: str) -> object:
    """导入领域模块（先把项目根加入 sys.path，兼容 `python scripts/xxx.py`）。"""
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(module_path)


def _normalize_allowed_map(raw: object) -> dict[str, frozenset[str]]:
    """把邻接集归一化为 ``{状态值: {目标值}}``（枚举与字符串同口径）。"""
    if not hasattr(raw, "items"):
        return {}
    result: dict[str, frozenset[str]] = {}
    for source, targets in raw.items():
        result[_status_value(source)] = frozenset(
            _status_value(target) for target in targets
        )
    return result


def _describe_allowed(allowed: dict[str, frozenset[str]]) -> str:
    """邻接集的可读描述（报错用，按 source 排序保证 diff 稳定）。"""
    return (
        "{"
        + ", ".join(
            f"{source}: {sorted(targets)}"
            for source, targets in sorted(allowed.items())
        )
        + "}"
    )


# --------------------------------------------------------------------------- #
# 2. 资源归属边界
# --------------------------------------------------------------------------- #
@assertion("scope.project_organization_fk")
def scope_project_organization_fk() -> AssertionResult:
    """Project 的组织归属必须在数据库层用（organization_id, tenant_id）复合外键锁死。

    单列外键只能证明「组织存在」，不能证明「组织归属正确」（审查 P1-03 的原话）。
    因此断言必须是复合外键：迁移里两个列清单都出现 organization_id + tenant_id。
    """
    migration = "data/migrations/versions/d2e3f4a5b6c7_organization_tenant_ownership_fk.py"
    text = _read(migration)
    if "fk_projects_organization_tenant" not in text:
        return (
            False,
            f"{migration} 未建立复合外键 fk_projects_organization_tenant："
            "单列外键无法阻止跨租户组织引用",
        )
    tree = _tree(migration)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "create_foreign_key":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != "fk_projects_organization_tenant":
            continue
        col_lists = [
            arg for arg in node.args[1:] if isinstance(arg, (ast.List, ast.Tuple))
        ]
        pairs = [
            [elt.value for elt in arg.elts if isinstance(elt, ast.Constant)]
            for arg in col_lists
        ]
        expected = ["organization_id", "tenant_id"]
        if len(pairs) < 2 or pairs[0] != expected or pairs[1] != expected:
            return (
                False,
                f"复合外键列不匹配：局部列 {pairs[0] if pairs else '?'} / "
                f"引用列 {pairs[-1] if pairs else '?'}，须均为 {expected}",
            )
        return (True, "")
    return (False, f"{migration} 中找不到 fk_projects_organization_tenant 的列定义")


@assertion("scope.child_resource_fk")
def scope_child_resource_fk() -> AssertionResult:
    """Episode / ScriptRevision 对父级的归属必须由外键保证，不靠 Service 查询兜底。"""
    orm = _read("services/project_planning/repository/orm.py")
    if "ForeignKey" not in orm:
        return (False, "project_planning ORM 无任何 ForeignKey 声明")
    missing = [
        name
        for name in ("fk_episodes_project", "fk_script_revisions_episode")
        if name not in orm and name not in _all_migrations_text()
    ]
    if missing:
        return (False, f"子资源外键缺失: {missing}")

    # P1-03：Shot 的返工依据必须复合外键 (project_id, rework_basis_ref) →
    # rework_tasks(project_id, entity_id)。单列外键会放行「A 项目借用 B 项目的
    # 返工授权」。ORM 与正式迁移两条路径都查：只查一处，另一处可悄悄退回单列。
    fk_needle = '"project_id", "rework_basis_ref"'
    if fk_needle not in orm:
        return (
            False,
            "Shot.rework_basis_ref 的外键未声明 project_id 复合列——"
            "单列外键放行跨项目借用返工授权（P1-03）",
        )
    a13 = _read("data/migrations/versions/a13b0c1d2e3f_scene_shot_rework_runtime_tables.py")
    upgrade_mig = _read(
        "data/migrations/versions/b1c2d3e4f5a6_shot_rework_basis_composite_fk.py"
    )
    if "FOREIGN KEY (project_id, rework_basis_ref)" not in upgrade_mig:
        return (False, "存量升级迁移未把 fk_shots_rework_basis 改为复合外键（P1-03）")
    if (
        "uq_rework_tasks_entity_project" not in a13
        or "uq_rework_tasks_entity_project" not in upgrade_mig
    ):
        return (False, "复合外键的引用目标唯一键 uq_rework_tasks_entity_project 缺失")
    return (True, "")


@assertion("scope.asset_ownership_chain")
def scope_asset_ownership_chain() -> AssertionResult:
    """资产必须能表达「属于哪个项目」，且归属链只有一条路径。

    断言三件事（缺一不可）：
    1. AssetVersion 承载 project_id；
    2. project_id 到 project_planning.projects 有库级外键；
    3. 组织归属**不冗余落库**——只从 project 派生，避免项目换组织时资产不同步。
    """
    orm = _read("services/asset_service/repository/orm.py")
    if "project_id" not in orm:
        return (
            False,
            "AssetVersionModel 无 project_id：契约声明 scope=project/shot/asset，"
            "但模型无法表达项目归属，项目级资产授权无从判定",
        )
    if "ForeignKey" not in orm or "projects" not in orm:
        return (False, "AssetVersion.project_id 未指向 project_planning.projects 的外键")
    if "organization_id" in orm:
        return (
            False,
            "AssetVersionModel 冗余存储 organization_id：组织归属必须从 project 派生，"
            "冗余列会在项目换组织时产生第二事实源",
        )
    return (True, "")


# 组织维度 RLS 的实施迁移与「每张表该有的组织规则形态」。
# "column"：表自身带组织列，谓词必须**等于**单一事实源生成的那一段；
# "parent"：子表无组织列（组织归属从项目派生，冗余存储会产生第二事实源），
#           必须用父项目存在性表达，让组织规则经父表策略传递。
_ORG_RLS_TABLES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("project_planning", "projects", "column", ()),
    ("project_planning", "organizations", "column", ()),
    (
        "project_planning",
        "episodes",
        "parent",
        (
            "p.entity_id = project_planning.episodes.project_id",
            "p.tenant_id = project_planning.episodes.tenant_id",
        ),
    ),
    (
        "project_planning",
        "script_revisions",
        "parent",
        (
            "p.entity_id = project_planning.script_revisions.project_id",
            "p.tenant_id = project_planning.script_revisions.tenant_id",
        ),
    ),
    (
        "asset_service",
        "asset_versions",
        "parent",
        (
            "p.entity_id = asset_service.asset_versions.project_id",
            "p.tenant_id = asset_service.asset_versions.tenant_id",
        ),
    ),
    (
        "asset_service",
        "qc_reports",
        # qc_reports 没有 project_id：链是 qc_reports → asset_versions → projects，
        # 两跳都必须带自身行的关联，否则「任一跳丢条件」就会把整表子行放出去。
        "parent",
        (
            "a.entity_id = asset_service.qc_reports.asset_version_id",
            "a.tenant_id = asset_service.qc_reports.tenant_id",
            "p.entity_id = a.project_id",
            "p.tenant_id = a.tenant_id",
        ),
    ),
    # A13 的五个对象（迁移 a13b0c1d2e3f）。它们都有 project_id，所以组织归属走
    # 父项目存在性，与 episodes / script_revisions 同一形态——不冗余存
    # organization_id，项目换组织时不会留下第二事实源。
    #
    # 这五行是补漏，不是新增能力：A13 建表时没加任何策略，五张表在库层是完全敞开的
    # （业务角色可跨租户、跨组织读全部场次与镜头）。此前本元组是手写的，判据只对它
    # 列出的表求值，于是「加表」等于「漏检」——现在由 _tenant_scoped_business_tables()
    # 与真实矩阵比对，未登记的表会让本断言报红。
    *(
        (
            "project_planning",
            table,
            "parent",
            (
                f"p.entity_id = project_planning.{table}.project_id",
                f"p.tenant_id = project_planning.{table}.tenant_id",
            ),
        )
        for table in (
            "scenes",
            "shots",
            "shot_revisions",
            "shot_breakdown_drafts",
            "rework_tasks",
            # A22：规格自带 project_id，与上面五张同形态（组织经父项目传递）。
            "shot_specs",
            # A23：约束包同样自带 project_id（7.9 必填字段），形态一致。
            "constraint_packs",
        )
    ),
    # R16 三域拆分（a0b1c2d3e4f5）：五表已随域迁到各自 schema，父项目仍在
    # project_planning——谓词右值必须逐字取新 schema（相关性字符串不匹配就报红）。
    *(
        (
            schema,
            table,
            "parent",
            (
                f"p.entity_id = {schema}.{table}.project_id",
                f"p.tenant_id = {schema}.{table}.tenant_id",
            ),
        )
        for schema, table in (
            ("production_planning", "production_routes"),
            ("production_planning", "generation_tasks"),
            ("budget_quota", "shot_budget_requests"),
            ("qc_governance", "gate_decisions"),
            # Phase 3：QC 待检队列同样以父项目存在性表达组织可见性。
            ("qc_governance", "qc_tasks"),
            # A35 / ADR-R42：产物写入意图与登记回执都自带 project_id，
            # 组织可见性经父项目传递；漏登记会让这两张表的策略不被求值。
            ("production_planning", "artifact_manifests"),
            ("asset_service", "asset_registration_receipts"),
        )
    ),
    # R16：预算账本是追加式流水，无 project/org 列，按租户维度兜底隔离
    #（"tenant" 形态：仍校验 tenant 谓词与 FORCE，只是不要求父项目存在性）。
    ("budget_quota", "budget_ledger", "tenant", ()),
)


def _tenant_scoped_business_tables() -> frozenset[tuple[str, str]]:
    """真实矩阵里声明的全部业务表——本判据的**应检集合**。

    为什么不再维护一份手写表名：手写清单与真实 schema 是两个事实源，加了表而
    没加清单时，判据对那张表根本不求值，于是「漏加策略」表现为绿色。同一批改动
    里 ``test_boundary_role_grants`` 的手写快照已经因为加表而陈旧过，这里重犯了
    一次，代价是五张表的跨租户读在库层敞开。

    ``DOMAIN_GRANTS`` 本身已被 ``data.role_grants_least_privilege`` 比对过
    「ORM 有新表而矩阵没声明」，所以它到 ORM 元数据是一条被验证的链，不是又一份
    自由心证的副本。
    """
    from packages.shared.db_grants import DOMAINS, business_tables

    return frozenset(pair for domain in DOMAINS for pair in business_tables(domain))


_ORG_RLS_FORCED: tuple[tuple[str, str], ...] = (
    *((schema, table) for schema, table, _, _ in _ORG_RLS_TABLES),
    ("audit", "status_transitions"),
    ("audit", "approval_signatures"),
)


def evaluate_org_rls(
    statements: list[str],
    *,
    database_text: str,
    injects_organization: bool,
    self_injected_paths: list[str],
    tenant_tables: frozenset[tuple[str, str]] | None = None,
) -> list[str]:
    """纯判定：返回组织维度 RLS 未落地的位置（空列表 = 成立）。

    入参是**数据**而不是仓库根目录：断言的价值全在「判据能不能识别坏样本」，
    而坏样本不可能长在真实仓库里。把判定与取数分开，测试才能喂构造语句证明
    它真的会报红（与 ``evaluate_role_name_literals`` 同一手法）。

    ``statements`` 必须是**整条迁移链**的回放结果（按 upgrade 顺序），
    ``tenant_tables`` 是应检的业务表集合。两者都不再是手写清单：
    前者写死一个迁移文件时，后续迁移里的策略判定不进去；后者写死表名时，
    新增的表根本不参与求值。两种写死都会让「漏加策略」表现为绿色。
    """
    from packages.shared.rls import (
        RLS_ORGANIZATION_SETTING,
        organization_scope_predicate,
        tenant_scope_predicate,
    )

    def _table_policies(schema: str, table: str) -> dict[str, str]:
        """取某张表上**全部**存活策略（按策略名）。

        旧实现只保留「最后一条 CREATE POLICY」，把「文件顺序」当成了数据库终态：
        PostgreSQL 的多条 permissive 策略是 **OR**，restrictive 是 **AND**，
        创建顺序不决定授权语义。于是给同一张表再加一条「仅 owner 可读」策略，
        就会让闸门把隔离策略整个忘掉——那不是策略坏了，是判据算错了。

        现在按策略名建集合：``CREATE`` 加入、``DROP POLICY <name>`` 只移除同名，
        顺序置换不影响结果。匹配键是 ``ON <schema>.<table>``（DDL 定位点）并且
        要求后接空白或结束，否则 ``scenes_archive`` 会被当成 ``scenes``。
        """
        marker = f" ON {schema}.{table}"
        live: dict[str, str] = {}
        for statement in statements:
            index = statement.find(marker)
            if index < 0:
                continue
            trailing = statement[index + len(marker) : index + len(marker) + 1]
            if trailing not in ("", " "):
                continue
            if statement.startswith("CREATE POLICY "):
                name = _policy_name(statement)
                if name:
                    live[name] = statement
            elif statement.startswith("DROP POLICY "):
                name = _policy_name(statement, drop=True)
                if name:
                    live.pop(name, None)
        return live

    problems: list[str] = []

    # 判据 0：应检集合本身必须完整。
    # 这一段是全文件最重要的五行：此前 _ORG_RLS_TABLES 是手写清单，A13 加了五张
    # 表却没加清单，判据对它们根本不求值，于是「五张表零策略」报的是 PASS。表清单
    # 与真实 schema 一旦是两个事实源，「加表」就等于「漏检」——而漏检的方向恰好是
    # 不安全的一侧。
    declared = {(schema, table) for schema, table, _, _ in _ORG_RLS_TABLES}
    if tenant_tables is not None:
        for schema, table in sorted(tenant_tables - declared):
            problems.append(
                f"{schema}.{table} 是业务表但未登记组织 RLS 规则形态"
                "（未登记即漏检：它的策略不会被求值）"
            )
        for schema, table in sorted(declared - tenant_tables):
            problems.append(
                f"{schema}.{table} 登记了组织 RLS 规则形态但矩阵里没有这张表"
                "（陈旧登记：表已删或改名，判据在验一个不存在的东西）"
            )

    for schema, table, form, correlations in _ORG_RLS_TABLES:
        policies = _table_policies(schema, table)
        isolation_name = f"tenant_isolation_{table}"
        policy = policies.get(isolation_name)
        if policy is None:
            problems.append(
                f"{schema}.{table} 缺少策略 {isolation_name}"
                f"（现有策略：{sorted(policies) or '无'}）"
            )
            continue
        # 隔离策略必须对所有连接生效：只 TO 某个业务角色时，别的角色看这张表
        # 就是零策略——而零策略在 FORCE 下意味着「完全不可见」，看起来像权限
        # 收紧，实际是其它入口的功能崩塌。
        if "PUBLIC" not in _policy_grantees(policy):
            problems.append(
                f"{schema}.{table} 的隔离策略只对 {sorted(_policy_grantees(policy))} "
                "生效（应包含 PUBLIC）"
            )
        if tenant_scope_predicate() not in policy:
            problems.append(f"{schema}.{table} 策略缺租户谓词")
        if form == "column":
            if organization_scope_predicate() not in policy:
                problems.append(
                    f"{schema}.{table} 组织谓词与 organization_scope_predicate() 不一致"
                    "（手抄的副本会与单一事实源漂移）"
                )
            if RLS_ORGANIZATION_SETTING not in policy:
                problems.append(
                    f"{schema}.{table} 策略未读取组织上下文 {RLS_ORGANIZATION_SETTING}"
                )
        elif form == "tenant":
            # 无 project/org 列的业务表（预算账本）：以租户维度兜底隔离，
            # 只校验租户谓词（上面已校验）与策略名，不要求父项目存在性。
            pass
        else:
            if "EXISTS (SELECT 1 FROM" not in policy or (
                "project_planning.projects" not in policy
            ):
                problems.append(
                    f"{schema}.{table} 未用父项目存在性表达组织规则"
                    "（子表复制组织列会成为第二事实源）"
                )
            else:
                # 只出现 ``EXISTS`` 还不够：谓词必须与**本表这一行**关联
                # （``p.entity_id = <本表>.project_id``）。丢了关联的 EXISTS 是
                # 「租户里有任意一个可见项目」即真——他组织的子行会整片泄漏，
                # 而只查子串的判据照样报 PASS（评审 F2）。
                missing = [fragment for fragment in correlations if fragment not in policy]
                if missing:
                    problems.append(
                        f"{schema}.{table} 的父项目存在性未与自身行关联，缺 {missing}"
                        "（未关联的 EXISTS 会把全部他组织子行放行）"
                    )

        # 同一张表上的其它策略也要看：permissive 策略之间是 OR，一条
        # 「TO PUBLIC … USING (true)」就能把隔离策略整体旁路——只检查隔离策略
        # 本身的判据对这种形态是盲的。
        for other_name, other in sorted(policies.items()):
            if other_name == isolation_name:
                continue
            if _is_permissive_everything(other):
                problems.append(
                    f"{schema}.{table} 存在对 PUBLIC 完全放行的策略 {other_name}"
                    "（permissive 策略是 OR 关系，会旁路隔离策略）"
                )

    forced = {
        statement.removeprefix("ALTER TABLE ")
        .removesuffix(" FORCE ROW LEVEL SECURITY")
        .strip()
        for statement in statements
        if statement.startswith("ALTER TABLE ") and "FORCE ROW LEVEL SECURITY" in statement
    }
    for schema, table in _ORG_RLS_FORCED:
        if f"{schema}.{table}" not in forced:
            problems.append(f"{schema}.{table} 未 FORCE ROW LEVEL SECURITY")

    # 判据 3：业务代码不得自行注入作用域（唯一注入点是 build_tenant_uow_dependency）。
    # outbox relay 例外：它按租户枚举事件、没有组织维度（ADR-R24 §4 的设计选择）。
    if self_injected_paths:
        problems.append(
            f"业务代码自行注入 RLS 作用域（应经唯一依赖）: {self_injected_paths}"
        )

    # 判据 4：注入点必须带组织维度，且启动期验证被接线到每个业务域引擎。
    if not injects_organization:
        problems.append("build_tenant_uow_dependency 未把 principal.organization_id 注入会话")
    if database_text.count("verify_rls_enforcement(") < 2:
        problems.append(
            "database.py 启动期未对每个业务域引擎验证 RLS 生效"
            "（只定义一个不接线的检查等于没有检查）"
        )
    return problems


# 唯一的上下文注入点是 ``build_tenant_uow_dependency``；业务代码出现这三个调用
# 就是在另开一条绕过路径（多一个注入点，就多一处「以为注入了、其实没注」）。
_SCOPE_INJECTION_MARKERS: tuple[str, ...] = (
    "set_tenant_context(",
    "set_organization_context(",
    "set_session_scope(",
)


def find_self_injected_scope_paths(
    root: Path | None = None, *, roots: tuple[str, ...] = ("services", "apps")
) -> list[str]:
    """纯判定：业务代码里自行注入 RLS 作用域的文件（空列表 = 成立）。

    ``root`` 可注入，测试才能喂一个只含坏样本的临时目录证明它真的会报红；
    真实调用不传参数，扫仓库根。
    """
    base = ROOT if root is None else root
    return [
        rel
        for rel in _iter_python_files(roots, root=base)
        if any(
            marker in (base / rel).read_text(encoding="utf-8")
            for marker in _SCOPE_INJECTION_MARKERS
        )
    ]


_POLICY_MARKERS: tuple[str, ...] = ("CREATE POLICY", "FORCE ROW LEVEL SECURITY")

_POLICY_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_POLICY_TO_CLAUSE = re.compile(
    r"\bTO\s+([A-Za-z0-9_\"', ]+?)(?=\s+(?:USING|WITH\s+CHECK|AS|FOR)\b|\s*;|\s*$)"
)


def _policy_name(statement: str, *, drop: bool = False) -> str | None:
    """从 ``CREATE POLICY`` / ``DROP POLICY`` 语句里取策略名。

    只接受裸标识符：解析不出名字时返回 None，调用方会把它当成「没有这条策略」，
    报红方向是安全的一侧（少一条策略 ≠ 多一条放行）。
    """
    prefix = "DROP POLICY " if drop else "CREATE POLICY "
    if not statement.startswith(prefix):
        return None
    rest = statement[len(prefix) :]
    for optional in ("IF EXISTS ", "IF NOT EXISTS "):
        if rest.startswith(optional):
            rest = rest[len(optional) :]
    token = rest.split(" ", 1)[0].strip().strip('"')
    return token if _POLICY_IDENT.fullmatch(token) else None


def _policy_grantees(statement: str) -> frozenset[str]:
    """策略的适用角色。没有 ``TO`` 子句时 PostgreSQL 默认是 PUBLIC。"""
    match = _POLICY_TO_CLAUSE.search(statement)
    if match is None:
        return frozenset({"PUBLIC"})
    roles = {
        role.strip().strip('"').upper()
        for role in match.group(1).split(",")
        if role.strip()
    }
    return frozenset(roles or {"PUBLIC"})


def _is_permissive_everything(statement: str) -> bool:
    """``TO PUBLIC ... USING (true)``：一条策略就把整张表放开。"""
    if "RESTRICTIVE" in statement:
        return False
    if "PUBLIC" not in _policy_grantees(statement):
        return False
    normalized = re.sub(r"\s+", " ", statement)
    return "USING (true)" in normalized or "USING (TRUE)" in normalized


def _policy_statements() -> list[str]:
    """整条迁移链上所有策略相关语句，按 upgrade 顺序回放。

    候选文件由**内容**派生（含 ``CREATE POLICY`` / ``ROW LEVEL SECURITY``），
    而不是写死一个路径。写死路径时，后续迁移新建的策略永远不会进入本判据——
    A13 的五张表就是这样在「零策略」状态下通过了组织边界校验。

    顺序取自 ``_ordered_migrations()``（alembic 版本链），不是文件名序：策略会被
    后续迁移 DROP→CREATE 覆盖（``8f2a1b3c4d5e`` 建单维，``e9f0a1b2c3d4`` 升级
    为双维），顺序错了会把旧形态当成终态。

    派生是启发式的，但方向安全：若某个迁移用拼接手法造出策略语句而躲过关键词，
    结果是「找不到该表的策略」→ 报红（假阴性），而不是假通过。
    """
    collected: list[str] = []
    business_schemas = {schema for schema, _ in _tenant_scoped_business_tables()}
    for _, rel in _ordered_migrations():
        text = (ROOT / rel).read_text(encoding="utf-8")
        if not any(marker in text for marker in _POLICY_MARKERS):
            continue
        try:
            statements = _rendered_migration_statements(rel)
        except Exception as exc:
            # 只容忍「与本判据无关」的迁移。典型例子是 outbox 分区迁移 f6a7b8c9d1e2：
            # 它按库里实际的 occurred_at 范围算月份，回放时没有数据可读。
            #
            # 判据是「这份迁移涉不涉及业务 schema」——不涉及就不可能定义业务表的
            # 策略，跳过它不会漏掉任何应检项。反之，涉及业务 schema 却回放不了，
            # 说明本判据的 op 替身缺 API，必须当场报错：把「回放不了」静默降级成
            # 「没有策略」，正是上一次让五张表零策略通过的那类失真。
            if any(f"{schema}." in text for schema in business_schemas):
                raise AssertionError(
                    f"涉及业务 schema 的迁移无法回放：{rel}（{type(exc).__name__}: {exc}）"
                ) from exc
            continue
        for statement in statements:
            if statement.startswith(("CREATE POLICY ", "DROP POLICY ", "ALTER TABLE ")):
                collected.append(statement)
    return collected


@assertion("scope.org_rls_enforced")
def scope_org_rls_enforced() -> AssertionResult:
    """组织边界必须在数据库层强制，且与应用层同源、有启动期生效验证（SCOPE-4）。

    为什么不是「查迁移里有没有 organization 字样」：组织谓词在两个维度上都会
    漂移——(a) 迁移里手抄一份 SQL、规则改了不跟着改；(b) 只在某个入口注入上下文，
    没注入的入口看起来正常，只是恰好只看得见个人租户的数据。四个判据分别封死：

    1. 每张表的策略都带租户谓词，且**组织规则以正确形态存在**（有组织列的表用
       单一事实源生成的那一段，子表用父项目存在性）；
    2. ``FORCE ROW LEVEL SECURITY`` 覆盖全部受控表（防所有权转移后静默失效）；
    3. 上下文注入点唯一：业务代码（services/apps）不得自己 ``SET`` 作用域；
    4. 启动期验证真的被调用（每个业务域引擎一次），而不是只定义不接线。
    """
    problems = evaluate_org_rls(
        _policy_statements(),
        database_text=_read("packages/shared/database.py"),
        injects_organization=_injects_organization_from_principal(
            "packages/shared/rls.py"
        ),
        self_injected_paths=find_self_injected_scope_paths(),
        tenant_tables=_tenant_scoped_business_tables(),
    )
    return _require(not problems, "；".join(problems))


# --------------------------------------------------------------------------- #
# 3. 数据所有权边界
# --------------------------------------------------------------------------- #
@assertion("data.domain_role_split")
def data_domain_role_split() -> AssertionResult:
    """每个业务域独立数据库角色 + 独立连接池（缺任一项即未隔离）。

    查的是「角色名从配置解析」而不是「文件里出现某个角色名」：后者在文档字符串
    里就能满足（本断言此前正是如此），而角色名写死成字面量恰恰是要防的缺陷——
    自定义角色名的部署会建出没人用的角色、再 GRANT 到不存在的角色（评审 T5）。
    """
    migration = "data/migrations/versions/8f3b2c1d4e5a_split_domain_roles.py"
    text = _read(migration)
    missing_fields = [
        field
        for field in ("postgres_project_planning_user", "postgres_asset_service_user")
        if field not in text
    ]
    if missing_fields:
        return (
            False,
            f"迁移未从配置解析域角色名（缺 {missing_fields}）："
            "写死角色名会在自定义角色名的部署里建错角色 / GRANT 失败",
        )
    database = _read("packages/shared/database.py")
    pools = [
        name
        for name in (
            "project_planning_sessionmaker",
            "asset_service_sessionmaker",
            "production_planning_sessionmaker",
            "budget_quota_sessionmaker",
            "qc_governance_sessionmaker",
        )
        if name not in database
    ]
    if pools:
        return (False, f"database.py 未提供独立域会话工厂: {pools}（拆角色但共池等于没拆）")
    return (True, "")


# --------------------------------------------------------------------------- #
# 授权面等于动作面（DATA-6）：矩阵是声明处，收敛迁移是执行器
# --------------------------------------------------------------------------- #
_LEAST_PRIVILEGE_REVISION = "c1a2b3d4e5f6"


def _non_docstring_literals(rel_path: str) -> list[str]:
    """源码中的字符串字面量，**排除文档字符串**。

    为什么要排除：迁移的模块 docstring 会写「``GRANT … ON ALL TABLES`` 是错的」
    这类说明。把它算成违规，等价于「文档里不许提旧实现」——判据会从
    「代码里还有手写 GRANT」退化成「注释里提过 GRANT」。与
    ``evaluate_role_name_literals`` 踩过的坑同型。
    """
    tree = _tree(rel_path)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _unfrozen_converge_calls(rel_paths: list[str]) -> list[str]:
    """调用 ``converge_statements`` 但没传 ``tables=`` 快照的位置（``path:line``）。

    判据只看**调用点**，不看结果：读活矩阵的迁移在「结果」上完全正常——它生成
    的就是矩阵，语句比对当然相等。问题在时间轴上：历史迁移会随矩阵漂移，未来
    新表的建表迁移之前就有人对它 ``REVOKE ALL``，clean install 崩在一个看起来
    与授权无关的位置。这类缺陷无法用「当前是否等于矩阵」发现（评审 F1）。
    """
    found: list[str] = []
    for rel_path in rel_paths:
        path = ROOT / rel_path
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name not in ("converge_statements", "infra_converge_statements"):
                continue
            if not any(kw.arg == "tables" for kw in node.keywords):
                found.append(f"{rel_path}:{node.lineno}")
    return found


def _ordered_migrations() -> list[tuple[str, str]]:
    """``[(revision, 相对路径)]``，按 alembic 版本链的 upgrade 顺序。

    用版本链而不是文件名排序：迁移 id 是随机前缀，字典序与执行序无关。
    所有需要「按执行顺序遍历迁移」的判据都必须经这里取序，否则会出现
    「同一个链、两种顺序」——那是两个事实源，只是看起来都像对的。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    directory = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    ordered = [script.revision for script in directory.walk_revisions("base", "heads")][::-1]

    def _relative(rev: str) -> str:
        script = directory.get_revision(rev)
        return Path(str(script.path)).resolve().relative_to(ROOT).as_posix()

    return [(rev, _relative(rev)) for rev in ordered]


def _migration_chain(revision: str) -> tuple[str, list[str]]:
    """按 alembic 版本链返回 ``(该迁移的相对路径, 它之后的迁移相对路径列表)``。

    判据「此后的迁移不得手写 GRANT」必须按**执行顺序**划界，否则换成随机 id 就失效。
    用 revision id 而不是文件路径定位，则让「迁移文件被改名」不再让断言失明。
    """
    ordered = _ordered_migrations()
    revisions = [rev for rev, _ in ordered]
    if revision not in revisions:
        raise AssertionError(f"版本链里找不到 {revision}（断言目标被删除或改名）")
    index = revisions.index(revision)
    return ordered[index][1], [path for _, path in ordered[index + 1 :]]


_AUTHORIZATION_STATEMENT_PREFIXES: tuple[str, ...] = (
    "GRANT ",
    "REVOKE ",
    "ALTER DEFAULT PRIVILEGES",
)


def _is_authorization_statement(statement: str) -> bool:
    """该语句是否属于「授权面」（GRANT / REVOKE / ALTER DEFAULT PRIVILEGES）。

    授权断言只对授权面负责。建表、建索引、建 RLS 策略同样由迁移执行，但它们
    分别有 schema 对齐与 ``scope.org_rls_enforced`` 两类判据；把它们算进「授权
    语句集合」会让每一次新增 DDL 都误报「迁移多执行」。
    """
    normalized = statement.strip().upper()
    return any(
        normalized.startswith(prefix) for prefix in _AUTHORIZATION_STATEMENT_PREFIXES
    )


def evaluate_least_privilege(
    *,
    matrix_problems: list[str],
    business: dict[str, frozenset[tuple[str, str]]],
    orm_tables: dict[str, frozenset[str]],
    converge: dict[str, list[str]],
    rendered: list[str],
    grant_forbidden_literals: dict[str, list[str]],
    unfrozen_converge_calls: list[str],
    infra_converge: dict[str, list[str]] | None = None,
    auxiliary_converge: Sequence[str] = (),
) -> list[str]:
    """判定「域角色的授权面 == 矩阵声明」（纯函数，供负向用例喂构造数据）。

    五类判据，各自封一个失效模式：

    1. **矩阵覆盖**：本域 schema 的每张 ORM 表都必须在矩阵里；矩阵里声明的表
       也必须真实存在。前者防「新增表静默无权限」，后者防「表已删、矩阵还留着」
       （收敛迁移会直接失败，但那是在部署时才发现）。
    2. **迁移 == 矩阵**：回放**全部授权迁移**得到的语句，必须与矩阵生成的语句
       逐条相等。防「矩阵改了、迁移没跟上」以及「迁移里手写了一份看似相同的 DDL」。
       为什么是「全部迁移」而不只是收敛那次：迁移是时间胶囊，新表由新迁移授权，
       并集才等于矩阵（评审 F1）。
    3. **此后的迁移不得手写 GRANT**：授权只能经矩阵收敛。这条把「下一次有人
       用 ``GRANT … ON ALL TABLES`` 图省事」挡在 CI，而不是等运行库审计发现。
       大小写不敏感——SQL 关键字不分大小写，``grant`` 与 ``GRANT`` 同权（评审 F2）。
    4. **授权迁移必须声明自己的表范围**：``converge_statements`` 的调用必须显式
       传 ``tables=``。不传就是读活矩阵，历史迁移会在未来新表的建表迁移之前
       对它 ``REVOKE ALL``，clean install 直接失败（评审 F1 的复发防线）。
    5. 矩阵自身的问题（``validate_matrix``）原样上报。
    """
    problems: list[str] = list(matrix_problems)

    # 1. 覆盖（双向）。
    for domain, tables in orm_tables.items():
        declared = {table for schema, table in business.get(domain, frozenset()) if schema == domain}
        missing = sorted(tables - declared)
        if missing:
            problems.append(
                f"{domain}: ORM 表 {missing} 未在授权矩阵声明"
                "（新表要么声明最小权限，要么它根本没有写入路径）"
            )
        stale = sorted(declared - tables)
        if stale:
            problems.append(
                f"{domain}: 授权矩阵声明了不存在的表 {stale}"
                "（收敛迁移会 GRANT 失败，陈旧声明必须先删除）"
            )

    # 2. 迁移语句 == 矩阵生成。
    #
    # 比较的是**授权语句的集合**，不是迁移里出现的全部 DDL。授权迁移同时还会建表、
    # 建索引、建 RLS 策略——它们不属于「授权面」，各有专门的断言（schema 对齐、
    # scope.org_rls_enforced）。不过滤的话，任何新增 DDL 的迁移都会被报成
    # 「迁移多执行 N 条」，而那个报错的修法只能是放宽判据或删断言——方向反了。
    #
    # 之所以是**集合**而不是列表：授权收敛天然可重入（REVOKE ALL 后 GRANT，
    # 重复执行结果相同），且迁移是时间胶囊——每张新表由它自己的迁移收敛一次，
    # 同一域的 ``ALTER DEFAULT PRIVILEGES ... REVOKE`` 会在多条迁移里各出现一次。
    # 按列表比较会把「幂等语句出现了两次」误判成「迁移多执行了语句」，
    # 而真正的失效模式（少一条 GRANT、多一条矩阵没声明的 GRANT）在集合语义下
    # 一个都不会漏。
    rendered_set = {
        statement.strip()
        for statement in rendered
        if _is_authorization_statement(statement)
    }
    expected_set = {
        statement.strip()
        for statement in (
            statement
            for domain in sorted(converge)
            for statement in converge[domain]
        )
        if _is_authorization_statement(statement)
    }
    # 平台运维角色的授权（outbox_ops_role 等）也必须由矩阵收敛：它们不走
    # DOMAINS，但同样是「声明 → 迁移执行」，不能因为不是业务域就退回手写 GRANT。
    # 不并进来会出现诡异局面——迁移按矩阵执行运维角色的授权，却被判成
    # 「迁移多执行 N 条」。
    for infra in sorted(infra_converge or {}):
        expected_set |= {
            statement.strip()
            for statement in infra_converge[infra]
            if _is_authorization_statement(statement)
        }
    # 第三类授权面：既不是业务域的表级权限，也不是运维角色的收敛——维护身份对
    # 维护 schema / 守卫函数 / SECDEF 函数的授权与收紧。它们同样由 db_grants
    # 声明、由迁移执行；不并进来就会变成「迁移多执行 N 条」而被迫放宽判据。
    expected_set |= {
        statement.strip()
        for statement in auxiliary_converge
        if _is_authorization_statement(statement)
    }
    if rendered_set != expected_set:
        missing = sorted(expected_set - rendered_set)
        unexpected = sorted(rendered_set - expected_set)
        detail: list[str] = []
        if missing:
            detail.append(f"迁移少执行 {len(missing)} 条（首条：{missing[0]}）")
        if unexpected:
            detail.append(f"迁移多执行 {len(unexpected)} 条（首条：{unexpected[0]}）")
        problems.append(
            "收敛迁移的语句与授权矩阵不一致："
            + "；".join(detail)
            + "（迁移必须是矩阵的执行器，不得手写 DDL）"
        )

    # 3. 此后的迁移不得手写 GRANT（含 ALTER DEFAULT PRIVILEGES … GRANT）。
    #    SQL 关键字大小写不敏感：`grant` / `Grant` 与 `GRANT` 同权。正则若只认
    #    大写，一句小写 grant 就能让矩阵被绕过而闸门仍然 PASS（评审 F2）。
    for rel_path, literals in sorted(grant_forbidden_literals.items()):
        offenders = [
            literal.strip().splitlines()[0][:80]
            for literal in literals
            if re.search(r"(?<![A-Za-z])GRANT\b", literal, re.IGNORECASE)
        ]
        if offenders:
            problems.append(
                f"{rel_path} 手写 GRANT（应改为在 db_grants.py 声明 + converge_statements 执行）："
                f"{offenders[:3]}"
            )

    # 4. 授权迁移必须声明自己的表范围（评审 F1）。
    #    判据针对「调用点」而不是「结果」：结果看起来一样，但读活矩阵的迁移会在
    #    未来新表上先于建表执行，clean install 崩在建表迁移之前——那时报错指向
    #    REVOKE 而不是「有人把时间胶囊写成了镜像」，排障成本极高。
    for location in unfrozen_converge_calls:
        problems.append(
            f"{location} 调用 converge_statements 未传 tables= 快照："
            "历史迁移读了活矩阵，未来新增表会让 clean install 在建表前对它 REVOKE（评审 F1）"
        )

    return problems


@assertion("data.role_grants_least_privilege")
def data_role_grants_least_privilege() -> AssertionResult:
    """域角色的表级权限 == 矩阵声明，且授权只能经矩阵收敛（DATA-6 / N-06）。

    为什么不能只做「运行库实测」：实测只在有 PG 的 CI 里跑，且只反映**当前**
    数据库——迁移里再写一句 ``GRANT … DELETE``，实测要等它被应用才发现，而
    那时已经授出去了。静态判据看的是「迁移会执行什么」，能在合并前拦住。

    也不能只做静态：矩阵与真实 ACL 是否一致（部署是否被手工改过）只能实测。
    两部分合起来才是完整的证据链，缺一半都会留一个盲区。
    """
    from packages.shared.db_grants import (
        DOMAINS,
        INFRA_ROLES,
        business_tables,
        converge_statements,
        declared_privileges,
        infra_converge_statements,
        maintenance_command_statements,
        maintenance_identity_statements,
        maintenance_membership_statements,
        validate_infra_matrix,
        validate_matrix,
    )
    from packages.shared.db_roles import domain_role, infra_role
    from services.asset_service.repository import orm as asset_orm
    from services.budget_quota.repository import orm as budget_orm
    from services.production_planning.repository import orm as production_orm
    from services.project_planning.repository import orm as planning_orm
    from services.qc_governance.repository import orm as qc_orm

    # ORM 是「schema 里有哪些业务表」的权威视图：alembic check 保证它与迁移
    # 无漂移，因此用它做覆盖判据既准确又不需要连库。
    # 两个域模块都要导入：它们共用同一个 Base，只导入一个就只注册一个 schema，
    # 覆盖判据会误报「矩阵声明了不存在的表」（本地实测踩过）。
    by_schema: dict[str, set[str]] = {}
    for module in (planning_orm, production_orm, budget_orm, qc_orm, asset_orm):
        for table in module.Base.metadata.tables.values():
            by_schema.setdefault(table.schema or "", set()).add(table.name)

    converge_migration, later_migrations = _migration_chain(_LEAST_PRIVILEGE_REVISION)
    scan_paths = [converge_migration, *later_migrations]

    problems = evaluate_least_privilege(
        matrix_problems=validate_matrix() + validate_infra_matrix(),
        business={domain: business_tables(domain) for domain in DOMAINS},
        orm_tables={
            domain: frozenset(by_schema.get(domain, set())) for domain in DOMAINS
        },
        converge={
            domain: converge_statements(domain_role(domain), domain)
            for domain in DOMAINS
        },
        # 运维角色的授权同样只能经矩阵收敛（D6-9 的 outbox_ops_role）。它没有
        # 自有 schema，不能进 DOMAINS——但「不是业务域」不等于「可以手写 GRANT」。
        infra_converge={
            infra: infra_converge_statements(infra_role(infra), infra)
            for infra in INFRA_ROLES
        },
        # 回放**全部**授权迁移并取并集：迁移是时间胶囊，新表由新迁移授权，
        # 只看收敛那一次会在「矩阵加表」后误报（评审 F1）。
        rendered=[
            statement
            for path in scan_paths
            for statement in _rendered_migration_statements(path)
        ],
        grant_forbidden_literals={
            path: _non_docstring_literals(path) for path in scan_paths
        },
        unfrozen_converge_calls=_unfrozen_converge_calls(scan_paths),
        # 维护身份的授权面（WP-03）：不属任何业务域、也不是运维角色，
        # 但同样是「矩阵声明 → 迁移执行」。这里只并 upgrade 的语句：闸门回放
        # 的是 upgrade()，downgrade 的复原语句不在比对集合里（否则会要求
        # 每条 downgrade 语句都必须在 upgrade 里出现一次，语义反了）。
        auxiliary_converge=[
            *maintenance_membership_statements(),
            *maintenance_identity_statements(),
            # WP-A 第一阶段：三个只读命令 + 会话成员判定（含 owner 对 qc_tasks 的
            # 只读授权）。修复阶段的锁权限届时单独追加，不在这里提前扩张。
            *maintenance_command_statements(),
        ],
    )
    if problems:
        return (False, "；".join(problems))

    # 矩阵必须真的被「按域取值」而不是靠元组下标：这里顺带确认两个域的角色
    # 都能解析出来（配置缺失时 domain_role 会直接报错，比静默空串好）。
    declared_counts = {domain: len(declared_privileges(domain)) for domain in DOMAINS}
    return _require(
        all(count > 0 for count in declared_counts.values()),
        f"授权矩阵为空：{declared_counts}（空矩阵会让「最小权限」变成「没有权限」）",
    )


# 角色名的默认值只允许出现在 config.py（那是「默认值」的唯一定义处）。
# 其余任何位置写死角色名，都会让「按配置建的角色」与「被授权的角色」不是同一个。
_ROLE_NAME_LITERAL_ALLOWED_FILES = frozenset({"packages/shared/config.py"})
_ROLE_NAME_SCAN_ROOTS = ("services", "packages", "apps", "workers", "data/migrations")
_DEFAULT_ROLE_NAMES = frozenset(
    {
        "app_role",
        "outbox_relay_role",
        "project_planning_role",
        "asset_service_role",
        # R16 三域拆分与 D6-9 运维角色同在「按配置解析」这条判据之下：
        # 少了它们，扫描集合就只覆盖早期两个域角色，新角色写死字面量不会被拦。
        "production_planning_role",
        "budget_quota_role",
        "qc_governance_role",
        "outbox_ops_role",
    }
)

# 按 SQL 标识符边界匹配：``"GRANT ... TO app_role"`` 要拦，
# ``"app_role_name"``（读取函数名）不能误伤。
_ROLE_NAME_TOKEN = {
    role: re.compile(rf"(?<![A-Za-z0-9_]){role}(?![A-Za-z0-9_])")
    for role in _DEFAULT_ROLE_NAMES
}


@assertion("db.role_names_from_config")
def db_role_names_from_config() -> AssertionResult:
    """数据库角色名不得写成字面量（配置是唯一来源）。

    codex 评审 T5 的事实：D2 迁移把 ``project_planning_role`` /
    ``asset_service_role`` 写成了字面量，而其他迁移是从 ``Settings`` 取的。
    两类迁移混在一起时，自定义角色名的部署会在升级路径上失败——这类失效读代码
    看不出来（代码看起来完全正常），只能在闸门里按「字面量」这个形态去拦。

    判据：``services`` / ``packages`` / ``apps`` / ``workers`` / ``data/migrations``
    下的 Python 源码里，**非文档字符串**的字符串常量不得等于任一默认角色名。
    文档字符串允许（讲清「谁是谁」必须能直呼其名），代码里的字符串不允许
    （那里应当调用 ``packages/shared/db_roles.py``）。
    """
    problems = evaluate_role_name_literals(ROOT)
    return _require(not problems, "；".join(problems))


def evaluate_role_name_literals(root: Path) -> list[str]:
    """纯判定：返回把默认角色名写进代码（非文档字符串）的位置。"""
    problems: list[str] = []
    scanned = 0
    for root_name in _ROLE_NAME_SCAN_ROOTS:
        for path in sorted((root / root_name).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in _ROLE_NAME_LITERAL_ALLOWED_FILES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            scanned += 1
            docstrings = _docstring_node_ids(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstrings:
                    continue
                for role in _DEFAULT_ROLE_NAMES:
                    if not _ROLE_NAME_TOKEN.get(role).search(node.value):
                        continue
                    problems.append(f"{rel}: 字符串字面量含默认角色名 {role!r}")
    if scanned == 0:
        problems.append("未扫描到任何源码：断言等价于通过")
    return problems


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """收集模块/类/函数文档字符串的节点 id（这些位置的字符串不参与判定）。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


@assertion("data.cross_domain_via_outbox")
def data_cross_domain_via_outbox() -> AssertionResult:
    """跨域写必须走 outbox（同事务），且服务间禁止直接 import 对方实现。"""
    writer = _read("packages/shared/outbox_writer.py")
    if "session" not in writer:
        return (False, "OutboxWriter 未绑定调用方 session：无法保证业务写与事件同事务")
    arch = _read("scripts/architecture_check.py")
    if "R2" not in arch:
        return (False, "architecture_check 缺少 R2（服务间直连）闸门")
    return (True, "")


@assertion("data.relay_column_grants")
def data_relay_column_grants() -> AssertionResult:
    """Relay 只持投递元数据的列级 UPDATE，不得改事件正文。"""
    guard = "packages/shared/partition_guard.py"
    columns = _module_string_lists(guard, "RELAY_UPDATE_COLUMNS")
    if columns is None:
        return (False, f"{guard} 未定义 RELAY_UPDATE_COLUMNS：列级授权无事实源")
    forbidden = {"payload", "event_type", "tenant_id", "aggregate_type", "aggregate_id"}
    leaked = sorted(forbidden & set(columns))
    if leaked:
        return (False, f"RELAY_UPDATE_COLUMNS 含事件业务列 {leaked}：改正文仍被允许")
    if not set(columns) <= {"published_at", "attempts", "next_attempt_at", "dead_lettered_at"}:
        return (False, f"RELAY_UPDATE_COLUMNS 出现未登记列: {columns}")
    migration = "data/migrations/versions/b9e1c2d3f4a5_relay_column_level_update.py"
    if "UPDATE" not in _read(migration).upper():
        return (False, f"{migration} 未包含列级 UPDATE 授权收口")
    return (True, "")


# --------------------------------------------------------------------------- #
# 4. 进程密钥 / 凭证边界
# --------------------------------------------------------------------------- #
@assertion("keys.per_process_secrets")
def keys_per_process_secrets() -> AssertionResult:
    """签发私钥只出现在身份进程；超级用户口令不得进入业务进程。

    后者是上一版遗漏的半边：KEYS-1 的文句只写了「签发私钥」，于是「API/Relay
    持 postgres 超级用户口令」在闸门里完全没有形状。这里一并断言。
    """
    config = "packages/shared/config.py"
    table = _dict_of_name_tuples(config, "_PRODUCTION_REQUIRED_SECRETS_BY_PROCESS")
    if table is None:
        return (False, f"{config} 中 _PRODUCTION_REQUIRED_SECRETS_BY_PROCESS 结构不可解析")

    private_key = "identity_private_key_pem"
    holders = sorted(role for role, fields in table.items() if private_key in fields)
    if holders != ["identity"]:
        return (False, f"签发私钥的必需进程应为 ['identity']，实际 {holders}")

    superuser = "postgres_password"
    holders = sorted(role for role, fields in table.items() if superuser in fields)
    # outbox_maintenance 需要建分区/ATTACH PARTITION，属 DDL 例外（与迁移同性质），
    # 已在 credential_scope/CRED-1 的例外清单中登记；业务进程一律不得持有。
    if holders != ["outbox_maintenance"]:
        return (
            False,
            f"超级用户口令的必需进程应为 ['outbox_maintenance']（DDL 例外），实际 {holders}",
        )
    return (True, "")


@assertion("keys.process_scoped_validation")
def keys_process_scoped_validation() -> AssertionResult:
    """进程入口必须显式声明 process_role，启动检查按进程分列执行。"""
    config = _read("packages/shared/config.py")
    if "get_process_settings" not in config:
        return (False, "config.py 未提供 get_process_settings（进程级配置入口）")
    entrypoints = [
        "workers/identity/app.py",
        "workers/relay/__main__.py",
        "workers/outbox_maintenance/__main__.py",
    ]
    missing = [ep for ep in entrypoints if "get_process_settings" not in _read(ep)]
    return _require(
        not missing, f"以下进程入口未声明 process_role（会回落成 api 语义）: {missing}"
    )


@assertion("keys.physical_separation")
def keys_physical_separation() -> AssertionResult:
    """签发模块只能被身份进程 import，API 层被架构闸门禁止。"""
    arch = _read("scripts/architecture_check.py")
    if "R4" not in arch:
        return (False, "architecture_check 缺少 R4（apps 不得 import 签发模块）")
    identity_app = _read("workers/identity/app.py")
    if "jwks" not in identity_app.lower():
        return (False, "workers/identity 未提供 JWKS 端点：验签方拿不到公钥")
    return (True, "")


@assertion("keys.kid_content_addressed")
def keys_kid_content_addressed() -> AssertionResult:
    """kid 必须是公钥内容指纹（RFC 7638），各方零配置地算出同一个值。

    为什么这是不变量而不是实现细节：kid 是验签方索引公钥的唯一键。
    若 kid 是配置项或随机串，签发方与验签方就各自持有一份「同一事实的副本」，
    副本漂移即全平台验签失败；若 kid 是固定字面量（曾经的 "current"），
    多把密钥同名会静默覆盖，轮换根本无法表达。

    行为断言（不是查注释）：
    1. 签发侧生成的 kid 必须等于从公钥独立算出的指纹；
    2. 关键模块不得再出现字面量 kid 赋值；
    3. 验签方必须自己从公钥派生（而非接收传入的 kid）。
    """
    from packages.identity.keys import generate_signing_key, public_key_thumbprint

    key = generate_signing_key()
    if key.kid != public_key_thumbprint(key.public_pem):
        return (False, "签发侧 kid 不等于公钥指纹（kid 不再是内容派生）")

    # 检查的是**代码里的字符串字面量**，不是文本包含：注释里追述「此前写成
    # "current"」属于历史说明，不应报红；只有真正参与赋值的字面量才是违规。
    hardcoded = [
        path
        for path in (
            "services/identity_issuance/__init__.py",
            "services/identity_access/__init__.py",
            "packages/identity/bootstrap.py",
        )
        if "current" in _string_literals(path)
    ]
    if hardcoded:
        return (False, f"仍存在字面量 kid 赋值（多密钥会同名覆盖）: {hardcoded}")

    # 参数面断言（评审 P2 的教训）：kid 只要能作为形参传入，调用方就能给公钥
    # 贴一个与内容无关的标签——令牌头写 A、验签方按指纹索引 B，令牌全部拒收。
    # 上一版把 kid 的「缺省值」改成指纹就算修完了，可入口仍然接受 kid，
    # 于是这个缺口从「写死」变成了「可绕过」。根治是让入口**没有**这个形参。
    offenders = [
        f"{path}::{name}"
        for path, name in (
            ("packages/identity/keys.py", "generate_signing_key"),
            ("packages/identity/keys.py", "load_signing_key"),
            ("packages/identity/keys.py", "public_jwk_from_pem"),
            ("packages/identity/jwks.py", "PublicKeyStore"),
        )
        if "kid" in _parameter_names(path, name)
    ]
    if offenders:
        return (False, f"以下入口仍可传入 kid（标签可与公钥内容不一致）: {offenders}")

    # kid 必须是 SigningKey 的派生属性而不是可赋值字段，否则「构造时传错」
    # 依然是一个合法状态。
    if "kid" in _annotated_fields("packages/identity/keys.py", "SigningKey"):
        return (False, "SigningKey 仍把 kid 作为可赋值字段（应改为公钥指纹派生属性）")

    # 验签侧必须自己从公钥算索引键，而不是接收外部标签。
    if "public_key_thumbprint" not in _read("packages/identity/jwks.py"):
        return (False, "验签仓储未从公钥派生 kid：签发方与验签方会各自维护 kid 副本")
    return (True, "")


@assertion("cred.role_names_distinct")
def cred_role_names_distinct() -> AssertionResult:
    """域角色名必须互不相同（否则模块级角色隔离在配置层静默失效）。

    行为断言：把两个域配成同名，配置构造必须失败。只检查「有没有校验代码」
    不够——要证明它真的会拒绝。
    """
    from packages.shared.config import Settings

    try:
        Settings(
            postgres_project_planning_user="dup_role",
            postgres_asset_service_user="dup_role",
        )
    except Exception:
        return (True, "")
    return (
        False,
        "把 project_planning / asset_service 配成同名角色时配置未拒绝："
        "DATA-1 的角色隔离可被一个环境变量抹掉",
    )


# 每进程允许持有的数据库环境变量白名单（唯一事实源）。
# 新增服务必须在此登记，否则 CRED-1 直接报红——闸门防的正是「新进程默认继承全套凭证」。
# 例外只有两类，且都是 DDL/DBA 性质，不是业务进程：
#   migrate / outbox-maintenance 需要建表、建分区、ATTACH PARTITION，必须持有表属主
#   权限（当前即超级用户）。它们已在此显式登记，不靠散落注释解释。
PROCESS_ALLOWED_DB_ENV: dict[str, frozenset[str]] = {
    "postgres": frozenset({"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}),
    "migrate": frozenset(
        {
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB",
        }
    ),
    "api": frozenset(
        {
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_DB",
            "POSTGRES_PROJECT_PLANNING_USER",
            "POSTGRES_PROJECT_PLANNING_PASSWORD",
            "POSTGRES_ASSET_SERVICE_USER",
            "POSTGRES_ASSET_SERVICE_PASSWORD",
            # R16 三域（a0b1c2d3e4f5）：API 需要这三个域角色才能按域注入 UoW。
            # 不加进白名单，compose 一旦注入就报「继承未授权数据库凭证」——
            # 这条判据的意图是「每个进程只拿到自己该连的角色」，不是「不许扩展域」。
            "POSTGRES_PRODUCTION_PLANNING_USER",
            "POSTGRES_PRODUCTION_PLANNING_PASSWORD",
            "POSTGRES_BUDGET_QUOTA_USER",
            "POSTGRES_BUDGET_QUOTA_PASSWORD",
            "POSTGRES_QC_GOVERNANCE_USER",
            "POSTGRES_QC_GOVERNANCE_PASSWORD",
        }
    ),
    # 只投递事件：仅 relay 角色。
    "relay": frozenset(
        {
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_DB",
            "POSTGRES_RELAY_USER",
            "POSTGRES_RELAY_PASSWORD",
        }
    ),
    # 只签发 token / 分发公钥：不碰数据库，一条数据库变量都不该有。
    "identity": frozenset(),
    # 缓存进程：同样一条数据库变量都不该有。登记空集不是形式主义——它让
    # 「这个单元的数据库凭证面是零」成为被闸门核对过的事实，而不是没人看过。
    "redis": frozenset(),
    # DDL 运维作业（分区维护）：与 migrate 同性质的显式例外。
    "outbox-maintenance": frozenset(
        {
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB",
        }
    ),
}


def load_compose_services() -> dict[str, dict]:
    """解析 ``infra/compose/docker-compose.yml``，返回 ``{服务名: spec}``。

    闸门与测试共用同一个解析入口：判定规则只写一份，测试才能证明「闸门看的
    就是真实编排文件」，而不是证明「闸门自己跟自己对得上」。
    """
    data = load_yaml(_read("infra/compose/docker-compose.yml"), source="docker-compose.yml")
    if not isinstance(data, dict):
        raise AssertionError("docker-compose.yml 顶层不是映射")
    return data.get("services") or {}


def resolve_service_env(spec: dict) -> dict[str, object]:
    """解析服务的 environment（``<<: *anchor`` 已由严格 loader 的 merge 语义合并）。"""
    env = spec.get("environment") or {}
    if isinstance(env, list):  # "KEY=VALUE" 形式
        parsed = {}
        for item in env:
            key, _, value = str(item).partition("=")
            parsed[key] = value
        return parsed
    if not isinstance(env, dict):
        raise AssertionError(f"environment 形状无法解析: {type(env).__name__}")
    return env


def evaluate_process_credential_surface(services: dict[str, dict]) -> list[str]:
    """纯判定：返回问题列表（空列表即通过）。

    判定规则只有一条——**先查登记，再比集合**：
    服务未在 ``PROCESS_ALLOWED_DB_ENV`` 登记即失败（新增进程不可能默认继承全套
    凭证），登记了则 env 里的 ``POSTGRES_*`` 必须落在允许集合内。
    反向也查：登记了却不存在的服务同样报错，避免白名单腐化成僵尸条目。
    """
    problems: list[str] = []
    for name, spec in services.items():
        allowed = PROCESS_ALLOWED_DB_ENV.get(name)
        if allowed is None:
            problems.append(f"{name}: 未在 PROCESS_ALLOWED_DB_ENV 登记允许的数据库凭证")
            continue
        env = resolve_service_env(spec)
        unexpected = sorted(
            key for key in env if key.startswith("POSTGRES_") and key not in allowed
        )
        if unexpected:
            problems.append(f"{name}: 继承未授权数据库凭证 {unexpected}")
    missing_declared = sorted(set(PROCESS_ALLOWED_DB_ENV) - set(services))
    if missing_declared:
        problems.append(f"声明了不存在的服务: {missing_declared}")
    return problems


@assertion("cred.process_credential_surface")
def cred_process_credential_surface() -> AssertionResult:
    """每个部署单元只能拿到运行时真实使用的凭证。

    这条断言直接解析 ``infra/compose/docker-compose.yml``：
    不解析就等于又在相信文档。判定规则由 ``PROCESS_ALLOWED_DB_ENV`` 声明，
    与编排文件同源比对（新增服务未登记即失败）。
    """
    problems = evaluate_process_credential_surface(load_compose_services())
    return _require(not problems, "；".join(problems))


def _all_migrations_text() -> str:
    """全部迁移文件文本拼接（用于「某约束是否在任一迁移中建立」的查找）。"""
    parts = []
    for path in sorted((ROOT / "data/migrations/versions").glob("*.py")):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


# 状态推进的唯一入口白名单：文件 → 允许调用 transition_to 的函数。
# 只有这两个函数能推进状态；新增第三个即报红（ADR-R23 §D2-b）。
_TRANSITION_ENTRY_WHITELIST: dict[tuple[str, str], str] = {
    ("services/asset_service/service/asset_service.py", "_advance"): "资产域唯一推进点",
    ("services/project_planning/service/transition.py", "advance_status"): "项目域状态推进点",
    # 合并自分叉归属链：Shot 修订确认时状态不变、但版本指针前移，事实表有
    # from_status <> to_status 约束，只能写事件不能伪造迁移事实，故与
    # advance_status 分列。落库仍走同一审计入口，不是绕过白名单的新写法。
    (
        "services/project_planning/service/transition.py",
        "save_versioned_entity",
    ): "项目域非状态版本变更唯一落库点",
}

# 状态字段名。改状态有两种写法（调用领域方法 / 直接赋值 self.status），
# 断言必须覆盖两种，否则「方法名」被覆盖而「状态被改写」没被覆盖（评审 T1）。
STATUS_ATTR = "status"

# 租约字段：状态之外的第二组「写错就静默失去 fencing」的列。
#
# 为什么它们必须进同一道闸门
# --------------------------
# 只保护 ``status`` 是不够的：一个 Worker 只要能在领域方法之外改写
# ``lease_owner`` / ``lease_expires_at`` / ``lease_generation``，就能把「我不再
# 持有租约」改写成「我还持有」，而且事实行、事件、投影全都不会报错。三个字段
# 分别守不同的东西：owner 是身份、expires 是有效期、generation 是代次；
# 缺任一个都能拼出一条能通过校验的假租约。
LEASE_ATTRS: frozenset[str] = frozenset(
    {"lease_owner", "lease_expires_at", "lease_generation"}
)

# 领域层「改状态」的唯一方法名。新增一个方法名不该让断言失效——所以真正的
# 兜底是下面的落库规则：改状态想生效就必须落库，落库点只有审计入口。
_STATUS_MUTATORS = frozenset({"transition_to"})

# 落库调用：状态变更要产生效果必须经过它。把落库点钉在审计入口，等于把
# 「改了但没留痕」这条路的物理入口封死（评审 T1 之后的加固）。
_PERSIST_METHOD = "save"

# 事实行的唯一写入口。
_FACT_ENTRY = "advance_status"


@assertion("data.transition_single_entry")
def data_transition_single_entry() -> AssertionResult:
    """状态推进只能从每域唯一的入口发生（ADR-R23 §D2-b，不变量 DATA-4）。

    判据与理由见 ``evaluate_transition_entries``（纯函数，供测试喂构造目录，
    证明规则真的会拒绝，而不是「碰巧现在没问题」）。
    """
    problems = evaluate_transition_entries(ROOT)
    return _require(not problems, "；".join(problems))


def evaluate_transition_entries(root: Path) -> list[str]:
    """纯判定：扫描 ``root`` 下的业务代码，返回违反「唯一推进入口」的位置。

    为什么查**调用点**而不是查函数是否存在：事实表的完整性依赖「有推进 ⇒ 有事实行」。
    只要有人新增第 N 条路径、在服务方法里直接 ``transition_to``，事实行就静默缺失——
    状态改了、事件发了、审计查不到，而没有任何闸门会红。

    判据有三条，从三个物理入口封堵「改了状态却没写事实」：

    1. **改状态**：``transition_to`` 只允许出现在 ``/domain/`` 内（聚合自己的
       委托）或 ``_TRANSITION_ENTRY_WHITELIST``（每域唯一推进点）。
    2. **直接赋值**：``self.status = ...`` / ``setattr(self, "status", ...)`` 只允许
       出现在 ``/domain/`` 下名为 ``transition_to`` 的函数里。剧本的
       ``request_lock`` / ``approve_lock`` 原本就是在这里直接赋值——审批语义最
       关键的两条转移反而没有事实行，而旧断言只查方法名，看不见。
    3. **落库**：``.save(...)`` 只允许出现在白名单入口里（实测全仓服务层只有
       两处，都在入口内）。状态变了但不落库等于没变；这条把「先改状态、再自己
       save」这条绕行路封死，而且不依赖记住哪个方法名会改状态。

    为什么不用「白名单逐步添加领域方法」：那会把断言变成一张越写越长的审批名单。
    第 1、3 条按**位置**判定，新增领域方法无需改断言，绕过入口才会红。
    """
    calls_outside: list[str] = []
    assigns_outside: list[str] = []
    lease_assigns_outside: list[str] = []
    saves_outside: list[str] = []
    scanned = 0
    for root_name in ("services", "packages", "apps"):
        for path in sorted((root / root_name).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            scanned += 1
            in_domain = "/domain/" in rel
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                audited_entry = (rel, node.name) in _TRANSITION_ENTRY_WHITELIST
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        func = sub.func
                        if not isinstance(func, ast.Attribute):
                            if _is_self_status_setattr(sub):
                                assigns_outside.append(f"{rel}::{node.name}（setattr）")
                            if _is_setattr_of_lease(sub):
                                lease_assigns_outside.append(
                                    f"{rel}::{node.name}（setattr）"
                                )
                            continue
                        if in_domain or audited_entry:
                            continue
                        if func.attr in _STATUS_MUTATORS:
                            calls_outside.append(f"{rel}::{node.name}")
                        elif func.attr == _PERSIST_METHOD:
                            saves_outside.append(f"{rel}::{node.name}")
                    elif isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        targets = (
                            sub.targets
                            if isinstance(sub, ast.Assign)
                            else [sub.target]
                        )
                        # 租约字段：只允许在 ``/domain/`` 内改写（聚合自己的租约
                        # 方法）。服务层、仓储、Worker、路由任何一处直接赋值都
                        # 意味着「这条写入没经过 fencing 判定」。
                        if not in_domain and any(
                            _is_attr_of(t, LEASE_ATTRS) for t in targets
                        ):
                            lease_assigns_outside.append(f"{rel}::{node.name}")
                        if not any(_is_self_status_attr(t) for t in targets):
                            continue
                        if node.name != "transition_to" or not in_domain:
                            assigns_outside.append(f"{rel}::{node.name}")

    problems: list[str] = []
    if calls_outside:
        problems.append(
            "在唯一推进入口之外调用 transition_to：" + "、".join(sorted(set(calls_outside)))
        )
    if assigns_outside:
        problems.append(
            "在领域 transition_to 之外直接改写 self.status（事实行会静默缺失）："
            + "、".join(sorted(set(assigns_outside)))
        )
    if lease_assigns_outside:
        problems.append(
            "在 /domain/ 之外直接改写租约字段（lease_owner / lease_expires_at / "
            "lease_generation 必须只由领域方法维护，否则 fencing 判定被旁路）："
            + "、".join(sorted(set(lease_assigns_outside)))
        )
    if saves_outside:
        problems.append(
            f"在唯一推进入口之外落库（改状态即可绕过事实行与 {_FACT_ENTRY}）："
            + "、".join(sorted(set(saves_outside)))
        )
    if not _TRANSITION_ENTRY_WHITELIST:
        problems.append("推进入口白名单为空：断言会无条件通过，等于没有")
    if scanned == 0:
        problems.append("未扫描到任何源码：断言等价于通过")
    return problems


def _is_self_status_attr(node: ast.expr) -> bool:
    """是否为 ``self.status`` 这个赋值目标。"""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == STATUS_ATTR
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _is_attr_of(node: ast.expr, names: frozenset[str]) -> bool:
    """``<任意对象>.<names 之一>``：租约字段可能挂在 ``self``、``task``、``row``。"""
    return isinstance(node, ast.Attribute) and node.attr in names


def _is_setattr_of_lease(call: ast.Call) -> bool:
    """``setattr(任意对象, "lease_*", ...)``。

    只看第二个实参（属性名字面量）：第一个实参是**谁**都可以（``task``、
    ``row``、``self``），它甚至可以是普通名字而不是属性访问——按对象形态
    过滤会让 ``setattr(task, ...)`` 这种最常见的写法整条漏掉。
    """
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "setattr"
        and len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value in LEASE_ATTRS
    )


def _is_self_status_setattr(call: ast.Call) -> bool:
    """``setattr(self, "status", ...)``：同一件事的另一种写法，必须同罪。"""
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "setattr"
        and len(call.args) >= 2
        and _is_self_status_attr(call.args[0])
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value == STATUS_ATTR
    )


class _EmptyScalarResult:
    """忠实模拟 SQLAlchemy ``Result.scalars()`` 的返回：支持 .all()/.first()/迭代。

    迁移里的幂等守卫会用 ``bind.execute(...).scalars().all()`` 判断约束是否存在；
    真实 SQLAlchemy 的 ``scalars()`` 返回 ScalarResult 而非 list。此前 mock 返回裸
    list，导致任何写法正确的迁移在回放时崩在 ``'list' object has no attribute
    'all'``——闸门把 mock 的 API 失真报成「迁移无法回放」，指向错误的方向。
    """

    def all(self) -> list[object]:
        return []

    def first(self) -> None:
        return None

    def __iter__(self):
        return iter(())


class _EmptyResult:
    """空查询结果：迁移里的「先查后判」在回放时一律得到空集。"""

    def scalars(self) -> _EmptyScalarResult:
        return _EmptyScalarResult()

    def all(self) -> list[object]:
        return []

    def one(self) -> None:
        return None

    def first(self) -> None:
        """空结果的首行为 None——迁移里的 `if row is None` 判据据此走「不存在」分支。"""
        return None


class _EmptyBind:
    """迁移动态查库时的假连接。

    回放迁移是为了看它**会执行什么 DDL**，不是为了拿真实数据；查询给空集，
    判据（如「有没有未声明的表」）在回放中自然不触发。若这里返回自记录对象，
    ``SELECT`` 文本会混进语句列表，导致「生成的 DDL」与「实际执行」对不上。
    """

    def execute(self, *args: object, **kwargs: object) -> _EmptyResult:
        return _EmptyResult()


class _SqlCollector:
    """假的 alembic ``op``：只记录 ``execute()`` 的 SQL，不连数据库。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: object, *args: object, **kwargs: object) -> None:
        self.statements.append(str(sql))

    def get_bind(self) -> _EmptyBind:
        return _EmptyBind()

    # 结构类 DDL 在本断言里不需要 SQL 文本：这里判定的是权限/安全语句，
    # 建表语句由另一条判据（schema 对齐）负责。显式实现而不是 __getattr__ 兜底，
    # 是为了让「迁移调用了没被支持的 op」仍然是一个错误，而不是静默跳过。
    def f(self, name: str) -> str:
        """alembic 的 ``op.f()``：标记「该名字已按约定定型，不再套用命名约定」。

        回放时按原样返回——我们要的正是迁移真正传给数据库的那个名字。
        缺这个方法时，任何正确使用了 ``op.f`` 的迁移都会让回放崩在 AttributeError，
        而闸门把「崩了」报成「无法加载迁移」，指向错误的方向（查文件路径而不是查 API）。
        Alembic 的 16 处 ``index=True`` 生成索引名都带 schema 前缀，``op.f`` 是标准写法。
        """
        return name

    def create_table(self, *args: object, **kwargs: object) -> None:
        return None

    def create_index(self, *args: object, **kwargs: object) -> None:
        return None

    def create_unique_constraint(self, *args: object, **kwargs: object) -> None:
        return None

    def drop_table(self, *args: object, **kwargs: object) -> None:
        return None

    def drop_index(self, *args: object, **kwargs: object) -> None:
        return None

    def drop_constraint(self, *args: object, **kwargs: object) -> None:
        return None

    # 下列结构类 op 同样不需要 SQL 文本，但它们**必须**被显式支持：组织 RLS 判据
    # 要回放整条迁移链，而链上任何一份迁移用到未实现的 op 都会让回放崩在
    # AttributeError——闸门把崩溃报成「无法加载迁移」，把排查方向指向文件路径。
    # 只支持「当前链上出现过」的 op 是不够的（加一份迁移就要改这里），所以按
    # alembic 的结构类 DDL 接口补齐：它们都不产生需要判定的权限/安全语句。
    def add_column(self, *args: object, **kwargs: object) -> None:
        return None

    def drop_column(self, *args: object, **kwargs: object) -> None:
        return None

    def alter_column(self, *args: object, **kwargs: object) -> None:
        return None

    def create_foreign_key(self, *args: object, **kwargs: object) -> None:
        return None

    def create_check_constraint(self, *args: object, **kwargs: object) -> None:
        return None

    def create_primary_key(self, *args: object, **kwargs: object) -> None:
        return None


def _rendered_migration_statements(
    rel_path: str, *, direction: str = "upgrade"
) -> list[str]:
    """回放一份迁移的 ``upgrade()``（或 ``downgrade()``），返回语句列表。

    与 ``_rendered_migration_sql`` 同源（后者只是本函数的拼接结果）。需要按语句
    判定时用本函数：把多条 DDL 拼成一个字符串再按行切，会随迁移的排版变化而失效。

    ``direction="downgrade"`` 用来验证「回滚只撤销自己动过的东西」——回滚把上游
    迁移建立的安全属性一并摘掉时，正向断言看不出来（评审 F1）。
    """
    import importlib.util

    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location("_probe_migration", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法加载迁移：{rel_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    collector = _SqlCollector()
    module.op = collector  # type: ignore[attr-defined]
    getattr(module, direction)()
    return list(collector.statements)


def _rendered_migration_sql(rel_path: str) -> str:
    """回放一份迁移的 ``upgrade()``，返回它真正会执行的 SQL 文本。

    为什么回放而不是读源码：迁移普遍用 f-string 拼 DDL（schema/表名是常量），
    源码里根本不存在 ``CREATE TABLE audit.status_transitions`` 这样的字面量。
    读源码只能匹配到片段，回放才能匹配到语句——后者才是「数据库实际收到了什么」。
    """
    return "\n".join(_rendered_migration_statements(rel_path))



@assertion("data.transition_fact_table")
def data_transition_fact_table() -> AssertionResult:
    """迁移事实表的结构与守卫必须真实存在（ADR-R23 §D2-a，不变量 DATA-5）。

    只查「表存在」是不够的：本表的价值全在约束上——唯一约束（并发双写不可能）、
    CHECK（no-op 不是迁移）、提交时事件校验（有迁移必有事件）、TRUNCATE 守卫
    （整表清空不触发行级触发器）、FORCE RLS（所有者之外也受策略约束）。
    少任何一条，表还在，但不变量已经不在。
    """
    # 迁移用 f-string 拼 DDL，源码文本里没有渲染后的字面量；直接回放 upgrade()
    # 捕获真实执行的 SQL——断言必须看「跑出来的语句」，而不是「源码长什么样」。
    text = _rendered_migration_sql(
        "data/migrations/versions/d8e9f0a1b2c3_audit_status_transitions.py"
    )
    required = {
        "audit schema": "CREATE SCHEMA IF NOT EXISTS audit",
        "事实表": "CREATE TABLE audit.status_transitions",
        "版本唯一约束": "uq_status_transition_version",
        "no-op 禁止": "ck_status_transition_advances",
        "行级 append-only": "BEFORE UPDATE OR DELETE ON audit.status_transitions",
        "整表清空守卫": "BEFORE TRUNCATE ON audit.status_transitions",
        "提交时事件校验": "AFTER INSERT ON audit.status_transitions",
        "延迟到提交": "DEFERRABLE INITIALLY DEFERRED",
        "RLS FORCE": "FORCE ROW LEVEL SECURITY",
        "既有 qc_reports TRUNCATE 守卫": "BEFORE TRUNCATE ON asset_service.qc_reports",
        "既有 asset_versions TRUNCATE 守卫": "BEFORE TRUNCATE ON asset_service.asset_versions",
    }
    missing = [label for label, needle in required.items() if needle not in text]
    if missing:
        return (False, "迁移事实表缺少结构性保证：" + "、".join(missing))

    model = (ROOT / "packages/shared/status_transition.py").read_text(encoding="utf-8")
    if '__tablename__ = "status_transitions"' not in model:
        return (False, "ORM 映射未指向 status_transitions：规则与持久化不同源")
    if "SCHEMA_NAME = \"audit\"" not in model:
        return (False, "ORM 映射未落在 audit schema")
    return (True, "")



@assertion("data.accumulative_countersign")
def data_accumulative_countersign() -> AssertionResult:
    """累积式会签事实表的结构性保证 + 凑齐判定单一入口（ADR-R45 / DATA-7）。

    四层判据，缺一即红：
    1. 迁移回放文本含全部结构保证（表/唯一约束/三态 CHECK/席位 CHECK/三层守卫/
       提交时事件校验/RLS FORCE）——只查表存在不够，价值全在约束上；
    2. ORM 映射同源（表名 + schema + 约束名与迁移逐字一致）；
    3. 凑齐判定是唯一纯函数且被服务调用；
    4. AST 白名单：签署事实的写入点只在 countersign 服务（测试目录除外）——
       写入点一旦散开，「绕过判定直接插 SIGNED 行」就静默放宽会签。
    """
    text = _rendered_migration_sql(
        "data/migrations/versions/e4f5a6b7c8d9_audit_approval_signatures.py"
    )
    required = {
        "事实表": "CREATE TABLE audit.approval_signatures",
        "签署唯一约束": "uq_signature_event",
        "三态 CHECK": "ck_signature_kind",
        "席位正数 CHECK": "ck_signature_seat_positive",
        "行级 append-only": "BEFORE UPDATE OR DELETE ON audit.approval_signatures",
        "整表清空守卫": "BEFORE TRUNCATE ON audit.approval_signatures",
        "提交时事件校验": "AFTER INSERT ON audit.approval_signatures",
        "延迟到提交": "DEFERRABLE INITIALLY DEFERRED",
        "RLS FORCE": "FORCE ROW LEVEL SECURITY",
        "事件校验跨租户同宽": "AND tenant_id = NEW.tenant_id",
    }
    missing = [label for label, needle in required.items() if needle not in text]
    if missing:
        return (False, "会签事实表缺少结构性保证：" + "、".join(missing))

    model = (ROOT / "packages/shared/approval_signature.py").read_text(encoding="utf-8")
    if '__tablename__ = "approval_signatures"' not in model:
        return (False, "ORM 映射未指向 approval_signatures：规则与持久化不同源")
    if 'SCHEMA_NAME = "audit"' not in model:
        return (False, "ORM 映射未落在 audit schema")
    for constraint in ("uq_signature_event", "ck_signature_kind", "ck_signature_seat_positive"):
        if f'name="{constraint}"' not in model:
            return (False, f"ORM 缺约束名 {constraint}：autogenerate 会漂移")

    rules = (ROOT / "packages/shared/countersign_rules.py").read_text(encoding="utf-8")
    if "def evaluate_countersignature(" not in rules:
        return (False, "凑齐判定纯函数缺失：判据无单一事实源")
    service = (ROOT / "services/identity_access/countersign.py").read_text(encoding="utf-8")
    if "evaluate_countersignature(" not in service:
        return (False, "签署服务未调用唯一凑齐判据：判定与写入分裂")
    if "principal_seats(" not in service:
        return (False, "席位集合未从 Action Registry 派生：手写席位清单会腐化")

    # AST 白名单：除测试外，只有签署服务可构造签署事实 ORM。
    allowed_writer = "services/identity_access/countersign.py"
    writers: list[str] = []
    for root_name in ("services", "packages", "apps", "workers"):
        base = ROOT / root_name
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel == allowed_writer:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "ApprovalSignatureModel"):
                    writers.append(rel)
    if writers:
        return (False, "签署事实写入点越界（须只在 countersign 服务）：" + "、".join(sorted(set(writers))))
    return (True, "")
