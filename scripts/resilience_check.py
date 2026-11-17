"""韧性证据取证脚本：跑压测/崩溃恢复/崩溃重投，并把度量落成可核对的 JSON。

用法：

    python scripts/resilience_check.py                     # 默认写 docs/evidence/phase1-resilience.json
    python scripts/resilience_check.py --output out.json    # 自定义输出
    python scripts/resilience_check.py --keep-metrics       # 保留每场景明细

为什么单独一个脚本而不是把 resilience 测试塞进默认 pytest 闸门：
- 这些用例按规模耗时（并发 worker、崩溃回收轮询），放默认闸门会逼人调小规模；
- 证据需要**环境上下文**（commit、Python、平台、退出码）才可核对，pytest
  输出本身不带这些。

脚本不判断"是否达标"：阈值判断写在测试断言里（不变量），这里只做汇总与
落盘——把阈值判断搬进脚本会让"证据"变成"脚本说合格"。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "evidence" / "phase1-resilience.json"
_METRICS_ENV = "RESILIENCE_METRICS_PATH"


def _git_commit() -> str:
    """当前 HEAD（拿不到就写 unknown，不让证据生成失败）。"""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _run_pytest(metrics_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, _METRICS_ENV: str(metrics_path)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "resilience", "-q", "--durations=5"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _load_metrics(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1 韧性证据取证")
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="证据 JSON 输出路径（默认 docs/evidence/phase1-resilience.json）",
    )
    parser.add_argument(
        "--keep-metrics",
        action="store_true",
        help="保留每场景明细的临时度量文件（调试用）",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        metrics_path = Path(tmp) / "metrics.json"
        completed = _run_pytest(metrics_path)
        metrics = _load_metrics(metrics_path)
        if args.keep_metrics:
            kept = Path("artifacts/resilience/metrics.json")
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_bytes(json.dumps(metrics, ensure_ascii=False, indent=2).encode())

    tail = [line for line in completed.stdout.strip().splitlines() if line.strip()][-5:]
    payload = {
        "schema": "phase1-resilience-evidence/1",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": _git_commit(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "note": (
                "单机开发环境、共享 PostgreSQL、ASGITransport 进程内调用；"
                "不构成生产容量承诺（基线 12.6）"
            ),
        },
        "pytest_exit_code": completed.returncode,
        "pytest_summary": tail,
        "scenarios": metrics,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # write_bytes：证据文件必须是稳定字节（Windows 的 write_text 会翻成 CRLF）。
    args.output.write_bytes(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    )

    print(completed.stdout)
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
    print(f"evidence written: {args.output}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
