## 覆盖率声明

- 范围内文件总数：41；完整精读：19；全字节元数据机检：3；当前修订部分阅读：19；清单中完全未触及：0。
- 这是三份 owned proof 文件和明列依赖的范围，不代表全仓精读。B 只有字节、哈希与元数据依据；C 表示旧修订曾完整读或当前读了相关部分，但协作方之后修改，未冒充完整精读当前版本。
- 完整精读文件的绝对路径、当前 SHA、行数和依据，以及全部 B/C 清单与原因，见同目录 `reading-current.json`。源码未做语义精读的 B 文件也显式列在该文件的 `unread_semantics` 中。

本分工已按用户“额度见底，停止新增实施，固化代码并详细交接”的最新指令停止新增代码和测试扩展。新增付费调用、GitHub 操作和共享 git index 操作均为 0。本文是交接，未把原修复工程宣称全部交付。

## 当前代码和责任边界

正式 source：`/Users/mac/Documents/Codex/2026-11-04/ni/work/tiger-intel-capacity-implementation-20261106/source`。

本分工 owned 三份文件，当前字节哈希如下。Root 正在串行 checkpoint，git HEAD 会继续变化，以最终固化提交和这里的文件哈希对照，不把观察到的 HEAD 称为 main 或发布回执。

| 文件 | SHA256 |
| --- | --- |
| `trading_system/llm/work_proof.py` | `7d4427e326458d96e566791b71a9e22d2ec2c6e1b5b214e00f0defa346826740` |
| `trading_system/llm/intelligence_proof.py` | `9b62f946ae98b0fe62479a889552ebf989078b8e323ef0ce723f19287762967a` |
| `tests/test_research_work_proof.py` | `3ed4d9bbeb41ed84944f262b3475b7e9a0e8c10d7f3622f30ab1b3ba5fa8a508` |

**A — 新独立验真已实现**：ResearchWorkProof 绑定 parent admission、精确 policy、冻结预算计数器、WorkStore manifest 和投影；逐一重建合法 UnitSpec、child stage input、结果引用、子封印与父子原始证据字节。原生回复继续经 `_Replay._chat_observation` 验用量、response ID、同请求格式重答和全局次数。没有以一个 validated 标志作为业务成功依据。

**A — 文档和研究链已实现**：重新执行原件身份绑定、逐份 extract / normalize / typed validation / document_review 与终态 disposition；重新计算研究 snapshot 的原文、annotation、eligibility、冲突、frontier 和 coverage。三个 analysis 子任务必须绑定同一 snapshot、targets、完整或明确的事实视图和文档依赖。decision 子任务绑定原数值上下文、账户 parent、原确定 wire、只允许的 picks 和最终 instructions。

**A — 来源和披露链已实现**：只有显式新 policy 能展开 tasks.source.join，随后继续走原 Archive 的原始响应、解析、请求计划验真。新 source reserve / committed 计数按原 attempt 重算；source_runtime 的 start / finish 只验闭合、版本与合法范围，不夸大成独立 HTTP 时间重叠证据。新 round / final assessment 使用 research_risk，cross-validation 使用 research_relations；pending_risk、pending_candidates 和冻结后续 research_scope 都重算绑定。

**A — 失败账务已实现**：nested `tiger.work-native-accounting/v1` 保持 `semantics_state=unverified`，top 为 `tiger.failed-model-accounting/v4`。父 snapshot refs 与 ledger artifact refs 分别保留，父执行已计 native 不重复相加。known usage 按 attempt 和 provider response identity 去重；late known 不清除尚未 reconciled 的旧 unknown hold。prepared 显示 not_sent，仍保留 reservation。完整 rejected native 的原 finish / shape 和已知用量保留，不伪造成 stop，不获得格式重答许可。无原生回复的旧 unknown 不补造已知用量。

## 调用接口

collector、恢复机制或失败任务应调用：

```python
verify_partial_intelligence_model(
    run, admission, *, scene_policy_path=None,
    event_relative="events.jsonl",
)
```

非默认 event_relative 只允许显式新 research policy，且路径匹配 `parent-attempts/[0-9]{1,12}/events.jsonl`，依然使用 root/evidence。完整成功 receipt 仍只读 root/events.jsonl，不能把 archived 失败 stream 当完整成功。该接口已通知 impl_snapshots 对接 parent resume。

重要返回字段包括：schema、business_completed=False、all_required_completed=False、known_native_attempts、known_usage 或 None、unknown_attempts、requires_reconciliation、format_reanswers、failure_stage、event_tail_sha256、parent_event_relative、parent_observed_attempts，以及 research_work.attempts / native_observations / unknown_reservations。

`partial_work_accounting(replay, *, parent_accounting=None)` 是已有 _Replay 的内部调用入口。每条 attempt 保留 unit、call、ordinal、request_ref、native_ref、dispatch_state、ledger_state、usage_known 和原生 observations（包括 late）。

## 实际验证和证据边界

回执根目录：`/Users/mac/Documents/Codex/2026-11-04/ni/outputs/tiger-repair-implementation-20261108`。每份 XML 的精确路径、SHA、机检计数和执行状态，以及实际命令，都在同目录 `capacity-governance-handoff/receipt.json`。

| 检查 | 真实结果 | 相对上述根目录的回执 |
| --- | --- | --- |
| EP02 原始预算断言 | 4 项先失败，后修复为 4 项通过 | `ep02/baseline-failures.xml`、`initial-counter-passing-v2.xml` |
| EP02 最新专属和 model_budget | 37/37 通过，10.25 秒 | `ep02/budget-unit-final-with-replay-hook.xml` |
| EP02 原 broad | 373 中 288 通过、85 fixture 失败；修复 fixture 后，精确相同 85 项重跑全部通过；不是单次 373 全绿 | `ep02/targeted-regression-v1.xml`、`targeted-regression-fixed-v2.xml` |
| 新 doc / snapshot 第一轮 | 21 中 20 通过；1 项测试误改旧内存而非持久化文件，后修为实际 manifest 篡改 | `work-proof-doc-snapshot.xml`；后续 50 项通过覆盖了修正 |
| 新 partial 账务 | 6/6 通过，1.24 秒 | `work-proof-accounting-v2.xml` |
| archived parent path / parser policy | 10/10 通过，1.49 秒 | `work-proof-parent-history.xml` |
| 新 proof 小检查综合 | 50/50 通过，12.76 秒；完整流程 1 项单独执行 | `work-proof-before-full-v3.xml` |
| 完整流程和独立 proof | 1/1 通过，196.88 秒 | `work-proof-full-current-v3.xml` |

完整通过夹具位于 `work-proof-full-current-v3-work/test_all_five_model_stages_and0/run`，保留 events、WorkStore、native evidence、Archive、TEST_FIXTURE-result.json、TEST_FIXTURE-admission.json。它完成 41 stage、110 份 TEST_FIXTURE 原件、224 个 synthetic HTTP chat（clean 220，其余 4 个 stage 各 1）、140 source requests、85 native searches。没有付费或真实生产成功声明。

**B — 这次通过的精确冻结版本**：runtime tree `75e5591ac6ff29407bc45ecf5edb3af9d64d9398e4ec395959ffda2d5815ecfc`，179 runtime 文件；source-manifest SHA `7eb0b0e4dab7fed66a43b5d141f681548cd141c51f86557d869536eab7f52a34`。不能将其改称后续统一当前树通过。

中间失败完整保留。第一次 full 在 JSON 热循环中 461.14 秒被中断，XML 即使没有 failure / error 也**不是通过回执**；sample、原 DB 均保留。impl 的 WorkStore 每次操作一致快照和依赖 memo 修复后，原规模流程 172 秒完成运行，proof 因新 ParsePolicy v1 默认值与旧检查不一致失败。完成新契约默认、同 limits、source usage 和 runtime 对接后，才得到上面的 196.88 秒通过。没有缩小案例规模。

## 尚未验证，不能升级宣称的内容

- **D — 最后 10 个 source_index / run_id 用例尚未跑**：刚补 verify_source_index 与 verify_report_identity，包括 raw 和 reported 同时伪造的负例；停止指令到达后没有继续运行。此前 50/50 和 full 1/1 不能为这批增量背书。
- **D — 当前统一 source 未完成新一轮全链和最近历史回归**。Root 后续还有 rejected-native 安全收口、resume、evaluation 和文档；各 owner 的检查按各自 SHA 汇总，不能混成一次当前全仓通过。
- 本分工没有执行真实 paid 237、GitHub CI、main 集成或 SOURCE Release，也没有解除旧真实 run 的 unknown 或失败状态。

## EP09 长文分段：只有设计，没有半成品实现

本分工没有创建 segment 实现模块；没有改 work_required clean funcs、work_semantics segmentation、event_identity annotation_limits，也没有改 research_relations limits 透传。先前提前加入的 segmentation_contract、segment_max_count、segment_context_neighbors 三个 policy 字段已由 root 删除；AST 元数据机检当前 _POLICY 中这些键为空。因此现有代码不会宣称已经具备分段能力。

已与 root 选定 B 方案，后续实施要点如下；这些是设计，不是已交付功能：

1. 确定性结构和合法完整模型窗口切段，逐字 core 覆盖 `[0,len(original))`，上下文只来自同一原件。完整保留经过验证的 heading / table header / footnote 上下文和相邻结构单位，窗口不足显式失败，不静默裁切。
2. segment span 使用 range_id 与段内位置，由程序验证和偏移到原文全局坐标。程序 namespace 并重绑 IDs；quote / value / unit / currency 保持字面，禁止代码或模型补造。
3. 程序保留所有 typed facts，metadata synthesis 只承担有证据绑定的综合。每个 segment 对照全 merged facts 复核，再做有界 global review；完整 facts 或跨节约束无法合法容纳时明确 unresolved，不能截前若干段冒充完整。
4. max_segment_count=64 是明确资源准入上限；超过应失败保留原件。闭合 annotation_limits 只能由同一原件、精确 plan/hash 和完整覆盖推导，不能由模型任意提高。旧默认 128 spans / 64 subjects / 64 events / 128 claims 与小文档原 extract+review wire 要保留。
5. 原件只存一次 artifact，UnitSpec 引用原件和 plan，避免任务图 N 次复制全文。最终 proof 必须独立重建 plan、覆盖、merge、metadata 和每个 review；gold 样本冻结，不改来迎合代码。

Root 已同意后续 owner 边界：本分工可改 event_identity 显式 source-bound limits 分支；research_relations 只新增 optional limits 透传；work_required.Acquisition.validate 只从已核验 terminal segment manifest 导出 limits。停止指令先于上述代码实施。

## 仅在用户另行恢复实施后接续

1. 对照固化 commit 和三份 proof 文件哈希，先跑最后 10 项 index / run-ID 检查，再跑 current dedicated proof 和最近 legacy 回归。当前停止状态不执行扩展检查。
2. 新 source 完全冻结后再原规模 full proof；root 决定的短语法检查单独记录，不拿旧 75e5591 夹具冒充新树。
3. EP09 先完整读当前文件及直接依赖，再做真实 clean orchestration、窗口 / 结构 plan、字面绑定 merge、metadata / reviews、显式 limits 和独立 proof；先失败断言，gold 样本不改。
4. 原 24EP / 42AT、真实237、独立验收和 main / Release 继续由 root 总控。EP22 的 7–14 天观察和 staged P2 不能用短夹具伪造通过。

EP02 单独完整报告见 `../ep02/REPORT.md`、`delivery.json`、`reading-receipt.json`。本次 receipt.json 的 test_commands 是已执行命令记录，不是要求现在继续运行。

## 收尾报告

- 已完成：本分工已通过的 EP02；新独立 proof、失败账务与 archived partial 接口的实现和列明的实际验证；按停止指令固化哈希、命令、回执、完整阅读清单和接续边界。
- 未完成/未覆盖：最后 index / run-ID 10 项未跑、最新统一树全链 / 历史回归未跑、EP09 未实现、真实237 / CI / main / Release 未由本分工完成。原因是用户明确停止新增实施，不是上下文或模型自设预算理由。
- 自主追加事项及理由：从 full fixture 发现并协同修复 WorkStore 重复 JSON / 依赖核验的性能问题；对新并发来源、marker 和 archived partial 补契约校验；没有新增 paid probe。
- 采用的假设：parent admission / source / policy 由既有外层准入先验真，本分工继续绑定 WorkStore 和原 native；本地 synthetic fixture 不代表生产容量或语义完美。
- 置信度：已列冻结版本高；当前最终未验证增量中。B/C/D 边界分别列明。
- 建议复核：top v4 与 nested ledger / parent 去重和 unknown hold；source-index / run-ID 未跑增量；new-policy-only 分支与旧默认路径差异。

## 自检表

- [是] 覆盖了最新“停止实施并详细交接”的本分工范围，没有把原 24EP 工程冒称完成。
- [是] 没有把可自主完成的交接事项推给用户，没有以不必要的问句收尾。
- [是] 结论与代码对应证据、SHA、实际测试状态；B/C/D 未冒充当前完整精读或全仓通过。
- [是] 可供资深工程师逐项复核；未验证增量和未实施设计明确可见。
