"""ShotSpec 领域不变量（SS-1 / SS-2 / SS-3 与锁定证据）。

为什么判据钉在聚合根而不是只测 HTTP：SS-3 的六个失败分支在服务层走的是同一个
函数，但**能不能被绕过**取决于聚合根是否在 ``__post_init__`` 就判定。只在服务层
校验时，任何不经过服务的写入（迁移脚本、后台补偿、将来的批量导入）都能把一份
自相矛盾的规格写进库，而库层 CHECK 只能拒绝「切点单调与帧数」，拒绝不了
「fps 是浮点」「身份键两处不一致」这类形态。

本文件刻意不碰数据库：对象自身的一致性不需要 IO，把它放进需要 PG 的用例里
会让「一条纯算术判断」的失败被环境问题掩盖（本轮就发生过：容器退出导致全库
用例 401，若不看根因极易误判为领域回归）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from services.project_planning.domain.enums import ShotSpecStatus
from services.project_planning.domain.shot_spec import (
    ShotSpec,
    assert_cut_points_within_duration,
    normalize_locked_dimensions,
    total_frames,
)
from services.project_planning.domain.state_machine import (
    SHOT_SPEC_ALLOWED_TRANSITIONS,
    SHOT_SPEC_LOCK_ACTION,
)

_TENANT = uuid4()
# 1000ms、30fps 的镜头 = 30 帧，合法帧号 0…29；多数用例用这个口径。
_DURATION = 1000
_FPS_NUM = 30
_FPS_DEN = 1


def _spec(**overrides: object) -> ShotSpec:
    kwargs: dict[str, object] = {
        "entity_id": "spec-1",
        "project_id": "proj-1",
        "shot_revision_id": "rev-1",
        "duration_ms": _DURATION,
        "fps_num": _FPS_NUM,
        "fps_den": _FPS_DEN,
        "cut_points_frames": [0, 10, 20],
        "aspect_ratio": "2.39:1",
        "coordinate_system": "SCENE_LOCAL",
        "camera_path": "push-in",
        "subject_paths": "A→B",
        "shot_size_changes": "MS→CU",
        "previz_ref": "previz://shot-1/v3",
        "created_by": "director",
        "tenant_id": _TENANT,
    }
    kwargs.update(overrides)
    return ShotSpec(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 帧数换算：整数运算的唯一理由就是「差一帧」
# --------------------------------------------------------------------------


def test_ntsc_rate_is_not_approximated_by_float() -> None:
    """30000/1001 与 29.97 的差别在 1 秒处正好是一帧的边界。

    浮点法（``1000 / 1000 * 29.97``）四舍五入得 30，本实现得 29。若两者混用，
    「库里合法、领域拒绝」的切点判定会在整秒处出现一帧分歧——这类分歧只在
    NTSC 素材上复现，最难查。
    """
    assert total_frames(1000, 30000, 1001) == 29
    assert total_frames(1000, 30, 1) == 30


def test_total_frames_floors_partial_tail() -> None:
    """不足一整帧的尾部不构成可落点的帧边界，故向下取整。"""
    assert total_frames(999, 25, 1) == 24  # 24.975 帧


def test_ten_second_ntsc_clip_loses_the_fractional_frame() -> None:
    """10 秒 29.97fps = 299 帧（不是 300）：与库层整数除法逐字等价。"""
    assert total_frames(10_000, 30000, 1001) == 299


# --------------------------------------------------------------------------
# SS-3：六个失败分支各有独立修复方向，因此各自独立断言
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cut_points", "expected"),
    [
        ([], "不能为空"),
        ([0, True], "必须是整数帧"),
        ([0, 1.5], "必须是整数帧"),
        ([0, -1], "不能为负"),
        ([5, 10], "首点必须为 0"),
        ([0, 10, 10], "严格递增"),
        ([0, 8, 12, 30], "超出镜头帧数 30"),
    ],
)
def test_ss3_rejections_are_distinguishable(
    cut_points: list[int], expected: str
) -> None:
    with pytest.raises(DomainError) as exc:
        assert_cut_points_within_duration(
            cut_points,
            duration_ms=_DURATION,
            fps_num=_FPS_NUM,
            fps_den=_FPS_DEN,
        )

    assert exc.value.error_code.code == "VALIDATION_ERROR"
    assert expected in exc.value.detail


def test_cut_point_equal_to_frame_count_is_out_of_range() -> None:
    """边界是 ``< frames`` 而不是 ``<= frames``：30 帧镜头的帧号只到 29。"""
    with pytest.raises(DomainError) as exc:
        assert_cut_points_within_duration(
            [0, 30], duration_ms=_DURATION, fps_num=_FPS_NUM, fps_den=_FPS_DEN
        )

    assert "超出镜头帧数" in exc.value.detail


def test_last_valid_frame_boundary_is_accepted() -> None:
    assert_cut_points_within_duration(
        [0, 10, 29], duration_ms=_DURATION, fps_num=_FPS_NUM, fps_den=_FPS_DEN
    )


def test_bool_is_rejected_even_though_it_is_an_int_subclass() -> None:
    """``True`` 会被 ``isinstance(x, int)`` 放行并在库里变成 1——必须按类型判。"""
    with pytest.raises(DomainError) as exc:
        assert_cut_points_within_duration(
            [0, True], duration_ms=_DURATION, fps_num=_FPS_NUM, fps_den=_FPS_DEN
        )

    assert "cut_points_frames[1]" in exc.value.detail


def test_ss3_is_enforced_at_construction_not_only_at_the_service() -> None:
    """绕过服务的写入也必须造不出矛盾对象：构造期即拒。"""
    with pytest.raises(DomainError) as exc:
        _spec(cut_points_frames=[0, 10, 40])

    assert "超出镜头帧数" in exc.value.detail


# --------------------------------------------------------------------------
# fps / 时长：类型与取值
# --------------------------------------------------------------------------


def test_float_frame_rate_is_rejected() -> None:
    """29.97 是合法 float，但会让整数换算静默退化成浮点。"""
    with pytest.raises(DomainError) as exc:
        _spec(fps_num=29.97)

    assert "必须是整数" in exc.value.detail


@pytest.mark.parametrize("field", ["duration_ms", "fps_num", "fps_den"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_numbers_are_rejected(field: str, value: int) -> None:
    with pytest.raises(DomainError) as exc:
        _spec(**{field: value})

    assert "正整数" in exc.value.detail


def test_shot_spec_id_must_match_entity_id() -> None:
    with pytest.raises(DomainError) as exc:
        _spec(shot_spec_id="spec-other")

    assert "必须与 entity_id 一致" in exc.value.detail


def test_draft_may_leave_descriptive_fields_empty() -> None:
    """契约对这些字段只给了 ``type: string``（无 minLength）。

    「无尺寸变化」就是空串，「尚未决定相机路径」也是空串。要求非空等于用契约
    没有的格式拒绝合法输入，且调用方在契约里读不到这条限制。
    """
    spec = _spec(
        aspect_ratio="",
        coordinate_system="",
        camera_path="",
        subject_paths="",
        shot_size_changes="",
        previz_ref="",
    )

    assert spec.camera_path == ""
    assert spec.to_dict()["shot_size_changes"] == ""


@pytest.mark.parametrize("field", ["project_id", "shot_revision_id"])
def test_binding_keys_cannot_be_blank(field: str) -> None:
    """绑定键空白的规格无法定位到任何修订，必须拒绝。

    ``shot_spec_id`` 不在此列：它由 ``entity_id`` 派生（身份单一约束），
    传别的值会先在「必须与 entity_id 一致」处被拒。
    """
    with pytest.raises(DomainError) as exc:
        _spec(**{field: "  "})

    assert "不能为空" in exc.value.detail


# --------------------------------------------------------------------------
# 锁定维度：集合语义
# --------------------------------------------------------------------------


def test_locked_dimensions_are_a_set_not_a_string() -> None:
    """顺序与空白不该产生两个不同的值，否则「锁了哪些维度」的比对会假不等。"""
    assert normalize_locked_dimensions("camera, subject") == "camera,subject"
    assert normalize_locked_dimensions("subject,camera") == "camera,subject"
    assert normalize_locked_dimensions("  camera ， subject ") == "camera,subject"
    assert normalize_locked_dimensions("camera,camera") == "camera"
    assert normalize_locked_dimensions("  ,  ") == ""


# --------------------------------------------------------------------------
# 状态机：创建动作不拥有锁定边，LOCKED 不可复活
# --------------------------------------------------------------------------


def test_create_action_cannot_lock() -> None:
    """``SHOTSPEC.CREATE`` 是 MEDIUM 风险且不拥有任何边。

    若它也能走 ``DRAFT -> LOCKED``，就等于把一次未受锁定审批的写入藏进创建
    接口——这正是 A21 拆分两个动作的理由。
    """
    spec = _spec()

    with pytest.raises(DomainError) as exc:
        spec.transition_to(ShotSpecStatus.LOCKED, "audit://1", "SHOTSPEC.CREATE")

    assert exc.value.error_code.code == "STATE_TRANSITION_DENIED"
    assert spec.status is ShotSpecStatus.DRAFT, "拒绝后状态不得改变"


def test_lock_requires_dimensions_and_leaves_entity_untouched() -> None:
    """没有维度的 LOCKED 说不清锁了什么，也就无法据此拒绝后续修改。"""
    spec = _spec()

    with pytest.raises(DomainError) as exc:
        spec.transition_to(ShotSpecStatus.LOCKED, "audit://1", SHOT_SPEC_LOCK_ACTION)

    assert exc.value.error_code.code == "VALIDATION_ERROR"
    assert "locked_dimensions" in exc.value.detail
    assert spec.status is ShotSpecStatus.DRAFT
    assert spec.entity_version == 1, "拒绝后版本不得前移"


def test_locked_is_terminal_no_resurrection() -> None:
    """终态不是靠 ``if`` 拒绝，而是边表里没有出边。"""
    spec = _spec()
    spec.locked_dimensions = "camera"
    spec.transition_to(ShotSpecStatus.LOCKED, "audit://1", SHOT_SPEC_LOCK_ACTION)

    assert SHOT_SPEC_ALLOWED_TRANSITIONS[ShotSpecStatus.LOCKED] == frozenset()

    with pytest.raises(DomainError) as exc:
        spec.transition_to(ShotSpecStatus.LOCKED, "audit://2", SHOT_SPEC_LOCK_ACTION)

    assert exc.value.error_code.code == "STATE_TRANSITION_DENIED"


# --------------------------------------------------------------------------
# 序列化：契约形态
# --------------------------------------------------------------------------


def test_serialization_matches_contract_shape() -> None:
    data = _spec().to_dict()

    assert "project_id" not in data, "项目键是内部归属键，契约里没有它"
    assert "tenant_id" not in data
    assert data["fps"] == {"num": 30, "den": 1}, "契约里 fps 是有理数对象"
    assert data["status"] == "DRAFT"
    assert data["shot_spec_id"] == "spec-1"
    assert data["cut_points_frames"] == [0, 10, 20]
    assert data["control_signal_refs"] == []


def test_total_frames_method_uses_the_same_arithmetic_as_the_check() -> None:
    spec = _spec(duration_ms=10_000, fps_num=30000, fps_den=1001)

    assert spec.total_frames() == total_frames(10_000, 30000, 1001) == 299
