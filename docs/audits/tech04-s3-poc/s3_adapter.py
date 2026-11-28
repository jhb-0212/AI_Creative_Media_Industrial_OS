"""TECH-04 候选 S3 Adapter（隔离 PoC，非生产代码，不进 adapters/）。

实现 ObjectStoragePort 的 put/get/exists 三方法，**无 delete**（铁律）。
locator 直接复用 packages.shared.storage_locator 的唯一语法定义，
返回 local://<...> 定位符——与 LocalFS 完全同构，调用方无需改动。

对象键布局：沿用 locator 的 sha256 分段 <a>/<b>/<digest>.<ext>，
使 S3 侧路径与 LocalFS 侧一一对应；bucket 由构造参数给定（PoC 用专用桶）。

条件写入防覆盖：put 使用 S3 的 ``IfNoneMatch='*'``（S3 Conditional Writes，
AWS 2024-11 起支持；实测 SeaweedFS 与 RustFS 均返回 412 PreconditionFailed）。
已存在同键时抛 StorageIntegrityError 而不是静默覆盖——对应资产不可覆盖红线。

失败语义全部翻译成 ports.py 声明的异常族，不泄漏 botocore 异常。
"""

from __future__ import annotations

import hashlib
import os

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from adapters.storage.ports import (
    StorageError,
    StorageIntegrityError,
    StorageLocatorError,
    StorageSizeLimitError,
    StoredObject,
)
from packages.shared.storage_locator import (
    EXT_BY_MEDIA_TYPE,
    LOCATOR_SHAPE,
    build_locator,
    extension_for_media_type,
    parse_locator,
)


class S3StorageError(StorageError):
    """S3 后端不可用/权限拒绝等基础设施错误。"""


class S3ObjectStorage:
    """候选 S3 对象存储适配器：与 LocalFsObjectStorage 同构的端口实现。"""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        max_bytes: int,
        connect_timeout: float = 2.0,
        read_timeout: float = 5.0,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes 必须是正数")
        self._bucket = bucket
        self._max_bytes = max_bytes
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                retries={"max_attempts": 0},
            ),
        )

    @staticmethod
    def _ext_for(media_type: str) -> str:
        ext = extension_for_media_type(media_type)
        if ext is None:
            raise StorageLocatorError(
                f"未登记的 media_type={media_type}；已登记：{', '.join(sorted(EXT_BY_MEDIA_TYPE))}"
            )
        return ext

    @staticmethod
    def _key_for(locator: str) -> str:
        parsed, problem = parse_locator(locator)
        if parsed is None:
            raise StorageLocatorError(f"{problem}（应为 {LOCATOR_SHAPE}）")
        return f"{parsed.sha256[0:2]}/{parsed.sha256[2:4]}/{parsed.sha256}.{parsed.extension}"

    async def put(self, *, data: bytes, media_type: str, expected_sha256: str = "") -> StoredObject:
        ext = self._ext_for(media_type)
        if len(data) > self._max_bytes:
            raise StorageSizeLimitError(
                f"内容 {len(data)} 字节超出上限 {self._max_bytes}（media_type={media_type}）"
            )
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 and expected_sha256.lower() != digest:
            raise StorageIntegrityError(
                f"上游声明的 sha256={expected_sha256} 与实际内容 {digest} 不符：拒绝落盘"
            )
        locator = build_locator(digest, ext)
        key = self._key_for(locator)
        # SELFTEST_MUTANT：自检专用，去掉条件写入头以模拟「防覆盖被移除」的坏状态。
        # 仅用于证伪取证装置能在真坏时报红；正常取证不得设置。
        put_kwargs = {"Bucket": self._bucket, "Key": key, "Body": data, "ContentType": media_type}
        if os.environ.get("TECH04_SELFTEST_MUTANT") != "1":
            put_kwargs["IfNoneMatch"] = "*"
        try:
            self._client.put_object(**put_kwargs)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            http = exc.response["ResponseMetadata"]["HTTPStatusCode"]
            if http == 412 or code == "PreconditionFailed":
                raise StorageIntegrityError(
                    f"对象已存在，拒绝覆盖（资产不可覆盖）：{locator}"
                ) from exc
            raise S3StorageError(f"put_object 失败 code={code} http={http}") from exc
        except BotoCoreError as exc:
            # 网络中断/超时/连接失败是 BotoCoreError 子类，不是 ClientError。
            # 必须翻译成端口异常，否则 botocore 异常泄漏给调用方——正是 ports.py
            # 顶部声明要避免的事（换后端会波及所有调用方）。
            raise S3StorageError(f"put_object 网络/基础设施失败 {type(exc).__name__}") from exc
        return StoredObject(
            locator=locator,
            sha256=digest,
            size_bytes=len(data),
            media_type=media_type,
        )

    async def get(self, locator: str) -> bytes:
        key = self._key_for(locator)
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            data = resp["Body"].read()
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("NoSuchKey", "404", "NotFound"):
                raise StorageIntegrityError(f"定位符对应的对象不存在：{locator}") from exc
            raise S3StorageError(f"get_object 失败 code={code}") from exc
        except BotoCoreError as exc:
            raise S3StorageError(f"get_object 网络/基础设施失败 {type(exc).__name__}") from exc
        parsed, problem = parse_locator(locator)
        assert parsed is not None, problem
        if hashlib.sha256(data).hexdigest() != parsed.sha256:
            raise StorageIntegrityError(f"内容哈希与定位符不符（存储被篡改/损坏）：{locator}")
        if len(data) > self._max_bytes:
            raise StorageSizeLimitError(f"已存内容 {len(data)} 字节超出上限：{locator}")
        return data

    async def exists(self, locator: str) -> bool:
        if parse_locator(locator)[0] is None:
            return False
        try:
            key = self._key_for(locator)
        except StorageLocatorError:
            return False
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            http = exc.response["ResponseMetadata"]["HTTPStatusCode"]
            if http == 404:
                return False
            raise S3StorageError(f"head_object 失败 http={http}") from exc
        except BotoCoreError as exc:
            raise S3StorageError(f"head_object 网络/基础设施失败 {type(exc).__name__}") from exc

    @property
    def client(self):
        """仅取证脚本用于注入故障/检查 multipart，生产 Port 不暴露此属性。"""
        return self._client
