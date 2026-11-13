"""契约一致性校验脚本。

职责：校验冻结契约（contracts/）与实现派生之间的一致性，作为 CI 门禁。
当前覆盖：
1. contracts/ 下所有 YAML / JSON 文件的语法解析。
2. contracts/state_machines/state_machines.yaml 中 Project 状态机
   与 services/project_planning/domain/state_machine.py 的转移表一致性。

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
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] YAML 解析失败: {path} -> {exc}")
        return False


def check_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except Exception as exc:  # noqa: BLE001
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
# Project 状态机一致性校验
# --------------------------------------------------------------------------- #
def check_project_state_machine() -> bool:
    """校验 Project 状态机：契约 state_machines.yaml ↔ 领域 state_machine.py。"""
    sm_path = CONTRACTS / "state_machines" / "state_machines.yaml"
    data = yaml.safe_load(sm_path.read_text(encoding="utf-8"))

    project_entry = None
    for obj in data.get("objects", []):
        if obj.get("object") == "Project":
            project_entry = obj
            break

    if project_entry is None:
        print("[FAIL] state_machines.yaml 中未找到 Project 状态机条目")
        return False

    contract_states = project_entry.get("states", "")
    contract_transitions = parse_state_chain(contract_states)

    # 领域实现转移表
    from services.project_planning.domain.state_machine import ALLOWED_TRANSITIONS

    impl_transitions: set[tuple[str, str]] = set()
    for src, targets in ALLOWED_TRANSITIONS.items():
        for tgt in targets:
            impl_transitions.add((src.value, tgt.value))

    # 双向比对：契约转移 == 实现转移
    only_contract = contract_transitions - impl_transitions
    only_impl = impl_transitions - contract_transitions

    if only_contract or only_impl:
        if only_contract:
            print(f"[FAIL] 契约有、实现缺的转移: {sorted(only_contract)}")
        if only_impl:
            print(f"[FAIL] 实现有、契约缺的转移: {sorted(only_impl)}")
        return False

    # 一致，但若登记状态为待复核，给出提示（不阻断）。
    status = project_entry.get("registration_status", "")
    if status == "PENDING_PHASE1_REVIEW":
        print(
            "[WARN] Project 状态机已一致，但契约为 PENDING_PHASE1_REVIEW"
            "（待 Phase 1 验收复核固化），请勿在生产环境修改其状态集。"
        )
    print(f"[PASS] Project 状态机一致：{len(contract_transitions)} 条转移")
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

    ok &= check_project_state_machine()

    if ok:
        print("[PASS] 契约一致性校验通过")
        return 0
    print("[FAIL] 契约一致性校验未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
