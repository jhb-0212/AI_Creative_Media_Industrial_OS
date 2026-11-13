"""领域层公共实体信封（C.3.1 的单一事实源）。

设计要点（不屎山代码 + 消除重复机制 R4）：
- Project / ScriptRevision / Episode 三个聚合根重复声明了同一套 C.3.1
  通用实体信封字段（entity_id/entity_type/schema_version/entity_version/
  status/owner_module/created_at/created_by/updated_at/audit_ref）、同一套
  ``_utcnow()`` 时间源、同一套 ``__post_init__`` 非空校验、同一套
  ``to_dict`` 信封序列化。
- 这些是**同一机制的多份复制**，不是不同业务概念，必须收敛到单一基类。
  三个聚合根的业务差异（project_name/project_tier/title 等）保留在各自
  类中，只把「信封机制」上提。
- 本模块不含任何 ORM / FastAPI 依赖，是纯数据 + 行为，保证可测试可演进。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

# 契约 C.3.1 通用实体信封的 schema_version 固定值（V2.0.5）。
SCHEMA_VERSION = 205
OWNER_MODULE = "project-planning"


def utcnow() -> datetime:
    """统一 UTC 时间源（8.2 规则：所有时间统一存 UTC）。

    收敛为模块级单一函数，替换三个聚合根各自的 ``_utcnow`` 复制。
    """
    return datetime.now(UTC)


# 信封字段名 -> 最大长度（对齐 ORM 列定义），供统一非空/超长校验。
# 空字符串视为空。业务字段（project_name 等）由各聚合根单独校验。
_ENVELOPE_MAX_LEN: dict[str, int] = {
    "entity_id": 64,
    "created_by": 64,
}


@dataclass
class Entity:
    """C.3.1 通用实体信封基类（领域层单一事实源）。

    字段（实例数据）：
    - entity_id / created_by 由子类构造时传入（必填）。
    - schema_version / entity_version / owner_module / created_at / updated_at /
      audit_ref 有统一默认值。

    ``entity_type`` 是聚合根固定常量（"Project"/"ScriptRevision"/"Episode"），
    不作为实例字段，由子类声明为类属性 ``ENTITY_TYPE`` 覆盖。

    状态（status）是各聚合根的业务枚举字段，由子类声明。

    「不可变」语义：状态只能沿状态机向前迁移，终态不可原地复活；
    版本内容不原地覆盖。因 transition_to 需原地更新 status/entity_version，
    本类不声明 ``@dataclass(frozen=True)``。
    """

    entity_id: str
    created_by: str
    tenant_id: UUID
    schema_version: int = SCHEMA_VERSION
    entity_version: int = 1
    owner_module: str = OWNER_MODULE
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    audit_ref: str | None = None
    # 抽象占位：各子类（Project/ScriptRevision/Episode）用具体枚举重新声明并
    # 覆盖默认值（dataclass 字段覆盖语义）。基类用 Any 标注仅为提供统一的
    # ``self.status.value`` 访问基础，避免 getattr 魔法；实际实例恒为子类，
    # 此 None 默认值不会被使用（Entity 不直接实例化）。
    status: Any = None

    # 子类覆盖：聚合根类型标识（"Project" / "ScriptRevision" / "Episode"）。
    # 用 ClassVar 标记，确保不被 dataclass 当作实例字段，避免基类/子类
    # 字段默认值继承冲突（ClassVar 不参与 __init__ 与字段解析）。
    ENTITY_TYPE: ClassVar[str] = ""

    @property
    def entity_type(self) -> str:
        """聚合根类型标识（对齐 C.3.1 信封的 entity_type 字段）。

        作为 property 返回 ENTITY_TYPE 类属性，既保留原 ``instance.entity_type``
        的访问语义（测试与序列化均兼容），又不作为 dataclass 实例字段参与
        默认值继承（避免基类/子类字段默认值冲突）。
        """
        return self.ENTITY_TYPE

    def _validate_envelope(self) -> None:
        """校验信封公共必填字段非空、不超长（对齐 ORM 列定义）。"""
        for name, max_len in _ENVELOPE_MAX_LEN.items():
            value = getattr(self, name)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"{name} 不能为空")
            if len(value) > max_len:
                raise ValueError(f"{name} 长度超过上限 {max_len}")

    def _advance_version(self, audit_ref: str) -> None:
        """状态迁移后的统一副作用：版本 +1、更新时间戳、登记审计引用。

        收敛三个聚合根 transition_to 中重复的「版本自增 + 更新时间 + 写
        audit_ref」三行，保证迁移副作用口径一致（R4 消除重复机制）。
        """
        self.entity_version += 1
        self.updated_at = utcnow()
        self.audit_ref = audit_ref

    def _envelope_dict(self) -> dict[str, Any]:
        """序列化信封公共字段（业务字段由子类补充）。

        tenant_id 是平台级多租户隔离键，随信封一并序列化，供 RLS 上下文
        与审计事件引用（对齐决策 5：所有核心表带 tenant_id）。
        """
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "tenant_id": str(self.tenant_id),
            "schema_version": self.schema_version,
            "entity_version": self.entity_version,
            "status": self.status.value,
            "owner_module": self.owner_module,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "updated_at": self.updated_at.isoformat(),
            "audit_ref": self.audit_ref,
        }
