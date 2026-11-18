"""授权断言 ``data.role_grants_least_privilege`` 的负向用例（证明它会报红）。

为什么断言也要有负向用例：只证明「真实仓库现在 PASS」无法区分两种情形——
断言判据真的成立，还是判据写得太松、怎么改都 PASS。本文件对每一类失效
各构造一份输入，断言它**恰好**报出对应的那条问题。

四类失效对应四类判据（见 ``evaluate_least_privilege`` 文档）：
1. ORM 有新表但矩阵没声明（新表静默无权限）；
2. 矩阵声明了不存在的表（陈旧声明，收敛迁移会失败）；
3. 收敛迁移的语句与矩阵不一致（手写 DDL / 矩阵改了迁移没跟上）；
4. 收敛迁移之后的迁移手写 GRANT（绕过矩阵的唯一入口）。
"""

from __future__ import annotations

import pytest

from packages.shared.db_grants import business_tables, converge_statements
from packages.shared.db_roles import domain_role
from scripts import boundary_assertions as ba

_PROJECT = "project_planning"
_ASSET = "asset_service"


def _converge() -> dict[str, list[str]]:
    from packages.shared.db_grants import DOMAINS

    return {domain: converge_statements(domain_role(domain), domain) for domain in DOMAINS}


def _rendered() -> list[str]:
    from packages.shared.db_grants import DOMAINS

    return [statement for domain in sorted(DOMAINS) for statement in _converge()[domain]]


def _business() -> dict[str, frozenset[tuple[str, str]]]:
    from packages.shared.db_grants import DOMAINS

    return {domain: business_tables(domain) for domain in DOMAINS}


def _orm() -> dict[str, frozenset[str]]:
    return {
        _PROJECT: frozenset({"projects", "episodes", "script_revisions", "organizations"}),
        _ASSET: frozenset({"asset_versions", "qc_reports"}),
    }


def _evaluate(**overrides: object) -> list[str]:
    """以合规样本为基准，按需替换某一维，返回问题列表。"""
    kwargs: dict[str, object] = {
        "matrix_problems": [],
        "business": _business(),
        "orm_tables": _orm(),
        "converge": _converge(),
        "rendered": _rendered(),
        "grant_forbidden_literals": {},
    }
    kwargs.update(overrides)
    return ba.evaluate_least_privilege(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 0. 合规样本必须零问题（否则下面的「报一条」没有基准）
# --------------------------------------------------------------------------- #
def test_compliant_sample_has_no_problems() -> None:
    assert _evaluate() == []


def test_matrix_problems_are_propagated() -> None:
    problems = _evaluate(matrix_problems=["矩阵说 DELETE 可以"])
    assert "矩阵说 DELETE 可以" in problems


# --------------------------------------------------------------------------- #
# 1. ORM 有新表，矩阵没声明
# --------------------------------------------------------------------------- #
def test_undeclared_orm_table_is_reported() -> None:
    orm = _orm()
    orm[_ASSET] = orm[_ASSET] | {"rights_manifests"}
    problems = _evaluate(orm_tables=orm)
    assert any("rights_manifests" in p and "未在授权矩阵声明" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 2. 矩阵声明了不存在的表（陈旧声明）
# --------------------------------------------------------------------------- #
def test_stale_matrix_table_is_reported() -> None:
    business = _business()
    business[_ASSET] = business[_ASSET] | {(_ASSET, "legacy_renders")}
    problems = _evaluate(business=business)
    assert any("legacy_renders" in p and "不存在" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 3. 迁移语句与矩阵不一致
# --------------------------------------------------------------------------- #
def test_extra_grant_in_migration_is_reported() -> None:
    rendered = [*_rendered(), "GRANT DELETE ON project_planning.projects TO project_planning_role"]
    problems = _evaluate(rendered=rendered)
    assert any("迁移多执行 1 条" in p for p in problems), problems


def test_missing_statement_in_migration_is_reported() -> None:
    rendered = _rendered()[1:]
    problems = _evaluate(rendered=rendered)
    assert any("迁移少执行 1 条" in p for p in problems), problems


def test_missing_default_privilege_revoke_is_reported() -> None:
    """少撤销一条默认权限也算不一致：那正是「未来新表自动带权限」的入口。"""
    rendered = [
        statement
        for statement in _rendered()
        if "REVOKE ALL ON SEQUENCES" not in statement
    ]
    problems = _evaluate(rendered=rendered)
    assert any("迁移少执行" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 4. 后续迁移手写 GRANT
# --------------------------------------------------------------------------- #
def test_handwritten_grant_in_later_migration_is_reported() -> None:
    problems = _evaluate(
        grant_forbidden_literals={
            "data/migrations/versions/zzz_later.py": [
                "GRANT SELECT, INSERT ON project_planning.projects TO role"
            ]
        }
    )
    assert any("手写 GRANT" in p for p in problems), problems


def test_default_privilege_grant_is_also_handwritten_grant() -> None:
    """``ALTER DEFAULT PRIVILEGES … GRANT`` 同样是手写授权，必须被拦。"""
    problems = _evaluate(
        grant_forbidden_literals={
            "data/migrations/versions/zzz_later.py": [
                "ALTER DEFAULT PRIVILEGES IN SCHEMA project_planning "
                "GRANT SELECT ON TABLES TO role"
            ]
        }
    )
    assert any("手写 GRANT" in p for p in problems), problems


def test_revoke_only_migration_is_allowed() -> None:
    """只 REVOKE 的迁移不触发判据（收紧授权永远安全，不该逼人绕路）。"""
    problems = _evaluate(
        grant_forbidden_literals={
            "data/migrations/versions/zzz_later.py": [
                "REVOKE DELETE ON project_planning.projects FROM role"
            ]
        }
    )
    assert problems == []


# --------------------------------------------------------------------------- #
# 5. 文档字符串不算违规（判据针对代码，不针对说明）
# --------------------------------------------------------------------------- #
def test_docstring_mentioning_grant_is_not_a_violation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """迁移的模块 docstring 会写「``GRANT … ON ALL TABLES`` 是错的」。

    若把 docstring 算成违规，结论就变成「文档里不许提旧实现」——判据与意图
    相反，且下一个人会为了过闸门删掉说明。用 AST 排除 docstring 后只剩代码。
    """
    module = tmp_path / "migration.py"
    module.write_text(
        '"""说明：不要写 GRANT SELECT ON project_planning.projects TO role。"""\n'
        "\n"
        "def upgrade() -> None:\n"
        '    """这里也只是文档：GRANT 是错的。"""\n'
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ba, "ROOT", tmp_path)
    assert ba._non_docstring_literals("migration.py") == []


def test_real_grant_literal_is_detected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 docstring 的字面量里出现 GRANT，必须被取到（否则判据形同虚设）。"""
    module = tmp_path / "migration.py"
    module.write_text(
        "def upgrade() -> None:\n"
        '    op.execute("GRANT SELECT ON project_planning.projects TO role")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ba, "ROOT", tmp_path)
    literals = ba._non_docstring_literals("migration.py")
    assert any("GRANT SELECT ON project_planning.projects" in literal for literal in literals)


# --------------------------------------------------------------------------- #
# 6. 真实仓库：断言通过（与上面的构造用例互补）
# --------------------------------------------------------------------------- #
def test_real_repository_passes() -> None:
    """真实仓库必须 PASS——构造用例证明判据会红，这条证明它现在是绿的。"""
    ok, reason = ba.get_assertion("data.role_grants_least_privilege")()
    assert ok, reason
