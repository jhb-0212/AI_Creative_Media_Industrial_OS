"""授权断言 ``data.role_grants_least_privilege`` 的负向用例（证明它会报红）。

为什么断言也要有负向用例：只证明「真实仓库现在 PASS」无法区分两种情形——
断言判据真的成立，还是判据写得太松、怎么改都 PASS。本文件对每一类失效
各构造一份输入，断言它**恰好**报出对应的那条问题。

五类失效对应 ``evaluate_least_privilege`` 的前四条判据与矩阵自检：
1. ORM 有新表但矩阵没声明（新表静默无权限）；
2. 矩阵声明了不存在的表（陈旧声明，收敛迁移会失败）；
3. 折叠终态与矩阵不一致（超额授权 / 缺权 / PUBLIC 留权 / 未声明表 / 列级越界）；
4. 迁移手写 GRANT（绕过矩阵的唯一入口，大小写均拦）；
5. 授权迁移没声明 tables 快照（读活矩阵，未来新表会让 clean install 崩）。

第 3 类的用例刻意分成两组：**会被收敛掉的历史超额授权不算问题**（这是折叠相对
「逐条比对」的实质性改进，`i9d0` → `l2a3` 就是这种形态），而**收敛不掉**的必须
报红。少了前一组，判据会退化成「不许有历史」；少了后一组，判据会退化成「有过
收敛就都算对」。
"""

from __future__ import annotations

import pytest

from packages.shared.db_grants import (
    DOMAINS,
    business_tables,
    converge_statements,
    declared_column_privileges,
    declared_privileges,
)
from packages.shared.db_roles import domain_role
from scripts import boundary_assertions as ba

_PROJECT = "project_planning"
_ASSET = "asset_service"


def _converge() -> dict[str, list[str]]:
    return {domain: converge_statements(domain_role(domain), domain) for domain in DOMAINS}


def _rendered() -> list[str]:
    return [statement for domain in sorted(DOMAINS) for statement in _converge()[domain]]


def _business() -> dict[str, frozenset[tuple[str, str]]]:
    return {domain: business_tables(domain) for domain in DOMAINS}


def _orm() -> dict[str, frozenset[str]]:
    return {
        _PROJECT: frozenset({"projects", "episodes", "scenes", "script_revisions", "organizations"}),
        _ASSET: frozenset({"asset_versions", "qc_reports"}),
    }


def _evaluate(**overrides: object) -> list[str]:
    """以合规样本为基准，按需替换某一维，返回问题列表。"""
    kwargs: dict[str, object] = {
        "matrix_problems": [],
        "business": _business(),
        "orm_tables": _orm(),
        "declared": {domain: declared_privileges(domain) for domain in DOMAINS},
        "declared_columns": {
            domain: declared_column_privileges(domain) for domain in DOMAINS
        },
        "roles": {domain: domain_role(domain) for domain in DOMAINS},
        # 单链样本即「矩阵的全量执行」：作为基线，它必须在零问题的前提下成立，
        # 否则下面所有「恰好报一条」的断言都失去意义。
        "chains": {"main": _rendered()},
        "grant_forbidden_literals": {},
        "unfrozen_converge_calls": [],
    }
    kwargs.update(overrides)
    return ba.evaluate_least_privilege(**kwargs)  # type: ignore[arg-type]


def _with_extra(*statements: str) -> dict[str, list[str]]:
    return {"main": [*_rendered(), *statements]}


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
# 3. 折叠终态与矩阵不一致
# --------------------------------------------------------------------------- #
def test_over_grant_on_matrix_table_is_reported() -> None:
    problems = _evaluate(
        chains=_with_extra(
            f"GRANT DELETE ON project_planning.episodes TO {domain_role(_PROJECT)}"
        )
    )
    assert any("DELETE" in p and "≠ 矩阵声明" in p for p in problems), problems


def test_missing_grant_in_migration_is_reported() -> None:
    """矩阵声明了权限、迁移里没有 —— 结果是运行期 42501，而不是「暂时没人用」。"""
    granted = (
        f"GRANT SELECT, INSERT, UPDATE ON project_planning.scenes TO {domain_role(_PROJECT)}"
    )
    chains = {"main": [statement for statement in _rendered() if statement != granted]}
    problems = _evaluate(chains=chains)
    assert any("project_planning.scenes" in p and "≠ 矩阵声明" in p for p in problems), problems


def test_missing_default_privilege_revoke_is_reported() -> None:
    """少撤销一条默认权限也算不一致：那正是「未来新表自动带权限」的入口。"""
    chains = {
        "main": [
            statement
            for statement in _rendered()
            if "REVOKE ALL ON SEQUENCES" not in statement
        ]
    }
    problems = _evaluate(chains=chains)
    assert any("缺少默认权限撤销" in p for p in problems), problems


def test_grant_on_undeclared_table_is_reported() -> None:
    """矩阵之外的表被授权 —— 授权面由「谁顺手建了表」决定，正是 N-06 的根因。"""
    problems = _evaluate(
        chains=_with_extra(
            f"GRANT SELECT ON project_planning.shots TO {domain_role(_PROJECT)}"
        )
    )
    assert any("未声明的表权限" in p and "shots" in p for p in problems), problems


def test_public_grant_is_reported() -> None:
    """``PUBLIC`` 的授权对所有角色生效，按角色过滤的比对看不见它（评审 F4）。"""
    problems = _evaluate(
        chains=_with_extra("GRANT SELECT ON project_planning.scenes TO PUBLIC")
    )
    assert any("PUBLIC 仍持有" in p for p in problems), problems


def test_mass_grant_is_reported() -> None:
    """``ON ALL TABLES`` 折不出来，必须报红而不是被静默跳过。"""
    problems = _evaluate(
        chains=_with_extra(
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {_PROJECT} TO {domain_role(_PROJECT)}"
        )
    )
    assert any("整 schema 授权无法折算" in p for p in problems), problems


def test_default_privilege_grant_is_reported() -> None:
    """给域角色授默认权限是「未来每张新表天生带权限」，终态判据必须拦住。"""
    problems = _evaluate(
        chains=_with_extra(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_PROJECT} GRANT SELECT ON TABLES TO "
            f"{domain_role(_PROJECT)}"
        )
    )
    assert any("默认 TABLES 权限" in p for p in problems), problems


def test_column_level_over_grant_is_reported() -> None:
    """列级授权是另一份 ACL：整表比对相等不代表列级也对。"""
    problems = _evaluate(
        chains=_with_extra(
            f"GRANT SELECT (entity_id) ON project_planning.projects TO "
            f"{domain_role(_ASSET)}"
        )
    )
    assert any("列级授权" in p for p in problems), problems


def test_over_grant_revoked_by_later_convergence_is_not_reported() -> None:
    """「先超额授权、再由后续迁移收敛」是合法修复形态，不得报红。

    这正是 ``i9d0``（批次五建表时手写 ``GRANT … DELETE``）→ ``l2a3``（批次八按
    矩阵收敛）的真实形态。旧判据（语句多重集逐条相等）把它判成「多执行 1 条」，
    于是判据逼人在「不改历史」与「过闸门」之间二选一——最终一定会有人删掉收敛
    迁移去迎合闸门。判据要守的是**终态**，不是「历史上从未超额」。
    """
    converged_scenes = [
        statement
        for statement in _converge()[_PROJECT]
        if "project_planning.scenes" in statement
    ]
    chains = {
        "main": _rendered(),
        "feat": [
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON project_planning.scenes TO "
            f"{domain_role(_PROJECT)}",
            *converged_scenes,
        ],
    }
    assert _evaluate(chains=chains) == []


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


def test_real_repository_scans_pending_migration_chains() -> None:
    """DATA-6 的扫描集合必须覆盖 ``pending/`` 下的分支链。

    这条守的是一个**范围**缺陷而不是内容缺陷：旧实现按 alembic 主链派生扫描集合，
    而 ``data/migrations/pending/<feature>/`` 不在 ``versions/`` 里（放进去会造成双
    head 二义性），于是分支链新建并授权的表（``scenes``）对判据完全隐形——报出来
    的还是「主链少执行 3 条」，把「我没扫到」说成「你少做了」。

    断言三件事，缺一件扫描集合就会再次失去意义：目录被发现、顺序可判定（没有
    分叉）、并且链里确实有矩阵执行器（否则「扫到了」也只是扫到一堆无关文件）。
    """
    from scripts import boundary_assertions as ba

    chains = ba._pending_migration_chains()
    assert chains, "pending 目录未被扫描：分支链的授权迁移将完全隐形"
    ordered = [path for paths in chains.values() for path in paths]
    assert ba._unfrozen_converge_calls(ordered) == []
    assert any(ba._calls_converge_statements(path) for path in ordered), (
        "pending 链里没有任何 converge_statements 调用："
        "授权迁移没被识别为矩阵执行器，折叠会漏掉整张表"
    )


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
