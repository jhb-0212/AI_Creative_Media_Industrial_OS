"""shot_planning 领域（ADR-R27：镜头域独立数据所有权）。

本包承载 Shot / ShotDraft / ShotRevision / ShotSpec / ShotVisualPlan /
ConstraintPack 的领域模型、状态机与用例。与 project_planning 彻底隔离：
项目域不再写 Shot 数据，镜头域不直接读项目域 ORM（跨域只读走 Query API）。
"""

from __future__ import annotations

__all__: list[str] = []
