"""运维 CLI：吊销一枚访问令牌（jti denylist，P3-2）。

为什么不做 HTTP logout：action_registry 里没有登记 logout 动作，凭空加端点
等于猜契约（用户红线）。吊销是低频、高危、需审计归属的运维动作，用迁移角色
执行的 CLI 更合适：应用进程（app_role）对 auth.revoked_tokens 只有 SELECT，
无法自颁自撤。

用法（仓库根，.venv）：

    python scripts/revoke_token.py --jti <jti> --tenant <uuid> \\
        --subject <subject_id> --expires 2026-11-14T12:00:00+08:00 \\
        --reason "设备丢失" --revoked-by ops

    python scripts/revoke_token.py --prune-expired

jti / expires 从令牌 payload 取（jwt 解码即可，不需要私钥）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.shared.database import build_migration_url

_INSERT = text(
    """
    INSERT INTO auth.revoked_tokens
        (jti, tenant_id, subject_id, expires_at, reason, revoked_by)
    VALUES (:jti, :tenant_id, :subject_id, :expires_at, :reason, :revoked_by)
    """
)

_PRUNE = text("DELETE FROM auth.revoked_tokens WHERE expires_at <= now()")


def _parse_expires(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("--expires 必须带时区（例如 2026-11-14T12:00:00+08:00）")
    return parsed.astimezone(UTC)


async def _revoke(args: argparse.Namespace) -> None:
    engine = create_async_engine(build_migration_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT,
                {
                    "jti": args.jti,
                    "tenant_id": UUID(args.tenant),
                    "subject_id": args.subject,
                    "expires_at": _parse_expires(args.expires),
                    "reason": args.reason,
                    "revoked_by": args.revoked_by,
                },
            )
    finally:
        await engine.dispose()
    print(f"[OK] 已吊销 jti={args.jti}（subject={args.subject}）")


async def _prune() -> None:
    engine = create_async_engine(build_migration_url())
    try:
        async with engine.begin() as conn:
            result = await conn.execute(_PRUNE)
    finally:
        await engine.dispose()
    print(f"[OK] 已清理过期吊销记录 {result.rowcount} 条")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="吊销 JWT（jti denylist）")
    parser.add_argument("--jti")
    parser.add_argument("--tenant", help="租户 UUID（审计归属）")
    parser.add_argument("--subject", help="令牌主体 id（审计归属）")
    parser.add_argument("--expires", help="令牌原始过期时间（ISO8601，带时区）")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--revoked-by", default="ops")
    parser.add_argument("--prune-expired", action="store_true")
    args = parser.parse_args(argv)

    if args.prune_expired:
        asyncio.run(_prune())
        return 0

    missing = [
        name
        for name, value in (
            ("--jti", args.jti),
            ("--tenant", args.tenant),
            ("--subject", args.subject),
            ("--expires", args.expires),
        )
        if not value
    ]
    if missing:
        parser.error(f"吊销令牌缺少参数: {', '.join(missing)}")

    try:
        asyncio.run(_revoke(args))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
