"""分层闸门（scripts/architecture_check.py）的测试。

分两组，缺一不可：
1. **仓库现状**：真仓库必须 0 违规——防止闸门上线即假绿或长期红着没人管；
2. **闸门自身**：用合成目录树逐条证明它能抓住它声称能抓的东西
   （反向依赖、惰性反向依赖、TYPE_CHECKING 反向依赖、服务间直连、
   导入期循环、未登记的新层）。上一轮的教训很清楚：闸门没有负向测试，
   就等于「删两行条目也能绿」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts 不是包，按模块加载（与 tests/unit/test_contract_check.py 同口径）。
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import architecture_check as arch  # noqa: E402


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 仓库现状
# --------------------------------------------------------------------------- #
def test_repository_has_no_layering_violation() -> None:
    """当前仓库必须 0 违规（R0/R1/R2/R3/R4 全部）。"""
    results = arch.run_all()
    flattened = {rule: [v.message for v in violations] for rule, violations in results.items()}
    assert flattened == {
        "R0 覆盖面": [],
        "R1 层间依赖方向": [],
        "R2 服务间直连": [],
        "R3 导入期循环": [],
        "R4 数据库身份旁路": [],
    }


def test_main_exit_code_is_zero_on_clean_repo() -> None:
    assert arch.main() == 0


def test_every_layer_has_a_rule() -> None:
    """LAYERS 与 LAYER_RULES 必须一一对应，漏一层就等于放过一层。"""
    assert {rule.layer for rule in arch.LAYER_RULES} == set(arch.LAYERS)


def test_platform_dependency_is_still_needed() -> None:
    """平台能力白名单里的条目必须真的还被引用（否则就是陈旧白名单）。"""
    assert arch.check_stale_platform_dependencies(arch.collect_imports()) == []


# --------------------------------------------------------------------------- #
# R1 层间依赖方向
# --------------------------------------------------------------------------- #
def test_detects_reverse_layer_import(tmp_path: Path) -> None:
    _write(tmp_path, "packages/lib.py", "import services.thing\n")
    _write(tmp_path, "services/thing.py", "VALUE = 1\n")
    violations = arch.check_layer_direction(arch.collect_imports(tmp_path))
    assert len(violations) == 1
    assert "packages 不得依赖 services" in violations[0].message
    assert violations[0].file == "packages/lib.py"
    assert violations[0].line == 1


def test_lazy_import_still_counts_as_direction_violation(tmp_path: Path) -> None:
    """把反向依赖藏进函数体也是依赖——人工扫描最常漏的就是这种。"""
    _write(
        tmp_path,
        "packages/lib.py",
        "def helper():\n    import workers.relay.consumer  # noqa: F401\n",
    )
    _write(tmp_path, "workers/relay/consumer.py", "VALUE = 1\n")
    violations = arch.check_layer_direction(arch.collect_imports(tmp_path))
    assert [v.message.split("（")[0] for v in violations] == [
        "packages 不得依赖 workers"
    ]


def test_type_checking_import_counts_as_direction_violation(tmp_path: Path) -> None:
    """类型引用同样是耦合：mypy 能过不代表依赖方向成立。"""
    _write(
        tmp_path,
        "packages/lib.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from services.thing import Model\n",
    )
    _write(tmp_path, "services/thing.py", "class Model: ...\n")
    assert len(arch.check_layer_direction(arch.collect_imports(tmp_path))) == 1


def test_apps_may_not_reach_workers_or_adapters(tmp_path: Path) -> None:
    _write(tmp_path, "apps/api/main.py", "import workers.relay.webhook\n")
    _write(tmp_path, "workers/relay/webhook.py", "X = 1\n")
    violations = arch.check_layer_direction(arch.collect_imports(tmp_path))
    assert "apps 不得依赖 workers" in violations[0].message


def test_allowed_downward_import_is_clean(tmp_path: Path) -> None:
    """合法方向不许误报：apps→services、services→packages、workers→packages。"""
    _write(tmp_path, "apps/api/main.py", "import services.thing\n")
    _write(tmp_path, "services/thing.py", "import packages.lib\n")
    _write(tmp_path, "packages/lib.py", "import json\n")
    _write(tmp_path, "workers/relay/consumer.py", "import packages.lib\n")
    assert arch.check_layer_direction(arch.collect_imports(tmp_path)) == []


def test_dynamic_import_is_detected_for_direction(tmp_path: Path) -> None:
    """`importlib.import_module("...")` 也是依赖：只扫 ast.Import 会留下后门。"""
    _write(
        tmp_path,
        "packages/lib.py",
        "import importlib\nimportlib.import_module('workers.relay.consumer')\n",
    )
    _write(tmp_path, "workers/relay/consumer.py", "VALUE = 1\n")
    violations = arch.check_layer_direction(arch.collect_imports(tmp_path))
    assert [v.message.split("（")[0] for v in violations] == ["packages 不得依赖 workers"]


def test_module_level_dynamic_import_counts_for_cycle(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "packages/a.py",
        "import importlib\nimportlib.import_module('packages.b')\n",
    )
    _write(tmp_path, "packages/b.py", "import packages.a\n")
    violations = arch.check_import_cycles(tmp_path, arch.collect_imports(tmp_path))
    assert len(violations) == 1


def test_non_literal_dynamic_import_is_not_guessed(tmp_path: Path) -> None:
    """变量拼接的动态导入无法静态判定——不猜，交给 review（口径写在 docstring 里）。"""
    _write(
        tmp_path,
        "packages/lib.py",
        "import importlib\nname = 'services.thing'\nimportlib.import_module(name)\n",
    )
    _write(tmp_path, "services/thing.py", "VALUE = 1\n")
    assert arch.check_layer_direction(arch.collect_imports(tmp_path)) == []


# --------------------------------------------------------------------------- #
# R2 服务间直连
# --------------------------------------------------------------------------- #
def test_detects_cross_service_import(tmp_path: Path) -> None:
    _write(tmp_path, "services/asset_service/api.py", "from services.qc_governance.rules import R\n")
    _write(tmp_path, "services/qc_governance/rules.py", "R = 1\n")
    violations = arch.check_cross_service(arch.collect_imports(tmp_path))
    assert len(violations) == 1
    assert "services.asset_service 不得直接 import services.qc_governance" in (
        violations[0].message
    )


def test_platform_capability_dependency_is_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "services/asset_service/api.py",
        "from services.identity_access.auth_dependency import require_role\n",
    )
    _write(tmp_path, "services/identity_access/auth_dependency.py", "require_role = None\n")
    imports = arch.collect_imports(tmp_path)
    assert arch.check_cross_service(imports) == []
    assert arch.check_stale_platform_dependencies(imports) == []


def test_stale_platform_dependency_is_reported() -> None:
    """白名单条目没人引用 → 报陈旧（用空图模拟「引用没了」）。"""
    violations = arch.check_stale_platform_dependencies([])
    assert [v.rule for v in violations] == ["R2 服务间直连"] * len(
        arch.PLATFORM_SERVICE_DEPENDENCIES
    )
    assert all("陈旧白名单" in v.message for v in violations)


# --------------------------------------------------------------------------- #
# R3 导入期循环
# --------------------------------------------------------------------------- #
def test_detects_import_time_cycle(tmp_path: Path) -> None:
    _write(tmp_path, "packages/a.py", "import packages.b\n")
    _write(tmp_path, "packages/b.py", "import packages.a\n")
    violations = arch.check_import_cycles(tmp_path, arch.collect_imports(tmp_path))
    assert len(violations) == 1
    assert "packages.a" in violations[0].message and "packages.b" in violations[0].message


def test_lazy_import_does_not_form_import_time_cycle(tmp_path: Path) -> None:
    """惰性导入是本仓库既定的解法，不算导入期循环（但 R1 仍照抓方向）。"""
    _write(tmp_path, "packages/a.py", "def f():\n    import packages.b  # noqa: F401\n")
    _write(tmp_path, "packages/b.py", "import packages.a\n")
    imports = arch.collect_imports(tmp_path)
    assert arch.check_import_cycles(tmp_path, imports) == []


def test_submodule_import_pulls_in_parent_package(tmp_path: Path) -> None:
    """import 子模块会执行父包 ``__init__``：父包与子模块互相引用应判为循环。"""
    _write(tmp_path, "packages/pkg/__init__.py", "from packages.pkg.core import CORE\n")
    _write(tmp_path, "packages/pkg/core.py", "from packages.pkg import __all__ as _a\n")
    violations = arch.check_import_cycles(tmp_path, arch.collect_imports(tmp_path))
    assert len(violations) == 1


# --------------------------------------------------------------------------- #
# R0 覆盖面
# --------------------------------------------------------------------------- #
def test_unregistered_top_level_code_dir_is_reported(tmp_path: Path) -> None:
    """新增一层却没登记规则 → 报红（否则新层天然免疫 R1-R3）。"""
    _write(tmp_path, "plugins/pipeline.py", "X = 1\n")
    violations = arch.check_layer_coverage(tmp_path)
    assert any("plugins" in v.message for v in violations)


def test_registered_layers_are_not_reported(tmp_path: Path) -> None:
    _write(tmp_path, "packages/lib.py", "X = 1\n")
    assert arch.check_layer_coverage(tmp_path) == []


def test_non_code_dir_is_ignored(tmp_path: Path) -> None:
    """docs/scripts 之类不参与分层判定。"""
    _write(tmp_path, "docs/readme.md", "# 文档\n")
    assert arch.check_layer_coverage(tmp_path) == []


def test_syntax_error_is_reported_as_value_error(tmp_path: Path) -> None:
    _write(tmp_path, "packages/broken.py", "def f(:\n")
    with pytest.raises(ValueError) as exc_info:
        arch.collect_imports(tmp_path)
    assert "packages/broken.py" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# R4 数据库身份旁路
# --------------------------------------------------------------------------- #
def test_business_module_building_its_own_pool_is_reported(tmp_path: Path) -> None:
    """业务模块自建连接池 → 报红（否则它可以挑一个权限更大的角色连库）。"""
    _write(
        tmp_path,
        "services/asset_service/repository/repo.py",
        "from packages.shared.database import create_engine\n",
    )
    violations = arch.check_database_identity_bypass(tmp_path)
    assert any("create_engine" in v.message for v in violations), violations


def test_business_module_reaching_state_pools_is_reported(tmp_path: Path) -> None:
    """业务模块从 app.state 挑池 → 报红（等于绕开依赖注入的域绑定）。"""
    _write(
        tmp_path,
        "services/project_planning/api/routes.py",
        "def f(request):\n    return request.app.state.pools\n",
    )
    violations = arch.check_database_identity_bypass(tmp_path)
    assert any("app.state.pools" in v.message for v in violations), violations


def test_getattr_form_is_also_reported(tmp_path: Path) -> None:
    """getattr 写法与属性访问等价，不能成为绕过闸门的语法技巧。"""
    _write(
        tmp_path,
        "apps/api/routes.py",
        'def f(request):\n    return getattr(request.app.state, "pools", None)\n',
    )
    violations = arch.check_database_identity_bypass(tmp_path)
    assert any("getattr" in v.message for v in violations), violations


def test_allowlisted_assembly_point_is_clean(tmp_path: Path) -> None:
    """登记在案的装配点不该报红（白名单按模块名精确匹配）。

    临时仓库里其余白名单条目必然"无引用"，会触发陈旧登记——那是另一条规则
    的正确行为，因此这里只断言"没有针对该文件本身的违规"。
    """
    _write(
        tmp_path,
        "packages/shared/unit_of_work.py",
        "from packages.shared.database import DomainPools\n",
    )
    violations = arch.check_database_identity_bypass(tmp_path)
    assert not [v for v in violations if v.file.endswith("unit_of_work.py")], violations


def test_missing_allowlist_entry_is_reported_as_stale(tmp_path: Path) -> None:
    """白名单条目若已无任何引用 → 报陈旧登记（白名单不许常驻）。"""
    violations = arch.check_database_identity_bypass(tmp_path)
    stale = [v for v in violations if "陈旧登记" in v.message]
    assert stale, "空仓库下所有白名单条目都是陈旧的，必须报出来"


def test_repository_database_bypass_is_clean() -> None:
    """当前仓库的装配点没有旁路（R4 现状 0 违规）。"""
    assert arch.check_database_identity_bypass() == []
