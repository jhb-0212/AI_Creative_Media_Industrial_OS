"""集成测试的 project_planning 播种helpers（A13 五张新表）。

为什么单独一份：A13 之后，任何要验证场次/镜头/修订的集成测试都得先建
「项目 → 集数 → 场次 → 镜头」这条链。每个测试文件各抄一份 INSERT，就会在
schema 演进时各自陈旧——而陈旧的播种代码表现为「测试莫名其妙变红」，
修法是改测试，而不是发现问题。

播种一律走 ``admin_sessionmaker``（超级用户、不经 RLS）：被测的是**读取与写入
是否被策略约束**，不是「能不能播种」。业务角色对本域表有写权限，但用它播种会把
「策略拒绝」与「播种失败」混成同一个错误，负向用例就分不清是谁拒绝的。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text


def now() -> datetime:
    return datetime.now(UTC)


async def seed_organization(admin_sessionmaker, *, org_id, tenant) -> None:
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, :name, :now, 'tester', :now)
                """
            ),
            {"org": org_id, "tid": tenant, "name": "集成测试组织", "now": now()},
        )
        await session.commit()


async def seed_project(admin_sessionmaker, *, project_id, tenant, org_id=None) -> None:
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO project_planning.projects
                    (entity_id, entity_type, schema_version, entity_version,
                     status, owner_module, tenant_id, created_at, created_by,
                     updated_at, project_name, project_tier, organization_id)
                VALUES
                    (:eid, 'Project', 205, 1, 'DRAFT', 'project_planning',
                     :tid, :now, 'tester', :now, :name, 'B', :org)
                """
            ),
            {
                "eid": project_id,
                "tid": tenant,
                "now": now(),
                "name": "集成测试项目",
                "org": org_id,
            },
        )
        await session.commit()


async def seed_episode(admin_sessionmaker, *, episode_id, project_id, tenant) -> None:
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO project_planning.episodes
                    (entity_id, entity_type, schema_version, entity_version,
                     status, owner_module, tenant_id, created_at, created_by,
                     updated_at, project_id, episode_ref, title)
                VALUES
                    (:eid, 'Episode', 205, 1, 'DRAFT', 'project_planning',
                     :tid, :now, 'tester', :now, :pid, 'EP01', '集成测试集')
                """
            ),
            {"eid": episode_id, "tid": tenant, "now": now(), "pid": project_id},
        )
        await session.commit()


SCENE_INSERT = """
    INSERT INTO project_planning.scenes
        (entity_id, entity_type, schema_version, entity_version, status,
         owner_module, tenant_id, created_at, created_by, updated_at,
         scene_id, scene_code, project_id, episode_id)
    VALUES
        (:eid, 'Scene', 205, 1, 'DRAFT', 'project_planning',
         :tid, :now, 'tester', :now, :eid, :code, :pid, :epid)
"""


async def seed_scene(
    admin_sessionmaker, *, scene_id, project_id, episode_id, tenant, code="SC01"
) -> None:
    async with admin_sessionmaker() as session:
        await session.execute(
            text(SCENE_INSERT),
            {
                "eid": scene_id,
                "tid": tenant,
                "now": now(),
                "code": code,
                "pid": project_id,
                "epid": episode_id,
            },
        )
        await session.commit()


SHOT_INSERT = """
    INSERT INTO project_planning.shots
        (entity_id, entity_type, schema_version, entity_version, status,
         owner_module, tenant_id, created_at, created_by, updated_at,
         shot_id, shot_code, scene_id, project_id, shot_revision_id)
    VALUES
        (:eid, 'Shot', 205, 1, 'NOT_STARTED', 'project_planning',
         :tid, :now, 'tester', :now, :eid, :code, :sid, :pid, :rev)
"""


async def seed_shot(
    admin_sessionmaker,
    *,
    shot_id,
    scene_id,
    project_id,
    tenant,
    code="SH01",
    revision_ref="rev_pending",
) -> None:
    async with admin_sessionmaker() as session:
        await session.execute(
            text(SHOT_INSERT),
            {
                "eid": shot_id,
                "tid": tenant,
                "now": now(),
                "code": code,
                "sid": scene_id,
                "pid": project_id,
                "rev": revision_ref,
            },
        )
        await session.commit()


SHOT_REVISION_INSERT = """
    INSERT INTO project_planning.shot_revisions
        (entity_id, entity_type, schema_version, entity_version, status,
         owner_module, tenant_id, created_at, created_by, updated_at,
         shot_revision_id, shot_id, project_id, episode_id, scene_id,
         revision_no, description, narrative_intent, character_refs,
         location_ref, prop_refs, constraint_pack_applicability,
         creative_autonomy, risk, quality_profile, intent_ref,
         basis_script_revision_id, script_anchors, source_candidate_refs,
         change_origin, changed_fields, provenance, confirmed_by, confirmed_at)
    VALUES
        (:eid, 'ShotRevision', 205, 1, :status, 'project_planning',
         :tid, :now, 'tester', :now,
         :eid, :shot, :pid, :epid, :sid,
         :revno, '镜头描述', '叙事意图', '[]'::jsonb,
         'loc_1', '[]'::jsonb, 'NONE',
         'SEMI_AUTO', '{}'::jsonb, 'QP_STANDARD', '{}'::jsonb,
         :script_rev, '[]'::jsonb, '[]'::jsonb,
         'INITIAL', '[]'::jsonb, '{}'::jsonb, 'tester', :now)
"""


def shot_revision_params(
    *,
    entity_id: str,
    shot_id: str,
    project_id: str,
    episode_id: str,
    scene_id: str,
    tenant,
    revision_no: int,
    status: str = "ACTIVE",
    script_revision_ref: str = "sr_1",
) -> dict[str, object]:
    return {
        "eid": entity_id,
        "status": status,
        "tid": tenant,
        "now": now(),
        "shot": shot_id,
        "pid": project_id,
        "epid": episode_id,
        "sid": scene_id,
        "revno": revision_no,
        "script_rev": script_revision_ref,
    }
