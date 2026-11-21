"""核验电池：把「事实是否成立」变成可复算的执行记录。

为什么需要它
------------
标注规范（``docs/standards/verification-and-audit-annotation-standard.md``）要求报告给出
带证据等级的核验表。**手填这张表就是一份会腐烂的快照**：新增一类可核验对象时表里不会
多一行，漏检表现为正常；某项标注过时也没人回头改。所以本脚本把核验表变成产物。

它是什么、不是什么
------------------
**是**：一组真实执行的探针，每个探针返回「状态 + 证据等级 + 证据位置 + 未覆盖面」，
      外加从闸门输出解析出的关键指标，最后渲染成报告可直接引用的 Markdown。
**不是**：合规判断器。本脚本只说「事实如何」，不说「是否违反基准」——后者是审计结论，
      由报告人工给出并逐条引用这里的核验编号。混在一起会让「确实实现了」与「实现得对」
      互相掩盖，这正是规范 §0 要防的第一类错误。

与既有闸门的边界（不复制第三份实现）
------------------------------------
- 静态/契约检查**不在这里定义命令**：一律从 ``merge_readiness.CHECK_COMMANDS`` 读取，
  解释器解析复用 ``merge_readiness.resolve_command``。命令清单只有一份，新增闸门会自动
  出现在核验表里；某个命令没在 ``_COMMAND_CATEGORY`` 登记类别时**报红**（fail-closed），
  而不是悄悄落进默认类别。
- ``merge_readiness`` 的 docstring 已声明它**不覆盖** pytest 全量、PostgreSQL 迁移、
  downgrade→upgrade 往返。本脚本只补这一段缺口，不重跑它已编排的东西。
- 本脚本补充的、任何闸门都不回答的判据：基线正文与侧车的独立哈希复算、修订台账的
  只追加性（改写历史条目会报红）。

用法
----
    python scripts/verification_battery.py                    # 全部探针，写出证据文件
    python scripts/verification_battery.py --skip-tests       # 跳过 pytest（分钟级）
    python scripts/verification_battery.py --out -            # 打到标准输出，不落盘
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import yaml  # noqa: E402
from merge_readiness import CHECK_COMMANDS, resolve_command  # noqa: E402

from packages.shared.yaml_strict import load_yaml_file  # noqa: E402

BASELINE_DOC = ROOT / "docs" / "baseline" / "AI影视工业化生产平台V2.0.5.md"
INVENTORY = ROOT / "contracts" / "phase0" / "frozen_inventory.yaml"
MANIFEST = ROOT / "contracts" / "phase0" / "phase0_manifest.md"
MANIFEST_SHA = ROOT / "contracts" / "phase0" / "phase0_manifest.md.sha256"
AMENDMENTS = ROOT / "contracts" / "phase0" / "contract_amendments.yaml"

# 规范 §5 固定的七类核验对象。这里只登记类别，不登记具体条目——
# 条目由下面的探针派生，避免「类别清单」与「实际探针」两处维护。
CATEGORIES: dict[str, str] = {
    "C1": "基准文件完整性",
    "C2": "冻结清单与修订台账",
    "C3": "契约一致性",
    "C4": "Manifest 哈希",
    "C5": "静态与结构检查",
    "C6": "数据库迁移",
    "C7": "自动化测试与核心链路",
}

# ``merge_readiness.CHECK_COMMANDS`` 的每一项必须在这里登记「类别 + 稳定核验编号」。
# 未登记即报红：新增闸门却不归类，等于让它游离在七类覆盖之外。
# 编号刻意写死而不是从名称 hash 出来——报告里的编号必须跨次运行可引用，
# hash() 受 PYTHONHASHSEED 影响，同一项检查每次跑出来的编号都不一样。
_COMMAND_REGISTRY: dict[str, tuple[str, str]] = {
    "ruff 全仓 lint": ("C5", "C5.1"),
    "mypy 类型检查": ("C5", "C5.2"),
    "契约派生一致性": ("C3", "C3.1"),
    "分层依赖校验": ("C5", "C5.3"),
    "契约冻结校验": ("C2", "C2.1"),
}

VERIFIED = "V-VERIFIED"
PARTIAL = "V-PARTIAL"
UNVERIFIED = "V-UNVERIFIED"
FAILED = "V-FAILED"
BLOCKED = "V-BLOCKED"
NA = "V-NA"


@dataclass(frozen=True)
class Probe:
    """一条核验事实。字段与规范 §1.3 的表达格式一一对应。"""

    check_id: str
    category: str
    obj: str
    state: str
    evidence: tuple[str, ...]
    location: str
    limitation: str
    detail: str = ""


@dataclass
class Battery:
    probes: list[Probe] = field(default_factory=list)
    metrics: dict[str, str] = field(default_factory=dict)
    metric_sources: dict[str, str] = field(default_factory=dict)

    def add(self, probe: Probe) -> Probe:
        self.probes.append(probe)
        return probe

    def metric(self, key: str, value: str, where: str) -> None:
        self.metrics[key] = value
        self.metric_sources[key] = where

    # -- 完备性 ---------------------------------------------------------- #
    def coverage_gaps(self) -> list[str]:
        """双向完备性：类别缺探针、探针类别未登记，两个方向都报。

        只检查「每个类别至少有一个探针」还不够——探针声明了一个不在 CATEGORIES 里的
        类别时，那个类别会以「有探针」的姿态出现，实际没有任何规范依据。这正是
        「应检集合必须派生、不得手写」要防的形态：漏检永远发生在没人登记的角落。
        """
        gaps: list[str] = []
        declared = set(CATEGORIES)
        used = {p.category for p in self.probes}
        for missing in sorted(declared - used):
            gaps.append(f"类别 {missing}（{CATEGORIES[missing]}）没有任何探针")
        for unknown in sorted(used - declared):
            gaps.append(f"探针声明了未登记的类别 {unknown}")
        seen: set[str] = set()
        for probe in self.probes:
            if probe.check_id in seen:
                gaps.append(f"核验编号重复：{probe.check_id}")
            seen.add(probe.check_id)
        return gaps


# --------------------------------------------------------------------------- #
# 执行辅助
# --------------------------------------------------------------------------- #
def _run(
    cmd: list[str], *, timeout: int = 1800
) -> tuple[int | None, str]:
    """执行命令，返回 (退出码, 合并输出)。退出码 None 表示环境阻塞。"""
    try:
        completed = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return None, f"可执行文件不存在：{exc}"
    except subprocess.TimeoutExpired:
        return None, f"命令超时（{timeout}s）：{' '.join(cmd)}"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _tail(text: str, lines: int = 6) -> str:
    rows = [row for row in text.strip().splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> tuple[bool, str]:
    code, out = _run(["git", *args], timeout=60)
    return code == 0, out.strip()


# --------------------------------------------------------------------------- #
# C1 / C4：独立哈希复算（不引用闸门结论）
# --------------------------------------------------------------------------- #
def probe_baseline_document(battery: Battery) -> None:
    """基线正文的字节数与 SHA-256 必须与冻结清单登记值一致。

    刻意自己算一遍而不是读 ``freeze_manifest --check`` 的 PASS：
    引用闸门的结论只能得到 E3，而且闸门若被改坏，这里也会跟着绿。独立复算是 E2 的意义。
    """
    if not BASELINE_DOC.exists():
        battery.add(
            Probe(
                "C1.1",
                "C1",
                "V2.0.5 基线正文",
                FAILED,
                ("E2",),
                f"{_rel(BASELINE_DOC)}（文件不存在）",
                "缺少权威源，全部契约判定的依据不可用",
            )
        )
        return
    inventory = load_yaml_file(INVENTORY)
    baseline = inventory.get("baseline") or {}
    expected_bytes = baseline.get("document_bytes")
    expected_sha = str(baseline.get("document_sha256") or "")
    actual_bytes = BASELINE_DOC.stat().st_size
    actual_sha = _sha256(BASELINE_DOC)
    battery.metric("baseline_bytes", str(actual_bytes), "C1.1")
    battery.metric("baseline_sha256", actual_sha, "C1.1")
    matches = actual_bytes == expected_bytes and actual_sha == expected_sha
    battery.add(
        Probe(
            "C1.1",
            "C1",
            "V2.0.5 基线正文",
            VERIFIED if matches else FAILED,
            ("E2",),
            f"{_rel(BASELINE_DOC)} 与 {_rel(INVENTORY)}:baseline",
            "只证明文件字节未变，不证明其内容正确或已被人类批准"
            if matches
            else "字节数或哈希与冻结登记不一致，基线已被改动",
            detail=(
                f"bytes={actual_bytes}（登记 {expected_bytes}）; "
                f"sha256={actual_sha[:16]}…（登记 {expected_sha[:16]}…）"
            ),
        )
    )


def probe_manifest_sidecar(battery: Battery) -> None:
    """清单正文哈希 vs 侧车 ``.sha256``：独立复算，防止清单被手改后仍称一致。"""
    if not MANIFEST.exists() or not MANIFEST_SHA.exists():
        battery.add(
            Probe(
                "C4.1",
                "C4",
                "冻结清单自指哈希",
                BLOCKED,
                ("E2",),
                f"{_rel(MANIFEST)} / {_rel(MANIFEST_SHA)}",
                "清单或侧车缺失，无法核验",
            )
        )
        return
    declared = MANIFEST_SHA.read_text(encoding="utf-8").strip().split()
    actual = _sha256(MANIFEST)
    ok = bool(declared) and declared[0] == actual
    battery.metric("manifest_sha256", actual, "C4.1")
    battery.add(
        Probe(
            "C4.1",
            "C4",
            "冻结清单自指哈希",
            VERIFIED if ok else FAILED,
            ("E2",),
            f"{_rel(MANIFEST_SHA)}:1",
            "侧车与正文同源生成（--write 同时刷新），因此同谋篡改无法被本判据发现"
            if ok
            else "清单正文与侧车哈希不符：正文被手改过",
            detail=f"actual={actual[:16]}… declared={declared[0][:16] if declared else '空'}…",
        )
    )


# --------------------------------------------------------------------------- #
# C2：修订台账只追加
# --------------------------------------------------------------------------- #
def probe_amendments_append_only(battery: Battery) -> None:
    """修订只能追加，不得改写历史条目（台账头部第 3 条写入口径）。

    判据是「当前台账是 HEAD 台账的前缀超集」，而不是逐字段比对：后者会把
    「补一个 approved_at」这种合法追加也判红。前缀比对恰好表达「历史条目一字不动」。
    """
    current = load_yaml_file(AMENDMENTS)
    entries = current.get("amendments") or []
    ids = [str(e.get("id")) for e in entries if isinstance(e, dict)]
    battery.metric("amendments_count", str(len(ids)), "C2.2")
    debt = current.get("traceability_debt") or []
    battery.metric("traceability_debt", str(len(debt)), "C2.2")

    ok, head_raw = _git("show", "HEAD:contracts/phase0/contract_amendments.yaml")
    if not ok:
        battery.add(
            Probe(
                "C2.2",
                "C2",
                "修订台账只追加",
                BLOCKED,
                ("E2",),
                "git show HEAD:contracts/phase0/contract_amendments.yaml",
                "拿不到 HEAD 版本（浅克隆或无 git），无法判断是否改写历史",
            )
        )
        return
    try:
        head_ids = [str(e.get("id")) for e in (load_yaml_text(head_raw).get("amendments") or [])]
    except Exception as exc:
        battery.add(
            Probe(
                "C2.2",
                "C2",
                "修订台账只追加",
                BLOCKED,
                ("E2",),
                "HEAD 台账无法解析",
                f"无法解析 HEAD 版台账：{exc}",
            )
        )
        return
    rewritten = [i for i, old in enumerate(head_ids) if i >= len(ids) or ids[i] != old]
    battery.add(
        Probe(
            "C2.2",
            "C2",
            "修订台账只追加",
            VERIFIED if not rewritten else FAILED,
            ("E2",),
            f"{_rel(AMENDMENTS)} vs HEAD",
            "只比对 id 序列，不比对条目内容：改写正文但不改 id 的形态本判据看不到",
            detail=(
                f"HEAD {len(head_ids)} 条 / 当前 {len(ids)} 条"
                + (f"；历史位置被改写：{rewritten[:3]}" if rewritten else "")
            ),
        )
    )


def load_yaml_text(text: str) -> dict[str, Any]:
    """解析 YAML 文本（用于比对 git 中的历史版本）。"""
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# C2/C3/C5：委托 merge_readiness 的命令清单
# --------------------------------------------------------------------------- #
_METRIC_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("actions_total", r"Action Registry ↔ OpenAPI 一致：(\d+) 个 action", "C3.1"),
    ("active_actions", r"ACTIVE (\d+) 个（已实现 (\d+) / 登记未实现 (\d+)）", "C3.1"),
    ("events_total", r"事件契约：(\d+) 个事件 / (\d+) 个发出点 / 显式缺口 (\d+) 项", "C3.1"),
    ("invariants_total", r"(\d+) 条不变量（断言验证 (\d+) / partial (\d+) / debt (\d+)）", "C3.1"),
    ("schemas_total", r"Schema 对象完整：(\d+) 个定义", "C3.1"),
    (
        "transition_entry_debt_edges",
        r"状态转移入口债务：显式登记 (\d+) 条边（(\d+) 个状态机共 (\d+) 条边逐边比对）",
        "C3.1",
    ),
    ("amendments_count_reported", r"契约冻结：.*?（(\d+) 条修订", "C2.1"),
)


def _harvest_metrics(battery: Battery, output: str) -> set[str]:
    """从闸门输出里抽取指标，返回本次成功解析的键集合（供 fail-closed 判定）。"""
    harvested: set[str] = set()
    for key, pattern, source in _METRIC_PATTERNS:
        match = re.search(pattern, output)
        if not match:
            continue
        if key in ("active_actions",):
            battery.metric("active_actions", match.group(1), source)
            battery.metric("implemented_actions", match.group(2), source)
            battery.metric("registered_not_implemented", match.group(3), source)
        elif key == "events_total":
            battery.metric("events_total", match.group(1), source)
            battery.metric("events_emitted", match.group(2), source)
            battery.metric("event_gaps", match.group(3), source)
        elif key == "invariants_total":
            battery.metric("invariants_total", match.group(1), source)
            battery.metric("invariants_verified", match.group(2), source)
            battery.metric("invariants_partial", match.group(3), source)
            battery.metric("invariants_debt", match.group(4), source)
        elif key == "transition_entry_debt_edges":
            battery.metric("transition_entry_debt_edges", match.group(1), source)
            battery.metric("state_machines_compared", match.group(2), source)
            battery.metric("transition_edges_compared", match.group(3), source)
        else:
            battery.metric(key, match.group(1), source)
        harvested.add(key)
    return harvested


def _expect_format_hint(output: str) -> str:
    return _tail(output, 4).replace("\n", " / ")


def probe_gate_commands(battery: Battery) -> None:
    """逐项执行 ``CHECK_COMMANDS``，每项产出一条探针。

    这里不判断「这条检查是否应该通过」——只报告退出码与解析到的指标。
    某个命令的类别没登记时，探针落为 FAILED 并写明原因：让「新闸门游离在核验面之外」
    在表上看得见，而不是被默认类别吞掉。
    """
    for index, (name, cmd) in enumerate(CHECK_COMMANDS):
        registered = _COMMAND_REGISTRY.get(name)
        resolved = resolve_command(ROOT, cmd)
        code, output = _run(resolved, timeout=1800)
        if registered is None:
            battery.add(
                Probe(
                    f"C5.unregistered.{index}",
                    "C5",
                    f"未归类的检查器：{name}",
                    FAILED,
                    ("E3",),
                    " ".join(cmd),
                    "该命令未在 _COMMAND_REGISTRY 登记类别与编号，核验表无法说明它覆盖哪一类对象",
                )
            )
            continue
        category, check_id = registered
        if code is None:
            battery.add(
                Probe(
                    check_id,
                    category,
                    name,
                    BLOCKED,
                    ("E3",),
                    " ".join(cmd),
                    f"命令未能执行：{output.splitlines()[0] if output else '未知原因'}",
                )
            )
            continue
        harvested = _harvest_metrics(battery, output)
        # 退出码 0 但关键指标解析不到，只算部分核验：结论所依赖的数字没拿到，
        # 不能因为「进程返回 0」就宣称「已核验」。
        limitation = "本项只证明检查器自身的判定域，不覆盖运行期行为"
        state = VERIFIED if code == 0 else FAILED
        detail = ""
        if code == 0 and category == "C3" and "active_actions" not in harvested:
            state = PARTIAL
            limitation = "退出码 0，但未能从输出解析动作实现覆盖指标（输出格式可能已变）"
        if code != 0:
            state = FAILED
            detail = _expect_format_hint(output)
            limitation = "检查器非零退出，详见 detail"
        battery.add(
            Probe(
                check_id,
                category,
                name,
                state,
                ("E3",),
                " ".join(resolved),
                limitation,
                detail=detail,
            )
        )


def probe_freeze_reported_amendments(battery: Battery) -> None:
    """闸门报告的修订条数必须与台账实际条数一致（两处数字不能各说各话）。"""
    reported = battery.metrics.get("amendments_count_reported")
    actual = battery.metrics.get("amendments_count")
    if reported is None or actual is None:
        battery.add(
            Probe(
                "C2.3",
                "C2",
                "修订条数口径",
                UNVERIFIED,
                ("E3",),
                "freeze_manifest 输出 / 台账",
                "缺少任一侧数字，无法比对",
            )
        )
        return
    battery.add(
        Probe(
            "C2.3",
            "C2",
            "修订条数口径",
            VERIFIED if reported == actual else FAILED,
            ("E2", "E3"),
            f"{_rel(AMENDMENTS)} / freeze_manifest 输出",
            "只比对条数，不比对条目内容（内容一致性由 C2.1 与 C3.1 承担）",
            detail=f"闸门报告 {reported} 条 / 台账实际 {actual} 条",
        )
    )


# --------------------------------------------------------------------------- #
# C6：数据库迁移
# --------------------------------------------------------------------------- #
def probe_migrations(battery: Battery) -> None:
    """迁移链 head 与数据库 current 必须一致，且模型无未生成的变更。"""
    py = sys.executable
    heads_code, heads_out = _run([py, "-m", "alembic", "heads"], timeout=120)
    current_code, current_out = _run([py, "-m", "alembic", "current"], timeout=120)
    check_code, check_out = _run([py, "-m", "alembic", "check"], timeout=300)

    def _rev(text: str) -> str:
        for line in text.splitlines():
            match = re.match(r"^([0-9a-f]{8,})\s*\(head\)", line.strip())
            if match:
                return match.group(1)
        return ""

    head = _rev(heads_out)
    current = _rev(current_out)
    battery.metric("migration_head", head or "UNKNOWN", "C6.1")
    battery.metric("migration_current", current or "UNKNOWN", "C6.1")

    if heads_code is None or current_code is None:
        battery.add(
            Probe(
                "C6.1",
                "C6",
                "迁移链与数据库版本",
                BLOCKED,
                ("E5",),
                "alembic heads / current",
                "alembic 未能执行（依赖或配置缺失）",
            )
        )
    else:
        same = bool(head) and head == current
        battery.add(
            Probe(
                "C6.1",
                "C6",
                "迁移链与数据库版本",
                VERIFIED if same else FAILED,
                ("E5",),
                "alembic heads / alembic current（真实 PostgreSQL）",
                "只证明版本一致，不证明 downgrade 可逆（往返验证需单独执行）",
                detail=f"head={head or '未解析'} current={current or '未解析'}",
            )
        )

    if check_code is None:
        battery.add(
            Probe(
                "C6.2",
                "C6",
                "模型与迁移无漂移",
                BLOCKED,
                ("E5",),
                "alembic check",
                "alembic 未能执行",
            )
        )
    else:
        drift_free = check_code == 0
        battery.add(
            Probe(
                "C6.2",
                "C6",
                "模型与迁移无漂移",
                VERIFIED if drift_free else FAILED,
                ("E5",),
                "alembic check（真实 PostgreSQL）",
                "只比对 ORM 模型与迁移，不校验数据层约束是否真的生效",
                detail="" if drift_free else _expect_format_hint(check_out),
            )
        )


# --------------------------------------------------------------------------- #
# C7：自动化测试
# --------------------------------------------------------------------------- #
# 测试目录 → 层。这个映射是「类别登记」，不是待扫清单：
# 待扫路径从文件系统派生（见 _discover_test_targets），新增目录若没在这里登记会报红。
# 初版把路径写在元组里，结果漏了 tests/architecture——手写清单的腐烂方向恰好是漏检。
_TEST_DIR_LAYERS: dict[str, tuple[str, str]] = {
    "unit": ("C7.1", "单元"),
    "engineering": ("C7.1", "工程判据"),
    "architecture": ("C7.1", "架构判据"),
    "integration": ("C7.2", "集成（真实 PG）"),
    "resilience": ("C7.2", "韧性"),
    "permission": ("C7.2", "权限负向"),
    "e2e": ("C7.3", "端到端主链（真实 PG + ASGI）"),
    "<root>": ("C7.1", "tests 根目录用例"),
}

_TEST_ROOT = ROOT / "tests"


def _discover_test_targets() -> tuple[dict[str, list[Path]], list[str]]:
    """从文件系统派生每一层的 pytest 目标，并指出未登记的测试目录。

    返回 ``(层编号 → 路径列表, 未登记目录名)``。「未登记」单独返回而不是直接报错，
    是为了让它变成一条可见的 FAILED 探针——判据自身的缺口必须出现在核验表里。
    """
    layers: dict[str, list[Path]] = {}
    unregistered: list[str] = []
    for entry in sorted(_TEST_ROOT.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("__", ".")):
            continue
        if not any(entry.rglob("test_*.py")):
            continue
        registered = _TEST_DIR_LAYERS.get(entry.name)
        if registered is None:
            unregistered.append(entry.name)
            continue
        check_id = registered[0]
        layers.setdefault(check_id, []).append(entry)
    root_registered = _TEST_DIR_LAYERS.get("<root>")
    root_files = sorted(_TEST_ROOT.glob("test_*.py"))
    if root_files and root_registered is None:
        unregistered.append("<root>")
    elif root_files:
        layers.setdefault(root_registered[0], []).extend(root_files)
    return layers, unregistered


def _parse_pytest_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in ("passed", "failed", "error", "errors", "skipped", "xfailed"):
        match = re.search(rf"(\d+) {label}\b", output)
        if match:
            key = "error" if label == "errors" else label
            counts[key] = max(counts.get(key, 0), int(match.group(1)))
    return counts


# 宿主环境的安全删除钩子标记。命中时 pytest 的退出码不再可信：
# 它拦的是临时目录清理，不是测试断言。
_SAFE_DELETE_MARKER = "SAFE_DELETE_BULK_CONFIRM_REQUIRED"


def _pytest_state(code: int, counts: dict[str, int]) -> tuple[str, str]:
    """从退出码与 pytest 摘要推导状态：「拿不到数字」与「测出失败」必须分开。

    ``-qq`` 会抑制统计摘要，此时退出码 0、点阵全绿但 counts 为空。
    把它判成 FAILED 是误报代码缺陷；判成 VERIFIED 是凭空宣称数量证据。
    唯一正确的结论是 PARTIAL：执行本身成功，但本次没有拿到可引用的数量证据。
    """
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0)
    if code == 0 and passed == 0 and failed == 0:
        return (
            PARTIAL,
            "退出码 0，但未解析到 pytest 摘要（输出格式变化或统计被抑制），缺数量证据",
        )
    if code != 0 or failed > 0 or passed == 0:
        return FAILED, "存在失败或退出码非零，详见 detail"
    return VERIFIED, "执行通过只证明本层用例覆盖的行为，不证明生产环境可靠"


def probe_tests(battery: Battery, *, skip: bool) -> None:
    """分层执行 pytest：每层一条探针，断言基于真实执行而非文件存在。"""
    layers, unregistered = _discover_test_targets()
    for name in unregistered:
        battery.add(
            Probe(
                "C7.0",
                "C7",
                f"未登记的测试目录：tests/{name}",
                FAILED,
                ("E2",),
                f"tests/{name}",
                "该目录含 test_*.py 但未在 _TEST_DIR_LAYERS 登记归属层，核验表会漏掉它",
            )
        )
    for check_id, paths in sorted(layers.items()):
        label = "、".join(
            label for key, (cid, label) in _TEST_DIR_LAYERS.items()
            if cid == check_id and _TEST_ROOT / key in paths
        )
        if skip:
            battery.add(
                Probe(
                    check_id,
                    "C7",
                    label,
                    BLOCKED,
                    ("E4",),
                    " ".join(str(p.relative_to(ROOT)) for p in paths),
                    "本次以 --skip-tests 跳过执行，无运行证据",
                )
            )
            continue
        # pyproject 的 addopts 已含 -q；这里再传一次会叠加成 -qq，
        # pytest 就不打印「N passed in Xs」摘要，指标解析会全部落空。
        # 固定工作区内的 basetemp：默认的 /tmp/pytest-of-* 会累计几百个旧目录，
        # 宿主安全钩子一旦拦下批量回收，就会把全绿会话变成非零退出（见 C7 阻塞处理）。
        basetemp = ROOT / ".pytest-basetemp"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            *(str(p) for p in paths),
            "--no-header",
            f"--basetemp={basetemp}",
        ]
        code, output = _run(cmd, timeout=3600)
        if code is None:
            battery.add(
                Probe(
                    check_id,
                    "C7",
                    label,
                    BLOCKED,
                    ("E4",),
                    " ".join(paths),
                    f"未执行完成：{output.splitlines()[0] if output else '未知原因'}",
                )
            )
            continue
        counts = _parse_pytest_counts(output)
        passed = counts.get("passed", 0)
        failed = counts.get("failed", 0) + counts.get("error", 0)
        battery.metric(f"{check_id}.passed", str(passed), check_id)
        battery.metric(f"{check_id}.failed", str(failed), check_id)

        # 宿主安全钩子会拦下 pytest 的批量临时目录清理，并把提示写进输出、
        # 把一个本来全绿的会话变成非零退出。这不是代码失败，而是证据环境不可用：
        # 按规范 §1.1 只能标 V-BLOCKED，标 V-FAILED 等于把环境问题算到代码头上。
        if _SAFE_DELETE_MARKER in output and failed == 0:
            state = BLOCKED
            limitation = (
                "宿主安全钩子拦截 pytest 临时目录清理，退出码与统计摘要均不可信；"
                "需在非拦截环境复跑"
            )
        else:
            state, limitation = _pytest_state(code, counts)
        if check_id == "C7.3":
            limitation = (
                "只覆盖主链的已实现段；剧本→拆镜→ShotSpec→ConstraintPack 这一段"
                "由逐域用例证明，端到端链路尚未串起"
            )
        battery.add(
            Probe(
                check_id,
                "C7",
                label,
                state,
                ("E4", "E5") if check_id != "C7.1" else ("E4",),
                f"pytest {' '.join(str(p.relative_to(ROOT)) for p in paths)}",
                limitation,
                detail=f"passed={passed} failed={failed}" + (
                    f"；{_expect_format_hint(output)}" if state == FAILED else ""
                ),
            )
        )


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _git_context() -> tuple[str, str]:
    ok, sha = _git("rev-parse", "--short", "HEAD")
    ok2, dirty = _git("status", "--porcelain")
    return (sha if ok else "UNKNOWN", "不洁净" if (ok2 and dirty) else "洁净")


def render(battery: Battery) -> str:
    sha, tree = _git_context()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        "# 核验电池（时点证据）",
        "",
        f"> 生成时间：{now}（UTC）　提交：`{sha}`　工作树：{tree}",
        "> 本文件由 `scripts/verification_battery.py` 生成，**禁止手工编辑**。",
        "> 本文件只陈述事实与证据等级，不含合规判断；合规结论见同日审计报告。",
        "",
        "## 1. 核验总表",
        "",
        "| 编号 | 类别 | 核验对象 | 核验状态 | 证据等级 | 证据位置 | 未完成事项 |",
        "|---|---|---|---|---|---|---|",
    ]
    order = {key: index for index, key in enumerate(CATEGORIES)}
    for probe in sorted(battery.probes, key=lambda p: (order.get(p.category, 99), p.check_id)):
        evidence = "+".join(probe.evidence)
        lines.append(
            f"| {probe.check_id} | {probe.category} {CATEGORIES.get(probe.category, '?')} | "
            f"{probe.obj} | `[{probe.state}]` | `[{evidence}]` | {probe.location} | "
            f"{probe.limitation} |"
        )

    lines += ["", "## 2. 逐条证据", ""]
    for probe in battery.probes:
        evidence = "+".join(probe.evidence)
        lines.append(
            f"- `[{probe.state}][{evidence}] {probe.check_id} {probe.obj}："
            f"证据：{probe.location}；限制：{probe.limitation}`"
        )
        if probe.detail:
            lines.append(f"  - 详情：{probe.detail}")

    lines += ["", "## 3. 关键指标（从闸门输出解析，未经人工转录）", ""]
    if battery.metrics:
        lines += ["| 指标 | 值 | 来源 |", "|---|---|---|"]
        for key in sorted(battery.metrics):
            lines.append(f"| `{key}` | {battery.metrics[key]} | {battery.metric_sources[key]} |")
    else:
        lines.append("（本次未解析到任何指标）")

    lines += ["", "## 4. 类别覆盖完备性", ""]
    counts: dict[str, int] = {}
    for probe in battery.probes:
        counts[probe.category] = counts.get(probe.category, 0) + 1
    for key, name in CATEGORIES.items():
        lines.append(f"- {key} {name}：{counts.get(key, 0)} 条探针")
    gaps = battery.coverage_gaps()
    lines.append("")
    lines.append("- 完备性判定：" + ("PASS（七类均有探针，无未登记类别）" if not gaps else "FAIL"))
    for gap in gaps:
        lines.append(f"  - {gap}")
    lines.append("")
    return "\n".join(lines)


def summarize(battery: Battery) -> str:
    rows: list[str] = []
    for state in (VERIFIED, PARTIAL, UNVERIFIED, FAILED, BLOCKED, NA):
        count = sum(1 for p in battery.probes if p.state == state)
        if count:
            rows.append(f"{state}={count}")
    gaps = battery.coverage_gaps()
    return (
        f"{len(battery.probes)} 条探针（{' / '.join(rows)}）"
        f"　完备性={'PASS' if not gaps else 'FAIL'}" + ("\n" + "\n".join(gaps) if gaps else "")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="执行核验电池并输出时点证据（只陈述事实）")
    parser.add_argument("--skip-tests", action="store_true", help="跳过 pytest（分钟级）")
    parser.add_argument("--out", default=None, help="输出路径；'-' 表示标准输出")
    args = parser.parse_args()

    battery = Battery()
    probe_baseline_document(battery)
    probe_manifest_sidecar(battery)
    probe_amendments_append_only(battery)
    probe_gate_commands(battery)
    probe_freeze_reported_amendments(battery)
    probe_migrations(battery)
    probe_tests(battery, skip=args.skip_tests)

    report = render(battery)
    if args.out == "-":
        print(report)
    else:
        sha, _ = _git_context()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        out = Path(args.out) if args.out else (
            ROOT / "docs" / "evidence" / "verification" / f"{today}-{sha}.md"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        # 显式 LF：Windows 上 write_text 默认换行转换会把证据文件翻成 CRLF，
        # 每次重跑都制造整文件假 diff（本仓已因同类问题翻车过一次）。
        with out.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(report)
        print(f"已写出：{_rel(out)}")

    print(summarize(battery))
    # 退出码：仅当存在 FAILED 或完备性不成立时非零。PARTIAL/BLOCKED 是事实，不是失败。
    hard_fail = any(p.state == FAILED for p in battery.probes) or bool(battery.coverage_gaps())
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
