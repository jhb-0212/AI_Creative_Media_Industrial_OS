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
import re
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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


@assertion("lifecycle.edge_ownership")
def lifecycle_edge_ownership() -> AssertionResult:
    """状态推进的准入条件必须挂在转移边上，且边有唯一所有者。

    这是 P1-02 的结构性判据（而不是「有没有校验证据」这种用例级断言）：
    只要准入条件长在用例方法里，第二条推进路径就会漏；只有长在边上才不可能漏。
    """
    from services.asset_service.domain import state_machine as sm

    edges = getattr(sm, "ASSET_TRANSITIONS", None)
    if not edges:
        return (False, "状态机没有结构化边表 ASSET_TRANSITIONS（仍只有邻接集）")

    unowned = [f"{e.source.value}->{e.target.value}" for e in edges if not e.owner_action]
    if unowned:
        return (False, f"以下边没有所有者（任何动作都能推进）: {unowned}")

    approved = sm.edge_for(sm.AssetStatus.HUMAN_REVIEW, sm.AssetStatus.APPROVED)
    if approved is None:
        return (False, "缺少 HUMAN_REVIEW -> APPROVED 边")
    if approved.owner_action != "ASSET.VERSION.APPROVE":
        return (
            False,
            f"APPROVED 边的所有者为 {approved.owner_action}："
            "通用状态接口可绕过审批（P1-02 复发）",
        )

    if not approved.guards:
        return (False, "APPROVED 边没有任何准入守卫：审批无凭据也能通过")

    # 邻接集必须是边表的投影，否则两份结构会漂移（契约闸门读的是邻接集）。
    derived = {
        status: frozenset(
            edge.target for edge in edges if edge.source is status
        )
        for status in sm.AssetStatus
    }
    if derived != dict(sm.ASSET_ALLOWED_TRANSITIONS):
        return (
            False,
            "ASSET_ALLOWED_TRANSITIONS 与边表不一致（两份结构已漂移）",
        )

    # 服务层不得再持有证据规则：规则只在 guards 里。
    service = _read("services/asset_service/service/asset_service.py")
    leaked = [
        marker
        for marker in ("if not asset.rights_manifest_ref", "report_count == 0")
        if marker in service
    ]
    if leaked:
        return (False, f"服务层仍持有证据判定代码（两套规则）: {leaked}")
    return (True, "")


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
    return _require(not missing, f"子资源外键缺失: {missing}")


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


# --------------------------------------------------------------------------- #
# 3. 数据所有权边界
# --------------------------------------------------------------------------- #
@assertion("data.domain_role_split")
def data_domain_role_split() -> AssertionResult:
    """每个业务域独立数据库角色 + 独立连接池（缺任一项即未隔离）。"""
    migration = "data/migrations/versions/8f3b2c1d4e5a_split_domain_roles.py"
    text = _read(migration)
    missing = [
        name
        for name in ("project_planning_role", "asset_service_role")
        if name not in text
    ]
    if missing:
        return (False, f"迁移未定义域角色: {missing}")
    database = _read("packages/shared/database.py")
    pools = [
        name
        for name in ("project_planning_sessionmaker", "asset_service_sessionmaker")
        if name not in database
    ]
    if pools:
        return (False, f"database.py 未提供独立域会话工厂: {pools}（拆角色但共池等于没拆）")
    return (True, "")


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

    if "public_key_thumbprint" not in _read("services/identity_access/__init__.py"):
        return (False, "验签方未从公钥派生 kid：签发方与验签方会各自维护 kid 副本")
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
    import yaml

    data = yaml.safe_load(_read("infra/compose/docker-compose.yml"))
    if not isinstance(data, dict):
        raise AssertionError("docker-compose.yml 顶层不是映射")
    return data.get("services") or {}


def resolve_service_env(spec: dict) -> dict[str, object]:
    """解析服务的 environment（``<<: *anchor`` 已被 yaml.safe_load 合并）。"""
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
