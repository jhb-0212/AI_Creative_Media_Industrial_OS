"""本地受控存储的边界测试（P1-07）。

判据不是「正常路径能用」，而是**每条越界路径都被拒绝**。因此每个用例都
显式构造一种越界形态，而不是只测 happy path——只测 happy path 的守卫
在把防线删掉之后照样全绿。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from adapters.storage.local_fs import LocalFsObjectStorage
from adapters.storage.ports import (
    StorageIntegrityError,
    StorageLocatorError,
    StorageSizeLimitError,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"payload-" * 8


def _storage(tmp_path: Path, *, max_bytes: int = 1024 * 1024) -> LocalFsObjectStorage:
    return LocalFsObjectStorage(tmp_path / "objects", max_bytes=max_bytes)


async def test_put_returns_content_addressed_locator(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    stored = await storage.put(data=_PNG, media_type="image/png")

    digest = hashlib.sha256(_PNG).hexdigest()
    assert stored.sha256 == digest
    assert stored.locator == f"local://{digest[0:2]}/{digest[2:4]}/{digest}.png"
    assert stored.size_bytes == len(_PNG)
    assert await storage.get(stored.locator) == _PNG


async def test_same_content_twice_is_idempotent(tmp_path: Path) -> None:
    """内容寻址 ⇒ 重复落盘是 no-op，不需要额外协调（重试/并发天然安全）。"""
    storage = _storage(tmp_path)
    first = await storage.put(data=_PNG, media_type="image/png")
    second = await storage.put(data=_PNG, media_type="image/png")
    assert first == second


async def test_path_traversal_is_unrepresentable_not_merely_filtered(
    tmp_path: Path,
) -> None:
    """穿越在**语法层**不可表达：正则不认识 ``..``，因此不存在「过滤漏一版」。"""
    storage = _storage(tmp_path)
    for forged in (
        "local://../../etc/passwd",
        "local://ab/cd/../../../../etc/passwd.png",
        "local://ab/cd/" + "a" * 64 + ".png/../../../etc/passwd",
        "/etc/passwd",
        "file:///etc/passwd",
    ):
        with pytest.raises(StorageLocatorError):
            await storage.get(forged)
        # exists 对非法定位符返回 False 而不是抛错：它被用作「有没有」的判断，
        # 抛错会让调用方在判断前就得先 try，反过来诱发「吞掉异常当作不存在」。
        assert await storage.exists(forged) is False


async def test_locator_must_be_hex_not_arbitrary_text(tmp_path: Path) -> None:
    """大写十六进制与非十六进制字符都拒绝：定位符只有一种合法形态。"""
    storage = _storage(tmp_path)
    digest = hashlib.sha256(_PNG).hexdigest()
    for forged in (
        f"local://{digest[0:2]}/{digest[2:4]}/{digest.upper()}.png",
        f"local://{digest[0:2]}/{digest[2:4]}/{digest}.sh",
        f"local://{digest[0:2]}/{digest[2:4]}/{digest[:-1]}.png",
    ):
        with pytest.raises(StorageLocatorError):
            await storage.get(forged)


async def test_symlink_in_root_is_refused(tmp_path: Path) -> None:
    """根目录被外部改动（出现符号链接）⇒ 拒绝读，不顺着链接读出去。"""
    storage = _storage(tmp_path)
    stored = await storage.put(data=_PNG, media_type="image/png")
    real = storage.root / stored.locator.removeprefix("local://")
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG)
    real.unlink()
    os.symlink(outside, real)

    with pytest.raises(StorageIntegrityError, match="符号链接"):
        await storage.get(stored.locator)


async def test_tampered_content_is_detected_on_read(tmp_path: Path) -> None:
    """内容被改（哈希与文件名不符）⇒ 拒绝返回，不把未校验内容交给下游。"""
    storage = _storage(tmp_path)
    stored = await storage.put(data=_PNG, media_type="image/png")
    real = storage.root / stored.locator.removeprefix("local://")
    real.write_bytes(_PNG + b"tampered")

    with pytest.raises(StorageIntegrityError, match="哈希与定位符不符"):
        await storage.get(stored.locator)


async def test_oversize_is_rejected_before_writing(tmp_path: Path) -> None:
    storage = _storage(tmp_path, max_bytes=16)
    with pytest.raises(StorageSizeLimitError, match="超出上限"):
        await storage.put(data=_PNG, media_type="image/png")
    # 前置判定的证据：磁盘上什么都没留下（不是「写完再删」）。
    assert not (tmp_path / "objects").exists() or not any(
        (tmp_path / "objects").rglob("*.png")
    )


async def test_upstream_declared_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    """上游声明的 sha256 与实际内容不符 ⇒ 拒绝落盘。

    这条防的是「网关说了产物是 X、实际给的是 Y」：落盘会把矛盾固化进资产台账，
    而台账一旦记录就不会再被复核。
    """
    storage = _storage(tmp_path)
    with pytest.raises(StorageIntegrityError, match="不符"):
        await storage.put(
            data=_PNG, media_type="image/png", expected_sha256="0" * 64
        )


async def test_unknown_media_type_is_rejected(tmp_path: Path) -> None:
    """白名单：未登记的媒体类型不放行，避免任意扩展名落进内容目录。"""
    storage = _storage(tmp_path)
    with pytest.raises(StorageLocatorError, match="未登记的 media_type"):
        await storage.put(data=b"x", media_type="application/x-sh")


async def test_zero_max_bytes_is_refused_at_construction(tmp_path: Path) -> None:
    """无上限的存储是故障放大器：构造期就拒绝，不给「先跑起来再说」的机会。"""
    with pytest.raises(ValueError, match="max_bytes"):
        LocalFsObjectStorage(tmp_path / "objects", max_bytes=0)
