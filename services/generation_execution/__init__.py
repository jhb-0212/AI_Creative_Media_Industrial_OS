"""Generation Execution Domain 生成执行域（单一路线生成）。

数据所有权（V2.0.5 14.2 / 契约修订 V205-R14-A6/A7）：
- 独立 PostgreSQL schema ``generation_execution`` + 独立数据库角色
  ``svc_generation_execution``（packages/shared/db_roles.py）；
- ProductionRoute（路线适配器目录）与 GenerationTask（生成任务，主链段 3）
  是本模块的权威所有对象；其它域不得直写本域表，本域也不直写他域表。
"""
