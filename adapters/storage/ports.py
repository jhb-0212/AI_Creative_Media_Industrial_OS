"""存储端口：**受控定位符 + 内容寻址**，不暴露文件系统路径（P1-07）。

问题
----
整改前 ``asset_versions.storage_ref`` 是一个自由 ``dict``，``adapters/storage``
是空目录——也就是说「产物存在哪」这件事**没有任何受控实现**。任何调用方都能
声明 ``{"path": "../../etc/passwd"}`` 或 ``{"path": "C:/Windows/System32"}``，
而消费方（QC 检测、交付打包）会照着这个字符串去读。这不是理论风险：请求体
里的 ``storage_ref`` 由客户端直接提交，服务端只校验「非空」。

设计：定位符不可寻址任意路径
----------------------------
定位符的形态是内容寻址的：

    local://ab/cd/abcd...（sha256）/png

三点后果，都是结构性的而不是靠校验：

1. **路径穿越不可能存在**：定位符里没有任何用户可控的路径片段。``../../x``
   在语法上就解析不出来（正则不匹配即拒绝），不需要「过滤 ``..``」这种
   永远漏一版的补丁。
2. **资产不可覆盖**（红线）：同一个定位符必然对应同一份内容——因为文件名
   就是内容的哈希。想覆盖就必须换内容，换内容就必然换定位符，于是「覆盖」
   在结构上退化成了「新增一份」。不需要额外的「禁止覆盖」守卫去追每种写入路径。
3. **写入天然幂等**：重复落同一份内容是无副作用的 no-op。重试、并发、重放
   都不需要额外协调。

失败语义由端口自己声明
----------------------
沿用 ``packages/shared/port_errors.py`` 的哲学：端口声明失败类型，不把
底层库的异常（``OSError`` / ``botocore`` 异常）泄漏给调用方。否则「换了存储
后端」这件事会波及所有调用方，端口也就名存实亡。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class StorageError(Exception):
    """存储端口失败的基类。调用方只应捕获这一族异常。"""


class StorageLocatorError(StorageError):
    """定位符非法。

    涵盖「语法不合法」（不是受控形态）与「指向根目录之外」。两者对调用方是
    同一件事：**你给我的定位符我不认**——而不是「文件不存在」。区分它们是
    为了排障（前者是调用方拼错了，后者是数据丢了），不是为了给出不同处理。
    """


class StorageIntegrityError(StorageError):
    """完整性校验失败：内容哈希与定位符不符、指向符号链接、或内容被截断。

    一律 fail-closed。存储层看到「哈希对不上」时唯一安全的动作是拒绝——
    返回一份未经校验的内容，等于把「这份产物是不是它自称的那份」这个问题
    留给下游，而下游（QC / 交付）没有能力回答它。
    """


class StorageSizeLimitError(StorageIntegrityError):
    """内容超出配置的大小上限。

    单独一个类型是因为它的处置方式不同：哈希不符是「数据有问题」（重取或报障），
    超限是「这份内容不该走这条路」（换通道或调上限）。混成一个会让运维分不清
    该看数据还是该看配置。
    """


@dataclass(frozen=True)
class StoredObject:
    """一次成功落盘的结果。``locator`` 是此后唯一的引用方式。"""

    locator: str
    sha256: str
    size_bytes: int
    media_type: str


class ObjectStoragePort(Protocol):
    """对象存储端口。

    刻意只有三个方法：``put`` / ``get`` / ``exists``。没有 ``delete``——
    产物的生命周期由资产域的状态机决定（归档不是删除），存储层提供删除
    等于给了一条绕过状态机的旁路。需要清理时由运维作业直接操作存储，
    并留下与资产台账的对应记录。
    """

    async def put(
        self, *, data: bytes, media_type: str, expected_sha256: str = ""
    ) -> StoredObject:
        """落盘并返回受控定位符。

        ``expected_sha256`` 非空时用于核对**上游声明**的哈希（例如模型网关
        返回的 ``digest``）。不匹配即 ``StorageIntegrityError``：上游说的和
        实际拿到的不是同一份内容，此时落盘会把矛盾固化进资产台账。
        """
        ...

    async def get(self, locator: str) -> bytes:
        """按定位符读回内容，并在返回前复核哈希。"""
        ...

    async def exists(self, locator: str) -> bool:
        """定位符对应的内容是否存在。非法定位符返回 ``False`` 而非抛错。"""
        ...
