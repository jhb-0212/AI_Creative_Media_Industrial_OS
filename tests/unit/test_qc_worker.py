"""QC Worker（Phase 3）的单元测试。

关键不变量：没有检测器时不得领取任务（不制造假绿）；有检测器时判定必须带报告，
且该报告必须真实存在并属于本任务的资产版本与租户（「有 ID」≠「有证据」）；
执行器异常不得写假的 REJECTED，只能让租约过期后交回队列。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from packages.shared.evidence_ports import ReportFacts
from services.project_planning.domain.enums import QCTaskStatus
from services.qc_governance.domain.qc_task import EnqueueOrigin
from workers.qc.worker import QCInspection, QCWorker

_TENANT = uuid4()


class FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}
        # 短事务改造的可观测判据：每个阶段各开一个作用域并提交
        self.opened = 0
        self.commits: list[str] = []

    async def find_by_generation_task(self, generation_task_id: str):
        for task in self.rows.values():
            if task.generation_task_id == generation_task_id:
                return task
        return None

    async def add(self, task):
        self.rows[task.entity_id] = task
        return task

    async def save(self, task, expected_version: int):
        self.rows[task.entity_id] = task
        return task

    async def get_by_id(self, entity_id: str):
        return self.rows.get(entity_id)

    async def next_claimable_for_claim(self):
        queued = [t for t in self.rows.values() if t.status is QCTaskStatus.QUEUED]
        return min(queued, key=lambda t: t.created_at) if queued else None

    async def list_expired(self, now: datetime):
        return [
            t
            for t in self.rows.values()
            if t.status is QCTaskStatus.RUNNING
            and t.lease_expires_at is not None
            and t.lease_expires_at < now
        ]


class FakeInspector:
    def __init__(self, *, configured=True, inspection=None, raise_exc=None, delay=0.0):
        self.configured = configured
        self.inspection = inspection or QCInspection("PASSED", "report-1", ["asset-1"])
        self._raise = raise_exc
        self._delay = delay
        self.calls = 0

    async def inspect(self, task):
        self.calls += 1
        if self._delay:
            import asyncio

            await asyncio.sleep(self._delay)
        if self._raise is not None:
            raise self._raise
        return self.inspection


@dataclass
class _Unit:
    service: Any
    repository: FakeRepo

    async def commit(self) -> None:
        self.repository.commits.append("commit")


def _scope_factory(repo: FakeRepo, service: Any):
    @asynccontextmanager
    async def scope():
        repo.opened += 1
        yield _Unit(service=service, repository=repo)

    return scope


def _report_lookup(
    *,
    asset_version_id: str = "asset-1",
    tenant_id=_TENANT,
    known: frozenset[str] = frozenset({"report-1"}),
):
    """报告核验器替身：只承认 ``known`` 里的 id，其余视为不存在。"""

    async def lookup(qc_report_id: str, want_tenant):
        if qc_report_id not in known:
            return None
        return ReportFacts(asset_version_id=asset_version_id, tenant_id=tenant_id)

    return lookup


def _setup(inspector, *, report_lookup=None) -> tuple[QCWorker, FakeRepo, object]:
    from packages.shared.in_memory import InMemoryOutboxWriter
    from services.qc_governance.service.qc_task_service import QCTaskService

    repo = FakeRepo()
    service = QCTaskService(
        repo,
        InMemoryOutboxWriter(),
        report_lookup=report_lookup or _report_lookup(),
    )
    worker = QCWorker(
        scope=_scope_factory(repo, service),
        executor=inspector,
        worker_id="qc-worker-1",
        lease_seconds=120,
        idle_sleep=0.05,
    )
    return worker, repo, service


async def _queued(service, generation_task_id="gen-1"):
    return await service.enqueue(
        project_id="proj-1",
        asset_version_id="asset-1",
        generation_task_id=generation_task_id,
        quality_profile_version="qp-1",
        tenant_id=_TENANT,
        origin=EnqueueOrigin(source="RELAY", operator_id="outbox-relay"),
        shot_id="shot-1",
    )


async def test_unconfigured_inspector_never_claims() -> None:
    worker, repo, service = _setup(FakeInspector(configured=False))
    await _queued(service)
    assert await worker.process_one() is False
    assert repo.rows[next(iter(repo.rows))].status is QCTaskStatus.QUEUED


async def test_configured_inspector_writes_pass_with_report() -> None:
    worker, repo, service = _setup(FakeInspector())
    task = await _queued(service)

    assert await worker.process_one() is True
    done = repo.rows[task.entity_id]
    assert done.status is QCTaskStatus.PASSED
    assert done.qc_report_id == "report-1"
    assert done.lease_owner == ""


async def test_inspector_exception_leaves_task_running_for_reclaim() -> None:
    worker, repo, service = _setup(FakeInspector(raise_exc=RuntimeError("boom")))
    task = await _queued(service)

    assert await worker.process_one() is True
    current = repo.rows[task.entity_id]
    # 不写假的 REJECTED：停 RUNNING，等租约到期回收。
    assert current.status is QCTaskStatus.RUNNING
    assert current.attempt_count == 1


async def test_sweep_reclaims_expired_only() -> None:
    worker, repo, service = _setup(FakeInspector(configured=False))
    fresh = await _queued(service, "gen-fresh")
    await service.claim(task_id=fresh.entity_id, worker_id="qc-1", lease_seconds=120)
    dead = await _queued(service, "gen-dead")
    await service.claim(task_id=dead.entity_id, worker_id="qc-2", lease_seconds=120)
    dead.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)

    assert await worker.sweep_expired() == 1
    assert repo.rows[fresh.entity_id].status is QCTaskStatus.RUNNING
    assert repo.rows[dead.entity_id].status is QCTaskStatus.QUEUED


def test_qc_result_literal() -> None:
    assert QCInspection("REJECTED", "report-2").result == "REJECTED"


# ---- 真实检测器（资产完整性）的判定逻辑 ------------------------------------ #


class _Asset:
    def __init__(self, *, entity_id="asset-1", project_id="proj-1", sha256="", storage_ref=""):
        self.entity_id = entity_id
        self.project_id = project_id
        self.sha256 = sha256
        self.storage_ref = storage_ref


def _inspector(storage):
    """storage 是必填的真实存储端口——P1-01 后不再有「无存储检测器」。"""
    from workers.qc.asset_inspector import AssetIntegrityInspector

    ins = AssetIntegrityInspector(asset_sessionmaker=None, storage=storage)
    assert ins.configured is True
    return ins


def _storage(tmp_path):
    from adapters.storage.local_fs import LocalFsObjectStorage

    return LocalFsObjectStorage(tmp_path, max_bytes=1024 * 1024)


def test_cross_project_asset_is_blocked_not_merely_flagged() -> None:
    """跨项目引用必须**阻断执行**，不能只记一条 finding 后照旧写报告。"""
    from workers.qc.asset_inspector import (
        InspectionBlockedError,
        assert_asset_within_task_scope,
    )

    task = type("T", (), {"project_id": "proj-1"})()
    asset = _Asset(
        project_id="proj-other",
        sha256="a" * 64,
        storage_ref="local://aa/aa/" + "a" * 64 + ".png",
    )

    with pytest.raises(InspectionBlockedError) as excinfo:
        assert_asset_within_task_scope(task, asset)
    # 报错要能定位到「谁引用了谁」，否则现场只能看到一句「检测失败」。
    assert "proj-1" in str(excinfo.value)
    assert "proj-other" in str(excinfo.value)


async def test_same_project_asset_passes_scope_guard_and_leaves_verified_evidence(tmp_path) -> None:
    """对照组：归属一致时守卫不误杀，且报告里留下「已核验」的事实。"""
    from packages.shared.storage_locator import build_locator
    from workers.qc.asset_inspector import assert_asset_within_task_scope

    insp = _inspector(_storage(tmp_path))
    task = type("T", (), {"project_id": "proj-1"})()
    asset = _Asset(
        project_id="proj-1", sha256="a" * 64, storage_ref=build_locator("a" * 64, "png")
    )

    assert_asset_within_task_scope(task, asset)  # 不抛即通过

    findings = await insp._check(task, asset)
    scope = next(f for f in findings if f["check"] == "project_scope")
    assert scope["passed"] is True
    assert scope["expected"] == scope["actual"] == "proj-1"


async def test_integrity_checks_verify_content_hash(tmp_path) -> None:
    """真实定位符落盘后能读回即通过；哈希与资产声明不符时读回失败。"""
    import hashlib

    payload = b"qc-fixture"
    digest = hashlib.sha256(payload).hexdigest()
    storage = _storage(tmp_path)
    stored = await storage.put(data=payload, media_type="image/png")

    insp = _inspector(storage)
    task = type("T", (), {"project_id": "proj-1"})()
    asset = _Asset(
        project_id="proj-1", sha256=digest, storage_ref=stored.locator
    )
    findings = await insp._check(task, asset)
    assert all(f["passed"] for f in findings)
    content = next(f for f in findings if f["check"] == "content_verifiable")
    assert content["size_bytes"] == len(payload)

    # 内容是真的、但挂错了资产：换一个指向其它内容的合法定位符，仍声称原
    # sha256。存储端口能读回（内容 vs 内嵌哈希一致），但内嵌哈希 ≠ 资产声明，
    # 必须判不可验证——否则「产物存在」被当成「就是这项资产的产物」。
    other = await storage.put(data=b"different", media_type="image/png")
    asset.storage_ref = other.locator
    findings = await insp._check(task, asset)
    content = next(f for f in findings if f["check"] == "content_verifiable")
    assert content["passed"] is False
    assert content["sha256_matches"] is False


async def test_integrity_checks_reject_unverifiable_storage_ref(tmp_path) -> None:
    """非法 / 非受控定位符必须判不可验证，而不是抛异常打断 Worker。"""
    insp = _inspector(_storage(tmp_path))
    task = type("T", (), {"project_id": "proj-1"})()
    asset = _Asset(project_id="proj-1", sha256="a" * 64, storage_ref="s3://bucket/key")

    findings = await insp._check(task, asset)
    content = next(f for f in findings if f["check"] == "content_verifiable")
    assert content["passed"] is False
    assert "非法存储定位符" in content["reason"]


async def test_claim_and_finish_use_separate_short_transactions() -> None:
    """P1-01 判据：领取与判定各自开作用域并提交，执行期间不持有长事务。"""
    worker, repo, service = _setup(FakeInspector())
    task = await _queued(service)

    assert await worker.process_one() is True

    assert repo.opened == 2
    assert repo.commits == ["commit", "commit"]
    assert repo.rows[task.entity_id].status is QCTaskStatus.PASSED
