"""项目范围的授权谓词（B3 归属链的应用层出口）。

为什么单独一个模块：
- 「能不能对某个项目做某件事」是身份域的判断，不是业务领域的规则；放进任何
  一个业务模块都会让别的模块反向依赖它。
- 数据库层的 RLS 已经兜住了**读**与**越界写**（见迁移
  ``e7f8a9b0c1d2_add_project_scope_rls``），本模块只负责一件事：在**写入
  入口**给出可解释的 403，而不是把数据库的 42501 直接暴露成 500。
  两者不是重复：数据库是「不能绕过」，这里是「说得清为什么」。

与 B2 的关系：
- B2 会把「Principal + Action + Resource → ALLOW/DENY」收敛为统一决策入口，
  届时该入口复用这里的谓词作为 scope 维度，不再另写一份范围判定。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal


def assert_project_scope(principal: Principal, project_id: str) -> None:
    """要求调用方对指定项目有授权（越界一律 403）。

    用于**请求体显式携带 project_id** 的写入入口：调用方自己点了项目名，
    拒绝时不需要掩饰项目是否存在，403 比 404 更准确（也不泄漏存在性，
    因为 id 是调用方提供的）。
    """
    if not principal.can_access_project(project_id):
        raise DomainError(
            ErrorRegistry.AUTH_SCOPE_DENIED,
            detail=f"无权在该项目下写入资源: {project_id}",
        )


def assert_tenant_wide_scope(principal: Principal, action_key: str) -> None:
    """要求调用方持有本组织内全部项目范围（新建**归属根**时使用）。

    为什么新建项目不能由项目级用户完成：
    - 新项目的 id 不属于任何既有范围，若放行，调用方写进去之后自己也读不回来
      （自锁）；
    - 若为此放宽数据库的 WITH CHECK，就等于允许「往看不见的项目里写数据」，
      那正是本次要关闭的口子。
    因此新建归属根要求 ``*``（组织管理员/制片口径），由身份服务按成员关系签发。
    """
    if not principal.has_all_projects_scope:
        raise DomainError(
            ErrorRegistry.AUTH_SCOPE_DENIED,
            detail=f"执行 {action_key} 需要本组织内全部项目范围",
        )


__all__ = ["assert_project_scope", "assert_tenant_wide_scope"]
