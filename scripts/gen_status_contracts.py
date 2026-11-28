"""状态契约派生器：从 state_machines.yaml 生成枚举注册表与 Schema 的 status.enum。

为什么需要它
-----------
状态集合此前有三份手写副本：`state_machines.yaml` 的文本状态链、`enum_registry.yaml`
的状态列表、`object_schemas.schema.json` 的 `status.enum`。三份各自手改，于是：

- `enum_registry.yaml` 头部写着「由 state_machines 派生」，实测却多出 PG0 / PG3 /
  PMS / PASS / PUBLISHED 这类根本不是状态的值——它们是正文备注里的关键词被机械
  抓取的结果，说明这份「派生文件」其实一直是手写的；
- 76 个带 `status` 的对象里只有 6 个收了 `enum`，其余是裸 `string`：任何字符串都能
  通过校验，C.3.1「未知枚举值必须拒绝」形同虚设；
- 没有人能回答「改一个状态集要改几处」，因此也没有人敢改。

本脚本把「一处权威、两处派生」变成可执行的规则：

    contracts/state_machines/state_machines.yaml   (allowed_states，唯一权威)
            │
            ├─► contracts/enums/enum_registry.yaml          (整份生成)
            └─► object_schemas.schema.json 的 status.enum   (逐块生成)

用法
----
    python scripts/gen_status_contracts.py --check   # CI 与本地闸门（只读）
    python scripts/gen_status_contracts.py --write   # 契约变更后重算派生结果

`--write` 只做「把权威源的改动同步到派生处」，不是「让闸门变绿」的工具：
它不会、也不能凭空造出状态值——那些只能写进 state_machines.yaml。

设计要点
--------
- **Schema 按块改写，不整份重排**：`object_schemas.schema.json` 是手工排版的，
  整份 `json.dumps` 会产出近八千行无意义 diff，评审时真正的改动会被淹没。
  这里只重写每个定义的 `status` 属性块，其余字节原样保留。
- **派生集合按字母序**：状态是集合不是序列，顺序不承载语义。字母序让两份派生结果
  可逐字节比对，也让「只是顺序不同」永远不会被误判成契约漂移。
- **幂等**：`--check` 的实现就是「算一遍，和磁盘上的比字节」。因此任何手工改派生
  文件的行为都会立刻暴露，不存在「改了但恰好一致」的灰色地带。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from packages.shared.yaml_strict import load_yaml_file  # noqa: E402

STATE_MACHINES = ROOT / "contracts" / "state_machines" / "state_machines.yaml"
ENUM_REGISTRY = ROOT / "contracts" / "enums" / "enum_registry.yaml"
SCHEMA = ROOT / "contracts" / "schemas" / "object_schemas.schema.json"
TAXONOMY = ROOT / "contracts" / "enums" / "status_field_taxonomy.yaml"
LAYERS = ROOT / "contracts" / "enums" / "status_layers.yaml"
FIELD_VALUE_SETS = ROOT / "contracts" / "enums" / "field_value_sets.yaml"

_LIFECYCLE = "lifecycle"
_ENVELOPE_ONLY = "envelope_only"
_LIFECYCLE_GAP = "lifecycle_gap"
_CLASSES = (_LIFECYCLE, _ENVELOPE_ONLY, _LIFECYCLE_GAP)


# --------------------------------------------------------------------------- #
# 权威源读取
# --------------------------------------------------------------------------- #
def load_yaml_mapping(path: Path) -> dict[str, Any]:
    data = load_yaml_file(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 解析结果不是映射（可能为空文件）")
    return data


def lifecycle_states(data: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """返回 ``{对象: 允许状态（字母序）}``，唯一的派生入口。

    只收 ``objects`` 分区：``projections`` 是 UI 投影，没有可写入的 status；
    ``text_authority_only`` 的对象还没建模，给它们造 enum 就是给不存在的对象
    造契约。
    """
    if data is None:
        data = load_yaml_mapping(STATE_MACHINES)
    states: dict[str, list[str]] = {}
    for entry in data.get("objects") or []:
        name = str(entry.get("object", "")).strip()
        allowed = entry.get("allowed_states")
        if not name or not isinstance(allowed, list) or not allowed:
            raise ValueError(f"state_machines 条目 {name!r} 缺 allowed_states")
        values = [str(v) for v in allowed]
        if len(set(values)) != len(values):
            raise ValueError(f"{name}.allowed_states 有重复值")
        states[name] = sorted(values)
    return states


def taxonomy_classes(path: Path | None = None) -> dict[str, str]:
    """返回 ``{对象: 分类}``。分类非法直接抛错（契约损坏，不可静默）。"""
    data = load_yaml_mapping(path or TAXONOMY)
    result: dict[str, str] = {}
    for entry in data.get("objects") or []:
        name = str(entry.get("object", "")).strip()
        cls = str(entry.get("class", "")).strip()
        if not name:
            raise ValueError("status_field_taxonomy 有条目缺 object")
        if cls not in _CLASSES:
            raise ValueError(f"{name} 的分类非法: {cls!r}（允许 {_CLASSES}）")
        if name in result:
            raise ValueError(f"{name} 在 status_field_taxonomy 里重复登记")
        result[name] = cls
    return result


def field_value_sets_from_data(data: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    """解析已加载的值集台账；测试注入坏状态时走同一解析入口。"""
    result: dict[tuple[str, str], list[str]] = {}
    for entry in data.get("fields") or []:
        obj = str(entry.get("object", "")).strip()
        field = str(entry.get("field", "")).strip()
        values = entry.get("values")
        key = (obj, field)
        if not obj or not field or not isinstance(values, list) or not values:
            raise ValueError(f"field_value_sets 条目 {key!r} 缺 object/field/values")
        parsed = [str(v).strip() for v in values]
        if any(not v for v in parsed) or len(set(parsed)) != len(parsed):
            raise ValueError(f"field_value_sets 条目 {key!r} 的值集有空值或重复值")
        if key in result:
            raise ValueError(f"field_value_sets 条目 {key!r} 重复登记")
        if field == "status":
            raise ValueError(f"field_value_sets 不得登记信封 status（{obj} 应走状态机）")
        result[key] = sorted(parsed)
    return result


def field_value_sets(path: Path | None = None) -> dict[tuple[str, str], list[str]]:
    """返回 ``{(对象, 字段): 权威值集（字母序）}``。

    只管非 status 的维度字段：信封 status 的权威源仍是 state_machines.yaml。
    两套事实源按「字段名」分界，重叠登记会在 contract_check 里报红。
    """
    return field_value_sets_from_data(load_yaml_mapping(path or FIELD_VALUE_SETS))


def envelope_only_sentinel(layers: dict[str, Any] | None = None) -> str:
    data = layers if layers is not None else load_yaml_mapping(LAYERS)
    sentinel = str(data.get("envelope_only_sentinel", "")).strip()
    if not sentinel:
        raise ValueError("status_layers 缺 envelope_only_sentinel")
    return sentinel


def enum_required(
    states: dict[str, list[str]],
    classes: dict[str, str],
    layers: dict[str, Any] | None = None,
    definitions: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """需要收 enum 的对象与取值集合。

    lifecycle     → 权威状态集（state_machines.allowed_states）；
    envelope_only → 单个哨兵值，禁止裸 string（没有生命周期 ≠ 任意值）；
    lifecycle_gap → 不收 enum，保留带债务登记的裸 string。
    """
    required: dict[str, list[str]] = {
        name: values for name, values in states.items() if classes.get(name) == _LIFECYCLE
    }
    if definitions is None:
        # 延迟加载：--check 只需要 authority + taxonomy + layers 即可算出派生结果；
        # 传入 definitions 时按传入对象集生成，避免与磁盘上的 schema 互相依赖。
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        definitions = schema.get("definitions") or schema.get("$defs") or {}
    sentinel = envelope_only_sentinel(layers)
    for name, spec in definitions.items():
        if not isinstance(spec, dict):
            continue
        if "status" not in (spec.get("properties") or {}):
            continue
        if classes.get(name) == _ENVELOPE_ONLY:
            required[name] = [sentinel]
    return required


# --------------------------------------------------------------------------- #
# enum_registry.yaml 生成
# --------------------------------------------------------------------------- #
_ENUM_HEADER = """\
# 枚举注册表（派生文件，禁止手改）
#
# 本文件由 scripts/gen_status_contracts.py 从
# contracts/state_machines/state_machines.yaml 的 allowed_states 生成。
# 手改会在 `python scripts/gen_status_contracts.py --check` 下立刻报漂移，
# 因为那条检查的实现就是「重新生成一遍，逐字节比对」。
#
# 为什么还要留这份「可以随时重算」的文件：
# - phase0 冻结包（phase0_manifest.md 第 ⑨ 项）按字节登记它，删除会破坏冻结链；
# - 人类评审时需要一份「只有对象与状态值」的清单，不必在千行状态机文件里翻。
# 但它不承载任何独立信息：想改状态只能改 state_machines.yaml。
#
# 权威状态值的三层分离（权威 / 展示投影别名 / 跨域标记）已移到
# contracts/enums/status_layers.yaml —— 那两层是手工裁定的语义，与派生文件混放
# 正是上一版出现 PG0 / PMS / PASS 这类假状态的原因。

generated_by: scripts/gen_status_contracts.py
source: contracts/state_machines/state_machines.yaml
object_count: {count}
objects:
"""


def render_enum_registry(states: dict[str, list[str]]) -> str:
    lines = [_ENUM_HEADER.format(count=len(states))]
    for name in sorted(states):
        values = ", ".join(states[name])
        lines.append(f"- object: {name}\n  states: [{values}]\n")
    return "".join(lines)


# --------------------------------------------------------------------------- #
# object_schemas.schema.json 的 status.enum 生成
# --------------------------------------------------------------------------- #
_DEF_OPEN = re.compile(r'^(\s*)"(?P<name>[^"]+)": \{\s*$')


def _read_text(path: Path) -> tuple[str, str]:
    """读取文本并保留原换行风格（contracts/ 是 -text，字节即契约）。"""
    raw = path.read_bytes().decode("utf-8")
    return raw, ("\r\n" if "\r\n" in raw else "\n")


def _lines(raw: str, nl: str) -> list[str]:
    return [line + nl for line in raw.split(nl)[:-1]] if raw.endswith(nl) else raw.split(nl)


def _definitions_span(lines: list[str]) -> list[tuple[str, int, int]]:
    """返回每个定义块的 ``(名字, 起始行, 结束行)``（闭合行含在内）。"""
    spans: list[tuple[str, int, int]] = []
    i = 0
    while i < len(lines):
        match = _DEF_OPEN.match(lines[i].rstrip("\r\n"))
        if match and len(match.group(1)) == 4:
            depth = 0
            for j in range(i, len(lines)):
                text = lines[j].rstrip("\r\n")
                depth += text.count("{") - text.count("}")
                if depth == 0:
                    spans.append((match.group("name"), i, j))
                    i = j
                    break
        i += 1
    return spans


def _normalize_def_indent(lines: list[str], start: int, end: int) -> None:
    """把定义体缩进不足 6 空格的块右移 2 格（幂等）。

    存在的理由：手工往 JSON 里插块时容易少一级缩进（Scene 就曾整块少两级），
    语法合法、评审难看出，但会让后续按缩进定位的改写错位。这里按「第一行子键的
    缩进」判断，发现不足就整块右移，保证同一份文件里定位规则始终成立。
    """
    if start + 1 > end:
        return
    first = lines[start + 1].rstrip("\r\n")
    actual = len(first) - len(first.lstrip(" "))
    if actual >= 6:
        return
    shift = " " * (6 - actual)
    for idx in range(start + 1, end + 1):
        body = lines[idx].rstrip("\r\n")
        if body:
            lines[idx] = shift + body + lines[idx][len(body) :]


def _property_block(
    lines: list[str], start: int, end: int, field: str
) -> tuple[int, int, int] | None:
    """在定义块内定位一个属性对象，返回 ``(起始行, 结束行, 键缩进)``。

    只认属性层（定义 4 → properties 6 → 属性 8）。按精确文本匹配键名，
    不会把 `required` 数组里的同名字样误认成属性定义。
    """
    opener = f'"{field}": {{'
    for idx in range(start + 1, end + 1):
        text = lines[idx].rstrip("\r\n")
        if text.strip() != opener:
            continue
        indent = len(text) - len(text.lstrip(" "))
        if indent != 8:
            continue
        depth = 0
        for j in range(idx, end + 1):
            body_line = lines[j].rstrip("\r\n")
            depth += body_line.count("{") - body_line.count("}")
            if depth == 0:
                return idx, j, indent
    return None


def _status_block(lines: list[str], start: int, end: int) -> tuple[int, int, int] | None:
    """在定义块内定位信封 status 对象（兼容旧调用入口）。"""
    return _property_block(lines, start, end, "status")


def _render_property_block(
    field: str, key_indent: str, nl: str, properties: dict[str, Any], had_comma: bool
) -> list[str]:
    """按原文件的排版风格重建任意属性块（键 2 空格递进）。"""
    inner = " " * (len(key_indent) + 2)
    render: dict[str, Any] = {"type": properties.get("type", "string")}
    if "enum" in properties:
        render["enum"] = list(properties["enum"])
    if "description" in properties:
        render["description"] = properties["description"]
    unknown = set(properties) - {"type", "enum", "description"}
    if unknown:
        raise ValueError(f"{field} 属性含未预期键，拒绝静默丢弃: {sorted(unknown)}")

    # json.dumps 的首尾两行是外层的 { 与 }，由本函数自行输出（带原文件的缩进与逗号）。
    body = json.dumps(render, ensure_ascii=False, indent=2).split("\n")[1:-1]
    out = [f'{key_indent}"{field}": {{{nl}']
    for line in body:
        # 去掉 json.dumps 的一级基准缩进（2 空格），保留条目的相对层级。
        out.append(inner + line[2:] + nl)
    out.append(f"{key_indent}}}{',' if had_comma else ''}{nl}")
    return out


def _render_status_block(
    key_indent: str, nl: str, properties: dict[str, Any], had_comma: bool
) -> list[str]:
    """兼容旧入口：重建信封 status 属性块。"""
    return _render_property_block("status", key_indent, nl, properties, had_comma)


def patch_schema(
    raw: str,
    nl: str,
    required: dict[str, list[str]],
    field_values: dict[tuple[str, str], list[str]] | None = None,
) -> str:
    """把 status.enum 与已裁定维度字段的 enum 设成权威值集，其余对象清掉 status enum。

    两条派生链共用同一份 Schema，但权威源不同：status 来自 state_machines，
    维度字段来自 field_value_sets。只按显式登记的字段改写，绝不扫描全文件
    猜测「哪些字段该收 enum」——那会把普通字符串字段误伤成封闭枚举。
    """
    lines = _lines(raw, nl)
    data = json.loads(raw)
    definitions = data.get("definitions") or data.get("$defs") or {}
    field_values = field_values or {}

    for _name, start, end in _definitions_span(lines):
        _normalize_def_indent(lines, start, end)

    # 先把所有改写算好，再从后往前落盘：splice 会改变后面的行号，
    # 一边遍历一边改会让后续定义的定位整体错位（改着改着写到邻居对象里）。
    patches: list[tuple[int, int, list[str]]] = []
    for name, start, end in _definitions_span(lines):
        spec = definitions.get(name)
        if not isinstance(spec, dict):
            continue
        declared_fields = sorted(field for (obj, field) in field_values if obj == name)
        targets = ["status", *declared_fields]
        for field in targets:
            found = _property_block(lines, start, end, field)
            if found is None:
                if field == "status":
                    continue  # 无信封字段的对象（如纯列表定义）不归本派生器管
                raise ValueError(f"field_value_sets 登记了 {name}.{field}，但 Schema 没有这个属性")
            block_start, block_end, indent = found
            props = dict(spec.get("properties", {}).get(field) or {})
            want = required.get(name) if field == "status" else field_values[(name, field)]
            if want:
                props["enum"] = list(want)
            else:
                props.pop("enum", None)
            had_comma = lines[block_end].rstrip("\r\n").endswith(",")
            patches.append(
                (
                    block_start,
                    block_end,
                    _render_property_block(field, " " * indent, nl, props, had_comma),
                )
            )

    # 必须按**行号降序**落盘，不能按收集顺序倒序：一个定义里既有 status 又有
    # 维度字段时，收集顺序是 ["status", 维度字段...]，而文件里的物理顺序并不相同，
    # 倒序应用会让先前的扩行把后面的行号推走，补丁写进邻居属性里。
    for block_start, block_end, replacement in sorted(patches, reverse=True):
        lines[block_start : block_end + 1] = replacement

    patched = "".join(lines)
    # 自证：改写只允许改变 enum，其他语义必须逐键相等。
    _assert_only_enum_changed(data, json.loads(patched))
    return patched


def _assert_only_enum_changed(before: dict[str, Any], after: dict[str, Any]) -> None:
    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "enum"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    if strip(before) != strip(after):
        raise SystemExit("[FAIL] schema 改写越界：除 enum 外还有字段被改动")


# --------------------------------------------------------------------------- #
# 漂移检测与落盘
# --------------------------------------------------------------------------- #
def _drifted_enum_objects(
    expected: dict[str, tuple[str, ...]], actual: dict[str, tuple[str, ...]]
) -> list[str]:
    """列出 enum 期望值与实际值不一致的键；文件级排版差异返回空列表。"""
    return sorted(
        key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key)
    )


def _schema_enum_map(schema_text: str) -> dict[str, tuple[str, ...]]:
    """从 Schema 文本抽出 ``对象.字段 -> enum 值``，用于定位漂移字段。"""
    definitions = json.loads(schema_text).get("definitions") or {}
    result: dict[str, tuple[str, ...]] = {}
    for name, spec in definitions.items():
        if not isinstance(spec, dict):
            continue
        for field, prop in (spec.get("properties") or {}).items():
            if isinstance(prop, dict) and isinstance(prop.get("enum"), list):
                result[f"{name}.{field}"] = tuple(str(v) for v in prop["enum"])
    return result


def drift_problems() -> list[str]:
    """返回派生结果与磁盘文件的差异（空列表 = 一致）。"""
    states = lifecycle_states()
    classes = taxonomy_classes()
    layers = load_yaml_mapping(LAYERS)
    raw, nl = _read_text(SCHEMA)
    definitions = json.loads(raw).get("definitions") or {}
    required = enum_required(states, classes, layers, definitions)
    field_values = field_value_sets()
    problems: list[str] = []

    expected_registry = render_enum_registry(states)
    actual_registry = ENUM_REGISTRY.read_text(encoding="utf-8")
    if expected_registry != actual_registry:
        actual_states = {
            str(e.get("object")): tuple(str(v) for v in (e.get("states") or []))
            for e in load_yaml_file(ENUM_REGISTRY).get("objects") or []
        }
        drifted = _drifted_enum_objects(
            {name: tuple(values) for name, values in states.items()}, actual_states
        )
        problems.append(
            "enum_registry.yaml 与 state_machines.allowed_states 不一致"
            f"（漂移对象: {', '.join(drifted) or '文件级差异'}；"
            "跑 --write 重算，不要手改派生文件）"
        )

    expected_schema = patch_schema(raw, nl, required, field_values)
    if expected_schema != raw:
        drifted = _drifted_enum_objects(_schema_enum_map(expected_schema), _schema_enum_map(raw))
        problems.append(
            "object_schemas.schema.json 的字段 enum 与权威源不一致"
            f"（漂移字段: {', '.join(drifted) or '文件级差异'}；"
            "status 来自 state_machines，维度字段来自 field_value_sets；跑 --write 重算）"
        )
    return problems


def write_all() -> None:
    states = lifecycle_states()
    classes = taxonomy_classes()
    layers = load_yaml_mapping(LAYERS)
    raw, nl = _read_text(SCHEMA)
    definitions = json.loads(raw).get("definitions") or {}
    required = enum_required(states, classes, layers, definitions)
    field_values = field_value_sets()

    ENUM_REGISTRY.write_bytes(render_enum_registry(states).encode("utf-8"))
    SCHEMA.write_bytes(patch_schema(raw, nl, required, field_values).encode("utf-8"))

    lifecycle_count = sum(1 for cls in classes.values() if cls == _LIFECYCLE)
    print(
        f"[WRITE] enum_registry: {len(states)} 个对象；"
        f"schema status.enum: {len(required)} 个对象收 enum"
        f"（生命周期 {lifecycle_count} + 信封哨兵 {len(required) - lifecycle_count}），"
        f"维度字段 enum: {len(field_values)} 个字段；"
        "lifecycle_gap 对象按台账保留裸 string 并挂债务字段"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="状态契约派生器")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="只读校验派生结果")
    group.add_argument("--write", action="store_true", help="重算并写入派生文件")
    args = parser.parse_args()

    if args.write:
        write_all()
        return 0

    problems = drift_problems()
    for problem in problems:
        print(f"[FAIL] {problem}")
    if problems:
        return 1
    print("[PASS] 状态派生一致：enum_registry 与 schema status.enum 均由 allowed_states 重算得到")
    return 0


if __name__ == "__main__":
    sys.exit(main())
