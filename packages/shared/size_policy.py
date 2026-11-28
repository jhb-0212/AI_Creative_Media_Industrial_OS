"""版本化尺寸策略（ADR-R47 Owner 裁决 ①，P2）。

为什么是代码常量而不是一张表
--------------------------
尺寸策略是「某个画幅比例落在哪个像素尺寸」的**策略投影**，不是业务事实：它没有
租户、没有生命周期、没有审批链，也没有人按 id 引用它的某一行。为它建表会凭空多
一个领域对象，随之而来的是 schema / taxonomy / CRUD / 冻结重签一整套成本，而收益
只是「可以 UPDATE 一行」——那恰好是裁决禁止的行为（被引用版本不得原地改）。

因此策略以代码内的版本化常量发布：

- ``policy_id`` 与 ``policy_version`` 一起写进 ``GenerationSpec`` 并随规格冻结；
- 内容变化**必须**同时升 ``policy_version``。``tests/unit/test_generation_spec``
  钉死了 (id, version, 表内容) 的摘要，改内容而不升版本会当场报红；
- 已冻结的规格永远按它自己记录的版本解释，不查「当前策略」。

已知比例 → 尺寸的映射只允许整数像素，且与画幅比例的相对误差 ≤1%（与
``GenerationSpec`` 的一致性校验同一判据）。
"""

from __future__ import annotations

import hashlib
import json

from packages.errors.error_codes import DomainError, ErrorRegistry

SIZE_POLICY_ID = "afs-default"
SIZE_POLICY_VERSION = "1"

#: 画幅比例 → (宽, 高)。键必须满足 ``GenerationSpec.aspect_ratio`` 的正则。
_SIZES: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "4:3": (1152, 864),
    "3:2": (1152, 768),
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "2:3": (768, 1152),
    "3:4": (864, 1152),
    "21:9": (1280, 549),
}

#: 与画幅比例的最大相对误差（Owner 裁决：1%）。
MAX_ASPECT_DRIFT = 0.01


def policy_digest() -> str:
    """策略内容的稳定摘要（测试用来钉住「改内容必须升版本」）。"""
    payload = json.dumps(
        {
            "policy_id": SIZE_POLICY_ID,
            "policy_version": SIZE_POLICY_VERSION,
            "sizes": _SIZES,
            "max_aspect_drift": MAX_ASPECT_DRIFT,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def resolve_size(aspect_ratio: str, *, policy_version: str) -> str:
    """按**指定版本**解析尺寸；未知版本与未知比例都拒绝，不落默认值。

    参数里的 ``policy_version`` 来自规格冻结值，而不是调用方的「当前版本」。
    本策略只有一个已发布版本；未来新增版本必须保留旧版本的解析分支，否则已冻结
    的规格会在升级后变成无法解释的历史。
    """
    version = (policy_version or "").strip()
    if version != SIZE_POLICY_VERSION:
        # 旧版本必须继续可解释：当前没有旧版本，所以未知版本一律 fail-closed。
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=(
                f"size-policy-version-missing: 未知尺寸策略版本 {version!r}"
                f"（已发布: {SIZE_POLICY_VERSION}）"
            ),
        )
    ratio = (aspect_ratio or "").strip()
    size = _SIZES.get(ratio)
    if size is None:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=f"spec-size-unresolved: 画幅比例 {ratio!r} 不在尺寸策略 {SIZE_POLICY_ID}@{version} 中",
        )
    return f"{size[0]}x{size[1]}"


def parse_ratio(ratio: str) -> tuple[int, int] | None:
    """``W:H`` → (W, H)；不合法返回 None（调用方决定拒绝文案）。"""
    parts = (ratio or "").split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    width, height = int(parts[0]), int(parts[1])
    if width < 1 or height < 1:
        return None
    return width, height


def aspect_matches(size: str, aspect_ratio: str) -> bool:
    """尺寸与画幅比例相对误差是否 ≤1%。"""
    size_parts = (size or "").split("x")
    if len(size_parts) != 2 or not all(p.isdigit() for p in size_parts):
        return False
    size_w, size_h = int(size_parts[0]), int(size_parts[1])
    ratio = parse_ratio(aspect_ratio)
    if ratio is None or size_w < 1 or size_h < 1:
        return False
    ar_w, ar_h = ratio
    return abs(size_w / size_h - ar_w / ar_h) <= MAX_ASPECT_DRIFT * (ar_w / ar_h)


__all__ = [
    "MAX_ASPECT_DRIFT",
    "SIZE_POLICY_ID",
    "SIZE_POLICY_VERSION",
    "aspect_matches",
    "parse_ratio",
    "policy_digest",
    "resolve_size",
]
