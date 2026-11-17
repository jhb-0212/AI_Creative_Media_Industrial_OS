"""生成执行 worker（GENERATION.REQUEST 受理后的任务执行环）。

为什么是独立 worker 而不是塞进 API 进程：
- 生成是长耗时操作（供应商调用/渲染），必须与请求处理解耦（8.x 异步解耦）；
- 执行用 svc_generation_execution 数据库身份（任务表所有者域），
  与 API 的四域池同角色但独立连接生命周期；
- 9.4：Worker 崩溃 → 租约到期后重新领取（幂等重试）——本 worker 的领取
  以乐观版本控制裁决（两个实例同时领取同一任务只有一个成功）。

诚实边界（ADR-R32）：模型网关域（services/model_gateway）当前为空壳，
本 worker 以**确定性适配器**执行任务生命周期闭环（PENDING→RUNNING→
SUCCEEDED/FAILED + result_ref 登记），不调用外部供应商。真实供应商适配
随 model_gateway 域落地后接入同一 ``GenerationAdapter`` 协议——执行环
（领取/转移/守卫/并发裁决）不因适配器更换而改变。
"""
