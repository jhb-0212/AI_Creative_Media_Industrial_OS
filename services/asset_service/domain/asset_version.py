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
from packages.shared.storage_locator import parse_locator
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.domain.state_machine import ASSET_LIFECYCLE

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

    # 归属键（唯一）：资产属于哪个项目。授权判定沿 project → organization 派生，
    # 组织归属不在本模型落库（冗余列=第二事实源）。
    project_id: str = ""

    # 业务字段（信封字段由 Entity 基类提供）。
    asset_type: str = ""
    sha256: str = ""
    # 受控内容寻址定位符（P1-07）。**不是**自由映射：整改前是
    # ``dict[str, Any]``，任何调用方都能声明 ``{"path": "../../etc/passwd"}``，
    # 且与上面的 ``sha256`` 毫无关系。形态收敛后「资产在哪」可由哈希推出，
    # 「指向的内容是否就是声称的那份」也变成可判定的（见 ``__post_init__``）。
    storage_ref: str = ""
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

        # 归属必填：无项目的资产无法被授权（SCOPE-3）。空串与 None 一律拒绝，
        # 避免「有归属字段但恒为空」这种看起来合规、实际不可判定的状态。
        if not (self.project_id or "").strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="project_id 不能为空：资产必须归属一个项目",
            )
        if len(self.project_id) > 64:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 长度超过上限 64"
            )
        self.project_id = self.project_id.strip()

        if not self.asset_type.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="asset_type 不能为空"
            )
        if len(self.asset_type) > 32:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="asset_type 长度超过上限 32"
            )

        # 资产可用性最低要求：必须能校验完整性（sha256）、必须能定位内容
        # （storage_ref 是受控定位符）。缺任一项的「资产」是空壳，不得入库。
        if not _SHA256_RE.fullmatch(self.sha256 or ""):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="sha256 必须是 64 位十六进制摘要",
            )
        self.sha256 = self.sha256.lower()

        # 定位符必须是受控形态。这一步不是「格式校验」，而是把三条结构性保证
        # 落到入口：形态固定为内容寻址后，路径穿越不可表达、覆盖只能退化成
        # 新增、写入天然幂等。放过一个自由值，这三条同时失效。
        parsed, problem = parse_locator(self.storage_ref or "")
        if parsed is None:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"storage_ref 非法：{problem}",
            )

        # 跨字段一致性：定位符里内嵌的哈希必须等于本行的 sha256。
        #
        # 这是 JSON Schema 的 pattern 表达不了的那一半（pattern 只看单个值），
        # 也恰恰是最容易被写成「两份事实源」的地方：同一条资产可以声称内容
        # 哈希是 A、定位符却指向 B，两列各自合法、合起来无意义。下游按哪个走
        # 都会错——按 sha256 校验会把好内容判成篡改，按定位符读会取回别的
        # 内容。所以两者必须由**同一个值**推出，任何不一致都在这里停下。
        if parsed.sha256 != self.sha256:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    "storage_ref 与 sha256 不一致：定位符指向 "
                    f"{parsed.sha256}，本行声明 {self.sha256}"
                    "（两列各自合法但合起来无意义，必须由同一个内容哈希推出）"
                ),
            )

        for source_id in self.source_asset_version_ids:
            if not isinstance(source_id, str) or not source_id.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="source_asset_version_ids 含空值",
                )

    def transition_to(
        self, target: AssetStatus, audit_ref: str, action: str
    ) -> AssetVersion:
        """执行状态迁移：结构合法性 + 边所有权（31.5 / N-07）。

        所有权在这里**再判一次**，与 ``asset_service`` 服务层的前置 ``edge_owned_by``
        不是重复：服务层那一步是为了拿到边对象取 ``guards``，这一步是聚合自身的
        不变量——任何持有本对象的人（测试、未来的新用例、脚本）都无法绕过。
        两道判定读的是同一张边表，因此不会出现「服务层放行、领域层拦截」以外的
        第三种结果；若发生不一致，说明有人绕过了唯一推进入口，本该失败。
        """
        ASSET_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构（asset_version_id 与 entity_id 同值）。

        project_id 与 tenant_id 同待遇：**不进响应**。它是内部归属键，随响应下发
        等于让调用方据此推断他人项目/组织拓扑；归属校验在服务端完成，
        客户端只需要 asset_version_id。
        """
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
