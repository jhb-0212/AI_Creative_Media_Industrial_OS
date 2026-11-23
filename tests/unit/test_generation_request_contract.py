"""生成请求/终结的入参契约（P1-02 / P1-06）。

判据是「请求方无法表达执行结果、也无法自称身份」这一条语义，不是某个字段名：
字段集合里不允许出现 ``result`` 与 ``worker_id``，且传了就报错而不是被静默忽略——
静默忽略会让调用方误以为自己提交了成功/指定了执行者，两者都比拒绝更糟。

``worker_id`` 自 P1-06 起从三个租约请求模型里移除：租约持有者取自 Principal。
把它留在请求体里，``require_lease_owner`` 就退化成一次公开的字符串比较。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.production_planning.api.generation_routes import (
    FinishRequest,
    RequestGenerationRequest,
)

_VALID_BODY = {
    "project_id": "proj-1",
    "shot_id": "shot-1",
    "shot_revision_id": "rev-1",
    "production_route_ref": "route-1",
    "model_adapter_ref": "adapter-1",
    "constraint_snapshot_id": "snap-1",
    "budget_ref": "budget-1",
    "prompt_ref": "prompt-1",
}


def test_request_model_has_no_result_field() -> None:
    """创建请求不得有任何表达执行结果的字段。

    按字段集合判定而不是只测「传 result 会 422」：若有人把字段改名成
    ``outcome`` 再加回来，只测固定字段名的用例会全绿而语义已被破坏。
    """
    fields = set(RequestGenerationRequest.model_fields)
    assert "result" not in fields
    assert "outcome" not in fields
    assert "status" not in fields
    assert fields == {
        "project_id",
        "shot_id",
        "shot_revision_id",
        "production_route_ref",
        "model_adapter_ref",
        "constraint_snapshot_id",
        "budget_ref",
        "prompt_ref",
        "generation_task_id",
    }


def test_request_rejects_result_field_explicitly() -> None:
    """旧调用方仍传 ``result`` 时必须 422，不能被静默忽略。"""
    with pytest.raises(ValidationError) as excinfo:
        RequestGenerationRequest(**_VALID_BODY, result="SUCCEEDED")
    assert "result" in str(excinfo.value)


def test_request_accepts_clean_body() -> None:
    assert RequestGenerationRequest(**_VALID_BODY).generation_task_id == ""


def test_finish_request_carries_product_refs() -> None:
    """终结请求必须能登记产物引用，否则 API 路径永远写不进 asset_version_id。"""
    body = FinishRequest(
        result="SUCCEEDED",
        asset_version_id="av-1",
        quality_profile_version="qp-1",
    )
    assert body.asset_version_id == "av-1"
    assert body.quality_profile_version == "qp-1"


def test_finish_request_allows_empty_refs_at_model_level() -> None:
    """模型层允许留空，拒绝发生在服务层。

    这里刻意不在 Pydantic 层强制非空：``FAILED`` 本来就没有产物，
    模型层无法区分「哪种结果的产物是必填」。强制点放在服务层的
    「SUCCEEDED 必须带产物」处，一处判定、语义集中。
    """
    body = FinishRequest(result="FAILED")
    assert body.asset_version_id == ""
    assert body.quality_profile_version == ""
