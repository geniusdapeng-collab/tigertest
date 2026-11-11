# Tiger 大运行可靠性与数据覆盖发布（2026-11-11）

任务：`T-2026-1111-0002`

任务卡：GitHub Issue #73

审计基线：`d0176e8b98dff575e5638e43c203b64c6aab1dc9`

## 目标

修复默认 `us-public-intelligence-paper-v2` 在大文档工作图和 7,200 秒业务预算下暴露的六项运行问题。改动仍只服务公开数据、本地 paper 研究和可恢复执行，不开放真实券商、实盘或交易阈值变更。

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
  `INVALID_REQUEST` 等永久错误立即停止。最终 receipt proof 使用 15 分钟
  单次预算，并可在总 wait deadline 内重试，且永不重新分配 run identity。

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

本地验证证明代码、协议、证据链和夹具行为；真实 v2 长运行、候选 CI、
`main` 合并和 Release 字节回读由任务回执继续记录。
