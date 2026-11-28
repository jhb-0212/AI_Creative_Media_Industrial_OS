"""GenerationSpec 生成输入快照（ADR-R47 P2）。

为什么它必须是**内容寻址**的不可变记录
------------------------------------
生成任务的请求方只需给出「哪条生产路线 + 哪个镜头规格」，提示词、尺寸、媒体类型
都由系统派生。派生结果必须冻结：任务开始执行后再去重算，等于允许「同一任务在不同
时刻生成不同内容」，这会让证据链失效且无法对账。

因此身份由内容决定：

    spec_sha256 = sha256("afs:generation-spec:v1" + LF + tenant_id + LF + canonical_bytes)
    generation_spec_id = "gs1_" + base32(spec_sha256)[:26]

``tenant_id`` 进哈希域：同一份内容在不同租户下是两份规格，租户归属不能靠一张旁表
随便改——改了哈希就对不上。

常规化规则（逐条都是判据，不是风格）
----------------------------------
- JSON 键按 UTF-8 字节序（ASCII 键与码点序一致）、无空白、``ensure_ascii=False``；
- 文本 NFC；``\\r\\n``/``\\r`` 统一成 ``\\n``；**不 trim**（首尾空白是内容，不是格式）；
- 整数不带小数点；小数只允许以字符串承载（浮点表示不唯一，不能进身份域）；
- ``negative_prompt`` 缺省时**键不出现**（``""`` 与缺失是两种语义）；
- 空串/纯空白一律拒绝，不做「空等于无」的隐式归一。

``prompt_sha256`` 只覆盖提示词正文（NFC 字节），不进身份域：它是与网关证据对账的
独立列，允许在两份内容相同的规格之间相等，不参与「这是不是同一份规格」的判定。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from unicodedata import normalize as unicode_normalize

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from packages.shared.generation_template import TEMPLATE_VERSION, render_prompt
from packages.shared.size_policy import (
    SIZE_POLICY_ID,
    SIZE_POLICY_VERSION,
    aspect_matches,
    resolve_size,
)
from services.project_planning.domain.enums import NoLifecycleStatus

DERIVATION_VERSION = "afs-derive-v1"
SPEC_SCHEMA_VERSION = 1

_ID_PREFIX = "gs1_"
_DOMAIN_SEPARATOR = "afs:generation-spec:v1"

#: 媒体类型白名单。``application/octet-stream`` 被刻意排除：它是「不知道是什么」
#: 的兜底值，允许它进规格等于允许一份说不清产物形态的任务通过准入。
MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "video/mp4", "video/webm", "audio/wav"}
)

#: 生成参数键白名单（Owner 裁决：≤16 键、仅 string/整数/布尔，不做自由 JSON）。
PARAMETER_KEYS: frozenset[str] = frozenset({"guidance_scale", "seed", "steps"})
_MAX_PARAMETER_KEYS = 16

_SIZE_RE = re.compile(r"^[1-9][0-9]{2,4}x[1-9][0-9]{2,4}$")
_RATIO_RE = re.compile(r"^[1-9][0-9]{0,2}:[1-9][0-9]{0,2}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def normalize_text(value: str) -> str:
    """把文本收敛到唯一字节表示（NFC + 换行统一），不做 trim。"""
    text = unicode_normalize("NFC", value)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """身份域 JSON 的规范字节序列（排序键、无空白、不转义非 ASCII）。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def compute_spec_sha256(tenant_id: str, payload: dict[str, Any]) -> str:
    """按固定域前缀 + 租户 + 规范字节计算规格摘要。"""
    material = (
        _DOMAIN_SEPARATOR.encode("utf-8")
        + b"\n"
        + str(tenant_id).encode("utf-8")
        + b"\n"
        + canonical_bytes(payload)
    )
    return hashlib.sha256(material).hexdigest()


def generation_spec_id_for(spec_sha256: str) -> str:
    digest_bytes = bytes.fromhex(spec_sha256)
    encoded = base64.b32encode(digest_bytes).decode("ascii").lower().rstrip("=")
    return _ID_PREFIX + encoded[:26]


def _canonical_value(value: Any, *, key: str) -> Any:
    """参数值的唯一表示：string / 整数 / 布尔。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return normalize_text(value)
    raise DomainError(
        ErrorRegistry.VALIDATION_ERROR,
        detail=f"generation_parameters[{key}] 只允许字符串/整数/布尔，实际为 {type(value).__name__}",
    )


def normalize_parameters(raw: dict[str, Any] | None) -> dict[str, Any]:
    """按白名单校验并规范化生成参数（不做类型转换）。"""
    params = dict(raw or {})
    if len(params) > _MAX_PARAMETER_KEYS:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=f"generation_parameters 键数 {len(params)} 超过上限 {_MAX_PARAMETER_KEYS}",
        )
    unknown = sorted(set(params) - PARAMETER_KEYS)
    if unknown:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=f"generation_parameters 含未登记键 {unknown}（白名单: {sorted(PARAMETER_KEYS)}）",
        )
    return {key: _canonical_value(value, key=key) for key, value in params.items()}


def identity_payload(
    *,
    project_id: str,
    shot_revision_id: str,
    shot_spec_id: str,
    production_route_ref: str,
    constraint_snapshot_id: str,
    template_version: str,
    derivation_version: str,
    size_policy_id: str,
    size_policy_version: str,
    model_id: str,
    provider: str,
    generation_parameters: dict[str, Any],
    prompt_text: str,
    negative_prompt: str | None,
    media_type: str,
    size: str,
    aspect_ratio: str,
) -> dict[str, Any]:
    """身份域字段集合。``prompt_sha256``/``spec_sha256`` 不在其中（自指）。"""
    payload: dict[str, Any] = {
        "project_id": project_id,
        "shot_revision_id": shot_revision_id,
        "shot_spec_id": shot_spec_id,
        "production_route_ref": production_route_ref,
        "constraint_snapshot_id": constraint_snapshot_id,
        "template_version": template_version,
        "derivation_version": derivation_version,
        "size_policy_id": size_policy_id,
        "size_policy_version": size_policy_version,
        "model_id": model_id,
        "provider": provider,
        "generation_parameters": generation_parameters,
        "prompt_text": normalize_text(prompt_text),
        "media_type": media_type,
        "size": size,
        "aspect_ratio": aspect_ratio,
    }
    if negative_prompt is not None:
        payload["negative_prompt"] = normalize_text(negative_prompt)
    return payload


@dataclass(frozen=True)
class ShotSpecFacts:
    """派生所需的最小 ShotSpec 事实（跨域列级只读的投影）。

    刻意不携带整行：生产域只需要「哪份规格、哪个修订、画幅、可渲染字段、是否
    锁定」。把整行 ORM 对象传进来会让生产域的判定依赖另一个域的表结构，未来
    对方加一列就可能改变这里的比较结果。
    """

    entity_id: str
    project_id: str
    shot_revision_id: str
    aspect_ratio: str
    camera_path: str
    subject_paths: str
    shot_size_changes: str
    locked_dimensions: str
    status: str


@dataclass
class GenerationSpec(Entity):
    """生成输入快照（NO_LIFECYCLE；写入即冻结，无 UPDATE 路径）。"""

    ENTITY_TYPE = "GenerationSpec"

    owner_module: str = "production-planning"

    generation_spec_id: str = ""
    project_id: str = ""
    shot_revision_id: str = ""
    shot_spec_id: str = ""
    production_route_ref: str = ""
    constraint_snapshot_id: str = ""
    template_version: str = TEMPLATE_VERSION
    derivation_version: str = DERIVATION_VERSION
    size_policy_id: str = SIZE_POLICY_ID
    size_policy_version: str = SIZE_POLICY_VERSION
    model_id: str = ""
    provider: str = ""
    generation_parameters: dict[str, Any] = field(default_factory=dict)
    prompt_text: str = ""
    negative_prompt: str | None = None
    media_type: str = ""
    size: str = ""
    aspect_ratio: str = ""
    spec_sha256: str = ""
    prompt_sha256: str = ""
    status: NoLifecycleStatus = NoLifecycleStatus.NO_LIFECYCLE

    def __post_init__(self) -> None:
        super()._validate_envelope()
        if self.status is not NoLifecycleStatus.NO_LIFECYCLE:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"GenerationSpec 无生命周期，status 只能是 NO_LIFECYCLE，实际 {self.status!r}",
            )
        for name, max_len in (
            ("generation_spec_id", 64),
            ("project_id", 64),
            ("shot_revision_id", 64),
            ("shot_spec_id", 64),
            ("production_route_ref", 64),
            ("constraint_snapshot_id", 128),
            ("template_version", 64),
            ("derivation_version", 64),
            ("size_policy_id", 64),
            ("size_policy_version", 64),
            ("model_id", 128),
            ("provider", 64),
        ):
            value = str(getattr(self, name) or "")
            if not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度 {len(value)} 超过上限 {max_len}",
                )
        self.prompt_text = normalize_text(self.prompt_text)
        if not self.prompt_text.strip():
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="prompt_text 不能为空")
        if len(self.prompt_text) > 8192:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"prompt_text 长度 {len(self.prompt_text)} 超过上限 8192",
            )
        if self.negative_prompt is not None:
            self.negative_prompt = normalize_text(self.negative_prompt)
            if len(self.negative_prompt) > 4096:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"negative_prompt 长度 {len(self.negative_prompt)} 超过上限 4096",
                )
            if not self.negative_prompt.strip():
                # 「空负向提示」在语义上等于「没有负向提示」，保留空串会让
                # 身份域里出现两种等价写法（"" 与缺键）。
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="negative_prompt 为空串或纯空白；无负向约束时应省略该字段",
                )
        if self.media_type not in MEDIA_TYPES:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"spec-media-type-unsupported: media_type={self.media_type!r}",
            )
        if not _SIZE_RE.match(self.size):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"size 不符合 WxH（100–99999）: {self.size!r}",
            )
        if not _RATIO_RE.match(self.aspect_ratio):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"aspect_ratio 不符合 W:H: {self.aspect_ratio!r}",
            )
        if not aspect_matches(self.size, self.aspect_ratio):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"spec-size-aspect-inconsistent: size={self.size} 与 "
                    f"aspect_ratio={self.aspect_ratio} 相对误差超过 1%"
                ),
            )
        self.generation_parameters = normalize_parameters(self.generation_parameters)
        self.prompt_sha256 = hashlib.sha256(self.prompt_text.encode("utf-8")).hexdigest()
        payload = self.identity_payload()
        self.spec_sha256 = compute_spec_sha256(str(self.tenant_id), payload)
        expected_id = generation_spec_id_for(self.spec_sha256)
        if not self.generation_spec_id:
            self.generation_spec_id = expected_id
        if self.generation_spec_id != expected_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"generation_spec_id 与内容摘要不一致：声明 {self.generation_spec_id}，"
                    f"按内容应为 {expected_id}"
                ),
            )
        if not self.entity_id:
            self.entity_id = self.generation_spec_id
        if self.entity_id != self.generation_spec_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="GenerationSpec 的身份单值：entity_id 必须等于 generation_spec_id",
            )

    def identity_payload(self) -> dict[str, Any]:
        return identity_payload(
            project_id=self.project_id,
            shot_revision_id=self.shot_revision_id,
            shot_spec_id=self.shot_spec_id,
            production_route_ref=self.production_route_ref,
            constraint_snapshot_id=self.constraint_snapshot_id,
            template_version=self.template_version,
            derivation_version=self.derivation_version,
            size_policy_id=self.size_policy_id,
            size_policy_version=self.size_policy_version,
            model_id=self.model_id,
            provider=self.provider,
            generation_parameters=self.generation_parameters,
            prompt_text=self.prompt_text,
            negative_prompt=self.negative_prompt,
            media_type=self.media_type,
            size=self.size,
            aspect_ratio=self.aspect_ratio,
        )

    def to_dict(self) -> dict[str, Any]:
        """响应形状：与 ``object_schemas`` 的 GenerationSpec 定义严格一致。"""
        data = self._envelope_dict()
        payload = self.identity_payload()
        data.update(payload)
        data.update(
            {
                "generation_spec_id": self.generation_spec_id,
                "prompt_sha256": self.prompt_sha256,
                "spec_sha256": self.spec_sha256,
                "status": self.status.value,
            }
        )
        # ``identity_payload`` 里可能没有 negative_prompt 键；契约把它声明为可选
        # （string|null），缺省即返回 None，避免响应形状随内容变化。
        data.setdefault("negative_prompt", None)
        return data


def derive_spec(
    *,
    shot_spec: Any,
    tenant_id: Any,
    created_by: str,
    project_id: str,
    production_route_ref: str,
    constraint_snapshot_id: str,
    model_adapter_ref: str,
    provider: str,
    media_type: str,
    negative_prompt: str | None = None,
    generation_parameters: dict[str, Any] | None = None,
) -> GenerationSpec:
    """从 LOCKED ShotSpec 派生一份冻结规格。

    尺寸解析顺序（ADR-R47）：任务级约束（当前未承载，跳过）→ 约束快照（无对象，
    显式跳过）→ 生产路线显式媒体约束（现无字段，跳过）→ ``ShotSpec.aspect_ratio``
    + 指定版本 SizePolicy → 无来源即拒绝。这里**不落任何默认值**。
    """
    ratio = str(getattr(shot_spec, "aspect_ratio", "") or "").strip()
    if not _RATIO_RE.match(ratio):
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=f"spec-aspect-unresolved: ShotSpec.aspect_ratio={ratio!r} 不可解析",
        )
    size = resolve_size(ratio, policy_version=SIZE_POLICY_VERSION)
    prompt = render_prompt(shot_spec)
    prompt = normalize_text(prompt).strip()
    if not prompt:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR, detail="shot-spec-has-no-renderable-fields"
        )
    params = normalize_parameters(generation_parameters)
    # 信封校验要求 entity_id 非空，而 entity_id 又必须等于内容摘要派生的 id：
    # 先按同一套身份域规则算出摘要与 id，再构造对象；对象的 __post_init__ 会
    # 重新算一遍并核对，任何分歧都会在那里报错（不是信任这里的预计算）。
    payload = identity_payload(
        project_id=project_id,
        shot_revision_id=str(getattr(shot_spec, "shot_revision_id", "")),
        shot_spec_id=str(getattr(shot_spec, "entity_id", "")),
        production_route_ref=production_route_ref,
        constraint_snapshot_id=constraint_snapshot_id,
        template_version=TEMPLATE_VERSION,
        derivation_version=DERIVATION_VERSION,
        size_policy_id=SIZE_POLICY_ID,
        size_policy_version=SIZE_POLICY_VERSION,
        model_id=model_adapter_ref,
        provider=provider,
        generation_parameters=params,
        prompt_text=prompt,
        negative_prompt=negative_prompt,
        media_type=media_type,
        size=size,
        aspect_ratio=ratio,
    )
    spec_sha256 = compute_spec_sha256(str(tenant_id), payload)
    spec_id = generation_spec_id_for(spec_sha256)
    return GenerationSpec(
        entity_id=spec_id,
        created_by=created_by,
        tenant_id=tenant_id,
        generation_spec_id=spec_id,
        spec_sha256=spec_sha256,
        project_id=project_id,
        shot_revision_id=str(getattr(shot_spec, "shot_revision_id", "")),
        shot_spec_id=str(getattr(shot_spec, "entity_id", "")),
        production_route_ref=production_route_ref,
        constraint_snapshot_id=constraint_snapshot_id,
        model_id=model_adapter_ref,
        provider=provider,
        generation_parameters=params,
        prompt_text=prompt,
        negative_prompt=negative_prompt,
        media_type=media_type,
        size=size,
        aspect_ratio=ratio,
    )


__all__ = [
    "DERIVATION_VERSION",
    "MEDIA_TYPES",
    "PARAMETER_KEYS",
    "SPEC_SCHEMA_VERSION",
    "GenerationSpec",
    "ShotSpecFacts",
    "canonical_bytes",
    "compute_spec_sha256",
    "derive_spec",
    "generation_spec_id_for",
    "identity_payload",
    "normalize_parameters",
    "normalize_text",
]
