"""ShotSpecService 用例单元测试（内存 fake 仓储，无数据库依赖）。

覆盖：
- create_spec：绑定修订必须存在；Agent 拒绝（C1：未确认不得自动推进）；
- lock_spec（SHOTSPEC.LOCK）：Agent 拒绝、规格不存在、跨项目拒绝、
  版本不匹配拒绝（CONCURRENT_MODIFICATION）、锁定成功发审计事件。
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.identity.principal import Principal, SubjectType
from packages.shared.outbox_writer import OutboxWriter
from services.shot_planning.domain.shot_revision import ShotRevision
from services.shot_planning.domain.shot_spec import ShotSpec
from services.shot_planning.repository.shot_revision_repo import (
    ShotRevisionRepository,
)
from services.shot_planning.repository.shot_spec_repo import ShotSpecRepository
from services.shot_planning.service.spec_service import ShotSpecService

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _principal(
    subject_id: str = "director_001",
    subject_type: SubjectType = SubjectType.HUMAN,
) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        subject_type=subject_type,
        roles=frozenset({"导演"}),
        project_ids=frozenset({"*"}),
    )


class _RecordingOutboxWriter(OutboxWriter):
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, *, aggregate_type, aggregate_id, tenant_id, event_type, payload):  # type: ignore[override]
        self.events.append({"event_type": event_type, "payload": payload})
        return uuid4()


class _FakeSpecRepository(ShotSpecRepository):
    def __init__(self) -> None:
        self._store: dict[str, ShotSpec] = {}

    async def add(self, spec: ShotSpec) -> None:
        self._store[spec.entity_id] = spec

    async def save(self, spec: ShotSpec, expected_version: int) -> None:
        # 聚合根原地变更：save 收到的 expected_version 是读取时版本，
        # 变更后应恰好推进 1（乐观锁口径由真实仓储的条件 UPDATE 保证）。
        assert spec.entity_version == expected_version + 1, (
            "版本推进必须恰好 +1（乐观锁口径）"
        )
        self._store[spec.entity_id] = spec

    async def get_by_id(self, entity_id: str) -> ShotSpec | None:
        return self._store.get(entity_id)


class _FakeRevisionRepository(ShotRevisionRepository):
    def __init__(self) -> None:
        self._store: dict[str, ShotRevision] = {}

    def seed(self, revision: ShotRevision) -> None:
        self._store[revision.entity_id] = revision

    async def add(self, revision: ShotRevision) -> None:
        self._store[revision.entity_id] = revision

    async def get_by_id(self, entity_id: str) -> ShotRevision | None:
        return self._store.get(entity_id)

    async def get_active_by_shot(self, shot_id: str) -> ShotRevision | None:
        candidates = [r for r in self._store.values() if r.shot_id == shot_id]
        return max(candidates, key=lambda r: r.revision) if candidates else None


def _seed_revision(repo: _FakeRevisionRepository) -> ShotRevision:
    revision = ShotRevision(
        entity_id=uuid.uuid4().hex,
        shot_id="shot_001",
        project_id="proj_001",
        episode_ref="EP001",
        scene_ref="SC001",
        intent_summary="意图",
        shot_description="描述",
        revision=1,
        created_by="director_001",
        tenant_id=_TENANT,
    )
    repo.seed(revision)
    return revision


def _make_service() -> tuple[
    ShotSpecService, _FakeSpecRepository, _FakeRevisionRepository, _RecordingOutboxWriter
]:
    spec_repo = _FakeSpecRepository()
    revision_repo = _FakeRevisionRepository()
    outbox = _RecordingOutboxWriter()
    return (
        ShotSpecService(spec_repo, revision_repo, outbox),
        spec_repo,
        revision_repo,
        outbox,
    )


async def test_create_spec_binds_confirmed_revision() -> None:
    """规格必须绑定已存在的正式修订（C1：未确认不得自动推进）。"""
    service, _, revision_repo, outbox = _make_service()
    revision = _seed_revision(revision_repo)

    spec = await service.create_spec(
        shot_revision_id=revision.entity_id,
        duration_ms=4200,
        aspect_ratio="16:9",
        fps="24/1",
        coordinate_system="RIGHT_HANDED/Y/METER",
        camera_path={"type": "DOLLY", "keyframes": []},
        cut_points_frames=[0, 48, 100],
        principal=_principal(),
    )
    assert spec.shot_id == "shot_001"
    assert spec.project_id == "proj_001"
    assert spec.status.value == "ACTIVE"
    assert spec.locked_dimensions == []
    # 创建路径无冻结审计事件（action_registry 未登记 SHOTSPEC.CREATE）。
    assert outbox.events == []


async def test_create_spec_missing_revision_denied() -> None:
    """绑定的修订不存在 → NOT_FOUND（规格是修订的派生结构化描述）。"""
    service, *_ = _make_service()
    with pytest.raises(DomainError) as exc_info:
        await service.create_spec(
            shot_revision_id="missing",
            duration_ms=4200,
            aspect_ratio="16:9",
            fps="24/1",
            coordinate_system="RIGHT_HANDED/Y/METER",
            camera_path={"type": "STATIC"},
            principal=_principal(),
        )
    assert exc_info.value.error_code.code == "NOT_FOUND"


async def test_create_spec_agent_denied() -> None:
    """Agent 创建规格拒绝（规格仅由授权人类工作流建立）。"""
    service, *_ = _make_service()
    with pytest.raises(DomainError) as exc_info:
        await service.create_spec(
            shot_revision_id="rev_x",
            duration_ms=4200,
            aspect_ratio="16:9",
            fps="24/1",
            coordinate_system="RIGHT_HANDED/Y/METER",
            camera_path={"type": "STATIC"},
            principal=_principal("agent_001", SubjectType.AGENT),
        )
    assert exc_info.value.error_code.code == "AUTH_SCOPE_DENIED"


async def test_lock_spec_success_emits_audit_event() -> None:
    """锁定成功：维度/依据/版本齐备，发出 SHOTSPEC_LOCKED 审计事件。"""
    service, _, revision_repo, outbox = _make_service()
    revision = _seed_revision(revision_repo)
    spec = await service.create_spec(
        shot_revision_id=revision.entity_id,
        duration_ms=4200,
        aspect_ratio="16:9",
        fps="24/1",
        coordinate_system="RIGHT_HANDED/Y/METER",
        camera_path={"type": "DOLLY", "keyframes": []},
        principal=_principal(),
    )

    locked = await service.lock_spec(
        spec_id=spec.entity_id,
        project_id="proj_001",
        dimensions=["camera_path", "cut_points"],
        rationale="运镜与切点经导演确认",
        version=1,
        principal=_principal(),
    )
    assert locked.locked_dimensions == ["camera_path", "cut_points"]
    assert locked.locked_by == "director_001"
    assert locked.entity_version == 2
    assert len(outbox.events) == 1
    event = outbox.events[0]
    assert event["event_type"] == "SHOTSPEC_LOCKED"
    assert event["payload"]["shot_spec_id"] == spec.entity_id
    assert event["payload"]["locked_dimensions"] == ["camera_path", "cut_points"]


async def test_lock_spec_version_mismatch_denied() -> None:
    """锁定必须基于最新版本（版本齐备）：版本不匹配 → CONCURRENT_MODIFICATION。"""
    service, _, revision_repo, _ = _make_service()
    revision = _seed_revision(revision_repo)
    spec = await service.create_spec(
        shot_revision_id=revision.entity_id,
        duration_ms=4200,
        aspect_ratio="16:9",
        fps="24/1",
        coordinate_system="RIGHT_HANDED/Y/METER",
        camera_path={"type": "STATIC"},
        principal=_principal(),
    )
    with pytest.raises(DomainError) as exc_info:
        await service.lock_spec(
            spec_id=spec.entity_id,
            project_id="proj_001",
            dimensions=["camera_path"],
            rationale="理由",
            version=99,
            principal=_principal(),
        )
    assert exc_info.value.error_code.code == "CONCURRENT_MODIFICATION"


async def test_lock_spec_cross_project_denied() -> None:
    """跨项目锁定拒绝（防跨项目挂载）。"""
    service, _, revision_repo, _ = _make_service()
    revision = _seed_revision(revision_repo)
    spec = await service.create_spec(
        shot_revision_id=revision.entity_id,
        duration_ms=4200,
        aspect_ratio="16:9",
        fps="24/1",
        coordinate_system="RIGHT_HANDED/Y/METER",
        camera_path={"type": "STATIC"},
        principal=_principal(),
    )
    with pytest.raises(DomainError) as exc_info:
        await service.lock_spec(
            spec_id=spec.entity_id,
            project_id="proj_other",
            dimensions=["camera_path"],
            rationale="理由",
            version=1,
            principal=_principal(),
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"


async def test_lock_spec_agent_denied() -> None:
    """Agent 锁定规格拒绝（仅导演/授权 TD）。"""
    service, _, revision_repo, _ = _make_service()
    revision = _seed_revision(revision_repo)
    spec = await service.create_spec(
        shot_revision_id=revision.entity_id,
        duration_ms=4200,
        aspect_ratio="16:9",
        fps="24/1",
        coordinate_system="RIGHT_HANDED/Y/METER",
        camera_path={"type": "STATIC"},
        principal=_principal(),
    )
    with pytest.raises(DomainError) as exc_info:
        await service.lock_spec(
            spec_id=spec.entity_id,
            project_id="proj_001",
            dimensions=["camera_path"],
            rationale="理由",
            version=1,
            principal=_principal("agent_001", SubjectType.AGENT),
        )
    assert exc_info.value.error_code.code == "AUTH_SCOPE_DENIED"
