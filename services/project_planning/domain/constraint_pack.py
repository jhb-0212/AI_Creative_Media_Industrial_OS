"""ConstraintPack 聚合根（隐式连续性约束包，7.9 / A23 实现）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#ConstraintPack（字段与 required）；
- contracts/state_machines/state_machines.yaml → objects → ConstraintPack
  （DRAFT→ACTIVE；ACTIVE→STALE/SUPERSEDED/REVOKED）；
- 正文 7.9：包级字段契约 + 条目八字段 + 「只有 ACTIVE 可作为生成输入」。

本对象回答「这组镜头的哪些维度已被授权锁死、凭什么锁」。由此派生四条边界，
都是刻意的：

- **不承载镜头内容**：内容归 ShotSpec / ShotRevision。约束包只说「不许改什么」，
  不说「要拍成什么样」。
- **不承载生成结果**：它是生成的**输入前置**，不是产物。
- **条目不是独立对象**：契约把 constraints 定义为包内 `object[]`，条目没有
  自己的生命周期与状态机。给它建表会让「包内条目」与「独立条目」成为两个
  事实源，而契约只承认前者。
- **包版本不是实体版本**：`version` 是业务版本（变更须新建版本并 supersede
  旧版本），`entity_version` 是并发控制的乐观锁版本。两者混同会让「改了一次
  状态」看起来像「出了一个新版本」。

为什么不变量要分「创建时」与「锁定时」两段
--------------------------------------------------
DRAFT 的价值就是可以渐进填写：先建包、再补维度与条目。若在构造时要求
「APPLICABLE 必须至少锁一项」，草稿阶段就无法存在——调用方只能一次性提交
完整包，而 7.9 的定位是「登记」，登记本来就是先记后定。

但 LOCK 的含义是「授权人已作出判断」，此时「适用却什么都没锁」说不通。
所以内容完备性判定挂在 `transition_to(ACTIVE)` 上，与 ShotSpec 的
「LOCKED 必须有 locked_dimensions」同一个位置、同一个理由。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import ConstraintPackStatus
from services.project_planning.domain.state_machine import CONSTRAINT_PACK_LIFECYCLE

# 7.9 原文：「已授权锁定维度：camera_intrinsics、camera_extrinsics、camera_path、
# blocking、shot_size_nodes、cut_points、spatial_relations」。
#
# 这份词表来自契约，不是实现者编的——这是它与 ShotSpec.locked_dimensions 的
# 关键差别：8.12 只给了「锁定维度」这个概念，没给全集，所以那边「不编词表」是
# 对的；7.9 给了全集，这里不校验就等于把契约明文当成建议。
LOCKED_DIMENSIONS: frozenset[str] = frozenset(
    {
        "camera_intrinsics",
        "camera_extrinsics",
        "camera_path",
        "blocking",
        "shot_size_nodes",
        "cut_points",
        "spatial_relations",
    }
)

APPLICABLE = "APPLICABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"
_APPLICABILITY_VALUES = frozenset({APPLICABLE, NOT_APPLICABLE})

# 7.9 原文：「每条必含 constraint_id、target、shot_scope、rule、source、
# authority_level、valid_at、approval_ref」。
#
# 只断言「必含」，不断言「仅含」：契约没有禁止附加字段，禁止它就是发明约束。
# 也不校验取值格式：契约只给了字段名与「必填」，没给 valid_at 的类型、
# authority_level 的等级集。编一套格式等于用一个契约里读不到的规则拒绝合法输入
# （A22 在 shot_size_changes 上踩过同一个坑）。
CONSTRAINT_ENTRY_REQUIRED: tuple[str, ...] = (
    "constraint_id",
    "target",
    "shot_scope",
    "rule",
    "source",
    "authority_level",
    "valid_at",
    "approval_ref",
)

_ENTRY_ID = "constraint_id"


def validate_constraint_entries(entries: list[dict[str, Any]]) -> None:
    """校验约束条目集合：每条是对象、八字段齐备且非空、条目 id 包内唯一。

    为什么 id 必须唯一：约束条目没有独立对象，`constraint_id` 就是它在这份包里
    的唯一地址。重复 id 会让「撤销第 7 条约束」这一类操作（下一批的治理边）
    指向两个条目，而调用方无从区分。
    """
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(f"constraints[{index}] 必须是对象，实际为 {type(entry).__name__}"),
            )
        missing = [
            key for key in CONSTRAINT_ENTRY_REQUIRED if key not in entry or _blank(entry[key])
        ]
        if missing:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"constraints[{index}] 缺必填字段或取值为空: {missing}",
            )
        constraint_id = str(entry[_ENTRY_ID]).strip()
        if constraint_id in seen:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"constraints[{index}] 的 {_ENTRY_ID}={constraint_id} 在包内重复："
                    "条目 id 是包内唯一地址"
                ),
            )
        seen.add(constraint_id)


def normalize_dimensions(raw: list[str]) -> list[str]:
    """规范化锁定维度：去空白、按契约声明序排序、去重。

    排序而不是保持调用方顺序：`locked_dimensions` 的语义是**集合**（「哪些维度
    被锁」）。保持原序会让 ``["blocking","cut_points"]`` 与
    ``["cut_points","blocking"]`` 成为两个不同的值，此后任何「两份包的锁定维度
    是否相同」的比对都会假不等——而且不会报错，只会给出错误答案。

    不在这里判词汇表：词表校验另有一个入口（``assert_known_dimensions``），
    因为「规范化」与「合法性」是两件事，混在一处会让「想只排序」的调用方
    被迫接受一次合法性判定。
    """
    parts = [str(item).strip() for item in raw]
    unique = {part for part in parts if part}
    # 按 LOCKED_DIMENSIONS 的声明序输出（frozenset 无序，故用固定元组定序），
    # 未知维度排在已知维度之后并按字典序——它随后会被合法性判定拒绝，
    # 这里只需要一个确定性结果，而不是一个"看起来正常"的结果。
    known_order = (
        "camera_intrinsics",
        "camera_extrinsics",
        "camera_path",
        "blocking",
        "shot_size_nodes",
        "cut_points",
        "spatial_relations",
    )
    ordered = [name for name in known_order if name in unique]
    ordered.extend(sorted(unique - set(known_order)))
    return ordered


def assert_known_dimensions(dimensions: list[str]) -> None:
    """锁定维度必须落在 7.9 的受控取值内。

    契约原文用「：」列举了七个值，这在字段契约里就是枚举。允许任意字符串会让
    ``camera_instrinsics``（拼错）或 ``camera``（含糊）进入库，而下游按这些名字
    找不到任何可执行的锁定语义——错误沉默地留在数据里，只在生成阶段以
    「为什么这一版没被拦住」的形式出现。
    """
    unknown = [name for name in dimensions if name not in LOCKED_DIMENSIONS]
    if unknown:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=(
                f"locked_dimensions 含契约未声明的维度 {unknown}；"
                f"7.9 只允许 {sorted(LOCKED_DIMENSIONS)}"
            ),
        )


def _blank(value: Any) -> bool:
    """条目字段是否「取值为空」。

    统一转文本再判空，与库层 ``constraint_entries_complete`` 的
    ``btrim(e->>k) = ''`` 同口径——同一不变量两层判据必须逐字等价，否则会出现
    「领域拒绝、库里合法」或反过来这种最难查的分歧。

    ``None`` 单独处理：``str(None)`` 是 ``"None"``（非空），会被放行；而库里
    JSON null 经 ``->>`` 得到 SQL NULL。不显式判 None，两层就会在这里分叉。
    """
    if value is None:
        return True
    return not str(value).strip()


@dataclass
class ConstraintPack(Entity):
    """约束包（多镜头作用域；锁定后只有治理边可走，内容不可原地改）。"""

    ENTITY_TYPE = "ConstraintPack"

    owner_module: str = OWNER_MODULE

    constraint_pack_id: str = ""
    project_id: str = ""
    # 场景级包可为空（镜头级包不带场景）。契约 7.9：「镜头级包可为空」。
    scene_id: str = ""
    # 作用镜头范围。空集合的包没有任何约束对象，视为非法——但「空」与
    # 「无锁定约束」是两件事：后者用 applicability=NOT_APPLICABLE 表达，
    # 且同样要指名镜头（「这些镜头没有锁定约束」也是个判断）。
    shot_ids: list[str] = field(default_factory=list)
    shot_group_ids: list[str] = field(default_factory=list)
    applicability: str = APPLICABLE
    locked_dimensions: list[str] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    # 来源 BASELINE PrevizVersion；EXPLORATORY 型不得填入（契约 7.9）。
    source_previz_revision: str = ""
    # 包版本（业务版本，非 entity_version）。
    version: int = 1
    approved_by: str = ""
    approved_at: datetime | None = None
    status: ConstraintPackStatus = ConstraintPackStatus.DRAFT

    def __post_init__(self) -> None:
        super()._validate_envelope()
        # constraint_pack_id 是**逻辑包**标识，entity_id 是**版本行**标识，两者不是
        # 同一件事。7.9 的 version 字段写「变更须新建版本并 supersede 旧版本，不得
        # 原地覆盖」：新版本 = 新行（新 entity_id）+ 同一个包标识 + version 递增。
        # 若强制两者相等，CP-6（同一逻辑包至多一个 ACTIVE）与
        # uq_constraint_pack_version 都会退化成「同一行不可能出现两次」——结构上恒真
        # 的约束不是约束，只是把两条不变量登记成 RUNTIME_ENFORCED 的虚假声明。
        # 之所以在 Scene/Shot/ShotRevision/ShotSpec 上写了 identity_single：它们的
        # 契约（SC-1/SH-1/SR-1）明确要求「身份单一」且没有跨行版本；本对象不同，
        # 照抄那种形态会把版本语义抹掉。
        # 缺省取 entity_id：首个版本两者自然相同，调用方不必关心这个区别。
        self.constraint_pack_id = (self.constraint_pack_id or "").strip() or self.entity_id

        for name in ("constraint_pack_id", "project_id"):
            if not getattr(self, name).strip():
                raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空")
        for name, max_len in (
            ("constraint_pack_id", 64),
            ("project_id", 64),
            ("scene_id", 64),
            ("source_previz_revision", 200),
            ("approved_by", 64),
        ):
            if len(getattr(self, name)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

        # 7.9：「无锁定约束的镜头必须显式标 NOT_APPLICABLE，不得留空」。
        # 留空（空串/未知值）在这里被拒，而不是被当成默认的 APPLICABLE——
        # 「没说」与「说了适用」是两种判断，不能靠默认值合并。
        if self.applicability not in _APPLICABILITY_VALUES:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"applicability 必须是 APPLICABLE 或 NOT_APPLICABLE，"
                    f"实际为 {self.applicability!r}；无锁定约束不得留空"
                ),
            )

        # shot_ids 引用的是 Shot（逻辑镜头），不是 ShotRevision：约束作用在
        # 「这个镜头」而不在「某一版」。空集合会让包没有作用对象。
        self.shot_ids = normalize_id_set(self.shot_ids, field_name="shot_ids", required=True)
        # shot_group_ids 是**可选**字段（7.9 required 不含它）：空数组表示
        # 「本包不绑定连续镜头组」，是合法输入，不能套用 shot_ids 的非空要求。
        self.shot_group_ids = normalize_id_set(
            self.shot_group_ids, field_name="shot_group_ids", required=False
        )

        self.locked_dimensions = normalize_dimensions(self.locked_dimensions)
        assert_known_dimensions(self.locked_dimensions)
        validate_constraint_entries(self.constraints)

        # CP-4：NOT_APPLICABLE 的字面含义就是「没有锁定约束」。若同时列了锁定
        # 维度或约束条目，这份包在自述矛盾——下游无论信哪一半都会错。
        if self.applicability == NOT_APPLICABLE and (self.locked_dimensions or self.constraints):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    "applicability=NOT_APPLICABLE 时不得携带 locked_dimensions "
                    "或 constraints：标为不适用的包就是「无锁定约束」"
                ),
            )

        # version 的类型判定与 ShotSpec 的 duration_ms 同因：``>= 1`` 放行
        # 浮点与 bool，前者会在库里被截断，后者变成 True。
        if type(self.version) is not int:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"version 必须是整数，实际为 {self.version!r}",
            )
        if self.version < 1:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="version 必须为正整数")
        if self.approved_at is not None and not isinstance(self.approved_at, datetime):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="approved_at 必须是 datetime（或在 DRAFT 阶段留空）",
            )

    def transition_to(
        self, target: ConstraintPackStatus, audit_ref: str, action: str
    ) -> ConstraintPack:
        """执行状态迁移：结构 + 所有权 + 生效证据。

        LOCK（DRAFT→ACTIVE）要求三件事同时成立，缺一即拒：

        1. **审批人**（``approved_by``）：契约 7.9 的 required 字段，且
           「不得为 Agent」。后半句不在这里判——授权层已把 LOCK 动作限定给
           导演/授权 TD（Agent/Worker 角色没有该权限），在这里再按字符串猜
           主体类型，就是 A13 批判过的「用 requested_by == approved_by 冒充
           职责分离」。判据只有一个来源，放在最可靠的那一层。
        2. **审批时间**（``approved_at``）：没有时间点的审批无法与版本对应。
        3. **内容自洽**：APPLICABLE 时至少要锁住一个维度或给出一条约束。
           否则「适用」无对应物，此后既拦不住任何修改，也解释不了为什么建这个包。

        NOT_APPLICABLE 的包同样走这条边（契约：「同样必须经 ACTIVE，以证明
        『无锁定约束』是授权人判断而非默认值」），此时第 3 条自然满足——
        它的内容为空是 ``__post_init__`` 已经保证的事。
        """
        CONSTRAINT_PACK_LIFECYCLE.edge_owned_by(self.status, target, action)
        if target is ConstraintPackStatus.ACTIVE:
            if not self.approved_by.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="锁定必须给出 approved_by（审批人不能为空）",
                )
            if self.approved_at is None:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="锁定必须给出 approved_at（审批时间不能为空）",
                )
            if (
                self.applicability == APPLICABLE
                and not self.locked_dimensions
                and not self.constraints
            ):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        "applicability=APPLICABLE 的包至少要有 1 个锁定维度或"
                        "1 条约束；「适用但没有锁任何东西」无法作为生成输入"
                    ),
                )
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约形态（7.9 字段名逐字对应）。

        ``project_id`` **要输出**——与 ShotSpec 相反，7.9 的字段契约里它
        是一个必填字段（包自带作用域），不是内部归属键。是否输出由契约决定，
        不由「它看起来像内部键」决定。
        """
        data = self._envelope_dict()
        data.update(
            {
                "constraint_pack_id": self.constraint_pack_id,
                "project_id": self.project_id,
                "scene_id": self.scene_id,
                "shot_ids": list(self.shot_ids),
                "shot_group_ids": list(self.shot_group_ids),
                "applicability": self.applicability,
                "locked_dimensions": list(self.locked_dimensions),
                "constraints": list(self.constraints),
                "source_previz_revision": self.source_previz_revision,
                "version": self.version,
                "approved_by": self.approved_by,
                # 契约类型是 string；DRAFT 阶段无审批时间，输出空串而不是 null
                # （required 字段不出现 null 形态，避免客户端把它读成「已审批但时间未知」）。
                "approved_at": (self.approved_at.isoformat() if self.approved_at else ""),
            }
        )
        return data


def normalize_id_set(raw: list[str], *, field_name: str, required: bool) -> list[str]:
    """规范化 id 集合：去空白、去空串、保序去重。

    保序（而不是排序）：``shot_ids`` 的顺序在契约里没有语义，但「保序去重」
    让调用方看到的响应与自己给出的顺序一致，便于逐字段 diff；而
    ``locked_dimensions`` 是集合比较的对象，故排序。

    ``required`` 区分必填与可选字段：``shot_ids`` 是 7.9 的 required，
    ``shot_group_ids`` 不是。把两者写成同一条规则，会让「不绑定镜头组」
    这种合法输入被拒——而错误信息还会说「不能为空」，指向一个契约里
    并不存在的必填要求。
    """
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"{field_name}[{index}] 必须是字符串，实际为 {type(item).__name__}",
            )
        value = item.strip()
        if not value:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"{field_name}[{index}] 不能为空字符串",
            )
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    if required and not result:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=f"{field_name} 不能为空：约束包必须指名作用对象",
        )
    return result


__all__ = [
    "APPLICABLE",
    "CONSTRAINT_ENTRY_REQUIRED",
    "LOCKED_DIMENSIONS",
    "NOT_APPLICABLE",
    "ConstraintPack",
    "assert_known_dimensions",
    "normalize_dimensions",
    "normalize_id_set",
    "validate_constraint_entries",
]
