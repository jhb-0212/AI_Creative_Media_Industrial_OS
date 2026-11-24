"""专用 handler 的事件载荷严格性（第四批）。

判据不是「解析没抛异常」——那是假阳性温床。每条都注入一个具体的坏状态：
未知字段、缺版本、未知版本、类型错误，并断言被拒绝；另有一条派生断言保证
handler 注册表与载荷模型注册表集合相等，防止新增消费端忘了登记模型。
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest

from packages.shared.event_payloads import (
    EVENT_PAYLOAD_SCHEMA_VERSION,
    EVENT_PAYLOAD_SCHEMAS,
    EventPayloadError,
    declared_schema_versions,
    parse_event_payload,
    stamp_event_payload,
)
from workers.relay.asset_registration import build_asset_registration_handlers
from workers.relay.handlers import build_generation_handlers

_TENANT = str(uuid4())


def _transition_base() -> dict[str, Any]:
    return {
        "schema_version": EVENT_PAYLOAD_SCHEMA_VERSION,
        "from_status": "AWAITING_ASSET_REGISTRATION",
        "to_status": "SUCCEEDED",
        "tenant_id": _TENANT,
        "transition_id": "tr-1",
    }


def _samples() -> dict[str, dict[str, Any]]:
    return {
        "GENERATION_SUCCEEDED": {
            **_transition_base(),
            "generation_task_id": "task-1",
            "worker_id": "svc:asset-registration",
            "evidence_ref": "receipt-ok",
            "attempt_count": 1,
            "project_id": "proj-1",
            "shot_id": "shot-1",
            "asset_version_id": "av-1",
            "quality_profile_version": "qp-1",
        },
        "ASSET_REGISTRATION_REQUESTED": {
            **_transition_base(),
            "generation_task_id": "task-1",
            "project_id": "proj-1",
            "locator": "local://ab/cd/abcd" + "e" * 60 + ".png",
            "sha256": "abcd" + "e" * 60,
            "attempt_id": "attempt-1",
            "media_type": "image/png",
            "quality_profile_version": "qp-1",
            "worker_id": "worker-1",
        },
        "ASSET_REGISTRATION_COMPLETED": {
            "schema_version": EVENT_PAYLOAD_SCHEMA_VERSION,
            "generation_task_id": "task-1",
            "project_id": "proj-1",
            "tenant_id": _TENANT,
            "attempt_id": "attempt-1",
            "asset_version_id": "av-1",
            "quality_profile_version": "qp-1",
        },
        "ASSET_REGISTRATION_REJECTED": {
            "schema_version": EVENT_PAYLOAD_SCHEMA_VERSION,
            "generation_task_id": "task-1",
            "project_id": "proj-1",
            "tenant_id": _TENANT,
            "attempt_id": "attempt-1",
            "reason": "sha256 mismatch",
        },
        "ASSET_REGISTRATION_CONFLICT": {
            "schema_version": EVENT_PAYLOAD_SCHEMA_VERSION,
            "generation_task_id": "task-1",
            "project_id": "proj-1",
            "tenant_id": _TENANT,
            "expected_fingerprint": "a" * 64,
            "observed_fingerprint": "b" * 64,
            "observed": {"sha256": "c" * 64},
        },
    }


@pytest.mark.parametrize("event_type", sorted(EVENT_PAYLOAD_SCHEMAS))
def test_complete_payload_is_accepted(event_type: str) -> None:
    parse_event_payload(event_type, _samples()[event_type])


@pytest.mark.parametrize("event_type", sorted(EVENT_PAYLOAD_SCHEMAS))
def test_unknown_field_is_rejected(event_type: str) -> None:
    payload = {**_samples()[event_type], "unexpected": 1}
    with pytest.raises(EventPayloadError, match=r"unexpected|载荷不符合契约"):
        parse_event_payload(event_type, payload)


@pytest.mark.parametrize("event_type", sorted(EVENT_PAYLOAD_SCHEMAS))
def test_missing_or_unknown_version_is_rejected(event_type: str) -> None:
    payload = _samples()[event_type]
    without = {k: v for k, v in payload.items() if k != "schema_version"}
    with pytest.raises(EventPayloadError):
        parse_event_payload(event_type, without)
    with pytest.raises(EventPayloadError):
        parse_event_payload(event_type, {**payload, "schema_version": "999"})


def test_wrong_types_are_not_coerced() -> None:
    succeeded = _samples()["GENERATION_SUCCEEDED"]
    with pytest.raises(EventPayloadError):
        parse_event_payload(
            "GENERATION_SUCCEEDED", {**succeeded, "attempt_count": "1"}
        )
    conflict = _samples()["ASSET_REGISTRATION_CONFLICT"]
    with pytest.raises(EventPayloadError):
        parse_event_payload(
            "ASSET_REGISTRATION_CONFLICT", {**conflict, "observed": "not-a-dict"}
        )


def test_blank_critical_fact_is_rejected() -> None:
    succeeded = _samples()["GENERATION_SUCCEEDED"]
    with pytest.raises(EventPayloadError):
        parse_event_payload(
            "GENERATION_SUCCEEDED", {**succeeded, "asset_version_id": "   "}
        )


def test_writer_stamps_the_same_version() -> None:
    stamped = stamp_event_payload({"x": 1})
    assert stamped["schema_version"] == EVENT_PAYLOAD_SCHEMA_VERSION
    assert declared_schema_versions() == dict.fromkeys(EVENT_PAYLOAD_SCHEMAS, EVENT_PAYLOAD_SCHEMA_VERSION)


def test_handler_registry_and_payload_registry_are_the_same_set() -> None:
    """新增专用 handler 却忘了登记载荷模型时，这条必须红。"""
    handlers: dict[str, Any] = {}
    handlers.update(build_generation_handlers(lambda _event: None))
    handlers.update(
        build_asset_registration_handlers(
            production_planning_sessionmaker=cast(Any, object()),
            asset_sessionmaker=cast(Any, object()),
        )
    )
    # relay 装配时依赖为空则不注册；这里用哨兵工厂强制两侧都注册。
    assert set(handlers) == set(EVENT_PAYLOAD_SCHEMAS)
