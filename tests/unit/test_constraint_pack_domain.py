"""ConstraintPack 领域不变量（7.9 / A23）。

本文件盯的是契约里那些「必须 / 不得」在**对象构造那一刻**是否成立，而不是
服务层有没有记得调用校验：

- CP-1 适用性自述一致（NOT_APPLICABLE 与「列了维度/条目」不能同时成立）；
- CP-2 锁定维度必须落在 7.9 列举的七个值内（这是 ConstraintPack 与 ShotSpec
  的关键差别：8.12 没给全集，所以那边不编词表；7.9 给了全集，不校验就等于把
  契约明文当成建议）；
- CP-3 条目八字段必填且 id 包内唯一；
- CP-4 作用镜头非空、去重；
- CP-6 版本是正整数且必须是 int（``>= 1`` 放行 float 与 bool）。

状态迁移的所有权判定也在这里：``DRAFT→ACTIVE`` 只归 ``CONSTRAINT.PACK.LOCK``，
三条治理边只归 ``CONSTRAINT.PACK.STATUS.CHANGE``。若两者可以互走，那么
「锁一份新包」与「撤销一份已生效的包」就成了同一种通行证。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from services.project_planning.domain.constraint_pack import (
    LOCKED_DIMENSIONS,
    ConstraintPack,
    normalize_dimensions,
)
from services.project_planning.domain.enums import ConstraintPackStatus
from services.project_planning.domain.state_machine import (
    CONSTRAINT_PACK_LOCK_ACTION,
    CONSTRAINT_PACK_STATUS_CHANGE_ACTION,
)

_TENANT = uuid4()


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "constraint_id": "c1",
        "target": "character.costume",
        "shot_scope": "S01-S05",
        "rule": "红色外套不得更换",
        "source": "revision:director-note-3",
        "authority_level": "DIRECTOR",
        "valid_at": "2026-11-21T00:00:00Z",
        "approval_ref": "approval://d-17",
    }
    base.update(overrides)
    return base


def _pack(**overrides: Any) -> ConstraintPack:
    params: dict[str, Any] = {
        "entity_id": "pack-1",
        "created_by": "agent-1",
        "tenant_id": _TENANT,
        "project_id": "proj-1",
        "shot_ids": ["shot-1"],
        "applicability": "APPLICABLE",
        "locked_dimensions": ["cut_points"],
    }
    params.update(overrides)
    return ConstraintPack(**params)


# --------------------------------------------------------------------------- #
# 基本构造
# --------------------------------------------------------------------------- #
def test_minimal_pack_is_draft() -> None:
    pack = _pack()

    assert pack.status is ConstraintPackStatus.DRAFT
    assert pack.constraint_pack_id == "pack-1"
    assert pack.version == 1
    assert pack.approved_by == ""
    assert pack.approved_at is None


def test_pack_id_defaults_to_entity_id() -> None:
    """未给包标识时取 entity_id：首个版本两者自然一致，调用方不必关心区别。"""
    assert _pack().constraint_pack_id == "pack-1"
    assert _pack(constraint_pack_id="   ").constraint_pack_id == "pack-1"


def test_pack_id_may_differ_from_entity_id_within_one_logical_pack() -> None:
    """第二个版本：新行（新 entity_id）+ 同一逻辑包标识 + version 递增。

    这两者一旦被钉成同一个值，CP-6（同一逻辑包至多一个 ACTIVE）与
    uq_constraint_pack_version 就只能在「同一行与自己冲突」这个不可能事件上生效，
    等于把两条不变量登记成 ENFORCED 却什么也没挡住。
    """
    pack = _pack(constraint_pack_id="cp-1", version=2)

    assert pack.constraint_pack_id == "cp-1"
    assert pack.entity_id == "pack-1"
    assert pack.version == 2


def test_blank_project_is_rejected() -> None:
    with pytest.raises(DomainError) as exc:
        _pack(project_id="   ")

    assert "project_id 不能为空" in exc.value.detail


# --------------------------------------------------------------------------- #
# CP-4 作用镜头
# --------------------------------------------------------------------------- #
def test_shot_ids_are_deduplicated_keeping_order() -> None:
    pack = _pack(shot_ids=["shot-2", "shot-1", "shot-2"])

    assert pack.shot_ids == ["shot-2", "shot-1"]


def test_empty_shot_ids_is_rejected() -> None:
    """约束包必须指名作用对象：空集合让「这些镜头被约束」无处落地。"""
    with pytest.raises(DomainError) as exc:
        _pack(shot_ids=[])

    assert "不能为空" in exc.value.detail


@pytest.mark.parametrize("bad", ["", "   "])
def test_blank_shot_id_is_rejected(bad: str) -> None:
    with pytest.raises(DomainError) as exc:
        _pack(shot_ids=[bad])

    assert "不能为空字符串" in exc.value.detail


def test_non_string_shot_id_is_rejected() -> None:
    with pytest.raises(DomainError) as exc:
        _pack(shot_ids=[123])

    assert "必须是字符串" in exc.value.detail


def test_shot_group_ids_may_be_empty() -> None:
    """``shot_group_ids`` 是可选字段（7.9 的 required 不含它）。

    若照搬 ``shot_ids`` 的非空要求，「本包不绑定连续镜头组」这种合法输入会被拒，
    而错误信息还会说「不能为空」——指向一个契约里并不存在的必填要求。
    """
    pack = _pack(shot_group_ids=[])

    assert pack.shot_group_ids == []


def test_shot_group_ids_are_deduplicated() -> None:
    pack = _pack(shot_group_ids=["g1", "g1", "g2"])

    assert pack.shot_group_ids == ["g1", "g2"]


# --------------------------------------------------------------------------- #
# CP-1 适用性
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["", "APPLICABLE_MAYBE", "not_applicable"])
def test_unknown_applicability_is_rejected(value: str) -> None:
    """7.9：「无锁定约束的镜头必须显式标 NOT_APPLICABLE，不得留空」。

    留空或被写成别的形态时不得默认成 APPLICABLE——「没说」与「说了适用」是
    两种判断，用默认值合并会让默认值冒充授权结论。
    """
    with pytest.raises(DomainError) as exc:
        _pack(applicability=value)

    assert "applicability 必须是" in exc.value.detail


def test_not_applicable_with_no_content_is_allowed() -> None:
    pack = _pack(applicability="NOT_APPLICABLE", locked_dimensions=[], constraints=[])

    assert pack.applicability == "NOT_APPLICABLE"
    assert pack.locked_dimensions == []


def test_not_applicable_with_dimensions_is_rejected() -> None:
    with pytest.raises(DomainError) as exc:
        _pack(applicability="NOT_APPLICABLE", locked_dimensions=["blocking"])

    assert "不得携带" in exc.value.detail


def test_not_applicable_with_constraints_is_rejected() -> None:
    with pytest.raises(DomainError) as exc:
        _pack(
            applicability="NOT_APPLICABLE",
            locked_dimensions=[],
            constraints=[_entry()],
        )

    assert "不得携带" in exc.value.detail


# --------------------------------------------------------------------------- #
# CP-2 锁定维度
# --------------------------------------------------------------------------- #
def test_all_contract_dimensions_are_accepted() -> None:
    pack = _pack(locked_dimensions=sorted(LOCKED_DIMENSIONS))

    assert set(pack.locked_dimensions) == LOCKED_DIMENSIONS


def test_unknown_dimension_is_rejected() -> None:
    """契约用「：」列举了七个值，那就是枚举；拼错的名字会静默失去锁定语义。"""
    with pytest.raises(DomainError) as exc:
        _pack(locked_dimensions=["camera_instrinsics"])

    assert "契约未声明的维度" in exc.value.detail


def test_dimensions_are_normalized_to_declaration_order() -> None:
    assert normalize_dimensions(["cut_points", "blocking", "cut_points"]) == [
        "blocking",
        "cut_points",
    ]


# --------------------------------------------------------------------------- #
# CP-3 约束条目
# --------------------------------------------------------------------------- #
def test_complete_entry_is_accepted() -> None:
    pack = _pack(constraints=[_entry()])

    assert pack.constraints[0]["constraint_id"] == "c1"


@pytest.mark.parametrize(
    "missing",
    [
        "constraint_id",
        "target",
        "shot_scope",
        "rule",
        "source",
        "authority_level",
        "valid_at",
        "approval_ref",
    ],
)
def test_entry_missing_any_required_field_is_rejected(missing: str) -> None:
    """七条必填字段各自单独缺一次：只测一个字段会放过其余七个的漏检。"""
    entry = _entry()
    entry.pop(missing)

    with pytest.raises(DomainError) as exc:
        _pack(constraints=[entry])

    assert missing in str(exc.value.detail)


def test_entry_with_blank_value_is_rejected() -> None:
    with pytest.raises(DomainError) as exc:
        _pack(constraints=[_entry(rule="   ")])

    assert "缺必填字段或取值为空" in exc.value.detail


def test_entry_with_json_null_value_is_rejected() -> None:
    """``None`` 必须与库层的 JSON null 同口径。

    ``str(None)`` 是 ``"None"``（非空），不显式判 None 会让领域放行、库里拒绝，
    出现「领域通过、写库报 23514」这种最难查的分歧。
    """
    with pytest.raises(DomainError) as exc:
        _pack(constraints=[_entry(approval_ref=None)])

    assert "取值为空" in exc.value.detail


def test_entry_extra_fields_are_preserved() -> None:
    """只断言「必含」，不断言「仅含」：契约没有禁止附加字段。"""
    pack = _pack(constraints=[_entry(note="附加说明")])

    assert pack.constraints[0]["note"] == "附加说明"


def test_duplicate_constraint_id_is_rejected() -> None:
    with pytest.raises(DomainError) as exc:
        _pack(constraints=[_entry(), _entry(rule="另一条规则")])

    assert "在包内重复" in exc.value.detail


def test_non_object_entry_is_rejected() -> None:
    with pytest.raises(DomainError) as exc:
        _pack(constraints=["not-an-object"])  # type: ignore[list-item]

    assert "必须是对象" in exc.value.detail


# --------------------------------------------------------------------------- #
# CP-6 版本
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_version_is_rejected(value: int) -> None:
    with pytest.raises(DomainError) as exc:
        _pack(version=value)

    assert "必须为正整数" in exc.value.detail


@pytest.mark.parametrize("value", [1.5, True])
def test_non_integer_version_is_rejected(value: Any) -> None:
    """``>= 1`` 不表达类型：``1.5`` 与 ``True`` 都能通过。

    前者写进 Integer 列会被静默截断（同一份包自报两个版本号），后者变成 1。
    """
    with pytest.raises(DomainError) as exc:
        _pack(version=value)

    assert "必须是整数" in exc.value.detail


def test_non_datetime_approved_at_is_rejected() -> None:
    with pytest.raises(DomainError) as exc:
        _pack(approved_at="2026-11-21")

    assert "approved_at 必须是 datetime" in exc.value.detail


# --------------------------------------------------------------------------- #
# 状态迁移：所有权 + 生效证据
# --------------------------------------------------------------------------- #
def _activate(pack: ConstraintPack, *, action: str = CONSTRAINT_PACK_LOCK_ACTION) -> None:
    pack.transition_to(ConstraintPackStatus.ACTIVE, "audit://1", action)


def test_lock_requires_approver() -> None:
    pack = _pack()

    with pytest.raises(DomainError) as exc:
        _activate(pack)

    assert "approved_by" in exc.value.detail


def test_lock_requires_approved_at() -> None:
    pack = _pack(approved_by="director")

    with pytest.raises(DomainError) as exc:
        _activate(pack)

    assert "approved_at" in exc.value.detail


def test_applicable_pack_must_lock_something() -> None:
    """「适用但没有锁任何东西」无法作为生成输入：既拦不住修改，也解释不了为什么建它。"""
    pack = _pack(
        approved_by="director",
        approved_at=datetime(2026, 11, 21, tzinfo=UTC),
        locked_dimensions=[],
        constraints=[],
    )

    with pytest.raises(DomainError) as exc:
        _activate(pack)

    assert "至少要有 1 个锁定维度或1 条约束" in exc.value.detail


def test_lock_with_dimension_succeeds_and_advances_version() -> None:
    pack = _pack(approved_by="director", approved_at=datetime(2026, 11, 21, tzinfo=UTC))

    _activate(pack)

    assert pack.status is ConstraintPackStatus.ACTIVE
    assert pack.entity_version == 2
    assert pack.audit_ref == "audit://1"


def test_not_applicable_pack_can_be_activated() -> None:
    """7.9：NOT_APPLICABLE 的包同样必须经 ACTIVE，以证明「无锁定约束」是授权人判断。"""
    pack = _pack(
        applicability="NOT_APPLICABLE",
        locked_dimensions=[],
        approved_by="director",
        approved_at=datetime(2026, 11, 21, tzinfo=UTC),
    )

    _activate(pack)

    assert pack.status is ConstraintPackStatus.ACTIVE


def test_lock_edge_is_owned_by_lock_action_only() -> None:
    """治理动作用来走 ON 边的入口不能顺带走 LOCK 边。"""
    pack = _pack(approved_by="director", approved_at=datetime(2026, 11, 21, tzinfo=UTC))

    with pytest.raises(DomainError) as exc:
        _activate(pack, action=CONSTRAINT_PACK_STATUS_CHANGE_ACTION)

    assert "独占" in exc.value.detail


def test_governance_edges_are_owned_by_status_change_only() -> None:
    """LOCK 动作不得撤销一份已生效的包——这是两条语义完全不同的边。"""
    pack = _pack(
        approved_by="director",
        approved_at=datetime(2026, 11, 21, tzinfo=UTC),
    )
    _activate(pack)

    with pytest.raises(DomainError) as exc:
        pack.transition_to(ConstraintPackStatus.REVOKED, "audit://2", CONSTRAINT_PACK_LOCK_ACTION)

    assert "独占" in exc.value.detail

    pack.transition_to(
        ConstraintPackStatus.REVOKED, "audit://2", CONSTRAINT_PACK_STATUS_CHANGE_ACTION
    )
    assert pack.status is ConstraintPackStatus.REVOKED


def test_governance_states_have_no_outgoing_edges() -> None:
    """STALE / SUPERSEDED / REVOKED 都是治理结论，不得原地恢复。"""
    for terminal in (
        ConstraintPackStatus.STALE,
        ConstraintPackStatus.SUPERSEDED,
        ConstraintPackStatus.REVOKED,
    ):
        pack = _pack(
            approved_by="director",
            approved_at=datetime(2026, 11, 21, tzinfo=UTC),
        )
        _activate(pack)
        pack.transition_to(terminal, "audit://2", CONSTRAINT_PACK_STATUS_CHANGE_ACTION)

        with pytest.raises(DomainError) as exc:
            pack.transition_to(
                ConstraintPackStatus.ACTIVE,
                "audit://3",
                CONSTRAINT_PACK_STATUS_CHANGE_ACTION,
            )

        assert "未登记" in exc.value.detail


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def test_to_dict_matches_contract_fields() -> None:
    pack = _pack(approved_by="director", approved_at=datetime(2026, 11, 21, tzinfo=UTC))

    data = pack.to_dict()

    # project_id 要输出：7.9 的字段契约里它是必填字段（包自带作用域），
    # 与 ShotSpec 恰好相反（那边它是内部归属键）。
    assert data["project_id"] == "proj-1"
    assert data["approved_at"] == "2026-11-21T00:00:00+00:00"
    assert data["status"] == "DRAFT"
    assert data["locked_dimensions"] == ["cut_points"]
    # 契约 required 的字段一个都不能少。
    for field in (
        "applicability",
        "approved_at",
        "approved_by",
        "constraint_pack_id",
        "constraints",
        "locked_dimensions",
        "project_id",
        "shot_ids",
        "status",
        "version",
    ):
        assert field in data


def test_draft_serializes_approved_at_as_empty_string() -> None:
    """required 字段不出现 null：客户端无法区分「已审批但时间未知」与「未审批」。"""
    data = _pack().to_dict()

    assert data["approved_at"] == ""
