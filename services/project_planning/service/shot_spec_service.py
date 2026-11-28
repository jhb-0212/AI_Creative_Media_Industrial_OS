"""ShotSpec 应用服务（镜头规格登记与锁定，A22）。

两条用例对应急剧不同的风险档：
- ``create_spec``（``SHOTSPEC.CREATE``，MEDIUM）：绑定精确修订 + SS-3 + 归属校验；
- ``lock_spec``（``SHOTSPEC.LOCK``，HIGH）：把「锁定维度 / 依据 / 版本 / 审批事件」
  四要素一次性落齐。四要素缺一即拒，因为 LOCKED 的语义就是「这些维度此后不可
  再改」——一份说不清锁了什么、凭什么锁的 LOCKED，只是一个改不动也解释不了的
  对象。

创建与锁定刻意分成两个动作：``LOCK`` 兼做创建，等于把一次未受创建侧校验的写入
藏进 HIGH 风险审批里。这与 ``SHOT.DRAFT.CREATE`` / ``SHOT.DRAFT.CONFIRM`` 的分离
同构，也是 A21 新增 ``SHOTSPEC.CREATE`` 的理由。

锁定为什么不走「读 → 改 → 存」：状态推进必须经 ``advance_status``（唯一入口，
同事务写迁移事实与事件），否则「状态改了、审计查不到」会静默发生，且
``data.transition_single_entry`` 判据会报红。依据（``lock_basis_ref``）写进
迁移事实的 ``authority_ref``——D2 已裁定授权依据由该列承载，不新建
ApprovalRecord 对象。
"""

from __future__ import annotations

import uuid
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.project_planning.domain.enums import ShotSpecStatus
from services.project_planning.domain.shot_spec import (
    ShotSpec,
    normalize_locked_dimensions,
)
from services.project_planning.domain.state_machine import (
    SHOT_REVISION_LIFECYCLE,
    SHOT_SPEC_LOCK_ACTION,
)
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.shot_planning_repositories import (
    ShotRevisionRepository,
    ShotSpecRepository,
)
from services.project_planning.service.transition import advance_status


class ShotSpecService:
    """镜头规格的创建、锁定与查询。

    三个依赖都是构造期必需：规格的存在性证明靠修订仓储，归属证明靠项目仓储。
    可选依赖会退化成运行期 ``if xxx is None``——那正好是「守卫缺失」这种失效
    唯一不会被类型系统发现的位置。
    """

    def __init__(
        self,
        repository: ShotSpecRepository,
        shot_revision_repository: ShotRevisionRepository,
        project_repository: ProjectRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._repository = repository
        self._shot_revision_repository = shot_revision_repository
        self._project_repository = project_repository
        self._outbox_writer = outbox_writer

    async def create_spec(
        self,
        *,
        project_id: str,
        shot_revision_id: str,
        duration_ms: int,
        fps_num: int,
        fps_den: int,
        cut_points_frames: list[int],
        aspect_ratio: str,
        coordinate_system: str,
        camera_path: str,
        subject_paths: str,
        shot_size_changes: str,
        previz_ref: str,
        created_by: str,
        tenant_id: UUID,
        control_signal_refs: list[str] | None = None,
        entity_id: str | None = None,
    ) -> ShotSpec:
        """登记一份规格：绑定精确修订（SS-2）+ 切点校验（SS-3）+ 归属与冻结判定。

        顺序是刻意的，四步都不能换位：

        1. **幂等回读**（按 entity_id）排在最前。重放一个当时成功过的请求是重试，
           不是新建；若先判冻结，修订被取代之后的重试会变成 4xx，调用方将永远
           无法确认自己那次创建到底有没有生效。
        2. **项目存在性**。父资源不存在就谈不上「修订不属于该项目」，先给 404
           能让调用方修对参数，而不是收到一句误导性的归属错误。
        3. **带共享锁读修订**。冻结判定（第 4 步）读的是另一张表里的行，而那行
           会被 ``ACTIVE -> SUPERSEDED`` 并发改写——「先查后写」在并发下必被
           穿透，故必须持锁读、在锁内判定（锁模式见仓储层的
           ``shot_revision_lock_statement``）。
        4. **同项目 + 未冻结**。跨项目绑定给可读 400（库层复合外键是最后防线）；
           已 SUPERSEDED 的修订拒绝新规格——它是冻结态，不是「暂时不可用」。

        「同一修订至多一份规格」不在这里判：并发两个请求都能查到没有，唯一约束
        是唯一能真正拒绝的东西（冲突在 add 的异常分支里解释）。
        """
        project_id = project_id.strip()
        shot_revision_id = shot_revision_id.strip()
        created_by = created_by.strip()
        for name, value in (
            ("project_id", project_id),
            ("shot_revision_id", shot_revision_id),
            ("created_by", created_by),
        ):
            if not value:
                raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空")

        # 1. 幂等（先于父链锁读与冻结判定，理由见 docstring）
        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(
                    existing,
                    project_id=project_id,
                    shot_revision_id=shot_revision_id,
                    duration_ms=duration_ms,
                    fps_num=fps_num,
                    fps_den=fps_den,
                    cut_points_frames=cut_points_frames,
                )
                return existing

        # 2. 项目存在性
        if await self._project_repository.get_by_id(project_id) is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"项目不存在: {project_id}")

        # 3. 带共享锁读修订（与「修订被取代」串行化）
        revision = await self._shot_revision_repository.lock_for_child_write(shot_revision_id)
        if revision is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"镜头修订不存在: {shot_revision_id}")

        # 4a. 跨项目绑定：读写都在同一租户上下文内，所以「读到了」不等于「有权挂」。
        #     让可读错误先于 23503 出现，是为了让调用方知道该改哪个参数。
        if revision.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"镜头修订 {shot_revision_id} 属于项目 {revision.project_id}，"
                    f"与声明的项目 {project_id} 不一致（SS-2：绑定精确修订）"
                ),
            )

        # 4b. 冻结态不复用：已 SUPERSEDED 的修订只保留既有规格作为历史证据，
        #     不接受新规格（判定由状态声明派生，不在这里写 `if status is ...`）。
        SHOT_REVISION_LIFECYCLE.assert_child_creation_allowed(
            revision.status, child_object="ShotSpec"
        )

        spec = ShotSpec(
            entity_id=entity_id or uuid.uuid4().hex,
            project_id=project_id,
            shot_revision_id=shot_revision_id,
            duration_ms=duration_ms,
            fps_num=fps_num,
            fps_den=fps_den,
            cut_points_frames=list(cut_points_frames),
            aspect_ratio=aspect_ratio.strip(),
            coordinate_system=coordinate_system.strip(),
            camera_path=camera_path.strip(),
            subject_paths=subject_paths.strip(),
            shot_size_changes=shot_size_changes.strip(),
            previz_ref=previz_ref.strip(),
            control_signal_refs=list(control_signal_refs or []),
            created_by=created_by,
            tenant_id=tenant_id,
        )

        try:
            await self._repository.add(spec)
        except ReferentialIntegrityError as exc:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="规格绑定的修订或项目已变化，请基于最新数据重试",
            ) from exc
        except DuplicateEntityError as exc:
            # 两种冲突都是 409，但修复方向相反，必须分开说：
            # (a) 同一 entity_id 重放 → 回读同一请求，内容一致就返回；
            # (b) 该修订已有规格 → 点出占用者（调用方该改用已有规格或换修订）。
            if entity_id is not None:
                existing = await self._repository.get_by_id(entity_id)
                if existing is not None:
                    _assert_same_request(
                        existing,
                        project_id=project_id,
                        shot_revision_id=shot_revision_id,
                        duration_ms=duration_ms,
                        fps_num=fps_num,
                        fps_den=fps_den,
                        cut_points_frames=cut_points_frames,
                    )
                    return existing
            occupied = await self._repository.get_by_revision(shot_revision_id)
            if occupied is not None:
                raise DomainError(
                    ErrorRegistry.IDEMPOTENCY_CONFLICT,
                    detail=(
                        f"修订 {shot_revision_id} 已有一份规格"
                        f"（shot_spec_id={occupied.entity_id}）；"
                        "同一修订至多一份规格，改规格须新修订 + 新规格"
                    ),
                ) from exc
            raise DomainError(ErrorRegistry.INTERNAL_ERROR, detail="镜头规格写入冲突") from exc

        self._outbox_writer.record(
            aggregate_type="ShotSpec",
            aggregate_id=spec.entity_id,
            event_type="SHOTSPEC_CREATED",
            tenant_id=tenant_id,
            payload={
                "shot_spec_id": spec.entity_id,
                "project_id": spec.project_id,
                "shot_revision_id": spec.shot_revision_id,
                "duration_ms": spec.duration_ms,
                "cut_point_count": len(spec.cut_points_frames),
                "created_by": created_by,
                "tenant_id": str(tenant_id),
                "status": spec.status.value,
            },
        )
        return spec

    async def get_spec(self, entity_id: str) -> ShotSpec:
        spec = await self._repository.get_by_id(entity_id)
        if spec is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"镜头规格不存在: {entity_id}")
        return spec

    async def lock_spec(
        self,
        *,
        entity_id: str,
        locked_dimensions: str,
        lock_basis_ref: str,
        locked_by: str,
        tenant_id: UUID,
    ) -> ShotSpec:
        """锁定规格：四要素齐备才落 LOCKED。

        - 锁定维度：请求必填，规范化后非空（集合语义：排序去重，避免
          ``"a,b"`` 与 ``"b,a"`` 成为两个值）；
        - 依据：``lock_basis_ref`` 写进迁移事实的 ``authority_ref``；
        - 版本：乐观锁 + 事实行里的 ``entity_version``；
        - 审批事件：``SHOTSPEC_LOCKED`` 与状态推进同事务。
        """
        locked_by = locked_by.strip()
        lock_basis_ref = lock_basis_ref.strip()
        if not locked_by:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="locked_by 不能为空")
        if not lock_basis_ref:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="锁定必须给出 lock_basis_ref（锁定依据不能为空）",
            )

        spec = await self._repository.get_by_id(entity_id)
        if spec is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"镜头规格不存在: {entity_id}")

        # 终态不可原地复活：边表里 LOCKED 没有出边，重复锁定会被 edge_owned_by
        # 以「非法状态转移 LOCKED -> LOCKED」拒绝。这里提前给出更贴切的文案，
        # 判定仍由状态机兜底（两层同一事实，不是两套规则）。
        if spec.status is ShotSpecStatus.LOCKED:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"规格 {entity_id} 已锁定（LOCKED 为终态）："
                    "改规格须新修订 + 新规格，不得原地解锁"
                ),
            )

        spec.locked_dimensions = normalize_locked_dimensions(locked_dimensions)
        if not spec.locked_dimensions:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="锁定必须给出至少一个锁定维度",
            )

        await advance_status(
            spec,
            ShotSpecStatus.LOCKED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ShotSpec",
            event_type="SHOTSPEC_LOCKED",
            tenant_id=tenant_id,
            payload={
                "shot_spec_id": spec.entity_id,
                "project_id": spec.project_id,
                "shot_revision_id": spec.shot_revision_id,
                "locked_dimensions": spec.locked_dimensions,
                "lock_basis_ref": lock_basis_ref,
            },
            resource_type="ShotSpec",
            owner_action=SHOT_SPEC_LOCK_ACTION,
            conflict_detail="镜头规格并发冲突，请基于最新状态重试",
            actor_field="locked_by",
            actor=locked_by,
            authority_ref=lock_basis_ref,
        )
        return spec


def _assert_same_request(
    existing: ShotSpec,
    *,
    project_id: str,
    shot_revision_id: str,
    duration_ms: int,
    fps_num: int,
    fps_den: int,
    cut_points_frames: list[int],
) -> None:
    """幂等键复用必须是同一请求：内容不一致一律 409。

    只比请求体里那些「决定对象身份」的字段。锁定维度不参与比对——它是后续
    LOCK 用例写入的结果，不是创建请求的一部分。
    """
    if (
        existing.project_id != project_id
        or existing.shot_revision_id != shot_revision_id
        or existing.duration_ms != duration_ms
        or existing.fps_num != fps_num
        or existing.fps_den != fps_den
        or list(existing.cut_points_frames) != list(cut_points_frames)
    ):
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail="同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求",
        )
