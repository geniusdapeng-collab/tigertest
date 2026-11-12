# Tiger 外部审计 13 项完整修复计划

> 任务：`T-2026-1111-0001`  
> 任务卡：GitHub Issue #71  
> 仓库：`geniusdapeng-collab/workroom-tiger`  
> 分支：`task/T-2026-1111-0001`  
> 审计基线：`ebecb0f8ed46885f80598de7117369ee5ba322cf`  
> 状态：已形成完整计划，等待按计划落失败断言和实施代码  
> 环境：Tiger 隔离运行 `20261110T162139Z-tiger-bd52ef`

## 1. 目标与完成定义

逐项修复外部审计确认的 13 项缺陷：

- P0：MC-001、MC-002、MC-003、MC-004、MC-005
- P1：MC-006、MC-007、MC-008、MC-009
- P2：MC-010、MC-011、MC-012、MC-013

完成必须同时满足：

1. 13 张 MCD 问题卡均有 `文件:行号@完整 commit` 和可执行失败断言。
2. 每项先记录失败，再由修复后断言转绿。
3. P0/P1 由未参与修复的独立验收角色在干净完整提交上复跑并转为 `verified`。
4. 全量 Python、Node、治理和秘密扫描门禁通过。
5. GitHub PR 的精确 head/base 和原仓 CI 通过。
6. 普通 merge 后回读 `main` SHA/tree，并完成源码 Release 与资产摘要回读。
7. 不把“代码已合并”写成“真实 237 业务闭环已完成”。

## 2. 边界与非目标

### 2.1 必须守住

- 仅纸面/模拟盘；不连接券商，不提交真实订单。
- 不轮换或持久化任何秘密。
- 不通过删除文档、删除证据或重写哈希来规避 closeout 失败。
- 不把未知付费发送当成“没有发生”或“费用为零”。
- 不把观察断开、调度取消和外部进程强制终止混为同一状态。
- 状态加速必须读取可验证的增量投影；完整证明保留给 `receipt`/`verify`。
- 历史 bundle 兼容只能窄范围、可证明、不可扩展到新运行。

### 2.2 本轮不做

- 不完成真实 237 全链业务闭环；该边界继续由 Issue #65 跟踪。
- 不完成真实金融金标、真实 cohort、14 天观察等业务质量门禁。
- 不修改 `main`、Release 之外的部署环境或券商账户。
- 不以 UI 视觉改版替代运行协议修复。

## 3. 系统不变量

| ID | 不变量 | 本轮约束 |
|---|---|---|
| INV-1 | 外部或变更性动作先经过权限、围栏和审批 | 仅修改研究运行时，不开放新动作权限 |
| INV-2 | 证据和回执只增不改 | 未知 attempt、reservation、失败证据必须保留 |
| INV-3 | 无真实回执不算完成 | closeout、receipt、verify 必须区分 engine 与 publication |
| INV-4 | 模拟与真实、观察与业务完成必须分明 | 取消观察不等于业务取消；源码发布不等于业务闭环 |
| INV-5 | 秘密不得进入代码、文档、日志或 Release | GitHub token 仅单条命令进程内使用 |
| INV-6 | 高风险金融动作必须单独授权 | 本轮不接真实资金和券商 |
| INV-7 | 历史证据不得被静默升级 | parser 兼容必须同时验证原文、文本、时间、URL 和附件 |
| INV-8 | 预算和计费未知不可清零 | unknown usage 保留 reservation，最多影响单一 cell/document |

## 4. 13 项修复卡

### MC-001 / P0 / 审计 P0-1：脱敏后 parser 配置哈希漂移

**现状证据**

- `service.py` 只对 `tiger-main-text/v1` 和 `tiger-scrapling/v1` 使用脱敏后的最终字节重新解析。
- `tiger.structure-text/v1` 在脱敏后仍保留脱敏前 metadata。
- `replay.py` 从最终落盘字节重放时得到新的 parser config hash。
- 当前可确定性复现：
  - recorded：`54b0a89b0ff0eb00f1deb0c1b727c2f4169ad1a5b1ac8b1150d6f3b600f648b7`
  - replayed：`b3b203ec68d4ae8c24dd25ec19c2097a86b9b76805217c14b7fa14b51eb74557`

**实施**

1. 将“是否需要从最终落盘字节重新规范化”从固定 extractor 白名单改为按内容类型和字节变化判断。
2. 所有落盘 HTML、XHTML、纯文本、JSON/text 数据在发生秘密脱敏后，都从最终字节重新提取；PDF/二进制不伪造可重解析能力。
3. `structure-text/v1` 使用同一显式 parse policy 重新生成 parse manifest 和 extractor config hash。
4. 若最终字节重解析失败，记录结构化 parser terminal，不回退到脱敏前 metadata。
5. `replay.py` 增加一条窄兼容规则，仅用于已冻结历史 bundle：
   - recorded extractor 必须是 `tiger.structure-text/v1`
   - recorded config 必须等于 `digest({"parser": recorded_parser, "previous": null})`
   - observed config 必须等于 `digest({"parser": observed_parser, "previous": observed_underlying_config})`
   - 必须同时证明 text、raw hash、title、published/updated time、canonical URL、attachments 全部一致
   - 显式 policy 不能成为通用豁免

**失败断言**

- 构造 `structure-text/v1 + 显式 parse policy + URL/secret redaction` 的采集 fixture。
- 旧实现必须因 recorded/replayed config 不一致而失败。
- 修复后 acquisition 必须保存最终字节对应的 config，并可在 closeout replay。
- 另构造 text/time/title/attachments/policy 任一变化，历史兼容必须仍拒绝。

**验收**

- 新运行无历史兼容标记。
- 精确复现的两个历史 hash 只在完整等价证明下被接受。
- 不允许“hash 不同就删文档”或“所有显式 policy 都放行”。

**回滚**

- 回退 parser reparse 与窄兼容两个独立提交；旧冻结 bundle 保持只读。

### MC-002 / P0 / 审计 P0-2：paid Search/Fetch unknown 终止整轮

**现状证据**

- `service.py` 的 `_query()` 在 paid source 返回 `delivery=unknown` 后抛 `RECONCILIATION_REQUIRED`。
- `_collect_entry()` 不吸收该错误，整个 `collect()` 退出。
- cell 已写 gap，但 job 停在 running。

**实施**

1. paid Search/Fetch 对 HTTP 408/429/5xx 使用有界退避重试，受原 deadline、请求和字节预算约束。
2. 每次 attempt 都保留 provider、delivery、HTTP status、retry-after、usage 和 reservation。
3. 无法由已知 HTTP 响应证明“未计费”的 unknown，最多按显式 policy 重试一次；客户端部分收到 200/headers/body 后断流时不得伪装成未发送。
4. 达到上限后，把受影响 cell 写成新终态 `source_unresolved`，保留 `source_diagnostics`。
5. `_collect_entry()` 吸收这种单元失败并继续其他独立 entry。
6. 最终允许 `partial evidence + WAIT/AVOID`；不得允许 BUY，也不得把 unknown usage 写成 0。
7. resume 可以继续其他未完成 unit，但不得静默释放或改写未知 attempt。

**失败断言**

- 建立一个源码 unknown、另一个源码正常的 fixture。
- 旧实现整轮抛错；修复后第一 cell 为 `source_unresolved`，第二 cell 正常完成。
- usage 仍有 `requires_reconciliation=true`，原始 attempt 和 reservation 仍存在。
- 最终研究动作不能为 BUY。

**验收**

- 单点付费故障的爆炸半径限制在受影响 cell。
- 不存在换 request hash 自动重发未知请求的路径。

**回滚**

- 保留新状态投影数据的向下读取兼容；回退 dispatch 分支只恢复旧单 cell 失败行为。

### MC-003 / P0 / 审计 P0-3：chat 网络未知把 worker 和 resume 钉死

**现状证据**

- `deepseek_required.py` 在 dispatch 后才发 chat。
- Timeout/TransportFailure 直接映射为 `NETWORK_TIMEOUT`/`TRANSPORT_FAILURE`。
- attempt 状态保留 dispatched/unknown 后，`research_resume.py` 永久拒绝。

**实施**

1. chat 调用分类处理：
   - 可证明未发出：安全有限重试。
   - HTTP 429/500/503：按官方建议和 retry-after 有界退避。
   - 已发出后的读超时/连接中断：保留 unknown usage，最多按显式预算 policy 重试一次。
2. 持久化每次 retry diagnostic 和 attempt identity。
3. 对某文档最终无法收敛的模型调用引入 `model_unresolved` 终态：
   - 保留 unknown usage/reservation。
   - 文档不得进入 scoring_documents。
   - 后续文档可继续处理。
4. 若最终决策依赖该文档，只能 WAIT/AVOID/证据不足。
5. resume 不再因一个已终态的 document 永久阻塞无关文档；未终态 unknown 仍要求先 reconcile。

**失败断言**

- 模拟一次 post-dispatch timeout。
- 修复后失败分类、重试次数和 unknown usage 均可回读。
- 文档变 `model_unresolved`，其他文档继续，最终 action 不为 BUY。
- 第二次 resume 不得重复未知调用。

**验收**

- 不把未知计费写成 0。
- 不把“文档不可用”偷换成“整轮不可恢复”。

**回滚**

- 回退 chat retry policy 和 document terminal 两个提交；已落盘 unknown evidence 保持可读。

### MC-004 / P0 / 审计 P0-4：closeout 共用 deadline 且重复全量证明

**现状证据**

- engine 和 closeout 共用 business deadline。
- `business_summary` 在一次 publication 中实际调用 3 次。
- `verify_run` 被重复调用，每次都重建 summary/model proof。

**实施**

1. runtime input 增加 `closeout_reserve_seconds`，保持向后兼容默认值。
2. admission 生成 `analysis_deadline` 与 `closeout_deadline`；engine 只能使用 analysis 预算。
3. 增加一次性 prepared publication proof：
   - receipt
   - summary
   - bundle/work proof
   - event tail
   - account version
   - prepared pointer
4. 发布前构造一次 proof；pointer 写入后只回读 pointer、receipt hash、version hash 和 durability。
5. Engine 在 analysis deadline 前完成时，closeout 仍可在 reserve 内发布。
6. Engine 超时和 closeout 超时分别记录，不互相伪造。

**失败断言**

- 统计一次完整 seal 的 `business_summary` 调用次数，要求为 1。
- 构造 engine 在 analysis deadline 结束后、总 deadline 前完成的 fixture，要求 closeout 可发布。
- 构造 engine 消耗全部预算的 fixture，要求明确 ANALYSIS_DEADLINE_EXCEEDED，而不是进入 closeout。

**验收**

- 不通过无限延长 deadline 规避问题。
- 重复 seal 必须幂等复用 prepared proof。

**回滚**

- 新增 reserve 字段兼容缺失；回退 prepared proof 时继续读取旧 receipt 结构。

### MC-005 / P0 / 审计 P0-5：status 执行昂贵证明且客户端误判瞬时失败

**现状证据**

- `agent_runner.py` 的 status 打开 `WorkStore`。
- `WorkStore.open()` 立即完整 verify。
- status 随后再次读取并哈希 validated results。
- `workflow-client.mjs` 遇到任何 `operation_ok=false` 立即终止等待。

**实施**

1. 持久化原子 `status-projection.json`，包含：
   - schema/run_id/revision
   - event tail hash
   - graph state
   - unit/document counts
   - stage
   - usage summary
   - block reasons
   - updated_at
2. 每次任务状态和事件提交原子刷新投影。
3. status 默认只读投影，不打开/verify 整个 WorkStore。
4. receipt/verify 继续执行完整证明。
5. 客户端轮询采用指数退避；只对稳定、不可恢复的错误码立即停止。
6. 投影缺失或哈希不满足时明确返回 `STATUS_PROJECTION_UNAVAILABLE`，不得假装绿色。

**失败断言**

- 构造大 WorkStore，status 不调用 `_verified_material_set`/WorkStore.open 的完整 verify。
- 投影 revision 和 event tail 在事件提交后原子更新。
- 客户端对瞬时 transport/5xx 继续轮询；对 `INVALID_REQUEST` 等永久错误立即停止。

**验收**

- status 与 verify 分离。
- status 输出不含完整证据集合。

**回滚**

- status 可回退到完整 verify，但客户端退避与永久错误分类可独立保留。

### MC-006 / P1 / 审计 P1-6：resume 准备固定 45 秒上限

**现状证据**

- `agent_runner.py` 把 `prepare_resume` 全部包在 45 秒 `PreparationDeadline`。
- 归档恢复、WorkStore 打开和全量 verify 都可能占满 45 秒。

**实施**

1. 45 秒只覆盖锁、身份、快速前缀检查和 recovery 准入。
2. 引入独立 recovery phase 和 incremental verification checkpoint。
3. checkpoint 绑定 commit/event tail/material manifest hash。
4. 完整 proof 未成立前不得发送任何新外部请求。
5. 超过 45 秒时返回 `RECOVERY_CONTINUES`/可恢复状态，而不是错误假设准备失败。

**失败断言**

- 模拟慢归档/大 WorkStore，超过 45 秒但 checkpoint 仍有效，恢复流程不丢失原始身份。
- checkpoint 损坏或 event tail 漂移时拒绝恢复。

**回滚**

- 移除 checkpoint 后仍可按旧全量恢复，但不重新启用固定 45 秒硬失败。

### MC-007 / P1 / 审计 P1-7：closeout 诊断被抹平

**现状证据**

- `agent_supervisor.py` 的 `error_record` 为未知异常生成泛化错误。
- `details_ref` 固定指向可能仍为 `success=true` 的 `engine-result.json`。
- intelligence proof 把 parser/bundle 错误统一成 `MODEL_REQUIRED_FAILED`。

**实施**

1. 新增 `closeout-error.json`，字段包括：
   - schema/run_id/stage/code/phase
   - artifact refs
   - failure document ID/hash
   - sanitized message
   - retryable
   - engine_result_sha256
2. parser replay 失败映射为 `INTELLIGENCE_BUNDLE_REPLAY_FAILED`。
3. 模型失败才映射为 `MODEL_REQUIRED_FAILED`。
4. status 分开显示 `engine_result` 和 `closeout` 两个子状态。
5. closeout 失败不覆盖 engine success 事实。

**失败断言**

- 构造 parser replay 失败，错误 code 必须是 `INTELLIGENCE_BUNDLE_REPLAY_FAILED`。
- `closeout-error.json` 的 engine result hash 与真实 engine-result 一致。
- status 同时保留 `engine_result=success` 与 `closeout=failed`。

**回滚**

- 旧 status 客户端仍可读子状态缺失时的扁平错误。

### MC-008 / P1 / 审计 P1-8：scheduler 重复全量扫描

**现状证据**

- 每次 claim 先 `resume_plan()`。
- `resume_plan()` 重读并重哈希全部 validated results。
- 受限 worker 模式又调用一次 resume/status。
- attempt 查询扫描整个 attempts 表。

**实施**

1. 增加 validated dependency index 与 ready-set cache。
2. cache key 绑定 `plan_hash + event_tail_sha256`。
3. 每轮 scheduler tick 只构建一次调度视图。
4. `claim_ready()` 只读目标 unit 的依赖记录。
5. 为 `attempts(state, unit_id)` 增加索引/查询路径。
6. ready cache 失效后从权威账本重建，不使用陈旧绿色。

**失败断言**

- 多个 claim 连续执行时，validated material 不应每次全部重哈希。
- event tail 改变后 cache 必须失效。
- 受限 worker 模式每次 tick 最多重建一次视图。

**验收**

- 普通路径至少消除二次方级重复工作；不把未经 profile 的 O(N³) 当作事实。

**回滚**

- 删除缓存路径后仍能回退到正确但较慢的 resume plan。

### MC-009 / P1 / 审计 P1-9：CLI wait 上限与后端 deadline 不一致

**实施**

1. 根据后端 `MAX_DEADLINE_SECONDS` 和 closeout/preparation margin 计算 CLI 最大 wait。
2. `--wait-timeout-seconds` 允许覆盖最大 7200 秒业务预算。
3. 默认 wait 仍可在没有显式值时按 describe 动态推导。
4. 无 `--wait` 继续返回 run_id，由客户端长轮询。

**失败断言**

- 7200 秒配置合法。
- 超过后端最大预算加 margin 的值明确拒绝。
- 默认 wait 覆盖 5400/7200 秒配置。

**回滚**

- 保留 1–3600 秒输入的兼容解析，但对合法更长预算不再拒绝。

### MC-010 / P2 / 审计 P1-10：guardian 等待继承管道 EOF

**实施**

1. direct child 退出后启动固定 drain grace（默认 1.5 秒）。
2. grace 结束后关闭读端并清理 owned process group。
3. process receipt 记录 `group_cleanup_before_reap`、`drain_timed_out`、退出时间差。
4. 不等待业务 deadline，不持有无关孙进程管道。

**失败断言**

- 构造继承 stdout/stderr 的孙进程。
- 修复后 guardian 在 direct child 退出后固定 grace 内结算。
- 证据记录 drain grace 超时和 group cleanup。

**回滚**

- 可回退到等待 EOF，但必须保留 deadline 上限和清理证据。

### MC-011 / P1/P2 / 审计 P1-11：cancel 不暴露在途 inventory

**实施**

1. 明确区分：
   - `cancel_scheduling`
   - `cancelling`
   - `force_cancel_process`
2. 默认 cancel 立即停止新 dispatch，并返回 active unit/attempt inventory。
3. status 显示 `cancelling`、剩余 unit、未知 attempt。
4. 强制取消只在单独、明确模式下终止 owned process group。
5. 已发出未知请求仍须 reconciliation，不能宣传为已撤销。
6. `Coordinator.close(timeout=...)` 返回未完成 inventory，不无限等待。

**失败断言**

- cancel 后 200ms 不再启动新 unit。
- 返回 active unit inventory。
- 最终状态区分 `cancelling` 与强制进程结束。

**回滚**

- 旧客户端可忽略 additions，继续读取原有 cancel_requested。

### MC-012 / P2 / 审计 P2-12：status/evidence 输出过大

**实施**

1. status 默认 `detail=summary`，只返回 axes/counts/block reasons。
2. 增加 `detail=cards|sources` 和 cursor/limit。
3. cards/sources 分页，单项有稳定 ID。
4. receipt/verify 保留完整不可变证据。
5. 默认 status 目标小于 256 KiB。

**失败断言**

- 默认输出不含全部 securities/sources。
- cards/sources 分页无重复/遗漏。
- cursor 有稳定排序。
- 默认 fixture 输出小于 256 KiB。

**回滚**

- 可通过显式 detail 请求恢复完整卡片视图。

### MC-013 / P2 / 审计 P2-13：setup/doctor/stdin UX

**实施**

1. `doctor.sh` 统一调用正式 `tiger-agent doctor`/`describe`。
2. 优先使用当前 `.venv` Python 或可信 runner，不固定系统 `python3`。
3. 检查 DeepSeek `LLM_API_KEY` 和固定 backend/base URL/model，移除 Kimi 文案。
4. 非交互命令允许从普通 stdin 读取唯一 JSON 请求；多 JSON/空输入明确报错。
5. 保留 operation 必须为 argv[0] 的正常 CLI 设计，不把它当缺陷。

**失败断言**

- doctor 识别 `.venv` Python 与 DeepSeek 合同。
- 管道 JSON 在省略 `--input-file -` 时可用。
- 空/多 JSON 输入返回明确 `INVALID_REQUEST`。

**回滚**

- 保留显式 `--input-file -` 作为兼容入口。

## 5. 实施批次

### 批次 0：冻结失败与证据合同

1. 建立 13 张 MCD 问题卡。
2. 为每张卡指定精确 root cause 文件和断言。
3. 在未修改业务代码时记录失败结果。
4. 固定审计基线 `ebecb0f`。

### 批次 1：止血

- MC-001 parser 最终字节一致性与窄兼容
- MC-002 source unknown 单元终态
- MC-003 model unknown 单元终态
- MC-004 closeout reserve 与 prepared proof

### 批次 2：地基与正确性

- MC-005 status projection
- MC-006 incremental recovery
- MC-007 structured closeout diagnostics
- MC-008 scheduler index/cache

### 批次 3：运行边界与体验

- MC-009 CLI wait
- MC-010 guardian drain grace
- MC-011 cancellation inventory
- MC-012 status pagination
- MC-013 doctor/stdin

## 6. 兼容与迁移

1. 新运行只接受新合同；历史 bundle 不得被静默重写。
2. `structure-text` 历史兼容必须同时验证全部非 parser metadata。
3. `source_unresolved`、`model_unresolved`、`cancelling`、`force_cancel_process` 是新增状态；旧客户端未知时按未完成处理。
4. `closeout_reserve_seconds` 缺失时使用默认 reserve。
5. `status-projection.json` 缺失时返回明确不可用；可显式 fallback 到完整 verify，但不得默认。
6. scheduler cache 只能优化读取，不能成为第二事实源。

## 7. 验证矩阵

### 定向

每张卡一个或一组 pytest/node test，先失败后通过。

### 回归

- intelligence parser/replay/proof/source/status
- agent runtime/supervisor/runner/recovery
- tasks store/scheduler/context
- CLI/MCP 契约
- MCD scripts

### 全量

- Python：仓库标准 pytest
- Node：`scripts/*.test.mjs` 与 CLI/MCP
- 治理：commit-msg、lock、secret scan、product/bundle/UI 相关门禁
- 精确候选 CI
- main push CI

### 独立验收

- P0/P1 全部由 acceptance role 在干净完整提交重跑。
- 修复者角色和验收角色必须不同。
- P2 如未闭环，必须有明确 regression command；默认目标是全部 verified。

## 8. 发布与回读

1. 提交按批次拆分，提交信息带 `[T-2026-1111-0001]`。
2. 推送 `task/T-2026-1111-0001`，创建 PR 并绑定 Issue #71。
3. 精确 head/base 完整 review 和测试后，普通 merge。
4. 回读 `main` SHA/tree、双亲与 main CI。
5. 发布不可变源码 Release，包含：
   - source tarball
   - manifest
   - verification/summary
   - SHA256SUMS
6. 远程下载每个资产并核对摘要。
7. Issue #71 写五行回执并关闭。

## 9. 风险与冲突面

| 风险 | 控制 |
|---|---|
| parser 兼容放得过宽 | 两条精确 hash + 全字段等价，仅历史 bundle |
| unknown 状态破坏旧消费方 | 新增状态 + 旧真相仍保留；未知按不可完成读取 |
| status cache 掩盖新错误 | cache key 绑定 event tail；verify 仍权威 |
| closeout proof 复用旧证据 | prepared proof 绑定当前 commit、event tail、source tree |
| cancel 被误写为外部撤销 | 强制取消单独模式，unknown request 保留 |
| 大规模跨文件改动引入回归 | 四批提交，每批定向测试后再扩全量 |

主要冲突文件：

- `trading_system/intelligence/service.py`
- `trading_system/intelligence/replay.py`
- `trading_system/llm/deepseek_required.py`
- `trading_system/llm/intelligence_proof.py`
- `trading_system/agent_runtime.py`
- `trading_system/agent_supervisor.py`
- `trading_system/agent_runner.py`
- `trading_system/tasks/store.py`
- `trading_system/tasks/scheduler.py`
- `trading_system/tasks/context.py`
- `trading_system/research_status.py`
- `bundles/trading/capabilities/workflow-client.mjs`
- `scripts/tiger-agent.mjs`
- `scripts/doctor.sh`

## 10. 计划完成清单

- [ ] 13 张 MCD 问题卡已上账
- [ ] 13 张卡的失败断言已记录
- [ ] 批次 1 全部转绿
- [ ] 批次 2 全部转绿
- [ ] 批次 3 全部转绿
- [ ] 全量 Python 门禁通过
- [ ] Node/CLI/MCP 门禁通过
- [ ] P0/P1 独立 acceptance verified
- [ ] PR 精确候选 CI 通过
- [ ] main 普通 merge 和 main CI 回读
- [ ] Release 与资产摘要回读
- [ ] Issue #71 回执与关单
