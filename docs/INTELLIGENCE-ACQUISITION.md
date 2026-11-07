# Tiger 情报采集、证据与离线评测

本指南面向通过 Codex CLI、DeepSeek Harness 或本仓 CLI 使用 Tiger 的操作者。
新的研究必须在业务请求中显式选择 `us-public-intelligence-paper-v2`。`run` 和
`describe` 都要求 `profile` 字段，省略会被拒绝；历史 profile 保留旧回执语义。
它把「取得线索」「获得可评分的
证据」「获准执行纸面计划」分别记录。采到文章、HTTP 请求成功、模型完成和检查单
有状态，都不能单独表示研究质量合格。真实证券操作仍由人执行；情报命令不连接
账户、不下单，也不修改原有数值风险阈值。

## 三个常见场景

1. **为当日候选做研究**：完整工作流先取得行情并冻结独立数值候选，再针对宏观、
   六条产业链、板块、候选和已接入持仓/待成交记录建立研究问题。采集、原文核验、结构化提取、
   逐目标证据资格和冻结证据包完成后，语义分析只引用各目标自身的可用证据。
2. **单独同步或短时监测来源**：`tiger-intelligence` 只采集已登记来源。同步可建立
   下一轮研究的原文档案；监测次数、间隔和墙钟有上限，固定请求中的截止时点，
   不将后来抓到的可变网页当作早已存在的历史版本。
3. **复核一次判断或比较搜索工具**：用 `status` 看失败、空结果、缺口和实际用量；
   用 `evidence` 验证冻结包或某一原文版本。工具比较读取独立标注的数据集和冻结
   capture，在离线程序里计算指标，不调用模型评价自身，也不把工具结果并集当真值。

完整研究仍使用 [Agent 操作指南](AGENT-CLIENTS.md) 的 `tiger-agent.mjs` 及同一
四字段业务请求。下面的采集入口是独立操作，不是完整研究成功回执。

## 受控采集入口

从仓库根目录运行，显式指定已安装依赖的 Python 3.11+。解释器、归档目录、网络
访问模式、可选来源启用和监测边界属于可信启动配置，不能放在请求 JSON 中。
默认 `readonly` 在打开输入文件和创建归档之前拒绝 `sync`、`monitor`。

```json
{
  "schema": "tiger.intelligence-request/v1",
  "request_id": "operator-intelligence-20261105-001",
  "as_of": "2026-11-05T00:00:00+00:00",
  "symbols": ["AAPL", "MSFT"],
  "source_ids": ["fed_policy", "bls_calendar"]
}
```

`as_of` 必须含时区且不能在未来。`symbols` 为仓库审定内嵌美股池中的 1–100 个
唯一代码；研究境外公司可以在正式语义证据中保留其身份，不由这个入口扩大交易池。
`source_ids` 只能选择可信 registry 中已启用的 ID。请求不接受 URL、自由 query、
文件路径、env、凭据、模型、账户、资金或权限设置；未知字段、重复 JSON 键、
非有限数字和超过 64 KiB 的请求会被拒绝。

```bash
node scripts/tiger-intelligence.mjs sync \
  --python /absolute/path/to/python \
  --archive-root /absolute/operator/path/intelligence-archive \
  --mode acquire --input-file intelligence-request.json

node scripts/tiger-intelligence.mjs monitor \
  --python /absolute/path/to/python \
  --archive-root /absolute/operator/path/intelligence-archive \
  --mode acquire --cycles 3 --interval-seconds 30 --wall-seconds 300 \
  --input-file intelligence-request.json
```

单次采集的工具上限固定为：24 次请求（含 Fetch）、12 次 Fetch、8 MiB 解码后的响应实体字节、
1 轮、串行执行、60 秒业务时限。底层传输还会检查域名、重定向、目标 IP、响应大小
与时间。监测最多 12 轮，间隔 1–300 秒，墙钟 60–3600 秒；没有足够剩余时间容纳
一轮完整业务时限时停止接纳下一轮。Node 宿主另有进程时限和输出上限。完整研究
采用其审查过的采集计划限额；这些工具预算与模型合法上下文/输出窗口分别管理。

同一个 `request_id` 与轮次绑定固定计划。重复相同请求读取原冻结结果，不重新付费
采集；相同 ID 改动截止、股票或来源会冲突。监测每一轮有独立 job 和回执。
中断可能留下在途或未知结果，必须用原身份核查，不能将观察中断写成采集完成。

返回的 `semantics=acquisition_only_unassessed` 和 `account_connected=false`
说明该命令仅建立原文档案。来源没有结果、访问失败、身份或凭据缺失、超预算、
发布时间不明与资料冲突会保留明确状态；采集到正文不自动填平研究问题的证据缺口。

```bash
# job.json: {"job_id":"intel_<32 hex>"}
node scripts/tiger-intelligence.mjs status \
  --python /absolute/path/to/python \
  --archive-root /absolute/operator/path/intelligence-archive --input-file job.json

# evidence.json: {"bundle_id":"bundle_<32 hex>"}
# 或 {"document_id":"<原返回的文档版本ID>"}
# 或 {"bundle_id":"bundle_<32 hex>","claim_id":"<原返回的claim ID>"}
node scripts/tiger-intelligence.mjs evidence \
  --python /absolute/path/to/python \
  --archive-root /absolute/operator/path/intelligence-archive --input-file evidence.json
```

查询使用只读归档，不创建数据库、不采集、不加载凭据。证据返回前重新检查元数据
和原文 blob 哈希；不存在、跨租户或损坏的证据拒绝返回。单次结果超过 8 MiB 时
返回 `OUTPUT_TOO_LARGE`，不把部分正文当完整成功。

## 来源合同与凭据

来源合同在 `trading_system/intelligence/registry.py`。只有可信配置可登记端点、
域名、权威范围和 IR 发行人身份；模型、搜索响应和网页中的「官方」字样无权注册
或提升来源。默认来源和可选适配器分别报告是否真正可用。

| source_id | 用途 | 启动条件与边界 |
|---|---|---|
| `deepseek` | 原生 Search / Fetch；完整研究复用原 DeepSeek 客户端审计 | `LLM_API_KEY`；provider 正文与原站字节分别记录，不把摘要冒充全文 |
| `brave` | 可选新闻检索 | 默认关闭；启动 `--enable-source brave` 且 `TIGER_INTEL_BRAVE_API_KEY` 非空 |
| `tavily` | 可选检索与提供商 Markdown | 默认关闭；启动 `--enable-source tavily` 且 `TIGER_INTEL_TAVILY_API_KEY` 非空 |
| `exa` | 可选语义发现与提供商正文 | 默认关闭；启动 `--enable-source exa` 且 `TIGER_INTEL_EXA_API_KEY` 非空 |
| `web_page` | 对已发现 URL 的原站字节抓取与附件 | 仍受传输目标/IP/重定向限制；请求不能直接指定自由 URL |
| `sec_directory` / `sec_current` / `sec_submissions` | SEC 发行人身份、当前披露发现、CIK/受理时间/文件身份与原文 | 合法操作者 User-Agent 来自 `TIGER_INTEL_SEC_USER_AGENT`；缺失身份明确留缺口，不能用假邮箱 |
| `fed_policy` | 美联储政策发布 feed 与原文 | 有限发布窗口；feed 空结果不能证明没有更早事件 |
| `bls_calendar` | 官方发布日历 | 未来日程是计划事件，不是统计结果已经发布 |
| `fred` | 登记系列与 vintage 数据 | `TIGER_INTEL_FRED_API_KEY`；日级 vintage 不能证明盘中某时刻已可用 |
| `cboe_vix` | 官方 VIX 历史数据 | 观察日与真实可用时点分别披露；不是候选公司的财报证据 |
| 可信登记的 issuer IR feed | 指定发行人公告与附件 | 必须登记 issuer_id 与允许域名；没有「全市场 IR 已覆盖」的承诺 |

`.env.example` 只列变量名和空值，不自动加载。实际凭据放在仓库外的受保护环境
文件，由操作者可信启动器导出。请求 JSON、报告、原文目录、git、Release 或 URL
均不接受密钥；只读 Node 子进程不会收到采集 key。提供商用量未知时保留
`requires_reconciliation`，不能在成本报告里把未知数写成 0。

### 模型 JSON 失败、格式重答与计量

新接纳的情报 paper 运行在冻结 policy 的 `model_contract` 中登记
`response_observation_contract=tiger.deepseek-json-observation/v2`。该标记由已核验的
冻结源码确定；业务请求不能开启、关闭或更换合同。没有该源码标记的历史运行继续
重放原 policy、wire 与证明，不补写为新合同。

- **HTTP 返回体无法读成 JSON**：诊断明确标为 `http_envelope`，只保存实际观察到的
  HTTP 状态、响应字节数、哈希、是否纯空白与是否完整读完。没有已核验的模型、响应
  身份、stop 和 usage 时，计费仍未知，不自动重发。纯空白 HTTP 返回体不能当成
  已知模型空回答。
- **已核验完整回复的 assistant 内容不是合法 JSON，或仅有空白**：先把去凭据、
  不含私有 reasoning 的原生回复与实际 usage 持久化，再解析内容。只有原失败能由
  同一快照重算且内容未因脱敏改变时，才可使用一次格式重答。
- **模型第二次仍失败**：保留第一、第二次各自的原生观测。两次 usage 已知就分别
  计入；第二次外层响应或 usage 未知则继续待对账，不能删除首次已知用量。

格式失败与原有 schema 失败共享整次执行的**一个**重答额度，不按阶段、分片或
重新创建客户端重置。重答发送相同完整 wire、同模型和同场景，仍使用原合法输出
窗口；不补裁原文、不降低数组/字符串限制、不修补 JSON、也不改变行情范围或数值
风险门槛。DeepSeek 官方说明 JSON 模式可能返回仅含 U+0020 的 assistant 内容，
即使 finish_reason 为 stop；这只解释为什么需显式区分空内容与空 HTTP 返回体，
不能证明某次历史失败的实际原因。[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)

原生观测通过 `response_observation` 的显式快照引用绑定到成功 IO 或失败阶段的
事件链。独立证明从这些原回复重新计算 JSON/schema 结果和重答资格；失败阶段
始终是 failed，不因已留计量证据而变成完整研究。已通过 canonical model、响应
身份、stop 和 usage 核心检查的失败可以保留已知用量；length、非 canonical
回复或未通过核心检查的回复仍是未知计量，不能宣称所有带 usage 的失败均已结算。

成功的独立模型证明另列 `chat_accounting`，包括格式重答前后的全部已知 chat
用量；单个调用的 `usage` 仍是最后一次实际回复的 usage。失败时
`model-failure-accounting.json` 仅说明显式绑定的 chat 观测与已知/未知计量。
没有 chat 观测表示观测数为 0，不能推断整次运行免费；Search/Fetch 和来源采集
仍须单独核对原 archive 账目。验证被拒绝或摘要无法写入时明确记为 unverified，
保留原业务失败，不补造零费用。

如操作者需要仅让固定模型域名直连，可在可信启动环境显式设置
`NO_PROXY=localhost,127.0.0.1,::1,api.deepseek.com`。默认仍是三个 loopback 项；
只允许额外加入 `api.deepseek.com`，拒绝其他域名、URL、凭据形式及重复项。
原代理配置继续用于其他网络来源；该启动配置不是一次失败的根因证明，也不能由
业务请求修改端点或全局代理。

完整 v2 工作流可由可信启动环境的 `TIGER_INTEL_REGISTRY_FILE` 指向审查过的绝对
配置路径，登记可选来源和发行人 IR；`TIGER_INTEL_ARCHIVE_ROOT` 可显式绑定已有
原文档案作为基线。基线只复用截止前已观察的原文版本，不复用模型标注或旧语义分。
独立采集 CLI 只开放内置来源 ID 的启用，不从请求加载任意配置文件或 IR URL。

### 只用已观察原文完成新一轮研究

可信启动者可在同一 registry JSON 顶层设置 `"acquisition_mode": "snapshot_only"`，
与 `sources`、`registered_sources`、`policy_version` 并列；示例见
[snapshot-only.json.example](../config/intelligence/snapshot-only.json.example)。它仍须通过
`TIGER_INTEL_ARCHIVE_ROOT` 显式绑定原文档案。启动时封存审定股票池与共享主题的有限
原文快照，快照为空、来源身份不匹配、原文损坏或未知 mode 都拒绝接纳。普通业务请求
继续只有原四字段，不能选择采集模式、路径或扩大来源权限。

该模式保留完整审定池、行情候选、研究问题、来源许可、请求与 Fetch 预算、两轮研究
及五个必需语义模型阶段。采集计划明确记录 `acquisition_mode` 并绑定 plan hash，
采集不构造可联网的来源适配器，所有来源调用在占用请求预算之前被拒绝；初次收集只读入已经
验证的原文版本。后续补查记录 `SNAPSHOT_ONLY_NO_LIVE_ACQUISITION` 和零来源调用，
资料不足仍为 partial/null；不能由已有一篇文章推断其他目标已经覆盖。模型仍需重新
读取这些原文并完成结构化、舆情、风险、板块叙事与决策，旧模型标注或结论不会复用。

新模式的 admission 快照仅将元数据读取预算设为 32 MiB，以容纳多个完整原文的
索引与关联回执；仍限制最多 256 原文、2048 候选扫描、64 MiB 原文加文本、8192
关联和 30 秒准备时限。这些实际 limits 随 manifest 冻结并在离线证明中重验。
缺省和显式 `live` 保留原 8 MiB 元数据预算与原采集计划字节合同。达到任一边界仍
披露快照缺口，不裁剪文章或把 partial 改成 completed。

返回的 `intelligence.acquisition_mode`、零来源用量、已封存 baseline 和重新生成的
五阶段模型回执分别可核查。零来源请求不表示模型免费，也不消除另一轮任务已存在
的未知计费用量；原失败运行和未知回执应保留原身份，新的 snapshot-only 研究使用
新的 admission、任务与回执。离线证明只使用本轮封存原文，不要求旧档案一直在线。

可信配置可以只启用官方直接源而不启用搜索工具。必需资料不足时，补查仍留下
`research_unavailable` 回执并冻结 partial 包，五个必需语义阶段照常调用模型；
不能把「没有可用搜索工具」当成模型失败或改用规则代替。最终资料状态与来源诊断
分别保留：`reason` 描述确定性的证据资格，`source_reason` 和 `source_diagnostics`
从来源 gap 事件历史重新折叠，避免后续语义复核抹掉不可用、超预算等来源原因。

供应商请求格式来自 [DeepSeek Search & Fetch](https://api-docs.deepseek.com/guides/search/)、
[Brave News Search](https://api-dashboard.search.brave.com/app/documentation/news-search)、
[Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)、
[Exa Search](https://docs.exa.ai/reference/search)。直接源语义见
[SEC 开发者资源](https://www.sec.gov/about/developer-resources)、
[FRED realtime 参数](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)、
[BLS 发布日历](https://www.bls.gov/schedule/) 与 [Cboe 历史数据](https://www.cboe.com/tradable_products/vix/vix_historical_data/)。
供应商调整不由文档中的适配器名称自动吸收，升级后须重跑合同与离线/真实验收。

## 原文、时间与证据用途

每条采集保存原始响应字节、提取文本、版本哈希、原始/最终/规范 URL、来源版本、
publisher/origin、查询命中、时间精度和附件引用。HTML 优先 `<main>`/`<article>`，
移除导航等结构；没有可靠主区时保持未知完整性。PDF 保留页码定位；缺少解析依赖、
加密或图片型 PDF 不伪造正文。附件发现与实际下载分别留痕，未抓到的附件是缺口。
没有 OCR、付费墙绕过、访问挑战绕过或任意动态浏览器执行能力。

发布时间、更新时间、抓取时间、档案首次发现、事件发生/生效时间、报告期间、
统计观察期和可用时点各自保留。日期无时区或只有日期时不补造盘中精度。
旧日期的可变网页在截止后才第一次抓到时，不能仅靠旧发布日期证明这个正文版本
在当时存在。允许的不可变披露例外必须绑定受理身份、来源回执和确切正文哈希。

同一文档可有多个 subject 和 event。公司名称、ticker/CIK、宏观系列、监管机构、
行业与境外研究主体分别标识。模型提供原文 quote/offset、事件类型、期间和目标，
确定性验证器检查确切原文区间、审定身份和时序；未知值保留未知，不用抓取时间、
标题 hash 或英文季度正则替代全部事件语义。

### 清洗标识与历史重放

新清洗提示使用 `typed-clean-complete-format/v1`。事件的 `target_ids` 只选本次输入列出的
裸产业链 ID 或 ETF 代码；采集档案里的 `global:`、`chain:`、`sector:`、`security:`
和 `account:` 标识属于另一套命名空间，不能直接复制进事件。宏观或政策属性通过
`event_type=macro/policy` 表达；不涉及给定产业链或 ETF 时，事件目标可以为空数组。

每个 typed event 必须引用本篇 `subjects[].id`，且主体有可核验的原文 span。无法
确证主体时不造该 typed event 及依赖它的 claim，原文和文档行仍完整保留，中文摘要
如实写明事实或反证以及未解决的主体缺口；有原文依据的负面事件和反证仍须保留。
这只是对模型输出格式和引用关系的说明，不放宽 schema，不补写或删除模型答案，
也不改变原文分批、五阶段、合法窗口、一次执行级格式重答或逐事件证据资格规则。

摘要等纯文本说明字段不能使用 `<` 或 `>` 表达比较、排序，须写成“优于”“低于”等
中文；原文、逐字 quote 和 value/unit/currency 的字面绑定继续保留，宿主不转义、
替换或修补模型答案。每批提示明确写出本批真实行数，要求按输入顺序逐字复制每个 ID。
同 URL、同故事或相同正文的不同来源和版本仍各自输出，不能去重、按相关性删行或
添加 `_dup` 之类的新 ID；资料不足的行仍保留文档、中文摘要、反证和未解决缺口。

历史 `typed-clean-legacy/v1`、`typed-clean-summary-characters/v1` 与
`typed-clean-identifiers/v1` 的提示字节继续保留。
独立证明按已识别的冻结模块 SHA 选择当时的提示，未知或被修改的冻结实现拒绝重放；
新的提示不能补签旧运行，也不能让旧的不合格回答变成合格。

`allowed_uses` 按事件分为 background、analysis、scoring、action。一段宏观材料
可以供背景阅读，却不能借此取得某股票的行动资格。action 只是允许作为行动证据
来源，数值阈值、仓位、止损和最终计划校验仍单独执行。转载按共同原始来源归组，
不同域名不能自动当独立证据。

舆情、风险与板块叙事使用逐目标 `assessed`、`insufficient_evidence`、`conflicted`
状态。没有该目标可用资料时分数为 `null`；全局存在文章不等于六条链和全部板块
均已核查。缺口研究仍在预算和来源合同内，不能无上限递归搜索。

已接入持仓与待成交记录进入评分前研究范围。若某条持仓或待成交证券不在本轮审定
roster，保留原代码并新增必需的 `account:holding:<symbol>` 或
`account:pending:<symbol>` 连接缺口，分别标明 `HOLDING_OUTSIDE_REVIEWED_ROSTER`
或 `PENDING_OUTSIDE_REVIEWED_ROSTER`；不猜测身份，也不扩大可交易集合。此类必需
账户缺口会阻止新的买入计划。完全没有连接持仓则保持 `not_connected`，不能写成
已接入且空仓；这与确实已接入的空数组不同。

归档的 SQLite 是可变查询投影；文档 blob、事件链和冻结 manifest 分别绑定哈希。
冻结同时检查 plan hash、revision、数值 frontier hash 及在途请求，之后不可原地
补改。晚到回执保留为晚到事件，不能混进已发布评分。历史 profile 与原证据规则
保留用于旧回执；不能拿新规则补签旧运行或把旧运行改写为新 profile 验收。

## 可复现评测与验证范围

```bash
python scripts/intelligence-benchmark.py \
  --dataset tests/fixtures/intelligence_benchmark/dataset.json \
  --capture tests/fixtures/intelligence_benchmark/capture-ideal.json \
  --capture tests/fixtures/intelligence_benchmark/capture-degraded.json \
  --out /absolute/operator/path/new-benchmark-report.json
```

仓内首批 **100 题是 synthetic_fixture**，五类各 20：宏观政策、财报/指引、行业
供应链、负面风险、时间/实体/转载陷阱。它们只验证评测器合同与回归路径，不能
作为真实金融研究 gold、供应商召回率、15 分钟 SLA 或生产质量改善的证明。
标记的 calibration/blind 划分也是公开夹具，不能冒称真实盲测。

真实比较需要另外冻结独立一手事件种子与人工裁决的 `reviewed_gold` 数据集，记录
reviewer、method、reviewed_at 和与 capture 独立的来源。默认/质量模式固定同一
问题、截止、候选范围、来源许可和提取器；调参集与盲测集分开。实际采集必须走
有硬预算的 service，评测程序自身没有网络、模型或凭据入口。

报告包括精确 reference URL 召回、经独立 subject/event 标注与时间检验的事件
召回、带确切正文哈希/引用的主证据率、未来发布时间、未知时间、错误身份、共同
origin 重复率、分层结果、P50/P95 延迟和已报告成本。失败/未跑题仍在分母；空
分母为未验证。原文存在性、语义正确性和完整互联网召回不是同一个指标。
`prospective`、`frozen_replay` 与 `historical_reconstruction` 分别披露，不自动
混成一个胜者结论。

历史严格 v1 运行还可用独立入口诊断其冻结语料：

```bash
python scripts/intelligence-replay.py \
  --run-root /absolute/operator/path/runs/run-<original-id> \
  --out /absolute/operator/path/new-native-replay.json
```

该程序先验证原事件链与原始文档快照，再按新资格规则逐篇披露阻断原因。它不联网、
不重新生成模型标注、不补抓原网站，也不修改原运行权限。可选 `--annotations`
只接收绑定原文 hash 的显式外部 typed 标注，其模型来源保持未验证。
早先 56 篇语料可以完整回放已捕获的字节；没有原站正文观测或新 typed 模型回执时，
不能把旧 provider 文本升级成完整原文、宣称 56 篇重新通过评分，或据此比较供应商召回。

完整 v2 回执的独立证明还从冻结行情、日历、持仓及实际行情/metric/options I/O 重放
`tech.monitor`、`tech.cycle_linkage`、MRS、SHS、ICS、候选扫描与 frontier、TSS 和数值风控。
两条动量阶段直接从原始行情重新计算；不能把保存的动量输出重新哈希并连带改写融合
和下游得分来获得验证通过。期权历史读取与原账户 before
版本绑定，重放不执行实际账本 writer 或 provider；保存的数值分和 approved 名单只是
比较目标。离线夹具没有账户 commitment 时明确披露该边界。

本地 HTTP 夹具、离线模型响应和单元测试只证明合同与自有场景。真实付费来源、
真实同模型研究效果、客户生产、账户收益与 RDAS P/O 域必须有各自授权、可信总预算、
真实回执和独立结果核验；配置了 key 或生成了一份报告不表示这些验收已通过。

## 常见失败

| 代码/状态 | 含义与处理 |
|---|---|
| `READ_ONLY` | 启动配置未选择 acquire；请求不能提升权限 |
| `INVALID_REQUEST` / `BAD_AS_OF` | 未知字段、未审定代码、来源或时间不合法；修正输入，保留原身份冲突信息 |
| `SOURCE_AUTH_REQUIRED` / `SOURCE_IDENTITY_REQUIRED` | 来源凭据或合法身份缺失；保持缺口，不伪造身份或切换账号 |
| `RATE_LIMITED` / `SOURCE_UNAVAILABLE` | 访问失败与空结果不同，按回执处理 |
| `BUDGET_EXHAUSTED` / `WALL_DEADLINE` | 已到工具/观察上限；没有完成的项继续保留缺口 |
| `PLAN_CONFLICT` / `JOB_REVISION_CONFLICT` | 同一身份的输入或冻结版本冲突；不可覆盖原证据 |
| `INFLIGHT_ATTEMPTS` / `FROZEN_BUNDLE` | 在途请求不得冻结；已冻结任务不得补改 |
| `EVIDENCE_NOT_FOUND` / `HASH_MISMATCH` | 证据缺失或损坏；拒绝生成成功摘要，不重写 hash 兜底 |
