"""C.3.1 通用实体信封（跨模块单一事实源）。

为什么在 packages/shared 而不是某个领域包：
- 信封字段（entity_id / entity_type / schema_version / entity_version /
  status / owner_module / created_at / created_by / updated_at / audit_ref）
  是**所有领域对象共有的机制**，不是某个模块的业务概念。
- 之前它只存在于 services/project_planning/domain/entity.py，导致资产域等
  新模块要么反向依赖 project_planning（依赖方向错误），要么复制一份信封
  校验（同一机制多份复制，必然漂移）。上提到 shared 是根治，不是搬家。

module_owner 缺省值：
- 基类默认 ``core``；各领域聚合根把 ``owner_module`` 覆写为自身模块名
  （project-planning / asset-service ...），保证「数据所有权」可追溯到模块。

本模块不含 ORM / FastAPI 依赖，是纯数据 + 行为，保证可测试可演进。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry

# 契约 C.3.1 通用实体信封的 schema_version 固定值（V2.0.5）。
SCHEMA_VERSION = 205

DEFAULT_OWNER_MODULE = "core"


def utcnow() -> datetime:
    """统一 UTC 时间源（8.2 规则：所有时间统一存 UTC）。"""
    return datetime.now(UTC)


# 信封字段名 -> 最大长度（对齐 ORM 列定义），供统一非空/超长校验。
# 空字符串视为空。业务字段由各聚合根单独校验。
_ENVELOPE_MAX_LEN: dict[str, int] = {
    "entity_id": 64,
    "created_by": 64,
}


@dataclass
class Entity:
    """C.3.1 通用实体信封基类。

    字段（实例数据）：
    - entity_id / created_by / tenant_id 由子类构造时传入（必填）。
    - schema_version / entity_version / owner_module / created_at / updated_at /
      audit_ref 有统一默认值。

    ``entity_type`` 是聚合根固定常量，由子类声明为类属性 ``ENTITY_TYPE``。

    「不可变」语义：状态只能沿状态机向前迁移，终态不可原地复活；
    版本内容不原地覆盖。因 transition_to 需原地更新 status/entity_version，
    本类不声明 ``@dataclass(frozen=True)``。
    """

    entity_id: str
    created_by: str
    tenant_id: UUID
    schema_version: int = SCHEMA_VERSION
    entity_version: int = 1
    owner_module: str = DEFAULT_OWNER_MODULE
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    audit_ref: str | None = None
    # 抽象占位：各子类用具体枚举重新声明并覆盖默认值（dataclass 字段覆盖）。
    # 基类用 Any 标注仅为提供统一的 ``self.status.value`` 访问基础；实际实例
    # 恒为子类，此 None 默认值不会被使用（Entity 不直接实例化）。
    status: Any = None

    # 子类覆盖：聚合根类型标识（"Project" / "AssetVersion" ...）。
    # 用 ClassVar 标记，确保不被 dataclass 当作实例字段。
    ENTITY_TYPE: ClassVar[str] = ""

    @property
    def entity_type(self) -> str:
        """聚合根类型标识（对齐 C.3.1 信封的 entity_type 字段）。"""
        return self.ENTITY_TYPE

    def _validate_envelope(self) -> None:
        """校验信封公共必填字段非空、不超长（对齐 ORM 列定义）。"""
        for name, max_len in _ENVELOPE_MAX_LEN.items():
            value = getattr(self, name)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def _advance_version(self, audit_ref: str) -> None:
        """状态迁移后的统一副作用：版本 +1、更新时间戳、登记审计引用。"""
        self.entity_version += 1
        self.updated_at = utcnow()
        self.audit_ref = audit_ref

    def _envelope_dict(self) -> dict[str, Any]:
        """序列化信封公共字段（业务字段由子类补充）。"""
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
