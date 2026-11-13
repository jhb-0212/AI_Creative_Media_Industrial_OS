"""ScriptService 输入归一化（strip）与空值校验的单元测试（M2/L3）。

使用内存 fake 仓储，验证：
- project_id / episode_ref / title / created_by 首尾空格在查询与入库前统一 strip。
- 空值（纯空格）字段在服务层被拒绝（VALIDATION_ERROR）。
"""

from __future__ import annotations

import pytest

from packages.errors.error_codes import DomainError
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.repository.script_repo import ScriptRepository
from services.project_planning.service.script_service import ScriptService


class _RecordingRepository(ScriptRepository):
    """记录每次调用参数的 fake 仓储，用于断言 strip 语义。"""

    def __init__(self) -> None:
        self._store: dict[str, ScriptRevision] = {}
        self.latest_calls: list[tuple[str, str]] = []

    async def add(self, script: ScriptRevision) -> None:
        self._store[script.entity_id] = script

    async def save(self, script: ScriptRevision) -> None:
        self._store[script.entity_id] = script

    async def get_by_id(self, entity_id: str) -> ScriptRevision | None:
        return self._store.get(entity_id)

    async def get_latest_by_episode(
        self, project_id: str, episode_ref: str
    ) -> ScriptRevision | None:
        self.latest_calls.append((project_id, episode_ref))
        for s in self._store.values():
            if (
                s.project_id == project_id
                and s.episode_ref == episode_ref
                and s.status.value != "SUPERSEDED"
            ):
                return s
        return None


@pytest.fixture
def repo() -> _RecordingRepository:
    return _RecordingRepository()


async def test_strip_normalizes_query_and_store(repo: _RecordingRepository) -> None:
    """带首尾空格的 project_id/episode_ref 应在查询与入库前被 strip。"""
    service = ScriptService(repo)
    script = await service.import_script(
        project_id="  proj_001  ",
        episode_ref="  EP001  ",
        title="  测试剧本  ",
        created_by="  user_001  ",
    )
    # 查询 latest 时用的是 strip 后的值
    assert repo.latest_calls == [("proj_001", "EP001")]
    # 入库的领域对象字段已 strip
    assert script.project_id == "proj_001"
    assert script.episode_ref == "EP001"
    assert script.title == "测试剧本"
    assert script.created_by == "user_001"


async def test_rejects_whitespace_only_episode_ref(repo: _RecordingRepository) -> None:
    service = ScriptService(repo)
    with pytest.raises(DomainError) as exc_info:
        await service.import_script(
            project_id="proj_001",
            episode_ref="   ",
            title="标题",
            created_by="user_001",
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"


async def test_rejects_whitespace_only_created_by(repo: _RecordingRepository) -> None:
    service = ScriptService(repo)
    with pytest.raises(DomainError) as exc_info:
        await service.import_script(
            project_id="proj_001",
            episode_ref="EP001",
            title="标题",
            created_by="   ",
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"
