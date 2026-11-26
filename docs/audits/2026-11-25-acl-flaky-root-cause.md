# `test_append_only_tables_reject_row_mutation` 偶发红灯：根因调查（**不销项**）

> 日期：2026-11-25 ｜ 性质：根因调查，不重试、不放宽测试、不销项 ｜
> 关联：`2026-11-25-p2-release-review.md` §5 第 5 项 ｜ 用例：`tests/integration/test_tenant_isolation_invariants.py:189`

## 0. 一句话结论

**那条红灯的日志不存在**，所以不能宣称「已定位」。本记录做的是三件事：把「能让它变红」
的路径穷举成可判别的指纹表、把「套件内竞态」排除掉、在干净库上证明当前不红——并顺带
发现一处必须修的潜在机制和一处论证缺口。三项都**不构成销项理由**。

## 1. 取证：先确认那份日志在不在

结论先给：**不在**。近 300 次 CI 运行（239 次，失败 37 次）里，2026-11-20 之后的
9 次失败逐条核对到失败步骤，没有一次停在 `test_append_only_tables_reject_row_mutation`：

| run | 失败步骤 | 与该用例有关 |
|---|---|---|
| `45367501199` | Migration downgrade roundtrip | 否（Tests 步骤 ✓） |
| `45358892920` | Tests —— `test_gc_schema_gap_rulings` / `test_shot_breakdown_contract` | 否 |
| `45307743488` | Type check (mypy) | 否 |
| `45305408391` | Lint (ruff) | 否 |
| `45027303983` | Migrations sync check (alembic check) | 否 |
| `45027025528` | Migrations sync check (alembic check) | 否 |
| `45026692434` | Tests 未执行（前序步骤失败） | 否 |
| `45026085919` | Tests 未执行（前序步骤失败） | 否 |
| `45019126407` | Type check (mypy) | 否 |

再往前的失败属于 `main` 与其它分支、且早于该用例引入（R10，`c344d47`）。

**这条事实本身要写下来**：一次「偶发红灯」如果没留下日志、也没留下当时的库状态，它能
支撑的结论只有「当时红过」。任何「已修好」的说法都缺证据；任何「某个原因」的说法都只是
推测。所以本项维持挂账。

## 2. 这条断言实际依赖什么（比它看起来复杂）

用例读的不是表上的 ACL，是 **`information_schema.role_table_grants` 这个视图的行集**：

```sql
SELECT privilege_type FROM information_schema.role_table_grants
WHERE grantee = :role AND table_schema = 'asset_service' AND table_name = :table
```

三点推论，逐条都能改写「红/绿的含义」：

1. **观察者身份是判据的一部分。** 该视图只返回「grantor 或 grantee 是当前启用角色」的
   授权行。观察者一旦既不是授予者、也不是被授予者、也不是超级用户，行集就是空的。
2. **grantee 来自配置而不是登记表**：`get_settings().postgres_asset_service_user`
   （默认 `asset_service_role`），不是 `db_roles.domain_role("asset_service")` 那个
   「角色名唯一来源」。环境变量写错一个字符，红的是同一条断言。
3. **授权由两条相反操作合成**（`8f3b2c1d4e5a_split_domain_roles.py`）：
   先 `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA asset_service`，
   再由 `_revoke_append_only_overreach()` 收回 `UPDATE, DELETE ON qc_reports` 与
   `DELETE ON asset_versions`。少了后半段，红；少了前半段，也红。

## 3. 三类能红的路径，各自的指纹（带实测）

| # | 原因 | `实际 {...}` 长什么样 | 证据等级 |
|---|---|---|---|
| M1 | 观察者看不见（admin 连接既非 superuser、非表 owner、也非 grantee 成员） | `[]` | **实测** |
| M2 | 收口未生效 / 库不在预期 revision（少授或多授） | 少一项（如 `['INSERT']`）或多一项（如 `['INSERT','SELECT','UPDATE']`） | **实测（等价路径）** |
| M3 | 表被重建后继承 default privileges | 多到全量：`['DELETE','INSERT','SELECT','UPDATE']` | 机制成立，未实测（当前只有一处 `CREATE TABLE qc_reports`） |

### 3.1 M1 实测：同一个查询换个观察者就是 0 行

```
以 app_role 观察            → <0 行>
以 asset_service_role 自己  → SELECT,INSERT
以 postgres（超级用户）     → INSERT,SELECT
```

在干净库（迁移到 head 的隔离容器）上复现，与库版本无关：这是**目录视图的可见性规则**，
不是数据问题。也就是说，如果某次运行的 admin 连接换了个身份，这条断言会报
「授权口径不符：实际 []」，读起来像「授权丢了」，实际是「看不见」。

### 3.2 M2 实测：多授一条 UPDATE 就会红，撤回即绿

```
GRANT UPDATE ON asset_service.qc_reports TO asset_service_role;
→ AssertionError: qc_reports 授权口径不符：实际 ['INSERT', 'SELECT', 'UPDATE']，期望 ['INSERT', 'SELECT']
REVOKE UPDATE ... ;  → 1 passed
```

这条实测有两个用处：一是确认断言对「多授」敏感（不是装饰），二是给出 M2 的精确指纹——
**今后再红，先看 `实际` 集合，指纹直接指向原因**，不必再从头猜。

### 3.3 M3：为什么它是「潜在」而不是「现存」

`_grant_business_schema()` 同时做了两件事：对**现存**对象 `GRANT … ON ALL TABLES`，以及对
**未来**对象 `ALTER DEFAULT PRIVILEGES … GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES`。
收口只对现存对象做一次 `REVOKE`。于是任何今后重建 `qc_reports` / `asset_versions` 的迁移，
新表都会按默认权限拿回 `UPDATE`/`DELETE`，而收口不会自动重放——红，且看起来像「有人改了授权」。

当前只有一处 `CREATE TABLE qc_reports`（`c1d2e3f4a5b6`），所以它没发生过。但这条路径与
「偶发」是同一个形态：**没有任何代码改动，某天开始红**。详见 §6 建议 3。

## 4. 排除：套件内竞态不成立（可复跑）

- **没有并行**：`addopts = "-ra -q"`，CI 是 `uv run pytest tests -q`，未装 xdist、无 `-n`；
- **没有任何用例改 ACL**：全仓 `tests/` 里没有执行 `GRANT` / `REVOKE` / `CREATE ROLE` /
  `ALTER DEFAULT PRIVILEGES` 的语句，也没有调用 alembic（`tests/engineering` 只解析源码
  文本，不连库执行）；
- **干净库上不复现**：隔离容器 + `alembic upgrade head` 后，
  `tests/integration` 全套 **240 例全绿**；`test_tenant_isolation_invariants.py` 4 例连跑
  **5 次全绿**。

因此：**库在同一个 revision、连接身份不变时，这条用例是确定性的**。「偶发」只能来自
「库的 revision / 角色状态不同」或「连接身份不同」——M1/M2/M3 已覆盖全部出口。

## 5. 顺带发现：它绿也不一定说明本分支对

本机库 `ai_film_api_tests` 停在 `b1c2d3e4f5a6`，本分支 head 是 `f9b0c1d2e3f4`。
在这个落后的库上跑 `pytest tests`：1826 例中 186 例红（缺 `audit.approval_signatures`
关系、`storage_ref` 列类型不符、`artifact_manifests.project_id` 不存在……都是「库落后于
代码」的形态），而**这条 ACL 用例仍然是绿的**——因为 `b7c8d9e0f1a2` 与 `8f3b2c1d4e5a`
在那个 revision 上已是祖先。

两个方向都要说清楚：

- 它红 ⇒ 「当前这个库不满足」，**不等于**「本分支改坏了授权」；
- 它绿 ⇒ 「当前这个库满足」，**不等于**「本分支的迁移产出了这个口径」。

作为回归闸门，两边都偏弱。这不是新缺陷，而是这条断言一直没被写下来的前提。

## 6. 建议（须单独裁决，本轮不实施）

1. **判据换成角色相对函数**：`has_table_privilege('asset_service_role',
   'asset_service.qc_reports', 'UPDATE')`。它与观察者无关，而且把「经角色继承得到的权限」
   也算进去——现在的目录视图看不见继承，是另一种漏报。
   一个实现细节：文本形式需要能解析表名，用 `app_role` 连接执行会撞
   `permission denied for schema asset_service`；要用 OID 形式或在 owner/superuser 连接上跑。
2. **断言观察者前提**。conftest 的 `admin_sessionmaker` 写了「前提必须被验证而不是假设」
   并断言 `rolsuper`；本模块的 `admin_engine` 只有 docstring 说「迁移角色连接」，没有任何
   断言。同一份前提在仓库里一处被验证、一处被假设，正好落在最需要它的位置。
3. **收口与默认权限同源**：`ALTER DEFAULT PRIVILEGES` 不该给 append-only 表满量 DML，
   否则 M3 迟早变成一条真红灯。
4. **断言库在 head**：把这个用例的结论与自己声称的前提绑死；否则 §5 那种「绿得没道理」
   会一直存在。

四条都改判据/改迁移，属「改则须单独裁决」的范围（`2026-11-25-p2-release-review.md` §8.4
的同一口径），本轮一条都不做。

## 7. 本记录不改变什么

- **不销项**：P2 §5 第 5 项仍为「根因未定位」——原始日志不存在（§1），只是把可能性从
  「未知」收敛成「三类、各有指纹」；
- **未改测试**：没加重试、没放宽判据、没改断言文案；
- **未改运行时与迁移**：M3 只是记录，没有动 `ALTER DEFAULT PRIVILEGES`；
- **未动本机库** `ai_film_api_tests`（仍停在 `b1c2d3e4f5a6`）；复现用的是隔离容器
  `afs-rca-pg`（端口 15455），跑完已删除。

## 8. 复跑命令

```bash
# 1) 干净库复现环境（与用户本地库隔离，跑完即删）
docker run -d --name afs-rca-pg -p 15455:5432 -e POSTGRES_PASSWORD=rca_pw \
  -e POSTGRES_DB=ai_film_rca postgres:16
export POSTGRES_HOST=localhost POSTGRES_PORT=15455 POSTGRES_USER=postgres \
       POSTGRES_PASSWORD=rca_pw POSTGRES_DB=ai_film_rca
python -m alembic upgrade head
python -m pytest tests/integration -q                       # 240 passed
python -m pytest tests/integration/test_tenant_isolation_invariants.py -q  # ×5

# 2) 观察者依赖（M1）：同一查询，三个观察者
docker exec afs-rca-pg psql -U app_role -d ai_film_rca -tAc \
  "SELECT coalesce(string_agg(privilege_type,','),'<0 行>') FROM information_schema.role_table_grants WHERE grantee='asset_service_role' AND table_schema='asset_service' AND table_name='qc_reports';"
docker exec afs-rca-pg psql -U postgres -d ai_film_rca -tAc \
  "SELECT string_agg(privilege_type,',' ORDER BY privilege_type) FROM information_schema.role_table_grants WHERE grantee='asset_service_role' AND table_schema='asset_service' AND table_name='qc_reports';"

# 3) 角色相对判据（建议 1 的可行性验证；注意须在能解析表名的连接上跑）
docker exec afs-rca-pg psql -U postgres -d ai_film_rca -tAc \
  "SELECT has_table_privilege('asset_service_role','asset_service.qc_reports','UPDATE'),
          has_table_privilege('asset_service_role','asset_service.qc_reports','INSERT,SELECT');"
# → f | t

# 4) 清理
docker rm -f afs-rca-pg
```
