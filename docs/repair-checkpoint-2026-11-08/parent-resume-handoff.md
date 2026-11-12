# 工作单元与父流程恢复：停止实施时交接

停止依据：产品所有者最新明确要求因额度见底，停止新增实施和扩展测试，固化成果并详细交接。已按该指令停止；本交接没有运行新测试、没有操作共享 Git index、没有新增 provider/付费调用或 GitHub 操作。

## 覆盖率声明

- 本交接纳入 38 个相关路径，完整清单见 `reading-and-hash-inventory.json`，其中是否实际存在由各行 `exists` 表示。
- 修改前或此前完整阅读：22；本次新编写、未完成最终独立审查：3；仅相关函数/局部阅读：5；本父流程子任务未完整阅读：8。
- 全部路径做了停止点文件存在性/字节数/行数/SHA-256 库存。这是 **B 级机检**，不等于代码理解度；不能把停止点哈希盘点冒充“对该最终 SHA 全量精读”。
- 22 份完整阅读是基线阅读事实。其后本 Agent 或其他 owner 继续修改的文件，没有进行最终全文件复读验收。JSON 明确将 `current_hash_is_historical_full_read_receipt` 设为 false，避免把后来字节错误归入早前阅读回执。
- 原工作单元阶段的逐文件完整阅读旧哈希及子 Agent 阅读回执仍保存在 `../work-units/STATE.json` 和 `../work-units/CONTINUATION.md`。新父流程文件完整、独立审查尚未完成。

## 证据分级

- **A**：实际代码阅读得出的局部实现结论；下面会明确是哪一阶段和哪些函数。
- **B**：停止点 Git/哈希库存、实际测试输出和测量数据；只证明对应检查，不外推到业务生产成功。
- **C**：只读取了某个文件的相关函数或变更片段，不能视为全文件理解。
- **D**：尚未实施、尚未运行或尚未核实的设计和后续步骤。

## 先读结论

**工作单元基础及其子级模型恢复已经提交并交给 root；正式 same-run parent resume 和 research.supplement 公共入口尚未接线。不能把当前 WIP 当作 EP09/17/18 完成交付。**

- **A/B**：正式 Python 与 Node `OPERATIONS` 仍是 doctor / describe / run / status / receipt / verify。没有 resume 或 research.supplement 操作，没有对应 CLI/MCP/schema 入口。
- **A/B**：`agent_supervisor.engine()` 仍在原位置构造 `ExecutionContext(store=EvidenceStore(run, run.name))`。没有调用新 `research_resume.execution_context()`，没有父 attempt start/IO journal/归档/closeout 的正式接线。
- **A/B**：新 `research_resume.py`、`research_replay.py` 只完成了部分基础实现及低层测试，尚未形成正式入口到实际业务的恢复闭环。
- **D**：未做新父流程公共入口验收、真实独立进程恢复、完整同任务五阶段业务/receipt 验收、补研新任务入口验收、整仓最终 CI。没有本轮父流程恢复的生产通过声明。

## 固化成果的位置

- 活动 root 仓：`/Users/mac/Documents/Codex/2026-11-04/ni/work/tiger-intel-capacity-implementation-20261106/source`。
- 停止点源码包：`parent-resume-WIP-20261108-032750.zip`。它只包含本子任务直接涉及的 9 个文件及库存/patch，不包含运行状态、环境凭据、账户数据或整个仓库。
- ZIP SHA-256：`679c0e0d594e9e24daaa6d04729541746da76cce836a2d1e712f10a5a34bfe7d`。
- 停止点归档时 root HEAD：`97aa30cd84d4d57043e7f2e503c59a1b7f12e332`。root 同时在固化其他 owner 成果，因此它不同于较早状态查询的 `393332eda711892f50d65ff957db746bacdff34f`；以库存 JSON 和 ZIP 为这一份快照的事实源。
- `owned-current.patch`：停止点 6 个已跟踪相关文件相对当时 HEAD 的 diff，**包含 root 原有 opt-in 改动，不能当作全是本 Agent 新写**。
- `reading-and-hash-inventory.json`：38 路径及当前字节数/行数/SHA-256、阅读类别。
- `transcribed-test-receipts.json`：已完成工具测试输出的明确转录，不是新测试，也不是事后伪造的 JUnit。
- 之前的阶段状态：`../work-units/CONTINUATION.md`；本次连续实施状态：`../parent-resume-state.md`。

## 已完成并提交的工作单元基础

下列为本 Agent 已交 root 的独立提交；root 已 cherry-pick，不应再重复应用这些依赖或重跑已经完成的检查来伪装新结果。

| 提交 | 实际内容 | 验证边界 |
| --- | --- | --- |
| `9b28f68` | 新 versioned SQLite WorkStore、不可变计划及 append-only extend_plan、CAS lease/epoch、外部 intent/dispatch/native/settlement、全 run 格式重答 CAS、UnitResult、isolated context/coordinator、tokens+memory 加权准入、完整原文 segment manifest | 初始 52 个 focused 用例；不等于正式 parent resume |
| `45931cc` | 原已 settled native 的纯重放、保留原 call_id/wire/schema、known observed→settle 窗口允许其他受限 worker | content 和 strict-tool 本地 transport/loopback 测试；没有重新调用 provider |
| `e7d58c6` | SQLite 最后 connection 关闭删除 WAL/SHM 的 lstat 竞态；actual transport hook 才 mark_dispatch；本地失败 prepared→not_sent；原 unknown reservation 不释放 | 两类确定性 sidecar 红→绿；与正式真实 client hook 依赖一起验过 |
| `91211c8` | first settled / second unsent 的混合恢复；before CAS、after CAS、after retry prepared 三窗口；同一 global CAS 恢复，first 不重发、second 实发一次 | content + strict-tool 真实 loopback 6 个窗口用例；原 native/arguments 未改 |
| `950518a` | 每个 recovery/read 操作一次一致性 graph snapshot、共享依赖 memo，消除按 DAG 路径重读全 plan/结果 | 最终 72/72 focused；所有 result 仍实际读 bytes、重算 hash 和绑定；无跨调用 verified-flag 缓存 |

对应旧近回归为 **162/162**，有 `../work-units/nearest-regression-v2.xml`。最后 focused 为 **72/72**，有 `../work-units/focused-v8-bounded-dag-validation.xml`。这些是相应子级实现及当时源的验证，不能外推为当前整个 dirty root 或所有新父流程代码通过。

独立 Agent `parse_quality` 的 WR-01 恢复 probe 在 `91211c8` 修复后通过，独立四个 test_task 文件加 probe 为 71/71，证据目录 `../independent-workstore-review/`。其审查版本不含后面未完成的 parent lifecycle。

### 性能事故及修复证据

- **B**：capacity 原规模离线 fixture 一次运行约 461 秒仍停在父 clean begin；只中断了它自己启动的离线测试，现场保留，没有动用户/provider run。
- **B**：cProfile 证实旧 `read_result` 对带一个依赖的单个结果用时约 0.037 秒，主要重复读取/解析完整 2.37 MB plan；逐节点/逐路径调用造成放大。
- **B**：`950518a` 后，对该现场 220 个 unit 中 131 个 reusable，一次 `resume_plan()` 实际约 0.334 秒。每个 reused artifact 仍逐个校验 hash 和全部绑定。
- **D**：capacity 的原规模完整业务/proof 再验由 capacity/root 持有；本 Agent 不声明那个最终证明已通过。

## 停止时 root 中本子任务直接涉及的文件

| 文件 | 当前状况和归属 | 验证程度 |
| --- | --- | --- |
| `trading_system/research_resume.py` | **NEW WIP**。显式 parent contract，attempt start/current pointer，原 admission 绑定，archive-intent→原工作/日志归档→归档 seal，原 account-before/calendar 恢复，未知外部尝试预检，父 history 与 canonical events 准备函数 | 仅部分低层功能测试；正式入口、完整 history/closeout 未验 |
| `trading_system/research_replay.py` | **NEW WIP**。closed provider typed decoder + normalize 往返；eager parent IO journal；父 epoch fence；provider observation cache；新父 EvidenceStore | 低层 typed replay/归档/拒写用例通过；完整独立进程/最终 history 未验 |
| `tests/test_research_parent_resume.py` | **NEW WIP 测试**。刻意没有完整 frozen source 的低层 fixture，不冒充公开业务或 financial receipt | 15/15；没有正式 public resume/supplement 测试 |
| `trading_system/tasks/store.py` | 此前已提交基础上，停止点新增 `attempts()`、按 unit 的 SQL 查询，以及 `recover_stopped_workers(verifier)` | 新 stopped-owner 回调路径未做完整 OS 锁所有权验收；不得直接开放给外部请求 |
| `trading_system/agent_evidence.py` | `verify_events(..., event_relative='events.jsonl')` 仅增加可选读取路径，默认仍旧 root/events；新 policy 的 path gate 放在 caller | 旧默认未主动放宽；新完整事件路径/history 发布未验 |
| `trading_system/agent_runtime.py` | root 原有 research_work opt-in 差异保留。此子任务加 requiredsource 新 helper，及 `validate_admission(..., validate_working_calendar=True)`，默认仍验证原 working calendar；恢复中断归档时计划用 False 仅跳过 working copy，原 frozen calendar record/source 仍验 | 私有 False 路径未接正式调用；receipt/version 幂等与 scope 绑定未实现 |
| `tests/test_agent_runtime_policy.py` | 仅修 prepared fixture，按实际 `freeze_source()[files]` 派生 `source_research_work_policy`，没有弱化 validate | 两个明确旧失败/读取测试通过 |
| `trading_system/agent_runner.py` | 本子任务没有落入新 entry 代码；保持 root 先前 opt-in 差异 | **resume/supplement 未实现** |
| `trading_system/agent_supervisor.py` | 本子任务没有落入 parent attempt 接线；保持 root 先前 attach_research_work 差异 | **仍旧 ExecutionContext/EvidenceStore** |

特别注意：root 的 frozen `_POLICY` 已包含 parent_attempt / provider_replay 标识，source required set 也已经包含新 WIP 文件，但正式 lifecycle 尚未接线。这是未完成的研发状态，不能因文件存在或 policy 有 marker 就声称任务可正式恢复。

## 新父流程阶段已实际运行的测试

解释器：`/Users/mac/Documents/Codex/2026-11-04/ni/work/tiger-cloud-run-20261104/python-venv/bin/python`。工作目录均为上述 root source。

| 命令/结果 | 事实及限制 |
| --- | --- |
| `python -B -m pytest tests/test_research_parent_resume.py -q --tb=short` → **15 passed in 0.62s**，exit 0 | full cell/dtype/aware-index provider replay；不导入任意类/Path；原 failed work 与 account-before 分开保留/恢复；archive 中途失败后继续；旧 parent epoch 拒写；model/source inflight 阻断且不移旧工作；原 deadline 不延长；旧 policy 不升级。**仅低层本地 FS/SQLite，不是公开入口或生产验收。** |
| `python -B -m pytest tests/test_deepseek_json_observation.py::test_new_frozen_source_rejects_coherently_rehashed_missing_contract tests/test_agent_runtime_policy.py::test_frozen_contract_reads_without_current_configuration_or_new_directories -q --tb=short` → **2 passed in 1.47s**，exit 0 | 修正 prepared fixture 的实际 frozen research_work 绑定。源码验证没有放宽。 |
| 首次新父低层 XML `../work-units/parent-lowlevel-initial.xml` → **4 failed / 11 passed** | 保留的历史 RED：factory 的 `policy` positional/kwargs 名称碰撞；改成 `frozen_policy` 后上述 15/15 绿。这个 XML **不是最终绿回执**，不要误引用。 |

最终 15/15 和 2/2 的原工具 stdout 已明确转录在 `transcribed-test-receipts.json`，附 tool chunk id。没有给未运行检查补造 XML。

停止时本 Agent 没有仍在运行的长测试；所有以上短测试已收结果。收到停止指令后没有新起测试。

## 已确定但尚未实施的接线方案

### Same-run parent resume（EP09/EP17）

1. Python/CLI 新 operation `resume`，严格 request 只 `{run_id}`；paper mode 才可执行；readonly 在打开输入文件/写状态前拒绝。旧 profile 或旧 frozen contract 拒绝升级。
2. 仍以 submission → account → run 的既有锁顺序、原 frozen source 的 supervisor 启动。active/committed 应幂等观察，不启动第二个 worker。真实 `stopped_verifier` 必须证明当前进程拥有原 account/run 锁及正确文件 inode；当前低层测试传入的 `lambda: None` 只是测试夹具，不能作为生产验证。
3. 完整校验原 admission/source/policy/account-before/calendar/原 deadline、实际 account pointer。pointer 与原 parent 不同则拒绝 CAS，不能重新播种账户或刷新 admitted_at。
4. 新 supervisor 在 durable acceptance 前建立/使用 parent attempt；engine 使用新 `research_resume.execution_context`，而不是旧固定 store；父 provider I/O 每次完成 eager checkpoint，恢复按 stage/method/index/exact input 复用原观测。新 ctx 不放松旧线程 owner。
5. 对原 WorkStore 的已知 observed native，先用实际 frozen native replay 核对 usage 才可 settle；任何 chat/source inflight/unknown 保留 hold 并阻塞新发送；永久业务失败保持失败。停机 fence 仅回收已证明停止的 worker，不能复活 nonretryable 业务。
6. 旧 failed attempt 的事件、IO journal、工作、日志、process/engine/accepted 通过 append-only archive 保留；fresh `work` 仅从原 account-before 和原 frozen calendar 重建。原 request、source、policy、clock、账户基线永不改。
7. engine failure 调用 capacity 已实现的 `verify_partial_intelligence_model(..., event_relative=current_attempt['events_path'])`；root/evidence 不变。完整 receipt 仍只校验 canonical root/events。
8. clean process exit 后才 `publish_parent_completion`，再走原完整 business/paper/model proof 和原 account CAS。需要把 history 标准 ref 绑定到新 contract receipt，并由 verify_run 独立复核；目前这些 runtime 接线没写。
9. engine 已成功而 seal 前崩溃：走 **closeout-only**，不再运行 pipeline。`materialize_version`/receipt 应改成新 contract 专属的 write-once-equal 幂等：已有 receipt 必须完整 `verify_run(require_committed=False)`，保留原 sealed_at，仍检查原 deadline/CAS，再提交 pointer；已 committed 只回读。旧 profile 仍保留历史拒绝/不可恢复语义。
10. `admission_guards` 的线程准备 wrapper 需要只对白名单固定 operation 泛化，保留原 45 秒 prep/cleanup 和 lost-ack 语义；没有任何自由 argv/source/model/deadline 参数开放。

候选方案已比较：选择“全父流程确定性重建 + frozen provider IO + durable unit/native 复用”；没有选择“序列化任意中间 pipeline 对象/放松旧 stage cursor”，后者扩大反序列化面和旧契约变更。

### Explicit supplement（EP18）

- 已与 root 对齐的 operation：`research.supplement`；MCP：`tiger_research_supplement`。
- 严格 request：`schema_version='tiger.research-supplement-request/v1'`、`parent_run_id`、`symbols`、`idempotency_key`。profile/account/universe 从独立验证的 parent 导出，不在 request 额外开放。
- 始终是 **new logical run / new admission / new full chain**，不扩大旧 freeze、不复用旧交易批准。root 最终已确认：旧截止已过也可通过这个显式新问题进入下一任务，但必须 `original_signal_expired=true`，旧候选仍 expired_pending；fresh new deadline 仍硬约束，fresh as_of ≥ requested_at。
- 幂等必须先按 key 恢复已有 successor，核对 stable parent receipt + canonical symbols，不能因重试重新生成 requested_at 而冲突或重复付费。仅新 successor 才 create_scope。
- 计划把 `research-scope.json` freeze，`scope['manifest_sha256']` 同时绑定 admission 和 policy；`validate_admission` 需支持 runtime/scope 两个可选字段的四种组合，并核实 scope 的实际 verified parent pending manifest，而非相信 stored flag。
- root 已写 `research_followup.py` 的 `pending_candidates/create_scope/validate_scope/apply_scope/candidate_outcomes`，并已把 `attach_research_work` 的 frozen scope 验证/属性和 pipeline frontier 入口接好。**本 Agent 的 runner/runtime/public entry 尚未写。**
- `_submit` 的新私有 scope builder / stable supplement request hash 需要在原 admission lock 内处理，旧 run 四字段 request 保持；不可嵌套重复获取同一个 flock 导致死锁，不能普通 run 用相同 key 静默剥掉原 scope。

### Node / MCP / status

- Nodeclient / authoritative JSON schema / CLI / MCP 尚未改。需要添加两项 operation、paper-only mode gate、正确 model env 传递、CLI `--wait` 支持 run/resume/supplement；receipt/verify 继续只读且永不执行。
- 原 readonly 工具数量语义应保留（隐藏 run + 两项新 mutation），paper 才显示新工具。补研 tool name 需显式映射，避免把 Python 的点号 operation 直接拼成含点 MCP 名称。
- parse_quality 持有新纯 `research_status.py` 和 report.py/report_html.py，本 Agent 不编辑它们。接口已约定：`project_research_status(raw=None, *, work_status=None, document_dispositions=None, verification=None, observed_state=None, report_ready=False, observation_state='connected', run_id=None)`。
- 完成态 `verification` 必须传完整 verify_run 返回，且含 published-account-ancestry 才能标 account committed。完整 result.raw 只在 verify_run 已验证后读；raw completed/status flag 或 summary 不能替代。进行态可 raw={} + store/status/dispositions，账户保持 unverified。**入口投影未接。**

## 必须标为未验证或待审查的点

1. 三个新 WIP 文件没有完成最终全文件独立审查；15/15 低层测试不是其完整功能覆盖。
2. `publish_parent_completion/_history_value/verify_parent_history` 的全历史、完整 ordered stream、canonical 发布、receipt 绑定没有正式测试；closeout-only 仅有 helper 判断，尚无生产 supervisor 路由。
3. `recover_stopped_workers` 的真实锁/进程所有权 callback、正在运行 lease 的停机 fence、双任务账户争用、旧 worker 返回没有本次新父流程 OS 级回归。旧基础 epoch 测试不能代替它。
4. 归档 journal 中断点还需要覆盖：intent 后/逐文件 move 后/work rename 后/archive seal 后/new start 后/pointer 前后/ack 丢失；已过低层只覆盖了 work rotation 后 calendar seed 失败这一处。
5. Parent I/O journal 的 fsync 写失败是否立即 poison context、字段类型/extra fields、重复 call key、来源 reuse 链、损坏末行、symlink/hardlink/permission/full-disk/clock 边界还需完整独立审查。当前完成态 history 应 fail closed，但不能把这个设计推断当已验。
6. 归档文件的原相对路径/typed Path 引用需要结合 archive mapping 做历史解释，不能把 archived failed artifacts 的旧 root/work 路径直接当当前 working projection。
7. provider codec 仅接受已明示的 normalized subset，未知类/Path 会拒绝；完整真实 numerical provider 回传类型、dtype、多重索引和错误重放调用链尚未走全。
8. source archive 的 readonly 查询只按原 run/tenant 查看 inflight/unknown，不会清除它；新的完整 source/native reconciliation 验收尚未运行。
9. 原 `observe_run` 的 error code 白名单仍只认识旧 EXIT_CODES；正式入口需将新稳定恢复/研究错误纳入映射，不能让真实失败被改写成不透明 RESULT_INVALID。
10. 新 `research_evaluation.py` 已由 root 另行出现，但本 Agent 没有把它加入 requiredsource；root 要求将来一起补，未在本交接擅自添加。
11. 新 public resume/supplement、显式 expired-old/new-task 边界、partial proof 新 path 接线、最终完整 CI、实际 237 原件业务/receipt、main 集成均未由这个父流程阶段完成。

## 收尾报告

- 已完成：按停止指令停止新增实施；保留已提交工作单元成果与真实测试边界；固化当前 9 文件 WIP 源码包/patch/hash库存；保存完整接线设计、owner 边界、真实已过/历史失败回执及剩余风险。
- 未完成/未覆盖：正式 same-run parent resume、supplement 公共入口、Node/MCP/schema/status 接线、完整父流程 history/closeout/CAS 验收与全仓最终交付；原因是产品所有者明确要求停止，而非把未完成降级冒充完成。
- 自主追加事项及理由：修复独立 50-doc 验收暴露的 DAG 验证性能放大；修 prepared fixture 保持真实 frozen source 绑定；新增停止点源码 ZIP/哈希库存防上下文或并发固化时丢失当前 WIP。都与已授权主任务直接相关。
- 采用的假设：parent IO typed subset 以当前数值 provider 真实规范为准；未知类型 fail closed；补研 symbols 未来应 canonicalize 作 stable identity；没有把这些未实现设计称为当前能力。
- 置信度：已提交子级恢复与其已验边界为高；当前未完成父流程代码为中低，需要完成接线和独立审查后才能判断。
- 建议复核：先核对 `OPERATIONS` 和 supervisor 构造位置，确认正式入口未接；再看三个 NEW WIP 文件及实际 callback/归档安全；最后核对 `transcribed-test-receipts.json` 与历史 RED XML，防止把 15 个低层用例外推为完整业务通过。

## 自检表

- [是] 覆盖了本次“停止新增实施、固化成果并详细交接”的全部范围，并逐项列出原实施未完成部分。
- [是] 没有把可自主整理的状态推给用户；没有新起测试、provider 调用或 GitHub 动作；没有操作共享 index。
- [是] 测试、库存、代码阅读和未验证设计分级；没有把机检当精读、历史基线当最终 SHA 审查、低层夹具当真实业务完成。
- [是] 提供了实际字节快照、校验 hash、代码位置、owner 边界、命令/真实输出和剩余审查点，便于后续工程师逐行复核。
