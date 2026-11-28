"""ShotSpec 聚合根（镜头结构化规格，8.12 / A21 契约 / A22 实现）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#ShotSpec（字段、类型、SS-1…SS-4）；
- contracts/state_machines/state_machines.yaml → objects → ShotSpec
  （DRAFT → LOCKED，唯一一条边归 SHOTSPEC.LOCK）。

本对象回答「怎么拍、多长、切在哪」，由此派生三条边界，都是刻意的：
- **不承载时间线定位**（SS-4）：入出点与顺序归 TimelineDraft。放进来会让
  「同一镜头在另一版剪辑里被挪了位置」变成规格变更。
- **不承载生产进度**：进度属 Shot 生命周期，规格是版本化的静态描述。
- **时长单一权威**（SS-1）：ShotRevision 与 TimelineDraft 不再维护同义时长。

SS-3（切点单调与帧数换算）放在这里而不是只放服务层：它是**对象自身的一致性**，
不需要任何 IO。服务层再校验一次是为了给出更早、更可读的错误，库层 CHECK 是为了
拦住绕过服务的写入——三层同口径，不是三份实现（换算函数只有一份）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import ShotSpecStatus
from services.project_planning.domain.state_machine import SHOT_SPEC_LIFECYCLE

# 有理帧率的分母基准：时长 × fps 换算成帧数时使用（见 total_frames）。
_MS_PER_SECOND = 1000


def total_frames(duration_ms: int, fps_num: int, fps_den: int) -> int:
    """按有理帧率把时长换算成帧数（整数运算，不做浮点近似）。

    30000/1001 这类 NTSC 帧率必须用 num/den 表达：先算 ``duration_ms * num``
    再整除 ``1000 * den``，与库层 CHECK 的整数除法逐字等价（都是正数截断）。
    先除后乘会丢掉整帧，导致「库里合法、领域拒绝」这种最难查的分歧。

    结果向下取整是刻意的：镜头不足一整帧的尾部无法成为切点区间，切点必须落在
    真实的帧边界上。
    """
    return duration_ms * fps_num // (_MS_PER_SECOND * fps_den)


def assert_cut_points_within_duration(
    cut_points: list[int], *, duration_ms: int, fps_num: int, fps_den: int
) -> None:
    """SS-3：切点严格递增、首点为 0、末点不超过时长换算出的帧数。

    四个失败各有独立文案，因为修复方向完全不同：
    - 空/缺 0：切点集合没有起点，区间左闭右开无从谈起；
    - 重复：``uniqueItems`` 的语义要求，重复点会造出零长区间；
    - 非递增：切点乱序会让「第 n 段」失去唯一解；
    - 超时长：**不是**宽松提示——超出总帧数的切点在时间线上没有落点。
    """
    if not cut_points:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail="cut_points_frames 不能为空（至少包含起点 0）",
        )
    for index, point in enumerate(cut_points):
        # bool 是 int 的子类：True 会被 ``isinstance`` 放行并在库里变成 1。
        if type(point) is not int:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"cut_points_frames[{index}] 必须是整数帧，实际为 {point!r}",
            )
        if point < 0:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"cut_points_frames[{index}] 不能为负：{point}",
            )
    if cut_points[0] != 0:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=f"cut_points_frames 首点必须为 0，实际为 {cut_points[0]}",
        )
    for index in range(1, len(cut_points)):
        if cut_points[index] <= cut_points[index - 1]:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    "cut_points_frames 必须严格递增（SS-3）："
                    f"第 {index} 点 {cut_points[index]} 未大于前一点 "
                    f"{cut_points[index - 1]}"
                ),
            )
    frames = total_frames(duration_ms, fps_num, fps_den)
    if cut_points[-1] >= frames:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=(
                f"切点 {cut_points[-1]} 超出镜头帧数 {frames}"
                f"（duration_ms={duration_ms}，fps={fps_num}/{fps_den}；SS-3）"
            ),
        )


def normalize_locked_dimensions(raw: str) -> str:
    """把锁定维度清单规范成「排序去重、英文逗号分隔」的稳定形态。

    为什么要规范化而不是原样存：``locked_dimensions`` 在契约里是字符串，而它的
    语义是**集合**。原样存会让 ``"camera, subject"`` 与 ``"subject,camera"``
    成为两个不同的值——此后任何「锁了哪些维度」的比对都会假不等，且这种不等
    不会报错，只会给出错误答案。

    不做词汇表校验：契约没有给「必锁维度全集」（这正是 A21 不设
    PARTIALLY_LOCKED 的原因）。在这里编一套维度名，等于用一个未经批准的词表
    拒绝合法业务输入。
    """
    parts = [part.strip() for part in re.split(r"[,，]", raw)]
    dimensions = sorted({part for part in parts if part})
    return ",".join(dimensions)


@dataclass
class ShotSpec(Entity):
    """镜头规格（绑定精确修订；锁定后不可原地修改）。"""

    ENTITY_TYPE = "ShotSpec"

    owner_module: str = OWNER_MODULE

    shot_spec_id: str = ""
    project_id: str = ""
    shot_revision_id: str = ""
    duration_ms: int = 0
    fps_num: int = 0
    fps_den: int = 0
    cut_points_frames: list[int] = field(default_factory=list)
    aspect_ratio: str = ""
    coordinate_system: str = ""
    camera_path: str = ""
    subject_paths: str = ""
    shot_size_changes: str = ""
    previz_ref: str = ""
    control_signal_refs: list[str] = field(default_factory=list)
    # 锁定维度（SS 未给全集，故只约束形态）：DRAFT 时为空串，LOCK 时非空。
    locked_dimensions: str = ""
    status: ShotSpecStatus = ShotSpecStatus.DRAFT

    def __post_init__(self) -> None:
        super()._validate_envelope()
        if self.shot_spec_id and self.shot_spec_id != self.entity_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="shot_spec_id 必须与 entity_id 一致",
            )
        self.shot_spec_id = self.entity_id
        # 绑定与身份键必须非空：它们是「这份规格属于哪一条修订」的唯一证据，
        # 空值等于一份无法定位的规格。
        for name in ("shot_spec_id", "project_id", "shot_revision_id"):
            if not getattr(self, name).strip():
                raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空")
        # 描述性字段只约束长度上限。契约对它们只给了 ``type: string``（无
        # minLength）：DRAFT 阶段「尚未决定」「无尺寸变化」都可以是空串，在这里
        # 要求非空等于用一个契约没有的格式拒绝合法输入——与 locked_dimensions
        # 不编造维度词表是同一条理由。
        for name, max_len in (
            ("shot_spec_id", 64),
            ("project_id", 64),
            ("shot_revision_id", 64),
            ("aspect_ratio", 32),
            ("coordinate_system", 32),
            ("camera_path", 4000),
            ("subject_paths", 4000),
            ("shot_size_changes", 500),
            ("previz_ref", 200),
            ("locked_dimensions", 512),
        ):
            if len(getattr(self, name)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )
        # 「正整数」这句话只校验 ``< 1`` 是半个判据：``29.97`` 通过，``True`` 也通过
        # （bool 是 int 的子类）。前者会让整数换算静默变成浮点，后者在库里变成 1，
        # 两种都能写进 Integer 列——缺陷留到序列化或读回时才暴露。故先判类型。
        for name in ("duration_ms", "fps_num", "fps_den"):
            value = getattr(self, name)
            if type(value) is not int:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 必须是整数（禁用浮点帧率），实际为 {value!r}",
                )
        if self.duration_ms < 1:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="duration_ms 必须为正整数")
        if self.fps_num < 1 or self.fps_den < 1:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="fps 分子与分母都必须是正整数（禁用浮点帧率）",
            )
        # SS-3 在构造时判定：一份自身矛盾的规格不该被创建出来，更不该进库。
        assert_cut_points_within_duration(
            self.cut_points_frames,
            duration_ms=self.duration_ms,
            fps_num=self.fps_num,
            fps_den=self.fps_den,
        )

    def transition_to(self, target: ShotSpecStatus, audit_ref: str, action: str) -> ShotSpec:
        """执行状态迁移：结构 + 所有权 + 终态证据。

        锁定必须带齐「锁定维度」——没有维度的 LOCKED 只是把状态改成了 LOCKED，
        说不清锁了什么，也就无法据此拒绝后续修改（DRAFT 与 LOCKED 的差别正是
        「这些维度不可再改」）。依据与版本落在迁移事实里（authority_ref /
        entity_version），审批事件由同一事务的 SHOTSPEC_LOCKED 承载。
        """
        SHOT_SPEC_LIFECYCLE.edge_owned_by(self.status, target, action)
        if target is ShotSpecStatus.LOCKED and not self.locked_dimensions.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="锁定必须给出 locked_dimensions（锁定维度不能为空）",
            )
        self.status = target
        self._advance_version(audit_ref)
        return self

    def total_frames(self) -> int:
        """本规格的帧数（与库层 CHECK 同一算式）。"""
        return total_frames(self.duration_ms, self.fps_num, self.fps_den)

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约形态。

        **不输出 project_id**：它在 ShotSpec 的契约里不是字段（8.12 只要求绑定
        ``shot_revision_id``）。它与 ``tenant_id`` 同构——都是内部归属键：调用方
        通过修订定位规格，不需要项目键；输出它既制造契约漂移，也让「归属」看起来
        有第二个可被外部引用的入口。需要项目上下文的地方从授权解析取。
        """
        data = self._envelope_dict()
        data.update(
            {
                "shot_spec_id": self.shot_spec_id,
                "shot_revision_id": self.shot_revision_id,
                "duration_ms": self.duration_ms,
                # 契约里 fps 是有理数对象；库里拆两列是为了让「分子分母都为正」
                # 成为列级约束，序列化时再组装回契约形态。
                "fps": {"num": self.fps_num, "den": self.fps_den},
                "cut_points_frames": list(self.cut_points_frames),
                "aspect_ratio": self.aspect_ratio,
                "coordinate_system": self.coordinate_system,
                "camera_path": self.camera_path,
                "subject_paths": self.subject_paths,
                "shot_size_changes": self.shot_size_changes,
                "previz_ref": self.previz_ref,
                "control_signal_refs": list(self.control_signal_refs),
                "locked_dimensions": self.locked_dimensions,
            }
        )
        return data
