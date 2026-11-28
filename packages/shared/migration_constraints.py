"""迁移内的对象来源标记：只删除"确由本 revision 创建"的约束。

为什么需要
----------
``create_unique_constraint`` 常常带一个"已存在则复用"的守卫。降级时若直接
``DROP CONSTRAINT``，会把父 revision 本来就有的约束删掉；若用 ``IF EXISTS``
跳过，又会掩盖"升级确实创建了、降级漏删"的缺陷。两种做法都把状态不确定性
藏了起来。

这里用 PostgreSQL 自带的 ``COMMENT ON CONSTRAINT`` 作为**来源标记**：
升级创建后写入 ``wp04-created-by:<revision>``，降级只删除带同一标记、
且定义仍与升级时一致的约束。标记缺失（更早 revision 建的）→ 保留；
标记存在但定义不一致 → ``RAISE EXCEPTION`` 阻断（不做"按标记盲删"）。

为什么返回 SQL 而不是在这里调 ``op.execute``
------------------------------------------------------------
静态审计（``scripts/audit_identity_ddl.py``）会以"只解析、不连库"的方式加载
迁移模块；此时 Alembic 的 ``op`` 代理没有建立，在辅助模块里直接调用
``op.execute`` 会抛 ``NameError: proxy object has not yet been established``。
把语句作为字符串返回，由迁移文件在真实的 ``op`` 上下文里执行。
"""

from __future__ import annotations

from collections.abc import Sequence

MARK_PREFIX = "wp04-created-by:"


def mark_created_constraint(table: str, name: str, schema: str, revision: str) -> str:
    """返回给本 revision 新建约束打来源标记的 SQL（幂等）。"""
    return f"COMMENT ON CONSTRAINT {name} ON {schema}.{table} IS '{MARK_PREFIX}{revision}'"


def drop_marked_constraint(
    table: str,
    name: str,
    schema: str,
    revision: str,
    columns: Sequence[str],
) -> str:
    """返回"标记匹配且定义一致才删除"的 SQL。

    ``columns`` 是升级创建该唯一约束时声明的列顺序。降级时按
    ``pg_constraint.conkey`` 还原列序并逐字比较：标记对但列集变了，
    说明迁移文本与库状态已分叉，必须阻断而不是删掉一个"名字对、语义错"的对象。
    """
    marker = f"{MARK_PREFIX}{revision}"
    expected = ",".join(columns)
    return f"""
        DO $$
        DECLARE
            marker text;
            actual_columns text;
            actual_type "char";
        BEGIN
            SELECT d.description,
                   cols.actual_columns,
                   con.contype
            INTO marker, actual_columns, actual_type
            FROM pg_constraint con
            JOIN pg_description d
              ON d.objoid = con.oid
             AND d.classoid = 'pg_constraint'::regclass
            LEFT JOIN LATERAL (
                SELECT string_agg(a.attname, ',' ORDER BY u.ord) AS actual_columns
                FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
                JOIN pg_attribute a
                  ON a.attrelid = con.conrelid
                 AND a.attnum = u.attnum
            ) cols ON TRUE
            WHERE con.conname = '{name}'
              AND con.conrelid = '{schema}.{table}'::regclass;

            IF marker = '{marker}' AND actual_type = 'u' THEN
                IF actual_columns = '{expected}' THEN
                    EXECUTE format(
                        'ALTER TABLE %I.%I DROP CONSTRAINT %I',
                        '{schema}', '{table}', '{name}'
                    );
                ELSE
                    RAISE EXCEPTION
                        '约束 %.% / % 的列集与来源标记不一致（标记=%，实际=%），拒绝按标记删除',
                        '{schema}', '{table}', '{name}', '{expected}', actual_columns
                        USING ERRCODE = 'object_not_in_prerequisite_state';
                END IF;
            ELSIF marker = '{marker}' THEN
                RAISE EXCEPTION
                    '约束 %.% / % 不是 UNIQUE 约束（或类型已变），拒绝按标记删除',
                    '{schema}', '{table}', '{name}'
                    USING ERRCODE = 'object_not_in_prerequisite_state';
            ELSIF marker IS NOT NULL THEN
                RAISE NOTICE 'constraint %.% / % 非本 revision 创建（marker=%），保留',
                    '{schema}', '{table}', '{name}', marker;
            END IF;
        END $$;
        """
