"""ConstraintPack 应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现约束包用例：
  1. ``create_pack``（CONSTRAINT.PACK.CREATE）：Agent/TD/导演创建约束包草案
     （一律落 DRAFT；无锁定约束的镜头显式标 NOT_APPLICABLE）；
  2. ``lock_pack``（CONSTRAINT.PACK.LOCK）：授权人激活约束包（DRAFT→ACTIVE），
     仅锁定经批准的维度；审批人与版本固定。
- 契约口径（7.9 / ADR-088）：
  * Agent 仅能创建 DRAFT——创建路径永远产 DRAFT，激活只能走 lock_pack；
  * NOT_APPLICABLE 包同样须经 ACTIVE（证明「无锁定约束」是授权人判断
    而非默认值），由领域校验保证其不携带锁定维度与约束条目；
  * 激活发出契约登记的审计事件 CONSTRAINT_PACK_LOCKED。
- 身份三要素（主体 / 租户 / 组织）只从 Principal 取（可信来源）。
"""

from __future__ import annotations

import uuid
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from services.shot_planning.domain.constraint_pack import ConstraintPack
from services.shot_planning.domain.enums import ConstraintPackApplicability
from services.shot_planning.repository.constraint_pack_repo import (
    ConstraintPackRepository,
)


def _assert_same_pack_request(
    existing: ConstraintPack,
    *,
    project_id: str,
    shot_ids: list[str],
    applicability: ConstraintPackApplicability,
    version: int,
) -> None:
    """幂等键复用必须是同一请求：内容不一致一律 409。"""
    if (
        existing.project_id != project_id
        or existing.shot_ids != shot_ids
        or existing.applicability != applicability
        or existing.version != version
    ):
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail="同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求",
        )


class ConstraintPackService:
    """约束包创建与授权激活用例。"""

    def __init__(
        self,
        pack_repository: ConstraintPackRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._pack_repository = pack_repository
        self._outbox_writer = outbox_writer

    async def create_pack(
        self,
        *,
        project_id: str,
        shot_ids: list[str],
        applicability: ConstraintPackApplicability,
        locked_dimensions: list[str] | None = None,
        constraints: list[dict[str, Any]] | None = None,
        scene_id: str | None = None,
        shot_group_ids: list[str] | None = None,
        source_previz_revision: str | None = None,
        version: int = 1,
        principal: Principal,
        entity_id: str | None = None,
    ) -> ConstraintPack:
        """创建约束包草案（CONSTRAINT.PACK.CREATE，MEDIUM，Agent/TD/导演）。

        无论调用主体是 Agent 还是人类，创建一律落 DRAFT（7.9：Agent 仅能
        创建 DRAFT；激活必须走 CONSTRAINT.PACK.LOCK）。内容合法性
        （维度词典、条目必填键、NOT_APPLICABLE 语义）由聚合根构造期校验。
        """
        project_id = project_id.strip()
        if not project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )
        shot_ids = [s.strip() for s in shot_ids if s.strip()]
        if not shot_ids:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="shot_ids 不能为空"
            )
        created_by = principal.subject_id.strip()

        # 幂等：已存在同 id 且内容一致直接返回。
        if entity_id is not None:
            existing = await self._pack_repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_pack_request(
                    existing,
                    project_id=project_id,
                    shot_ids=shot_ids,
                    applicability=applicability,
                    version=version,
                )
                return existing

        pack = ConstraintPack(
            entity_id=entity_id or uuid.uuid4().hex,
            project_id=project_id,
            scene_id=scene_id,
            shot_ids=shot_ids,
            shot_group_ids=shot_group_ids or [],
            applicability=applicability,
            locked_dimensions=locked_dimensions or [],
            constraints=constraints or [],
            source_previz_revision=source_previz_revision,
            version=version,
            created_by=created_by,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )

        try:
            await self._pack_repository.add(pack)
        except DuplicateEntityError as exc:
            existing = await self._pack_repository.get_by_id(pack.entity_id)
            if existing is not None:
                _assert_same_pack_request(
                    existing,
                    project_id=project_id,
                    shot_ids=shot_ids,
                    applicability=applicability,
                    version=version,
                )
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="约束包写入冲突"
            ) from exc

        self._outbox_writer.record(
            aggregate_type="ConstraintPack",
            aggregate_id=pack.entity_id,
            event_type="CONSTRAINT_PACK_CREATED",
            tenant_id=principal.tenant_id,
            payload={
                "constraint_pack_id": pack.entity_id,
                "project_id": pack.project_id,
                "shot_ids": pack.shot_ids,
                "applicability": pack.applicability.value,
                "version": pack.version,
                "created_by": created_by,
                "tenant_id": str(principal.tenant_id),
                "status": pack.status.value,
            },
        )

        return pack

    async def lock_pack(
        self,
        *,
        pack_id: str,
        project_id: str,
        principal: Principal,
    ) -> ConstraintPack:
        """授权激活约束包（CONSTRAINT.PACK.LOCK，HIGH，导演/授权 TD）。

        硬约束：
        - Agent 拒绝（授权层已按 allowed_principals 拦截，此处防御性兜底；
          「Agent 不得自批自用」红线）；
        - 包必须存在且归属请求项目（防跨项目激活）；
        - 仅 DRAFT → ACTIVE（STALE/SUPERSEDED/REVOKED 不可原地复活）；
        - approved_by / approved_at 固化为激活人与激活时间，激活后版本锁定
          （变更须新建版本并 supersede 旧版本，7.9）。
        """
        if principal.is_agent:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=(
                    "Agent 不得激活约束包（仅导演/授权 TD 可执行 "
                    "CONSTRAINT.PACK.LOCK）"
                ),
            )

        pack_id = pack_id.strip()
        project_id = project_id.strip()
        if not pack_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="pack_id 不能为空"
            )
        if not project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )

        pack = await self._pack_repository.get_by_id(pack_id)
        if pack is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"约束包不存在: {pack_id}"
            )
        if pack.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"约束包 {pack_id} 归属项目 {pack.project_id} "
                    f"与请求 {project_id} 不一致"
                ),
            )

        expected_version = pack.entity_version
        approved_by = principal.subject_id.strip()
        pack.activate(approved_by=approved_by, audit_ref=f"CONSTRAINT.PACK.LOCK:{pack.entity_version}")
        await self._pack_repository.save(pack, expected_version)

        self._outbox_writer.record(
            aggregate_type="ConstraintPack",
            aggregate_id=pack.entity_id,
            event_type="CONSTRAINT_PACK_LOCKED",
            tenant_id=principal.tenant_id,
            payload={
                "constraint_pack_id": pack.entity_id,
                "project_id": pack.project_id,
                "applicability": pack.applicability.value,
                "locked_dimensions": pack.locked_dimensions,
                "version": pack.version,
                "approved_by": pack.approved_by,
                "approved_at": pack.approved_at.isoformat() if pack.approved_at else None,
                "tenant_id": str(principal.tenant_id),
                "status": pack.status.value,
            },
        )

        return pack

    async def supersede_pack(
        self,
        *,
        pack_id: str,
        superseded_by_pack_id: str,
        principal: Principal,
    ) -> ConstraintPack:
        """新版本包激活后取代旧包（7.9：变更须新建版本并 supersede 旧版本）。

        内部领域用例：由「新建版本 → 激活新版本 → 取代旧版本」的版本链
        调用，不暴露公共端点（无冻结授权动作）。
        """
        if principal.is_agent:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="Agent 不得取代约束包版本（仅授权人类可执行）",
            )
        old_pack = await self._pack_repository.get_by_id(pack_id)
        if old_pack is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"约束包不存在: {pack_id}"
            )
        new_pack = await self._pack_repository.get_by_id(superseded_by_pack_id)
        if new_pack is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"取代方约束包不存在: {superseded_by_pack_id}",
            )
        if new_pack.version <= old_pack.version:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"取代方版本 {new_pack.version} 必须大于被取代方版本 "
                    f"{old_pack.version}（版本递增不覆盖）"
                ),
            )

        expected_version = old_pack.entity_version
        old_pack.supersede(
            audit_ref=f"SUPERSEDED_BY:{superseded_by_pack_id}"
        )
        await self._pack_repository.save(old_pack, expected_version)
        return old_pack
