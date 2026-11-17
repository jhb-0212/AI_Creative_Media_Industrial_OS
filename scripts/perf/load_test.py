"""API 压测脚本（F09：性能验收的可复现入口）。

为什么需要它：README 长期诚实登记「高并发/故障恢复无压测证据」，但「无证据」
不是终点——必须有一个可复现的压测入口，把「凭 pool_size=10 猜并发能力」升级为
「用真实请求测出 P50/P95/P99/QPS/错误率」。

设计约束（不引入重依赖，不污染生产代码）：
- 只用标准库 + httpx（已是生产依赖），不用 locust/k6 等重工具；若需要更强
  压测能力，方案文档给出 k6 脚本路径。
- 脚本只读：不写库、不改数据，压测走 GET /readyz（探针）与 GET 只读端点，
  避免压测污染业务数据。
- 输出 P50/P95/P99 延迟、QPS、错误率、状态码分布，可直接填入验收报告。

用法：
    python scripts/perf/load_test.py --url http://localhost:8000 \
        --concurrency 50 --requests 2000 --token <Bearer token>
    # 只压探针（无需鉴权）：
    python scripts/perf/load_test.py --url http://localhost:8000 --probe-only
"""

from __future__ import annotations

import argparse
import asyncio
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
        help="压测目标路径（默认 /readyz；压只读端点时如 /assets/{id}）",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    path = "/readyz" if args.probe_only else args.path
    url = args.url.rstrip("/") + path

    latencies: list[float] = []
    status_counter: Counter[int] = Counter()
    errors: list[str] = []

    semaphore = asyncio.Semaphore(args.concurrency)

    async def _worker(client: httpx.AsyncClient) -> None:
        async with semaphore:
            start = time.perf_counter()
            try:
                resp = await client.get(url, headers=headers)
                latencies.append(time.perf_counter() - start)
                status_counter[resp.status_code] += 1
            except Exception as exc:  # 连接失败等：计入错误，不中断压测
                errors.append(f"{type(exc).__name__}: {exc}")

    total_start = time.perf_counter()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0), limits=httpx.Limits(max_connections=args.concurrency)
    ) as client:
        tasks = [_worker(client) for _ in range(args.requests)]
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

    print("=" * 60)
    print(f"压测目标: {url}")
    print(f"并发 {args.concurrency} / 总请求 {args.requests} / 耗时 {total_elapsed:.2f}s")
    print("-" * 60)
    print(f"成功请求: {len(latencies)}")
    print(f"QPS:      {qps:.2f}")
    print(f"平均延迟: {statistics.mean(latencies):.1f} ms")
    print(f"P50:      {p50 * 1000:.1f} ms")
    print(f"P95:      {p95 * 1000:.1f} ms")
    print(f"P99:      {p99 * 1000:.1f} ms")
    print(f"错误数:   {len(errors)}")
    print(f"状态码分布: {dict(sorted(status_counter.items()))}")
    if errors:
        print("错误样例（前 3 条）:")
        for err in errors[:3]:
            print(f"  - {err}")
    print("=" * 60)
    return 0


def _chunks(items: list[object], size: int) -> list[list[object]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


if __name__ == "__main__":
    sys.exit(asyncio.run(_run(_parse_args())))
