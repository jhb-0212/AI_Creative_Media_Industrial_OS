"""资产完整性检测器（QC Worker 的真实检测能力，Phase 3 后续）。

检测什么（只做能举证的事）：
1. 资产版本在资产域真实存在；
2. 资产的 `project_id` 与 QC 任务一致（不检别人项目的产物）；
3. `sha256` 是 64 位十六进制（可用于内容比对）；
4. `storage_ref` 能通过统一的 ObjectStoragePort 读回内容——定位符解析、
   根目录约束、符号链接识别、SHA256 复核全部由存储端口完成，检测器不自己
   解析路径（P1-01：#49 后 storage_ref 不再是字典，旧的 dict 路径解析必抛
   AttributeError，真实 QC 路径不可用）。

不做像素级/语义级质量判断——那需要模型网关与质量档案执行器，当前没有。
检测结论通过 `AssetService.create_qc_report`（`QC.REPORT.CREATE` 的同一服务层）
登记报告；没有报告就不允许终结 QC 任务，这条约束在领域层强制。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from adapters.storage.ports import ObjectStoragePort, StorageError
from packages.identity.principal import Principal
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.rls import set_organization_context, set_tenant_context
from packages.shared.storage_locator import parse_locator
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
    """读取资产域并登记 QC 报告；只依赖 sessionmaker 与存储端口，便于单测替换。

    ``storage`` 是读取产物内容的**唯一**入口（P1-01）。定位符解析、越界防护、
    符号链接识别、SHA256 复核都在存储端口内部完成；检测器只判断「能不能读回」
    并把结果记进报告，绝不自己拼路径——这正是 #49 之前 dict 路径解析出错的地方。
    """

    asset_sessionmaker: Any
    storage: ObjectStoragePort
    detector_version: str = "asset-integrity-v1"

    configured = True

    async def inspect(self, task: Any) -> QCInspection:
        async with self.asset_sessionmaker() as session:
            await set_tenant_context(session, task.tenant_id)
            # 组织级项目的资产版本经 ``EXISTS(projects)`` 继承组织面 RLS，
            # 不注入组织就读不到——会被误判成「资产不存在」而 blocked。
            # 组织只来自任务事实行（``task.organization_id``），不在这里推断。
            await set_organization_context(session, task.organization_id)
            assets = SqlAlchemyAssetRepository(session)
            service = AssetService(
                assets,
                SqlAlchemyQCReportRepository(session),
                SqlAlchemyOutboxWriter(session),
            )
            asset = await assets.get_by_id(task.asset_version_id)
            if asset is None:
                raise InspectionBlockedError(f"资产版本不存在: {task.asset_version_id}")
            if asset.status not in _REPORTABLE:
                raise InspectionBlockedError(f"资产状态 {asset.status.value} 不接受新的 QC 报告")

            # 归属核验是**执行的准入条件**，不是一条检测项（见函数文档）。
            assert_asset_within_task_scope(task, asset)

            findings = await self._check(task, asset)
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

    async def _check(self, task: Any, asset: Any) -> list[dict[str, Any]]:
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

        # 内容可验证分两环，缺一不可：
        #   ① 存储端口按定位符读回内容 —— 端口内部复核「内容 vs 定位符内嵌哈希」；
        #   ② 定位符内嵌哈希 == 资产声明的 sha256 —— 否则「内容是真的、但挂错了
        #      资产」，检测仪只证明产物存在却没证明它就是这项资产宣称的那份。
        # 两环合起来才等于修复前 `digest == asset.sha256` 的判定，但路径解析、
        # 越界、符号链接一律交给端口，检测器只做哈希比对这一件它能举证的事。
        locator = asset.storage_ref
        parsed, problem = parse_locator(locator)
        if parsed is None:
            findings.append(
                {
                    "check": "content_verifiable",
                    "passed": False,
                    "locator": locator,
                    "reason": f"非法存储定位符: {problem}",
                }
            )
            return findings
        digest_matches = parsed.sha256 == (asset.sha256 or "").lower()
        try:
            content = await self.storage.get(locator)
            findings.append(
                {
                    "check": "content_verifiable",
                    "passed": digest_matches,
                    "locator": locator,
                    "size_bytes": len(content),
                    "sha256_matches": digest_matches,
                    "reason": "" if digest_matches else "定位符内嵌哈希与资产声明的 sha256 不符",
                }
            )
        except StorageError as exc:
            findings.append(
                {
                    "check": "content_verifiable",
                    "passed": False,
                    "locator": locator,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
        return findings


__all__ = [
    "AssetIntegrityInspector",
    "InspectionBlockedError",
    "assert_asset_within_task_scope",
]
