"""测试包入口：在任何应用模块被导入之前固定"密钥边界"。

为什么这段代码必须放在 ``__init__.py`` 而不是 ``conftest.py``：

- 包初始化一定早于包内任何模块（含 conftest）的导入，因此这里设置的进程环境
  变量、生成的测试密钥，对**所有** import 顺序都生效。
- 如果放在 conftest 里，就得保证"设置环境变量"的语句排在
  ``from apps.api.main import app`` 之前——一旦有人调整 import 顺序（或工具自动
  排序），设置会晚于应用导入，测试会用上"没有验签公钥"的配置而全量失败，
  且失败原因看起来与 B4 无关。

这里做两件事（对应包 B4 的形态要求）：

1. **API 侧只给公钥**：``IDENTITY_PUBLIC_KEY_PEM`` 写入环境变量，私钥绝不写入。
   ``IDENTITY_PROCESS_ROLE=api`` 时若配置里有私钥，配置校验会直接拒绝启动——
   测试环境也遵守这条，否则测试证明的部署形态与生产不一致。
2. **私钥留在测试进程内存里**：``TEST_SIGNING_KEY`` 由
   ``issue_token(signing_key=...)`` 显式使用。签发能力来自"显式传给签发函数"，
   而不是"进程配置里有"。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

from packages.identity.action_registry import all_action_specs
from packages.identity.capability import Capability
from packages.identity.keys import generate_signing_key

#: 测试签发密钥：只在测试进程内存在，不进入 Settings / 环境变量。
TEST_SIGNING_KEY = generate_signing_key(kid="current")

os.environ["IDENTITY_PUBLIC_KEY_PEM"] = TEST_SIGNING_KEY.public_pem.decode("utf-8")
os.environ.setdefault("IDENTITY_PROCESS_ROLE", "api")
# 防御：外部 shell 若残留私钥变量，会让 API 进程的拒绝启动逻辑误伤测试。
os.environ.pop("IDENTITY_PRIVATE_KEY_PEM", None)


def make_test_capability(
    subject_id: str,
    tenant_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
    *,
    actions: list[str] | None = None,
    projects: list[str] | None = None,
) -> Capability:
    """构造测试用 capability 值对象（不签 JWT，供服务层单测直接注入）。

    默认允许全部契约动作，但项目范围必须显式跟随 principal 的范围语义；
    需要验证越权时用 ``actions`` / ``projects`` 显式收窄。
    """
    now = datetime.now(UTC)
    return Capability(
        jti=f"cap-{uuid.uuid4().hex}",
        subject_id=subject_id,
        tenant_id=str(tenant_id),
        organization_id=str(organization_id or tenant_id),
        issuer="test-authz-service",
        project_scope=frozenset(projects if projects is not None else ["*"]),
        episode_scope=frozenset(),
        shot_scope=frozenset(),
        asset_scope=frozenset(),
        allowed_actions=frozenset(
            actions
            if actions is not None
            else [spec.action_key for spec in all_action_specs()]
        ),
        denied_actions=frozenset(),
        budget_scope=frozenset(),
        max_amount=None,
        currency=None,
        resource_quota=(),
        delegation_chain=(),
        approval_ref=None,
        revocation_ref=None,
        policy_version="V205-R14",
        nonce=uuid.uuid4().hex,
        task_id="test-task",
        workflow_run_id="test-run",
        issued_at=now,
        not_before=now,
        expires_at=now + timedelta(minutes=5),
    )
