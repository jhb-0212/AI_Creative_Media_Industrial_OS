"""Worker 侧 GenerationSpec 解析器（ADR-R47 §5，P2）。

四步校验，任何一步失败都给出可区分的失败码
----------------------------------------
1. ``prompt_ref`` 等式：任务必须写 ``genspec:<id>``，且 ``generation_spec_id`` 一致；
2. 租户/存在：规格必须存在且属于本任务租户（RLS + 显式比对双保险）；
3. ``schema_version`` 支持集：未知版本 fail-closed，不尝试「尽力解读」；
4. 重算两列哈希：``spec_sha256`` 覆盖身份域，``prompt_sha256`` 覆盖正文。

第 4 步刻意在 Worker 重算而不是信数据库：库里的哈希只能证明「写入时算过一次」，
不能证明「现在读到的内容与那份哈希是同一份内容」——后者才是执行依据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.shared.rls import set_tenant_context
from services.production_planning.domain.generation_spec import (
    SPEC_SCHEMA_VERSION,
    compute_spec_sha256,
)
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
)


class SpecUnresolvableError(Exception):
    """规格无法作为执行依据；``code`` 是稳定的失败码（写进证据前缀）。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class ResolvedGenerationInput:
    """执行所需的全部输入事实（来自冻结规格，不来自任务行）。"""

    prompt: str
    media_type: str
    size: str
    model_id: str
    provider: str


class RepositorySpecResolver:
    """从生成域仓储读取规格并校验；短只读事务，读后即释放连接。"""

    def __init__(self, sessionmaker: Any) -> None:
        self._sessionmaker = sessionmaker

    async def __call__(self, task: Any) -> ResolvedGenerationInput:
        """执行器按可调用对象装配（``resolve_input=resolver``）。"""
        return await self.resolve(task)

    async def resolve(self, task: Any) -> ResolvedGenerationInput:
        spec_id = str(getattr(task, "generation_spec_id", "") or "").strip()
        task_prompt_ref = str(getattr(task, "prompt_ref", "") or "")
        if not spec_id or task_prompt_ref != f"genspec:{spec_id}":
            raise SpecUnresolvableError(
                "spec-unresolvable",
                f"任务 {getattr(task, 'entity_id', '?')} 的 prompt_ref 与 generation_spec_id 不成等式",
            )

        async with self._sessionmaker() as session:
            await set_tenant_context(session, task.tenant_id)
            repository = SqlAlchemyGenerationTaskRepository(session)
            spec = await repository.get_spec(spec_id)
            if spec is None:
                raise SpecUnresolvableError("spec-missing", f"规格 {spec_id} 不存在或不属于本租户")
            if spec.tenant_id != task.tenant_id:
                raise SpecUnresolvableError(
                    "spec-foreign-tenant", f"规格 {spec_id} 的租户与任务不一致"
                )
            if spec.schema_version > SPEC_SCHEMA_VERSION:
                raise SpecUnresolvableError(
                    "spec-schema-version-unsupported",
                    f"规格 {spec_id} 的 schema_version={spec.schema_version} "
                    f"超出支持集 {SPEC_SCHEMA_VERSION}",
                )
            if spec.project_id != getattr(
                task, "project_id", ""
            ) or spec.shot_revision_id != getattr(task, "shot_revision_id", ""):
                raise SpecUnresolvableError(
                    "spec-mismatch",
                    f"规格 {spec_id} 绑定 {spec.project_id}/{spec.shot_revision_id}，"
                    f"任务为 {getattr(task, 'project_id', '?')}/{getattr(task, 'shot_revision_id', '?')}",
                )

            recomputed = compute_spec_sha256(str(spec.tenant_id), spec.identity_payload())
            if recomputed != spec.spec_sha256:
                raise SpecUnresolvableError(
                    "spec-tampered",
                    f"规格 {spec_id} 内容哈希重算为 {recomputed}，与登记 {spec.spec_sha256} 不一致",
                )

            # 第 5 步（提案 §5 的失败码表）：执行前复核 ShotSpec 仍 LOCKED 且仍是
            # 规格绑定的那一份。创建时已校验过，但规格冻结后 ShotSpec 仍可能被
            # 治理流程改动；Worker 不重新派生，只确认「依据仍然成立」。
            facts = await repository.get_shot_spec_for_revision(
                project_id=spec.project_id,
                shot_revision_id=spec.shot_revision_id,
                tenant_id=task.tenant_id,
                shot_spec_id=spec.shot_spec_id,
            )
            if facts is None:
                raise SpecUnresolvableError(
                    "spec-shot-spec-mismatch",
                    f"规格 {spec_id} 绑定的 ShotSpec {spec.shot_spec_id} 已不存在",
                )
            if facts.status != "LOCKED":
                raise SpecUnresolvableError(
                    "spec-shot-spec-unlocked",
                    f"ShotSpec {facts.entity_id} 当前为 {facts.status}，不再是 LOCKED",
                )
            if (
                facts.entity_id != spec.shot_spec_id
                or facts.shot_revision_id != spec.shot_revision_id
            ):
                raise SpecUnresolvableError(
                    "spec-shot-spec-mismatch",
                    f"ShotSpec {facts.entity_id} 与规格 {spec_id} 的绑定不一致",
                )
            return ResolvedGenerationInput(
                prompt=spec.prompt_text,
                media_type=spec.media_type,
                size=spec.size,
                model_id=spec.model_id,
                provider=spec.provider,
            )


__all__ = [
    "RepositorySpecResolver",
    "ResolvedGenerationInput",
    "SpecUnresolvableError",
]
