"""进程凭证面（CRED-1）的运行时行为证据。

为什么需要这组测试（根因）
------------------------------------------------------------
第二轮审查 P1-01 的事实是：identity 进程全文不碰数据库，却因为 compose 里
只有一个 ``x-db-env`` 全量 anchor，继承了 postgres 超级用户口令。也就是说，
进程职责已经拆开，凭证面没拆，边界只存在于文档里。

只改 compose 而不留证据，下一个人加服务时仍会 ``<<: *db-env`` 一把梭，
而且看不出来——权限看起来都通。所以这里锁三件事：

1. identity 一条 ``POSTGRES_*`` 都没有（不是「少几条」，是零）；
2. relay 只有 relay 角色的凭证（不是「也包含」。多一条就是越界）；
3. 新增服务不登记白名单就直接被判失败（闸门与测试同源，防「新进程默认继承」）。

判定逻辑在 ``scripts/boundary_assertions.py`` 里只有一份实现
（``evaluate_process_credential_surface``）。本文件既喂真实编排文件，也喂构造的
services 字典——后者证明「规则真的会拒绝」，而不是「碰巧现在没问题」。
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts 不是包，按模块加载（与 test_contract_check.py 一致）。
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import boundary_assertions as ba  # noqa: E402

# relay 只该拿到这些：连接参数 + 自己的角色口令。
RELAY_DB_ENV = {
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_RELAY_USER",
    "POSTGRES_RELAY_PASSWORD",
}

# 超级用户凭证只允许出现在 DDL / DBA 单元（建表、建分区必须表属主权限）。
#
# ``bootstrap`` 是本清单里唯一的**身份** DDL 单元：``CREATE ROLE`` /
# ``ALTER ROLE … NOSUPERUSER`` 只有超管能执行，而它同时要拿每个角色自己的口令
# 才能设密码——这两件事都不能拆给别的进程。它的边界不靠「少给凭证」保证，而靠
# 「一次性、只跑声明语句、``--roles`` 一个阶段、跑完即退」保证；迁移侧仍然只碰
# 结构、只断言身份存在，职责不合并。
DDL_UNITS = {"postgres", "migrate", "outbox-maintenance", "bootstrap"}


def _env_of(services: dict[str, dict], name: str) -> dict[str, object]:
    assert name in services, f"编排文件缺少服务 {name}"
    return ba.resolve_service_env(services[name])


def _postgres_keys(env: dict[str, object]) -> set[str]:
    return {key for key in env if key.startswith("POSTGRES_")}


# --------------------------------------------------------------------------- #
# 真实编排文件：凭证面必须与进程职责同形
# --------------------------------------------------------------------------- #
def test_identity_service_has_no_database_credentials() -> None:
    """identity 不碰数据库，因此一条数据库变量都不该注入。

    这条不测「少几条」，测「零」。只要有一条，就说明它继承了别人的凭证面，
    而进程内部根本没有用到它的代码——那唯一的用途只剩被滥用。
    """
    env = _env_of(ba.load_compose_services(), "identity")
    assert _postgres_keys(env) == set(), (
        f"identity 注入了数据库凭证 {sorted(_postgres_keys(env))}："
        "签发进程不碰数据库，这些凭证没有任何合法用途"
    )


def test_relay_service_only_has_relay_credentials() -> None:
    """relay 只投递事件：多一条凭证就是越界，不是「顺便也能用」。"""
    env = _env_of(ba.load_compose_services(), "relay")
    assert _postgres_keys(env) == RELAY_DB_ENV, (
        f"relay 的数据库凭证面与预期不符: 多了 "
        f"{sorted(_postgres_keys(env) - RELAY_DB_ENV)}，少了 "
        f"{sorted(RELAY_DB_ENV - _postgres_keys(env))}"
    )


def test_superuser_credentials_confined_to_ddl_units() -> None:
    """超级用户口令只能出现在 DDL / DBA 单元。

    业务进程拿超级用户口令，等于所有列级 GRANT、所有域角色隔离都可以被绕过，
    DATA-1/DATA-3 会退化成「文档里的约定」。
    """
    services = ba.load_compose_services()
    leaked = {
        name: sorted(
            _postgres_keys(ba.resolve_service_env(spec)) & {"POSTGRES_USER", "POSTGRES_PASSWORD"}
        )
        for name, spec in services.items()
        if name not in DDL_UNITS
    }
    leaked = {name: keys for name, keys in leaked.items() if keys}
    assert leaked == {}, f"非 DDL 单元持有超级用户凭证: {leaked}"


def test_real_compose_passes_credential_surface_check() -> None:
    """当前编排必须整体通过判定（失败原因即闸门报错原文）。"""
    problems = ba.evaluate_process_credential_surface(ba.load_compose_services())
    assert problems == [], f"编排凭证面存在问题: {problems}"


# --------------------------------------------------------------------------- #
# 构造输入：证明规则真的会拒绝，而不是碰巧现在干净
# --------------------------------------------------------------------------- #
def test_new_service_without_declaration_is_rejected() -> None:
    """新服务未登记白名单 → 直接判失败。

    这是 CRED-1 真正的防线：不指望加服务的人自觉，而是未登记就红。
    用构造的 services 验证，避免「注册一个真实服务再删掉」这种污染仓库的测法。
    """
    services = {
        "postgres": {"environment": {"POSTGRES_USER": "postgres"}},
        "worker-new": {
            "environment": {
                "POSTGRES_HOST": "postgres",
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": "postgres",
            }
        },
    }
    problems = ba.evaluate_process_credential_surface(services)
    assert any("worker-new" in p and "未在 PROCESS_ALLOWED_DB_ENV 登记" in p for p in problems), (
        f"新增服务未登记却未被拒绝: {problems}"
    )


def test_registered_service_missing_from_compose_is_reported() -> None:
    """白名单里登记了、编排里却没有 → 也要报错。

    否则白名单会腐化：服务删了、条目留着，「已覆盖」是个假象。
    """
    problems = ba.evaluate_process_credential_surface({})
    assert any("声明了不存在的服务" in p for p in problems), f"白名单僵尸条目未被检出: {problems}"
