"""资产完整性检测器（QC Worker 的真实检测能力，Phase 3 后续）。

检测什么（只做能举证的事）：
1. 资产版本在资产域真实存在；
2. 资产的 `project_id` 与 QC 任务一致（不检别人项目的产物）；
3. `sha256` 是 64 位十六进制（可用于内容比对）；
4. `storage_ref` 能定位内容；若提供本地路径，则要求文件存在且哈希一致。

不做像素级/语义级质量判断——那需要模型网关与质量档案执行器，当前没有。
检测结论通过 `AssetService.create_qc_report`（`QC.REPORT.CREATE` 的同一服务层）
登记报告；没有报告就不允许终结 QC 任务，这条约束在领域层强制。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from packages.identity.principal import Principal
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.rls import set_tenant_context
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.repository.sqlalchemy_asset_repo import (
    SqlAlchemyAssetRepository,
)
from services.asset_service.repository.sqlalchemy_qc_repo import (
    SqlAlchemyQCReportRepository,
)
from services.asset_service.service.asset_service import AssetService
from workers.qc.worker import QCInspection

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# `storage_ref` 里常见的本地定位键；命中任一即视为「可本地校验内容」。
_LOCAL_PATH_KEYS = ("path", "local_path", "file", "file_path")

# 资产处于这两种状态时才能追加 QC 报告；其它状态说明它已进入下游流程，
# 再补技术报告会与既成的审核结论冲突。
_REPORTABLE = frozenset({AssetStatus.GENERATED, AssetStatus.TECHNICAL_QC})


class InspectionBlockedError(RuntimeError):
    """无法完成检测（不是「检测不合格」）。

    例如资产不存在、状态已不接受报告。不得写假的 REJECTED：那会把上游缺失
    伪装成质量结论。Worker 会让租约过期回队，缺口以 `attempt_count` 可见。
    """


def assert_asset_within_task_scope(task: Any, asset: Any) -> None:
    """归属核验：QC 任务声明的项目必须等于资产实际归属。

    这是**执行的准入条件**，不是一条检测项。若只把它记成 finding，随后仍会写
    报告、仍会把资产从 GENERATED 推进到 TECHNICAL_QC——于是同一租户内项目 A
    的任务可以通过错误引用影响项目 B 的资产。「发现了错误引用」不等于
    「阻止了错误引用的副作用」。

    抽成模块级函数而不是内联在 ``inspect`` 里，是为了让它能在不连数据库的
    情况下被直接证伪：判定逻辑与 I/O 分离，测试验的是同一条实现。
    """
    if asset.project_id != task.project_id:
        raise InspectionBlockedError(
            f"QC 任务声明项目 {task.project_id}，但资产 {asset.entity_id} "
            f"实际归属 {asset.project_id}：跨项目引用，拒绝检测与写入"
        )


@dataclass
class AssetIntegrityInspector:
    """读取资产域并登记 QC 报告；只依赖 sessionmaker，便于单测替换。"""

    asset_sessionmaker: Any
    detector_version: str = "asset-integrity-v1"

    configured = True

    async def inspect(self, task: Any) -> QCInspection:
        async with self.asset_sessionmaker() as session:
            await set_tenant_context(session, task.tenant_id)
            assets = SqlAlchemyAssetRepository(session)
            service = AssetService(
                assets,
                SqlAlchemyQCReportRepository(session),
                SqlAlchemyOutboxWriter(session),
            )
            asset = await assets.get_by_id(task.asset_version_id)
            if asset is None:
                raise InspectionBlockedError(
                    f"资产版本不存在: {task.asset_version_id}"
                )
            if asset.status not in _REPORTABLE:
                raise InspectionBlockedError(
                    f"资产状态 {asset.status.value} 不接受新的 QC 报告"
                )

            # 归属核验是**执行的准入条件**，不是一条检测项（见函数文档）。
            assert_asset_within_task_scope(task, asset)

            findings = self._check(task, asset)
            verdict: Literal["PASSED", "REJECTED"] = (
                "PASSED" if all(f["passed"] for f in findings) else "REJECTED"
            )
            _asset, report = await service.create_qc_report(
                asset_version_id=task.asset_version_id,
                quality_profile_version=task.quality_profile_version,
                detector_versions={"asset-integrity": self.detector_version},
                findings=findings,
                principal=Principal(
                    subject_id="qc-worker",
                    tenant_id=task.tenant_id,
                    roles=frozenset({"qc"}),
                ),
            )
            await session.commit()
            return QCInspection(
                verdict,
                report.qc_report_id,
                [f"asset-sha256:{asset.sha256}"],
            )

    def _check(self, task: Any, asset: Any) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []

        # 走到这里归属必然一致（``inspect`` 已前置阻断）。保留这条不是为了判定，
        # 而是让报告里留下「归属已核验」的事实——报告是 append-only 证据，
        # 读报告的人无法从别处推断当时是否核验过。
        findings.append(
            {
                "check": "project_scope",
                "passed": True,
                "expected": task.project_id,
                "actual": asset.project_id,
            }
        )
        sha_ok = bool(_SHA256_RE.fullmatch(asset.sha256 or ""))
        findings.append(
            {
                "check": "sha256_format",
                "passed": sha_ok,
                "actual": asset.sha256,
            }
        )

        locator = self._local_path(asset.storage_ref)
        if locator is None:
            findings.append(
                {
                    "check": "content_verifiable",
                    "passed": False,
                    "reason": "storage_ref 未提供本地路径，内容不可验证",
                }
            )
        else:
            path = Path(locator)
            exists = path.is_file()
            digest = ""
            if exists:
                digest = _sha256_of(path)
            findings.append(
                {
                    "check": "content_verifiable",
                    "passed": exists and digest == asset.sha256,
                    "path": locator,
                    "exists": exists,
                    "sha256_matches": digest == asset.sha256 if exists else False,
                }
            )
        return findings

    @staticmethod
    def _local_path(storage_ref: dict[str, Any]) -> str | None:
        for key in _LOCAL_PATH_KEYS:
            value = storage_ref.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "AssetIntegrityInspector",
    "InspectionBlockedError",
    "assert_asset_within_task_scope",
]
