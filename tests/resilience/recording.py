"""韧性测试的度量记录（把"跑过了"变成"可核对的数字"）。

为什么不用 pytest 的自定义插件：证据要能被 ``scripts/resilience_check.py``
原样消费，而插件只在 pytest 进程里存在。测试**自己**把度量写成 JSON 文件，
脚本只做汇总——证据的产出路径不依赖任何"框架记得调用我们"的约定。

写入路径由 ``RESILIENCE_METRICS_PATH`` 指定（脚本会注入临时路径），默认落在
``artifacts/resilience/metrics.json``（不入库）。多次运行追加而不是覆盖：
同一台机器上重跑一次不该抹掉上一次的数字。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ENV_PATH = "RESILIENCE_METRICS_PATH"
_DEFAULT_PATH = Path("artifacts/resilience/metrics.json")


def record(scenario: str, **metrics: Any) -> None:
    """追加一条场景度量（字段名即证据字段名，不做二次改名）。"""
    path = Path(os.environ.get(_ENV_PATH) or _DEFAULT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    existing.append(
        {
            "scenario": scenario,
            "recorded_at": datetime.now(UTC).isoformat(),
            **metrics,
        }
    )
    # write_bytes：Windows 上 write_text 会把 \n 翻译成 \r\n，导致同一份证据
    # 在不同机器上字节不同（diff 噪声）。证据文件必须是稳定字节。
    path.write_bytes(
        json.dumps(existing, ensure_ascii=False, indent=2).encode("utf-8")
    )


def percentile(values: list[float], p: float) -> float:
    """线性插值分位数（p ∈ [0,1]；空列表返回 0，避免证据里出现 NaN）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = p * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight
