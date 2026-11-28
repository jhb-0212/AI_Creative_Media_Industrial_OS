"""asset_versions.storage_ref：自由 JSON 引用 → 受控内容寻址定位符（P1-07 / #179）

问题
----
``storage_ref`` 此前是 ``jsonb``，契约里 ``AssetVersion`` 声明为自由 object、
``AssetReference`` 声明为裸 string——同一个概念两种类型，且都不约束取值。
后果不是「数据脏一点」：

- 调用方可声明 ``{"path": "../../etc/passwd"}``，消费方（QC 检测、交付打包）
  照着读；
- 同一 ``storage_ref`` 可先后指向不同内容 ⇒「资产不可覆盖」在存储层没有
  任何结构性保障；
- ``sha256`` 列与 ``storage_ref`` 里的哈希可以互相矛盾，没有一处会说话。

本迁移把它收敛为唯一的受控形态::

    local://<sha256 前2位>/<sha256 第3-4位>/<完整 sha256>.<扩展名>

形态固定为内容寻址后，路径穿越不可表达、覆盖只能退化成新增、写入天然幂等
——三条都成为结构性事实，不靠校验。

为什么迁移**不**替存量猜测
--------------------------
存量里可能出现旧形态 ``{"uri": "s3://bucket/x.png"}``。有三种「顺手」的做法，
都不能采用：

1. **直接搬字符串**：把 ``s3://...`` 塞进 text 列，缺口一点没变（自由引用换
   了个类型），而且会让 CHECK 无法建立。
2. **按 sha256 派生**：``local://xx/yy/<hash>.ext`` 语法全合规、CHECK 全绿，
   但它指向的内容很可能不存在——这是把脏数据洗成「看起来合规」，比留着
   脏数据更危险：绿灯下的红灯没有人会去看。
3. **置空**：已 APPROVED / GOLD 的资产会变成「已批准但无内容」，制造一个
   比原问题更严重的矛盾。

所以本迁移只做**能确定的转换**（``{"uri": "<合法定位符>"}``），遇到其余
存量一律 fail-closed 并列出明细。迁移不是业务裁定者：这些行是「内容真实
存在、只是引用格式旧」还是「空壳资产」，只有人能回答。

唯一键 ``uq_projects_entity_tenant`` 与归属外键与本迁移无关，动的是内容
寻址这一维度。

约束表达式的三处对应
--------------------
PostgreSQL 的 POSIX 正则**不支持反向引用**（``\\1``），因此 SQL 侧用
「形态正则 + 位置算术」两段合取表达与 Python / 契约 pattern 相同的判据。
位置由 ``local://`` + 2 + ``/`` + 2 + ``/`` + 64 的固定结构算出（1-based）：
段1 在 9、段2 在 12、哈希起点在 15、扩展名在 79。三处等价性由
``tests/integration/test_storage_locator_constraint.py`` 用同一批样本锁定。

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None

_TABLE = "asset_service.asset_versions"
_CONSTRAINT = "ck_asset_versions_storage_ref_locator"

# 与 packages/shared/storage_locator.py 的 LOCATOR_PATTERN 同源（该正则含
# 反向引用，PG 不支持，故此处拆成两段）。扩展名枚举与登记表一致。
_SHAPE = (
    r"^local://[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\."
    r"(bin|jpg|mp4|png|wav|webm|webp)$"
)

# 存量可转换的判据：有 uri 键 ∧ 是字符串 ∧ 满足受控形态 ∧ 前两段由哈希派生
# ∧ 内嵌哈希等于 sha256 列。与实现判定逐项对应，避免「迁移放行的值，运行时
# 拒绝」这种两套标准；尤其最后一条——它判的是「这一行是不是自相矛盾」。
_CONVERTIBLE = f"""
    storage_ref ? 'uri'
    AND jsonb_typeof(storage_ref -> 'uri') = 'string'
    AND (storage_ref ->> 'uri') ~ '{_SHAPE}'
    AND substr(storage_ref ->> 'uri', 9, 2) = substr(storage_ref ->> 'uri', 15, 2)
    AND substr(storage_ref ->> 'uri', 12, 2) = substr(storage_ref ->> 'uri', 17, 2)
    AND substr(storage_ref ->> 'uri', 15, 64) = lower(sha256)
"""

_CHECK_EXPRESSION = (
    f"storage_ref ~ '{_SHAPE}'"
    " AND substr(storage_ref, 9, 2) = substr(storage_ref, 15, 2)"
    " AND substr(storage_ref, 12, 2) = substr(storage_ref, 17, 2)"
    " AND substr(storage_ref, 15, 64) = lower(sha256)"
)


def upgrade() -> None:
    # 存量体检用 DO 块在**数据库里**判，而不是 Python 读出来再判断。
    #
    # 两个理由：①这条判据的对象是库里的数据，判定离数据越近越不会说谎（Python
    # 侧读到的行与随后 DDL 看到的行之间隔着网络往返与隔离级别）；②迁移必须是
    # **可静态回放**的——闸门（boundary_assertions）会把 op 换成一个只收集 SQL
    # 的替身来读「这份迁移会执行什么」，任何在 upgrade() 里读结果集的写法都会
    # 让回放崩在 AttributeError，而闸门会把崩溃报成「无法加载迁移」，指向错误
    # 的排查方向。
    op.execute(
        f"""
        DO $storage_ref_guard$
        DECLARE
            total_rows bigint;
            bad_rows   bigint;
            samples    text;
        BEGIN
            SELECT count(*) INTO total_rows FROM {_TABLE};
            SELECT count(*) INTO bad_rows FROM {_TABLE} WHERE NOT ({_CONVERTIBLE});
            IF bad_rows > 0 THEN
                SELECT string_agg(
                           format('    %s [%s] %s', entity_id, status, storage_ref::text),
                           E'\\n')
                  INTO samples
                  FROM (SELECT entity_id, status, storage_ref
                          FROM {_TABLE}
                         WHERE NOT ({_CONVERTIBLE})
                         LIMIT 5) AS probe;
                RAISE EXCEPTION USING
                    MESSAGE = format(
                        'asset_versions 有 %s/%s 行的 storage_ref 无法转换成受控定位符，'
                        '迁移在此停下（不猜业务语义）',
                        bad_rows, total_rows),
                    DETAIL = coalesce(samples, ''),
                    HINT = '处置（择一，均由人决定）：a) 内容真实存在、只是引用格式旧：'
                           '用 packages.shared.storage_locator.build_locator(sha256, 扩展名) '
                           '重算后写回（必须与 sha256 列一致）；b) 内容不存在（空壳资产）：'
                           '这类行不该以资产身份存在，备份后删除。'
                           '禁止按 sha256 批量派生定位符——那会造出语法合规但指向'
                           '不存在内容的假数据，CHECK 全绿而产物读不出来。';
            END IF;
        END
        $storage_ref_guard$;
        """
    )

    # 类型收窄。USING 明确取 uri 值：不做 COALESCE 到空串（空串会作为一个
    # 「长度合法但形态非法」的值留在库里，之后只在 CHECK 处失败，错误现场
    # 离真正原因更远）。
    op.alter_column(
        "asset_versions",
        "storage_ref",
        schema="asset_service",
        type_=sa.String(length=96),
        existing_type=sa.dialects.postgresql.JSONB(),
        existing_nullable=False,
        postgresql_using="(storage_ref ->> 'uri')",
    )

    # NOT VALID → VALIDATE 两阶段：先在轻锁下挂上（此后新写入即受检），再
    # 全表校验。存量已在上方逐行确认可转换，因此 VALIDATE 必然通过——若它
    # 失败，说明转换与判定不一致，这种分叉必须在这里暴露而不是留到线上。
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK ({_CHECK_EXPRESSION}) NOT VALID"
    )
    op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_CONSTRAINT}")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    # 回到自由 jsonb 形态：把定位符包回 {"uri": ...}，与旧消费方的读法一致。
    # 不保留「曾经是受控形态」的痕迹——downgrade 的语义是「回到旧结构」，
    # 留一个混合形态会让旧代码读到它不认识的值。
    op.alter_column(
        "asset_versions",
        "storage_ref",
        schema="asset_service",
        type_=sa.dialects.postgresql.JSONB(),
        existing_type=sa.String(length=96),
        existing_nullable=False,
        postgresql_using="jsonb_build_object('uri', storage_ref)",
    )
