"""ORM 模型注册的唯一入口。

为什么需要单独一处
------------------
``Base.metadata`` 只包含**已被 import** 的模型模块。alembic 的 ``env.py`` 原先靠
一份手写 import 清单把各域模型凑齐；任何依赖 metadata 的断言（autogenerate、
「ORM 声明 vs 数据库实际」的漂移检查、schema 快照）都必须用同一份清单。

分成两份清单的后果不是「多写几行」：漏导一个域时，``env.py`` 里 autogenerate 会
静默少表（生成一份缺表的迁移），测试里断言会静默漏检并保持绿色——两处都不报错，
而「没检查到」与「检查通过」在输出上无法区分。

因此这里只声明一次模块清单，谁需要完整的 metadata 谁调用它；新增域只改这一处。
"""

from __future__ import annotations

import importlib

# 导入顺序无关紧要（模型之间靠字符串外键引用，不依赖先定义）。
# 新增域时只在此追加，不要在各调用点各写一份 import。
_ORM_MODULES: tuple[str, ...] = (
    "packages.identity.revoked_token_model",
    "packages.shared.approval_signature",
    "packages.shared.outbox",
    "packages.shared.status_transition",
    "services.asset_service.repository.orm",
    "services.budget_quota.repository.orm",
    "services.production_planning.repository.orm",
    "services.project_planning.repository.orm",
    "services.qc_governance.repository.orm",
    # 调度投影（qc_maintenance schema）：派生自 qc_tasks，但独立成表，
    # 不进 orm.py 是为了让「投影不是业务表」这条边界在文件层面可见。
    "services.qc_governance.repository.projection_orm",
    # 维护工单与审计账本（qc_maintenance schema）：维护身份的唯一授权载体。
    "services.qc_governance.repository.maintenance_orm",
)


def load_all_orm_models() -> None:
    """导入全部域 ORM 模块，使 ``Base.metadata`` 完整。幂等。"""
    for module_name in _ORM_MODULES:
        importlib.import_module(module_name)


def orm_module_names() -> tuple[str, ...]:
    """返回受管模块清单的副本（供测试断言清单本身未被悄悄缩短）。"""
    return _ORM_MODULES


__all__ = ["load_all_orm_models", "orm_module_names"]
