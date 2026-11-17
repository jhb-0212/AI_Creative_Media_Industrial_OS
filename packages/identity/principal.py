"""Principal 值对象（可信身份的唯一运行时出口）。

设计要点（P0-3 根治核心）：
- 旧实现里 requested_by / approved_by / created_by 是客户端请求体里的裸字符串，
  任何调用方都能填任意值冒充他人，身份来源不可信。
- 本类收敛「主体身份 + 租户 + 组织 + 项目范围」为不可变值对象，只能由 JWT
  验签成功后从 token claims 构造（见 jwt_core.decode），业务代码绝不从请求体
  字符串拼出 Principal。

字段语义：
- subject_id：主体唯一标识（sub claim，如用户 ID / Agent ID / 服务账号 ID）。
- subject_type：主体类型（sub_type claim）。**授权决策需要它**：Agent 不得通过
  CRITICAL 动作（红线「Agent 不得自批自用」），这不是靠调用方声明"我是人"，
  而是靠签发时写死的类型。
- tenant_id：租户标识（tenant_id claim，UUID），是 RLS 行级隔离键。
- organization_id：组织标识（org_id claim，UUID）。租户是"哪家客户"，组织是
  "客户内部的哪个制作主体"——同一租户下可以有多个组织，跨组织访问必须被拒
  （Action 的 scope 大量声明为 ``tenant+organization``）。
- project_ids：可访问的项目范围（project_ids claim）。
  语义**显式三态**，不允许"缺省即全部"：
  * 含 ``"*"``：**本组织内**全部项目（组织管理员/制片）；
  * 非空集合：仅这些项目；
  * 空集：一个项目也访问不了（fail-closed）。
  缺省即全部是历史上最常见的越权来源，因此这里刻意不给那个默认值。
  注意 ``*`` 的单位是组织而不是租户：组织是客户内部的责任边界，跨组织
  连"全部项目"也不放行（见 ADR-R17 与项目范围 RLS 策略）。
- roles：角色集合（roles claim，如 导演/编剧/制片），驱动 RBAC 判定。
- jti：令牌唯一标识（jti claim），用于防重放与审计关联。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from packages.identity.capability import Capability

#: project_ids 中表示"本组织内全部项目"的通配符。
#: 用 ``*`` 而不是空集/None：把"全部"写成一个具体的值，读代码的人一眼能看到
#: 权限范围有多宽，而不是靠"没有声明"去推断。
PROJECT_SCOPE_ALL = "*"


class SubjectType(StrEnum):
    """主体类型（决定能否通过 CRITICAL 授权）。"""

    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"


@dataclass(frozen=True)
class Principal:
    """认证成功后的可信主体上下文（不可变）。

    一经构造不可变，避免在请求处理过程中被篡改；tenant_id / organization_id
    用 UUID 类型保证格式合法（拒绝任意字符串注入 RLS 上下文）。
    """

    subject_id: str
    tenant_id: UUID
    organization_id: UUID
    subject_type: SubjectType = SubjectType.HUMAN
    roles: frozenset[str] = field(default_factory=frozenset)
    project_ids: frozenset[str] = field(default_factory=frozenset)
    jti: str | None = None
    #: 独立 Authorization Service 签发的短时能力凭证；None 表示尚未提供，
    #: 授权决策会按 fail-closed 拒绝。它不是从访问令牌 claims 里拼出来的。
    capability: Capability | None = None

    def __post_init__(self) -> None:
        if not self.subject_id.strip():
            raise ValueError("subject_id 不能为空")

    def has_role(self, role: str) -> bool:
        """是否持有指定角色（RBAC 判定入口，大小写敏感精确匹配）。"""
        return role in self.roles

    def has_any_role(self, *roles: str) -> bool:
        """是否持有任一角色。"""
        return any(self.has_role(role) for role in roles)

    @property
    def is_agent(self) -> bool:
        """是否 Agent 主体（CRITICAL 动作的职责分离判定依据）。"""
        return self.subject_type is SubjectType.AGENT

    @property
    def has_all_projects_scope(self) -> bool:
        """是否持有"本组织内全部项目"范围。"""
        return PROJECT_SCOPE_ALL in self.project_ids

    def can_access_project(self, project_id: str) -> bool:
        """是否可访问指定项目（空范围一律拒绝，不做"没有范围就放行"的推断）。"""
        if self.has_all_projects_scope:
            return True
        return project_id in self.project_ids

    def to_dict(self) -> dict[str, object]:
        """序列化为审计友好的结构（不泄漏敏感信息）。

        ``project_ids`` 原样输出：审计需要知道"当时这个人能看到哪些项目"，
        脱敏会让事后追责失去依据。
        """
        return {
            "subject_id": self.subject_id,
            "subject_type": self.subject_type.value,
            "tenant_id": str(self.tenant_id),
            "organization_id": str(self.organization_id),
            "roles": sorted(self.roles),
            "project_ids": sorted(self.project_ids),
            "jti": self.jti,
            "capability_jti": self.capability.jti if self.capability else None,
        }
