"""QC 报告核验器（资产域提供的适配器）。

用途
----
QC 终结（``QC.TASK.PROCESS``）前必须核验「这份报告是否真属于本任务的资产
版本与同一租户」。报告在资产域（``qc_reports``），判定在 QC 域——这是一次
**跨域读取**，而分层规则 R2 禁止 ``services`` 之间互相 import 实现。

四种做法只有一种对：QC 域 import 资产域仓储（违反 R2）；资产域 import QC 域
的 DTO 声明端口（同样违反 R2，只是方向反过来）；把它登记成「平台能力」白名单
（这不是平台能力，白名单会变成常驻后门）；**契约下沉中立层 + 数据属主提供适配
+ 组合根装配**——也就是：

- 契约：``packages/shared/evidence_ports.py``（``ReportFacts`` / ``ReportLookup``）
- 适配：本文件（读取口径只此一处）
- 装配：``apps/api/wiring.py``（API）与 ``workers/qc/__main__.py``（Worker）

这样 QC 域只认识一个 ``Callable`` 形状，不知道资产域的存在；报告 schema 变了
不会出现「检测读到了、判定核验不到」的两份口径。

租户上下文由调用方负责
----------------------
本函数**不设置** RLS 作用域：唯一的注入点是 ``build_tenant_uow_dependency``
（SCOPE-4）。业务代码另开注入点会让某个入口「看起来注入了、其实没注」，这类
缺陷在数据上表现为「只看得见个人租户的数据」而不是报错。因此：

- API 侧传入的会话已由 ``get_asset_tenant_uow`` 设好上下文；
- Worker 侧（独立进程，无该依赖）在 ``workers/qc/__main__.py`` 里自行设置，
  再调用本函数——注入点集中在进程入口，不在域内。
"""

from __future__ import annotations

from typing import Any

from packages.shared.evidence_ports import ReportFacts, ReportLookup
from services.asset_service.repository.sqlalchemy_qc_repo import (
    SqlAlchemyQCReportRepository,
)


def build_report_lookup(session: Any) -> ReportLookup:
    """把资产域会话封装成报告核验器；会话的租户上下文由调用方保证。

    失败契约：查不到返回 ``None``，由使用方翻译成「报告不存在」。不在此抛异常：
    「不存在」是业务拒绝，「读取失败」应上抛为基础设施错误，压平成同一个结论
    会让排障无法区分真实越权与真实缺失。
    """

    async def lookup(qc_report_id: str, tenant_id: Any) -> ReportFacts | None:
        report = await SqlAlchemyQCReportRepository(session).get_by_id(qc_report_id)
        if report is None:
            return None
        return ReportFacts(
            asset_version_id=report.asset_version_id,
            tenant_id=report.tenant_id,
        )

    return lookup


__all__ = ["build_report_lookup"]
