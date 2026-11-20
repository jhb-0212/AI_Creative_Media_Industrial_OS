"""授权断言 ``data.role_grants_least_privilege`` 的负向用例（证明它会报红）。

为什么断言也要有负向用例：只证明「真实仓库现在 PASS」无法区分两种情形——
断言判据真的成立，还是判据写得太松、怎么改都 PASS。本文件对每一类失效
各构造一份输入，断言它**恰好**报出对应的那条问题。

五类失效对应五类判据（见 ``evaluate_least_privilege`` 文档）：
1. ORM 有新表但矩阵没声明（新表静默无权限）；
2. 矩阵声明了不存在的表（陈旧声明，收敛迁移会失败）；
3. 授权迁移的语句与矩阵不一致（手写 DDL / 矩阵改了迁移没跟上）；
4. 迁移手写 GRANT（绕过矩阵的唯一入口，大小写均拦）；
5. 授权迁移没声明 tables 快照（读活矩阵，未来新表会让 clean install 崩）。
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
    """从真实 ORM 元数据派生「各域有哪些业务表」。

    为什么不再手写表名：手写快照是第二事实源，必然与真实 ORM 漂移。A13 新增
    5 张 Scene/Shot 表后，这份快照立刻陈旧，本文件的「合规样本」随即报红——
    但报红的语义是「判据坏了」，实际是「样本落后了」，下一个人会去改判据而
    不是改样本。改为派生后，样本与生产断言 ``data.role_grants_least_privilege``
    同源，新增表自动跟上。

    两个域模块都要导入：它们共用同一个 Base，只导入一个就只注册一个 schema。
    """
    from packages.shared.db_grants import DOMAINS
    from services.asset_service.repository import orm as asset_orm
    from services.project_planning.repository import orm as planning_orm

    by_schema: dict[str, set[str]] = {}
    for module in (planning_orm, asset_orm):
        for table in module.Base.metadata.tables.values():
            by_schema.setdefault(table.schema or "", set()).add(table.name)
    return {domain: frozenset(by_schema.get(domain, set())) for domain in DOMAINS}


def _evaluate(**overrides: object) -> list[str]:
    """以合规样本为基准，按需替换某一维，返回问题列表。"""
    kwargs: dict[str, object] = {
        "matrix_problems": [],
        "business": _business(),
        "orm_tables": _orm(),
        "converge": _converge(),
        "rendered": _rendered(),
        "grant_forbidden_literals": {},
        "unfrozen_converge_calls": [],
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


def test_lowercase_grant_is_also_handwritten_grant() -> None:
    """SQL 关键字不分大小写：``grant`` 与 ``GRANT`` 同权（评审 F2）。

    正则若只认大写，一句小写 grant 就能绕过矩阵而闸门仍然 PASS——判据从
    「没有手写授权」退化成「没有用大写字母写授权」。
    """
    problems = _evaluate(
        grant_forbidden_literals={
            "data/migrations/versions/zzz_later.py": [
                "grant select, insert on project_planning.projects to role"
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


# --------------------------------------------------------------------------- #
# 5. 授权迁移必须声明自己的表范围（评审 F1）
# --------------------------------------------------------------------------- #
def test_converge_call_without_tables_snapshot_is_reported() -> None:
    """读活矩阵的迁移在「结果」上完全正常，问题出在时间轴上。

    这条用例守住的是「判据能不能发现一个看起来正确的迁移」：它的语句与矩阵
    逐条相等（所以判据 2 通过），但未来矩阵加表时它会在建表迁移之前 REVOKE
    那张还不存在的表。只能靠调用点判据抓。
    """
    problems = _evaluate(
        unfrozen_converge_calls=[
            "data/migrations/versions/c1a2b3d4e5f6_least_privilege_business_schema.py:88"
        ]
    )
    assert any("未传 tables= 快照" in p for p in problems), problems


def test_real_repository_declares_snapshot() -> None:
    """真实迁移必须已声明快照：否则上一条用例只是「规则存在但没人遵守」。"""
    from scripts import boundary_assertions as ba

    converge_migration, later = ba._migration_chain(ba._LEAST_PRIVILEGE_REVISION)
    assert ba._unfrozen_converge_calls([converge_migration, *later]) == []


# --------------------------------------------------------------------------- #
# 5. 授权迁移必须声明自己的表范围（评审 F1）
# --------------------------------------------------------------------------- #
def test_legacy_downgrade_does_not_touch_shared_tables() -> None:
    """降级只回滚本迁移改过的东西——业务 schema 的域角色表级权限。

    共享表（audit / outbox / auth / public）的历史口径从未被本迁移改动，所以
    「升级前是什么」就是矩阵此刻声明的权限。早先的降级对它们一律授全量 DML，
    把 ``auth.revoked_tokens`` 从只读放大成可 DELETE：降级一次就能删掉吊销记录，
    让已吊销令牌复活——降级路径成了「终态不可复活」的后门。
    """
    from packages.shared.db_grants import DOMAINS, legacy_statements
    from packages.shared.db_roles import domain_role

    shared_markers = ("audit.", "outbox.", "auth.", "public.")
    for domain in DOMAINS:
        statements = legacy_statements(domain_role(domain), domain)
        touched = [
            statement
            for statement in statements
            if any(marker in statement for marker in shared_markers)
        ]
        assert not touched, f"{domain} 降级语句触碰了共享表：{touched}"


def test_legacy_downgrade_never_grants_delete_on_revoked_tokens() -> None:
    """把上一条的核心风险单独钉死：吊销记录不可被删。"""
    from packages.shared.db_grants import DOMAINS, legacy_statements
    from packages.shared.db_roles import domain_role

    for domain in DOMAINS:
        statements = legacy_statements(domain_role(domain), domain)
        assert not any("revoked_tokens" in s for s in statements), statements


# --------------------------------------------------------------------------- #
# 6. 降级不得把共享表放大成可写（评审 F3）
# --------------------------------------------------------------------------- #
