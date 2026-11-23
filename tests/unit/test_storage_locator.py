"""受控存储定位符的语法单测（P1-07 / #179）。

这组用例守的不是「正则能匹配」，而是三条**结构性质**：

1. ``build_locator`` 与 ``parse_locator`` 互逆：能解析出来的定位符，必定等于
   用它的 sha256 与扩展名重建的结果。
2. 前两段必须由哈希派生。这条是本轮才发现的真实缺口——旧实现只校验语法，
   于是 ``local://ff/ee/<真实哈希>.png`` 语法合法、能登记，但文件实际写在
   ``<hash[0:2]>/<hash[2:4]>/`` 下，**永远读不到**，而且报的是
   ``StorageIntegrityError``（数据丢了），不是「你拼错了」。
3. 扩展名只能是登记过的产物类型；非法值一律拒绝，不留「看起来能过」的模糊带。
"""

from __future__ import annotations

import pytest

from packages.shared.storage_locator import (
    ALLOWED_EXTENSIONS,
    EXT_BY_MEDIA_TYPE,
    LOCATOR_PATTERN,
    LOCATOR_SHAPE,
    ParsedLocator,
    build_locator,
    extension_for_media_type,
    is_controlled_locator,
    parse_locator,
    sql_locator_check,
)

_DIGEST = "0123456789abcdef" * 4  # 64 位


def test_build_then_parse_roundtrips() -> None:
    """构造与解析互逆：解析结果 = 输入的 (哈希, 扩展名)。"""
    for extension in sorted(ALLOWED_EXTENSIONS):
        locator = build_locator(_DIGEST, extension)
        parsed, problem = parse_locator(locator)
        assert problem == ""
        assert parsed == ParsedLocator(sha256=_DIGEST, extension=extension)


def test_parse_result_rebuilds_identical_locator() -> None:
    """性质：任一被接受的定位符，都等于用它自身解析出的字段重建的结果。

    这比「往返一次相等」更强——它排除了「解析成功但字段丢失信息」（例如
    解析时把扩展名归一成别的值、或丢弃前两段），那种情况下往返仍可能相等，
    但重建会暴露差异。
    """
    for extension in sorted(ALLOWED_EXTENSIONS):
        locator = build_locator(_DIGEST, extension)
        parsed, _ = parse_locator(locator)
        assert parsed is not None
        assert build_locator(parsed.sha256, parsed.extension) == locator


def test_locator_embeds_digest_prefix_as_two_segments() -> None:
    """前两段就是哈希的前四位，各占两字符。"""
    locator = build_locator(_DIGEST, "png")
    assert locator.startswith(f"local://{_DIGEST[0:2]}/{_DIGEST[2:4]}/{_DIGEST}.png")


@pytest.mark.parametrize("swap", [("ff", "ee"), ("01", "23"), ("00", "00")])
def test_mismatched_prefix_segments_are_rejected(swap: tuple[str, str]) -> None:
    """前两段与哈希不符 → 拒绝，且原因必须指向「拼错了」而非「数据丢了」。

    ``("00","00")`` 也在列表里：即使前两段看起来「像个哈希前缀」，只要与
    这一行的哈希不符就是错的。若判据写成「是不是十六进制」，它会蒙混过关。
    """
    first, second = swap
    if (first, second) == (_DIGEST[0:2], _DIGEST[2:4]):
        pytest.skip("该组合恰好与真实前缀相同，不构成错位用例")
    locator = f"local://{first}/{second}/{_DIGEST}.png"
    parsed, problem = parse_locator(locator)
    assert parsed is None
    assert "由哈希派生" in problem


def test_unregistered_extension_is_rejected_with_readable_reason() -> None:
    """未登记的扩展名被拒，且原因列出已登记集合（不是笼统的「格式错误」）。"""
    unregistered = next(
        ext for ext in ("sh", "exe", "tmp", "html") if ext not in ALLOWED_EXTENSIONS
    )
    locator = build_locator(_DIGEST, "png").replace(".png", f".{unregistered}")
    parsed, problem = parse_locator(locator)
    assert parsed is None
    assert "不是登记的产物类型" in problem


@pytest.mark.parametrize(
    "locator",
    [
        "",
        "   ",
        "s3://bucket/frame.png",
        "local://../../etc/passwd",
        "local://ab/cd/../../../../etc/passwd.png",
        f"local://aa/aa/{_DIGEST[:-1]}.png",  # 63 位
        f"local://aa/aa/{_DIGEST}0.png",  # 65 位
        f"local://AA/aa/{_DIGEST}.png",  # 大写段
        f"local://aa/aa/{_DIGEST.upper()}.png",  # 大写哈希
        f"local://aa/aa/{'z' * 64}.png",  # 非十六进制
        f"local://aa/aa/{_DIGEST}.png/extra",  # 尾随路径
        f"/local://aa/aa/{_DIGEST}.png",  # 前导斜杠
    ],
)
def test_malformed_locators_are_rejected(locator: str) -> None:
    parsed, problem = parse_locator(locator)
    assert parsed is None
    assert problem


def test_non_string_input_is_rejected_not_crashed() -> None:
    """``storage_ref`` 历史上可能是 dict：非字符串必须判假而不是抛 TypeError。"""
    for value in ({"uri": "local://aa/aa/x.png"}, None, 42, b"local://aa/aa/x.png"):
        assert is_controlled_locator(value) is False
        parsed, problem = parse_locator(value)  # type: ignore[arg-type]
        assert parsed is None
        assert problem


def test_build_rejects_bad_digest_and_extension() -> None:
    """构造端同样严格：宽松的构造端只是把非法值推迟到读取时才炸。"""
    with pytest.raises(ValueError, match="64 位"):
        build_locator("abc", "png")
    with pytest.raises(ValueError, match="扩展名"):
        build_locator(_DIGEST, "sh")


def test_build_accepts_uppercase_digest_by_normalizing() -> None:
    """构造时归一化大写：领域层已把 sha256 转小写，这里允许直接传原值。"""
    assert build_locator(_DIGEST.upper(), "png") == build_locator(_DIGEST, "png")


def test_extension_mapping_is_injective() -> None:
    """扩展名 → 媒体类型必须单射，否则「产物是什么类型」只能靠猜。

    ``ParsedLocator.media_type`` 反查时取第一个匹配项；若两个媒体类型共享
    同一扩展名，反查结果取决于字典顺序——那是不可解释的行为。
    """
    extensions = list(EXT_BY_MEDIA_TYPE.values())
    assert len(extensions) == len(set(extensions))
    for media_type, extension in EXT_BY_MEDIA_TYPE.items():
        assert extension_for_media_type(media_type) == extension
        locator = build_locator(_DIGEST, extension)
        parsed, _ = parse_locator(locator)
        assert parsed is not None
        assert parsed.media_type == media_type


def test_unknown_media_type_has_no_extension() -> None:
    assert extension_for_media_type("model/gltf-binary") is None


def test_contract_pattern_is_anchored_and_lists_every_extension() -> None:
    """契约 pattern 必须锚定两端并枚举全部白名单扩展名。

    不锚定时 ``re.match`` 只看前缀——``local://aa/aa/<hash>.png/../../x`` 会被
    判为合法，而那正是路径穿越的写法。
    """
    assert LOCATOR_PATTERN.startswith("^")
    assert LOCATOR_PATTERN.endswith("$")
    for extension in ALLOWED_EXTENSIONS:
        assert extension in LOCATOR_PATTERN


def test_sql_check_expression_uses_derived_positions() -> None:
    """SQL 表达式的位置由结构算出，不与 Python 侧独立漂移。

    这里只断言「位置指向什么」，真正执行期的等价性由集成测试
    ``test_storage_locator_constraint.py`` 用真实 INSERT 锁定——SQL 文本对不对，
    最终只有数据库能回答。
    """
    expression = sql_locator_check("storage_ref")
    # 段 1 在 9（"local://" 占 1..8），哈希起点在 15；段 2 在 12，哈希第 3-4 位在 17。
    assert "substr(storage_ref, 9, 2) = substr(storage_ref, 15, 2)" in expression
    assert "substr(storage_ref, 12, 2) = substr(storage_ref, 17, 2)" in expression
    # 默认带上跨列一致：定位符内嵌哈希 == sha256 列。
    assert "substr(storage_ref, 15, 64) = lower(sha256)" in expression
    # 无哈希列的表可以不传，此时不生成该合取项。
    assert "lower(" not in sql_locator_check("storage_ref", digest_column=None)


def test_sql_check_rejects_unsafe_identifiers() -> None:
    """列名会拼进 SQL：必须是简单标识符，否则是注入面。"""
    with pytest.raises(ValueError):
        sql_locator_check("storage_ref; DROP TABLE x")
    with pytest.raises(ValueError):
        sql_locator_check("storage_ref", digest_column="sha256) OR (1=1")


def test_locator_shape_hint_is_human_readable() -> None:
    """错误消息里的形态说明与真实形态一致——对不上号会把人引向错误的排查方向。"""
    assert LOCATOR_SHAPE == "local://<sha256 前2位>/<sha256 第3-4位>/<完整 sha256>.<扩展名>"
