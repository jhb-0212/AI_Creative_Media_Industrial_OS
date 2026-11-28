"""版本化提示词模板（ADR-R47，P2）。

提示词是 ``ShotSpec`` 的派生输出，而不是存储事实：ShotSpec 里没有 prompt 字段，
也不允许 Prompt 对象存储（ADR-R47 拒绝清单）。派生必须来自**代码内版本化模板**：

- 模板只读 ShotSpec 的结构化字段（镜头运动 / 主体调度 / 景别变化 / 画幅）；
- 同一份 ShotSpec + 同一 ``template_version`` ⇒ 同一 ``prompt_text``；
- 改模板文字**必须**升版本（v1 已冻结进 ``GenerationSpec.template_version``，
  测试钉住 ``template_digest()``）。已冻结的规格永远按自己记录的版本解释。

为什么不是 Jinja / 自由格式化
----------------------------
模板一旦允许自由插值，就没人能证明「同输入同输出」。这里用**显式字段拼接**：
字段顺序、分隔符、空字段省略规则都写死在版本分支里，跨解释器/跨平台稳定。
"""

from __future__ import annotations

import hashlib

TEMPLATE_VERSION = "afs-shot-spec-v1"

#: 拼接顺序与中文标签。顺序是身份的一部分：调整顺序等于改模板，必须升版本。
_FIELDS: tuple[tuple[str, str], tuple[str, str], tuple[str, str], tuple[str, str]] = (
    ("camera_path", "镜头运动"),
    ("subject_paths", "主体调度"),
    ("shot_size_changes", "景别变化"),
    ("aspect_ratio", "画幅比例"),
)

_SENTENCE_END = "。"


def template_digest() -> str:
    """模板内容的稳定摘要（测试用来钉住「改文字必须升版本」）。"""
    payload = f"{TEMPLATE_VERSION}|" + "|".join(f"{name}:{label}" for name, label in _FIELDS)
    return hashlib.sha256((payload + "|" + _SENTENCE_END).encode("utf-8")).hexdigest()


def render_prompt(shot_spec: object) -> str:
    """把 ShotSpec 的结构化字段渲染成提示词正文。

    空字段省略而不是写「无」：把「没有调度」写成一句话会改变模型的理解，而
    「省略」在提示词语义上就是「不约束」。四个字段全空时 fail-closed——没有
    任何可渲染内容时强行拼一句空话，等于生成一张与意图无关的图却全链路无错。
    """
    parts: list[str] = []
    for name, label in _FIELDS:
        value = str(getattr(shot_spec, name, "") or "").strip()
        if value:
            parts.append(f"{label}：{value}")
    if not parts:
        raise ValueError("shot-spec-has-no-renderable-fields")
    return "；".join(parts) + _SENTENCE_END


__all__ = ["TEMPLATE_VERSION", "render_prompt", "template_digest"]
