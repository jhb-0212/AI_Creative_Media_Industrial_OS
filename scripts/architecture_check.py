"""分层依赖闸门（铁律 1「不屎山框架」的 CI 化）。

为什么需要它：分层规则写在 `docs/architecture/repository-structure.md` §3 已经很久了，
但它一直是**人工约定**——审查报告连两轮都写着「AST 扫描 0 反向依赖；仍无
tests/architecture / import-linter」。人工扫描的问题是：① 只有想起来才扫；
② 扫完的结论过期无人知道；③ 新人加一条反向 import，CI 依然全绿。
本脚本把那句话变成可执行、可复现、有负向测试的闸门。

三条规则 + 一条防呆（每条都写明**作用范围**，因为范围划错会让闸门要么形同虚设、
要么天天误报）：

R1 层间依赖方向 —— 覆盖**所有** import（含函数体内惰性导入、`TYPE_CHECKING` 分支）。
   理由：依赖是结构事实，写在函数里也还是依赖；「先反向依赖再藏进函数」正是
   人工扫描最容易漏的形态。
   规则表见 `LAYER_RULES`；`apps → services → adapters`、`*/workers/agents → packages`。

R2 服务间直连 —— 覆盖所有 import。领域模块之间只允许经 API/命令 + Outbox 事件交互
   （14.2 数据所有权），禁止直接 import 对方实现。
   唯一允许的例外是**平台能力**（鉴权依赖 `services.identity_access`），且必须登记在
   `PLATFORM_SERVICE_DEPENDENCIES` 里并写清理由；条目一旦没人引用就报「陈旧登记」，
   不允许把白名单变成永久后门。

R3 模块导入期循环 —— **只看模块级 import**。理由：惰性导入（函数体内 import）是本仓库
   既有的、有意的解法（`packages/shared/logging.py` 为了打通 trace_id 就这么做），
   它不会造成导入期死锁，因此不计入循环；但若两模块在**导入期**互相依赖，
   那就是真问题（import 顺序敏感、可能直接 ImportError），必须拦住。

R0 覆盖面防呆 —— 仓库顶层出现新的代码目录（比如将来加 `plugins/`）时，必须在本文件的
   `LAYERS` / `LAYER_RULES` 里登记，否则闸门报红。否则新增一层会天然免疫所有规则。

用法：``python scripts/architecture_check.py``；CI 在 contract-check job 里跑一次，
单元测试见 ``tests/architecture/test_architecture.py``（闸门自身也有测试）。
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 六个代码层。顺序不表达依赖，只用于输出稳定；依赖由 LAYER_RULES 表达。
LAYERS: tuple[str, ...] = (
    "apps",
    "services",
    "packages",
    "workers",
    "adapters",
    "agents",
)

# 非代码层目录：测试、脚本、契约、基础设施等，不参与分层判定。
NON_LAYER_DIRS = frozenset(
    {"tests", "scripts", "contracts", "docs", "data", "infra", "alembic"}
)


@dataclass(frozen=True)
class LayerRule:
    """一条「某层不许依赖哪些层」的规则（带理由，理由不是装饰）。"""

    layer: str
    forbidden: frozenset[str]
    reason: str


LAYER_RULES: tuple[LayerRule, ...] = (
    LayerRule(
        layer="packages",
        forbidden=frozenset({"apps", "services", "workers", "adapters", "agents"}),
        reason="packages 是纯横切库（配置/日志/鉴权/RBAC），只允许依赖自身；"
        "反向依赖会让它变成隐形的业务模块，谁都能改、谁都不敢动",
    ),
    LayerRule(
        layer="adapters",
        forbidden=frozenset({"apps", "services", "workers", "agents"}),
        reason="adapters 是外部系统适配的最底层，只允许依赖 packages；"
        "它若反向依赖 services，外部实现就会绑死领域模型",
    ),
    LayerRule(
        layer="agents",
        forbidden=frozenset({"apps", "services", "workers", "adapters"}),
        reason="agents 是 Skills 契约执行层（sandbox 语义），只允许依赖 packages；"
        "直接调服务实现会绕过编排与授权",
    ),
    LayerRule(
        layer="services",
        forbidden=frozenset({"apps", "workers", "agents"}),
        reason="services 是内核，只允许向下依赖 packages/adapters；"
        "依赖 apps 或 workers 意味着内核被入口/执行细节污染",
    ),
    LayerRule(
        layer="workers",
        forbidden=frozenset({"apps", "agents"}),
        reason="workers 是独立进程，只允许依赖 packages/services/adapters；"
        "依赖入口层会形成进程级环（API 与 worker 互引）",
    ),
    LayerRule(
        layer="apps",
        forbidden=frozenset({"workers", "agents", "adapters"}),
        reason="apps 是薄装配层：只做协议适配，业务经 services；"
        "直连 workers/adapters 属跨层直连（装配应经 services 暴露）",
    ),
)

# 服务间允许的「平台能力」依赖（R2 的唯一例外）。条目必须有理由；
# 若某条目不再被任何模块引用，闸门报「陈旧登记」，必须删掉——白名单不许常驻。
PLATFORM_SERVICE_DEPENDENCIES: dict[str, str] = {
    "services.identity_access": (
        "鉴权平台能力（require_role 依赖）：每个服务的路由都要挂授权，不属于领域数据耦合。"
        "所有服务都自己实现一遍鉴权依赖才是真正的重复；若将来下沉到 packages/identity，"
        "本条目必须删除"
    ),
}


@dataclass(frozen=True)
class ImportRecord:
    """一条 import 记录（相对导入已归一化为绝对模块名）。"""

    source_file: str  # 相对仓库根的 posix 路径（报错用）
    source_module: str  # 点分模块名
    source_layer: str
    target_module: str  # 已解析的点分模块名（可能是外部库）
    line: int
    module_level: bool  # 是否在模块导入期执行（函数体内 import 为 False）


@dataclass(frozen=True)
class Violation:
    """一条违规。rule 用于分组输出，message 用于直接看懂「错在哪、为什么」。"""

    rule: str
    file: str
    line: int
    message: str


# --------------------------------------------------------------------------- #
# 解析：把源码变成 ImportRecord 列表
# --------------------------------------------------------------------------- #
def _iter_py_files(root: Path) -> Iterator[Path]:
    """遍历六个层目录下的全部 .py（跳过 __pycache__）。"""
    for layer in LAYERS:
        base = root / layer
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _module_name(path: Path, root: Path) -> str:
    """文件 → 点分模块名（``a/b/__init__.py`` → ``a.b``）。"""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_parts(path: Path, root: Path) -> list[str]:
    """文件所属包的分段（用于解析相对导入）。"""
    parts = list(path.relative_to(root).parent.parts)
    return parts


def _resolve_target(node: ast.Import | ast.ImportFrom, path: Path, root: Path) -> list[str]:
    """把一条 import 语句解析成绝对模块名列表（相对导入按所在包展开）。"""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    if not node.level:
        return [module] if module else []

    package = _package_parts(path, root)
    # level=1 表示「当前包」；level=2 上跳一层，以此类推。
    keep = len(package) - (node.level - 1)
    base = package[:keep]
    if module:
        base = [*base, module]
    return [".".join(base)] if base else []


def _is_type_checking(test: ast.expr) -> bool:
    """``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` 判定（导入期不执行）。"""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _dynamic_import_target(node: ast.AST) -> str | None:
    """识别 ``importlib.import_module("x")`` / ``__import__("x")`` 的字面量目标。

    为什么必须识别：动态导入是绕过静态扫描最省事的办法——`import_module` 一写，
    `ast.Import` 里什么都没有，闸门就瞎了（本仓库 scripts/ 里就有这种写法，
    属于「读者看得懂、闸门看不见」的典型）。只取字符串字面量：变量拼接的导入
    无法静态判定，那属于刻意的规避行为，由 review 兜底。
    """
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in {"import_module", "__import__"}:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _module_level_nodes(tree: ast.Module) -> set[int]:
    """收集「模块导入期会执行」的 import 节点 id。

    展开模块级的 if/try/with/for/while（这些在导入期就会跑），跳过
    ``if TYPE_CHECKING:``（只在类型检查器里存在）。函数/类体内的 import 不在此列。
    模块级的动态导入（``importlib.import_module("x")`` 作为独立语句）同样计入。
    """
    found: set[int] = set()

    def visit(statements: Iterable[ast.stmt]) -> None:
        for stmt in statements:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                found.add(id(stmt))
            elif isinstance(stmt, ast.Expr) and _dynamic_import_target(stmt.value):
                found.add(id(stmt.value))
            elif isinstance(stmt, ast.If):
                if _is_type_checking(stmt.test):
                    continue
                visit(stmt.body)
                visit(stmt.orelse)
            elif isinstance(stmt, ast.Try) or (
                hasattr(ast, "TryStar") and isinstance(stmt, ast.TryStar)
            ):
                visit(stmt.body)
                for handler in stmt.handlers:
                    visit(handler.body)
                visit(stmt.orelse)
                visit(stmt.finalbody)
            elif isinstance(stmt, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
                visit(stmt.body)
                visit(getattr(stmt, "orelse", []))

    visit(tree.body)
    return found


def collect_imports(root: Path = ROOT) -> list[ImportRecord]:
    """扫描全部层目录，产出 ImportRecord 列表（含惰性导入与动态导入，供 R1/R2 使用）。"""
    records: list[ImportRecord] = []
    for path in _iter_py_files(root):
        source_module = _module_name(path, root)
        source_layer = source_module.split(".")[0]
        source_file = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # 语法错误由 ruff/mypy 管，这里不重复报
            raise ValueError(f"{source_file} 语法错误，无法做分层检查：{exc}") from exc
        module_level = _module_level_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                targets = _resolve_target(node, path, root)
            else:
                dynamic = _dynamic_import_target(node)
                targets = [dynamic] if dynamic else []
                if not targets:
                    continue
            for target in targets:
                if not target:
                    continue
                records.append(
                    ImportRecord(
                        source_file=source_file,
                        source_module=source_module,
                        source_layer=source_layer,
                        target_module=target,
                        line=node.lineno,
                        module_level=id(node) in module_level,
                    )
                )
    return records


def _layer_of(module: str) -> str | None:
    head = module.split(".")[0]
    return head if head in LAYERS else None


def _module_of(module: str, depth: int = 2) -> str:
    """取前 ``depth`` 段：``services.asset_service.api.x`` → ``services.asset_service``。"""
    return ".".join(module.split(".")[:depth])


# --------------------------------------------------------------------------- #
# R1 层间依赖方向
# --------------------------------------------------------------------------- #
def check_layer_direction(imports: list[ImportRecord]) -> list[Violation]:
    """任一层出现被禁方向的 import 即违规（含惰性导入与 TYPE_CHECKING）。"""
    rules = {rule.layer: rule for rule in LAYER_RULES}
    violations: list[Violation] = []
    for record in imports:
        rule = rules.get(record.source_layer)
        if rule is None:
            continue
        target_layer = _layer_of(record.target_module)
        if target_layer is None or target_layer not in rule.forbidden:
            continue
        violations.append(
            Violation(
                rule="R1 层间依赖方向",
                file=record.source_file,
                line=record.line,
                message=(
                    f"{record.source_layer} 不得依赖 {target_layer}"
                    f"（import {record.target_module}）：{rule.reason}"
                ),
            )
        )
    return violations


# --------------------------------------------------------------------------- #
# R2 服务间直连
# --------------------------------------------------------------------------- #
def check_cross_service(imports: list[ImportRecord]) -> list[Violation]:
    """领域服务之间禁止互相 import（登记的平台能力除外）。"""
    violations: list[Violation] = []
    for record in imports:
        if record.source_layer != "services":
            continue
        if _layer_of(record.target_module) != "services":
            continue
        source = _module_of(record.source_module)
        target = _module_of(record.target_module)
        if source == target or target in PLATFORM_SERVICE_DEPENDENCIES:
            continue
        violations.append(
            Violation(
                rule="R2 服务间直连",
                file=record.source_file,
                line=record.line,
                message=(
                    f"{source} 不得直接 import {target}（{record.target_module}）："
                    "领域模块之间只经 API/命令 + Outbox 事件交互（14.2 数据所有权）；"
                    "确需平台能力请登记到 PLATFORM_SERVICE_DEPENDENCIES 并写明理由"
                ),
            )
        )
    return violations


def check_stale_platform_dependencies(imports: list[ImportRecord]) -> list[Violation]:
    """登记了却没人引用的平台能力 = 陈旧白名单，必须删条目。"""
    referenced = {
        _module_of(record.target_module)
        for record in imports
        if record.source_layer == "services" and _layer_of(record.target_module) == "services"
    }
    return [
        Violation(
            rule="R2 服务间直连",
            file="scripts/architecture_check.py",
            line=0,
            message=(
                f"PLATFORM_SERVICE_DEPENDENCIES 中的 {target!r} 已无任何服务引用"
                "——陈旧白名单必须删除，白名单不许常驻"
            ),
        )
        for target in sorted(PLATFORM_SERVICE_DEPENDENCIES)
        if target not in referenced
    ]


# --------------------------------------------------------------------------- #
# R3 模块导入期循环
# --------------------------------------------------------------------------- #
def _module_graph(root: Path, imports: list[ImportRecord]) -> dict[str, set[str]]:
    """模块级 import 构成的有向图（节点仅限仓库内模块）。

    目标模块若不存在，说明是外部库或未落地的包，忽略；存在的目标会同时连到其祖先包
    （import 子模块必然执行父包 ``__init__``，这是循环最常见的来源）。
    """
    known = {_module_name(path, root) for path in _iter_py_files(root)}
    graph: dict[str, set[str]] = {module: set() for module in known}
    for record in imports:
        if not record.module_level:
            continue
        parts = record.target_module.split(".")
        for depth in range(len(parts), 0, -1):
            candidate = ".".join(parts[:depth])
            if candidate in known and candidate != record.source_module:
                graph[record.source_module].add(candidate)
    return graph


def _strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan 强连通分量（迭代实现，避免深链递归溢出）。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for start in graph:
        if start in index:
            continue
        worklist: list[tuple[str, Iterator[str]]] = [(start, iter(sorted(graph[start])))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while worklist:
            node, children = worklist[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    worklist.append((child, iter(sorted(graph[child]))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            worklist.pop()
            if worklist:
                parent = worklist[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(sorted(component))
    return components


def check_import_cycles(root: Path = ROOT, imports: list[ImportRecord] | None = None) -> list[Violation]:
    """模块导入期存在环 → 违规（惰性导入不计，见模块 docstring 的 R3 说明）。"""
    records = imports if imports is not None else collect_imports(root)
    graph = _module_graph(root, records)
    return [
        Violation(
            rule="R3 导入期循环",
            # 循环是「模块群」的属性，不是某一行的问题，因此定位写模块名而非伪造路径。
            file=component[0],
            line=0,
            message=(
                "模块导入期循环依赖："
                + " → ".join([*component, component[0]])
                + "。导入顺序会变敏感、可能直接 ImportError；"
                "拆出叶子模块（如把共享常量/配置下沉）或改为事件解耦，不要靠 import 顺序救"
            ),
        )
        for component in _strongly_connected(graph)
    ]


# --------------------------------------------------------------------------- #
# R4 数据库身份旁路
# --------------------------------------------------------------------------- #
#: 直接构造连接池 / DSN 的入口名。业务代码只应经依赖注入拿到"自己域"的工作单元，
#: 一旦允许直接建池，就等于允许它挑一个权限更大的角色去连库。
_DB_FACTORY_NAMES: frozenset[str] = frozenset(
    {"DomainPools", "create_engine", "create_sessionmaker", "build_database_url"}
)

#: 允许直接接触连接池装配的模块（必须有理由；无命中即报「陈旧登记」）。
DATABASE_ACCESS_ALLOWLIST: dict[str, str] = {
    "packages.shared.database": (
        "连接池的定义与 lifespan 挂载点：DomainPools 在此创建并挂到 app.state"
    ),
    "packages.shared.unit_of_work": (
        "域感知工作单元依赖的构造点：把 domain 映射到连接池，是所有路由拿到 UoW 的唯一入口"
    ),
    "services.identity_access.deps": (
        "身份治理依赖装配点：集中导出 get_project_uow / get_asset_uow 与吊销检查取池，"
        "业务路由不得自建"
    ),
    "apps.api.main": (
        "就绪探针用身份域池探活（最小权限域），不承载任何业务读写"
    ),
    "workers.relay.__main__": (
        "Relay 进程入口：只起投递域连接池，与 API 进程身份不同"
    ),
}

#: 允许出现 ``app.state.pools`` 的模块（与上表分列：import 工厂 ≠ 访问运行时池）。
_STATE_POOLS_ALLOWLIST: frozenset[str] = frozenset(
    {
        "packages.shared.database",
        "packages.shared.unit_of_work",
        "services.identity_access.deps",
        "apps.api.main",
    }
)


def _database_access_hits(path: Path) -> list[tuple[int, str]]:
    """返回该文件里"直接取数据库身份"的 (行号, 说明) 列表。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "packages.shared.database":
            for alias in node.names:
                if alias.name in _DB_FACTORY_NAMES:
                    hits.append((node.lineno, f"直接 import {alias.name}"))
        if isinstance(node, ast.Attribute) and node.attr == "pools":
            parent = node.value
            if isinstance(parent, ast.Attribute) and parent.attr == "state":
                hits.append((node.lineno, "直接访问 app.state.pools"))
        # getattr(obj, "pools", ...) 与属性访问等价，必须同样计入——
        # 否则"换个写法"就能从闸门下走过，闸门只证明了字面形态。
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "pools"
        ):
            hits.append((node.lineno, '经 getattr 取 app.state.pools'))
    return hits


def check_database_identity_bypass(root: Path = ROOT) -> list[Violation]:
    """R4：业务模块不得自行挑选数据库连接身份。

    为什么这条必须是闸门而不是文档：按域拆角色之后，"跨模块写表被数据库拒绝"
    的前提是**每个模块只拿到自己域的池**。若某模块能自己 ``create_engine(...)``
    或从 ``app.state.pools`` 里挑一个池，权限边界就退化回"代码自觉"。
    """
    violations: list[Violation] = []
    used: set[str] = set()
    for path in _iter_py_files(root):
        module = _module_name(path, root)
        for line, detail in _database_access_hits(path):
            if module in DATABASE_ACCESS_ALLOWLIST or module in _STATE_POOLS_ALLOWLIST:
                used.add(module)
                continue
            violations.append(
                Violation(
                    rule="R4 数据库身份旁路",
                    file=path.relative_to(root).as_posix(),
                    line=line,
                    message=(
                        f"{detail}：业务模块只能经依赖注入拿自己域的工作单元。"
                        "自行建池/挑选池会让越权写入绕过数据库角色边界"
                    ),
                )
            )

    for module, reason in DATABASE_ACCESS_ALLOWLIST.items():
        if module not in used:
            violations.append(
                Violation(
                    rule="R4 数据库身份旁路",
                    file="scripts/architecture_check.py",
                    line=0,
                    message=(
                        f"陈旧登记：{module!r} 已不再直接接触连接池，请从 "
                        f"DATABASE_ACCESS_ALLOWLIST 删除（原理由：{reason}）"
                    ),
                )
            )
    return violations


# --------------------------------------------------------------------------- #
# R0 覆盖面防呆
# --------------------------------------------------------------------------- #
def check_layer_coverage(root: Path = ROOT) -> list[Violation]:
    """顶层代码目录必须在 LAYERS / LAYER_RULES 里登记，否则新层免疫所有规则。"""
    registered = {rule.layer for rule in LAYER_RULES}
    violations: list[Violation] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in NON_LAYER_DIRS or child.name in LAYERS:
            continue
        if not any(child.rglob("*.py")):
            continue
        violations.append(
            Violation(
                rule="R0 覆盖面",
                file=child.name,
                line=0,
                message=(
                    f"顶层目录 {child.name!r} 里有代码但未登记分层规则："
                    "请在 scripts/architecture_check.py 的 LAYERS / LAYER_RULES 补登记"
                    "（否则新层天然免疫 R1-R3）"
                ),
            )
        )
    missing_rules = sorted(set(LAYERS) - registered)
    violations.extend(
        Violation(
            rule="R0 覆盖面",
            file="scripts/architecture_check.py",
            line=0,
            message=f"LAYERS 中的 {layer!r} 没有对应的 LayerRule，规则表不完整",
        )
        for layer in missing_rules
    )
    return violations


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run_all(root: Path = ROOT) -> dict[str, list[Violation]]:
    """按规则分组跑完全部检查（供 main 与测试共用，返回结构化结果）。"""
    imports = collect_imports(root)
    return {
        "R0 覆盖面": check_layer_coverage(root),
        "R1 层间依赖方向": check_layer_direction(imports),
        "R2 服务间直连": check_cross_service(imports) + check_stale_platform_dependencies(imports),
        "R3 导入期循环": check_import_cycles(root, imports),
        "R4 数据库身份旁路": check_database_identity_bypass(root),
    }


def main() -> int:
    """CI 入口：全部规则无违规返回 0，否则打印每条违规并返回 1。"""
    try:
        results = run_all()
    except ValueError as exc:
        print(f"[FAIL] 分层检查无法进行: {exc}")
        return 1

    total = sum(len(violations) for violations in results.values())
    for rule, violations in results.items():
        if not violations:
            continue
        for violation in violations:
            location = f"{violation.file}:{violation.line}" if violation.line else violation.file
            print(f"[FAIL] {rule} | {location} | {violation.message}")
    if total:
        print(f"[FAIL] 分层依赖校验未通过：{total} 处违规")
        return 1

    print(
        f"[PASS] 分层依赖：{len(LAYER_RULES)} 层规则 / R1 方向 0 违规 / "
        f"R2 跨服务 0 违规（平台能力 {len(PLATFORM_SERVICE_DEPENDENCIES)} 项）/ "
        f"R3 导入期循环 0 处 / R4 数据库身份旁路 0 处"
        f"（装配点 {len(DATABASE_ACCESS_ALLOWLIST)} 个）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
