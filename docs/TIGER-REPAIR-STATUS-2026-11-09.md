# Tiger 修复计划状态（2026-11-09）

## 基线与身份

- 第一轮候选：`41446f343ed4fc78331e7b6719cc792170ed80e6`。
- 第二批任务：`T-2026-1109-0001`，Issue `#63`。
- 本文件记录实现和证据边界，不把机制测试写成真实生产研究通过。

## 第一轮已落地

- 完整模型请求计费、固定 tokenizer、provider framing 与实际发送边界。
- 持久 WorkStore、attempt/unknown 预留、同 run resume、候选 supplement。
- 原件解析 manifest、source-bound facts、关系/时点、coverage、pending risk。
- 有界来源与文档并发、统一研究快照、独立 work proof。
- 四轴状态、逐证券卡、静态报告和 topic CLI/MCP。
- 模型字符偏移不再被信任；唯一 quote 由程序回填 span。
- 主体 identifier 必须在原文 span 中逐字出现。
- document review completion 只覆盖当前提供正文，来源完整性由程序单独判定。

## 第二批已落地

1. 负向 document review 不再抛成整批失败，而是形成可独立验真的
   `unverifiable` 终态；该原件不进入评分文档集，其余原件可继续。
2. WorkStore 为白名单内的语义验证错误保留稳定错误码；
   任意异常正文和模型值仍不落盘。
3. 新增固定 `cancel` 操作：只接原 `run_id`，持久化调度取消；
   不撤销 unknown external attempt，显式取消后不可 resume。

本地定向证据（B）：

- 真实 runner、公开恢复、资源准入、work execution 与语义组合：`74 passed`。
- 独立 `ResearchWorkProof`：`63 passed`，包含负向 review 的 `unverifiable` 终态重建。
- CLI / MCP / workflow schema：`56 passed`。

## 第三批语义终态（2026-11-10）

1. 模型回复通过 JSON schema 后，若主体、期间、数值或原文 span 的唯一绑定仍
   失败，只把该原件记为 `unverifiable`；稳定错误码进入受控 work result，
   不持久化异常正文或模型值。
2. 小文档的 review 单元以失败 extract 的 result hash 构造依赖终态，不再
   伪造第二次模型复核；分段文档先为每段保留 extract/review 事实，再由
   metadata synthesis 单元绑定全部失败段，形成唯一原件终态。
3. 分段合并后的最终语义校验失败同样进入 `unverifiable`。传输、schema
   format 在共享格式重答已使用或额度已占用且 usage 已知时，也进入逐原件
   终态；失败模型 I/O 保留在 child seal。传输、未知 usage、存储和内部图
   不变量错误仍是 run-level failure。
4. `ResearchWorkProof` 逐个重放真实 child；依赖终态必须与上游 result hash、
   source hash 和稳定错误码一致；schema failure 的 error_ref 及其原生
   response observation 必须重放，不允许以成功的 validated 行冒充模型复核。

定向证据（B）：

- evidence plus research work execution/proof：`138 passed`。
- 语义、执行、独立回执、公开恢复、资源/任务账本与状态组合：`167 passed`。

## 剩余非代码门禁

- 最终真实 237 必须在新合同冻结后重新运行，并生成逐原件终态、
  receipt、verify 和 usage 回执。
- 42 个 AT 仍需要一份逐项证据索引，绑定命令、结果、证据路径和未覆盖原因。
- 真实金融金标的独立裁决、真实 cohort/reliability、真实风险补证闭环尚未完成。
- live UI 的取消/恢复/断线展示仍需要宿主端到端走查；当前完成的是 Python、
  CLI、JSON Schema 和 MCP 契约。
- 回滚演练、7–14 天观察、持续容量和跨任务失效传播仍属于后续时间门禁。

## 不变量

- 取消观察不等于取消调度。
- partial 来源不得伪装为完整证据。
- 失败单元、unknown usage 和已发起的 live reservation 不得删除或改成零。
- 没有独立 receipt/verify 的真实研究不能标记业务完成。
- 没有普通 main 合并、main CI 和 Release 字节回读，不能标记发布完成。
