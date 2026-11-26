"""组合根：跨域端口装配（``apps`` 层是唯一同时认识多个服务的地方）。

本文件只保留「把谁接到谁身上」这一件事，适配实现放在数据属主那一侧
（``services/asset_service/repository/qc_report_lookup.py``），契约放在中立层
（``packages/shared/evidence_ports.py``）。理由见那两个文件的模块文档。

为什么装配点必须显式且集中在 ``app.state``
------------------------------------------
报告核验器的失效方式是**静默的**：没接上时服务层 fail-closed 拒绝终结，症状是
「每次 QC 判定都 500」，看起来像业务 bug；而如果哪天有人把它改成「没接上就
放行」，症状会变成更糟的「看起来通过」。两种情况都没有语法/类型错误可捕获，
只有「装配点」与「使用点」共用一个常量、并在测试里对比一次，才能证伪漂移。

因此：键只在 ``QC_REPORT_LOOKUP_FACTORY`` 定义一次，写入方（本文件的
``register_cross_domain_ports``）与读取方（``qc_task_routes`` 的依赖函数）
都从这里对齐；``tests/unit/test_qc_report_lookup_wiring.py`` 负责把两边钉死。
"""

from __future__ import annotations

from fastapi import FastAPI

from services.asset_service.repository.qc_report_lookup import build_report_lookup
from services.production_planning.repository.generation_facts_lookup import (
    build_generation_facts_lookup,
)

# ``app.state`` 上的键。使用点因不得依赖 apps（分层规则 R1）只能写字面量，
# 故由测试断言两边一致。
QC_REPORT_LOOKUP_FACTORY = "qc_report_lookup_factory"
GENERATION_FACTS_LOOKUP_FACTORY = "generation_facts_lookup_factory"


def register_cross_domain_ports(app: FastAPI) -> None:
    """把跨域端口挂到应用装配面。未来新增端口在此逐条登记。"""
    setattr(app.state, QC_REPORT_LOOKUP_FACTORY, build_report_lookup)
    setattr(app.state, GENERATION_FACTS_LOOKUP_FACTORY, build_generation_facts_lookup)


__all__ = [
    "GENERATION_FACTS_LOOKUP_FACTORY",
    "QC_REPORT_LOOKUP_FACTORY",
    "register_cross_domain_ports",
]
