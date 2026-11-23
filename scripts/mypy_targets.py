"""mypy 检查目录的**唯一事实源**：从 ``architecture_check.LAYERS`` 派生。

为什么不能各写一份清单
----------------------
CI 的 workflow 写 ``packages services apps workers``、本地合并闸门再写一遍，
两份清单在「新增一层」时都不会报错——它们只是**漏掉**。而漏掉的层默认没有
类型保护，也没有任何信号：mypy 不跑它，就不会有人发现它的类型已经漂了。

本轮的实际触发点：``adapters/`` 落地了模型网关与存储实现，而 CI 仍写着
「adapters 为占位」，于是新代码天然没有类型检查。这类缺口不是靠"下次记得改"
解决的，只能让清单派生。

派生规则
--------
取 ``LAYERS`` 中**实际含 .py** 的目录。``agents/`` 目前只有 ``.gitkeep``，
对它跑 mypy 会得到 ``no .py files to check`` 的非零退出码，看起来像类型错误
而实际是空目录——所以按「有代码」过滤，而不是按「是不是一个层」过滤。
"""

from __future__ import annotations

import pathlib

try:
    # 作为脚本被调用时（``python scripts/mypy_targets.py`` / merge_readiness 内），
    # sys.path[0] 是 scripts/ 本身，ARCHITECTURE_CHECK 是顶层模块。
    from architecture_check import LAYERS
except ImportError:  # pragma: no cover - 取决于调用方式
    # 作为包模块被 import 时（测试、其它从仓库根启动的脚本），走包内路径。
    from scripts.architecture_check import LAYERS

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def mypy_targets() -> tuple[str, ...]:
    """返回应交给 mypy 检查的目录名（按 LAYERS 顺序，稳定可复现）。"""
    targets: list[str] = []
    for layer in LAYERS:
        directory = _REPO_ROOT / layer
        if not directory.is_dir():
            continue
        if any(directory.rglob("*.py")):
            targets.append(layer)
    return tuple(targets)


if __name__ == "__main__":
    print(" ".join(mypy_targets()))
