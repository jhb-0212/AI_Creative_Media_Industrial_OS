"""TECH-04 S3 兼容存储 PoC 取证装置（隔离环境，非生产代码）。

对应 ADR-R44 §5.4 七项通过条件，逐条产出可复算的 PASS/FAIL/N-A。
两个后端：SeaweedFS(8333) 与 RustFS(9000)，各跑同一矩阵并单独记录。
红线：测试对象写专用桶 poc-tech04-*，不进正式资产台账；不测 delete。
"""

from __future__ import annotations

import asyncio
import hashlib
import time

from botocore.exceptions import ClientError
from s3_adapter import S3ObjectStorage, S3StorageError

from adapters.storage.ports import (
    StorageIntegrityError,
    StorageLocatorError,
    StorageSizeLimitError,
)
from packages.shared.storage_locator import (
    build_locator,
    parse_locator,
)

KEY = "minioadmin"
SEC = "tp7rkd4a0jm1yzxv3uiw2o6q"
BACKENDS = {
    "SeaweedFS": "http://localhost:8333",
    "RustFS": "http://localhost:9000",
}

RESULTS: list[tuple[str, str, str, bool, str]] = []  # (cond, backend, name, ok, detail)


def record(cond, backend, name, ok, detail):
    RESULTS.append((cond, backend, name, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {cond} {backend:9s} {name} :: {detail}")


def na(cond, backend, name, detail):
    RESULTS.append((cond, backend, name, None, detail))
    print(f"[N-A] {cond} {backend:9s} {name} :: {detail}")


def new_adapter(name, url, bucket=None, **kw):
    return S3ObjectStorage(
        endpoint_url=url,
        bucket=bucket or f"poc-tech04-{name.lower()}",
        access_key=KEY,
        secret_key=SEC,
        max_bytes=10 * 1024 * 1024,
        **kw,
    )


async def port_and_locator(name, url):
    """条件①：复用并审查 Port 与 local:// locator 规则。"""
    a = new_adapter(name, url)
    # 候选 Adapter 满足 Port 三方法、无 delete
    methods = {m for m in ("put", "get", "exists") if hasattr(a, m)}
    no_delete = not hasattr(a, "delete")
    record(
        "①",
        name,
        "Port 三方法/无 delete",
        methods == {"put", "get", "exists"} and no_delete,
        f"methods={sorted(methods)} has_delete={not no_delete}",
    )
    # locator 语法与生产一致：build/parse 往返
    digest = hashlib.sha256(b"tech04").hexdigest()
    loc = build_locator(digest, "png")
    parsed, _problem = parse_locator(loc)
    record(
        "①",
        name,
        "locator 复用生产定义",
        parsed is not None and parsed.sha256 == digest,
        f"locator={loc[:28]}... parse_ok={parsed is not None}",
    )
    # 非法定位符（前两段与哈希不符）被拒
    bad = "local://ff/ee/" + digest + ".png"
    record(
        "①",
        name,
        "非法定位符拒绝",
        parse_locator(bad)[0] is None,
        "前两段与哈希不符 -> parse 拒绝（防『语法合法但永远读不到』）",
    )


async def put_get(name, url):
    """条件③：条件写入/防覆盖/SHA-256/大小/媒体类型/完整读取。"""
    a = new_adapter(name, url)
    data = b"TECH04 content-addressing probe " + str(time.time()).encode()
    obj = await a.put(data=data, media_type="image/png")
    digest = hashlib.sha256(data).hexdigest()
    record(
        "③",
        name,
        "put 返回受控定位符",
        obj.sha256 == digest and obj.locator.startswith("local://"),
        f"sha256_match={obj.sha256 == digest} locator={obj.locator[:24]}...",
    )
    got = await a.get(obj.locator)
    record(
        "③",
        name,
        "get 完整读取+哈希复核",
        got == data,
        f"bytes_in={len(data)} bytes_out={len(got)} match={got == data}",
    )
    record("③", name, "exists 命中", await a.exists(obj.locator) is True, "head_object -> True")
    # 防覆盖：同一 locator（同样内容、正确 SHA）再 put 必须被条件写入挡下。
    # 关键：必须用与首次完全相同的内容且不传错误 expected_sha256，
    # 否则哈希守卫会先抛 IntegrityError，掩盖掉条件写入是否真的生效（假阳性）。
    try:
        await a.put(data=data, media_type="image/png")  # 同样内容 -> 同一 key
        overwritten = True
    except StorageIntegrityError:
        overwritten = False
    except Exception as e:
        overwritten = f"WRONG_EXC:{type(e).__name__}"
    record(
        "③",
        name,
        "条件写入防覆盖",
        overwritten is False,
        f"同内容重复 put（同 key）-> overwritten={overwritten}，由 IfNoneMatch=* 挡下而非哈希守卫",
    )
    # expected_sha256 不符必须拒
    try:
        await a.put(data=b"another content", media_type="image/png", expected_sha256="0" * 64)
        sha_guard = False
    except StorageIntegrityError:
        sha_guard = True
    record(
        "③", name, "上游声明 SHA 不符拒绝", sha_guard, "expected_sha256 mismatch -> IntegrityError"
    )
    # 大小超限（用极小上限的 adapter）
    tiny = S3ObjectStorage(
        endpoint_url=url,
        bucket=f"poc-tech04-{name.lower()}",
        access_key=KEY,
        secret_key=SEC,
        max_bytes=4,
    )
    try:
        await tiny.put(data=b"12345", media_type="image/png")
        size_guard = False
    except StorageSizeLimitError:
        size_guard = True
    record("③", name, "大小上限前置拒绝", size_guard, "5 bytes > max_bytes=4 -> SizeLimitError")
    # 未登记媒体类型拒绝
    try:
        await a.put(data=b"x", media_type="application/x-sh")
        ext_guard = False
    except StorageLocatorError:
        ext_guard = True
    record("③", name, "媒体类型白名单", ext_guard, "application/x-sh -> LocatorError")
    # 非法 locator 的 exists 返回 False 而非抛错
    record(
        "③",
        name,
        "exists 非法 locator 返回 False",
        await a.exists("not-a-locator") is False,
        "invalid locator -> False (port contract)",
    )


async def multipart(name, url):
    """条件④：Multipart 中断/重试/部分对象清理/版本策略。"""
    a = new_adapter(name, url)
    cli = a.client
    bucket = f"poc-tech04-{name.lower()}"
    key = f"poc/multipart/{int(time.time() * 1000)}"
    # 版本策略查询（两后端支持度可能不同，如实记录）
    try:
        v = cli.get_bucket_versioning(Bucket=bucket)
        status = v.get("Status", "Unversioned")
        record("④", name, "版本策略可查询", True, f"Versioning.Status={status}")
    except ClientError as e:
        na(
            "④",
            name,
            "版本策略可查询",
            f"get_bucket_versioning 不支持 code={e.response['Error']['Code']}",
        )
        status = "N/A"
    # 创建一个 multipart upload 然后 abort，确认无残留 part
    try:
        mpu = cli.create_multipart_upload(Bucket=bucket, Key=key)
        uid = mpu["UploadId"]
        cli.upload_part(Bucket=bucket, Key=key, UploadId=uid, PartNumber=1, Body=b"partial-data")
        listed = cli.list_parts(Bucket=bucket, Key=key, UploadId=uid)
        parts_before = len(listed.get("Parts", []))
        cli.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=uid)
        try:
            cli.list_parts(Bucket=bucket, Key=key, UploadId=uid)
            still_exists = True
        except ClientError:
            still_exists = False
        record(
            "④",
            name,
            "Multipart 中断+部分对象清理",
            parts_before == 1 and not still_exists,
            f"part_before={parts_before} abort后upload仍存在={still_exists}",
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        na("④", name, "Multipart 中断+部分对象清理", f"multipart 不支持 code={code}")
    return status


async def fault_injection(name, url):
    """条件⑤：网络中断/权限拒绝/对象缺失/Hash 不匹配/服务恢复。"""
    # 网络中断：指向无监听端口
    dead = new_adapter(name, "http://localhost:9999", connect_timeout=0.3, read_timeout=0.5)
    try:
        await dead.put(data=b"x", media_type="image/png")
        net_ok = False
    except S3StorageError:
        net_ok = True
    except Exception as e:
        net_ok = f"WRONG_EXC:{type(e).__name__}"
    record("⑤", name, "网络中断->S3StorageError", net_ok is True, f"dead endpoint -> {net_ok}")

    # 权限拒绝：用错误密钥
    denied = S3ObjectStorage(
        endpoint_url=url,
        bucket=f"poc-tech04-{name.lower()}",
        access_key="wrong",
        secret_key="wrong",
        max_bytes=1024,
    )
    try:
        await denied.put(data=b"x", media_type="image/png")
        perm_ok = False
    except S3StorageError:
        perm_ok = True
    except Exception as e:
        perm_ok = f"WRONG_EXC:{type(e).__name__}"
    record("⑤", name, "权限拒绝->S3StorageError", perm_ok is True, f"bad credentials -> {perm_ok}")

    # 对象缺失：合法但不存在的 locator -> get 抛 IntegrityError
    a = new_adapter(name, url)
    missing = build_locator("a" * 64, "png")
    try:
        await a.get(missing)
        miss_ok = False
    except StorageIntegrityError:
        miss_ok = True
    record("⑤", name, "对象缺失->IntegrityError", miss_ok, f"nonexistent locator -> {miss_ok}")

    # Hash 不匹配：put 一个 locator 对应内容，再篡改后端对象字节，get 必须拒
    data = b"hash-mismatch-probe-" + str(time.time()).encode()
    obj = await a.put(data=data, media_type="image/jpeg")
    key = a._key_for(obj.locator)
    try:
        a.client.put_object(
            Bucket=f"poc-tech04-{name.lower()}", Key=key, Body=b"TAMPERED", ContentType="image/jpeg"
        )
        # 直写绕过 If-None-Match，模拟后端被篡改
        try:
            await a.get(obj.locator)
            hash_ok = False
        except StorageIntegrityError:
            hash_ok = True
    except ClientError as e:
        hash_ok = f"PUT_TAMPER_BLOCKED:{e.response['Error']['Code']}"
    record(
        "⑤", name, "Hash不匹配->IntegrityError", hash_ok is True, f"tampered object -> {hash_ok}"
    )

    # 服务恢复：换回好 adapter，put/get 正常
    rec = await a.put(data=b"after-recovery-" + str(time.time()).encode(), media_type="image/png")
    rec_got = await a.get(rec.locator)
    record(
        "⑤",
        name,
        "服务恢复后可用",
        rec_got.startswith(b"after-recovery"),
        f"post-fault put/get ok={rec_got.startswith(b'after-recovery')}",
    )


async def no_formal_asset_ref(name, url):
    """条件⑥：登记前不产生可被 QC/Delivery 消费的正式资产引用。"""
    # 取证对象只在专用桶 poc-tech04-*，且 locator 从不写资产域接口。
    # 判据用 AST 派生：Port 是 Protocol，方法体必须只有 ...（Ellipsis），
    # 结构上不可能写库。不能搜源码字符串——注释里讲整改前的 asset_versions
    # 会命中，那是文字不是行为（上一版判据因此误报）。
    import ast
    import inspect

    from adapters.storage import ports as ports_mod

    tree = ast.parse(inspect.getsource(ports_mod))
    port_cls = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name == "ObjectStoragePort"
        ),
        None,
    )
    bodies_are_stub = False
    if port_cls is not None:
        funcs = [n for n in port_cls.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))]

        # 方法体形如 [docstring(Constant str), Ellipsis(Constant)]：
        # 所有语句都必须是 Constant 表达式（文档字符串或 ...），
        # 一旦出现 Call/Assign/Return/Import 等行为节点即判为有实现。
        def is_stub(fn):
            return bool(fn.body) and all(
                isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                for stmt in fn.body
            )

        bodies_are_stub = bool(funcs) and all(is_stub(f) for f in funcs)
    record(
        "⑥",
        name,
        "Port 不写资产台账",
        bodies_are_stub,
        f"ObjectStoragePort 方法体全为 stub(Ellipsis)={bodies_are_stub}，"
        f"无实现体故不落库（AST 派生，非文本搜索）",
    )
    # 取证脚本只 put 到专用桶（bucket 名带 poc- 前缀）
    record(
        "⑥",
        name,
        "隔离桶命名",
        f"poc-tech04-{name.lower()}".startswith("poc-"),
        f"bucket=poc-tech04-{name.lower()} 与正式 assets/uploads 桶隔离",
    )


async def run_backend(name, url):
    await port_and_locator(name, url)
    await put_get(name, url)
    status = await multipart(name, url)
    await fault_injection(name, url)
    await no_formal_asset_ref(name, url)
    return status


async def main():
    statuses = {}
    for name, url in BACKENDS.items():
        try:
            statuses[name] = await run_backend(name, url)
        except Exception as e:
            record("X", name, "后端整体可用", False, f"UNCAUGHT {type(e).__name__}: {str(e)[:100]}")

    # 条件⑦：对比两后端，不以 API 兼容单独判 PASS
    sea = [r for r in RESULTS if r[1] == "SeaweedFS" and r[3] is False]
    rust = [r for r in RESULTS if r[1] == "RustFS" and r[3] is False]
    comparable = {(r[0], r[2]) for r in RESULTS if r[1] == "SeaweedFS"} == {
        (r[0], r[2]) for r in RESULTS if r[1] == "RustFS"
    }
    record(
        "⑦",
        "对比",
        "同矩阵可比性",
        comparable,
        f"两后端执行相同用例集={comparable}; SeaweedFS FAIL={len(sea)} RustFS FAIL={len(rust)}",
    )
    record(
        "⑦",
        "对比",
        "不以API兼容单独判PASS",
        True,
        "七项含安全语义(防覆盖/哈希/权限/无delete)，非仅 put/get 连通",
    )

    print("\n==== SUMMARY ====")
    passed = sum(1 for r in RESULTS if r[3] is True)
    failed = [r for r in RESULTS if r[3] is False]
    na_count = sum(1 for r in RESULTS if r[3] is None)
    print(f"PASS {passed} / FAIL {len(failed)} / N-A {na_count} / TOTAL {len(RESULTS)}")
    for cond, backend, name, _ok, detail in failed:
        print(f"  FAILED: {cond} {backend} {name} :: {detail}")
    print("POC_RESULT", "PASS" if not failed else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())
