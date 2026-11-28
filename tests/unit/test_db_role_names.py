"""数据库角色名的单一来源（CRED-3）：代码不得写死角色名。

为什么需要这组测试（根因）
------------------------------------------------------------
角色名是配置（``POSTGRES_APP_USER`` / ``POSTGRES_RELAY_USER`` /
``POSTGRES_PROJECT_PLANNING_USER`` / ``POSTGRES_ASSET_SERVICE_USER``），
但在 codex 评审 T5 之前，12 处迁移与 DDL 构造器把它写成了字面量
（``APP_ROLE = "app_role"``、``role: str = APP_ROLE``）。

后果不是风格问题，而是**两种都不会被读代码发现的失效**：

1. 部署方把角色名改成 ``afs_app`` 后，迁移仍 ``CREATE ROLE app_role`` 并
   ``GRANT TO app_role``——建出一个没人用的角色，业务进程连库时权限全无；
2. 或 ``app_role`` 在该环境不存在，迁移 GRANT 失败，升级路径直接堵死。

这类缺陷的共同形态是「代码字符串里出现了默认角色名」，所以判据按形态来定，
而不是按「有没有调用某个函数」来定——后者只要换一种写法就能绕过。

判定逻辑在 ``scripts/boundary_assertions.py`` 里只有一份实现
（``evaluate_role_name_literals``）。本文件既喂真实仓库（必须零违规），
也喂构造目录（规则必须真的会拒绝），与 ``test_credential_surface.py`` 同构。
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts 不是包，按模块加载（与 test_contract_check.py 一致）。
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import boundary_assertions as ba  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 真实仓库：必须零违规（否则闸门与测试一起红）
# --------------------------------------------------------------------------- #
def test_repository_has_no_hardcoded_role_names() -> None:
    """全仓（迁移 + 共享 DDL 构造器 + 服务）不得出现默认角色名字面量。"""
    problems = ba.evaluate_role_name_literals(REPO_ROOT)
    assert problems == [], "存在写死的默认角色名：" + "；".join(problems)


def test_config_keeps_defaults_where_they_belong() -> None:
    """默认角色名只允许出现在 config.py（默认值的唯一定义处）。

    这条不是同义反复：它把「允许的位置」钉在证据上——如果有人把默认值搬到
    别处（例如新建一个 constants 模块），上面那条测试会红，而这条说明白
    「该放哪里」。
    """
    config = (REPO_ROOT / "packages/shared/config.py").read_text(encoding="utf-8")
    for name in ba._DEFAULT_ROLE_NAMES:
        assert f'"{name}"' in config, f"config.py 未定义默认角色名 {name}"


# --------------------------------------------------------------------------- #
# 构造目录：规则必须真的会拒绝（否则「零违规」只是碰巧）
# --------------------------------------------------------------------------- #
def test_literal_role_name_in_migration_is_rejected(tmp_path: Path) -> None:
    """迁移里写死角色名必须被判违规（T5 的原始形态）。"""
    _write(
        tmp_path,
        "data/migrations/versions/x1_create_role.py",
        'APP_ROLE = "app_role"\n\n\ndef upgrade() -> None:\n'
        '    op.execute(f"GRANT SELECT ON t TO {APP_ROLE}")\n',
    )
    problems = ba.evaluate_role_name_literals(tmp_path)
    assert problems, "写死角色名的迁移没有被判违规"
    assert "app_role" in problems[0]


def test_literal_role_name_in_sql_string_is_rejected(tmp_path: Path) -> None:
    """直接写进 SQL 字符串（不经常量）同样必须被判违规。"""
    _write(
        tmp_path,
        "packages/shared/ddl.py",
        'GRANT_SQL = "GRANT SELECT ON t TO asset_service_role"\n',
    )
    problems = ba.evaluate_role_name_literals(tmp_path)
    assert problems, "SQL 字符串里的角色名没有被判违规"


def test_role_name_in_docstring_is_allowed(tmp_path: Path) -> None:
    """文档字符串可以直呼角色名：讲清「谁是谁」必须能写名字。

    这条防的是判据过宽——把注释/文档也算违规，会逼人写「某角色」这种
    看不懂的说明，最后大家都去关闸门。
    """
    _write(
        tmp_path,
        "packages/shared/doc.py",
        '"""本模块说明 project_planning_role 与 asset_service_role 的分工。"""\n\nVALUE = 1\n',
    )
    assert ba.evaluate_role_name_literals(tmp_path) == []


def test_reader_function_name_is_not_a_role_name(tmp_path: Path) -> None:
    """``"app_role_name"`` 不是角色名：按标识符边界匹配，不误伤读取函数。"""
    _write(
        tmp_path,
        "packages/shared/roles.py",
        '__all__ = ["app_role_name"]\n',
    )
    assert ba.evaluate_role_name_literals(tmp_path) == []


def test_empty_tree_is_rejected_instead_of_silently_passing(tmp_path: Path) -> None:
    """目录为空必须报红：扫不到代码的断言等于没有断言。"""
    problems = ba.evaluate_role_name_literals(tmp_path)
    assert problems and "未扫描到任何源码" in problems[0]
