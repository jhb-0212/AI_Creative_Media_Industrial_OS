"""Phase 1 核心主链端到端测试（S5 复验的可执行部分）。

为什么需要这个文件
------------------------------------------------------------
现有 528 个测试里，跨域**正向**链路是一条都没有的：
``tests/integration`` 逐项证明约束成立，``tests/unit/test_*_api.py`` 按域证明
端点可用，``tests/unit/test_resource_scope_api.py`` 只证明了越权会被拒。
于是「每个零件都对」被反复证明，「装起来还能不能跑通」从未被证明。

第二轮审查的 P1-02 正是这种盲区的产物：单测全绿，端到端却存在一条准入标准
不同的旁路。所以本文件不重复零件测试，只做三件事：

1. **走通主链**：项目 → 集数 → 剧本 → 双人锁 → 资产 → QC → 审批 → 入金库。
   每一跳都用真实端点、真实 JWT、真实 PostgreSQL，断言状态真的变了。
2. **在链中间验证拦截**：角色不符、证据缺失、终态复活，三种拒绝必须发生在
   **业务流程中**，而不是在孤立用例里被单独构造出来。
3. **验证审批路径的唯一性**：通用状态接口在结构上不可达 APPROVED。

主链口径来自 ``contracts/phases/phase_map.yaml`` 的 Phase 1 core_production。
当前实现只覆盖到「核心 QC → 人工 Gate」这一段：AI 拆镜、ShotSpec、
ConstraintPack、单一路线生成、DeliveryManifest 五环尚未被**端到端串起**。
其中 ShotSpec（A22）与 ConstraintPack（A23）已有逐域实现、端点与逐域用例，
但本文件没有把它们接进这条链；「尚未串起」不等于「尚未实现」——
把两者写混会让读者低估已交付面，也会让 Phase 1 验收缺口被错误定位
（见 ``contracts/runtime/action_implementations.yaml`` 的 not_implemented 台账）。
因此本文件覆盖的是 Phase 1 的**已实现段**，不宣称整条 Phase 1 主链已通。

角色取值说明
------------------------------------------------------------
契约 ``allowed_principals`` 是按 ``/`` 切分的角色集合（见
``action_registry.parse_allowed_principals``），括号内限定语会被剥离。这里
用契约里明确的角色名（制片 / 编剧 / 导演 / Worker / QC Worker），不猜映射。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from apps.api.main import app
from tests.conftest import TEST_TENANT_ID, make_test_token

# 主链全程在同一个组织内完成：组织维度是归属链的一环（tenant → org → project），
# 用无组织项目会让这条链少一段，测不出 SCOPE-3 的真实路径。
_ORG = uuid.uuid4()
_SHA = "e" * 64


@pytest.fixture(scope="module", autouse=True)
async def _organization(admin_sessionmaker) -> None:
    """播种组织行（复合外键要求组织真实存在且与项目同租户）。

    走**管理连接**：契约里没有 ``ORGANIZATION.*`` 动作，业务域角色对
    organizations 只有 SELECT（DATA-6），业务连接根本插不进去。组织是平台级
    归属根，由管理面创建；测试播种对应的是这条尚不存在的路径。管理连接不受
    RLS 约束，因此不需要注入作用域。
    """
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, :name, now(), 'tester', now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"org": _ORG, "tid": TEST_TENANT_ID, "name": f"主链组织-{_ORG.hex[:6]}"},
        )
        await session.commit()


def _actor(
    subject_id: str,
    roles: list[str],
    *,
    organization_id: uuid.UUID | None = _ORG,
) -> AsyncClient:
    """构造一个真实主体（真实 JWT + 真实 org claim + 真实 ASGI 端点）。"""
    token = make_test_token(
        subject_id=subject_id,
        roles=roles,
        organization_id=organization_id,
    )
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _create_project(client: AsyncClient) -> str:
    resp = await client.post(
        "/projects",
        json={
            "project_name": f"主链项目_{uuid.uuid4().hex[:8]}",
            "project_tier": "B",
            "organization_id": str(_ORG),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["entity_id"]


async def _create_episode(client: AsyncClient, project_id: str, ref: str) -> str:
    resp = await client.post(
        "/episodes",
        json={"project_id": project_id, "episode_ref": ref, "title": f"第{ref}集"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["entity_id"]


async def _import_script(client: AsyncClient, project_id: str, episode_ref: str) -> str:
    resp = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": episode_ref,
            "title": f"{episode_ref} 剧本",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["entity_id"]


async def _create_asset(client: AsyncClient, project_id: str) -> str:
    """登记资产：带权利证据（否则审批阶段的 RIGHTS_EVIDENCE 守卫必然拦住）。"""
    resp = await client.post(
        "/assets",
        json={
            "project_id": project_id,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": {"uri": f"s3://bucket/{uuid.uuid4().hex}.png"},
            "rights_manifest_ref": "rights-manifest-chain",
            "entity_id": f"asset_{uuid.uuid4().hex[:10]}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["asset_version_id"]


async def _report_qc(client: AsyncClient, asset_id: str) -> dict:
    resp = await client.post(
        f"/assets/{asset_id}/qc-reports",
        json={
            "quality_profile_version": "qp-1",
            "detector_versions": {"sharpness": "1.0"},
            "findings": [{"code": "SHARPNESS", "verdict": "PASS"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# 主链正向：Phase 1 已实现段的完整走通
# --------------------------------------------------------------------------- #
async def test_phase1_core_production_chain() -> None:
    """项目 → 集数 → 剧本 → 双人锁 → 资产 → QC → 审批 → GOLD 全链走通。

    这条用例的价值不在「都返回 200」，而在于每个 200 都伴随一个**状态真的变了**
    的断言：如果某一步只是返回了既有的缓存/未落库对象，后续步骤的守卫会失败。
    最后的读回用另一个主体、另一个会话，专门排除「响应里的幻觉」。
    """
    async with (
        _actor("producer_1", ["制片"]) as producer,
        _actor("writer_1", ["编剧"]) as writer,
        _actor("director_1", ["导演"]) as director,
        _actor("worker_1", ["Worker"]) as worker,
        _actor("qc_1", ["QC Worker"]) as qc,
    ):
        # 1. 项目创建：created_by 来自 JWT，组织归属由请求体给出并走同一套授权判定。
        created = await producer.post(
            "/projects",
            json={
                "project_name": f"主链项目_{uuid.uuid4().hex[:8]}",
                "project_tier": "B",
                "organization_id": str(_ORG),
            },
        )
        assert created.status_code == 201, created.text
        project = created.json()
        project_id = project["entity_id"]
        assert project["status"] == "DRAFT"
        assert project["created_by"] == "producer_1"
        # 内部归属键不下发：响应不携带归属拓扑，客户端只需 entity_id。
        assert "tenant_id" not in project

        # 2. 项目进入生产（状态机 DRAFT → ACTIVE）。
        activated = await producer.post(
            f"/projects/{project_id}/status", json={"target_status": "ACTIVE"}
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["status"] == "ACTIVE"

        # 3. 集数登记并推进（EPISODE.CREATE / EPISODE.STATUS.CHANGE）。
        episode_id = await _create_episode(writer, project_id, "EP01")
        episode_active = await writer.post(
            f"/episodes/{episode_id}/status", json={"target_status": "ACTIVE"}
        )
        assert episode_active.status_code == 200, episode_active.text
        assert episode_active.json()["status"] == "ACTIVE"

        # 4. 剧本导入：必须挂到真实项目 + 真实集数（悬空引用在服务层被拒）。
        script_id = await _import_script(writer, project_id, "EP01")
        script = (await writer.get(f"/scripts/{script_id}")).json()
        assert script["status"] == "DRAFT"
        assert script["created_by"] == "writer_1"

        # 5. 锁申请：requested_by 是当前主体，不是客户端字符串。
        requested = await writer.post(f"/scripts/{script_id}/lock/request")
        assert requested.status_code == 200, requested.text
        assert requested.json()["status"] == "LOCK_PENDING"
        assert requested.json()["requested_by"] == "writer_1"

        # 6. 换人批准：approved_by 来自 JWT，与申请人不同 → 职责分离成立。
        locked = await director.post(f"/scripts/{script_id}/lock/approve")
        assert locked.status_code == 200, locked.text
        assert locked.json()["status"] == "LOCKED"

        # 7. 资产登记：带 project_id 归属键，写入即授权（判定对象是父项目）。
        #    这里只断言创建响应，不读回——Worker 没有 ASSET.VERSION.READ 角色，
        #    让创建者读自己刚写的资产会 403。读回放在最后用制片完成。
        asset_id = await _create_asset(worker, project_id)
        created_asset = (await director.get(f"/assets/{asset_id}")).json()
        assert created_asset["status"] == "GENERATED"
        assert created_asset["created_by"] == "worker_1"

        # 8. 角色不符在进业务前就被拒：Worker 无 ASSET.VERSION.APPROVE 角色。
        denied = await worker.post(f"/assets/{asset_id}/approve")
        assert denied.status_code == 403
        assert denied.json()["error_code"] == "AUTH_SCOPE_DENIED"

        # 9. 有角色但无 QC 证据同样被拒：守卫挂在边上，不在用例方法里。
        #    这一步是 P1-02 的关键——审批门槛是边的属性，两条路径共用。
        no_evidence = await director.post(f"/assets/{asset_id}/approve")
        assert no_evidence.status_code == 409
        assert no_evidence.json()["error_code"] == "STATE_TRANSITION_DENIED"
        assert "QC" in no_evidence.json()["message"]

        # 10. QC 证据登记：首份报告把资产推入 TECHNICAL_QC（该边 owner 是 QC.REPORT.CREATE）。
        reported = await _report_qc(qc, asset_id)
        assert reported["asset_version"]["status"] == "TECHNICAL_QC"

        # 11. 审批：approve 沿「下一条归我所有的边」逐跳推进，
        #     一次调用完成 TECHNICAL_QC → HUMAN_REVIEW → APPROVED。
        approved = await director.post(f"/assets/{asset_id}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "APPROVED"

        # 12. 入受控资产库（APPROVED → GOLD，owner 是 ASSET.STATUS.CHANGE）。
        gold = await director.post(f"/assets/{asset_id}/status", json={"target_status": "GOLD"})
        assert gold.status_code == 200, gold.text
        assert gold.json()["status"] == "GOLD"

        # 13. 终态不可原地复活：GOLD 无出边。
        revived = await director.post(f"/assets/{asset_id}/status", json={"target_status": "GOLD"})
        assert revived.status_code == 409
        assert revived.json()["error_code"] == "STATE_TRANSITION_DENIED"

        # 14. 读回核对：换主体、走只读事务，证明状态是落库的而非响应里的幻觉。
        readback = await producer.get(f"/assets/{asset_id}")
        assert readback.status_code == 200, readback.text
        assert readback.json()["status"] == "GOLD"


# --------------------------------------------------------------------------- #
# 链中间的三类拦截
# --------------------------------------------------------------------------- #
async def test_script_lock_cannot_self_approve() -> None:
    """同一主体既申请又批准 → 拒绝。

    为什么不用「角色不足」来做这条负向：那样只能证明 RBAC 生效，证明不了职责
    分离。这里刻意给主体同时具备编剧与导演角色——授权层面完全够格，唯一挡住
    它的是「申请人与批准人不得为同一主体」这条红线。
    """
    async with (
        _actor("producer_2", ["制片"]) as producer,
        _actor("dual_role_1", ["编剧", "导演"]) as dual_role,
    ):
        project_id = await _create_project(producer)
        await _create_episode(dual_role, project_id, "EP01")
        script_id = await _import_script(dual_role, project_id, "EP01")

        requested = await dual_role.post(f"/scripts/{script_id}/lock/request")
        assert requested.status_code == 200, requested.text
        assert requested.json()["requested_by"] == "dual_role_1"

        self_approved = await dual_role.post(f"/scripts/{script_id}/lock/approve")
        assert self_approved.status_code == 409
        assert self_approved.json()["error_code"] == "STATE_TRANSITION_DENIED"
        assert "同一主体" in self_approved.json()["message"]

        # 换人后立刻成功：证明上一步的拒绝来自主体相同，不是别的原因。
        async with _actor("director_2", ["导演"]) as director:
            approved = await director.post(f"/scripts/{script_id}/lock/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "LOCKED"


async def test_change_status_cannot_reach_approved() -> None:
    """通用状态接口不能抢占别的动作拥有的边（P1-02 的结构性修法）。

    这里必须选一条**真实存在、但 owner 是别的动作**的边，才能证明「所有权」
    约束；选一条不存在的边（如 TECHNICAL_QC 一步跳到 APPROVED）只会得到
    「未登记」——那是状态机覆盖度问题，不是所有权问题。

    GENERATED → TECHNICAL_QC 的 owner 是 ``QC.REPORT.CREATE``：它存在、合法，
    但对 ``ASSET.STATUS.CHANGE`` 不可达。于是「用通用接口跳过 QC 报告直接进
    检测态」这条路在结构上被堵死，而不是靠某人记得加 if。
    """
    async with (
        _actor("producer_3", ["制片"]) as producer,
        _actor("worker_3", ["Worker"]) as worker,
        _actor("qc_3", ["QC Worker"]) as qc,
        _actor("director_3", ["导演"]) as director,
    ):
        project_id = await _create_project(producer)
        asset_id = await _create_asset(worker, project_id)

        # 所有权证明：这条边归 QC.REPORT.CREATE，通用状态接口不得推进。
        stolen_edge = await director.post(
            f"/assets/{asset_id}/status", json={"target_status": "TECHNICAL_QC"}
        )
        assert stolen_edge.status_code == 409
        assert stolen_edge.json()["error_code"] == "STATE_TRANSITION_DENIED"
        # 错误语义要指名「这条边归谁」，否则排障时会误以为是状态机漏登记，
        # 下一个人「修」的方向就是把 owner 约束删掉。
        assert "QC.REPORT.CREATE" in stolen_edge.json()["message"]

        # 结构不可达：APPROVED 需要两跳（TECHNICAL_QC → HUMAN_REVIEW → APPROVED），
        # 一步跳到它连边都不存在——即使证据齐备、角色够格也走不通。
        jump = await director.post(f"/assets/{asset_id}/status", json={"target_status": "APPROVED"})
        assert jump.status_code == 409
        assert jump.json()["error_code"] == "STATE_TRANSITION_DENIED"
        assert "未登记" in jump.json()["message"]

        # 对照：按所有者走就通——QC 报告推进 TECHNICAL_QC，专用审批动作推进 APPROVED。
        await _report_qc(qc, asset_id)
        approved = await director.post(f"/assets/{asset_id}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "APPROVED"
