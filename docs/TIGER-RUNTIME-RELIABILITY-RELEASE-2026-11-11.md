# Tiger 大运行可靠性与数据覆盖发布（2026-11-11）

任务：`T-2026-1111-0002`

任务卡：GitHub Issue #73

审计基线：`d0176e8b98dff575e5638e43c203b64c6aab1dc9`

## 目标

修复默认 `us-public-intelligence-paper-v2` 在大文档工作图和 7,200 秒业务预算下暴露的六项运行问题。改动仍只服务公开数据、本地 paper 研究和可恢复执行，不开放真实券商、实盘或交易阈值变更。

真实大工作图验收又发现并修复了收尾阶段的重复证明放大，以及 HTTP 429
未决尝试的级联阻断，因此发布共闭环八项问题。

## 修复

### 1. 调度热路径按依赖索引领取

- `WorkStore.claim()` 使用 SQLite JSON 依赖查询、`units(state)` 和
  `attempts(state,unit_id)` 索引，只读取下一条合法候选，不再先加载全部
  validated unit 和全部 unit 投影。
- Coordinator 为 scope 保存状态投影，只刷新完成的 future、过期 lease 和
  显式状态变更；compact graph summary 最多每 250ms 刷新一次。
- `read_result()` 只加载目标 unit 的依赖闭包和终态原件，不再在每次 join
  时解析整张工作图。
- 回归断言在 2,800 个 unit、连续 64 次 claim 的情况下，完整 unit 投影读取
  次数必须为 0。

### 2. SQLite 写锁有界重试

- 写事务使用 250ms 单次锁等待，并在 10 秒总预算内重试
  `SQLITE_BUSY/LOCKED`。
- 只重试尚未进入事务体的锁获取和可安全重试的 `COMMIT`；handler、外部调用
  或事务体异常不会被整段重放。
- Coordinator 对调度层可安全重试的 `STORE_BUSY` 再做 5 秒指数退避，
  避免一次瞬时争用扩大为整轮语义失败。

### 3. 大文档图并发容量

- 新冻结策略把 `document_workers` 从 4 提高到 16，
  `parser_workers` 从 2 提高到 4。
- 实际并发仍受 `max_active_input_tokens=1048576`、
  `max_active_memory_bytes=3 GiB`、来源预算和原业务 deadline 约束。
- 之前冻结的 4-worker 策略继续可读、可恢复；不会因升级策略而拒绝历史 run。

### 4. 快速 status 与等待重试

- 完整 `verify_run` 成功后，发布阶段生成 receipt-bound
  `research-status-summary.json`。默认 status 只校验 committed pointer、
  receipt hash 和该紧凑投影，不再重跑全量证据证明。
- `receipt` / `verify` 仍是 authoritative full proof；status 的
  `completion_proof.full_proof_command` 明确指向 `verify`。
- `run --wait` 对只读 status 的 60 秒观察超时进行指数退避重试；
  `INVALID_REQUEST` 等永久错误立即停止。最终 receipt proof 使用 90 分钟
  单次预算，总 wait 上限为 4 小时，可在总 deadline 内重试，且永不重新
  分配 run identity；7,200 秒业务截止不因观察窗口延长。

### 5. 行情补洞与失效股票池隔离

- `signal_stocks` 在达到最低覆盖率后，若缺口不超过 64 个，仍会走可信真实源
  定向补洞；不生成合成数据。
- 25 个已不在当前 NASDAQ Trader 普通股目录中的内嵌池代码被列入
  `RETIRED_UNIVERSE`，从 core/extended/focused 和产业链批量分母中排除。
- 报告新增 `retired_or_renamed_pruned` 数据说明；显式 `file` / `full`
  模式不被静默改写。

### 6. WAIT 决策投影

- completed、证据充分、模型五阶段完成且记录动作为 `WAIT` 时，
  `research_status.axes.decision.state` 投影为 `WAIT`。
- `WAIT` 不再降级为 `unknown`，也不会伪造成 BUY、HOLD 或
  `WAIT_EVIDENCE`。

### 7. 收尾复用同一份完整证明

- supervisor 不再在进入 `seal_and_publish` 前单独重算 parent history；发布阶段
  先生成一次 stage proof，再将其显式传给 parent history、receipt 生成、发布前
  verify 和发布后 readback。
- receipt 仍逐字节核验 parent history 和 canonical events；省略的是同一锁定进程
  内对同一批不可变证据的重复重放，不是完整性检查。
- 回归断言确认对已准备好的 stage proof 和 parent history 不会再次调用
  `verify_events` 或完整 history proof。

### 8. 429 有界重试与未决尝试隔离

- 同一 durable chat attempt 对 HTTP 429 和可重试传输失败做最多两次指数退避；
  同一 wire 不重复生成 `dispatch_started` 事件。
- 全局并发 chat 发送上限为 8，仍高于旧的 4-worker 上限，但避免 16 个文档
  worker 同时冲击提供方 rate limit。
- 单个 chat envelope 的读取超时提高到 240 秒、总响应预算提高到 900 秒，
  避免高负载下仍在持续传输的 200 响应被旧 480 秒截止误判为未知 usage。
- 重试后仍收到完整非 2xx envelope 时，按“未产生模型 completion”的零 usage
  结算，只影响本单元，不再让全局未决检查把其他并发单元级联打成
  `ATTEMPT_UNKNOWN`。

## 兼容与回滚

- WorkStore schema 版本保持 `1`；只新增可重复创建的查询索引，不改事件或
  结果 envelope。
- 新策略和上一版 4-worker 策略都进入只读接受列表；历史 run 不升级合同。
- 快速状态投影是 receipt-bound 派生视图，不替代 receipt/verify。
- 发布回滚使用本任务普通 revert；代码内部按四组边界组织，便于定向审查：
  - 调度索引与写锁重试；
  - 并发策略；
  - 快速状态与 CLI wait；
  - 股票池、补洞与 WAIT 投影。

## 验证

- 本地全量 Python：`3739 passed, 7 skipped`，耗时
  `1204.91s`，只有 4 条既有 multiprocessing `fork()` 警告。
- WorkStore/Coordinator/child context/recovery/resource admission：
  `62 passed`。
- Research work execution：`8 passed`。
- Research work proof：`68 passed`。
- Parent resume/public recovery/followup：`31 passed`。
- Agent runtime/evidence/audit：`112 passed`。
- Node CLI、workflow client 与新增 wait 重试：`40 passed`。
- Parent-history proof 复用回归：`17 passed`；Agent runtime：`43 passed`。

本地验证证明代码、协议、证据链和夹具行为；真实 v2 长运行、候选 CI、
`main` 合并和 Release 字节回读由任务回执继续记录。
