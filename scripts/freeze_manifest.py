"""Phase 0 冻结包清单：生成与校验（契约冻结生命周期的唯一权威实现）。

为什么这个脚本必须存在
--------------------
第二轮审查之后，``contract_check.py`` 一直报 PASS，但第三轮审查发现它证明的是
「两份派生文件互相一致」（81 个 Action ↔ 81 条 OpenAPI），而**不能**证明
「仍然符合 V2.0.5 基线」（基线是 75 / 75 / 76 / 35）。派生文件被直接改、
冻结清单没有重新生成，于是出现「绿着但已漂移」。

本脚本闭合这条断链，判定分三层，且规则只在这里实现一次：

1. **权威源未变**：基线主文档的字节数与 SHA-256 与冻结清单一致；
2. **派生文件未私改**：十一类冻结产物的字节数与 SHA-256 与清单一致；
3. **演进有据**：当前契约名称全集必须精确等于
   「基线清单 ∪ 已批准修订」——任何未登记的新增、缺失的基线项、
   登记了却不存在的陈旧条目，全部报红。

第 3 条是关键：它让「合规演进」与「静默漂移」在机器上可区分。
只检查数量不够（+6 可以换成「删 6 加 6」），只检查哈希也不够
（重新生成清单就把哈希「修好」了），必须同时锁名称全集。

与 ``contract_check.py`` 的分工
------------------------------
- 本脚本：**基线一致性**（是否符合 V2.0.5，只看名称全集与冻结产物完整性）；
- ``contract_check.py``：**派生一致性**（Action ↔ OpenAPI ↔ Schema ↔ 状态机
  之间的交叉引用、字段、事件、债务台账是否自洽）。

两者都不重复对方的规则，避免同一规则两处维护后各自漂移。

用法
----
    python scripts/freeze_manifest.py --check   # CI 与本地闸门（只读）
    python scripts/freeze_manifest.py --write   # 修订获批后重新生成清单

``--write`` 只在**人类批准的修订已经落到 contract_amendments.yaml** 之后使用；
它不是「让闸门变绿」的工具，写之前 ``--check`` 报的漂移项必须先解释清楚。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE_DOC = ROOT / "docs" / "baseline" / "AI影视工业化生产平台V2.0.5.md"

# 治理 YAML 统一走严格 loader（拒绝重复映射键）；独立运行时需注入仓库根到 sys.path。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from packages.shared.yaml_strict import DuplicateYamlKeyError, load_yaml_file  # noqa: E402

# 「游离副本」扫描时跳过的目录：它们不是仓库交付物，把基线正文放进去也不构成
# 「读者会读到第二份正文」的风险（依赖包与工具缓存的内部文件不会被当作基线引用）。
_SCAN_EXCLUDED_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__",
     ".mypy_cache", ".ruff_cache", ".pytest_cache"}
)

# 正文 31.6 的自述数量口径：基线是「冻结时刻的快照」，它自述的 action 数必须等于
# 冻结清单登记的基线数，不能随手改成「当前注册数」（那是派生文件的事实）。
_SELF_REPORTED_ACTIONS = re.compile(r"当前共注册\s*(\d+)\s*项规范 action_key")
INVENTORY_PATH = ROOT / "contracts" / "phase0" / "frozen_inventory.yaml"
AMENDMENTS_PATH = ROOT / "contracts" / "phase0" / "contract_amendments.yaml"
MANIFEST_PATH = ROOT / "contracts" / "phase0" / "phase0_manifest.md"
MANIFEST_SHA_PATH = ROOT / "contracts" / "phase0" / "phase0_manifest.md.sha256"

ACTION_REGISTRY = ROOT / "contracts" / "action_registry" / "action_registry.yaml"
OPENAPI = ROOT / "contracts" / "openapi" / "openapi.draft.yaml"
OBJECT_SCHEMAS = ROOT / "contracts" / "schemas" / "object_schemas.schema.json"
STATE_MACHINES = ROOT / "contracts" / "state_machines" / "state_machines.yaml"


class FreezeDriftError(ValueError):
    """冻结包与基线/修订登记不一致。

    单独定义而不是直接用 ``ValueError``：``main`` 需要区分「可预期的漂移，
    打印 [FAIL] 后退出」与「脚本自身的编程错误，应保留 traceback 以便排查」。
    """


@dataclass(frozen=True)
class Artifact:
    """十一类冻结产物中的一份。

    ``bytes``/``sha256`` 为 None 表示自指文件（清单自身），其哈希写在
    ``phase0_manifest.md.sha256`` 里，不在此表中登记。
    """

    category: str
    path: Path
    section: str
    self_reference: bool = False


# 十一类冻结产物：顺序与基线 31.11 的类目编号一致，便于人工对照。
ARTIFACTS: tuple[Artifact, ...] = (
    Artifact("① 对象 Schema", OBJECT_SCHEMAS, "8.x 对象契约 / C.3 数据契约"),
    Artifact("② OpenAPI 草案", OPENAPI, "31.6（由 Action Registry 派生）"),
    Artifact("③ 状态机", STATE_MACHINES, "31.5 / 8.6 / 32.6 / 33.3 / 34.7"),
    Artifact("④ Action Registry", ACTION_REGISTRY, "31.6 唯一注册表"),
    Artifact("⑤ 权限矩阵", ROOT / "contracts" / "permission" / "permission_matrix.yaml", "C.5 / 31.6"),
    Artifact("⑥ 预算与成本口径", ROOT / "contracts" / "budget" / "budget_caliber.yaml", "C.2 / 34.6"),
    Artifact("⑦ 指标字典", ROOT / "contracts" / "metrics" / "metric_dictionary.yaml", "31.10 / 34.13"),
    Artifact("⑧ 冻结包清单", MANIFEST_PATH, "31.11 契约证据清单", self_reference=True),
    Artifact("⑨ 枚举注册表", ROOT / "contracts" / "enums" / "enum_registry.yaml", "31.5 / C.7"),
    Artifact("⑩ ADR 主索引", ROOT / "docs" / "adr" / "adr_index.yaml", "31.12 / 32.13 / 33.15"),
    Artifact("⑪ 阶段表", ROOT / "contracts" / "phases" / "phase_map.yaml", "31.2 唯一阶段总表"),
)


def _load_yaml(path: Path) -> Any:
    """读取 YAML；文件缺失/重复键属于冻结包损坏，抛可读错误而非静默覆盖。"""
    if not path.exists():
        raise FreezeDriftError(f"缺少文件：{_rel(path)}")
    try:
        return load_yaml_file(path)
    except DuplicateYamlKeyError as exc:
        # 重复映射键曾让 contract_amendments 的 revocations 块静默失效；
        # 冻结校验必须把它当作包损坏报红，而不是让 safe_load 隐式取后键。
        raise FreezeDriftError(f"冻结包 YAML 损坏：{exc}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path) -> str:
    """路径展示口径：仓库内用相对路径，仓库外（测试临时目录）用绝对路径。"""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _mapping(value: Any, where: str) -> dict[str, Any]:
    """收敛「期望映射却拿到别的东西」这一类坏形状。"""
    if not isinstance(value, dict):
        raise FreezeDriftError(f"{where} 必须是映射，实际是 {type(value).__name__}")
    return value


def _sequence(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise FreezeDriftError(f"{where} 必须是列表，实际是 {type(value).__name__}")
    return value


def _strings(value: Any, where: str) -> list[str]:
    """取字符串列表；元素非字符串即坏形状（而不是静默 str() 后被当成合法名称）。"""
    items = _sequence(value, where)
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise FreezeDriftError(f"{where}[{index}] 必须是非空字符串，实际是 {item!r}")
    return items


# --------------------------------------------------------------------------- #
# 从派生文件读出「当前契约名称全集」
# --------------------------------------------------------------------------- #
def current_actions() -> set[str]:
    data = _mapping(_load_yaml(ACTION_REGISTRY), "action_registry.yaml")
    actions = _sequence(data.get("actions"), "action_registry.actions")
    keys: list[str] = []
    for index, item in enumerate(actions):
        entry = _mapping(item, f"action_registry.actions[{index}]")
        key = entry.get("action_key")
        if not isinstance(key, str) or not key.strip():
            raise FreezeDriftError(f"action_registry.actions[{index}].action_key 缺失或非字符串")
        keys.append(key)
    if len(set(keys)) != len(keys):
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        raise FreezeDriftError(f"action_registry 存在重复 action_key：{duplicates}")
    declared_total = data.get("total_actions")
    if declared_total != len(keys):
        raise FreezeDriftError(
            f"action_registry.total_actions={declared_total} 与实际条目数 {len(keys)} 不一致"
        )
    return set(keys)


def current_schema_objects() -> set[str]:
    import json

    if not OBJECT_SCHEMAS.exists():
        raise FreezeDriftError(f"缺少文件：{_rel(OBJECT_SCHEMAS)}")
    data = _mapping(json.loads(OBJECT_SCHEMAS.read_text(encoding="utf-8")), "object_schemas")
    defs = data.get("$defs") or data.get("definitions")
    if not isinstance(defs, dict) or not defs:
        raise FreezeDriftError("object_schemas 未包含 $defs/definitions 定义集合")
    return set(defs)


def current_state_machines() -> set[str]:
    data = _mapping(_load_yaml(STATE_MACHINES), "state_machines.yaml")
    objects = _sequence(data.get("objects"), "state_machines.objects")
    names: list[str] = []
    for index, item in enumerate(objects):
        entry = _mapping(item, f"state_machines.objects[{index}]")
        name = entry.get("object")
        if not isinstance(name, str) or not name.strip():
            raise FreezeDriftError(f"state_machines.objects[{index}].object 缺失或非字符串")
        names.append(name)
    return set(names)


# --------------------------------------------------------------------------- #
# 第一层：权威源未变
# --------------------------------------------------------------------------- #
def check_baseline_document(inventory: dict[str, Any]) -> list[str]:
    """基线主文档必须与冻结清单登记的字节数和哈希完全一致。"""
    baseline = _mapping(inventory.get("baseline"), "frozen_inventory.baseline")
    if not BASELINE_DOC.exists():
        return [f"缺少基线主文档：{_rel(BASELINE_DOC)}（权威源必须随工程交付）"]
    problems: list[str] = []
    actual_bytes = BASELINE_DOC.stat().st_size
    expected_bytes = baseline.get("document_bytes")
    if actual_bytes != expected_bytes:
        problems.append(f"基线主文档字节数 {actual_bytes} ≠ 登记的 {expected_bytes}")
    actual_hash = _sha256(BASELINE_DOC)
    expected_hash = str(baseline.get("document_sha256") or "")
    if actual_hash != expected_hash:
        problems.append(
            f"基线主文档 SHA-256 变更：{actual_hash[:16]}… ≠ 登记的 {expected_hash[:16]}…；"
            "正文哈希变化即新修订，必须重新派生全部契约产物并重签清单"
        )
    return problems


def check_stray_baseline_copies() -> list[str]:
    """仓库内不得存在与基线**同名**的正文副本（只看文件名，不看内容）。

    判定依据是「同名即歧义」而不是「内容不同才算漂移」：读者（尤其是新加入的
    人或 Agent）按文件名找权威源，两份同名的正文会让「哪一份受校验」变成需要
    考古的问题；内容相同也一样——因为下一个人改的可能是没受校验的那一份，而
    CI 全程绿灯。内容不同只是把这种歧义变成了既成事实。

    存量教训（N-01）：根目录曾长期存在一份手工回填过修订的副本，`README` 还把它
    称为「配套基线文档」，于是同一件事出现 75 / 83 / 84 三个数字，且引用了一个
    不存在的 ADR。固定路径校验对此完全无感。
    """
    if not BASELINE_DOC.exists():
        # 缺失由 check_baseline_document 报，这里不重复报同一件事。
        return []
    name = BASELINE_DOC.name
    folded = name.casefold()
    authoritative = BASELINE_DOC.resolve()
    stray: list[Path] = []
    for current, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _SCAN_EXCLUDED_DIRS]
        # 按 casefold 比对：Windows 上同名不同大小写本就是同一个文件，而 Linux CI
        # 大小写敏感，`…V2.0.5.MD` 会以「不同文件名」逃过精确匹配。
        matches = [f for f in files if f.casefold() == folded]
        for filename in matches:
            candidate = Path(current) / filename
            if candidate.resolve() != authoritative:
                stray.append(candidate)
    return [
        f"发现基线正文的游离副本：{_rel(path)}；基线唯一权威路径是 {_rel(BASELINE_DOC)}"
        "——正文修订只能改权威路径并在修改后重新派生、重签清单，副本必须删除"
        for path in sorted(stray)
    ]


def check_baseline_self_reported_counts(inventory: dict[str, Any]) -> list[str]:
    """正文自述的 action 数量必须等于冻结清单登记的基线数量。

    基线正文 31.6 有一句「当前共注册 N 项规范 action_key」。它描述的是**冻结时刻**
    的事实，因此 N 必须等于 ``frozen_inventory.baseline.counts.actions``，而不是
    当前注册表的大小（后者已含修订增量，登记在 ``contract_amendments.yaml``）。

    这条断言针对的失败形态很具体：手工把修订内容回填正文时，很自然地会顺手把 N
    改成「现在有多少个」——回填 8 条却写 83、而真实是 84，正文与注册表就此分叉，
    且没有任何一层能发现（哈希层只保证「正文没被改」，不保证「正文说得对」）。

    句子不存在时不报错：删除这句话必然改变正文字节，由第一层哈希校验拦截，无需
    在这里重复判定。
    """
    if not BASELINE_DOC.exists():
        return []
    baseline = _mapping(inventory.get("baseline"), "frozen_inventory.baseline")
    counts = _mapping(baseline.get("counts"), "frozen_inventory.baseline.counts")
    expected = counts.get("actions")
    text = BASELINE_DOC.read_text(encoding="utf-8")
    problems: list[str] = []
    for matched in _SELF_REPORTED_ACTIONS.finditer(text):
        declared = int(matched.group(1))
        if declared != expected:
            problems.append(
                f"基线正文 31.6 自述 action 数 {declared} ≠ 冻结清单登记的基线数 {expected}；"
                "修订增量登记在 contract_amendments.yaml，正文不得回填成「当前注册数」"
            )
    return problems


# --------------------------------------------------------------------------- #
# 第二层：派生文件未私改
# --------------------------------------------------------------------------- #
def artifact_stats() -> list[tuple[Artifact, int | None, str | None]]:
    """计算十一类产物的字节数与哈希；缺失文件按 0/None 记录，由校验统一报错。"""
    stats: list[tuple[Artifact, int | None, str | None]] = []
    for artifact in ARTIFACTS:
        if artifact.self_reference or not artifact.path.exists():
            stats.append((artifact, None, None))
            continue
        stats.append((artifact, artifact.path.stat().st_size, _sha256(artifact.path)))
    return stats


def check_manifest_integrity() -> list[str]:
    """清单正文登记的字节数/哈希必须与磁盘实际一致，且清单自身哈希可校验。"""
    if not MANIFEST_PATH.exists():
        return [f"缺少冻结包清单：{_rel(MANIFEST_PATH)}（先执行 --write）"]
    problems: list[str] = []
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    for artifact, size, digest in artifact_stats():
        if artifact.self_reference:
            continue
        rel = _rel(artifact.path)
        if size is None or digest is None:
            problems.append(f"{artifact.category} 文件缺失：{rel}")
            continue
        if f"`{rel}`" not in text:
            problems.append(f"清单未登记 {artifact.category} 的文件名：{rel}")
            continue
        if f"`{digest}`" not in text:
            problems.append(
                f"{artifact.category} 内容已变更但清单未重签：{rel}"
                f"（实际 {digest[:16]}…）；冻结产物被改动必须走修订流程后 --write"
            )
    if not MANIFEST_SHA_PATH.exists():
        problems.append("缺少 phase0_manifest.md.sha256（清单自指哈希）")
        return problems
    declared = MANIFEST_SHA_PATH.read_text(encoding="utf-8").strip().split()
    if not declared:
        problems.append("phase0_manifest.md.sha256 内容为空")
        return problems
    if _sha256(MANIFEST_PATH) != declared[0]:
        problems.append("清单自身哈希与 .sha256 不符（清单正文被手改过）")
    return problems


# --------------------------------------------------------------------------- #
# 第三层：演进有据（基线 ∪ 修订 == 实际，双向精确）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Amendment:
    """一条已批准修订的规范化视图（只保留判定需要的字段）。"""

    id: str
    added_actions: frozenset[str]
    added_objects: frozenset[str]
    added_state_machines: frozenset[str]
    removed_actions: frozenset[str]
    removed_objects: frozenset[str]
    removed_state_machines: frozenset[str]


_REQUIRED_AMENDMENT_FIELDS = ("title", "reason", "adr", "approved_by", "approved_at")


def _collect_names(
    entries: Any, key: str, where: str
) -> frozenset[str]:
    """收集 ``[{action_key: X}, ...]`` 或 ``[{object: X}, ...]`` 的名称集合。"""
    names: list[str] = []
    for index, item in enumerate(_sequence(entries, where)):
        entry = _mapping(item, f"{where}[{index}]")
        name = entry.get(key)
        if not isinstance(name, str) or not name.strip():
            raise FreezeDriftError(f"{where}[{index}].{key} 缺失或非字符串")
        names.append(name)
    return frozenset(names)


def load_amendments() -> list[Amendment]:
    """读取并校验修订登记的结构完整性（缺字段即 FAIL，不允许含糊债务）。"""
    data = _mapping(_load_yaml(AMENDMENTS_PATH), "contract_amendments.yaml")
    entries = _sequence(data.get("amendments") or [], "contract_amendments.amendments")
    amendments: list[Amendment] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(entries):
        where = f"contract_amendments.amendments[{index}]"
        entry = _mapping(item, where)
        amendment_id = entry.get("id")
        if not isinstance(amendment_id, str) or not amendment_id.strip():
            raise FreezeDriftError(f"{where}.id 缺失")
        if amendment_id in seen_ids:
            raise FreezeDriftError(f"修订 id 重复：{amendment_id}")
        seen_ids.add(amendment_id)
        missing = [key for key in _REQUIRED_AMENDMENT_FIELDS if not str(entry.get(key) or "").strip()]
        if missing:
            raise FreezeDriftError(f"修订 {amendment_id} 缺字段：{missing}")
        additions = _mapping(entry.get("additions") or {}, f"{where}.additions")
        removals = _mapping(entry.get("removals") or {}, f"{where}.removals")
        amendments.append(
            Amendment(
                id=amendment_id,
                added_actions=_collect_names(
                    additions.get("actions") or [], "action_key", f"{where}.additions.actions"
                ),
                added_objects=_collect_names(
                    additions.get("schema_objects") or [],
                    "object",
                    f"{where}.additions.schema_objects",
                ),
                added_state_machines=_collect_names(
                    additions.get("state_machines") or [],
                    "object",
                    f"{where}.additions.state_machines",
                ),
                removed_actions=_collect_names(
                    removals.get("actions") or [], "action_key", f"{where}.removals.actions"
                ),
                removed_objects=_collect_names(
                    removals.get("schema_objects") or [],
                    "object",
                    f"{where}.removals.schema_objects",
                ),
                removed_state_machines=_collect_names(
                    removals.get("state_machines") or [],
                    "object",
                    f"{where}.removals.state_machines",
                ),
            )
        )
    return amendments


def _diff_against_baseline(
    label: str,
    baseline: set[str],
    actual: set[str],
    added: set[str],
    removed: set[str],
) -> list[str]:
    """判定「实际 == (基线 − 撤销) ∪ 增补」，并区分四种失败语义。

    四种失败必须分开报，因为处理方式完全不同：
    - 未登记新增：有人绕过了修订流程（要补登记或回滚，不是改代码）；
    - 基线项缺失：冻结内容被删（可能是误删，也可能是未登记的缩减）；
    - 陈旧增补：登记过期（实现已删但修订还在，属台账失真）；
    - 撤销与增补冲突：同一条目既加又删，登记自相矛盾。
    """
    problems: list[str] = []
    expected = (baseline - removed) | added
    unregistered = sorted(actual - expected)
    missing = sorted(expected - actual)
    stale = sorted(added - actual)
    conflicted = sorted(added & removed)
    if unregistered:
        problems.append(
            f"{label} 存在未登记新增 {unregistered}："
            "必须先登记契约修订（contract_amendments.yaml）再改派生文件"
        )
    if missing:
        problems.append(f"{label} 缺少基线/登记项 {missing}：冻结内容不得静默删除")
    if stale:
        problems.append(f"{label} 修订登记陈旧 {stale}：实际已不存在，必须删条目或补实现")
    if conflicted:
        problems.append(f"{label} 同一条目既增补又撤销 {conflicted}：登记自相矛盾")
    return problems


def check_amendment_consistency(inventory: dict[str, Any]) -> list[str]:
    """基线清单 + 已批准修订 必须与当前契约名称全集逐项相等。"""
    baseline = _mapping(inventory.get("baseline"), "frozen_inventory.baseline")
    counts = _mapping(baseline.get("counts"), "frozen_inventory.baseline.counts")

    base_actions = set(_strings(inventory.get("actions"), "frozen_inventory.actions"))
    base_objects = set(_strings(inventory.get("schema_objects"), "frozen_inventory.schema_objects"))
    base_machines = set(_strings(inventory.get("state_machines"), "frozen_inventory.state_machines"))

    # 清单自述数量必须与名称全集长度一致，避免「改了列表忘了改计数」。
    problems: list[str] = []
    for label, declared, names in (
        ("actions", counts.get("actions"), base_actions),
        ("schema_objects", counts.get("schema_objects"), base_objects),
        ("state_machines", counts.get("state_machines"), base_machines),
    ):
        if declared != len(names):
            problems.append(
                f"frozen_inventory.baseline.counts.{label}={declared} 与名称全集 {len(names)} 不一致"
            )

    amendments = load_amendments()
    added_actions = frozenset().union(*(a.added_actions for a in amendments)) if amendments else frozenset()
    added_objects = frozenset().union(*(a.added_objects for a in amendments)) if amendments else frozenset()
    added_machines = (
        frozenset().union(*(a.added_state_machines for a in amendments)) if amendments else frozenset()
    )
    removed_actions = frozenset().union(*(a.removed_actions for a in amendments)) if amendments else frozenset()
    removed_objects = frozenset().union(*(a.removed_objects for a in amendments)) if amendments else frozenset()
    removed_machines = (
        frozenset().union(*(a.removed_state_machines for a in amendments)) if amendments else frozenset()
    )

    # 增补项若已在基线里，说明登记写错了（重复登记 = 台账噪声）。
    for label, overlap in (
        ("actions", set(added_actions) & base_actions),
        ("schema_objects", set(added_objects) & base_objects),
        ("state_machines", set(added_machines) & base_machines),
    ):
        if overlap:
            problems.append(f"{label} 修订登记了基线已有的条目 {sorted(overlap)}：重复登记")

    problems += _diff_against_baseline(
        "Action", base_actions, current_actions(), set(added_actions), set(removed_actions)
    )
    problems += _diff_against_baseline(
        "Schema 对象", base_objects, current_schema_objects(), set(added_objects), set(removed_objects)
    )
    problems += _diff_against_baseline(
        "状态机", base_machines, current_state_machines(), set(added_machines), set(removed_machines)
    )
    return problems


def check_openapi_parity() -> list[str]:
    """OpenAPI 操作数与 Action 数必须一致（派生关系，不是独立契约）。

    这条放在这里而不是 contract_check，是因为它属于「基线 75 条路径由
    75 个 Action 派生」这一条基线的延伸：派生关系一旦断开，41.6 的
    「契约可校验」就失去意义。
    """
    data = _mapping(_load_yaml(OPENAPI), "openapi.draft.yaml")
    paths = _mapping(data.get("paths") or {}, "openapi.paths")
    methods = {"get", "post", "put", "patch", "delete"}
    operations = sum(1 for item in paths.values() for method in item if method in methods)
    actions = len(current_actions())
    if operations != actions:
        return [f"OpenAPI 操作数 {operations} ≠ Action 数 {actions}（派生关系断裂）"]
    return []


# --------------------------------------------------------------------------- #
# 清单生成
# --------------------------------------------------------------------------- #
def write_manifest() -> None:
    """按磁盘实际状态重写清单与自指哈希。

    只在修订获批后调用；脚本不判断「该不该重签」，那是人的决定。
    """
    inventory = _mapping(_load_yaml(INVENTORY_PATH), "frozen_inventory.yaml")
    baseline = _mapping(inventory.get("baseline"), "frozen_inventory.baseline")
    rows: list[str] = []
    for artifact, size, digest in artifact_stats():
        rel = _rel(artifact.path)
        if artifact.self_reference:
            rows.append(
                f"| {artifact.category} | `{rel}` | — | 自指不登记，见 "
                f"`phase0_manifest.md.sha256` | {artifact.section} |"
            )
        elif size is None or digest is None:
            raise FreezeDriftError(f"{artifact.category} 文件不存在，无法登记：{rel}")
        else:
            rows.append(f"| {artifact.category} | `{rel}` | {size} | `{digest}` | {artifact.section} |")

    amendments = load_amendments()
    amendment_lines = (
        "\n".join(f"- `{a.id}`" for a in amendments) if amendments else "- （无）"
    )
    body = f"""# Phase 0 契约冻结包清单

> 本文件由 `scripts/freeze_manifest.py --write` 生成，**禁止手改**。
> 手改会被 `--check` 的清单自指哈希校验当场判定为篡改。

基线文档：`docs/baseline/AI影视工业化生产平台V2.0.5.md`（{baseline.get("document_bytes")} 字节）
文档版本：`{baseline.get("revision")}`（版本号按项目约束冻结，不随修订升号）
冻结时刻仓库快照：`{baseline.get("frozen_at_source_commit")}`
基线 SHA-256：`{baseline.get("document_sha256")}`

本清单把 31.11 声明的十一类契约文件与磁盘实际文件逐项对齐：每份文件给出类目、
字节数、SHA-256 与对应正文章节，可直接复核。第八类即本清单自身，因自指不登记
自身哈希，其哈希见同目录 `phase0_manifest.md.sha256`，由生成器在写入后计算。

## 文件清单

| 正文类目 | 文件 | 字节 | SHA-256 | 对应章节 |
|---|---|---|---|---|
{chr(10).join(rows)}

## 已批准修订

{amendment_lines}

契约名称全集必须精确等于「基线清单 ∪ 已批准修订」，由
`scripts/freeze_manifest.py --check` 逐名称判定（不是只看数量）。

## 校验命令

```bash
python scripts/freeze_manifest.py --check   # 基线未变 + 派生未私改 + 演进有据
python scripts/contract_check.py            # 派生之间的交叉一致性
```

## 写入口径

1. 只改基线文档 → 必须新修订升版，并重新派生全部产物后 `--write`；
2. 只加业务契约（Action / Schema / 状态机）→ 先在 `contract_amendments.yaml`
   登记理由与审批人，再改派生文件，最后 `--write`；
3. 顺序反了（先改派生文件）→ `--check` 报「未登记新增」，不得用 `--write` 掩盖；
4. 旧机制清理：不得保留任何手写的数量副本（`total_actions` 等必须与条目数一致）。
"""
    MANIFEST_PATH.write_text(body, encoding="utf-8")
    MANIFEST_SHA_PATH.write_text(
        f"{_sha256(MANIFEST_PATH)}  phase0_manifest.md\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def check_all() -> list[str]:
    """三层判定合并执行，返回全部问题（而不是遇到第一个就退出）。"""
    inventory = _mapping(_load_yaml(INVENTORY_PATH), "frozen_inventory.yaml")
    problems: list[str] = []
    problems += check_baseline_document(inventory)
    problems += check_stray_baseline_copies()
    problems += check_baseline_self_reported_counts(inventory)
    problems += check_manifest_integrity()
    problems += check_amendment_consistency(inventory)
    problems += check_openapi_parity()
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 0 冻结包清单生成与校验")
    parser.add_argument(
        "--write",
        action="store_true",
        help="按磁盘实际状态重写清单（仅在修订获批后使用）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验（默认行为，CI 使用）",
    )
    args = parser.parse_args(argv)

    try:
        if args.write:
            write_manifest()
            print(f"[PASS] 已重写冻结包清单：{_rel(MANIFEST_PATH)}")
            return 0
        problems = check_all()
    except FreezeDriftError as exc:
        print(f"[FAIL] 冻结包结构损坏：{exc}")
        return 1
    except Exception as exc:
        # 兜底：CI 只看到 traceback 会失去结论，这里收敛成一行可读失败原因。
        print(f"[FAIL] 冻结包校验异常：{type(exc).__name__}: {exc}")
        return 1

    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}")
        return 1

    inventory = _mapping(_load_yaml(INVENTORY_PATH), "frozen_inventory.yaml")
    baseline = _mapping(inventory["baseline"], "frozen_inventory.baseline")
    counts = _mapping(baseline["counts"], "frozen_inventory.baseline.counts")
    actions = current_actions()
    objects = current_schema_objects()
    machines = current_state_machines()
    amendments = load_amendments()
    print(
        f"[PASS] 契约冻结：基线 {baseline['revision']} 未变"
        f"（{str(baseline['document_sha256'])[:16]}…）"
        f" / 十一类产物哈希一致"
        f" / 演进有据（{len(amendments)} 条修订："
        f"Action {len(actions)} = {counts['actions']}+{len(actions) - int(counts['actions'])}，"
        f"Schema {len(objects)} = {counts['schema_objects']}"
        f"+{len(objects) - int(counts['schema_objects'])}，"
        f"状态机 {len(machines)} = {counts['state_machines']}"
        f"+{len(machines) - int(counts['state_machines'])}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
