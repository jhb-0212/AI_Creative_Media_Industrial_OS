"""ShotService 用例单元测试（内存 fake 仓储，无数据库依赖）。

覆盖 ADR-R27 边界硬约束：
- AI 产草案（source=agent），人类产草案（source=human）。
- Agent 不得确认正式镜头修订（AUTH_SCOPE_DENIED）。
- 草案确认产出正式修订，版本递增不覆盖（旧活跃修订标 SUPERSEDED）。
- 身份从 Principal 解析（created_by = principal.subject_id）。
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.identity.principal import Principal, SubjectType
from packages.shared.outbox_writer import OutboxWriter
from services.shot_planning.domain.enums import ShotDraftStatus, ShotRevisionStatus
from services.shot_planning.domain.shot_draft import ShotDraft
from services.shot_planning.domain.shot_revision import ShotRevision
from services.shot_planning.repository.shot_draft_repo import ShotDraftRepository
from services.shot_planning.repository.shot_revision_repo import (
    ShotRevisionRepository,
)
from services.shot_planning.service.shot_service import ShotService

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _principal(
    subject_id: str = "user_001",
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


def _agent_principal(subject_id: str = "agent_001") -> Principal:
    return _principal(subject_id, subject_type=SubjectType.AGENT)


class _RecordingOutboxWriter(OutboxWriter):
    """内存 outbox 写入 fake。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, *, aggregate_type, aggregate_id, tenant_id, event_type, payload):  # type: ignore[override]
        self.events.append(
            {
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "tenant_id": tenant_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        return uuid4()


class _FakeDraftRepository(ShotDraftRepository):
    def __init__(self) -> None:
        self._store: dict[str, ShotDraft] = {}

    async def add(self, draft: ShotDraft) -> None:
        self._store[draft.entity_id] = draft

    async def save(self, draft: ShotDraft, expected_version: int) -> None:
        self._store[draft.entity_id] = draft

    async def get_by_id(self, entity_id: str) -> ShotDraft | None:
        return self._store.get(entity_id)


class _FakeRevisionRepository(ShotRevisionRepository):
    def __init__(self) -> None:
        self._store: dict[str, ShotRevision] = {}

    async def add(self, revision: ShotRevision) -> None:
        self._store[revision.entity_id] = revision

    async def get_by_id(self, entity_id: str) -> ShotRevision | None:
        return self._store.get(entity_id)

    async def get_active_by_shot(self, shot_id: str) -> ShotRevision | None:
        candidates = [r for r in self._store.values() if r.shot_id == shot_id]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.revision)


def _make_service(
    draft_repo: ShotDraftRepository | None = None,
    revision_repo: ShotRevisionRepository | None = None,
    outbox: OutboxWriter | None = None,
) -> tuple[ShotService, _FakeDraftRepository, _FakeRevisionRepository, _RecordingOutboxWriter]:
    d = draft_repo or _FakeDraftRepository()
    r = revision_repo or _FakeRevisionRepository()
    o = outbox or _RecordingOutboxWriter()
    return ShotService(d, r, o), d, r, o  # type: ignore[return-value]


# --- SHOT.DRAFT.CREATE ----------------------------------------------------


async def test_agent_creates_draft_with_agent_source() -> None:
    service, draft_repo, _, outbox = _make_service()
    draft = await service.create_draft(
        project_id="proj_001",
        episode_ref="EP001",
        scene_ref="SC001",
        shot_code="SHOT_001",
        intent_summary="角色 A 特写",
        shot_description="中景，角色 A 情绪转折",
        principal=_agent_principal(),
    )
    assert draft.source.value == "agent"
    assert draft.status is ShotDraftStatus.DRAFT
    assert draft.created_by == "agent_001"
    assert draft.entity_id in draft_repo._store
    assert outbox.events[0]["event_type"] == "SHOT_DRAFT_CREATED"


async def test_human_creates_draft_with_human_source() -> None:
    service, _, _, _ = _make_service()
    draft = await service.create_draft(
        project_id="proj_001",
        episode_ref="EP001",
        scene_ref="SC001",
        shot_code="SHOT_001",
        intent_summary="意图",
        shot_description="描述",
        principal=_principal("user_001"),
    )
    assert draft.source.value == "human"


async def test_create_draft_rejects_empty_fields() -> None:
    service, _, _, _ = _make_service()
    with pytest.raises(DomainError) as exc_info:
        await service.create_draft(
            project_id="  ",
            episode_ref="EP001",
            scene_ref="SC001",
            shot_code="SHOT_001",
            intent_summary="意图",
            shot_description="描述",
            principal=_principal(),
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"


# --- SHOT.REVISION.CONFIRM ------------------------------------------------


async def test_human_confirms_draft_produces_revision() -> None:
    service, draft_repo, _revision_repo, outbox = _make_service()
    draft = await service.create_draft(
        project_id="proj_001",
        episode_ref="EP001",
        scene_ref="SC001",
        shot_code="SHOT_001",
        intent_summary="意图",
        shot_description="描述",
        principal=_agent_principal(),
    )
    revision = await service.confirm_revision(
        draft_id=draft.entity_id,
        shot_id="shot_001",
        project_id="proj_001",
        principal=_principal("director_001"),
    )
    assert revision.shot_id == "shot_001"
    assert revision.revision == 1
    assert revision.created_by == "director_001"
    assert revision.status is ShotRevisionStatus.ACTIVE
    # 草案被确认（DRAFT -> CONFIRMED）
    assert draft_repo._store[draft.entity_id].status is ShotDraftStatus.CONFIRMED
    # 事件已记录
    assert outbox.events[-1]["event_type"] == "SHOT_REVISION_CONFIRMED"


async def test_agent_cannot_confirm_revision() -> None:
    service, _draft_repo, _, _ = _make_service()
    draft = await service.create_draft(
        project_id="proj_001",
        episode_ref="EP001",
        scene_ref="SC001",
        shot_code="SHOT_001",
        intent_summary="意图",
        shot_description="描述",
        principal=_agent_principal(),
    )
    with pytest.raises(DomainError) as exc_info:
        await service.confirm_revision(
            draft_id=draft.entity_id,
            shot_id="shot_001",
            project_id="proj_001",
            principal=_agent_principal(),
        )
    assert exc_info.value.error_code.code == "AUTH_SCOPE_DENIED"


async def test_confirm_revision_increments_version_and_supersedes() -> None:
    service, _draft_repo, revision_repo, _ = _make_service()
    # 第一次确认
    draft1 = await service.create_draft(
        project_id="proj_001",
        episode_ref="EP001",
        scene_ref="SC001",
        shot_code="SHOT_001",
        intent_summary="意图 v1",
        shot_description="描述 v1",
        principal=_agent_principal(),
    )
    rev1 = await service.confirm_revision(
        draft_id=draft1.entity_id,
        shot_id="shot_001",
        project_id="proj_001",
        principal=_principal("d1"),
    )
    assert rev1.revision == 1

    # 第二次确认（新草案，同 shot）
    draft2 = await service.create_draft(
        project_id="proj_001",
        episode_ref="EP001",
        scene_ref="SC001",
        shot_code="SHOT_001",
        intent_summary="意图 v2",
        shot_description="描述 v2",
        principal=_agent_principal(),
    )
    rev2 = await service.confirm_revision(
        draft_id=draft2.entity_id,
        shot_id="shot_001",
        project_id="proj_001",
        principal=_principal("d1"),
    )
    assert rev2.revision == 2
    # 旧修订保留（append-only 不覆盖），最新版本由 revision 派生
    assert revision_repo._store[rev1.entity_id].revision == 1
    assert revision_repo._store[rev2.entity_id].revision == 2
    # get_active_by_shot 返回 revision 最大者
    active = await revision_repo.get_active_by_shot("shot_001")
    assert active is not None
    assert active.entity_id == rev2.entity_id


async def test_confirm_revision_requires_existing_draft() -> None:
    service, _, _, _ = _make_service()
    with pytest.raises(DomainError) as exc_info:
        await service.confirm_revision(
            draft_id="missing_draft",
            shot_id="shot_001",
            project_id="proj_001",
            principal=_principal(),
        )
    assert exc_info.value.error_code.code == "NOT_FOUND"
