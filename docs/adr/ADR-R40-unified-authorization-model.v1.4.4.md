# ADR-R40：统一授权模型（选型 B 目标架构）

> 状态：**PROPOSED（v1.4.4 已定版·文本冻结；已记录 D-10 三项裁决；阶段 3 仍不放行）**
> 定版含义：**文本冻结、可指认、可复算**，不等于「已批准」「已发布」或「设计已被确认」。
> **v1.4.4 的性质**：只做一件事——把所有者对「归属约束分阶段治理」的三项裁决写成 §13 D-10，
> 并把终态验收条件下沉为可复算的 14 行台账（附录 J）。**不新增设计、不激活迁移、不放宽任何判定。**
> 本文件指纹（SHA-256 / git blob SHA1 / 行数 / 字节数 / 采集 commit）唯一登记于
> `docs/adr/ADR-R40-VERSION.txt`，该文件为 append-only 版本索引；**本文正文不内嵌自身哈希**
> （内嵌会随任何一次改动自相矛盾，这是 v1.4.2 之前「版本找不到」的根因之一）。
> 日期：2026-11-20
> 版本基线：本文档由 **v1.4.3**（`ADR-R40-unified-authorization-model.v1.4.3.md`，2833 行 / 273354 字节 /
> SHA-256 `374e784e…ede4` / git blob `6c9df334…`）整体复制后做定点修订，**未重写正文**；
> v1.4.3 再上溯自 v1.4.2（`ADR-R40-unified-authorization-model.md`，2552 行 / 238449 字节 /
> SHA-256 `9c0edd1b…f53cc` / git blob `e6ab6622…`）。**v1.4.2 与 v1.4.3 原件字节均未被触碰**，
> 三版并存可逐版对照（见 `docs/adr/ADR-R40-VERSION.txt`）。
> 关联：ADR-R16/R17/R18/R19（main 身份治理）、ADR-R24/R25（main RLS/最小权限）、
> ADR-R16-database-role-per-domain / R17-project-scope-isolation（分支侧，待重编号至 R41/R42）、
> `docs/audits/2026-11-19-db-role-system-reconciliation.md`（双体系对账）、
> `docs/audits/2026-11-19-r40-readonly-evidence.md`（**本轮作为独立交付物提供**；v1.4 / v1.4.1 / v1.4.2 只读取证原始输出存档，E0~E11）
> 决策来源：所有者对选型 B 的正式认可（2026-11-19）；四轮复审「原则性认可 + 前置条件」；
> 第四轮复审指令「只修订 ADR + 只读取证，不建 Registry 文件、不改迁移、不操作数据库、不进阶段 3」。
> 验收原则：根因治理、单一事实源、最小权限、fail-closed、历史可追溯、真实 PostgreSQL 证据。
> **本 ADR 记录的是目标决策与设计，不是已实现或已验证的事实；一切「已落地」表述以真实 PostgreSQL
> 执行证据为准（附录 C/附录 E），当前所有实现层验收均标记为未完成。**

## 修订记录（历史可追溯）

| 版本 | 日期 | 状态 | 变更摘要 |
| --- | --- | --- | --- |
| v1.0 | 2026-11-19 | PROPOSED | 初稿：确立选型 B、威胁模型、GUC 选型、迁移硬约束、Expand-Contract 顺序 |
| v1.1 | 2026-11-19 | PROPOSED（第一轮复审后） | 补 §4 GUC 语义/事务/fail-closed、§5 权限不变量、§6 处置矩阵、§7 失败回滚；关闭 §10；新增 §11/§12/附录 A |
| v1.2 | 2026-11-19 | PROPOSED（第二轮复审后） | 修正 §4.2 GUC 物理表示、§4.1/§4.4/§12 GUC 可伪造边界、§5.1 登录模型与迁移所有者、§6 证据台账（真实 checksum + 库结构）、§6.4 回滚测试策略、§11 Authorization Registry、§12 E1；新增附录 C/D |
| v1.3 | 2026-11-19 | PROPOSED（第三轮复审后，待复审） | **结构性重构（非补丁叠加）**：P1-1 三 GUC 威胁边界统一（§3/§4.1/§4.4/§4.5/§12）；P1-2 身份四层分离与可验证权限不变量（§5.1/§5.3）；P1-3 逐库逐 Revision 十列证据矩阵 + 结构验收标准（附录 C，含本轮新取证）；P1-4 G1~G5 拆为「设计放行条件 DP」与「实现验收条件 IP」（§8/§8.1）；P2 Registry 唯一载体/资源模型/派生校验/漂移检测（附录 D）；新增附录 F 落实矩阵 |
| v1.4 | 2026-11-19 | PROPOSED（第四轮复审后，待复审） | **补齐「决策能否无歧义实现与验证」**：P1-2 迁移成员关系 INHERIT FALSE/SET TRUE 裁定 + ops_admin 授权闭环（ADMIN OPTION 边界）+ 外部回收核验（不依赖进程 finally）+ INV-9（afs_owner 全属性）+ 传递闭包检查；P1-3 C.3 改为可计算验收（具体对象名/属性/角色/权限/负向行为）+ C.5 完整只读取证命令 + 原始输出存档 + main 4 角色过渡态定位；P1-4 IP-4/6/7 环境与责任边界拆分；P2 资源 ID 补 object_type + DB 逻辑ID映射 + RLS 结构化语义 + TRUNCATE/REFERENCES/TRIGGER 权限 + SECURITY DEFINER 例外 6 字段。重定 DP1~DP5（DP2/DP3/DP5 待补充），不宣称全部解决 |
| v1.4.1 | 2026-11-19 | PROPOSED（第四轮复审补充修订后，待复审） | **消除 v1.4 遗留的权限矛盾与验收歧义**：① §5.1.3 改用方案 A（`ops_admin` 不持 `ADMIN OPTION`、无成员边；窗口 GRANT/REVOKE 归 bootstrap 超级用户受控步骤），消除「永久 ADMIN OPTION vs 常态 grantee 为空」硬矛盾，新增 INV-10b；② C.3.2 裁定 Policy 按命令拆分 + 唯一命名 + PERMISSIVE/RESTRICTIVE 组合 + USING/WITH CHECK 可观察结果；③ C.3.3 明确授权主体与负向主体分离（`svc_asset` 不得当 outbox 越权主体）；④ C.3.4 按 ADR-R10/R25 纠正 append-only（`asset_versions` = SIU，无 DELETE）；⑤ C.5 重组为集群级一次 + 库级 12 类查询 + 固定 commit SHA；⑥ D.2 引入 RLS 结构化 AST（含类型/NULL/scope_dimensions）、D.3 补函数签名 ID 与改名历史、D.5 区分目标/观测 OWNER、D.6.0c 定义 AST→SQL→测试同源派生；⑦ §7.0.1 给出 main 4 个旧角色逐角色生命周期与退役顺序；⑧ 附录 E 增至 16 条（新增 #14 超级用户残余、#15 函数 PUBLIC EXECUTE、#16 search_path）。**DP2/DP3/DP5 仍标「待复审确认」，不自行判定满足** |
| v1.4.2 | 2026-11-19 | PROPOSED（第五轮复审后，待复审） | **只修「生成器能不能产出合法 SQL」与「事实源是否唯一」，不再扩评审矩阵**：① §4.2 逻辑类型裁定 `app.current_projects` = `text[]`（**不是 `uuid[]`**），依据 E11 实测 `project_id varchar(64)`、971 行 0 行 UUID 形态、取值 `project-1`；唯一解析通道改为原生数组输入 `current_setting(...)::text[]`，`string_to_array` 全面作废；② D.6.0c 派生 1b 按命令派生 Policy 子句（SELECT→`USING`、INSERT→`WITH CHECK`、UPDATE→两者、DELETE→`USING`），子句不存在则完全省略（v1.4.1 统一模板作废）；③ D.6.0c 派生 2 拆为 2a RLS 行为测试（主体 `svc_asset`，须有 SELECT）与 2b ACL 越权测试（主体 `svc_shot_planning`，须无权限），并统一 D-8 边界；④ 新增 D.2.2「outbox / audit 隔离范围的业务裁定」；⑤ C.5.3 有效权限补 `relkind IN ('r','p')` + 新增 ⑬分区父子 ACL 对照、⑭递归成员闭包、⑮全对象类型有效权限；⑥ D.5 SECDEF 目标 `search_path = pg_catalog, pg_temp` + 对象 schema-qualified + 生成迁移「先 REVOKE PUBLIC」规则；⑦ D.2 删除重复的 `roles.mappings`，域→角色映射唯一权威在 `domains[].runtime_role`；⑧ §7.0.4 旧角色 DROP 的**双库**依赖验收闸门。**⑨ 补齐的查询已实跑（E12）**：发现三个基础设施角色 `role_exists=f`、INV 存在**真空真**缺陷（新增 §5.3 合取式判定 + C.5.2 PASS/FAIL 查询 + 附录 E #20）、⑬ 自身有 collation 缺陷已修；并复现 ⑨ 目标事实（父表有 `app_role` SELECT、三分区无）与两库漂移（verify 仍含 `DELETE`）。附录 E 增至 20 条，附录 H 增至 H1~H9 |
| **v1.4.3** | 2026-11-20 | **PROPOSED（已定版，待第七轮复审）** | **只做「版本可指认 + 分叉口径可区分 + 证据可复算」三件事，不扩设计、不改结论**：① **R1** 修正版本标注声明——原文声明用 `【vX.Y】`，实做 21 处用 `(vX.Y)`／`（vX.Y`，导致按声明 grep 只能看到 2 处；改为「块级 `【vX.Y】` + 行内括号」双形式并给出统一检索正则；② **R2** 附录 C.1 标题与导语按实际成员改判为**口径 C（语义不等价 3 项）**，并指向新增 C.1.1（原文标题称「同 ID 不同内容」，按该定义实测应为 10 个，**成员对、标签错**）；③ **R3** 新增 **C.1.1 全量分叉清单**（10 行，逐行给 main/feat 的 blob SHA1 + SHA-256 + 归一化残差 + 路径，来源固定为 `main@5352d197` / `feat@59b13af7`）；④ **R4** 新增 **C.1.0 三口径定义**（A 迁移身份冲突 = 10 / B 跨分支分歧项 = 10 / C 语义不等价 = 3），明确**三数不可互换**，并写明 T2 等价性的前置条件 `POSTGRES_APP_USER=app_role`；⑤ **R5** 修正 C.1 中 `b7c8`/`9e3b` 的 main 侧 SHA-256（原按 CRLF 计算，与同行 4 行口径不一致），并声明哈希口径为 `git show <ref>:<path>` 原始字节；⑥ **R6** 附录 §7.0.1~§7.0.4 全部升级为 `###` 标题，且把 §7.0.4 物理移到 §7.0.3 之后（**编号不变**，交叉引用零破坏）；⑦ **R7** 修正 D.1 与 §8 中「只定义载体、不创建文件」的失真表述——Registry 已落地 337 行并接入契约闸门第 12 项；⑧ **R8** §8.1-A 按最新证据更新 DP3/DP5 说明，**仍不自行判定满足**；⑨ **新增附录 C.6 归属约束脏状态实验（E13，49 步全绿）**并据此在附录 E 增 #21/#22；⑩ **新增附录 I**（v1.4.2 → v1.4.3 落实矩阵 R1~R8 + E13，七字段 + 六态）。**本版已定版（文本冻结，指纹见 `docs/adr/ADR-R40-VERSION.txt`）：未发布、未进入阶段 3、未改任何迁移与业务代码；定版不构成批准。** |
| **v1.4.4** | 2026-11-20 | **PROPOSED（已定版，待第七轮复审）** | **只做一件事：把所有者对「归属约束分阶段治理」的三项裁决写成 §13 D-10，并把终态验收条件下沉为可复算台账**：① **D-10.1** `NOT VALID` **有条件采纳**——仅作过渡手段，**拒绝永久豁免**，终态必须 14 张表 `VALIDATE` 全部完成（五步治理路径 + 四条不可接受结果）；② **D-10.2** 拒绝「守卫晚于数据订正」的次序，改为**写入保护先行**——并裁定「归属外键」与「不可变守卫」是两类不同守卫，前者必须先立；后者**不再使用 `DISABLE TRIGGER`**，改为**工单令牌 + 映射行 + 专用角色**的最小豁免面（附残留风险 #24）；③ **D-10.3** 哨兵行**默认移入隔离项目**，七条要求逐条落成机制与验收判据；隔离项目**不是垃圾桶**（附残留风险 #25）；④ 新增**附录 J**：D-10 落实矩阵 + 14 行终态验收台账口径 + 本轮边界；⑤ 附录 E 由 22 条增至 **25 条**（新增 #23 永久 `NOT VALID` 化、#24 受控例外被滥用、#25 隔离项目退化为垃圾桶），并**更新 #22 状态为「已裁定」**（原文「裁决前不得编写补齐迁移」一句按本轮实情更正说明）。**本版不激活迁移、不改业务代码、不动生产/模板/`ai_film_verify`，阶段 3 仍不放行。** |

> **v1.4.3 边界声明（替代 v1.4.2 原文的「纯文档修订」表述）**：
> v1.4.3 **不再是纯文档修订**——本轮确实做了两件 v1.4.2 明文声明未做的事，故原表述已失真，逐项更正：
> ① **Registry 文件已创建**（`contracts/authorization/authorization_registry.yaml`，337 行，8 域 / 3 基础设施角色 /
> 7 资源 / 16 条 Policy AST / 2 条 SECDEF 例外 / 4 条显式债务，已接入 `scripts/contract_check.py` 第 12 项），
> 详见 D.1 与 §8.1-A 的 DP5；
> ② **在隔离实例上执行了数据库写操作**（附录 C.6 归属约束脏状态实验，49 步；仅作用于由模板克隆的
> `t3_lab` 库，模板库、两分支 head 库、`ai_film_industrial`、`ai_film_verify` 与生产**均未修改**）；
> ③ 仍未做：未进入阶段 3、未修改任何迁移文件与 Revision、未修改 main / 分支代码、未修改任何真实数据库的
> 角色 / GRANT / RLS Policy、未执行 ADR 重编号；**v1.4.3 已于本轮定版**（文本冻结 + 指纹登记于
> `docs/adr/ADR-R40-VERSION.txt`），定版**只锁文本，不构成批准**。**「未创建 Registry 文件」一句在本版作废。**

> **版本标注规则（R1 裁定，取代 v1.4.2 原文的单一 `【vX.Y】` 声明）**
> 原文第 30 行声明「新增/修订内容以 `【vX.Y】` 标注」，但实测 `【v1.4.2】` 仅 2 处、`(v1.4.2)`／`（v1.4.2`
> 共 21 处——按声明检索只能看到 2 处改动，是「找不到条款」的第二类根因。此处**不采用**「把 21 处改成 `【】`」
> 的做法（会引入 21 处无意义改动、且不改检索方式仍会漏），而是**把规则写实**：
>
> | 形式 | 用途 | 示例 |
> | --- | --- | --- |
> | `【vX.Y】` | **块级**：一小节、一段裁定、一张表的整体归属 | `【v1.3 关键新发现…】` |
> | `(vX.Y)` / `（vX.Y` | **行内**：表格单元格、条目、单句的局部修订 | `v1.4.1：资源 ID 四段齐全` |
>
> **统一检索正则（覆盖两种形式，复审请用这条而不是 `grep "【"`）**：
> `grep -nE '[【(（]v1\.4(\.1|\.2|\.3)?[】)）]' docs/adr/ADR-R40-unified-authorization-model.v1.4.3.md`
> 两者的区别是**归属粒度**，不是排版随意：块级标注整节适用，行内标注只适用于该单元格/该句。
> v1.4.3 自身新增内容一律用 `【v1.4.3】`（块级）与 `(v1.4.3)`（行内）。
> 凡标注「作废」的段落一律以新版为准，保留原文仅为可追溯，**不得作为现行规范引用**。
> **v1.4 指令符合性补齐（同版本内）**：按第四轮修订指令逐条复核后，补齐此前遗漏的六类"必须"项——
> ① ops_admin 七问闭环（§5.1.3）；② FORCE RLS 作用边界与 INV-9 前提关系（§5.3）；
> ③ 权限枚举补 CREATE/CONNECT/TEMPORARY 并按对象类型区分（D.4）；④ RLS 方案 A/B 二选一裁定为方案 B（D.6.0）；
> ⑤ Registry 与历史迁移版本固定关系（D.6.0b）；⑥ 附录 F 重构为七字段 + 六态标注（附录 F）。
> 版本号不变（仍为 v1.4 第四轮复审稿），状态不变（PROPOSED）。
> **v1.4 之后 · 所有者决策写入（同版本内，2026-11-19）**：新增 §13 记录两项正式裁决——
> D-8（风险 #8 GUC 可伪造：**已接受**，维持威胁边界，不建新机制）；
> D-9（风险 #9：**不授权阶段 3 退役 8 个目标 `svc_*`**，保留待 IP-4 按 Registry 校准）。
> 同步更新附录 E（风险数统一为 13 条 #1~#13，#8/#9 状态按裁决修订）、§6.5（纠正目标角色误当退役对象）、
> §7.0（Expand-Contract 适用于 main 4 个旧应用角色的逐角色退役表）。**状态仍 PROPOSED，阶段 3 不放行。**
> **v1.4 之后 · 第四轮补充修订（同版本内，2026-11-19）**：按补充复审修正证据口径——
> ① 新增 §6.1.1「集群级角色事实 vs 数据库级对象事实」（PostgreSQL role 为集群级 shared catalog，
> 两库共享同一套身份，**禁止按库重复计数**；已由 E9 只读验证：`relisshared=t`、两库角色逐字相同）；
> ② 重写 §6.5 为「集群级角色表 + 数据库级依赖表 + 证据强度分级」，把「六项全部核清」收紧为
> 「仓库与数据库可观测范围内」，显式标出外部部署/CI 密钥/外部 Worker/定时任务/历史连接日志**未检查缺口**；
> ③ §13 D-9 增加「保留 ≠ 认可」六项裁定与六类证据区分；④ 修正 §6.1 表、C.2 矩阵、C.3.5、附录 E #9、
> 附录 F（F14 修订 + 新增 F15~F17）。**D-8 保持已接受且不写成已消除；状态仍 PROPOSED，阶段 3 不放行。**
> 所有数据库结构数据均由只读 SELECT 于 2026-11-19 现场采集，每条命令与原始输出逐条留存于
> `docs/audits/2026-11-19-r40-readonly-evidence.md`（附录 C.5 引用）。取证确认 PostgreSQL 16.15。
> **v1.4.1 · 第四轮复审补充修订（2026-11-19）**：按第四轮复审结论修复六类问题，均给出唯一裁定而非
> 增加包装层——① ops_admin 权限模型改为「方案 A：移除能力」（§5.1.3，消除与 INV-10 的硬矛盾）；
> ② C.3 Policy 命名/命令拆分/PERMISSIVE 组合/USING-WITH CHECK 可观察结果定死（C.3.2~C.3.4）；
> ③ append-only 回查 ADR-R10/R11/R25 纠正为 `asset_versions=SIU`（C.3.4/D.2）；
> ④ C.5 重组为「集群级一次 + 库级 12 类查询 + 固定 commit SHA」（C.5.2~C.5.4，证据 E10）；
> ⑤ Registry 引入可执行 RLS AST + 目标/观测 OWNER 分离 + 函数签名 ID（D.2/D.3/D.5/D.6.0c）；
> ⑥ main 4 个旧角色逐角色生命周期（§7.0.1~7.0.3）。附录 E 增至 16 条，附录 G 为落实矩阵。
> **D-8/D-9 裁决不变；DP2/DP3/DP5 仍不自行判定满足；状态仍 PROPOSED，阶段 3 不放行。**
> **v1.4.2 · 第五轮复审后修订（2026-11-19）**：本轮**停止扩展审查矩阵**，只解决四件会直接影响运行的事——
> ① **AST 不能产出合法 SQL**：`guc_array` 解析错误 + Policy 模板对所有命令输出双子句（已在 D.6.0c 重写并给可执行样例）；
> ② **事实源重复**：`domains[].runtime_role` 与 `roles.mappings[]` 双写域→角色映射（删除后者）；
> ③ **证据缺口**：有效权限漏分区父表、无递归成员闭包、无全对象类型有效权限（C.5.3 补齐 ⑬⑭⑮）；
> ④ **威胁/主体边界**：RLS 行为测试与 ACL 越权测试共用零权限主体（拆为 2a/2b）。
> 过程中用只读 SQL（E11）证实了一个**比复审指出的更严重的事实**：`app.current_projects` 声明为 `uuid[]` 本身
> 就与真实数据冲突——`project_id` 是 `varchar(64)`，971 行中 0 行是 UUID 形态。因此本轮把逻辑类型改为 `text[]`，
> 而不是只修解析函数。**这不是新增设计，是让设计回到真实数据的类型上。**
>
> **v1.4.2 追加：补齐的查询已实跑（E12）**。此前 ⑨⑬⑭⑮ 与递归闭包只是「写出来了」，从未执行。
> 实跑后发现三件事：① **三个基础设施角色 `afs_owner`/`afs_migrator`/`ops_admin` 根本不存在**
> （`role_exists=f`）；② 因此所有以它们为对象的 INV **在原始查询下返回 0 行、看起来像通过**——
> 这是**真空真（vacuous truth）**，已新增 #20 并把判定式改为 `role_exists AND <谓词>`，C.5.2 增加
> 输出 `PASS/FAIL` 的查询（实测输出 `FAIL: afs_owner 不存在`）；③ **⑬ 查询自身有 collation 缺陷**
> （`recursive query "t" column 6 has collation "default" ... but "C" overall`），修 `NULL::text COLLATE "C"`
> 后才跑通——这条证明「文档里的查询不实跑，无法确认可执行」。
> 同时 E12 复现了 ⑨ 想要复现的事实：`app_role` 只在 `relkind='p'` 父表有 SELECT，三个分区全无；
> 并给出两库漂移精确值（verify 库 `project_planning` 四表仍含 `DELETE`，industrial 已无）。
> 仍为纯文档修订 + 只读取证：**未创建 Registry 文件**（← v1.4.3 作废：文件已落地，见文首边界声明）、未改代码/迁移、未执行 GRANT/REVOKE、未改数据库角色或 Policy。
> 上面这句是 **v1.4.2 的原文历史记录**，保留仅为可追溯；其事实陈述已被 v1.4.3 更正，**不得作为当前状态引用**。

> **【v1.4.3 · 版本可指认 / 分叉口径可区分 / 证据可复算（2026-11-20，定版文本冻结）】**
> 第六轮复审提出三件互不替代的事，本轮逐件闭环，但**不新增设计、不改任何既有结论**：
>
> 1. **「找不到 v1.4.2 正文」不是文档不存在，是单一可变路径反复覆盖**（版本号只写在正文里，
>    文件名不带版本）。v1.4.3 起改为**文件名带版本 + 目录内 `ADR-R40-VERSION.txt` 版本索引**，
>    每版登记「版本 / 行数 / 字节 / SHA-256 / git blob SHA1 / 基线」。v1.4.2 原件保留不动，双版并存。
> 2. **「3 个迁移冲突」与「10 个分叉」不是同一指标**，不能把 3 改成 10。本轮在 C.1.0 定义三个口径
>    （A 迁移身份冲突 = 10、B 跨分支分歧项 = 10、C 语义不等价 = 3），三数并列且**不可互换**；
>    C.1 按实际成员改判为口径 C，新增 C.1.1 给出全量 10 项的双哈希与路径。
> 3. **哈希口径混用**：C.1 原表 6 行中 2 行按 CRLF 计算、4 行按 LF，任何人复核都会「对不上」且
>    无法分辨是文档错还是文件被改。本轮把主锚点改为 **git blob SHA1**（由 Git 对仓库内字节计算，
>    不受 `core.autocrlf` 影响），文件 SHA-256 降为辅助列并标注口径 `git show <ref>:<path>` 原始字节。
>
> 另修三处「表述与现实不符」：`§7.0.4` 的**可检索性缺陷**（加粗段落 + 序号倒序，导致复审判定「条款不存在」，
> 实为排版问题——本轮保留编号不改、只升级标题层级并移到 §7.0.3 之后）；D.1/§8 的「只定义载体、不创建文件」
> （文件已落地 337 行）；以及 §8.1-A 的 DP3/DP5 说明（按最新证据更新，**仍不自行判定满足**）。
> **状态：本版已定版（文本冻结），未发布；阶段 3 仍不放行。**定版只锁文本，不构成批准；
> 指纹（SHA-256 / blob SHA1 / 行数 / 字节数）登记于 `docs/adr/ADR-R40-VERSION.txt`。

## 1. 决策

**以分支的授权模型作为最终目标架构，吸收 main 已完成的身份治理、状态机治理与契约治理成果。**

- 授权底座：8 个 `DatabaseDomain` 域角色 + 项目级数据隔离 + 分域连接池。
- 保留 main：kid 公钥指纹 / JWKS 轮换 / 归属链根解析 / 状态迁移事实表 /
  基线单一事实源 / 状态枚举单一源 / 四道契约闸门 / 869 测试基线。
- **但分支现有代码不被认定为已验证的安全底座**——它必须在真实 PostgreSQL 上按第 6/7 节验证后才能成为事实。
- 【v1.2】选型 B 已获所有者**正式认可**，为唯一目标架构方向，不再重开选型争论。
- 【v1.3】第三轮复审继续认可选型 B；当前阻塞点不是架构方向，而是「身份模型、迁移证据、
  阶段闸门」尚未闭环。本轮只解决这三项的定义与证据问题，**不据此放行阶段 3**。

## 2. 为什么是 B（目标架构方向）

分支的 8 域角色把 `shot_planning` / `generation_execution` / `qc_governance` / `workflow`
建成独立数据库身份，项目级 scope 让 RLS 从「同租户可见」收紧到「同项目可见」。
这对 Phase 1 主链（生成→QC→交付）的域隔离是必要的：继续用 main 的两域模型会迫使
新域共享权限边界，违背「一个事实一个权威源」与最小权限。

## 3. 威胁模型（诚实边界，不得过度表述）【v1.3 统一三 GUC 口径】

**已核实的物理事实**：
- 分支 `c2d3e4f5a6b7` 中 8 个域角色共用 `POSTGRES_APP_PASSWORD`（`_domain_password()`）；
- 分支 `DomainPoolRegistry.for_all_domains()` 在同一 API 进程内创建全部域连接池。

**因此本模型的安全边界是**：
- ✅ **能防**：业务代码的跨域误写/越权读写——资产域代码拼 SQL 写 project_planning 表时，
  PostgreSQL 层拒绝（域角色只对授权 schema 有权限）。这是数据库角色隔离的核心价值。
- ❌ **不能防**：单一业务进程完全失陷后的横向移动。8 个角色共用口令且同进程持全部池，
  进程一旦失陷即持有全部域凭证。**不得**把本模型在文档/测试中表述为「服务级安全隔离」
  或「进程失陷后其他域仍安全」。
- 若要达成进程级隔离，属独立议题（独立进程 + 独立凭证 + 独立部署边界），不在本次范围。

【v1.2 补充 · 威胁场景排除】自定义 GUC 可被持有数据库连接的任意 SQL 执行者自行设置，
**不是不可伪造的安全凭证**。本模型承诺的安全边界限定为「**可信应用调用链下的授权隔离**」：
即正常业务请求从已认证授权的 Principal 生成租户/组织/项目上下文，应用代码不得绕过统一授权入口。

【v1.3 · P1-1 关键澄清：三个 GUC 同属可伪造上下文，不存在更强保护的上下文】
§4 定义的三个上下文——`app.current_tenant_id`、`app.current_organization_id`、
`app.current_projects`——**全部是 PostgreSQL 自定义 GUC（customized options）**，
三者对持连接的 SQL 执行者具有完全相同的可伪造性。v1.2 仅围绕项目 GUC 说明伪造边界，
容易让实施者误以为租户/组织上下文有更强的数据库保护。本轮明确统一裁定：

- 三者**都不是**不可伪造凭证，都不能抵御持有数据库连接的恶意 SQL 执行者自行 `set_config()`；
- 三者的**唯一可信来源**都是经过认证、授权后的 `Principal`，只能经统一授权入口生成并注入；
- 三者**都必须**执行归属一致性校验：租户来自 Principal 认证结果，组织必须属于当前租户，
  项目必须同时属于当前组织与当前租户（§4.1）；
- 不得只对项目 GUC 声明伪造风险，也不得暗示租户/组织 GUC 具备「数据库层不可绕过」的属性。

以下场景**明确排除在本模型承诺之外**，除非另行建设数据库可验证的授权上下文机制（当前不建设）：
- 持有数据库连接的恶意 SQL 执行者（如成功注入的 SQL）自行设置上述任一 GUC 伪造上下文；
- 数据库角色本身的权限提升或伪造身份。

不得通过增加一个 Python 校验函数就宣称覆盖上述被排除场景（详见 §4.5、§12 E1.4）。
本轮**不建设**签名令牌 / 数据库可信上下文等可验证授权上下文机制；若将来需要覆盖右列威胁，
须独立立项，不得在本 ADR 下以「加了校验函数」名义声称已覆盖。

## 4. 唯一组织 GUC（消除双轨）

| 上下文 | 唯一名称（完整名称，含 `app.` 前缀） | 说明 |
| --- | --- | --- |
| 租户 | `app.current_tenant_id` | 两边已一致 |
| 组织 | **`app.current_organization_id`** | 采用 main 命名；分支的 `app.current_org_id` 退役 |
| 项目 | **`app.current_projects`** | 采用分支命名（main 无此项） |

选择 `app.current_organization_id` 的理由：它已在 main 的验证库 `ai_film_verify` 的 RLS 策略、
迁移与集成测试中落地并有真实执行证据；改名成本低于推翻既有验证证据。分支的
`app.current_org_id` 需在统一实现中全部替换。

统一范围（不得遗留双轨）：Python 注入入口、Principal 读取与编码、**所有 PostgreSQL RLS Policy**、
迁移脚本、安全负向测试。**禁止**为兼容长期保留两个 GUC 名或两套注入函数。

【审查修订 v1.1】GUC 只是会话上下文，不是权限本身；§4.1~§4.4 为强制不变量，§4.5 为边界澄清。
【v1.3】§4.1/§4.4/§4.5 的伪造边界口径统一覆盖三个 GUC（P1-1）。

### 4.1 上下文必须来自可信身份（三 GUC 统一）

- `app.current_tenant_id`、`app.current_organization_id`、`app.current_projects` 三者
  **只能**从经过身份验证与授权后的 `Principal` 生成；
- **禁止**直接信任 HTTP 请求传入的租户/组织/项目 ID 作为授权依据；
- **禁止**业务代码通过任意设置 GUC 的方式扩大自身权限（GUC 写入口只保留在统一授权中间件）；
- 三者必须做**归属一致性校验**（v1.3 从「仅项目」扩展到全部三个上下文）：
  - 租户：来自 Principal 的认证结果，不接受请求体覆盖；
  - 组织：必须属于当前租户（`organization.tenant_id == current_tenant_id`）；
  - 项目：项目集合中的每个 ID 必须同时属于当前组织与当前租户；
  - 跨租户/跨组织的组织 ID 或项目 ID 在注入前即拒绝（fail-closed）。

【v1.2 澄清 · 应用约束 ≠ 数据库强制边界】上述「写入口只保留在统一授权中间件」是**应用架构约束**，
不是 PostgreSQL 权限约束。自定义 GUC 在 PostgreSQL 层可由当前会话自行设置；业务连接若可执行任意
SQL，仍可能自行调用 `set_config()`。因此本节保证的是「正常业务调用链从可信 Principal 生成范围、
应用代码不允许绕过统一入口」，**不保证**「数据库角色无法伪造 GUC」。该口径对三个 GUC 一视同仁
（§3、§4.5、§12 E1.4）。

### 4.2 项目集合的数据格式（v1.2 修正物理表示；**v1.4.2 修正逻辑类型**）

【v1.2 修正】PostgreSQL 自定义 GUC 的值本质是**文本**，不能直接声明为类型化 `uuid[]` 变量。
必须明确区分逻辑类型与物理表示，并由统一函数负责编码/解析。

| 项目 | 规定 |
| --- | --- |
| **逻辑类型** | **`text[]`**（元素为项目标识 `text`）——【v1.4.2 裁定】不再是 `uuid[]`，依据见下 |
| **GUC 物理表示** | 规范化 PostgreSQL **数组文本**（形如 `'{project-1,project-2}'`），由统一编码函数生成 |
| **元素编码约束** | 元素必须匹配 `^[A-Za-z0-9_-]{1,64}$`（与 `project_id varchar(64)` 同域）；该字符集**不含** `,` `{` `}` `"` `\`，故数组文本永不需要引号转义 |
| **写入** | 统一写入函数使用**参数化** `set_config('app.current_projects', <规范化文本>, true)`；禁止字符串拼接 |
| **读取** | 统一解析 helper：`current_setting('app.current_projects', true)::text[]`——**直接使用 PostgreSQL 原生数组输入语法**，不自行切分 |
| 空集合 `{}` | 表示「无任何项目可见」，**不得**解释为「全部项目」 |
| `NULL` / 缺失上下文 | `current_setting(..., true)` 返回 `NULL` 时一律拒绝（fail-closed），**不得**解释为无限制访问 |
| 非法文本 / 非法元素 | 解析失败即拒绝并报错（`malformed array literal` / 元素不合编码约束），**不得静默转换成空集合** |
| 重复 ID | 注入前去重；仍异常则拒绝（视为构造错误） |
| 超量项目 ID | 设上限（配置定义，超限拒绝），防止超大数组绕过或拖垮 RLS 求值 |
| 通配符 / 全量标记 | 不设；不存在「用特殊值代表全部项目」的通路 |
| 设置粒度 | **事务局部**（`set_config` 第三参数 `is_local = true`），禁止会话级设置残留上下文 |

统一编码函数与统一解析 helper 是 `app.current_projects` 的唯一读写通道（与 §11/附录 D 授权注册表一致）；
Python 与 SQL 两侧不得各自实现一套编码/解析逻辑。
【v1.3】租户 GUC（`uuid` 文本）、组织 GUC（`uuid` 文本）同样使用**参数化** `set_config` 与
统一解析 helper，不得字符串拼接；三者共同遵守「事务局部、非法即拒、缺失即拒」。

**【v1.4.2 根因裁定】逻辑类型必须从 `uuid[]` 改为 `text[]`——这不是解析技巧问题，是类型与真实数据不符**

第五轮复审指出 D.6.0c 用 `string_to_array` 切分数组文本错误（花括号残留在首末元素）。追问根因时用只读 SQL
（E11）发现更严重的一层：**`uuid[]` 这个逻辑类型本身就无法承载真实的项目标识**。

| 只读证据（E11.2，`ai_film_industrial`） | 结论 |
| --- | --- |
| `asset_service.asset_versions.project_id` = `character varying(64)` | 不是 `uuid` 列 |
| 971 行中形态匹配 `^[0-9a-f]{8}-[0-9a-f]{4}-` 的行数 = **0** | 无一行为 UUID 形态 |
| 主键分布：`project-1` 684 行；其余为 32 位无连字符 hex（如 `2140479b0e494e6baf17ea8f4a938224`） | 项目标识是**不透明 slug** |
| `asset_versions` 外键：`FOREIGN KEY (project_id, tenant_id) REFERENCES project_planning.projects(entity_id, tenant_id)` | 引用列 `entity_id` 同为 `varchar(64)` |
| `'{project-1}'::uuid[]` | **`ERROR 22P02 invalid input syntax for type uuid: "project-1"`** |

因此「用 `uuid[]` 解析」在任何实现下都不可行：要么注入时 22P02 失败，要么（若强转成 text 再比）永远匹配不到。
**唯一可行裁定**：

1. `app.current_projects` 逻辑类型 = **`text[]`**；物理表示 = 原生数组文本；解析 = `::text[]`（原生输入语法）。
2. 元素编码由编码器强制 `^[A-Za-z0-9_-]{1,64}$`，使数组文本**无歧义且无需转义**（E11.3：只有含 `,` 的元素才需要
   引号，而该字符集排除 `,` `{` `}` `"` `\`）。
3. **禁止** `string_to_array(...)`、**禁止** Python/生成器/SQL 各自解析（唯一通道）。
4. 与列比较时**不对列做 cast**：`project_id`（`varchar`）与 `text[]` 元素（`text`）在 text 域直接比较。
   对列写 `::uuid` 会让 684 行在求值时直接 22P02（E11.2 变体 C），禁止。
5. 租户/组织 GUC 的列确实是 `uuid`，仍用 `::uuid` 解析，**不随之改动**。

> 这不是「为过测试而改类型」：`project_id` / `entity_id` 的 `varchar(64)` 是既有数据模型事实（E11.2），
> 本 ADR 无权也未修改它。逻辑类型必须服从被比较列的真实类型，否则 RLS 谓词在运行期必然失效。

### 4.3 上下文的事务边界

- 通过 `set_config(..., true)` 在**事务级**注入上下文，不使用会话级 `SET`；
- 事务**提交**后上下文随事务结束而失效，不得残留；
- 事务**回滚**后上下文不得残留，下一次事务必须显式重新注入；
- 连接池复用时必须验证不会继承上一个请求的上下文（复用前上下文为空）；
- 异步任务、Worker、Relay 必须显式建立自己的授权上下文，不得依赖 API 进程遗留上下文。

**必须验证的四个场景（缺一不可）**：`set_config(..., true)` 生效范围 → 事务提交后失效 → 事务回滚后失效 → 连接池复用不继承。

### 4.4 RLS fail-closed 强制验收矩阵【v1.3 三 GUC 齐全】

| 场景 | 预期 |
| --- | --- |
| 没有租户上下文（`app.current_tenant_id` 缺失） | 拒绝 |
| 没有组织上下文（`app.current_organization_id` 缺失） | 拒绝 |
| 没有项目上下文（`app.current_projects` 缺失/NULL） | 拒绝 |
| 组织 ID 不属于当前租户 | 拒绝（v1.3 增补：组织归属一致性） |
| 项目 ID 不属于当前组织 | 拒绝 |
| 项目 ID 不属于当前租户 | 拒绝 |
| 未经授权的组织/项目 ID 试图通过可信授权入口进入 Principal 范围 | 拒绝（在注入前校验） |
| 连接池复用旧上下文 | 不得继承旧权限 |

【v1.2 口径修正】上表「伪造 ID」精确定义为「**未经授权的组织/项目 ID 试图通过可信授权入口进入 Principal 范围**」，
由 §4.1 的归属一致性校验拦截。持有数据库连接的 SQL 执行者自行 `set_config` 伪造任一 GUC（租户/组织/项目），
**不在本矩阵覆盖范围**（§3 威胁场景排除、§4.5）。所有负向测试必须使用真实业务域连接身份执行，
不得用超级用户代替（§12）。
- 上述场景**不得只测 SELECT**：INSERT / UPDATE / DELETE 同样覆盖；
- 每个 RLS Policy 必须校验 `USING`（读可见性）与 `WITH CHECK`（写合法性）约束一致，
  防止「看得见但写不进」或「写越权」的不对称。

### 4.5 GUC 可伪造边界小结（v1.2 建立，v1.3 统一三 GUC）

【v1.3 · P1-1】下表适用于全部三个 GUC，不再只针对项目上下文：

| 维度 | 本模型承诺 | 本模型不承诺 |
| --- | --- | --- |
| 三 GUC 来源 | 从可信 Principal 生成，经租户/组织/项目归属校验 | — |
| 应用代码 | 只能经统一授权入口设置 GUC | 抵御绕过应用的恶意代码直接改库 |
| 数据库角色 | 受域权限 + RLS 约束 | 无法自行 `set_config` 伪造租户/组织/项目上下文 |
| 上下文威胁 | 缺失/非法/空上下文 fail-closed | 抵御持连接的任意 SQL 执行者伪造任一 GUC |

【v1.3 明确】三个 GUC 在数据库中具有**同等可伪造性**：不存在「租户/组织 GUC 因沿用 main 命名
而受额外数据库保护」的事实。E1 只验证「可信应用调用链下的授权边界」，不得把任何一种普通 GUC
描述为抵御任意 SQL 执行者的安全边界（§12 E1.4）。
如需覆盖右列，须另建数据库可验证授权上下文机制（如签名令牌 / 受信任过程写入不可伪造的上下文表），
作为独立议题立项，不得在本 ADR 下以「加了校验函数」名义声称已覆盖。

## 5. 八域角色与凭证边界【v1.3 身份四层分离】

目标角色（沿用分支 `DatabaseDomain` 枚举与 `svc_<domain>` 命名）：
`svc_project_planning` / `svc_shot_planning` / `svc_asset` / `svc_generation_execution` /
`svc_qc_governance` / `svc_workflow` / `svc_outbox_relay` / `svc_identity`。

**fail-closed 授权配套**：分支刻意不使用 `ALTER DEFAULT PRIVILEGES`——必须配套
「新增表无显式 GRANT 时 CI 报错」的授权完整性检查（`scripts/boundary_assertions.py` 扩展），
否则权限缺失会拖到生产运行时才暴露。

【审查修订 v1.1】仅定义角色名不构成完整的数据库授权模型；§5.1~§5.4 为强制约束。
【v1.3】取证发现（附录 C.2）：当前两库**不存在** `afs_owner`/`afs_migrator` 角色，业务表 OWNER
全部是 `postgres` 超级用户，所有表/分区 `relforcerowsecurity=f`。因此 §5 的身份分层是**纯目标设计**，
当前数据库状态不满足，属阶段 4 实现内容，不得提前宣称完成。

### 5.1 身份分层与登录模型【v1.3 结构性重写；v1.4 补齐授权机制】

v1.2 把「对象所有权」与「迁移执行」都压在 `afs_migrator` 上，并把其 LOGIN 属性留给实施阶段裁定，
构成开放决策。v1.3 **根因性分离为四个身份类别**；v1.4 按第四轮复审补齐
「成员关系选项、管理员授权来源、异常回收、角色属性不变量」四处实现歧义：

| 身份类别 | 角色命名 | LOGIN | 对象所有权 | 职责 | 成员关系与权限边界 |
| --- | --- | --- | --- | --- | --- |
| 业务运行时 | `svc_<domain>` ×8 | **是**（每域独立凭证，经密钥管理注入） | **不得**拥有任何业务对象 | 业务运行时连接池，最小 DML/EXECUTE | 不得是 `afs_owner`/`afs_migrator`/运维管理员/任何跨域角色的成员；无 `CREATEDB`/`CREATEROLE`/`SUPERUSER`/`BYPASSRLS` |
| 对象所有者 | `afs_owner`（NOLOGIN 权限角色） | **否** | **拥有全部**业务表/序列/函数/Policy/Schema | 仅作为所有权归属主体，使业务角色不持表、RLS 对其生效 | 不设可连接凭证；不被任何 `svc_*` 继承；需要 FORCE RLS 的表对其强制生效 |
| 迁移执行者 | `afs_migrator`（LOGIN 迁移专用） | **是**（迁移/CI 作业专用凭证，仅存在于迁移作业，不进业务进程） | **不长期持有**对象所有权；仅迁移窗口内临时具备 owner 能力 | 执行 DDL、按明确授权变更结构 | **常态下不是 `afs_owner` 成员**；窗口授权用 `GRANT afs_owner TO afs_migrator WITH ADMIN FALSE, INHERIT FALSE, SET TRUE`（不授予 ADMIN OPTION、不自动继承、仅可主动 SET ROLE，见 §5.1.1）；作业结束由**外部编排层**回收（不依赖进程 finally，见 §5.1.2）；凭证不进业务进程（接 CRED-1） |
| 运维管理员 | `ops_admin`（具名，LOGIN） | **是**（运维/故障恢复凭证） | 默认不拥有对象 | 只读诊断、进程级运维；**可发起/审批运维与 DDL 请求，但自己既不 `GRANT`、也不 `SET ROLE`、也不执行 DDL**（v1.4.2 明确） | **不持有 `afs_owner` 的任何成员边，也不持有其 `ADMIN OPTION`**（§5.1.3 方案 A：授权能力收敛到 bootstrap 超级用户的受控步骤）；无 CREATEROLE/CREATEDB/SUPERUSER/BYPASSRLS；不在任何业务表 ACL 中 |

【v1.3 对 P1-2 六个必答问题的逐项裁定】（第 6 问对应复审正文「如何避免长期持有对象所有权权限」）
1. **`afs_migrator` 是否 LOGIN**：是。迁移执行需要建立数据库连接，故设 LOGIN；凭证仅限迁移/CI 作业，
   与业务进程凭证物理隔离。
2. **是否同时承担对象所有权与迁移执行**：否。对象归 `afs_owner`（NOLOGIN），迁移执行归 `afs_migrator`，
   两者是**两个独立角色**；`afs_migrator` 仅在迁移窗口内临时以 owner 身份变更结构，不构成 owner 身份本身。
3. **§5.3「数据库迁移角色」与「对象所有者」是否两个身份**：是，分别为 `afs_migrator` 与 `afs_owner`；
   v1.2 中二者合一的写法作废。
4. **迁移执行身份如何获得必要权限**：迁移窗口内由**受控编排步骤（bootstrap 超级用户）**执行
   `GRANT afs_owner TO afs_migrator WITH ADMIN FALSE, INHERIT FALSE, SET TRUE`
   ——**禁止自动继承** owner 权限，仅允许在迁移会话内主动 `SET ROLE afs_owner`；绑定作业单号、
   时间窗、审批留痕。不通过 `SUPERUSER`/`BYPASSRLS`/`CREATEDB`/`CREATEROLE` 常驻高权绕过（§5.1.1）。
   **不由 `ops_admin` 执行**：它不持有 `ADMIN OPTION`（§5.1.3 方案 A）。
5. **迁移完成后如何避免长期持有对象所有权权限**：**不依赖迁移进程自身的 finally**——由独立外部
   编排层（CI/CD 迁移编排器）在迁移**启动前检查无遗留窗口授权**、在成功/失败/**进程异常终止**
   后统一执行 `REVOKE` 并核验；未回收则阻止后续迁移作业与阶段验收（§5.1.2）。
6. **迁移完成后如何避免业务运行时继承迁移权限**：`svc_*` 与 `afs_owner`/`afs_migrator`/`ops_admin`
   无任何成员关系，业务连接池只用 `svc_*` 建连；`afs_migrator` 凭证不进业务进程。故迁移权限不会沿
   连接或成员关系链流入运行时（INV-3/INV-4）；CI 额外检查成员关系的**传递闭包与最终有效权限**，
   防止多层间接跨域（INV-3b）。

#### 5.1.1 迁移成员关系选项裁定（PG16 INHERIT/SET）【v1.4】

第四轮复审指出：v1.3 只写「GRANT + SET ROLE + REVOKE」，未裁定成员关系是否自动继承。
PostgreSQL 16 支持对**每条成员关系**单独设置 `INHERIT` / `SET` 选项（`pg_auth_members.inherit_option` /
`set_option`）。唯一目标裁定：

```sql
-- 迁移窗口内（由受控编排步骤 / bootstrap 超级用户执行，绑定作业单号；非 ops_admin）
GRANT afs_owner TO afs_migrator WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
-- 窗口内迁移会话：必须显式切换才获得 owner 能力
SET ROLE afs_owner;   -- 执行 DDL
RESET ROLE;
-- 窗口结束：由外部编排层执行（见 5.1.2）
REVOKE afs_owner FROM afs_migrator;
```

- `INHERIT FALSE`：`afs_migrator` 平常连接不自动携带 `afs_owner` 的任何权限，必须主动 `SET ROLE`；
  这样即使迁移凭证在窗口外被使用，也无法静默以 owner 身份操作。
- `SET TRUE`：允许窗口内显式切换到 owner，这是执行 DDL 的必要能力。
- **禁止** `INHERIT TRUE`（自动继承 owner 权限，等于变相长期持有）；**禁止**仅靠 `SET ROLE` 而不设
  `INHERIT FALSE`（PG 默认继承会放大权限）。
- 该选项必须在 `pg_auth_members` 中以 `inherit_option=f, set_option=t` 被 CI 核对（INV-6b）。
- 【取证现状】证据存档 E2 显示当前两库仅有 PostgreSQL 内建 `pg_monitor` 的 3 条成员关系
  （`admin=f, inherit=t, set=t`），业务角色间零成员边——尚无 `afs_*` 角色，故本裁定为阶段 4 实现目标。

#### 5.1.2 异常中断下的回收保障（不只依赖进程 finally）【v1.4】

第四轮复审指出：进程被强制终止、CI Runner 崩溃、主机断电时，迁移进程自身的 `finally` 不保证执行，
残留的窗口授权会让迁移凭证继续通过 `SET ROLE` 取得 owner 权限。根因方案是**把回收责任移出迁移进程**：

| 环节 | 保障机制 | 失败处理 |
| --- | --- | --- |
| 迁移启动前 | 外部编排层查询 `pg_auth_members`，确认**不存在** `afs_migrator → afs_owner` 边；存在即拒绝启动（防遗留） | 报漂移失败，需人工清理后重试 |
| 授权 | 受控编排步骤以 bootstrap 超级用户会话执行带 `ADMIN FALSE, INHERIT FALSE, SET TRUE` 的 GRANT，记录作业单号/时间窗/审批人（`ops_admin` 不参与，§5.1.3） | 无审批留痕不得授权 |
| 执行 | 迁移在独立会话内 `SET ROLE afs_owner` 执行 DDL | — |
| 回收（成功/失败/异常） | **外部编排层**（非迁移进程）在作业终态后执行 `REVOKE` 并复查成员边为空；以作业系统看门狗/超时兜底覆盖进程崩溃 | 未回收：阻止后续所有迁移作业与 IP-3/IP-4 验收，告警 |
| 审计 | 授权与回收两端各留一条审计记录（作业单号、时间、执行身份、成员边状态 before/after） | 缺一端视为漂移 |

- 迁移凭证的有效期由密钥/凭证系统设置短 TTL，作为**纵深防御**（即使回收失败，凭证也会失效）；
  TTL 具体值属阶段 4 运维标准，不在本 ADR 写死。
- CI 常态断言：任意非迁移窗口时刻，`pg_auth_members` 中 `afs_migrator → afs_owner` 边必须为空（INV-6）。

#### 5.1.3 授权能力来源裁定（ADMIN OPTION 边界；v1.4.1 根因重写）【v1.4.1】

第四轮复审指出 v1.4 的两处硬矛盾，全部成立：

1. `GRANT afs_owner TO ops_admin WITH ADMIN OPTION` 未指定 `INHERIT`/`SET`。PG16 的成员关系三选项相互独立，
   新建边不指定时 `INHERIT` 取成员角色的 `rolinherit`、`SET` 默认 TRUE → `ops_admin` 可能自动继承、
   也可 `SET ROLE afs_owner` 直接成为对象所有者，与「仅管理成员关系、不持对象权限」的定位冲突。
2. **ADMIN OPTION 本身就是一条 `pg_auth_members` 成员边**。因此「`ops_admin` 永久持有 ADMIN OPTION」与
   INV-10「`afs_owner` 常态 grantee 为空」**不可能同时成立**。这是 v1.4 引入的逻辑矛盾，必须消除，
   不能靠改写措辞掩盖。

**根因事实（PostgreSQL 原生能力边界）**：`GRANT <role> TO <member>` 要求 grantor 对该 role 持有
`ADMIN OPTION` 或为超级用户；而 PostgreSQL **不提供 grantee allow-list**——持有 ADMIN OPTION 者可把该 role
授予任意角色。所以「保留 ADMIN OPTION + Registry 白名单 + 事后 CI」在数据库层**无法阻止**越界授予，
只能事后发现。**检测不能替代预防。**

**唯一裁定：方案 A —— 移除能力，而不是检测越界**

| 项 | 裁定 |
| --- | --- |
| `ops_admin` 是否持有 `afs_owner` 的 ADMIN OPTION | **否**（v1.4 写法作废）。它对 `afs_owner` 无任何成员边、无 `admin_option` |
| `ops_admin` 是否可 `SET ROLE afs_owner` | **否**（无成员边即无 SET 能力） |
| 谁执行窗口 `GRANT`/`REVOKE` | **集群 bootstrap 超级用户**（当前为 `postgres`），且**只**在外部编排的受控步骤内执行（§5.1.2），不在 CI 业务作业、不在任何业务进程 |
| 是否引入新的授权角色 | **否**。新增 `afs_grantor` 之类身份只是把同一矛盾换个名字，属叠床架屋 |
| 为何可行 | 消除能力后，「非超级用户不能授予 `afs_owner`」由**能力缺失**保证（预防），不依赖 CI 事后发现（检测） |
| `ops_admin` 实际职责 | LOGIN、只读诊断（`pg_stat_activity`/日志）、进程级运维；**需要 DDL 时走与 `afs_migrator` 相同的窗口机制**，不得常驻任何对象权限 |

**复审指令 3.3 七问的逐项裁定**：

| # | 问题 | 裁定 |
| --- | --- | --- |
| 1 | 谁创建 `ops_admin` | bootstrap 超级用户在受控初始化会话创建，审计留痕。当前集群**不存在**该角色（E10.0），属 IP-4 建立项 |
| 2 | 谁授予它管理 `afs_owner` 成员关系的能力 | **无人授予** —— 按方案 A 它不具备该能力；窗口授权由 bootstrap 直接执行 |
| 3 | 是否使用 ADMIN OPTION | **否**（对任何角色都不持有） |
| 4 | 是否允许 `CREATEROLE` | **否**。`CREATEROLE` 会让它绕过方案 A 自行建角色/管角色，必须为 `f` |
| 5 | 能否管理其他数据库角色 | **否**（无 `CREATEROLE`、无任何 `admin_option`） |
| 6 | 谁有权撤销其权限 | bootstrap 超级用户；`ops_admin` 不能自我扩权 |
| 7 | 如何审计 | 窗口 `GRANT`/`REVOKE` 两端由编排层记录作业单号/执行身份/grantee/时间窗；CI 断言「不存在 `roleid=afs_owner` 且 `admin_option=true` 的非超级用户边」（INV-10b） |

**可验证的不变量（替代 v1.4 的 allow-list 声明）**：

- `pg_auth_members` 中 `roleid = afs_owner AND admin_option = true` 的边**集合为空**（超级用户能力不在
  `pg_auth_members` 体现，故不违反本项）；
- `afs_owner` 的成员边常态为空；窗口内 ⊆ {`afs_migrator`}，且 `admin_option=f, inherit_option=f, set_option=t`；
- `ops_admin` 在 `pg_auth_members` 中**既不是** `afs_owner` 的 member，也不持有其 `admin_option`。

【诚实边界（不得省略）】本方案把「授予 `afs_owner` 的能力」集中到**超级用户**这一层。超级用户不受 RLS、
也不受 `FORCE ROW LEVEL SECURITY` 约束，数据库自身无法限制它——这是**不可消除的残余风险**，只能靠
「受控编排 + 短 TTL 凭证 + 双人审批 + 两端审计」降低。本 ADR **不宣称**数据库层能约束超级用户，
也**不得**把本方案表述为「PostgreSQL 强制了 grantee 白名单」。这与 §3 威胁模型（不抵御持有任意 SQL
执行能力的攻击者；D-8 Accepted Risk）一致。该残余风险已登记于附录 E #14。

**可验证的权限不变量（阶段 4 实现，阶段 6 在真实 PG 验证；均为只读查询可核对）**：

| 编号 | 不变量 | 验证方式（真实 PG 目录） |
| --- | --- | --- |
| INV-1 | 所有业务表/序列/函数/Policy 的 OWNER = `afs_owner` | `pg_class.relowner` / `pg_proc.proowner` / `pg_policy` 归属解析为 `afs_owner` |
| INV-2 | 任何 `svc_*` 都不是任何业务对象的 owner | `relowner NOT IN (SELECT oid FROM pg_roles WHERE rolname LIKE 'svc_%')` |
| INV-3 | `pg_auth_members` 中不存在 `member ∈ svc_*` 且 `roleid ∈ {afs_owner, afs_migrator, ops_admin, 其他域角色}`（含所有直接边） | 查 `pg_auth_members`（直接成员边为零） |
| INV-3b | 【v1.4】成员关系的**传递闭包**中，`svc_*` 不能通过任意多层中间角色间接获得 `afs_owner`/跨域角色的权限；**最终有效权限**（`has_*_privilege`）与直接边断言一致 | 递归查询 `pg_auth_members` 传递闭包 + `has_table_privilege`/`has_schema_privilege`/`aclexplode` 最终有效权限比对 |
| INV-4 | 所有 `svc_*`：`rolsuper=f, rolbypassrls=f, rolcreatedb=f, rolcreaterole=f` | 查 `pg_roles` 四个属性位 |
| INV-5 | `afs_owner.rolcanlogin=f` | 查 `pg_roles` |
| INV-6 | `afs_migrator.rolcanlogin=t`；**常态** `pg_auth_members` 中**无** `afs_migrator → afs_owner` 边（仅窗口临时授予、外部编排层回收）；其凭证不出现在 `PROCESS_ALLOWED_DB_ENV` | 查 `pg_roles`/`pg_auth_members`（常态成员边为空）+ `boundary_assertions.cred.process_credential_surface`；授权/回收两端审计留痕由 CI 核对 |
| INV-6b | 【v1.4】窗口内 `afs_migrator → afs_owner` 边若存在，其 `inherit_option=f, set_option=t`；不允许 `inherit_option=t` | 查 `pg_auth_members.inherit_option` / `set_option`（PG16） |
| INV-7 | 需要强制 RLS 的表 `relforcerowsecurity=t` | 查 `pg_class.relforcerowsecurity`（当前取证全为 f，为阶段 4 必改项） |
| INV-8 | `ops_admin` 默认不在业务表 ACL 授予项（例外须登记） | 查 `pg_class.relacl` + 附录 D 第 5 部分显式例外 |
| INV-9 | 【v1.4】`afs_owner` 必须为 `NOLOGIN / NOSUPERUSER / NOBYPASSRLS / NOCREATEDB / NOCREATEROLE`（`rolcanlogin=f, rolsuper=f, rolbypassrls=f, rolcreatedb=f, rolcreaterole=f`）；其成员关系边界满足 INV-3/INV-6 | 查 `pg_roles` 五个属性位；`FORCE RLS` 只对非超级用户/无 BYPASSRLS 的 owner 生效，故本不变量是 INV-7 有效的前提 |
| INV-10 | 【v1.4.1 重写】`ops_admin` 无 `CREATEROLE/CREATEDB/SUPERUSER/BYPASSRLS`；且它在 `pg_auth_members` 中**不是** `afs_owner` 的 member、**不持有** `afs_owner` 的 `admin_option`（方案 A：它不具备授权能力）；`afs_owner` 的 grantee 常态为空、窗口内 ⊆ {`afs_migrator`} 且 `admin_option=f` | 查 `pg_roles` 属性 + `pg_auth_members`：`ops_admin` 相关边为空集；`afs_owner` 的边集合与 `admin_option` 逐条核对 |
| INV-10b | 【v1.4.1 新增；**v1.4.2 补前置**】**先决**：`afs_owner` 必须存在（`role_exists = true`）；在此前提下不存在 `roleid = afs_owner AND admin_option = true` 的**非超级用户**成员边；该集合必须为空 | `SELECT count(*) FROM pg_auth_members m JOIN pg_roles g ON g.oid=m.roleid JOIN pg_roles u ON u.oid=m.member WHERE g.rolname='afs_owner' AND m.admin_option AND NOT u.rolsuper;` 必须为 0 |

> 【诚实边界】上述 INV-1~INV-10b 当前**全部未验证**，其中 INV-1/INV-5/INV-6/INV-7/INV-9/INV-10 按现有取证
> 明确**不满足**（owner 为 postgres、`afs_*`/`ops_admin` 角色不存在、FORCE RLS 为 f）。它们是实现目标，
> 不是现状描述。取证现状见 `docs/audits/2026-11-19-r40-readonly-evidence.md` E2/E3/E4。
> **INV-10b 的空集陷阱（v1.4.2 由 E12.1 实测证实，不再是理论担忧）**：E12.1 实测三个基础设施角色
> **`role_exists = f`**（`afs_owner` / `afs_migrator` / `ops_admin` 在集群中都不存在），E12.2 递归闭包返回
> **0 行**，E12.3 的 INV-10b 直接查询同样返回 **0 行**。也就是说：**一个根本不存在的角色，会「完美通过」
> 所有以它为对象的权限不变量**——`member_of_count=0`、`is_granted_to_count=0`、`admin_edges_held=0`、
> 闭包为空，全部是空集满足（vacuously true）。
>
> **因此所有 INV 的判定式必须写成合取式，而不是单独断言**：
> `role_exists(:role) AND <不变量谓词>`。只写后者会让「未创建」伪装成「已合规」。
> 这条不是文字严谨性问题——E12 的输出就是活生生的反例：三个角色一条记录都没有，
> 而每一条不变量查询都返回 0，极易被读成「全部达标」。
>
> 只有在 `afs_owner` 创建且窗口授权执行之后，INV-10b 才具备判别力。

**登录模型裁定（唯一目标模型，不再保留开放表述）**：采用「**登录角色 = 权限角色**」模型——
八个 `svc_*` 域角色**均为 `LOGIN` 角色**，各自持有独立凭证（如独立口令/证书，经密钥管理注入），
每个域的运行时连接池用本域 `svc_*` 角色建立连接。
- 该模型沿用分支 `DatabaseDomain` 语义，是 §3 威胁模型描述的既有形态；
- **不采用**「NOLOGIN 权限角色 + 独立登录角色映射」模型作为业务运行时方案；不得两种模型并存；
  （`afs_owner` 为 NOLOGIN 是**所有权主体**，不是业务运行时权限角色，二者不冲突。）
- 凭证策略：每域独立凭证（目标态），消除 §3 记录的「8 角色共用 `POSTGRES_APP_PASSWORD`」现状——
  该现状是分支未经真实验证的既有实现，属阶段 4 必须修正项；
- 域与角色的唯一映射由 §11/附录 D 授权注册表登记，连接建立时按域解析角色，禁止硬编码散落。

### 5.2 Schema 与对象授权约束

授权完整性必须覆盖全部对象类型，而非只查表级 GRANT：

| 对象类型 | 要求 |
| --- | --- |
| Schema | `USAGE` 按域授权，跨域 schema 无 `USAGE` |
| Table | 按域授予最小 DML 权限 |
| Sequence | `USAGE` / `SELECT` 按域授权（自增列常见遗漏点） |
| Function | 按域授予 `EXECUTE`；重点审计 `SECURITY DEFINER` 函数 |
| View / Materialized View | 按域授权，并检查其底层定义是否越过域边界 |
| 后续新增对象 | 全部纳入授权完整性检查，无「新增后补充授权」的窗口 |

- **PUBLIC 默认权限**：必须显式回收不必要的 `PUBLIC` 权限（`pg_default_acl` / 各对象 ACL）；
- **SECURITY DEFINER 风险**：即便表访问已隔离，仍可能存在以 definer 身份跨域访问的函数——
  所有 `SECURITY DEFINER` 函数须登记、审计其执行身份与可访问对象，无例外清单视为不合格。
  【v1.3 取证】两库均存在 `outbox.list_active_tenants` 为 `DEFINER`，industrial 库另有
  `audit.assert_transition_event_exists` 为 `DEFINER`；这些须纳入 §5.2 审计清单（附录 C.3）。

### 5.3 RLS 绕过约束【v1.3 对齐身份四层】

必须验证：
- 域角色不是其访问的业务表的 OWNER（所有权归 `afs_owner`，见 §5.1 / INV-2）；
- 域角色无 `BYPASSRLS` / 非 `SUPERUSER`（INV-4）；
- 表所有者不得通过普通业务连接执行跨域访问——`afs_owner` 为 NOLOGIN，无业务连接路径（INV-5）；
- 适用的业务表全部开启 RLS；
- 必须强制 RLS 的表使用 `FORCE ROW LEVEL SECURITY`（防止表 owner 被自身 RLS 豁免）；
  【v1.3 取证】当前两库所有表/分区 `relforcerowsecurity=f`，为阶段 4 必改项（INV-7）。
- 任何 RLS 例外对象必须有明确业务理由与登记记录（`object` / `reason` / `owner` / `unblocks_when`）。

**【v1.4.1 · FORCE RLS 确定性对象清单】**（替代 v1.4 的「需要强制 RLS 的业务表」开放表述；
取自 E10.2 全量对象清单，`public.alembic_version` 除外——它是迁移版本表，不含租户数据，只需 `SELECT` 授权）：

| # | 对象 | 类型 | industrial | verify |
| --- | --- | --- | --- | --- |
| 1 | `asset_service.asset_versions` | 表 | ✓ | ✓ |
| 2 | `asset_service.qc_reports` | 表 | ✓ | ✓ |
| 3 | `audit.status_transitions` | 表 | ✓ | **缺表**（前向补齐） |
| 4 | `auth.revoked_tokens` | 表 | ✓ | ✓ |
| 5 | `outbox.maintenance_runs` | 表 | ✓ | ✓ |
| 6 | `outbox.outbox_events` | 分区父表 | ✓ | ✓ |
| 7 | `outbox.outbox_events_default` | 分区 | ✓ | ✓ |
| 8 | `outbox.outbox_events_y202611` | 分区 | ✓ | ✓ |
| 9 | `outbox.outbox_events_y202612` | 分区 | ✓ | ✓ |
| 10 | `project_planning.episodes` | 表 | ✓ | ✓ |
| 11 | `project_planning.organizations` | 表 | ✓ | ✓ |
| 12 | `project_planning.projects` | 表 | ✓ | ✓ |
| 13 | `project_planning.script_revisions` | 表 | ✓ | ✓ |

- **工业库 13 个对象、验证库 12 个对象**，全部须 `relrowsecurity=t` **且** `relforcerowsecurity=t`；
  当前两库 `relforcerowsecurity` 全为 `f`（E4/E10.7），故 INV-7 明确不满足。
- 任何对象**不**开 FORCE 必须走 D.5 例外登记并说明理由；**不得**用「反正是 owner 自己的表」当理由——
  owner 是 `afs_owner`（NOLOGIN），其 RLS 约束正是靠 FORCE 生效（INV-9 前提下）。
- 该清单是 CI 的**可计算断言输入**：对象集合 + 两个开关位，逐项 diff，不依赖人工解释。

**【v1.4 · 指令 3.1】FORCE RLS 的作用边界（必须准确理解，不得夸大）**：
- `FORCE ROW LEVEL SECURITY` 的唯一作用：让**表所有者自身**也受该表 RLS Policy 约束（默认表 owner 被豁免）。
  因此把它与「owner 为 `afs_owner`（NOLOGIN、无业务连接路径）」配套，才能堵住「所有者绕过」路径。
- **FORCE RLS 对超级用户（`SUPERUSER`）与带 `BYPASSRLS` 属性的角色无效**——PostgreSQL 中这两类角色
  始终绕过 RLS，任何 Policy / FORCE 设置都不能约束它们。因此 INV-9（`afs_owner` 五属性全 false 且
  `rolsuper=f/rolbypassrls=f`）是 INV-7（FORCE RLS）真正生效的**前提**：
  - 若表 owner 仍是 `postgres`（超级用户，当前取证现状），即使开启 FORCE RLS 也不产生隔离效果；
  - 故阶段 4 必须先转 owner 为 `afs_owner` 并满足 INV-9，再开 FORCE RLS，顺序不可颠倒。
- 结论：RLS 隔离有效性 = `域角色非 owner`（INV-2）+ `域角色无 SUPERUSER/BYPASSRLS`（INV-4）
  + `owner=afs_owner 且无提权属性`（INV-9）+ `FORCE RLS`（INV-7）+ `正确 Policy 语义`（附录 D.6）五者合取，
  缺一不可；不得仅凭 `relrowsecurity/relforcerowsecurity` 两个开关位断言隔离成立（复审 P1-3 同一要求）。

【v1.2 新增 · 角色类别区分】运维/验证中必须严格区分以下四类身份（v1.3 与 §5.1 四层对齐，
名称统一为 `svc_*` / `afs_owner` / `afs_migrator` / `ops_admin`），禁止为「让 RLS 测试通过」
而给业务角色临时增加表所有权或 RLS 绕过权限：
1. 普通业务域角色（`svc_*`）；2. 数据库对象所有者（`afs_owner`）；
3. 数据库迁移执行角色（`afs_migrator`）；4. 运维管理员（`ops_admin`）。
测试发现权限不足时，应修正授权注册表（附录 D），不得就地提权（提权即破坏 §11 单一事实源与最小权限）。

### 5.4 授权完整性 CI（以数据库实际授权为验证对象）

当前「新增表无显式 GRANT 时 CI 报错」升级为：

**所有新增数据库对象必须存在明确授权策略；缺少授权策略、超出授权范围、意外继承权限或产生跨域授权时，CI 必须失败。**

CI **不得只解析 SQL 文本**，必须在真实 PostgreSQL 测试环境中读取最终权限状态，检查对象包括但不限于：
`information_schema`、`pg_roles`、`pg_class`、`pg_namespace`、`pg_proc`、`pg_default_acl`
及 PostgreSQL ACL 相关函数（`has_table_privilege` / `has_schema_privilege` / `aclexplode` 等）。

- 验证对象是**数据库实际权限**，不是迁移文件"看起来是否正确"；
- 必须有负向测试证明未授权访问**确实失败**，且负向测试以真实业务域连接身份执行（§12）。

## 6. 迁移历史处置（硬约束，来自实际数据库盘点）

### 6.1 已核实的数据库现状（2026-11-19，ai-film-postgres @ 15432，只读取证）

| 库 | alembic_version | 业务表数 | schema | `asset_versions` 约束 | 角色（集群级共享，见 §6.1.1） |
| --- | --- | --- | --- | --- | --- |
| `ai_film_industrial` | `c1a2b3d4e5f6`（main head） | 13（+1 分区父表） | 含 `audit` | 3：pk + **`ck_asset_versions_status`** + `fk_asset_versions_project_tenant` | 同一套集群角色（引用 §6.1.1，不按库计数） |
| `ai_film_verify` | `b3c4d5e6f7a8` | 12（+1 分区父表） | 无 `audit` | 2：pk + `fk_asset_versions_project_tenant`（**缺状态 CHECK**） | 同一套集群角色（与上一行是同一批，**不是各有一份**） |
| `postgres` | 无 alembic_version | **系统库** | — | — | **不参与业务迁移对账** |

> 【v1.4 补充修正 · 角色不是数据库级对象】上表右列此前写作「main 4 + 分支 8 svc_*（混跑）」两行各写一次，
> 容易误读为「两个库各有 12 个角色」。**PostgreSQL 的 role 是集群级（cluster-wide）对象**，两库共享同一套身份。
> 正确表述：**集群共 12 个业务角色（main 4 + svc 8），两库共享**。详见 §6.1.1。

### 6.1.1 证据范围：集群级角色事实 vs 数据库级对象事实【v1.4 · 按补充复审修正】

【v1.4 补充修正，根因级】PostgreSQL 的部分系统目录是**共享目录（shared catalog）**，整个实例只有一份；
另一部分目录（对象、ACL、Policy）是**每库独立**的。若不区分，就会把同一批角色重复统计成两份。本轮裁定：

| 事实类别 | 承载 catalog | 作用范围 | 记录方式 |
| --- | --- | --- | --- |
| 角色存在与属性（LOGIN / SUPERUSER / BYPASSRLS / CREATEDB / CREATEROLE / INHERIT） | `pg_authid` → `pg_roles` | **集群级**（`relisshared=t`） | **只记一份集群事实**，禁止按库重复计数 |
| 角色成员关系（含 INHERIT/SET/ADMIN OPTION） | `pg_auth_members` | **集群级**（`relisshared=t`） | 只记一份 |
| 数据库清单 | `pg_database` | **集群级**（`relisshared=t`） | 只记一份 |
| Schema / Table / Sequence / Function / View 及 OWNER | `pg_namespace` / `pg_class` / `pg_proc` | **数据库级**（每库独立） | 两库分别记录 |
| 对象 ACL / 默认 ACL | `pg_class.relacl` / `pg_default_acl` / `aclexplode` | **数据库级** | 两库分别记录 |
| RLS Policy | `pg_policy` / `pg_policies` | **数据库级** | 两库分别记录 |
| 活跃会话 | `pg_stat_activity` | **数据库级 + 时间点快照** | 标注库名与时刻，不外推 |

**本轮只读验证（2026-11-19，同实例 `ai-film-postgres @ 15432`，证据存档 E9）**：

- `pg_authid` / `pg_auth_members` / `pg_database` 的 `relisshared = t` → 确为集群级；
- 两库 `pg_roles` 业务角色均 **12 个**，8 个 `svc_*` 名称逐字相同 → 是**同一批角色**，不是每库各 12 个（合计 24 个是错误统计）；
- 两库 `pg_auth_members` 均 **3 条**（仅 PostgreSQL 内建 `pg_monitor` 三条），业务角色之间零成员边 → 同一集群事实；
- 两库对象级事实**确实不同**：`relkind IN ('r','p')` 计数 14(`industrial`) vs 13(`verify`)，即 §6.1 所述
  「13 张业务表 + 1 个分区父表」与「12 张业务表 + 1 个分区父表」；`pg_policy` 11 vs 10、schema 6 vs 5 →
  属数据库级，两库分别记录正确。

**禁止表述**：① 「两个库各有 12 个角色」（重复统计）；② 「verify 库拥有 8 个 svc_*」而不说明这是集群级共享身份
（易被误读为 verify 库私有）。**正确表述**：「集群共 12 个业务角色（main 4 + svc 8），`ai_film_industrial` 与
`ai_film_verify` 共享该身份集；两库的差异体现在对象所有权 / ACL / Policy / 会话等数据库级事实上」。

**关键结构证据**：`ai_film_verify` 标记 `b3c4d5e6f7a8`，但其 `asset_versions` **缺**
`ck_asset_versions_status`（状态 CHECK）、且全库**无**状态转移表 / Phase1 表——
证明该库执行的是 **main 版 b3（资产归属）**，而非分支版 b3（状态约束）。
`ai_film_industrial` 则同时具备资产归属与状态 CHECK。这是「Revision ID 相同 ≠ 迁移内容相同」的直接结构证据。

【v1.3 · P1-3 系统库边界澄清】`postgres` 是 PostgreSQL 系统库，无 `alembic_version`、
不承载业务表，**不属于业务迁移验证/对账对象**。§6 与附录 C 的结构对账、前向补齐与验收矩阵
只针对 `ai_film_industrial` 与 `ai_film_verify` 两个业务库；不得把系统库与两个业务库混为同一验收对象。
本轮仅确认其存在，未对其做任何取证或变更。

### 6.2 三个 Revision 冲突处置原则

1. Alembic 只记 revision ID，不校验迁移文件内容。`ai_film_verify` 已用 `b3c4d5e6f7a8`
   记录了 main 版内容；若把分支状态约束迁移挂同一 ID，该库会误判已是最新而**永久跳过**状态约束。
2. **不得**假设改文件名/改 ID 就能修复已执行数据库。
3. 三种情况的处置原则：
   - 已在持久化数据库执行的迁移 → 保留原 Revision 与语义；
   - 从未执行且未发布的分支迁移 → 可重新规划 Revision，但必须保留原始变更意图；
   - 与已存在 Revision 冲突的分支业务变更 → 通过全新唯一 Revision 前向实现。
4. 两套业务变更（main 的资产归属 + 分支的状态约束）**都要保留**，分别映射到唯一、连续的迁移。
5. **不为了让 Alembic 变成一条漂亮的线而压平历史**：若数据库已存在不同历史路径，
   必须明确安全收敛方案；必要时采用显式分支合并迁移，但两个 Revision 不得承担同一身份。
6. **不得只以 `alembic_version` 判断数据库是否已具备某项能力**——`ai_film_verify` 已证明
   Revision 相同不等于内容相同。验收以数据库实际结构与数据状态为准。
7. 【v1.2 保守原则】对无法证明执行来源的冲突迁移（尤其 b7c8、9e3b），**不得自行推定其未执行**；
   一律保守处理：不修改历史身份，目标架构通过新的唯一 Revision 补齐并验证双方业务效果。

### 6.3 分支验证缺失（不得视为完成态）

分支最新提交 `59b13af` 明确记录 Docker/PostgreSQL 不可用，部分测试与 Alembic 验证**未执行**。
因此分支安全模型目前**未经真实数据库验证**——这正是阶段 6 必须补的证据。

### 6.4 迁移实验安全边界（v1.2 修正回滚测试策略）

【v1.2 修正】原「真实库 upgrade → downgrade → upgrade」策略有破坏真实验证库的风险，
改为按数据库用途分级，**禁止在真实验证库直接执行可能破坏数据的 downgrade**：

| 数据库 | 允许的实验 | 禁止 |
| --- | --- | --- |
| **全新一次性 PostgreSQL 数据库**（可销毁） | 完整 `upgrade head → downgrade → re-upgrade` 循环，用于验证迁移可逆性 | — |
| `ai_film_industrial` | **备份后**验证前向升级 `upgrade head` | 直接破坏性 downgrade |
| `ai_film_verify` | **备份后**验证前向升级；通过备份恢复演练验证恢复方案 | 直接 downgrade；任何可能删除数据/约束/业务对象的操作 |
| 数据库克隆（clone） | 破坏性回滚实验、异常恢复演练 | 在原库直接演练 |

- 迁移可回滚性必须验证，但**不得用真实验证库的数据安全换取回滚测试结果**；
- 所有破坏性回滚实验必须在数据库克隆或一次性数据库中执行；
- 前向升级验证前必须先备份，并确认备份可恢复（备份有效性本身需验证）。

### 6.5 角色与依赖盘点（承接 §6.1，禁简单 DROP；v1.4 按证据范围拆分）

`ai_film_verify` 是 F01-F05 安全重构的真实验证库，不得当临时库处理。按 §6.1.1 的证据范围，把盘点拆成
**集群级角色事实（一份）** 与 **数据库级对象依赖（两库分别）**，避免重复统计与身份/授权混淆。

**表 A · 集群级角色身份事实（整个实例一份，不按库重复）**

| 事实项 | 结论 | 证据 |
| --- | --- | --- |
| 角色集合 | 集群共 **12 个业务角色**：main 4（`app_role`/`asset_service_role`/`outbox_relay_role`/`project_planning_role`）+ 分支 8 `svc_*` | E2 / E9；两库查询逐字相同 |
| 角色属性 | 12 个角色均 `rolsuper=f / rolbypassrls=f / rolcreatedb=f / rolcreaterole=f` | E2 |
| 成员关系 | `pg_auth_members` 共 3 条，均为 PG 内建 `pg_monitor`；**业务角色之间零成员边** | E2 / E9 |
| 目标基础设施角色 | **不存在** `afs_owner` / `afs_migrator` / `ops_admin` | E2 |

**表 B · 数据库级对象依赖与权限（两库分别记录）**

| 盘点项 | `ai_film_industrial` | `ai_film_verify` | 证据 |
| --- | --- | --- | --- |
| 对象 OWNER | 业务表/分区 owner 全为 `postgres`；12 业务角色均非 owner | 同左 | E3 |
| 遗留 GRANT / ACL | 见 E5（默认零授权已生效） | 见 E5（asset/project 仍 `arwd` 自动授权，**未补齐**） | E5 |
| RLS Policy | 11 条（含 audit 表） | 10 条（缺 audit 表 1 条） | E4 |
| 活跃会话（时间点快照） | 取证时刻无业务角色会话（仅 `postgres/psql` 取证连接） | 同左 | E8 |
| 当前会话依赖 | 快照时刻无 | 快照时刻无 | E8 |

**证据强度分级（严格区分，禁止互相替代）**：

| 证据类型 | 本轮结论 | 能证明 | **不能证明** |
| --- | --- | --- | --- |
| ① 角色存在 | 12 个业务角色存在（集群级） | 角色被创建过 | 由哪个版本/迁移/人工创建（**创建来源不可证**） |
| ② 角色创建来源 | **不可证** | — | 不能据现状还原 9e3b 哪一版执行过 |
| ③ 数据库对象依赖 | 两库均无业务角色为 owner；ACL 见 E5 | 当前对象层无所有权依赖 | 不覆盖未检查的临时对象 |
| ④ 当前会话 | 取证时刻无业务角色会话 | **仅该时刻**无活跃连接 | 其他时间、其他实例是否有连接 |
| ⑤ 仓库静态引用 | 运行时 `config.py` 仅配 main 4；8 个 `svc_*` 在 packages/workers/api/infra/migrations/tests 中 0 引用 | **已检查范围**内无静态引用 | 仓库外部署配置、CI/CD 密钥、外部 Worker、定时任务、运维脚本 |
| ⑥ 外部部署与历史运行依赖 | **未检查（证据缺口）** | — | 不能宣称不存在 |

【v1.4 · 证据表述收紧（按补充复审）】**不得**把上述结论扩大解释为「所有运行时依赖均不存在」。
准确表述是：**已完成「仓库与数据库可观测范围内」的依赖盘点；外部部署配置、CI/CD 密钥管理、外部 Worker、
定时任务、运维脚本与历史连接日志尚未检查，属证据缺口。**「无当前活跃会话」只证明取证时间点状态；
「仓库 0 引用」只证明已检查范围内无静态引用。

【v1.4 · 盘点目的澄清（按所有者裁决 D-9 纠正）】此盘点**不是**为「退役 `svc_*`」做准备——
8 个 `svc_*` 是选型 B 的**目标运行时角色**（§5.1），不是待退役对象。盘点真实用途是：
在 IP-4 按 Authorization Registry 做**目标态核对**时提供依赖侧基线。
§7 的 Expand-Contract **仅针对 main 4 个旧应用角色**（§7.0.1），不适用于目标 `svc_*`。

**所有者裁决（§13 D-9）**：保留这 8 个目标角色，不在阶段 3 退役；本轮不执行任何 DROP/REVOKE/重建同名。
**保留现状 ≠ 认可现状**——角色属性、凭证、ACL 的验收归 IP-4（见 §13 D-9 的「保留不等于认可」表）。
进入 IP-4 后依 Registry 逐项校准（名称/域映射/LOGIN/独立凭证/成员关系/实际 ACL/连接与使用/INV-1~INV-10b），
符合目标态则复用并做权限治理，存在不符合的旧授权则按实际差异受控调整。

## 7. 角色退役顺序（Expand-Contract，禁止一次性切换）

### 7.0 适用范围裁定（v1.4 · 按所有者决策 D-9，勿混淆两类角色）

**关键澄清**：8 个 `svc_*` 是选型 B 的**目标运行时角色**（§5.1），
本节 Expand-Contract 的退役对象**只针对 main 侧 4 个旧应用角色**，**不适用于目标 `svc_*`**。
两者不得混为一谈——退役目标角色等于自毁目标架构。

| 对象 | 当前决定 | 依据 / 处理阶段 |
| --- | --- | --- |
| 8 个目标域角色 `svc_*` | **保留**，不在阶段 3 退役 | IP-4 依 Registry 做目标态核对与权限治理（§13 D-9） |
| main 4 个旧应用角色（`app_role`/`project_planning_role`/`asset_service_role`/`outbox_relay_role`） | 按本节 Expand-Contract **逐个**迁移并退役（见下表） | §7 四步 + 逐角色计划；退役条件见 §7.1/§7.2 |
| 来源不可证的历史角色 | 不推定来源，逐项核验 | §6.5 六项盘点（E8）作依赖基线；保守原则 |
| 已确认无依赖的非目标角色 | 后续按实际身份单独制定退役方案 | 不属于本 ADR 当前授权范围 |

**v1.4.1 修正**：v1.4 只写「`DROP app_role`」，但 C.3.5 把四个角色都定义为过渡角色——两者不一致。
本节给出**逐角色**完整生命周期（复审第六节要求）。四个角色都是最终态的**淘汰对象**，没有需要长期保留的角色；
`app_role` 是跨域通用角色、无 1:1 目标，因此**最后**退役。

### 7.0.1 逐角色生命周期（唯一裁定表）

| 旧角色 | 目标替代 | 当前运行时使用（证据） | 保留到何时 | 何时停止使用 | 何时 `REVOKE` | 何时 `DROP` | 回滚条件 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `app_role` | **无 1:1**：由各域 `svc_*` 共同取代（当前已具备的是 `svc_project_planning` + `svc_asset`） | `config.postgres_app_user`，API/共享连接池默认身份（`config.py`） | 最后一个域切换完成后的**下一个发布版本** | 所有域连接池均改用 `svc_*`，`postgres_app_user` 从配置与密钥清单移除 | Contract 步骤（停用后、DROP 前） | **最后**执行；条件：无任何可回滚运行时版本依赖它 + Verify 全绿 | 从备份恢复角色定义与 ACL；或按前向重建脚本重建（`9e3b` main 版可重放） |
| `project_planning_role` | `svc_project_planning` | `config.postgres_project_planning_user`（项目域连接池） | 项目域切换完成后的下一个发布版本 | 项目域连接池改用 `svc_project_planning` | 该项目域 Contract 步骤 | 该项目域 Verify 全绿且无可回滚版本依赖 | 同域回滚：恢复该角色与其 4 张表授权（`SIU`/`SIUD` 视库而定） |
| `asset_service_role` | `svc_asset` | `config.postgres_asset_service_user`（资产域连接池） | 资产域切换完成后的下一个发布版本 | 资产域连接池改用 `svc_asset` | 该域 Contract 步骤 | 该域 Verify 全绿且无可回滚版本依赖 | 同上；注意 `asset_versions` 目标为 `SIU`（无 DELETE） |
| `outbox_relay_role` | `svc_outbox_relay` | `config.postgres_relay_user`（relay 进程专用） | relay 切换完成后的下一个发布版本 | relay 改用 `svc_outbox_relay` | relay Contract 步骤 | relay Verify 全绿且无可回滚版本依赖 | 同上；relay 仅需 outbox `SELECT` |

### 7.0.2 退役顺序约束（不得并行乱序）

1. 按域逐个 Contract：`project_planning_role` → `asset_service_role` → `outbox_relay_role`（relay 依赖 outbox
   写入方稳定后再切）；每个域独立 Verify。
2. `app_role` **最后**退役：它是跨域兜底，任一个域未切换完就 DROP 会导致该域连接失败。
3. **DROP 顺序与依赖方向相反**：先 DROP 无依赖的域角色，最后 DROP `app_role`。
4. 任一角色 DROP 前必须重新跑一次 `pg_stat_activity` + 代码引用检索 + 部署配置核对（§6.5 证据强度分级），
   **不得**仅凭「上次盘点是空的」就删。

### 7.0.3 与 `svc_*` 治理的边界

- 4 个旧角色的退役属 §7 Expand-Contract；8 个 `svc_*` 的校准属 IP-4 目标态核对。两者**并行但不互相替代**：
  旧角色退役不会自动让 `svc_*` 合规，`svc_*` 合规也不以旧角色消失为前提。
- 阶段 3（IP-3）**只**重建迁移历史；4 个旧角色的 `REVOKE`/`DROP` 均属阶段 4（IP-4）及之后的发布动作，
  **不得**提前放进阶段 3。

### 7.0.4 DROP 前的跨库依赖验收（v1.4.2 新增，PostgreSQL Role 是集群级对象）

§7.0.2 第 4 条只要求「重跑 `pg_stat_activity` + 代码引用 + 部署配置」——这**只覆盖了会话与代码**，
不足以支撑 `DROP ROLE`。因为角色是**集群级**对象，而权限与所有权是**库级**对象事实：

- 角色可能在 `ai_film_industrial` 上没有依赖，却在 `ai_film_verify` 上仍是某张表的 OWNER 或 ACL 的 grantee；
- 反过来也可能只在一个库有默认权限依赖（`pg_default_acl`）；
- 只要**任一库**存在依赖，`DROP ROLE` 就会失败（或更糟：在有人先 `REASSIGN` 之后静默丢权限）。

因此 **DROP 闸门定义为「双库 + 集群级」四组证据全部为空/为真**，缺一不得执行：

| # | 检查项 | 作用域 | 判据（全部必须满足） |
| --- | --- | --- | --- |
| 1 | 对象所有权依赖 | **两个业务库各自** | `pg_class`/`pg_namespace`/`pg_proc`/`pg_type` 中 `owner = :role` 的计数 = **0**（C.5.3 ③④） |
| 2 | ACL 授权依赖 | **两个业务库各自** | `relacl`/`nspacl`/`proacl`/`datacl` 展开后 `grantee = :role` 的条目 = **0**（C.5.3 ⑥⑬、C.5.2 datacl） |
| 3 | 默认权限依赖 | **两个业务库各自** | `pg_default_acl` 展开后 `grantee = :role`，或 `defaclrole = :role` 的条目 = **0**（C.5.3 ⑧） |
| 4 | 成员关系依赖 | **集群级（一次）** | 该角色在 `pg_auth_members` 中**既无入边也无出边**（`member`/`roleid` 双向计数 = 0，含传递闭包） |
| 5 | 活跃会话 | **集群级（一次）** | `pg_stat_activity` 中 `usename = :role` 的会话数 = **0**（含所有库、所有应用名） |
| 6 | 可回滚运行时依赖 | 仓库 + 部署侧 | 「运行时版本 → 依赖角色」对照表中**没有任何仍可回滚的版本**引用该角色（§7.2） |
| 7 | 外部部署 / Worker / 定时任务 / 密钥 | 部署侧 | 已核验且留证（§6.5 已标注为**未检查缺口**，此处必须补齐后才可 DROP） |

**执行顺序与不可逆性**：

1. 逐库跑 #1~#3（同一角色，**两个库都要跑**，任一行非空即中止）；
2. 跑 #4~#7（集群级 + 部署侧）；
3. 全部为空/为真后，先 `REVOKE`（可回滚），再 `DROP`（不可逆）；
4. `DROP` 前后各存一份 C.5.3 输出作为证据，纳入发布记录。

> 反例（必须避免）：因为 `ai_film_industrial` 上查不到依赖就 `DROP app_role`——而 `ai_film_verify`
> 的 `project_planning` 四表仍有 `D` 授权（E10.5 实测差异），该 DROP 会直接失败或留下悬空授权。
> 这也解释了为什么 §7.0.2 的「逐个 Contract」必须以**双库**为单位，而不是单库。

分支 `c2d3e4f5a6b7` 最终 `DROP app_role`，但运行时/连接池/授权模型必须同步切换。
采用四步，不得合并为一次不可控切换：

1. **Expand**：创建新 8 域角色并按域授权；保留 `app_role` 及旧应用角色。
2. **Deploy**：部署使用新域角色的运行时（API/各域服务/worker/relay/定时任务全覆盖）。
3. **Verify**：验证所有域的连接/查询/写入；确认无旧角色连接与依赖（含 relay、worker、定时任务）。
4. **Contract**：确认无残留后，才按 7.0.1/7.0.2 的逐角色顺序 `REVOKE` + `DROP`（`app_role` 最后）。

### 7.1 失败回滚条件

| 阶段 | 失败条件 | 处理原则 |
| --- | --- | --- |
| Expand | 新角色授权不完整 | 不部署新运行时 |
| Deploy | 任一域连接或业务调用失败 | 保留旧角色，不执行 Contract |
| Verify | 发现旧角色仍被使用 | 不得退役旧角色 |
| Contract | 删除旧角色后发现遗留依赖 | 视为发布失败，按预先设计的恢复方案处理 |

### 7.2 运行时回滚 ≠ 数据库授权回滚

新运行时已使用八域角色、数据库已创建新权限，但部署失败需恢复旧版本时，
**旧版本必须仍然能够运行**。因此明确：

- **旧角色保留到哪个版本**：旧 `app_role` 至少保留到新运行时稳定通过 Verify 之后的下一个发布版本；
- **哪些运行时版本依赖旧角色**：所有仍可被回滚到的历史运行时版本均视为依赖旧角色，
  维护一张「运行时版本 → 依赖角色」对照表；
- **何时允许 DROP**：仅在确认无任何可回滚运行时版本依赖旧角色、且 Verify 全绿后；
- **DROP 之后如何处理紧急恢复**：预先定义恢复方案（从备份恢复角色定义 / 前向重建脚本），
  并纳入发布 runbook。

**角色退役与业务代码部署不得作为原子操作。**

### 7.3 发布回滚窗口标准（v1.2 新增）

【v1.2 新增】ADR 不写死具体小时数，但要求阶段 4 形成**可审核的发布标准**并写入发布 runbook，
至少定义：
- 新运行时需稳定运行的观察窗口时长；
- 观察哪些错误指标（连接失败率、越权拒绝率、域间调用错误等）及阈值；
- 哪些历史运行时版本仍在允许回滚范围内（对应 §7.2 依赖表）；
- 最终 `DROP app_role` 的批准人（具名角色）与批准留痕要求。

## 8. 实施顺序【v1.3 · P1-4 阶段闸门结构性重构】

> 【流程澄清】「文档修订完成」与「所有者最终批准」是两件事：本 ADR 每轮复审后修订，
> 但状态始终为 **PROPOSED**，**未获最终批准**；阶段 3 须经所有者放行后方可启动。

| 阶段 | 内容 | 该阶段实现验收条件（IP） |
| --- | --- | --- |
| 0 ✅ | 冻结保护两分支与工作区 | 保护标签 main 5352d19 / 分支 59b13af 已打，原历史未改 |
| 1 ✅ | 盘点实际库 Alembic/角色/RLS 状态 | 见 §6.1 与附录 C 证据 |
| 2 📝 | 本 ADR（统一授权模型） | v1.4.2 第五轮复审后修订完成，PROPOSED 待第六轮复审（非已批准；DP3/DP5 仍未通过，DP2 基本通过待确认） |
| 3 🔒 | 重建目标迁移历史 | 先满足 §8.1-A **设计放行条件 DP1~DP5**；再按 §8.1-B IP-3 验收（唯一 Revision、链无多头、一次性库演练通过） |
| 4 | 统一运行时角色/连接池/GUC/RLS | IP-4：§5 INV-1~INV-10b 在真实 PG 成立；三 GUC 统一注入；无双轨（环境/责任边界见 §8.1-B） |
| 5 | 吸收 main 治理成果 | IP-5：§1 保留清单不回退；869 测试 + 四闸门全绿 |
| 6 | PostgreSQL 真实验证 | IP-6：E1 七项全部有真实执行证据；§5.4 实际权限检查；漂移检测通过 |
| 7 | Phase 1 重新验收 | IP-7：A3/B1/B2/C6/D1 逐项关闭或正式裁定；E1 独立通过 |
| 8 | 新 PR 审查合并 | 不在旧分支原地 merge |

### 8.1 阶段闸门双层定义【v1.3 · P1-4 核心修订】

v1.2 用一个「G1~G5 全过」同时表达「设计裁定完成」与「实现完成」，导致 §8.1 写「文档已定义、
实现待验证」却又要求全部通过才能进阶段 3 的自相矛盾，并与附录 E「Registry 未实现」冲突。
本轮**从根上拆为两类互斥闸门**，不得以其中一类代替另一类：

- **A 类 · 阶段 3 设计放行条件（DP，Design-Pass）**：阶段 3 启动**之前**必须满足；
  只要求目标架构决策明确、无开放决策、方案与验收标准确定、真实证据采集到位；
  **不要求**提前完成本应属于阶段 4/6 的运行时实现。
- **B 类 · 各阶段实现验收条件（IP，Implementation-Pass）**：在对应阶段完成真实实现并提供证据，
  作为进入下一阶段的前提（见 §8 表 IP-3~IP-7）。
- **Registry 三态明确区分**（消除 v1.2 的 G5/附录 E 矛盾）：
  ① **设计完成**（DP5，阶段 3 前；v1.4 仍待补充资源 ID/RLS 语义等定义，见 §8.1-A）；
  ② **实现完成**（IP-4，阶段 4：Registry 文件 + 派生器 + CI 一致性校验落地）；
  ③ **真实数据库一致性验证**（IP-6，阶段 6：真实 PG 上实际权限与 Registry 无漂移）。

#### 8.1-A 阶段 3 设计放行条件（DP1~DP5，全部满足 + 所有者放行方可启动阶段 3）

【v1.4 · 第四轮复审重定】按 v1.3 自定标准与复审判断，DP1/DP4 满足，DP2/DP3/DP5 **待补充**——
即"设计放行条件"本身尚未全部满足，阶段 3 不得放行。

| 闸门 | 设计放行条件（只验设计/证据，不验运行时实现） | 落点 | 复审判断（v1.4.2） |
| --- | --- | --- | --- |
| DP1 | 威胁模型与三个 GUC 的可伪造边界定义一致、无「某 GUC 受更强保护」的暗示 | §3 / §4.1 / §4.4 / §4.5 / §12 | **满足**：三 GUC 口径已统一（复审认可） |
| DP2 | 身份四层模型定型：LOGIN/所有权/迁移身份/成员关系/权限不变量全部明确，无开放决策 | §5.1 / §5.3（INV-1~INV-10b） | **第五轮：基本通过**（`ops_admin` 的 ADMIN OPTION 矛盾已消除）。v1.4.2 补齐 §5.1 身份表中 `ops_admin` 的边界表述——「可发起/审批运维与 DDL 请求，但不 `GRANT`、不 `SET ROLE`、不执行 DDL」，与 §5.1.3 方案 A 一致；`afs_migrator` 窗口边固定 `ADMIN FALSE, INHERIT FALSE, SET TRUE`。**仍标待复审确认**，不自行判定满足 |
| DP3 | 迁移证据矩阵齐全：逐库逐 Revision 十列矩阵 + **可计算**结构验收 + 完整可复现取证 | 附录 C（C.2 / C.3 / C.5 + 证据存档） | **第五轮：未通过**（有效权限查询漏分区父表、缺递归成员闭包与全对象有效权限、E0~E10 未随附件提供）。v1.4.2 已修 ⑨（`relkind IN ('r','p')`）、新增 ⑬⑭⑮、C.5.2 补递归闭包与基础设施角色属性核查；证据存档作为**独立交付物**提供（E0~E11，1657+ 行）。**仍需用修正后的查询重采两库证据后才可判满足，本轮不自行判定**。**（v1.4.3）新增三项可复算证据，但仍不判满足**：① 双路径全量重放（`industrial` 与 main 链各 1203 行 `pg_dump --schema-only`，仅 2 行 `\restrict` 随机 token 不同；ACL 逐行一致）→ 证明 **main 链可复现、分支链无结构残留**；② 10 个分叉 Revision 的归一化等价性由程序判定（C.1.1，7 个 T2 残差为 0）；③ 附录 C.6 归属约束脏状态实验（49 步全绿）。三项都只把「证据」做实，不改变 DP3 的判定——DP3 要求的是**逐库逐 Revision 十列矩阵全部可计算**，而 C.2 仍是 6 行 × 10 列，未覆盖新增的 T2/T4 项 |
| DP4 | 迁移实验安全边界确定：真实验证库仅前向验证，破坏性回滚在克隆/一次性库 | §6.4 | **满足**：策略已确定（复审认可） |
| DP5 | Authorization Registry 设计定义完成：唯一载体/**无矛盾**资源 ID/RLS **可执行 AST**/操作枚举/派生校验/漂移检测明确 | 附录 D（D.1~D.6） | **第五轮：未通过**（AST 数组解析错误、Policy SQL 模板非法、域角色双重映射、SECDEF 搜索路径不足）。v1.4.2 已重写 D.6.0c 派生 0/1/1b/1c/2a/2b 并给可执行 SQL 样例；D.2.1 删除 `roles.mappings` 消除重复事实源；D.5 目标 `search_path=pg_catalog, pg_temp` + 迁移先 REVOKE PUBLIC。**生成器未实现，不自行判定满足**。**（v1.4.3）唯一变化是「载体不再是纯设计」**：`authorization_registry.yaml` 已落地 337 行，配 27 例单测与契约闸门第 12 项，`design_delta` DD-1/DD-2 已在文件头登记（详见 D.1）。但 DP5 判的是「设计定义完成」，其中 **AST→SQL 生成器仍未实现**、漂移检测仍未落地，故**判定不变（未通过）**。**载体存在不能被读作 DP5 达成的证据**——这正是 E13(C.6) 揭示的同一类错误：把「对象存在」当成「不变量成立」 |

> 本轮不宣称 DP 全部满足——DP2/DP3/DP5 的补充需经第四轮复审确认。DP 满足只代表「可规划阶段 3」，
> 不代表阶段 3 已获批，也不代表任何实现已完成。阶段 3 启动仍需所有者明确放行。

> DP 满足只代表「可以启动阶段 3 的设计与规划」，**不代表阶段 3 已获批准，也不代表任何实现已完成**。
> 阶段 3 启动仍需所有者明确放行；启动后按 §8.1-B IP-3 验收。

#### 8.1-B 各阶段实现验收条件（IP，进入下一阶段前必须提供真实证据）

【v1.4 · P1-4】复审指出 IP-4/IP-6、IP-6/IP-7 存在验收重叠。本轮为每阶段明确
**验收环境 / 责任方 / 证据边界**，三者互不替代：

| 阶段 | 编号 | 验收环境 | 责任方 | 验收内容（证据边界） |
| --- | --- | --- | --- | --- |
| 3 | IP-3 | **一次性库**（可销毁） | 迁移实施 | 迁移历史重建：三冲突 Revision 处置落地；alembic head 唯一无多头；按附录 C.3 在一次性库完成 upgrade/downgrade/re-upgrade 演练；系统库不参与。**边界：只验迁移可逆与结构，不验运行时授权** |
| 4 | IP-4 | **阶段 4 集成分支的测试 PG**（非生产、非最终验收库） | 运行时实现 | 角色/连接池/GUC/RLS 统一实现：INV-1~INV-10b 成立；三 GUC 统一注入且 §4.3 四场景通过；无双轨；`svc_*` 独立凭证；**Registry 文件 + 派生器 + CI 校验机制实现完成（DP5 ②态）**。**边界：验实现是否落地，用构建期/测试期证据；漂移检测机制就位但不做最终快照签署** |
| 5 | IP-5 | 集成分支 CI | 治理集成 | 吸收 main 治理成果：§1 保留清单（kid/JWKS/状态机/契约闸门/869 测试）不回退 |
| 6 | IP-6 | **真实验收 PostgreSQL**（独立、最终证据库） | 安全验证（独立于实现方） | **独立最终技术验收**：E1.1~E1.7 技术证据全部采集；§5.4 实际权限检查；Registry 与真实权限一致性**最终快照无漂移（DP5 ③态）**；b7c8/9e3b 前向补齐后结构符合 C.3。**边界：负责 E1 技术证据，不做 Phase 1 正式签署** |
| 7 | IP-7 | Phase 1 验收评审 | 所有者/验收委员会 | **Phase 1 正式验收与签署**：引用 IP-6 的 E1 技术证据并独立确认 E1 通过；A3/B1/B2/C6/D1 逐项关闭或正式裁定。**边界：只做正式验收与签署，不重复执行 E1 技术测试**（避免与 IP-6 重复） |

**三个重叠点的裁定**：
1. **IP-4 vs IP-6（真实 PG 权限验证）**：IP-4 在**阶段 4 测试 PG**做实现期验证，证明机制落地；IP-6 在**独立最终验收库**做最终证据采集——环境与证据不同，IP-4 通过不替代 IP-6。
2. **IP-6 vs IP-7（E1）**：IP-6 负责完成 E1 技术证据（执行测试、产出结果）；IP-7 负责 Phase 1 正式验收与签署（审查并采纳 IP-6 证据），不重复执行测试。
3. **IP-4 vs IP-6（Registry）**：IP-4 完成 Registry 文件、派生器与 CI 校验机制（②态）；IP-6 在真实库验证实际权限快照与 Registry 无漂移（③态）。

> 不得为通过阶段闸门而提前实现尚未到期的阶段任务（如在阶段 3 前实现运行时 RLS），
> 也不得把未实现内容标记为完成。DP 与 IP 是不同对象，互不替代。

## 9. 反对的做法（明确禁止）

- 直接整树 merge 两分支逐块解冲突（已证实产生 config.py 双进程机制等跨文件语义不一致）。
- 仅靠改 revision 文件名处理冲突迁移（§6.2 硬约束）。
- 长期保留两套组织 GUC / 两套进程身份机制 / 两套角色体系。
- 把数据库角色隔离表述为完整服务级安全隔离（§3）。
- 以静态审查、模拟 SQL 或 CI 绿灯替代真实 PostgreSQL 安全证据。
- 因选择 B 而回退 main 的任何治理成果。
- 【v1.1】只查表级 GRANT 即宣称授权完整（须覆盖 §5.2 全部对象类型 + PUBLIC 权限 + SECURITY DEFINER）。
- 【v1.1】只以 `alembic_version` 判断数据库能力（§6.2 第 6 项）。
- 【v1.1】为压平迁移历史而让两个 Revision 承担同一身份（§6.2 第 5 项）。
- 【v1.1】在 verify 库上简单 `DROP svc_*` 角色（§6.5）。
- 【v1.1】把运行时回滚当成数据库授权回滚（§7.2）。
- 【v1.2】把自定义 GUC 当作不可伪造凭证，或以「加了校验函数」名义扩大安全承诺（§4.5）。
- 【v1.2】把 LOGIN/NOLOGIN 模型、迁移所有者归属留到实施阶段临时决定（§5.1）。
- 【v1.2】在真实验证库直接执行破坏性 downgrade（§6.4）。
- 【v1.2】用超级用户身份执行负向测试替代真实业务域连接（§12）。
- 【v1.3】只对项目 GUC 声明伪造边界、暗示租户/组织 GUC 受更强数据库保护（§3/§4.5）。
- 【v1.3】把对象所有权与迁移执行合并为同一角色，或把 LOGIN 属性留给实施阶段裁定（§5.1）。
- 【v1.3】根据 `svc_*` 角色存在就推断 9e3b 由某版迁移创建（附录 C.2）。
- 【v1.3】用「G1~G5 全过」一类状态同时表达设计放行与实现验收（§8.1）。
- 【v1.3】把 postgres 系统库纳入业务迁移对账对象（§6.1）。

## 10. 开放问题裁决（原三问已关闭）

【审查修订 v1.1】按裁决，原三个开放问题逐条关闭：

| 原问题 | 裁决 | 落点 |
| --- | --- | --- |
| 1. verify 混跑 svc_* 角色如何处理？ | **纳入目标授权模型（它们是目标运行时角色，非旧角色）；保留，不在阶段 3 退役**。先做依赖盘点作 IP-4 校准基线；不因来源不可证就 DROP，也不重建同名。详见 D-9 | §6.5 / §7.0 / §13 D-9 |
| 2. R16~R26 如何重编号？ | **采用 R41~R51，建立永久映射表**；同步索引/交叉引用/目录/旧编号引用/自动化检查 | 附录 A |
| 3. 威胁模型是否纳入 Phase 1 验收？ | **必须纳入**，作为独立验收项 E1；E1 须有真实 PostgreSQL 执行证据 | §12 |

> 影响提示：R40 已由本 ADR 占用，分支侧 R16~R26 顺延至 R41~R51；R27~R39 在 main 无同号冲突，不重编号。

## 11. 授权策略单一事实源（v1.2 明确 Authorization Registry）

【v1.1 原则】系统中多种授权模型表现形式（`DatabaseDomain`、Python 权限配置、PostgreSQL Role、
Schema GRANT、RLS Policy、Principal 项目范围、授权完整性 CI、迁移文件中的权限定义）
**不得各自维护一套独立业务授权规则**。核心约束：
1. 域身份、域角色、域资源映射必须有唯一权威定义；
2. PostgreSQL GRANT、RLS Policy、运行时连接池配置必须与该定义一致，由派生器生成或 CI 校验一致；
3. 授权完整性 CI 必须验证各层之间的一致性；
4. 不允许通过重复维护 Python 权限表、SQL 权限表和文档权限表来实现所谓「双重保险」；
5. 无法自动派生的数据库权限必须登记为显式例外（责任人 / 理由 / 退出条件）。

【v1.2 实现模型澄清】`DatabaseDomain` 枚举**只能表达域身份集合**，不足以单独表达完整授权规则。
唯一权威源明确为 **Authorization Registry（授权注册表）**，其结构定义见**附录 D**，至少包含五部分：
1. 域身份（`DatabaseDomain`）；2. 域角色映射；3. 资源映射；4. 操作权限；5. 显式例外。
各层（PostgreSQL 权限、运行时连接池、CI）由它派生，或由 CI 校验一致性。
本原则**不是**要求做一个新的权限管理系统或复杂抽象，而是避免权限规则散落在多个互不一致的文件中。
【v1.3】Registry 的唯一物理载体、资源唯一标识、派生/校验边界与漂移检测见附录 D（P2 落实）。

## 12. Phase 1 独立验收项 E1：数据库授权边界与威胁模型一致性

E1 是 Phase 1 验收的**独立必过项**，不并入既有 A3/B1/B2/C6/D1，须单独通过。
验收标准以 §3 威胁模型、§4 fail-closed、§5 权限不变量为准。
**所有负向测试必须以真实业务域连接身份执行，不得用 PostgreSQL 超级用户代替；
文档检索只能证明「未发现违规表述」，须与威胁模型审查记录结合，不能单独证明系统安全。**

| 编号 | 验收点 | 通过标准 | 证据要求 |
| --- | --- | --- | --- |
| E1.1 | 跨域误写被数据库拒绝 | 域 A 连接写域 B 表被 PG 拒绝 | 真实 PG 执行日志/测试输出，业务域连接身份 |
| E1.2 | 未授权跨域读取被拒绝（**ACL 层**） | 域 A 连接读域 B 表被拒绝，SQLSTATE=**42501** | 真实 PG 执行日志/测试输出，业务域连接身份 |
| E1.3 | 项目级隔离有效（**RLS 层**） | 有 SELECT 权限的域角色 + 合法但不含目标项目的上下文 ⇒ **0 行**（不是 42501）；且合法项目**必须可见**（≥1 行） | 真实 PG + 多项目测试数据；主体必须是授权角色（`svc_asset`），不含持连接伪造场景 |
| E1.4 | 缺失上下文默认拒绝 + 三 GUC 伪造边界一致 | §4.4 矩阵全部拒绝；**伪造边界按 §3/§4.5**：三个 GUC 同属可伪造上下文，仅验证「未授权组织/项目 ID 不得经可信入口进入 Principal」，不宣称抵御持连接 SQL 执行者自行 `set_config`；E1 只覆盖可信调用链 | 逐场景负向测试，业务域连接身份；**当前无真实执行证据（附录 E）** |
| E1.5 | 连接池上下文不串用 | §4.3 四场景（set_config 事务级/提交/回滚/池复用）全通过 | 事务级测试输出 |
| E1.6 | 无意外 RLS 绕过权限 | §5.3 全部满足：覆盖 `pg_proc` 函数执行权限、`SECURITY DEFINER` 审计、PUBLIC 权限、实际 BYPASSRLS 情况，例外均有登记 | `pg_roles`/`pg_class`/`pg_proc`/`pg_default_acl`/ACL 函数查询结果 |
| E1.7 | 不夸大安全边界 | 文档/测试无「服务级隔离」等表述（§3），且未暗示任一 GUC 受更强数据库保护（§4.5） | 文档检索 + 威胁模型审查记录（二者结合） |

**【v1.4.2 必须区分】E1.2 与 E1.3 测的不是同一层，主体不同、期望不同（D.6.0c 派生 2a/2b）**：

| 验收点 | 测试层 | 主体 | 期望 | 若出现反向结果的含义 |
| --- | --- | --- | --- | --- |
| E1.2 | ACL（表级权限） | **无**该表权限的角色（`svc_shot_planning`） | `42501 permission denied` | 若得到「0 行」⇒ 该角色其实有权限，测试无效 |
| E1.3 | RLS（行级策略） | **有** SELECT 权限的角色（`svc_asset`） | 越界 **0 行**；合法项目 ≥1 行 | 若得到 42501 ⇒ 主体选错，落到 ACL 层，未验证 RLS |
| E1.3 补充 | RLS 正向 | 同上 | `project-1` 上下文下必须能看到 **684 行**（E11.4 基准） | 若为 0 行 ⇒ `guc_array` 解析/类型仍错（这正是 v1.4.1 的失败） |

> 「0 行」与「42501」不可互换。E1.3 的**正向断言**（合法项目必须可见）此前一直缺失——
> 只有负向断言时，v1.4.1 那种「永远 0 行」的错误实现反而会"通过"全部越权测试。v1.4.2 起 E1.3 必须同时断言正向。

**E1 必须有真实 PostgreSQL 执行证据**，不得以静态审查、模拟 SQL 或 CI 绿灯替代。
**当前状态：E1 设计通过，真实执行证据未采集（附录 E）；不得据此宣称 E1 已通过。**

**落点指引**：当前 Phase 1 验收清单位于受保护的原始分支
`docs/evidence/phase1-coverage-audit.md`；本轮为纯文档修订且禁止修改原始分支，
故 E1 完整标准以本节为唯一权威源。阶段 7 归并后由所有者将 E1 条目纳入该验收清单，
引用本节编号，不复制标准正文（避免第二事实源）。

## 13. 所有者决策记录（第四轮，v1.4 后追加）

> 本节记录所有者对附录 E 两项开放风险的正式裁决。**决策只改变风险处置态度与阶段归属，
> 不改变 ADR 状态（仍 PROPOSED）、不批准阶段 3、不授权任何数据库写操作。**

### D-8：风险 #8（GUC 上下文可伪造）—— 接受风险

| 字段 | 裁定 |
| --- | --- |
| 决策 | **选择①：维持现有威胁边界**。不建设数据库可验证授权上下文机制（签名令牌 / 可信上下文表等均不引入） |
| 决策依据 | ① 选型 B 的安全承诺是「可信应用调用链下的域授权与项目隔离」；② 三个 GUC 都是普通自定义 GUC，均不能抵御持连接恶意 SQL 执行者；③ 引入新机制会扩大架构范围，不应为关闭单项风险临时叠加授权层；④ 当前重点是保证应用 SQL 执行路径可信，不让任意 SQL 执行能力落入不可信调用方 |
| 范围 | **不承诺**抵御持有数据库连接且能执行任意 SQL 的攻击者 |
| 当前措施 | 可信 Principal 注入 + 三 GUC 归属一致性校验 + 事务局部设置 + 缺失上下文 fail-closed（§3/§4.1/§4.4/§4.5） |
| 验收边界 | E1 只验证可信应用调用链下的授权边界（E1.4），**不测试**对抗任意 SQL 执行者的能力 |
| 重新评估条件 | 未来若要求数据库本身抵御恶意 SQL 执行者，**独立立项**数据库可验证授权上下文机制，不在本 ADR 下以「加了校验函数」名义声称已覆盖 |
| 表述红线 | **接受风险 ≠ 消除风险**；§3/§4.5/E1.4 口径不变，不得把风险接受写成数据库层已解决 |
| 状态 | **已接受（Accepted Risk）** — 仅风险处置态度确定；无实现层变更 |

### D-9：风险 #9（verify 混跑角色来源不明）—— 不在阶段 3 退役目标角色

| 字段 | 裁定 |
| --- | --- |
| 决策 | **不授权**在阶段 3 退役这 8 个 `svc_*` 角色 |
| 关键澄清 | 8 个 `svc_*` 是选型 B 的**目标运行时角色**（§5.1），不是应整体退役的旧角色；§7 Expand-Contract 只针对旧 `app_role`，两者不得混为一谈 |
| 证据范围 | 角色是**集群级**对象（§6.1.1），两库共享同一套身份；依赖盘点完成于「仓库与数据库可观测范围内」，外部部署/CI 密钥/外部 Worker/定时任务/历史连接日志属**未检查缺口**（§6.5 证据强度分级） |
| 当前处理 | ① 保留集群中已存在的 8 个 `svc_*`；② 记录其迁移创建来源不可证；③ 保留已完成的依赖盘点证据（§6.5/E8/E9）；④ 不因来源无法证明就推定角色无效；⑤ **不执行 DROP/REVOKE，也不重建同名角色** |
| 后续处理（IP-4） | 依 Authorization Registry 对现有角色做目标态核对：名称与域映射、LOGIN 属性、独立凭证、成员关系、实际 ACL、数据库连接与运行时使用、是否符合 INV-1~INV-10b；符合目标态则复用并完成必要权限治理，存在不符合的旧授权则按实际差异受控调整 |
| Expand-Contract 适用范围 | 目标 `svc_*`：保留待 IP-4 校准；旧 `app_role`：按 §7 迁移退役；来源不可证角色：不推定来源、逐项核验；已确认无依赖的非目标角色：后续按实际身份单独制定退役方案（见 §7.0） |
| 阶段边界 | 阶段 3 是迁移历史重建；角色与连接池统一属阶段 4（IP-4），不得把角色退役提前放进阶段 3 |
| 表述红线 | **依赖盘点完成 ≠ 来源得到证明 ≠ 允许删除角色**；**保留现状 ≠ 认可现状** |
| 状态 | **不授权退役（目标角色保留）** — 仅阶段归属确定；本轮无任何数据库操作 |

**「保留 ≠ 认可」六项裁定（v1.4 补充复审要求）**：

| 事项 | 裁决 |
| --- | --- |
| 保留现有角色对象 | **是** |
| 承认其创建来源已得到证明 | **否**（来源不可证） |
| 承认其属性/凭证/ACL 已符合目标架构 | **否**，待 IP-4 验收 |
| 允许其当前直接承担新运行时业务 | **否**，待 IP-4 校准 |
| 阶段 3 DROP 或重建 | **不允许** |
| IP-4 依据 Registry 校准 | **允许**，作为计划任务 |

**六类证据严格区分（不得互相替代）**：① 角色存在证据（集群级 `pg_roles`）；② 角色创建来源证据（**不可证**）；
③ 数据库对象依赖证据（owner/ACL，库级）；④ 当前会话证据（时间点快照）；⑤ 仓库静态引用证据（**仅已检查范围**）；
⑥ 外部部署与历史运行依赖证据（**未检查，证据缺口**）。

### D-8 / D-9 对全局的影响边界

- 两项裁决**不改变** §3 威胁模型、§4 三 GUC、§5 身份分层、§6 迁移处置、§8 DP/IP 闸门的技术定义；
- 两项裁决**不解除** DP2/DP3/DP5 的待复审状态，也**不构成**阶段 3 放行；
- 附录 E 的 #8/#9 状态按本节更新（#8 已接受、#9 保留），其余 11 条不变；
- ADR 状态保持 **PROPOSED**，阶段 3 不放行，本轮未修改迁移、代码或数据库。

### D-10：归属约束分阶段治理 —— 三项裁决（2026-11-20，v1.4.4 新增）

> **核心原则（所有者原话）**：允许分阶段治理历史数据，但**不允许把临时豁免变成永久的数据
> 完整性债务**；**先建立写入安全边界，再进行存量订正**。
>
> 本节只记录裁决与它的可执行含义。**裁决明确 ≠ 迁移方案通过实施验收**：ADR 的审批闸门
> （DP2/DP3/DP5）一项未动，阶段 3 仍不放行。

#### D-10.1 `NOT VALID` —— 有条件采纳，拒绝永久豁免

| 字段 | 裁定 |
| --- | --- |
| 决策 | **有条件采纳**：允许 `NOT VALID` 作为分阶段迁移的过渡手段；**拒绝**将其作为最终状态 |
| 采纳依据（技术事实） | PostgreSQL 对支持该机制的约束，`NOT VALID` 添加时跳过历史数据初始扫描，**但仍约束后续新增/更新**；后续可用 `VALIDATE CONSTRAINT` 完成历史数据验证。本项目已实测：新写入违规被 `23503` 拒绝、组织错配被 `23503` 拒绝、脏行存在时 `VALIDATE` 失败（见附录 J.2 与迁移验证 13 项） |
| 五步治理路径（不可跳步、不可换序） | ① 添加 `NOT VALID` 约束 → 先保护新写入；② 识别并订正 14 张表存量归属异常；③ 对订正后数据执行完整性检查；④ 执行 `VALIDATE CONSTRAINT`；⑤ 验证 14 张表归属约束**全部成立** |
| 终态验收 | **14 张表全部完成归属闭环**——不是「约束已创建」，而是「14 行台账每行 `convalidated = true` 且违规行数 = 0」，由脚本从 `pg_constraint` 与逐表比对**实读**（附录 J.2） |
| 明确不可接受（四条） | ① 永久保留 `NOT VALID`；② 把历史脏数据作为长期豁免；③ 将未验证约束标记为通过；④ 用文档声明替代真实数据库验证 |
| 表述红线 | **「约束存在」≠「不变量成立」**；**「已写迁移」≠「已闭环」**；`NOT VALID` 只能被表述为「新写入闸门已生效、历史未验证」 |
| 状态 | **有条件采纳** — 过渡态合法，终态不合法 |

**保留的数据模型判断（本轮不重开）**：13 张表以 **Project 为归属事实源**，Organization 由 Project
派生；`production_routes` 是**组织级候选对象**，须单独处理，**不得机械套用项目级三列约束**
（它无 `project_id`，被多项目共享；见 14 表矩阵 #1）。

#### D-10.2 部署顺序 —— 拒绝「守卫晚于数据订正」，写入保护必须先行

| 字段 | 裁定 |
| --- | --- |
| 决策 | **拒绝**「先订正历史数据、后上线写入守卫」的次序。必须先建立写入保护，或先建立等效的写入冻结/受控写入机制，并在其生效前持续有效 |
| 拒绝理由（所有者） | 订正完成时数据可能已满足约束；守卫未生效期间业务仍可能写入新的错误归属；启用约束时又出现新的历史异常，**前面的订正因此失去意义** |
| 硬约束 | **不允许存在没有保护的写入窗口** |
| 状态 | **已裁定** — 部署次序由「守卫后置」改为「写入保护先行」 |

**执行前必须消除的术语歧义（否则 D-10.1 与 D-10.2 会互相矛盾）**：

本项目同时存在两个被叫做「守卫」的机制，它们保护的东西完全不同：

| 名称 | 实际机制 | 保护什么 | 能否「先上」 | 依据 |
| --- | --- | --- | --- | --- |
| **归属写入保护** | 复合外键（`NOT VALID` 亦可） | 新写入的**归属正确性**（项目存在 + 同租户 + 组织一致） | **必须先上**，且实测确实已生效 | 迁移验证 D3b/D3d：新写入违规一律 `23503` |
| **不可变守卫** | `asset_versions_guard` 触发器（ADR-R11） | 已写入**事实不被篡改**（内容列 append-only） | **必须在线的同时提供受控例外**；无例外则订正本身非法 | T3 C8：`UPDATE project_id` → `P0001`；C10 禁守卫后同一语句 OK |

若把 D-10.2 的「守卫」读成**不可变守卫**且要求它无条件先上，则与 D-10.1 **在逻辑上不可同时满足**：
订正归属列必然被 `P0001` 拒绝 → 只能走「前向新建版本 + 旧行标 `SUPERSEDED`」→ 但旧行仍在表内
且仍违规 → `VALIDATE` 必然 `23503`（实测 D3e）→ 14 表永不闭环，与 D-10.1 直接冲突。

**本 ADR 的消解方式（不重新解释裁决，而是把裁决做得更强）**：

1. **层 1 · 归属写入保护（常开、先上）**：14 条复合外键以 `NOT VALID` 建立。它本身就是
   「先保护新写入」，`INSERT`/`UPDATE` 违规即 `23503`，无需等数据订正完成。
2. **层 2 · 不可变守卫（常开 + 最小豁免面）**：**不再使用 `DISABLE TRIGGER`**。守卫保持在线，
   归属订正改走**受控例外**——由守卫函数自身判定，四重条件**同时满足**才放行：
   - ① 会话角色是专用角色 `afs_data_repair` 的成员（**GUC 不可伪造此条**，绕开 D-8 的 GUC 可伪造边界）；
   - ② 会话设置了工单令牌 `app.ownership_repair_ticket`；
   - ③ `ownership_repair_map` 中存在 `table_name + entity_id` 匹配、`applied_at IS NULL`、
     且 `ticket_ref` 等于该令牌的待修行；
   - ④ 本次变更**只涉及归属三列**（`project_id` / `organization_id` / `tenant_id`），
     其余内容列仍一律 `P0001`。
   订正完成即把映射行 `applied_at` 盖章 → 例外自动关闭，守卫恢复全量不可变。
3. **无未保护窗口的结构性保证**：窗口内既不摘除守卫，也不放开归属校验；即使并发的业务写入
   越过服务层直接进库，**仍会被层 1 与层 2 同时拦住**——这比「禁守卫 + 长事务锁表」更强，
   因为后者在锁持有期间保护是被摘掉的。

**为什么否决 `DISABLE TRIGGER`（此前的建议，现撤回）**：

| 维度 | `DISABLE TRIGGER` | 受控例外（D-10.2 采纳） |
| --- | --- | --- |
| 豁免面 | **全局**：整张表所有行、所有列、所有会话 | **单行 + 单列集 + 单工单 + 单角色** |
| 锁 | 需 `ACCESS EXCLUSIVE` 并持到事务结束（阻塞全部读写） | 仅行锁 |
| 可审计 | 仅能从日志看出「某时刻禁过触发器」 | 每一行改动都有映射行、工单号、`reason`、`applied_at` |
| 失败模式 | 事务内出现任何意外语句时，**保护不在** | 保护始终在，只是对特定行开了窄门 |
| 残留风险 | 低（但保护被整体摘除） | **#24 受控例外被滥用**（须登记，见附录 E） |

#### D-10.3 哨兵行 —— 默认移入隔离项目

| 字段 | 裁定 |
| --- | --- |
| 决策 | **默认将哨兵行移入专用隔离项目**（不是迁到真实项目、不是删除） |
| 拒绝「迁到真实项目」作默认 | 不能为了满足外键而**虚构真实业务归属** |
| 拒绝「删除」作默认 | 可能破坏历史审计、追溯与数据完整性 |
| 例外通道 | 迁到真实项目、或删除，**必须走单独例外工单**，保留审批与证据 |
| 边界红线（所有者） | **隔离项目不能成为新的永久垃圾桶**，它是**受治理的历史数据隔离边界** |

**七条要求逐条落成机制与验收判据**：

| # | 要求（所有者） | 落地机制 | 验收判据（可复算） |
| --- | --- | --- | --- |
| 1 | 必须是**真实存在**、符合归属模型的项目记录，不能只是伪造 UUID | 插入真实 `project_planning.projects` 行：`entity_id = 'quarantine-' \|\| organization_id`（每组织一个，46 字符 < `varchar(64)`）；`tenant_id` / `organization_id` 取该组织真实值 | `SELECT count(*) FROM projects WHERE entity_id = 'quarantine-'||organization_id` 与其组织数相等；哨兵行的 `(project_id, tenant_id, organization_id)` 三列外键 `VALIDATE` 通过 |
| 2 | 必须有**明确的隔离属性和生命周期** | `status = 'ARCHIVED'`（终态，不回流）、`project_tier` 取专用值、`project_name` 标注「数据隔离项目（非业务）」、`created_by = '(system) ownership-migration'`；**禁止**被状态机推进回 `ACTIVE` | 台账记录其 `status`；任何把它改回 `ACTIVE` 的写入须被拒（策略或状态机，见下） |
| 3 | 迁移后**不得被正常业务查询当作真实业务数据** | `projects` 增 `AS RESTRICTIVE` 策略：`entity_id NOT LIKE 'quarantine-%'`，仅当 `app.include_quarantine = 'on'` 时放行 | 以业务主体（`svc_*`）查询不得返回隔离项目；以审计主体开 GUC 后可返回 |
| 4 | **不得作为普通业务项目开放给业务写入** | 同表增 `AS RESTRICTIVE` `WITH CHECK`：`entity_id NOT LIKE 'quarantine-%'`，仅审计主体可写 | 以业务主体 `INSERT`/`UPDATE` 指向隔离项目须被拒 |
| 5 | 迁移**必须保留原始标识、原始归属值和变更记录** | `ownership_repair_map` 保存 `entity_id`、`old_project_id`、`new_project_id`、`old_organization_id`、`reason`、`ticket_ref`、`applied_at`；**不覆盖原值** | 每个被迁移行在映射表中恰好一条记录；`old_project_id` 保留 `legacy-unassigned` 原值 |
| 6 | 迁移后**验证所有外键、归属约束及 RLS 行为** | 附录 J.2 台账（外键 `convalidated` + 违规行数）+ RLS 行为用例（业务主体不可见/不可写、审计主体可见/可写） | 两项都过才算该表 PASS；RLS 未验证时该行判 `证据不足`，不得判 PASS |
| 7 | 迁到真实项目或删除**走单独例外工单** | 例外工单须含：业务依据、影响行数、审批人、删除前备份位置；删除另受 append-only 约束（经 DML 不可删） | 台账「例外工单」列非空且可回溯到审批记录 |

**隔离项目为什么不会退化成垃圾桶（结构性理由，不是承诺）**：

- **误删会被数据库拦住**：哨兵行的三列外键指向隔离项目，且 `ON DELETE` 为 `NO ACTION`，
  删除隔离项目直接 `23503`——这是天然的保护，不依赖人的自觉。
- **体积可观测**：台账固定输出「隔离行数 / 总行数」比值；该比值只应随时间下降
  （被逐步判定为真实归属后迁出），持续上升即触发复审。
- **只能进不能改**：要求 3/4 的 RESTRICTIVE 策略使业务侧既看不见也写不进，隔离区不会成为
  「先塞进去再说」的随手出口。

#### D-10 三项裁决是否互相冲突：结论

- **D-10.1 与 D-10.2 相容**，前提是把「守卫」区分为**归属写入保护**与**不可变守卫**（见 D-10.2 表），
  且不可变守卫提供**受控例外**而非被禁用。二者合取后的次序是：
  **层 1 外键（先）→ 层 2 守卫 + 受控例外（先）→ 存量订正（走受控例外）→ `VALIDATE` → 台账 14 行全 PASS**。
- **D-10.1 与 D-10.3 相容**：隔离项目是让 `VALIDATE` 可达的唯一默认路径（另外两条是例外工单）。
- **D-10.2 与 D-10.3 相容**：哨兵行订正同样走受控例外，不需要禁守卫。
- **若所有者对「守卫」采用另一种读法**（要求不可变守卫无条件先上、且不开例外通道），
  则 D-10.1 的 14 表闭环**在 PostgreSQL 语义下不可达**（证明见 D-10.2 的矛盾段）。
  此时必须在「14 表闭环」与「守卫无条件先行」之间明确放弃一项，**不能两项同时声称成立**。

#### D-10 对全局的影响边界

- **不改变** §3 威胁模型、§4 三 GUC、§5 身份分层、§6 迁移处置、§8 DP/IP 闸门的技术定义；
- **不解除** DP2/DP3/DP5 的待复审状态，**不构成**阶段 3 放行；
- **不激活**补齐迁移：迁移文件已写完并在克隆库验证（13 项），但位于
  `data/migrations/pending/`，**未接入任何 Alembic 链**；
- **不修改**业务代码：受控例外所需的守卫函数变更属实现层，须在获授权后以独立迁移提交；
- 附录 E 由 22 条增至 **25 条**（新增 #23/#24/#25），#22 状态更新为「已裁定」；
- ADR 状态保持 **PROPOSED**，阶段 3 不放行。

## 附录 A：ADR 编号映射表 R16~R26 → R41~R51

映射列：旧编号 → 新编号 → 原始文件身份 → 变更原因。
变更原因对所有条目一致：与 main 侧同号 ADR 语义冲突，且 R40 已被本 ADR 占用，故整体顺延。**只改编号不改语义。**

| 旧编号 | 新编号 | 原始文件身份（分支侧） | 变更原因 |
| --- | --- | --- | --- |
| R16 | R41 | `ADR-R16-database-role-per-domain.md` | 与 main R16 同号异义；R40 已占用，顺延 |
| R17 | R42 | `ADR-R17-project-scope-isolation.md` | 同上 |
| R18 | R43 | `ADR-R18-unified-authorization-decision.md` | 同上 |
| R19 | R44 | `ADR-R19-signing-key-boundary.md` | 同上 |
| R20 | R45 | `ADR-R20-data-invariants-in-database.md` | 同上 |
| R21 | R46 | `ADR-R21-repository-error-semantics.md` | 同上 |
| R22 | R47 | `ADR-R22-delivery-manifest-as-evidence.md` | 同上 |
| R23 | R48 | `ADR-R23-gate-decision-as-fact.md` | 同上 |
| R24 | R49 | `ADR-R24-delivery-approval-consumption.md` | 同上 |
| R25 | R50 | `ADR-R25-release-package-governance.md` | 同上 |
| R26 | R51 | `ADR-R26-capability-token-runtime-enforcement.md` | 同上 |

**边界**：分支侧 R27~R39 在 main 无同号冲突，**不重编号**。
**同步检查位置**（不只改文件名）：ADR 索引、交叉引用、文档目录、决策记录引用、其它文档旧编号、自动化检查中的编号引用。
重映射是阶段 3 前置动作，但阶段 3 获批前不执行；本轮未执行 ADR 重编号。

## 附录 B：修改记录（对应三轮审查）

### B.1 v1.1 七项必改（第一轮审查）

| # | 审查要求 | 落实位置 | 状态 |
| --- | --- | --- | --- |
| 1 | §4 GUC 语义/事务边界/fail-closed | §4.1~§4.4 | 已落实 |
| 2 | §5 八域权限不变量/RLS 绕过/实际授权检查 | §5.1~§5.4 | 已落实 |
| 3 | §6 三 Revision 处置矩阵与结构验收 | §6.2 + 附录 C | 已落实 |
| 4 | §7 Expand-Contract 失败回滚 | §7.1/§7.2 | 已落实 |
| 5 | §10 关闭三开放问题 | §10 | 已落实 |
| 6 | 授权策略单一事实源原则 | §11 + 附录 D | 已落实 |
| 7 | Phase 1 独立验收项 E1 | §12 | 已落实 |

### B.2 v1.2 七项必改（第二轮审查）

| # | 审查要求 | 落实位置 | 状态 |
| --- | --- | --- | --- |
| 1 | §4.2 修正 GUC 类型描述（逻辑 uuid[] / 物理文本 / set_config 参数化 / 统一解析 / 非法拒绝） | §4.2 | **部分作废**（v1.4.2 依 E11.2 将逻辑类型改为 `text[]`；本条中「逻辑 uuid[]」不再有效，其余四项仍有效） |
| 2 | §4.1/§4.4/§12 明确 GUC 可伪造边界 | §3 / §4.1 / §4.4 / §4.5 / §12 E1.4 | 已落实（v1.3 扩展到三 GUC） |
| 3 | §5.1 明确登录模型 + 迁移所有者分离 | §5.1 / §5.3 | 已裁定（v1.3 结构性重写） |
| 4 | §6 迁移历史证据台账（checksum + 执行证据 + 结构证据 + 前向 + 恢复） | 附录 C | 已落实（真实取证，v1.3 扩展十列矩阵） |
| 5 | §6.4 修正回滚测试策略 | §6.4 | 已落实 |
| 6 | §11 明确 Authorization Registry | §11 + 附录 D | 已落实（v1.3 补唯一载体等） |
| 7 | §12 完善 E1 | §12 | 已落实 |
| 附 | §7 回滚窗口 runbook 要求 / §8 流程状态区分 | §7.3 / §8 | 已落实 |

### B.3 v1.3 本轮修订（第三轮复审，P1×4 + P2）

| # | 复审要求 | 落实位置 | 状态 |
| --- | --- | --- | --- |
| P1-1 | 三 GUC（租户/组织/项目）威胁边界统一，不暗示某 GUC 受更强保护；E1 不扩大安全承诺 | §3 / §4.1 / §4.2 / §4.4 / §4.5 / §12 E1.4 / §9 | 已落实 |
| P1-2 | 明确对象所有者与迁移执行者身份关系（LOGIN/继承/成员关系/权限边界 + 可验证不变量） | §5.1 / §5.3 / §6.5 / §9 | 已落实 |
| P1-3 | 逐库逐 Revision 十列证据矩阵 + 结构验收标准；verify 逐项盘点；系统库排除 | §6.1 / 附录 C（C.2/C.3/C.4/C.5） / §12 | 已落实（本轮新取证） |
| P1-4 | G1~G5 拆为设计放行 DP 与实现验收 IP；Registry 三态区分 | §8 / §8.1-A / §8.1-B / §9 | 已落实 |
| P2 | Registry 唯一载体/结构化定义/资源唯一标识/映射/例外字段/派生/校验/漂移检测 | §11 / 附录 D | 已落实 |
| 附 | 新增 P1/P2 逐项落实矩阵与未解风险更新 | 附录 F / 附录 E | 已落实 |

## 附录 C：迁移历史证据台账（真实只读取证，2026-11-19，v1.3 扩展）

【v1.2 建立，v1.3 结构性扩展】本台账所有 checksum、结构与 ACL 数据均由只读命令现场采集：
迁移文件 SHA256 与 git blob hash 交叉验证一致；数据库结构经 `information_schema` /
`pg_catalog`（`pg_class`/`pg_policy`/`pg_proc`/`pg_roles`/`pg_auth_members`/`pg_default_acl`）查询。
**采集过程未修改任何迁移、数据库或角色，未执行 upgrade/downgrade 或任何写操作。**
采集命令见 C.5。

### C.1.0 三个口径的定义（【v1.4.3】优先读这一节，否则一定会误读下面的 3 和 10）

【v1.4.3 新增】复审指出「Revision ID 冲突数量」与「跨分支文件分叉数量」不是同一个指标，
不能把原文的「3 个」直接改成「10 个」。这个判断成立。实测后可给出**三个互不替代**的口径：

| 口径 | 定义（判定单元） | 实测值 | 用途（为什么必须单独存在） |
| --- | --- | --- | --- |
| **A. 迁移身份冲突** | 同一个 Revision ID 在两侧被**不同内容**的文件定义 | **10** | 判断 Alembic 迁移图能否直接拼接。Alembic **只比 ID 不比内容**，故这 10 个它全部「无感」——这是最危险的一类静默失败 |
| **B. 跨分支分歧项** | 经「路径 + 内容哈希 + 归一化文本」确认的**独立决策单元** | **10**（T1×1 + T2×7 + T3×1 + T4×1） | 判断逐项裁决的**工作量**：每一类要不同的裁定方式（T2 一条配置约束即可，T1 需前向补齐） |
| **C. 语义不等价冲突** | 归一化掉「角色名来源」后**仍有实质差异**的分歧项 | **3**（`b3c4` / `9e3b` / `b7c8`） | 判断哪些项**必须逐句读**，不能靠等价性推断放行 |

> **三个数字不可互换，理由各不相同**：
> - A 与 B 数值**恰好都是 10**，但**成员与判据完全不同**。A 的关键是「ID 相同、内容不同」；
>   B 的关键是「决策单元」。数值相同纯属巧合，**必须并列呈现，不得合并成一个「10 个冲突」**。
> - C 的 3 项是 A 的 10 项的真子集，是「必须人工逐句裁决」的那部分。**不能用 C 代替 A**
>   （会低估迁移图拼接风险），**也不能用 A 代替 C**（会高估裁决工作量）。
>
> **口径 B 的成立不是断言，是机器判定**（`docs/audits/mig-recon/verify_t2_equivalence.py`）：
> 把两侧源码的角色名表达式归一化为同一占位符（`app_role_name()`/`APP_ROLE`/`_APP_ROLE` → `ROLE_NAME`，
> 删除常量定义行、导入行、空行），再用 `difflib` 比对。结果见 C.1.1「归一化残差」列——
> 7 个 T2 残差 **0 行**（相似度 1.000），T3/T4/T1 分别 20/7/292 行。**分类结果与残差签名完全自洽**
> （T2 → 0 残差；其余 → 有残差），这本身构成对分类正确性的交叉验证。

> **【v1.4.3】口径 B 的前置条件（必须写在用到它的地方）**：
> 7 个 T2 的等价性**只在 `POSTGRES_APP_USER` 保持默认值 `app_role` 时成立**。原文的「等价」指的是
> 「角色名取值的**来源**不同、取值相同」——main 走 `app_role_name()` 读配置，分支走常量 `APP_ROLE`。
> **一旦部署侧改过 `POSTGRES_APP_USER`**，同一迁移在两侧会生成**不同的角色名字面量**，7 个 T2 立即
> 从「形式差异」变成「真实角色名分叉」，必须重新裁决。这不是理论风险：该变量是部署配置项，
> 改它不需要改代码，也不会被任何现有闸门拦住。

### C.1 迁移文件摘要：口径 C（**必须逐句裁决的语义不等价冲突**，3 项）

【v1.4.3 改判】原 C.1 标题为「迁移文件摘要（定义该 Revision 的文件，非引用它的文件）」，并在表后
声称这 6 行是「『同 Revision ID、不同迁移内容』的硬证据」。按该字面定义（ID 相同、内容不同），
实测是 **10 个**，不是 3 个——**原标题漏了 7 个**。但被登记的这 3 个恰好等于**口径 C 的 3 个语义不等价项**，
即：**成员选对了，标签写错了**。本节按实际成员改判为口径 C；全量 10 项见 **C.1.1**。

这个区分不是文字游戏，它决定两件事：若按原文字理解为「只有 3 处内容不同」→ 会低估迁移图拼接风险
（另 7 处虽在默认配置下等价，但**一旦部署侧改过 `POSTGRES_APP_USER`，7 处同时变成真实角色名分叉**，
见 C.1.0 前置条件）；若把 3 直接改成 10 → 会高估裁决工作量（7 处不需要逐项业务裁定，只需一条配置约束）。

**哈希口径（【v1.4.3】R5 修正）**：`git blob hash` = `git rev-parse <ref>:<path>`，**主锚点**；
`SHA256` = `git show <ref>:<path>` 的**原始字节**（LF，不经工作区转换），辅助列。
v1.4.2 版此表 6 行中 `b7c8 main`、`9e3b main` 两行按 **CRLF** 计算，与其余 4 行口径不一致——
**已在下表更正为 LF 值**，原文的 CRLF 值不再保留（保留会造成「两个 SHA 都对」的错觉）。

| Revision | 侧 | 定义文件 | SHA256（LF 原始字节） | git blob hash（主锚点） |

| --- | --- | --- | --- | --- |
| `b3c4d5e6f7a8` | main | `b3c4d5e6f7a8_asset_project_ownership.py` | `fbc0da26e3caf0fc99ef8464fb780d231451ed83362a2bf004f94a50382a4823` | `dbc51ce794d95cd62245edde52ab7f5ca3cb64dd` |
| `b3c4d5e6f7a8` | 分支 | `b3c4d5e6f7a8_asset_status_transitions.py` | `0d1c44d24f10d3b231a29457fd5968c35b8a71b43a446ae0a2d15957f7a6e606` | `8899ef436376c211af3b91a403e7d482a9afa89d` |
| `b7c8d9e0f1a2` | main | `b7c8d9e0f1a2_partition_protection_and_grant_baseline.py` | `0caf8be5f0bd19c10d7c8349c24be9532547e1925c1a55df99951a5a9317630c` | `dbae1fc43715b33c6334a86e25debb7a915f69ed` |
| `b7c8d9e0f1a2` | 分支 | `b7c8d9e0f1a2_partition_protection_and_grant_baseline.py`（**同名不同内容**） | `2a8d7d2395c5d936b6e6be2be393c437a781f664c68d0f26773d7fe210f27c8f` | `816a0f1c337601d36b2309646622c208cc56df81` |
| `9e3b4c5d6e7f` | main | `9e3b4c5d6e7f_create_app_role_and_grants.py` | `76e330519a95d2981b1b3b414294d3c207d62ace3406bb529ba63f2b31af3418` | `3b5b6753dc4876622e0a74a5fa0f84e91f8fe145` |
| `9e3b4c5d6e7f` | 分支 | `9e3b4c5d6e7f_create_app_role_and_grants.py`（**同名不同内容**） | `57c3d775ac07e90a8ecce53e39449f226c3cd68d38f9c984f2d474130ad81497` | `41faff78a58e068775db8befa6745c8d5bc99691` |

> 【v1.3 复核】本轮重新计算六个文件的 SHA256 与 blob hash，与 v1.2 台账**逐位一致**。
> b7c8 与 9e3b 两侧**文件名相同但 SHA256 与 blob hash 均不同**——「同 Revision ID、不同迁移内容」的硬证据，
> 支持 §6.2「不得复用已占用 Revision」的硬约束。

### C.1.1 全量分叉清单（口径 A/B 的 10 项，【v1.4.3】新增）

【v1.4.3 新增】本表是**口径 A（迁移身份冲突 = 10）与口径 B（跨分支分歧项 = 10）的成员全集**，
不是口径 C 的 3 项。C.1 只登记其中**语义不等价**的 3 项；本表登记**全部 10 项**并给出各自的可判定特征。

**固定来源（不可变，复现必须用这两个 ref）**：

| 侧 | ref | commit | 说明 |
| --- | --- | --- | --- |
| 主支 | `main` | `5352d1979421f41bfb9943774378671cfe94de71` | 第六轮复审基线 |
| 功能分支 | `origin/feat/identity-data-ownership` | `59b13af73640d10b81703b514202173976c432c5` | 同上 |

**双哈希口径**：`blob` = `git rev-parse <ref>:<path>`（**主锚点**，Git 对仓库内字节计算，不受
`core.autocrlf` 影响）；`SHA-256` = `git show <ref>:<path>` 原始字节（**辅助**，与工作区换行设置无关）。
**两者必须同时给出**：只有 SHA-256 时，换行设置一变就「对不上」且无法自证；只有 blob 时，
仓库外的人无法用 `sha256sum` 复核。

| Revision | 类型 | main blob SHA1 | 分支 blob SHA1 | main SHA-256 | 分支 SHA-256 | 归一化残差（行 / 相似度） | 定义文件（路径） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `9e3b4c5d6e7f` | T3 | `3b5b6753dc4876622e0a74a5fa0f84e91f8fe145` | `41faff78a58e068775db8befa6745c8d5bc99691` | `76e330519a95d2981b1b3b414294d3c207d62ace3406bb529ba63f2b31af3418` | `57c3d775ac07e90a8ecce53e39449f226c3cd68d38f9c984f2d474130ad81497` | 20 / 0.885 | 同名 `9e3b4c5d6e7f_create_app_role_and_grants.py` |
| `a1b2c3d4e5f6` | T2 | `78a31e49b267a710fcab12ed00c21398b6bf423c` | `7285b67106ec943fe0c82a3c37dbea74dbbd286f` | `5945a047a92e6061ec46c8eda891ac30cd588bf83104a1db5326e959dd6fdd48` | `a3913fcb5fabfebfa3c24837f84258f6f367723bcfec0a843a08171fe4733a99` | 0 / 1.000 | 同名 `a1b2c3d4e5f6_fix_outbox_backlog_exclude_dead.py` |
| `b3c4d5e6f7a8` | T1 | `dbc51ce794d95cd62245edde52ab7f5ca3cb64dd` | `8899ef436376c211af3b91a403e7d482a9afa89d` | `fbc0da26e3caf0fc99ef8464fb780d231451ed83362a2bf004f94a50382a4823` | `0d1c44d24f10d3b231a29457fd5968c35b8a71b43a446ae0a2d15957f7a6e606` | 292 / 0.076 | **不同名**：main `b3c4d5e6f7a8_asset_project_ownership.py` / 分支 `b3c4d5e6f7a8_asset_status_transitions.py` |
| `b5c6d7e8f9a0` | T2 | `cc756baa79df3d3732a46d5a30f8396227abf2b5` | `a08f17c95da21e02384750572d7b7fde104663a9` | `2231518088b96bbac71a20d0bf40ff7b13dc421fef801c01dbd289e5cf4f9b25` | `f686de889d1e84b46825e0d5003ef674018b4137d8fbb5e7a5e99d1290efcf07` | 0 / 1.000 | 同名 `b5c6d7e8f9a0_revoke_maintenance_runs_from_app_role.py` |
| `b7c8d9e0f1a2` | T4 | `dbae1fc43715b33c6334a86e25debb7a915f69ed` | `816a0f1c337601d36b2309646622c208cc56df81` | `0caf8be5f0bd19c10d7c8349c24be9532547e1925c1a55df99951a5a9317630c` | `2a8d7d2395c5d936b6e6be2be393c437a781f664c68d0f26773d7fe210f27c8f` | 7 / 0.968 | 同名 `b7c8d9e0f1a2_partition_protection_and_grant_baseline.py` |
| `c1d2e3f4a5b6` | T2 | `91d1f7308a032e4338208d7d6ebd5118b9570ac6` | `26f543e8a7381bc46db33823bfed6dfd4e98069c` | `4e3e9dbfaf58622a69297ce703b73b51ecedfc8716e829e5864c6c5dd2f921c3` | `525a23270fd78c89e6e8ee8bfc0946e93fdcc0ab6730e675299c99adf2c45809` | 0 / 1.000 | 同名 `c1d2e3f4a5b6_create_asset_service_tables.py` |
| `c6d7e8f9a0b1` | T2 | `fe3e78d772a7e206c23958d17712ce3a610d2f14` | `80a1b71e7f99869a2e765f8288530c87ab5582d2` | `ad6705cb7a04520a178161d9ff3db54d1d1e29c5c5cbb81359dfeab40720d29f` | `bd40ef42796117989f8d388b57ca4784ec68f1a7707de44a7a21b0b480345645` | 0 / 1.000 | 同名 `c6d7e8f9a0b1_create_auth_revoked_tokens.py` |
| `d4e5f6a7b8c9` | T2 | `b57a6d2ede1eeb0caadc2f18b0131f6a0f1c2617` | `2079b1654780346f38d2ee51b1e3a4d677482255` | `4c468f56490c1691cabfb37a3f9acbbaed2cc54ffde982c59e9228ad89c399b9` | `239b00967fa9d0ab2e53e3edb4cc44d4bf8270602f1ae4916666367649488fe0` | 0 / 1.000 | 同名 `d4e5f6a7b8c9_restrict_outbox_grants.py` |
| `e5f6a7b8c9d0` | T2 | `760cf198c78df011d15d526f61fdf791796849cb` | `3c0b563d118680778f3ec5df661897c820d82733` | `e6a1ae52d9ae43e4213cbdef2720ac11cb740f5c4a1310931d0e3ccc23e4fe0a` | `3a17dbac31b58117a95a2a03d1e0fcd1b79b8b8fc5fd0ea1ebc67f3a18cafc4c` | 0 / 1.000 | 同名 `e5f6a7b8c9d0_add_outbox_tenant_enumeration_function.py` |
| `f6a7b8c9d1e2` | T2 | `8c536fc20776105f0508fdd5d652bc0b62d07e57` | `7c16a5aad1a4c60b80ab3d488ba17e92de15648e` | `4cc9a1291d964240876fc8c360e8988574987b01aaeb6ddb76b3922482ec1950` | `e01331efb969f66490f738d17349c4286890d5f78ad3095bb893486613490145` | 0 / 1.000 | 同名 `f6a7b8c9d1e2_partition_outbox_events.py` |

**计数（与 C.1.0 口径 A/B 对应）**：`T3×1 + T2×7 + T1×1 + T4×1 = 10`。其中残差为 0 的是 7 个 T2
（**机器判定，非人工目测**），残差非 0 的 3 个即口径 C 的成员，与 C.1 表逐行对应。

**类型含义（本轮裁定，供逐项处理时引用）**：

| 类型 | 定义 | 处理方式 | 是否需逐句读 |
| --- | --- | --- | --- |
| **T1** | 两侧是**两个不同功能**，非同一改动的两种写法 | 保留已执行身份，缺口用**唯一新 Revision 前向补齐**（不得二选一） | 需（本项目已裁定：`b3c4` main = 资产归属项目 + 跨 schema 复合外键；分支 = 状态只能沿已登记边推进，两者可共存） |
| **T2** | 仅**角色名来源**不同（配置函数 vs 常量），数值一致 | 一条配置约束即可（`POSTGRES_APP_USER=app_role`），不逐项裁定 | 不需要（但必须锁定 C.1.0 的前置条件） |
| **T3** | 角色名来源 + `ALTER ROLE` **属性收敛** + 开发口令守卫 | 采纳 main 侧（分支的 `ALTER` 分支不收敛 `BYPASSRLS`，直接决定数据库还是不是隔离边界） | 需 |
| **T4** | **调用点绑定**：迁移与 helper 必须成对取用 | 两侧成对取用，禁止交叉组合（`role=` vs `roles=` 会 `TypeError`） | 需（但属组合约束，非效果差异） |

**复现命令（只读，可逐行核对本表）**：

```bash
python docs/audits/mig-recon/build_hash_inventory.py     # 生成 10 行双哈希 → hash-inventory.md/json
python docs/audits/mig-recon/verify_t2_equivalence.py \
  9e3b4c5d6e7f a1b2c3d4e5f6 b3c4d5e6f7a8 b5c6d7e8f9a0 b7c8d9e0f1a2 \
  c1d2e3f4a5b6 c6d7e8f9a0b1 d4e5f6a7b8c9 e5f6a7b8c9d0 f6a7b8c9d1e2   # 归一化残差
git rev-parse main:data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py
git rev-parse origin/feat/identity-data-ownership:data/migrations/versions/b7c8d9e0f1a2_partition_protection_and_grant_baseline.py
```

> 【v1.4.3】本表**逐行**均带「分支（ref+commit）/ 路径 / 双哈希」，不依赖任何转录；
> JSON 原文 `docs/audits/mig-recon/hash-inventory.json` 含每行 `main_bytes`/`feat_bytes`/`diff_lines`。

### C.2 逐库逐 Revision 十列证据矩阵【v1.3 · P1-3 核心交付】

【v1.3 新增】按「每个业务数据库 × 每个冲突 Revision」建立证据矩阵。仅覆盖
`ai_film_industrial` 与 `ai_film_verify` 两个业务库；`postgres` 系统库不参与（§6.1）。
首列 `#` 仅为行标识（供正文交叉引用，非证据维度）；其后 10 列即复审要求的 10 项证据维度。
checksum 列给出 SHA256 前 12 位（完整值见 C.1）。

| # | 数据库 | Revision | main checksum | 分支 checksum | alembic_version 执行证据 | 实际数据库结构证据 | 缺失业务效果 | 目标前向迁移 | 结构验收标准 | 异常恢复方案 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | ai_film_industrial | b3c4d5e6f7a8 | `fbc0da26…` | `0d1c44d2…` | 当前 `c1a2b3d4e5f6`（已越过 b3c4）；历史执行轨迹不可还原，按结构判定 | `asset_versions` 有 pk + `ck_asset_versions_status` + `fk_asset_versions_project_tenant`；有 `audit.status_transitions` | 无（资产归属与状态约束均具备） | 保留现状；不复用冲突 ID | 状态 CHECK + 复合外键 + 审计事实表均在（查 `pg_constraint`） | 一次性库演练 downgrade；真库仅前向 + 备份恢复 |
| 2 | ai_film_industrial | b7c8d9e0f1a2 | `0caf8be5…` | `2a8d7d23…` | 当前 `c1a2b3d4e5f6`；执行来源不可证 | 3 个分区均 `relrowsecurity=t`、各 1 policy、父表 RLS=t；**默认权限已反转**（outbox 仅 `r/ar/ar`，asset/project 无 defacl）；`relforcerowsecurity` 全 f | FORCE RLS 未启用（INV-7 未满足） | 保守保留已执行身份；分区保护 + 授权基线以唯一 Revision 前向确保双方效果 | 分区 RLS + policy 在；默认权限无 `arwd` 自动授权；强制 RLS 表 FORCE=t | 一次性库演练；先完成来源盘点再定是否需补齐 |
| 3 | ai_film_industrial | 9e3b4c5d6e7f | `76e33051…` | `57c3d775…` | 当前 `c1a2b3d4e5f6`；**角色存在不能证明由哪版迁移创建** | 角色为**集群级共享**：共 12 业务角色（main 4 + svc 8，见 §6.1.1，**不按库重复计数**），全 `rolsuper=f/rolbypassrls=f/createdb=f/createrole=f`，业务角色零成员边；**本库对象层**：表 owner 全 `postgres`，无 `afs_owner`/`afs_migrator` | 目标身份分层（INV-1/5/6）完全不存在 | 保留应用角色历史；8 个 `svc_*` 为**目标运行时角色**，IP-4 校准（§7.0/§13 D-9）；main 4 个旧角色按 §7.0.1 逐角色退役 | `pg_roles` 属性满足 §5.1/INV-4；owner=afs_owner；成员关系满足 INV-3/6 | 旧角色按 §7.0.1 保留至各自 Verify 完成；DROP 后按备份恢复/前向重建 |
| 4 | ai_film_verify | b3c4d5e6f7a8 | `fbc0da26…` | `0d1c44d2…` | 当前停留 `b3c4d5e6f7a8` | `asset_versions` 有 `project_id` + 复合外键，**无** `ck_asset_versions_status`；状态转移表=0、Phase1 表=0；无 `audit` schema | 分支状态 CHECK + 状态转移表 + audit 事实表全缺 | 保留 main 资产归属历史身份；分支状态约束用全新唯一 Revision 前向补齐 | 资产归属 + 状态 CHECK + `audit.status_transitions` 同时存在（查 `pg_constraint`/`pg_class`） | 一次性库演练；真库仅前向 + 备份恢复演练 |
| 5 | ai_film_verify | b7c8d9e0f1a2 | `0caf8be5…` | `2a8d7d23…` | 当前 `b3c4d5e6f7a8`；执行来源不可证 | 3 分区 RLS=t、各 1 policy、父表 RLS=t；**默认权限未反转**（asset_service=`arwd`+`rU`、project_planning=`arwd`+`rU`、outbox=`r/ar/ar`）；`relforcerowsecurity` 全 f | 授权基线反转业务效果缺失（与 industrial 库不一致）；FORCE RLS 未启用 | 保守保留；分区保护 + 默认零授权以唯一 Revision 前向确保 | 默认权限无 `arwd` 自动授权（与 industrial 一致）；分区 RLS + policy 在；FORCE=t | 一次性库演练；先盘点来源再定补齐 |
| 6 | ai_film_verify | 9e3b4c5d6e7f | `76e33051…` | `57c3d775…` | 当前 `b3c4d5e6f7a8`；**角色存在不能证明由哪版迁移创建** | **同一集群角色集**（与 #3 是同一批 12 角色，不重复计数）；**本库对象层**：owner=postgres、无 afs_* | 目标身份分层完全不存在 | 同 #3：8 个 `svc_*` 目标角色保留待 IP-4 校准（§7.0/§13 D-9）；旧 `app_role` 走 §7 | 同 #3（§5.1/INV-1~8） | 同 #3 |

【v1.3 关键新发现（写入矩阵 #2/#5）】**b7c8「授权基线反转」的业务效果在两个库不一致**：
- `ai_film_industrial`：`asset_service`/`project_planning` schema **无** defacl 记录（`ALTER DEFAULT PRIVILEGES ... REVOKE ALL` 生效）；
- `ai_film_verify`：两 schema 仍保留 `arwd`/`rU` 全量自动授权（main 版 9e3b 的旧状态）。
这证明两库 b7c8 的落地程度不同；阶段 3 前向补齐必须在 verify 库也实现默认零授权，并以「两库默认权限一致且无 arwd 自动授权」为验收标准。
【P1-3 保守原则】对 #2/#3/#5/#6 这类执行来源不可证的 Revision，**不推定未执行、不复用冲突 ID**；
以「当前实际结构」为准规划唯一 Revision 前向补齐，补齐后的结构验收标准见 C.3。

### C.3 结构验收标准（可计算验收矩阵）【v1.3 建立；v1.4 提升为可计算形式】

【v1.4 · P1-3】第四轮复审指出：v1.3 的「精确对象清单」仍含范围描述（如「各 ≥1 条 Policy」），
一条错误的 Policy 同样能满足「至少一条」。本轮把每条验收改为**可计算形式**：
`具体对象名 + 预期属性 + 预期角色 + 预期权限 + 负向行为测试`。属性取自 `pg_catalog`，
负向行为测试须用真实业务域连接在阶段 6 执行（不用超级用户）。原始证据见证据存档 E3/E4/E5/E7。

#### C.3.1 当前 RLS Policy 与目标的差距（取证事实，驱动前向补齐）

证据存档 E4 显示两库所有 RLS Policy 的形态**高度一致且与目标架构存在实质差距**：

| 维度 | 当前实际（E4） | 目标要求（§4/§5.3） | 差距 |
| --- | --- | --- | --- |
| Policy 名称 | `tenant_isolation_<table>`（每表一条） | 需按租户+组织+项目三上下文命名 | 语义不足 |
| `roles` | **全部 `public`** | 应按域角色限定（`TO svc_<domain>`） | 未限定角色 |
| `cmd` | **全部 `ALL`** | 应按 SELECT/INSERT/UPDATE/DELETE 分别约束 | 未分命令 |
| `permissive` | **全部 `PERMISSIVE`** | 需明确 permissive/restrictive 组合 | 未裁定 |
| `USING` | 仅 `tenant_id = current_setting('app.current_tenant_id')::uuid` | 需含组织+项目谓词 | **无组织/项目隔离** |
| `WITH CHECK` | **全部为空** | 写操作需 WITH CHECK 约束 | 写越权无拦截 |
| `relforcerowsecurity` | **全部 f** | 需强制 RLS（INV-7） | owner 可被豁免 |

**结论**：当前策略只有租户级读可见性，没有组织/项目级隔离、没有写合法性检查、不限定角色/命令。
这与 §4「三 GUC 项目级隔离」目标存在实质差距，是阶段 3/4 前向补齐与阶段 6 E1 验收的核心对象。

#### C.3.2 Policy 模型裁定：命名、命令拆分与组合语义【v1.4.1 新增，消除 v1.4 冲突】

第四轮复审指出 v1.4 的 C.3.2 同时要求「同一 `policyname='tenant_isolation_outbox_events'`」与「`cmd` 按命令
分别存在」——PostgreSQL 同一表内 Policy **名称唯一**，两者不可兼得。**唯一裁定：按命令拆分，每条 Policy
独立且确定命名**（不使用 `FOR ALL`）。理由：`USING` 过滤可见行、`WITH CHECK` 校验写入行，两者作用不同，
`FOR ALL` 只能给出一套表达式，无法表达「DELETE 一律无策略」这类差异；按命令拆分后，「无 DELETE 策略 ⇒
零行可删」本身成为 append-only 的纵深防御层。

**命名规则（确定性，可被 CI 逐条核对）**：`pol_<table>_<cmd>_<kind>`

- `<cmd>` ∈ {`select`, `insert`, `update`, `delete`}；`<kind>` ∈ {`scope`（PERMISSIVE）, `guard`（RESTRICTIVE）}。
- 同一表内每条命令至多一条 `scope` 与一条 `guard`，故名称天然唯一。
  示例：`pol_outbox_events_select_scope`、`pol_outbox_events_insert_scope`。
- 父表与分区各自是独立命名空间（同名不冲突），但**必须分别存在**：PG 对分区的直接访问**只应用分区自身的
  Policy**，父表 Policy 不会下推到「直接查分区」的语句。

**PERMISSIVE / RESTRICTIVE 组合裁定（每条命令必须可判定）**：

| 命令 | PERMISSIVE（必需） | RESTRICTIVE（可选） | 语义 | 若缺少 PERMISSIVE |
| --- | --- | --- | --- | --- |
| SELECT | `pol_<t>_select_scope`：`USING` = scope | 无（首版） | 仅 scope 内行可见 | 任何行都不可见 |
| INSERT | `pol_<t>_insert_scope`：`WITH CHECK` = scope | 无 | 写入行必须落在 scope 内 | 任何插入被拒（42501） |
| UPDATE | `pol_<t>_update_scope`：`USING` = scope **且** `WITH CHECK` = scope | 无 | 只能改 scope 内行，且不得改出 scope | 零行更新 |
| DELETE | **不建**（append-only / 无 DELETE 授权） | — | 无 permissive ⇒ 零行可删 | 即预期行为 |

组合规则（PG 原生语义，必须写明）：同一命令下多条 PERMISSIVE 之间为 **OR**；RESTRICTIVE 与「所有适用
PERMISSIVE 的并集」之间为 **AND**；**若某命令没有任何适用 PERMISSIVE，则该命令对任何行都不可见/不可写**。
因此 **不得只定义一条 RESTRICTIVE Policy 就宣称隔离成立**——v1.4 D.2 中 `permissive: false` 的单条示例
作废，改为「每命令一条 PERMISSIVE `scope` + 可选 RESTRICTIVE `guard`」。

**USING 与 WITH CHECK 的可观察结果（E1 必须按此断言，不得统一写成「被拒绝」）**：

| 场景 | 触发机制 | 正确预期（可观察） | SQLSTATE |
| --- | --- | --- | --- |
| 越权 SELECT | `USING` 过滤 | 返回 **0 行**（不是报错） | 无 |
| 越权 UPDATE（他域行） | `USING` 过滤 | **0 行受影响**（不是报错） | 无 |
| 越权 DELETE | `USING` 过滤 | **0 行受影响** | 无 |
| 非法 INSERT（越 scope） | `WITH CHECK` | **报错**，行未写入 | 42501（new row violates row-level security policy） |
| UPDATE 把行改出 scope | `WITH CHECK` | **报错** | 42501 |
| 无表级权限访问 | 权限检查**先于** RLS | **报错** | 42501（permission denied for table） |
| 上下文缺失（GUC 未设置） | 表达式求值为 NULL | **0 行**（fail-closed） | 无 |

> 这四类结果必须分别断言。把「0 行」写成「报错」会让测试在错误预期下通过——这正是复审指出的假通过风险。

**子句存在性裁定（v1.4.2 新增，与 D.6.0c 派生 1b 同源）**：命令决定合法子句，**不存在的子句必须完全省略**，
不得填空表达式、也不得统一输出双子句（PG16 `CREATE POLICY` 会直接语法报错）：

| 命令 | `USING` | `WITH CHECK` | `pg_policies` 断言 |
| --- | --- | --- | --- |
| SELECT | 必需 | **禁止出现** | `with_check IS NULL` |
| INSERT | **禁止出现** | 必需 | `qual IS NULL` |
| UPDATE | 必需 | 必需 | 两者均 `IS NOT NULL` |
| DELETE | 必需 | **禁止出现** | `with_check IS NULL` |

> v1.4.1 的 D.6.0c 生成模板对所有命令统一输出 `USING (...) WITH CHECK (...)`，对 `SELECT`/`DELETE` 生成**非法 SQL**、
> 对 `INSERT` 生成无语义子句；该模板在 v1.4.2 **作废**，以本表为唯一规则。

#### C.3.3 Policy 授权主体裁定（负向主体不得与授权主体混用）【v1.4.1 新增】

按 E10.4/E10.5 的实际 ACL 反推业务主体（当前 main 4 角色的授权是目标 `svc_*` 的映射依据）：

| 资源 | 允许的域角色（目标） | 依据（当前 ACL / 有效权限，E10.4/E10.5） |
| --- | --- | --- |
| `outbox.outbox_events`（父表 + 3 分区） | `svc_project_planning`、`svc_asset`（生产者，INSERT+SELECT）；`svc_outbox_relay`（消费者，SELECT） | project_planning_role `SI`、asset_service_role `SI`、outbox_relay_role `S` |
| `asset_service.asset_versions` | `svc_asset`（SELECT/INSERT/UPDATE） | asset_service_role `SIU` |
| `asset_service.qc_reports` | `svc_asset`（SELECT/INSERT） | asset_service_role `SI` |
| `audit.status_transitions` | `svc_project_planning`、`svc_asset`（SELECT/INSERT） | 两角色 `SI` |

- **负向测试主体必须是「对被测资源零权限」的域角色**：取 `svc_shot_planning`（当前零表权限，E10.5）。
- **`svc_asset` 是 outbox 的合法生产者，绝不能用作 outbox 的越权测试主体**——v1.4 用 `svc_asset` 直连分区
  做「跨域读被拒」测试会得到「读成功」的假阴性（复审 5.3 命中）。负向主体与授权主体必须分列。
- 该分列由 Registry `permissions` 自动推导（授权主体 = 有显式操作的域角色；负向主体 = 不在该资源
  `permissions` 中的域角色），不靠人工挑选。

#### C.3.4 b7c8 验收（分区保护 + 默认零授权 + append-only，可计算）【v1.4.1 修正】

对每个业务库分别执行；对象名与属性均为确定值（E4/E5/E10.4/E10.5/E10.7）：

- **分区 RLS（确定性对象清单，4 张表）**：`outbox.outbox_events`（partitioned 父表）、
  `outbox.outbox_events_default`、`outbox.outbox_events_y202611`、`outbox.outbox_events_y202612`。
  - 四者均须 `relrowsecurity=t` **且** `relforcerowsecurity=t`（当前四者 `t/f`，FORCE 全未启用）。
  - **目标 Policy 清单（每表 2 条，共 8 条）**：
    `pol_outbox_events_select_scope`（TO `svc_project_planning`,`svc_asset`,`svc_outbox_relay`；PERMISSIVE；
    `USING`=scope）、`pol_outbox_events_insert_scope`（TO `svc_project_planning`,`svc_asset`；PERMISSIVE；
    `WITH CHECK`=scope）。**不建 UPDATE/DELETE 策略**：当前无任何业务角色对 outbox 持有 UPDATE/DELETE
    （E10.5），无策略即零行可改删，属纵深防御。
  - 当前实际：每表 1 条 `tenant_isolation_outbox_events`，`roles=public`/`cmd=ALL`/`PERMISSIVE`/
    `USING` 仅 tenant/`WITH CHECK` 空（E4）→ 名称、角色、命令、表达式**四项均不符**。
  - 若 IP-4 确需 relay 回写投递状态（当前 `outbox_relay_role` 仅 `S`，E10.5），**必须显式新增**
    `pol_outbox_events_update_scope` 与 `UPDATE` 授权并登记，不得默认开启。
  - **父表/分区 ACL 不对称（取证事实）**：父表有 `app_role=S`，而三个分区**没有** `app_role` 授权
    （E10.4）。因此「经父表访问」与「直连分区访问」权限结果不同，验收必须分别断言，不能只测父表。
- **默认零授权（确定性属性）**：
  - 对象：`pg_default_acl` 中 schema `project_planning`/`outbox`/`asset_service` 的记录。
  - 预期：三 schema 的 `defaclacl` 不含 `INSERT/UPDATE/DELETE/TRUNCATE` 自动授权（`arwd` 中的 `awd` 须消失）；
    **verify 库当前仍为 `arwd`**（asset/project），industrial 已清除 → verify 必须前向补齐到一致。
  - 负向行为测试：新建表后业务角色默认无 DML，须显式 GRANT 才可写（fail-closed）。
- **append-only（按 ADR-R10/R11/R25 的正式定义，v1.4.1 纠正 v1.4 的错误）**：

  | 表 | 目标表级权限 | 依据 | 约束机制 |
  | --- | --- | --- | --- |
  | `asset_service.asset_versions` | `SELECT, INSERT, UPDATE`（**无 DELETE**） | R10「撤销 DELETE、保留状态迁移所需 UPDATE」；R25 矩阵同 | `trg_asset_versions_guard`（BEFORE UPDATE/DELETE，逐列白名单，R11：仅 `status`/`audit_ref`/`entity_version`/`updated_at` 类列可改）+ `trg_asset_versions_no_truncate` |
  | `asset_service.qc_reports` | `SELECT, INSERT`（**无 UPDATE/DELETE**） | R10「撤销 UPDATE, DELETE」；R25 矩阵同 | `trg_qc_reports_guard`（BEFORE UPDATE/DELETE 一律拒）+ `trg_qc_reports_no_truncate` |

  - **v1.4 的「asset_versions 无 UPDATE」是错误表述，作废**。UPDATE 是状态迁移的必需能力，其不可变性由
    R11 的列级白名单触发器保证，不由表级权限保证。
  - 负向行为测试（分别断言，不可合并）：① `DELETE` 任一表 → **42501 permission denied**（无表级权限）；
    ② `UPDATE asset_versions SET sha256=...`（内容列）→ **被触发器拒绝**；③ `UPDATE asset_versions
    SET status=...`（白名单列）→ **成功**；④ `UPDATE qc_reports` → 被触发器拒绝且/或无表级权限。
  - 【verify 差距】verify 库缺 `trg_asset_versions_no_truncate`/`trg_qc_reports_no_truncate` 与 `audit`
    schema（E10.7 第二次输出仅 2 个触发器）→ 前向补齐项。
- **alembic_version 读取**：对象 `public.alembic_version`，预期 `SELECT` 仅授予需要 `/readyz` 的域角色
  （当前 `asset_service_role`、`project_planning_role` 有 `S`，E10.5）；逐角色核对 `TRUNCATE`/`UPDATE`
  为 false。

#### C.3.5 9e3b 验收（角色与基础授权，可计算）

> 【v1.4 · 证据范围】角色存在与属性是**集群级**事实（§6.1.1），本小节的 `pg_roles`/`pg_auth_members`
> 验收对**整个实例一次**即可，不得对两库各计一遍；`owner`/ACL 等对象级验收才按库分别执行。

- **main 4 角色定位（过渡态；四个角色各有独立退役计划，见 §7.0 逐角色表）**：`app_role`、
  `project_planning_role`、`asset_service_role`、`outbox_relay_role` 均为 **main 侧过渡角色**，
  **不是最终架构的常驻角色**。v1.4 只写「DROP `app_role`」是不完整的：四个角色中 `app_role` 是跨域通用角色
  （无 1:1 目标），另三个各自有明确的目标 `svc_*` 对应（§7.0）。退役按域逐个 Contract，`app_role` 最后退役。
- **`svc_*` 当前零表权限（关键取证）**：E10.5 的有效权限矩阵中，8 个 `svc_*` **一行都不出现**——两库全部
  业务表权限都在 main 4 角色手上。因此「`svc_*` 已可承担运行时」不成立；它们只是**已创建但未授权、未被使用**
  的角色对象（与 E8 的代码零引用一致）。IP-4 必须完成授权与运行时切换后才谈「在用」。
- **目标态角色（确定性属性，INV-1~INV-10b）**：`svc_*` ×8（LOGIN）、`afs_owner`（NOLOGIN）、
  `afs_migrator`（LOGIN）、`ops_admin`（LOGIN）；逐个查 `pg_roles` 五属性位 + `pg_auth_members`
  直接边与传递闭包（当前 E2 显示**集群级**业务角色 12 个、零成员边、无 `afs_*`/`ops_admin`）。
- **成员关系选项**：窗口内 `afs_migrator → afs_owner` 边须 `inherit_option=f, set_option=t`（INV-6b）；
  常态无该边（INV-6）；`ops_admin` 在 `pg_auth_members` 中**零出边、零入边**——既不作为 member 持有任何角色
  （含 `afs_owner`），也不持有任何 `admin_option`（方案 A，INV-10/INV-10b）。
- **负向行为测试**：跨域读/写被拒（E1.1/E1.2）；`svc_*` 无 TRUNCATE（E1.6 扩展）。
- 【P1-3 硬约束】不得根据两库存在 8 个 `svc_*` 角色就推断 9e3b 分支版已执行；角色存在只证明
  「角色被创建过」，创建来源不可证，按保守原则处理。

#### C.3.6 b3c4 验收（资产归属 vs 状态约束，可计算）

- `ai_film_verify` 补齐后应存在的**确定性对象**（industrial 库为参照，见 E7）：
  - 约束：`asset_versions.project_id` 复合外键 `fk_asset_versions_project_tenant`、
    状态 CHECK `ck_asset_versions_status`（查 `pg_constraint.conname`）。
  - 表：`audit.status_transitions`（当前 verify 无 audit schema）。
  - 触发器/函数：append-only 拒绝 UPDATE/DELETE/TRUNCATE 的触发器及函数
    （industrial 参照：`audit.reject_mutation`/`reject_truncate`/`assert_transition_event_exists`；
    精确 `tgname` 以证据存档 E7b 为准）。
  - 索引：`asset_versions` 按项目列举的索引（E7 精确名）。
  - FORCE：`audit.status_transitions` 需 `relrowsecurity=t, relforcerowsecurity=t`。
  - 负向行为测试：无 event 的 transition 提交被拒、重复 entity_version 被拒（既有测试节点）。
- 两库均保留 main 资产归属业务效果；以 `pg_constraint`/`pg_class`/`pg_proc` 实际结构为准，
  不以 `alembic_version` 为准（§6.2 第 6 项）。

**两库逐项盘点结果（v1.3 取证，verify 为重点）**：

| 盘点项 | ai_film_verify | ai_film_industrial | 差异判定 |
| --- | --- | --- | --- |
| 业务表数（不含分区父表） | 12 | 13 | industrial 多 `audit.status_transitions` |
| schema | asset_service/auth/outbox/project_planning/public | + `audit` | verify 缺 audit schema |
| 函数 | 3：asset_versions_guard、qc_reports_guard（INVOKER）、outbox.list_active_tenants（DEFINER） | 6：+ audit.assert_transition_event_exists（DEFINER）、reject_mutation、reject_truncate | verify 缺 3 个 audit 函数 |
| RLS Policy | 10 | 11 | verify 缺 1 条（audit 表）；**两库 Policy 均 roles=public/cmd=ALL/PERMISSIVE/USING 仅 tenant/WITH CHECK 空**（E4） |
| 默认权限 | asset/project 仍 `arwd`+`rU` | asset/project 无 defacl | 授权基线反转不一致（关键） |
| 分区 RLS | 3 分区 RLS=t，父表 RLS=t，FORCE 全 f | 同 | 一致；FORCE 均未启用（INV-7 不满足） |
| 角色（**集群级共享**） | 集群共 12 业务角色（main 4 + svc 8），全无提权，业务角色零成员边（仅 PG 内建 pg_monitor 3 条），无 afs_*/ops_admin | **同一批**（非各有一份，勿重复计数） | 集群级一致；目标身份分层均不存在 |
| 成员关系选项 | 仅内建 pg_monitor（admin=f/inherit=t/set=t），业务角色零边 | 同 | 无 afs_migrator→afs_owner 窗口边（INV-6b 待实现） |
| 有效权限（TRUNCATE） | 仅 asset_service_role 有 sel/ins/upd，del/trunc=f；全部 svc_* 全 f | 同 | svc_* 无 TRUNCATE（符合 P2 TRUNCATE 默认禁止） |
| SECURITY DEFINER | outbox.list_active_tenants（owner=postgres） | + audit.assert_transition_event_exists（owner=postgres） | 须纳入 D.6 例外 6 字段登记 |
| 表 owner | 全 `postgres` | 全 `postgres` | 一致；均非 afs_owner（INV-1 不满足） |

### C.4 目标前向迁移与恢复方案

| Revision | 目标前向迁移（不复用冲突 ID） | 补齐后结构（见 C.3） | 异常恢复 |
| --- | --- | --- | --- |
| b3c4d5e6f7a8 | 保留 main 资产归属历史身份；分支状态 CHECK + 转移表 + audit 事实表用**全新唯一 Revision** 前向补齐 | verify 具备状态 CHECK + `audit.status_transitions` + 相关函数/policy | 一次性库演练 upgrade/downgrade/re-upgrade；真库仅前向 + 备份恢复演练（§6.4） |
| b7c8d9e0f1a2 | 保守保留已执行身份；分区保护 + 默认零授权 + append-only 以唯一 Revision 前向确保双方效果；verify 补齐到与 industrial 一致 | 两库默认权限一致且无 `arwd` 自动授权；分区 RLS + policy 在；FORCE=t | 同上；先完成来源盘点再定是否需补齐 |
| 9e3b4c5d6e7f | 保留应用角色历史；§5.1 四层身份经 Expand-Contract（§7）前向演进 | INV-1~INV-10b 在真实 PG 成立；`svc_*` 独立凭证；owner=afs_owner；窗口成员关系 inherit=f/set=t | main 4 个旧应用角色按 §7.0.1 逐角色退役（`app_role` 最后）；DROP 后按备份恢复/前向重建 |

**保守原则**：对 b7c8 / 9e3b 这类无法证明执行来源的冲突迁移，不自行推定未执行、不复用冲突 ID；
阶段 3 启动后按 C.3 结构验收标准在一次性库逐条演练，全绿（IP-3）后方可进入阶段 4。

### C.5 只读取证命令与原始输出存档（可复现，均为只读）【v1.4 建立；v1.4.1 补全缺口】

第四轮复审逐条比对后指出 C.5 的七处缺口（缺 `nspowner`/`relacl` 逐对象名/函数签名/`nspacl`/`datacl`/
序列视图 ACL/全量有效权限、E7 仅注释、Git 用可移动分支名）。v1.4.1 全部补入，并**按事实范围重组**：
集群级只采一次、库级分库采集、Git 固定 commit。

#### C.5.1 采集边界

- 全部为 `SELECT`；**未**执行任何 DDL/`GRANT`/`REVOKE`/`SET ROLE`/`upgrade`/`downgrade`。
- 口令从 `.env` 读取后只放环境变量，**不打印、不写入任何文件**。
- 采集身份为 `postgres`（仅用于读 `pg_catalog`）；**它不构成安全验证主体**——负向行为测试必须用真实
  域角色连接在阶段 6 执行（§12）。
- 集群级事实（`pg_roles`/`pg_auth_members`/`pg_database`，`relisshared=t`）**只采一次**；
  对象级事实（`pg_class`/`pg_namespace`/`pg_proc`/`pg_policy`/`pg_default_acl`/ACL）**分库采集**。

#### C.5.2 集群级（一次，不做两库重复统计）

```bash
PSQL="/d/Program Files/PostgreSQL/16/bin/psql"
PSQL_BASE=(-h localhost -p 15432 -U postgres)
export PGPASSWORD=$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)   # 不打印

# 实例身份与版本（证据归属）
"$PSQL" "${PSQL_BASE[@]}" -d postgres -c "SELECT version(), current_database();"
"$PSQL" "${PSQL_BASE[@]}" -d postgres -c "SELECT system_identifier FROM pg_control_system();"

# 证明角色目录是集群级共享目录
"$PSQL" "${PSQL_BASE[@]}" -d postgres -c \
 "SELECT relname, relisshared FROM pg_class WHERE relname IN ('pg_authid','pg_auth_members','pg_database');"

# 角色属性（全部业务相关角色 + 基础设施角色）
"$PSQL" "${PSQL_BASE[@]}" -d postgres -c \
 "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolinherit,
         rolreplication, rolconnlimit
  FROM pg_roles
  WHERE rolname LIKE 'svc_%' OR rolname LIKE '%_role'
     OR rolname IN ('postgres','afs_owner','afs_migrator','ops_admin')
  ORDER BY rolname;"

# 成员关系：含 PG16 三选项与 grantor（INV-6b/INV-10/INV-10b 的唯一证据来源）
"$PSQL" "${PSQL_BASE[@]}" -d postgres -c \
 "SELECT g.rolname AS group_role, m.rolname AS member_role,
         mbr.admin_option, mbr.inherit_option, mbr.set_option, gr.rolname AS grantor
  FROM pg_auth_members mbr
  JOIN pg_roles g ON g.oid = mbr.roleid
  JOIN pg_roles m ON m.oid = mbr.member
  JOIN pg_roles gr ON gr.oid = mbr.grantor
  ORDER BY 1,2;"

# 数据库级 ACL（datacl）与展开（含 PUBLIC，grantee=0）
"$PSQL" "${PSQL_BASE[@]}" -d postgres -c \
 "SELECT datname, pg_get_userbyid(datdba) AS owner,
         COALESCE(array_to_string(datacl,' | '),'<NULL=default>') AS datacl
  FROM pg_database WHERE NOT datistemplate ORDER BY 1;"
"$PSQL" "${PSQL_BASE[@]}" -d postgres -c \
 "SELECT d.datname, COALESCE(pg_get_userbyid(a.grantee),'PUBLIC') AS grantee, a.privilege_type
  FROM pg_database d
  CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl, acldefault('d'::\"char\", d.datdba))) a
  WHERE NOT d.datistemplate ORDER BY 1,2,3;"

# 基础设施角色属性核查（集群级；角色不存在时输出 role_exists=f，不得据「查不到」推断「合规」）
# v1.4.2 新增：INV-9 / INV-10 / INV-10b 的属性面证据
"$PSQL" "${PSQL_BASE[@]}" -d postgres -c \
 "SELECT x.rolname,
         (r.oid IS NOT NULL) AS role_exists,
         COALESCE(r.rolcanlogin,false)  AS canlogin,
         COALESCE(r.rolsuper,false)     AS super,
         COALESCE(r.rolbypassrls,false) AS bypassrls,
         COALESCE(r.rolcreatedb,false)  AS createdb,
         COALESCE(r.rolcreaterole,false) AS createrole,
         COALESCE(r.rolinherit,false)   AS inherit_attr,
         (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) AS member_of_count,
         (SELECT count(*) FROM pg_auth_members m WHERE m.roleid = r.oid) AS is_grantor_of_count,
         (SELECT count(*) FROM pg_auth_members m WHERE m.roleid = r.oid AND m.admin_option) AS admin_edges_held
  FROM (VALUES ('afs_owner'),('afs_migrator'),('ops_admin')) x(rolname)
  LEFT JOIN pg_roles r ON r.rolname = x.rolname;"

# INV-10b 的合取式判定（v1.4.2 新增）：输出 PASS/FAIL 而非"0 行"
# 关键：角色不存在时必须输出 FAIL，不能用「查不到任何违规边」冒充通过。
# E12.1/E12.3 实测：三个基础设施角色 role_exists=f，若只查违规边则全部返回 0 行 → 假通过。
"$PSQL" "${PSQL_BASE[@]}" -d postgres -c \
 "WITH o AS (SELECT oid FROM pg_roles WHERE rolname='afs_owner'),
       bad AS (SELECT count(*) AS n FROM pg_auth_members m, o
                WHERE m.roleid = o.oid AND m.admin_option
                  AND NOT EXISTS (SELECT 1 FROM pg_roles u WHERE u.oid=m.member AND u.rolsuper))
  SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM o) THEN 'FAIL: afs_owner 不存在（真空真陷阱，禁止记为通过）'
              WHEN (SELECT n FROM bad) > 0     THEN 'FAIL: 存在非超级用户的 admin_option 边'
              ELSE 'PASS' END AS inv_10b,
         EXISTS (SELECT 1 FROM o) AS role_exists,
         (SELECT n FROM bad)      AS offending_edges;"

# 递归成员关系传递闭包（INV-6b/INV-10/INV-10b 的「多层」证据；原始边列表不构成闭包）
# depth=1 为直接边；is_cycle 防止异常自引用导致无限递归
"$PSQL" "${PSQL_BASE[@]}" -d postgres -c \
 "WITH RECURSIVE g AS (
    SELECT m.roleid, m.member, m.admin_option, m.inherit_option, m.set_option, m.grantor,
           1 AS depth, ARRAY[m.roleid, m.member] AS path, false AS is_cycle
      FROM pg_auth_members m
    UNION ALL
    SELECT g.roleid, m.member, m.admin_option, m.inherit_option, m.set_option, m.grantor,
           g.depth + 1, g.path || m.member, m.member = ANY(g.path)
      FROM g JOIN pg_auth_members m ON m.roleid = g.member
     WHERE NOT g.is_cycle)
  SELECT rg.rolname AS granted_role, ru.rolname AS member_role, g.depth,
         g.admin_option AS admin, g.inherit_option AS inherit_opt, g.set_option AS set_opt,
         pg_get_userbyid(g.grantor) AS grantor, g.is_cycle,
         array_to_string(ARRAY(SELECT pg_get_userbyid(x) FROM unnest(g.path) x), ' -> ') AS chain
    FROM g JOIN pg_roles rg ON rg.oid = g.roleid
           JOIN pg_roles ru ON ru.oid = g.member
   WHERE left(rg.rolname,3) <> 'pg_' AND left(ru.rolname,3) <> 'pg_'
   ORDER BY 1,2,3;"
```

#### C.5.3 数据库级（对 `ai_film_industrial` 与 `ai_film_verify` 各执行一次）

```bash
for DB in ai_film_industrial ai_film_verify; do
  # ① Alembic revision（执行版本证据，不等于结构证据）
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c "SELECT version_num FROM alembic_version;"

  # ② Schema OWNER + nspacl
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT nspname, pg_get_userbyid(nspowner) AS owner,
           COALESCE(array_to_string(nspacl,' | '),'<NULL=default>') AS nspacl
    FROM pg_namespace
    WHERE nspname NOT LIKE 'pg_%' AND nspname<>'information_schema' ORDER BY 1;"

  # ③ 逐对象 OWNER（表/分区/序列/视图/物化视图，输出具体名称，不做聚合计数）
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned'
                          WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' END AS objtype,
           n.nspname||'.'||c.relname AS object, pg_get_userbyid(c.relowner) AS owner
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE c.relkind IN ('r','p','S','v','m')
      AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
    ORDER BY 1,2;"

  # ④ 函数：完整签名（区分重载）+ OWNER + SECDEF + proconfig(search_path)
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) AS args,
           pg_get_userbyid(p.proowner) AS owner, p.prosecdef AS security_definer,
           p.provolatile, COALESCE(array_to_string(p.proconfig,','),'<none>') AS proconfig
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
    ORDER BY 1,2,3;"

  # ⑤ 函数 proacl 展开（NULL 即默认 EXECUTE TO PUBLIC，必须展开才能看见 PUBLIC）
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT n.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||')' AS func,
           COALESCE(pg_get_userbyid(a.grantee),'PUBLIC') AS grantee, a.privilege_type
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f'::\"char\", p.proowner))) a
    WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
    ORDER BY 1,2,3;"

  # ⑥ 全部对象 ACL（表/分区/序列/视图/物化视图，含 PUBLIC）
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT n.nspname||'.'||c.relname AS object,
           CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned'
                          WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' END AS objtype,
           COALESCE(pg_get_userbyid(a.grantee),'PUBLIC') AS grantee, a.privilege_type
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    CROSS JOIN LATERAL aclexplode(
      COALESCE(c.relacl, acldefault(CASE WHEN c.relkind='S' THEN 'S'::\"char\" ELSE 'r'::\"char\" END, c.relowner))
    ) a
    WHERE c.relkind IN ('r','p','S','v','m')
      AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
    ORDER BY 1,3,4;"

  # ⑦ RLS Policy 完整定义（数量不是安全性证明）
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT schemaname, tablename, policyname, array_to_string(roles,',') AS roles,
           cmd, permissive, qual AS using_expr, with_check
    FROM pg_policies ORDER BY 1,2,3;"
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' ORDER BY 1,2;"

  # ⑧ 默认权限（只证明配置，不证明有效权限）
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT n.nspname, pg_get_userbyid(d.defaclrole) AS defaclrole, d.defaclobjtype,
           array_to_string(d.defaclacl,' | ') AS defaclacl
    FROM pg_default_acl d JOIN pg_namespace n ON n.oid=d.defaclnamespace ORDER BY 1,2,3;"

  # ⑨ 全量有效权限（12 角色 × 全部业务表；未列出的行=全 f）
  # 【v1.4.2 修正】relkind 必须含 'p'：v1.4.1 用 relkind='r' 会漏掉分区父表 outbox_events，
  # 而 C.3.4 恰恰认定「父表有 app_role SELECT、三个分区没有」——旧查询无法复现自己认定的差异。
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT r.rolname, n.nspname||'.'||c.relname AS object,
           concat_ws('',
             CASE WHEN has_table_privilege(r.oid,c.oid,'SELECT')     THEN 'S' END,
             CASE WHEN has_table_privilege(r.oid,c.oid,'INSERT')     THEN 'I' END,
             CASE WHEN has_table_privilege(r.oid,c.oid,'UPDATE')     THEN 'U' END,
             CASE WHEN has_table_privilege(r.oid,c.oid,'DELETE')     THEN 'D' END,
             CASE WHEN has_table_privilege(r.oid,c.oid,'TRUNCATE')   THEN 'T' END,
             CASE WHEN has_table_privilege(r.oid,c.oid,'REFERENCES') THEN 'R' END,
             CASE WHEN has_table_privilege(r.oid,c.oid,'TRIGGER')    THEN 'G' END) AS privs,
           c.relkind      -- v1.4.2：显式输出 relkind，使 p（分区父表）与 r（分区/普通表）可直接区分
    FROM pg_roles r
    CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
      AND (r.rolname LIKE 'svc_%' OR r.rolname LIKE '%_role'
           OR r.rolname IN ('afs_owner','afs_migrator','ops_admin'))
      AND (has_table_privilege(r.oid,c.oid,'SELECT') OR has_table_privilege(r.oid,c.oid,'INSERT')
        OR has_table_privilege(r.oid,c.oid,'UPDATE') OR has_table_privilege(r.oid,c.oid,'DELETE')
        OR has_table_privilege(r.oid,c.oid,'TRUNCATE') OR has_table_privilege(r.oid,c.oid,'REFERENCES')
        OR has_table_privilege(r.oid,c.oid,'TRIGGER'))
    ORDER BY 1,2;"

  # ⑩ 约束（含完整定义）
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT n.nspname, c.conname, c.contype, c.conrelid::regclass AS tbl,
           pg_get_constraintdef(c.oid) AS definition
    FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
    WHERE n.nspname IN ('audit','asset_service','project_planning','outbox','auth','public')
    ORDER BY 1,2;"

  # ⑪ 触发器 + 触发器函数完整签名 + 启用状态
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT n.nspname AS tbl_schema, c.relname AS tbl, t.tgname, t.tgenabled,
           fn.nspname||'.'||f.proname||'('||pg_get_function_identity_arguments(f.oid)||')' AS trigger_function,
           pg_get_userbyid(f.proowner) AS fn_owner, pg_get_triggerdef(t.oid) AS trigger_def
    FROM pg_trigger t
    JOIN pg_class c ON c.oid=t.tgrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    JOIN pg_proc f ON f.oid=t.tgfoid
    JOIN pg_namespace fn ON fn.oid=f.pronamespace
    WHERE NOT t.tgisinternal AND n.nspname NOT LIKE 'pg_%' ORDER BY 1,2,3;"

  # ⑫ 索引完整定义
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT n.nspname AS schema, t.relname AS tbl, i.relname AS index_name,
           pg_get_indexdef(ix.indexrelid) AS index_def
    FROM pg_index ix
    JOIN pg_class i ON i.oid=ix.indexrelid
    JOIN pg_class t ON t.oid=ix.indrelid
    JOIN pg_namespace n ON n.oid=t.relnamespace
    WHERE n.nspname IN ('audit','asset_service','project_planning','outbox','auth')
    ORDER BY 1,2,3;"

  # ⑬ 分区父子 ACL 对照（v1.4.2 新增）：递归展开分区树，把父表与每个子分区的 ACL 并排输出，
  #    可直接复现「父表有 app_role SELECT、子分区没有」的不对称，并给出 has_table_privilege 判定
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "WITH RECURSIVE t AS (
      -- 【v1.4.2 修复 E12 实测缺陷】非递归项 NULL 的 collation 为 default、递归项为 "C"，
      -- 直接写 NULL::text 会报 `recursive query "t" column 6 has collation "default" ... but "C" overall`；
      -- 必须显式 COLLATE "C" 才能执行。
      SELECT c.oid, n.nspname||'.'||c.relname AS object, c.relkind, c.relacl, c.relowner, NULL::text COLLATE "C" AS parent
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE c.relkind='p' AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
      UNION ALL
      SELECT ch.oid, n.nspname||'.'||ch.relname, ch.relkind, ch.relacl, ch.relowner, t.object
        FROM t JOIN pg_inherits i ON i.inhparent=t.oid
               JOIN pg_class ch ON ch.oid=i.inhrelid
               JOIN pg_namespace n ON n.oid=ch.relnamespace)
    SELECT COALESCE(t.parent,'<parent>') AS parent, t.object, t.relkind,
           COALESCE(pg_get_userbyid(a.grantee),'PUBLIC') AS grantee, a.privilege_type,
           has_table_privilege('app_role', t.oid, 'SELECT') AS app_role_select,
           has_table_privilege('asset_service_role', t.oid, 'SELECT') AS asset_service_role_select
      FROM t CROSS JOIN LATERAL aclexplode(COALESCE(t.relacl, acldefault('r'::"char", t.relowner))) a
     ORDER BY 1,2,4,5;"

  # ⑭ 全对象类型最终有效权限（v1.4.2 新增）：Schema / Table+Partition / View+MatView / Sequence / Function / Database
  #    区分三层口径：⑥ 是「直接 ACL」、⑧ 是「默认 ACL」、本查询是「最终有效权限」（含继承与 PUBLIC）
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "WITH r AS (
      SELECT oid, rolname FROM pg_roles
       WHERE left(rolname,4)='svc_' OR rolname LIKE '%_role'
          OR rolname IN ('afs_owner','afs_migrator','ops_admin'))
    SELECT 'SCHEMA' AS objtype, r.rolname, n.nspname AS object,
           concat_ws('', CASE WHEN has_schema_privilege(r.oid,n.oid,'USAGE')  THEN 'U' END,
                          CASE WHEN has_schema_privilege(r.oid,n.oid,'CREATE') THEN 'C' END) AS privs
      FROM r CROSS JOIN pg_namespace n
     WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
    UNION ALL
    SELECT 'TABLE/PARTITION', r.rolname, n.nspname||'.'||c.relname,
           concat_ws('', CASE WHEN has_table_privilege(r.oid,c.oid,'SELECT')     THEN 'S' END,
                          CASE WHEN has_table_privilege(r.oid,c.oid,'INSERT')     THEN 'I' END,
                          CASE WHEN has_table_privilege(r.oid,c.oid,'UPDATE')     THEN 'U' END,
                          CASE WHEN has_table_privilege(r.oid,c.oid,'DELETE')     THEN 'D' END,
                          CASE WHEN has_table_privilege(r.oid,c.oid,'TRUNCATE')   THEN 'T' END,
                          CASE WHEN has_table_privilege(r.oid,c.oid,'REFERENCES') THEN 'R' END,
                          CASE WHEN has_table_privilege(r.oid,c.oid,'TRIGGER')    THEN 'G' END)
      FROM r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
    UNION ALL
    SELECT 'VIEW/MATVIEW', r.rolname, n.nspname||'.'||c.relname,
           concat_ws('', CASE WHEN has_table_privilege(r.oid,c.oid,'SELECT') THEN 'S' END,
                          CASE WHEN has_table_privilege(r.oid,c.oid,'INSERT') THEN 'I' END,
                          CASE WHEN has_table_privilege(r.oid,c.oid,'UPDATE') THEN 'U' END,
                          CASE WHEN has_table_privilege(r.oid,c.oid,'DELETE') THEN 'D' END)
      FROM r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE c.relkind IN ('v','m') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
    UNION ALL
    SELECT 'SEQUENCE', r.rolname, n.nspname||'.'||c.relname,
           concat_ws('', CASE WHEN has_sequence_privilege(r.oid,c.oid,'USAGE')  THEN 'U' END,
                          CASE WHEN has_sequence_privilege(r.oid,c.oid,'SELECT') THEN 'S' END,
                          CASE WHEN has_sequence_privilege(r.oid,c.oid,'UPDATE') THEN 'W' END)
      FROM r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE c.relkind='S' AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
    UNION ALL
    SELECT 'FUNCTION', r.rolname,
           n.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||')',
           CASE WHEN has_function_privilege(r.oid,p.oid,'EXECUTE') THEN 'X' ELSE '' END
      FROM r CROSS JOIN pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
     WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND p.prokind='f'
    UNION ALL
    SELECT 'DATABASE', r.rolname, current_database(),
           concat_ws('', CASE WHEN has_database_privilege(r.oid,current_database(),'CONNECT')   THEN 'C' END,
                          CASE WHEN has_database_privilege(r.oid,current_database(),'TEMPORARY') THEN 'T' END,
                          CASE WHEN has_database_privilege(r.oid,current_database(),'CREATE')    THEN 'X' END)
      FROM r
     ORDER BY 1,2,3;"

  # ⑮ 空权限行的显式补集（v1.4.2 新增）：④~⑭ 只输出「有权限」的行，无法证明「某角色在某对象上为零」。
  #    对每个「角色 × 业务表」输出完整的 f/t 布尔矩阵，用于断言「svc_* 当前零表权限」这类事实
  "$PSQL" "${PSQL_BASE[@]}" -d "$DB" -c \
   "SELECT r.rolname, n.nspname||'.'||c.relname AS object,
           has_table_privilege(r.oid,c.oid,'SELECT')     AS sel,
           has_table_privilege(r.oid,c.oid,'INSERT')     AS ins,
           has_table_privilege(r.oid,c.oid,'UPDATE')     AS upd,
           has_table_privilege(r.oid,c.oid,'DELETE')     AS del,
           has_table_privilege(r.oid,c.oid,'TRUNCATE')   AS trun,
           has_table_privilege(r.oid,c.oid,'REFERENCES') AS ref,
           has_table_privilege(r.oid,c.oid,'TRIGGER')    AS trg
      FROM pg_roles r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
       AND (left(r.rolname,4)='svc_' OR r.rolname LIKE '%_role'
            OR r.rolname IN ('afs_owner','afs_migrator','ops_admin'))
     ORDER BY 1,2;"
done
```

> 【v1.4.2 · 口径声明】**直接 ACL（⑥⑬）≠ 默认 ACL（⑧）≠ 最终有效权限（⑨⑭⑮）**，三者必须分开取证。
> 任何「某项有效权限已完整验证」的结论，只能引用 ⑨/⑭/⑮ 的输出；⑥⑬ 证明的是授权形态而非有效结果。
> 查询未覆盖的对象类型（如 `relkind='f'` 外部表、`relkind='c'` 复合类型）在本 ADR 范围内**不存在**（E10.2 实测），
> 故不宣称覆盖，也不得反向宣称「已全覆盖」。

#### C.5.4 Git 取证身份（固定 commit，不用可移动分支名）

v1.4 用 `origin/feat/identity-data-ownership` 作为证据身份，分支推进后同一命令会得到不同结果——**不可复现**。
v1.4.1 固定为完整 commit SHA（当前值，后续复审须核对这些 SHA 未变）：

| 侧 | ref | 完整 commit SHA |
| --- | --- | --- |
| main | `main` | `5352d1979421f41bfb9943774378671cfe94de71` |
| 分支 | `feat/identity-data-ownership` | `59b13af73640d10b81703b514202173976c432c5` |

```bash
MAIN=5352d1979421f41bfb9943774378671cfe94de71
BR=59b13af73640d10b81703b514202173976c432c5

# main 侧三个冲突文件：在固定 commit 上取内容再算哈希（不读工作区，避免未提交改动污染）
for f in b3c4d5e6f7a8_asset_project_ownership.py \
         b7c8d9e0f1a2_partition_protection_and_grant_baseline.py \
         9e3b4c5d6e7f_create_app_role_and_grants.py; do
  p="data/migrations/versions/$f"
  echo "-- $MAIN:$p"
  git show "$MAIN:$p" | sha256sum
  git rev-parse "$MAIN:$p"
done

# 分支侧三个冲突文件：同上，固定 commit
for f in b3c4d5e6f7a8_asset_status_transitions.py \
         b7c8d9e0f1a2_partition_protection_and_grant_baseline.py \
         9e3b4c5d6e7f_create_app_role_and_grants.py; do
  p="data/migrations/versions/$f"
  echo "-- $BR:$p"
  git show "$BR:$p" | sha256sum
  git rev-parse "$BR:$p"
done
```

#### C.5.5 原始证据存档索引（可独立复核）

存档：`docs/audits/2026-11-19-r40-readonly-evidence.md`（每条命令 + 原始输出逐条留存）。

| 段 | 内容 | 对应正文 |
| --- | --- | --- |
| E0 | 环境、版本、非模板库列表 | §6.1 |
| E1 | 两库 `alembic_version` | C.2 第 5 列 |
| E2 / E9 / E10.0 | 角色属性 + 完整成员关系（含三选项）+ `relisshared` 证明 | §6.1.1 / §6.5 表A / INV-6b/10/10b |
| E3 / E10.1 / E10.2 | Schema/表/序列/函数 OWNER（含对象名） | INV-1 / C.3.6 |
| E4 / E10.7 | Policy 完整定义（roles/cmd/permissive/USING/WITH CHECK）+ FORCE | C.3.1 / C.3.2 / C.3.4 |
| E5 / E10.3 / E10.4 / E10.5 | 默认权限、对象 ACL（含 PUBLIC、序列/视图）、全量有效权限 | C.3.3 / C.3.4 / D.5 |
| E6 / E10.9 | 六个迁移文件 SHA256 + blob（v1.4.1 固定 commit） | C.1 / C.2 第 3/4 列 |
| E7/E7b / E10.6 / E10.8 | 约束定义、触发器（含函数签名）、索引定义 | C.3.6 |
| E8 | 活跃会话 + 运行时代码引用（§6.5 依赖盘点） | §6.5 表B / 附录 E #9 |
| **E11** | **GUC 数组语义与真实键类型**（v1.4.2 新增）：`string_to_array` 错误解析、`::uuid[]`/`::text[]` 正确解析、`project_id` 真实类型与取值形态、`project-1` 触发 22P02、旧/新策略表达式在真实表上的可见行数对比（0 行 vs 684 行）、`text[]` 边界（空串/畸形/含逗号需引号） | §4.2 / D.6.0c 派生 0~1 / 附录 E #17 |
| **E12** | **补齐查询的首次实跑**（v1.4.2 新增）：基础设施角色 `role_exists`、INV-10b 合取式判定（PASS/FAIL）、原始违规边、递归成员闭包、⑨ 两库全量有效权限、⑬ 分区父子 ACL（含其 collation 缺陷修复）、⑭ 全对象类型有效权限（6 类）、⑮ 零权限补集布尔矩阵、两库漂移精确值 | **§5.3 / C.5.2 / C.5.3 ⑨⑬⑭⑮ / §7.0.4 / 附录 E #20** |

> **【v1.4.2 · 交付完整性】证据存档作为独立交付物提交，可用下列信息校验未被裁剪：**

| 文件 | 行数 | 字节 | SHA256 |
| --- | --- | --- | --- |
| `docs/audits/2026-11-19-r40-readonly-evidence.md` | 2317 | 190996 | `11b4d44c66a43a0969bff4f1ecd25e2751873596b05c95c9cc9d43da87f3ea78` |
| `docs/audits/2026-11-19-r40-e12-raw.txt`（E12 完整原始输出，机器可读） | 1184 | 110289 | `d8f57d1ba259447d21e7f9e0d6d367e5ca2ef5789edb9f1b78406fc9d7e79847` |

> 校验命令：`sha256sum docs/audits/2026-11-19-r40-readonly-evidence.md`
> 复核方式：不需重跑 SQL——每条命令与其原始输出已**成对**留存；重跑只用于确认环境未变（版本/端口/system_identifier 见 E0/E10.0）。

> 【P1-3 证据口径】`pg_default_acl` 只证明默认权限**配置**，不能单独证明对象**有效权限**；必须同时核查
> `pg_class.relacl`（`aclexplode` 展开）与 `has_*_privilege`。`pg_proc.proacl` 为 NULL 时表示
> 「默认 EXECUTE TO PUBLIC」，**只有用 `acldefault` 展开才看得见 PUBLIC**——E10.3 正是这样发现 5 个函数
> 带默认 `PUBLIC EXECUTE`。负向行为测试另用真实域角色连接在阶段 6 执行（§12）。

### C.6 归属约束在脏状态下的行为（E13，【v1.4.3】新增，49 步全绿）

【v1.4.3 新增】**命名冲突先声明**：本节代号 `E13`，与 C.1.0 口径中的分叉**类型 T3**
（`9e3b` 角色属性收敛）**毫无关系**。两者都曾被叫作「T3」，本版起一律用 `E13`（证据编号）
与「类型 T3」（分叉分类）区分，不得混用。

**为什么这项证据属于本 ADR（不是跑题）**：§5.3 的 RLS 以 `organization_id` 作为项目级 scope 的判定键
（ADR-R24）。**如果 `organization_id` 这一列本身不被任何约束校验，那么 RLS 的组织边界就建立在一个
可任意写入的列上**——授权模型「数据库层 fail-closed」的承诺随之落空。本节就是这条链的实证检验。

**环境**：由分支链 head 的模板库克隆出 `t3_lab`（port 15444，独立集群），实验后即弃；
模板库、两分支 head 库、`ai_film_industrial`、`ai_film_verify`、生产均未修改。

| 场景 | 实测 SQLSTATE | 结论 |
| --- | --- | --- |
| 1 正常归属 | 外键建成（`convalidated=t`） | 归属完整时无副作用 |
| 2 孤儿项目 | 建键 `23503`；修后 `VALIDATE` 成功 | 拒绝而非静默删数据（行数不变） |
| 3 租户错配 | `23503` | 两列外键即可拦 |
| 4 **组织错配** | **两列外键放行**；三列外键 `23503` | **两列方案有组织盲区** |
| 5 哨兵值 `legacy-unassigned` | 触发器 `P0001`；写不存在项目**放行**；含哨兵行时建键 `23503` | 触发器只挡一个魔法字符串，**不是归属约束** |
| 6 NULL / MATCH SIMPLE | `23502`；62 个归属列可空数 = **0** | 分支侧放行路径列级不可达 |
| 7 `NOT VALID` 分阶段 | 跳过历史扫描，新写入仍 `23503` | 不是长期豁免 |

**四项对本 ADR 有直接后果的发现**：

1. **「约束已存在」≠「不变量成立」**：主支已有 `fk_asset_versions_project_tenant`（`convalidated=t`），
   但它只比 `(project_id, tenant_id)`，**组织错配行可原样通过**（场景 4）。这与附录 E #20 的
   「真空真」是同一类错误的两种形态：**一个只看「有没有违规行」的不变量，无法发现「不变量本身没覆盖那一维」**。
   ⇒ §8.1-B 的验收标准必须写**拦截用例**（该行必须被 `23503` 拒绝），不得写「约束已创建」。
2. **归属列同时是「不可变内容列」**：`trg_asset_versions_guard` 对 `project_id`/`organization_id`/`tenant_id`
   的就地 `UPDATE` 一律 `P0001`（ADR-R11）。⇒ 历史脏归属**不能**在业务路径上修正，
   补齐只能走「维护窗口（成对 `DISABLE/ENABLE TRIGGER` + `UPDATE` + `VALIDATE`）」或「前向新建版本」。
   这直接决定 IP-4 迁移的形态，不是实现细节。
3. **两侧可空性契约相反**：分支侧 62 个归属列全 `NOT NULL`；主支 `projects.organization_id` 可空，
   且在 `organizations` **0 行**时写 `NULL` 组织仍成功（`MATCH SIMPLE` 跳过），而主支 RLS 策略
   显式放行 `organization_id IS NULL`。⇒ 合并时把 NULL 组织行迁入 `NOT NULL` 列会 `23502`，
   **`NOT VALID` 挡不住 `23502`**。收敛方向应为「向分支的 NOT NULL 收敛」，不是把分支放宽。
4. **追加型守卫拦不住 `TRUNCATE`**（不触发行级触发器）。⇒ 「资产不可删除」的准确表述是
   **「经 DML 不可删除」**，验收与对外表述都不得扩大。

**产物**：`docs/audits/mig-recon/t3_dirty_state.py`（驱动）、`t3-dirty-state.md`/`.json`（机器原文）、
`docs/audits/2026-11-20-t3-dirty-state-findings.md`（结论与未覆盖项）。
**复现**：`python docs/audits/mig-recon/t3_dirty_state.py`（每次重建 `t3_lab`）。
**边界**：只以 `asset_versions` 为代表性表跑通全链路，**不外推为 14 表均已验证**；
`VALIDATE` 在并发/大表上的锁级别与耗时、`ON DELETE` 语义均未测。

## 附录 D：Authorization Registry 设计定义（v1.2 建立，v1.3 收敛为可实施定义）

【v1.2 建立，v1.3 · P2 结构性收敛】唯一授权事实源的定义。目标：把散落在 Python/SQL/文档/迁移中的业务授权规则
收敛到一个权威结构，其余各层由它派生或由 CI 校验一致。**不是新权限管理系统，不引入复杂抽象，
不让 Python/SQL/文档/迁移各自维护一份独立授权事实。**

### D.1 唯一物理载体（v1.3 新增，P2 必答 1）

- **唯一权威载体**：`contracts/authorization/authorization_registry.yaml`（结构化 YAML，纳入契约冻结与版本控制）。
  它是业务授权规则的**唯一事实源**；Python 运行时、PostgreSQL GRANT/Policy、连接池映射、CI 校验均从它派生或向它对账。
- **不得**存在第二份独立维护的授权事实：`packages/shared/db_grants.py`、`packages/shared/db_roles.py`、
  迁移中的 `GRANT` 语句、本文档的表格，都必须是 Registry 的**派生结果或只读投影**，不得反过来成为事实源。
  本 ADR 表格只描述设计意图，不构成授权事实；落地以 Registry 为准。
- **与现有契约的关系**：沿用 `contracts/security_boundary.yaml` 的「assertion + runtime_evidence + 禁止手写 satisfied」机制
  （该文件已由 `scripts/contract_check.py::check_security_boundary` 执行）；Registry 是其中授权规则的结构化落地，
  不新造一套并行契约闸门。
- **实现归 IP-4（阶段 4）**，设计完成归 DP5（阶段 3 前）。
  【v1.4.3 更正】v1.4.2 此处写「本轮**只定义载体，不创建文件**」——**该句已失真，本版作废**。
  载体**已创建**并已纳入契约闸门：

  | 项 | 事实（2026-11-20 实测） |
  | --- | --- |
  | 载体路径 | `contracts/authorization/authorization_registry.yaml` |
  | 规模 | **337 行**；`SHA-256 = 3f941e9e675b1d1d00770c3ce1adb8f3ac234ec93bc64d570418a11512ca9250` |
  | 内容 | **8 域** / 3 个基础设施角色 / **7 资源** / **16 条 Policy AST** / 2 条 SECDEF 例外 / 4 条显式债务 |
  | 派生实现 | `packages/shared/authorization/{ast,registry,ddl}.py`（AST → SQL 派生，含按命令分支的 Policy 子句） |
  | 契约闸门 | `scripts/contract_check.py::check_authorization_registry`（第 **12** 项），并校验 `permissions_source.module` 真实存在 |
  | 单测 | `tests/unit/test_authorization_registry.py`，**27 例** |
  | 文件头登记的 `design_delta` | DD-1：`scope_dimensions` 改为「列名 → 类型」映射；DD-2：`permissions` 本轮不由 Registry 接管（仍以 `permission_matrix.yaml` 为准） |

  > **为什么这不能写成「已实现」**：载体存在 ≠ DP5 满足，更 ≠ IP-4 完成。已创建的只是
  > **结构化事实源 + 派生器 + 静态闸门**；「生成器能否对真实库产出可执行迁移」「真实 PG 上实际权限
  > 与 Registry 是否无漂移」分别属 DP5 与 IP-6，仍为未完成（§8.1-A / §8.1-B）。
  > 本行的意义只有一个：**文档不得再声称「未创建文件」，否则复审者无法判断实现边界是否被越过。**


### D.2 结构化定义（v1.3 新增，P2 必答 2）

Registry 顶层结构（字段为设计定义，非已实现的 schema）：

```yaml
schema_version: "1.2"    # v1.4.1：资源 ID 四段齐全 + RLS 结构化 AST + 权限/例外修正
databases:      # 数据库逻辑 ID ⇄ PostgreSQL 实际库名映射（D.3）
  industrial: { physical_name: ai_film_industrial, role: business }
  verify:     { physical_name: ai_film_verify,     role: business }
  # postgres 系统库不登记：不参与业务迁移对账（§6.1）
domains:        # 第1部分：域身份 + 域→运行时角色映射（**唯一权威**；v1.4.2 删除重复的 roles.mappings）
  - { id: project_planning,     runtime_role: svc_project_planning }
  - { id: shot_planning,        runtime_role: svc_shot_planning }
  - { id: generation_execution, runtime_role: svc_generation_execution }
  - { id: asset_service,        runtime_role: svc_asset }
  - { id: qc_governance,        runtime_role: svc_qc_governance }
  - { id: workflow,             runtime_role: svc_workflow }
  - { id: outbox_relay,         runtime_role: svc_outbox_relay }
  - { id: identity,             runtime_role: svc_identity }
roles:          # 第2部分：**基础设施角色**（身份四层，§5.1）。域→业务角色映射不在此处（v1.4.2）
  infrastructure_roles:
    object_owner:
      name: afs_owner
      login: false
      attributes: [NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE]        # INV-9
    migration_executor:
      name: afs_migrator
      login: true
      owner_membership: { mode: window_only, admin: false, inherit: false, set: true }  # INV-6b
      granted_by: bootstrap_superuser     # 受控编排步骤；**不是** ops_admin（§5.1.3 方案 A）
    ops_admin:
      name: ops_admin
      login: true
      attributes: [NOCREATEROLE, NOCREATEDB, NOSUPERUSER, NOBYPASSRLS]
      owner_membership: none              # 无成员边、无 ADMIN OPTION（§5.1.3）
      capabilities: [read_only_diagnostics, process_ops]
resources:      # 第3部分：资源映射（唯一标识规则见 D.3：四段必填）
  - id: industrial.asset_service.table.asset_versions
    database: industrial
    schema: asset_service
    object_type: table
    object_name: asset_versions
    domain: asset_service
    scope_dimensions: [tenant_id, project_id]   # 本表**实际具备**的隔离维度（无 organization_id）
    rls:
      enabled: true
      force: true
      policies:                       # 每条 Policy 独立命名（C.3.2 命名规则），不含 DELETE
        - id: pol_asset_versions_select_scope
          commands: [SELECT]
          permissive: true            # 允许访问必须有 PERMISSIVE；RESTRICTIVE 只能叠加约束
          roles: [svc_asset]
          using: &scope_asset
            all:
              - eq: { column: tenant_id,  value: { guc: app.current_tenant_id } }
              - in: { column: project_id, value: { guc_array: app.current_projects, cast: text } }
        - id: pol_asset_versions_insert_scope
          commands: [INSERT]
          permissive: true
          roles: [svc_asset]
          with_check: *scope_asset     # 写入必须落在同一 scope（AST 引用，不复制）
        - id: pol_asset_versions_update_scope
          commands: [UPDATE]
          permissive: true
          roles: [svc_asset]
          using: *scope_asset
          with_check: *scope_asset     # 防止把行改出 scope
        # 无 DELETE 策略：append-only（R10/R25），无 permissive ⇒ 零行可删
permissions:    # 第4部分：操作权限（每资源 × 每角色，枚举与对象类型约束见 D.4）
  - resource: industrial.asset_service.table.asset_versions
    role: svc_asset
    operations: [SELECT, INSERT, UPDATE]   # 无 DELETE/TRUNCATE（R10：撤销 DELETE、保留状态迁移 UPDATE）
exceptions:     # 第5部分：显式例外（字段见 D.5）
  - object: industrial.outbox.function.list_active_tenants
    kind: SECURITY_DEFINER
    reason: "SECURITY DEFINER 提供租户枚举（当前实现）"
    owner: <具名责任人>
    unblocks_when: "改为按 RLS 直接读取后移除"
    expires_at: "2027-01-01"
    execute_roles: [svc_identity]        # 目标：仅该域可执行
    effective_owner: afs_owner           # **目标** OWNER（与 INV-1 一致）；当前观测为 postgres（E10.3）
    observed_owner: postgres             # 只读取证值，不得直接当成目标期望值
    search_path: "pg_catalog, pg_temp"   # 目标：pg_catalog 首位 + pg_temp 显式末位（当前 outbox, pg_catalog → 不符）；函数体内对象须 schema-qualified
    accessed_resources: [industrial.project_planning.table.organizations]
    privilege_scope: "仅读取租户枚举列"
    security_test: "tests/...::test_secdef_no_cross_tenant"
    public_execute: false                # 目标；当前实测为 true（默认 ACL，E10.3）→ 必须 REVOKE
```

**RLS 表达式 AST（唯一可执行语法，替代 `current_tenant` / `same_as_using` 之类未定义字符串）**

复审指出：`using: "tenant_id = current_tenant AND ..."` 与 `with_check: "same_as_using"` 不是可执行定义——
`current_tenant` 未映射到任何 GUC、`same_as_using` 只是字符串约定，生成器与测试都无法据此派生。**唯一裁定：
Registry 只接受下列受限 AST，不接受自由文本。**

| 节点 | 形状 | 语义 | 派生 SQL |
| --- | --- | --- | --- |
| `all` | `{ all: [node, ...] }` | 逻辑 AND | `(n1 AND n2 ...)` |
| `any` | `{ any: [node, ...] }` | 逻辑 OR | `(n1 OR n2 ...)` |
| `not` | `{ not: node }` | 逻辑 NOT | `NOT (n)` |
| `eq` | `{ eq: { column: <col>, value: <src> } }` | 等值 | `col = <src_sql>` |
| `in` | `{ in: { column: <col>, value: <src> } }` | 数组包含 | `col = ANY (<src_sql>)` |
| `is_null` | `{ is_null: { column: <col> } }` | 判空 | `col IS NULL` |
| 值源 `guc` | `{ guc: <guc_name> }` | 单值上下文 | `current_setting('<guc>', true)::<col_type>` |
| 值源 `guc_array` | `{ guc_array: <guc_name>, element_type: text }` | 数组上下文（**唯一解析通道**，§4.2） | `current_setting('<guc>', true)::<element_type>[]` |
| 值源 `literal` | `{ literal: <value> }` | 常量（仅允许在例外策略中使用） | 字面量转义 |

**类型与 NULL 规则（必须由同一 helper 实现，不允许各处手写 cast）**：

- `column` 必须存在于该资源，且必须在 `scope_dimensions` 内；**引用不存在的列 → Registry 校验直接报红**
  （这正是 v1.4 用统一三谓词会踩的坑：`asset_versions` 无 `organization_id`、`outbox_events` 无
  `project_id`、`status_transitions` 无 `project_id`）。
- `eq`/`in` 的 `column` 类型与值源元素类型不一致时，**必须在 AST 里显式声明 `cast`**；
  但 `asset_versions.project_id`（`varchar`）与 `app.current_projects`（`text[]`）**同域，声明 `cast: none`**，
  比较直接为 `project_id = ANY (...::text[])`。**禁止对列写 `::uuid`**——E11.2 实测该写法在 684 行上直接 22P02。
- `guc_array` **不得**再接受 `cast` 参数（v1.4.1 的 `cast: text` 会诱导生成 `::text[]` 之外的切分实现）；
  元素类型只由 `element_type` 声明，解析只走原生数组输入语法。
- **缺失上下文 fail-closed**：`current_setting(name, true)` 在 GUC 未设置时返回 NULL；`eq`/`in` 求值为
  NULL → 行被过滤（0 行），`WITH CHECK` 为 NULL → 写入被拒。**不得**为「未设置」补默认值。
- `with_check` 必须是独立 AST，**不得**使用 `same_as_using` 之类别名；若语义确需相同，用 YAML 锚点引用
  同一 AST 节点（单一定义、两处使用），而不是另写一份或写别名。

**每条资源必须在 Registry 声明 `scope_dimensions`**（本表实际具备的隔离维度），并且 Policy AST 只能引用
其中列。这消除「三上下文必须逐表齐备」的错误假设：三 GUC 是**授权输入**，不是每张表都必须有对应列。

| 资源 | 实际列（E10.11） | scope_dimensions | 说明 |
| --- | --- | --- | --- |
| `asset_service.asset_versions` | `tenant_id uuid`, `project_id varchar`（无 org） | tenant + project | 项目级隔离；org 经 `projects` 间接成立 |
| `outbox.outbox_events` | `tenant_id uuid`（无 org/project） | tenant only | 事件表按租户隔离；域隔离靠角色 ACL，不靠列谓词 |
| `audit.status_transitions` | `tenant_id uuid`, `organization_id uuid`（无 project） | tenant + org | 审计事实按租户+组织 |
| `project_planning.projects` | `tenant_id uuid`, `organization_id uuid` | tenant + org | 组织归属主体 |

#### D.2.1 域→角色映射的单一事实源裁定【v1.4.2 新增，消除重复定义】

v1.4.1 在**两处**同时定义同一个事实：`domains[].runtime_role` 与 `roles.mappings[].runtime_login_role`
（且 `mappings` 只列了 `project_planning` 一项示例，却被描述为完整映射）。这违反 §11「一个事实一个权威源」，
且**不能用「CI 检查两者一致」来掩盖**——那只是把矛盾推迟到 CI。

**唯一裁定：保留 `domains[].runtime_role`，删除 `roles.mappings`。** 理由：

1. 域身份与「该域以哪个数据库角色运行」是**同一个概念的两面**，分开存放必然双写；
2. `roles` 段此后的职责单一化：只描述**基础设施角色**（`afs_owner`/`afs_migrator`/`ops_admin`）的属性与成员关系选项；
3. 域的数量是固定的 8 个（E10.0），`domains[]` 天然穷举，不需要第二处"示例式"映射。

**校验规则（生成器与 CI 必须实现）**：

| 规则 | 判定 |
| --- | --- |
| `domains[]` 必须**恰好 8 项**，`id` 唯一 | 少一项或多一项即报红（不得只留示例） |
| `set(domains[].runtime_role)` 必须等于集群内 `pg_roles` 的 `svc_*` 集合 | 差集非空即报红（双向差集，不只查"缺失"） |
| `roles` 段**不得**再出现 `mappings` 键 | 出现即报红（防止有人加回第二事实源） |
| 每个 `resources[].domain` 必须能在 `domains[].id` 中找到 | 悬空即报红 |

#### D.2.2 隔离范围的业务裁定：outbox 与 audit 为什么不是项目级【v1.4.2 新增】

第五轮复审指出：Registry 把 `outbox_events` 设为 tenant-only、`status_transitions` 设为 tenant+org，
却仍在总体表述里承诺「项目级数据隔离」。这不是字段遗漏，**必须给出业务裁定**，否则承诺与实现不一致。

**先看事实（E11.4/E10.11）**：

| 资源 | 实际列 | 能否做项目级谓词 | 关键事实 |
| --- | --- | --- | --- |
| `outbox.outbox_events` | `event_id`, `aggregate_type`, `aggregate_id`, `tenant_id`, `event_type`, **`payload jsonb`**, `occurred_at`, … | **不能**（无 `project_id` 列） | `payload` 是 `jsonb`，**结构上可以装任何内容**，包括项目敏感内容 |
| `audit.status_transitions` | `transition_id`, `tenant_id`, `organization_id`, `resource_type`, `resource_id`, `from_status`, `to_status`, `actor_subject`, `authority_ref`, `evidence_refs`, … | **不能**（无 `project_id` 列） | 只有状态迁移元数据，无业务正文 |

**裁定 1（outbox）**：`outbox_events` 是**传输层表，不是业务数据表**；其 `payload` **禁止承载项目级业务内容**。

- 允许的 `payload` 键为封闭白名单：`resource_type` / `resource_id` / `from_status` / `to_status` /
  `entity_version` / `occurred_at`；**禁止**自由文本、提示词、剧本正文、资产二进制引用、评审意见等内容型字段。
- 该约束落在**事件契约校验**（写入口），不是 RLS；RLS 只负责租户边界。
- 消费者**不得**从 `payload` 取业务内容，必须凭 `aggregate_id` 回源到业务域 API，由该域的 RLS 再判一次。
- **因此 tenant-only 是充分的**：表内不存在项目内容，跨项目可见的只有「同租户内某实体发生了某类状态事件」这一元信息。

**裁定 2（audit）**：`status_transitions` 是**合规事实台账**，其可见范围**有意**为「同租户 + 同组织」，
即允许同组织内跨项目审阅。

- 理由：审计的目的是治理与追溯，若按项目切分，组织级审计（如「本季度所有项目的 Gate 4 通过率」）将无法完成；
  且该表**只有迁移元数据**（状态、动作、主体、授权引用），不含项目业务内容。
- 访问主体收敛为「迁移相关域」：`svc_project_planning`、`svc_asset`（SI）+ 治理域只读（IP-4 校准），
  不向全租户开放。
- **残余风险（明示）**：同组织内的业务角色可以看到其他项目实体的 `resource_id` 与状态迁移轨迹——
  这是**有意设计**，不是漏配；如未来要求项目级审计隔离，必须新增 `project_id` 列并另立 ADR，不得在此偷改谓词。

**裁定 3（承诺口径）**：「项目级隔离」的覆盖面**只包括**声明了 `project_id` 维度且 Policy 引用该维度的资源
（当前为 `asset_service.asset_versions`）。`outbox_events`（tenant-only）与 `status_transitions`（tenant+org）
**不得**被描述为「项目级隔离已成立」；任何对外表述必须写成「租户级 + 域 ACL」或「租户 + 组织级」。

| 资源 | 隔离级别（准确表述） | 是否覆盖「项目级隔离」承诺 |
| --- | --- | --- |
| `asset_service.asset_versions` | 项目级（tenant + project） | **是** |
| `outbox.outbox_events` | 租户级 + 域 ACL（payload 受契约白名单约束） | 否（传输层，不含项目内容） |
| `audit.status_transitions` | 租户 + 组织级（有意跨项目） | 否（合规台帐，元数据） |
| `project_planning.projects` | 租户 + 组织级 | 否（归属主体） |

> 本裁定**不新增权限层**：仍用既有 RLS + 域 ACL + 事件契约，只是把「哪些资源属于哪种隔离级别」写死，
> 并禁止用 tenant-only 资源去佐证项目级隔离已全面成立。

### D.3 资源唯一标识规则【v1.3 建立；v1.4 修复格式矛盾】

【v1.4 · P2】第四轮复审指出 v1.3 的 D.3 格式 `<database>.<schema>.<object_type>.<object_name>`
与 D.2 示例 `industrial.asset_service.asset_versions`（缺 object_type）直接矛盾。本轮统一：

- **唯一格式**：`<database_id>.<schema>.<object_type>.<object_name>`，四段全部必填，
  示例：`industrial.asset_service.table.asset_versions`、`verify.outbox.table.outbox_events`、
  `industrial.outbox.function.list_active_tenants`。所有示例（含 D.2、D.4、D.5）一律按此格式。
- **object_type 枚举**：`table` / `partition` / `sequence` / `function` / `view` / `matview` / `policy` / `schema`。
- **数据库逻辑 ID ⇄ 实际库名映射**：`industrial → ai_film_industrial`、`verify → ai_film_verify`（D.2 `databases`）；
  Registry 用逻辑 ID 作资源标识前缀，派生器/CI 据此映射到真实 PostgreSQL 库名，避免标识无法对应真实对象。
- `postgres` 系统库不作为资源命名空间（§6.1）。资源标识在 Registry 内全局唯一；
  同一 object 在不同业务库算不同资源（因两库结构已分叉，附录 C）。

【v1.4.1 补三条此前缺失的规则】

1. **函数必须带参数类型签名**（否则无法区分重载）：`industrial.outbox.function.list_active_tenants()`、
   `industrial.audit.function.assert_transition_event_exists()`。无参函数也必须写 `()`；
   `object_name` = `name(arg_type, ...)`，与 `pg_get_function_identity_arguments()` 输出一致。
2. **唯一性由生成器校验，不靠人工**：Registry 加载时对 `resources[].id` 做集合去重；同时校验
   「每个 id 的 `<database>.<schema>.<object_type>` 与对应字段一致」「`object_name` 与 D.2 的
   `object_name` 一致」。任一不符即 CI 报红（防止 v1.4 那种「规则改了、示例没改」的残留）。
3. **重命名/删除的历史处理**：资源 id 一旦发布即为**稳定标识**，不得随物理对象改名而静默改写
   （否则历史迁移的授权记录会指向不存在的目标）。改名走「新增 id + 旧 id 标记 `retired_at` +
   一条前向迁移」；删除同理，先 `retired_at` 再在前向迁移中 `REVOKE` 并移除。
   `retired_at` 非空的 id 不得出现在 `permissions` 中。

> v1.4 的残留示例 `project_planning.table.organizations`（缺数据库逻辑 ID）与
> `industrial.asset_service.asset_versions`（缺 object_type）均已按上述格式修正；D.2/D.4/D.5
> 全部示例已交叉检查。

### D.4 映射关系与操作权限枚举【v1.3 建立；v1.4 补 TRUNCATE/REFERENCES/TRIGGER】

| 映射 | 内容 | 来源 |
| --- | --- | --- |
| 逻辑库 ID → 物理库名 | `industrial → ai_film_industrial`、`verify → ai_film_verify` | D.3 / D.2 databases |
| 域 → 角色 | `domain → svc_<domain>`（LOGIN）；基础设施四角色见 D.2 | §5.1 |
| 角色 → 资源 | 每角色可访问的资源集合（`resources[].domain` + `permissions[].role`） | D.2 |
| 资源 → 操作 | 每资源对每角色允许的操作（**完整枚举见下**） | D.2 permissions |
| 资源 → RLS | 每资源 `rls.enabled`/`rls.force`/`rls.policies[]`（结构化语义，非语义名称） | D.6 / §5.3 |
| 例外 → 对象 | 无法派生者进入 `exceptions`，字段见 D.5 | D.5 |

**操作权限完整枚举**（v1.4 按指令 5.2 补齐 TRUNCATE/CREATE/CONNECT/TEMPORARY 并区分对象类型）：

| 操作 | 默认 | 规则 |
| --- | --- | --- |
| `SELECT`/`INSERT`/`UPDATE`/`DELETE` | 按 `permissions` 显式声明 | 最小权限；DELETE/UPDATE 受 append-only 约束 |
| **`TRUNCATE`** | **一律默认禁止** | **RLS 不拦截 TRUNCATE**（PG 语义），不能把「表有 RLS」当全操作保护；仅显式例外可开，须 ACL 实际核对；所有 `svc_*` 默认无 TRUNCATE（当前取证 E5 已符合） |
| **`REFERENCES`** | 按外键需要显式声明 | 限制可 REFERENCES 的对象范围 |
| **`TRIGGER`** | 默认禁止 | 仅显式例外（如维护角色）可管理触发器；审计事实表触发器归属须登记 |
| `EXECUTE` | 按函数显式声明 | 重点审计 `SECURITY DEFINER`（D.5） |
| `USAGE` | schema/sequence 按域声明 | — |
| **`CREATE`** | **默认禁止** | 仅对象所属 schema 的管理身份可持有；任何 `svc_*` 默认无 CREATE（防运行时建表逃逸授权）；仅显式例外可开并审计 |
| **`CONNECT`** | 仅 LOGIN 角色按域声明 | 数据库连接权；NOLOGIN/基础设施角色不适用；默认不授予 PUBLIC（当前取证待 IP-6 核对） |
| **`TEMPORARY`** | **默认禁止** | 防运行时创建临时对象绕过域边界；`svc_*` 默认无 TEMPORARY，仅显式例外可开 |

**按对象类型的合法权限子集**（指令 5.2：不同对象类型使用不同枚举，不得把所有权限平铺到所有对象；
Registry 的 `permissions[].operations` 必须落在其资源 `object_type` 的合法子集内，否则 CI 报红）：

| object_type | 合法操作子集 | 默认拒绝项 |
| --- | --- | --- |
| `table` / `partition` | SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER | TRUNCATE / TRIGGER 默认禁；append-only 对象再禁 UPDATE/DELETE |
| `sequence` | USAGE, SELECT, UPDATE | 除自增列所需外默认禁 UPDATE |
| `function` | EXECUTE | 默认不授予 PUBLIC；SECDEF 按 D.5 例外登记 |
| `schema` | USAGE, CREATE | CREATE 默认禁 |
| `view` / `matview` | SELECT（及底层对象所需 USAGE） | 所有 DML 默认禁 |
| `policy` | 不作为独立授权对象（由 `resources[].rls` 派生，见 D.6） | —  |
| `database` | CONNECT, CREATE, TEMPORARY | CREATE / TEMPORARY 默认禁；CONNECT 按 LOGIN 域声明 |

> 全局默认拒绝：`permissions` 未显式声明的操作一律视为拒绝（fail-closed）；CI 按 `object_type` 校验
> 每项 `operations` 的合法性，并叠加真实 PG ACL 检查（D.6），不依赖单一来源。

### D.5 显式例外登记字段【v1.3 建立；v1.4 补 SECURITY DEFINER 六字段】

每条例外必须包含通用字段：`object`（D.3 唯一标识）、`reason`、`owner`（具名责任人）、
`unblocks_when`、`expires_at`（复核期限）。缺字段或解除后未删除的陈旧例外，CI 报红
（沿用 `contracts/security_boundary.yaml` 债务机制）。

【v1.4.1】`kind: SECURITY_DEFINER` 的例外**额外必填字段**（在 v1.4 六字段基础上补三处，按复审要求）：

| 字段 | 用途 | 约束 |
| --- | --- | --- |
| `execute_roles` | 哪些角色允许 EXECUTE | 不得含未授权角色；`svc_*` 逐个显式列 |
| `effective_owner` | 函数**目标**执行身份（`SECURITY DEFINER` 的 definer） | **必须等于 `afs_owner`**（与 INV-1 一致） |
| `observed_owner` | 只读取证的当前值 | 仅作差距记录，**不得**当作目标期望值（当前全部为 `postgres`） |
| `search_path` | 固定安全搜索路径 | **目标 = `pg_catalog, pg_temp`**（v1.4.2）：`pg_catalog` 在首位；`pg_temp` **显式列在最后**；中间不得出现任何用户可写 schema；函数体内**所有对象引用必须 schema-qualified** |
| `accessed_resources` | 函数可访问对象清单 | 用 D.3 四段标识，逐项列出 |
| `privilege_scope` | 执行后实际获得的权限范围 | 自然语言，须与 `accessed_resources` 一致 |
| `security_test` | 负向/越权测试节点 | 阶段 6 真实 PG 执行 |
| `public_execute` | PUBLIC 是否拥有 EXECUTE | **期望 false**；CI 展开 `proacl` 自动核对 |

**目标 OWNER 与当前观测 OWNER 必须分开（v1.4 的错误在此）**：v1.4 把 `effective_owner: postgres` 直接写进
Registry，与 INV-1「所有业务函数 OWNER = `afs_owner`」直接冲突；且 `postgres` 是超级用户，
**即使开启 FORCE RLS 也不受 RLS 约束**，等于把「受 RLS 约束」的安全承诺作废。唯一裁定：

- 业务函数（含两个 SECDEF 函数）目标 OWNER = `afs_owner`，且 `afs_owner` 满足 INV-9
  （NOSUPERUSER/NOBYPASSRLS），使 SECDEF 函数执行时**仍受 RLS/FORCE RLS 约束**；
- 若某函数确因技术原因必须保留超级用户 OWNER，则**必须单独登记为例外并明示「不受 RLS 约束」**，
  不得与上述模型混写，也不得宣称它受 RLS 保护；
- `effective_owner`（目标）与 `observed_owner`（现状）并存，CI 只拿目标做断言，拿观测做差距报告。

**两个 SECDEF 函数的完整签名与实测（E10.3，只读取证）**：

| 函数（完整签名） | 实测 OWNER | SECDEF | 实测 `search_path` | 实测 PUBLIC EXECUTE | **目标配置（v1.4.2）** |
| --- | --- | --- | --- | --- | --- |
| `outbox.list_active_tenants()`（无参数） | `postgres` | 是 | **`outbox, pg_catalog`**（用户可写 schema 在前 → **劫持风险**） | 否（ACL 已限定 `app_role`/`outbox_relay_role`/`postgres`） | OWNER=`afs_owner`；`search_path=pg_catalog, pg_temp`；访问对象改写为 `outbox.outbox_events`（schema-qualified）；EXECUTE 仅 `svc_identity`；**先 REVOKE PUBLIC** |
| `audit.assert_transition_event_exists()`（无参数） | `postgres` | 是 | `pg_catalog, outbox`（`pg_catalog` 在前，但缺 `pg_temp` 显式末位） | **是**（`grantee=OID 0`，默认 ACL） | OWNER=`afs_owner`；`search_path=pg_catalog, pg_temp`；访问对象 `outbox.outbox_events`；EXECUTE 仅 `svc_asset`/`svc_project_planning`；**先 REVOKE PUBLIC** |

**【v1.4.2 裁定】`search_path` 目标不能只写 `pg_catalog`——必须显式处理 `pg_temp`**

第五轮复审指出：v1.4.1 把目标写成 `pg_catalog`，仍未处理 PostgreSQL 对**临时 schema 的隐式优先搜索**。
按 PG16 的 schema 搜索规则：`pg_catalog` 总是被搜索（若不在路径中则**先于**路径各项搜索）；
而会话的临时 schema `pg_temp_N` 若存在，**对关系名与类型名的解析总是被搜索，未在路径中列出时排在最先（甚至早于
`pg_catalog`）**。也就是说：

- 只写 `pg_catalog` ≠ 安全：调用者建一个同名临时表，就可能劫持函数内的**未限定**关系名；
- 把 `pg_temp` **显式列在最后**后，临时 schema 只在路径末尾被搜索，不再抢先。

**四项必须同时成立（缺一即视为不安全配置）**：

| 项 | 规则 |
| --- | --- |
| 搜索路径顺序 | `search_path = 'pg_catalog, pg_temp'`——`pg_catalog` 首位、`pg_temp` 显式末位、中间**不得**出现任何用户可写 schema |
| 对象限定 | 函数体内**所有**关系/函数/类型引用必须 **schema-qualified**（如 `outbox.outbox_events`），不得依赖路径解析；路径只作为运算符/内建函数的兜底 |
| OWNER | 目标 **`afs_owner`**（且满足 INV-9：`NOSUPERUSER`/`NOBYPASSRLS`），使 SECDEF 函数执行时**仍受 FORCE RLS 约束**。**不得**把当前观测的 `postgres` 当作目标值——超级用户即使 FORCE RLS 也不受限，等于作废安全承诺 |
| EXECUTE 范围 | **先** `REVOKE ALL ON FUNCTION ... FROM PUBLIC`，**再**按 `execute_roles` 显式 `GRANT`（D.6.0c 派生 1c） |

> 「目标配置」与「当前观测配置」必须分列（上表已分列）。`observed_owner=postgres`、
> 观测 `search_path=outbox, pg_catalog` 都只是差距记录，**不得**写进 `effective_owner` / `search_path` 字段。

**`public_execute` 的落地动作（不只是检测）**：Registry 写 `public_execute: false` 与 CI 检查都只是**检测**；
真正生效的是**迁移里先 `REVOKE`**。因此生成器对每个新建/重建的业务函数固定产出
`REVOKE ALL ON FUNCTION <sig> FROM PUBLIC;` + 按 `execute_roles` 的 `GRANT EXECUTE`（D.6.0c 派生 1c）。
E10.3 实测 6 个函数中 **5 个**带默认 `PUBLIC EXECUTE`（`asset_versions_guard`、`qc_reports_guard`、
`assert_transition_event_exists`、`reject_mutation`、`reject_truncate`）——若迁移不先 REVOKE，
这些函数在重建后仍会对 PUBLIC 开放。

**`public_execute` 取证口径**：`pg_proc.proacl` 为 NULL 时表示「默认 EXECUTE TO PUBLIC」，
必须用 `acldefault('f'::"char", proowner)` 展开才看得见。E10.3 显示：`list_active_tenants` 有显式 ACL
（3 项，无 PUBLIC），**另外 5 个函数全部带 PUBLIC EXECUTE**（`asset_versions_guard`、`qc_reports_guard`、
`assert_transition_event_exists`、`reject_mutation`、`reject_truncate`）。这说明「函数默认对 PUBLIC 开放」
是当前的真实风险面，不能只看「是否 SECDEF」。

### D.6 派生、校验与漂移检测（v1.3 新增，P2 必答 6/7/8）【v1.4 裁定方案 + 版本固定】

#### D.6.0 RLS 事实源方案二选一裁定（指令 5.3，唯一方案）

指令 5.3 要求在两种方案中选定唯一一种。经「更少重复事实、更易验证」原则评估，**唯一裁定采用方案 B**，
并吸收方案 A 的派生做法（B 是 A 的超集）：

- **方案 A**（Registry 保存完整结构化 RLS 定义、由生成器派生 SQL）：仅解决「Registry→SQL」单向派生，
  但未要求对真实落地 Policy 做核对，也未要求行为测试由同一源生成，仍可能残留「SQL 被手工改动」的分叉。
- **方案 B（选定）**：Registry 保存完整可验证 RLS 语义；SQL Policy 由受控生成器从该语义派生；
  **CI 必须对真实 `pg_policies` 的实际定义逐列核对**（逐列对照证据存档 E4 的 `roles`/`cmd`/
  `permissive`/`qual`/`with_check`）；**E1 行为测试由同一 `rls.policies[]` 结构化规则自动生成**。
  B 同时闭合「声明→SQL→实际落地→行为验证」四处，重复事实最少、验证最完整。
- **不得出现**三套事实并存：Registry 声明一种语义、SQL 写另一种语义、测试再手工维护第三种预期行为。
  落地时 `rls.policies[]` 是唯一权威；生成产物（SQL、行为测试）均为其确定性投影，不反向成为事实源。
- 该裁定落实 §11 单一事实源，且复用 `scripts/boundary_assertions.py` 与 `contracts/security_boundary.yaml`
  既有契约机制，不新增权限系统（见本节末尾「不建新系统」）。

#### D.6.0b Registry 与历史迁移的版本固定关系（指令 5.5）

关键矛盾：若历史迁移在未来重新读取「当前 Registry」并重生成授权，会改变已执行历史语义。唯一裁定：

- **历史迁移不可变**：已生成并入库的迁移文件（含其中的 GRANT/Policy DDL）是**冻结产物**，
  不得因 Registry 后续变更而重生成或改写；其权威来源是生成当时的 Registry 快照。
- **生成器版本固定**：Registry 顶部 `schema_version`（当前 `"1.1"`）+ 生成器的代码版本/提交号，
  连同生成时的 Registry 内容哈希，一并写入每个迁移文件的头部注释（如
  `generated-by: registry@<commit>#<sha256>; schema_version: 1.1`）。历史可追溯、可复现。
- **新迁移用「当时快照」生成**：新前向迁移只读取被冻结的那一版 Registry 快照（或经评审的当前版本，
  并把版本号固化进迁移头），不读取「运行时的未来版本」；Registry 演进本身须产出新的前向迁移。
- **CI 一致性检查（两层）**：① 现有历史迁移的头部版本标注与实际 DDL 冻结一致（防止被偷偷重生成）；
  ② 当前 Registry 的最新派生期望与「已应用迁移的累积效果」做双向 diff（D.6 漂移检测），差异即报漂移。
- **不因为 Registry 是事实源就让历史迁移可变**：事实源约束的是「新变更从哪里来」，不是「历史被重写」。

#### D.6.0c AST 派生规则：SQL 与行为测试必须同源【v1.4.1 新增；v1.4.2 修正解析与模板】

复审指出「方案 B 已选定」不等于「方案 B 可实施」：只要表达式还是自由文本，SQL 与测试就必然各自维护一套语义。
因此本节定义**确定性派生**——同一个 AST 节点同时产出 SQL 片段与测试断言，二者不得分别手写。

**派生 0（先决条件）：GUC 逻辑类型必须服从被比较列的真实类型**

`app.current_projects` 的逻辑类型是 **`text[]`，不是 `uuid[]`**（§4.2 v1.4.2 裁定）。依据 E11.2：

| 实测项（`ai_film_industrial`） | 值 |
| --- | --- |
| `asset_service.asset_versions.project_id` | `character varying(64)` |
| 971 行中 UUID 形态行数 | **0** |
| 实际取值样例 | `project-1`（684 行）、`2140479b0e494e6baf17ea8f4a938224`（32 位无连字符 hex） |
| `'{project-1}'::uuid[]` | **ERROR 22P02 `invalid input syntax for type uuid`** |

若生成器按 `uuid[]` 派生，产物要么在注入上下文时 22P02，要么（强行转 text 后）永远匹配不到任何行。
**派生 0 的规则**：`guc_array` 的 `element_type` 必须等于「被比较列的底层类型族」——`project_id` 为 `varchar` ⇒
`element_type: text`；租户/组织 GUC 的比较列是 `uuid` ⇒ `guc`（单值）用 `::uuid`，**不是** `guc_array`。

**派生 1：AST → SQL（生成器纯函数，不读运行时环境）**

| AST | 源 | 派生 SQL（v1.4.2） |
| --- | --- | --- |
| `asset_versions` 的 `*scope_asset` | D.2 | `(tenant_id = current_setting('app.current_tenant_id', true)::uuid AND project_id = ANY (current_setting('app.current_projects', true)::text[]))` |
| `status_transitions` 的 scope | 同规则 | `(tenant_id = current_setting('app.current_tenant_id', true)::uuid AND organization_id = current_setting('app.current_organization_id', true)::uuid)` |
| `outbox_events` 的 scope | 同规则 | `(tenant_id = current_setting('app.current_tenant_id', true)::uuid)` |

派生规则（逐条可被 CI 断言）：

1. **数组只有一个解析通道**：`current_setting('<guc>', true)::<element_type>[]`——直接使用 PostgreSQL **原生数组
   输入语法**解析 §4.2 定义的物理文本（`{project-1,project-2}`）。**禁止** `string_to_array(...)`、`regexp_split_to_array`、
   `unnest`+`split_part` 等任何手工切分；**禁止**在 Python/生成器/SQL 各自实现一套解析（§4.2 唯一通道）。
2. **元素编码使原生解析无歧义**：编码器只放行 `^[A-Za-z0-9_-]{1,64}$`，该字符集不含 `,` `{` `}` `"` `\`，
   因此数组文本**永不需要引号转义**（E11.3：仅含 `,` 的元素才需要引号，而该字符集排除逗号）。
3. **类型对齐由 AST 声明、生成器不得推断**：`cast: none`（同域）或显式 `cast`（异域），二选一必须写死；
   缺失即校验报红。**禁止**生成对列的 `::uuid`（会在 684 行上直接 22P02，E11.2 变体 C）。
4. 比较形态固定 `col = ANY (<array_sql>)`；**不得**展开成 `col IN ('a','b',...)` 拼接（退化为字符串拼接）。
5. 缺失 / NULL / 畸形数组一律 fail-closed：`current_setting(...,true)` 返回 NULL ⇒ `= ANY(NULL)` 为 NULL ⇒ 0 行；
   畸形文本 ⇒ `22P02` 直接报错（**不得**吞掉转成空集合）；`'{}'` ⇒ `false` ⇒ 0 行（E11.3 实测）。

**派生 1b：AST → Policy SQL 模板（按命令派生，不适用子句完全省略）**

| `commands` | 生成的子句 | 禁止出现 |
| --- | --- | --- |
| `SELECT` | `USING (<using_sql>)` | `WITH CHECK`（PG16 语法不接受） |
| `INSERT` | `WITH CHECK (<with_check_sql>)` | `USING`（PG16 语法不接受） |
| `UPDATE` | `USING (<using_sql>) WITH CHECK (<with_check_sql>)` | —（两者必须分别来自各自 AST） |
| `DELETE` | `USING (<using_sql>)` | `WITH CHECK` |

生成器规则：

1. 子句集合**只由 `commands` 决定**，与 `using`/`with_check` 是否为空无关；若 Registry 给某命令写了不适用的键
   （如 `SELECT` 写 `with_check`）⇒ **校验报红**，不得静默忽略（否则"写了却不生效"）。
2. 必需键缺失（`SELECT`/`DELETE` 无 `using`、`INSERT` 无 `with_check`、`UPDATE` 缺任一）⇒ **校验报红**，
   **不得**填默认表达式或复制另一个 AST。
3. `DELETE` 策略仅在资源 `permissions` 含 `DELETE` 且 Registry 显式声明时生成；append-only 资源
   （`asset_versions` = SIU、`qc_reports` = SI）**不生成 DELETE 策略**——无 PERMISSIVE ⇒ 任何 DELETE 影响 0 行（C.3.2）。
4. 命名严格 `pol_<table>_<cmd>_<kind>`，`<kind>` ∈ {`scope`, `guard`}（C.3.2 唯一命名规则）；
   同一表同一命令至多一条 `scope` + 一条 `guard`，名称天然唯一。
5. 分区表：**父表与每个分区各生成一套 Policy**——PG 对「直接访问分区」只应用分区自身 Policy，
   父表 Policy 不下推（E10.7/E11.4：`outbox_events` 为 `relkind='p'`，含 `_default`/`_y202611`/`_y202612` 三个子分区）。

**完整 SQL 输出样例（可执行形态，非伪代码）**

`asset_service.asset_versions`（Registry：`commands: [SELECT] / [INSERT] / [UPDATE]`，无 DELETE）：

```sql
-- SELECT：只有 USING；绝不出现 WITH CHECK
CREATE POLICY pol_asset_versions_select_scope ON asset_service.asset_versions
  AS PERMISSIVE FOR SELECT TO svc_asset
  USING ((tenant_id = current_setting('app.current_tenant_id', true)::uuid
          AND project_id = ANY (current_setting('app.current_projects', true)::text[])));

-- INSERT：只有 WITH CHECK；绝不出现 USING
CREATE POLICY pol_asset_versions_insert_scope ON asset_service.asset_versions
  AS PERMISSIVE FOR INSERT TO svc_asset
  WITH CHECK ((tenant_id = current_setting('app.current_tenant_id', true)::uuid
               AND project_id = ANY (current_setting('app.current_projects', true)::text[])));

-- UPDATE：USING 与 WITH CHECK 都必须存在，且分别来自各自 AST 节点
CREATE POLICY pol_asset_versions_update_scope ON asset_service.asset_versions
  AS PERMISSIVE FOR UPDATE TO svc_asset
  USING ((tenant_id = current_setting('app.current_tenant_id', true)::uuid
          AND project_id = ANY (current_setting('app.current_projects', true)::text[])))
  WITH CHECK ((tenant_id = current_setting('app.current_tenant_id', true)::uuid
               AND project_id = ANY (current_setting('app.current_projects', true)::text[])));

-- DELETE：不生成（append-only）。无 PERMISSIVE DELETE 策略 ⇒ DELETE 影响 0 行。
```

`outbox.outbox_events`（分区父表 + 3 个子分区，各一套；此处只列 SELECT 与 INSERT）：

```sql
-- 父表
CREATE POLICY pol_outbox_events_select_scope ON outbox.outbox_events
  AS PERMISSIVE FOR SELECT TO svc_project_planning, svc_asset, svc_outbox_relay
  USING ((tenant_id = current_setting('app.current_tenant_id', true)::uuid));
CREATE POLICY pol_outbox_events_insert_scope ON outbox.outbox_events
  AS PERMISSIVE FOR INSERT TO svc_project_planning, svc_asset
  WITH CHECK ((tenant_id = current_setting('app.current_tenant_id', true)::uuid));

-- 三个子分区必须各自重复（分区不继承父表 Policy）；仅对象名不同
CREATE POLICY pol_outbox_events_select_scope ON outbox.outbox_events_default  AS PERMISSIVE FOR SELECT TO svc_project_planning, svc_asset, svc_outbox_relay
  USING ((tenant_id = current_setting('app.current_tenant_id', true)::uuid));
CREATE POLICY pol_outbox_events_insert_scope ON outbox.outbox_events_default  AS PERMISSIVE FOR INSERT TO svc_project_planning, svc_asset
  WITH CHECK ((tenant_id = current_setting('app.current_tenant_id', true)::uuid));
CREATE POLICY pol_outbox_events_select_scope ON outbox.outbox_events_y202611 AS PERMISSIVE FOR SELECT TO svc_project_planning, svc_asset, svc_outbox_relay
  USING ((tenant_id = current_setting('app.current_tenant_id', true)::uuid));
CREATE POLICY pol_outbox_events_insert_scope ON outbox.outbox_events_y202611 AS PERMISSIVE FOR INSERT TO svc_project_planning, svc_asset
  WITH CHECK ((tenant_id = current_setting('app.current_tenant_id', true)::uuid));
CREATE POLICY pol_outbox_events_select_scope ON outbox.outbox_events_y202612 AS PERMISSIVE FOR SELECT TO svc_project_planning, svc_asset, svc_outbox_relay
  USING ((tenant_id = current_setting('app.current_tenant_id', true)::uuid));
CREATE POLICY pol_outbox_events_insert_scope ON outbox.outbox_events_y202612 AS PERMISSIVE FOR INSERT TO svc_project_planning, svc_asset
  WITH CHECK ((tenant_id = current_setting('app.current_tenant_id', true)::uuid));
```

> 上述 SQL 的关键点在于**可执行性**：`SELECT` 若带 `WITH CHECK`、`INSERT` 若带 `USING`，PG16 会直接语法报错，
> 不会"降级为无害"。因此这不是风格问题，而是生成器必须按 `commands` 分支的硬约束。
> 表达式部分与 E11.1 变体 B 完全一致（`...::text[]`），已在真实表上以只读方式验证：同一上下文下
> v1.4.1 产物返回 **0 行**、本式返回 **684 行**（E11.1）。

**派生 1c：函数 EXECUTE 授权模板（REVOKE 必须先于 GRANT 发生）**

```sql
-- 生成器对「每个新建或重建的业务函数」固定产出两段，顺序不可交换
REVOKE ALL ON FUNCTION outbox.list_active_tenants() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION outbox.list_active_tenants() TO svc_identity;
```

- 原因：`CREATE FUNCTION` 对 `PUBLIC` 默认授予 `EXECUTE`（E10.3 实测 6 个函数中 5 个如此）。
  只在 Registry 写 `public_execute: false`、只在 CI 事后检查，都是**检测**而非**落地动作**；
  必须由**迁移本身**先 `REVOKE`，再按 Registry 显式 `GRANT`。
- 禁止另建「函数默认权限」第二套事实源：**不启用** `ALTER DEFAULT PRIVILEGES ... REVOKE EXECUTE ON FUNCTIONS
  FROM PUBLIC` 作为主手段（该决策沿用 v1.4），因为它会同时影响未来所有函数并掩盖单个函数的授权意图；
  每个函数的 EXECUTE 角色必须在 Registry 逐条登记（D.5 `execute_roles`）。
- CI 仍要展开 `proacl`（`acldefault('f'::"char", proowner)`）断言 `PUBLIC` 不在 grantee 集合中——**检测兜底，不替代迁移动作**。

**派生 2a：RLS 行为测试（主体必须对该资源有 SELECT）**

| 项 | 规定 |
| --- | --- |
| 测试主体 | 对该资源 `permissions` 含 `SELECT` 的业务角色（`asset_versions` → `svc_asset`）；**不得**使用零权限角色 |
| 前置断言 | `has_table_privilege('<主体>', '<表>', 'SELECT') = true` —— 否则测试无意义（见派生 2b） |
| 上下文来源 | 可信 Principal 调用链（§4.1）构造的**另一个真实、合法**的项目范围；**不得**绕过 Principal 直接 `set_config` |
| 判定 | 得到 `42501 permission denied` 即**测试失败**（说明主体选错，落到了 ACL 层而不是 RLS 层） |

| 用例（由 AST 自动生成） | 构造 | 期望（由 AST 推导） | 观察量 |
| --- | --- | --- | --- |
| in-scope SELECT | 上下文含目标项目 | 行数 = 已插入行数 | 行数 |
| out-of-scope SELECT | 合法但不含目标项目的范围 | **0 行**（不是报错） | 行数 |
| UPDATE 他域行 | 目标行不在 scope | **0 行受影响** | 受影响行数 |
| INSERT 越 scope | `WITH CHECK` 不满足 | **42501** | SQLSTATE |
| UPDATE 改出 scope | 行在 scope、新值越 scope | **42501** | SQLSTATE |
| 缺失上下文 | 不注入 | SELECT 0 行；INSERT/UPDATE 42501 | 行数 / SQLSTATE |
| 空项目集合 `{}` | 注入 `'{}'` | **0 行**（不等于"全部可见"） | 行数 |

**派生 2b：ACL 越权测试（主体必须对该资源无表权限）**

| 项 | 规定 |
| --- | --- |
| 测试主体 | 不在该资源 `permissions` 中的域角色（默认取 `svc_shot_planning`） |
| 前置断言 | `has_table_privilege('<主体>', '<表>', '<任一操作>') = false` —— 否则该主体不能用于 ACL 测试 |
| 目标 | 证明**表级权限检查先于 RLS 求值**：无权限即 42501，与 Policy 表达式无关 |
| 判定 | SELECT / INSERT / UPDATE / DELETE 全部得到 `42501 permission denied for table ...`；**"0 行"视为失败** |

**两类测试不得共用主体**（这正是 v1.4.1 的缺陷）：用零权限角色测「out-of-scope SELECT 返回 0 行」时，
权限检查先于 RLS，实际得到的是 42501，**无法证明任何 RLS 语义**；反过来，用 `svc_asset` 测 ACL 越权，
它本来就有权限，永远得不到 42501。主体、目标、期望三者必须同时区分。

**D-8 边界（不变，且此处必须避免自我美化）**：测试只能构造**另一个真实合法的 Principal 范围**；
**不得**用持连接的任意 SQL 执行者直接 `set_config` 伪造上下文，再把结果表述成"数据库抵御了 GUC 伪造"。
D-8 已明确接受该风险（§13），此处不借测试翻案。

**派生 3：CI 双向核对**

1. Registry → 期望 Policy 集合（名称/roles/cmd/permissive/表达式规范化/**子句集合**）；
2. `pg_policies` → 实际集合；
3. 规范化后逐列 diff（空白/括号差异在规范化阶段消除，语义差异必须暴露）；
4. 差异分两类报错：「实际有、Registry 无」（越权/漂移）与「Registry 有、实际无」（缺失）；
5. **子句存在性断言**（v1.4.2 新增，与派生 1b 同源）：`with_check IS NULL` ⟺ `cmd='INSERT'`；
   `qual IS NULL` ⟺ `cmd='INSERT'`；`cmd IN ('SELECT','DELETE')` ⇒ `with_check` 必须为 NULL。
   该断言同时验证「生成器没有给 SELECT 塞 WITH CHECK」与「没有被手工补丁改回去」；
6. **分区覆盖断言**（v1.4.2 新增）：`pg_class.relkind='p'` 的每个父表，其 `pg_inherits` 子分区必须
   逐个出现在期望 Policy 集合中；缺任一分区即失败（分区不继承父表 Policy）。

- **自动派生（Registry → 落地）**：
  - 业务域 `GRANT`/`REVOKE` 语句由 Registry 的 `permissions` 生成（迁移不再手写 GRANT，接 DATA-6）；
  - 连接池 `domain → svc_<domain>` 映射由 **`domains[].runtime_role`** 生成（v1.4.2：唯一权威，`roles.mappings` 已删除）；
  - **RLS Policy 完整定义由 `resources[].rls.policies[]` 派生**——包含 `roles`（TO）、`commands`（FOR）、
    `permissive`、`using`、`with_check`，直接生成 `CREATE POLICY`，不再是「语义名称 + 人工维护表达式」；
  - 身份四层属性/成员关系选项由 `roles.infrastructure_roles` 派生（INV-9/INV-6b/INV-10/INV-10b）。
- **只能人工维护**：例外条目、`privilege_scope` 的自然语言描述、资源归属的领域判断；其余一律派生。
  （RLS 语义的单一事实源闭环与方案裁定见 D.6.0；当前实际 Policy 与目标的结构性差距见 C.3.1。）
- **CI 校验（Registry → 实际数据库，真实 PG）**：
  - 读 `pg_roles`/`pg_auth_members`（含传递闭包）/`pg_class`/`pg_namespace`/`pg_proc`/`pg_default_acl`/
    `pg_policy`/`pg_policies` 与 ACL 函数（`aclexplode`/`has_*_privilege`），与 Registry 声明逐项比对；差异即失败（§5.4）。
  - 校验 INV-1~INV-10b（§5.1）、E1.6（§12），并核对 `public_execute`（D.5）。
- **漂移检测（Registry ⇄ PostgreSQL 实际权限）**：
  - CI 在真实 PG 计算「实际权限快照」（对象 ACL 展开 + 有效权限 + RLS 语义），与 Registry 派生期望值做双向 diff：
    「实际有、Registry 无」（越权/意外继承）与「Registry 有、实际无」（授权缺失）均报漂移失败；
  - 快照与 diff 结果纳入可追溯证据，漂移未清零不得通过 IP-6；
  - 禁止以「迁移文件看起来正确」或 `pg_default_acl` 单一来源替代实际权限快照（复审要求，见 C.5 证据口径）。
- **不建新系统**：以上均为对现有 `scripts/boundary_assertions.py` 与 `contracts/security_boundary.yaml`
  机制的扩展，不引入独立的权限管理应用或运行时授权服务。
  【v1.4.3 更正】原文此句结尾为「本轮未创建 Registry 文件」——**该句作废**：载体已创建（337 行，
  见 D.1 事实表）。此处保留「不建新系统」的裁定，删除与事实冲突的「未创建文件」表述。

## 附录 E：技术风险清单（v1.4.4 更新，共 25 条 #1~#25）

> **数量口径统一**：本清单为 #1~#25 共 **25 条**（v1.4.3 为 22 条；**v1.4.4 新增 #23 永久 `NOT VALID` 化、
> #24 受控例外被滥用、#25 隔离项目退化为垃圾桶**，均系 D-10 裁决自身带来的残余风险，必须与裁决同时登记）。
> v1.4.1 为 16 条；v1.4.2 新增 #17 项目标识非 UUID、#18 outbox 无项目维度 + payload 可载内容、#19 旧查询漏分区父表、**#20 权限不变量的真空真陷阱**；v1.4 为 13 条；v1.4.1 新增 #14 超级用户授予者残余风险、
> #15 函数默认 PUBLIC EXECUTE、#16 `search_path` 前置用户 schema）。此前口述「12 条」为历史计数笔误，
> 以本表为准；两轮之间不得再出现第三种数量。
> **状态语义**：#1~#7、#10~#13 为「未验证/未实现/未执行」的开放风险；#8 已被所有者**接受**
> （Accepted Risk，风险接受**不等于**消除，见 §13 D-8）；#9 已由所有者**裁定不在阶段 3 退役目标角色**
> （见 §13 D-9），其来源不可证部分仍保留。**任何一条状态变化都不得被表述为数据库层已解决。**

| # | 风险 | 说明 | 真实状态 | 解除条件 / 处理决定 |
| --- | --- | --- | --- | --- |
| 1 | 分支底座未经真实 DB 验证 | `59b13af` 自述 Docker/PG 不可用，部分测试与迁移验证未执行 | 未验证 | IP-6 在真实 PG 补齐 §4.3 四场景、§5.4 权限负向、§12 E1 全部证据 |
| 2 | 两业务库结构对账 | 已采集 checksum 与结构（附录 C + 证据存档 E0~E8），矩阵与可计算验收已定义 | 证据已采集；目标结构未具备 | IP-3 按 C.3 在一次性库演练通过；`postgres` 系统库不参与 |
| 3 | b7c8 授权基线两库不一致 | verify 库 asset/project 仍 `arwd` 自动授权，industrial 已清除（C.2 #2/#5） | 已确认差异，未补齐 | IP-6：verify 补齐到默认零授权，两库一致且无 `arwd` |
| 4 | RLS Policy 语义与目标差距 | 两库 Policy 均 roles=public/cmd=ALL/PERMISSIVE/USING 仅 tenant/WITH CHECK 空（C.3.1，E4）；且**三上下文不能统一套用**：`asset_versions` 无 `organization_id`、`outbox_events` 只有 `tenant_id`、`status_transitions` 无 `project_id`（E10.11） | 已确认，未实现 | IP-4 按 Registry 的 `scope_dimensions` + AST 逐资源生成 Policy（不得一套谓词套全表）；IP-6 E1 行为测试通过 |
| 5 | 目标身份分层不存在 | 两库无 `afs_owner`/`afs_migrator`/`ops_admin`；表 owner 全 `postgres`；INV-1/5/6/9/10 不满足 | 已确认，未实现 | IP-4 建立四层身份并满足 INV-1~INV-10b |
| 6 | FORCE RLS 未启用 | 两库所有表/分区 `relforcerowsecurity=f`，INV-7 不满足；超级用户 owner 下 FORCE 亦无效（接 security_boundary SCOPE-4 债务 1） | 已确认，未实现 | IP-4 转 owner 为 `afs_owner` 并对必强制表启用 FORCE |
| 7 | 迁移窗口授权回收依赖外部编排 | 进程 finally 无法覆盖崩溃/断电；需外部编排层回收 + 凭证短 TTL（§5.1.2） | 设计已定义，未实现 | IP-4 实现外部回收核验与审计；IP-6 验证常态无残留成员边（INV-6） |
| **8** | **GUC 上下文可伪造** | 三个 GUC 均为普通 PostgreSQL 自定义 GUC，均可被持连接的 SQL 执行者自行 `set_config`；**接受风险 ≠ 消除风险**（§3/§4.5/E1.4） | **已接受（Accepted Risk）** — 所有者裁决①：维持现有威胁模型排除声明，不建设数据库可验证授权上下文机制（§13 D-8） | 范围：不承诺抵御持连接且可执行任意 SQL 的攻击者；当前措施：可信 Principal 注入 + 三 GUC 归属校验 + 事务局部 + 缺失 fail-closed；验收边界：E1 只验证可信调用链，不测试对抗任意 SQL 执行者；重新评估条件：未来要求数据库本身抵御恶意 SQL 时独立立项。**不得写成数据库层已解决** |
| **9** | **角色创建来源不可证（集群级共享）** | 8 个 `svc_*` 是**选型 B 的目标运行时角色**（§5.1），不是待退役旧角色；角色属**集群级**对象，两库共享同一套（§6.1.1）；创建来源不可证 | **来源不可证 + 可观测范围内依赖盘点完成**（§6.5 表A/表B + E8/E9） — 所有者裁决：保留目标 `svc_*`，不在阶段 3 退役，延至 IP-4 按 Registry 校准（§13 D-9） | 保留现有 `svc_*`；不 DROP/REVOKE、不重建同名；IP-4 依 Registry 核对目标态（名称/域映射/LOGIN/独立凭证/成员关系/实际 ACL/连接与使用/INV-1~INV-10b）。**证据缺口**：外部部署配置、CI/CD 密钥、外部 Worker、定时任务、运维脚本、历史连接日志未检查，不得宣称「全部运行时依赖不存在」。**依赖盘点（可观测范围）≠ 来源证明 ≠ 允许删除** |
| 10 | 迁移链重建未验证 | 三 Revision 处置为决策，尚未在库上演练 | 未验证 | IP-3 用一次性库逐条演练（§6.4），全绿后方可进入阶段 4 |
| 11 | ADR 重编号未执行 | 映射表已定，文件/引用未改动；本轮未执行 | 未执行 | 阶段 3 前置动作执行，跑契约闸门确认无悬挂引用 |
| 12 | Registry 实现进度（【v1.4.3】按事实更新） | **已落地**：载体 `authorization_registry.yaml` 337 行 + 派生层 `packages/shared/authorization/{ast,registry,ddl}.py` + 契约闸门第 12 项 + 27 例单测。**未落地**：AST→SQL **生成迁移文件**、真实库**漂移检测**。v1.4.2 原文「本轮未创建文件」已作废（D.1） | **载体与派生层已实现；生成与漂移检测为设计态**（不得记作「已实现」） | IP-4 实现生成器 + CI 一致性校验（②态）；IP-6 真实 PG 一致性验证（③态） |
| 13 | E1 真实执行证据缺失 | E1 设计通过，无真实 PostgreSQL 执行证据 | 未采集 | IP-6 采集 E1.1~E1.7 技术证据；IP-7 正式验收签署（责任分离，见 §8.1-B） |
| **14** | **`afs_owner` 的唯一授予者是超级用户（方案 A 的残余风险）** | PostgreSQL 原生无 grantee allow-list；任何非超级用户 grantor 必须自持 `admin_option` 边，会与「常态 grantee 为空」冲突。故窗口授权归 bootstrap 超级用户（§5.1.3） | **不可消除的残余风险（设计上接受）** | 缓解：受控编排 + 短 TTL 凭证 + 双人审批 + 两端审计；**不宣称数据库能约束超级用户**；与 D-8 威胁模型一致。若未来要求数据库层约束授权者，独立立项 |
| **15** | **业务函数默认 `PUBLIC EXECUTE`** | E10.3：6 个函数中 **5 个**带默认 `PUBLIC EXECUTE`（`grantee=OID 0`），仅 `list_active_tenants` 有显式 ACL | 已确认（只读取证），未修 | IP-4 对所有业务函数 `REVOKE ALL ON FUNCTION ... FROM PUBLIC` 并按 D.2 显式授权；IP-6 由 CI 展开 `proacl` 核对 `public_execute=false` |
| **16** | **`list_active_tenants` 的 `search_path` 前置用户 schema（劫持风险）** | E10.3：`proconfig = search_path=outbox, pg_catalog` —— 用户可写 schema 排在 `pg_catalog` **之前**，理论上可用同名对象劫持函数内引用 | 已确认，未修 | IP-4 改为 `SET search_path = pg_catalog`（或全部 schema 限定）；CI 断言 `proconfig` 中 `pg_catalog` 位于首位且无用户 schema 在其前 |
| **17** | **`app.current_projects` 的 `uuid[]` 逻辑类型与真实键类型冲突** | E11.2：`project_id`/`projects.entity_id` 均为 `varchar(64)`；971 行中 0 行为 UUID 形态，取值含 `project-1`（684 行）与 32 位无连字符 hex → `'{project-1}'::uuid[]` 直接 `22P02`。v1.4.1 同时存在两个错误：`string_to_array` 解析错误 + 逻辑类型本身错误 | **v1.4.2 已裁定**（§4.2 改 `text[]`）；属**设计已修、实现未落地** | IP-4 落地后由 E1 断言：同一上下文下合法项目必须可见（E11.1 变体 B = 684 行）；CI 断言生成 SQL 不含 `string_to_array` 与列级 `::uuid` |
| **18** | **`outbox_events` 无项目维度且 `payload` 可承载任意内容** | 列仅到 `tenant_id`，`payload` 为 `jsonb`；tenant-only RLS 无法阻止同租户同域角色跨项目读取事件内容 | **v1.4.2 已裁定**（D.2.2 裁定 1：payload 白名单 + 消费者回源）；契约校验未实现 | IP-4 落地事件契约 `payload` 键白名单校验；CI 对 `payload` 键做封闭集校验；对外表述禁止称其为「项目级隔离」 |
| **19** | **旧有效权限查询漏掉分区父表（`relkind='p'`）** | v1.4.1 的 C.5.3 ⑨ 用 `c.relkind='r'`，`outbox_events` 父表（`relkind='p'`）不在结果中，导致查询无法复现 C.3.4 自己认定的「父表有 `app_role` SELECT、三个分区没有」 | **v1.4.2 已修**（⑨ 改 `IN ('r','p')`，新增 ⑬ 分区父子 ACL 对照）；重新取证待执行 | 用修正后的 ⑨⑬ 重采两库证据，并逐分区落表；DROP 闸门（§7.0.4）依赖该输出 |
| **20** | **权限不变量的真空真陷阱：角色不存在时「全部通过」** | E12.1 实测 `afs_owner`/`afs_migrator`/`ops_admin` 的 `role_exists=f`；E12.2/E12.3 实测**同一事实**在原始查询下返回 0 行（易被读作通过）、在合取式判定下返回 **FAIL**；`member_of_count=0`、`is_granted_to_count=0`、`admin_edges_held=0`、递归闭包 0 行——全部是空集满足 | **v1.4.2 已裁定，且修法已由 E12.2 实跑验证**：所有 INV 判定式须写成 `role_exists(:role) AND <谓词>`；C.5.2 增加输出 PASS/FAIL 的判定查询；§5.3 明确「0 行 ≠ 合规」 | 修法有效，但**三个角色仍未创建**，故 INV-1/5/6/6b/8/9/10/10b 当前只能判「未建立」，**不得判「满足」** |
| **21** | **复合归属外键的组织维度盲区：约束存在但不变量不成立** | E13 场景 4：主支 `fk_asset_versions_project_tenant`（`convalidated=t`）只比 `(project_id, tenant_id)`，**组织错配行原样通过**；改三列 `(project_id, tenant_id, organization_id)` 后立即 `23503`。分支侧该表**无任何外键**，故「归属已闭环」在两支都不成立 | **E13 实测确认（49 步）**；属「约束语义不足」而非「约束缺失」 | IP-4 归属外键一律三列；§8.1-B 验收写**拦截用例**（组织错配行必须被拒），不得写「约束已创建」。与 #20 同源：只看「有无违规行」的不变量，发现不了「不变量没覆盖那一维」 |
| **22** | **归属列同时是不可变内容列 → 历史脏归属无法就地修复** | E13 场景 2/3/4：`trg_asset_versions_guard` 对 `project_id`/`organization_id`/`tenant_id` 的 `UPDATE` 一律 `P0001`（ADR-R11）；同一语句在 `DISABLE TRIGGER` 期间成功。且分支 62 个归属列全 `NOT NULL`，主支 `projects.organization_id` 可空且在 `organizations` 0 行时仍可写 `NULL`（`MATCH SIMPLE`），RLS 策略显式放行 `NULL` 组织 | **E13 实测确认；D-10 已裁定** | **已裁定（D-10.2/D-10.3）**：采用 ① 的**受控例外**形态（**不是** `DISABLE TRIGGER`，改为工单令牌 + 映射行 + 专用角色 + 仅归属三列），② 前向新建降为**例外路径**（单独不成立：旧行仍在表内，`VALIDATE` 必 `23503`，实测 D3e）。可空性冲突**向分支 NOT NULL 收敛**；`NOT VALID` 挡不住 `23502` |
| **23** | **`NOT VALID` 被永久化（过渡手段变成终态）** | `NOT VALID` 只保护新写入、不标记历史违规行；一旦被当作终态，14 表归属永不被证明，且脏行集合只增不减、无机制提示剩余量。这与 D-10.1 的「终态必须闭环」直接冲突 | **D-10.1 已明确拒绝**，但仍需机制防止执行层面滑回去 | 台账（附录 J.2）固定输出 14 行 `convalidated`；任一行为 `false` 时整体验收判 `未通过`，**不得以「迁移已执行」替代**。台账脚本须能在脏库上输出 FAIL（已实测可 FAIL，非恒真） |
| **24** | **受控例外被滥用（`afs_data_repair` 角色 + 工单令牌）** | 例外通道允许变更归属三列，是 D-10.2 为消除「禁守卫窗口」而引入的窄门。若角色长期授予或令牌被伪造，可绕过不可变不变量改写历史归属。GUC 本身可伪造（D-8 已接受该威胁边界），故判定不能只靠 GUC | **设计层已用四重条件收窄**（角色成员 + 工单令牌 + 映射行 + 仅三列），残余风险**未消除** | 角色仅在窗口期授予、窗口结束即 `REVOKE`；映射表每行须有工单号与审批；异常变更须能由 `ownership_repair_map` 反查到人。**不得声称「不可变已完全保证」** |
| **25** | **隔离项目退化为永久垃圾桶** | D-10.3 的默认路径是「移入隔离项目」。若缺乏治理，隔离区会持续吸收「无法判定」的数据，从隔离边界变成第二个归属黑洞，等价于把问题从「脏归属」改名为「隔离归属」 | **D-10.3 已设结构性约束**（外键 `NO ACTION` 防误删、业务侧 RESTRICTIVE 不可见不可写、台账输出隔离占比），但**无自动化上限告警** | 台账固定输出「隔离行数 / 总行数」比值；比值持续上升须触发复审。隔离项目**不得**被业务写入、不得推进回 `ACTIVE`、不得作为默认归属目标被新代码引用 |

> 以上风险除 #8（已接受）与 #9（目标角色保留）外，均为**决策层已规划、执行层未验证或明确不满足**；
> #14 是**设计上接受的残余风险**（不可消除），#15/#16 是**v1.4.1 只读取证新发现、已登记待修**，
> #17/#18/#19 是 **v1.4.2 用 E11 新证实/新修正**的三条：#17 是**设计错误已纠正**（§4.2 改 `text[]`）、
> #18 是**业务边界已裁定、落地未做**（D.2.2）、#19 是**取证工具缺陷已修，并由 E12.5/E12.6 实跑确认修复有效**
> （⑨ 已能看见父表行、⑬ 修掉 collation 缺陷后可执行）。三者都不得记作「已解决」。
>
> **#20 是 E12 实跑后才被证实的，性质与前述都不同**：它不是「文档写错了」，而是**取证方法本身的缺陷**——
> 以「查不到违规」为通过条件，会让「对象根本不存在」与「对象存在且合规」得到同一个结果（0 行）。
> 这比 #19 更危险：#19 是**漏看**（少一行），#20 是**误判**（把不存在读成合规）。
> 修法（合取式判定 + PASS/FAIL 输出）已落地并由 E12.2 验证有效，但只有在三个角色创建之后，
> 该判定才有**正例**可验；在此之前它只能输出 FAIL，不能输出 PASS。
> **#21/#22 由 E13 实测新增（附录 C.6），与 #20 属同一「判定方法」家族**：#20 是「对象不存在被判为合规」，
> #21 是「约束存在但没覆盖那一维」，两者都源于**用一个不完整的不变量去宣布闭环**。
> 因此 #21 的处置不是「再补一条约束」，而是**把验收标准从「约束是否存在」改成「违规行是否被拒」**——
> 前者可被满足而行为错误，后者只能被行为证伪。这也直接影响 §8.1-B 的写法，已在该节登记。
> #22 **已裁定（D-10.2/D-10.3）**：采用受控例外而非 `DISABLE TRIGGER`，前向新建降为例外路径。
> 关于 v1.4.3 原文「在裁决前不得编写补齐迁移」——本版如实说明：补齐迁移是在**所有者明确
> 授权「补齐迁移编写」**后编写的，且**位于 `data/migrations/pending/`、未接入任何 Alembic 链、
> 未在任何现有库上执行**（仅在克隆库验证）。原文那句的目的是**禁止擅自进入实施**，
> 该目的未被违反；但「已写迁移」不得被表述为「迁移方案已通过验收」。

> 风险 #8 的「已接受」仅表示所有者认可当前威胁边界，**不表示 GUC 伪造场景已被数据库层消除**
> （§3/§4.5/E1.4 口径不变）；风险 #9 的「保留」仅表示不在阶段 3 退役目标角色，**不表示其来源已被证明**。
> 本 ADR 保持 PROPOSED 至所有者最终批准；未经批准不得进入阶段 3，不得以本文档修订完成为由宣称
> 数据库授权已实现或已验证。

## 附录 F：v1.3 → v1.4 第四轮复审逐项落实矩阵（交付物）

【v1.4 · 按指令第八节重构】每项修改标注七字段；**状态字段仅取六种枚举之一**：
`已完成设计定义` / `已有只读取证支持` / `待实现` / `待真实数据库验证` / `证据不足` / `存在阻塞`。
**文档完成 ≠ 实现完成**：所有状态均不表示数据库能力已实现或已验收。

### F.1 逐项落实矩阵（七字段）

| # | 原问题（复审指出） | 根因 | 修改位置 | 修改内容 | 验收标准 | 当前状态 | 是否需真实 PG 验证 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F1 | 临时成员关系未裁定 INHERIT/SET，可自动继承 owner 权限 | 授权机制留待实施自定 | §5.1.1 / INV-6b | PG16 裁定 `WITH INHERIT FALSE, SET TRUE`；常态无边，窗口内仅显式切换 | `pg_auth_members.inherit_option=f, set_option=t`；常态 `afs_migrator→afs_owner` 边为空 | 已完成设计定义 | 是（IP-4 实现、IP-6 验证） |
| F2 | ops_admin 凭什么授权未闭环 | 只写"由 ops_admin 执行 GRANT"未写权限来源 | §5.1.3 七问表 / INV-10/10b | **v1.4.1 重写**：方案 A —— `ops_admin` 不持 ADMIN OPTION、无成员边；窗口 GRANT/REVOKE 由 bootstrap 超级用户在受控步骤执行（PG 原生无 grantee allow-list，故移除能力而非检测越界） | `roleid=afs_owner AND admin_option` 的非超级用户边为空；`ops_admin` 无 afs_owner 边 | 已完成设计定义 | 是（IP-4 建角色、IP-6 核对） |
| F3 | finally 回收不覆盖崩溃/断电 | 回收责任在迁移进程内 | §5.1.2 | 外部编排层启动前查残留 + 终态回收 + 看门狗兜底 + 凭证短 TTL | 任意非窗口时刻成员边为空；未回收阻止后续作业与验收 | 已完成设计定义 | 是（IP-4/IP-6） |
| F4 | afs_owner 的 BYPASSRLS/SUPERUSER 等属性未成不变量；FORCE RLS 作用被夸大 | 只要求 NOLOGIN | §5.3 / INV-9 | 五属性全 false；明确 FORCE RLS 仅约束表 owner、对 SUPERUSER/BYPASSRLS 无效，INV-9 是 INV-7 生效前提 | `pg_roles` 五属性位；owner=afs_owner 后再开 FORCE | 已完成设计定义 | 是（IP-4/IP-6） |
| F5 | INV-3 只查直接成员关系 | 未看传递闭包与有效权限 | INV-3b | 递归查传递闭包 + `has_*_privilege`/`aclexplode` 比对最终有效权限 | 闭包中 svc_* 不间接获得跨域权限；有效权限与直接边一致 | 已完成设计定义 | 是（IP-4/IP-6） |
| F6 | C.3 验收是范围描述，不可自动化 | "至少一条 Policy"等模糊标准 | C.3.1~C.3.6 | 可计算验收：具体对象名+预期属性/角色/权限+负向行为；覆盖 Policy 的 TO/FOR/USING/WITH CHECK | 每条标准可生成 CI 断言；负向行为测试以真实域连接执行 | 已完成设计定义 | 是（IP-3 结构、IP-6 行为） |
| F7 | C.5 命令不足以复现结论 | 只查单库、缺成员关系/OWNER/Policy 定义/完整哈希 | C.5 + 证据存档 | 补齐两库全部只读命令；原始输出逐条存档 E0~E7b；不含密码 | 每条 C.2 证据可映射到存档中的实际命令与输出 | 已有只读取证支持 | 否（存档已采集，历史来源部分证据不足） |
| F8 | Registry 资源 ID 格式矛盾、缺 object_type/DB 映射 | D.2 示例与 D.3 规则冲突 | D.2 / D.3 | 统一四段格式 + DB 逻辑ID映射 + 全示例对齐 | 每个资源 ID 四段齐全且全局唯一；CI 校验格式 | 已完成设计定义 | 否（设计态；实现归 IP-4） |
| F9 | 权限枚举缺 TRUNCATE/CREATE/CONNECT/TEMPORARY，未按对象类型区分 | 平铺枚举 | D.4 | 补四项 + 按 object_type 合法子集 + 默认拒绝 | operations ⊆ 对象类型合法子集；svc_* 无 TRUNCATE/CREATE/TEMPORARY（E5 已符合 TRUNCATE，取证见 F7） | 已完成设计定义 | 是（IP-6 全量 ACL） |
| F10 | RLS 语义两套事实，未选方案 | 语义名称在 Registry、真实语义在 SQL/测试 | D.6.0 | 二选一裁定方案 B（CI 核对实际 Policy + 行为测试同源生成） | 无三套事实；`pg_policies` 逐列与 Registry 一致；行为测试同源 | 已完成设计定义 | 是（IP-6） |
| F11 | SECDEF 例外字段不足 | 仅 object/reason/owner/unblocks_when/expires_at | D.2 / D.5 | 补 execute_roles/effective_owner/search_path/accessed_resources/privilege_scope/security_test + PUBLIC EXECUTE 自动核对 | 两函数按实际取证（owner=postgres）填全；public_execute 由 CI 核对 | 已完成设计定义 | 是（IP-6 核对 proacl） |
| F12 | Registry 与历史迁移关系未定，可能重写历史 | 未区分事实源与新变更 vs 历史不可变 | D.6.0b | 历史迁移不可变 + 生成器版本固定 + 新迁移用当时快照 + 两层 CI 一致性 | 迁移头含版本/哈希标注；历史 DDL 不被重生成 | 已完成设计定义 | 否（设计态；实现归 IP-4） |
| F13 | IP-4/6/7 验收重叠；"三库对账"用词错 | 责任边界未拆清 | §8.1-B / 附录 E | 每阶段明确环境/责任/证据；三重叠点裁定；改"两业务库" | 各 IP 证据互不替代；系统库不参与对账 | 已完成设计定义 | 否（责任边界为设计） |
| F14 | 分支 8 个 svc_* 与 main 4 角色未说明过渡/最终态 | 目标态描述含糊 | C.3.5 / §7 / §7.0.1 / §13 D-9 | main 4 角色（app_role/asset_service_role/outbox_relay_role/project_planning_role，取证 E2）按 §7.0.1 逐角色退役；8 个 `svc_*` 是**目标运行时角色，保留不退役**，IP-4 依 Registry 校准 | 角色分类（目标 vs 旧）无歧义；不按角色存在推定 9e3b 已执行 | 已有只读取证支持 | 是（IP-4 目标态校准） |
| F15 | 把集群级角色按库重复记为「两库各 12 个」；角色/对象证据范围未区分 | 未区分 shared catalog 与库级对象 catalog | §6.1.1 / §6.1 / §6.5 / C.2 / C.3.5 / 附录 E / F14 | 明确 pg_roles/pg_auth_members/pg_database 为集群级（`relisshared=t`，只记一份）；owner/ACL/Policy/会话为库级（分库记录）；修正所有角色数量表述 | 全文无「两库各 12 角色」类重复统计；角色事实仅一份 | 已有只读取证支持 | 是（E9 已证；IP-6 复核） |
| F16 | 「六项依赖全部核清」过度解释 | 会话快照/仓库静态引用被外推为「无任何运行时依赖」 | §6.5 证据强度分级 / §13 D-9 / 附录 E #9 | 区分六类证据（存在/来源/对象/会话/仓库引用/外部部署）；标注外部部署、CI 密钥、外部 Worker、定时任务、历史连接日志为**未检查缺口** | 表述限定为「仓库与数据库可观测范围内」；缺口显式列出 | 已完成设计定义 | 是（外部部署核查属 IP-6/运维面） |
| F17 | 保留角色被误读为认可现状 | 「保留」与「验收通过」未分离 | §13 D-9「保留≠认可」表 | 六项裁定：保留角色=是；来源已证=否；权限符合目标=否待 IP-4；当前承担新业务=否；阶段 3 DROP/重建=不允许；IP-4 校准=允许 | 「保留现状 ≠ 认可现状」表述明确且可核对 | 已完成设计定义 | 否（表述裁定；验收归 IP-4） |

### F.2 状态分布与诚实边界

- `已完成设计定义`：F1~F6、F8~F13、F16、F17 —— 仅表示决策已无歧义，可在对应阶段实现；**不表示已实现**。
- `已有只读取证支持`：F7、F14、F15 —— 仅表示证据存档 E0~E9 中有真实 PostgreSQL 只读输出支撑。
- `待实现` / `待真实数据库验证`：所有 INV-1~INV-10b、E1、Policy 语义、漂移检测、Registry 文件与派生器，
  均归 IP-4/IP-6，**当前一项未完成**。
- `证据不足`：三个冲突 Revision 的历史执行来源（b7c8/9e3b 的执行身份不可证）；角色创建来源不可证；
  外部部署/CI 密钥/外部 Worker/定时任务/历史连接日志未检查 —— 均按保守原则不推定。
- `存在阻塞`：阶段 3 启动被 DP2/DP3/DP5 复审确认 + 所有者明确放行两项阻塞；当前均未解除。

---

> **本轮结论（v1.4）**：第四轮复审四项待办已逐项补齐，并按指令要求重构为七字段落实矩阵与六态标注；
> 追加 D-8/D-9 所有者裁决与本节 F15~F17 的补充修正。
> 迁移身份的成员关系、管理员授权、异常回收、角色属性与传递闭包已无开放决策；C.3 可计算验收与 C.5
> 可复现证据已落地；Registry 的资源 ID、操作枚举、RLS 方案（B）、SECDEF 例外、历史版本固定已收敛。
> **补充修正（F15~F17）**：角色证据范围按「集群级 vs 数据库级」分离（不再按库重复计数）；
> 依赖证据收紧为「仓库与数据库可观测范围内」并显式列出外部部署等缺口；「保留 ≠ 认可」六项裁定明确。
> **但 DP2/DP3/DP5 仍标记"待复审确认"，不宣称全部满足；所有实现层验收（INV/E1/Policy/漂移）均未完成；
> 历史来源、角色来源与外部部署依赖存在"证据不足/缺口"。**
> 本 ADR 保持 PROPOSED，交付后停止，等待所有者第四轮复核，不得自行批准阶段 3。

## 附录 G：v1.4 → v1.4.1 第四轮复审剩余问题落实矩阵（交付物）

【v1.4.1 · 按第四轮复审「第八节 交付」要求】每项标注七字段；状态**仅取六种枚举之一**：
`已完成设计定义` / `已有只读取证支持` / `待实现` / `待真实数据库验证` / `证据不足` / `存在阻塞`。
**文档完成 ≠ 实现完成**；不自行把 DP2/DP3/DP5 标为满足。

### G.1 逐项落实（16 项）

| # | 原问题 | 根因 | 修改位置 | 修改内容 | 验收标准 | 当前状态 | 需真实 PG 验证 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| G1 | `ops_admin` 永久持 `ADMIN OPTION` 与 INV-10「常态 grantee 为空」互斥 | ADMIN OPTION 本身就是一条成员边；写文档时未按 PG 语义自检 | §5.1.3 / INV-10 | 改方案 A：`ops_admin` 不持任何 `afs_owner` 边；窗口授权归 bootstrap | `roleid=afs_owner` 的边常态为空 | 已完成设计定义 | 是（IP-4 建角色 / IP-6 核对） |
| G2 | `GRANT ... WITH ADMIN OPTION` 未指定 INHERIT/SET | 只写「ADMIN OPTION」未写三选项 | §5.1.1 / §5.1.3 | 窗口边固定 `ADMIN FALSE, INHERIT FALSE, SET TRUE`；`ops_admin` 无边故三选项均不适用 | `pg_auth_members` 三列逐一吻合 | 已完成设计定义 | 是（IP-6） |
| G3 | 用 Registry allow-list + CI 冒充「PG 强制 grantee 白名单」 | PG 原生无该能力 | §5.1.3 | 明确「原生不提供」；改为移除能力（非超级用户无人能授予） | 非超级用户的 `admin_option` 边为空集 | 已完成设计定义 | 是（IP-6） |
| G4 | C.5 命令覆盖不足（缺 nspowner/逐对象名/函数签名/nspacl/datacl/序列视图 ACL/全量有效权限/E7 仅注释） | 按「结论」写命令，未按「证据维度」写命令 | C.5.2 / C.5.3 | 拆集群级一次 + 库级 12 类查询，全部可复制执行 | 每条 C.2/C.3 结论都能对应到一条命令 | 已有只读取证支持 | 是（IP-6 复跑） |
| G5 | E0~E9 无法独立复核（文件未随 ADR 提供，且不在 main/分支） | 证据存档为本地未跟踪文件 | C.5.5 / 交付说明 | 提供完整证据存档（含新增 E10 系列）；说明其不在 git 跟踪范围 | 存档可被逐条比对命令与输出 | 已有只读取证支持 | 否（文档交付） |
| G6 | Git 用可移动分支名作证据身份 | 用 `origin/...` 取内容 | C.5.4 | 固定 `main@5352d19…` 与 `feat@59b13af…`，按 commit 取 blob 再算 SHA256 | 同一 commit 重跑哈希不变 | 已有只读取证支持 | 否（git 只读） |
| G7 | 同一表要求同一 `policyname` 又要求按命令分别存在 | 未拆「命名」与「命令拆分」两件事 | C.3.2 | 按命令拆分；`pol_<table>_<cmd>_<kind>` 唯一命名 | 同表 Policy 名唯一且可枚举 | 已完成设计定义 | 是（IP-3 结构） |
| G8 | 只定义 RESTRICTIVE Policy 却宣称隔离成立 | 未写 PERMISSIVE/RESTRICTIVE 组合语义 | C.3.2 | 每命令必须有 1 条 PERMISSIVE；RESTRICTIVE 只叠加；无 PERMISSIVE ⇒ 零行 | 生成器对每条命令都能判定组合结果 | 已完成设计定义 | 是（IP-6 行为） |
| G9 | 未指明每个资源授权给哪个域角色；`svc_asset` 被当作越权测试主体 | 授权主体与负向主体未分离 | C.3.3 | 按 E10.5 ACL 反推授权主体；负向主体取零权限域角色（`svc_shot_planning`） | 负向主体与授权主体集合不相交 | 已有只读取证支持 | 是（IP-6） |
| G10 | 所有越权场景统一写成「被拒绝」，与 PG 实际行为不符 | 未区分 USING 过滤与 WITH CHECK 拒绝 | C.3.2 | 7 类场景 × 可观察结果 × SQLSTATE 表 | 测试断言按表生成，不手写 | 已完成设计定义 | 是（IP-6） |
| G11 | `asset_versions` 无 UPDATE / D.2 只有 SI / 又说 UPDATE 待复核，互相冲突 | 未回查 ADR-R10/R11/R25 的真实定义 | C.3.4 / D.2 | 定为 `SELECT, INSERT, UPDATE`（无 DELETE）；内容列不可变由 R11 触发器保证；`qc_reports` 仅 `SI` | 表级权限与 R10/R25 矩阵一致 | 已有只读取证支持 | 是（IP-6） |
| G12 | 未定义父表 Policy 与分区 Policy 的关系、直连分区行为 | 只写「3 个分区各 ≥1 条」 | C.3.2 / C.3.4 | 父表与 3 分区各建 2 条同名策略；直连分区只应用分区策略；父/分区 ACL 不对称须分别断言 | 4 张表 × 2 条策略可枚举 | 已有只读取证支持 | 是（IP-3/IP-6） |
| G13 | D.2 残留 `project_planning.table.organizations`（缺数据库 ID） | 改了 D.3 规则未全量回扫示例 | D.2 / D.3 | 修正为 `industrial.project_planning.table.organizations`；D.2/D.4/D.5 全量交叉检查 | Registry 加载时唯一性与格式校验通过 | 已完成设计定义 | 否（静态校验） |
| G14 | RLS 表达式是未定义字符串（`current_tenant`/`same_as_using`） | 把「语义名称」当成了「可执行定义」 | D.2 / D.6.0c | 定义受限 AST（all/any/not/eq/in/is_null × guc/guc_array/literal）+ 类型与 NULL 规则 + `scope_dimensions`；AST 同时派生 SQL 与测试 | 同一 AST 产出的 SQL 与测试断言同源 | 已完成设计定义 | 是（IP-4 实现 / IP-6 验证） |
| G15 | D.2 `effective_owner: postgres` 与 INV-1 冲突 | 把「观测值」当「目标值」，且忽略超级用户不受 RLS 约束 | D.2 / D.5 | 区分 `effective_owner`(目标=`afs_owner`) 与 `observed_owner`(=postgres)；补两函数完整签名、PUBLIC EXECUTE、search_path 实测 | 目标 OWNER 与 INV-1/INV-9 一致 | 已有只读取证支持 | 是（IP-4/IP-6） |
| G16 | C.3.5 说四个 main 角色都是过渡态，§7 只 `DROP app_role` | 退役计划未逐角色展开 | §7.0.1 / §7.0.2 / C.3.5 | 四个角色逐一定义「目标角色/使用方/保留到/停用/REVOKE/DROP/回滚」；`app_role` 最后退役；DROP 顺序与依赖相反 | 每个角色都有可核对的退役条件 | 已完成设计定义 | 是（IP-4 发布动作） |

### G.2 状态分布（六态全覆盖）

- `已完成设计定义`：G1、G2、G3、G7、G8、G10、G13、G14、G16 —— 决策已无歧义，**不表示已实现**。
- `已有只读取证支持`：G4、G5、G6、G9、G11、G12、G15 —— 证据存档 E10 中有真实只读输出支撑，
  但**证据存在 ≠ 目标结构已具备**。
- `待实现`：D.6.0c 的生成器、D.2 的 Registry 文件、§7.0.1 的退役动作 —— 全部归 IP-4。
- `待真实数据库验证`：INV-1~INV-10b、E1.1~E1.7、Policy 行为、漂移检测 —— 归 IP-6。
- `证据不足`：三个冲突 Revision 的历史执行来源；8 个 `svc_*` 的创建来源；外部部署/CI 密钥/外部 Worker/
  定时任务/运维脚本/历史连接日志（未检查）—— 按保守原则不推定。
- `存在阻塞`：阶段 3 启动仍被「DP2/DP3/DP5 复审确认」+「所有者明确放行」两项阻塞，当前均未解除。

### G.3 本轮新增/未解决的技术风险

见附录 E：#14（超级用户是 `afs_owner` 唯一授予者的残余风险）、#15（5 个业务函数默认 `PUBLIC EXECUTE`）、
#16（`list_active_tenants` 的 `search_path` 前置用户 schema）。

---

> **v1.4.1 结论**：第四轮复审指出的六类问题（ops_admin 权限矛盾、C.5 证据不足、C.3 Policy 冲突、
> Registry 表达式不可执行、SECDEF OWNER 冲突、main 4 角色生命周期不一致）已逐项给出**唯一裁定**，
> 并附带**只读取证**（E10）。但：
> **DP2 / DP3 / DP5 仍不自行判定满足**，须经所有者复审确认；
> **所有实现层验收（INV、E1、Policy 行为、漂移检测、Registry 文件与生成器）一项未完成**；
> **#14 是不可消除的残余风险，#15/#16 是新发现待修项**。
> ADR 保持 **PROPOSED**；阶段 3 **不放行**；交付后停止，等待所有者复审。

## 附录 H：v1.4.1 → v1.4.2 第五轮复审落实矩阵（交付物）

【v1.4.2 · 按第五轮复审「第十节 交付」要求】每项七字段；状态**仅取六种枚举之一**：
`已完成设计定义` / `已有只读取证支持` / `待实现` / `待真实数据库验证` / `证据不足` / `存在阻塞`。
**本轮验收重点不是 G 矩阵是否打勾，而是：AST 能否产生合法且与真实 GUC 编码一致的 SQL、
真实业务身份能否得到预期的 RLS/ACL 结果、原始证据能否独立复核。**

### H.1 逐项落实（9 项：H1~H8 为复审问题，H9 为 E12 实跑追加）

| # | 复审问题 | 根因 | 修改位置 | 修改内容 | 验收标准 | 当前状态 | 需真实 PG 验证 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| H1 | `guc_array` 用 `string_to_array` 切分数组文本，花括号残留 → 合法项目查不到 | 把「PG 数组文本」当成「逗号分隔字符串」；且逻辑类型 `uuid[]` 与真实键类型不符 | §4.2 / D.2 AST 表 / D.6.0c 派生 0~1 | 逻辑类型改 `text[]`；唯一通道 `current_setting(...)::<element_type>[]`；禁止任何手工切分；元素编码 `^[A-Za-z0-9_-]{1,64}$` 保证无需转义 | 生成 SQL 中不含 `string_to_array`/`regexp_split_to_array`；同一上下文下合法项目可见行数 > 0（E11.1 变体 B = 684） | 已完成设计定义 | 是（IP-4 后由 E1 断言） |
| H2 | Policy 模板对所有命令统一输出 `USING` + `WITH CHECK` → 生成非法 SQL | 把「表达式」与「子句」混为一谈，未按 `commands` 分支 | C.3.2 子句存在性裁定 / D.6.0c 派生 1b | SELECT→`USING`、INSERT→`WITH CHECK`、UPDATE→两者、DELETE→`USING`；不适用子句完全省略；给四类完整可执行 SQL 样例 | 生成器输出可被 PG16 直接执行；`pg_policies` 断言 `with_check IS NULL ⟺ cmd='INSERT'` | 已完成设计定义 | 是（IP-4 迁移执行时） |
| H3 | RLS 行为测试与 ACL 越权测试共用零权限主体 | 未意识到「权限检查先于 RLS」导致零权限主体只能测出 42501 | D.6.0c 派生 2a / 2b / §12 | 2a 用 `svc_asset`（须有 SELECT，前置断言 `has_table_privilege=true`）；2b 用 `svc_shot_planning`（须无权限，前置断言 `=false`）；目标/期望分别定义 | 2a 得到 42501 即失败；2b 得到「0 行」即失败 | 已完成设计定义 | 是（IP-6 E1） |
| H4 | outbox（tenant-only）/ audit（tenant+org）与「项目级隔离」承诺不一致 | 把传输层表与合规台账当成业务数据表，未写业务裁定 | D.2.2（新增） | outbox：`payload` 封闭白名单 + 消费者回源；audit：有意组织级 + 元数据限定；承诺覆盖面明确为仅 `asset_versions` | 对外表述不得称 outbox/audit 为「项目级隔离」；事件契约校验落地后 CI 封闭集断言 | 已完成设计定义 | 是（契约校验在 IP-4） |
| H5 | 有效权限查询 `relkind='r'` 漏分区父表；缺递归成员闭包与全对象有效权限 | 按「表」写查询，未按「对象类型 × 权限判定函数」写 | C.5.2（递归闭包、基础设施角色属性、INV-10b 合取式判定）/ C.5.3 ⑨⑬⑭⑮ | ⑨ 改 `IN ('r','p')` 并输出 `relkind`；⑬ 分区父子 ACL 对照（并修其 collation 缺陷）；⑭ 全对象类型有效权限；⑮ 零权限补集布尔矩阵；C.5.2 补递归闭包与 PASS/FAIL 判定 | ⑬ 能复现「父表有 `app_role` SELECT、三分区无」；⑭ 覆盖 schema/table/partition/view/matview/sequence/function/database | **已有只读取证支持**（E12.5/E12.6 已实跑：父表行出现、三分区 `app_role_select=f` 精确复现；⑭ 覆盖 6 类对象；⑮ 证明 `svc_*` 表权限全 f）。但 **⑬ 曾因 collation 缺陷无法执行**，修复后才跑通——说明「查询可执行」本身需实跑验证 | 否（⑨⑬⑭⑮ 已重采为 E12）；其余 ⑩⑪⑫ 沿用 E7/E10 |
| H6 | SECDEF 目标 `search_path` 仅 `pg_catalog`，未处理 `pg_temp` 隐式优先；PUBLIC EXECUTE 只检测不落地 | 把「路径有 pg_catalog」当成「路径安全」；把 CI 检测当成授权动作 | D.5（目标配置四项）/ D.6.0c 派生 1c | 目标 `pg_catalog, pg_temp`；对象全部 schema-qualified；OWNER=`afs_owner`；迁移先 `REVOKE ALL ... FROM PUBLIC` 再 `GRANT` | 两函数目标配置逐项落表；迁移文本含 REVOKE 且位于 GRANT 之前；CI 断言 PUBLIC 不在 grantee | 已完成设计定义 | 是（IP-4） |
| H7 | `domains[].runtime_role` 与 `roles.mappings[]` 双写域→角色映射 | 同一事实两处结构化定义，靠 CI 对齐掩盖 | D.2 / D.2.1（新增）/ D.6.0c | 删除 `roles.mappings`；`domains[].runtime_role` 为唯一权威；四条校验规则（8 项、集合相等、禁止回加、无悬空 domain） | Registry 中不存在 `mappings` 键；`set(domains[].runtime_role)` = 集群 `svc_*` 集合（双向差集为空） | 已完成设计定义 | 否（静态校验） |
| H8 | 旧角色 DROP 只检查会话与代码引用，未证明两库均无对象依赖 | 未把「角色集群级」与「权限库级」的错位落到验收闸门 | §7.0.4（新增） | 七项闸门：所有权/ACL/默认权限（**逐库**）+ 成员关系/活跃会话（集群级）+ 可回滚版本 + 外部部署 | 同一角色两库各跑一遍，任一行非空即中止；先 REVOKE 后 DROP | 已完成设计定义 | 是（DROP 前必须实跑） |
| H9 | **（E12 追加，复审未提出）** 权限不变量在「对象不存在」时真空真，0 行被读作合规 | 不变量以「查不到违规」为通过条件，未把「对象存在」纳入合取 | §5.3 / C.5.2 新增判定查询 / 附录 E #20 | 所有 INV 判定式改 `role_exists AND <谓词>`；C.5.2 增加输出 PASS/FAIL 的查询（角色缺失直接 FAIL）；附录 E 增 #20 | E12.2 在一次查询中同时输出 `inv_10b`、`role_exists`、`offending_edges` 三列，角色缺失时必须是 FAIL | **已有只读取证支持**（E12.2 已实跑输出 FAIL） | 是（角色创建后须复验 PASS 路径） |

### H.2 状态分布（六态全覆盖）

- `已完成设计定义`：H1~H4、H6~H8 —— 仅表示**决策已无歧义、可实现**，**不表示已实现或已验证**。
- `已有只读取证支持`：H1（E11.1/E11.2/E11.3 证明问题存在 + 修复后 684 行）、
  **H5（E12.5/E12.6 已按修好的查询实跑：父表行出现、三分区 `app_role_select=f`、⑭ 覆盖 6 类对象、
  ⑮ 证明 `svc_*` 表权限全 f）**、**H9（E12.2 已实跑输出 FAIL）**。
  注意区分两种「取证支持」：H1 证明的是**修复后行为正确**（684 行可见），
  H5/H9 证明的是**查询能跑通且结论方向正确**（不证明运行时 RLS/ACL 行为）。
- `待实现`：生成器（H1/H2/H3/H6）、事件契约校验（H4）、Registry 文件（H7）、退役动作与 DROP 闸门执行（H8）。
- `待真实数据库验证`：Policy 行为（2a/2b）、INV-1~INV-10b（**因三个基础设施角色不存在，当前只能判「未建立」**）、
  E1.1~E1.7、漂移检测。⑨⑬⑭⑮ **已重采为 E12**（不再是待办），但其结论只到「catalog 状态」，不到「运行时行为」。
- `证据不足`：三个冲突 Revision 的历史执行来源；8 个 `svc_*` 创建来源；外部部署/CI 密钥/Worker/定时任务/
  历史连接日志（§6.5 已列为未检查缺口）。
- `存在阻塞`：阶段 3 启动仍被「DP2/DP3/DP5 复审确认」+「所有者明确放行」阻塞，**当前均未解除**。

### H.3 本轮未解决 / 新增的技术风险

见附录 E：**#17**（`uuid[]` 与真实键类型冲突，设计已改、实现未落）、**#18**（outbox 无项目维度、
`payload` 可载内容，边界已裁定、契约未实现）、**#19**（旧查询漏分区父表，工具已修，**E12.5/E12.6 实跑确认修复有效**）、
**#20**（**权限不变量的真空真陷阱**，修法已落地并由 E12.2 实跑验证，但角色未创建前只能输出 FAIL）。
连同 #1~#16 中的开放项，#14 仍为**不可消除残余风险**，#8 仍为**已接受**（接受 ≠ 消除），
#9 仍为**保留目标角色**（保留 ≠ 认可）。

**E12 实跑带来的三项新事实（必须与「已修」区分）**：

| 事实 | 性质 | 能否记为「已解决」 |
| --- | --- | --- |
| 三个基础设施角色 `role_exists=f` | **实现缺失**（目标态从未建立） | 否——只能记为「未建立」 |
| INV 的真空真缺陷 | **取证方法缺陷**，不是实现缺陷 | 修法是（已落地并实测）；**结论不是**（仍无 PASS 正例） |
| ⑬ 的 collation 缺陷 | **文档内查询不可执行** | 是（已修并跑通）；但暴露「未实跑的查询不得宣称可复现」 |

---

> **v1.4.2 结论（含 E12 实跑后的修订）**：第五轮复审指出的 8 项已逐项给出**唯一裁定**，其中 2 项（AST 解析、Policy 模板）
> 修的是「生成器会产出非法或语义错误的 SQL」这类会直接影响运行的缺陷，并给出了可在 PG16 直接执行的完整样例；
> 1 项（`uuid[]` → `text[]`）是本轮用只读 SQL 证实的**比复审更深的根因**——逻辑类型与真实键类型不符。
> 但：
> **DP3 / DP5 仍不自行判定满足**（C.5 查询已补齐**且已实跑为 E12**，但「查询能跑」≠「运行时行为已验证」；
> Registry 仅设计完成，生成器未实现）；
> **DP2 待复审确认**（bootstrap 执行边界表述已明确）；
> **所有实现层验收（生成器、Policy 行为、INV、E1、漂移检测、事件契约）一项未完成**；
> **#14 不可消除，#17/#18/#19/#20 为本轮新增、新修正或新证实的开放项**。
>
> **E12 实跑改变了哪些判断**（这是本轮相对纯文档修订的增量）：
> 此前 DP3 的短板是「查询不完整」，现在查询完整**且跑通**；但跑通后暴露了更根本的问题——
> **目标角色不存在**（`role_exists=f`），所以「有效权限」这一整类证据在当前库里**只能证明"没有"，
> 不能证明"有"**。也就是说，DP3 的阻塞从「取证工具不足」变成了「取证对象未建」+「运行时行为未验」。
> 这比原来更难绕开：补查询可以一天做完，建角色要等 IP-4。
> ADR 保持 **PROPOSED**；**阶段 3 不放行**；交付后停止，等待所有者第六轮复审。

---

## 附录 I：v1.4.2 → v1.4.3 第六轮复审落实矩阵（【v1.4.3】新增交付物）

【v1.4.3 新增】沿用 F/G/H 的七字段与六态枚举。本附录与前三个矩阵的区别是：
**前三个矩阵回答「设计是否说清楚了」，本矩阵回答「版本是否可指认、数字是否可区分、证据是否可复算」**——
这是复审在连续两轮里唯一没有被闭环的一类问题，也是最容易在下一轮重演的一类。

### I.1 逐项落实（9 项：R1~R8 + E13）

| # | 复审问题 | 根因 | 修改位置 | 修改内容 | 验收标准 | 当前状态 | 需真实 PG 验证 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R1 | 声明用 `【vX.Y】` 标注，实做 21 处用 `(vX.Y)`／`（vX.Y`，按声明 grep 只见 2 处 | 声明与实做两套，且未规定「块级/行内」两种归属粒度 | 文首「版本标注规则」 | 明确双形式及其**归属粒度**（`【】`=块级、括号=行内），给出统一检索正则 `[【(（]v1\.4…` | 用该正则能命中全部历史标注；不再声称单一形式 | 已完成（文档内自洽） | 否 |
| R2 | C.1 标题称「同 ID 不同内容」但只列 3 行，实为 10 个 | 把「必须逐句裁决的 3 项」当成了「全部内容分叉」 | 附录 C.1 标题与导语 | 改判为**口径 C（语义不等价 3 项）**，并指向新增 C.1.1 | C.1 只含 3 项且自称口径 C；全量见 C.1.1 | 已完成 | 否 |
| R3 | 全量分叉从未逐行落表，无法复核 | 分叉只在结论层描述，未落到「每行可复算」 | 新增 C.1.1 | 10 行 × 8 列：Revision / 类型 / 双 blob SHA1 / 双 SHA-256 / 归一化残差 / 路径；固定 ref+commit | 10 行可由 `build_hash_inventory.py` 逐行重算一致 | 已完成（机器生成，非手抄） | 否 |
| R4 | 「3 个迁移冲突」与「10 个分叉」被混为同一指标 | 未区分判定单元（ID 冲突 / 决策单元 / 语义不等价） | 新增 C.1.0 | 三口径表（A=10 / B=10 / C=3）+「三数不可互换」+ T2 的 `POSTGRES_APP_USER` 前置条件 | 文中任何「N 个冲突」都必须带口径字母；三数并列不合并 | 已完成 | 否 |
| R5 | C.1 的 SHA-256 六行中两行按 CRLF、四行按 LF，复核必然「对不上」 | 取字节方式未固定，且未区分主锚点与辅助列 | 附录 C.1 表 + C.2 四格 | 改 LF 实际值；主锚点改 **git blob SHA1**，SHA-256 降为辅助并标注 `git show` 原始字节 | C.1 六行 SHA-256 全部等于 `git show <ref>:<path>` 的 `sha256sum`；blob 列全对 | 已完成（已逐一重算） | 否 |
| R6 | §7.0.4 被判「条款不存在」 | **排版**：加粗段落（非标题）+ 序号倒序（夹在 7.0.2 与 7.0.3 之间） | 附录 §7.0.1~§7.0.4 | 四级全部升级为 `###`；§7.0.4 物理移到 §7.0.3 之后；**编号不变** | `grep "^### 7\.0"` 按 1→2→3→4 顺序命中；全库对 §7.0.4 的两处引用仍有效 | 已完成（编号零破坏） | 否 |
| R7 | D.1 与 §8/附录E #12 称「不创建文件」，而文件已落地 | 文档滞后于实现，且**滞后方向是「低估进度」**，会让复审无法判断边界是否被越过 | D.1 / 附录 E #12 / §8.1-A DP5 | 逐处改为事实：337 行 + 双哈希 + 8 域/7 资源/16 Policy/2 例外/4 债务 + 闸门第 12 项 + 27 单测；同时写明「载体存在 ≠ DP5 满足」 | 文中不再出现「未创建 Registry 文件」；DP5 仍判未通过 | 已完成 | 否（IP-4 验证生成器） |
| R8 | DP3/DP5 说明落后于证据 | 新证据（重放实验、等价性机器验证、E13）未回写 | §8.1-A DP3/DP5 单元格 | 补三项可复算证据；**判定不变**（DP3 矩阵仍 6 行、DP5 生成器仍缺） | DP 判定表与证据一致，且不出现「因有证据故满足」的推断 | 已完成 | 否 |
| E13 | 归属约束在脏状态下能否成立，从未实证 | 「约束已创建」被当成「不变量成立」的判定习惯 | 新增附录 C.6；附录 E #21/#22 | 49 步七场景：目标键缺失 `42830`、组织盲区（两列放行/三列拦截）、脏数据不可就地修 `P0001`、哨兵行阻塞建键、62 列全 NOT NULL、`NOT VALID` 生命周期 | 「组织错配行必须被 `23503` 拒绝」可复现；不出现「约束存在即通过」 | **已有只读取证支持**（49/49，E13） | 是（仅代表性表；并发/大表锁未测） |

### I.2 状态分布（六态全覆盖）

- `已完成（文档内自洽）`：R1、R2、R4、R6 —— 仅表示**表述与事实一致、可自检**，不表示任何实现就绪。
- `已完成（机器生成）`：R3、R5 —— 可由脚本逐行重算，不接受人工抄录。
- `已完成（事实回写）`：R7、R8 —— 只修正「文档与现实的偏差」，**判定结论一律未放宽**。
- `已有只读取证支持`：E13（49 步全绿）—— 证明的是**行为**，但仍限于单表与串行场景。
- `待实现`：Registry 生成器（H1/H2/H3/H6）、漂移检测、事件契约校验。
- `待裁决`：**#22 的修复路径（维护窗口 vs 前向新建）**；阶段 3 放行。
- `证据不足`：T1 `b3c4` 的历史执行来源；外部部署/CI 密钥/Worker/定时任务；生产库脏行数量与分布。
- `存在阻塞`：DP2/DP3/DP5 未获复审确认 + 所有者未放行，**阶段 3 仍被阻塞**。

### I.3 本轮**未做**的事（边界，逐条可核）

| 事项 | 状态 | 原因 |
| --- | --- | --- |
| ADR 定版 | **已做**（仅文本冻结 + 指纹登记）；**发布未做** | 所有者本轮授权「定版」，未授权「发布」；定版不构成批准 |
| 修改 v1.4.2 原件 | **未做** | 原件字节与 SHA-256 保持 `9c0edd1b…`，双版并存可逐版对照 |
| 修改 v1.4.2 原件 | **未做** | 原件字节与 SHA-256 保持 `9c0edd1b…`，双版并存可逐版对照 |
| 编写补齐迁移 | **未做** | 需先裁决 #22；且归属矩阵与 E13 只到「设计输入」层 |
| 进入阶段 3 | **未做** | DP 未确认、所有者未放行 |
| 修改迁移 / 代码 / 真实库角色与 Policy | **未做** | 全程零改动；E13 仅作用于克隆库 `t3_lab` |
| 生产环境取证 | **未做** | 生产状态仍为「未知」（既定裁决），未连接 |

### I.4 v1.4.3 结论

> 本轮**没有新增任何设计**。三件事分别是：把**版本做成可指认的**（文件名 + 版本索引 + 双哈希）、
> 把**三个数字分开**（A=10 / B=10 / C=3，不可互换）、把**证据做成可复算的**（10 行分叉机器生成、49 步实验）。
> 另修三处「文档与现实不符」：§7.0.4 的可检索性、D.1/§8/#12 的「未创建文件」、DP3/DP5 的证据滞后。
>
> **判定一律未放宽**：DP3、DP5 仍不满足；DP2 仍待复审确认；#21 因 E13 而新增、#22 尚无裁决
> （← **v1.4.4 已更新：#22 于 §13 D-10 裁定**，此句为 v1.4.3 时点陈述，保留以可追溯）；
> 所有实现层验收（生成器、Policy 行为、INV、E1、漂移检测、事件契约）**一项未完成**。
>
> **状态**：`PROPOSED`（**v1.4.3 已定版·文本冻结**；未发布）；**阶段 3 不放行**；待所有者第七轮复审。
> 指纹登记：`docs/adr/ADR-R40-VERSION.txt`（append-only）。定版只锁文本，不构成批准，不放宽任何判定。

---

## 附录 J：D-10 落实矩阵与 14 行终态验收台账（【v1.4.4】新增交付物）

【v1.4.4 新增】沿用 F/G/H/I 的七字段与六态枚举。本附录与前四个矩阵的差别是：
**前四个回答「设计是否说清楚了」，J 回答「裁决是否可执行、终态是否可判定」**——
D-10 的三项裁决都是「治理方式」而非「技术方案」，最容易变成一段无法验收的表态。

### J.1 逐项落实（3 项裁决 → 12 条可核判据）

| # | 裁决 | 落到哪一节 | 可核判据（机器可验 / 人工可核） | 当前状态 | 需真实 PG 验证 |
| --- | --- | --- | --- | --- | --- |
| J1 | ① `NOT VALID` 有条件采纳 | §13 D-10.1 | 迁移中 14 条外键全部以 `NOT VALID` 建立，且台账 `convalidated` 初始为 `false` | 已完成（迁移克隆库 D1，14/14） | 是（各表逐条 `VALIDATE`） |
| J2 | ① 终态必须 14 表闭环 | §13 D-10.1 + J.2 | 台账 14 行全部 `PASS`（存在 ∧ `convalidated` ∧ 违规行数=0） | **未达成**（当前 0/14，因未激活迁移） | 是 |
| J3 | ① 拒绝永久 `NOT VALID` | §13 D-10.1 + 附录 E #23 | 台账存在 `PENDING_VALIDATE` 行时，整体验收必须判「未通过」；脚本须能在脏库输出 FAIL | 机制已完成（台账脚本可 FAIL，已实测） | 否 |
| J4 | ① 拒绝「文档声明替代真实验证」 | §13 D-10.1 + J.2 | 台账每个 PASS 都带来源：`pg_constraint` 实读 + 逐表 `LEFT JOIN` 违规计数；不接受人工填写 | 已完成 | 是 |
| J5 | ② 写入保护先行 | §13 D-10.2 | 层 1 外键在任何订正之前建立；层 2 守卫全程在线（无 `DISABLE TRIGGER`） | 层 1 已证（D3b/D3d 拒绝违规写入）；层 2 **待实现** | 是 |
| J6 | ② 无未保护窗口 | §13 D-10.2 | 订正事务内不得出现 `DISABLE TRIGGER`；例外须同时满足四重条件 | 设计完成，**未实现** | 是（并发写入用例） |
| J7 | ② 守卫先于订正生效 | §13 D-10.2 | 启用受控例外的同时守卫仍拦截「非映射行」与「非归属列」的变更 | 设计完成，**未实现** | 是（负向用例必须被拒） |
| J8 | ③ 哨兵行移入隔离项目 | §13 D-10.3 | 每个组织存在 `quarantine-<organization_id>` 真实项目行；哨兵行三列外键可 `VALIDATE` | **未执行**（无生产数据可迁） | 是 |
| J9 | ③ 隔离项目真实存在且合规 | §13 D-10.3 要求 1 | 隔离项目行满足 `entity_id = 'quarantine-'||organization_id` 且三列与所属组织一致 | 未执行 | 是 |
| J10 | ③ 业务侧不可见/不可写 | §13 D-10.3 要求 3/4 | 业务主体查询返回 0 行；业务主体写入被拒；审计主体开 GUC 后可读写 | **未实现**（策略未上线） | 是 |
| J11 | ③ 保留原始标识与变更记录 | §13 D-10.3 要求 5 | 每个被迁移行在 `ownership_repair_map` 恰一条，`old_project_id` 保留原值 | 设计完成（表结构见裁决文档） | 是 |
| J12 | ③ 例外走单独工单 | §13 D-10.3 要求 7 | 台账「例外工单」列非空且可回溯审批 | 未发生 | 否（流程） |

### J.2 终态验收台账（14 行，**唯一验收判据**）

**判定规则（三条件合取，缺一即 `未通过`）**：

```
PASS(t) ⇔ 约束存在(t) ∧ convalidated(t) = true ∧ 违规行数(t) = 0
```

其中「违规行数」= 该表按 `(project_id, tenant_id, organization_id)` 与 `projects` 做
`LEFT JOIN` 后 `p.entity_id IS NULL` 的行数（B 类表按 `(organization_id, tenant_id)` 与
`organizations` 比对）。**不允许用「迁移已执行」或「约束已创建」替代。**

台账由 `docs/audits/mig-recon/acceptance_ledger.py` 生成，输出 `acceptance-ledger.md/json`。
表体结构（14 行 + 隔离占比）：

| # | 表 | 类型 | 目标约束 | 约束存在 | `convalidated` | 违规行数 | 隔离行数 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `generation_execution.production_routes` | B 组织级 | `fk_production_routes_organization_tenant` | 待采集 | 待采集 | 待采集 | — | **未执行** |
| 2 | `generation_execution.generation_tasks` | A 项目级 | `fk_generation_tasks_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 3 | `generation_execution.shot_route_selections` | A 项目级 | `fk_shot_route_selections_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 4 | `shot_planning.shot_drafts` | A 项目级 | `fk_shot_drafts_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 5 | `shot_planning.shot_revisions` | A 项目级 | `fk_shot_revisions_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 6 | `shot_planning.shot_visual_plans` | A 项目级 | `fk_shot_visual_plans_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 7 | `shot_planning.constraint_packs` | A 项目级 | `fk_constraint_packs_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 8 | `asset_service.delivery_manifests` | A 项目级 | `fk_delivery_manifests_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 9 | `asset_service.asset_versions` | A 项目级 | `fk_asset_versions_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 10 | `qc_governance.gate_decisions` | A 项目级 | `fk_gate_decisions_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 11 | `qc_governance.qc_review_sessions` | A 项目级 | `fk_qc_review_sessions_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 12 | `workflow.approval_flows` | A 项目级 | `fk_approval_flows_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 13 | `workflow.approval_tickets` | A 项目级 | `fk_approval_tickets_project_tenant_org` | 待采集 | 待采集 | 待采集 | 待采集 | **未执行** |
| 14 | `project_planning.projects` | L2 归属根 | `fk_projects_organization_tenant` | 待采集 | 待采集 | 待采集 | — | **未执行** |

> **为什么表里是 14 行而不是「14 张域表」**：13 张域表（`production_routes` 为组织级）+ 归属根
> `projects`。`projects` 自身的组织外键若不闭环，下面 13 行的闭环就无法解释「项目属于哪个组织」。
> **两张 C 派生级表（`shot_specs` / `approval_steps`）不在本台账**——它们本轮刻意不建归属外键，
> 归属由父对象派生；把它们计入台账会让「闭环」变成假指标（建了不该建的约束来凑数）。

### J.3 本轮（v1.4.4）**未做**的事（边界，逐条可核）

| 事项 | 状态 | 原因 |
| --- | --- | --- |
| 激活补齐迁移 | **未做** | 未获授权；迁移仍在 `data/migrations/pending/`，未接入 Alembic 链 |
| 实现受控例外（守卫函数改造） | **未做** | 属实现层变更，须单独授权；本轮只写裁决与机制定义 |
| 上线隔离项目的 RLS 策略 | **未做** | 同上；本轮给出机制与验收判据，不含 DDL |
| 修改任何业务代码 | **未做** | 明确不授权 |
| 操作生产 / 模板库 / `ai_film_verify` | **未做** | 明确不授权；实验仅在克隆库，用完即删 |
| 销毁四个容器（15441–15444） | **未做** | 所有者要求保持现状 |
| 修改 v1.4.2 / v1.4.3 原件 | **未做** | 两版字节与指纹均未变，三版并存 |

### J.4 v1.4.4 结论

> 本版**没有新增技术设计**。它做的是把三项治理裁决变成**可判定**的东西：
> ① 把「终态」从一句表态变成 **14 行台账**（三条件合取，脚本实读，可在脏库输出 FAIL）；
> ② 把「先保护写入」从一句要求变成**两层保护 + 受控例外**，并撤回上一轮「禁守卫」的建议——
> 因为禁守卫期间保护是被摘掉的，而受控例外只是对特定行开一道可审计的窄门；
> ③ 把「隔离项目」从一句默认口径变成**七条机制 + 三条防止退化成垃圾桶的结构性约束**。
>
> 同时如实登记 D-10 自身带来的三条残余风险（#23 永久 `NOT VALID`、#24 例外被滥用、
> #25 隔离区变垃圾桶）——**裁决不等于风险消失**。
>
> **判定一律未放宽**：DP2/DP3/DP5 状态未动；阶段 3 仍不放行；14 表闭环 **0/14 达成**；
> 受控例外、隔离项目策略、台账实采**均未实现**。
>
> **状态**：`PROPOSED`（**v1.4.4 已定版·文本冻结**；未发布）；指纹登记于
> `docs/adr/ADR-R40-VERSION.txt`。**「D-10 已明确」不等于「迁移方案已通过实施验收」。**
