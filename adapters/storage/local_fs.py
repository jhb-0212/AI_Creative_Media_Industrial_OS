"""本地受控根目录的对象存储实现（在拿到 S3 之前的唯一合法落地方式）。

它存在的意义不是「本地开发方便」，而是把 P1-07 的读取边界真正做实：
只要有一个实现遵守这套边界，上层就不必各自实现「路径检查」——那种分散的
检查必然有一处在某个分支上漏掉。

四条边界及其实现方式
--------------------
| 威胁 | 靠什么拦住 | 为什么不是「校验」 |
|---|---|---|
| 路径穿越（``../../etc/passwd``） | 定位符正则 | 定位符里没有路径片段，穿越无从表达 |
| 符号链接指向根外 | 写时禁止 + 读前 ``lstat`` 拒绝 | 内容寻址目录下的文件由本进程创建，出现符号链接本身就是异常 |
| 超大内容占满磁盘 | ``max_bytes`` 前置判定 | 先看长度再落盘，不做「写完再删」 |
| 内容与声明不符 / 被篡改 | 落盘与读取都复核 sha256 | 哈希即文件名，校验是常量成本 |

不做的两件事，以及原因
----------------------
- **不做目录遍历/列举**：列举接口会被用来「找一份能用的产物」，而正确的
  产物由资产域台账指定。提供列举等于提供一个绕过台账的入口。
- **不做删除**：见 ``ports.py`` 的说明。

并发写同一份内容：因为文件名就是内容哈希，两个进程同时写同一 sha256 结果
必然相同，``os.replace`` 的原子性保证任何时刻读者都看不到半个文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from adapters.storage.ports import (
    StorageIntegrityError,
    StorageLocatorError,
    StorageObjectMissingError,
    StorageSizeLimitError,
    StorageUnavailableError,
    StoredObject,
)
from packages.shared.storage_locator import (
    EXT_BY_MEDIA_TYPE,
    LOCATOR_SHAPE,
    build_locator,
    extension_for_media_type,
    parse_locator,
)

# 定位符语法**只有一份实现**：``packages/shared/storage_locator.py``。
#
# 本模块此前自带一份 ``_LOCATOR_RE`` 与一份扩展名白名单，而资产域登记时又
# 各自判断「非空就行」——三处对「什么是一个合法定位符」的回答并不相同。
# 现在解析、构造、契约 pattern 全部由那一份定义派生；这里只负责把它的
# 「失败原因」翻译成本适配器的异常类型。
__all__ = ["EXT_BY_MEDIA_TYPE", "LocalFsObjectStorage"]


class LocalFsObjectStorage:
    """把内容存进受控根目录，对外只给内容寻址的定位符。"""

    def __init__(self, root: str | Path, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes 必须是正数：无上限的存储在故障时会占满磁盘")
        # 根目录在构造时就 resolve：此后所有比较都基于同一个绝对路径，
        # 不会因为工作目录变化或相对路径含 ``..`` 而漂移。
        self._root = Path(root).expanduser().resolve()
        self._max_bytes = max_bytes

    @property
    def root(self) -> Path:
        return self._root

    async def put(self, *, data: bytes, media_type: str, expected_sha256: str = "") -> StoredObject:
        ext = self._ext_for(media_type)
        if len(data) > self._max_bytes:
            raise StorageSizeLimitError(
                f"内容 {len(data)} 字节超出上限 {self._max_bytes}（media_type={media_type}）"
            )
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 and expected_sha256.lower() != digest:
            raise StorageIntegrityError(
                f"上游声明的 sha256={expected_sha256} 与实际内容 {digest} 不符："
                "拒绝落盘（否则资产台账会把矛盾固化下来）"
            )

        locator = build_locator(digest, ext)
        target = self._path_for(locator)
        if not target.exists():
            # 落盘走线程：文件 IO 是阻塞调用，直接 await 外层会把事件循环卡住，
            # 而存储 IO 恰恰是最容易变慢的一环（磁盘抖动 / 网络盘）。
            await asyncio.to_thread(self._write_atomically, target, data)

        return StoredObject(
            locator=locator,
            sha256=digest,
            size_bytes=len(data),
            media_type=media_type,
        )

    async def get(self, locator: str) -> bytes:
        target = self._path_for(locator)
        data = await asyncio.to_thread(self._read_checked, target, locator)
        return data

    async def exists(self, locator: str) -> bool:
        if parse_locator(locator)[0] is None:
            return False
        try:
            target = self._path_for(locator)
        except StorageLocatorError:
            return False
        return await asyncio.to_thread(target.is_file)

    # --- 内部 -------------------------------------------------------------

    @staticmethod
    def _ext_for(media_type: str) -> str:
        ext = extension_for_media_type(media_type)
        if ext is None:
            raise StorageLocatorError(
                f"未登记的 media_type={media_type}；已登记：{', '.join(sorted(EXT_BY_MEDIA_TYPE))}"
            )
        return ext

    def _path_for(self, locator: str) -> Path:
        """把定位符解析成绝对路径。

        **不调用 ``resolve()``**：``resolve`` 会跟随最后一层符号链接，于是
        「根目录内的符号链接指向根外」会被判成「定位符越界」——两个不同的问题
        被压成同一个错误，排障时无法区分，而且真正的威胁（根目录被外部改动）
        被伪装成了调用方拼错。符号链接由 ``_read_checked`` 的 ``lstat`` 识别。

        也没有额外的 ``..`` 过滤或根前缀比对：``local://<2 hex>/<2 hex>/
        <64 hex>.<白名单扩展名>`` 这个形态里**没有任何位置**可以放下路径片段，
        穿越因此不可表达。多留一条永远不触发的判定只会让人误以为它是防线，
        而真正的防线是那条正则——它有专门的用例锁定（含大写、短哈希、非产物
        扩展名三种伪造形态）。

        解析失败一律 ``StorageLocatorError``（含「前两段与哈希不符」）：
        这类定位符**语法合法但永远读不到内容**，属于拼写错误，与「数据丢了」
        是两件事——把后者归给前者会让人去查存储，而真正该查的是调用方。
        """
        parsed, problem = parse_locator(locator)
        if parsed is None:
            raise StorageLocatorError(f"{problem}（应为 {LOCATOR_SHAPE}）")
        return (
            self._root
            / parsed.sha256[0:2]
            / parsed.sha256[2:4]
            / (f"{parsed.sha256}.{parsed.extension}")
        )

    def _write_atomically(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件建在同一目录：跨目录的 rename 不保证原子（可能退化成复制）。
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                # fsync 之后再 replace：否则断电可能留下「已改名但内容为空」的文件，
                # 而文件名（哈希）会让它看起来是完整内容——最坏的一种损坏。
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _read_checked(self, target: Path, locator: str) -> bytes:
        try:
            info = target.lstat()
        except FileNotFoundError as exc:
            # 「确定不存在」与「存在但损坏」是两种事实，恢复流程据此做不同决定
            # （前者可放弃并重生成，后者放弃并记不可复用）。用专用子类表达。
            raise StorageObjectMissingError(f"定位符对应的内容不存在：{locator}") from exc
        except OSError as exc:
            # 权限/IO 错误：无法判定对象是否存在，绝不能当成「不存在」。
            raise StorageUnavailableError(f"读取 {locator} 失败（存储后端不可用）：{exc}") from exc
        if not os.path.isfile(target) or os.path.islink(target):
            # 内容寻址目录下的文件只由本进程创建；符号链接意味着根目录被外部改动，
            # 此时「读到什么」不由我们决定，必须拒绝而不是顺着链接读出去。
            raise StorageIntegrityError(f"定位符指向非普通文件（可能是符号链接）：{locator}")
        if info.st_size > self._max_bytes:
            raise StorageSizeLimitError(
                f"已存内容 {info.st_size} 字节超出上限 {self._max_bytes}：{locator}"
            )
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise StorageUnavailableError(f"读取 {locator} 失败（存储后端不可用）：{exc}") from exc
        digest = hashlib.sha256(data).hexdigest()
        parsed, problem = parse_locator(locator)
        assert parsed is not None, problem  # 调用前已校验
        if digest != parsed.sha256:
            raise StorageIntegrityError(f"内容哈希与定位符不符（存储被篡改或损坏）：{locator}")
        return data
