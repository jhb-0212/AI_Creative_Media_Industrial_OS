"""生成产出描述（worker 执行环与资产域衔接的唯一数据面）。

为什么摘要是适配器给的，不是 worker 算的：
- 只有**执行者**知道它到底产出了哪些字节。worker 若自己对引用字符串算摘要，
  等于让搬运工替生产者签字——摘要从此不再证明内容，只证明"有人算过一次"。
- 真实供应商适配（model_gateway 落地后）从响应里拿到内容或内容定位，摘要由
  它给出；确定性适配器给出的是它自己写下的确定性产出的摘要。两者同一协议，
  执行环不因适配器更换而改变（ADR-R32 §3 债务 2 的收口方式）。

为什么 ``artifact_ref`` 与 ``storage_ref`` 分开：
- ``artifact_ref`` 是生成侧对产出的自称（供应商返回的定位串/作业 id），
  用于追溯"这条资产是哪个产出物"；
- ``storage_ref`` 是资产域可定位内容的地址（AssetVersion 的必填项）。
  两者在 Phase 1 常常同值，但语义不同：前者属于执行域，后者属于资产域，
  合并成一个字段会让资产域被迫理解供应商的定位格式。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry

#: SHA-256 十六进制摘要（64 位；与 AssetVersion 校验口径一致，大小写不敏感）。
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

#: asset_type 长度上限（与 AssetVersion 同口径：契约列 String(32)）。
_ASSET_TYPE_MAX_LEN = 32


@dataclass(frozen=True)
class GenerationOutput:
    """一次成功生成的产出描述（不可变；执行环唯一传给资产域的数据）。

    字段刻意只保留"登记一个资产版本所必需"的四项：多一个字段就多一处跨域
    耦合面，而资产登记不需要知道生成过程的任何细节。
    """

    #: 生成侧产出引用（供应商作业 id / 结果 URI；追溯用）。
    artifact_ref: str
    #: 产出内容的 SHA-256（适配器自证；资产域据此做完整性校验与幂等判定）。
    sha256: str
    #: 资产域可定位内容地址（AssetVersion.storage_ref 直接落库）。
    storage_ref: dict[str, Any]
    #: 资产类型（AssetVersion.asset_type 取值面）。
    asset_type: str

    def __post_init__(self) -> None:
        """构造即校验：不合格的产出不得进入资产域（防止空壳资产入库）。"""
        if not (self.artifact_ref or "").strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="artifact_ref 不能为空（适配器必须自证产出引用）",
            )
        if not _SHA256_RE.fullmatch(self.sha256 or ""):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="产出 sha256 必须是 64 位十六进制摘要",
            )
        if not self.storage_ref:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="产出 storage_ref 不能为空（资产必须能被定位）",
            )
        asset_type = (self.asset_type or "").strip()
        if not asset_type:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="产出 asset_type 不能为空"
            )
        if len(asset_type) > _ASSET_TYPE_MAX_LEN:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"产出 asset_type 长度超过上限 {_ASSET_TYPE_MAX_LEN}",
            )


__all__ = ["GenerationOutput"]
