"""模型网关端口：协议无关的生成契约 + 失败二分。

为什么要有一个「协议无关」的端口
--------------------------------
生成 Worker 的职责是「拿一个任务、产出可入库的字节」。它不该知道对面是
OpenAI 兼容的 ``/images/generations``，还是某厂商自研的同步接口。把协议细节
留在适配器里，切换网关时改动面就只有 ``adapters/``
一个目录；否则每次换厂商都要动 Worker，而 Worker 是唯一同时持有租约与终态的
组件——动它就有丢任务的风险。

失败必须二分，混成一个就会两头出错
----------------------------------
| 失败性质 | 例子 | 若混同的后果 |
|---|---|---|
| 瞬时（可能自愈） | 超时、连接重置、5xx、429 | 记为永久失败 ⇒ 任务白丢，需要人工捞 |
| 确定性（不会自愈） | 参数非法、内容被拒、模型不存在 | 记为可重试 ⇒ 无限重试，持续烧钱 |

两类对「要不要再跑一次」给出相反答案，所以必须是两个类型，不能靠
``except Exception`` 之后的字符串猜。

第三类（配置矛盾）单独列出：它不是「这次调用失败」，而是「这个进程根本不该
开始调用」。把它和瞬时失败混在一起，会出现「配置错了却在重试」的空转。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class GatewayError(Exception):
    """网关端口失败的基类。调用方只应捕获这一族异常。"""


class GatewayRetryableError(GatewayError):
    """瞬时失败：换一次时机可能就成功（超时 / 5xx / 429 / 连接问题）。"""


class GatewayRejectedError(GatewayError):
    """确定性失败：同样的请求再发一次结果不会变（4xx / 内容被拒 / 产物非法）。"""


class GatewayMisconfiguredError(GatewayError):
    """配置矛盾：缺 base_url、缺 api_key、协议不一致等。

    与「调用失败」的区别是**发生在发请求之前**，因此不消耗配额、也不该被
    当成任务失败去重试——正确处置是让进程暴露问题（启动即失败或诚实失败），
    而不是在运行期反复试。
    """


@dataclass(frozen=True)
class GenerationRequest:
    """一次生成请求。字段只保留「换任何网关都需要」的部分。"""

    task_id: str
    prompt: str
    model: str
    media_type: str
    size: str = ""
    # 厂商特有参数走 extra 透传：把它拆成具体字段会让端口随厂商漂移，
    # 而不给透传又会逼适配器去读全局配置——那是另一种耦合。
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayArtifact:
    """网关返回的产物字节 + 对账信息。

    刻意**不带**存储定位符：产物先要有字节，落地是下一步的事。让适配器直接
    返回 ``storage_ref`` 会让「存储在哪」变成网关适配器的知识，而它是存储端口
    的知识。
    """

    data: bytes
    media_type: str
    declared_sha256: str = ""
    # 上游请求 id：对账与排障用。没有它时「这笔钱花在哪个任务上」无法回溯。
    upstream_request_id: str = ""


class ModelGatewayPort(Protocol):
    """模型网关端口：一个方法，一个产物。"""

    async def generate(self, request: GenerationRequest) -> GatewayArtifact:
        """生成一份产物。

        失败必须抛本模块的两个二分类型之一（或 ``GatewayMisconfiguredError``），
        不得把 ``httpx`` / ``requests`` 的异常直接漏给调用方——否则「换网关」
        会波及所有 ``except`` 分支。
        """
        ...
