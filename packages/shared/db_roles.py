"""数据库运行角色与业务域的映射（唯一事实源）。

为什么需要这个模块（根因）：
- 迁移 ``9e3b4c5d6e7f`` 建立了单一 ``app_role``，并对每个 Schema
  ``GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES``。结果是 API、Outbox
  Relay、分区维护共用同一角色：**任何一处代码都能写任何一张表**，模块边界
  只是 Python 目录结构，在数据库层不存在。
- 目录分层（``architecture_check``）只能约束 ``import``，拦不住「asset 模块
  拼一条 SQL 去写 project_planning.projects」。要拦这件事，只能让越权写入
  在 PostgreSQL 层面失败。

设计边界（刻意的取舍）：
- 本模块**只**描述「业务域 → 数据库角色名」。具体 GRANT 语句在迁移里，
  不在代码里生成——授权是数据库状态，必须能通过 ``alembic history`` 追溯，
  在应用启动时"顺手补授权"会让权限来源不可审计。
- 因此这里没有 ``writable_schemas`` 之类的字段：那会与迁移里的真实 GRANT
  形成第二份事实源，两份迟早漂移，而漂移的表现是"测试通过、线上越权"。

新增一个域时要做三件事（缺一即 fail-closed）：
1. 在此处加枚举值并登记角色名（否则连接没有角色可用，启动即失败）；
2. 加迁移创建角色并精确授权（否则该域连自己的表都读不到，测试立刻红）；
3. 把该域加入 ``partition_guard`` 的分区回收名单（否则新角色能直连分区）。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class DatabaseDomain(StrEnum):
    """业务域的数据库运行身份（每个域一个独立 PostgreSQL 角色）。

    ``StrEnum`` 的取值同时是"域标识"与日志/指标里的标签，避免再引入一套
    数字编号或缩写（缩写是排障时的额外翻译成本）。
    """

    # 项目/集数/剧本（services.project_planning）
    PROJECT_PLANNING = "project_planning"
    # 资产版本与 QC 报告（services.asset_service）
    ASSET = "asset"
    # Gate 决策与质量治理裁决（services.qc_governance）
    #
    # 为什么 Gate 决策不复用 ``ASSET`` 角色：Gate 是**对人类职责主体的授权裁决**，
    # 资产域角色代表"生成/检测流水线"。若两者共用身份，资产写入路径就同时握有
    # 裁决写入权——「Agent 不得自批自用」在数据库层失去最后一道边界。
    QC_GOVERNANCE = "qc_governance"
    # Outbox 投递（workers.relay）——只能读事件、回写投递状态
    OUTBOX_RELAY = "outbox_relay"
    # 身份与吊销（services.identity_access）——只能读写 auth schema
    IDENTITY = "identity"


# 域 → PostgreSQL 角色名。
#
# 命名约定 ``svc_<domain>``：前缀表达"服务账号"，与人类运维账号区分；
# 角色名是数据库对象名，一经上线不可随意改名（改名等于重建角色 + 重授权），
# 因此这里集中定义，避免迁移与代码各写一份字符串拼错。
_DOMAIN_ROLES: Final[Mapping[DatabaseDomain, str]] = MappingProxyType(
    {
        DatabaseDomain.PROJECT_PLANNING: "svc_project_planning",
        DatabaseDomain.ASSET: "svc_asset",
        DatabaseDomain.QC_GOVERNANCE: "svc_qc_governance",
        DatabaseDomain.OUTBOX_RELAY: "svc_outbox_relay",
        DatabaseDomain.IDENTITY: "svc_identity",
    }
)


def role_for(domain: DatabaseDomain) -> str:
    """返回业务域对应的数据库角色名。

    未登记的域直接 ``KeyError``（而不是回退到某个默认角色）：回退会让新域
    悄悄以旧角色身份运行，正是本模块要消灭的那类"看起来能用"的越权路径。
    """
    try:
        return _DOMAIN_ROLES[domain]
    except KeyError as exc:  # pragma: no cover - 枚举与映射不一致属编码期错误
        raise KeyError(
            f"业务域 {domain!r} 未登记数据库角色：请在 _DOMAIN_ROLES 增补"
        ) from exc


def all_business_roles() -> tuple[str, ...]:
    """全部业务域角色名（排序后返回，供分区回收等"遍历所有应用角色"的场景）。

    排序只为让生成的 SQL 与测试断言稳定可读，不代表权限优先级。
    """
    return tuple(sorted(_DOMAIN_ROLES.values()))


__all__ = [
    "DatabaseDomain",
    "all_business_roles",
    "role_for",
]
