# D4-3 决策包：`f2b3c4d5e6a7` 在迁移内销毁集群级角色

> 本文件是**待 Owner 裁决**的决策包，不是裁决结果。分类在矩阵中登记为
> `unproven_blocking`，闸门因此保持阻断，D5 不得准入。

## 一、事实（实测与读码，均已复核）

1. `f2b3c4d5e6a7_qc_worker_identity_retirement.py` 的 `upgrade()` 执行：

   ```sql
   DO $$ DECLARE dependency_count integer; BEGIN
     IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'qc_worker') THEN RETURN; END IF;
     SELECT count(*) INTO dependency_count FROM pg_shdepend d JOIN pg_roles r ON r.oid = d.refobjid
       WHERE d.refclassid = 'pg_authid'::regclass AND r.rolname = 'qc_worker';
     IF dependency_count > 0 THEN RAISE EXCEPTION '...拒绝 DROP...'; END IF;
     EXECUTE format('DROP ROLE %%I', 'qc_worker');
   END $$;
   ```

   —— 确实有依赖守卫（`pg_shdepend` 计数 > 0 即拒绝），但动作本身是**销毁集群级登录角色**。

2. `downgrade()` 调 `restore_retired_role()`：读取 `role_specs.RETIRED_ROLES` 的历史属性重建，
   **必须** `AUTHORIZE_RETIRED_ROLE_RESTORE=f2b3c4d5e6a7` 才执行，否则 `RuntimeError`（fail-closed）。
   实测未授权时降级整条失败（`triage_9e3b-f2b3.log` 第 15–32 行）。

3. **矩阵此前对这条完全失明**：`reversibility_matrix.parse_actions` 只认字面
   `DROP ROLE <name>`，而这里是 `EXECUTE format('DROP ROLE %%I', 'qc_worker')`（还是双百分号转义），
   解析结果为空 → 升级侧"没有动作" → 只剩降级那条 `CREATE ROLE qc_worker`，
   被"已登记退役"规则洗成 note → 判 `symmetric`。归档矩阵即如此（`matrix-pre-fix-backup.json`）。
   本轮修了解析器（含 `%%?I` 形式），该 revision 立刻变成 asymmetric：

   - `RISK:升级在迁移中销毁集群级角色 qc_worker`
   - `RISK:升级退役身份 qc_worker，降级重建（已退役身份复活）`

4. 与 WP-02 既定原则冲突：角色是集群级对象，迁移只拥有"本库的对象与授权"；
   仓库里已有专门的 `scripts/retire_legacy_roles.py` 承担生命周期动作。

## 二、影响面

- **真实集群（9 库）**：`app_role` 有跨库依赖的先例说明角色常被多库共用。`qc_worker` 当前
  经查零依赖、零授权（`role_specs` 声明 `cluster_wide_zero_authorization_verified=False`，
  即"单库范围已验证、集群范围未证"），所以现在能删；**但只要任一库或外部配置开始使用它，
  升级就会在部署中途失败**（守卫抛 `dependent_objects_still_exist`），且失败点在链条中段。
- **降级路径**：未授权即失败 → "可逆性"在默认路径下并不成立，只有拿到显式确认才能回退。
- **闸门**：矩阵现在会因它阻断；这不是误报，是把原先被洗掉的破坏性动作显性化。

## 三、两个可选处置（二选一，需 Owner 书面裁决）

### 选项 A（推荐）：把退役移出迁移

- `f2b3` 的 `upgrade()` 改为**只断言**：`qc_worker` 不存在（已退役）或存在但零授权零依赖，
  不再 `DROP ROLE`；
- 真正的销毁由 `scripts/retire_legacy_roles.py`（或 bootstrap 的 retire 子命令）在部署流程中显式执行；
- 降级同理不再 `CREATE ROLE`，`restore_retired_role` 仅在受控恢复流程中使用；
- 结果：该 revision 变成 `symmetric`（或仅剩 note），矩阵不再需要分类，闸门自然通过。

代价：多一步部署动作（一次性、可审计）；`f2b3` 不再是"自包含退役"。

### 选项 B：保留迁移内销毁，Owner 签署单向安全契约

- 矩阵分类改为 `intentional_one_way`，快照条目附 `safety_contract`，须写明：
  1. 迁移只允许销毁**已登记退役且 `cluster_wide_zero_authorization_verified=True`** 的角色；
  2. 销毁前必须通过 `pg_shdepend` 全局校验（现实现已有）；
  3. 失败即中止整条降级/升级，不允许跳过；
  4. 该 revision 的降级恢复只能由运维显式授权，视为独立受控操作，不计入"自动可逆"。
- 前置条件：`qc_worker` 的 `cluster_wide_zero_authorization_verified` 必须先由 Owner 认可为 `True`
  （当前声明为 `False`，即集群范围尚未证明零授权）。

代价：把"迁移可销毁集群身份"固化为制度，后续 4 个仍创建角色的 revision（`a0b1c2d3e4f5`、
`a5b6c7d8e9f0`、`e0f1a2b3c4d5`、`e6a7b8c9d0e1`）整改时口径要一致。

## 四、我未做的事（授权边界）

- 未替 Owner 选择方案，未改 `f2b3` 代码语义；
- 未把该 revision 标成 `no_restore_proven` 或 `intentional_one_way`；
- 未提交、未推送。

## 五、裁决后需要的动作（供 Owner 勾选）

- [ ] 选 A → 我改 `f2b3` upgrade/downgrade、重跑往返、重生成三件套、矩阵应自然变 symmetric
- [ ] 选 B → Owner 提供 `safety_contract` 文本与 `cluster_wide_zero_authorization_verified=True` 依据，
      我登记 `intentional_one_way` 并入快照
