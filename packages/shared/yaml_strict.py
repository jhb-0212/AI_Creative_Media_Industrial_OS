"""严格 YAML 载入：拒绝显式重复的映射键（治理 YAML 的唯一载入事实源）。

为什么必须是单一事实源
--------------------
PyYAML 的 ``safe_load`` 对重复映射键是「后键静默覆盖前键」——不报错、不警告。
``contract_amendments.yaml`` 曾因末尾重复的 ``revocations: []`` 块被后者覆盖，
这类静默缺陷正是 G5 / D1 记录的病根。若只修那一个文件，同类问题仍会在下一份
契约 YAML 上重演。因此所有读取契约/治理 YAML 的代码一律经由本模块，
不直接用 ``yaml.safe_load``。

判据
----
- 拒绝**显式书写两次及以上**的映射键（含嵌套——``construct_mapping`` 递归触发）。
- **不拒绝** YAML merge key（``<<: *anchor``）：它是有意的键继承语义，由 PyYAML
  的 ``flatten_mapping`` 展开，显式键覆盖 merge 键是标准行为，不算重复声明。

失败模式：重复键抛 ``DuplicateYamlKeyError``（ValueError 子类），携带键名、行号、来源，
可被闸门的 ``except`` 统一兜底报红，也可被运行时加载器转成结构化错误。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class DuplicateYamlKeyError(ValueError):
    """治理 YAML 中存在显式重复的映射键。"""

    def __init__(self, key: Any, line: int, source: str = "<string>") -> None:
        self.key = key
        self.line = line
        self.source = source
        super().__init__(
            f"{source}:{line} 存在重复映射键 {key!r}——"
            "PyYAML 默认静默覆盖后键，会让契约/台账声明失效"
        )


class StrictSafeLoader(yaml.SafeLoader):
    """在 ``flatten_mapping`` 展开 merge key 之前检测显式重复键。"""

    source: str = "<string>"

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: dict[tuple[str, str], int] = {}
        for key_node, _value_node in node.value:
            # merge key（<<: *anchor）是有意的继承语义，不是重复声明。
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            # 只按标量键判定身份；复杂键（序列/映射）交给标准 SafeLoader 处理，
            # 契约 YAML 的键均为字符串标量，不会因此漏掉真实重复键。
            if not isinstance(key_node, yaml.ScalarNode):
                continue
            ident = (str(key_node.tag), str(key_node.value))
            if ident in seen:
                raise DuplicateYamlKeyError(
                    key_node.value,
                    key_node.start_mark.line + 1,
                    self.source,
                )
            seen[ident] = key_node.start_mark.line + 1
        return super().construct_mapping(node, deep=deep)


# 每个 source 绑定一个 Loader 子类，把来源写进异常供点名；source 集合有限，可缓存。
_LOADERS: dict[str, type[StrictSafeLoader]] = {}


def _loader_for(source: str) -> type[StrictSafeLoader]:
    cached = _LOADERS.get(source)
    if cached is None:
        cached = type(
            "_BoundStrictSafeLoader",
            (StrictSafeLoader,),
            {"source": source},
        )
        _LOADERS[source] = cached
    return cached


def load_yaml(text: str, *, source: str = "<string>") -> Any:
    """严格解析 YAML 文本：显式重复键抛 ``DuplicateYamlKeyError``。"""
    try:
        return yaml.load(text, Loader=_loader_for(source))
    except DuplicateYamlKeyError:
        raise
    except yaml.YAMLError as exc:
        raise ValueError(f"{source} YAML 解析失败: {exc}") from exc


def load_yaml_file(path: Path | str) -> Any:
    """严格解析 YAML 文件；缺失/解析失败转可读 ``ValueError``。"""
    target = Path(path)
    if not target.exists():
        raise ValueError(f"YAML 文件缺失: {target}")
    return load_yaml(target.read_text(encoding="utf-8"), source=str(target))
