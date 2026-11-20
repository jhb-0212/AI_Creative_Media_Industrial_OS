"""RLS 表达式的受限 AST：唯一可执行语法（ADR-R40 附录 D.2 / D.6.0c）。

为什么不是自由文本
------------------
「``using: "tenant_id = current_tenant AND ..."``」这类字符串看起来像定义，
实际不是：``current_tenant`` 没有映射到任何 GUC，生成器无法编译，测试也只能
手写另一份预期行为。于是「Registry 声明一种语义、SQL 写另一种、测试再维护
第三种」成为必然。本模块把表达式收敛成**闭合的节点集合**：解析器只认这些
节点，编译器只产出这些节点对应的 SQL，第四个节点类型不存在就编译不出来。

两条硬规则（都由 E11.2/E11.4 实测得出，不是风格偏好）
----------------------------------------------------
1. **数组上下文只走原生数组输入语法**：``current_setting('app.current_projects', true)::text[]``。
   禁止 ``string_to_array(current_setting(...), ',')``——PostgreSQL 的数组文本
   是 ``{a,b}``，按逗号切会得到 ``{"{a", "b}"}``（花括号残留在首末元素上），
   查询不报错、只是永远匹配不到任何行。同一上下文下旧写法 0 行、新写法 684 行。
2. **元素类型必须与被比较列同族**：``uuid[]`` 与真实列 ``varchar(64)`` 不可比，
   ``{project-1}::uuid[]`` 直接 22P02。类型不符在此处报错，而不是等到运行时。

本模块不做任何 I/O，也不读注册表之外的配置：给同样的节点与列类型，永远得到
同样的 SQL（可做逐字黄金比对）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

# 文本族：这些类型之间可直接比较（varchar 列 = ANY(text[]) 合法且无隐式转换损失）。
_TEXT_FAMILY: Final[frozenset[str]] = frozenset(
    {"text", "varchar", "character varying", "citext", "bpchar", "character"}
)

_IDENT_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z_][a-z0-9_]*$")


class ExpressionError(ValueError):
    """AST 不合法：未知节点、引用越界列、类型不可比、语法键冲突。"""


# --------------------------------------------------------------------------- #
# 值源与节点（闭合集合：新增节点必须同时改解析与编译，否则类型检查就不过）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Guc:
    """单值上下文，如 ``app.current_tenant_id``。"""

    name: str


@dataclass(frozen=True, slots=True)
class GucArray:
    """数组上下文（唯一解析通道，§4.2）。

    刻意**没有** ``cast`` 字段：v1.4.1 的 ``cast: text`` 会诱导出
    ``::text[]`` 之外的切分实现，元素类型只由 ``element_type`` 声明。
    """

    name: str
    element_type: str


@dataclass(frozen=True, slots=True)
class Literal:
    """常量。仅允许在例外策略中使用（闸门 V8b 校验）。"""

    value: str


@dataclass(frozen=True, slots=True)
class Eq:
    column: str
    value: Guc | GucArray | Literal


@dataclass(frozen=True, slots=True)
class InArray:
    column: str
    value: Guc | GucArray | Literal


@dataclass(frozen=True, slots=True)
class IsNull:
    column: str


@dataclass(frozen=True, slots=True)
class AllOf:
    children: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class AnyOf:
    children: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class NotExpr:
    child: Node


type ValueSource = Guc | GucArray | Literal
type Node = Eq | InArray | IsNull | AllOf | AnyOf | NotExpr


# --------------------------------------------------------------------------- #
# 解析（原始 dict -> 节点）
# --------------------------------------------------------------------------- #
def parse(raw: object, *, where: str) -> Node:
    """把 YAML 里的映射解析成节点；任何不合规形状都在这里报错并带位置。"""
    mapping = _mapping(raw, where)
    if len(mapping) != 1:
        raise ExpressionError(
            f"{where}: 节点必须恰好有一个键，实际 {sorted(mapping)}"
            "（多键会让「这个节点是什么意思」取决于字典序）"
        )
    ((kind, body),) = mapping.items()

    match kind:
        case "all":
            return AllOf(_children(body, f"{where}.all"))
        case "any":
            return AnyOf(_children(body, f"{where}.any"))
        case "not":
            return NotExpr(parse(body, where=f"{where}.not"))
        case "is_null":
            inner = _mapping(body, f"{where}.is_null")
            _reject_extra(inner, {"column"}, f"{where}.is_null")
            return IsNull(_column(inner.get("column"), f"{where}.is_null.column"))
        case "eq" | "in":
            return _predicate(kind, body, where=f"{where}.{kind}")
        case _:
            raise ExpressionError(
                f"{where}: 未知节点 {kind!r}"
                "（合法：all/any/not/eq/in/is_null；不支持自由文本表达式）"
            )


def _predicate(kind: str, body: object, *, where: str) -> Node:
    inner = _mapping(body, where)
    _reject_extra(inner, {"column", "value"}, where)
    column = _column(inner.get("column"), f"{where}.column")
    if "value" not in inner:
        raise ExpressionError(f"{where}: 缺少 value")
    source = _value_source(inner["value"], where=f"{where}.value")
    return Eq(column, source) if kind == "eq" else InArray(column, source)


def _value_source(raw: object, *, where: str) -> ValueSource:
    mapping = _mapping(raw, where)
    # 值源用「键标识种类」，element_type 是同层修饰项而非独立键——
    # 因此不能要求 mapping 只有一个键（那会把合法的 guc_array 写法判死）。
    kinds = [key for key in mapping if key in {"guc", "guc_array", "literal"}]
    if len(kinds) != 1:
        raise ExpressionError(
            f"{where}: 值源必须恰好声明一个来源（guc/guc_array/literal），实际 {sorted(mapping)}"
        )
    kind = kinds[0]
    match kind:
        case "guc":
            _reject_extra(mapping, {"guc"}, where)
            return Guc(_guc_name(mapping["guc"], where))
        case "guc_array":
            # v1.4.1 的 cast 参数已作废：出现即报错，不静默忽略。
            if "cast" in mapping:
                raise ExpressionError(
                    f"{where}: guc_array 不再接受 cast 参数"
                    "（元素类型只由 element_type 声明，解析只走原生数组输入语法）"
                )
            _reject_extra(mapping, {"guc_array", "element_type"}, where)
            if "element_type" not in mapping:
                raise ExpressionError(f"{where}: guc_array 需要 element_type")
            return GucArray(
                _guc_name(mapping["guc_array"], where),
                _sql_type(mapping["element_type"], f"{where}.element_type"),
            )
        case _:
            _reject_extra(mapping, {"literal"}, where)
            body = mapping["literal"]
            if not isinstance(body, str) or not body:
                raise ExpressionError(f"{where}.literal: 必须是非空字符串")
            return Literal(body)


# --------------------------------------------------------------------------- #
# 编译（节点 -> SQL 片段）
# --------------------------------------------------------------------------- #
def compile_sql(node: Node, column_types: Mapping[str, str]) -> str:
    """编译为 SQL 表达式（不含外层 ``USING`` / ``WITH CHECK`` 包裹）。

    ``column_types`` 是该资源声明的 ``scope_dimensions``（列名 → SQL 类型）。
    引用其中不存在的列直接报错——这正是 v1.4 「三谓词逐表齐备」踩过的坑：
    ``asset_versions`` 没有 ``organization_id``、``outbox_events`` 没有 ``project_id``。
    """
    match node:
        case AllOf(children=children):
            return "(" + " AND ".join(compile_sql(c, column_types) for c in children) + ")"
        case AnyOf(children=children):
            return "(" + " OR ".join(compile_sql(c, column_types) for c in children) + ")"
        case NotExpr(child=child):
            return f"NOT ({compile_sql(child, column_types)})"
        case IsNull(column=column):
            _column_type(column, column_types)
            return f"{_ident(column)} IS NULL"
        case Eq(column=column, value=value):
            declared = _column_type(column, column_types)
            return f"{_ident(column)} = {_single_value_sql(value, declared)}"
        case InArray(column=column, value=value):
            declared = _column_type(column, column_types)
            return f"{_ident(column)} = ANY ({_array_value_sql(value, declared, column)})"
        case _:
            # 结构上不可达：Node 是闭合联合。留此分支是为了让「新增节点忘了改编译」
            # 在运行期立即失败，而不是静默产出 None 之类的东西。
            raise ExpressionError(f"未实现的节点类型：{type(node).__name__}")


def _single_value_sql(value: ValueSource, declared: str) -> str:
    match value:
        case Guc(name=name):
            return f"current_setting({_literal_sql(name)}, true)::{_cast_target(declared)}"
        case GucArray(name=name):
            raise ExpressionError(
                f"eq 的值源不能是数组上下文 {name!r}（数组只用于 in 的 ANY 比较）"
            )
        case Literal(value=raw):
            return _literal_sql(raw)


def _array_value_sql(value: ValueSource, declared: str, column: str) -> str:
    match value:
        case GucArray(name=name, element_type=element_type):
            _ensure_comparable(element_type, declared, column)
            return f"current_setting({_literal_sql(name)}, true)::{_cast_target(element_type)}[]"
        case Guc(name=name):
            raise ExpressionError(f"in 的值源不能是单值上下文 {name!r}（需要 guc_array）")
        case Literal(value=raw):
            raise ExpressionError(
                f"in 不接受 literal（{raw!r}）：数组上下文必须来自 GUC，"
                "否则隔离边界由文件内容而不是请求身份决定"
            )


def _ensure_comparable(element_type: str, declared: str, column: str) -> None:
    left = element_type.strip().lower()
    right = declared.strip().lower()
    if left == right or (left in _TEXT_FAMILY and right in _TEXT_FAMILY):
        return
    raise ExpressionError(
        f"列 {column!r}（{declared}）与数组元素类型 {element_type} 不可比较："
        "元素类型必须与列同族（E11.2：{project-1}::uuid[] 在 varchar 列上直接 22P02）"
    )


def _column_type(column: str, column_types: Mapping[str, str]) -> str:
    try:
        return column_types[column]
    except KeyError:
        raise ExpressionError(
            f"列 {column!r} 不在该资源的 scope_dimensions 内"
            f"（可用：{sorted(column_types)}）；"
            "策略只能引用该表**实际具备**的隔离维度"
        ) from None


def _cast_target(sql_type: str) -> str:
    """列声明类型 -> 可直接写在 ``::`` 后的类型名。"""
    normalized = sql_type.strip().lower()
    if normalized == "character varying":
        return "varchar"
    if normalized == "character":
        return "bpchar"
    return normalized


# --------------------------------------------------------------------------- #
# 小工具（严格类型，供解析与编译共用）
# --------------------------------------------------------------------------- #
def _mapping(raw: object, where: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ExpressionError(f"{where}: 期望映射，实际 {type(raw).__name__}")
    for key in raw:
        if not isinstance(key, str):
            raise ExpressionError(f"{where}: 键必须是字符串，实际 {key!r}")
    return raw


def _children(raw: object, where: str) -> tuple[Node, ...]:
    if not isinstance(raw, list) or not raw:
        raise ExpressionError(f"{where}: 需要非空列表（空 AND/OR 语义未定义）")
    return tuple(parse(item, where=f"{where}[{index}]") for index, item in enumerate(raw))


def _reject_extra(mapping: Mapping[str, object], allowed: set[str], where: str) -> None:
    extra = set(mapping) - allowed
    if extra:
        raise ExpressionError(
            f"{where}: 不认识的键 {sorted(extra)}（合法：{sorted(allowed)}）"
            "——拼错的键会让声明的约束静默失效"
        )


def _column(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not _IDENT_RE.match(raw):
        raise ExpressionError(f"{where}: 列名必须是 ^[a-z_][a-z0-9_]*$，实际 {raw!r}")
    return raw


def _guc_name(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not re.match(r"^app\.[a-z_][a-z0-9_]*$", raw):
        raise ExpressionError(f"{where}: GUC 名必须形如 app.<name>（小写标识符），实际 {raw!r}")
    return raw


def _sql_type(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not re.match(r"^[a-z][a-z0-9_ ]*$", raw):
        raise ExpressionError(f"{where}: SQL 类型名不合法，实际 {raw!r}")
    return raw


def _ident(name: str) -> str:
    return name if _IDENT_RE.match(name) else f'"{name}"'


def _literal_sql(value: str) -> str:
    """常量转义：单引号加倍；同时拒绝含 NUL 的字符串（PG 不接受）。"""
    if "\x00" in value:
        raise ExpressionError("常量含 NUL 字符，PostgreSQL 不接受")
    return "'" + value.replace("'", "''") + "'"


__all__ = [
    "AllOf",
    "AnyOf",
    "Eq",
    "ExpressionError",
    "Guc",
    "GucArray",
    "InArray",
    "IsNull",
    "Literal",
    "Node",
    "NotExpr",
    "ValueSource",
    "compile_sql",
    "parse",
]
