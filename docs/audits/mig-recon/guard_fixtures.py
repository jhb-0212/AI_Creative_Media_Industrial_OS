"""13 张不可变守卫表的**最小合法行**夹具（声明式）。

为什么要汇总到一处
------------------
逐表覆盖验证的第一个障碍不是守卫，而是「这张表怎么造出一行合法数据」：
每张表都有 10 列信封 + 若干业务必填列，还各有 CHECK 枚举。若在验证脚本里
写 13 个 `xxx_insert()` 函数，验证脚本会长成 13 份手抄的 DDL 副本，
表结构一变就静默漂移——本项目已有过「夹具漏写必填列，全部撞 23502，
断言失去意义」的教训。

所以这里只做一件事：**每张表一份声明式模板**（列 → SQL 字面量），
由 `insert_sql()` 统一拼装；脚本侧不需要知道任何一张表的列名。

口径
----
* 值全部来自模板主体（`Template`，占位符 `${key}` / `${project}` / `${tenant}` / `${org}`），
  不做运行期字符串改写。
* `key` 必须能同时满足该表的唯一键：多数表主键是 `entity_id`；`qc_reports` 是 `qc_report_id`。
* 状态值取该表 CHECK 允许的最小合法值；**刻意不选需要额外举证的状态**
  （如 `production_routes.VERIFIED` 要求 capability_refs 非空）。
* 不在本文件里判断「守卫该不该拦」——那是验证脚本的事。
"""

from __future__ import annotations

from string import Template

TENANT = "00000000-0000-0000-0000-000000000001"
ORG = "00000000-0000-0000-0000-000000000011"
ORG2 = "00000000-0000-0000-0000-000000000012"
PROJECT_OLD = "proj-1"
PROJECT_NEW = "proj-2"

OWNERSHIP_COLS = ("project_id", "organization_id", "tenant_id")

# 需要额外存在的前置行（避免表和表之间的外键把夹具卡住）
ROUTE_ID = "rt-coverage-base"


def _env(entity_type: str, status: str, module: str, **extra: str) -> dict[str, str]:
    """10 列信封 + 表特有业务列。信封列顺序固定，读写方便对照 schema。"""
    return {
        "entity_id": "'${key}'",
        "entity_type": f"'{entity_type}'",
        "schema_version": "2",
        "entity_version": "1",
        "status": f"'{status}'",
        "owner_module": f"'{module}'",
        "tenant_id": "'${tenant}'",
        "organization_id": "'${org}'",
        "created_at": "now()",
        "created_by": "'migx'",
        "updated_at": "now()",
        **extra,
    }


_JSON_EMPTY = "'[]'::jsonb"
_JSON_OBJ = "'{}'::jsonb"


# ---------------------------------------------------------------------------
# 逐表模板（顺序与 b0c9 的 GUARD_ORDER 一致）
# ---------------------------------------------------------------------------
FIXTURES: dict[str, dict[str, str]] = {
    "asset_service.asset_versions": _env(
        "AssetVersion",
        "GENERATED",
        "asset_service",
        project_id="'${project}'",
        asset_type="'VIDEO'",
        sha256="repeat('a',64)",
        storage_ref='\'{"bucket":"b","key":"${key}"}\'::jsonb',
        source_asset_version_ids=_JSON_EMPTY,
    ),
    "asset_service.delivery_manifests": _env(
        "DeliveryManifest",
        "DRAFT",
        "asset_service",
        project_id="'${project}'",
        asset_refs=_JSON_EMPTY,
        checksum="'ck-${key}'",
        picture_lock_ref="'pl-1'",
        qc_final_ref="'qr-final-1'",
        rebuild_recipe="'recipe'",
        rights_evidence_refs=_JSON_EMPTY,
        spec_ref="'spec-1'",
    ),
    # qc_reports 没有信封：主键是 qc_report_id，且无 entity_id（T3 已证实）
    "asset_service.qc_reports": {
        "qc_report_id": "'${key}'",
        "asset_version_id": "'av-ref-${key}'",
        "tenant_id": "'${tenant}'",
        "organization_id": "'${org}'",
        "project_id": "'${project}'",
        "quality_profile_version": "'qp-1'",
        "detector_versions": _JSON_OBJ,
        "findings": _JSON_EMPTY,
        "created_by": "'migx'",
        "created_at": "now()",
        "updated_at": "now()",
    },
    "generation_execution.generation_tasks": _env(
        "GenerationTask",
        "PENDING",
        "generation_execution",
        project_id="'${project}'",
        shot_id="'shot-${key}'",
        route_id=f"'{ROUTE_ID}'",
        shot_spec_ref="'ss-1'",
        visual_plan_ref="'vp-1'",
        constraint_snapshot_ref="'cs-1'",
        budget_reservation_ref="'br-1'",
        quota_reservation_ref="'qr-1'",
        dependency_refs=_JSON_EMPTY,
        attempt="1",
    ),
    # 唯一一张**没有 project_id** 的表：归属两列，走 organizations 两列外键
    "generation_execution.production_routes": _env(
        "ProductionRoute",
        "UNVERIFIED",
        "generation_execution",
        route_type="'STANDARD'",
        supported_control_signals=_JSON_EMPTY,
        required_appearance_refs=_JSON_EMPTY,
        qc_dimensions=_JSON_EMPTY,
        capability_refs=_JSON_EMPTY,
    ),
    "generation_execution.shot_route_selections": _env(
        "ShotRouteSelection",
        "ACTIVE",
        "generation_execution",
        project_id="'${project}'",
        shot_id="'shot-${key}'",
        route_id=f"'{ROUTE_ID}'",
    ),
    "qc_governance.gate_decisions": _env(
        "GateDecision",
        "DECIDED",
        "qc_governance",
        project_id="'${project}'",
        gate_key="'G1'",
        gate_instance_id="'gi-${key}'",
        decision="'REJECTED'",
        constraint_snapshot_id="'cs-${key}'",
        ruleset_version="1",
        entry_criteria="'ec'",
        required_artifacts="'ra'",
        reviewers=_JSON_EMPTY,
        reason_code="'RC'",
        comment="'coverage fixture'",
        evidence_refs=_JSON_EMPTY,
        scope=_JSON_OBJ,
        decided_by="'migx'",
        decided_by_subject_type="'SERVICE'",
        decided_at="now()",
    ),
    "qc_governance.qc_review_sessions": _env(
        "QCReviewSession",
        "OPEN",
        "qc_governance",
        project_id="'${project}'",
        asset_version_id="'av-ref-${key}'",
        qc_report_ref="'qr-${key}'",
        reference_set=_JSON_OBJ,
        reviewers='\'[{"reviewer":"migx"}]\'::jsonb',
        findings='\'[{"finding":"coverage"}]\'::jsonb',
        decisions=_JSON_EMPTY,
    ),
    "shot_planning.constraint_packs": _env(
        "ConstraintPack",
        "ACTIVE",
        "shot_planning",
        project_id="'${project}'",
        shot_ids=_JSON_EMPTY,
        shot_group_ids=_JSON_EMPTY,
        applicability="'APPLICABLE'",
        locked_dimensions=_JSON_EMPTY,
        constraints=_JSON_OBJ,
        version="1",
    ),
    "shot_planning.shot_specs": _env(
        "ShotSpec",
        "ACTIVE",
        "shot_planning",
        shot_revision_id="'sr-${key}'",
        shot_id="'shot-${key}'",
        project_id="'${project}'",
        duration_ms="4200",
        aspect_ratio="'16:9'",
        fps="'30/1'",
        coordinate_system="'frame-origin-left-top'",
        camera_path=_JSON_EMPTY,
        subject_paths=_JSON_EMPTY,
        shot_size_changes=_JSON_EMPTY,
        cut_points_frames=_JSON_EMPTY,
        control_signal_refs=_JSON_EMPTY,
        locked_dimensions=_JSON_EMPTY,
    ),
    "shot_planning.shot_visual_plans": _env(
        "ShotVisualPlan",
        "DRAFT",
        "shot_planning",
        shot_id="'shot-${key}'",
        project_id="'${project}'",
        intent_summary="'coverage'",
        camera_language="'STATIC'",
        shot_size="'MS'",
        lens_range="'35mm'",
        motion_intent="'none'",
        lighting_intent="'soft'",
        locked_fields=_JSON_EMPTY,
        open_fields=_JSON_EMPTY,
        revision="1",
    ),
    "workflow.approval_flows": _env(
        "ApprovalFlow",
        "ACTIVE",
        "workflow",
        action_key="'SHOT.REVISION.CONFIRM'",
        resource_type="'ShotRevision'",
        resource_id="'sr-${key}'",
        project_id="'${project}'",
        mode="'ALL'",
        groups=_JSON_EMPTY,
        ruleset_version="1",
    ),
    "workflow.approval_steps": _env(
        "ApprovalStep",
        "DECIDED",
        "workflow",
        ticket_id="'tk-${key}'",
        group_name="'g1'",
        voter_subject_id="'u-${key}'",
        voter_subject_type="'user'",
        decision="'APPROVE'",
        reason="'coverage'",
        decided_at="now()",
        project_id="'${project}'",
    ),
}

# 行标识列：判定函数用它去映射表里找 (表, 行标识, 工单)
KEY_COL: dict[str, str] = {
    fqn: ("qc_report_id" if fqn.endswith("qc_reports") else "entity_id") for fqn in FIXTURES
}


def insert_sql(
    fqn: str,
    key: str,
    *,
    project: str = PROJECT_OLD,
    tenant: str = TENANT,
    org: str = ORG,
    overrides: dict[str, str] | None = None,
) -> str:
    """拼出一条最小合法 INSERT。列顺序即模板顺序，值来自声明式模板。

    ``overrides`` 用于**跨表真实引用**（例如 `qc_reports.asset_version_id` 上有既存外键，
    必须指向一条真实的 asset_versions 行）。把这类依赖写成显式参数，而不是在模板里
    塞一个「看起来像 ID」的假值——假值会让整条用例在 23503 上失败，并被误读成守卫行为。
    """
    cols = dict(FIXTURES[fqn])
    if overrides:
        for col, val in overrides.items():
            if col not in cols:
                raise KeyError(f"{fqn} 没有列 {col}，override 无意义")
            cols[col] = f"'{val}'"
    values = [
        Template(v).substitute(key=key, project=project, tenant=tenant, org=org)
        for v in cols.values()
    ]
    return f"INSERT INTO {fqn} ({', '.join(cols)}) VALUES ({', '.join(values)})"


def has_project(fqn: str) -> bool:
    return "project_id" in FIXTURES[fqn]


def ownership_set(fqn: str, *, project: str = PROJECT_NEW, org: str = ORG2) -> str:
    """归属更新片段：有 project_id 的表改 project_id，没有的改 organization_id。

    `production_routes` 的归属只有 (organization_id, tenant_id) 两列——
    若也写 `SET project_id=...` 会直接报「列不存在」，把「覆盖不到」伪装成「通过」。
    """
    if has_project(fqn):
        return f"project_id = '{project}'"
    return f"organization_id = '{org}'"


def ownership_values(fqn: str, *, project: str = PROJECT_NEW, org: str = ORG2) -> dict[str, str]:
    """映射行里对应的 new_* 值（与 ownership_set 必须同源，否则例外永远匹配不上）。"""
    if has_project(fqn):
        return {"new_project_id": project, "new_organization_id": ORG, "new_tenant_id": TENANT}
    return {"new_project_id": None, "new_organization_id": org, "new_tenant_id": TENANT}


def old_values(fqn: str) -> dict[str, str | None]:
    if has_project(fqn):
        return {
            "old_project_id": PROJECT_OLD,
            "old_organization_id": ORG,
            "old_tenant_id": TENANT,
        }
    return {"old_project_id": None, "old_organization_id": ORG, "old_tenant_id": TENANT}


def nonexistent_target(fqn: str, missing_org: str, *, project: str = "proj-404") -> str:
    """一个**确定不存在**的归属目标，用于证明例外放行后外键仍然生效。

    有 project_id 的表返回不存在的项目号；`production_routes` 返回不存在的组织号。
    这两条路径的被引用键不同（三列 vs 两列），所以不能共用同一个常量。
    """
    return project if has_project(fqn) else missing_org
