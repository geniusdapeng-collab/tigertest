# TigerTest v4 可靠性与验收

## 定位

`us-tech-intelligence-paper-v4` 复用
`us-tech-intelligence-paper-v3` 的选股范围、五阶段真实模型要求、风险阈值和纸面交易边界，
只增加可靠性、恢复、审计和可观测性合同。v3 的 prompt、schema、hash 和历史回执继续按冻结
源码只读重放；v4 不迁移或补签旧 run。

## 合同

| 合同 | 作用 |
|---|---|
| `tiger.run-readiness/v1` | 分离 `runtime_ready`、`model_ready`、`sec_identity_ready` 和 `full_research_ready` |
| `tiger.process-observation/v1` | 用 Darwin `libproc` 或 Linux `waitid` 证明进程组和退出状态 |
| `tiger.source-quarantine/v1` | source attempt 的局部隔离 |
| `tiger.model-quarantine/v1` | model attempt 的局部隔离 |
| `tiger.validation-diagnostic/v1` | `RESULT_REJECTED` 的 validator、安全 schema 位置和原因码 |
| `tiger.run-progress/v1` | 原子进度投影，明确不是业务 receipt |
| `decision-format-v1` | 最终决策对 market/company source ID 的逐证券约束 |

## 失败语义

- unknown attempt 保留原 `attempt`、usage unknown 状态和 reservation；同请求永久不重发。
- quarantine 只影响其 `affected_targets`。受影响证券不得 BUY；全局宏观缺口只允许
  WAIT/AVOID。其他 target 可以继续。
- 文档 schema/格式失败在 v4 直接形成局部 `unverifiable`，不消费全局格式重答。
- `completion_tokens >= output_ceiling` 一律为 `MODEL_OUTPUT_EXHAUSTED`。
- provider 批次使用同一绝对 hard deadline；完成子集保留，未完成 symbol 进入下一真实源，
  不从已完成结果外推，也不使用合成源。
- SEC `robots.txt` 与原文请求使用同一已审定操作者 User-Agent；该身份只发往
  `sec.gov` 及其子域，不泄漏到其他来源。
- v4 不接受 `completed_degraded`。只有 `completed`、`receipt.operation_ok=true`、
  `verify.operation_ok=true`、`verification.ok=true`、相同 receipt hash、非空 summary
  和完整五阶段证据同时成立才算完成。

## 安装与 doctor

`scripts/setup.sh backend` 只准备 Python 后端；`scripts/setup.sh full` 额外要求
Node 24+ 和 pnpm，并执行 `pnpm --dir governance install --frozen-lockfile --ignore-scripts`。
两者均要求 Python 3.11+，Python 依赖从 `requirements.lock` 以 `--require-hashes` 安装。
`setup` 写入机器可读 `baseline.json`。

`doctor.sh` 默认硬失败，不吞掉错误。`doctor` 的 `ready` 只在完整研究条件成立时返回；
运行时和模型可用但 SEC 操作者身份缺失时返回 `reduced_evidence`，该状态禁止 BUY。

## 验收

离线必须覆盖：

- 新旧 Tiger 身份、未知身份和错误 MCP 入口；
- 缺 Node、Python 3.10、Python 3.11/3.12 和 doctor 失败；
- Darwin live/zombie/ESRCH/query failure 及不依赖 `/bin/ps`；
- source/model quarantine、reservation 保留、同请求不重发和无关 target 继续；
- 393216/393216 output exhaustion、final decision 跨证券引用拒绝；
- provider hard deadline、完成子集、progress 和 receipt 不可替代；
- v3 prompt/hash 保持冻结，v4 使用新合同。

真实云端验收使用全新 state-root、公开数据和真实 DeepSeek Flash。故障注入轮先证明局部隔离；
实时轮必须由 receipt/verify 签收。任何缺少真实模型、SEC 身份、原文证据或未知用量的结果
都不能报告 BUY 或业务完成。
