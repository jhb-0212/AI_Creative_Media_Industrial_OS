"""AssetVersion 聚合根（领域模型，纯 Python，无框架依赖）。

契约对齐：
- contracts/schemas/object_schemas.schema.json → AssetVersion：
  权威所有者为 Asset Service；新回流生成新版本，不覆盖原版本，不自动设为
  APPROVED/GOLD；信封字段 + asset_type/sha256/storage_ref/version/
  source_asset_version_ids/rights_manifest_ref/adapter_id/tool_product/operator_ref。
- contracts/action_registry.yaml → ASSET.VERSION.CREATE（MEDIUM，Worker/
  Model Adapter，「仅新增不可变版本，不覆盖旧版本或改变 GOLD」）。
- contracts/state_machines/state_machines.yaml → AssetVersion 生命周期。

不可变语义：
- 版本记录创建后，内容字段（sha256/storage_ref/asset_type ...）不原地改写；
  状态只能沿状态机向前迁移；终态不可复活。
- 「新增版本」是新建一条记录并通过 source_asset_version_ids 指向前序版本，
  不是更新旧记录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.domain.state_machine import assert_transition_asset

# SHA-256 十六进制摘要（64 位，大小写不敏感；入库统一小写）。
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# 本模块数据所有权标识（覆写信封 owner_module 默认值）。
ASSET_OWNER_MODULE = "asset-service"


@dataclass
class AssetVersion(Entity):
    """受控资产版本（Asset Service 的权威对象）。"""

    ENTITY_TYPE = "AssetVersion"

    # 数据所有权：信封 owner_module 覆写为本模块。
    owner_module: str = ASSET_OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    asset_type: str = ""
    sha256: str = ""
    storage_ref: dict[str, Any] = field(default_factory=dict)
    source_asset_version_ids: list[str] = field(default_factory=list)
    rights_manifest_ref: str | None = None
    adapter_id: str | None = None
    tool_product: str | None = None
    operator_ref: str | None = None
    version: str | None = None
    status: AssetStatus = AssetStatus.GENERATED

    def __post_init__(self) -> None:
        """校验信封 + 业务字段（对齐 ORM 列定义与契约必填语义）。"""
        super()._validate_envelope()

        if not self.asset_type.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="asset_type 不能为空"
            )
        if len(self.asset_type) > 32:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="asset_type 长度超过上限 32"
            )

        # 资产可用性最低要求：必须能校验完整性（sha256）、必须能定位内容
        # （storage_ref 非空）。缺任一项的「资产」是空壳，不得入库。
        if not _SHA256_RE.fullmatch(self.sha256 or ""):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="sha256 必须是 64 位十六进制摘要",
            )
        self.sha256 = self.sha256.lower()

        if not self.storage_ref:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="storage_ref 不能为空"
            )

        for source_id in self.source_asset_version_ids:
            if not isinstance(source_id, str) or not source_id.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="source_asset_version_ids 含空值",
                )

    def transition_to(self, target: AssetStatus, audit_ref: str) -> AssetVersion:
        """执行状态迁移，未登记转移拒绝（31.5）。"""
        assert_transition_asset(self.status, target)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构（asset_version_id 与 entity_id 同值）。"""
        data = self._envelope_dict()
        data.update(
            {
                # 契约对象同时暴露 entity_id 与 asset_version_id：本实现用一个
                # 主键承载两者，避免双主键口径分裂（写入/查询只有一条真相）。
                "asset_version_id": self.entity_id,
                "asset_type": self.asset_type,
                "sha256": self.sha256,
                "storage_ref": self.storage_ref,
                "source_asset_version_ids": list(self.source_asset_version_ids),
                "rights_manifest_ref": self.rights_manifest_ref,
                "adapter_id": self.adapter_id,
                "tool_product": self.tool_product,
                "operator_ref": self.operator_ref,
                "version": self.version,
            }
        )
        return data
