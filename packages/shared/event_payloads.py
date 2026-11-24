"""被专用 handler 消费的事件载荷模型（严格反序列化边界）。

为什么需要它
------------
事件一旦跨进程投递，payload 就是外部输入。此前消费端用
``str(payload.get(name) or "").strip()`` 取值：未知字段被静默忽略，错误类型
（dict / list / int）被 ``str()`` 变成看似合法的字符串，缺少版本时也没有任何
信号。调用方以为身份或内容生效了，实际只是被吞掉——这是比 422 更危险的失败。

本模块把「能消费什么」变成模型事实：

- ``extra="forbid"``：未知字段直接拒绝；
- ``strict=True``：类型不符直接拒绝，不做隐式强转；
- ``schema_version`` 必填且只接受当前版本：缺失或新版本都 fail-closed；
- 字段集合取**实际发出的全集**，不是 event_catalog 的 ``payload_required``
  最小集（``advance_status`` 还会注入 from_status/to_status/tenant_id/
  transition_id）。照最小集写会拒绝所有合法事件；

写入侧用 :func:`stamp_event_payload` 打版本，消费侧用
:func:`parse_event_payload` 解析；两侧共用同一份字段定义，不再靠人记得同步。

48 个只走 webhook 外发、不解析 payload 的事件不在此登记——它们没有消费边界，
强行建模只会造出无法证伪的空壳。
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal, get_args

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

# 事件载荷 schema 版本。改这里必须同时改各模型的 Literal 注解；测试会派生校验。
EVENT_PAYLOAD_SCHEMA_VERSION: Final = "1"
SCHEMA_VERSION_FIELD: Final = "schema_version"


class EventPayloadError(ValueError):
    """事件载荷不符合消费契约：发出方违约，交由 relay 失败/死信路径处理。"""


def _non_blank(value: str) -> str:
    """纯空白与缺失等价：留着会让「没给事实」变成看似合法的空格字符串。"""
    stripped = value.strip()
    if not stripped:
        raise ValueError("不能为空白")
    return stripped


NonBlankStr = Annotated[str, Field(min_length=1), AfterValidator(_non_blank)]


class StrictEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"]


class TransitionEventPayload(StrictEventPayload):
    """由 ``advance_status`` 统一补齐的四个公共字段。"""

    from_status: str
    to_status: str
    tenant_id: str
    transition_id: str


class GenerationSucceededPayload(TransitionEventPayload):
    generation_task_id: NonBlankStr
    worker_id: NonBlankStr
    evidence_ref: str
    attempt_count: int
    project_id: NonBlankStr
    shot_id: str
    asset_version_id: NonBlankStr
    quality_profile_version: NonBlankStr


class AssetRegistrationRequestedPayload(TransitionEventPayload):
    generation_task_id: NonBlankStr
    project_id: NonBlankStr
    locator: NonBlankStr
    sha256: NonBlankStr
    attempt_id: NonBlankStr
    media_type: NonBlankStr
    quality_profile_version: str
    worker_id: NonBlankStr


class AssetRegistrationCompletedPayload(StrictEventPayload):
    generation_task_id: NonBlankStr
    project_id: NonBlankStr
    tenant_id: NonBlankStr
    attempt_id: NonBlankStr
    asset_version_id: NonBlankStr
    quality_profile_version: NonBlankStr


class AssetRegistrationRejectedPayload(StrictEventPayload):
    generation_task_id: NonBlankStr
    project_id: NonBlankStr
    tenant_id: NonBlankStr
    attempt_id: NonBlankStr
    reason: NonBlankStr


class AssetRegistrationConflictPayload(StrictEventPayload):
    generation_task_id: NonBlankStr
    project_id: NonBlankStr
    tenant_id: NonBlankStr
    expected_fingerprint: str
    observed_fingerprint: str
    observed: dict[str, str]


# 消费端唯一注册表。键必须等于 event_type；新增专用 handler 时同步登记。
EVENT_PAYLOAD_SCHEMAS: Final[dict[str, type[StrictEventPayload]]] = {
    "GENERATION_SUCCEEDED": GenerationSucceededPayload,
    "ASSET_REGISTRATION_REQUESTED": AssetRegistrationRequestedPayload,
    "ASSET_REGISTRATION_COMPLETED": AssetRegistrationCompletedPayload,
    "ASSET_REGISTRATION_REJECTED": AssetRegistrationRejectedPayload,
    "ASSET_REGISTRATION_CONFLICT": AssetRegistrationConflictPayload,
}


def stamp_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """写入侧打版本；调用方只声明业务字段，版本由这里统一注入。"""
    return {**payload, SCHEMA_VERSION_FIELD: EVENT_PAYLOAD_SCHEMA_VERSION}


def parse_event_payload(event_type: str, payload: dict[str, Any]) -> StrictEventPayload:
    """按 event_type 严格解析；未知事件/未知字段/未知版本一律 fail-closed。"""
    schema = EVENT_PAYLOAD_SCHEMAS.get(event_type)
    if schema is None:
        raise EventPayloadError(f"{event_type} 未登记载荷模型，拒绝消费")
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise EventPayloadError(f"{event_type} 载荷不符合契约：{exc}") from exc


def declared_schema_versions() -> dict[str, str]:
    """派生每个模型声明的版本，供自检比对常量与注解是否漂移。"""
    result: dict[str, str] = {}
    for name, schema in EVENT_PAYLOAD_SCHEMAS.items():
        literal_args = get_args(schema.model_fields[SCHEMA_VERSION_FIELD].annotation)
        result[name] = literal_args[0] if len(literal_args) == 1 else "<invalid>"
    return result


__all__ = [
    "EVENT_PAYLOAD_SCHEMAS",
    "EVENT_PAYLOAD_SCHEMA_VERSION",
    "SCHEMA_VERSION_FIELD",
    "AssetRegistrationCompletedPayload",
    "AssetRegistrationConflictPayload",
    "AssetRegistrationRejectedPayload",
    "AssetRegistrationRequestedPayload",
    "EventPayloadError",
    "GenerationSucceededPayload",
    "StrictEventPayload",
    "declared_schema_versions",
    "parse_event_payload",
    "stamp_event_payload",
]
