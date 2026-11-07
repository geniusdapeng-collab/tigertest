# 持续情报与只读主题研究

本入口服务于“研究一个主题、跟踪审定来源、核对某人原话”等任务。来源采集先保存原文版本和真实观察时点，研究使用固定、有界的主题快照；报告逐结论引用原文，同时说明反证和资料缺口。主题报告不创建 paper 账户，不批准交易，也不是既有严格 paper 流程的完成回执。

## 对话入口与权限

用户说明目标，外层 Agent 根据可信部署准备请求并调用 CLI 或独立 MCP。用户不必编写 JSON。Node 只做协议和进程传输，Python 负责来源准入、任务身份、预算、快照、模型、检查点与验证。现有 `tiger-agent` 的六个 paper 操作和四字段请求继续保留。

| 可信启动模式 | 可用行为 |
|---|---|
| `readonly` | 查询既有进度、独立核验报告、读取所选快照证据、查看来源健康；不创建目录或启动模型 |
| `acquire` | 上述行为，加创建主题、登记 watch、执行有界采集 tick；不运行语义研究模型 |
| `research` | 上述行为，加提交只读主题研究；内置模型来自可信环境 |

解释器、state-root、archive-root、tenant、policy-file 和模式只在可信启动参数中选择。请求不能传任意 URL、provider、模型、凭据、路径、自由 argv 或账户权限。人物、机构和公司身份必须来自可信来源目录；默认证券身份为本仓审定名单中的 `us:NVDA` 等值。新人物及其任职有效区间、机构条线与授权 feed 由维护者配置，网页和自然语言不能注册自身为可信来源。

`health` 中的 registered/enabled/credential_ready/entitlement_ready 与实际 last_success/connected 分开。只读进程不接收模型密钥；它报告的凭据缺失不能证明另一个 research 部署未配置凭据。配置通过、工具发现、公开原文实连、模型研究完成是不同证据。

## 三个完整场景

“全面研究 AI 数据中心需求，比较公司指引、行业数据、机构观点和反证”：Agent 建立问题及主体范围，选择必需、补充和有理由的不适用维度。系统有界预取后才封研究快照；每条结论有精确引用、事实/观点/预测类别和适用条件。授权研报或行业数据未接通时保留缺口，不能用其他类型的文章数量替代。

“跟踪关注公司的公告，没变化不用提醒”：Agent 以 acquire 能力登记 watch，常开 worker 对到期来源领取有代次的短租约，每次 tick 创建新的观察身份。条件请求、原文/提取配置摘要、游标和来源成功水位决定变化；未变保持安静，更正、撤回和持续失联只记本地待复核事件。watch 和 tick 不自动启动付费语义研究。

“确认某位专家原话，并与行业事实对照”：Agent 使用已审定人物身份及任职区间，区分原话和二手转述。研究报告引用可定位的原文片段，列条件、相反证据与未知项。没有原始讲话或身份不明时如实说明；人物观点不变成买入批准。

## CLI 与 MCP

从仓库根目录使用 `node scripts/tiger-topic.mjs`。所有路径必须由已核实的部署取得；下面是可移植参数示例，示例路径不是可直接执行的本机部署。

```bash
node scripts/tiger-topic.mjs health --python /absolute/path/to/python \
  --state-root /absolute/path/to/topic-state --archive-root /absolute/path/to/intelligence-archive \
  --input-file health.json
node scripts/tiger-topic.mjs create --python /absolute/path/to/python \
  --state-root /absolute/path/to/topic-state --archive-root /absolute/path/to/intelligence-archive \
  --mode acquire --input-file topic.json
node scripts/tiger-topic.mjs research --python /absolute/path/to/python \
  --state-root /absolute/path/to/topic-state --archive-root /absolute/path/to/intelligence-archive \
  --mode research --input-file research.json
```

`health.json` 为 `{}`；`topic.json` 示例：

```json
{
  "question": "研究 NVDA 的需求、经营披露与关键反证",
  "entity_ids": ["us:NVDA"],
  "horizon": "current",
  "material_policy": {
    "issuer_disclosure": "required",
    "industry_data": "required",
    "formal_research": "required",
    "institutional_commentary": "supplemental"
  },
  "idempotency_key": "operator-topic-scope-001"
}
```

研究请求恰好包含 `topic_id`、整数 `version`、`idempotency_key`。前两项必须使用 create 的实际回值。提交返回真实 `job_id`；随后 status 和 report 都使用 `{"job_id":"实际回值"}`。`research --wait` 只轮询该身份，观察窗口可通过可信 CLI 的 `--wait-seconds` 设置；它不会重复提交或改变任务总期限。最终仍须调用 report 独立验真。

watch 请求含 `topic_id/version/source_ids/cadence_preset/idempotency_key`，cadence 为 `core/normal/low/daily`。effective cadence 取可信来源与域名限制中更严格者，不承诺将不能高频的来源提高到请求频率。`tick` 输入 `{}`，只执行一个到期、有限额的采集单位：最多 48 次实际 HTTP 请求、24 次正文 fetch、240 秒，持有 300 秒租约；包含 robots 探测与重定向的请求和字节均计入共享 host 账本。默认 core 间隔为 300 秒；可信来源或域名要求更慢时取更严格值。常开宿主可调用 `MonitoringWorker.run(max_ticks,max_seconds,interval_seconds)` 做有界运行；主机休眠和断网期间只记录检查空窗，不回填首次观察。

独立 STDIO 服务为 `node scripts/tiger-topic-mcp.mjs`，使用同样的 `--python/--state-root/--archive-root/--mode/--tenant/--policy-file` 可信启动参数。它提供：

- `tiger_intelligence_topic_create`、`tiger_intelligence_research`、`tiger_intelligence_watch`。
- `tiger_intelligence_status`、`tiger_intelligence_report`、`tiger_intelligence_evidence`、`tiger_intelligence_health`。
- `tiger_intelligence_tick`，仅在 acquire/research 部署中可见。

readonly 不显示写工具，直接调用也会在 Python 启动前拒绝。初始化和 tools/list 不启动 Python 或创建库。服务器使用 initialize → initialized → tools/list 生命周期，最大 128 KiB 帧、四个在途观察请求、有界输出背压；取消仅停止该次观察，已接纳独立研究 worker 可能继续。

支持本地执行的 Codex/Qoder Agent 可以直接使用同权限 CLI。可选 MCP 条目见 [topic-mcp.json.example](../config/agent-clients/topic-mcp.json.example)；模板只提供服务器 argv，不写全局设置、不自动提高信任或权限、不证明宿主已加载。原 paper 配置生成器继续生成 paper workflow 服务器，不会悄悄增添 research 权限。

## 资料范围、容量与完成语义

12 个资料维度为：公司法定披露、经营/IR、正式研报、机构评论、行业数据、宏观政策、市场结构、资金/披露持仓、人物原话、竞争技术、新闻突发、替代观察。每个实体×维度有独立覆盖状态。`not_applicable` 必须为 `{"status":"not_applicable","reason":"与本问题无关的具体原因"}`，缺订阅、抓取失败和超预算不是不适用。

每次研究的固定快照绑定来源目录、主题版本、cutoff、事件前沿、成员及选择证明。数据库先按范围选择；候选扫描、关联证明、文档数、字节和校验时间分别有限额。默认归档观察窗口最多回看 760 天以保留财务/统计基线，搜索问题另按 horizon 及资料窗口规划。新资料进入后续快照，原快照不跟着活动库变化。超界显式 partial，不截断原文冒充完整资料。

新 admission 默认明确启用 `model_snapshot_view_contract = tiger.topic-model-snapshot-view/v1`。归档中的完整 manifest、成员、版本及事件/选择证明保持原字节，并继续由程序逐成员核对原始文档和真实归档回执；模型请求仅把重复的密码学证明替换为固定字段的快照上下文，保留 snapshot ID、完整 manifest 摘要、cutoff、来源目录、主题/范围、数量、边界和缺口。所有原文 documents 及其完整元数据仍原样进入研究和独立复核，不裁原文、不删资料、不改变输出 schema、模型、推理档位或风险规则。report 从完整归档重新构造该视图，逐对象核对实际请求中的原文，并检查两个模型阶段均收到最终快照的每个原文；引用派生和 v2 重答继续绑定完整 manifest、原请求与响应，重答标记同时绑定视图摘要。旧 admission 缺该合同或显式为 null 时继续发送原完整快照同字节，不回填历史请求。

同一滚动网址的最新版本按已核实的报告期分别选择；新季度不能覆盖旧季度仍有效的版本。只有同源、同合同、同解析表示及同报告期的完整原文，才追加 `supersedes` 和 `observed_source_version` 关系；该关系表示本系统观察到同一来源版本替换，不代表发布者正式声明更正或撤回。报告期未知时保留未知标记，不能填满指定报告期覆盖。旧记录保持原字节；兼容旧归档时只在有界索引内查找并最多读取一份受大小限制的前版元数据，超界留下无法核实的缺口。

快照的 source_ids 在查询前限定为当前启用、授权有效且允许 topic_analysis 的可信来源，不能因同一实体曾经搜过，就读入已撤权或仅允许 discovery 的历史原文。覆盖不仅检查引文存在，还检查完整正文、可信资料类型和实际主体/作者依据；query 的 requested entity/material 标签不能独立填满必需覆盖。

预取与快照共用同一来源选择：只选择 topic 中 active 资料维度对应的可信来源，再与当前权限相交；保留默认 search 与必要的 web_page/身份发现依赖。明确 not_applicable 的无关资料来源不因历史归档存在就进入窄主题快照。此选择不裁正文、不把搜索标签升级为正式研报或人物原话，也不是固定核心方向与热点雷达选题器。

每个正式报告的时间覆盖按封存 cutoff 检查原始发布日期，重读报告不会改用今天的时间。current 使用各资料类型的窗口，显式 week/month 使用 7/30 天；baseline 使用各资料的历史窗口。当前综合问题中的已核实 SEC 法定财报（10-K、10-Q、20-F 及修订）可作为最长 760 天的财务背景基线，不能冒充近期新增催化，也不表示已经核实它是最新发布版本。日期精度不足时仅用保守区间比较，不补造时区或发布时间；新抓取与 Last-Modified 不能把旧观点刷新成当期证据。旧期原文继续保留，但无法满足所需时间覆盖时，报告明确 partial/unknown，并保留 freshness_checks 和原因。

默认 `tiger.topic-policy/v1` 具有 2400 秒从接纳到收尾的总期限，保留 510 秒供独立复核、30 秒供收尾；最多两次取材/研究版本。整个任务的采集总上限为 120 请求、96 次 fetch、32 MiB，第二次补查扣除前一次已观察用量。语义研究最多 12 次真实调用尝试，任务 token 配额与单次模型合法窗口分别计量；每次 outbound（含 schema repair）发送前持久预占完整输入加最大合法输出，按实际 usage 结算。默认保留同一模型、high 分析和 max 独立复核，不压小输出窗口换取通过。

新 admission 的 `model_transport_contract = tiger.topic-model-transport/v1` 将 topic chat 的 socket idle 等待上限设为既有响应总上限 480 秒，并用独立 POSIX alarm 将一次实际运输限制为 `min(480, 任务剩余-30-分析阶段预留510)`；max 复核只扣收尾的30秒。更早的 worker watchdog 保留原 handler、停止原因和扣除实际耗时后的剩余时间。旧 policy 缺字段或为 null 时保留原180秒 idle 与原请求字节，paper、模型/推理档位、输出窗口与 schema 不变。Requests 的 [read timeout 是字节间隔而非总 wall](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts)；DeepSeek 官方说明非流式请求可能返回 [空行 keep-alive](https://api-docs.deepseek.com/quick_start/rate_limit/)，因此心跳不延长本地绝对期限，也不能证明供应商已开始或完成推理。

新合同在原 attempt 的 unknown 结算事务中追加封闭的 `model_failure_observed` 事件：绑定 job/request/policy/source/attempt/stage、失败阶段和代码、elapsed、实际 deadline owner 与安全 HTTP status/body prefix hash/观测字节/complete。没有原生 checkpoint 时仍为 unknown，观测到零字节不代表零费用；不保存原始正文、reasoning、凭据或异常文本，也不自动重发。旧任务缺少这些新观察时不追溯补造传输原因或实际用量。

只有完整请求经实际 tokenizer 证实超出模型窗口时，才按完整原文边界拆批，再保留来源 id/hash/精确引文作合成；独立复核也按完整原文分批并做全局一致性复核。单篇原文本身超窗，或完整批次/合成所需预算、时限仍不够，明确失败而不裁剪证据。已完成批次与实际模型响应在同 job 内恢复，避免中断后再次付费。

可信 `--policy-file` 可覆盖原政策中的同名字段，随 job admission 冻结并计算摘要；网页和请求无权改它。调整墙钟或调用/token 配额时，须记录真实失败及容量观测，再重新验证；旧任务仍受原 admission 约束。解析器使用可信 Scrapling HTML policy，PDF/HTML 的解析子进程由独立时间、字节、页数和内存边界保护；原始字节保留，超限资料是 partial。

任务队列默认最多 128 个未终结研究，同一 state-root 只启动一个实际 detached worker，按接纳顺序处理。其他 job 留在持久队列，排队时间计入原总期限；到期任务明确失败。首次 research 将该 state-root 绑定到部署 tenant，其他 tenant 使用独立 root。文件锁保护 worker 和模型调用，SQLite 原子预占防同键及并发透支。任务的状态、预取计划、采集回执、固定快照、模型响应和阶段输出持久保存。相同键只找回原 job；确定已完成的阶段和模型响应在同一个 job 内恢复，不作为新研究调用证据跨 job 复用。来源目录变化使原任务拒绝继续，不能用新权限重解释旧快照。

admission 从可信入口读取和准备之前开始计时，构造、完整源码指纹、数据库锁等待和落盘均计入原期限。收到的模型响应先持久绑定原 attempt 和原请求，再结算用量；中断后只对账原响应，缺失或篡改时保留预占并阻断，不自动重发。

引用使用不可变版本文档的 `id`（实际采集为 `docver_*`），不能使用逻辑 `doc_id`（`intel_*`）、版本摘要或 URL。输出 schema 对本次、每个原文批次及最终合成分别绑定合法文档、实体和 active 资料维度。报告中的 quote 必须通过原文精确连续片段校验，包含 NBSP 等 Unicode 空格与标点；文档 ID、数值、大小写、标点、换行和空白数量均不得自动改写。

新任务默认在不可变 admission policy 中明确启用 `citation_resolution_contract = tiger.topic-citation-resolution/nbsp-to-original/v1`。只有模型 quote 本身不 exact、且在同一不可变文档中存在唯一连续原文片段时，才允许将模型的普通空格 U+0020 一对一物化为该原文对应位置的 NBSP U+00A0；不允许反向替换、其他 Unicode 规范化、tab/换行、增删或合并空白、歧义匹配及跨文档查找。物化后的引文继续运行原有 exact、主体范围和正文完整性校验，不因此取得资料权限或事实资格。

这一步产生单独的 analysis derivative 和逐处 resolution 回执，不改模型 raw response、checkpoint 或 IO proof 的 parsed 内容。回执绑定实际 response/request、原始及派生 analysis 摘要、原始模型 quote、原文 text 摘要、文档 ID、连续字符/字节位置和每处差异。独立 report 验证从真实模型回复及封存原文重新计算派生正文与全部回执，不接受 verified 标志或只重算摘要的伪造；独立模型复核看到派生正文及校对依据，可读报告也标明引用校对。旧 admission 缺少该合同或显式为 null 时只接受原有 exact 模型回复，不能通过添加可变 trace 字段启用。旧报告继续使用原发布源码验真，字节和证据语义不回填。

结构通过后，若引文仍不符合上述已接纳合同及确定性 exact 校验，整个 job 最多申请一个同模型重答身份，完整保留原快照、原文、旧回复、校验诊断及实际用量；可选择更短的完整连续片段。再次失败则终止，不无限修复。能按已接纳 NBSP 合同确定性定位的首次回复直接进入独立复核，不额外申请付费重答。既有全局有界 schema retry、12 次尝试、完整合法模型窗口和原墙钟仍生效；每次实际 outbound 独立计量。中断后复用同一原始响应和校对依据，不重发已收到的付费回复。

新 admission 默认明确启用 `analysis_feedback_contract = tiger.topic-analysis-feedback/v1`。分析提示直接说明：事实的每个支持及反证原文都必须 `completeness=complete`，不能用 partial/snippet/unknown 原文或换成“观点”标签绕过；quote 必须来自同一个被引用文档。若失败，重答仅收到首个坏结论与引用的有界定位（数组位置、文档 ID、完整性及 quote 摘要），不自动跨文档找替代、不改变原文或模型答案。v2 重答检查点绑定原请求、原模型 response 和实际重答；report 从原回复与固定原文重新计算诊断。旧 policy 缺少该字段或为 null 时仍使用原提示和 code-only v1 检查点，不回填历史任务。诊断能帮助模型纠错，但不保证一次重答必定成功；未修正时按原门禁失败，资料不足可明确列为 unknown。

模型回复的 choices 缺失、为空或形状错误时，若原 response identity/model 与 usage 可以核实，仍先封存去除 reasoning 的安全账务回执并结算已知用量，业务继续按原语义门禁失败；失败不是免费调用。缺 identity 或 usage 时保持 unknown 预占，重放阻断，不编造用量或另发替代请求。

admission 与报告同时绑定 trading_system 下全部 Python 源码、两个 topic Node 入口、共享 workflow client、模型政策、requirements 与固定 tokenizer 资产的内容指纹。恢复和 report 验真发现实现变化时返回 SOURCE_CHANGED，应使用原发布版本继续核查；缺少此绑定的早期实验 job 返回 LEGACY_JOB_REQUIRES_MATCHING_RELEASE，不把旧检查点重新解释为新严格任务。

| 状态 | 可以对用户说什么 |
|---|---|
| accepted / running | 已接纳或正在进行；继续查询原 job_id |
| completed | report 已独立核验，必需覆盖达到本 topic 合同；仍不是 paper 或实盘完成 |
| partial | 有已复核的可用结论，但必需资料或容量边界存在缺口；明确列出 |
| failed / interrupted | 本次未完成；保留原身份、失败和检查点，不换键掩盖 |
| blocked，USAGE_RECONCILIATION_REQUIRED | 某次付费投递/用量不明，保持预占；先核对原调用，不再发送 |

报告逐条验证 document ID、完整原文中的精确引文、主体范围、资料类别和可引用事实的全文状态；独立复核重新读取同一固定快照，并核对实际同模型响应、请求摘要、usage 和报告摘要。报告固定回答：研究到什么时点、主结论、变化、各维度证据、支持与反证、仍不知道什么、何时因何复核。读取 report 不联网、不补跑模型、不修改账户。

CLI/MCP 的 `report` 返回 `data.schema = tiger.topic-report-view/v1`：`data.report` 是经独立核验的原始封存报告，`data.sealed_receipt_sha256` 等于该报告的 `receipt_sha256`；`data.current_source_status` 是报告外层的本地当前观察视图，不计入原报告的封存摘要。读取完成状态与可读正文时使用 `data.report.state`、`data.report.readable_report`，同时检查外层 `current_source_status`。

后续 monitor 对同一主题版本和快照实际来源合同观察到 404/410 时，视图列出 `source_unavailable`、`needs_review`、实际观察时点及受影响的文档/结论引用；封存报告的原始字节、cutoff、引文和摘要保持原样。来源不可用不能自动推成历史原话或事实已被证伪。来源合同变更、观察库缺失/损坏、没有同主题同版本观察或观察范围超界都明确标记；`unknown` 或 `no_known_warning` 不表示刚刚联网检查过或当前来源健康。查询不创建 monitor 目录、不采集网络来源，也不自动清除待复核记录。

## 验证与边界

离线入口：`python -m pytest tests/test_intelligence_topic.py` 与 `TIGER_TEST_PYTHON=/absolute/path/to/python node --test scripts/tiger-topic.test.mjs`。覆盖真实 SQLite/快照/模型运输链的 HTTP 夹具、未知投递、阶段恢复、返回响应后崩溃、预算拒绝、租户、篡改、必需缺口，以及 STDIO 生命周期、取消、隐藏工具和真实 Python health/create。夹具不是生产质量、吞吐或来源实连证据；真实 source/model 验收结果由本次发布回执另行保存。

银行完整研报、专门行业数据、人物身份和私有渠道只有在来源、授权、凭据、时点与原文合同成立后才能成为覆盖证据。尚未接通时保留明确缺口；本入口不采购订阅、不登录私有账号、不绕过站点限制，也不以多 Agent、多爬虫或更多文章代替研究完整性。

## 公开试点来源与方向边界

`config/intelligence/pilot-registry.example.json` 和配套 catalog 是小范围 AI 基建公开试点，不是全市场默认配置。NVIDIA 新闻稿只取审定 newsroom 主体正文；Goldman Sachs 公开节目原文登记为机构评论/人物原话，不冒充授权完整研报，2026-09-01 内容属于历史背景，不声称当前任职；Fed G.17 使用审定的公开统计 TXT 下载合同，只允许同源精确 GET 路径且无 query，不扩大成整个域名 robots 例外。robots 缓存保留真实失败；已观察到的重复 Content-Type 只有同时包含 plain text 且正文是有效规则时接受，HTML/challenge 仍拒绝。

来源准入白名单、处理数量上限与主题版本不能代替研究方向选择。当前 paper 流程的主线池在后续评分阶段收敛；本入口允许明确主题与实体范围，但尚未新增“固定垂直核心方向＋有界热点雷达”的上游选题器，也没有自动为所有行业维护核心信源组合。不同用途分别采用事件新鲜度、最新已发布财报/统计版本和历史背景，不能把搜索回看窗口当成来源实时性或长期 SLO 证明。
