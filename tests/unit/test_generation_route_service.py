"""RouteService 用例单元测试（内存 fake 仓储，无数据库依赖）。

覆盖（PRODUCTION.ROUTE.SELECT / ACT-ROUTE-NEG-001 / C4 单一路线生成）：
- select_route：VERIFIED 路线选中、写下生效路线并发出 PRODUCTION_ROUTE_SELECTED；
- UNVERIFIED 路线拒绝（未验证能力不得进入生产路由）；
- 幽灵路线 404（NOT_FOUND）；
- 必填上下文缺失拒绝（route_id/project_id/shot_id）；
- 同路线重复选择幂等（不新增记录、不发第二条事件）；
- 不同路线拒绝（ROUTE_ALREADY_SELECTED + 结构化 details）；
- **并发裁决**：唯一约束先于本请求落下（add 抛 DuplicateEntityError），
  按"谁是生效路线"给出幂等或拒绝，而不是把冲突当成功。
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal, SubjectType
from packages.shared.outbox_writer import OutboxWriter
from services.generation_execution.domain.enums import ProductionRouteStatus
from services.generation_execution.domain.ports import ShotFacts
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.domain.shot_route_selection import (
    ShotRouteSelection,
)
from services.generation_execution.repository.production_route_repo import (
    ProductionRouteRepository,
)
from services.generation_execution.repository.shot_route_selection_repo import (
    ShotRouteSelectionRepository,
)
from services.generation_execution.service.route_service import RouteService

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _principal(
    subject_id: str = "producer_001",
    subject_type: SubjectType = SubjectType.HUMAN,
) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        subject_type=subject_type,
        roles=frozenset({"制片"}),
        project_ids=frozenset({"*"}),
    )


class _RecordingOutboxWriter(OutboxWriter):
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, *, aggregate_type, aggregate_id, tenant_id, event_type, payload):  # type: ignore[override]
        self.events.append(
            {
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        return uuid4()


class _FakeRouteRepository(ProductionRouteRepository):
    def __init__(self) -> None:
        self._store: dict[str, ProductionRoute] = {}

    def seed(self, route: ProductionRoute) -> None:
        self._store[route.entity_id] = route

    async def add(self, route: ProductionRoute) -> None:
        self._store[route.entity_id] = route

    async def get_by_id(self, entity_id: str) -> ProductionRoute | None:
        return self._store.get(entity_id)


class _FakeSelectionRepository(ShotRouteSelectionRepository):
    """模拟数据库唯一约束：同 (project, shot) 第二次插入必抛 DuplicateEntityError。"""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], ShotRouteSelection] = {}
        #: 置 True 时下一次 add 先"被别的并发请求抢先"再抛冲突（竞态路径）。
        self.race_with: ShotRouteSelection | None = None

    async def add(self, selection: ShotRouteSelection) -> None:
        if self.race_with is not None:
            raced = self.race_with
            self.race_with = None
            self._store[(raced.project_id, raced.shot_id)] = raced
            raise DuplicateEntityError(entity=f"{selection.project_id}/{selection.shot_id}")
        key = (selection.project_id, selection.shot_id)
        if key in self._store:
            raise DuplicateEntityError(entity=f"{selection.project_id}/{selection.shot_id}")
        self._store[key] = selection

    async def get_effective(
        self, *, project_id: str, shot_id: str
    ) -> ShotRouteSelection | None:
        return self._store.get((project_id, shot_id))

    def seed(self, selection: ShotRouteSelection) -> None:
        self._store[(selection.project_id, selection.shot_id)] = selection


class _FakeShotOwnership:
    """镜头归属 fake：默认"本租户内任意 shot 都存在于 proj_001"。

    默认放行是为了让既有用例只关心选路语义；镜头级拒绝由
    ``TestShotExistence`` 显式构造（``ghost`` / 指定项目）。
    """

    def __init__(self, project_id: str = "proj_001") -> None:
        self._project_id = project_id
        self._overrides: dict[str, ShotFacts] = {}
        self.missing: set[str] = set()

    def bind(self, shot_id: str, *, project_id: str) -> None:
        self._overrides[shot_id] = ShotFacts(shot_id=shot_id, project_id=project_id)

    def mark_missing(self, shot_id: str) -> None:
        self.missing.add(shot_id)

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        shot_id: str,
    ) -> ShotFacts | None:
        if shot_id in self.missing:
            return None
        if shot_id in self._overrides:
            return self._overrides[shot_id]
        return ShotFacts(shot_id=shot_id, project_id=self._project_id)


def _service(
    store: _FakeRouteRepository | None = None,
    shots: _FakeShotOwnership | None = None,
) -> tuple[RouteService, _FakeRouteRepository, _FakeSelectionRepository, _RecordingOutboxWriter]:
    repo = store or _FakeRouteRepository()
    selections = _FakeSelectionRepository()
    outbox = _RecordingOutboxWriter()
    port = shots or _FakeShotOwnership()
    return (
        RouteService(repo, selections, outbox, port),
        repo,
        selections,
        outbox,
    )


def _seed_route(
    repo: _FakeRouteRepository,
    *,
    route_id: str = "route_001",
    status: ProductionRouteStatus = ProductionRouteStatus.VERIFIED,
) -> ProductionRoute:
    route = ProductionRoute(
        entity_id=route_id,
        route_type="blender_scripted",
        supported_control_signals=["camera_path"],
        required_appearance_refs=[],
        qc_dimensions=["geometry_validity"],
        capability_refs=["evidence_pkg_001"] if status is ProductionRouteStatus.VERIFIED else [],
        status=status,
        created_by="ops_seed",
        tenant_id=_TENANT,
    )
    repo.seed(route)
    return route


def _seed_selection(
    selections: _FakeSelectionRepository,
    *,
    route_id: str,
    project_id: str = "proj_001",
    shot_id: str = "SHOT_001",
) -> ShotRouteSelection:
    selection = ShotRouteSelection(
        entity_id=f"srs_{uuid.uuid4().hex[:8]}",
        project_id=project_id,
        shot_id=shot_id,
        route_id=route_id,
        created_by="producer_000",
        tenant_id=_TENANT,
    )
    selections.seed(selection)
    return selection


class TestSelectRoute:
    @pytest.mark.asyncio
    async def test_verified_route_selected_and_effective_route_written(self) -> None:
        service, repo, selections, outbox = _service()
        route = _seed_route(repo, status=ProductionRouteStatus.VERIFIED)

        result = await service.select_route(
            route_id=route.entity_id,
            project_id="proj_001",
            shot_id="SHOT_001",
            principal=_principal(),
        )

        assert result.created is True
        assert result.route.entity_id == route.entity_id
        # 选中不改变路线状态：选择是"使用事实"，路线状态是"能力事实"。
        assert result.route.status is ProductionRouteStatus.VERIFIED
        # 生效路线确实写下了（C4 的载体，不是"只发了个事件"）。
        stored = await selections.get_effective(project_id="proj_001", shot_id="SHOT_001")
        assert stored is not None
        assert stored.route_id == route.entity_id
        assert stored.entity_id == result.selection.entity_id
        assert stored.entity_id.startswith("srs_")

        assert len(outbox.events) == 1
        event = outbox.events[0]
        assert event["event_type"] == "PRODUCTION_ROUTE_SELECTED"
        # 事件聚合体与契约同口径（动作矩阵的资源类型是 ProductionRoute）。
        assert event["aggregate_type"] == "ProductionRoute"
        assert event["aggregate_id"] == route.entity_id
        payload = event["payload"]
        for key in (
            "selection_id",
            "route_id",
            "route_type",
            "project_id",
            "shot_id",
            "selected_by",
            "tenant_id",
            "status",
        ):
            assert key in payload, f"审计事件缺契约登记字段: {key}"
        assert payload["route_id"] == route.entity_id
        assert payload["selection_id"] == result.selection.entity_id
        assert payload["selected_by"] == "producer_001"

    @pytest.mark.asyncio
    async def test_same_route_reselection_is_idempotent(self) -> None:
        """同一路线重复选择：返回既有记录，不新增、不发第二条事件（D3）。"""
        service, repo, selections, outbox = _service()
        route = _seed_route(repo)
        existing = _seed_selection(selections, route_id=route.entity_id)

        result = await service.select_route(
            route_id=route.entity_id,
            project_id="proj_001",
            shot_id="SHOT_001",
            principal=_principal(),
        )

        assert result.created is False
        assert result.selection.entity_id == existing.entity_id
        assert outbox.events == [], "幂等重放不得产生第二条审计事实"

    @pytest.mark.asyncio
    async def test_second_route_rejected_with_structured_details(self) -> None:
        """C4：已有生效路线时选另一条路线 → 409 ROUTE_ALREADY_SELECTED。"""
        service, repo, selections, outbox = _service()
        first = _seed_route(repo, route_id="route_first")
        second = _seed_route(repo, route_id="route_second")
        existing = _seed_selection(selections, route_id=first.entity_id)

        with pytest.raises(DomainError) as exc_info:
            await service.select_route(
                route_id=second.entity_id,
                project_id="proj_001",
                shot_id="SHOT_001",
                principal=_principal(),
            )
        err = exc_info.value
        assert err.error_code.code == "ROUTE_ALREADY_SELECTED"
        assert err.error_code.http_status == 409
        # 结构化细节：调用方要能据此知道"被哪条路线挡住了"。
        assert err.context["effective_route_id"] == first.entity_id
        assert err.context["effective_selection_id"] == existing.entity_id
        assert err.context["requested_route_id"] == second.entity_id
        assert err.context["shot_id"] == "SHOT_001"
        assert outbox.events == [], "拒绝路径不得发出审计事件"

    @pytest.mark.asyncio
    async def test_concurrent_same_route_is_idempotent(self) -> None:
        """并发同一路线：唯一约束先落下，本请求回查后按幂等返回。"""
        service, repo, selections, outbox = _service()
        route = _seed_route(repo)
        selections.race_with = ShotRouteSelection(
            entity_id="srs_winner",
            project_id="proj_001",
            shot_id="SHOT_001",
            route_id=route.entity_id,
            created_by="producer_999",
            tenant_id=_TENANT,
        )

        result = await service.select_route(
            route_id=route.entity_id,
            project_id="proj_001",
            shot_id="SHOT_001",
            principal=_principal(),
        )

        assert result.created is False
        assert result.selection.entity_id == "srs_winner"
        assert outbox.events == [], "输掉竞争的一方不得补发审计事实"

    @pytest.mark.asyncio
    async def test_concurrent_different_route_rejected(self) -> None:
        """并发两条路线：唯一约束裁决，输家得到 409 而不是"看起来成功"。"""
        service, repo, selections, outbox = _service()
        winner_route = _seed_route(repo, route_id="route_winner")
        loser_route = _seed_route(repo, route_id="route_loser")
        selections.race_with = ShotRouteSelection(
            entity_id="srs_winner",
            project_id="proj_001",
            shot_id="SHOT_001",
            route_id=winner_route.entity_id,
            created_by="producer_999",
            tenant_id=_TENANT,
        )

        with pytest.raises(DomainError) as exc_info:
            await service.select_route(
                route_id=loser_route.entity_id,
                project_id="proj_001",
                shot_id="SHOT_001",
                principal=_principal(),
            )
        assert exc_info.value.error_code.code == "ROUTE_ALREADY_SELECTED"
        assert exc_info.value.context["effective_route_id"] == winner_route.entity_id
        assert outbox.events == []

    @pytest.mark.asyncio
    async def test_unverified_route_rejected(self) -> None:
        """ACT-ROUTE-NEG-001：UNVERIFIED 路线拒绝。"""
        service, repo, _selections, outbox = _service()
        route = _seed_route(repo, status=ProductionRouteStatus.UNVERIFIED)

        with pytest.raises(DomainError) as exc_info:
            await service.select_route(
                route_id=route.entity_id,
                project_id="proj_001",
                shot_id="SHOT_001",
                principal=_principal(),
            )
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"
        assert outbox.events == [], "拒绝路径不得发出审计事件"

    @pytest.mark.asyncio
    async def test_ghost_route_not_found(self) -> None:
        service, _repo, _selections, outbox = _service()
        with pytest.raises(DomainError) as exc_info:
            await service.select_route(
                route_id="route_missing",
                project_id="proj_001",
                shot_id="SHOT_001",
                principal=_principal(),
            )
        assert exc_info.value.error_code.code == "NOT_FOUND"
        assert outbox.events == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["route_id", "project_id", "shot_id"])
    async def test_missing_context_rejected(self, field: str) -> None:
        service, _repo, _selections, _outbox = _service()
        kwargs: dict[str, str] = {
            "route_id": "route_001",
            "project_id": "proj_001",
            "shot_id": "SHOT_001",
        }
        kwargs[field] = "  "
        with pytest.raises(DomainError) as exc_info:
            await service.select_route(principal=_principal(), **kwargs)
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"


class TestShotExistence:
    """选路不得落在不存在的镜头上（P1-1）。

    没有这条校验时，任意字符串都能登记生效路线：唯一键被一个从未存在的镜头
    占用，后续"该镜头已选路"的判断全部建立在虚空上。
    """

    @pytest.mark.asyncio
    async def test_ghost_shot_not_found(self) -> None:
        shots = _FakeShotOwnership()
        shots.mark_missing("SHOT_GHOST")
        service, repo, _selections, outbox = _service(shots=shots)
        _seed_route(repo)

        with pytest.raises(DomainError) as exc_info:
            await service.select_route(
                route_id="route_001",
                project_id="proj_001",
                shot_id="SHOT_GHOST",
                principal=_principal(),
            )
        err = exc_info.value
        assert err.error_code.code == "NOT_FOUND"
        assert "镜头不存在" in err.detail
        assert outbox.events == [], "拒绝路径不得发出审计事件"

    @pytest.mark.asyncio
    async def test_shot_from_other_project_rejected(self) -> None:
        """镜头存在但归属别的项目 → 422（不是 404：不是"没有"，是"不是你的"）。"""
        shots = _FakeShotOwnership()
        shots.bind("SHOT_999", project_id="proj_other")
        service, repo, _selections, outbox = _service(shots=shots)
        _seed_route(repo)

        with pytest.raises(DomainError) as exc_info:
            await service.select_route(
                route_id="route_001",
                project_id="proj_001",
                shot_id="SHOT_999",
                principal=_principal(),
            )
        err = exc_info.value
        assert err.error_code.code == "VALIDATION_ERROR"
        assert "归属项目 proj_other" in err.detail
        assert outbox.events == []
