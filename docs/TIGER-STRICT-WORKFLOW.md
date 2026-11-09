# Tiger 严格工作流实现与维护边界

本文件说明 Tiger 专用 Agent 入口的实现约束。操作者命令、Codex/DeepSeek Harness 配置和错误码见 [AGENT-CLIENTS.md](AGENT-CLIENTS.md)；请求及返回结构见 `bundles/trading/agent-workflow.schema.json`。本轮研发任务为 [T-2026-1021-0902 / issue 26](https://github.com/geniusdapeng-collab/workroom-tiger/issues/26)。

## 使用者、场景和范围

使用者是通过 Codex、DeepSeek Harness 或其他支持 shell/STDIO MCP 的 Agent 调用 Tiger 的操作者。目标是执行完整公开行情研究和本地模拟账户流程，并能区分“任务已接纳”“流程失败”“结果通过独立校验”。调用者无需自行拼接内部阶段。

三个具体场景：

1. 操作者要求查看当前公开美股研究结果。Agent 先检查运行条件，提交固定请求，保存 run_id，等待后读取并校验回执，分别展示新信号、已有模拟持仓、待成交订单和现金。
2. 桌面软件在运行期间关闭或连接中断。已接纳的业务进程继续运行；Agent 使用原 run_id 查询。若提交响应丢失，则用原请求和原幂等键恢复身份，不新建任务来猜测是否执行过。
3. 操作者复核历史报告或发现文件损坏。receipt/verify 从磁盘重新核验来源、阶段、文件和账户提交；损坏时拒绝结果，不通过重跑、修补文件或编辑状态来制造成功。

P0 为完整阶段执行、固定请求边界、独立模拟账户、持久幂等、进程超时、可核验回执和失败传播。缺少任何一项，Agent 都可能把部分执行或重复写入误报为完成。P1 为两种传输入口、项目配置生成/检查和操作指引，降低各宿主自行包装造成的不一致。P2 为后续 profile、平台和部署扩展；当前接口不接收这些扩展参数。

当前不提供真实券商下单、用户真实持仓导入、策略调优/审批、自由阶段执行、远程多租户服务、分布式锁或远程签名审计。内层模型仅按下述独立 model-required 固定合同接入。通用 WorkLoom 只读能力目录继续保持其权限约束。

## 方案选择

| 方案 | 成本与优点 | 风险与适用前提 |
|---|---|---|
| 只有 CLI，调用者自行等待和整合 | 接口数量少，适合只支持 shell 的固定客户端 | 各宿主容易重复实现生命周期和完成判据；后加 MCP 可能复制业务逻辑 |
| 一个 Python 运行器，薄 CLI 和专用 MCP | 阶段、账户、幂等和验证只有一个执行实现；CLI 可独立复核 MCP 的行为 | 需要额外协议、配置和进程清理测试；宿主挂载仍需实际连接证明 |

实现采用第二种。Node 校验请求、启动可信 Python、管理观察期限和协议返回；Python 执行完整流程并持有账户状态。工具请求不能指定 Python、state-root、环境变量、provider、阶段或策略。

## 固定 profile 和版本

新的 AI 研究和官网更新默认必须显式选择 `us-public-intelligence-paper-v2`。`us-public-model-required-paper-v1` 保留旧严格路径；CLI 兼容函数默认 profile `us-public-paper-v1` 仅保留旧调用/历史语义；`us-public-active-paper-v1` 为可选实验。各配置账户名称均为 `public-usd`，初始资金为 100000 USD；应使用独立 state-root，避免在同一账户混用决策/待成交语义。另建 state-root 不会继承旧账户，更不表示导入了真实券商资产。

model-required 的纯固定 policy 声明 `deepseek-flash` / `DeepSeek-V4.1-Flash-1017` 观察映射、JSON mode、场景化 thinking 及同提供方 `provider_native_search`，不读取 key 来生成 policy hash。实际配置仅从可信启动环境四变量取得，原两个 profile 继续剥离模型凭据。官方别名映射不是不可变模型快照；来源、观察日期及限制随 manifest 保存。`clean.llm_semantic` 与 `tech.sentiment` 明确非思考，`tech.risk` / `sector.narrative` 使用 high，最终 `decision.debate` 使用 max；策略来自 bundle `model-policy.yml`，各调用的独立证明绑定同一场景。请求通过仓库内官方 tokenizer 对完整序列化内容计价，输出预算自动取 `min(393216, 1048576 - 输入 tokens)`，禁止固定小预算或按字符截断证据。Flash 不支持托管 Responses web_search，应用通过原生 Search 收集全部 14 个既有主题，并把安全资料传给 Flash；Fetch 接口支持留在适配器，固定 collector 不调用。官方能力和加载步骤见 [AGENT-CLIENTS.md](AGENT-CLIENTS.md)。

各受控 profile 共用的运行正确性修复与可选实验见 [TIGER-ACTIVITY-EVALUATION.md](TIGER-ACTIVITY-EVALUATION.md)。评分、数据、单笔风险、总敞口和集中度阈值不变。严格入口均要求真实 A/B/C 入场模板，在排序后预约完整持仓及待成交预算；下一开盘只消费昨日冻结预算、信号时 ADV 摩擦档及实际开盘估值。今日收盘结论不能回写昨日信号开盘准入。错过指定 T+1 后释放待成交预算，无新鲜日 K 时也不会永久占用名额。旧待成交若无原信号冻结预算则显式拒绝，不能用今天数据补造昨天的证据。

本次按产品所有者明确授权修复原有执行缺陷；`raw.paper_execution_version=signal-budgeted-open-v1` 与 `raw.paper_execution_budgeted=true` 区分新运行。保留原 policy manifest 精确字节用于旧回执验证，由每次冻结源码 hash、版本标记和执行快照说明修复前后代码身份。旧已封存回执不会改写成修复后的执行；原 profile 的分数阈值、主/次池与产业链优先规则保留。新增 activity profile 另启用板块代理及主线溢出实验；没有足够收益与活跃度改善证据，正式自动化仍选择默认 profile。

市场日期在接纳时以 `America/New_York` 冻结，保存 UTC 时间、宿主偏移和市场日期。后续计算使用该日期，不能在运行跨日时重新读取“今天”来改变记账日期。外部数据自身的日期、缺失和来源另行披露。

严格入口的 Yahoo 日线下载显式保留交易所时区（`ignore_tz=False`），先把供应商原始带时区索引和值写入证据，再由现有观察边界投影到冻结的纽约日期。韩、台与美股混合取数时不能先丢失时区，也不能事后把无时区日期统一猜成 UTC、纽约时间或直接放行次日；真正越过纽约日期的观测仍然拒绝。日线日期标签不是实时成交时间，也不证明该交易日已经收盘。普通 Yahoo 入口保留原来的默认行为。参数语义见 [yfinance 官方下载文档](https://ranaroussi.github.io/yfinance/reference/yfinance.functions.html)。

profile 的请求字段、允许源、阶段注册表和策略语义是版本化合同。对外保持 `us-public-model-required-paper-v1` 的四字段请求，内部版本由冻结源码及 policy 标明。新的 `tiger.paper-execution.v1` 使用独立纸面账务合同和阶段顺序。旧回执按与冻结源码匹配的旧合同、原顺序和原字节验证。旧日记单列为历史假设，只有具备冻结批准、纸面成交和结算事件的记录进入校准。新研究使用独立状态目录。已升级纸面账拒绝由旧执行入口写入。

## 执行顺序和证据

唯一阶段定义位于 `trading_system/redline/execution.py::daily_stage_specs`，由现有 pipeline 注册表与 daily 外层闭环组成。当前实际顺序为：

```text
daily.preflight → state.reset → pipeline.prepare
→ search.collect → clean.rule_base → clean.llm_semantic → clean.cross_validate
→ data.prepare → tech.monitor → tech.cycle_linkage → tech.sentiment → tech.risk
→ tech.fusion → sector.narrative → layer1.mrs → layer2.sector → layer2b.chain
→ layer0.scan → layer3.tss → layer4.risk → decision.debate
→ iteration.calibration → report.emit
→ accounting.validate → report.initial → paper.step → paper.persist
→ journal.log → journal.settle → journal.stats → governance.initialize
→ review.daily → governance.emit → governance.verify → report.final_json
→ report.plan → report.markdown → report.html → daily.result
```

开始步骤时立即保存输入快照；结束前必须保存一次输出。重复、乱序、重叠、缺输出、跨线程写证据和未完成 I/O 均违反执行合同。异常结束记录 failed，并阻止成功推进。原两个 profile 的模型禁用，允许有明确证据的 passthrough；model-required 的五个 LLM 阶段全部 `allow_passthrough=false`，不可用、空结果或透传均为失败，不能把外层聊天模型当成内层成功。

model-required 在 `clean.llm_semantic`、`tech.sentiment`、`tech.risk`、`sector.narrative`、`decision.debate` 实际调用固定 Flash，每个响应要求正确 model/id/usage/finish_reason、完整闭合 schema 与确切条目覆盖。无风险空 alerts 有真实 assessment 才是有效评估，无买入也必须执行最终模型判断。确定性规则只做格式去重、来源交叉验证、数值因子/风险/账务校验及原模型输出的字段映射，禁止规则替代语义。原生 Search 依据净化后的原始发布域名计数来源多样性，仍保持原 ≥2 来源且至少一个 ≤T2 的阈值；跨域转载未必是独立报道，模型须区分同一原始报道、AI 评论与公司公告，该规则计数本身不证明报道独立。

最终判断前只为数值风控已经批准的股票补查公司层证据。每个批准标的发起一条定向 SEC 检索，结果最多四条。本契约使用过去 90 个自然日内有可核对发行人交易代码的 SEC EDGAR 正式文件，按冻结接单时点限制已公开信息。只有日粒度的发布时间必须早于接单市场日。第三方报道和宏观材料可以辅助理解，不能充当模型输出中的买入来源。最终模型仍可否决任何已批准标的。

`model.search_count` 保留 14 个主题检索的原口径。上游交叉核验按发行人或证券标识、事件、报告期或有效时点分组，归并同稿和同来源。同一公司不同报告期不互为佐证。未具备时点及身份的资料单列，证据为空的语义阶段仍要求真实模型返回有效的证据不足结果。

每个批准标的的定向检索结果独立记入 `disclosures.company_evidence`。新合同 `sec-submission-original/v2` 通过 SEC 申报元数据和原文核对发行人、受理时间及申报号。直接 SEC 访问需要可信启动环境的 `TIGER_SEC_CONTACT` 邮箱。缺少联络标识、源端拒绝或原文不可用时明确记录核验不可用，候选仅可观察。历史文件继续按原合同核验。 [SEC EDGAR 官方接口](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

独立验证器从事件和子 I/O 快照重算全部请求/schema/安全响应哈希、响应身份和 usage，重建完整模型输入文档及数值上下文，逐字段核对清洗/舆情/风险/叙事实际输出与 parsed 模型答案，并核对下游消费。最终模型选择只允许缩小原风险批准集合；人读计划中的价格/股数/止损/仓位/风险绑定原 TradePick。任何对应缺失或规则代写均为 `MODEL_REQUIRED_FAILED`，在账户指针提交之前失败。成功仅为 `completed`，不能发布 completed_degraded。原始远端响应不落盘，仅保存摘要；安全响应和模型 JSON 可重算，属于本地协作进程完整性证据，不是远端签名证明。

快照保留实际已知类型的数据，包括表格索引、列和值；非有限值用明确缺失表示，未知对象和循环引用拒绝序列化。输出不能只用数量代替内容，也不能用无约束的字符串转换掩盖类型。阶段输出包含实际子 I/O 的输入、输出或错误引用。

严格报告目前不包含白皮书 PDF 附件；「核心交易理念」明确显示未附 PDF，不生成活动下载链接，也不从源码仓、工作目录或旧报告中猜测补入附件。该边界沿用现有冻结源码、报告文件清单与回执合同，不增加未记录的附件读取。普通日报、研究报告及 JSON 转 HTML 工具只在实际 HTML 输出目录内有可读、路径安全且通过 PDF 基础结构检查的同名白皮书时显示下载，并使用实测字节数；未附或校验失败均在页面披露。基础检查覆盖 PDF 文件头、末尾 EOF 与交叉引用位置，不等于完整 PDF 内容或交易结论审核。网站导出保留页面声明的本地 PDF 及 HTML 原始字节，核验复制后的摘要；声明的附件缺失、不可读、结构无效、路径越界、含符号链接或多市场同 URL 对应不同内容时失败，不宣布站点生成成功。

`events.jsonl` 为顺序追加记录，每条绑定 run_id、序号、步骤编号、输入/输出引用、前一事件摘要和前一步输出。验证器重新计算哈希、顺序、状态与引用，并检查完整业务结果和报告。保存的 `verified=true` 标记或文件存在均不能替代这些检查。

严格模型日报的 `raw.research_audit` 从封存的扫描、精评、风控、公司证据、模型和复盘阶段重建。逐股记录标识、形态、期权证据、组合限额、来源与模型动作。逐关汇总分别给出分母、通过、拒绝、未知和不适用，重叠原因单列。报告生成前的阶段表截止治理链校验，独立回执从同一证据流补齐报告生成和封存阶段，分别列出子调用尝试、返回和失败。子调用返回只证明收到了记录，数据缺失和证据降级另行计数。

复盘中每条规则只报告实际覆盖的分母和已查命中。缺少来源快照时标记 `insufficient_evidence`，没有适用成交时标记 `not_applicable`。已查为零时命中值为空。逐笔复核记录可沿信号标识、纸面事件和阶段输出摘要追溯。

子 I/O 还必须符合写入端的语义合同：方法名非空且不含换行；原错误快照保留 error_type、固定脱敏 message 和布尔 outcome_observed 三个字段。新的 model-required 模型 I/O 可另附闭合的安全 diagnostic，内容限固定错误码、HTTP 状态或受控 schema 路径/关键字；不保存原响应、推理内容、凭据或任意异常正文。未及时观察到结果的 false 标志可以合法记录，不能被当成字符串或数字。即使重新计算所有文件摘要和事件链，错误字段类型、未知或不合法的诊断字段及非法方法名仍会被拒绝；原有无诊断证据不追补原因。

事件日历覆盖文件是可选输入。运行接纳时将冻结源码中的覆盖文件原样复制；未提供时显式生成空覆盖层并记录 `source=builtin_only`。内置日历规则仍由冻结源码执行。`calendar-input.json` 与实际输入字节均受核验；已有文件的格式错误不能被当成“没有覆盖文件”静默忽略。当前没有自动财报日期数据源。

## 状态、账户和进程

状态目录包含短期提交锁、账户锁、幂等键映射、运行目录和账户版本目录。对外业务请求仅含 schema_version、profile、paper_account、idempotency_key 四个字段。第一次接纳持久化请求身份后，才启动独立业务进程；同键重试只能返回原身份与实际状态。

新 intelligence run 在 worker 正常停止后可通过单独 `resume` 控制操作继续原 run_id。resume 不创建新 identity，不改变原 request/source/policy/account-before/calendar/deadline；父 attempt 的失败事件、I/O、work、日志与进程证据只增不改归档，新 attempt 从原账户与日历重建。已收到 native 先复算 usage；unknown/in-flight 调用保留预留并要求先对账。engine 已完成但 seal 前崩溃只做 closeout，不重跑 pipeline。

`supplement` 不是旧 run 的续跑：它从独立核验的父 receipt 读取 pending candidate，建立新 scope、新 admission 和完整新 run。旧快照不扩写，旧批准不复用；即使原信号已过期，也只标记 `original_signal_expired` 并执行新研究。

运行期间，所有业务写入发生在该 run 的工作目录。账户提交的步骤为：

1. 持有排他的账户锁，记录当前账户指针和只读输入快照。
2. 完成全部阶段，生成最终 JSON、Markdown、HTML、复盘与账本。
3. 建立包含父指针及文件摘要的新账户版本。
4. 封存回执，独立核验阶段、文件、来源、日期、金额和父指针。
5. 原子替换 `accounts/public-usd/current.json`；这是账户提交的权威位置。

替换前的失败不会使该运行成为已提交账户；替换后的状态写入失败也不能抹去已发生的提交。只读查询沿当前指针和父版本链证明历史提交，不依赖可变状态文件。多个普通 JSON 文件写入并非数据库事务；实现采用准备文件和原子发布指针，并披露目录同步失败或缺少同步确认的情况。

进程分为提交/查询进程、supervisor、guardian 和 engine。业务进程与客户端观察进程分离；断连或取消观察不会取消已接纳任务。supervisor 负责截止时间、清理与账户发布；guardian 独立监测 supervisor 的生命通道，并约束 engine 所属进程组。清理在回收保留的组首进程前执行，不能仅凭 PID 文件推断任务是否活跃。

控制管道关闭也不等于 guardian 已退出。supervisor 先验证完整成功控制记录，再在原业务期限内观察真实退出：Linux 使用保留子进程待回收状态的 WNOWAIT；macOS 读取自有组首进程的 PID、父 PID、组 ID 和僵尸状态。身份不符、查询失败或到期均不能作为成功。观察到退出后仍先清理进程组，最后回收并要求真实退出码为零，才允许发布账户。

状态查询先采样运行锁，再读取状态文件和账户提交链。写者完成最后的状态写入后才释放运行锁，同一个 run 不会重新启动。若采样时锁仍被持有，查询可以保守地多返回一次 running；不得把完成前的状态/指针快照与完成后的无锁状态拼成虚假的 interrupted。已发布的账户提交仍优先于可变状态文件。

当前支持 macOS/Linux 所需的 POSIX 锁和目录写入语义，Python 最低 3.11。运行环境按允许列表构造，保留原 HOME。仅 model-required 的业务启动额外保留 LLM_BACKEND、LLM_BASE_URL、LLM_MODEL、LLM_API_KEY 和用于 SEC 核验的 TIGER_SEC_CONTACT。请求不能覆写这些设置，其他 profile 不继承这些设置。业务缓存和临时文件在 run 内。每次新业务进程在首次使用 yfinance 前设置实际缓存位置。第三方包身份记录解释器字节和安装元数据，未声称逐文件证明全部依赖供应链。

## 时间、并发和错误

单个业务任务的总期限为 2400 秒。Node 单次提交/查询观察上限为 60 秒，CLI 默认等待窗口为 2460 秒；观察超时和业务超时是两个不同结果。

原两个 profile 的公开搜索 45 秒收集预算适用于每一批查询；主题会展开为多批查询，不能把它描述为整个搜索阶段只花 45 秒。收集期限之后的数据不再参与决策，已启动线程另有最多 15 秒的收尾等待；未结束的线程阻止流程成功。model-required 使用同步、有界响应的原生 Search，每次请求连接/读取期限和 1 MiB 响应上限由适配器固定；无跨模型或来源回退，任一必需主题失败即停，最外层 2400 秒业务期限仍然生效。

一个 state-root 的模拟账户同时只允许一个业务写者，短期提交锁负责键映射和锁交接。专用 MCP 最多允许四个在途请求；多数为只读观察，这不会放宽账户写互斥。

所有外部调用均需区分普通来源不可用和运行合同/证据失败。普通公开源失败按原有真实源回退规则处理并披露；不能切换合成数据。证据写入、日期边界、阶段合同、账户状态或回执校验失败必须终止严格任务，不允许转为合法 LLM 透传。

严格工作流的 Stooq 串行日线批次采用每批连续三次无有效产出的可用性探测预算；观察到有效 DataFrame 即重置连续计数，已取得的原始帧保留。预算耗尽后，未尝试的 ticker 交给原有可信真实源回退链，不降低覆盖率、新鲜度或数据质量门槛。少数普通缺失不能证明远端服务失败，所以证据只声明 `source_exhausted=true`、`reason=availability_budget_exhausted`、`service_failure_asserted=false`，列明尝试数、无产出数、按类型汇总的错误及未尝试清单；不序列化原始错误正文。批次恰好结束或空输入不会伪报预算耗尽。普通非严格 Stooq 调用默认仍无此探测数量上限。

`EvidenceWriteError`、`ExecutionContractError` 和 `StrictObservationError` 必须原样终止，不能作为无产出累计后继续回退。严格模式保持 `allow_challenge=false`，遇到访问验证仍拒绝，不求解或绕过挑战。三次是探测次数边界，不保证整个公开取数阶段的墙钟上限；各原有来源仍有自己的网络等待，最外层2400秒截止时间继续有效。该修复只避免一个连续无产出的串行来源先消耗数百次请求，不能保证公开源一定提供足够数据。

错误返回统一 envelope 和受控 code/message/run_id/stage/details_ref；不把原始 HTTP 正文、密钥或完整环境返回给 Agent。详细退出码见操作指南。

## 验证、发布和恢复

新增 Python 测试验证真实本机进程、锁、幂等、截止时间、进程组清理、输入输出证据、账本版本和故障注入；Node 测试验证协议、模式、取消、流边界和配置保存。离线夹具明确标识为 TEST_FIXTURE，不能替代公开行情实跑。测试结果由各次执行日志、源摘要和独立验收回执提供，不在本文维护容易漂移的通过数量。

云端现行 `workloom-ui-contract` 保留全部原有检查，并在已安装 Python 依赖之后执行专用 Node 套件。第一 main 的 MC-041 加载时序故障仅修测试就绪判据；原始完整内容、顺序和几何断言继续存在，详见 [TIGER-CLIENT-REPAIRS.md](TIGER-CLIENT-REPAIRS.md)。

发布需普通 PR、准确候选 CI、会话审查与独立验收、普通 main 合并及读回。实际公开数据执行另记录源提交、冻结文件树、run_id、市场日期、模型缺失和 receipt/verify。配置解析和原始 MCP 通信不证明某个宿主已挂载；Codex 项目信任仍由正常客户端机制处理。

回退代码使用普通版本控制流程。失败任务和已提交账户证据保留；不得自动删除幂等键、回退账户指针或通过新键隐藏失败。需要新的逻辑任务时，明确记录修复原因和新任务身份。若代码回退涉及不同 policy 语义，先使用匹配验证器检查历史账户兼容性。

这些保证适用于同一用户下协作运行的进程。拥有任意源码、证据及 shell 写权限的主体仍能绕开本入口；本地哈希链不能替代权限隔离、远端签名或不可回滚存储。实现也不保证同时恶意终止所有监护进程、任意外部进程并发修改或所有主机断电故障下的恢复。
