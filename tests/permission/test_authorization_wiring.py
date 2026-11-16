"""授权接线闸门（AST 静态检查）：确认"统一入口"没有旁路。

三条规则，针对的都是在真实评审里出现过的失守形态：

W1 **动作必须注册**：路由里写了 ``require_action("PROJ.CREATE")``（拼错）时，
   运行时会在第一个请求里 500，而 CI 全绿。这里在 CI 阶段就拦住。

W2 **审批类动作必须带审批对象**：职责分离在依赖阶段只能记为 deferred，
   真正执行靠业务层传 ``applicant_subject_id``。少传一个参数 = 静默跳过职责分离。
   所以对 ``authorize(...)`` 的每个调用点做静态检查：动作是审批类就必须有该参数。

W3 **不得自行比对角色**：``principal.roles & allowed`` 绕开决策层就等于绕开
   Agent/CRITICAL、组织、项目三类判定。除 ``packages/identity`` 自身外，
   任何模块出现 ``principal.roles`` / ``has_role`` 都报红。
"""

from __future__ import annotations

import ast
from pathlib import Path

from packages.identity.action_registry import all_action_specs

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED_DIRS = ("services", "apps", "workers")

#: 依赖工厂本身：动作名作为函数参数传入，此处不可能有字面量。
#: 豁免它不会削弱检查——真正的调用点（路由/服务）仍然必须写字面量。
_WIRING_ALLOWLIST = {"services/identity_access/deps.py"}

#: W2 的**逐调用点**豁免：键必须是 (文件相对路径, 动作键)，值必须写全三项。
#:
#: 为什么需要它：``decide()`` 对审批动作缺 applicant 时返回 ALLOW 并把
#: ``separation_of_duties`` 记进 ``deferred_checks``。W2 存在的意义正是禁止
#: 这种"永久 deferred"。但 Gate 链在冻结契约里**没有申请人载体**：没有
#: GATE.REQUEST 动作、GateDecision 没有 applicant 字段、也没有 Gate 实例提交
#: 记录。从请求体收一个字符串当申请人，等于把"谁提请的"交给调用方声明——
#: 那会制造假的审计事实，比显式登记缺口危险得多。
#:
#: 豁免不是免检：① 只按 (文件, 动作) 精确匹配，不按目录放行；② 必填理由与
#: 解除条件，缺一项即报红；③ ``test_w2_exemptions_are_used_and_reasoned``
#: 检查它不是陈旧条目；④ 补偿性控制在 Gate 的领域/数据库测试里被真实断言。
_W2_EXEMPTIONS: dict[tuple[str, str], dict[str, str]] = {
    (
        "services/qc_governance/api/gate_routes.py",
        "PRODUCTION.GATE.APPROVE",
    ): {
        "reason": (
            "冻结契约的 GateDecision 无申请人载体（无 GATE.REQUEST 动作、无 applicant "
            "字段、无 Gate 实例提交记录）；从请求体接收申请人会伪造审计事实"
        ),
        "unblocks_when": (
            "契约修订增加服务端来源的 Gate 实例提交人字段，或 qc_governance 落地 "
            "GateSubmission 表后由资源阶段提供可信 applicant_subject_id"
        ),
        "compensating_control": (
            "Agent 不得决策由三层执行：授权层 CRITICAL + 领域 __post_init__ + "
            "数据库 CHECK；decided_by 只能来自已验签 Principal，请求体不接受"
        ),
    },
}

_W2_EXEMPTION_FIELDS = ("reason", "unblocks_when", "compensating_control")


def _iter_py_files() -> list[Path]:
    files: list[Path] = []
    for dirname in _SCANNED_DIRS:
        files.extend(sorted((_REPO_ROOT / dirname).rglob("*.py")))
    return files


def _iter_calls(node: ast.AST) -> list[ast.Call]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Call)]


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_every_required_action_is_registered() -> None:
    """W1：``require_action("X")`` 的 X 必须存在于 Action Registry。"""
    registered = {spec.action_key for spec in all_action_specs()}
    unknown: list[str] = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _iter_calls(tree):
            if _call_name(call) != "require_action" or not call.args:
                continue
            arg = call.args[0]
            if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                unknown.append(
                    f"{path.relative_to(_REPO_ROOT)}:{call.lineno} 动作名不是字面量"
                    "（无法静态校验，请写常量字符串）"
                )
                continue
            if arg.value not in registered:
                unknown.append(
                    f"{path.relative_to(_REPO_ROOT)}:{call.lineno} 未注册动作 {arg.value}"
                )
    assert unknown == [], unknown


def _iter_authorize_calls() -> list[tuple[str, str | None, int, bool]]:
    """返回全部 ``authorize(...)`` 调用点的 (相对路径, 动作键, 行号, 是否传申请人)。

    动作键不是字面量时为 ``None`` ——调用方据此报红，而不是猜一个名字放行。
    """
    found: list[tuple[str, str | None, int, bool]] = []
    for path in _iter_py_files():
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if relative in _WIRING_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _iter_calls(tree):
            if _call_name(call) != "authorize" or len(call.args) < 2:
                continue
            arg = call.args[1]
            action = arg.value if isinstance(arg, ast.Constant) else None
            has_applicant = any(
                kw.arg == "applicant_subject_id" for kw in call.keywords
            )
            found.append(
                (
                    relative,
                    action if isinstance(action, str) else None,
                    call.lineno,
                    has_applicant,
                )
            )
    return found


def test_approval_actions_must_pass_applicant() -> None:
    """W2：针对审批类动作的 ``authorize(...)`` 必须传 ``applicant_subject_id``。"""
    approval_keys = {spec.action_key for spec in all_action_specs() if spec.is_approval}
    violations: list[str] = []
    for relative, action, lineno, has_applicant in _iter_authorize_calls():
        if action is None:
            violations.append(
                f"{relative}:{lineno} 动作名不是字面量（无法静态校验职责分离）"
            )
            continue
        if action not in approval_keys:
            continue
        if (relative, action) in _W2_EXEMPTIONS:
            continue
        if not has_applicant:
            violations.append(
                f"{relative}:{lineno} 审批动作 {action} 未传 applicant_subject_id"
                "（职责分离会被静默跳过）"
            )
    assert violations == [], violations


def test_w2_exemptions_are_used_and_reasoned() -> None:
    """W2 的豁免必须窄、必填理由、且真实指向现存调用点（不留历史垃圾）。"""
    violations: list[str] = []
    for key, detail in _W2_EXEMPTIONS.items():
        missing = [field for field in _W2_EXEMPTION_FIELDS if not detail.get(field)]
        if missing:
            violations.append(f"W2 豁免 {key} 缺字段: {missing}")
        if len(key) != 2 or not all(isinstance(part, str) and part for part in key):
            violations.append(f"W2 豁免键必须是 (文件, 动作): {key!r}")

    used = {(relative, action) for relative, action, _, _ in _iter_authorize_calls()}
    for key in _W2_EXEMPTIONS:
        if key not in used:
            violations.append(f"W2 豁免 {key} 没有对应的 authorize 调用点（陈旧债务）")
    assert violations == [], violations


def test_no_module_compares_roles_directly() -> None:
    """W3：除 ``packages/identity`` 外，任何模块不得自行比对角色。

    ``principal.roles & allowed_principals`` 是旧实现的唯一判定方式；它绕开
    Agent/CRITICAL、组织、项目三类判定。允许它存在等于允许第二套授权实现。
    """
    violations: list[str] = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr not in {"roles", "has_role", "has_any_role"}:
                continue
            # 只认 ``principal.<attr>``：变量名就是约定，其他对象上的 roles
            # 字段（如 ORM 行）不属于授权判定。
            if isinstance(node.value, ast.Name) and node.value.id == "principal":
                violations.append(
                    f"{path.relative_to(_REPO_ROOT)}:{node.lineno} 直接访问 "
                    f"principal.{node.attr}（请走 authorize/require_action）"
                )
    assert violations == [], violations


def test_authorize_is_only_imported_from_packages() -> None:
    """W3 的补充：授权判定只有一份实现，不得出现并行的本地副本。"""
    offenders: list[str] = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.endswith("authorization")
                and not node.module.startswith("packages.identity")
            ):
                offenders.append(
                    f"{path.relative_to(_REPO_ROOT)}:{node.lineno} 从非单一事实源 "
                    f"{node.module} 导入授权实现"
                )
    assert offenders == [], offenders
