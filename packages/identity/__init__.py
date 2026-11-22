"""平台级身份基础设施（packages/identity）。

本包是身份治理单元（P0-3）的底层内核，承载「可信身份来源」这一单一事实源。
依赖方向：本包只依赖标准库 + PyJWT + cryptography，**不依赖任何业务模块**，
供 services/identity_access（领域组装层）与各 API 路由消费。

分层（单向依赖，禁止反向引用）：
- principal.py   —— Principal 值对象（subject_id / tenant_id / roles / jti）
- keys.py        —— RSA 密钥对管理（私钥签发用、公钥验签用，私钥不落日志）
- jwt_core.py    —— RS256 JWT 签发/验签（issuer/audience 校验、短期过期、唯一 jti）
- jwks.py        —— JWKS 文档导出 + 按 kid 缓存（供网关/路由层验签取公钥）

安全红线（对齐 6「密钥不落日志」）：私钥是 SecretStr/字节流，任何 repr、
日志、序列化都不得泄漏；验签方只接触公钥。
"""
