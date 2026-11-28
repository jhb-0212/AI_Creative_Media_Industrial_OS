"""五个聚合根的 SQLAlchemy 仓储实现（写入边界：project_planning 模块专属）。

与 Episode 实现同型：
- 领域模型 ↔ ORM 映射集中在此，隔离持久化细节。
- ``add`` 用 SAVEPOINT，把驱动异常翻译成端口契约（唯一冲突可幂等回读，
  引用/RLS 失败必须上抛）。
- ``save`` 用条件更新（乐观锁），命中 0 行抛 ConcurrentModificationError。
- 只经本文件写这五张表，其它模块禁止直写。
"""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.port_errors import translate_write_error
from services.project_planning.domain.constraint_pack import ConstraintPack
from services.project_planning.domain.enums import (
    ConstraintPackStatus,
    ReworkTaskStatus,
    SceneStatus,
    ShotDraftStatus,
    ShotRevisionStatus,
    ShotSpecStatus,
    ShotStatus,
)
from services.project_planning.domain.rework_task import ReworkTask
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.shot_draft import ShotBreakdownDraft
from services.project_planning.domain.shot_revision import ShotRevision
from services.project_planning.domain.shot_spec import ShotSpec
from services.project_planning.repository._versioning import (
    _orm_update_values,
    conditional_update,
)
from services.project_planning.repository.orm import (
    ConstraintPackModel,
    ReworkTaskModel,
    SceneModel,
    ShotBreakdownDraftModel,
    ShotModel,
    ShotRevisionModel,
    ShotSpecModel,
)
from services.project_planning.repository.shot_planning_repositories import (
    ConstraintPackRepository,
    ReworkTaskRepository,
    SceneRepository,
    ShotDraftRepository,
    ShotRepository,
    ShotRevisionRepository,
    ShotSpecRepository,
)


def _bad_status(table: str, value: str) -> DomainError:
    return DomainError(ErrorRegistry.INTERNAL_ERROR, detail=f"{table} 存量状态非法: {value!r}")


def scene_lock_statement(entity_id: str, *, exclusive: bool) -> Select[SceneModel]:
    """父行锁读取语句：SC-5 两个方向的唯一落点（ADR-R28 §6.3）。

    锁模式为什么是**这一对**，而不是随便选一个：

    - 归档侧 ``FOR UPDATE``（``exclusive=True``）：它建立前置条件（「其下无在产
      镜头」），因此必须把「锁 → 重判 → 写」这三步与任何想破坏前置条件的人隔开。
      排他锁与所有行锁冲突，做到这一点。
    - 子对象侧 ``FOR SHARE``（``exclusive=False``）：**不是** ``FOR KEY SHARE``。
      ``KEY SHARE`` 只与改键值/删除冲突，与 ``FOR NO KEY UPDATE`` 兼容——而
      ``UPDATE ... SET status`` 取的正是 ``NO KEY UPDATE``。也就是说，只靠子侧
      的 ``KEY SHARE``，父侧一次**没显式加 FOR UPDATE** 的归档就能穿过去，屏障
      静默失效。``FOR SHARE`` 与 ``NO KEY UPDATE`` 冲突，因此它不依赖父侧一定
      写了排他锁（防御纵深：两侧各自独立成立，而不是互为前提）。
      同时 ``FOR SHARE`` 与子表 INSERT 的外键检查（取 ``KEY SHARE``）兼容，
      不会让正常的镜头插入自我阻塞。

    渲染口径：``read=True`` 单独给出 ``FOR SHARE``；再叠 ``key_share=True`` 会变成
    ``FOR KEY SHARE``（即上面被否掉的那个）。渲染结果由
    ``tests/unit/test_row_lock_protocol.py`` 逐字钉住，避免方言升级后悄悄改语义。
    """
    stmt = select(SceneModel).where(SceneModel.entity_id == entity_id)
    if exclusive:
        return stmt.with_for_update()
    return stmt.with_for_update(read=True)


def shot_revision_lock_statement(entity_id: str) -> Select[ShotRevisionModel]:
    """修订的**共享行锁**读取语句（A22：规格写入前的确认侧）。

    与 ``scene_lock_statement`` 的非排他分支同模式，理由也同源：子对象（ShotSpec）
    要与之互斥的是「修订状态被改写」（``ACTIVE -> SUPERSEDED``），而
    ``UPDATE ... SET status`` 取的是 ``NO KEY UPDATE``——``FOR KEY SHARE`` 与它
    兼容，屏障会静默失效；``FOR SHARE`` 才冲突。

    取代侧（``SHOT.REVISION.CONFIRM``）因此不需要为自己加排他锁：屏障由子侧
    独立成立，不依赖父侧写对（防御纵深，见 ADR-R28 §6.3 的同一论证）。
    """
    return (
        select(ShotRevisionModel)
        .where(ShotRevisionModel.entity_id == entity_id)
        .with_for_update(read=True)
    )


# --------------------------------------------------------------------------- #
# Scene
# --------------------------------------------------------------------------- #
def _scene_to_domain(m: SceneModel) -> Scene:
    try:
        status = SceneStatus(m.status)
    except ValueError as exc:
        raise _bad_status("scenes", m.status) from exc
    return Scene(
        entity_id=m.entity_id,
        scene_id=m.scene_id,
        scene_code=m.scene_code,
        project_id=m.project_id,
        episode_id=m.episode_id,
        created_by=m.created_by,
        tenant_id=m.tenant_id,
        status=status,
        schema_version=m.schema_version,
        entity_version=m.entity_version,
        owner_module=m.owner_module,
        created_at=m.created_at,
        updated_at=m.updated_at,
        audit_ref=m.audit_ref,
    )


def _scene_to_orm(s: Scene) -> SceneModel:
    return SceneModel(
        entity_id=s.entity_id,
        entity_type=s.ENTITY_TYPE,
        schema_version=s.schema_version,
        entity_version=s.entity_version,
        status=s.status.value,
        owner_module=s.owner_module,
        tenant_id=s.tenant_id,
        created_at=s.created_at,
        created_by=s.created_by,
        updated_at=s.updated_at,
        audit_ref=s.audit_ref,
        scene_id=s.scene_id,
        scene_code=s.scene_code,
        project_id=s.project_id,
        episode_id=s.episode_id,
    )


class SqlAlchemySceneRepository(SceneRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, scene: Scene) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_scene_to_orm(scene))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=scene.entity_id)

    async def save(self, scene: Scene, expected_version: int) -> None:
        await conditional_update(
            self._session,
            SceneModel,
            scene.entity_id,
            expected_version,
            _orm_update_values(_scene_to_orm(scene)),
        )

    async def get_by_id(self, entity_id: str) -> Scene | None:
        result = await self._session.execute(
            select(SceneModel).where(SceneModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _scene_to_domain(model) if model is not None else None

    async def get_by_project_code(self, project_id: str, scene_code: str) -> Scene | None:
        result = await self._session.execute(
            select(SceneModel).where(
                SceneModel.project_id == project_id,
                SceneModel.scene_code == scene_code,
            )
        )
        model = result.scalar_one_or_none()
        return _scene_to_domain(model) if model is not None else None

    async def lock_for_archive(self, entity_id: str) -> Scene | None:
        return await self._lock(entity_id, exclusive=True)

    async def lock_for_child_write(self, entity_id: str) -> Scene | None:
        return await self._lock(entity_id, exclusive=False)

    async def _lock(self, entity_id: str, *, exclusive: bool) -> Scene | None:
        result = await self._session.execute(scene_lock_statement(entity_id, exclusive=exclusive))
        model = result.scalar_one_or_none()
        return _scene_to_domain(model) if model is not None else None


# --------------------------------------------------------------------------- #
# Shot
# --------------------------------------------------------------------------- #
def _shot_to_domain(m: ShotModel) -> Shot:
    try:
        status = ShotStatus(m.status)
    except ValueError as exc:
        raise _bad_status("shots", m.status) from exc
    return Shot(
        entity_id=m.entity_id,
        shot_id=m.shot_id,
        shot_code=m.shot_code,
        scene_id=m.scene_id,
        project_id=m.project_id,
        shot_revision_id=m.shot_revision_id,
        blocked_reason=m.blocked_reason,
        rework_basis_ref=m.rework_basis_ref,
        cancellation_ref=m.cancellation_ref,
        cancelled_at=m.cancelled_at,
        source_draft_ref=m.source_draft_ref,
        created_by=m.created_by,
        tenant_id=m.tenant_id,
        status=status,
        schema_version=m.schema_version,
        entity_version=m.entity_version,
        owner_module=m.owner_module,
        created_at=m.created_at,
        updated_at=m.updated_at,
        audit_ref=m.audit_ref,
    )


def _shot_to_orm(s: Shot) -> ShotModel:
    return ShotModel(
        entity_id=s.entity_id,
        entity_type=s.ENTITY_TYPE,
        schema_version=s.schema_version,
        entity_version=s.entity_version,
        status=s.status.value,
        owner_module=s.owner_module,
        tenant_id=s.tenant_id,
        created_at=s.created_at,
        created_by=s.created_by,
        updated_at=s.updated_at,
        audit_ref=s.audit_ref,
        shot_id=s.shot_id,
        shot_code=s.shot_code,
        scene_id=s.scene_id,
        project_id=s.project_id,
        shot_revision_id=s.shot_revision_id,
        blocked_reason=s.blocked_reason,
        rework_basis_ref=s.rework_basis_ref,
        cancellation_ref=s.cancellation_ref,
        cancelled_at=s.cancelled_at,
        source_draft_ref=s.source_draft_ref,
    )


class SqlAlchemyShotRepository(ShotRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, shot: Shot) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_shot_to_orm(shot))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=shot.entity_id)

    async def save(self, shot: Shot, expected_version: int) -> None:
        await conditional_update(
            self._session,
            ShotModel,
            shot.entity_id,
            expected_version,
            _orm_update_values(_shot_to_orm(shot)),
        )

    async def get_by_id(self, entity_id: str) -> Shot | None:
        result = await self._session.execute(
            select(ShotModel).where(ShotModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _shot_to_domain(model) if model is not None else None

    async def get_by_project_code(self, project_id: str, shot_code: str) -> Shot | None:
        result = await self._session.execute(
            select(ShotModel).where(
                ShotModel.project_id == project_id,
                ShotModel.shot_code == shot_code,
            )
        )
        model = result.scalar_one_or_none()
        return _shot_to_domain(model) if model is not None else None

    async def has_with_status(self, scene_id: str, status: ShotStatus) -> bool:
        # 只取一列并 limit 1：判据是「存在性」，不是计数。用 count(*) 会为了
        # 一个布尔值扫全部行，在镜头多的场次上是纯浪费。
        result = await self._session.execute(
            select(ShotModel.entity_id)
            .where(ShotModel.scene_id == scene_id, ShotModel.status == status.value)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def existing_ids_in_project(self, project_id: str, shot_ids: list[str]) -> set[str]:
        """返回确实属于该项目的镜头 id（A23 约束包作用范围校验）。

        用 ``IN`` 而不是「逐个查」：一次往返换回命中集合，调用方据此点出越界 id。
        空列表直接返回空集（``IN ()`` 在 SQL 里是语法错误，不是「都不存在」）。
        """
        if not shot_ids:
            return set()
        result = await self._session.execute(
            select(ShotModel.entity_id).where(
                ShotModel.project_id == project_id,
                ShotModel.entity_id.in_(shot_ids),
            )
        )
        return set(result.scalars().all())


# --------------------------------------------------------------------------- #
# ShotRevision
# --------------------------------------------------------------------------- #
def _revision_to_domain(m: ShotRevisionModel) -> ShotRevision:
    try:
        status = ShotRevisionStatus(m.status)
    except ValueError as exc:
        raise _bad_status("shot_revisions", m.status) from exc
    return ShotRevision(
        entity_id=m.entity_id,
        shot_revision_id=m.shot_revision_id,
        shot_id=m.shot_id,
        project_id=m.project_id,
        episode_id=m.episode_id,
        scene_id=m.scene_id,
        revision_no=m.revision_no,
        description=m.description,
        narrative_intent=m.narrative_intent,
        character_refs=m.character_refs,
        location_ref=m.location_ref,
        prop_refs=m.prop_refs,
        constraint_pack_applicability=m.constraint_pack_applicability,
        constraint_pack_id=m.constraint_pack_id,
        creative_autonomy=m.creative_autonomy,
        risk=m.risk,
        quality_profile=m.quality_profile,
        intent_ref=m.intent_ref,
        basis_script_revision_id=m.basis_script_revision_id,
        script_anchors=m.script_anchors,
        source_draft_ref=m.source_draft_ref,
        source_candidate_refs=m.source_candidate_refs,
        change_origin=m.change_origin,
        supersedes_shot_revision_id=m.supersedes_shot_revision_id,
        change_summary=m.change_summary,
        changed_fields=m.changed_fields,
        provenance=m.provenance,
        confirmed_by=m.confirmed_by,
        confirmed_at=m.confirmed_at,
        created_by=m.created_by,
        tenant_id=m.tenant_id,
        status=status,
        schema_version=m.schema_version,
        entity_version=m.entity_version,
        owner_module=m.owner_module,
        created_at=m.created_at,
        updated_at=m.updated_at,
        audit_ref=m.audit_ref,
    )


def _revision_to_orm(r: ShotRevision) -> ShotRevisionModel:
    return ShotRevisionModel(
        entity_id=r.entity_id,
        entity_type=r.ENTITY_TYPE,
        schema_version=r.schema_version,
        entity_version=r.entity_version,
        status=r.status.value,
        owner_module=r.owner_module,
        tenant_id=r.tenant_id,
        created_at=r.created_at,
        created_by=r.created_by,
        updated_at=r.updated_at,
        audit_ref=r.audit_ref,
        shot_revision_id=r.shot_revision_id,
        shot_id=r.shot_id,
        project_id=r.project_id,
        episode_id=r.episode_id,
        scene_id=r.scene_id,
        revision_no=r.revision_no,
        description=r.description,
        narrative_intent=r.narrative_intent,
        character_refs=r.character_refs,
        location_ref=r.location_ref,
        prop_refs=r.prop_refs,
        constraint_pack_applicability=r.constraint_pack_applicability,
        constraint_pack_id=r.constraint_pack_id,
        creative_autonomy=r.creative_autonomy,
        risk=r.risk,
        quality_profile=r.quality_profile,
        intent_ref=r.intent_ref,
        basis_script_revision_id=r.basis_script_revision_id,
        script_anchors=r.script_anchors,
        source_draft_ref=r.source_draft_ref,
        source_candidate_refs=r.source_candidate_refs,
        change_origin=r.change_origin,
        supersedes_shot_revision_id=r.supersedes_shot_revision_id,
        change_summary=r.change_summary,
        changed_fields=r.changed_fields,
        provenance=r.provenance,
        confirmed_by=r.confirmed_by,
        confirmed_at=r.confirmed_at,
    )


class SqlAlchemyShotRevisionRepository(ShotRevisionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, revision: ShotRevision) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_revision_to_orm(revision))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=revision.entity_id)

    async def save(self, revision: ShotRevision, expected_version: int) -> None:
        await conditional_update(
            self._session,
            ShotRevisionModel,
            revision.entity_id,
            expected_version,
            _orm_update_values(_revision_to_orm(revision)),
        )

    async def get_by_id(self, entity_id: str) -> ShotRevision | None:
        result = await self._session.execute(
            select(ShotRevisionModel).where(ShotRevisionModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _revision_to_domain(model) if model is not None else None

    async def next_revision_no(self, shot_id: str) -> int:
        result = await self._session.execute(
            select(func.max(ShotRevisionModel.revision_no)).where(
                ShotRevisionModel.shot_id == shot_id
            )
        )
        current = result.scalar_one_or_none()
        return int(current or 0) + 1

    async def get_active_by_shot(self, shot_id: str) -> ShotRevision | None:
        result = await self._session.execute(
            select(ShotRevisionModel).where(
                ShotRevisionModel.shot_id == shot_id,
                ShotRevisionModel.status == ShotRevisionStatus.ACTIVE.value,
            )
        )
        model = result.scalar_one_or_none()
        return _revision_to_domain(model) if model is not None else None

    async def lock_for_child_write(self, entity_id: str) -> ShotRevision | None:
        result = await self._session.execute(shot_revision_lock_statement(entity_id))
        model = result.scalar_one_or_none()
        return _revision_to_domain(model) if model is not None else None


# --------------------------------------------------------------------------- #
# ShotSpec
# --------------------------------------------------------------------------- #
def _spec_to_domain(m: ShotSpecModel) -> ShotSpec:
    try:
        status = ShotSpecStatus(m.status)
    except ValueError as exc:
        raise _bad_status("shot_specs", m.status) from exc
    return ShotSpec(
        entity_id=m.entity_id,
        shot_spec_id=m.shot_spec_id,
        project_id=m.project_id,
        shot_revision_id=m.shot_revision_id,
        duration_ms=m.duration_ms,
        fps_num=m.fps_num,
        fps_den=m.fps_den,
        # ARRAY 列读回来是可迭代的；转 list 是为了让领域对象拿到确定的 shape
        # （驱动可能给 tuple，``==`` 比较与 JSON 序列化都会因此不一致）。
        cut_points_frames=list(m.cut_points_frames),
        aspect_ratio=m.aspect_ratio,
        coordinate_system=m.coordinate_system,
        camera_path=m.camera_path,
        subject_paths=m.subject_paths,
        shot_size_changes=m.shot_size_changes,
        previz_ref=m.previz_ref,
        control_signal_refs=m.control_signal_refs,
        locked_dimensions=m.locked_dimensions,
        created_by=m.created_by,
        tenant_id=m.tenant_id,
        status=status,
        schema_version=m.schema_version,
        entity_version=m.entity_version,
        owner_module=m.owner_module,
        created_at=m.created_at,
        updated_at=m.updated_at,
        audit_ref=m.audit_ref,
    )


def _spec_to_orm(s: ShotSpec) -> ShotSpecModel:
    return ShotSpecModel(
        entity_id=s.entity_id,
        entity_type=s.ENTITY_TYPE,
        schema_version=s.schema_version,
        entity_version=s.entity_version,
        status=s.status.value,
        owner_module=s.owner_module,
        tenant_id=s.tenant_id,
        created_at=s.created_at,
        created_by=s.created_by,
        updated_at=s.updated_at,
        audit_ref=s.audit_ref,
        shot_spec_id=s.shot_spec_id,
        project_id=s.project_id,
        shot_revision_id=s.shot_revision_id,
        duration_ms=s.duration_ms,
        fps_num=s.fps_num,
        fps_den=s.fps_den,
        cut_points_frames=list(s.cut_points_frames),
        aspect_ratio=s.aspect_ratio,
        coordinate_system=s.coordinate_system,
        camera_path=s.camera_path,
        subject_paths=s.subject_paths,
        shot_size_changes=s.shot_size_changes,
        previz_ref=s.previz_ref,
        control_signal_refs=list(s.control_signal_refs),
        locked_dimensions=s.locked_dimensions,
    )


class SqlAlchemyShotSpecRepository(ShotSpecRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, spec: ShotSpec) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_spec_to_orm(spec))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=spec.entity_id)

    async def save(self, spec: ShotSpec, expected_version: int) -> None:
        await conditional_update(
            self._session,
            ShotSpecModel,
            spec.entity_id,
            expected_version,
            _orm_update_values(_spec_to_orm(spec)),
        )

    async def get_by_id(self, entity_id: str) -> ShotSpec | None:
        result = await self._session.execute(
            select(ShotSpecModel).where(ShotSpecModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _spec_to_domain(model) if model is not None else None

    async def get_by_revision(self, shot_revision_id: str) -> ShotSpec | None:
        result = await self._session.execute(
            select(ShotSpecModel).where(ShotSpecModel.shot_revision_id == shot_revision_id)
        )
        model = result.scalar_one_or_none()
        return _spec_to_domain(model) if model is not None else None


# --------------------------------------------------------------------------- #
# ConstraintPack
# --------------------------------------------------------------------------- #
def _pack_to_domain(m: ConstraintPackModel) -> ConstraintPack:
    try:
        status = ConstraintPackStatus(m.status)
    except ValueError as exc:
        raise _bad_status("constraint_packs", m.status) from exc
    return ConstraintPack(
        entity_id=m.entity_id,
        constraint_pack_id=m.constraint_pack_id,
        project_id=m.project_id,
        scene_id=m.scene_id,
        # 驱动可能给 tuple（asyncpg 对 text[] 的返回形态），统一转 list：
        # 领域层用 list 做「去重保序」与「是否为空」判定，tuple 会让
        # ``pack.shot_ids == [...]`` 这类比较静默为假。
        shot_ids=list(m.shot_ids),
        shot_group_ids=list(m.shot_group_ids),
        applicability=m.applicability,
        locked_dimensions=list(m.locked_dimensions),
        constraints=list(m.constraints),
        source_previz_revision=m.source_previz_revision,
        version=m.version,
        approved_by=m.approved_by,
        approved_at=m.approved_at,
        status=status,
        schema_version=m.schema_version,
        entity_version=m.entity_version,
        owner_module=m.owner_module,
        created_at=m.created_at,
        created_by=m.created_by,
        updated_at=m.updated_at,
        audit_ref=m.audit_ref,
        tenant_id=m.tenant_id,
    )


def _pack_to_orm(p: ConstraintPack) -> ConstraintPackModel:
    return ConstraintPackModel(
        entity_id=p.entity_id,
        entity_type=p.ENTITY_TYPE,
        schema_version=p.schema_version,
        entity_version=p.entity_version,
        status=p.status.value,
        owner_module=p.owner_module,
        tenant_id=p.tenant_id,
        created_at=p.created_at,
        created_by=p.created_by,
        updated_at=p.updated_at,
        audit_ref=p.audit_ref,
        constraint_pack_id=p.constraint_pack_id,
        project_id=p.project_id,
        scene_id=p.scene_id,
        shot_ids=list(p.shot_ids),
        shot_group_ids=list(p.shot_group_ids),
        applicability=p.applicability,
        locked_dimensions=list(p.locked_dimensions),
        # 深拷贝：domain 的 dict 若与 ORM 共享引用，一次原地修改会同时改到
        # 「已提交的快照」与「待写入的对象」。JSONB 不像数组能靠 list() 复制。
        constraints=[dict(entry) for entry in p.constraints],
        source_previz_revision=p.source_previz_revision,
        version=p.version,
        approved_by=p.approved_by,
        approved_at=p.approved_at,
    )


class SqlAlchemyConstraintPackRepository(ConstraintPackRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, pack: ConstraintPack) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_pack_to_orm(pack))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=pack.entity_id)

    async def save(self, pack: ConstraintPack, expected_version: int) -> None:
        # 与 add 一样要翻译驱动异常：DRAFT→ACTIVE 会撞部分唯一索引
        # ``uq_constraint_pack_single_active``（同一逻辑包已有生效版本）。
        # 不翻译的话，路由层收到原始 IntegrityError 只能给 500，而这是一个
        # 有明确业务含义的冲突（新版本还没被授权取代旧版本）。
        try:
            await conditional_update(
                self._session,
                ConstraintPackModel,
                pack.entity_id,
                expected_version,
                _orm_update_values(_pack_to_orm(pack)),
            )
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=pack.entity_id)

    async def get_by_id(self, entity_id: str) -> ConstraintPack | None:
        result = await self._session.execute(
            select(ConstraintPackModel).where(ConstraintPackModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _pack_to_domain(model) if model is not None else None

    async def get_active_by_pack(self, constraint_pack_id: str) -> ConstraintPack | None:
        result = await self._session.execute(
            select(ConstraintPackModel).where(
                ConstraintPackModel.constraint_pack_id == constraint_pack_id,
                ConstraintPackModel.status == ConstraintPackStatus.ACTIVE.value,
            )
        )
        model = result.scalar_one_or_none()
        return _pack_to_domain(model) if model is not None else None

    async def get_by_pack_version(
        self, constraint_pack_id: str, version: int
    ) -> ConstraintPack | None:
        # 走唯一约束 uq_constraint_pack_version 的同一组列，因此最多一行；
        # 用 scalar_one_or_none 而不是 first()：出现两行就意味着唯一索引没生效，
        # 那是库层缺陷，应当在读取处就炸，而不是随手取一条让故障看起来正常。
        result = await self._session.execute(
            select(ConstraintPackModel).where(
                ConstraintPackModel.constraint_pack_id == constraint_pack_id,
                ConstraintPackModel.version == version,
            )
        )
        model = result.scalar_one_or_none()
        return _pack_to_domain(model) if model is not None else None


# --------------------------------------------------------------------------- #
# ShotBreakdownDraft
# --------------------------------------------------------------------------- #
def _draft_to_domain(m: ShotBreakdownDraftModel) -> ShotBreakdownDraft:
    try:
        status = ShotDraftStatus(m.status)
    except ValueError as exc:
        raise _bad_status("shot_breakdown_drafts", m.status) from exc
    return ShotBreakdownDraft(
        entity_id=m.entity_id,
        shot_breakdown_draft_id=m.shot_breakdown_draft_id,
        project_id=m.project_id,
        episode_ref=m.episode_ref,
        basis_script_revision_id=m.basis_script_revision_id,
        breakdown_scope=m.breakdown_scope,
        plan_name=m.plan_name,
        generation_method=m.generation_method,
        draft_version=m.draft_version,
        root_draft_ref=m.root_draft_ref,
        supersedes_draft_ref=m.supersedes_draft_ref,
        change_summary=m.change_summary,
        ai_rationale=m.ai_rationale,
        open_questions=m.open_questions,
        potential_issues=m.potential_issues,
        scene_breakdowns=m.scene_breakdowns,
        review_records=m.review_records,
        confirmed_by=m.confirmed_by,
        confirmed_at=m.confirmed_at,
        confirmed_shot_revision_refs=m.confirmed_shot_revision_refs,
        returned_by=m.returned_by,
        returned_at=m.returned_at,
        return_reason=m.return_reason,
        discarded_reason=m.discarded_reason,
        created_by=m.created_by,
        tenant_id=m.tenant_id,
        status=status,
        schema_version=m.schema_version,
        entity_version=m.entity_version,
        owner_module=m.owner_module,
        created_at=m.created_at,
        updated_at=m.updated_at,
        audit_ref=m.audit_ref,
    )


def _draft_to_orm(d: ShotBreakdownDraft) -> ShotBreakdownDraftModel:
    return ShotBreakdownDraftModel(
        entity_id=d.entity_id,
        entity_type=d.ENTITY_TYPE,
        schema_version=d.schema_version,
        entity_version=d.entity_version,
        status=d.status.value,
        owner_module=d.owner_module,
        tenant_id=d.tenant_id,
        created_at=d.created_at,
        created_by=d.created_by,
        updated_at=d.updated_at,
        audit_ref=d.audit_ref,
        shot_breakdown_draft_id=d.shot_breakdown_draft_id,
        project_id=d.project_id,
        episode_ref=d.episode_ref,
        basis_script_revision_id=d.basis_script_revision_id,
        breakdown_scope=d.breakdown_scope,
        plan_name=d.plan_name,
        generation_method=d.generation_method,
        draft_version=d.draft_version,
        root_draft_ref=d.root_draft_ref,
        supersedes_draft_ref=d.supersedes_draft_ref,
        change_summary=d.change_summary,
        ai_rationale=d.ai_rationale,
        open_questions=d.open_questions,
        potential_issues=d.potential_issues,
        scene_breakdowns=d.scene_breakdowns,
        review_records=d.review_records,
        confirmed_by=d.confirmed_by,
        confirmed_at=d.confirmed_at,
        confirmed_shot_revision_refs=d.confirmed_shot_revision_refs,
        returned_by=d.returned_by,
        returned_at=d.returned_at,
        return_reason=d.return_reason,
        discarded_reason=d.discarded_reason,
    )


class SqlAlchemyShotDraftRepository(ShotDraftRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, draft: ShotBreakdownDraft) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_draft_to_orm(draft))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=draft.entity_id)

    async def save(self, draft: ShotBreakdownDraft, expected_version: int) -> None:
        await conditional_update(
            self._session,
            ShotBreakdownDraftModel,
            draft.entity_id,
            expected_version,
            _orm_update_values(_draft_to_orm(draft)),
        )

    async def get_by_id(self, entity_id: str) -> ShotBreakdownDraft | None:
        result = await self._session.execute(
            select(ShotBreakdownDraftModel).where(ShotBreakdownDraftModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _draft_to_domain(model) if model is not None else None


# --------------------------------------------------------------------------- #
# ReworkTask
# --------------------------------------------------------------------------- #
def _rework_to_domain(m: ReworkTaskModel) -> ReworkTask:
    try:
        status = ReworkTaskStatus(m.status)
    except ValueError as exc:
        raise _bad_status("rework_tasks", m.status) from exc
    return ReworkTask(
        entity_id=m.entity_id,
        rework_task_id=m.rework_task_id,
        project_id=m.project_id,
        issue_id=m.issue_id,
        target_ref=m.target_ref,
        defect_category=m.defect_category,
        severity=m.severity,
        root_cause=m.root_cause,
        owner=m.owner,
        return_node=m.return_node,
        affected_asset_version_ids=m.affected_asset_version_ids,
        downstream_task_ids=m.downstream_task_ids,
        constraint_pack_ref=m.constraint_pack_ref,
        created_by=m.created_by,
        tenant_id=m.tenant_id,
        status=status,
        schema_version=m.schema_version,
        entity_version=m.entity_version,
        owner_module=m.owner_module,
        created_at=m.created_at,
        updated_at=m.updated_at,
        audit_ref=m.audit_ref,
    )


def _rework_to_orm(t: ReworkTask) -> ReworkTaskModel:
    return ReworkTaskModel(
        entity_id=t.entity_id,
        entity_type=t.ENTITY_TYPE,
        schema_version=t.schema_version,
        entity_version=t.entity_version,
        status=t.status.value,
        owner_module=t.owner_module,
        tenant_id=t.tenant_id,
        created_at=t.created_at,
        created_by=t.created_by,
        updated_at=t.updated_at,
        audit_ref=t.audit_ref,
        rework_task_id=t.rework_task_id,
        project_id=t.project_id,
        issue_id=t.issue_id,
        target_ref=t.target_ref,
        defect_category=t.defect_category,
        severity=t.severity,
        root_cause=t.root_cause,
        owner=t.owner,
        return_node=t.return_node,
        affected_asset_version_ids=t.affected_asset_version_ids,
        downstream_task_ids=t.downstream_task_ids,
        constraint_pack_ref=t.constraint_pack_ref,
    )


class SqlAlchemyReworkTaskRepository(ReworkTaskRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: ReworkTask) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_rework_to_orm(task))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=task.entity_id)

    async def save(self, task: ReworkTask, expected_version: int) -> None:
        await conditional_update(
            self._session,
            ReworkTaskModel,
            task.entity_id,
            expected_version,
            _orm_update_values(_rework_to_orm(task)),
        )

    async def get_by_id(self, entity_id: str) -> ReworkTask | None:
        result = await self._session.execute(
            select(ReworkTaskModel).where(ReworkTaskModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _rework_to_domain(model) if model is not None else None
