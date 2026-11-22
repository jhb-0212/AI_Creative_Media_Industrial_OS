"""ConstraintPack 应用服务（约束包登记与生效，A23）。

两条用例对应急剧不同的风险档：
- ``create_pack``（``CONSTRAINT.PACK.CREATE``，MEDIUM）：Agent 也能建，但只能建
  DRAFT；作用镜头必须是本项目的真实镜头，七维度与条目八字段由领域层与库层各判一次。
- ``lock_pack``（``CONSTRAINT.PACK.LOCK``，HIGH）：把「审批人 / 审批时间 / 依据 /
  审批事件」四要素一次性落齐，DRAFT → ACTIVE。只有 ACTIVE 可作生成输入
  （7.9），所以这条边是整个包唯一有下游后果的转移。

为什么 NOT_APPLICABLE 的包也要走 ``lock_pack``
--------------------------------------------------
7.9 原文：「标 NOT_APPLICABLE 的包同样必须经 ACTIVE，才能证明『已确认无锁定
约束』是由授权人作出的判断，而不是默认值」。若允许创建即生效，「这些镜头没有
锁定约束」就成了一句谁也没说过的默认话——而下游会把它当成授权结论使用。

锁定为什么不走「读 → 改 → 存」
--------------------------------------------------
状态推进必须经 ``advance_status``（唯一入口，同事务写迁移事实与事件），否则
「状态改了、审计查不到」会静默发生，且 ``data.transition_single_entry`` 判据
会报红。依据（``lock_basis_ref``）写进迁移事实的 ``authority_ref``。

版本推进（ACTIVE → SUPERSEDED）为什么不在本批
--------------------------------------------------
它需要一个**授权动作**：7.9 说「变更须新建版本并 supersede 旧版本」，但 31.6
只登记了 CREATE / LOCK，没有任何动作拥有这条边。把 supersede 藏进 CREATE，等于
让「导入一份新包」顺带撤销一份已生效的授权——那正是 N-07 修掉的形态。
责任动作 ``CONSTRAINT.PACK.STATUS.CHANGE`` 已在契约登记（``not_implemented``），
三条治理边逐条登记在 ``state_machine_gaps.yaml``。在此之前，库层的部分唯一索引
（同包至多一个 ACTIVE）会把「新版本锁不上」变成一条可读的 409，而不是静默的
双 ACTIVE。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.project_planning.domain.constraint_pack import (
    ConstraintPack,
    normalize_dimensions,
    normalize_id_set,
)
from services.project_planning.domain.enums import ConstraintPackStatus
from services.project_planning.domain.state_machine import (
    CONSTRAINT_PACK_LOCK_ACTION,
    CONSTRAINT_PACK_STATUS_CHANGE_ACTION,
)
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.shot_planning_repositories import (
    ConstraintPackRepository,
    ShotRepository,
)
from services.project_planning.service.transition import advance_status


class ConstraintPackService:
    """约束包的创建、生效与查询。

    四个依赖都是构造期必需：包的归属证明靠项目仓储，作用镜头证明靠镜头仓储。
    可选依赖会退化成运行期 ``if xxx is None``——那正好是「守卫缺失」这种失效
    唯一不会被类型系统发现的位置。
    """

    def __init__(
        self,
        repository: ConstraintPackRepository,
        shot_repository: ShotRepository,
        project_repository: ProjectRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._repository = repository
        self._shot_repository = shot_repository
        self._project_repository = project_repository
        self._outbox_writer = outbox_writer

    async def create_pack(
        self,
        *,
        project_id: str,
        shot_ids: list[str],
        applicability: str,
        created_by: str,
        tenant_id: UUID,
        scene_id: str = "",
        shot_group_ids: list[str] | None = None,
        locked_dimensions: list[str] | None = None,
        constraints: list[dict[str, Any]] | None = None,
        source_previz_revision: str = "",
        version: int = 1,
        constraint_pack_id: str | None = None,
        entity_id: str | None = None,
    ) -> ConstraintPack:
        """登记一份约束包（DRAFT）。

        顺序是刻意的：

        1. **幂等回读**（按 entity_id）排在最前：重放一个当时成功过的请求是重试，
           不是新建。若先判镜头归属，镜头在下游被改名/删除之后的重试会变成 4xx，
           调用方将永远无法确认自己那次创建到底有没有生效。
        2. **项目存在性**：父资源不存在就谈不上「这些镜头不属于该项目」，先给 404
           能让调用方修对参数，而不是收到一句误导性的归属错误。
        3. **作用镜头归属**：库层触发器（``trg_constraint_packs_shots``）是最后
           防线，但它抛的 23503 会被统一翻译成「引用关系不成立，不可幂等」，调用方
           收到 409 时不知道该改哪个 id。这里先查一次，给可读的 400 并点出越界 id。

        「同包同版本至多一份」不在这里判：并发两个请求都能查到没有，唯一约束
        ``uq_constraint_pack_version`` 才是唯一能真正拒绝的东西（冲突在 add 的
        异常分支里解释）。
        """
        project_id = project_id.strip()
        created_by = created_by.strip()
        for name, value in (("project_id", project_id), ("created_by", created_by)):
            if not value:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )

        # 入参先规范化一次，后续「幂等比对」与「构造」都用这一份结果。
        # 两处各规范化一次会得到两个可能不同的值：比对用原始串、入库用规范化串，
        # 于是同一请求的第二次提交会因为空格差异被判成另一个请求。
        scene_id = scene_id.strip()
        source_previz_revision = source_previz_revision.strip()
        # 逻辑包标识：显式给出即「同一逻辑包的另一版本」，缺省与实体 id 相同
        # （首个版本两者自然一致）。它与 entity_id 是两个维度，理由见领域层
        # __post_init__ 的注释：混为一谈会让 CP-6 与版本唯一性同时退化。
        expected_pack_id = (constraint_pack_id or "").strip() or (entity_id or "")
        normalized_shots = normalize_id_set(
            shot_ids, field_name="shot_ids", required=True
        )
        groups = normalize_id_set(
            shot_group_ids or [], field_name="shot_group_ids", required=False
        )
        dimensions = normalize_dimensions(list(locked_dimensions or []))
        entries = list(constraints or [])

        # 1. 幂等（先于归属校验，理由见 docstring）
        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(
                    existing,
                    project_id=project_id,
                    constraint_pack_id=expected_pack_id,
                    shot_ids=normalized_shots,
                    applicability=applicability,
                    locked_dimensions=dimensions,
                    constraints=entries,
                    shot_group_ids=groups,
                    scene_id=scene_id,
                    source_previz_revision=source_previz_revision,
                    version=version,
                )
                return existing

        # 2. 项目存在性
        if await self._project_repository.get_by_id(project_id) is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"项目不存在: {project_id}"
            )

        # 3. 作用镜头必须真实存在且属于同一项目。
        #    RLS 已经保证「查不到他租户的镜头」，所以「查不到」既可能是跨租户
        #    也可能是跨项目——对调用方是同一件事：这些 id 不能用作本包的作用范围。
        found = await self._shot_repository.existing_ids_in_project(
            project_id, normalized_shots
        )
        unknown = sorted(set(normalized_shots) - found)
        if unknown:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"约束包的作用镜头不属于项目 {project_id}：{unknown}；"
                    "约束包只能约束本项目的镜头"
                ),
            )

        pack_entity_id = entity_id or uuid.uuid4().hex
        pack = ConstraintPack(
            entity_id=pack_entity_id,
            # 缺省与实体 id 相同（首个版本两者自然一致）。显式给出不同的值表示
            # 「同一逻辑包的另一版本」——此时 uq_constraint_pack_version 与
            # uq_constraint_pack_single_active 才是真的在约束什么。
            constraint_pack_id=expected_pack_id or pack_entity_id,
            project_id=project_id,
            scene_id=scene_id,
            shot_ids=normalized_shots,
            shot_group_ids=groups,
            applicability=applicability,
            locked_dimensions=dimensions,
            constraints=entries,
            source_previz_revision=source_previz_revision,
            version=version,
            created_by=created_by,
            tenant_id=tenant_id,
        )

        try:
            await self._repository.add(pack)
        except ReferentialIntegrityError as exc:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="约束包绑定的项目或镜头已变化，请基于最新数据重试",
            ) from exc
        except DuplicateEntityError as exc:
            # 三种冲突都是 409，但修复方向完全不同，必须分开说：
            # (a) 同一 entity_id 重放 → 回读同一请求，内容一致就返回；
            # (b) 同包同版本已有内容 → 点出占用者（该换 version 或换 pack_id）。
            if entity_id is not None:
                existing = await self._repository.get_by_id(entity_id)
                if existing is not None:
                    _assert_same_request(
                        existing,
                        project_id=project_id,
                        constraint_pack_id=expected_pack_id,
                        shot_ids=normalized_shots,
                        applicability=applicability,
                        locked_dimensions=dimensions,
                        constraints=entries,
                        shot_group_ids=groups,
                        scene_id=scene_id,
                        source_previz_revision=source_previz_revision,
                        version=version,
                    )
                    return existing
            # 占用者必须点名：只说「版本已存在」，调用方无法判断该递增 version
            # 还是该换 constraint_pack_id（两者都是合法修法，但方向相反）。
            occupant = await self._repository.get_by_pack_version(
                pack.constraint_pack_id, version
            )
            who = occupant.entity_id if occupant is not None else "未知实体"
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=(
                    f"约束包 {pack.constraint_pack_id} 的版本 {version} "
                    f"已被 {who} 占用；"
                    "7.9 要求变更新建版本（version 递增），不得原地覆盖"
                ),
            ) from exc

        self._outbox_writer.record(
            aggregate_type="ConstraintPack",
            aggregate_id=pack.entity_id,
            event_type="CONSTRAINT_PACK_CREATED",
            tenant_id=tenant_id,
            payload={
                "constraint_pack_id": pack.constraint_pack_id,
                "project_id": pack.project_id,
                "applicability": pack.applicability,
                "shot_count": len(pack.shot_ids),
                "locked_dimension_count": len(pack.locked_dimensions),
                "constraint_count": len(pack.constraints),
                "version": pack.version,
                "created_by": created_by,
                "tenant_id": str(tenant_id),
                "status": pack.status.value,
            },
        )
        return pack

    async def get_pack(self, entity_id: str) -> ConstraintPack:
        pack = await self._repository.get_by_id(entity_id)
        if pack is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"约束包不存在: {entity_id}"
            )
        return pack

    async def lock_pack(
        self,
        *,
        entity_id: str,
        approved_by: str,
        approved_at: datetime,
        lock_basis_ref: str,
        tenant_id: UUID,
    ) -> ConstraintPack:
        """使约束包生效（DRAFT → ACTIVE）：四要素齐备才落 ACTIVE。

        - 审批人：``approved_by`` 来自 Principal，不是请求体里的字符串；
          「不得为 Agent」由授权层保证（Agent/Worker 角色没有本动作权限），
          在这里按字符串猜主体类型就是 A13 批判过的伪职责分离。
        - 审批时间：``approved_at`` 是**审批事实**，不是「本次请求的时间」。
          服务端替调用方编一个时间，等于替审批人声明了一件他没声明的事。
        - 依据：``lock_basis_ref`` 写进迁移事实的 ``authority_ref``。
        - 审批事件：``CONSTRAINT_PACK_LOCKED`` 与状态推进同事务。

        ``approved_by`` / ``approved_at`` 写进实体（契约的 required 字段），
        与迁移事实里的 ``actor_subject`` 是同一件事的两处记录：前者是业务字段，
        后者是不可篡改的审计事实。刻意不共用一份——审计事实不允许被后续业务
        更新覆盖。
        """
        approved_by = approved_by.strip()
        lock_basis_ref = lock_basis_ref.strip()
        if not approved_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="approved_by 不能为空"
            )
        if not lock_basis_ref:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="锁定必须给出 lock_basis_ref（审批依据不能为空）",
            )

        pack = await self._repository.get_by_id(entity_id)
        if pack is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"约束包不存在: {entity_id}"
            )

        # 重复锁定提前给贴切文案；判定仍由状态机兜底（ACTIVE 没有回到 ACTIVE
        # 的边，edge_owned_by 会以「非法状态转移」拒绝）。两层同一事实。
        if pack.status is ConstraintPackStatus.ACTIVE:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"约束包 {entity_id} 已生效（ACTIVE）；"
                    "内容变更须新建版本，不得原地重锁"
                ),
            )

        pack.approved_by = approved_by
        pack.approved_at = approved_at

        try:
            await advance_status(
                pack,
                ConstraintPackStatus.ACTIVE,
                repository=self._repository,
                outbox_writer=self._outbox_writer,
                aggregate_type="ConstraintPack",
                event_type="CONSTRAINT_PACK_LOCKED",
                tenant_id=tenant_id,
                payload={
                    "constraint_pack_id": pack.constraint_pack_id,
                    "project_id": pack.project_id,
                    "applicability": pack.applicability,
                    "locked_dimensions": list(pack.locked_dimensions),
                    "version": pack.version,
                    "lock_basis_ref": lock_basis_ref,
                    "approved_at": approved_at.isoformat(),
                },
                resource_type="ConstraintPack",
                owner_action=CONSTRAINT_PACK_LOCK_ACTION,
                conflict_detail="约束包并发冲突，请基于最新状态重试",
                actor_field="approved_by",
                actor=approved_by,
                authority_ref=lock_basis_ref,
            )
        except DuplicateEntityError as exc:
            # 部分唯一索引 uq_constraint_pack_single_active：同一逻辑包已有生效
            # 版本。这不是「重复提交」，而是「版本推进没有入口」——必须说清。
            active = await self._repository.get_active_by_pack(pack.constraint_pack_id)
            occupant = active.entity_id if active is not None else "未知版本"
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=(
                    f"约束包 {pack.constraint_pack_id} 已有生效版本（{occupant}）；"
                    "同一逻辑包至多一个 ACTIVE，版本推进须先取代旧版本，"
                    "而取代入口 CONSTRAINT.PACK.STATUS.CHANGE 尚未实现"
                ),
            ) from exc
        return pack

    async def change_status(
        self,
        *,
        entity_id: str,
        target_status: ConstraintPackStatus,
        changed_by: str,
        tenant_id: UUID,
        revocation_basis_ref: str = "",
        stale_source_ref: str = "",
        superseding_pack_ref: str = "",
    ) -> ConstraintPack:
        """推进治理边（ACTIVE → REVOKED / STALE / SUPERSEDED）。

        三条边各自必须有可追溯依据（7.9 + state_machine_gaps 的解除条件）：
        - REVOKED：撤销依据（凭什么撤销）；
        - STALE：失效源（哪个上游锁定维度/Previz 失效）；
        - SUPERSEDED：新版本引用（被哪个新包取代）。

        依据写进迁移事实的 ``authority_ref`` 并进入事件 payload，不新增包级字段——
        这三件事都是「这一次推进」的证据，不是包的持久属性。
        """
        changed_by = changed_by.strip()
        if not changed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="changed_by 不能为空"
            )
        pack = await self._repository.get_by_id(entity_id)
        if pack is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"约束包不存在: {entity_id}"
            )

        authority_ref, evidence_key = _governance_evidence(
            target_status,
            revocation_basis_ref=revocation_basis_ref,
            stale_source_ref=stale_source_ref,
            superseding_pack_ref=superseding_pack_ref,
        )
        extra: dict[str, Any] = {}
        if evidence_key:
            extra[evidence_key] = authority_ref

        await advance_status(
            pack,
            target_status,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ConstraintPack",
            event_type="CONSTRAINT_PACK_STATUS_CHANGED",
            tenant_id=tenant_id,
            payload={
                "constraint_pack_id": pack.constraint_pack_id,
                "project_id": pack.project_id,
                "version": pack.version,
                **extra,
            },
            resource_type="ConstraintPack",
            owner_action=CONSTRAINT_PACK_STATUS_CHANGE_ACTION,
            conflict_detail="约束包并发冲突，请基于最新状态重试",
            actor_field="changed_by",
            actor=changed_by,
            authority_ref=authority_ref,
        )
        return pack


def _governance_evidence(
    target_status: ConstraintPackStatus,
    *,
    revocation_basis_ref: str,
    stale_source_ref: str,
    superseding_pack_ref: str,
) -> tuple[str, str]:
    """治理边依据：目标状态决定哪个依据必填，返回 (authority_ref, payload_key)。"""
    if target_status is ConstraintPackStatus.REVOKED:
        if not revocation_basis_ref.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="REVOKED 必须给出撤销依据（revocation_basis_ref）",
            )
        return revocation_basis_ref.strip(), "revocation_basis_ref"
    if target_status is ConstraintPackStatus.STALE:
        if not stale_source_ref.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="STALE 必须指认失效源（stale_source_ref）",
            )
        return stale_source_ref.strip(), "stale_source_ref"
    if target_status is ConstraintPackStatus.SUPERSEDED:
        if not superseding_pack_ref.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="SUPERSEDED 必须给出新版本引用（superseding_pack_ref）",
            )
        return superseding_pack_ref.strip(), "superseding_pack_ref"
    raise DomainError(
        ErrorRegistry.STATE_TRANSITION_DENIED,
        detail=(
            "CONSTRAINT.PACK.STATUS.CHANGE 只用于治理边"
            f"（REVOKED/STALE/SUPERSEDED），不处理 {target_status.value}"
        ),
    )


def _assert_same_request(
    existing: ConstraintPack,
    *,
    project_id: str,
    constraint_pack_id: str,
    shot_ids: list[str],
    applicability: str,
    locked_dimensions: list[str],
    constraints: list[dict[str, Any]],
    shot_group_ids: list[str],
    scene_id: str,
    source_previz_revision: str,
    version: int,
) -> None:
    """幂等键复用必须是同一请求：内容不一致一律 409。

    比对**规范化之后**的值（维度已排序、id 已去重保序），因为那才是入库形态；
    拿原始入参与库里形态比较，会让「同一请求第二次提交」因为排版差异被判成
    另一个请求。

    ``constraints`` 按列表逐项比较而不是排序后比较：条目顺序在契约里没有声明
    为无序集合，擅自排序会把真实的顺序变化当成「同一请求」。
    """
    if (
        existing.project_id != project_id
        or existing.constraint_pack_id != constraint_pack_id
        or list(existing.shot_ids) != list(shot_ids)
        or existing.applicability != applicability
        or existing.locked_dimensions != normalize_dimensions(locked_dimensions)
        or existing.constraints != constraints
        or list(existing.shot_group_ids) != list(shot_group_ids)
        or existing.scene_id != scene_id
        or existing.source_previz_revision != source_previz_revision
        or existing.version != version
    ):
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail="同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求",
        )


__all__ = ["ConstraintPackService"]
