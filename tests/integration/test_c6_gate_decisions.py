"""C6 证据：Gate 0—4 各一次决策记录 + 交付清单不声明未发生的 Gate PASS。

验收口径（`Phase1-实现验收清单.md` C6）：
> Gate 0—4 各一次决策记录；每个 Gate 决策含 7.4 所列字段；
> DeliveryManifest 只引用已 VALIDATED 的对象，不声明未发生的 Gate PASS。

三件事分开证明，因为它们的失败模式不同：

1. **5 个 Gate 各一条决策记录**：走真实 `PRODUCTION.GATE.APPROVE` 入口（CRITICAL），
   逐个 gate_key 建记录。只测 Gate4（主链末段已有）会让 Gate 0—3 的字段缺失
   不被发现——它们的 `scope` 层级不同（项目/集/场景/镜头），字段齐备性要逐个验。
2. **7.4 字段齐备**：按 7.4 正文的字段清单逐项断言。7.4 列了 `exit_actions`
   （通过后解锁 DAG 节点、创建快照和通知），但 `object_schemas` 的 GateDecision
   **没有**该字段——这是刻意的：`exit_actions` 是**行为**不是**数据**，
   把它存进决策记录会让"当时批了什么"与"当时执行了什么"混为一谈。
   本测试显式断言"契约没有这个字段"，把口径差异钉死，而不是假装 15 项全绿。
3. **清单不声明 Gate PASS**：两条独立证据——(a) 契约字段集里没有任何 gate 类
   布尔字段（结构上做不到）；(b) 未批准的资产进不了清单（运行期拒绝）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from tests.conftest import apply_db_context, make_test_headers

pytestmark = pytest.mark.asyncio

_REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = _REPO_ROOT / "contracts" / "schemas" / "object_schemas.schema.json"
BASELINE = _REPO_ROOT / "docs" / "baseline" / "AI影视工业化生产平台V2.0.5.md"

#: 7.4「每个 Gate 必须包含」的字段，逐字取自正文（含 exit_actions）。
_FIELDS_74: tuple[str, ...] = (
    "gate_key",
    "gate_instance_id",
    "project_id",
    "scope",
    "entry_criteria",
    "required_artifacts",
    "ruleset_version",
    "constraint_snapshot_id",
    "reviewers",
    "decision",
    "reason_code",
    "comment",
    "evidence_refs",
    "decided_by",
    "decided_at",
    "exit_actions",
)

#: 5 个工业 Gate（7.5 主表）。scope 按各 Gate 的实际裁决层级取，
#: 不是为了凑字段而填同一个 {"level": "project"}。
_GATES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("GATE_0", {"level": "project", "project_id": "PLACEHOLDER"}),
    ("GATE_1", {"level": "episode", "episode_ref": "EP_01"}),
    ("GATE_2", {"level": "scene", "scene_ref": "SC_01"}),
    ("GATE_3", {"level": "shot", "shot_ref": "SH_010"}),
    ("GATE_4", {"level": "project", "project_id": "PLACEHOLDER"}),
)


def _client(subject: str, roles: list[str]) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=make_test_headers(subject_id=subject, roles=roles),
    )


def _gate_body(
    *, project_id: str, gate_key: str, scope: dict[str, Any], evidence: list[str]
) -> dict[str, Any]:
    """一条满足 7.4 的裁决请求（decided_by 由 Principal 提供，不在正文里）。"""
    resolved_scope = {
        key: (project_id if value == "PLACEHOLDER" else value)
        for key, value in scope.items()
    }
    return {
        "entity_id": f"gate_{gate_key.lower()}_{uuid.uuid4().hex[:8]}",
        "project_id": project_id,
        "gate_key": gate_key,
        "gate_instance_id": f"instance_{gate_key.lower()}_{uuid.uuid4().hex[:6]}",
        "decision": "APPROVED",
        "constraint_snapshot_id": f"snapshot_{uuid.uuid4().hex[:8]}",
        "ruleset_version": 1,
        "entry_criteria": "必要输入齐备且版本不可变",
        "required_artifacts": "脚本锁定稿、镜头清单、约束包",
        "reviewers": ["c6_director"],
        "reason_code": "ALL_CHECKS_PASSED",
        "comment": f"{gate_key} 检查项逐条通过",
        "evidence_refs": evidence,
        "scope": resolved_scope,
    }


async def test_gate_zero_to_four_each_have_one_decision_record(
    qc_sm: async_sessionmaker[AsyncSession],
) -> None:
    """Gate 0—4 各一条决策记录，且每条都含 7.4 字段。"""
    evidence_ref = [f"evidence_{uuid.uuid4().hex[:8]}" for _ in range(2)]

    async with _client("c6_director", ["导演"]) as director:
        # 裁决必须挂在真实项目上（项目级授权会真判一次归属）。
        resp = await director.post(
            "/projects", json={"project_name": "C6 Gate 记录项目", "project_tier": "A"}
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["entity_id"]

        recorded: list[dict[str, Any]] = []
        for gate_key, scope in _GATES:
            body = _gate_body(
                project_id=project_id, gate_key=gate_key, scope=scope, evidence=evidence_ref
            )
            resp = await director.post("/gate-decisions", json=body)
            assert resp.status_code == 201, f"{gate_key}: {resp.text}"
            recorded.append(resp.json())

    assert len(recorded) == 5, "Gate 0—4 必须各一条"

    problems: list[str] = []
    for gate_key, payload in zip((key for key, _ in _GATES), recorded, strict=True):
        # 1) 7.4 字段（exit_actions 单独处理，见下）
        for field in _FIELDS_74:
            if field == "exit_actions":
                continue
            if field not in payload:
                problems.append(f"{gate_key} 缺 7.4 字段 {field}")
                continue
            value = payload[field]
            if value in (None, "", [], {}):
                problems.append(f"{gate_key} 的 7.4 字段 {field} 为空")
        # 2) 逐条核对身份与结论来自服务端而非请求体
        if payload["decided_by"] != "c6_director":
            problems.append(f"{gate_key} 的 decided_by 不是可信主体: {payload['decided_by']}")
        if payload["gate_key"] != gate_key:
            problems.append(f"{gate_key} 的 gate_key 回读不一致: {payload['gate_key']}")
        if payload["project_id"] != project_id:
            problems.append(f"{gate_key} 的 project_id 回读不一致")
    assert not problems, "Gate 决策字段不达标：\n" + "\n".join(problems)

    # 3) 落库回读：不是"响应里好看"，而是数据库里真有 5 行。
    async with qc_sm() as session:
        await apply_db_context(session)
        count = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM qc_governance.gate_decisions "
                    "WHERE project_id = :pid AND decision = 'APPROVED'"
                ),
                {"pid": project_id},
            )
        ).scalar_one()
    assert count == 5, f"数据库中只有 {count} 条该项目裁决，期望 5"

    # 4) 审计：每条裁决都发一条 PRODUCTION_GATE_APPROVED（含主体与目标）。
    async with qc_sm() as session:
        await apply_db_context(session)
        rows = (
            await session.execute(
                sa.text(
                    "SELECT payload FROM outbox.outbox_events "
                    "WHERE event_type = 'PRODUCTION_GATE_APPROVED' "
                    "AND payload->>'project_id' = :pid"
                ),
                {"pid": project_id},
            )
        ).scalars().all()
    assert len(rows) == 5, f"Gate 事件数 {len(rows)} 与决策数不一致"
    for payload in rows:
        assert payload["decided_by"] == "c6_director"
        assert payload["gate_key"] in {key for key, _ in _GATES}
        assert payload["decision"] == "APPROVED"


async def test_field_list_matches_baseline_section_7_4() -> None:
    """`_FIELDS_74` 不能凭记忆写：逐项回正文核对。

    这份清单是"要求"而不是"实现"，所以不能像事件载荷那样从契约读取——
    但它同样不该是无人校验的字面量。这里从基线 7.4 小节切出正文，逐字段比对；
    基线改了字段而测试没跟，这条就红。
    """
    text = BASELINE.read_text(encoding="utf-8")
    start = text.index("### 7.4 Gate 通用契约")
    end = text.index("### 7.5 Gate 0—4")
    section = text[start:end]
    missing = [field for field in _FIELDS_74 if f"`{field}`" not in section]
    assert not missing, f"7.4 小节正文里找不到这些字段（清单与基线脱节）: {missing}"


async def test_exit_actions_is_behavior_not_stored_data() -> None:
    """7.4 的 `exit_actions` 不进决策记录：契约无该字段，口径差异显式钉死。

    如果将来契约加了 `exit_actions`，这条会红——那时候要重新判断
    "通过后解锁了什么"该由谁记录（当前由 DAG 编排与快照各自记录）。
    """
    schemas = json.loads(SCHEMAS.read_text(encoding="utf-8"))
    gate = schemas["definitions"]["GateDecision"]
    properties = set(gate["properties"])
    assert "exit_actions" not in properties, (
        "契约新增了 exit_actions：请更新 C6 口径（行为 vs 数据）并改这条断言"
    )
    # 其余 7.4 字段必须都在契约里——否则"字段齐备"只是响应体凑出来的。
    missing = [field for field in _FIELDS_74 if field != "exit_actions" and field not in properties]
    assert not missing, f"契约缺 7.4 字段: {missing}"


async def test_manifest_cannot_declare_a_gate_pass() -> None:
    """清单不声明 Gate PASS：字段集缺失 + 运行期拒绝未批准资产。"""
    schemas = json.loads(SCHEMAS.read_text(encoding="utf-8"))
    manifest_props = set(schemas["definitions"]["DeliveryManifest"]["properties"])
    gate_like = [name for name in manifest_props if "gate" in name.lower()]
    assert not gate_like, (
        f"DeliveryManifest 出现了 gate 类字段 {gate_like}：清单只能引用证据，"
        "不能声明结论"
    )

    async with (
        _client("c6_producer", ["制片"]) as producer,
        _client("c6_qc", ["QC Worker"]) as qc_worker,
        _client("c6_director", ["导演"]) as director,
        _client("c6_delivery", ["授权制片"]) as delivery,
    ):
        resp = await producer.post(
            "/projects", json={"project_name": "C6 清单项目", "project_tier": "B"}
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["entity_id"]

        asset_id = f"av_c6_{uuid.uuid4().hex[:8]}"
        resp = await qc_worker.post(
            "/assets",
            json={
                "project_id": project_id,
                "asset_type": "VIDEO",
                "sha256": "d" * 64,
                "storage_ref": {"uri": f"s3://ai-media/c6/{asset_id}.mp4"},
                "entity_id": asset_id,
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "GENERATED", "新建资产应为未审状态"

        manifest_body = {
            "project_id": project_id,
            "spec_ref": "spec_c6",
            "qc_final_ref": "qc_c6",
            "picture_lock_ref": "pl_c6",
            "asset_refs": [asset_id],
            "rights_evidence_refs": ["rights_c6"],
            "rebuild_recipe": "recipe://c6/v1",
        }
        # 未审资产进清单 → 拒绝（这是"只引用已成立证据"的运行期表达）。
        resp = await delivery.post("/delivery-manifests", json=manifest_body)
        assert resp.status_code == 422, resp.text
        assert "APPROVED" in resp.text

        # 过 QC + 人工批准后同一请求通过，且清单里没有任何 gate 类字段。
        resp = await qc_worker.post(
            f"/assets/{asset_id}/qc-reports",
            json={
                "quality_profile_version": "qp_v1",
                "detector_versions": {"motion_continuity": "1.2.0"},
                "findings": [{"detector": "motion_continuity", "verdict": "PASS", "score": 0.9}],
            },
        )
        assert resp.status_code == 201, resp.text
        qc_report_id = resp.json()["qc_report"]["qc_report_id"]

        resp = await director.post(f"/assets/{asset_id}/approve")
        assert resp.status_code == 200, resp.text

        manifest_body["qc_final_ref"] = qc_report_id
        resp = await delivery.post("/delivery-manifests", json=manifest_body)
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["status"] == "DRAFT"
        assert not [key for key in created if "gate" in key.lower()]
