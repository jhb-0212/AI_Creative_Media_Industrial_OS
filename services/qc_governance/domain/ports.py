"""质量治理域跨域端口（消费域定义协议，实现由组合根提供）。

为什么 QC 复查需要一条跨域读取：
- 会话的 ``qc_report_ref`` 是**引用**，不是文本。允许引用一个不存在的报告，
  就等于允许凭空造出一份"有证据的复核"；允许引用别的项目/别的资产版本的
  报告，就等于让证据跨项目漂流（审查第三轮 F2 的同一类问题）。
- 但 QC 报告属资产域（``asset_service.qc_reports``），本域不能直接读它的表
  （ADR-R28：读必须经过所有者，且用所有者域自己的数据库身份）。

因此这里只声明"我需要知道报告的三件事"，由组合根用资产域角色会话实现。
返回 ``QCReportFacts`` 而不是整个报告对象是刻意的：消费域不需要检测细节，
多给字段只会让两个域在字段变更上互相牵制。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class QCReportFacts:
    """所有者域对"这份 QC 证据是什么"的最小回答。"""

    qc_report_id: str
    project_id: str
    asset_version_id: str


class QCReportRefPort(Protocol):
    """QC 报告引用解析端口（不存在返回 ``None``，不抛异常）。"""

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        qc_report_id: str,
    ) -> QCReportFacts | None:
        """按报告号解析归属事实；不可见（含跨租户/跨组织）返回 ``None``。"""
        ...


__all__ = ["QCReportFacts", "QCReportRefPort"]
