"""编排接线测试：``bootstrap → migrate → api`` 这条链必须在文件里真实存在。

为什么需要这层测试：身份生命周期从迁移搬到 bootstrap 之后，「迁移不再建角色」
和「部署没有先建角色」这两件事**各自都合法**，合起来才是主线必红。这类缺口不会
被任何单侧测试发现——迁移测试只关心迁移，编排测试只关心 YAML 语法。所以这里把
三份文件（compose / CI / Dockerfile）的**相对顺序**钉成断言：

1. ``bootstrap`` 服务存在，命令是 ``scripts/bootstrap_identity.py --roles``；
2. ``migrate`` 以 ``service_completed_successfully`` 依赖 ``bootstrap``，
   ``api`` 以同一条件依赖 ``migrate``（失败即不启动，不是「重试看看」）；
3. ``bootstrap`` 的数据库凭证面已在 ``PROCESS_ALLOWED_DB_ENV`` 登记，且实际 env
   不超出登记集合（新增服务不登记时 CRED-1 会报红，这里提前钉死）；
4. 运行镜像带得动这个脚本（Dockerfile 不拷 ``scripts/`` 的话，compose 一定失败）；
5. CI 里 bootstrap 步骤出现在 ``alembic upgrade head`` 之前。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_boundary_assertions() -> object:
    """与其它闸门单测同法：按路径加载脚本模块（它不是包的一部分）。"""
    path = REPO_ROOT / "scripts" / "boundary_assertions.py"
    spec = importlib.util.spec_from_file_location("_ba_deploy_wiring", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ba() -> object:
    return _load_boundary_assertions()


def _depends_on(service: dict) -> dict:
    value = service.get("depends_on") or {}
    assert isinstance(value, dict), (
        "depends_on 必须是长语法（带 condition），短语法表达不了「成功才继续」"
    )
    return value


def test_bootstrap_service_runs_roles_phase(ba: object) -> None:
    services = ba.load_compose_services()
    assert "bootstrap" in services, "编排里没有 bootstrap 服务：空集群上 migrate 必然失败"
    command = services["bootstrap"].get("command") or []
    assert isinstance(command, list), "command 用列表形式，避免 shell 解析差异"
    assert command[:2] == ["python", "scripts/bootstrap_identity.py"], command
    assert "--roles" in command, "bootstrap 必须显式只做 roles 阶段（grants 在迁移之后）"


def test_bootstrap_runs_before_migrate_before_api(ba: object) -> None:
    services = ba.load_compose_services()

    assert "migrate" in services and "api" in services
    migrate_deps = _depends_on(services["migrate"])
    assert "bootstrap" in migrate_deps, "migrate 没有依赖 bootstrap：迁移会在建角色之前跑"
    assert migrate_deps["bootstrap"].get("condition") == "service_completed_successfully", (
        "只有 bootstrap 成功退出才允许迁移；其它 condition 等于「跑过就算」"
    )

    api_deps = _depends_on(services["api"])
    assert api_deps["migrate"].get("condition") == "service_completed_successfully"


def test_bootstrap_credentials_are_registered_and_bounded(ba: object) -> None:
    services = ba.load_compose_services()
    allowed = ba.PROCESS_ALLOWED_DB_ENV
    assert "bootstrap" in allowed, "新增服务未在 PROCESS_ALLOWED_DB_ENV 登记（CRED-1 会报红）"

    actual = {
        key
        for key in (services["bootstrap"].get("environment") or {})
        if key.startswith("POSTGRES_")
    }
    excess = sorted(actual - allowed["bootstrap"])
    assert not excess, f"bootstrap 的凭证面超出登记集合：{excess}"


def test_credential_surface_gate_is_clean(ba: object) -> None:
    problems = ba.evaluate_process_credential_surface(ba.load_compose_services())
    assert problems == [], problems


def test_runtime_image_ships_the_bootstrap_script() -> None:
    dockerfile = (REPO_ROOT / "infra" / "docker" / "Dockerfile.api").read_text(encoding="utf-8")
    assert "COPY scripts/bootstrap_identity.py" in dockerfile, (
        "运行镜像不含 bootstrap 脚本：compose 的 bootstrap 服务会以 'can't open file' 失败"
    )


def test_ci_bootstraps_before_upgrade_head() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    bootstrap_at = ci.find("scripts/bootstrap_identity.py --roles")
    upgrade_at = ci.find("uv run alembic upgrade head")
    assert bootstrap_at != -1, "CI 没有 bootstrap 步骤：空库上 Apply migrations 必红"
    assert upgrade_at != -1
    assert bootstrap_at < upgrade_at, "CI 里 bootstrap 出现在迁移之后，顺序反了"


def test_ci_has_a_clean_cluster_job_that_expects_failure_first() -> None:
    """空集群的「先失败、后成功」两段必须在 CI 里成对存在。"""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "fresh-cluster-bootstrap" in ci
    assert "is missing: run scripts/bootstrap_identity.py --roles first" in ci, (
        "干净集群用例没有断言迁移应当失败：只测「bootstrap 后能升级」等于自证"
    )


def test_ci_roundtrip_runs_on_a_dedicated_clean_database() -> None:
    """迁移往返必须在没有测试残留的库上跑。

    2026-11-28 的真实失败：主 job 的 ``pytest tests`` 在共享库上留下一批业务行，
    其中 1 条 ``generation_tasks`` 停在 ``AWAITING_ASSET_REGISTRATION``；紧随其后的
    ``alembic downgrade -58`` 在 ``e1f2a3b4c5d6`` 被 §8 守卫拦下（守卫是对的）。
    于是「downgrade() 能不能执行」这条证据失效，而 CI 只显示 exit 1。
    把往返放到独立库，才能让红/绿只反映迁移本身。
    """
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    start = ci.find("- name: Migration downgrade roundtrip")
    assert start != -1, "CI 里没有迁移往返步骤"
    step = ci[start:]
    assert "ai_film_roundtrip" in step, (
        "往返步骤没有独立库：主库的 pytest 残留会让 §8 守卫拦下降级，证据失效"
    )
    assert "CREATE DATABASE ai_film_roundtrip" in step
    assert 'alembic downgrade "-${DEPTH}"' in step, "深度必须来自派生脚本，不能写死"
    assert step.find("CREATE DATABASE ai_film_roundtrip") < step.find(
        'alembic downgrade "-${DEPTH}"'
    ), "先建库再回滚，顺序不能反"
