"""跨域读取端口的契约（不属于任何单个领域）。

为什么契约放在 ``packages`` 而不是某个服务里
-------------------------------------------
有些准入核验天然要读**另一个域**的事实：QC 终结要确认「这份报告是否真属于
本任务的资产版本与同一租户」，而报告在资产域、判定在 QC 域。

四种做法只有一种对：

① QC 域 import 资产域仓储 —— 违反 R2（领域之间只经 API/命令 + Outbox 交互）；
② 资产域 import QC 域的 DTO 声明端口 —— 同样违反 R2，只是方向反过来；
③ 把契约登记成「平台能力」白名单 —— 这不是平台能力，是一次具体的数据读取，
   而且白名单会变成常驻后门；
④ **契约下沉到中立层**（本文件）：使用方声明需要什么事实，数据属主提供适配，
   组合根把两者接起来。两边都只依赖 ``packages``，谁都不认识谁。

端口只带**核验需要的最少字段**：核验要的是归属事实，不是报告内容。让端口返回
整份报告会把「判定该不该通过」的能力从 QC 域泄漏到数据搬运层，也会诱导调用方
在装配处做业务判断。

失败契约：查不到就是 ``None``，由使用方翻译成「报告不存在」。
不在此处抛异常——「不存在」与「读取失败」是两件事，前者是业务拒绝，
后者应上抛成基础设施错误，压平成同一个结论会让排障无法区分。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ReportFacts:
    """一份 QC 报告的归属事实：报告存在 ⟹ 一定有资产版本与租户。"""

    asset_version_id: str
    tenant_id: UUID


# ``(qc_report_id, tenant_id) -> ReportFacts | None``
ReportLookup = Callable[[str, UUID], Awaitable[ReportFacts | None]]


__all__ = ["ReportFacts", "ReportLookup"]
