"""契约一致性校验脚本（占位）。

Phase 1 实现后，本脚本负责校验：
1. contracts/openapi/openapi.draft.yaml 与 apps/api 路由实现一致
2. contracts/schemas/object_schemas.schema.json 与领域模型派生一致
3. contracts/state-machines/state_machines.yaml 与状态机实现一致
4. contracts/action-registry/action_registry.yaml 与权限落点一致

当前为占位实现：仅做契约文件的语法/结构基本校验。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"


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


def main() -> int:
    ok = True
    yaml_files = list(CONTRACTS.rglob("*.yaml")) + list(CONTRACTS.rglob("*.yml"))
    json_files = list(CONTRACTS.rglob("*.json"))

    for f in yaml_files:
        ok &= check_yaml(f)
    for f in json_files:
        ok &= check_json(f)

    print(f"已校验 YAML {len(yaml_files)} 个，JSON {len(json_files)} 个")
    if ok:
        print("[PASS] 契约文件结构校验通过")
        return 0
    print("[FAIL] 存在契约文件解析错误")
    return 1


if __name__ == "__main__":
    sys.exit(main())
