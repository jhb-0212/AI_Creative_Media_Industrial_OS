"""资产登记请求的规范化指纹（PR-F / R42 T2 幂等冲突）。

为什么需要它
------------
T2 的幂等键是 ``(tenant_id, generation_task_id)``。但「键相同」不等于
「请求语义相同」：同一任务若收到不同 SHA / 项目 / locator 的登记请求，
只是早返回 ACK 会把内容冲突洗成正常重复（根因报告 G-1）。指纹把「本次
请求的完整语义」压成一个确定性哈希，与回执一起持久化，使重放时能判断
「是不是同一个请求」，而不只是「有没有来过」。

字段来源纪律
------------
- 全部字段取自**核验后的事实**，不是裸事件载荷：tenant/project 已与来源
  任务比对，locator/sha256 已与 Manifest 比对，quality_profile_version
  取自冻结的 GenerationTask（不信事件自报，D7）。
- 字段顺序固定、JSON 分隔符确定（``separators`` + ``sort_keys``），保证
  同一语义在任何进程/Python 版本得到相同指纹。字典键顺序变化不改变结果。
- sha256 归一化小写，避免大小写造成「语义相同、指纹不同」的假冲突。

指纹不参与鉴权、不驱动状态机——它只是「这次请求是不是原来那次」的证据。
"""

from __future__ import annotations

import hashlib
import json
from typing import TypedDict
from uuid import UUID


class RegistrationFingerprintInput(TypedDict):
    """指纹输入字段集合：类型定义是唯一事实源，字段清单从它派生。"""

    tenant_id: UUID
    generation_task_id: str
    project_id: str
    attempt_id: str
    locator: str
    sha256: str
    media_type: str
    quality_profile_version: str


# 指纹覆盖的字段集合从 TypedDict 派生：新增维度只改一处，字段清单与
# canonical 输入不可能各说各话（清单漏项会让冲突维度被静默洗成幂等）。
FINGERPRINT_FIELDS: tuple[str, ...] = tuple(RegistrationFingerprintInput.__annotations__)


def compute_registration_fingerprint(
    *,
    tenant_id: UUID,
    generation_task_id: str,
    project_id: str,
    attempt_id: str,
    locator: str,
    sha256: str,
    media_type: str,
    quality_profile_version: str,
) -> str:
    """返回 64 位小写十六进制指纹。输入都用关键字参数，避免 positional 错位。"""
    canonical = {
        "tenant_id": str(tenant_id),
        "generation_task_id": str(generation_task_id),
        "project_id": str(project_id),
        "attempt_id": str(attempt_id),
        "locator": str(locator),
        "sha256": str(sha256).strip().lower(),
        "media_type": str(media_type),
        "quality_profile_version": str(quality_profile_version),
    }
    # 断言覆盖集合与声明一致：新增字段忘了加进 canonical 会在这里暴露，
    # 而不是静默产出「缺一个维度」的指纹（那种指纹会让不同请求碰撞成相同）。
    assert tuple(canonical) == FINGERPRINT_FIELDS
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "FINGERPRINT_FIELDS",
    "RegistrationFingerprintInput",
    "compute_registration_fingerprint",
]
