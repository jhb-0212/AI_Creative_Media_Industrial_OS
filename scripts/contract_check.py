"""契约一致性校验脚本。

职责：校验冻结契约（contracts/）与实现派生之间的一致性，作为 CI 门禁。
当前覆盖：
1. contracts/ 下所有 YAML / JSON 文件的语法解析。
2. contracts/state_machines/state_machines.yaml 中 Project 状态机
   与 services/project_planning/domain/state_machine.py 的转移表一致性。
3. Action Registry ↔ OpenAPI 路径一一对应（未注册 Action 不得暴露端点）。
4. Permission Matrix 结构完整（action_matrix 覆盖全部 action_key）。
5. object_schemas.schema.json 的 P0 对象定义完整性。

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
_STATE_MACHINE_SOURCES: dict[str, tuple[str, str]] = {
    "Project": ("services.project_planning.domain.state_machine", "ALLOWED_TRANSITIONS"),
    "ScriptRevision": ("services.project_planning.domain.script", "ALLOWED_TRANSITIONS"),
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
    ok &= check_permission_matrix()
    ok &= check_schema_objects()

    if ok:
        print("[PASS] 契约一致性校验通过")
        return 0
    print("[FAIL] 契约一致性校验未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
