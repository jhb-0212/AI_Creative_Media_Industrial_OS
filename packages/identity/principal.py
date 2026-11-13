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

    def to_dict(self) -> dict[str, object]:
        """序列化为审计友好的结构（不泄漏敏感信息）。"""
        return {
            "subject_id": self.subject_id,
            "tenant_id": str(self.tenant_id),
            "roles": sorted(self.roles),
            "jti": self.jti,
        }
