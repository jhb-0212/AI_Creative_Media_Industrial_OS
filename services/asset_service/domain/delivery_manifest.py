"""DeliveryManifest 对象（交付清单：只引用已成立的证据，不声明未发生的事实）。

契约对齐：
- `contracts/schemas/object_schemas.schema.json` → DeliveryManifest：
  `asset_refs` / `checksum` / `manifest_id` / `picture_lock_ref` / `qc_final_ref` /
  `rebuild_recipe` / `rights_evidence_refs` / `spec_ref` + 实体信封。
- `contracts/action_registry.yaml` → `DELIVERY.MANIFEST.CREATE`（MEDIUM，
  「交付服务/授权制片」，「仅生成 DRAFT 并绑定完整证据引用」），负向用例
  `ACT-DELIVERY-NEG-002`「缺引用/哈希 Manifest 拒绝」。
- `state_machines.yaml` → DeliveryManifest：`DRAFT → VALIDATING → VALIDATED →
  RELEASED`，异常/失效 `INVALID / STALE / REVOKED / SUPERSEDED`；「每个 Manifest
  版本不可变」。

三条结构性决定（对应审查与验收清单原文）：

1. **CREATE 只可能产生 DRAFT。** `DELIVERY.MANIFEST.CREATE` 的契约约束就是"仅生成
   DRAFT"；这条约束属于**创建用例**，不是对象构造——`transition_to()` 让同一对象
   能沿登记边推进到 VALIDATING/VALIDATED/RELEASED（D4），但创建入口仍固定 DRAFT。
   进入 VALIDATED 必须核验审批引擎的 APPROVAL_TICKET_COMPLETED 权威事件，
   这是 D4 的 `DeliveryApprovalService` 的职责，客户端无法声明结论。
2. **清单不声明 Gate PASS，结构上做不到。** 契约里这个对象没有 `gate_pass` 之类
   的布尔字段，只有 `qc_final_ref` / `picture_lock_ref` / `rights_evidence_refs`
   这类**证据引用**。引用只能指向已存在的证据，不能表达"Gate4 已通过"这种结论——
   C6 的「不声明未发生的 Gate PASS」在这里是**字段集缺项**保证的，不是靠代码自觉。
3. **校验和由服务端计算且只覆盖可重建内容。** `checksum` 不包含 manifest_id 与
   时间戳：同一组引用与同一份 rebuild_recipe 必须算出同一个哈希，否则
   「用清单重建一次交付，结果与原件哈希一致」（G1）无法成立。

为什么重建配方（`rebuild_recipe`）是必填而非可选：没有它，`asset_refs` 只说明
"用了哪些资产"，不说明"按什么顺序、以什么规格拼起来"。哈希一致但产物不同，
就是最难发现的一种交付缺陷。

`rebuild_recipe` 的**类型是字符串**而不是结构化对象：这一点以契约为准
（`object_schemas.schema.json#DeliveryManifest.properties.rebuild_recipe`
声明为 `string`）。代价要说清：平台无法对配方内部做规范化，因此"同一份交付
算出同一哈希"要求**配方字符串本身是确定性的**——这是产出方的责任。平台能做
且已做的是：拒绝空配方、把配方原文纳入哈希（改一个字就换一个哈希）。
若将来需要结构化配方，那是契约修订，不应在此处单方面放开类型。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import utcnow
from services.asset_service.domain.enums import DeliveryManifestStatus
from services.asset_service.domain.state_machine import assert_transition_manifest


def compute_manifest_checksum(
    *,
    asset_refs: list[str],
    spec_ref: str,
    qc_final_ref: str,
    picture_lock_ref: str,
    rights_evidence_refs: list[str],
    rebuild_recipe: str,
) -> str:
    """按可重建内容计算清单校验和（纯函数，供创建与重建共用）。

    规范化规则刻意写死并集中在此处：
    - 引用列表**排序后**参与计算：清单顺序是展示语义，不是内容语义；
      让顺序影响哈希会让"同一交付"产生两个校验和。
    - JSON 用 `sort_keys` + 紧凑分隔符，避免字典插入顺序与空白造成哈希漂移。
    - `rebuild_recipe` 是**不透明字符串**（契约类型就是 string），原样参与哈希：
      平台不对它的内部做任何改写，改一个字就换一个哈希。
    - 只覆盖内容字段，不含 manifest_id / 时间戳 / 决策人：重建时这些必然不同，
      把它们纳入哈希等于让 G1（重建哈希一致）永远无法通过。
    """
    payload = {
        "asset_refs": sorted(asset_refs),
        "spec_ref": spec_ref,
        "qc_final_ref": qc_final_ref,
        "picture_lock_ref": picture_lock_ref,
        "rights_evidence_refs": sorted(rights_evidence_refs),
        "rebuild_recipe": rebuild_recipe,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class DeliveryManifest:
    """一份交付清单（创建即不可变：修正必须新建 manifest_id）。"""

    manifest_id: str
    spec_ref: str
    qc_final_ref: str
    picture_lock_ref: str
    asset_refs: list[str]
    rights_evidence_refs: list[str]
    rebuild_recipe: str
    created_by: str
    tenant_id: UUID
    organization_id: UUID | None = None
    # 项目归属只用于行级隔离策略（契约对象没有 project_id 字段，故不进 to_dict）。
    project_id: str = ""
    status: DeliveryManifestStatus = DeliveryManifestStatus.DRAFT
    schema_version: int = 205
    entity_version: int = 1
    owner_module: str = "asset_service"
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        """校验证据引用完整性（缺引用的清单不得存在）。"""
        if self.organization_id is None:
            self.organization_id = self.tenant_id
        for name in (
            "manifest_id",
            "spec_ref",
            "qc_final_ref",
            "picture_lock_ref",
            "created_by",
            "project_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )

        if not self.asset_refs:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="asset_refs 不能为空（交付清单必须引用具体资产版本）",
            )
        if not self.rights_evidence_refs:
            # 权利证据缺失的清单一旦被当作"可交付"，后果是版权风险直接出到客户侧。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="rights_evidence_refs 不能为空（缺权利证据的清单不得生成）",
            )
        if not isinstance(self.rebuild_recipe, str) or not self.rebuild_recipe.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="rebuild_recipe 不能为空（无重建配方的清单无法复现）",
            )
        # 状态取值本身由枚举保证。这里刻意不限制"构造时必须是 DRAFT"：
        # 从数据库读回的清单可能是 VALIDATED/RELEASED，构造期拒绝会让只读
        # 路径崩溃。"CREATE 只产出 DRAFT"属于创建用例的约束（见模块 docstring），
        # 而"能进入哪些状态"由 transition_to() 的状态机校验统一把守。

    def transition_to(
        self, target: DeliveryManifestStatus, *, audit_ref: str
    ) -> DeliveryManifest:
        """沿登记边推进状态，返回推进后的新对象（原对象不可变）。

        `audit_ref` 是推进该状态的权威依据引用——必须是 outbox 里的事件 id，
        使"这个状态是怎么来的"永久可追（ADR-R11-G）。内容字段在这条路径上
        没有任何入口可改，数据库列级 UPDATE 再加一道兜底。
        """
        assert_transition_manifest(self.status, target)
        if not audit_ref.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="状态推进必须提供权威依据 audit_ref（审批事件 id）",
            )
        return replace(
            self,
            status=target,
            audit_ref=audit_ref.strip(),
            entity_version=self.entity_version + 1,
            updated_at=utcnow(),
        )

    @property
    def checksum(self) -> str:
        """内容校验和（由可重建内容推导，不接受客户端提交）。"""
        return compute_manifest_checksum(
            asset_refs=self.asset_refs,
            spec_ref=self.spec_ref,
            qc_final_ref=self.qc_final_ref,
            picture_lock_ref=self.picture_lock_ref,
            rights_evidence_refs=self.rights_evidence_refs,
            rebuild_recipe=self.rebuild_recipe,
        )

    def rebuild_projection(self) -> dict[str, Any]:
        """重建视图：只用清单内容推导"这次交付由什么构成"，用于 G1 比对。

        与 `to_dict()` 的区别：本方法不含 manifest_id 与时间戳，因此重建结果
        与原件的 `checksum` 可直接比较；`to_dict()` 是面向契约的响应形态。
        """
        return {
            "asset_refs": sorted(self.asset_refs),
            "spec_ref": self.spec_ref,
            "qc_final_ref": self.qc_final_ref,
            "picture_lock_ref": self.picture_lock_ref,
            "rights_evidence_refs": sorted(self.rights_evidence_refs),
            "rebuild_recipe": self.rebuild_recipe,
            "checksum": self.checksum,
        }

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约结构（字段集 == DeliveryManifest 契约声明）。"""
        return {
            "manifest_id": self.manifest_id,
            "asset_refs": self.asset_refs,
            "checksum": self.checksum,
            "picture_lock_ref": self.picture_lock_ref,
            "qc_final_ref": self.qc_final_ref,
            "rebuild_recipe": self.rebuild_recipe,
            "rights_evidence_refs": self.rights_evidence_refs,
            "spec_ref": self.spec_ref,
            # 信封（契约 required 包含这些字段；tenant_id / project_id 不在契约内）
            "entity_id": self.manifest_id,
            "entity_type": "DeliveryManifest",
            "schema_version": self.schema_version,
            "entity_version": self.entity_version,
            "status": self.status.value,
            "owner_module": self.owner_module,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "updated_at": self.updated_at.isoformat(),
            "audit_ref": self.audit_ref,
        }
