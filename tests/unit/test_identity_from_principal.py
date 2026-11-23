"""身份不得自称：进入准入判定的「人」只能取自 Principal（P1-06 闸门）。

为什么需要这个文件
------------------------------------------------------------
``worker_id`` 与 ``approver`` 这两个字段此前都写在请求体里，而它们各自是
**唯一**的判定输入：

- ``lease_owner``：``require_lease_owner`` 用它决定「谁能续租/终结」。若它来自
  请求体，判定就等于「谁知道这个名字，谁就是持有者」——租约退化成一次公开的
  字符串比较，任何人都能接管别人的任务。
- ``approver``：领域层用 ``approver != requested_by`` 实现「自批拒绝」。若它
  来自请求体，申请人只要填一个别人的名字就能自批，而且库里留下一条**看起来
  正常**的审计记录——错误不在数据里，所以事后复核也查不出来。

两者的病根相同：**把身份当参数**。修法不是加校验，而是让请求模型里没有这个
槽位，调用方只能带凭据（JWT），身份从 Principal 解析。

本文件不重复领域层单测（那里测的是「规则本身」），只钉四件事：

1. 模型层：旧调用方仍传 ``worker_id`` / ``approver`` 时直接 ValidationError。
2. 线上契约：OpenAPI 里对应请求体**没有**这些属性，且 ``additionalProperties``
   为 false——客户端连「以为支持」的机会都没有。
3. 源码不变量（AST 派生，非人工清单）：任何端点里由 ``principal.<attr>`` 填充的
   语义槽位，其请求模型不得再声明同名字段。新增端点自动纳入，不靠人记。
4. 真库端到端：租约持有者真的等于令牌主体；伪造字段得到 422 且**任务状态没有
   任何变化**；换一个令牌续租被拒——证明租约绑的是身份，不是字符串。
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.main import app
from services.budget_quota.api.budget_routes import ApproveShotBudgetRequest
from services.production_planning.api.generation_routes import (
    FinishRequest,
    LeaseClaimRequest,
    LeaseRenewRequest,
)
from tests.conftest import TEST_TENANT_ID, make_test_token, set_test_scope

_REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 1. 模型层：自称身份必须在校验期就被拒绝
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "payload_name"),
    [
        (LeaseClaimRequest, "worker_id"),
        (LeaseRenewRequest, "worker_id"),
        (FinishRequest, "worker_id"),
        (ApproveShotBudgetRequest, "approver"),
    ],
)
def test_request_model_has_no_identity_slot(model: type, payload_name: str) -> None:
    """四个模型都必须**没有**身份字段，且对未知字段 fail-closed。

    只断言「字段不存在」不够：pydantic 默认 ``extra="ignore"`` 会把
    ``worker_id`` 静默丢掉后返回 200，调用方以为自己指定了执行者，实际执行者
    是令牌主体——这种「静默改写」比报错更难发现。所以同时断言
    ``extra="forbid"``，让旧调用方拿到 422。
    """
    assert payload_name not in model.model_fields, (
        f"{model.__name__} 又出现了身份槽位 {payload_name}：身份只能来自 Principal"
    )
    assert model.model_config.get("extra") == "forbid", (
        f"{model.__name__} 未设 extra='forbid'：旧调用方传 {payload_name} 会被静默忽略"
    )
    with pytest.raises(ValidationError):
        model.model_validate({payload_name: "someone_else"})


# --------------------------------------------------------------------------
# 2. 线上契约：OpenAPI 里不允许出现身份属性
# --------------------------------------------------------------------------

_IDENTITY_BOUND_PATHS = {
    "/generation-tasks/{entity_id}/lease": "worker_id",
    "/generation-tasks/{entity_id}/lease/renew": "worker_id",
    "/generation-tasks/{entity_id}/finish": "worker_id",
    "/shot-budget-requests/{entity_id}/approve": "approver",
}


def _request_schema(path: str, method: str = "post") -> dict:
    """取出某端点的 requestBody schema（内联或 ``$ref`` 已解析）。

    路径不存在时**显式报错**而不是让 KeyError 冒出去：路由被改名/挪前缀是
    很正常的重构，静默跳过会让这条守卫悄悄失效（那时它守护的正是最危险的
    东西：身份槽位）。
    """
    spec = app.openapi()
    assert path in spec["paths"], f"契约里找不到 {path}：路由改名后本条守卫已失效"
    operation = spec["paths"][path][method]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    if "$ref" in schema:
        ref = schema["$ref"].lstrip("#/").split("/")
        node: dict = spec
        for part in ref:
            node = node[part]
        return node
    return schema


@pytest.mark.parametrize("path", sorted(_IDENTITY_BOUND_PATHS))
def test_openapi_request_body_has_no_identity_property(path: str) -> None:
    """线上契约不得声明身份属性；不存在的路径要报错而不是静默跳过。"""
    schema = _request_schema(path)
    forbidden = _IDENTITY_BOUND_PATHS[path]
    assert forbidden not in schema.get("properties", {}), (
        f"{path} 的请求体仍声明 {forbidden}：客户端会合理地以为可以自己指定身份"
    )
    assert schema.get("additionalProperties") is False, (
        f"{path} 的请求体未 forbid 额外字段，伪造身份会被静默丢弃而非 422"
    )


# --------------------------------------------------------------------------
# 3. 源码不变量：由 principal 填充的槽位，不得由请求体声明（AST 派生）
# --------------------------------------------------------------------------


def _api_modules() -> list[Path]:
    """所有域 API 路由模块。集合从目录派生，新增域自动纳入。"""
    return sorted(_REPO_ROOT.glob("services/*/api/*.py"))


def _model_fields(classdef: ast.ClassDef) -> set[str]:
    """类体里声明的 pydantic 字段名（AnnAssign 形式）。"""
    return {
        stmt.target.id
        for stmt in classdef.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }


def _principal_filled_kwargs(func: ast.AST) -> set[str]:
    """函数里由 ``principal.<attr>`` 填充的关键字参数名。

    判据取的是「这个槽位被身份填充」这个**事实**，不是字段名字面量：写成
    ``worker_id=principal.subject_id`` 与 ``approver=principal.subject_id``
    都会被抓到，将来新增 ``reviewer=principal.subject_id`` 也自动纳入。
    """
    filled: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg is None:
                continue
            value = kw.value
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "principal"
            ):
                filled.add(kw.arg)
    return filled


def _request_body_model(func: ast.AST, model_names: set[str]) -> str | None:
    """端点函数里用作请求体的模型名（注解来自本模块内定义的模型）。"""
    args = func.args.args if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
    for arg in args:
        annotation = arg.annotation
        name: str | None = None
        if isinstance(annotation, ast.Name):
            name = annotation.id
        elif isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            name = annotation.value
        if name in model_names:
            return name
    return None


def test_no_request_model_shadows_principal_filled_slot() -> None:
    """同一语义槽位不得「既由身份填充、又由请求体声明」。

    这条不变量比逐个字段白名单可靠：白名单漏掉的新端点不会报错，它只是不在
    检查范围里——而「清单不会自己举手」已经在本项目栽过三次。
    """
    findings: list[str] = []
    scanned = 0

    for module_path in _api_modules():
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        models = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and any(
                (isinstance(base, ast.Name) and base.id == "BaseModel")
                or (isinstance(base, ast.Attribute) and base.attr == "BaseModel")
                for base in node.bases
            )
        }
        if not models:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(arg.arg == "principal" for arg in node.args.args):
                continue
            filled = _principal_filled_kwargs(node)
            if not filled:
                continue
            body_model = _request_body_model(node, set(models))
            if body_model is None:
                continue
            scanned += 1
            shadowed = filled & _model_fields(models[body_model])
            if shadowed:
                rel = module_path.relative_to(_REPO_ROOT)
                findings.append(
                    f"{rel}:{node.lineno} {node.name}() 用 Principal 填充 "
                    f"{sorted(shadowed)}，但请求模型 {body_model} 又声明了同名字段"
                )

    # 扫描面为 0 说明 AST 判据整体失效（例如全部端点改了参数名），
    # 此时「无发现」是假绿——必须报错而不是通过。
    assert scanned > 0, "AST 判据未匹配到任何端点，检查失效而非代码合规"
    assert not findings, "身份槽位同时由 Principal 与请求体提供：\n" + "\n".join(findings)


# --------------------------------------------------------------------------
# 4. 真库端到端：身份真的来自令牌，且伪造字段不留下任何效果
# --------------------------------------------------------------------------

_PROJECT_ID = "project-identity-1"
_SHOT_ID = "shot-identity-1"
_SHOT_REVISION_ID = "shotrev-identity-1"


@pytest.fixture(scope="module")
async def task_id(admin_sessionmaker) -> str:
    """播种项目 + 一个 REQUESTED 状态的生成任务，返回本次的任务 id。

    走**管理连接**：``production_planning_role`` 对 ``project_planning.projects``
    无写权限（DATA-1 拆角色），用业务角色连播种都插不进去。播种属于测试装置，
    不给产品角色开后门。

    任务 id 每次运行随机：本文件的用例跨条目推进同一个任务（422 → RUNNING →
    换令牌续租），固定 id 就得靠「重置」让第二次运行能跑。而重置是错的——
    ``audit.status_transitions`` 是 append-only（连超级用户 DELETE 都被
    ``RestrictViolation`` 拦下），「把历史审计事实删掉好让测试能重跑」正是这条
    红线要禁止的操作。改用新 id，装置自然幂等，历史事实一行不动。

    项目刻意 ``organization_id IS NULL``：租约端点只判租户，本用例要测的是
    「持有者从哪来」，掺进组织维度会让另一个不变量（org ABAC）在当前用例里
    也参与判定，失败时无法区分是哪一条出的问题。组织维度的租约用例在
    ``tests/permission`` 里单独覆盖。

    任务用 REQUESTED 出生态：``claim`` 的前置条件正是「无人持有且未开始」，
    预置成 RUNNING 会把被测命题（谁能拿到租约）换成别的。
    """
    suffix = uuid.uuid4().hex[:8]
    task_id = f"gentask-identity-{suffix}"
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO project_planning.projects
                    (entity_id, entity_type, schema_version, entity_version,
                     status, owner_module, tenant_id, created_at, created_by,
                     updated_at, project_name, project_tier, organization_id)
                VALUES
                    (:eid, 'Project', 205, 1, 'DRAFT', 'project_planning',
                     :tid, now(), 'tester', now(), '身份测试项目', 'B', NULL)
                ON CONFLICT (entity_id) DO NOTHING
                """
            ),
            {"eid": _PROJECT_ID, "tid": TEST_TENANT_ID},
        )
        await session.execute(
            text(
                """
                INSERT INTO production_planning.generation_tasks
                    (entity_id, entity_type, schema_version, entity_version,
                     status, owner_module, tenant_id, created_at, created_by,
                     updated_at, generation_task_id, project_id, shot_id,
                     shot_revision_id, production_route_ref, model_adapter_ref,
                     constraint_snapshot_id, budget_ref, prompt_ref)
                VALUES
                    (:eid, 'GenerationTask', 205, 1, 'REQUESTED',
                     'production_planning', :tid, now(), 'tester', now(),
                     :eid, :pid, :sid, :srid, 'route-1', 'adapter-1',
                     'snapshot-1', 'budget-1', 'prompt-1')
                """
            ),
            {
                "eid": task_id,
                "tid": TEST_TENANT_ID,
                "pid": _PROJECT_ID,
                "sid": _SHOT_ID,
                "srid": _SHOT_REVISION_ID,
            },
        )
        await session.commit()
    return task_id


@pytest.fixture(scope="module")
async def production_sessionmaker():
    """``production_planning_role`` 连接：回读必须用真实业务角色。

    用超级用户回读只能证明「行写进去了」，不能证明业务角色看得见、也没法验证
    RLS 下的可见性口径——而租约判定的读写都发生在该角色下。
    """
    from packages.shared.database import (
        build_production_planning_database_url,
        create_sessionmaker,
    )

    engine = create_async_engine(build_production_planning_database_url())
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


def _client(subject: str, roles: list[str]) -> AsyncClient:
    token = make_test_token(subject_id=subject, roles=roles)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _read_lease(
    sessionmaker: async_sessionmaker, task_id: str
) -> tuple[str, str]:
    """回读任务状态与租约持有者（业务角色 + 测试作用域）。"""
    async with sessionmaker() as session:
        await set_test_scope(session)
        row = (
            await session.execute(
                text(
                    "SELECT status, lease_owner FROM production_planning.generation_tasks"
                    " WHERE entity_id = :eid"
                ),
                {"eid": task_id},
            )
        ).one()
        return row[0], row[1]


async def test_forged_worker_id_is_rejected_and_leaves_no_trace(
    task_id: str,
    production_sessionmaker,
) -> None:
    """带 ``worker_id`` 的领取请求得到 422，且任务**完全没有被推进**。

    这一条正是「被忽略不了」的证据：若 422 只是响应码对了但请求已经落库，
    审计上看就是一次正常领取——那比不修更危险。
    """
    async with _client("worker_real", ["Worker"]) as client:
        response = await client.post(
            f"/generation-tasks/{task_id}/lease",
            json={"lease_seconds": 300, "worker_id": "worker_impostor"},
        )

    assert response.status_code == 422, response.text
    assert await _read_lease(production_sessionmaker, task_id) == ("REQUESTED", "")


async def test_lease_owner_is_the_token_subject_not_a_request_field(
    task_id: str,
    production_sessionmaker,
) -> None:
    """合规请求成功，且库里记下的持有者等于**令牌主体**。"""
    async with _client("worker_real", ["Worker"]) as client:
        response = await client.post(
            f"/generation-tasks/{task_id}/lease",
            json={"lease_seconds": 300},
        )

    assert response.status_code == 200, response.text
    assert response.json()["lease_owner"] == "worker_real"
    assert await _read_lease(production_sessionmaker, task_id) == ("RUNNING", "worker_real")


async def test_non_holder_cannot_renew_even_with_a_valid_token(task_id: str) -> None:
    """换一个同样是 Worker 的令牌续租被拒：租约绑身份，不绑字符串。

    这是整条修复的**结论性**证据：旧实现里只要知道 ``worker_id`` 就能续租，
    所以「换令牌」与「换请求体字符串」没有任何区别。修复后前者被令牌隔离，
    后者被 422 拦下，两条路都堵住了。
    """
    async with _client("worker_impostor", ["Worker"]) as client:
        response = await client.post(
            f"/generation-tasks/{task_id}/lease/renew",
            json={"lease_seconds": 300},
        )

    assert response.status_code != 200, response.text
    assert "租约" in response.text, response.text
