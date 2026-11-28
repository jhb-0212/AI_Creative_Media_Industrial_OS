"""集成测试的数据库版本闸门（C4）：被测库必须停在仓库声明的迁移 head。

为什么这是**全局前提**而不是某条用例内部的断言
------------------------------------------------
「库落后」影响的是所有依赖 schema / 迁移结果 / 权限配置的集成测试，不只一条
ACL 用例。而它的失效形态通常不是红灯，是**绿灯**：测试读到的是一份更旧的结构，
缺少的对象被当成「本库没有、不适用」，收紧的约束还没生效，于是结论看起来一切正常。
本仓库就发生过：库停在 ``b1c2d3e4f5a6``、head 是 ``f9b0c1d2e3f4``，
``tests/integration/test_tenant_isolation_invariants.py`` 仍四条全绿。

一旦这种绿色被当作结论，任何基于它的验收（例如「删掉重复用例后仍然绿」）都不可信。
所以判定放在集成测试基础设施层，一次判定、整目录生效，且**不提供豁免开关**：
能绕过前提的绿色结果等于没有前提。历史版本的兼容性验证属于迁移测试，应当用
独立数据库、显式指定目标 revision 运行，不能靠放宽本前提实现。

为什么把纯判定单独放这里
------------------------
判定是「两个集合是否相等」的纯逻辑，值得被单测逐种形态覆盖（落后 / 超前 /
分叉 / 空库）。取数是 I/O，只在 conftest 编排一次。两者混在一起会导致
「测闸门必须先有一个库」，那闸门就只能靠人肉验证。
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import build_migration_url

# tests/integration/_db_head.py -> tests/integration -> tests -> 仓库根
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class DatabaseHeadMismatchError(AssertionError):
    """被测库的 revision 集合与仓库迁移 head 集合不一致。"""


def repository_heads(alembic_ini: Path | None = None) -> tuple[str, ...]:
    """仓库**应该**处于哪个（些）revision。

    从 ``alembic.ini`` 的 ``script_location`` 派生，而不是在测试里写死快照：
    写死的值在每次新增迁移后都要手工同步，它迟早会变成一份「过时的期望」，
    而闸门会继续用它判出一个看起来正常、实际错误的结论。
    """
    config = Config(str(alembic_ini or ALEMBIC_INI))
    script = ScriptDirectory.from_config(config)
    return tuple(sorted(script.get_heads()))


def _describe(revisions: tuple[str, ...]) -> str:
    if not revisions:
        return "（空：库中没有迁移版本记录，视为未迁移）"
    return "、".join(revisions)


def check_database_at_head(current_revisions: tuple[str, ...], heads: tuple[str, ...]) -> None:
    """断言库 revision 集合等于仓库 head 集合，不等即抛。

    用集合而不是单值比较：``alembic_version`` 支持多行（多 head 分叉），
    「落后」「超前」「分叉」「空库」都是同一种事实——这个库不是本分支要测的形态。
    """
    if set(current_revisions) == set(heads):
        return

    raise DatabaseHeadMismatchError(
        "集成测试的全局前提不满足：被测库与仓库迁移 head 不一致。\n"
        f"  当前库 revision：{_describe(tuple(sorted(current_revisions)))}\n"
        f"  仓库 head　　　：{_describe(heads)}\n"
        "\n"
        "为什么必须相等：库落后时测试读到的是更旧的结构——缺少的对象会被当成\n"
        "「不需要检查」，收紧的约束还没生效，于是测试会绿得没有依据；库超前或\n"
        "分叉时，判据面对的是一份仓库里不存在的结构。两种情况下的绿色结论都不可采信。\n"
        "\n"
        "处理方式（在仓库根目录执行）：\n"
        "    alembic upgrade head\n"
        "\n"
        "需要验证历史版本的行为时，那属于迁移兼容性测试：用独立数据库、显式\n"
        "指定目标 revision 运行。本前提不提供豁免开关。"
    )


async def read_current_revisions(engine: AsyncEngine | None = None) -> tuple[str, ...]:
    """读被测库的 ``alembic_version``；表不存在按「尚未迁移」返回空集。

    「表不存在」与「连接失败」必须分开：前者是明确的「未迁移」，后者是环境
    不可用。把前者当成异常抛出，报错会指向一次连接错误，反而掩盖真正原因。
    """
    owned = engine is None
    if engine is None:
        engine = create_async_engine(build_migration_url())
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL"))
            if not exists:
                return ()
            rows = await conn.execute(text("SELECT version_num FROM alembic_version"))
            return tuple(str(version) for version in rows.scalars())
    finally:
        if owned:
            await engine.dispose()


__all__ = [
    "ALEMBIC_INI",
    "DatabaseHeadMismatchError",
    "check_database_at_head",
    "read_current_revisions",
    "repository_heads",
]
