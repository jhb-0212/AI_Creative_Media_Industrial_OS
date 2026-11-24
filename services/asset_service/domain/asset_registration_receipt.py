"""资产登记回执（ADR-R42 T2/T3）。

回执是追加式事实：一个生成任务至多一条，落库后不可改写。它同时承担
事件去重与成功终态的权威来源——Worker 自报的资产 ID 不构成登记事实。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from services.asset_service.domain.enums import AssetRegistrationReceiptStatus


@dataclass
class AssetRegistrationReceipt(Entity):
    ENTITY_TYPE = "AssetRegistrationReceipt"
    owner_module = "asset-service"

    generation_task_id: str = ""
    project_id: str = ""
    asset_version_id: str = ""
    message: str = ""
    status: AssetRegistrationReceiptStatus = AssetRegistrationReceiptStatus.REJECTED
    # 规范化登记请求指纹（R42 T2 幂等冲突）。存量回执可为 NULL；新回执必带。
    registration_fingerprint: str = ""

    def __post_init__(self) -> None:
        super()._validate_envelope()
        for name, max_len in (
            ("generation_task_id", 64),
            ("project_id", 64),
            ("asset_version_id", 64),
            ("message", 512),
            ("registration_fingerprint", 64),
        ):
            value = getattr(self, name)
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )
        # 非空指纹必须是 64 位小写十六进制，与迁移里的 DB CHECK 同口径。
        # 空串表示「存量回执无指纹」（映射为 DB NULL），允许。
        if self.registration_fingerprint and not re.fullmatch(
            r"[0-9a-f]{64}", self.registration_fingerprint
        ):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="registration_fingerprint 必须是 64 位小写十六进制或空串",
            )
        if (
            self.status is AssetRegistrationReceiptStatus.COMPLETED
            and not self.asset_version_id.strip()
        ):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="COMPLETED 回执必须带 asset_version_id",
            )


__all__ = ["AssetRegistrationReceipt"]
