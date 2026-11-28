"""质量策略权威源（ADR-R42 C4）。

唯一事实源（不是本模块，本模块只是它的运行时投影）：
- 主文档 4.1：所有 S/A/B 项目绑定同一个逻辑质量配置 ``UNIFIED_HIGH_QUALITY``；
- ADR-R17 第 4 条：``quality_profile`` 收窄为单值，堵住白模/展示标准冒充成片；
- 契约修订 V205-R14-A5 残余风险条目：单值，扩展镜头级质量档必须先走契约修订。

因此 ``quality_profile_version`` 不是调用方可自报的自由字段，而是这条契约裁定
单值的投影。「已发布版本注册表」在本项目并不存在——引入一张 QP 表或配置枚举
会制造第二份事实源，正是 ADR-R42 C4 要消除的东西。

绑定时机（R42 裁决）：
- ProductionRoute 创建时写入该值（冻结介质）；
- GenerationTask 创建时按 production_route_ref 读取 route、校验归属与状态后
  冻结到任务；此后 T1/T2/T3 与 QC 全程读任务冻结值，不再重新解析当前口径。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry

# 契约裁定单值。改名必须先改契约（主文档 4.1 / ADR-R17 / 修订 A5）。
UNIFIED_HIGH_QUALITY = "UNIFIED_HIGH_QUALITY"


def require_quality_profile(version: str) -> str:
    """校验一个来自权威介质（route / task）的 QP 版本值。

    只接受契约单值：任何来自请求体的自报值、空值、未知版本都在这里 fail-closed。
    这个函数不「解析默认值」——默认值出现在源头（selection），不在消费端。
    """
    value = (version or "").strip()
    if value != UNIFIED_HIGH_QUALITY:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=(f"质量策略版本非法：{value!r}；契约唯一合法值为 {UNIFIED_HIGH_QUALITY!r}"),
        )
    return value


__all__ = ["UNIFIED_HIGH_QUALITY", "require_quality_profile"]
