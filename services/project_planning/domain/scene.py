"""Scene 聚合根（领域模型，纯 Python，无框架依赖）。

对齐契约：
- 8.1 核心实体：Project → Episode → Scene → Shot 的项目层级聚合根，
  Scene 归属于 Episode，作用域收敛到 Project。
- contracts/state_machines/state_machines.yaml → objects → Scene：
  DRAFT→ACTIVE→ARCHIVED（`source_kind: derived_from_parent_lifecycle`）。
- contracts/schemas/object_schemas.schema.json#Scene。

设计要点：
- **单一身份**：``scene_id`` 不是独立字段，而是 ``entity_id`` 的只读投影。
  数据库有 ``CHECK (entity_id = scene_id)``，契约也要求两者同值；域模型里再
  存一份就多出一个可以与之不一致的副本——那种不一致不会报错，只会让同一行
  有两个身份。用 property 表达，物理上不可能不一致。
- 状态变更只经 ``transition_to``，未登记转移一律拒绝（31.5）。
- 终态（ARCHIVED）不可原地复活：边表里没有出边，不靠额外 if 拦住。
- 信封公共字段/校验/序列化继承自 Entity。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import SceneStatus
from services.project_planning.domain.state_machine import SCENE_LIFECYCLE


@dataclass
class Scene(Entity):
    """场次聚合根（归属于 Episode，作用域收敛到 Project）。

    只有身份、业务编号与生命周期三件事。场次的描述性内容（场次名/地点/日夜/
    进出状态）由拆镜草案的场次层承载（``ShotBreakdownDraft``），本对象重复一份
    就会与之产生两个「同一个场次」的叙述——这类重复在本项目的教训是：
    它们不会报错，只会慢慢变得不一致。
    """

    ENTITY_TYPE = "Scene"

    # 数据所有权：信封 owner_module 覆写为本模块。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    project_id: str = ""
    episode_id: str = ""
    scene_code: str = ""
    status: SceneStatus = SceneStatus.DRAFT

    #: 组织归属键（内部键，不进 API 响应，与 tenant_id 同构）。
    #:
    #: 为什么需要它：``scenes.organization_id`` 是 NOT NULL，且三列归属外键
    #: (project_id, tenant_id, organization_id) → projects 要求它与项目一致。
    #: 服务层从父项目拷贝，调用方不得自填——自己填就等于允许「场次挂在 A 项目
    #: 却写 B 组织」，而那正是批次四要消灭的形态。
    organization_id: UUID | None = None

    @property
    def scene_id(self) -> str:
        """场次稳定主键（8.2：scene_id 不得复用）。

        它同时是 ``Shot.scene_id`` 的引用目标与复合外键的被引用列。
        刻意做成 ``entity_id`` 的投影而不是独立字段——两套标识各自生成即是两套
        身份，而数据库只拦得住「写入时不一致」，拦不住「代码里本来是两份」。
        """
        return self.entity_id

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        for name, max_len in (
            ("project_id", 64),
            ("episode_id", 64),
            ("scene_code", 64),
        ):
            value = getattr(self, name)
            if not value or not str(value).strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
            if len(str(value)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def transition_to(
        self, target: SceneStatus, audit_ref: str, action: str
    ) -> Scene:
        """执行状态迁移：结构合法性 + 边所有权（31.5 / N-07）。

        ``action`` 是准入凭据而非标签：当前三条边都由 ``SCENE.STATUS.CHANGE``
        独占，但判定照常执行——规则的价值在于「未来新增专属动作时无需记得
        来加检查」。
        """
        SCENE_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。

        不输出 ``tenant_id`` / ``organization_id``：两者都是内部隔离与归属键，
        不是对外契约字段（object_schemas 全文无这两个键）。
        """
        data = self._envelope_dict()
        data.update(
            {
                "scene_id": self.scene_id,
                "scene_code": self.scene_code,
                "project_id": self.project_id,
                "episode_id": self.episode_id,
            }
        )
        return data
