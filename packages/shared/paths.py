"""仓库内资源定位（契约文件、静态数据）——唯一事实源。

为什么不让每处各自写 ``Path(__file__).resolve().parents[N]``
------------------------------------------------------------
``N`` 取决于文件所在层级：``packages/shared/x.py`` 是 2，``services/a/b/c.py``
是 3。抄一份注释、改一次目录层级，索引就静默指错——而这种错通常表现为
「运行到某个分支才 500」，不是启动即失败。D6-9 的实现里刚踩过一次
（``services/outbox_ops/service/dead_letter_service.py`` 照抄了
``services/identity_access/action_registry.py`` 的 ``parents[2]``，多一层目录，
直接指向了不存在的 ``services/contracts/``）。

因此：仓库根的推导只在这里发生一次；其余模块按**契约相对路径**取文件，
取不到时立刻给出可读的错误（而不是让调用方在别处看到 ``None``）。
"""

from __future__ import annotations

from pathlib import Path

# packages/shared/paths.py → parents[0]=shared, [1]=packages, [2]=仓库根。
REPO_ROOT = Path(__file__).resolve().parents[2]

CONTRACTS_DIR = REPO_ROOT / "contracts"


class RepoResourceMissingError(FileNotFoundError):
    """仓库内资源缺失：属于部署/打包错误，不是业务异常。"""


def contract_file(*parts: str) -> Path:
    """按 ``contracts/`` 下的相对路径取文件；不存在即报错（含绝对路径）。

    不用 ``Path.exists()`` 静默兜底：契约文件是编译期事实源，缺失意味着
    当前进程不该继续工作（例如从错误的工作目录启动、或打包漏了 contracts）。
    """
    path = CONTRACTS_DIR.joinpath(*parts)
    if not path.is_file():
        raise RepoResourceMissingError(f"契约文件缺失: {path}")
    return path
