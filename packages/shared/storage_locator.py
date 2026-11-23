"""受控存储定位符的**唯一**语法定义（P1-07 / #179）。

定位符形态
----------
::

    local://<sha256 前2位>/<sha256 第3-4位>/<完整 sha256>.<扩展名>

例如 ``local://ab/cd/abcdef...（共 64 位）....png``。

为什么收敛成这一种形态
----------------------
整改前 ``asset_versions.storage_ref`` 是自由 ``dict``，契约里 ``AssetVersion``
声明为 ``{"type": "object"}``、``AssetReference`` 声明为 ``{"type": "string"}``
——同一个概念两种类型，且都接受任意值。后果不是「数据脏一点」：

- 调用方可声明 ``{"path": "../../etc/passwd"}``，而消费方（QC 检测、交付打包）
  会照着它去读；
- 同一个 ``storage_ref`` 可以先后指向不同内容 ⇒ 「资产不可覆盖」这条红线
  在存储层没有任何结构性保障；
- ``sha256`` 列与 ``storage_ref`` 里的哈希可以互相矛盾，而没有任何一处会说话。

形态一旦固定为内容寻址，上面三条都变成**结构性不可能**，不是靠校验：

1. 穿越不可表达——定位符里没有任何位置能放下路径片段；
2. 覆盖退化成本不存在——文件名就是内容哈希，想覆盖必须换内容，换内容必然
   换定位符，于是「覆盖」变成了「新增一份」；
3. 写入天然幂等——重复落同一份内容是无副作用的 no-op。

``a``/``b`` 两段为什么必须**由 sha256 派生**
--------------------------------------------
这是本次收敛才发现的一处真实缺口：原实现只校验语法
（``^local://[0-9a-f]{2}/[0-9a-f]{2}/<64hex>.<ext>$``），不校验前两段是否
等于哈希的前四位。于是 ``local://ff/ee/<某个真实 digest>.png`` 语法合法、
能通过登记，但 ``put`` 时文件写在 ``<digest[0:2]>/<digest[2:4]>/`` 下，
``get`` 时去 ``ff/ee/`` 找 ⇒ **永远读不到**。

更糟的是它报 ``StorageIntegrityError``（含义：数据丢了/被篡改），而真实
原因是调用方拼错了——``adapters/storage/ports.py`` 自己写的排障约定正是
「定位符非法不该被当成文件不存在，区分二者是为了排障」。所以两段一致性
必须属于**语法**的一部分（``StorageLocatorError``），不能留给读取时的
哈希复核去发现。

同一哈希、同一扩展名必须得到同一定位符
--------------------------------------
``build_locator`` 与 ``parse_locator`` 是互逆的一对，且 ``parse`` 的校验强到
「能解析出来的定位符，必定等于用它的 sha256 重建的结果」。这条等价关系由
``tests/unit/test_storage_locator.py`` 的往返用例锁定。

为什么不放在 ``adapters/storage``
--------------------------------
``packages`` 是唯一被所有层允许依赖的纯横切库（见 ``architecture_check`` 的
R1）。定位符语法既要被存储适配器解析，也要被资产域登记时校验——放
``adapters`` 会让业务域反向依赖存储实现，那正是 ``adapters`` 那条分层规则的
理由里说的「外部实现绑死领域模型」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 产物类型 → 扩展名。白名单而非黑名单：新增一种产物类型要**显式登记**，
# 而不是「忘了拦」就自动放行任意扩展名（缺这一条时 ``.sh`` / ``.exe`` 都能
# 通过纯语法校验）。
EXT_BY_MEDIA_TYPE: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "audio/wav": "wav",
    "application/octet-stream": "bin",
}

# 合法扩展名集合，**从映射派生**而不是另列一份：两份清单迟早不一致，
# 而不一致的表现是「正则放行的扩展名被白名单拒绝」，排障时极易误判。
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(EXT_BY_MEDIA_TYPE.values())

# 契约里登记的 ``storage_ref.pattern``。
#
# 它必须与代码判定**等价**（由 ``contract_check`` 抽样比对，见
# ``check_storage_locator_pattern``）。写法上有两点是刻意的：
#
# - ``\1\2`` 反向引用表达「前两段等于哈希前四位」。JSON Schema 的 pattern
#   按 ECMA-262 语义求值，反向引用是标准特性，因此这条约束能在契约里表达；
#   若不写死，契约就只能约束语法、管不住那条会导致「永远读不到」的错位。
# - 扩展名用枚举而非 ``[a-z0-9]{2,5}``：契约无法做「第二级白名单」，
#   枚举是唯一能一次判死的写法。
LOCATOR_PATTERN: str = (
    r"^local://([0-9a-f]{2})/([0-9a-f]{2})/(\1\2[0-9a-f]{60})\.("
    + "|".join(sorted(ALLOWED_EXTENSIONS))
    + r")$"
)

# 人类可读的形态说明。错误消息与契约 description 共用同一份措辞——两处各写
# 一遍的结果是排障时对不上号（一边说 xx/yy、一边说前两位）。
LOCATOR_SHAPE = "local://<sha256 前2位>/<sha256 第3-4位>/<完整 sha256>.<扩展名>"

# 基础语法：只判「像不像」，不含派生关系与扩展名白名单。分层判定的唯一目的是
# **给出精确原因**（是形态错、还是扩展名没登记、还是前两段与哈希不符），
# 最终「是否合法」仍由 ``_STRICT_RE`` 一次判死，避免两条判定漂移。
_BASE_RE = re.compile(r"^local://([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\.([a-z0-9]{2,5})$")
_STRICT_RE = re.compile(LOCATOR_PATTERN)


@dataclass(frozen=True)
class ParsedLocator:
    """解析成功的定位符。字段是**位置的事实**，不是原始字符串。"""

    sha256: str
    extension: str

    @property
    def media_type(self) -> str:
        """反查媒体类型。

        定位符里只有扩展名，而一个扩展名对应唯一媒体类型（映射是单射的）。
        若将来出现一对多（如 ``jpg`` 既是 ``image/jpeg`` 又是别的），这里必须
        改成返回候选集——**静默挑一个**会让「产物是什么类型」变成猜测。
        """
        for media_type, ext in EXT_BY_MEDIA_TYPE.items():
            if ext == self.extension:
                return media_type
        raise LookupError(f"扩展名 .{self.extension} 不在登记表里")


def extension_for_media_type(media_type: str) -> str | None:
    """媒体类型 → 扩展名；未登记返回 ``None``（由调用方决定报什么错）。"""
    return EXT_BY_MEDIA_TYPE.get(media_type)


def build_locator(sha256: str, extension: str) -> str:
    """按内容哈希与扩展名构造定位符。

    对 ``sha256`` 与 ``extension`` 都不做「尽力而为」的修补：长度不对、含非
    十六进制字符、扩展名未登记，一律 ``ValueError``。构造端宽松等于把非法值
    放进存储，而读取端迟早要拒绝它——那时错误现场已经离真正的原因很远。
    """
    digest = sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"sha256 必须是 64 位小写十六进制：{sha256!r}")
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"扩展名 .{extension} 不在登记表里；"
            f"已登记：{', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    return f"local://{digest[0:2]}/{digest[2:4]}/{digest}.{extension}"


def parse_locator(locator: str) -> tuple[ParsedLocator | None, str]:
    """解析定位符，返回 ``(结果, 失败原因)``；成功时原因为空串。

    刻意返回原因而不是抛异常：``packages`` 不允许依赖 ``adapters``
    （R1 分层），所以这里不能抛 ``StorageLocatorError``。调用方（存储适配器）
    把原因包进自己的异常类型，措辞不必再拼一次。
    """
    if not isinstance(locator, str) or not locator:
        return None, "定位符为空"
    match = _BASE_RE.match(locator)
    if match is None:
        return None, f"定位符不是受控形态（应为 {LOCATOR_SHAPE}）：{locator!r}"

    first, second, digest, extension = match.groups()
    if extension not in ALLOWED_EXTENSIONS:
        return None, (
            f"扩展名 .{extension} 不是登记的产物类型：{locator!r}；"
            f"已登记：{', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    if digest != digest.lower():
        # ``_BASE_RE`` 的字符类已限定小写，这条分支只在正则被放宽后才可达；
        # 保留它是为了让「放宽正则」这件事不会静默变成「接受大写哈希」。
        return None, f"定位符里的哈希必须是小写十六进制：{locator!r}"
    if (first, second) != (digest[0:2], digest[2:4]):
        return None, (
            f"定位符前两段必须由哈希派生（应为 {digest[0:2]}/{digest[2:4]}，"
            f"实际 {first}/{second}）：{locator!r}；"
            "这类定位符语法合法但永远读不到内容，属于拼写错误而非数据丢失"
        )
    # 与契约 pattern 求同一判据：上面几条分支是**原因分解**，这里才是终态判定。
    # 两处若因正则演进分叉，这条断言会在测试里立刻暴露，而不是在线上表现为
    # 「契约拒了、实现收了」。
    if _STRICT_RE.match(locator) is None:  # pragma: no cover - 防御性一致校验
        return None, f"定位符与契约 pattern 不一致（实现分叉）：{locator!r}"
    return ParsedLocator(sha256=digest, extension=extension), ""


def is_controlled_locator(locator: object) -> bool:
    """是否受控定位符。给「只判真假、不要原因」的场景用（如闸门测试）。"""
    if not isinstance(locator, str):
        return False
    return parse_locator(locator)[0] is not None


def validate_locator_or_raise(locator: str) -> str:
    """校验并原样返回定位符；非法时抛 ``ValueError``（消息含具体原因）。

    给 pydantic 的 ``field_validator`` 用。之所以不声明 ``pattern``：契约里的
    pattern 含 ``\\1\\2`` 反向引用（表达「前两段由哈希派生」），而 pydantic v2
    的正则引擎不支持反向引用。退化成「不含派生关系的正则」就等于两套标准：
    入口放行 ``local://ff/ee/<真实哈希>.png``，领域层再拒，而 HTTP 层给出的
    错误信息只说明「格式不对」。
    """
    parsed, problem = parse_locator(locator)
    if parsed is None:
        raise ValueError(f"storage_ref 非法：{problem}")
    return locator


# --------------------------------------------------------------------------- #
# 数据库层约束表达式
# --------------------------------------------------------------------------- #
# 定位符的**第三处表达**（Python 正则 / 契约 pattern / SQL CHECK）。三处的
# 等价关系不靠人核对，靠 ``tests/integration/test_storage_locator_constraint.py``
# 把同一批样本分别喂给代码判定与真实 INSERT——任一处走样都会红。
#
# 为什么 SQL 不用同一条正则：**PostgreSQL 的 POSIX 正则不支持反向引用**
# （``\1``）。若把 ``\1\2`` 去掉只留语法，库就放行「前两段与哈希不符」的
# 定位符——正是本次收敛要消灭的那种「语法合法、永远读不到」。所以 SQL 侧
# 用「正则判形态」+「位置算术判派生」两段合取。
_SCHEME = "local://"
_SEGMENT_LEN = 2  # 前两段各取哈希的 2 个十六进制字符
_DIGEST_LEN = 64

# 位置由结构算出，不写魔法数字。PostgreSQL 的 substr 是 1-based。
_SEG1_POS = len(_SCHEME) + 1  # 第一个 2 位段
_SEG2_POS = _SEG1_POS + _SEGMENT_LEN + 1  # 跳过分隔符 "/"
_DIGEST_POS = _SEG2_POS + _SEGMENT_LEN + 1

# 只在 SQL 侧使用的形态正则（不含派生关系，派生由下一个合取项负责）。
_SQL_SHAPE_PATTERN = (
    rf"^local://[0-9a-f]{{{_SEGMENT_LEN}}}/[0-9a-f]{{{_SEGMENT_LEN}}}"
    rf"/[0-9a-f]{{{_DIGEST_LEN}}}\.("
    + "|".join(sorted(ALLOWED_EXTENSIONS))
    + r")$"
)


def sql_locator_check(column: str = "storage_ref", digest_column: str | None = "sha256") -> str:
    """返回可用于 ``CHECK (...)`` 的 SQL 布尔表达式。

    ``column`` 由调用方传入而不是写死，是为了让同一份判定能用在
    ``asset_versions`` 之外的未来表上——写死列名会逼人复制一份表达式，
    而复制出来的第二份迟早与第一份不一致。

    ``digest_column`` 非空时额外断言「定位符内嵌哈希 == 该列」。这一条把
    「内容身份」压成单列：同一行不能一边声称内容是 A、一边指向 B。它能在
    SQL 里表达，因此不必只靠领域层——领域层只覆盖经过它的写入路径，
    worker / 运维脚本 / 未来的新用例都不经过它。

    传 ``None`` 用于「没有独立哈希列、哈希只存在于定位符里」的表。
    """
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", column):
        raise ValueError(f"列名必须是简单标识符：{column!r}")
    expression = (
        f"{column} ~ '{_SQL_SHAPE_PATTERN}'"
        f" AND substr({column}, {_SEG1_POS}, {_SEGMENT_LEN})"
        f" = substr({column}, {_DIGEST_POS}, {_SEGMENT_LEN})"
        f" AND substr({column}, {_SEG2_POS}, {_SEGMENT_LEN})"
        f" = substr({column}, {_DIGEST_POS + _SEGMENT_LEN}, {_SEGMENT_LEN})"
    )
    if digest_column is None:
        return expression
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", digest_column):
        raise ValueError(f"哈希列名必须是简单标识符：{digest_column!r}")
    # ``lower()`` 而不是直接比较：sha256 列允许大写（领域层归一，但库里可能
    # 有历史行未经归一），而定位符形态强制小写。两边都归一再比，判的是
    # 「是不是同一份内容」，不是「字符串是否逐字相同」。
    return (
        expression
        + f" AND substr({column}, {_DIGEST_POS}, {_DIGEST_LEN})"
        + f" = lower({digest_column})"
    )
