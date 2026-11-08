# Tiger 情报系统修复：代码固化与详细交接

更新时间：2026-11-08（Asia/Shanghai）。本文件是产品所有者要求停止新增实施后的研发检查点，**不是完成验收、生产发布或账户执行成功证明**。

## 覆盖率声明

本次固化的代码范围是审计提交 `7dd502c3826de58e4f35659251797d4faa1c479a` 到代码检查点 `97aa30cd84d4d57043e7f2e503c59a1b7f12e332` 的 **104 个变更文件**：当前字节有完整阅读/完整编写核对记录的 A 级 36 个；保守归为全文件库存/哈希等 B 级机检的 68 个；单列抽样 0；未纳入库存 0。这里的 B 级包含此前读过但没有当前字节完整阅读回执的文件，不能理解为 104 文件均已精读或独立审查。

逐文件 SHA、等级和归类理由见 [reading-coverage.json](repair-checkpoint-2026-11-08/reading-coverage.json)。另外对当前 `trading_system/` 和 `tests/` 的 **321 个 Python 文件**执行了 AST 语法检查，0 错误；这是 B 级检查，不是完整构建或测试。三位实施者的阅读清单保留在附录，不把不同基线阅读次数简单相加。

本文证据等级：**A**＝实际代码阅读/实现核对；**B**＝实际运行、Git/哈希/元数据检查；**C**＝抽样或局部观察；**D**＝设计、未实现或未验证。

## 1. 现在应当知道的结论

1. 用户最新指令是：“我额度马上见底了，你把当前进展一个是成果代码固化，第二是整理一个详细的交接文档”。因此已停止新增实施、付费调用和扩大测试，转为保全当前代码、证据与接续说明。此前“实施至 main / Release”的目标暂不继续执行；不能擅自把本检查点合并上线。
2. **B：代码已固化。** 主要代码检查点为 `97aa30cd84d4d57043e7f2e503c59a1b7f12e332`；它包含当前工作中的 38 文件增量，6006 行新增、62 行删除，并承接本轮已经形成的 13 个提交。最后一个代码提交明确标为 `wip`，保留未完成事实。
3. **B：已有完整本地夹具通过。** 一个先前冻结版本完成了 41 个业务阶段、110 份 TEST_FIXTURE 文档、224 次本地 synthetic HTTP chat，并通过独立 proof，196.88 秒。它不是付费模型，不是原 237 份真实原件，也不是最终检查点全链通过。
4. **D：最关键缺口仍然存在。** 长文分段未实现；正式 same-run `resume` 与 `research.supplement` 公共入口未接线；四轴状态尚未在 runner/runtime/Node/MCP 统一；最终冻结版本的整仓 CI、真实 237、独立 M6、main 和 Release 尚未完成。
5. **B：停止后的必要短检查为 75/75 通过，0.88 秒；321 文件语法检查通过。** 当前 1858 个候选仓库文件对内存中的实际 GitHub/模型凭据及相关 base64 形式扫描为 0 命中。凭据没有写入交接、源码、归档或 Git 远程地址。
6. 没有真实券商、真实资金或代客交易授权。本次固化也没有修改真实账户、发布生产网站或制造新的付费业务记录。

## 2. 精确定位与权威顺序

| 项目 | 位置/身份 |
| --- | --- |
| GitHub 仓库 | `https://github.com/geniusdapeng-collab/workroom-tiger` |
| 任务 | `T-2026-1106-0001`；Issue `#61` |
| 分支 | `task/T-2026-1106-0001` |
| 现有 PR | `https://github.com/geniusdapeng-collab/workroom-tiger/pull/62`，保留 draft |
| 上次回读的 main | `946bc5b70ebe8fd9266e960d6c373df66ac81469`；这不是本检查点的集成回执 |
| 本轮修复审计基线 | `7dd502c3826de58e4f35659251797d4faa1c479a` |
| 本次代码检查点 | `97aa30cd84d4d57043e7f2e503c59a1b7f12e332` |
| 活动源码目录 | `/Users/mac/Documents/Codex/2026-11-04/ni/work/tiger-intel-capacity-implementation-20261106/source` |
| 本轮产物目录 | `/Users/mac/Documents/Codex/2026-11-04/ni/outputs/tiger-repair-implementation-20261108` |
| 本轮工作状态目录 | `/Users/mac/Documents/Codex/2026-11-04/ni/work/tiger-repair-implementation-20261108` |
| 已批准的完整计划 | `outputs/tiger-repair-execution-plan-20261108/repair-backlog.json`，SHA256 `b49fc0aff8d891f82af3696cbc47f7d72322c64daf25a9d520314e17b85633dd` |
| 原计划可携带副本 | [approved-repair-backlog.json](repair-checkpoint-2026-11-08/approved-repair-backlog.json)，24 个 EP、42 个 AT |
| 解释器 | `/Users/mac/Documents/Codex/2026-11-04/ni/work/tiger-cloud-run-20261104/python-venv/bin/python` |
| Node | `/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node` |

权威顺序：**本交接及最终 checkpoint receipt → 当前 Git 提交与实际文件 → 本轮具体测试/子任务回执 → 旧 STATE/CONTINUATION 历史记录**。旧文件仍保留过程证据，其中的 “ACTIVE / next / running / latest head” 可能已经过时，不能覆盖本次用户暂停指令。

仓库原有的两处未跟踪浏览器产物 `governance/apps/web/playwright-report/`、`governance/apps/web/test-results/` 保持原样；它们未进入本次源码提交。不要为了得到表面上的 clean 状态删除它们。以后正式验收使用独立、干净且准确提交的 checkout。

## 3. 已固化的实现及验证边界

### 3.1 请求计量与原生结果账务

**A/B：EP02 已从 HTTP JSON 近似计数改为固定官方 tokenizer 与 provider message/tool framing。** 文件包括 `llm/model_budget.py`、`llm/provider_prompt.py`、官方资产与 provenance、`strict_clean_transport.py`、`deepseek_required.py` 和 proof。发送边界再次核对 request hash、输入计数、输出上限和注册版本，避免规划后请求变化仍沿用旧预算。

旧真实 paper-009 的精确诊断已保留：provider 输入为 850560，旧输出 200320，合计 1050880，超过 1048576 共 2304。新计量在同一原请求上得到 850560，合法输出上限为 198016。没有通过硬减一个经验 2304 buffer、删原文或改模型解决。历史计数器/协议继续只读验证。**新预算的 37 个专项通过不等于当前 237 业务已经成功。**

**A/B：EP03 已补有界 HTTPError 观测。** `llm/http_observation.py` 记录最多 65536 字节的错误体观测长度、hash、完整性与安全分类；不落自由 provider 错误正文、不把错误摘要称原生完整体，不把 HTTP 400 的未知用量写为 0。原 HTTP 层专项及近回归 241 通过。

**A/B：`llm/rejected_native.py` 是显式 `tiger.rejected-native/v1`。** 已完整收到并能核验 model/id/usage，但因 length、错误 choices 或不完整 assistant envelope 被拒绝的响应，可以保留真实用量与实际 finish/shape。不得造 `stop`、解析截断内容或消耗格式重答额度。新记录只在显式新 policy 下生效，旧 paper-009 用量仍未知。停止前增加了 v3 路由一致性和 encoded-ID 防泄漏检查，11/11 通过；发现的 base64 ID 问题已修复。

### 3.2 工作单元、子级并发与恢复

**A/B：`trading_system/tasks/` 已实现显式 WorkStore。** SQLite 保存不可变 manifest、计划/扩展链、unit/edge、epoch/lease、attempt、原生观测、重答 CAS 与事件投影；外部 I/O 不在 SQL 写事务内。每个子级有自己的执行上下文、模型 client、原生响应和 seal，不放松原父级单 owner / 单 active IO 规则。

原生模型 attempt 先保留 intent，实际运输边界才标 dispatch；已 settled 的 native 纯重放，不第二次发送。全 run 格式重答仍最多一次，通过持久 CAS 决定。unknown 保留 reservation，不能因进程重建或 client 重建清空。

已修复三个具体问题：SQLite WAL/SHM 关闭竞态；第一次已落 native、第二次尚未发送时的恢复窗口；按 DAG 路径重复解析大 plan/重复读结果造成的性能放大。工作单元基础最后 72/72 focused、162/162 旧近回归；独立 WR-01 probe 已由另一 Agent 跑过。

**A/B：Coordinator 增加 stage scope。** 恢复早期已完成 clean 时可只读取该 scope；后面的 analysis 已失败仍保留失败事实，不让它导致已完成 scope 被重发，也不抢跑无关 ready 单元。本次 scope/resource/semantic/source-index 组合 26/26 通过。

**D：不能把上述子级恢复称为正式父流程恢复。** `research_resume.py` 和 `research_replay.py` 已保留 NEW WIP 代码，15 个低层测试通过，但公开入口、真实锁 callback、history/closeout、完整账户 CAS 和独立进程恢复尚未接通/验证，详见第 6 节。

### 3.3 采集、解析与语义

**A/B：采集从有效串行改为有界真实并发。** `intelligence/service.py`、adapters、host limits 和 `research_sources.py` 分开登记源 observer、配额、请求与子级结果，再由父级汇合。默认历史路径保留；新研究 policy 才启用 source workers。已存在 source/parser 组合与实际本地 HTTP 的并发检查，不能外推为长期 150 endpoint 生产容量。

**A/B：解析新增 manifest 与 resource pool。** 包含原文/正文 hash、段落、表格单元、脚注/附件、位置、时间来源及完整性；扫描 PDF 或混合空白页不得假标全文成功。`verify_manifest` 会由原字节重解析核对。金标在语义改动前冻结为 7 案例、23 个 critical 标签，3 development / 4 heldout；其中 6 个是合成夹具，1 个是真实归档 FED G17，归档 authority 仍 unverified。

**A/B：`llm/work_required.py` 与 `work_semantics.py` 实现逐文档 extract / normalize / validate / document_review。** 程序管理文档任务身份；模型返回语义内容，不能为通过双射而补造 ID 或字段。原文保留；引用仅在同文精确唯一范围内补位置；主体、期间、币种、单位、数值和重要事实复核不能被 bypass。小文档目前是提取与复核两个模型 unit。

**D：长文分段没有实现。** 当前没有 segment 模块、没有 source-bound 扩展 annotation limits，也没有分段复核/全父文汇合。曾提前加入的三项 segmentation policy 字段已在固化前删除，避免代码宣称不存在的能力。

### 3.4 证据、风险、快照与补研

**A/B：任务证据视图保留全 facts / source spans / 反证，并明确 omitted ranges。** 审计 metadata 留在原件与账本；不能将节省 token 等同于无遗漏。`research_snapshot.py` 绑定 source/policy/input/as_of/frontier/raw/annotation/eligibility/coverage。

**A/B：`research_relations.py` 新版本关系校验收紧全正文 origin、转载/依赖关系、时间与修订事实。** 旧 `event_identity.py` 默认未放松；未来或时间未知节点不能作为桥增加原时点独立来源数。宣称 supersedes 只保留待核线索，不直接删旧主张；撤回线索限制打分但不伪造已经证实的风险。78 个相关新旧测试通过；新增完整财务质量仍需独立复核。

**A/B：`research_risk.py` 分开待核风险与有资格打分的风险。** 未核线索可进入补证，不把“缺证”当作“未发现风险”。

**A/B：`research_followup.py` 及 frontier 可选 scope 已实现一部分候选补研逻辑。** 旧快照不会被直接扩池；新候选必须进入新 scope/new admission，旧截止已过必须披露 original signal expired。14 个 frontier/followup 相关用例通过。

**D：正式 `research.supplement` 请求尚未实现。** 目前只有 helper、可选 scope 处理与测试，不存在可对用户宣称能用的公开补研端到端入口。

### 3.5 专题和报告

**A/B：专题改动已提交并集成。** 新 `topic_semantics.py` 对 partial RSS 完整事实、会前/会后矛盾、同文引用、分批范围和 max_conclusions 做严格控制；analysis/review/report 共同绑定用户限制，不新增额外 LLM 阶段。曾发现 nested source_batch 范围问题并修复。专题最终扩大回归为 387 passed / 2 skipped，262.54 秒；Node 11 passed。跳过项和执行范围保留在原 receipt 中。

**A/B：报告新增四轴：process / evidence / decision / account。** `research_status.py` 是纯投影；静态报告固定为 report ready / pending publication。只有可信调用方真正执行完整 `verify_run` 后传入返回值，才可能显示账户 committed；raw success flag 不是证据。全证券卡分开合格 HOLD、WAIT_EVIDENCE、池外待补查和失败，价格/股数使用原批准值。

**A/B/C：已验证静态报告的响应式与真实控件。** 29 个聚焦测试、99 个报告近回归；本机 Chrome 布局 40/40，控件 4 宽度共 36 个断言通过。只人工目视了移动状态和桌面证券卡两张截图，其他截图是自动布局检查。原生 Chrome CUA 两次超时后使用本机 Google Chrome 独立 headless QA context，无外部请求；浏览器和临时 HTTP 服务已关闭。

**D：运行时的状态接口仍未全端接入。** 静态搜索/筛选/折叠不等于 live scheduler 的取消/恢复控制。完整扩大回归按用户指令中止，185 已通过、exit 2，不能写全量通过。

### 3.6 评测与复盘 NEW WIP

**A/B：`research_evaluation.py` 已写成可调用的离线 CLI 与纯函数，9 个用例通过。** 支持冻结 cohort、逐 run 独立核验后汇总、gold source/artifact/独立审阅绑定、分母保留及事后归因。没有发模型请求，没有把 heldout 标签接入运行时。

**D：它仍需独立审查和真实记录演练。** 目前没有对最终 237 运行过真实质量评测，也没有金融语义准确率证明。接手时重点复核：`validated_originals` 目前取 terminal disposition 的语义是否与 eventually-valid 指标一致；recovery 分母是否覆盖尚未再次领取的可恢复单元；cohort mode 是来源声明，不等于模型生产真实性认证；混合/未知字段及错误情况下的 metric 类型检查是否充分。不得凭 9 个机制测试把这些指标用于生产承诺。

## 4. 24 个工作包逐项状态

以下是实施状态，不是独立验收签字。完整原步骤、依赖与验收标准在附录计划 JSON，未删减任何 EP。

| 包 | 当前成果 | 未完成/下一步 |
| --- | --- | --- |
| EP00 三基线/合同 | 审计基线、任务/PR、显式新合同及本检查点保留 | MCD 台账主要只有 MC-003；完整24包/42AT追踪及独立验收基线未齐 |
| EP01 入口/产品承诺 | 新 source inventory 可选择 research_work；专题文档部分更新 | OPERATOR/STRICT-WORKFLOW/QUICKSTART/PROJECT_INTRO/README/PUBLIC_VERIFICATION 全面统一未做 |
| EP02 完整计量 | 固定官方 framing/tokenizer、发送前核对、历史计量兼容 | 最终真实请求/237验收未做 |
| EP03 外部观测/unknown | HTTPError安全观测、known rejected用量、持久CAS | 最终共同版本跨恢复/部分证明回归未完成 |
| EP04 容量/资源 | parser pool、任务token/memory准入、原deadline原则 | 全模块容量清单、实际峰值与磁盘故障矩阵未完整验收 |
| EP05 任务账本 | SQLite unit/attempt/lease/event、结果blob与依赖验证 | 最终统一版本独立验收 |
| EP06 子执行上下文 | 隔离client、子seal、parent join、owner fence | parent lifecycle正式接线和故障矩阵 |
| EP07 解析/位置 | manifest、结构/脚注/附件/时间、重解析核验 | 复杂文档适配与真实全量覆盖复核 |
| EP08 语义/身份 | 逐文档提取+确定性校验+模型复核；topic规则 | 最终语义金标与真实原文独立评价 |
| EP09 分段/局部恢复 | 子模型native恢复；parent低层WIP | 长文分段未实现；正式resume未接线 |
| EP10 审计/任务视图 | 原件+事实+任务视图、选取/遗漏索引 | 全量输入对照视图的重要事实/反证质量评估 |
| EP11 跨文/时点 | 新关系wrapper、全body origin、未来节点阻断 | 真实修订/撤稿/财务反证覆盖验收 |
| EP12 缺口补证 | 复用有界问题/coverage采集与新assessment | 最终新语义链的缺口/无进展/权限完整回归 |
| EP13 待核风险 | pending-risk与scoring分开，相关回归 | 真实风险线索补证闭环 |
| EP14 研究快照/proof | 冻结统一snapshot；子证据重建/partial账务 | 最后source_index/run_id的10项tamper测试未跑 |
| EP15 采集并发 | 有界真实source并发、独立observer/配额 | 最终一致性/质量/性能对照及长期容量 |
| EP16 文档并发 | child workers、weighted admission、逐文档join | 分段后资源/最终237性能与质量 |
| EP17 分析/账户 | 同snapshot三路分析；最终决策和账户串行 | parent恢复、closeout-only、账户并发/故障完整验收 |
| EP18 新候选研究 | pending/create_scope/apply_scope/frontier helpers | public supplement、请求schema、幂等/过期新任务验收 |
| EP19 状态/决策卡 | 纯四轴投影、Markdown/HTML、静态UI验证 | runner/runtime/Node/MCP、live状态/恢复/取消接线 |
| EP20 金标/可靠性 | 冻结7例23标签；NEW离线评测和归因9测试 | 指标独立审查、真实capture与heldout独立裁决 |
| EP21 最终全链验收 | 某冻结版本110原件全流程夹具通过 | 最终head整仓、42AT、真实237、独立M6均未完成 |
| EP22 观察/发布 | 本次研发备份与证据保全 | main/Release未发布；7–14天观察未执行 |
| EP23 持续情报P2 | 保留原计划分期；现有topic watch/tick | 跨任务失效索引、长期订阅/容量仍后续阶段，不虚报完成 |

## 5. 42 个 AT 的接续索引

“已有局部证据”不等于 AT 全项通过；继续前应按原计划的前置、步骤、预期逐项补齐实际结果。

| AT | 维度 | 当前可用证据/缺口 |
| --- | --- | --- |
| 01 | 正常与回归 | 旧近回归、110夹具；最终整仓未跑 |
| 02 | 入口 | source policy准备；公共resume/supplement/统一说明缺失 |
| 03 | 窗口 | provider_prompt_budget、EP02 37绿 |
| 04 | 参数边界 | 最后发送hash/合法窗口；最终统一源待回归 |
| 05 | 网络与恢复 | HTTPError与unknown专项；父流程正式恢复缺失 |
| 06 | 输出错误 | rejected-native 11绿，原生重答/unknown边界 |
| 07 | 模块边界 | parser limits/resource与typed annotation；长文未做 |
| 08 | 资源错误 | weighted resource、SQLite竞态；磁盘/峰值未穷尽 |
| 09 | 崩溃恢复 | 子native窗口+parent低层15；公共独立进程缺失 |
| 10 | 并发一致性 | WorkStore CAS/epoch与source并发；最终跨模块待验 |
| 11 | 调度故障 | scoped Coordinator/owner/取消观察；live父级未验 |
| 12 | 解析完整性 | parse manifest/golden source完整性；生产适配未穷尽 |
| 13 | 时点与财务 | source-bound validators与关系回归；真实财务质量未证 |
| 14 | 结构与身份 | 程序docID、跨度/主体/数值拒绝用例 |
| 15 | 覆盖计数 | 237终态夹具、状态原件计数；真实最终未验 |
| 16 | 局部修复 | 全run一次CAS及原native恢复；parent端到端缺失 |
| 17 | 长文分段 | 未实现 |
| 18 | 原件与视图 | source hash/完整facts/omission ranges；gold对照待做 |
| 19 | 独立性与冲突 | 关系wrapper 78项组合回归；全真实样本待审 |
| 20 | 金融质量 | 7例23关键标签已冻结；没有当前真实全通过证明 |
| 21 | 证据缺口 | 原coverage机制+新risk assessment；最终组合待验 |
| 22 | 业务边界 | 无合格证据不评分/提交既有合同；新全部路径待验 |
| 23 | 风险线索 | pending-risk专项；真实补证闭环未证 |
| 24 | 快照并发 | 新snapshot/旧归档证据；最终并发矩阵未完成 |
| 25 | 权限/安全 | owner fence、secret scan、只读约束；新入口待写 |
| 26 | 历史兼容 | 多组旧近回归；最终head全套未完成 |
| 27 | 受控并发 | source/doc/analysis真实本地重叠与限额；237未验 |
| 28 | 效率 | DAG热点已修并测量；同质量端到端对照未完成 |
| 29 | 分析汇合 | 110夹具三路snapshot一致性/proof |
| 30 | 账户原子性 | 原CAS保留；新恢复/closeout故障组合未验 |
| 31 | 候选补研 | scope/frontier14绿；公共入口未实现 |
| 32 | 状态真实性 | 29聚焦+75收口中的报告测试；跨入口缺失 |
| 33 | 前端体验 | Chrome布局40/控件36；live恢复/取消未验 |
| 34 | 统计正确性 | 新evaluator机制9绿；真实cohort与分母审查待做 |
| 35 | 真实全链 | 当前版本237真实原件尚未运行 |
| 36 | 回滚 | 历史只读/原件保全原则；实际新合同回滚演练未做 |
| 37 | 集成发布 | 本次仅任务分支备份；main/Release未做 |
| 38 | 长期演进 | P2未实施；持续容量没有实测结论 |
| 39 | 专题语义 | topic最终387绿/2skip，保留分批语义回执 |
| 40 | 监测连续性 | 既有watch/tick仍在；本轮未补独立完整连续验收 |
| 41 | 复盘归因 | 新attribution source/asof/paper边界机制测试；真实归因未做 |
| 42 | 对话专题 | topic MCP参数/结论数量11 Node绿；宿主完整对话交付待验 |

## 6. 恢复工作时的优先顺序与关键接线

本节是 **D 级待执行计划**。用户恢复实施前，不自动执行这些步骤。

### 第一批：先完成父流程恢复与正式合同

先读 [parent-resume-handoff.md](repair-checkpoint-2026-11-08/parent-resume-handoff.md)，然后完整读取 `research_resume.py`、`research_replay.py`、`agent_runner.py`、`agent_runtime.py`、`agent_supervisor.py`、Node边界与 schema。

- 当前正式操作仍只有 doctor / describe / run / status / receipt / verify。不存在 resume/supplement 入口，不要使用猜出的命令。
- supervisor 仍构造旧 `ExecutionContext` / `EvidenceStore`，没有调用新 parent factory。当前 policy 中 parent_attempt/provider_replay marker 仅为研发准备，不能据此判断恢复已可用。
- 同 run 恢复必须绑定原 request/source/policy/input/account/as_of/deadline；不能刷新截止、换幂等键、换 state-root 伪装继续。
- 实际 OS 锁和已停止 worker 验证必须落实；低层测试用的 `lambda: None` 不得作为生产 stopped verifier。
- source/chat unknown 先核对，禁止发送同请求探测结果；已 settled 原 native 纯重放；永久业务失败不复活。
- 原 failed events/log/work/engine/process 全部归档；新 work 从原 account-before/calendar 重建。archive journal 各崩溃点补测试，不删历史。
- 完整 receipt 仍验 canonical root/events；归档 partial 只允许显式新合同的 `parent-attempts/<数字>/events.jsonl`。
- engine完成但seal前崩溃要 closeout-only，不能再跑pipeline；账户CAS、write-once-equal、原sealed_at及原deadline都要独立验证。
- Python、CLI、MCP、JSON schema、mode gate、wait、错误码、status 一起改完。Node继续仅边界/传输，Python拥有业务/锁/幂等/验真。

### 第二批：实现长文分段与原件汇合

选定方案是确定性结构/合法窗口分段，而非额外模型先划大纲。完整 core ranges 必须覆盖 `[0,len)`，上下文只能来自同原件；程序按已哈希原文 range 映射位置、namespace内部ID并重绑引用。完整 facts 列表不能交给模型随意删除；段复核、metadata综合与父文整体复核均须有界且可验。不能容纳跨段关系时应明确失败，不能只输出前若干段。

拟议合同名 `tiger.document-semantics-segments/v1`，拟议资源上限64段、邻接上下文1结构单位，**当前 policy 尚无这些字段**。实现时需证明上限是准入/失败边界而非截断策略。扩展原 schema 的主体/跨度/事件/claim数量必须 source-bound、program-derived，并保留旧默认。proof 必须独立重算 plan、覆盖、合并、review 和最终结果。详细设计见 capacity 附录。

### 第三批：完成补研与四轴状态，修正文档

- supplement 始终 new run/new admission。请求只允许 parent_run_id/symbols/idempotency_key；profile/account/universe从已核验parent导出。
- 固定requested_at和scope identity，重试先找原 successor；不能每次重算时间导致重复付费。
- scope同时绑定admission/policy/实际文件，独立核对parent pending manifest；过期信号不能复用批准。
- `project_research_status` 完成态传完整 `verify_run` +已核验完整 result.raw；summary缺少全证券rows不能代替。
- report是发布前封存快照；runtime可显示实际账户已提交，但不能重写原封存报告伪装当时已经提交。
- 正式文档逐项对照真实入口、模型、预算计量、错误码、恢复边界，历史/演示/topic/paper分开。

### 第四批：质量、最终工程验收与真实237

先补 evaluator/归因独立审查、MCD完整台账与42AT，进行当前head相关回归，然后冻结最终代码再跑整仓/原CI。不要边改源码边让真实任务使用“同一版本”。

原237最终真实任务只在工程门通过、原授权/预算仍明确时运行，尽量避免重复付费试探。保留全部原件、原生回复、失败账务与原账户；真实完成需五类语义职责、逐原件终态、数值风险、账户版本与唯一提交、独立receipt/verify全部成立。零交易也不能豁免账户验真。

最后才走独立M6、普通PR合并、main回读及SOURCE Release/下载字节校验。7–14天同合同观察必须按真实日历另列；当前无法补写为已通过。

## 7. 既有测试与失败记录如何解释

索引：[test-index.json](repair-checkpoint-2026-11-08/test-index.json)，含本轮根目录45份XML的实际计数和SHA。**禁止直接把所有passed加总当当前版本测试数**：有重复、不同版本、不同范围以及中断记录。

| 记录 | 事实 |
| --- | --- |
| `checkpoint-focused.xml` | 本次固化前75 passed / 0 failed / 0 skipped，0.88s |
| `checkpoint-python-syntax.json` | 321个Python文件，语法错误0 |
| `checkpoint-credential-scan.json` | 1858仓库候选文件，实际内存凭据及编码命中0；不包含用户目录扫描 |
| `work-proof-full-current-v3.xml` | 1 passed / 196.88s；冻结runtime tree `75e5591ac6ff29407bc45ecf5edb3af9d64d9398e4ec395959ffda2d5815ecfc`，179 runtime files；110 fixture文档/224synthetic chat/41stage |
| `work-proof-before-full-v3.xml` | 50小proof/partial/native检查通过，12.76s，详见capacity receipt |
| `work-proof-full-initial.xml` | 461.14s后为排查CPU热点而中断；XML即使没有failure也不代表通过 |
| `topic-semantics-complete-final.xml` | 387 passed / 2 skipped，262.54s |
| `research-status-focused.xml` | 29 passed，状态和报告聚焦 |
| `research-status-report-regression.xml` | 99 passed，4.64s |
| `research-status-full-regression.xml` | 用户停止指令后中断；185 passed，104.34s，exit2，未完成 |
| `rejected-native-v3-identity.xml` | 1 failed /10 passed，暴露base64 ID问题；保留RED |
| `rejected-native-v3-identity-after.xml` | 修复后11 passed |
| `evaluation-before.xml` / `evaluation-after.xml` | 缺模块RED与新增机制9 passed；不证明真实金融质量 |
| parent-resume tests转录 | 15/15低层 + 2/2旧fixture指定测试；原工具输出转录，不补造JUnit |

最后10项 source_index/raw.run_id coherent tamper 检查虽已写入 `test_research_work_proof.py`，**没有运行**；当前统一源码也没有完整再跑110夹具。它们是恢复实施后的具体优先检查之一。

必要短复核可使用如下已实际执行过的本地命令；重新运行时写新的输出文件，保留本次原回执：

```bash
cd /Users/mac/Documents/Codex/2026-11-04/ni/work/tiger-intel-capacity-implementation-20261106/source
TIGER_PYTHON=/Users/mac/Documents/Codex/2026-11-04/ni/work/tiger-cloud-run-20261104/python-venv/bin/python
"$TIGER_PYTHON" -m pytest -q \
  tests/test_rejected_native.py tests/test_research_evaluation.py \
  tests/test_task_coordinator_scope.py tests/test_task_resource_admission.py \
  tests/test_research_work_semantics.py tests/test_repair_research_status.py
```

完整夹具/父恢复/报告浏览器的准确命令分别在三个专项附录，不要从本文猜测新入口或批量启动旧付费运行脚本。

## 8. 原真实业务证据、输入和安全边界

必须保留：

- 原237归档基线：`outputs/tiger-intel-capacity-implementation-20261106/paper-snapshot-baseline-original-237`。
- 原授权paper state：`outputs/tiger-intel-capacity-implementation-20261106/live-paper/state`。
- 原专题state/历史：`outputs/tiger-intel-capacity-implementation-20261106/live-postfix-7c0f9693`。
- 历史真实paper1—9及原native、错误体安全诊断、独立验证记录。此前均没有最终账户提交成功；不同代码/输入/探索性尝试不能混成生产成功率分母。
- 旧汇总：`work/tiger-intel-capacity-implementation-20261106/FINAL-DELIVERY-STATE.json`。它保留历史，不是本轮恢复实施的权威 next 指令。
- 本轮所有红/绿/中断日志、测试fixture和source freeze。源码archive不包含这些大体量业务状态，迁移机器时需另行保全。

永久保护 `/Users/mac/Downloads/1200可商用纯音乐` 及其所有子目录：不删除、移动、改名、覆盖或清空。它与本次固化无关。

GitHub 优先使用当前任务产品所有者令牌；当前 `/user` 回读为 `geniusdapeng-collab`。不要切换其他身份来绕过限制。凭据仅在原持久Node进程内存；本交接不包含值，也不创建凭据文件。新会话不应假设能继承该内存，令牌缺失时必须通过授权凭据渠道取得，不能从日志/仓库寻找密钥。

原持久 Node 变量包括 `tigerOwnerTokenNov08`、`tigerGhNov08`、`tigerRunChildNov08`、`tigerGitEnvNov08`、`tigerModelKey`。**只在同一个现存REPL里有意义**，不应将这些名字误认为环境变量或脚本接口。原会话未结束前不要 reset REPL；不要输出变量值、整个env或HTTP Authorization。

模型配置/外部端点来自可信启动，不能从研究请求、网页或原文修改。无新授权不得切换模型、购买订阅、连接券商或对客户发消息。本次代码备份没有创建定时任务。

## 9. 提交与备份说明

本轮审计后依次形成的提交（root集成SHA）：

- `0bb771a`：有界HTTP失败观测。
- `4fd95cc`、`ca7d6ff`：工作单元基础、原生结果重放。
- `d0746b9`：provider framing预算。
- `388ca4c`、`9761bf1`：解析/金标及source并发。
- `8d1a969`、`087372b`：实际发送边界/身份/SQLite sidecar。
- `50b9f24`、`5bbcbaf`：持久格式重答恢复窗口。
- `403c74a`：DAG核验性能修复。
- `9d545ab`：专题语义复核与结论数量。
- `393332e`：四轴状态及报告。
- `97aa30c`：本次WIP代码检查点，保存所有剩余研发改动。

交接文档随后单独提交。最终远程head、源码压缩包SHA、文件索引和分支回读由本地 `checkpoint-delivery-receipt.json` 记录；它将位于本轮outputs目录。代码与文档推送任务分支，PR保持draft，main不合并。备份提交显式 `[skip ci]`，避免用户要求停止后又启动昂贵扩大CI；这不构成未来发布的CI豁免，后续必须重新触发完整门禁。

## 10. 专项交接目录

- [预算/独立proof与分段设计](repair-checkpoint-2026-11-08/capacity-handoff.md)
- [该分工精确测试回执](repair-checkpoint-2026-11-08/capacity-receipt.json)
- [正式父流程恢复与补研接线](repair-checkpoint-2026-11-08/parent-resume-handoff.md)
- [父恢复真实工具结果转录](repair-checkpoint-2026-11-08/parent-resume-tests.json)
- [四轴状态、报告、浏览器检查](repair-checkpoint-2026-11-08/report-handoff.md)
- [报告机器回执](repair-checkpoint-2026-11-08/report-receipt.json)
- [本轮原批准24EP/42AT计划](repair-checkpoint-2026-11-08/approved-repair-backlog.json)

专项附录中的原相对路径名称指各自原outputs子目录，原绝对路径仍可在本机使用；本目录的重命名副本用于随仓库携带。各附录可能写有“待root集成”：报告成果现已集成为 `393332e`，全部当前WIP已进 `97aa30c`，以本主文为准。

## 收尾报告

- **已完成：** 按最新指令停止新增实施；收口现有短检查；保存所有本轮代码到明确WIP提交；保留原失败/中断证据；汇总24EP、42AT、精确文件/提交/接口、真实测试边界、凭据和恢复步骤；准备任务分支备份及源码归档。
- **未完成/未覆盖：** 原完整修复项目尚未完成，主要缺口见第4—6节；原因是用户明确要求停止以保留额度。没有把研发检查点称为上线完成。
- **自主追加事项及理由：** 将三份专项交接和机器回执随仓库保存，另做实际凭据扫描、语法检查和短回归，以便新会话能准确恢复而无需重复付费探索。
- **采用的假设：** “固化”指可恢复的任务分支备份，不指合并尚未验收的生产代码；继续实施须由用户恢复目标。旧read-only历史与原账户、原件始终保留。
- **置信度：** 当前提交、文件、短检查和已有冻结版本回执为高；未完成父流程/分段/质量指标/最终业务交付明确未验证。
- **建议复核：** 第一看公开OPERATIONS与supervisor实际接线；第二看最终版本与110夹具版本差异及最后10项未跑测试；第三看真实237、账户和main发布的硬完成标准。

## 自检表

- [是] 覆盖最新“代码固化+详细交接”的全部范围，并完整列出原工程未完成部分。
- [是] 没有把可自行完成的提交、库存、检查和交接推给用户，没有以不必要的问句收尾。
- [是] 区分A/B/C/D，已有通过、历史失败、中断、未实现和未验证均有依据。
- [是] 可按Git提交、逐文件SHA、原始测试回执和接口逐项审查，没有把低层夹具外推为生产完成。
