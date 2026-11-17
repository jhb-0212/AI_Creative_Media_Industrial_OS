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
8. 全部 ACTIVE action 的资源类型必须有定义/豁免，或在
   contracts/runtime/resource_schema_gaps.yaml 中登记显式债务
   （reason / contract_ref / unblocks_when / referenced_by），并双向校验：
   未登记缺口报红、陈旧债务报红、孤儿债务报红、字段缺失报红。
9. 事件契约（contracts/runtime/event_catalog.yaml ↔ 代码 ↔ action registry）：
   未登记事件报红、陈旧事件报红、emitted_by 漂移报红；已实现非 READ 动作的
   audit_event 必须已登记，否则须在 event_emission_gaps.yaml 显式登记债务。

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
from typing import Any

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


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """契约 YAML 的唯一加载入口：读取 + 解析 + 断言顶层为映射。

    设计要点（铁律 2/5：单一职责、禁止补丁式判空）：
    - 所有校验函数读取契约 YAML 一律经由此函数，判空与类型断言收敛到一处，
      避免各调用点各自 ``or {}`` / ``isinstance`` 兜底造成的碎片化。
    - 两类契约损坏都在此统一 fail-fast 转 ValueError，并携带**完整路径**
      （契约目录存在同名 ``*_gaps.yaml``，仅用 ``name`` 无法定位）：
      1. 空文件/非映射：``yaml.safe_load`` 返回 None 或非 dict；
      2. 语法损坏：``yaml.YAMLError``（解析器抛错，不裸冒泡成 traceback）。
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} 解析结果不是映射（可能为空文件）")
    return data


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
    data = load_yaml_mapping(sm_path)

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

    ar = load_yaml_mapping(ar_path)
    oa = load_yaml_mapping(oa_path)

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
    ar = load_yaml_mapping(CONTRACTS / "action_registry" / "action_registry.yaml")
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

    ledger = load_yaml_mapping(LEDGER_PATH)
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
    ar = load_yaml_mapping(CONTRACTS / "action_registry" / "action_registry.yaml")
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
    data = load_yaml_mapping(RESOURCE_EXEMPTIONS_PATH)
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
    exemptions: dict[str, str],
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
    """已实现 action 的资源类型必须有机器可读定义，或登记豁免原因。

    三条校验，缺一条就会让台账变成「假绿」：
    1. 已实现动作引用的类型：有 schema 或已豁免（否则 FAIL）；
    2. 豁免了但已有 schema → 冗余豁免 FAIL（台账与实际契约自相矛盾）；
    3. 豁免了但没被任何已实现动作引用 → 过期条目 FAIL。
    """
    if not OBJECT_SCHEMAS_PATH.exists():
        print(f"[FAIL] 缺少对象 schema: {OBJECT_SCHEMAS_PATH}")
        return False
    ledger = load_yaml_mapping(LEDGER_PATH)
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

    # 豁免台账的另一种失真：对象已经有 schema 了却还登记豁免。留着不仅误导
    # （台账说「没定义」，契约说「有定义」），还会在 serializer 探针被移除后
    # 变成真正的「假绿后门」——所以冗余豁免必须删除（审查 N4）。
    redundant = sorted(set(exemptions) & defs)
    if redundant:
        print(
            f"[FAIL] 豁免台账包含**已有对象 schema** 的条目（冗余豁免必须删除）: "
            f"{redundant}"
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
# 全部 ACTIVE action 的对象 schema 覆盖闸门（显式债务，不静默放行）
# --------------------------------------------------------------------------- #
RESOURCE_GAPS_PATH = CONTRACTS / "runtime" / "resource_schema_gaps.yaml"
RESOURCE_GAP_REQUIRED_FIELDS = ("referenced_by", "reason", "contract_ref", "unblocks_when")


def _active_resource_refs(
    active: set[str], resource_types: dict[str, str]
) -> dict[str, set[str]]:
    """resource_type → 引用它的 ACTIVE action 集合（组合类型按 / 拆开）。"""
    refs: dict[str, set[str]] = {}
    for key in active:
        for part in (p.strip() for p in resource_types.get(key, "").split("/")):
            if part:
                refs.setdefault(part, set()).add(key)
    return refs


def _load_resource_schema_gaps() -> dict[str, dict[str, object]]:
    if not RESOURCE_GAPS_PATH.exists():
        return {}
    data = load_yaml_mapping(RESOURCE_GAPS_PATH)
    raw = data.get("gaps") or {}
    return {str(name): (entry or {}) for name, entry in raw.items()}


def check_active_resource_schemas() -> bool:
    """所有 ACTIVE action 的资源类型：有定义、有豁免，或登记显式债务。

    ADR-R07 的原始闸门只查「已实现」动作，于是 ACTIVE 未实现的业务对象
    既不在 schema 检查内，又有 reason 台账兜底，形成双向盲区（9 个类型）。
    本闸门把口径扩到全部 ACTIVE，并允许「债务登记」替代「第九个豁免」：
    债务必须写清谁在引用、卡在什么契约、什么条件解开；一旦正文补齐 schema，
    债务条目必须删除，否则以陈旧债务报红。
    """
    if not OBJECT_SCHEMAS_PATH.exists():
        print(f"[FAIL] 缺少对象 schema: {OBJECT_SCHEMAS_PATH}")
        return False

    active = _active_action_keys()
    resource_types = _action_resource_types()
    defs = _object_schema_defs()
    exemptions = set(_schema_exemptions())
    gaps = _load_resource_schema_gaps()
    refs = _active_resource_refs(active, resource_types)
    ok = True

    # 1) 债务条目字段完整 + referenced_by 与契约实际引用一致（台账不能撒谎）
    for name, entry in gaps.items():
        missing_fields = [f for f in RESOURCE_GAP_REQUIRED_FIELDS if not entry.get(f)]
        if missing_fields:
            print(f"[FAIL] 债务条目 {name!r} 缺少字段: {missing_fields}")
            ok = False
            continue
        referenced = entry.get("referenced_by") or []
        if not isinstance(referenced, list):
            print(f"[FAIL] 债务条目 {name!r} 的 referenced_by 必须是列表")
            ok = False
            continue
        declared = {str(v) for v in referenced}
        if declared != refs.get(name, set()):
            print(
                f"[FAIL] 债务条目 {name!r} 的 referenced_by 与 ACTIVE 实际引用不一致："
                f"登记 {sorted(declared)}，实际 {sorted(refs.get(name, set()))}"
            )
            ok = False

    # 2) ACTIVE 引用但既无定义、无豁免、未登记债务 = 静默缺口
    for name, actions in sorted(refs.items()):
        if name in defs or name in exemptions or name in gaps:
            continue
        print(
            f"[FAIL] ACTIVE 动作 {sorted(actions)} 引用的资源类型 {name!r} "
            "既无 object schema、无豁免，也未在 contracts/runtime/"
            "resource_schema_gaps.yaml 登记债务"
        )
        ok = False

    # 3) 已补 schema 却还挂着债务 = 陈旧债务（不允许登记一次永久豁免）
    stale = sorted(set(gaps) & defs)
    if stale:
        print(f"[FAIL] 以下类型已有 object schema 但仍登记债务，必须删除条目: {stale}")
        ok = False

    # 4) 债务类型没有任何 ACTIVE 动作引用 = 孤儿债务（掩盖的是假缺口）
    orphan = sorted(set(gaps) - set(refs))
    if orphan:
        print(f"[FAIL] 债务台账包含未被任何 ACTIVE action 引用的条目: {orphan}")
        ok = False

    if ok:
        print(
            f"[PASS] ACTIVE action 的对象 schema 覆盖：{len(active)} 个动作 / "
            f"{len(refs)} 个资源类型（显式债务 {len(gaps)} 项，豁免 {len(exemptions)} 项）"
        )
    return ok


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

    pm = load_yaml_mapping(pm_path)
    ar = load_yaml_mapping(ar_path)

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
# Schema 对象完整性（结构 + 运行时字段一致性）
# --------------------------------------------------------------------------- #
SCHEMA_PATH = CONTRACTS / "schemas" / "object_schemas.schema.json"
_VALID_JSON_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)

# 已实现且可序列化的资源：用真实领域对象的 to_dict() 与契约定义双向比对。
# 只列"有运行时序列化器"的对象；PHASE_GATED 对象的覆盖由资源缺口台账负责。
_RESOURCE_SERIALIZERS: dict[str, tuple[str, str]] = {
    "AssetVersion": (
        "services.asset_service.domain.asset_version",
        "AssetVersion",
    ),
    "QCReport": ("services.asset_service.domain.qc_report", "QCReport"),
    "Project": ("services.project_planning.domain.project", "Project"),
    "Episode": ("services.project_planning.domain.episode", "Episode"),
    "ScriptRevision": ("services.project_planning.domain.script", "ScriptRevision"),
}


def _serializer_factories() -> dict[str, object]:
    """构造最小合法领域对象，用于校验 to_dict() 与契约定义的一致性。"""
    from uuid import uuid4

    from services.asset_service.domain.asset_version import AssetVersion
    from services.asset_service.domain.qc_report import QCReport
    from services.project_planning.domain.episode import Episode
    from services.project_planning.domain.project import Project
    from services.project_planning.domain.script import ScriptRevision

    tenant_id = uuid4()
    return {
        "AssetVersion": AssetVersion(
            entity_id="probe-asset",
            created_by="probe",
            tenant_id=tenant_id,
            asset_type="video",
            sha256="0" * 64,
            storage_ref={"bucket": "probe", "key": "probe"},
        ),
        "QCReport": QCReport(
            qc_report_id="probe-report",
            asset_version_id="probe-asset",
            tenant_id=tenant_id,
            quality_profile_version="qp-probe",
            detector_versions={"blur": "1.0"},
            findings=[{"code": "PROBE"}],
            created_by="probe",
        ),
        "Project": Project(
            entity_id="probe-project",
            created_by="probe",
            tenant_id=tenant_id,
            project_name="probe-project-name",
        ),
        "Episode": Episode(
            entity_id="probe-episode",
            created_by="probe",
            tenant_id=tenant_id,
            project_id="probe-project",
            episode_ref="EP001",
            title="probe-episode-title",
        ),
        "ScriptRevision": ScriptRevision(
            entity_id="probe-script",
            created_by="probe",
            tenant_id=tenant_id,
            project_id="probe-project",
            episode_ref="EP001",
            title="probe-script-title",
        ),
    }


def _resolve_local_ref(ref: str, definitions: dict[str, object]) -> str | None:
    """解析 `#/definitions/X` 或 `#/$defs/X`，返回定义名；外部引用返回 None。"""
    for prefix in ("#/definitions/", "#/$defs/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :].split("/")[0]
    return None


def check_schema_objects() -> bool:
    """校验 object_schemas.schema.json 的结构完整性与运行时字段一致性。

    为什么不再"只判非空 + 打印数量"（审计 P1-3 实测：76 个定义裁到 30 个仍
    PASS）：数量不是约束，行为才是。本闸门做四件事：
    1. 每个定义必须是 object、有非空 properties；
    2. required 必须是列表且每一项都在 properties 里；
    3. 每个 property 必须有形状（type / $ref / anyOf / oneOf / allOf /
       enum / const），type 必须是合法 JSON 类型；内部 $ref 必须能解析；
    4. 已实现对象的 to_dict() 字段集必须与定义严格对齐：契约 required 必须
       全出现（缺 = 契约承诺了不给），响应多出的键必须已在 properties 中
       （多 = 私有字段泄漏/字段漂移）。
    """
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    definitions = data.get("definitions", data.get("$defs", {}))
    if not definitions:
        print("[FAIL] object_schemas.schema.json 缺少 definitions")
        return False

    ok = True
    refs_checked = 0

    for name, spec in definitions.items():
        if not isinstance(spec, dict) or not spec:
            print(f"[FAIL] 定义 {name} 为空或不是对象")
            ok = False
            continue
        if spec.get("type") != "object":
            print(f"[FAIL] 定义 {name} 的 type 必须为 object")
            ok = False
        properties = spec.get("properties")
        if not isinstance(properties, dict) or not properties:
            print(f"[FAIL] 定义 {name} 缺少非空 properties（空壳对象）")
            ok = False
            continue

        required = spec.get("required", [])
        if not isinstance(required, list):
            print(f"[FAIL] 定义 {name} 的 required 必须是列表")
            ok = False
            required = []
        missing_required = [r for r in required if r not in properties]
        if missing_required:
            print(
                f"[FAIL] 定义 {name} 的 required 含未声明属性: {missing_required}"
            )
            ok = False

        for prop_name, prop_spec in properties.items():
            if not isinstance(prop_spec, dict) or not prop_spec:
                print(f"[FAIL] {name}.{prop_name} 不是有效的属性定义")
                ok = False
                continue
            shape_keys = ("type", "$ref", "anyOf", "oneOf", "allOf", "enum", "const")
            if not any(key in prop_spec for key in shape_keys):
                print(
                    f"[FAIL] {name}.{prop_name} 缺少形状（type/$ref/enum/...），"
                    "无法校验类型"
                )
                ok = False
            prop_type = prop_spec.get("type")
            if prop_type is not None:
                declared_types = (
                    prop_type if isinstance(prop_type, list) else [prop_type]
                )
                invalid = [
                    item
                    for item in declared_types
                    if item not in _VALID_JSON_TYPES
                ]
                if invalid:
                    print(
                        f"[FAIL] {name}.{prop_name} 的 type 非法: {invalid!r}"
                    )
                    ok = False
            ref = prop_spec.get("$ref")
            if isinstance(ref, str):
                refs_checked += 1
                target = _resolve_local_ref(ref, definitions)
                if target is not None and target not in definitions:
                    print(f"[FAIL] {name}.{prop_name} 的 $ref 悬空: {ref}")
                    ok = False

    # 运行时字段一致性：契约与代码谁也不能单方面多一个/少一个字段。
    factories = _serializer_factories()
    runtime_checked = 0
    for resource_name, _source in _RESOURCE_SERIALIZERS.items():
        spec = definitions.get(resource_name)
        if not isinstance(spec, dict):
            print(
                f"[FAIL] 已实现对象 {resource_name} 在契约中无定义"
                "（定义被删 = 契约失效）"
            )
            ok = False
            continue
        instance = factories.get(resource_name)
        if instance is None:
            print(f"[FAIL] 已实现对象 {resource_name} 缺少序列化探针工厂")
            ok = False
            continue
        runtime_keys = set(instance.to_dict())  # type: ignore[attr-defined]
        declared = set(spec.get("properties") or {})
        required = set(spec.get("required") or [])

        extra = sorted(runtime_keys - declared)
        if extra:
            print(
                f"[FAIL] {resource_name} 响应出现契约未声明字段: {extra}"
                "（私有字段泄漏或字段漂移）"
            )
            ok = False
        missing = sorted(required - runtime_keys)
        if missing:
            print(
                f"[FAIL] {resource_name} 响应缺少契约 required 字段: {missing}"
            )
            ok = False
        runtime_checked += 1

    if ok:
        print(
            f"[PASS] Schema 对象完整：{len(definitions)} 个定义结构校验通过"
            f"（$ref {refs_checked} 个），运行时字段一致 {runtime_checked} 个对象"
        )
    return ok


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
    ledger = load_yaml_mapping(LEDGER_PATH)
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


def _declared_state_gaps() -> dict[str, dict[str, dict[str, object]]]:
    """读取 ``gaps`` 分区，并**校验形状**（坏形状 = 契约损坏，抛 ValueError）。

    为什么单独抽出来：闸门后面要按 ``detail.get(...)`` 取字段，若 YAML 把条目写成
    字符串，就会在闸门内部抛 ``AttributeError`` 带 traceback 退出——那不是
    「结构化 FAIL」，而是脚本崩了（审查 N3）。把形状校验收敛到读取处，
    所有调用方（可达性闸门 / 入口债务闸门）自动一致。
    """
    if not STATE_GAPS_PATH.exists():
        return {}
    raw = load_yaml_mapping(STATE_GAPS_PATH).get("gaps") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{STATE_GAPS_PATH.name} 的 gaps 必须是「对象 → 状态映射」")
    result: dict[str, dict[str, dict[str, object]]] = {}
    for obj_name, states in raw.items():
        if not isinstance(states, dict):
            raise ValueError(
                f"{STATE_GAPS_PATH.name} 的 gaps.{obj_name} 必须是「状态 → 债务详情」映射，"
                f"实际是 {type(states).__name__}"
            )
        for state, detail in states.items():
            if not isinstance(detail, dict):
                raise ValueError(
                    f"{STATE_GAPS_PATH.name} 的 gaps.{obj_name}.{state} 必须是映射，"
                    f"实际是 {type(detail).__name__}"
                )
        result[str(obj_name)] = states
    return result


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
    objects = load_yaml_mapping(sm_path).get("objects", [])
    declared = _declared_state_gaps()
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


_TRANSITION_DEBT_REQUIRED = ("reason", "contract_ref", "unblocks_when")


def _implemented_action_keys() -> set[str]:
    """运行时台账（action_implementations.yaml）里标记为「已实现」的 action 集合。

    「入口是否真的存在」只认这一份：契约登记（action_registry）可以先行，凭它
    判定会把「已登记未实现」误算成入口已补上（审查 N3）。
    """
    ledger = load_yaml_mapping(LEDGER_PATH)
    return {str(key) for key in (ledger.get("implemented") or {})}


def _declared_transition_entry_gaps() -> dict[str, dict[str, object]]:
    """读取并校验 ``transition_entry_gaps`` 分区（形状坏 = 契约损坏 → ValueError）。

    文件缺失返回空登记：**不再无条件 PASS**——是否真的「无债务」由调用方的反向
    完备性检查判定，删文件不能把缺口变绿。
    """
    if not STATE_GAPS_PATH.exists():
        return {}
    raw = load_yaml_mapping(STATE_GAPS_PATH).get("transition_entry_gaps") or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{STATE_GAPS_PATH.name} 的 transition_entry_gaps 必须是「对象 → 债务详情」映射，"
            f"实际是 {type(raw).__name__}"
        )
    declared: dict[str, dict[str, object]] = {}
    for obj_name, detail in raw.items():
        if not isinstance(detail, dict):
            raise ValueError(
                f"{STATE_GAPS_PATH.name} 的 transition_entry_gaps.{obj_name} 必须是映射，"
                f"实际是 {type(detail).__name__}"
            )
        declared[str(obj_name)] = detail
    return declared


def _status_change_action_keys(resource_type: str) -> set[str]:
    """返回 action_registry 里某 resource_type 的「状态变更类」action_key。

    「状态变更类」定义为 action_key 不以 ``.CREATE`` / ``.READ`` 结尾的动作——
    CREATE 只创建对象（产出初始态）、READ 只读，均不触发状态转移；其余动作
    （IMPORT/LOCK/APPROVE/STATUS.CHANGE 等）才可能触发状态变更。
    """
    registry = load_yaml_mapping(CONTRACTS / "action_registry" / "action_registry.yaml")
    result: set[str] = set()
    for action in registry.get("actions", []):
        if not isinstance(action, dict):
            continue
        rtype = str(action.get("resource_type", "")).split("/")[0]
        key = str(action.get("action_key", ""))
        if rtype == resource_type and not key.endswith((".CREATE", ".READ")):
            result.add(key)
    return result


def check_transition_entry_gaps() -> bool:
    """校验「转移无 HTTP 入口」债务登记的诚实性（审计 P2-1 的闸门化）。

    与 check_state_reachability 的差异：后者只验证「状态在域转移表里可达」，
    但「可达」不等于「可执行」——Project/Episode 的转移在域表里可达，却没有任何
    action 能触发（停在 DRAFT 无法推进）。本函数校验 state_machine_gaps.yaml 的
    transition_entry_gaps 分区，**双向**校验：

    1. 正向：登记对象必须是已实现状态机（_STATE_MACHINE_SOURCES）；
    2. 正向：reason / contract_ref / unblocks_when 三项完整（债务不能含糊）；
    3. 正向防陈旧：若该对象**已有已实现的状态变更 action**，说明入口真的补上了，
       登记就变成陈旧债务，必须删条目；
    4. 反向完备性：已实现状态机里「有转移边、却没有任何已实现的状态变更 action」
       的对象**必须**登记债务——否则删掉条目、甚至删掉整个债务文件，闸门都会
       打印 PASS，P2-1 重新变成静默缺口（审查 N3）。

    两个刻意的口径：
    - 「已补上入口」只认**运行时台账 implemented** 的动作，不认 action_registry 里
      的登记。本项目要求「先在契约登记、再实现」，若按契约登记判定，先登记
      PROJECT.STATUS.CHANGE（not_implemented）就会把真实缺口误判成陈旧债务，
      反而逼着人删掉它。
    - 债务详情必须是映射：坏形状抛 ValueError（由 main 统一转 `[FAIL]`），
      不能让闸门带着 traceback 崩掉。
    """
    declared = _declared_transition_entry_gaps()
    implemented = _implemented_action_keys()
    ok = True
    count = 0

    for obj_name, detail in declared.items():
        if obj_name not in _STATE_MACHINE_SOURCES:
            print(f"[FAIL] {obj_name} 不在已实现状态机清单里，不得登记转移入口债务")
            ok = False
            continue
        count += 1
        missing = [
            key for key in _TRANSITION_DEBT_REQUIRED if not str(detail.get(key, "")).strip()
        ]
        if missing:
            print(f"[FAIL] {obj_name} 转移入口债务登记缺字段: {missing}")
            ok = False
        # 防陈旧：入口已实现却未删债务。
        live_actions = _status_change_action_keys(obj_name) & implemented
        if live_actions:
            print(
                f"[FAIL] {obj_name} 已有**已实现**的状态变更 action "
                f"{sorted(live_actions)}——转移入口债务已陈旧，必须删条目"
            )
            ok = False

    # 反向完备性：没有已实现入口的对象，必须把缺口写在债务文件里。
    for obj_name in sorted(_STATE_MACHINE_SOURCES):
        if obj_name in declared:
            continue
        if _status_change_action_keys(obj_name) & implemented:
            continue
        module_path, var_name = _STATE_MACHINE_SOURCES[obj_name]
        if not _load_impl_transitions(module_path, var_name):
            # 没有转移边的状态机不存在「转移入口」问题，不制造假债务。
            continue
        print(
            f"[FAIL] {obj_name} 有转移边但没有任何已实现的状态变更 action，"
            "却未在 state_machine_gaps.yaml 的 transition_entry_gaps 登记债务"
            "——缺口不能靠「删条目/删文件」变绿"
        )
        ok = False

    if ok:
        print(
            f"[PASS] 状态转移入口债务：显式登记 {count} 项"
            f"（{len(_STATE_MACHINE_SOURCES)} 个状态机无陈旧/越界/漏登记）"
        )
    return ok


# --------------------------------------------------------------------------- #
# 事件契约校验（Event Catalog ↔ 代码 ↔ Action Registry）
# --------------------------------------------------------------------------- #
EVENT_CATALOG_PATH = CONTRACTS / "runtime" / "event_catalog.yaml"
EVENT_GAPS_PATH = CONTRACTS / "runtime" / "event_emission_gaps.yaml"
ACTION_REGISTRY_PATH = CONTRACTS / "action_registry" / "action_registry.yaml"
# 事件只允许从业务代码里发出：测试代码里的伪造事件不参与契约。
EVENT_SCAN_ROOTS = (ROOT / "services", ROOT / "packages", ROOT / "workers")

_EVENT_CATALOG_REQUIRED = (
    "event_type",
    "aggregate_type",
    "emitted_by",
    "payload_required",
    "contract_ref",
)
_EVENT_GAP_REQUIRED = ("audit_event", "reason", "contract_ref", "unblocks_when")
_EVENT_LITERAL = re.compile(r'event_type\s*=\s*"([A-Z][A-Z0-9_]*)"')


def _scan_emitted_events() -> dict[str, set[str]]:
    """扫描业务代码里的 event_type 字面量，返回 {事件名: {发出文件路径}}。"""
    emitted: dict[str, set[str]] = {}
    for root in EVENT_SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            rel = path.as_posix()
            for name in _EVENT_LITERAL.findall(text):
                emitted.setdefault(name, set()).add(rel)
    return emitted


def check_event_catalog() -> bool:
    """双向校验事件契约：代码不得私发事件，台账不得说谎，缺口不得隐形。"""
    if not EVENT_CATALOG_PATH.exists():
        print(f"[FAIL] 缺少事件契约: {EVENT_CATALOG_PATH}")
        return False

    data = load_yaml_mapping(EVENT_CATALOG_PATH)
    entries = data.get("events") or []
    declared: dict[str, dict[str, object]] = {}
    ok = True

    for entry in entries:
        if not isinstance(entry, dict):
            print(f"[FAIL] event_catalog 条目必须是映射: {entry!r}")
            ok = False
            continue
        name = str(entry.get("event_type", "")).strip()
        if not name:
            print("[FAIL] event_catalog 有条目缺 event_type")
            ok = False
            continue
        if name in declared:
            print(f"[FAIL] 事件 {name} 重复登记")
            ok = False
            continue
        missing = [f for f in _EVENT_CATALOG_REQUIRED if not entry.get(f)]
        if missing:
            print(f"[FAIL] 事件 {name} 登记缺字段: {missing}")
            ok = False
        declared[name] = entry

    emitted = _scan_emitted_events()

    undeclared = sorted(set(emitted) - set(declared))
    if undeclared:
        print(f"[FAIL] 代码发出了未登记事件: {undeclared}（先登记契约，再发事件）")
        ok = False

    stale = sorted(set(declared) - set(emitted))
    if stale:
        print(f"[FAIL] 事件契约有陈旧条目（代码已不发）: {stale}")
        ok = False

    for name, entry in declared.items():
        emitted_by = str(entry.get("emitted_by", "")).replace("\\", "/")
        files = emitted.get(name, set())
        if emitted_by and not any(p.endswith(emitted_by) for p in files):
            print(
                f"[FAIL] 事件 {name} 的 emitted_by={emitted_by!r} "
                f"与实际发出文件不一致: {sorted(files)}"
            )
            ok = False

    # action.audit_event ↔ 事件契约/显式缺口：声明了动作却不发事件，必须登记债务。
    gaps: dict[str, dict[str, object]] = {}
    if EVENT_GAPS_PATH.exists():
        gap_data = load_yaml_mapping(EVENT_GAPS_PATH)
        gaps = gap_data.get("gaps") or {}
        for action_key, detail in gaps.items():
            missing = [
                f for f in _EVENT_GAP_REQUIRED if not (detail or {}).get(f)
            ]
            if missing:
                print(f"[FAIL] 事件缺口 {action_key} 登记缺字段: {missing}")
                ok = False

    registry = load_yaml_mapping(ACTION_REGISTRY_PATH)
    actions = {
        str(a.get("action_key")): a for a in (registry.get("actions") or []) if a
    }
    ledger = load_yaml_mapping(LEDGER_PATH)
    implemented = set((ledger.get("implemented") or {}).keys())

    for action_key in sorted(implemented):
        # READ 动作不改状态，是否写访问审计由接入层决定，不在本闸门强制。
        if action_key.endswith(".READ"):
            continue
        action = actions.get(action_key) or {}
        audit_event = str(action.get("audit_event", "")).strip()
        if not audit_event:
            print(f"[FAIL] 已实现动作 {action_key} 未在 registry 声明 audit_event")
            ok = False
            continue
        if audit_event in declared:
            if action_key in gaps:
                print(
                    f"[FAIL] 事件缺口 {action_key} 已能发出 {audit_event}，"
                    "陈旧债务必须删条目"
                )
                ok = False
            continue
        gap = gaps.get(action_key)
        if gap is None:
            print(
                f"[FAIL] 动作 {action_key} 声明的 audit_event {audit_event} "
                "既未登记也未登记缺口"
            )
            ok = False
        elif str(gap.get("audit_event", "")).strip() != audit_event:
            print(
                f"[FAIL] 事件缺口 {action_key} 的 audit_event 与 registry 不一致: "
                f"{gap.get('audit_event')!r} != {audit_event!r}"
            )
            ok = False

    orphan_gaps = sorted(set(gaps) - implemented)
    if orphan_gaps:
        print(f"[FAIL] 事件缺口登记了未实现的动作（孤儿债务）: {orphan_gaps}")
        ok = False

    if ok:
        print(
            f"[PASS] 事件契约：{len(declared)} 个事件 / "
            f"{len(emitted)} 个发出点 / 显式缺口 {len(gaps)} 项"
        )
    return ok


# --------------------------------------------------------------------------- #
# 安全与数据边界契约校验（Security & Data Boundary Contract）
# --------------------------------------------------------------------------- #
# 为什么需要这个校验（R1 一个事实一个权威源 + 边界缺口不可静默增长）
# ------------------------------------------------------------------------
# contracts/security_boundary.yaml 是授权/资源归属/数据所有权/进程密钥四条
# 边界的唯一权威源。它必须是**结构化约束**而非解释文档：本函数把它读进来，
# 逐条校验结构自洽——每条不变量必须落在 satisfied / partial / debt 三态之一，
# 且 debt 必须给 reason / contract_ref / unblocks_when 三项完整、partial 必须
# 给 satisfied / missing / unblocks_when。这样「边界缺口」就与 resource_schema_gaps /
# state_machine_gaps 一样，成为可双向校验的显式债务，而不是没人读的散文。
#
# 校验点：
# 1. 文件存在且顶层为映射，含四条边界（authorization / resource_scope /
#    data_ownership / process_keys），缺一报红；
# 2. 每条不变量有 id / name / statement / fact_source / verification / status；
# 3. status ∈ {satisfied, partial, debt}，且按状态要求对应字段：
#    - debt     → reason / contract_ref / unblocks_when 三项完整；
#    - partial  → satisfied / missing / unblocks_when 三项完整；
#    - satisfied→ 不得残留 debt / partial 子块（陈旧债务必须清理）。
# 4. 不变量 id 全局唯一（防复制粘贴出重名）。

SECURITY_BOUNDARY_PATH = CONTRACTS / "security_boundary.yaml"

_SECURITY_BOUNDARY_SECTIONS = (
    "authorization",
    "resource_scope",
    "data_ownership",
    "process_keys",
)

_SECURITY_BOUNDARY_INVARIANT_REQUIRED = (
    "id",
    "name",
    "statement",
    "fact_source",
    "verification",
    "status",
)

_SECURITY_BOUNDARY_STATUSES = ("satisfied", "partial", "debt")

_DEBT_REQUIRED = ("reason", "contract_ref", "unblocks_when")
_PARTIAL_REQUIRED = ("satisfied", "missing", "unblocks_when")


def check_security_boundary() -> bool:
    """校验安全与数据边界契约的结构自洽（债务/半满足/已满足三态一致）。"""
    data = load_yaml_mapping(SECURITY_BOUNDARY_PATH)
    ok = True

    missing_sections = [
        s for s in _SECURITY_BOUNDARY_SECTIONS if s not in data
    ]
    if missing_sections:
        print(
            f"[FAIL] security_boundary.yaml 缺少边界分区: {missing_sections}"
        )
        ok = False

    seen_ids: set[str] = set()
    total_invariants = 0
    debt_count = 0
    partial_count = 0

    for section in _SECURITY_BOUNDARY_SECTIONS:
        invariants = data.get(section, {}).get("invariants")
        if not isinstance(invariants, list):
            print(
                f"[FAIL] security_boundary.yaml 的 {section}.invariants 必须是列表"
            )
            ok = False
            continue

        for inv in invariants:
            if not isinstance(inv, dict):
                print(
                    f"[FAIL] {section} 存在非映射的不变量条目: {inv!r}"
                )
                ok = False
                continue
            total_invariants += 1

            inv_id = str(inv.get("id", "")).strip()
            missing_fields = [
                f for f in _SECURITY_BOUNDARY_INVARIANT_REQUIRED
                if not str(inv.get(f, "")).strip()
            ]
            if missing_fields:
                print(
                    f"[FAIL] {section}/{inv_id or '?'} 不变量缺字段: {missing_fields}"
                )
                ok = False

            if inv_id in seen_ids:
                print(f"[FAIL] security_boundary.yaml 存在重复不变量 id: {inv_id}")
                ok = False
            seen_ids.add(inv_id)

            status = str(inv.get("status", "")).strip()
            if status not in _SECURITY_BOUNDARY_STATUSES:
                print(
                    f"[FAIL] {inv_id} 的 status 非法: {status!r}（须为 "
                    f"{'/'.join(_SECURITY_BOUNDARY_STATUSES)}）"
                )
                ok = False
                continue

            if status == "debt":
                debt_count += 1
                debt = inv.get("debt")
                if not isinstance(debt, dict):
                    print(f"[FAIL] {inv_id} 为 debt 但缺少 debt 子块")
                    ok = False
                    continue
                missing = [
                    k for k in _DEBT_REQUIRED if not str(debt.get(k, "")).strip()
                ]
                if missing:
                    print(f"[FAIL] {inv_id} 债务登记缺字段: {missing}")
                    ok = False
                # 防陈旧：status 已是 satisfied 却仍挂 debt 子块。
                if "debt" in inv and inv.get("status") == "satisfied":
                    print(
                        f"[FAIL] {inv_id} 已满足却残留 debt 子块（陈旧债务必须清理）"
                    )
                    ok = False

            elif status == "partial":
                partial_count += 1
                part = inv.get("partial")
                if not isinstance(part, dict):
                    print(f"[FAIL] {inv_id} 为 partial 但缺少 partial 子块")
                    ok = False
                    continue
                missing = [
                    k for k in _PARTIAL_REQUIRED if not str(part.get(k, "")).strip()
                ]
                if missing:
                    print(f"[FAIL] {inv_id} 半满足登记缺字段: {missing}")
                    ok = False

            elif status == "satisfied" and ("debt" in inv or "partial" in inv):
                # 已满足的不变量不得残留任何债务/半满足子块。
                print(
                    f"[FAIL] {inv_id} 已满足却残留 debt/partial 子块"
                    "（陈旧债务必须清理）"
                )
                ok = False
    if ok:
        print(
            f"[PASS] 安全与数据边界契约：{total_invariants} 条不变量"
            f"（debt {debt_count} / partial {partial_count} / satisfied "
            f"{total_invariants - debt_count - partial_count}）结构自洽"
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

    try:
        ok &= check_state_machines()
        ok &= check_action_openapi_consistency()
        ok &= check_runtime_contract_coverage()
        ok &= check_implemented_resource_schemas()
        ok &= check_active_resource_schemas()
        ok &= check_implemented_permission_wiring()
        ok &= check_state_reachability()
        ok &= check_transition_entry_gaps()
        ok &= check_permission_matrix()
        ok &= check_schema_objects()
        ok &= check_event_catalog()
        ok &= check_security_boundary()
    except ValueError as exc:
        # 契约文件缺失/为空/非映射时，load_yaml_mapping 统一抛 ValueError；
        # 这是脚本级致命错误（契约损坏无法继续校验），在此统一兜底报红。
        print(f"[FAIL] 契约加载失败: {exc}")
        return 1

    if ok:
        print("[PASS] 契约一致性校验通过")
        return 0
    print("[FAIL] 契约一致性校验未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
