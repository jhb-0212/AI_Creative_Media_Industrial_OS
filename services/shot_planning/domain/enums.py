"""shot-planning 领域枚举。

严格对齐冻结契约：
- object_schemas.schema.json 中 ShotDraft / ShotRevision 的 x-afs-lifecycle
- 主文档 19.3 AI 拆镜流程、8.1 核心实体
"""

from __future__ import annotations

from enum import Enum


class ShotDraftStatus(str, Enum):
    """AI 拆镜草案状态。

    对齐契约：ShotDraft x-afs-lifecycle
    （DRAFT→CONFIRMED/SUPERSEDED；CONFIRMED 仅表示草案被采纳为正式
    ShotRevision 的输入，不是正式镜头；终态不可原地复活）。
    转移表权威定义见 domain/state_machine.py。
    """

    DRAFT = "DRAFT"            # 草案：AI 拆镜输出，待人工确认
    CONFIRMED = "CONFIRMED"    # 已采纳：被人工确认，作为正式 ShotRevision 输入
    SUPERSEDED = "SUPERSEDED"  # 被取代（终态，不可原地复活）


class ShotRevisionStatus(str, Enum):
    """正式镜头内容修订状态。

    对齐契约：ShotRevision 绑定稳定逻辑镜头 Shot，正式确认产生，
    版本递增不覆盖旧修订（8.2：ShotRevision 保存每次内容修订）。
    修订是 append-only 事实，无原地改写路径、无状态转移——最新版本
    由 revision 号派生，旧修订永远保留在表中。
    """

    ACTIVE = "ACTIVE"  # 落库即生效；无 SUPERSEDED 态（append-only，无需主动标记）


class ShotDraftSource(str, Enum):
    """草案来源主体类型（契约 source 字段取值）。"""

    AGENT = "agent"
    HUMAN = "human"
