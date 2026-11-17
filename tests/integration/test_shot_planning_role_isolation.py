"""shot_planning 域数据库角色隔离与 RLS 的集成测试。

要证明的不是"代码没这么写"，而是"这样写会被数据库拒绝"（ADR-R27 边界）：
1. shot_planning 域角色写不了 project_planning 表（跨域写入数据库层被拒）；
2. project_planning 域角色写不了 shot_planning 表（反向隔离同样成立）；
3. shot_planning 域角色写不了 shot_revisions 的 UPDATE（append-only 内容列，
   修订即新行，不授 UPDATE）；
4. shot_planning 域角色能写 shot_drafts（AI 产草案的正向路径）。

断言直接查系统目录 / 实际执行 SQL，而不是跑业务代码。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import set_raw_rls_context

pytestmark = pytest.mark.asyncio


def _denied(exc_info: pytest.ExceptionInfo[DBAPIError]) -> bool:
    message = str(exc_info.value).lower()
    return "permission denied" in message or "insufficient privilege" in message


async def test_shot_role_cannot_write_project_tables(
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """镜头规划域角色 INSERT 项目表 → permission denied。"""
    with pytest.raises(DBAPIError) as exc_info:
        async with shot_sm() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO project_planning.projects
                        (entity_id, entity_type, schema_version, entity_version, status,
                         owner_module, tenant_id, organization_id, created_at,
                         created_by, updated_at, project_name, project_tier)
                    VALUES
                        ('x', 'Project', 205, 1, 'DRAFT', 'shot_planning', :t,
                         :t, now(), 'probe', now(), '越权项目', 'B')
                    """
                ),
                {"t": uuid.uuid4()},
            )
    assert _denied(exc_info)


async def test_project_role_cannot_write_shot_tables(
    project_sm: async_sessionmaker[AsyncSession],
) -> None:
    """项目规划域角色 INSERT 镜头草案表 → permission denied。"""
    with pytest.raises(DBAPIError) as exc_info:
        async with project_sm() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO shot_planning.shot_drafts
                        (entity_id, entity_type, schema_version, entity_version, status,
                         owner_module, tenant_id, organization_id, created_at,
                         created_by, updated_at, project_id, episode_ref, scene_ref,
                         shot_code, intent_summary, shot_description, source)
                    VALUES
                        ('x', 'ShotDraft', 205, 1, 'DRAFT', 'project_planning', :t,
                         :t, now(), 'probe', now(), 'p', 'e', 's', 'c', 'i', 'd',
                         'agent')
                    """
                ),
                {"t": uuid.uuid4()},
            )
    assert _denied(exc_info)


async def test_shot_role_cannot_update_shot_revisions(
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """shot_revisions 是 append-only：镜头域角色也无 UPDATE 授权。"""
    with pytest.raises(DBAPIError) as exc_info:
        async with shot_sm() as session:
            await session.execute(
                text(
                    "UPDATE shot_planning.shot_revisions "
                    "SET shot_description = '篡改' WHERE false"
                )
            )
    assert _denied(exc_info)


async def test_shot_role_can_insert_shot_draft(
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """镜头规划域角色能写 shot_drafts（AI 产草案的正向路径）。"""
    tenant = uuid.uuid4()
    entity_id = f"shot_draft_probe_{uuid.uuid4().hex[:8]}"
    async with shot_sm() as session:
        await set_raw_rls_context(session, tenant_id=str(tenant))
        await session.execute(
            text(
                """
                INSERT INTO shot_planning.shot_drafts
                    (entity_id, entity_type, schema_version, entity_version, status,
                     owner_module, tenant_id, organization_id, created_at,
                     created_by, updated_at, project_id, episode_ref, scene_ref,
                     shot_code, intent_summary, shot_description, source)
                VALUES
                    (:e, 'ShotDraft', 205, 1, 'DRAFT', 'shot_planning', :t,
                     :t, now(), 'probe', now(), 'proj', 'ep', 'sc', 'sh',
                     '意图', '描述', 'agent')
                """
            ),
            {"e": entity_id, "t": tenant},
        )
        await session.commit()
    # 清理探针行（迁移角色直连删除，应用角色无 DELETE 权）。
    from sqlalchemy.ext.asyncio import create_async_engine

    from packages.shared.database import build_migration_url

    engine = create_async_engine(build_migration_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM shot_planning.shot_drafts "
                    "WHERE entity_id = :e"
                ),
                {"e": entity_id},
            )
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# ADR-R27 阶段 1C：状态机/不可变内容列守卫在数据库层成立（ADR-R20 范式）
# --------------------------------------------------------------------------- #


def _insert_pack_sql(entity_id: str) -> str:
    """约束包 INSERT 语句（DRAFT 态，NOT_APPLICABLE 干净包；entity_id 由调用方保证唯一）。"""
    return f"""
        INSERT INTO shot_planning.constraint_packs
            (entity_id, entity_type, schema_version, entity_version, status,
             owner_module, tenant_id, organization_id, created_at, created_by,
             updated_at, project_id, shot_ids, shot_group_ids, applicability,
             locked_dimensions, constraints, version)
        VALUES
            ('{entity_id}', 'ConstraintPack', 205, 1, 'DRAFT', 'shot_planning',
             :t, :t, now(), 'probe', now(), 'p', '["shot_1"]'::jsonb,
             '[]'::jsonb, 'NOT_APPLICABLE', '[]'::jsonb, '[]'::jsonb, 1)
    """


async def test_constraint_pack_illegal_transition_rejected_by_db(
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """DRAFT→REVOKED 未登记：直连 SQL 绕过服务层也会被守卫拒绝（D1）。"""
    t = uuid.uuid4()
    pack_id = f"pack_{uuid.uuid4().hex[:12]}"
    async with shot_sm() as session:
        await set_raw_rls_context(session, tenant_id=str(t), projects="*")
        await session.execute(text(_insert_pack_sql(pack_id)), {"t": t})
        await session.commit()

    with pytest.raises(DBAPIError) as exc_info:
        async with shot_sm() as session:
            await set_raw_rls_context(session, tenant_id=str(t), projects="*")
            await session.execute(
                text(
                    "UPDATE shot_planning.constraint_packs "
                    f"SET status = 'REVOKED' WHERE entity_id = '{pack_id}'"
                )
            )
    assert "非法状态转移" in str(exc_info.value)


async def test_constraint_pack_legal_transition_accepted_by_db(
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """已登记边（DRAFT→ACTIVE）直连 SQL 也可推进（守卫不挡合法路径）。"""
    t = uuid.uuid4()
    pack_id = f"pack_{uuid.uuid4().hex[:12]}"
    async with shot_sm() as session:
        await set_raw_rls_context(session, tenant_id=str(t), projects="*")
        await session.execute(text(_insert_pack_sql(pack_id)), {"t": t})
        await session.commit()

    async with shot_sm() as session:
        await set_raw_rls_context(session, tenant_id=str(t), projects="*")
        await session.execute(
            text(
                "UPDATE shot_planning.constraint_packs "
                f"SET status = 'ACTIVE', approved_by = 'director_001', "
                f"approved_at = now() WHERE entity_id = '{pack_id}'"
            )
        )
        await session.commit()


async def test_shot_spec_content_column_immutable_in_db(
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """C2：规格内容列（duration_ms）直连 SQL 改写被守卫拒绝。"""
    t = uuid.uuid4()
    spec_id = f"spec_{uuid.uuid4().hex[:12]}"
    async with shot_sm() as session:
        await set_raw_rls_context(session, tenant_id=str(t), projects="*")
        await session.execute(
            text(
                f"""
                INSERT INTO shot_planning.shot_specs
                    (entity_id, entity_type, schema_version, entity_version, status,
                     owner_module, tenant_id, organization_id, created_at, created_by,
                     updated_at, shot_revision_id, shot_id, project_id, duration_ms,
                     aspect_ratio, fps, coordinate_system, camera_path, subject_paths,
                     shot_size_changes, cut_points_frames, control_signal_refs,
                     locked_dimensions)
                VALUES
                    ('{spec_id}', 'ShotSpec', 205, 1, 'ACTIVE', 'shot_planning',
                     :t, :t, now(), 'probe', now(), 'rev_1', 'shot_1', 'p', 4200,
                     '16:9', '24/1', 'RIGHT_HANDED/Y/METER', '{{}}'::jsonb,
                     '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                """
            ),
            {"t": t},
        )
        await session.commit()

    with pytest.raises(DBAPIError) as exc_info:
        async with shot_sm() as session:
            await set_raw_rls_context(session, tenant_id=str(t), projects="*")
            await session.execute(
                text(
                    "UPDATE shot_planning.shot_specs SET duration_ms = 9999 "
                    f"WHERE entity_id = '{spec_id}'"
                )
            )
    assert "不可变内容列" in str(exc_info.value)


async def test_shot_planning_new_tables_no_delete_grant(
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """1C 三张表不授 DELETE：业务对象不可物理删除（授权层 + 守卫双兜底）。"""
    async with shot_sm() as session:
        await set_raw_rls_context(session, tenant_id=str(uuid.uuid4()), projects="*")
        for table in ("shot_specs", "shot_visual_plans", "constraint_packs"):
            result = await session.execute(
                text(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'shot_planning' AND table_name = :table "
                    "AND grantee = current_user"
                ),
                {"table": table},
            )
            privileges = {row[0] for row in result}
            assert "DELETE" not in privileges
            assert {"SELECT", "INSERT", "UPDATE"} <= privileges
