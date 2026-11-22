"""ScriptRevision 聚合根（领域模型，纯 Python，无框架依赖）。

对齐契约：
- 31.6 Action Registry → SCRIPT.IMPORT：resource_type=ScriptRevision，
  scope=project/episode，MEDIUM 风险，Token=Y，导演/编剧/制片，
  检查项目写权限、文件安全与导入来源 → SCRIPT_IMPORTED。
- ADR-032：AI 剧本拆镜输出默认草案，人工确认前不得写入 ScriptLock。
  剧本导入产出 DRAFT 状态的可编辑版本，锁定是独立动作（SCRIPT.LOCK.APPROVE）。
- C.3.1 通用实体信封（收敛到 domain/entity.py 的 Entity 基类）。

设计要点（不屎山代码）：
- 领域模型纯数据 + 行为，不依赖 ORM / FastAPI。
- 状态变更经显式方法，未登记转移一律拒绝（31.5）。
- 终态（SUPERSEDED）不可原地复活（红线）。
- 信封公共字段/校验/序列化继承自 Entity。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.state_machine import SCRIPT_LIFECYCLE


@dataclass
class ScriptRevision(Entity):
    """剧本版本聚合根。

    一次导入产生一个版本记录；新版本导入使旧版本转 SUPERSEDED（终态）。
    「不可变」语义指**版本内容不可变**（已入库的版本记录不原地覆盖内容，
    状态只能沿状态机向前迁移，终态不可复活），而非 Python frozen dataclass
    的字段级不可变。
    """

    ENTITY_TYPE = "ScriptRevision"

    # 数据所有权：信封 owner_module 覆写为本模块（基类默认 core）。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    project_id: str = ""
    episode_ref: str = ""
    title: str = ""
    status: ScriptStatus = ScriptStatus.DRAFT
    # 锁定申请人（LOCK_PENDING 态时写入）：申请锁定动作的调用者真实主体，
    # 批准动作据此做职责分离比对（approved_by != requested_by）。
    requested_by: str | None = None

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        for name, max_len in (
            ("project_id", 64),
            ("episode_ref", 64),
            ("title", 200),
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def request_lock(
        self, requested_by: str, audit_ref: str, action: str
    ) -> ScriptRevision:
        """提交锁定申请（DRAFT→LOCK_PENDING），登记申请人真实主体。

        职责分离（红线）的前半：申请人 = 调用本动作的主体，写入 requested_by，
        供批准动作做「批准人 != 申请人」的运行时比对。

        状态改经 ``transition_to``（而不是 ``self.status = ...``）：本聚合只允许
        一条改状态的路径，否则审计事实行会在这条转移上静默缺失——而「谁申请锁定」
        恰恰是最需要留痕的一条（ADR-R23 §D2-b）。

        ``action`` 透传给 ``transition_to`` 做边所有权判定：DRAFT→LOCK_PENDING
        归 ``SCRIPT.LOCK.REQUEST``，由 ``SCRIPT.IMPORT`` 请求这条边会被拒。
        """
        self.transition_to(ScriptStatus.LOCK_PENDING, audit_ref, action)
        self.requested_by = requested_by
        return self

    def approve_lock(
        self, approved_by: str, audit_ref: str, action: str
    ) -> ScriptRevision:
        """批准锁定（LOCK_PENDING→LOCKED），登记批准人真实主体。

        职责分离（红线）的后半：``approved_by`` 与申请人（``requested_by``）的
        比对由服务层在调用前完成（需要读库里的申请人）。

        批准人**不落实体字段**（本聚合没有 approved_by 列），而是落在两处第一类
        事实里：`SCRIPT_LOCK_APPROVED` 事件的 payload 与状态迁移事实的
        ``actor_subject``。此处只负责状态与版本号推进。

        ``action`` 透传给 ``transition_to``：LOCK_PENDING→LOCKED 归
        ``SCRIPT.LOCK.APPROVE``。这条边是审批链的关键一跳，所有权判定把它与
        「导入新版本」彻底分开。
        """
        self.transition_to(ScriptStatus.LOCKED, audit_ref, action)
        return self

    def transition_to(
        self, target: ScriptStatus, audit_ref: str, action: str
    ) -> ScriptRevision:
        """执行状态迁移：结构合法性 + 边所有权（31.5 / N-07）。

        本对象的三类动作分领五条边（申请/批准/取代），因此所有权判定在这里
        有实际后果：``SCRIPT.IMPORT`` 不能把剧本推成 ``LOCKED``，
        ``SCRIPT.LOCK.APPROVE`` 也不能把剧本推成 ``SUPERSEDED``。
        """
        SCRIPT_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。"""
        data = self._envelope_dict()
        data.update(
            {
                "project_id": self.project_id,
                "episode_ref": self.episode_ref,
                "title": self.title,
                "requested_by": self.requested_by,
            }
        )
        return data
