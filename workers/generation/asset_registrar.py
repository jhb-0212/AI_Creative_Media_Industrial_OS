"""R42 T0/T1 的注册端口实现（Generation 域本地事务）。

为什么实现在 Worker 侧
----------------------
它只负责把执行器算出的产物事实交给生成域服务，并在**自己的短事务**里提交。
T0 与 T1 各一个事务，中间不持有数据库会话——对象存储 I/O 不得持有事务或行锁
（ADR-R42 D4）。资产域写入由 T2 事件处理器负责，绝不在这里开第二个域的 Session。
"""

from __future__ import annotations

from typing import Any

from packages.shared.rls import set_tenant_context
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
)
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from workers.generation.executor import GeneratedArtifact


class SagaAssetRegistrar:
    def __init__(self, sessionmaker: Any) -> None:
        self._sessionmaker = sessionmaker

    def _service(self, session: Any) -> GenerationService:
        return GenerationService(
            SqlAlchemyGenerationTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
        )

    async def prepare(
        self, task: Any, *, locator: str, sha256: str, attempt_id: str
    ) -> None:
        async with self._sessionmaker() as session:
            await set_tenant_context(session, task.tenant_id)
            await self._service(session).prepare_artifact(
                task_id=task.entity_id,
                worker_id=task.lease_owner,
                locator=locator,
                sha256=sha256,
                attempt_id=attempt_id,
            )
            await session.commit()

    async def register(self, task: Any, artifact: GeneratedArtifact) -> None:
        async with self._sessionmaker() as session:
            await set_tenant_context(session, task.tenant_id)
            await self._service(session).confirm_artifact(
                task_id=task.entity_id,
                worker_id=task.lease_owner,
                attempt_id=artifact.attempt_id,
                locator=artifact.stored.locator,
                sha256=artifact.stored.sha256,
                media_type=artifact.stored.media_type,
            )
            await session.commit()


__all__ = ["SagaAssetRegistrar"]
