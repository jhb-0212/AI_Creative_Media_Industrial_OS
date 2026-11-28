"""质量策略权威源判据（ADR-R42 C4）。

权威值是契约裁定的单值 UNIFIED_HIGH_QUALITY（主文档 4.1 / ADR-R17 /
修订 V205-R14-A5）。本测试钉住两件事：
1. 唯一合法值就是契约单值——不接受任何自报或「当前版本」解析；
2. 任何空值/未知值在消费端 fail-closed，不存在「兜底默认」。
"""

from __future__ import annotations

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.quality_profile import (
    UNIFIED_HIGH_QUALITY,
    require_quality_profile,
)


def test_authoritative_value_is_contract_single_value() -> None:
    assert UNIFIED_HIGH_QUALITY == "UNIFIED_HIGH_QUALITY"


def test_require_accepts_only_authoritative_value() -> None:
    assert require_quality_profile(UNIFIED_HIGH_QUALITY) == UNIFIED_HIGH_QUALITY
    # 允许首尾空白（来自持久化字段的正常化），不接受别名。
    assert require_quality_profile(f"  {UNIFIED_HIGH_QUALITY}  ") == UNIFIED_HIGH_QUALITY


@pytest.mark.parametrize(
    "bad", ["", "   ", "qp-v1", "UNIFIED_HIGH_QUALITY@v2", "SUPPORTING_ASSET_QUALITY"]
)
def test_require_rejects_empty_or_unknown(bad: str) -> None:
    with pytest.raises(DomainError):
        require_quality_profile(bad)
