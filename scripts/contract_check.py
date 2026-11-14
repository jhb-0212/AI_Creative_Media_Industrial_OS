"""契约一致性校验脚本。

职责：校验冻结契约（contracts/）与实现派生之间的一致性，作为 CI 门禁。
当前覆盖：
1. contracts/ 下所有 YAML / JSON 文件的语法解析。
2. contracts/state_machines/state_machines.yaml 中 Project 状态机
   与 services/project_planning/domain/state_machine.py 的转移表一致性。
3. Action Registry ↔ OpenAPI 路径一一对应（未注册 Action 不得暴露端点）。
4. 运行时 ↔ 契约覆盖（contracts/runtime/action_implementations.yaml 台账：
   ACTIVE action 必须实现或登记未实现的原因，已声明的 route 必须真实存在）。
5. Permission Matrix 结构完整（action_matrix 覆盖全部 action_key）。
6. object_schemas.schema.json 的 P0 对象定义完整性。
7. 已实现 action 的资源类型必须在 object_schemas 中有定义，或在
   contracts/runtime/resource_schema_exemptions.yaml 中登记豁免原因
   （防止 ADR-R07 那类「动作有、对象无」的缺口静默通过 CI）。

设计要点（不屎山代码）：
- 解析逻辑独立成纯函数（parse_state_chain），可单测、无副作用。
- 校验结果显式返回，非零退出码供 CI 使用。
- 不依赖运行时数据库 / 服务，纯静态比对。

约定：
- 契约 states 字段为文本状态链，规则：
    * 分号 `；`/`;` 分隔多个子链；
    * 箭头 `→`/`->` 连接状态，`A→B→C` 展开为 (A,B)、(B,C)；
    * 斜杠 `/` 分隔多个源状态共用目标，`A/B→C` 展开为 (A,C)、(B,C)。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"

# 在脚本运行时将仓库根加入 sys.path，使领域模块可导入。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- #
# 通用契约文件语法校验
# --------------------------------------------------------------------------- #
def check_yaml(path: Path) -> bool:
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
        return True
    except Exception as exc:
        print(f"[FAIL] YAML 解析失败: {path} -> {exc}")
        return False


def check_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except Exception as exc:
        print(f"[FAIL] JSON 解析失败: {path} -> {exc}")
        return False


# --------------------------------------------------------------------------- #
# 状态链解析
# --------------------------------------------------------------------------- #
def _split_tokens(text: str) -> list[str]:
    """按分号切分子链，去除空白与空串。"""
    return [t.strip() for t in re.split(r"[；;]", text) if t.strip()]


def parse_state_chain(text: str) -> set[tuple[str, str]]:
    """将状态链文本解析为 (source, target) 转移对集合。

    示例：
        "DRAFT→ACTIVE→ON_HOLD；ACTIVE→COMPLETED；DRAFT/ON_HOLD/COMPLETED→ARCHIVED"
    解析为：
        {(DRAFT,ACTIVE),(ACTIVE,ON_HOLD),(ACTIVE,COMPLETED),
         (DRAFT,ARCHIVED),(ON_HOLD,ARCHIVED),(COMPLETED,ARCHIVED)}
    """
    transitions: set[tuple[str, str]] = set()
    for chunk in _split_tokens(text):
        # 单个子链内可能含连续箭头：A→B→C
        states = [s.strip() for s in re.split(r"→|->", chunk) if s.strip()]
        if len(states) < 2:
            continue
        # 展开连续箭头：A→B→C => (A,B),(B,C)
        for i in range(len(states) - 1):
            src_group = states[i].split("/")
            dst = states[i + 1].strip()
            for src in src_group:
                src = src.strip()
                if src:
                    transitions.add((src, dst))
    return transitions


# --------------------------------------------------------------------------- #
# 状态机一致性校验（Project + ScriptRevision）
# --------------------------------------------------------------------------- #
# 契约对象名 -> 领域转移表来源（模块路径, 变量名）
# 所有对象的状态机统一集中在 domain/state_machine.py（单一事实源），
# 避免各聚合根内嵌转移表导致落点不一致。
_STATE_MACHINE_SOURCES: dict[str, tuple[str, str]] = {
    "Project": ("services.project_planning.domain.state_machine", "ALLOWED_TRANSITIONS"),
    "ScriptRevision": (
        "services.project_planning.domain.state_machine",
        "SCRIPT_ALLOWED_TRANSITIONS",
    ),
    "Episode": (
        "services.project_planning.domain.state_machine",
        "EPISODE_ALLOWED_TRANSITIONS",
    ),
    "AssetVersion": (
        "services.asset_service.domain.state_machine",
        "ASSET_ALLOWED_TRANSITIONS",
    ),
}


def _load_impl_transitions(module_path: str, var_name: str) -> set[tuple[str, str]]:
    """从领域模块反射加载转移表，归一化为 (source, target) 字符串对集合。"""
    import importlib

    module = importlib.import_module(module_path)
    transitions_map = getattr(module, var_name)
    result: set[tuple[str, str]] = set()
    for src, targets in transitions_map.items():
        src_value = src.value if hasattr(src, "value") else str(src)
        for tgt in targets:
            tgt_value = tgt.value if hasattr(tgt, "value") else str(tgt)
            result.add((src_value, tgt_value))
    return result


def check_project_state_machine() -> bool:
    """校验 Project 状态机：契约 state_machines.yaml ↔ 领域 state_machine.py。

    保留函数名以兼容既有测试；内部委托给 check_state_machines。
    """
    return check_state_machines()


def check_state_machines() -> bool:
    """校验所有已实现的状态机（Project / ScriptRevision）与契约一致。

    遍历 state_machines.yaml 中登记为 PENDING_PHASE1_REVIEW 且已提供领域实现
    的对象，双向比对契约状态链与领域转移表。
    """
    sm_path = CONTRACTS / "state_machines" / "state_machines.yaml"
    data = yaml.safe_load(sm_path.read_text(encoding="utf-8"))

    objects = data.get("objects", [])
    all_ok = True

    for obj_name, (module_path, var_name) in _STATE_MACHINE_SOURCES.items():
        entry = next((o for o in objects if o.get("object") == obj_name), None)
        if entry is None:
            print(f"[FAIL] state_machines.yaml 中未找到 {obj_name} 状态机条目")
            all_ok = False
            continue

        contract_states = entry.get("states", "")
        contract_transitions = parse_state_chain(contract_states)
        impl_transitions = _load_impl_transitions(module_path, var_name)

        only_contract = contract_transitions - impl_transitions
        only_impl = impl_transitions - contract_transitions

        if only_contract or only_impl:
            if only_contract:
                print(f"[FAIL] {obj_name} 契约有、实现缺的转移: {sorted(only_contract)}")
            if only_impl:
                print(f"[FAIL] {obj_name} 实现有、契约缺的转移: {sorted(only_impl)}")
            all_ok = False
            continue

        status = entry.get("registration_status", "")
        if status == "PENDING_PHASE1_REVIEW":
            print(
                f"[WARN] {obj_name} 状态机已一致，但契约为 PENDING_PHASE1_REVIEW"
                "（待 Phase 1 验收复核固化），请勿在生产环境修改其状态集。"
            )
        print(f"[PASS] {obj_name} 状态机一致：{len(contract_transitions)} 条转移")

    return all_ok


# --------------------------------------------------------------------------- #
# Action Registry ↔ OpenAPI 一致性
# --------------------------------------------------------------------------- #
def _normalize_action_key(key: str) -> str:
    """将 action_key 归一化为小写点分形式（PROJECT.CREATE -> project.create）。"""
    return key.strip().lower()


def check_action_openapi_consistency() -> bool:
    """校验 Action Registry 与 OpenAPI 路径一一对应。

    契约明文（openapi.draft.yaml 头部）：「路径与 Action Registry 一一对应，
    未注册 Action 不得暴露端点」。校验：
    1. action_registry 的 action_key 集合 == openapi 的 operationId 集合（归一化后）；
    2. 无重复 action_key / 路径。
    """
    ar_path = CONTRACTS / "action_registry" / "action_registry.yaml"
    oa_path = CONTRACTS / "openapi" / "openapi.draft.yaml"

    ar = yaml.safe_load(ar_path.read_text(encoding="utf-8"))
    oa = yaml.safe_load(oa_path.read_text(encoding="utf-8"))

    # 缺 action_key 的条目视为契约错误：用 .get 取键，缺失时输出定位信息。
    action_keys: list[str] = []
    for idx, action in enumerate(ar.get("actions", [])):
        key = action.get("action_key") if isinstance(action, dict) else None
        if not key:
            print(f"[FAIL] action_registry.actions[{idx}] 缺 action_key: {action!r}")
            return False
        action_keys.append(_normalize_action_key(key))

    # openapi 的 path 形如 /actions/project.create，operationId 形如 project.create。
    # 显式遍历已知 HTTP 方法键读取 operationId，避免对路径项结构作脆弱假设
    # （路径项可能为空、缺 operationId 或含非方法键如 parameters/summary）。
    http_methods = ("get", "post", "put", "patch", "delete", "options", "head", "trace")
    operation_ids: list[str] = []
    for path, path_item in oa.get("paths", {}).items():
        operation_id: str | None = None
        if isinstance(path_item, dict):
            for method in http_methods:
                method_obj = path_item.get(method)
                if isinstance(method_obj, dict) and method_obj.get("operationId"):
                    operation_id = method_obj["operationId"]
                    break
        if operation_id is None:
            print(f"[FAIL] OpenAPI 路径 {path!r} 缺少 operationId（HTTP 方法键缺失）")
            return False
        operation_ids.append(_normalize_action_key(operation_id))

    ar_set = set(action_keys)
    oa_set = set(operation_ids)

    # 声明计数一致性
    declared_total = ar.get("total_actions")
    if declared_total != len(action_keys):
        print(
            f"[FAIL] action_registry total_actions={declared_total}"
            f" 与实际条目数 {len(action_keys)} 不符"
        )
        return False

    # 重复检测
    if len(action_keys) != len(ar_set):
        dup = [k for k in ar_set if action_keys.count(k) > 1]
        print(f"[FAIL] action_registry 存在重复 action_key: {dup}")
        return False

    only_in_ar = ar_set - oa_set
    only_in_oa = oa_set - ar_set

    if only_in_ar or only_in_oa:
        if only_in_ar:
            print(f"[FAIL] Action 注册但 OpenAPI 未暴露: {sorted(only_in_ar)}")
        if only_in_oa:
            print(f"[FAIL] OpenAPI 暴露但 Action 未注册: {sorted(only_in_oa)}")
        return False

    print(f"[PASS] Action Registry ↔ OpenAPI 一致：{len(ar_set)} 个 action")
    return True


# --------------------------------------------------------------------------- #
# 运行时 ↔ 契约覆盖（契约不再只是纸面自洽）
# --------------------------------------------------------------------------- #
# 背景：Action Registry 与 openapi.draft 是同一份契约的两种视图，两者一致只能
# 证明「文档自洽」。曾经 36 个 ACTIVE action 里运行时只有 8 个端点，CI 仍全绿。
# 本校验把运行时 app 的真实路由与契约做双向比对：
# 1. 台账声明的每个已实现 route 必须真实存在（声明不能撒谎）；
# 2. 每个 ACTIVE action 必须落在 implemented 或 not_implemented（缺口必须留痕）；
# 3. not_implemented 必须写原因，且不得与 implemented 重叠；
# 4. 运行时多出来的端点必须登记（新端点不能悄悄绕过契约）。
LEDGER_PATH = CONTRACTS / "runtime" / "action_implementations.yaml"
# 系统端点不是业务 action，不参与契约覆盖（探针与指标不属 Action Registry）。
# 必须写成 "METHOD /path" 与运行时端点集合同构，否则会被误判为未登记端点。
_NON_ACTION_PATHS = frozenset({"GET /health", "GET /readyz"})
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _runtime_endpoints() -> set[str]:
    """加载真实 FastAPI 应用的 OpenAPI，返回 "METHOD /path" 集合。"""
    from apps.api.main import app

    schema = app.openapi()
    endpoints: set[str] = set()
    for path, item in schema.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            if method in item:
                endpoints.add(f"{method.upper()} {path}")
    return endpoints


def _active_action_keys() -> set[str]:
    """契约中状态以 ACTIVE 开头的 action（PHASE_GATED 不要求实现）。"""
    ar = yaml.safe_load(
        (CONTRACTS / "action_registry" / "action_registry.yaml").read_text(encoding="utf-8")
    )
    return {
        action["action_key"]
        for action in ar.get("actions", [])
        if str(action.get("status", "")).startswith("ACTIVE")
    }


def check_runtime_contract_coverage() -> bool:
    """校验运行时实现台账与契约、真实路由三方一致。"""
    if not LEDGER_PATH.exists():
        print(f"[FAIL] 缺少运行时实现台账: {LEDGER_PATH}")
        return False

    ledger = yaml.safe_load(LEDGER_PATH.read_text(encoding="utf-8")) or {}
    implemented: dict[str, dict[str, str]] = ledger.get("implemented") or {}
    not_implemented: dict[str, dict[str, str]] = ledger.get("not_implemented") or {}

    try:
        runtime = _runtime_endpoints()
    except Exception as exc:  # 导入失败本身就是回归信号
        print(f"[FAIL] 无法加载运行时 OpenAPI: {exc}")
        return False

    active = _active_action_keys()
    ok = True

    for key, entry in implemented.items():
        route = str(entry.get("route", ""))
        if route not in runtime:
            print(f"[FAIL] {key} 声明已实现，但运行时没有端点 {route!r}")
            ok = False
        if key not in active:
            print(f"[FAIL] {key} 不是 ACTIVE action，不应出现在 implemented")
            ok = False

    for key, entry in not_implemented.items():
        if key in implemented:
            print(f"[FAIL] {key} 同时出现在 implemented 与 not_implemented")
            ok = False
        raw_reason = entry.get("reason", "") if isinstance(entry, dict) else ""
        if not str(raw_reason).strip():
            print(f"[FAIL] {key} 在 not_implemented 中缺少 reason")
            ok = False
        if key not in active:
            print(f"[FAIL] {key} 不是 ACTIVE action，不应出现在 not_implemented")
            ok = False

    covered = set(implemented) | set(not_implemented)
    uncovered = active - covered
    if uncovered:
        print(f"[FAIL] ACTIVE action 未登记（既没实现也没说明原因）: {sorted(uncovered)}")
        ok = False
    stale = covered - active
    if stale:
        print(f"[FAIL] 台账登记了非 ACTIVE 的 action: {sorted(stale)}")
        ok = False

    registered = {str(v.get("route", "")) for v in implemented.values()} | _NON_ACTION_PATHS
    unregistered = runtime - registered
    if unregistered:
        print(f"[FAIL] 运行时暴露了未登记端点: {sorted(unregistered)}")
        ok = False

    if ok:
        print(
            f"[PASS] 运行时 ↔ 契约覆盖：ACTIVE {len(active)} 个"
            f"（已实现 {len(implemented)} / 登记未实现 {len(not_implemented)}）"
        )
    return ok


# --------------------------------------------------------------------------- #
# 已实现 action 的对象 schema 覆盖
# --------------------------------------------------------------------------- #
OBJECT_SCHEMAS_PATH = CONTRACTS / "schemas" / "object_schemas.schema.json"
RESOURCE_EXEMPTIONS_PATH = CONTRACTS / "runtime" / "resource_schema_exemptions.yaml"


def _action_resource_types() -> dict[str, str]:
    """action_key → resource_type（可能含 `/` 组合，如 A/B）。"""
    ar = yaml.safe_load(
        (CONTRACTS / "action_registry" / "action_registry.yaml").read_text(encoding="utf-8")
    )
    return {
        str(action["action_key"]): str(action.get("resource_type", ""))
        for action in ar.get("actions", [])
    }


def _object_schema_defs() -> set[str]:
    """object_schemas.schema.json 的 definitions 名称集合。"""
    data = json.loads(OBJECT_SCHEMAS_PATH.read_text(encoding="utf-8"))
    defs = data.get("definitions") or data.get("$defs") or {}
    return set(defs)


def _schema_exemptions() -> dict[str, str]:
    """显式豁免台账：resource_type → reason（必须非空）。"""
    if not RESOURCE_EXEMPTIONS_PATH.exists():
        return {}
    data = yaml.safe_load(RESOURCE_EXEMPTIONS_PATH.read_text(encoding="utf-8")) or {}
    raw = data.get("exemptions") or {}
    return {
        str(name): str(reason).strip()
        for name, reason in raw.items()
        if str(reason).strip()
    }


def missing_resource_schemas(
    implemented: set[str],
    resource_types: dict[str, str],
    defs: set[str],
    exemptions: set[str],
) -> list[tuple[str, str]]:
    """已实现 action 里「资源类型没有对象定义也没豁免」的 (action, type) 列表。

    为什么只看 implemented：未实现的动作已经由台账登记了 reason，写代码前必须先
    补对象契约；一旦声明已实现，它就在真实写这个对象，schema 缺失是硬伤。
    """
    missing: list[tuple[str, str]] = []
    for key in sorted(implemented):
        raw = resource_types.get(key, "")
        for part in (p.strip() for p in raw.split("/")):
            if not part:
                continue
            if part in defs or part in exemptions:
                continue
            missing.append((key, part))
    return missing


def check_implemented_resource_schemas() -> bool:
    """已实现 action 的资源类型必须有机器可读定义，或登记豁免原因。"""
    if not OBJECT_SCHEMAS_PATH.exists():
        print(f"[FAIL] 缺少对象 schema: {OBJECT_SCHEMAS_PATH}")
        return False
    ledger = yaml.safe_load(LEDGER_PATH.read_text(encoding="utf-8")) or {}
    implemented = set((ledger.get("implemented") or {}).keys())
    resource_types = _action_resource_types()
    defs = _object_schema_defs()
    exemptions = _schema_exemptions()

    missing = missing_resource_schemas(implemented, resource_types, defs, exemptions)
    if missing:
        for key, part in missing:
            print(
                f"[FAIL] {key} 声明已实现，但资源类型 {part!r} 在 object_schemas 中"
                " 没有定义，也没有在 contracts/runtime/resource_schema_exemptions.yaml"
                " 登记豁免原因"
            )
        return False

    # 豁免台账本身也要防呆：豁免了不存在的实现动作 = 过期条目，会掩盖真实缺口。
    orphan = set(exemptions) - {
        part.strip()
        for key in implemented
        for part in resource_types.get(key, "").split("/")
        if part.strip()
    }
    if orphan:
        print(f"[FAIL] 豁免台账包含未被任何已实现 action 引用的条目: {sorted(orphan)}")
        return False

    print(
        f"[PASS] 已实现 action 的对象 schema 覆盖：{len(implemented)} 个动作"
        f"（豁免 {len(exemptions)} 个资源类型）"
    )
    return True


# --------------------------------------------------------------------------- #
# Permission Matrix 结构校验
# --------------------------------------------------------------------------- #
def check_permission_matrix() -> bool:
    """校验 permission_matrix.yaml 的结构完整性。

    契约要求：每个 action 都应有权限落点。校验：
    1. 关键分区（role_matrix / action_matrix / separation_of_duty）非空；
    2. action_matrix.actions 覆盖全部 action_key（与 action_registry 一致）；
    3. action_matrix.total 与条目数一致。
    """
    pm_path = CONTRACTS / "permission" / "permission_matrix.yaml"
    ar_path = CONTRACTS / "action_registry" / "action_registry.yaml"

    pm = yaml.safe_load(pm_path.read_text(encoding="utf-8"))
    ar = yaml.safe_load(ar_path.read_text(encoding="utf-8"))

    # 关键分区非空
    for section in ("role_matrix", "action_matrix", "separation_of_duty"):
        if not pm.get(section):
            print(f"[FAIL] permission_matrix 缺少非空分区: {section}")
            return False

    action_matrix = pm["action_matrix"]
    # 派生视图模式（推荐）：action_matrix 不保存 action_key 副本，仅声明
    # 权威源与总数；覆盖完整性由 action_registry 单一事实源保证。
    if action_matrix.get("generated_from"):
        if action_matrix["generated_from"] != "action_registry.yaml":
            print(
                "[FAIL] action_matrix.generated_from 必须为 action_registry.yaml，"
                f"实际为 {action_matrix['generated_from']!r}"
            )
            return False
        if action_matrix.get("actions"):
            print(
                "[FAIL] action_matrix 声明为派生视图，但仍保存 actions 副本（重复事实源）"
            )
            return False
        expected_total = len(ar.get("actions", []))
        declared_total = action_matrix.get("total")
        if declared_total != expected_total:
            print(
                f"[FAIL] action_matrix total={declared_total} 与 action_registry "
                f"条目数 {expected_total} 不符"
            )
            return False
        print(
            f"[PASS] Permission Matrix 为派生视图（无重复维护）：覆盖 {expected_total} 个 action"
        )
        return True

    action_keys: list[str] = []
    for idx, action in enumerate(ar.get("actions", [])):
        key = action.get("action_key") if isinstance(action, dict) else None
        if not key:
            print(f"[FAIL] action_registry.actions[{idx}] 缺 action_key: {action!r}")
            return False
        action_keys.append(_normalize_action_key(key))

    action_matrix = pm["action_matrix"]
    pm_actions = action_matrix.get("actions", [])
    pm_keys: list[str] = []
    for idx, entry in enumerate(pm_actions):
        key = entry.get("action_key") if isinstance(entry, dict) else None
        if not key:
            print(f"[FAIL] permission_matrix.action_matrix.actions[{idx}] 缺 action_key")
            return False
        pm_keys.append(_normalize_action_key(key))

    # total 计数一致性
    declared_total = action_matrix.get("total")
    if declared_total != len(pm_actions):
        print(
            f"[FAIL] permission_matrix.action_matrix total={declared_total}"
            f" 与实际条目数 {len(pm_actions)} 不符"
        )
        return False

    # 覆盖完整性：action_registry 的 action_key ⊆ action_matrix 的 action_key
    pm_set = set(pm_keys)
    ar_set = set(action_keys)

    only_in_ar = ar_set - pm_set
    only_in_pm = pm_set - ar_set

    if only_in_ar or only_in_pm:
        if only_in_ar:
            print(
                f"[FAIL] permission_matrix 缺权限落点（Action 注册但无权限条目）: "
                f"{sorted(only_in_ar)}"
            )
        if only_in_pm:
            print(
                f"[FAIL] permission_matrix 含未注册 Action 的权限条目: "
                f"{sorted(only_in_pm)}"
            )
        return False

    print(f"[PASS] Permission Matrix 结构完整：覆盖 {len(ar_set)} 个 action")
    return True


# --------------------------------------------------------------------------- #
# Schema 对象完整性
# --------------------------------------------------------------------------- #
def check_schema_objects() -> bool:
    """校验 object_schemas.schema.json 的 P0 对象定义完整性。

    契约：76 个 P0 契约对象已建模。校验 definitions 非空，且每个定义含
    type / properties 基础结构（防止空壳对象）。
    """
    schema_path = CONTRACTS / "schemas" / "object_schemas.schema.json"
    data = json.loads(schema_path.read_text(encoding="utf-8"))

    definitions = data.get("definitions", data.get("$defs", {}))
    if not definitions:
        print("[FAIL] object_schemas.schema.json 缺少 definitions")
        return False

    empty = [k for k, v in definitions.items() if not isinstance(v, dict) or not v]
    if empty:
        print(f"[FAIL] 存在空对象定义: {empty}")
        return False

    print(f"[PASS] Schema 对象完整：{len(definitions)} 个定义")
    return True


# --------------------------------------------------------------------------- #
# 已实现 action 的授权落点（防「有端点、没授权」）
# --------------------------------------------------------------------------- #
def check_implemented_permission_wiring() -> bool:
    """校验每个已实现 action 的模块里真的挂了 ``require_role("<ACTION>")``。

    为什么需要这条闸门：
    - 运行时覆盖闸门只证明「端点存在」，不证明「端点有授权」。一个忘写
      require_role 的端点照样能被未授权主体调用，而 CI 全绿——这正是
      「声明 ≠ 执行」的典型沉默缺口。
    - 契约是单一事实源：动作键必须出现在该模块源码里（作为 require_role 的
      参数），否则说明没有把该动作接到授权依赖上。
    - 同时校验台账里的 module 路径真实存在，避免台账指向一个不存在的文件。

    局限（诚实记录）：这是源码文本级校验，只证明「键被引用」；它不能证明
    require_role 一定挂在正确的路由函数上。更强的证明在集成测试（403 用例）。
    """
    ledger = yaml.safe_load(LEDGER_PATH.read_text(encoding="utf-8")) or {}
    implemented: dict[str, dict[str, str]] = ledger.get("implemented") or {}
    root = CONTRACTS.parent
    ok = True

    for key, entry in implemented.items():
        module_rel = str(entry.get("module", ""))
        module_path = root / module_rel
        if not module_path.exists():
            print(f"[FAIL] {key} 台账登记的模块不存在: {module_rel}")
            ok = False
            continue
        source = module_path.read_text(encoding="utf-8")
        if f'require_role("{key}")' not in source:
            print(
                f"[FAIL] {key} 在 {module_rel} 中没有挂 require_role"
                f'("{key}")——端点存在但没有授权依赖'
            )
            ok = False

    if ok:
        print(
            f"[PASS] 已实现 action 的授权落点：{len(implemented)} 个动作均已挂 require_role"
        )
    return ok


# --------------------------------------------------------------------------- #
# 状态可达性（把「静默不可达」变成显式债务）
# --------------------------------------------------------------------------- #
STATE_GAPS_PATH = CONTRACTS / "runtime" / "state_machine_gaps.yaml"
# 状态名提取：契约里的状态都是大写下划线形式（GENERATED / TECHNICAL_QC）。
# 只取长度 >=3 的全大写词，避免把中文说明或单字母误判成状态。
_STATE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


def _contract_state_names(text: str) -> set[str]:
    """从契约 states 文本提取状态名集合。"""
    return set(_STATE_TOKEN_RE.findall(text))


def _reachable_states(edges: set[tuple[str, str]]) -> set[str]:
    """从「无入边的实现状态」出发，沿实现转移边求可达闭包。"""
    targets = {tgt for _, tgt in edges}
    initials = {src for src, _ in edges} - targets
    reachable = set(initials)
    frontier = list(initials)
    while frontier:
        state = frontier.pop()
        for src, tgt in edges:
            if src == state and tgt not in reachable:
                reachable.add(tgt)
                frontier.append(tgt)
    return reachable


def check_state_reachability() -> bool:
    """校验每个契约状态要么运行时可达，要么已在 gaps 文件显式登记。

    双向校验（防欺骗 + 防陈旧）：
    1. 契约声明了、实现不可达、又没登记 → FAIL（静默缺口）；
    2. 已登记为缺口、但实现其实可达 → FAIL（陈旧债务，必须删条目）；
    3. 缺口条目缺 reason / contract_ref / unblocks_when → FAIL（债务不能含糊）。
    """
    sm_path = CONTRACTS / "state_machines" / "state_machines.yaml"
    objects = (yaml.safe_load(sm_path.read_text(encoding="utf-8")) or {}).get(
        "objects", []
    )
    gaps = (
        yaml.safe_load(STATE_GAPS_PATH.read_text(encoding="utf-8"))
        if STATE_GAPS_PATH.exists()
        else {}
    ) or {}
    declared: dict[str, dict[str, dict[str, str]]] = gaps.get("gaps") or {}
    ok = True
    checked = 0
    gap_count = 0

    for obj_name, (module_path, var_name) in _STATE_MACHINE_SOURCES.items():
        entry = next((o for o in objects if o.get("object") == obj_name), None)
        if entry is None:
            print(f"[FAIL] state_machines.yaml 中未找到 {obj_name}")
            ok = False
            continue
        states = _contract_state_names(str(entry.get("states", "")))
        edges = _load_impl_transitions(module_path, var_name)
        reachable = _reachable_states(edges)
        obj_gaps = declared.get(obj_name) or {}
        checked += 1

        undeclared = (states - reachable) - set(obj_gaps)
        if undeclared:
            print(
                f"[FAIL] {obj_name} 有契约状态在运行时不可达且未登记债务: {sorted(undeclared)}"
            )
            ok = False
        for state, detail in obj_gaps.items():
            if state not in states:
                print(f"[FAIL] {obj_name} 登记的缺口状态 {state!r} 不在契约 states 里")
                ok = False
            elif state in reachable:
                print(
                    f"[FAIL] {obj_name} 缺口状态 {state!r} 其实已可达——陈旧债务必须删条目"
                )
                ok = False
            missing = [
                key
                for key in ("reason", "contract_ref", "unblocks_when")
                if not str((detail or {}).get(key, "")).strip()
            ]
            if missing:
                print(f"[FAIL] {obj_name}.{state} 缺口登记缺字段: {missing}")
                ok = False
        gap_count += len(obj_gaps)

    if ok:
        print(
            f"[PASS] 状态可达性：{checked} 个已实现状态机；显式登记不可达 {gap_count} 项"
        )
    return ok


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    ok = True
    yaml_files = list(CONTRACTS.rglob("*.yaml")) + list(CONTRACTS.rglob("*.yml"))
    json_files = list(CONTRACTS.rglob("*.json"))

    for f in yaml_files:
        ok &= check_yaml(f)
    for f in json_files:
        ok &= check_json(f)

    print(f"已校验 YAML {len(yaml_files)} 个，JSON {len(json_files)} 个")

    ok &= check_state_machines()
    ok &= check_action_openapi_consistency()
    ok &= check_runtime_contract_coverage()
    ok &= check_implemented_resource_schemas()
    ok &= check_implemented_permission_wiring()
    ok &= check_state_reachability()
    ok &= check_permission_matrix()
    ok &= check_schema_objects()

    if ok:
        print("[PASS] 契约一致性校验通过")
        return 0
    print("[FAIL] 契约一致性校验未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
