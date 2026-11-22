"""API 压测脚本（F09：性能验收的可复现入口）。

为什么需要它：README 长期诚实登记「高并发/故障恢复无压测证据」，但「无证据」
不是终点——必须有一个可复现的压测入口，把「凭 pool_size=10 猜并发能力」升级为
「用真实请求测出 P50/P95/P99/QPS/错误率」。

两种模式（对应 performance-acceptance-plan.md 阈值表的两类路径）：

1. **只读模式**（默认）：压 ``GET /readyz``（探针）或任意只读端点。
   不写库，可对任何环境安全执行，用于测基座吞吐与 RLS 查询路径。

2. **写路径模式**（``--write-path``）：压 ``POST /assets``。
   **这是方案阈值表里唯一能验证「事务 + Outbox + 乐观锁」的路径**，也是此前
   缺失的能力——脚本原先只读，而验收方案要求写路径 P99，两边都不报错，
   于是 S5 永远差这一条证据。
   写模式必须显式给出 ``--token`` 与 ``--project-id``（鉴权与归属都是运行时
   真实约束，不能靠脚本绕过），并给每次请求打唯一 ``entity_id`` 前缀，
   便于事后按前缀清理。

设计约束（不引入重依赖，不污染生产代码）：
- 只用标准库 + httpx（已是生产依赖），不用 locust/k6 等重工具；若需要更强
  压测能力，方案文档给出 k6 脚本路径。
- 不负责清理：脚本不持有数据库凭证，也不该在压测环境之外删数据。
  写模式结束时打印本次写入的 entity_id 前缀与清理提示，由压测环境的运维执行。
- 输出 P50/P95/P99 延迟、QPS、错误率、状态码分布，可直接填入验收报告。

用法：
    # 只压探针（无需鉴权）：
    python scripts/perf/load_test.py --url http://localhost:8000 --probe-only

    # 只读端点（需 token）：
    python scripts/perf/load_test.py --path /assets/<id> --token <jwt>

    # 写路径（验收方案要求的事务 + Outbox + 乐观锁证据）：
    python scripts/perf/load_test.py --write-path \
        --project-id <perf-project-id> --token <jwt> \
        --concurrency 50 --requests 2000
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import statistics
import sys
import time
from collections import Counter

import httpx


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI 影视平台 API 压测")
    parser.add_argument("--url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--concurrency", type=int, default=50, help="并发数")
    parser.add_argument("--requests", type=int, default=2000, help="总请求数")
    parser.add_argument("--token", default="", help="Bearer token（压鉴权端点时提供）")
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="只压 GET /readyz 探针（无需鉴权，验证基座吞吐）",
    )
    parser.add_argument(
        "--path",
        default="/readyz",
        help="压测目标路径（默认 /readyz；写路径模式下默认 /assets）",
    )
    parser.add_argument(
        "--write-path",
        action="store_true",
        help="压写路径 POST /assets（验证事务 + Outbox + 乐观锁，需 --token 与 --project-id）",
    )
    parser.add_argument(
        "--project-id",
        default="",
        help="写路径必填：压测专用项目 id（资产必须挂真实项目，归属是运行时约束）",
    )
    parser.add_argument(
        "--perf-tag",
        default="perf",
        help="写路径 entity_id 前缀（默认 perf），用于事后按前缀清理",
    )
    return parser.parse_args()


def _validate(args: argparse.Namespace) -> str | None:
    """返回错误信息（None 表示参数可用）。fail-fast，不让配置错误变成假压测结果。"""
    if args.write_path and args.probe_only:
        return "--write-path 与 --probe-only 互斥"
    if args.write_path and not args.token:
        return "写路径必须提供 --token：POST /assets 走真实鉴权，无 token 会全 401"
    if args.write_path and not args.project_id:
        return "写路径必须提供 --project-id：资产必须挂真实项目，否则归属校验必然拒绝"
    if args.concurrency <= 0 or args.requests <= 0:
        return "并发数与总请求数必须为正整数"
    return None


def _target_path(args: argparse.Namespace) -> str:
    """写路径模式默认打 /assets；显式给了 --path 则尊重它。"""
    if args.write_path:
        return args.path if args.path != "/readyz" else "/assets"
    return "/readyz" if args.probe_only else args.path


def _write_body(args: argparse.Namespace, run_id: str, index: int) -> dict[str, object]:
    """构造单次写请求体。

    每次请求用**不同 sha256**：同内容重复登记会命中幂等回读（返回既有版本），
    那样压到的是「查询 + 幂等判定」而不是「插入 + 事务 + Outbox」。
    entity_id 带 run_id 与序号，既保证唯一主键，也让一次压测的数据可整体识别。
    """
    sha = hashlib.sha256(f"{run_id}:{index}".encode()).hexdigest()
    return {
        "project_id": args.project_id,
        "asset_type": "IMAGE",
        "sha256": sha,
        "storage_ref": {"uri": f"s3://perf/{run_id}/{index:06d}.png"},
        "rights_manifest_ref": "perf-rights-manifest",
        "entity_id": f"{args.perf_tag}-{run_id}-{index:06d}",
    }


async def _run(args: argparse.Namespace) -> int:
    problem = _validate(args)
    if problem is not None:
        print(f"[FAIL] {problem}")
        return 2

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    path = _target_path(args)
    url = args.url.rstrip("/") + path
    run_id = time.strftime("%Y%m%d%H%M%S")

    latencies: list[float] = []
    status_counter: Counter[int] = Counter()
    errors: list[str] = []

    semaphore = asyncio.Semaphore(args.concurrency)

    async def _worker(client: httpx.AsyncClient, index: int) -> None:
        async with semaphore:
            start = time.perf_counter()
            try:
                if args.write_path:
                    resp = await client.post(
                        url, headers=headers, json=_write_body(args, run_id, index)
                    )
                else:
                    resp = await client.get(url, headers=headers)
                latencies.append(time.perf_counter() - start)
                status_counter[resp.status_code] += 1
                # HTTP 4xx/5xx 也是错误（Codex P2）：只记异常会让「全部 500」
                # 的压测报 0 错误、退出 0，错误率验收被系统性低估。
                if resp.is_error:
                    errors.append(f"HTTP {resp.status_code}")
            except Exception as exc:  # 连接失败等：计入错误，不中断压测
                errors.append(f"{type(exc).__name__}: {exc}")

    total_start = time.perf_counter()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0), limits=httpx.Limits(max_connections=args.concurrency)
    ) as client:
        tasks = [_worker(client, i) for i in range(args.requests)]
        # 分批提交，避免一次性创建海量协程占用内存。
        for batch in _chunks(tasks, 500):
            await asyncio.gather(*batch)
    total_elapsed = time.perf_counter() - total_start

    if not latencies:
        print("[FAIL] 无任何成功请求，无法统计延迟")
        for err in errors[:5]:
            print(f"  错误: {err}")
        return 1

    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.50)]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[min(int(len(latencies) * 0.99), len(latencies) - 1)]
    qps = len(latencies) / total_elapsed
    # 成功 = 2xx；错误率分母是全部发起的请求（含超时/连接失败/4xx/5xx）。
    success = sum(count for status, count in status_counter.items() if 200 <= status < 300)
    error_rate = len(errors) / args.requests if args.requests else 0.0

    print("=" * 60)
    print(f"压测目标: {'POST ' if args.write_path else 'GET '}{url}")
    print(f"并发 {args.concurrency} / 总请求 {args.requests} / 耗时 {total_elapsed:.2f}s")
    print("-" * 60)
    print(f"成功请求(2xx): {success} / {args.requests}")
    print(f"错误率:   {error_rate:.2%}（HTTP 非 2xx + 异常）")
    print(f"QPS:      {qps:.2f}")
    # 延迟统一按毫秒输出：latencies 存的是秒，漏乘 1000 会得到「均值 0.1ms、
    # P50 79ms」这种自相矛盾的数字，验收报告据此下结论就会误判。
    print(f"平均延迟: {statistics.mean(latencies) * 1000:.1f} ms")
    print(f"P50:      {p50 * 1000:.1f} ms")
    print(f"P95:      {p95 * 1000:.1f} ms")
    print(f"P99:      {p99 * 1000:.1f} ms")
    print(f"错误数:   {len(errors)}")
    print(f"状态码分布: {dict(sorted(status_counter.items()))}")
    if errors:
        print("错误样例（前 3 条）:")
        for err in errors[:3]:
            print(f"  - {err}")
    if args.write_path and success:
        # 不代删数据：脚本没有数据库凭证，也不该在压测环境之外执行写操作。
        # 实测确认（2026-11-18）：asset_versions 有 ADR-R11 触发器，
        # 连超级用户的 DELETE 都会被拒——「压完清理」是做不到的。
        # 所以写模式只能对专用库/专用租户执行，这条约束必须写在输出里，
        # 否则下一个人对着生产库压一次，就永久留下一批不可删的脏数据。
        prefix = f"{args.perf_tag}-{run_id}-"
        print("-" * 60)
        print(f"本次写入 {success} 条资产（entity_id 前缀 {prefix}）")
        print("注意：asset_versions 为追加型不可变表（ADR-R11 触发器禁止 DELETE，")
        print("      超级用户同样被拒），本脚本写入的数据**无法清理**。")
        print("      写路径压测只能对专用库或专用租户执行，不可对生产库压。")
    print("=" * 60)
    # 存在任何错误（含 HTTP 4xx/5xx）即非零退出：错误率必须被消费方显式看见，
    # 而不是靠人读输出发现「成功请求其实全是 500」。
    return 1 if errors else 0


def _chunks(items: list[object], size: int) -> list[list[object]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


if __name__ == "__main__":
    sys.exit(asyncio.run(_run(_parse_args())))
