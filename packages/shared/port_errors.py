"""仓储端口的失败契约：端口声明自己的失败语义，不把 ORM 异常泄漏给内核。

根因（第二轮审查 P2-03）
------------------------------------------------------------
整改前服务层有四处 ``except IntegrityError``（项目 / 集数 / 剧本 / 资产），
这意味着**端口没有失败契约**，调用方只能借适配器所用库的异常来推断失败。
两个后果都是仓库里实测存在的，不是推演：

1. **端口不可替换**：``InMemoryProjectRepository`` 为了配合服务层，必须伪造
   ``sqlalchemy.exc.IntegrityError("INSERT", {}, ...)``。一个纯内存实现被迫依赖
   ORM——这不是"测试方便"，而是端口契约缺失的直接体证。
2. **语义被压平**：``IntegrityError`` 同时覆盖唯一冲突（SQLSTATE 23505）、外键
   冲突（23503）、CHECK / NOT NULL 违反（23514 / 23502）。服务层把"任何完整性
   错误"都当"同一 id 重复提交"，于是外键失败（引用的项目刚被并发删除）会被降级
   成幂等返回把错误吞掉，而不是 fail-closed 地暴露"你的引用不成立"。

契约（端口层唯一事实源）
------------------------------------------------------------
- ``DuplicateEntityError``：**唯一 / 主键冲突**（23505）。这是服务层唯一允许
  捕获并降级为"回读 + 幂等返回"的失败——因为冲突本身就证明目标行已存在。
- ``ReferentialIntegrityError``：其他完整性失败（外键 / CHECK / NOT NULL）。
  它表示调用方给的关系或值不成立，**不可**幂等、**不可**重试，直接上抛。

``classify_integrity_error`` 是给适配器用的翻译器：只有适配器知道自己在用什么
驱动，因此也只有它能做这个判定。它惰性导入 SQLAlchemy，保证本模块的异常类型
在零 ORM 环境下可 import（内存适配器、领域单测、契约闸门都只依赖异常类型）。
"""

from __future__ import annotations

from typing import Any

# PostgreSQL 唯一约束违反的 SQLSTATE（asyncpg / psycopg / psycopg2 同值）。
UNIQUE_VIOLATION_SQLSTATE = "23505"

# 驱动对象上可能携带 SQLSTATE 的属性名（按优先级）。asyncpg 用 sqlstate，
# psycopg2 用 pgcode，老版本 / 包装层还可能是 code。
_SQLSTATE_ATTRS: tuple[str, ...] = ("sqlstate", "pgcode", "code")


class RepositoryContractError(Exception):
    """仓储端口失败契约的基类。

    服务层只允许捕获这个基类的两个具名子类；捕获基类本身也意味着"我不区分
    失败语义"，那等于把压平语义的问题换个地方重演，因此两个子类各自有明确
    的可恢复性（见 docstring）。
    """

    retryable: bool = False
    idempotent_recoverable: bool = False


class DuplicateEntityError(RepositoryContractError):
    """唯一 / 主键冲突：目标实体已存在。

    语义：**可幂等恢复**。服务层应回读既有记录并判定"同内容 → 返回原结果、
    不同内容 → IDEMPOTENCY_CONFLICT"，而不是把它当内部错误。
    """

    idempotent_recoverable = True

    def __init__(self, entity_id: str, *, constraint: str = "") -> None:
        self.entity_id = entity_id
        self.constraint = constraint
        suffix = f"（唯一约束 {constraint}）" if constraint else ""
        super().__init__(f"实体 {entity_id} 已存在：唯一/主键冲突{suffix}")


class ReferentialIntegrityError(RepositoryContractError):
    """引用 / 取值不成立（外键、CHECK、NOT NULL 等）。

    语义：**不可幂等、不可重试**。它说明调用方给出的关系或值在数据库层不成立
    （例如引用的父实体已被并发删除），必须 fail-closed 上抛——把它降级成
    "重复提交、返回既有记录"会把真实的数据一致性问题藏在幂等语义后面。
    """

    def __init__(
        self, entity_id: str, *, constraint: str = "", sqlstate: str = ""
    ) -> None:
        self.entity_id = entity_id
        self.constraint = constraint
        self.sqlstate = sqlstate
        detail = f" SQLSTATE={sqlstate}" if sqlstate else ""
        suffix = f" 约束={constraint}" if constraint else ""
        super().__init__(
            f"实体 {entity_id} 违反引用完整性{detail}{suffix}："
            "引用关系或取值不成立，不可按幂等处理"
        )


def _sqlstate_of(exc: BaseException) -> str:
    """从驱动异常里取 SQLSTATE（取不到返回空串，由调用方走保守分支）。"""
    orig: Any = getattr(exc, "orig", exc)
    for attr in _SQLSTATE_ATTRS:
        value = getattr(orig, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _text_of(exc: BaseException) -> str:
    """异常链的可搜索文本（包装层与原始驱动异常都要看）。

    只看 ``str(exc)`` 会漏：SQLAlchemy 包装后的消息往往是
    ``(builtins.Exception) duplicate key ...`` 这类形式，真正的原因在 ``orig``。
    """
    parts = [str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(str(orig))
    return " ".join(parts).lower()


def classify_integrity_error(
    exc: BaseException, *, entity_id: str, constraint: str = ""
) -> RepositoryContractError:
    """把驱动的完整性异常翻译成端口契约异常（返回实例，由调用方 raise）。

    返回而不直接 raise：适配器要 ``raise classified from exc`` 保留驱动原始
    因果链，异常可读性不能因为翻译而丢失。

    判定顺序：SQLSTATE 优先；取不到 SQLSTATE 时退回文本特征（"duplicate key"），
    两者都不匹配即按"引用完整性"保守处理——**保守方向必须是"不幂等"**，
    否则偶发的外键失败会被误降级成幂等返回。
    """
    state = _sqlstate_of(exc)
    if state == UNIQUE_VIOLATION_SQLSTATE or (
        not state and "duplicate key" in _text_of(exc)
    ):
        return DuplicateEntityError(entity_id, constraint=constraint)
    return ReferentialIntegrityError(
        entity_id, constraint=constraint, sqlstate=state
    )
