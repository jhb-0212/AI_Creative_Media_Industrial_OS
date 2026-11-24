"""契约一致性校验脚本。

职责：校验冻结契约（contracts/）与实现派生之间的一致性，作为 CI 门禁。
当前覆盖：
1. contracts/ 下所有 YAML / JSON 文件的语法解析。
2. contracts/phases/phase_map.yaml 与基线 §31.2 唯一阶段总表逐字一致，
   非权威阶段表登记从正文派生且双向完备（Gate C 自动检查）。
4. contracts/state_machines/state_machines.yaml 中 Project 状态机
   与 services/project_planning/domain/state_machine.py 的转移表一致性。
3. Action Registry ↔ OpenAPI 路径一一对应（未注册 Action 不得暴露端点）。
5. 运行时 ↔ 契约覆盖（contracts/runtime/action_implementations.yaml 台账：
   ACTIVE action 必须实现或登记未实现的原因，已声明的 route 必须真实存在）。
6. Permission Matrix 结构完整（action_matrix 覆盖全部 action_key）。
7. object_schemas.schema.json 的 P0 对象定义完整性。
8. 已实现 action 的资源类型必须在 object_schemas 中有定义，或在
   contracts/runtime/resource_schema_exemptions.yaml 中登记豁免原因
   （防止 ADR-R07 那类「动作有、对象无」的缺口静默通过 CI）。
9. 全部 ACTIVE action 的资源类型必须有定义/豁免，或在
   contracts/runtime/resource_schema_gaps.yaml 中登记显式债务
   （reason / contract_ref / unblocks_when / referenced_by），并双向校验：
   未登记缺口报红、陈旧债务报红、孤儿债务报红、字段缺失报红。
10. 事件契约（contracts/runtime/event_catalog.yaml ↔ 代码 ↔ action registry）：
   未登记事件报红、陈旧事件报红、emitted_by 漂移报红；已实现非 READ 动作的
   audit_event 必须已登记，否则须在 event_emission_gaps.yaml 显式登记债务。
11. 对象引用目标（object_schemas 的 x-afs-reference-targets）：声明的字段必须存在、
    声明的目标必须是已定义对象；悬空引用报红（Scene 曾因此长期缺失而全绿）。
12. 状态枚举四层一致性（state_machines → enum_registry → object_schemas → Runtime）：
    allowed_states 与转移边、枚举注册表、Schema 的 status.enum、status 字段分类台账、
    别名/跨域标记层、已实现对象的 Domain Enum 与数据库 CHECK 全部逐值对齐。
    （旧版只比对象名不比状态集，于是三处状态集合可以各自漂移而全绿。）

设计要点（不屎山代码）：
- 状态值与转移边一律结构化（allowed_states / transitions / guards.transition）；
  不再解析自然语言状态链，避免从备注文字里「抓」出假状态。
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
BASELINE_DOC = ROOT / "docs" / "baseline" / "AI影视工业化生产平台V2.0.5.md"

# 在脚本运行时将仓库根加入 sys.path，使领域模块可导入。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 治理 YAML 的统一严格载入入口：拒绝重复映射键（见 packages/shared/yaml_strict）。
from packages.shared.yaml_strict import load_yaml, load_yaml_file  # noqa: E402


# --------------------------------------------------------------------------- #
# 通用契约文件语法校验
# --------------------------------------------------------------------------- #
def check_yaml(path: Path) -> bool:
    # 统一走严格 loader：重复映射键必须报红，不能像 safe_load 那样静默覆盖后键
    # （G5 / D1 记录的病根；contract_amendments 的重复 revocations 块曾真实发生）。
    try:
        load_yaml_file(path)
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
        data = load_yaml_file(path)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} 解析结果不是映射（可能为空文件）")
    return data


# --------------------------------------------------------------------------- #
# 状态机一致性校验（Project + ScriptRevision）
# --------------------------------------------------------------------------- #
# 契约对象名 -> 领域转移表来源（模块路径, 变量名）
# 所有对象的状态机统一集中在 domain/state_machine.py（单一事实源），
# 避免各聚合根内嵌转移表导致落点不一致。
# 不变量定级的词汇表（**唯一权威定义**）。
#   SCHEMA           —— 由 object_schemas 契约本身拒绝（字段集闭合、枚举范围）；
#   RUNTIME_ENFORCED —— 由运行时拒绝（领域守卫 / 库层约束 / 服务前置条件）；
#   RUNTIME_PENDING  —— 尚未落地，必须留在不变量里当债务。
# 为什么集中在这里而不是各测试各写一份：此前 5 个契约测试各自硬编码
# {SCHEMA, RUNTIME_PENDING}。A13 交付需要第三档时，五处会同时变红，而"修法"
# 是各改一遍——一份事实五个副本，下一次必然分叉，且分叉后没有任何闸门发现。
INVARIANT_LEVELS: frozenset[str] = frozenset(
    {"SCHEMA", "RUNTIME_ENFORCED", "RUNTIME_PENDING"}
)

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
    # A13（ADR-R28）
    "QCTask": (
        "services.qc_governance.domain.state_machine",
        "QC_TASK_ALLOWED_TRANSITIONS",
    ),
    "Scene": (
        "services.project_planning.domain.state_machine",
        "SCENE_ALLOWED_TRANSITIONS",
    ),
    "Shot": (
        "services.project_planning.domain.state_machine",
        "SHOT_ALLOWED_TRANSITIONS",
    ),
    "ShotRevision": (
        "services.project_planning.domain.state_machine",
        "SHOT_REVISION_ALLOWED_TRANSITIONS",
    ),
    "ShotBreakdownDraft": (
        "services.project_planning.domain.state_machine",
        "SHOT_DRAFT_ALLOWED_TRANSITIONS",
    ),
    "ReworkTask": (
        "services.project_planning.domain.state_machine",
        "REWORK_TASK_ALLOWED_TRANSITIONS",
    ),
    # A22：规格两态一条边（DRAFT → LOCKED），归 SHOTSPEC.LOCK。
    "ShotSpec": (
        "services.project_planning.domain.state_machine",
        "SHOT_SPEC_ALLOWED_TRANSITIONS",
    ),
    # A23：五态四条边——DRAFT→ACTIVE 归 CONSTRAINT.PACK.LOCK，三条治理边归
    # CONSTRAINT.PACK.STATUS.CHANGE（契约已登记、运行时未实现，逐边债务在
    # state_machine_gaps.yaml）。进这张表意味着四条边都必须与契约逐条相等。
    "ConstraintPack": (
        "services.project_planning.domain.state_machine",
        "CONSTRAINT_PACK_ALLOWED_TRANSITIONS",
    ),
    # R16-D3：生产链与预算 MVP 的三个生命周期对象。
    "ProductionRoute": (
        "services.project_planning.domain.state_machine",
        "PRODUCTION_ROUTE_ALLOWED_TRANSITIONS",
    ),
    "GenerationTask": (
        "services.project_planning.domain.state_machine",
        "GENERATION_TASK_ALLOWED_TRANSITIONS",
    ),
    # R42 T1：产物写入意图的确认边已落地，纳入已实现状态机比对。
    "ArtifactManifest": (
        "services.project_planning.domain.state_machine",
        "ARTIFACT_MANIFEST_ALLOWED_TRANSITIONS",
    ),
    "ShotBudgetRequest": (
        "services.project_planning.domain.state_machine",
        "SHOT_BUDGET_REQUEST_ALLOWED_TRANSITIONS",
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
    """校验所有已实现的状态机（Project / ScriptRevision / Episode / AssetVersion）与契约一致。

    比对对象是 ``transitions`` 结构化列表，不再是状态链文本——文本里 ``A→B / C``
    的斜杠在源侧表示「多个源」、在目标侧表示「多个目标」，靠正则解析只能猜。
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

        contract_transitions = contract_edges(entry)
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


def contract_edges(entry: dict[str, Any]) -> set[tuple[str, str]]:
    """把契约条目的 ``transitions`` 列表归一化为 ``(from, to)`` 集合。"""
    edges: set[tuple[str, str]] = set()
    for edge in entry.get("transitions") or []:
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("from", "")).strip()
        dst = str(edge.get("to", "")).strip()
        if src and dst:
            edges.add((src, dst))
    return edges


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
    # KEYS-1：验签方只认显式注入的公钥，加载运行时 app 前须引导开发/测试密钥，
    # 否则 apps.api.main 的导入链（deps → get_public_key_store）会因缺公钥拒绝启动。
    from packages.identity.bootstrap import bootstrap_signing_key_if_missing

    bootstrap_signing_key_if_missing()
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
    # 同一动作的第二个入口（如「选择」动作下的撤销治理边）：台账用 `extra_routes`
    # 显式列出，否则运行时端点会因为只比对主 route 而被判成「未登记」——那会逼着
    # 实现方为同一条准入凭据另造一个动作键，恰好是状态机边所有权要消除的形态。
    for entry in implemented.values():
        registered |= {str(r) for r in (entry.get("extra_routes") or [])}
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
    # A22：规格从第一天起纳入 to_dict ↔ 契约的双向比对。A13 的五个对象没进来，
    # 它们的「响应字段是否等于契约字段」至今无人校验——本条不重复那个缺口。
    "ShotSpec": ("services.project_planning.domain.shot_spec", "ShotSpec"),
    "ConstraintPack": (
        "services.project_planning.domain.constraint_pack",
        "ConstraintPack",
    ),
    # R16-D3：生产链与预算 MVP 四个对象纳入 to_dict ↔ 契约双向比对。
    "ProductionRoute": (
        "services.production_planning.domain.production_route",
        "ProductionRoute",
    ),
    "GenerationTask": (
        "services.production_planning.domain.generation_task",
        "GenerationTask",
    ),
    "ShotBudgetRequest": (
        "services.budget_quota.domain.shot_budget_request",
        "ShotBudgetRequest",
    ),
    "BudgetLedger": (
        "services.budget_quota.domain.budget_ledger",
        "BudgetLedger",
    ),
}


def _serializer_factories() -> dict[str, object]:
    """构造最小合法领域对象，用于校验 to_dict() 与契约定义的一致性。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    from packages.shared.storage_locator import build_locator
    from services.asset_service.domain.asset_version import AssetVersion
    from services.asset_service.domain.qc_report import QCReport
    from services.budget_quota.domain.budget_ledger import BudgetLedger
    from services.budget_quota.domain.shot_budget_request import ShotBudgetRequest
    from services.production_planning.domain.generation_task import GenerationTask
    from services.production_planning.domain.production_route import ProductionRoute
    from services.project_planning.domain.constraint_pack import ConstraintPack
    from services.project_planning.domain.enums import ConstraintPackStatus
    from services.project_planning.domain.episode import Episode
    from services.project_planning.domain.project import Project
    from services.project_planning.domain.script import ScriptRevision
    from services.project_planning.domain.shot_spec import ShotSpec

    tenant_id = uuid4()
    return {
        "AssetVersion": AssetVersion(
            entity_id="probe-asset",
            created_by="probe",
            tenant_id=tenant_id,
            project_id="probe-project",
            asset_type="video",
            sha256="0" * 64,
            # 探测对象也必须遵守 storage_ref 的受控形态：它是**第二个写入
            # 路径**，若这里仍用自由 dict，就等于「契约探测走一套、真实登记走
            # 另一套」，而探测全绿恰恰说明不了真实登记能过。
            storage_ref=build_locator("0" * 64, "mp4"),
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
        "ShotSpec": ShotSpec(
            entity_id="probe-spec",
            created_by="probe",
            tenant_id=tenant_id,
            project_id="probe-project",
            shot_revision_id="probe-revision",
            duration_ms=1000,
            fps_num=25,
            fps_den=1,
            cut_points_frames=[0],
            aspect_ratio="16:9",
            coordinate_system="camera_local",
            camera_path="dolly-in",
            subject_paths="lead-center",
            shot_size_changes="none",
            previz_ref="previz-probe",
        ),
        "ConstraintPack": ConstraintPack(
            entity_id="probe-pack",
            created_by="probe",
            tenant_id=tenant_id,
            project_id="probe-project",
            shot_ids=["probe-shot"],
            applicability="APPLICABLE",
            # 探针取 ACTIVE 形态：DRAFT 下 approved_at 为空串，会掩盖
            # ``approved_at`` 的序列化分支是否与契约类型一致。
            locked_dimensions=["cut_points"],
            constraints=[
                {
                    "constraint_id": "probe-constraint",
                    "target": "probe-target",
                    "shot_scope": "probe-scope",
                    "rule": "probe-rule",
                    "source": "probe-source",
                    "authority_level": "probe-level",
                    "valid_at": "2026-11-21T00:00:00Z",
                    "approval_ref": "probe-approval",
                }
            ],
            approved_by="probe",
            approved_at=datetime(2026, 11, 21, tzinfo=UTC),
            status=ConstraintPackStatus.ACTIVE,
        ),
        "ProductionRoute": ProductionRoute(
            entity_id="probe-route",
            created_by="probe",
            tenant_id=tenant_id,
            route_id="probe-route-id",
            project_id="probe-project",
            shot_id="probe-shot",
            task_ref="probe-task",
            route_type="IMAGE_TO_VIDEO",
            capability_ref="probe-capability",
            basis_ref="probe-basis",
        ),
        "GenerationTask": GenerationTask(
            entity_id="probe-task",
            created_by="probe",
            tenant_id=tenant_id,
            generation_task_id="probe-task-id",
            project_id="probe-project",
            shot_id="probe-shot",
            shot_revision_id="probe-revision",
            production_route_ref="probe-route-id",
            model_adapter_ref="probe-adapter",
            constraint_snapshot_id="probe-snapshot",
            budget_ref="probe-budget",
            prompt_ref="probe-prompt",
        ),
        "ShotBudgetRequest": ShotBudgetRequest(
            entity_id="probe-budget",
            created_by="probe",
            tenant_id=tenant_id,
            request_id="probe-budget-id",
            project_id="probe-project",
            shot_id="probe-shot",
            account_type="PRODUCTION",
            purpose="probe-purpose",
            cost_category="render",
            amount_limit=1000,
            requested_by="probe",
        ),
        "BudgetLedger": BudgetLedger(
            entity_id="probe-ledger",
            created_by="probe",
            tenant_id=tenant_id,
            transaction_id="probe-ledger-id",
            account_type="PRODUCTION",
            amount=1000,
            currency="CNY",
            transaction_kind="RESERVE",
            idempotency_key="probe-key",
            approval_ref="probe-approval",
            source="probe-budget-id",
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
# 字段级契约演进诊断（F07）
# --------------------------------------------------------------------------- #
SCHEMA_FIELD_SIGNATURES_PATH = CONTRACTS / "runtime" / "schema_field_signatures.yaml"


def _field_signature(spec: dict[str, Any]) -> dict[str, str]:
    """计算一个对象的字段签名：``field → 类型`` 映射（稳定可序列化）。

    类型归一规则：``type`` 取字符串（list 类型连接为 ``a|b``），``$ref`` 取
    ``#/...`` 末段，``enum`` 记 ``enum``，其余记 ``object``。目的是让「字段
    新增/删除/改类型」在签名层可精确 diff，而非只看文件哈希。
    """
    signature: dict[str, str] = {}
    properties = spec.get("properties") or {}
    for field, prop_spec in properties.items():
        if not isinstance(prop_spec, dict):
            signature[field] = "<invalid>"
            continue
        if "$ref" in prop_spec:
            signature[field] = f"ref:{prop_spec['$ref'].rsplit('/', 1)[-1]}"
        elif "type" in prop_spec:
            t = prop_spec["type"]
            signature[field] = (
                "|".join(t) if isinstance(t, list) else str(t)
            )
        elif "enum" in prop_spec:
            signature[field] = "enum"
        elif "const" in prop_spec:
            signature[field] = "const"
        else:
            signature[field] = "object"
    return dict(sorted(signature.items()))


def _current_field_signatures() -> dict[str, dict[str, str]]:
    """从 object_schemas.schema.json（唯一权威源）计算全部对象的字段签名。"""
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    definitions = data.get("definitions", data.get("$defs", {}))
    return {
        name: _field_signature(spec)
        for name, spec in definitions.items()
        if isinstance(spec, dict) and spec.get("type") == "object"
    }


def check_schema_field_evolution() -> bool:
    """字段级契约演进诊断（F07）：把「文件哈希变了」细化为「具体字段变了」。

    背景：freeze_manifest 对 object_schemas.schema.json 只做「文件哈希」校验，
    字段新增/删除/改类型只能体现为「哈希变了」，无法指出「哪个对象的哪个字段
    怎么变」。本函数补上字段级可见性——它从同一份 schema.json 计算字段签名，
    与派生台账比对，输出 add/remove/type-change 的精确 diff。

    台账（schema_field_signatures.yaml）是**派生诊断快照**，不是权威事实源：
    权威源仍是 object_schemas.schema.json；台账只记录「上次显式重签时的字段
    签名」，供 diff 定位用。字段变化本身不是错误（Phase 1 仍在演进），但必须
    **显式重签台账**（`--write-schema-signatures`）——未经重签的字段漂移会在
    这里被点名，引导人去 amendments 登记理由，而不是静默放过。
    """
    current = _current_field_signatures()
    if not current:
        print("[FAIL] object_schemas.schema.json 无任何 object 定义")
        return False

    if not SCHEMA_FIELD_SIGNATURES_PATH.exists():
        print(
            "[FAIL] 缺少字段签名台账 "
            f"{SCHEMA_FIELD_SIGNATURES_PATH.relative_to(ROOT)}："
            "字段级契约演进无法定位，请先执行 --write-schema-signatures 生成"
        )
        return False

    baseline = load_yaml_mapping(SCHEMA_FIELD_SIGNATURES_PATH).get("signatures")
    if not isinstance(baseline, dict):
        print("[FAIL] schema_field_signatures.yaml 的 signatures 必须是映射")
        return False

    ok = True
    # 1. 台账登记但 schema 已无的对象（陈旧签名）。
    for name in sorted(set(baseline) - set(current)):
        print(f"[FAIL] 字段签名台账含已删除对象 {name}（陈旧签名，须重签）")
        ok = False
    # 2. schema 有但台账未登记的对象（新对象，字段级未纳入诊断）。
    for name in sorted(set(current) - set(baseline)):
        print(f"[FAIL] 对象 {name} 未登记字段签名（须重签台账纳入诊断）")
        ok = False
    # 3. 字段级 diff：add / remove / type-change。
    for name in sorted(set(current) & set(baseline)):
        cur = current[name]
        base = baseline[name]
        if not isinstance(base, dict):
            print(f"[FAIL] 台账里 {name} 的字段签名不是映射")
            ok = False
            continue
        cur_fields = set(cur)
        base_fields = set(base)
        for added in sorted(cur_fields - base_fields):
            print(f"[FAIL] {name} 新增字段 {added}（{cur[added]}）")
            ok = False
        for removed in sorted(base_fields - cur_fields):
            print(f"[FAIL] {name} 删除字段 {removed}")
            ok = False
        for changed in sorted(cur_fields & base_fields):
            if cur[changed] != base[changed]:
                print(
                    f"[FAIL] {name}.{changed} 类型变化："
                    f"{base[changed]} → {cur[changed]}"
                )
                ok = False

    if ok:
        print(
            f"[PASS] 字段级契约演进：{len(current)} 个对象字段签名与台账一致"
        )
    return ok


def write_schema_field_signatures() -> None:
    """按当前 object_schemas.schema.json 重写字段签名台账（派生视图）。"""
    current = _current_field_signatures()
    body = {
        "generated_from": "object_schemas.schema.json",
        "note": (
            "派生诊断快照，非权威事实源。字段签名变化须显式重签本台账，"
            "并在 contract_amendments.yaml 登记字段演进理由（F07）。"
        ),
        "signatures": current,
    }
    SCHEMA_FIELD_SIGNATURES_PATH.write_text(
        yaml.safe_dump(body, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(
        f"[PASS] 已重写字段签名台账：{len(current)} 个对象"
    )


# --------------------------------------------------------------------------- #
# 存储定位符：契约 pattern 必须与存储实现同源（P1-07 / #179）
# --------------------------------------------------------------------------- #
STORAGE_LOCATOR_PATTERN_OWNERS = ("AssetVersion", "AssetReference")


def _locator_check_samples() -> list[str]:
    """构造用于两侧比对的样本，正例**从登记表派生**而非手写清单。

    手写正例的问题是「登记表加了新扩展名，样本忘了加」——于是新扩展名在
    契约里被拒、在实现里被收，而闸门仍绿。反例同理：未登记扩展名从登记表
    之外取，不写死具体值。
    """
    from packages.shared.storage_locator import (
        ALLOWED_EXTENSIONS,
        build_locator,
    )

    digest = "0123456789abcdef" * 4
    samples = [build_locator(digest, ext) for ext in sorted(ALLOWED_EXTENSIONS)]
    # 前两段与哈希不符：语法合法但永远读不到内容（本次收敛才发现的历史缺口）。
    samples.append(f"local://ff/ee/{digest}.png")
    # 哈希长短不对 / 非十六进制 / 大写。
    samples.append(f"local://01/23/{digest[:-1]}.png")
    samples.append(f"local://01/23/{'z' * 64}.png")
    samples.append(f"local://01/23/{digest.upper()}.png")
    # 路径穿越的各种写法（形态里没有位置放路径片段，因此都应被拒）。
    samples.append("local://../../etc/passwd")
    samples.append(f"local://01/23/{digest}.png/../../../etc/passwd")
    # 未登记扩展名：从登记表之外取，避免写死一个将来可能被登记的值。
    unregistered = next(
        ext for ext in ("sh", "exe", "tmp", "bin2", "jpg2") if ext not in ALLOWED_EXTENSIONS
    )
    samples.append(f"local://01/23/{digest}.{unregistered}")
    # 旧形态（自由引用）：本次改动的起点，必须两侧都拒。
    samples.append("s3://bucket/frame.png")
    samples.append("")
    return samples


def check_storage_locator_pattern() -> bool:
    """契约里 ``storage_ref.pattern`` 必须与存储层实现判定等价。

    为什么这条闸门必须存在
    ----------------------
    JSON Schema 的 ``pattern`` 只有**有人求值**时才起作用。本仓库里请求侧是
    手写 Pydantic 模型、响应侧由 ``contract_model`` 生成（它只读 ``type``，
    忽略 ``pattern``），因此契约里的 pattern 极易退化成「文档」——写了、但
    没有任何执行点。一旦如此，代码放宽正则而契约不变，两边就永久分叉，
    而所有闸门依旧全绿：这正是「不可机检的判据只能靠人记得看」的形状。

    判据两段，缺一不可
    ------------------
    1. **逐字相等**：抓「只改了一边」这种最常见情形，失败信息一眼可读。
       它本身是弱判据（写法不同可语义相同），所以还需要——
    2. **抽样等价**：把同一批样本分别喂给契约 pattern（``re``，与 ECMA-262
       的反向引用语义一致）与实现判定，结论必须逐条相同。这一条抓的是
       「逐字相同但求值结果不同」的情形，例如反向引用 ``\\1\\2`` 被某一侧
       当成转义字符。
    """
    from packages.shared.storage_locator import (
        LOCATOR_PATTERN,
        is_controlled_locator,
    )

    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    definitions = data.get("definitions", data.get("$defs", {}))
    ok = True
    for name in STORAGE_LOCATOR_PATTERN_OWNERS:
        spec = (definitions.get(name) or {}).get("properties", {}).get("storage_ref")
        if not isinstance(spec, dict):
            print(f"[FAIL] {name}.storage_ref 未在契约中定义")
            ok = False
            continue
        if spec.get("type") != "string":
            print(
                f"[FAIL] {name}.storage_ref 类型为 {spec.get('type')!r}，"
                "应为 string：受控定位符是**一个**值，不是结构化容器"
            )
            ok = False
        pattern = spec.get("pattern")
        if pattern != LOCATOR_PATTERN:
            print(
                f"[FAIL] {name}.storage_ref.pattern 与存储实现不同源：\n"
                f"    契约：{pattern!r}\n"
                f"    实现：{LOCATOR_PATTERN!r}\n"
                "    修法：以 packages/shared/storage_locator.LOCATOR_PATTERN 为准同步契约"
            )
            ok = False

    # 抽样等价：只要有任一处两侧结论不同，说明「契约说的」与「实现做的」不是
    # 同一件事——此时讨论哪种写法更规范没有意义，先让它们一致。
    pattern_value = (
        (definitions.get("AssetVersion") or {})
        .get("properties", {})
        .get("storage_ref", {})
        .get("pattern")
    )
    if isinstance(pattern_value, str):
        compiled = re.compile(pattern_value)
        for sample in _locator_check_samples():
            contract_verdict = bool(compiled.match(sample))
            implementation_verdict = is_controlled_locator(sample)
            if contract_verdict != implementation_verdict:
                shown = sample if sample else "<空串>"
                print(
                    f"[FAIL] 定位符判定两侧不一致：{shown!r} → "
                    f"契约={contract_verdict}，实现={implementation_verdict}"
                )
                ok = False
    if ok:
        print(
            f"[PASS] 存储定位符 pattern 与实现同源"
            f"（{len(STORAGE_LOCATOR_PATTERN_OWNERS)} 处声明 + 抽样等价）"
        )
    return ok


# --------------------------------------------------------------------------- #
# 字段级演进登记（F07 配套）：理由必须可机检
# --------------------------------------------------------------------------- #
CONTRACT_AMENDMENTS_PATH = CONTRACTS / "runtime" / "contract_amendments.yaml"
_AMENDMENT_REQUIRED_FIELDS = (
    "object",
    "field",
    "from",
    "to",
    "decided_at",
    "recorded_by",
    "reason",
    "contract_ref",
    "implementation",
)


def check_contract_amendments() -> bool:
    """演进登记里的 ``to`` 必须等于当前字段签名。

    这条判据刻意**不**去验证历史（「上次真的是 object 吗」无法从当前 schema
    反推，只能从 git 翻）。它验证的是另一件事：登记仍在描述现在。

    为什么这一条足以防止台账变垃圾桶
    --------------------------------
    字段后来又变一次（string → 别的）时，本条登记的 ``to`` 对不上当前签名，
    闸门报「陈旧登记」，人必须补一条新的。而纯文字的豁免没有这个性质——它
    写完之后与事实脱钩，却永远不会报错。
    """
    if not CONTRACT_AMENDMENTS_PATH.exists():
        print(
            "[FAIL] 缺少字段级演进登记 "
            f"{CONTRACT_AMENDMENTS_PATH.relative_to(ROOT)}（F07 配套，记录改字段的理由）"
        )
        return False

    body = load_yaml_mapping(CONTRACT_AMENDMENTS_PATH)
    amendments = body.get("amendments")
    if not isinstance(amendments, list) or not amendments:
        print("[FAIL] contract_amendments.yaml 的 amendments 必须是非空列表")
        return False

    current = _current_field_signatures()
    ok = True
    for index, entry in enumerate(amendments):
        if not isinstance(entry, dict):
            print(f"[FAIL] amendments[{index}] 不是映射")
            ok = False
            continue
        missing = [key for key in _AMENDMENT_REQUIRED_FIELDS if not entry.get(key)]
        if missing:
            print(
                f"[FAIL] amendments[{index}] 缺字段 {missing}："
                "登记必须有理由、依据与落地位置，不记进度"
            )
            ok = False
            continue
        object_name = str(entry["object"])
        field_name = str(entry["field"])
        signature = current.get(object_name)
        if signature is None:
            print(f"[FAIL] amendments[{index}] 指向已删除对象 {object_name}（陈旧登记）")
            ok = False
            continue
        actual = signature.get(field_name)
        if actual is None:
            print(
                f"[FAIL] amendments[{index}] 指向已删除字段 "
                f"{object_name}.{field_name}（陈旧登记）"
            )
            ok = False
            continue
        if actual != str(entry["to"]):
            print(
                f"[FAIL] amendments[{index}] 陈旧登记：{object_name}.{field_name} "
                f"登记的 to={entry['to']!r}，当前签名是 {actual!r}——"
                "字段又变了，必须补一条新登记而不是改旧的"
            )
            ok = False
    if ok:
        print(f"[PASS] 字段级演进登记：{len(amendments)} 条与当前签名一致")
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
        # 两种授权依赖都算「接上了」，且 require_resource_role 是更强的那个：
        # 它在动作级 RBAC/Capability 之外还判定资源归属（AUTH-1 的资源输入）。
        # 只认 require_role 会让资源级改造被误判为「没挂授权」。
        # 允许换行/缩进（资源级依赖参数较多，动作键常在下一行）。
        wired = bool(
            re.search(
                rf'require_(?:resource_)?role\(\s*"{re.escape(key)}"', source
            )
        )
        if not wired:
            print(
                f"[FAIL] {key} 在 {module_rel} 中没有挂 require_role"
                f'("{key}") 或 require_resource_role("{key}", ...)'
                "——端点存在但没有授权依赖"
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


# 状态名：契约里统一为大写下划线标识（GENERATED / TECHNICAL_QC）。
# allowed_states 已是结构化列表，此处仅用于校验单个状态值的书写形态。
_STATE_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


def _reachable_states(
    edges: set[tuple[str, str]], initial: str | None = None
) -> set[str]:
    """从初始状态出发，沿实现转移边求可达闭包。

    ``initial`` 来自契约的 ``initial_state``。为什么必须支持它：Shot 的
    BLOCKED→NOT_STARTED 是合法回退边，于是**每个状态都有入边**，
    「无入边即初始」的旧启发式会得到空集，把整个对象误判成全状态不可达。
    契约显式声明初始态后，根由契约给，不由图论猜。
    """
    if initial:
        initials = {initial}
    else:
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

    口径修正（v2）：比对的「契约状态」取自 ``allowed_states``，不再从文本状态链
    里正则抓大写词。旧口径下，``AssetVersion`` 的旁支状态（QUARANTINED 等）只能
    靠「note 里出现过大写词」被顺带抓到，抓不到就静默漏掉一整类缺口。
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
        states = {str(s) for s in entry.get("allowed_states") or []}
        edges = _load_impl_transitions(module_path, var_name)
        initial = entry.get("initial_state")
        reachable = _reachable_states(
            edges, str(initial) if initial is not None else None
        )
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


def _implemented_relay_events() -> set[str]:
    """relay 已注册的 event_type 也是合法的转移入口凭据（R42 T3）。

    T3 的 owner_action 刻意是事件名而不是 HTTP action：成功必须来自登记回执，
    不能借用 Worker 的 ``GENERATION.PROCESS``。入口是否存在因此不能只看 HTTP
    动作台账——那会把真实已实现的事件入口误判成缺口，反过来诱使人把 owner 改成
    Worker action，等于把自报成功重新开门。这里从**实际注册的 handler 集合**派生，
    不手写事件清单；注册消失时债务判定会自动变红。
    """
    from workers.relay.handlers import build_handlers

    sentinel = object()
    return set(
        build_handlers(
            qc_sessionmaker=sentinel,
            production_planning_sessionmaker=sentinel,
            asset_sessionmaker=sentinel,
        )
    )


def _declared_transition_entry_gaps() -> dict[str, dict[str, dict[str, object]]]:
    """读取并校验 ``transition_entry_gaps`` 分区（形状坏 = 契约损坏 → ValueError）。

    结构是三层：``对象 → 转移边标签 → 债务详情``。**边级**而不是对象级，理由见
    ``check_transition_entry_gaps`` 的 docstring。

    文件缺失返回空登记：**不再无条件 PASS**——是否真的「无债务」由反向完备性
    检查判定，删文件不能把缺口变绿。
    """
    if not STATE_GAPS_PATH.exists():
        return {}
    raw = load_yaml_mapping(STATE_GAPS_PATH).get("transition_entry_gaps") or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{STATE_GAPS_PATH.name} 的 transition_entry_gaps 必须是"
            f"「对象 → 转移边 → 债务详情」映射，实际是 {type(raw).__name__}"
        )
    declared: dict[str, dict[str, dict[str, object]]] = {}
    for obj_name, edges in raw.items():
        if not isinstance(edges, dict):
            raise ValueError(
                f"{STATE_GAPS_PATH.name} 的 transition_entry_gaps.{obj_name} "
                f"必须是「转移边 → 债务详情」映射，实际是 {type(edges).__name__}"
            )
        per_edge: dict[str, dict[str, object]] = {}
        for label, detail in edges.items():
            if not isinstance(detail, dict):
                raise ValueError(
                    f"{STATE_GAPS_PATH.name} 的 "
                    f"transition_entry_gaps.{obj_name}.{label} 必须是映射，"
                    f"实际是 {type(detail).__name__}"
                )
            per_edge[str(label)] = detail
        declared[str(obj_name)] = per_edge
    return declared


def check_transition_entry_gaps() -> bool:
    """校验「转移无 HTTP 入口」债务登记的诚实性（审计 P2-1 的闸门化）。

    **为什么判到边而不是对象**（A23 的判据升级）
    --------------------------------------------------
    旧的判据是对象级：只要该对象有**任意一个**已实现的状态变更 action，就认为
    「入口已补上」。这留了一个固定的盲区——一个对象有几条边，其中一条有入口、
    其余没有时，判据照样绿。A13 之后的 ``ConstraintPack`` 正好是这种形态：
    ``DRAFT→ACTIVE`` 归 ``CONSTRAINT.PACK.LOCK``（已实现），三条治理边归
    ``CONSTRAINT.PACK.STATUS.CHANGE``（未实现），对象级判据看不见后三条。

    「有边、可达、但没有任何动作能走」与「对象完全没有入口」是同一类失效，
    而对象级判据只覆盖后者。判到边之后，应检集合直接来自边表的
    ``Transition.owner_action``——不需要再按 action_key 命名猜「哪些动作会改
    状态」，也不需要额外维护一张手写清单（手写清单的陈旧方向恰好是漏检）。

    四类校验：

    1. 正向形状：登记对象必须是已实现状态机；登记的边必须真实存在（防手抄错边）；
    2. 正向完整：``reason`` / ``contract_ref`` / ``unblocks_when`` 三项齐全；
    3. 正向防陈旧：该边的 ``owner_action`` 已在运行时台账 implemented 中 → 入口
       真的补上了，登记必须删除；
    4. 反向完备性：每条 ``owner_action`` 未实现的边**必须**登记——删条目、甚至
       删整个债务文件，闸门都会报红。

    两个刻意的口径：
    - 「已补上入口」只认**运行时台账 implemented**，不认 action_registry 的登记。
      本项目要求「先在契约登记、再实现」，若按契约登记判定，先登记
      ``CONSTRAINT.PACK.STATUS.CHANGE`` 就会把真实缺口误判成陈旧债务，反而逼着
      人删掉它。
    - 本判据**不**回答「action 实现了但服务方法没写」这类更深的问题：那需要
      跑通业务路径，属集成测试的语义面。判据只对自己的语义面负责。
    """
    declared = _declared_transition_entry_gaps()
    implemented = _implemented_action_keys() | _implemented_relay_events()
    lifecycles, lifecycle_problems = _implemented_lifecycles()
    ok = True
    count = 0
    edge_total = 0

    for problem in lifecycle_problems:
        print(f"[FAIL] {problem}")
        ok = False

    # 1-3. 正向：登记必须指向真实的、仍未实现的边。
    for obj_name in sorted(declared):
        edges = declared[obj_name]
        if obj_name not in _STATE_MACHINE_SOURCES:
            print(f"[FAIL] {obj_name} 不在已实现状态机清单里，不得登记转移入口债务")
            ok = False
            continue
        lifecycle = lifecycles.get(obj_name)
        if lifecycle is None:
            print(f"[FAIL] {obj_name} 找不到 Lifecycle 实例，无法判定其转移入口债务")
            ok = False
            continue
        known = {edge.label: edge for edge in lifecycle.edges}
        for label in sorted(edges):
            count += 1
            detail = edges[label]
            edge = known.get(label)
            if edge is None:
                print(
                    f"[FAIL] {obj_name} 的债务登记指向不存在的转移边 {label!r}："
                    f"该对象已登记的边为 {sorted(known)}"
                )
                ok = False
                continue
            missing = [
                key
                for key in _TRANSITION_DEBT_REQUIRED
                if not str(detail.get(key, "")).strip()
            ]
            if missing:
                print(f"[FAIL] {obj_name} 边 {label} 的转移入口债务缺字段: {missing}")
                ok = False
            if edge.owner_action in implemented:
                print(
                    f"[FAIL] {obj_name} 边 {label} 的 owner_action "
                    f"{edge.owner_action} 已实现——该债务已陈旧，必须删条目"
                )
                ok = False

    # 4. 反向完备性：每条「owner_action 未实现」的边都必须登记。
    for obj_name in sorted(_STATE_MACHINE_SOURCES):
        lifecycle = lifecycles.get(obj_name)
        if lifecycle is None:
            # 已由 lifecycle_problems 报出（找不到 Lifecycle 的对象无法判定边）。
            continue
        declared_edges = declared.get(obj_name, {})
        for edge in lifecycle.edges:
            edge_total += 1
            if edge.owner_action in implemented:
                continue
            if edge.label in declared_edges:
                continue
            print(
                f"[FAIL] {obj_name} 的转移边 {edge.label} 由未实现的 "
                f"{edge.owner_action} 拥有，却未在 state_machine_gaps.yaml 的 "
                "transition_entry_gaps 登记债务"
                "——缺口不能靠「删条目/删文件」变绿"
            )
            ok = False

    if ok:
        print(
            f"[PASS] 状态转移入口债务：显式登记 {count} 条边"
            f"（{len(_STATE_MACHINE_SOURCES)} 个状态机共 {edge_total} 条边逐边比对）"
        )
    return ok


_GUARD_REQUIRED = ("transition", "fields", "rule")


def guard_problems(obj_name: str, entry: dict, definition: dict) -> list[str]:
    """校验单个对象的 ``guards`` 区，返回问题列表（空列表 = 通过）。

    抽成纯函数是为了可测试：闸门若只能对整份 state_machines.yaml 判定，坏守卫
    就只能靠「故意改坏契约再跑 CI」来验证，等于没有回归保护。测试直接构造
    entry + definition 调本函数即可。

    五条校验（与 check_state_machine_guards 的 docstring 一致）：
    1. ``transition`` 必须是 ``{from, to}`` 结构化边——v2 不再解析字符串状态链；
    2. 该边必须是此对象 ``transitions`` 已登记的边；
    3. ``fields`` 必须是非空字符串列表；
    4. ``fields`` 每一项都在 schema 的 properties 中；
    5. ``transition`` / ``fields`` / ``rule`` 三项齐全。
    """
    problems: list[str] = []
    guards = entry.get("guards")
    if guards is None:
        return problems
    if not isinstance(guards, list):
        return [f"{obj_name}.guards 必须是列表"]

    declared_edges = contract_edges(entry)
    properties = definition.get("properties") or {}

    for idx, guard in enumerate(guards):
        if not isinstance(guard, dict):
            problems.append(f"{obj_name}.guards[{idx}] 必须是映射")
            continue
        missing = [k for k in _GUARD_REQUIRED if not guard.get(k)]
        if missing:
            problems.append(f"{obj_name}.guards[{idx}] 缺字段: {missing}")
            continue

        edge = guard["transition"]
        if not isinstance(edge, dict) or set(edge) != {"from", "to"} or not all(
            isinstance(edge.get(k), str) and edge[k].strip() for k in ("from", "to")
        ):
            problems.append(
                f"{obj_name}.guards[{idx}].transition 必须是 {{from, to}} 结构化边"
                f"（收到 {edge!r}）；状态值不接受字符串解析"
            )
            continue
        pair = (edge["from"].strip(), edge["to"].strip())
        if pair not in declared_edges:
            problems.append(
                f"{obj_name}.guards[{idx}] 指向未登记的转移: {[pair]}"
                "（守卫不能保护一条状态机不允许的边）"
            )

        fields = guard["fields"]
        if not isinstance(fields, list) or not all(
            isinstance(f, str) and f.strip() for f in fields
        ):
            problems.append(f"{obj_name}.guards[{idx}].fields 必须是非空字符串列表")
            continue
        unknown = [f for f in fields if f not in properties]
        if unknown:
            problems.append(
                f"{obj_name}.guards[{idx}] 引用了 schema 未声明的字段: {unknown}"
            )

    return problems


def check_state_machine_guards() -> bool:
    """校验状态机 ``guards`` 区声明的转移与字段是真实可校验的。

    为什么需要这条闸门：``guards`` 是「某条转移必须携带哪些字段」的唯一契约
    表达，但它本身只是「转移边 + 字段名」的声明。字段名写错（例如把
    ``blocked_reason`` 写成 ``block_reason``）、或指向一条根本不存在的边，
    YAML 依然合法——实现时照着写，就会得到一个永远不放行、或永远不触发的
    守卫，而 CI 全绿。

    本闸门把 guards 钉在可校验事实上，并额外要求：声明 guards 的对象必须有
    object schema，否则被引用的字段无处校验，守卫会退化成注释。

    刻意不要求所有对象都有 guards：只有「转移需要携带证据」的对象才需要声明，
    无证据要求的转移不必造空守卫。
    """
    data = load_yaml_mapping(CONTRACTS / "state_machines" / "state_machines.yaml")
    objects = data.get("objects", [])
    schema = json.loads(OBJECT_SCHEMAS_PATH.read_text(encoding="utf-8"))
    definitions = schema.get("definitions") or schema.get("$defs") or {}

    ok = True
    object_count = 0
    guard_count = 0

    for entry in objects:
        if not isinstance(entry, dict) or not entry.get("guards"):
            continue
        obj_name = str(entry.get("object", ""))
        object_count += 1
        definition = definitions.get(obj_name)
        if not isinstance(definition, dict):
            print(
                f"[FAIL] {obj_name} 声明了 guards，但没有 object schema："
                "守卫字段无处校验（守卫会退化成注释）"
            )
            ok = False
            continue
        problems = guard_problems(obj_name, entry, definition)
        for problem in problems:
            print(f"[FAIL] {problem}")
        if problems:
            ok = False
        else:
            guard_count += len(entry["guards"])

    if ok:
        print(
            f"[PASS] 状态机守卫：{object_count} 个对象 / {guard_count} 条守卫"
            "的转移与字段全部可校验"
        )
    return ok


# --------------------------------------------------------------------------- #
# 子集合冻结覆盖：有子表引用的生命周期容器必须声明 frozen_states
# --------------------------------------------------------------------------- #
# 表名 → 生命周期对象的变量名。显式声明：表名到对象名不是机械映射
# （projects→Project 能猜，script_revisions→ScriptRevision 就会猜错复数规则）。
# 完备性由下面的判据双向守住，不依赖这张表是否写全。
_FROZEN_PARENT_TABLES: dict[str, str] = {
    "projects": "PROJECT_LIFECYCLE",
    "episodes": "EPISODE_LIFECYCLE",
    "scenes": "SCENE_LIFECYCLE",
    # A22：规格挂在修订下。已 SUPERSEDED 的修订不再接受新规格——它是冻结态
    # （规格只为可生产的修订存在），不是「暂时不可用」。
    "shot_revisions": "SHOT_REVISION_LIFECYCLE",
}

# 被引用但**不需要**冻结的父表。每条都必须给出理由——空理由等同于没表态。
_FREEZE_EXEMPT_PARENTS: dict[str, str] = {
    "organizations": "归属锚点，不是生命周期对象（没有状态机）",
    "shots": "修订与返工必须在 PRODUCTION_COMPLETE 之后创建，冻结会挡住合法流程",
    "rework_tasks": "被 shots 以值引用（rework_basis_ref），不是容器归属",
}


_PLANNING_SCHEMA = "project_planning"


def _planning_tables() -> dict[str, Any]:
    """规划域的表，按**不带 schema** 的表名索引。

    两个细节都是必需的，不是风格问题：

    - **按 schema 过滤**：ORM metadata 是全局共享的，测试环境一旦 import 了资产域，
      metadata 里就多出 ``asset_versions`` / ``qc_reports``。不过滤的话，同一份判据
      会随 import 顺序给出不同结果——判据不确定比判据覆盖不全更糟。过滤后本判据
      的语义面明确是「规划域容器」；将来资产域接同一规则时，把 schema 显式加进来，
      那是一次有意识的扩张，而不是被某次无关 import 悄悄改变判决。
    - **键取 ``table.name``**：``metadata.tables`` 的键带 schema 前缀，而外键的
      ``column.table.name`` 不带。混用会让存在性判断恒假，表现为「所有登记的表
      都不存在」——一个方向错误的判据。
    """
    from services.project_planning.repository import orm

    metadata = orm.ProjectModel.metadata
    return {
        table.name: table
        for table in metadata.tables.values()
        if table.schema == _PLANNING_SCHEMA
    }


def check_child_creation_freeze_coverage() -> bool:
    """有子表引用的生命周期容器必须声明 ``frozen_states``。

    为什么需要这条闸门（根因，不是加一道检查）
    ---------------------------------------------
    ``frozen_states`` 的默认值是空元组，于是「漏声明」与「确实不该冻结」在运行期
    完全同形：四条创建路径都不会报错，只是照常把子对象写进去。没有判据时，
    「归档即冻结」只是一份自觉遵守的约定——而这正是本项目反复栽的形态
    （``qc_reports``/``episodes``/``OWNERSHIP_STACK`` 三次）。

    集合一律**派生**：父表取自 ORM 外键，不手写。手写清单的陈旧方向恰好是不安全
    的那一侧——新增一张子表就是漏检，而漏检表现为绿灯。三向判据：

    1. 登记为冻结容器 → 必须有子表引用且 ``frozen_states`` 非空；
    2. 登记为豁免 → 必须存在、仍有子表引用、且写了理由（防陈旧豁免）；
    3. 有子表引用却两处都没登记 → 红（新增父表必须明确表态）。
    """
    from services.project_planning.domain import state_machine

    tables = _planning_tables()
    referenced: dict[str, set[str]] = {}
    for table in tables.values():
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent != table.name:
                referenced.setdefault(parent, set()).add(table.name)

    problems: list[str] = []

    for table, reason in _FREEZE_EXEMPT_PARENTS.items():
        if table not in tables:
            problems.append(f"冻结豁免登记了不存在的表 {table!r}")
            continue
        if not reason.strip():
            problems.append(f"冻结豁免表 {table!r} 未写明理由")
        if table not in referenced:
            problems.append(
                f"冻结豁免登记了 {table!r}，但它已没有子表引用："
                "陈旧豁免会让未来新增的子表绕过本判据"
            )

    for table, var_name in _FROZEN_PARENT_TABLES.items():
        if table not in tables:
            problems.append(f"冻结容器映射登记了不存在的表 {table!r}")
            continue
        lifecycle = getattr(state_machine, var_name, None)
        if lifecycle is None:
            problems.append(f"{table!r} 映射到不存在的生命周期对象 {var_name}")
            continue
        if table not in referenced:
            problems.append(
                f"{table!r} 被登记为冻结容器，但已没有子表引用它"
                "（归属关系变了，映射需要同步）"
            )
            continue
        if not lifecycle.frozen_states:
            problems.append(
                f"{table!r}（{lifecycle.object_name}）被 {sorted(referenced[table])} "
                "引用，但未声明 frozen_states：父对象归档后仍能新增子对象"
            )

    for parent in sorted(referenced):
        if parent in _FROZEN_PARENT_TABLES or parent in _FREEZE_EXEMPT_PARENTS:
            continue
        problems.append(
            f"{parent!r} 被 {sorted(referenced[parent])} 引用，但既未登记为冻结容器，"
            "也未登记为豁免：新增父表必须明确表态（ADR-R28 §6.5）"
        )

    for problem in problems:
        print(f"[FAIL] {problem}")
    if problems:
        return False
    print(
        f"[PASS] 子集合冻结覆盖：{len(_FROZEN_PARENT_TABLES)} 个冻结容器 / "
        f"{len(_FREEZE_EXEMPT_PARENTS)} 个已登记豁免 / "
        f"{len(referenced)} 个被引用的父表全部表态"
    )
    return True


# --------------------------------------------------------------------------- #
# 冻结态：契约声明 ↔ 实现声明（策略本身必须是契约事实）
# --------------------------------------------------------------------------- #
_FROZEN_STATES_FIELD = "frozen_states"


def _implemented_lifecycles() -> tuple[dict[str, Any], list[str]]:
    """反射已实现状态机模块里的 ``Lifecycle`` 实例，按 ``object_name`` 索引。

    为什么派生而不是再写一张「对象名 → Lifecycle 变量名」的表：那是又一份手写
    清单，新增状态机时它不会自己跟上，而漏登记的方向恰好是「少比对一个对象」——
    表现为绿灯。派生之后，模块里新出现的 Lifecycle 会**自己**进入比对范围；
    「找不到」与「重名」由返回值显式报出，而不是被静默跳过。

    只扫描 ``_STATE_MACHINE_SOURCES`` 引用的模块，而不是全仓：本判据的语义面是
    「已实现状态机的冻结策略」，跨域扩张（资产域接同一规则）应当是一次有意识的
    登记，而不是被某次无关 import 悄悄改变判决。
    """
    import importlib

    from packages.shared.lifecycle import Lifecycle

    lifecycles: dict[str, Any] = {}
    owners: dict[str, str] = {}
    problems: list[str] = []
    for module_path in sorted({path for path, _ in _STATE_MACHINE_SOURCES.values()}):
        module = importlib.import_module(module_path)
        for var_name, value in vars(module).items():
            if not isinstance(value, Lifecycle):
                continue
            owner = f"{module_path}.{var_name}"
            previous = owners.get(value.object_name)
            if previous is not None:
                problems.append(
                    f"object_name={value.object_name!r} 有多个 Lifecycle"
                    f"（{previous} / {owner}）：冻结策略会出现两份声明，"
                    "比对结果取决于遍历顺序"
                )
                continue
            owners[value.object_name] = owner
            lifecycles[value.object_name] = value
    return lifecycles, problems


def frozen_state_problems(entry: dict[str, Any], lifecycle: Any) -> list[str]:
    """单个对象：契约 ``frozen_states`` 与实现 ``Lifecycle.frozen_states`` 的比对。

    抽成纯函数是为了可测试，理由与 ``guard_problems`` 相同：闸门若只能对整份
    ``state_machines.yaml`` 判定，坏声明就只能靠「改坏契约再跑 CI」来验证，
    等于没有回归保护。

    四条校验：
    1. 字段必须**显式存在**——默认值与「声明为空」在运行期同形；
    2. 必须是字符串列表，且无重复（重复不影响 ``in``，但会让错误信息失真）；
    3. 每个值必须在 ``allowed_states`` 内（否则 ``in`` 恒假，冻结静默失效）；
    4. 两侧集合相等，差值分别报出（只报「不一致」会让人猜是哪一侧多了）。
    """
    object_name = lifecycle.object_name
    if _FROZEN_STATES_FIELD not in entry:
        return [
            f"{object_name} 的契约条目未声明 {_FROZEN_STATES_FIELD}："
            "「没声明」与「声明为空」在运行期同形，必须显式写出（不冻结写 []）"
        ]

    declared = entry.get(_FROZEN_STATES_FIELD)
    if not isinstance(declared, list):
        return [
            f"{object_name} 的 {_FROZEN_STATES_FIELD} 必须是列表，"
            f"实际是 {type(declared).__name__}"
        ]

    problems: list[str] = []
    labels: list[str] = []
    for item in declared:
        if not isinstance(item, str) or not item.strip():
            problems.append(
                f"{object_name} 的 {_FROZEN_STATES_FIELD} 含非字符串或空值：{item!r}"
            )
            continue
        labels.append(item.strip())

    duplicated = sorted({label for label in labels if labels.count(label) > 1})
    if duplicated:
        problems.append(
            f"{object_name} 的 {_FROZEN_STATES_FIELD} 有重复项：{duplicated}"
        )

    allowed = {str(state) for state in (entry.get("allowed_states") or [])}
    unknown = sorted(set(labels) - allowed) if allowed else []
    if unknown:
        problems.append(
            f"{object_name} 的 {_FROZEN_STATES_FIELD} 含未声明状态 {unknown}："
            "in 判定将永远为假，冻结会静默失效"
        )

    implemented = {str(getattr(state, "value", state)) for state in lifecycle.frozen_states}
    if set(labels) != implemented:
        problems.append(
            f"{object_name} 的冻结态契约与实现不一致："
            f"契约多 {sorted(set(labels) - implemented)} / "
            f"实现多 {sorted(implemented - set(labels))}"
        )
    return problems


def check_frozen_state_contract() -> bool:
    """冻结态必须是**契约事实**，且与实现逐对象一致。

    为什么把冻结策略搬进契约（根因）
    --------------------------------
    A19 之后「归档即冻结」的判定已经统一，但**策略本身**只写在代码里：改一行
    元组就能让归档后的项目重新接受新集数，而四道闸门全绿——覆盖判据只回答
    「有没有声明」，不回答「声明的是哪几个状态」。这与本项目对转移边的处理方式
    不一致：转移边在契约里，改边必须同改契约，``freeze_manifest --check`` 还会因
    哈希漂移报红。冻结态决定「父对象封存后还能不能长出新子对象」，是与转移同等
    强度的生命周期事实，没有理由成为唯一的例外。

    搬进契约的收益不是「多一处校验」，而是**判断变化从此有痕迹**：
    - 改代码不改契约 → 本判据红；
    - 改契约 → 文件哈希变更，``freeze_manifest --check`` 红，必须重新签清单；
    - 重新签清单 → 需要一条已批准的修订登记。
    三步都要求人看见「COMPLETED 的冻结语义变了」，而不是一行 diff 混在别的改动里。

    三向完备：已实现对象必须有 Lifecycle（否则策略无处声明）；契约条目必须显式
    声明该字段；两侧集合必须相等。另有反向一条：有 Lifecycle 却没登记进
    ``_STATE_MACHINE_SOURCES`` 的对象同样报红——它的转移与冻结都不会被比对。
    """
    data = load_yaml_mapping(CONTRACTS / "state_machines" / "state_machines.yaml")
    objects = data.get("objects", [])
    lifecycles, problems = _implemented_lifecycles()

    for object_name in sorted(lifecycles):
        if object_name not in _STATE_MACHINE_SOURCES:
            problems.append(
                f"{object_name} 有 Lifecycle 但未登记进 _STATE_MACHINE_SOURCES："
                "转移表与冻结策略都不会被本闸门比对"
            )

    compared = 0
    for object_name in sorted(_STATE_MACHINE_SOURCES):
        lifecycle = lifecycles.get(object_name)
        if lifecycle is None:
            problems.append(
                f"{object_name} 已实现状态机但找不到 Lifecycle 实例："
                "冻结策略无处声明，无法与契约比对"
            )
            continue
        entry = next((o for o in objects if o.get("object") == object_name), None)
        if entry is None:
            problems.append(f"state_machines.yaml 中未找到 {object_name} 条目")
            continue
        compared += 1
        problems.extend(frozen_state_problems(entry, lifecycle))

    for problem in problems:
        print(f"[FAIL] {problem}")
    if problems:
        return False
    print(f"[PASS] 冻结态契约一致：{compared} 个已实现状态机逐对象比对")
    return True


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


# --------------------------------------------------------------------------- #
# 对象引用目标解析（ADR-R19）
# --------------------------------------------------------------------------- #
_REFERENCE_FIELD_RE = re.compile(r"(_id|_ids|_ref|_refs)$")


def declared_reference_fields(definitions: dict[str, Any]) -> set[tuple[str, str]]:
    """返回所有已声明的 ``(对象, 字段)`` 引用对。"""
    declared: set[tuple[str, str]] = set()
    for name, spec in definitions.items():
        if not isinstance(spec, dict):
            continue
        targets = spec.get("x-afs-reference-targets")
        if isinstance(targets, dict):
            declared.update((name, field) for field in targets)
    return declared


def reference_target_problems(definitions: dict[str, Any]) -> list[str]:
    """校验 ``x-afs-reference-targets`` 声明的对象引用是否可解析。

    为什么需要：Scene 曾长期没有 schema，而 ``Shot.scene_id`` /
    ``ShotRevision.scene_id`` 一直指向它——所有闸门全绿。根因是既有校验只看
    ``action_registry.resource_type`` 引用了哪些类型，**字段级的「对象指向对象」
    从来没有闸门去看**，悬空引用只能靠人肉比对发现。

    把引用意图显式声明成 ``x-afs-reference-targets``（字段 → 目标对象）后，本函数
    逐条解析，三条规则：
    1. 声明的字段必须真的在该对象 properties 里（防声明腐化）；
    2. 声明的目标必须是已定义对象（悬空引用直接报红）；
    3. 目标缺失时正确处置是补 schema 或登记缺口台账，**不是删声明**——删声明只会让
       悬空引用重新变隐形。

    覆盖边界（输出里显式打印，避免假全覆盖）：本函数只保证「已声明的引用不悬空」，
    不保证「所有引用都已声明」。引用类字段当前共 475 个，其中大量是 ``entity_id`` /
    ``audit_ref`` 这类非对象引用；按命名自动推导会产出 393 条误报，因此只对场次域与
    镜头域显式声明，其余作为已知覆盖缺口写在 ADR-R19。
    """
    problems: list[str] = []
    for name, spec in definitions.items():
        if not isinstance(spec, dict):
            continue
        targets = spec.get("x-afs-reference-targets")
        if targets is None:
            continue
        if not isinstance(targets, dict) or not targets:
            problems.append(
                f"{name}.x-afs-reference-targets 必须是非空「字段 → 对象」映射"
            )
            continue
        properties = spec.get("properties") or {}
        for field, target in targets.items():
            if field not in properties:
                problems.append(
                    f"{name}.x-afs-reference-targets 声明了不存在字段: {field}"
                )
            if not isinstance(target, str) or not target.strip():
                problems.append(f"{name}.{field} 的引用目标必须是非空字符串")
                continue
            if target not in definitions:
                problems.append(
                    f"{name}.{field} 指向未定义对象 {target}：悬空引用"
                    "（补 schema 或登记缺口台账，不得删声明了事）"
                )
    return problems


def check_reference_targets() -> bool:
    """对象引用必须指向已定义对象，悬空引用不得靠删声明变绿。"""
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    definitions = data.get("definitions", data.get("$defs", {}))
    problems = reference_target_problems(definitions)
    for problem in problems:
        print(f"[FAIL] {problem}")
    if problems:
        return False

    declared = declared_reference_fields(definitions)
    uncovered = sum(
        1
        for name, spec in definitions.items()
        if isinstance(spec, dict)
        for field in (spec.get("properties") or {})
        if _REFERENCE_FIELD_RE.search(field) and (name, field) not in declared
    )
    print(
        f"[PASS] 引用目标可解析：{len(declared)} 条声明全部命中已定义对象"
        f"（覆盖 {len({n for n, _ in declared})} 个对象）；"
        f"未声明的引用类字段 {uncovered} 个（覆盖边界见 ADR-R19）"
    )
    return True


# --------------------------------------------------------------------------- #
# 状态枚举一致性：state_machines → enum_registry → object_schemas → Runtime
# --------------------------------------------------------------------------- #
# 为什么要有这一整段
# ----------------
# 上一版只有一条窄检查：state_machines 的对象集 ⊆ enum_registry 的对象集，且
# 注释明说「刻意不比状态集」。结果是三处状态集合可以各自漂移而全绿：
#   - state_machines 有状态、Schema 裸 string（76 个 status 里 70 个）；
#   - enum_registry 有状态、state_machines 没有（PG0 / PG3 / PMS / PASS）；
#   - Schema 有 enum、权威集合里没有（BudgetTransferRequest 的 8 个值）。
# 三条都能同时成立，因为没有任何一条规则把它们连起来。下面的闸门就是这三条加上
# 转移边、台账分类、别名层与运行时的双向校验；每条都能单独注入坏数据测试。
STATUS_TAXONOMY_PATH = CONTRACTS / "enums" / "status_field_taxonomy.yaml"
STATUS_LAYERS_PATH = CONTRACTS / "enums" / "status_layers.yaml"
FIELD_AUTHORITY_PATH = CONTRACTS / "enums" / "field_authority.yaml"
FIELD_VALUE_SETS_PATH = CONTRACTS / "enums" / "field_value_sets.yaml"
ENUM_REGISTRY_PATH = CONTRACTS / "enums" / "enum_registry.yaml"

CLASS_LIFECYCLE = "lifecycle"
CLASS_ENVELOPE_ONLY = "envelope_only"
CLASS_LIFECYCLE_GAP = "lifecycle_gap"
STATUS_CLASSES = (CLASS_LIFECYCLE, CLASS_ENVELOPE_ONLY, CLASS_LIFECYCLE_GAP)
DEBT_FIELDS = ("reason", "contract_ref", "unblocks_when")

# 运行时已实现状态机的对象 → (领域枚举模块, 类名)。
# 只有列在这里的对象才要求 Domain Enum 与数据库 CHECK 对齐——未实现的对象
# 不提前造 ORM 表，也不假装它有约束。
_STATUS_ENUM_SOURCES: dict[str, tuple[str, str]] = {
    "Project": ("services.project_planning.domain.enums", "ProjectStatus"),
    "ScriptRevision": ("services.project_planning.domain.enums", "ScriptStatus"),
    "Episode": ("services.project_planning.domain.enums", "EpisodeStatus"),
    "AssetVersion": ("services.asset_service.domain.enums", "AssetStatus"),
    # A22 回填：A13 交付的五个对象当时已有枚举与库层 CHECK，却没有进这两张表，
    # 于是「契约收紧枚举、库里还接受任意字符串」对它们无从发现——应检集合缺行
    # 的表现是绿色，方向和「漏加策略」一样。规格与本批一并补上，六个对象
    # 从此都被逐值比对。
    "Scene": ("services.project_planning.domain.enums", "SceneStatus"),
    "Shot": ("services.project_planning.domain.enums", "ShotStatus"),
    "ShotRevision": ("services.project_planning.domain.enums", "ShotRevisionStatus"),
    "ShotBreakdownDraft": (
        "services.project_planning.domain.enums",
        "ShotDraftStatus",
    ),
    "ReworkTask": ("services.project_planning.domain.enums", "ReworkTaskStatus"),
    "ShotSpec": ("services.project_planning.domain.enums", "ShotSpecStatus"),
    "ConstraintPack": ("services.project_planning.domain.enums", "ConstraintPackStatus"),
}

# 已落库的表 → 契约对象名（表名与对象名不同名是常态，显式登记避免靠猜）。
_STATUS_TABLES: dict[str, str] = {
    "project_planning.projects": "Project",
    "project_planning.episodes": "Episode",
    "project_planning.script_revisions": "ScriptRevision",
    "asset_service.asset_versions": "AssetVersion",
    # A22 回填（同 _STATUS_ENUM_SOURCES 的理由）
    "project_planning.scenes": "Scene",
    "project_planning.shots": "Shot",
    "project_planning.shot_revisions": "ShotRevision",
    "project_planning.shot_breakdown_drafts": "ShotBreakdownDraft",
    "project_planning.rework_tasks": "ReworkTask",
    "project_planning.shot_specs": "ShotSpec",
    "project_planning.constraint_packs": "ConstraintPack",
}


def state_machine_states(sm_objects: Any) -> dict[str, set[str]]:
    """``{对象: allowed_states}``。形状坏了直接抛 ValueError（契约损坏）。"""
    states: dict[str, set[str]] = {}
    for entry in sm_objects or []:
        if not isinstance(entry, dict):
            raise ValueError("state_machines.objects 的条目必须是映射")
        name = str(entry.get("object", "")).strip()
        allowed = entry.get("allowed_states")
        if not name:
            raise ValueError("state_machines 有条目缺 object")
        if not isinstance(allowed, list) or not allowed:
            raise ValueError(f"{name} 缺 allowed_states（允许状态集合是唯一权威）")
        values = [str(v).strip() for v in allowed]
        if len(set(values)) != len(values):
            raise ValueError(f"{name}.allowed_states 有重复值")
        bad = [v for v in values if not _STATE_TOKEN_RE.match(v)]
        if bad:
            raise ValueError(f"{name}.allowed_states 含非状态值: {bad}")
        if name in states:
            raise ValueError(f"{name} 在 state_machines 里重复登记")
        states[name] = set(values)
    return states


def transition_target_problems(sm_objects: Any) -> list[str]:
    """转移边的两端都必须属于该对象的 allowed_states。

    这是「状态集合与转移图分离」之后必须补的一条：既然两者分开存储，就必须有
    规则保证它们指向同一批状态。否则打错一个字母的边会在运行时表现为
    「永远无法进入的转移」，而契约层看不出来。
    """
    problems: list[str] = []
    for entry in sm_objects or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("object", "")).strip()
        allowed = {str(v).strip() for v in entry.get("allowed_states") or []}
        for edge in entry.get("transitions") or []:
            if not isinstance(edge, dict):
                problems.append(f"{name}.transitions 的条目必须是 {{from, to}} 映射")
                continue
            src = str(edge.get("from", "")).strip()
            dst = str(edge.get("to", "")).strip()
            if not src or not dst:
                problems.append(f"{name}.transitions 有条目缺 from/to")
                continue
            for endpoint in (src, dst):
                if endpoint not in allowed:
                    problems.append(
                        f"{name} 的转移边 {src}→{dst} 引用了未登记状态 {endpoint!r}"
                        "（转移的两端必须都在 allowed_states 里）"
                    )
    return problems


def taxonomy_problems(
    taxonomy_objects: Any,
    sm_states: dict[str, set[str]],
    definitions: dict[str, Any],
    basis_kinds: set[str] | None = None,
) -> list[str]:
    """台账必须**恰好**覆盖 schema 里每个带 status 的对象，且分类自证。

    双向：漏登记报红（否则新对象可以带着裸 status 悄悄进来），多登记报红
    （否则台账会积累幽灵条目，看的人以为缺口还在）。

    ``basis_kinds`` 是 ``status_field_taxonomy.yaml`` 声明的封闭依据类别集合。
    ``basis`` 必须取自其中——只校验「非空」会让拼错的类别（``immutabel_record``）
    静默通过，台账自称的封闭枚举就形同虚设（PR #14 评审）。默认从权威文件读取，
    测试可显式传入以注入非法值验证闸门。
    """
    if basis_kinds is None:
        basis_kinds = set(load_yaml_mapping(STATUS_TAXONOMY_PATH).get("basis_kinds") or {})
    problems: list[str] = []
    declared: dict[str, dict[str, Any]] = {}
    for entry in taxonomy_objects or []:
        if not isinstance(entry, dict):
            problems.append("status_field_taxonomy.objects 的条目必须是映射")
            continue
        name = str(entry.get("object", "")).strip()
        if not name:
            problems.append("status_field_taxonomy 有条目缺 object")
            continue
        if name in declared:
            problems.append(f"{name} 在 status_field_taxonomy 里重复登记")
            continue
        declared[name] = entry

    schema_objects = {
        name
        for name, spec in definitions.items()
        if isinstance(spec, dict) and "status" in (spec.get("properties") or {})
    }
    for name in sorted(schema_objects - set(declared)):
        problems.append(
            f"{name} 有 status 字段却没在 status_field_taxonomy 登记分类"
            "（C.3.1 的 status 必填不代表可以无约束：必须说明它是生命周期还是占位）"
        )
    for name in sorted(set(declared) - schema_objects):
        problems.append(
            f"status_field_taxonomy 登记了 {name}，但 schema 里没有该对象的 status 字段"
            "（陈旧条目必须删掉）"
        )

    for name, entry in declared.items():
        cls = str(entry.get("class", "")).strip()
        if cls not in STATUS_CLASSES:
            problems.append(f"{name} 的分类非法: {cls!r}（允许 {STATUS_CLASSES}）")
            continue
        if cls == CLASS_LIFECYCLE:
            if name not in sm_states:
                problems.append(
                    f"{name} 被登记为 lifecycle，但 state_machines 没有它的状态机"
                )
            continue
        if cls == CLASS_ENVELOPE_ONLY:
            basis = str(entry.get("basis", "")).strip()
            if not basis:
                problems.append(f"{name} 标为 envelope_only 却没写 basis（依据类别）")
            elif basis not in basis_kinds:
                problems.append(
                    f"{name} 的 basis {basis!r} 不在 basis_kinds 封闭枚举 "
                    f"{sorted(basis_kinds)} 中（拼错或自造类别会让台账的分类依据静默失效）"
                )
            if not str(entry.get("reason", "")).strip():
                problems.append(
                    f"{name} 标为 envelope_only 却没写 reason："
                    "「它没有生命周期」本身就是要论证的结论，不能只下结论"
                )
            if not str(entry.get("unblocks_when", "")).strip():
                problems.append(
                    f"{name} 标为 envelope_only 却没写 unblocks_when："
                    "不能让「暂无生命周期」变成永久兜底，必须写清哪项领域决策才能销项"
                )
            if name in sm_states:
                problems.append(
                    f"{name} 标为 envelope_only，但 state_machines 登记了它的状态机"
                    "（要么改分类，要么删状态机，不能两套并存）"
                )
            continue
        missing = [f for f in DEBT_FIELDS if not str(entry.get(f, "")).strip()]
        if missing:
            problems.append(f"{name} 标为 lifecycle_gap 却缺债务字段: {missing}")
        if name in sm_states:
            problems.append(
                f"{name} 标为 lifecycle_gap，但状态机已经登记——陈旧债务必须改分类"
            )
    return problems


def enum_registry_problems(registry_objects: Any, sm_states: dict[str, set[str]]) -> list[str]:
    """枚举注册表必须是状态机的**等值投影**：对象集、每对象状态集都相等。"""
    problems: list[str] = []
    registry: dict[str, set[str]] = {}
    for entry in registry_objects or []:
        if not isinstance(entry, dict):
            problems.append("enum_registry.objects 的条目必须是映射")
            continue
        name = str(entry.get("object", "")).strip()
        values = {str(v) for v in entry.get("states") or []}
        if not name:
            problems.append("enum_registry 有条目缺 object")
            continue
        registry[name] = values

    for name in sorted(set(sm_states) - set(registry)):
        problems.append(f"state_machines 登记了 {name}，enum_registry 没有（派生文件已脱钩）")
    for name in sorted(set(registry) - set(sm_states)):
        problems.append(
            f"enum_registry 有 {name}，state_machines 没有（注册表里出现了无权威源的状态集）"
        )
    for name in sorted(set(sm_states) & set(registry)):
        extra = sorted(registry[name] - sm_states[name])
        missing = sorted(sm_states[name] - registry[name])
        if extra:
            problems.append(f"{name} 的注册表多出权威集合里没有的状态: {extra}")
        if missing:
            problems.append(f"{name} 的注册表缺权威集合里的状态: {missing}")
    return problems


def envelope_only_sentinel(layers: dict[str, Any] | None = None) -> str:
    """信封占位哨兵（唯一允许写入无生命周期对象的 status 的值）。"""
    data = layers if layers is not None else load_yaml_mapping(STATUS_LAYERS_PATH)
    sentinel = str(data.get("envelope_only_sentinel", "")).strip()
    if not sentinel:
        raise ValueError("status_layers 缺 envelope_only_sentinel（信封占位不能再退回裸 string）")
    return sentinel


def schema_enum_problems(
    definitions: dict[str, Any],
    sm_states: dict[str, set[str]],
    classes: dict[str, str],
    layers: dict[str, Any] | None = None,
) -> list[str]:
    """Schema 的 status.enum 必须逐值等于权威集合或信封哨兵，不得留裸字符串。

    lifecycle      → enum 等于 state_machines.allowed_states；
    envelope_only  → enum 只允许哨兵值（没有生命周期 ≠ 可以写任意字符串）；
    lifecycle_gap  → 保留裸 string 是显式债务，必须有完整债务字段（由台账闸门校验）。
    """
    problems: list[str] = []
    sentinel = envelope_only_sentinel(layers)
    for name, spec in definitions.items():
        if not isinstance(spec, dict):
            continue
        props = spec.get("properties") or {}
        if "status" not in props:
            continue
        status_spec = props["status"] if isinstance(props["status"], dict) else {}
        enum_values = status_spec.get("enum")
        cls = classes.get(name)
        if cls == CLASS_ENVELOPE_ONLY:
            if not enum_values:
                problems.append(
                    f"{name} 标为 envelope_only，status 却是无约束裸字符串"
                    "（没有生命周期不等于允许任意值：只能写哨兵）"
                )
            elif {str(v) for v in enum_values} != {sentinel}:
                problems.append(
                    f"{name} 的 status.enum 必须只收信封哨兵 {sentinel!r}，"
                    f"实际为 {sorted(str(v) for v in enum_values)}"
                )
            continue
        if cls != CLASS_LIFECYCLE:
            if enum_values is not None:
                problems.append(
                    f"{name} 被登记为 {cls}，Schema 却收了 status.enum"
                    f"{sorted(enum_values)}（分类说它没有生命周期，enum 从哪来？）"
                )
            continue
        if not enum_values:
            problems.append(
                f"{name} 是 lifecycle 对象，Schema 的 status 却是无约束裸字符串"
                "（C.3.1：未知枚举值必须拒绝，裸 string 等于任何值都收）"
            )
            continue
        actual = {str(v) for v in enum_values}
        expected = sm_states.get(name, set())
        extra = sorted(actual - expected)
        missing = sorted(expected - actual)
        if extra:
            problems.append(f"{name}.status.enum 有权威集合里没有的值: {extra}")
        if missing:
            problems.append(f"{name}.status.enum 缺权威集合里的值: {missing}")
    return problems


def alias_layer_problems(layers: dict[str, Any], sm_states: dict[str, set[str]]) -> list[str]:
    """别名必须落在权威状态上；禁用值不得出现在对应对象的状态集里。

    杜绝两类历史错误：把展示别名 / STALE 标记写成状态值；把检查项编号（PG0）
    或系统缩写（PMS）当状态登记。
    """
    problems: list[str] = []
    all_states = set().union(*sm_states.values()) if sm_states else set()

    for alias in layers.get("state_projection_aliases") or []:
        if not isinstance(alias, dict):
            problems.append("state_layers.state_projection_aliases 的条目必须是映射")
            continue
        name = str(alias.get("alias", "")).strip()
        obj = str(alias.get("object", "")).strip()
        target = str(alias.get("maps_to_state", "")).strip()
        if obj not in sm_states:
            problems.append(f"别名 {name} 指向未登记状态机的对象 {obj}")
            continue
        if target not in sm_states[obj]:
            problems.append(
                f"别名 {name} 映射到 {obj}.{target}，但该状态不在 allowed_states 里"
                "（别名必须有落点）"
            )
        if name in all_states:
            problems.append(
                f"别名 {name} 与某个权威状态同名：展示投影不得占用权威状态值"
            )

    for mark in layers.get("cross_domain_marks") or []:
        if not isinstance(mark, dict):
            continue
        name = str(mark.get("mark", "")).strip()
        if name in all_states:
            problems.append(
                f"跨域标记 {name} 出现在某个对象的 allowed_states 里："
                "失效/阻断标记不是生命周期状态，混进去会让它被当成真实进度"
            )

    for item in layers.get("prohibited_state_values") or []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value", "")).strip()
        applies = item.get("applies_to") or []
        targets = list(sm_states) if applies == "all" else [str(t) for t in applies]
        for obj in targets:
            if value in sm_states.get(obj, set()):
                problems.append(
                    f"{obj}.allowed_states 含被禁用的伪状态值 {value}"
                    f"（{item.get('reason', '见 status_layers.yaml')}）"
                )
    return problems


def schema_gap_problems(
    gaps: Any,
    sm_states: dict[str, set[str]],
    definitions: dict[str, Any],
    text_only: set[str] | None = None,
) -> list[str]:
    """有状态机、无 schema 的对象必须显式登记缺口；补上 schema 后必须删条目。

    ``text_only`` 是「正文有状态链、对象未建模」的那一类（state_machines 的
    text_authority_only 分区）。它们同样必须出现在缺口台账里——「以正文为权威」
    不等于可以不给实现方留一个可查的缺口条目。
    """
    text_only = text_only or set()
    problems: list[str] = []
    declared: dict[str, dict[str, Any]] = {}
    for entry in gaps or []:
        if not isinstance(entry, dict):
            problems.append("status_field_taxonomy.schema_gaps 的条目必须是映射")
            continue
        name = str(entry.get("object", "")).strip()
        if name in declared:
            problems.append(f"schema_gaps 里 {name} 重复登记")
            continue
        declared[name] = entry

    missing_schema = {n for n in sm_states if n not in definitions}
    for name in sorted((missing_schema | text_only) - set(declared)):
        problems.append(
            f"{name} 有权威状态机但没有 object schema，且未在 schema_gaps 登记"
            "（缺口不能隐形，也不能靠豁免消除）"
        )
    for name, entry in declared.items():
        if name not in sm_states and name not in text_only:
            problems.append(f"schema_gaps 登记的 {name} 不是状态机对象（陈旧条目）")
        elif name in definitions:
            problems.append(
                f"{name} 已有 object schema，schema_gaps 条目已陈旧，必须删除"
            )
        debt = [f for f in DEBT_FIELDS if not str(entry.get(f, "")).strip()]
        if debt:
            problems.append(f"schema_gaps.{name} 缺字段: {debt}")
    return problems


def field_authority_problems(
    path: Path,
    definitions: dict[str, Any],
    required_split: set[str] | None = None,
) -> list[str]:
    """双 status 对象的权威写入位置必须唯一、字段必须真实存在且论证完整。

    ``required_split`` 是台账里所有 ``basis: status_split`` 的对象。每个这样的对象
    都必须在本文件里有裁定条目——只校验「已登记的条目合法」不够：新增一个
    status_split 对象却忘了裁定权威字段时，旧逻辑全绿，「两个事实源」重新隐形
    （PR #14 评审）。默认从 taxonomy 文件读取，测试可显式传入注入缺口。

    反向不要求相等：``same_fact_on_status`` 类条目（如 PrevizVersion 归位 lifecycle
    后的裁定）保留在此，证明「某字段不是第二套生命周期」，属合法的历史裁定。
    """
    if required_split is None:
        taxonomy = load_yaml_mapping(STATUS_TAXONOMY_PATH)
        required_split = {
            str(e.get("object", "")).strip()
            for e in taxonomy.get("objects") or []
            if isinstance(e, dict) and str(e.get("basis", "")).strip() == "status_split"
        }
    data = load_yaml_mapping(path)
    problems: list[str] = []
    seen: set[str] = set()
    allowed_kinds = {"authority_elsewhere", "distinct_processes", "same_fact_on_status"}
    for entry in data.get("objects") or []:
        if not isinstance(entry, dict):
            problems.append("field_authority.objects 的条目必须是映射")
            continue
        name = str(entry.get("object", "")).strip()
        if not name or name in seen:
            problems.append(f"field_authority 条目 object 缺失或重复: {name!r}")
            continue
        seen.add(name)
        spec = definitions.get(name)
        if not isinstance(spec, dict):
            problems.append(f"field_authority 登记了 {name}，但 object_schemas 没有该对象")
            continue
        props = spec.get("properties") or {}
        authoritative = [str(v).strip() for v in entry.get("authoritative_fields") or []]
        non_authoritative = [
            str(v).strip() for v in entry.get("non_authoritative_fields") or []
        ]
        if not authoritative or any(not v for v in authoritative):
            problems.append(f"field_authority.{name} 缺 authoritative_fields")
        if len(set(authoritative)) != len(authoritative):
            problems.append(f"field_authority.{name} 的 authoritative_fields 有重复")
        overlap = set(authoritative) & set(non_authoritative)
        if overlap:
            problems.append(f"field_authority.{name} 把同一字段既当权威又当非权威: {sorted(overlap)}")
        for field in set(authoritative) | set(non_authoritative):
            if field not in props:
                problems.append(f"field_authority.{name} 引用了 schema 不存在的字段 {field}")
        kind = str(entry.get("kind", "")).strip()
        if kind not in allowed_kinds:
            problems.append(f"field_authority.{name}.kind 非法: {kind!r}")
        for key in ("ruling", "reason", "unblocks_when", "source_section"):
            if not str(entry.get(key, "")).strip():
                problems.append(f"field_authority.{name} 缺 {key}（裁定不能只下结论）")
    # 完备性：每个 status_split 对象必须有权威裁定，不能靠「没登记」逃逸。
    for name in sorted(required_split - seen):
        problems.append(
            f"{name} 在 status_field_taxonomy 标为 status_split，却没有 field_authority 裁定"
            "——两个 status 事实必须指出哪个权威，不能让缺口隐形"
        )
    return problems


def field_value_set_problems(
    path: Path | None = None,
    definitions: dict[str, Any] | None = None,
    layers: dict[str, Any] | None = None,
    authority_entries: dict[str, set[str]] | None = None,
) -> list[str]:
    """非 status 维度字段的权威值集 ↔ Schema ↔ 待裁定台账双向校验。

    四个方向缺一不可：

    1. 声明了值集，Schema 的字段 enum 必须逐值相等（反向由派生器字节漂移兜住）；
    2. 转移边与 guard 引用的值/字段必须真实存在，不能是空头声明；
    3. 值集已落地的字段不得继续留在 status_layers 的待裁定区（陈旧债务）；
    4. status_split 的权威字段既没有值集、也没有待裁定登记 → 隐形缺口报红。
    """
    from gen_status_contracts import field_value_sets as _derived_sets

    source = path or FIELD_VALUE_SETS_PATH
    data = load_yaml_mapping(source)
    declared = _derived_sets(source)
    if definitions is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        definitions = schema.get("definitions") or schema.get("$defs") or {}
    if layers is None:
        layers = load_yaml_mapping(STATUS_LAYERS_PATH)
    if authority_entries is None:
        taxonomy = load_yaml_mapping(STATUS_TAXONOMY_PATH)
        split_objects = {
            str(e.get("object", "")).strip()
            for e in taxonomy.get("objects") or []
            if isinstance(e, dict) and str(e.get("basis", "")).strip() == "status_split"
        }
        authority_data = load_yaml_mapping(FIELD_AUTHORITY_PATH)
        authority_entries = {
            str(e.get("object", "")).strip(): {
                str(f).strip() for f in (e.get("authoritative_fields") or [])
            }
            for e in authority_data.get("objects") or []
            if isinstance(e, dict) and str(e.get("object", "")).strip() in split_objects
        }

    problems: list[str] = []
    for entry in data.get("fields") or []:
        obj = str(entry.get("object", "")).strip()
        field = str(entry.get("field", "")).strip()
        label = f"field_value_sets.{obj}.{field}"
        for key in ("dimension", "authority", "ruling", "source_section"):
            if not str(entry.get(key, "")).strip():
                problems.append(f"{label} 缺 {key}（值集不能只有一串字面值）")
        spec = definitions.get(obj)
        if not isinstance(spec, dict):
            problems.append(f"{label} 登记了不存在的对象 {obj}")
            continue
        props = spec.get("properties") or {}
        prop = props.get(field)
        if not isinstance(prop, dict):
            problems.append(f"{label} 登记了 Schema 不存在的字段")
            continue
        want = declared[(obj, field)]
        actual = prop.get("enum")
        if not isinstance(actual, list) or {str(v) for v in actual} != set(want):
            problems.append(
                f"{label} 的 Schema enum 与权威值集不一致："
                f"声明 {sorted(want)}，Schema {sorted(str(v) for v in actual or [])}"
            )
        for transition in entry.get("transitions") or []:
            for side in ("from", "to"):
                value = str(transition.get(side, "")).strip()
                if value and value not in want:
                    problems.append(f"{label} 的转移边引用未登记值 {value}")
        for guard in entry.get("guards") or []:
            for required_field in guard.get("require") or []:
                if str(required_field) not in props:
                    problems.append(f"{label} 的 guard 引用不存在的字段 {required_field}")

    pending: set[tuple[str, str]] = set()
    for entry in layers.get("non_lifecycle_dimensions") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("value_set_status", "")).strip() != "pending_owner_ruling":
            continue
        obj = str(entry.get("object", "")).strip()
        fields = str(entry.get("status_field", ""))
        for field in fields.split("/"):
            field = field.strip()
            if obj and field:
                pending.add((obj, field))
    for obj, field in sorted(pending & set(declared)):
        problems.append(
            f"{obj}.{field} 已裁定值集，却仍留在 status_layers 待裁定区（陈旧债务）"
        )
    for obj, auth_fields in sorted(authority_entries.items()):
        for field in sorted(auth_fields - {"status"}):
            if (obj, field) not in declared and (obj, field) not in pending:
                problems.append(
                    f"{obj}.{field} 是 status_split 的权威字段，却没有值集也没有待裁定登记"
                    "——裸 string 等于任何值都收，缺口不能隐形"
                )
    return problems


def governance_problems(sm_data: dict[str, Any], definitions: dict[str, Any]) -> list[str]:
    """状态机文件自身的账目必须对得上（对象数、冲突数、投影不入册）。"""
    problems: list[str] = []
    objects = sm_data.get("objects") or []
    declared_total = sm_data.get("total_objects")
    if declared_total != len(objects):
        problems.append(
            f"state_machines.total_objects={declared_total} 与实际条目数 {len(objects)} 不符"
        )
    conflicts = sum(1 for o in objects if isinstance(o, dict) and o.get("conflict") is True)
    if sm_data.get("conflict_count") != conflicts:
        problems.append(
            f"state_machines.conflict_count={sm_data.get('conflict_count')} "
            f"与实际标记 conflict 的条目数 {conflicts} 不符"
        )
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        gap = obj.get("transitions_gap")
        if gap is None:
            continue
        name = str(obj.get("object", "")).strip()
        if not isinstance(gap, dict):
            problems.append(f"{name}.transitions_gap 必须是映射")
            continue
        missing = [k for k in ("reason", "unblocks_when") if not str(gap.get(k, "")).strip()]
        if missing:
            problems.append(f"{name}.transitions_gap 缺字段: {missing}")
        if not obj.get("transitions"):
            problems.append(f"{name} 登记了 transitions_gap，却连一条明确边都没有")
    for projection in sm_data.get("projections") or []:
        if not isinstance(projection, dict):
            continue
        name = str(projection.get("object", "")).strip()
        if name in definitions:
            problems.append(
                f"{name} 被登记为 UI 投影，却同时有 object schema："
                "投影不是持久化对象，不得进对象契约"
            )
        if not projection.get("allowed_states"):
            problems.append(f"投影 {name} 缺 allowed_states（投影也有取值范围）")
    return problems


def runtime_status_problems(sm_states: dict[str, set[str]]) -> list[str]:
    """已实现对象的 Domain Enum 与数据库 CHECK 不得偏离契约。

    只查已实现的四处（见 _STATUS_ENUM_SOURCES / _STATUS_TABLES）。未实现的对象
    不提前造枚举与约束——那会让「契约已有」和「运行时已有」混为一谈。
    """
    import importlib

    from packages.shared.database import Base
    from packages.shared.status_constraint import status_check_sql

    # 触发模型注册（表元数据在 import 时建立）。
    importlib.import_module("services.project_planning.repository.orm")
    importlib.import_module("services.asset_service.repository.orm")

    problems: list[str] = []
    for obj_name, (module_path, class_name) in _STATUS_ENUM_SOURCES.items():
        expected = sm_states.get(obj_name)
        if expected is None:
            problems.append(f"实现里登记了 {obj_name}，state_machines 却没有它")
            continue
        enum_cls = getattr(importlib.import_module(module_path), class_name)
        actual = {str(member.value) for member in enum_cls}
        extra = sorted(actual - expected)
        missing = sorted(expected - actual)
        if extra:
            problems.append(f"{class_name} 有契约里没有的状态: {extra}")
        if missing:
            problems.append(
                f"{class_name} 少登记契约状态: {missing}"
                "（状态集合是唯一权威，运行时枚举必须与它逐值相等）"
            )

    for table_name, obj_name in _STATUS_TABLES.items():
        expected = sm_states.get(obj_name)
        if expected is None:
            continue
        table = Base.metadata.tables.get(table_name)
        if table is None:
            problems.append(f"找不到表 {table_name}（表名与对象的对应关系见 _STATUS_TABLES）")
            continue
        want = status_check_sql(expected)
        constraints = [
            c
            for c in table.constraints
            if getattr(c, "name", "") == f"ck_{table.name}_status"
        ]
        if not constraints:
            problems.append(
                f"{table_name} 缺 status CHECK 约束（ck_{table.name}_status）："
                "契约收紧了枚举，数据库却仍接受任意字符串"
            )
            continue
        actual_sql = " ".join(str(constraints[0].sqltext).split())
        if actual_sql != want:
            problems.append(
                f"{table_name} 的 status CHECK 与契约不符：实际 {actual_sql!r}，应为 {want!r}"
            )
    return problems


def check_status_enum_consistency() -> bool:
    """状态枚举的四层一致性闸门（state_machines → registry → schema → runtime）。"""
    sm_path = CONTRACTS / "state_machines" / "state_machines.yaml"
    sm_data = load_yaml_mapping(sm_path)
    sm_objects = sm_data.get("objects") or []
    taxonomy = load_yaml_mapping(STATUS_TAXONOMY_PATH)
    layers = load_yaml_mapping(STATUS_LAYERS_PATH)
    registry = load_yaml_mapping(ENUM_REGISTRY_PATH)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    definitions = schema.get("definitions") or schema.get("$defs") or {}

    sm_states = state_machine_states(sm_objects)
    classes = {
        str(entry.get("object")).strip(): str(entry.get("class")).strip()
        for entry in taxonomy.get("objects") or []
        if isinstance(entry, dict)
    }

    problems: list[str] = []
    problems += governance_problems(sm_data, definitions)
    problems += transition_target_problems(sm_objects)
    problems += taxonomy_problems(
        taxonomy.get("objects"), sm_states, definitions, set(taxonomy.get("basis_kinds") or {})
    )
    problems += enum_registry_problems(registry.get("objects"), sm_states)
    problems += schema_enum_problems(definitions, sm_states, classes, layers)
    split_objects = {
        str(e.get("object", "")).strip()
        for e in taxonomy.get("objects") or []
        if isinstance(e, dict) and str(e.get("basis", "")).strip() == "status_split"
    }
    problems += field_authority_problems(FIELD_AUTHORITY_PATH, definitions, split_objects)
    problems += field_value_set_problems(
        FIELD_VALUE_SETS_PATH, definitions, layers
    )
    problems += alias_layer_problems(layers, sm_states)
    problems += schema_gap_problems(
        taxonomy.get("schema_gaps"),
        sm_states,
        definitions,
        {
            str(entry.get("object", "")).strip()
            for entry in sm_data.get("text_authority_only") or []
            if isinstance(entry, dict)
        },
    )
    problems += runtime_status_problems(sm_states)
    # 逐字节的派生漂移也要归这条闸门管：CI 只跑 contract_check，若把 --check
    # 留成独立脚本，语义值一致但派生文件被手改（顺序、注释、格式）仍会漏网。
    # 延迟导入：两个脚本同属 scripts 工具层，无反向依赖；放到函数内避免
    # 单纯 import contract_check 就触发整套契约文件读取。
    from gen_status_contracts import drift_problems

    problems += drift_problems()

    for problem in problems:
        print(f"[FAIL] {problem}")
    if problems:
        return False

    lifecycle = sum(1 for c in classes.values() if c == CLASS_LIFECYCLE)
    envelope = sum(1 for c in classes.values() if c == CLASS_ENVELOPE_ONLY)
    gaps = sum(1 for c in classes.values() if c == CLASS_LIFECYCLE_GAP)
    total_states = sum(len(v) for v in sm_states.values())
    total_edges = sum(
        len(o.get("transitions") or []) for o in sm_objects if isinstance(o, dict)
    )
    print(
        f"[PASS] 状态枚举一致：{len(sm_states)} 个状态机对象 / {total_states} 个状态 / "
        f"{total_edges} 条转移；status 字段分类 {lifecycle} lifecycle + {envelope} 占位 + "
        f"{gaps} 缺口；{len(_STATUS_ENUM_SOURCES)} 个已实现对象的枚举与 CHECK 与契约一致"
    )
    return True



def _load_strict_event_payloads() -> tuple[dict[str, Any], str, dict[str, str]]:
    """从代码读取严格载荷注册表；独立入口便于装置注入，不让判据写死来源。"""
    from packages.shared import event_payloads as _payloads

    return (
        dict(_payloads.EVENT_PAYLOAD_SCHEMAS),
        str(_payloads.EVENT_PAYLOAD_SCHEMA_VERSION),
        dict(_payloads.declared_schema_versions()),
    )


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

    # 专用 handler 的事件载荷模型：catalog 声明必须与代码注册表逐项一致。
    # 只校验「有登记」不够——模型字段集合漂移会让 extra=forbid 拒绝合法事件，
    # 或让未知字段被放行；这里用两侧实读字段集合做双向判定。
    try:
        payload_models, payload_version, declared_versions = (
            _load_strict_event_payloads()
        )
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时应显式失败
        print(f"[FAIL] 事件载荷模型不可导入: {exc}")
        return False

    with_schema: dict[str, dict[str, object]] = {
        name: entry for name, entry in declared.items() if entry.get("payload_schema")
    }
    for name, entry in with_schema.items():
        declared_path = str(entry["payload_schema"])
        model_name = declared_path.rsplit(".", 1)[-1]
        model = payload_models.get(name)
        if model is None or model.__name__ != model_name:
            print(f"[FAIL] 事件 {name} 的 payload_schema {declared_path!r} 与消费注册表不一致")
            ok = False
            continue
        if str(entry.get("payload_schema_version", "")) != payload_version:
            print(f"[FAIL] 事件 {name} 的 payload_schema_version 与代码常量不一致")
            ok = False
        required = {str(f) for f in (entry.get("payload_required") or [])}
        fields = set(model.model_fields)
        if required != fields:
            print(
                f"[FAIL] 事件 {name} 的 payload_required 与模型字段不一致："
                f"仅台账 {sorted(required - fields)} / 仅模型 {sorted(fields - required)}"
            )
            ok = False

    unregistered = sorted(set(payload_models) - set(with_schema))
    if unregistered:
        print(f"[FAIL] 载荷模型已注册但事件契约缺 payload_schema: {unregistered}")
        ok = False

    drifted = {
        name: version
        for name, version in declared_versions.items()
        if version != payload_version
    }
    if drifted:
        print(f"[FAIL] 载荷模型的 schema_version 字面量与常量不一致: {drifted}")
        ok = False

    if ok:
        print(
            f"[PASS] 事件契约：{len(declared)} 个事件 / "
            f"{len(emitted)} 个发出点 / 显式缺口 {len(gaps)} 项 / "
            f"严格载荷模型 {len(with_schema)} 个"
        )
    return ok


# --------------------------------------------------------------------------- #
# 安全与数据边界契约校验（Security & Data Boundary Contract）
# --------------------------------------------------------------------------- #
# 为什么需要这个校验（R1 一个事实一个权威源 + 边界缺口不可静默增长）
# ------------------------------------------------------------------------
# contracts/security_boundary.yaml 是授权/资源归属/数据所有权/进程密钥/进程凭证
# 五条边界的唯一权威源。它必须是**结构化约束**而非解释文档。
#
# 本轮整改的关键变化：**status: satisfied 被禁止手写**。
# 此前的不变量靠人工填 satisfied 销项，本函数只校验 YAML 结构自洽——结果是
# AUTH-1（授权单一入口）标记 satisfied，而组织级判定实际手写在路由层；
# KEYS-1 标记 satisfied，而其文句根本没覆盖数据库凭证，identity 继承 postgres
# 超级用户口令对闸门因此是隐形的。闸门只能证明「有人写了 satisfied」，不能证明
# 「事实成立」，这就是第三轮、第二轮审查反复从同一条缝挖出 P1 的机制。
#
# 现在的判定方式（两级证据，各管各的事实）：
# - 无 status 字段 → 必须声明 assertion（scripts/boundary_assertions.py 的注册 id），
#   由闸门**执行**它判定成立与否；断言由红转绿只能靠改实现。
# - runtime_evidence（pytest 节点列表）→ 指向真实行为测试，闸门校验节点真实存在，
#   由 CI 的 `pytest tests` 在真实 PostgreSQL 上执行（闸门同时校验 CI 确实跑了它）。
# - status 仅允许 partial / debt，且必须带完整原因与解除条件。
#
# 校验点：
# 1. 文件存在且顶层为映射，含五条安全边界（authorization / resource_scope /
#    data_ownership / process_keys / credential_scope）与生命周期分区
#    （lifecycle），缺一报红；
# 2. 每条不变量有 id / name / statement / fact_source / verification；
# 3. status 出现即必须是 partial / debt，且子块字段完整；写 satisfied 直接报红；
# 4. 无 status 时必须 assertion 已注册且执行通过，runtime_evidence 节点真实存在；
# 5. 不变量 id 全局唯一（防复制粘贴出重名）。

SECURITY_BOUNDARY_PATH = CONTRACTS / "security_boundary.yaml"

_SECURITY_BOUNDARY_SECTIONS = (
    "authorization",
    "resource_scope",
    "data_ownership",
    "process_keys",
    "credential_scope",
    "lifecycle",
)

_SECURITY_BOUNDARY_INVARIANT_REQUIRED = (
    "id",
    "name",
    "statement",
    "fact_source",
    "verification",
)

# status 只允许「显式欠账」两种；satisfied 由断言执行结果决定，不再可写。
_SECURITY_BOUNDARY_DECLARED_STATUSES = ("partial", "debt")

_DEBT_REQUIRED = ("reason", "contract_ref", "unblocks_when")
_PARTIAL_REQUIRED = ("satisfied", "missing", "unblocks_when")


def _assertion_registry():
    """延迟导入断言注册表（脚本目录加入 sys.path，支持 `python scripts/xxx.py` 直跑）。"""
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import boundary_assertions

    return boundary_assertions


def _test_node_exists(node: str) -> bool:
    """校验 ``tests/x.py::test_y`` 指向的测试函数真实存在（防指向空气）。"""
    path_part, _, func_part = node.partition("::")
    path = Path(path_part)
    if not path.is_absolute():
        path = CONTRACTS.parent / path_part
    if not path.exists():
        return False
    if not func_part:
        return True
    # 参数化节点形如 test_x[a-b]：只校验函数本体存在。
    func_name = func_part.split("[", 1)[0]
    pattern = rf"^\s*(?:async\s+)?def\s+{re.escape(func_name)}\b"
    return re.search(pattern, path.read_text(encoding="utf-8"), re.M) is not None


def _ci_runs_full_test_suite() -> bool:
    """CI 必须真实执行 runtime_evidence 指向的测试（否则证据只是装饰）。"""
    workflow = CONTRACTS.parent / ".github/workflows/ci.yml"
    if not workflow.exists():
        return False
    return "pytest tests" in workflow.read_text(encoding="utf-8")


def check_security_boundary() -> bool:
    """校验安全与数据边界契约：断言执行结果 + 显式债务一致性。"""
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

    registry = _assertion_registry()
    seen_ids: set[str] = set()
    total_invariants = 0
    debt_count = 0
    partial_count = 0
    verified_count = 0

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
            has_sub_block = "debt" in inv or "partial" in inv

            if status == "satisfied":
                print(
                    f"[FAIL] {inv_id} 写了 status: satisfied —— 已禁止手写。"
                    "删掉该字段并声明 assertion（由闸门执行判定）；"
                    "确实未完成则写 partial/debt 并给原因。"
                )
                ok = False
                continue
            if status and status not in _SECURITY_BOUNDARY_DECLARED_STATUSES:
                print(
                    f"[FAIL] {inv_id} 的 status 非法: {status!r}（只允许 "
                    f"{'/'.join(_SECURITY_BOUNDARY_DECLARED_STATUSES)}）"
                )
                ok = False
                continue
            # 一条不变量只能有一种欠账形态：partial 挂 debt 子块会让「还差什么」
            # 与「何时解除」出现两套说法，正是本轮要消除的多事实源。
            if status == "partial" and "debt" in inv:
                print(f"[FAIL] {inv_id} 同时声明 partial 与 debt 子块（欠账形态必须唯一）")
                ok = False
                continue
            if status == "debt" and "partial" in inv:
                print(f"[FAIL] {inv_id} 同时声明 debt 与 partial 子块（欠账形态必须唯一）")
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
                continue

            if status == "partial":
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
                continue

            # 无 status：断言裁决路径。
            if has_sub_block:
                print(
                    f"[FAIL] {inv_id} 残留 debt/partial 子块但未声明 status"
                    "（债务必须显式化，不能靠断言通过就假装还清）"
                )
                ok = False

            assertion_id = str(inv.get("assertion", "")).strip()
            if not assertion_id:
                print(
                    f"[FAIL] {inv_id} 既无 status 也无 assertion："
                    "不变量没有可执行判据，只能靠人声称成立（本次整改禁止）。"
                    f"可用断言: {', '.join(registry.registered_ids())}"
                )
                ok = False
                continue

            func = registry.get_assertion(assertion_id)
            if func is None:
                print(
                    f"[FAIL] {inv_id} 引用了未注册的断言: {assertion_id}"
                    f"（已注册: {', '.join(registry.registered_ids())}）"
                )
                ok = False
                continue

            try:
                passed, reason = func()
            except Exception as exc:  # 断言自身崩溃也是红，不允许静默跳过
                print(f"[FAIL] {inv_id} 断言 {assertion_id} 执行异常: {exc!r}")
                ok = False
                continue

            if not passed:
                print(f"[FAIL] {inv_id} 断言未通过（{assertion_id}）: {reason}")
                ok = False
                continue

            evidence = inv.get("runtime_evidence") or []
            if not isinstance(evidence, list) or not evidence:
                print(
                    f"[FAIL] {inv_id} 缺 runtime_evidence：结构性断言不足以证明"
                    "运行时拦得住，必须指向真实行为测试"
                )
                ok = False
                continue
            dead_nodes = [str(n) for n in evidence if not _test_node_exists(str(n))]
            if dead_nodes:
                print(f"[FAIL] {inv_id} 的 runtime_evidence 指向不存在的测试: {dead_nodes}")
                ok = False
                continue
            verified_count += 1

    if not _ci_runs_full_test_suite():
        print(
            "[FAIL] CI 未执行 `pytest tests`：runtime_evidence 指向的测试没有被运行，"
            "证据链断裂"
        )
        ok = False

    if ok:
        print(
            f"[PASS] 安全与数据边界契约：{total_invariants} 条不变量"
            f"（断言验证 {verified_count} / partial {partial_count} / debt {debt_count}）"
        )
    return ok


# --------------------------------------------------------------------------- #
# 提案状态（P2-04：文档状态不得手写，必须引用事实源）
# --------------------------------------------------------------------------- #
# 为什么需要（这是 P2-04 的根因，不是文档洁癖）：
# 第二轮审查发现 ``docs/proposals/F01-F05-...md`` 头部写着「提案 / 待评审冻结」，
# 而它描述的六个阶段早已合并进 main；同一份文件里还写着「本地无 PG，集成测试
# 未能执行」，而 CI 每天在真实 PostgreSQL 上跑。**文档状态无人校验，就必然腐化**——
# 它腐化的方式很具体：下一个人读到「待评审」，就会重新评审一遍已经落地的东西，
# 或者反过来，把「已登记债务」当成「已解决」。
#
# 所以提案改为机器可读的 front-matter，闸门校验三件事：
# 1. ``status`` 合法，且与 ``open_items`` 自洽——
#    ``implemented`` 必须没有未完成项，``in_progress`` 必须说清还差什么；
# 2. ``authoritative_sources`` 指向的文件真实存在（不许指向空气）；
# 3. ``open_items[].source`` 能解析到**契约里真实存在的债**——
#    写 ``contracts/security_boundary.yaml#AUTH-3`` 就必须真有一条 AUTH-3 且带
#    residual_risk/debt/partial。这条是关键：否则「还差什么」又变成散文。
PROPOSALS_DIR = ROOT / "docs/proposals"
_PROPOSAL_STATUSES = ("proposed", "in_progress", "implemented", "superseded")
# 一条 open item 的 source 允许指向的三类"债"字段（契约里的显式未完成登记）。
_OPEN_ITEM_DEBT_KEYS = ("residual_risk", "debt", "partial")


def _read_front_matter(path: Path) -> dict[str, Any] | None:
    """读取 Markdown 顶部的 YAML front-matter（缺失/损坏返回 None）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                data = load_yaml("\n".join(lines[1:index]), source=str(path))
            except (yaml.YAMLError, ValueError):
                return None
            return data if isinstance(data, dict) else None
    return None


def _find_invariant(data: dict[str, Any], anchor: str) -> dict[str, Any] | None:
    """在（security_boundary 这类）分区-不变量结构里按 id 找条目。"""
    for value in data.values():
        if not isinstance(value, dict):
            continue
        for inv in value.get("invariants") or []:
            if isinstance(inv, dict) and inv.get("id") == anchor:
                return inv
    return None


def _resolve_open_item_source(source: str) -> str | None:
    """校验 open_items[].source；返回问题描述，None 表示成立。

    两种写法：
    - ``path``：文件必须存在（用于整份台账，例如 action_implementations.yaml）；
    - ``path#ANCHOR``：文件存在且 ANCHOR 指向**非空的未完成登记**——
      ANCHOR 命中不变量 id 时要求该条带 residual_risk/debt/partial；
      命中顶层键时要求其值非空（如 ``#not_implemented``）。
    """
    path_part, _, anchor = source.partition("#")
    target = ROOT / path_part
    if not path_part or not target.exists():
        return f"open_items.source 指向不存在的文件: {path_part!r}"
    if not anchor:
        return None
    try:
        data = load_yaml_mapping(target)
    except ValueError as exc:
        return f"open_items.source 无法加载: {exc}"
    invariant = _find_invariant(data, anchor)
    if invariant is not None:
        if any(str(invariant.get(key, "")).strip() for key in _OPEN_ITEM_DEBT_KEYS):
            return None
        return (
            f"open_items.source 指向不变量 {anchor}，但该条没有 residual_risk/debt/partial："
            "文档说还差什么，契约里必须有对应的债"
        )
    if anchor in data:
        value = data[anchor]
        if value:
            return None
        return f"open_items.source 指向的顶层键 {anchor} 为空，不能证明还有未完成项"
    return f"open_items.source 的锚点 {anchor} 在 {path_part} 中不存在"


def check_proposal_status() -> bool:
    """提案状态必须与契约事实自洽（P2-04）。"""
    ok = True
    files = sorted(PROPOSALS_DIR.glob("*.md"))
    if not files:
        print("[FAIL] docs/proposals 下没有提案文件：提案状态无从校验")
        return False

    counts: dict[str, int] = dict.fromkeys(_PROPOSAL_STATUSES, 0)
    for path in files:
        # 报告路径：仓库内用相对路径，测试传入临时目录时用绝对路径（不因位置崩）。
        rel = (
            path.relative_to(ROOT).as_posix()
            if path.is_relative_to(ROOT)
            else str(path)
        )
        meta = _read_front_matter(path)
        if meta is None:
            print(f"[FAIL] {rel} 缺少 YAML front-matter（状态必须机器可读）")
            ok = False
            continue

        status = str(meta.get("status", "")).strip()
        if status not in _PROPOSAL_STATUSES:
            print(f"[FAIL] {rel} 的 status 非法: {status!r}（允许 {_PROPOSAL_STATUSES}）")
            ok = False
            continue
        counts[status] += 1

        sources = meta.get("authoritative_sources") or []
        if not isinstance(sources, list) or not sources:
            print(f"[FAIL] {rel} 未声明 authoritative_sources：状态必须有事实源")
            ok = False
        else:
            for source in sources:
                if not (ROOT / str(source)).exists():
                    print(f"[FAIL] {rel} 的 authoritative_sources 指向不存在的文件: {source}")
                    ok = False

        items = meta.get("open_items")
        if items is None:
            items = []
        if not isinstance(items, list):
            print(f"[FAIL] {rel} 的 open_items 必须是列表")
            ok = False
            continue

        for item in items:
            if not isinstance(item, dict) or not str(item.get("item", "")).strip():
                print(f"[FAIL] {rel} 的 open_items 条目缺 item 描述")
                ok = False
                continue
            problem = _resolve_open_item_source(str(item.get("source", "")))
            if problem:
                print(f"[FAIL] {rel} 的 open_items[{item.get('item')}] {problem}")
                ok = False

        if status == "implemented" and items:
            print(
                f"[FAIL] {rel} 声明 implemented 却仍有 {len(items)} 项 open_items："
                "勾完成与留债不能并存"
            )
            ok = False
        if status == "in_progress" and not items:
            print(
                f"[FAIL] {rel} 声明 in_progress 却没有 open_items："
                "说不清还差什么，就该按 implemented 声明（或补齐未完成项）"
            )
            ok = False

    if ok:
        print(
            f"[PASS] 提案状态：{len(files)} 份提案"
            f"（implemented {counts['implemented']} / in_progress {counts['in_progress']} / "
            f"proposed {counts['proposed']} / superseded {counts['superseded']}）"
        )
    return ok


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Gate C：阶段表必须是基线 §31.2 的派生（不是又一份手写事实）
# --------------------------------------------------------------------------- #
_PHASE_31_2_HEADING = "31.2 平台 Phase 0—4 唯一阶段总表"
_PHASE_DOMAINS = ("core_production", "community", "other_domain")


def _normalize_phase_text(text: str) -> str:
    """只消除排版噪声（空白 / Markdown 强调），不消除任何语义字串。"""
    return re.sub(r"\s+", "", text.replace("**", ""))


def _baseline_phase_tables() -> tuple[dict[str, tuple[str, str, str]], set[tuple[int, str]]]:
    """从基线正文派生：权威阶段行 + 非权威阶段表所在的（标题行号, 章节名）。

    行号取章节标题行而不是表格行：章节身份由标题确定，表格内的空行/换行不该
    让登记项失效。集合双向比对时，行号漂移与章节缺席都会报红。
    """
    lines = BASELINE_DOC.read_text(encoding="utf-8").splitlines()
    authoritative: dict[str, tuple[str, str, str]] = {}
    others: set[tuple[int, str]] = set()
    heading: tuple[int, str] | None = None
    in_authoritative = False
    for index, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("### "):
            title = stripped[4:].strip()
            heading = (index, title)
            in_authoritative = title.startswith(_PHASE_31_2_HEADING)
            continue
        if not stripped.startswith("| Phase "):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if in_authoritative:
            authoritative[cells[0]] = (cells[1], cells[2], cells[3])
        elif heading is not None:
            section = heading[1].split("（", 1)[0].strip()
            others.add((heading[0], section))
    return authoritative, others


def check_phase_map_against_baseline() -> bool:
    """Gate C：逐字比对 §31.2 权威表，并双向校验非权威阶段表登记。

    freeze_manifest 只锁 phase_map 的哈希，发现不了「漂移后重签」；过去没有任何
    判据读基线正文。Phase 2 的 community/other_domain 因此与权威表相反（派生文件
    允许 Phase 2 产生 CreatorRelease，基线明确禁止），而闸门全绿。
    """
    baseline_rows, other_tables = _baseline_phase_tables()
    ok = True
    if len(baseline_rows) != 5:
        print(f"[FAIL] 基线 {_PHASE_31_2_HEADING} 应有 5 行，实际 {sorted(baseline_rows)}")
        return False

    data = load_yaml_file(CONTRACTS / "phases" / "phase_map.yaml")
    phases = data.get("phases")
    if not isinstance(phases, list):
        print("[FAIL] phase_map.phases 缺失或非列表")
        return False

    seen: set[str] = set()
    for entry in phases:
        if not isinstance(entry, dict):
            print(f"[FAIL] phase_map.phases 含非映射项：{entry}")
            ok = False
            continue
        phase_id = entry.get("id")
        if phase_id in seen or phase_id not in baseline_rows:
            print(f"[FAIL] phase_map 阶段 id 重复或基线中不存在：{phase_id}")
            ok = False
            continue
        seen.add(phase_id)
        for field, expected in zip(_PHASE_DOMAINS, baseline_rows[phase_id], strict=True):
            actual = _normalize_phase_text(str(entry.get(field) or ""))
            if actual != _normalize_phase_text(expected):
                print(f"[FAIL] {phase_id}.{field} 与基线 {_PHASE_31_2_HEADING} 不一致")
                ok = False
    missing_phases = set(baseline_rows) - seen
    if missing_phases:
        print(f"[FAIL] phase_map 缺少基线阶段：{sorted(missing_phases)}")
        ok = False

    registered: set[tuple[int, str]] = set()
    for item in data.get("non_authoritative_phase_tables") or []:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("line"), int)
            or not isinstance(item.get("section"), str)
        ):
            print(f"[FAIL] 非权威阶段表登记项格式错误：{item}")
            ok = False
            continue
        key = (item["line"], item["section"])
        if key in registered:
            print(f"[FAIL] 非权威阶段表登记重复：{key}")
            ok = False
        registered.add(key)
    for key in sorted(other_tables - registered):
        print(f"[FAIL] 基线非权威阶段表未登记：{key}")
        ok = False
    for key in sorted(registered - other_tables):
        print(f"[FAIL] 非权威阶段表登记已陈旧：{key}")
        ok = False

    if ok:
        print(
            f"[PASS] 阶段表派生一致：{len(baseline_rows)} 个阶段 / "
            f"{len(other_tables)} 个非权威表登记"
        )
    return ok

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
        ok &= check_phase_map_against_baseline()
        ok &= check_action_openapi_consistency()
        ok &= check_runtime_contract_coverage()
        ok &= check_implemented_resource_schemas()
        ok &= check_active_resource_schemas()
        ok &= check_implemented_permission_wiring()
        ok &= check_state_reachability()
        ok &= check_transition_entry_gaps()
        ok &= check_state_machine_guards()
        ok &= check_child_creation_freeze_coverage()
        ok &= check_frozen_state_contract()
        ok &= check_permission_matrix()
        ok &= check_schema_objects()
        ok &= check_schema_field_evolution()
        ok &= check_storage_locator_pattern()
        ok &= check_contract_amendments()
        ok &= check_reference_targets()
        ok &= check_status_enum_consistency()
        ok &= check_event_catalog()
        ok &= check_security_boundary()
        ok &= check_proposal_status()
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
    if "--write-schema-signatures" in sys.argv:
        write_schema_field_signatures()
        sys.exit(0)
    sys.exit(main())
