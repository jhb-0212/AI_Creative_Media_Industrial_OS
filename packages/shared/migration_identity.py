"""迁移侧身份断言：只断言 bootstrap 已建好的角色存在，不做任何 DDL。

WP-02 的统一写法
----------------
历史迁移里 `CREATE ROLE ... ; ALTER ROLE ... PASSWORD ...` 的守卫块有三个问题：

1. **责任越界**：角色属性与口令的声明处已经搬到 ``role_specs.py`` + bootstrap，
   迁移再写一遍就是第二份事实源；
2. **可逆性假象**：upgrade 建角色，downgrade 就"应该"删角色——于是三个历史迁移
   都在 downgrade 里 ``DROP ROLE``，而角色是集群级对象，只要还有别的库在用就删
   不掉，整条降级链在真实集群上直接断（`c5d6e7f8a9b0` / `7a8b9c0d1e2f` /
   `9e3b4c5d6e7f` 三个点，实测见 P4 根因分析）；
3. **静默分叉**：``IF NOT EXISTS`` 守卫让"角色缺失"不再是错误——迁移自己造一个
   出来，部署侧到底该由 bootstrap 建角色还是迁移建，永远说不清。

因此统一改为：**只断言，缺失即失败**，且失败信息直接指向修复动作
（``scripts/bootstrap_identity.py --roles``）。角色属性、口令、成员关系仍归
bootstrap；迁移只保留自己的对象与授权。

为什么把 ``op`` 作为参数传进来
------------------------
迁移门禁（``scripts/boundary_assertions.py``）会回放迁移的 ``upgrade()``/
``downgrade()``，做法是把**迁移模块自己的** ``op`` 换成记录器。本模块如果在
import 期就绑死 ``alembic.op``，回放时就会去调未被建立的真实代理并直接
``NameError``——闸门红、却与迁移逻辑无关。显式传入 ``op`` 后，记录器与真实代理
两种情形都走同一条路径，闸门回放与真实执行一致。
"""

from __future__ import annotations

from typing import Protocol


class _OpLike(Protocol):
    """只要求 ``execute``，不绑定 alembic 的代理对象。"""

    def execute(self, statement: str) -> object: ...


def assert_role_present(role: str, *, op: _OpLike) -> None:
    """断言角色已由 bootstrap 创建；缺失时带修复指令失败。"""
    role_literal = role.replace("'", "''")
    message = (
        f'role "{role}" is missing: run scripts/bootstrap_identity.py --roles first'
    ).replace("'", "''")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_literal}') THEN
                RAISE EXCEPTION '{message}';
            END IF;
        END
        $$
        """
    )


#: 受控降级入口的显式确认开关。值必须是**这次要恢复的 revision id**，
#: 不接受 `=1` 这类"打开就行"的写法——那会让人在生产上顺手打开一次就忘。
RETIRED_RESTORE_ENV = "AUTHORIZE_RETIRED_ROLE_RESTORE"


def restore_retired_role(role: str, *, op: _OpLike) -> None:
    """把退役角色按**历史定义**恢复；任何前置条件缺失都失败关闭。

    为什么不是一段 `CREATE ROLE ... PASSWORD ...`
    --------------------------------------------
    历史迁移的 downgrade 各自写死属性，恢复出来的是"当时那个人记得的属性"，
    而不是"退役前真实是什么"。这里改为读 `role_specs.RETIRED_ROLES` 的
    `historical_attributes`：属性、成员关系、口令字段名都从同一份声明来，
    降级回到的是那个 revision 的身份状态，不是今天的默认配置。

    五条硬约束在这里的落点：
    1. 声明来源唯一 —— 只读 `retired_roles_by_name()`；
    2. 退役身份不在 `role_specs()`，Bootstrap 不会建它，只有本函数会恢复；
    3. 属性/成员关系只按历史值设，不做"顺手补齐"；
    4. 口令从 `settings.<字段名>` 取，缺失或为空即抛错（不落任何生成物）；
    5. 必须 `AUTHORIZE_RETIRED_ROLE_RESTORE=<revision>` 显式确认，并 `RAISE WARNING`。

    失败类别与行为：
    - 未授权（环境变量不等于该 revision）→ `RuntimeError`，不执行任何 DDL；
    - 未登记为可恢复（`never_restore`）→ `RuntimeError`；
    - 凭据字段缺失/空值 → `RuntimeError`；
    - 历史属性出现提权位 → `RuntimeError`（声明层已被 `retired_role_problems` 拦，
      这里再挡一次，防止调用方绕过校验）。
    """
    import os

    from packages.shared.config import get_settings
    from packages.shared.role_specs import retired_roles_by_name

    contract = retired_roles_by_name().get(role)
    if contract is None:
        raise RuntimeError(
            f'role "{role}" 不在 RETIRED_ROLES：不是退役身份，禁止走恢复路径'
        )
    if contract.downgrade_restore_policy != "restore_on_downgrade":
        raise RuntimeError(
            f'role "{role}" 的 downgrade_restore_policy='
            f"{contract.downgrade_restore_policy}：该身份不支持自动恢复"
        )

    authorized = os.environ.get(RETIRED_RESTORE_ENV, "").strip()
    if authorized != contract.retired_at_revision:
        raise RuntimeError(
            f'恢复退役身份 "{role}" 需要显式确认：设置 '
            f"{RETIRED_RESTORE_ENV}={contract.retired_at_revision} 后重跑降级。"
            "这是集群级身份重建，不允许在普通降级里静默发生"
        )

    attrs = contract.historical_attributes
    for flag in ("superuser", "createdb", "createrole", "bypassrls"):
        if attrs.get(flag):
            raise RuntimeError(
                f'role "{role}" 的历史属性声明了提权位 {flag}=True：拒绝恢复'
            )

    login = bool(attrs.get("login", False))
    settings = get_settings()
    password_sql = ""
    if login:
        setting_name = attrs.get("password_setting") or (
            contract.credential_source or ""
        ).removeprefix("settings.")
        if not setting_name:
            raise RuntimeError(
                f'role "{role}" 是 LOGIN 但没有口令来源：降级恢复 fail-closed'
            )
        secret = getattr(settings, str(setting_name), None)
        if secret is None:
            raise RuntimeError(
                f'role "{role}" 的口令来源缺失：Settings 上没有 {setting_name}'
            )
        value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
        if not value:
            raise RuntimeError(
                f'role "{role}" 的口令来源为空：拒绝用空口令恢复登录身份'
            )
        password_sql = " PASSWORD '" + str(value).replace("'", "''") + "'"

    role_literal = role.replace("'", "''")
    no_escalation = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
    verb = "LOGIN" if login else "NOLOGIN"
    # 告警与 DDL 放在同一 DO 块：恢复动作本身必须留下可见告警，
    # 不能"先恢复、再指望某条日志被看到"。
    op.execute(
        f"""
        DO $$
        BEGIN
            RAISE WARNING '恢复退役身份 %（退役于 %）：降级正在重建集群级登录角色，'
                '请确认这是预期操作', '{role_literal}', '{contract.retired_at_revision}';
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_literal}') THEN
                CREATE ROLE {role_literal} {verb} {no_escalation}{password_sql};
            ELSE
                ALTER ROLE {role_literal} WITH {verb} {no_escalation}{password_sql};
            END IF;
        END
        $$
        """
    )

    # 成员关系按历史定义收敛：历史是零成员，就不能因为"默认配置里有 operator"
    # 而把权限组加回去；反之历史声明了成员，也必须补回。
    from packages.shared.db_roles import maintenance_roles

    operator_group = maintenance_roles().get("operator")
    if operator_group:
        operator_literal = operator_group.replace("'", "''")
        if operator_group in contract.member_of:
            op.execute(f"GRANT {operator_literal} TO {role_literal}")
        else:
            op.execute(f"REVOKE {operator_literal} FROM {role_literal}")

    for group in contract.member_of:
        if group == operator_group:
            continue
        group_literal = group.replace("'", "''")
        op.execute(f"GRANT {group_literal} TO {role_literal}")

