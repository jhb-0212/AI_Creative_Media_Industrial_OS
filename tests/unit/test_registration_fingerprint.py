"""登记请求指纹的单元测试（PR-F）。

指纹是 T2 幂等冲突判定的地基：同一语义必须得到同一指纹（否则合法重放被误判成
冲突），任一维度变化必须得到不同指纹（否则内容冲突被洗成幂等）。这两条都要测。
"""

from __future__ import annotations

from uuid import UUID

from packages.shared.registration_fingerprint import (
    FINGERPRINT_FIELDS,
    compute_registration_fingerprint,
)

_TENANT = UUID("00000000-0000-0000-0000-000000000041")


def _fp(**overrides: object) -> str:
    values: dict[str, object] = {
        "tenant_id": _TENANT,
        "generation_task_id": "task-1",
        "project_id": "proj-1",
        "attempt_id": "attempt-1",
        "locator": "local://ab/cd/" + "ab" + "cd" + "e" * 60 + ".png",
        "sha256": "ABCD" + "e" * 60,  # 含大写，测试归一化
        "media_type": "image/png",
        "quality_profile_version": "unified-high-quality-v1",
    }
    values.update(overrides)
    return compute_registration_fingerprint(**values)  # type: ignore[arg-type]


def test_fingerprint_is_deterministic() -> None:
    assert _fp() == _fp()


def test_fingerprint_is_64_lowercase_hex() -> None:
    fp = _fp()
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_sha256_case_is_normalized() -> None:
    # 同一摘要大小写不同应视为同一语义，不产生假冲突。
    assert _fp(sha256="ABCD" + "e" * 60) == _fp(sha256="abcd" + "e" * 60)


def test_every_dimension_changes_fingerprint() -> None:
    # 每个维度单独变化都必须改变指纹——漏一个维度就会让对应冲突被洗成幂等。
    changed_values: dict[str, object] = {
        "tenant_id": UUID("00000000-0000-0000-0000-000000000042"),
        "generation_task_id": "task-2",
        "project_id": "proj-2",
        "attempt_id": "attempt-2",
        "locator": "local://ab/cd/" + "ab" + "cd" + "f" * 60 + ".png",
        "sha256": "f" * 64,
        "media_type": "video/mp4",
        "quality_profile_version": "unified-high-quality-v2",
    }
    baseline = _fp()
    for field in FINGERPRINT_FIELDS:
        assert _fp(**{field: changed_values[field]}) != baseline, (
            f"维度 {field} 变化未改变指纹——该维度的冲突会被误判为幂等"
        )


def test_quality_profile_from_frozen_task_is_distinguished() -> None:
    # 质量策略是 D7 加固维度：冻结任务版本不同必须产生不同指纹。
    assert _fp(quality_profile_version="v1") != _fp(quality_profile_version="v2")
