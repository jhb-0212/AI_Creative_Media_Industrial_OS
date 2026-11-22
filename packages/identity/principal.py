"""Principal 值对象（可信身份的唯一运行时出口）。

设计要点（P0-3 根治核心）：
- 旧实现里 requested_by / approved_by / created_by 是客户端请求体里的裸字符串，
  任何调用方都能填任意值冒充他人，身份来源不可信。
- 本类收敛「主体身份 + 租户上下文」为不可变值对象，只能由 JWT 验签成功后
  从 token claims 构造（见 jwt_core.decode），业务代码绝不从请求体字符串
  拼出 Principal。

字段语义：
- subject_id：主体唯一标识（sub claim，如用户 ID / Agent ID / 服务账号 ID）。
- tenant_id：租户标识（tenant_id claim，UUID 字符串），是 RLS 行级隔离键。
- roles：角色集合（roles claim，如 导演/编剧/制片），驱动 RBAC 授权判定。
- capabilities：能力范围集合（caps claim），驱动 Capability 授权判定。
  与 roles 解决的问题不同（见 F02）：roles 回答「这个主体属于什么角色」，
  capabilities 回答「这个主体被授予了哪些具体资源/动作的范围」。
- jti：令牌唯一标识（jti claim），用于防重放与审计关联。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class Principal:
    """认证成功后的可信主体上下文（不可变）。

    一经构造不可变，避免在请求处理过程中被篡改；tenant_id 用 UUID 类型
    保证格式合法（拒绝任意字符串注入 RLS 上下文）。
    """

    subject_id: str
    tenant_id: UUID
    roles: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[str] = field(default_factory=frozenset)
    # 组织归属（JWT ``org`` claim，可选）：基线 27.3「组织/团队/项目/集/镜头
    # 多层级作用域」的资源级 ABAC 判定键。None = 无组织归属（个人租户或
    # 未声明组织的服务主体）；组织项目的访问必须与此匹配（fail-closed）。
    organization_id: UUID | None = None
    jti: str | None = None

    def __post_init__(self) -> None:
        if not self.subject_id.strip():
            raise ValueError("subject_id 不能为空")

    def has_role(self, role: str) -> bool:
        """是否持有指定角色（RBAC 判定入口，大小写敏感精确匹配）。"""
        return role in self.roles

    def has_any_role(self, *roles: str) -> bool:
        """是否持有任一角色。"""
        return any(self.has_role(role) for role in roles)

    def has_capability(self, capability: str) -> bool:
        """是否持有指定能力范围（Capability 判定入口，精确匹配）。"""
        return capability in self.capabilities

    def to_dict(self) -> dict[str, object]:
        """序列化为审计友好的结构（不泄漏敏感信息）。"""
        return {
            "subject_id": self.subject_id,
            "tenant_id": str(self.tenant_id),
            "organization_id": (
                str(self.organization_id) if self.organization_id else None
            ),
            "roles": sorted(self.roles),
            "capabilities": sorted(self.capabilities),
            "jti": self.jti,
        }
