"""资产域的跨域证据端口（只定义消费方需要的事实，不泄漏他域对象）。

为什么需要端口而不是直接 import shot_planning 的仓储：
`DeliveryManifest.spec_ref` 指向 ShotSpec——那是镜头规划域的对象。资产域
**判断不了**它是否成立，只能问所有者（ADR-R28「跨域只读经所有者」）。
如果资产域直接去读 `shot_planning` 的表，就等于给自己开了一张他域表的
长期通行证：将来镜头域改表结构时，破坏点会出现在资产域。

代价要说清：端口只能返回所有者域**愿意公开**的最小事实。这里返回
`spec_id / project_id / shot_id / status / entity_version` 五个字段，
足以完成"证据存在且归属同一项目"的比对，但不包含任何规格内容——
重建交付不需要重算规格，只需要确认它没被换掉/删掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class SpecEvidence:
    """ShotSpec 的所有者可公开事实（用于交付清单重建时逐项比对）。"""

    spec_id: str
    project_id: str
    shot_id: str
    #: 规格在所有者域的状态值（原文，不在此处映射成资产域枚举——
    #: 两个域的取值集合不同，映射会制造第二份权威）。
    status: str
    entity_version: int


class SpecEvidencePort(Protocol):
    """按 id 解析 ShotSpec 事实；不可见/不存在一律返回 ``None``。

    为什么不区分"不存在"与"无权读"：调用方是同一租户内的证据重建，
    区分两者只会让端口承担授权语义；不可见即 None，由调用方按"缺证据"处理
    （而"缺证据"正是要显式列出来的东西）。
    """

    async def resolve(
        self,
        *,
        tenant_id: UUID,
        organization_id: UUID | None,
        spec_id: str,
    ) -> SpecEvidence | None:
        """解析一条规格证据。"""
        ...
