"""生成「分叉清单冻结」文档。

设计约束：
1. 所有 SHA256 由程序从 git 对象直接计算，禁止手工转录（长哈希手抄必错）；
2. 唯一口径 = ``git show <ref>:<path>`` 的原始字节（LF），同时给 git blob SHA1 作二级锚点；
3. 冲突类型是人工判定，但判定结果与文件哈希一并落盘，保证「结论可追溯到输入」。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

MAIN = "main"
FEAT = "origin/feat/identity-data-ownership"
VERSIONS = "data/migrations/versions"

# (revision, main 文件名, feat 文件名, 类型, 差异摘要)
MIGRATION_FORKS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "b3c4d5e6f7a8",
        "b3c4d5e6f7a8_asset_project_ownership.py",
        "b3c4d5e6f7a8_asset_status_transitions.py",
        "T1",
        "文件名与数据库效果均不同：主支建资产→项目复合外键；分支建状态转移表并改写资产状态守卫",
    ),
    (
        "9e3b4c5d6e7f",
        "9e3b4c5d6e7f_create_app_role_and_grants.py",
        "9e3b4c5d6e7f_create_app_role_and_grants.py",
        "T3",
        "角色名来源 + 属性收敛：主支 `ALTER ROLE ... NOBYPASSRLS NOCREATEDB NOCREATEROLE` 重置存量属性并拒绝开发默认口令；分支仅 `ALTER ROLE ... PASSWORD`",
    ),
    (
        "a1b2c3d4e5f6",
        "a1b2c3d4e5f6_fix_outbox_backlog_exclude_dead.py",
        "a1b2c3d4e5f6_fix_outbox_backlog_exclude_dead.py",
        "T2",
        "仅角色名来源：`app_role_name()` → 本地常量 `APP_ROLE`",
    ),
    (
        "b5c6d7e8f9a0",
        "b5c6d7e8f9a0_revoke_maintenance_runs_from_app_role.py",
        "b5c6d7e8f9a0_revoke_maintenance_runs_from_app_role.py",
        "T2",
        "仅角色名来源",
    ),
    (
        "b7c8d9e0f1a2",
        "b7c8d9e0f1a2_partition_protection_and_grant_baseline.py",
        "b7c8d9e0f1a2_partition_protection_and_grant_baseline.py",
        "T4",
        "角色名来源 + 调用点绑定 helper 接口：分支对 `protect/unprotect_partition_statements` 必须显式传 `roles=(APP_ROLE,)`",
    ),
    (
        "c1d2e3f4a5b6",
        "c1d2e3f4a5b6_create_asset_service_tables.py",
        "c1d2e3f4a5b6_create_asset_service_tables.py",
        "T2",
        "仅角色名来源",
    ),
    (
        "c6d7e8f9a0b1",
        "c6d7e8f9a0b1_create_auth_revoked_tokens.py",
        "c6d7e8f9a0b1_create_auth_revoked_tokens.py",
        "T2",
        "仅角色名来源",
    ),
    (
        "d4e5f6a7b8c9",
        "d4e5f6a7b8c9_restrict_outbox_grants.py",
        "d4e5f6a7b8c9_restrict_outbox_grants.py",
        "T2",
        "仅角色名来源",
    ),
    (
        "e5f6a7b8c9d0",
        "e5f6a7b8c9d0_add_outbox_tenant_enumeration_function.py",
        "e5f6a7b8c9d0_add_outbox_tenant_enumeration_function.py",
        "T2",
        "仅角色名来源",
    ),
    (
        "f6a7b8c9d1e2",
        "f6a7b8c9d1e2_partition_outbox_events.py",
        "f6a7b8c9d1e2_partition_outbox_events.py",
        "T2",
        "仅角色名来源",
    ),
)

# (路径, 接口/符号, 类型, 差异摘要)
INTERFACE_FORKS: tuple[tuple[str, str, str, str], ...] = (
    (
        "packages/shared/partition_guard.py",
        "protect_partition_statements / unprotect_partition_statements",
        "T5-接口",
        "主支 `role: str | None = None`（单角色、可省略）；分支 `roles: Sequence[str]`（角色集合、必填）。参数名、类型、必填性三者同时不同",
    ),
    (
        "packages/shared/db_roles.py",
        "app_role_name / role_for / all_business_roles",
        "T5-接口",
        "符号集不相交：主支导出 `app_role_name()`、`relay_role_name()`、`domain_role*`；分支导出 `role_for()`、`all_business_roles()`",
    ),
    (
        "packages/shared/database.py",
        "build_database_url / build_migration_url / DomainPools",
        "T5-接口",
        "`build_migration_url` 两侧都存在（主支第 89 行、分支第 73 行），**不是分支独有**；真正的差异是分支新增 `DomainPools`/`_DomainPool` 与按域取 URL 的入参（`domain: DatabaseDomain`）",
    ),
    (
        "packages/shared/config.py",
        "Settings 域连接池字段",
        "T5-配置",
        "分支按八个 DatabaseDomain 组织连接配置；主支按四个应用角色组织",
    ),
    (
        "infra/compose/docker-compose.yml",
        "x-db-env 锚点 / 容器 environment",
        "T5-配置",
        "分支第 78、111 行（api、relay）以 `<<: *db-env` 继承含 `POSTGRES_USER/POSTGRES_PASSWORD` 的超管凭据；主支已拆为 superuser/business/relay 三个锚点",
    ),
)


def show(ref: str, path: str) -> bytes:
    return subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, check=True).stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_sha1(ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{ref}:{path}"], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], capture_output=True, text=True, check=True
    ).stdout.strip()


def migration_rows() -> str:
    lines = [
        "| # | 路径（两侧） | Revision | main SHA256 | feature SHA256 | 冲突类型 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for index, (rev, main_file, feat_file, kind, _note) in enumerate(MIGRATION_FORKS, 1):
        main_path = f"{VERSIONS}/{main_file}"
        feat_path = f"{VERSIONS}/{feat_file}"
        same = main_file == feat_file
        path_cell = (
            f"`{main_path}`<br>（两侧同名）"
            if same
            else f"main: `{main_file}`<br>feat: `{feat_file}`"
        )
        lines.append(
            f"| {index} | {path_cell} | `{rev}` "
            f"| `{sha256(show(MAIN, main_path))}` "
            f"| `{sha256(show(FEAT, feat_path))}` "
            f"| **{kind}** |"
        )
    return "\n".join(lines)


def migration_notes() -> str:
    lines = [
        "| Revision | 类型 | 差异摘要 | main blob SHA1 | feature blob SHA1 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for rev, main_file, feat_file, kind, note in MIGRATION_FORKS:
        main_path = f"{VERSIONS}/{main_file}"
        feat_path = f"{VERSIONS}/{feat_file}"
        lines.append(
            f"| `{rev}` | {kind} | {note} "
            f"| `{blob_sha1(MAIN, main_path)}` | `{blob_sha1(FEAT, feat_path)}` |"
        )
    return "\n".join(lines)


def interface_rows() -> str:
    lines = [
        "| # | 路径 | 接口 / 符号 | main SHA256 | feature SHA256 | 冲突类型 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for index, (path, symbol, kind, _note) in enumerate(INTERFACE_FORKS, 1):
        lines.append(
            f"| {index} | `{path}` | `{symbol}` "
            f"| `{sha256(show(MAIN, path))}` "
            f"| `{sha256(show(FEAT, path))}` "
            f"| **{kind}** |"
        )
    return "\n".join(lines)


def interface_notes() -> str:
    lines = ["| 路径 | 类型 | 差异摘要 |", "| --- | --- | --- |"]
    for path, _symbol, kind, note in INTERFACE_FORKS:
        lines.append(f"| `{path}` | {kind} | {note} |")
    return "\n".join(lines)


def main() -> None:
    counts = {}
    for _rev, _m, _f, kind, _n in MIGRATION_FORKS:
        counts[kind] = counts.get(kind, 0) + 1
    summary = " + ".join(f"{kind} {counts[kind]}" for kind in sorted(counts))

    template = Path("docs/audits/fork-inventory.template.md").read_text(encoding="utf-8")
    filled = (
        template.replace("{{MAIN_COMMIT}}", commit(MAIN))
        .replace("{{FEAT_COMMIT}}", commit(FEAT))
        .replace("{{MIGRATION_ROWS}}", migration_rows())
        .replace("{{MIGRATION_NOTES}}", migration_notes())
        .replace("{{INTERFACE_ROWS}}", interface_rows())
        .replace("{{INTERFACE_NOTES}}", interface_notes())
        .replace("{{COUNT_SUMMARY}}", summary)
        .replace("{{TOTAL}}", str(len(MIGRATION_FORKS)))
    )
    out = Path("docs/audits/2026-11-20-fork-inventory-frozen.md")
    out.write_text(filled, encoding="utf-8")
    print(f"written {out} lines={len(filled.splitlines())} bytes={len(filled.encode())}")
    print("counts:", summary, "total:", len(MIGRATION_FORKS))
    assert "{{" not in filled, "模板占位符未全部替换"


if __name__ == "__main__":
    main()
