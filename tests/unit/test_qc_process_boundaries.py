"""进程内「身份边界」的结构断言：靠 import 面而不是靠注释。

两条边界都是**结构**问题，不是调用约定：

1. QC Worker 进程必须同时做两件事——验 QC 池的 RLS 是否生效、验发现身份是否
   仍然「只能发现」。二者都在装配前执行，缺一条则「启动即证明」变成「文档说
   过」。
2. 维护登录池不得出现在 QC Worker 进程里：一旦它被 import 进来，「维护只能经
   命令触达数据」就只能靠代码自觉；反过来，维护命令进程也不得 import 三个
   业务/发现池——它的最小权限正是「只有一个登录身份」。

为什么用源码断言：这些是装配位置的事实，运行时无法从对象图反推「本来可以
import 却没有 import」。仓库对同类问题已有先例（进程入口角色契约测试）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_qc_startup_verifies_both_identities() -> None:
    source = _source("workers/qc/__main__.py")
    assert re.search(r"await verify_rls_enforcement\(", source), (
        "QC 池未在启动期验 RLS：BYPASSRLS 误配会让租户上下文变成装饰"
    )
    assert re.search(r"await verify_discovery_identity\(", source), (
        "发现身份未在启动期验能力面：别处补的一句 GRANT 不会被发现"
    )


def test_qc_worker_process_has_no_maintenance_pool() -> None:
    source = _source("workers/qc/__main__.py")
    assert "create_qc_maintenance_engine" not in source, (
        "QC Worker 进程不得装配维护登录池：维护命令必须留在独立进程"
    )


def test_maintenance_process_has_only_its_own_pool() -> None:
    source = _source("workers/qc_maintenance/__main__.py")
    for forbidden in (
        "create_qc_governance_engine",
        "create_asset_service_engine",
        "create_qc_scheduler_engine",
    ):
        assert forbidden not in source, (
            f"维护命令进程不得装配 {forbidden}：多一个池就多一条绕过命令的路径"
        )
