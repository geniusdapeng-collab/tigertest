# Tiger 接入 Codex / Qoder / DeepSeek Harness

本指南面向用 Agent 调用 Tiger 的操作者和维护者。原有 WorkLoom 能力目录提供仓库只读信息；Tiger CLI/MCP 调用同一个 Python 严格工作流；独立 `tiger-intelligence` 入口提供受控来源同步与证据查询。根目录是 Python 引擎，治理壳在 `governance/`。不要把根目录当成 Node 工作区，也不要修改通用能力加载器来开放本地 execute。

只在对话框中操作时，外层 Agent 先读 [TIGER-OPERATOR.md](TIGER-OPERATOR.md)，负责运行检查、准备固定请求、保存原始任务身份、查询和解释已核验的结果。用户可说“检查研究环境”“运行今天的新研究”“继续查看刚才的任务”，不必手写命令或 JSON。外层 Agent 能调用的权限仍来自可信启动配置。

| 调用端 | 本仓提供的配置方式 | 生效与验证 |
|---|---|---|
| Codex 本地主机 | `--client codex` → `.codex/config.toml` | 正常项目信任及新连接；实际发现 Tiger 工具后再调用 doctor |
| Qoder CLI | `--client qoder` → `.qoder/settings.local.json` | 正常目录信任及重连；核对工具与 doctor |
| Qoder 独立桌面 | 从 Connector 的 Add custom MCP 导入已复核的服务器条目 | 桌面保存用户级 Connector；项目文件生成不等于已导入 |
| DeepSeek Harness | `--client dsh` → `.dsh/tiger-mcp.yaml` 插件列表 overlay | 按所用部署装配后，由实际宿主发现并调用 |

同一操作者、同一 profile 的持续纸面账户保持固定可信 state-root；新逻辑任务使用新幂等键，重连或重试保留原 root、原请求和身份。切换 profile 或明确创建新隔离账户才使用独立 root；日常研究不会为了“从零评分”而丢掉账户历史。

## 1. 先检查，再选择访问模式

从仓库根目录运行。Node 要求由仓库现行运行时约束决定；新 Tiger 入口的 Python 最低要求为 3.11。推荐显式传入已安装本仓依赖的 Python；省略时先找仓库 `.venv/bin/python`，再找 PATH 中的 `python3`。系统 Python 版本不满足时，doctor 会失败，不会假装就绪。

```bash
node scripts/tiger-agent.mjs doctor --python /absolute/path/to/python \
  --state-root /absolute/path/to/existing-v2-state
printf '%s\n' '{"profile":"us-public-intelligence-paper-v2"}' | \
  node scripts/tiger-agent.mjs describe --python /absolute/path/to/python \
    --state-root /absolute/path/to/existing-v2-state --input-file -
```

这两条命令不调用行情或模型、不创建账户、不启动业务 worker。doctor 检查实际解释器和依赖；describe 返回固定 profile、阶段顺序和限制。`trading.repo.readiness` 只检查文件布局，不能代替 doctor。

doctor 的 `ready` 不核验模型凭据或提供者。缺少模型配置的 paper 请求仍可能先被接纳为 `accepted`，随后 worker 以 `MODEL_REQUIRED_FAILED` 失败；必须沿原 run_id 查询，并以 receipt/verify 的结果判断业务是否完成。

默认 `--mode readonly`。执行完整 paper 工作流必须由操作者在启动参数或项目部署配置中显式选择 `--mode paper`。请求 JSON 不能改变 mode、解释器、state-root、阶段或策略。

当前开放四个显式 profile。新的 AI 研究和官网更新必须明确选择 `us-public-intelligence-paper-v2`：真实同模型语义、独立数值 frontier、登记来源采集、逐目标证据与冻结包共同组成完成条件。`us-public-model-required-paper-v1` 保留历史严格运行及旧回执；`us-public-paper-v1` 和 `us-public-active-paper-v1` 保留原 disabled/passthrough 或明确实验语义，均不能代替新研究验收。兼容函数的默认值仍保留旧调用含义，操作者不得靠省略 profile 推断获得 v2。所有配置仍为美股公开数据、USD 本地 paper 账户 `public-usd`、独立初始资金 100000 USD，不连接真实券商、不执行真实资金订单。外层宿主模型不会自动接入内层模型。完整情报合同、来源启用、采集预算与评测边界见 [INTELLIGENCE-ACQUISITION.md](INTELLIGENCE-ACQUISITION.md)。

可选 active 实验的回退板块是冻结的 NASDAQ 供应商分类代理，不是 GICS、真实 ETF 成分或历史时点分类；第三条达标主线保留为次池。更高资金使用率会改变实际风险，不能把“上限不变”解释为“收益或回撤不变”。该实验不会随 model-required 配置启用。运行和评测边界见 [TIGER-ACTIVITY-EVALUATION.md](TIGER-ACTIVITY-EVALUATION.md)。

### 真实 DeepSeek 内层模型与原生搜索

本节保留严格 v1 的精确历史口径，便于旧运行复核。v2 继续要求同一 DeepSeek 客户端完成五个语义阶段，不将搜索供应商当替代文本模型；它以登记来源、原文/附件、结构化事件、逐目标 `null`/缺口与冻结证据包替代固定主题 collector。以下提到固定 14 主题、单一文档资格或原交叉验证的段落仅描述 v1；v2 的差异与独立采集操作以 [情报指南](INTELLIGENCE-ACQUISITION.md) 为准。

固定配置见根 `.env.example`，实际 key 存在仓库外受保护文件。运行器不自动寻找或加载 `.env`，也不读取 Codex/DSH 的登录缓存。可信启动器先将 `LLM_BACKEND=api`、`LLM_BASE_URL=https://api.deepseek.com`、`LLM_MODEL=deepseek-flash` 与非空 `LLM_API_KEY` 导出到进程环境，再执行 CLI/MCP。五个语义调用固定 JSON mode；thinking 从 bundle 场景策略读取：清洗与舆情为非思考，风险与板块叙事为 high，最终多空判断为 max。每次调用用仓库固定的官方 tokenizer 对完整序列化请求计数，并自动申请全部合法剩余输出窗口；输入超过 1,048,576 tokens 或输出超过 393,216 时失败关闭。启用 SEC 原文核验时，可信启动器还须导出操作者的 `TIGER_SEC_CONTACT` 邮箱。这些设置只传入 model-required 的业务子进程。请求 JSON 无权设置这些值。

```bash
# protected-deepseek.env 的实际内容由操作者在仓库外管理。
set -a
. /absolute/protected/path/deepseek.env
set +a
node scripts/tiger-agent.mjs run --python /absolute/path/to/python \
  --mode paper --state-root /absolute/path/to/model-required-state \
  --input-file model-required-request.json --wait
```

新任务的 `model-required-request.json` 使用下文同一四字段结构，将 profile 设为 `us-public-intelligence-paper-v2`；仅复核历史严格路径时显式选择 `us-public-model-required-paper-v1`。两个严格配置的 activity 实验均保持关闭。模型只能选择/否决数值风险闸门已批准的买入，不得新增、扩大或绕过任何风险限制；无合格买入也必须由实际模型生成等待理由。

严格研究保留原初排和主线补扫名单。对初筛通过但未进入名单的股票，用已有日线逐只检查同一套 A/B/C 入场形态；有形态的股票追加到精评，再走原有期权、评分、风控和模型证据检查。结果中的 `raw.scan_stats.entry_setup_supplement` 分别记录原名单数 `baseline_selected`、额外检查数 `checked`、追加数 `added` 与缺少可用结构行情的数量 `unavailable`。检查范围以已取得合格行情的股票池为准。

当前官方 API ID 为 `deepseek-flash`，2026-10-26 观察的 [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/) 映射是 `DeepSeek-V4.1-Flash-1017`。这是别名观察，不保证未来自动固定到同一发布；回执记录请求别名、实际 response.model、release 观察、response IDs 与 usage，不能虚构不可变日期 ID。

应用调用同提供方 [Search & Fetch](https://api-docs.deepseek.com/guides/search/) 的原生 Search API，收集既有六条产业链与八类板块主题，将净化文档送入 Flash。适配器支持原生 Fetch，固定 collector 当前没有调用它。Flash 的 Responses `web_search` 在 [官方矩阵](https://api-docs.deepseek.com/guides/responses_api/) 中会被忽略，因此这里准确披露为 `provider_native_search`，不宣称 Flash 自带托管 web_search 已执行。来源 URL 中的凭据、token、签名等在持久化和送模型之前排除；原始响应只保留摘要，回执保留可重算的安全响应。

从 `native-complete/v1` 起，格式清洗和模型请求保留原生搜索返回的完整净化正文，移除既有的 4,000 和 1,600 字符裁切。语义清洗先计划一次请求；只有完整请求超出模型上下文才按原文档边界分批，每批仍带全文与来源。后续分析优先发送全部入选正文；仅在确实超出上下文时采用已经逐篇生成、带原文哈希和来源 ID 的模型标注，原始正文在本轮证据内可复查。若单篇原文本身放不下，或标注与必需上下文仍放不下，任务明确失败。Tiger 严格路径目前只产生系统指令与用户数据两条文本消息，输出 schema 与现场事实随完整序列化请求计量；这条入口没有历史聊天、工具定义或多模态输入。

模型窗口由 [DeepSeek 模型规格](https://api-docs.deepseek.com/api/list-models/) 决定。回执分别保存本地对完整 HTTP JSON 的预估计数和供应商实际 `usage`；计费和实际用量以供应商为准。在输出数字改变自身序列化 token 数的边界，选择实际请求仍能放进窗口的最大已验证值。严格纸面入口使用自己的业务时限与证据链；通用 RDAS P 域的整轮调用/token/成本预占是另一条门禁，未接通可信真实预算时仍拒绝付费调用。Search/Fetch 的字节上限也不适用于 Chat 请求。Chat 响应有独立的传输内存与时间限制；达到传输限制或收到 `finish_reason=length` 时本轮不发布。格式纠错重试要求前一次已取得可验证的响应身份、`stop` 和实际 `usage`，保留原请求哈希、响应摘要和用量；未能核实的调用不重试。

五个语义阶段必须各有完整真实回复，清洗逐篇、舆情六链、叙事各 ETF 的覆盖均被验证；有效的无风险 `alerts=[]` 仍必须有真实风险评估。最终 `decision.debate` 始终调用模型，给出普通人能读懂的关注理由、时机、入场条件、失效条件、风险和来源。数值计划由原 TradePick 绑定，参考资金明确为纸面 100000 USD，`valid_for` 为研究日之后下一美股现金交易日的实际计划收盘时刻，含提前收盘和特殊休市。历史回执继续用其冻结规则验真。

每个语义请求都显式携带 admission 冻结的研究日期和纽约市场时区。从 `us-cash-session-close/v1` 起，严格模型纸面研究采用 admission 前已收盘的最近一个美股现金交易日。常规收盘是纽约 16:00，提前收盘是 13:00，恰在收盘时刻仍使用前一场。日历覆盖 2024–2028，包含 2025 年 1 月 9 日临时休市。SPY、板块 ETF 和个股均要求该日的可用收盘日线，缺同日收盘的标的从本轮研究中排除并披露。SPY 或板块覆盖不足时失败。交易所日历只证明预定交易时段已经结束，采用的是供应商提供的对应日线，可能延迟或修订。产业链监测和全球联动复用已对齐的美国上市成分股，全球联动还复用同一 SPY 基准。未核实本地收盘日历的海外成分会披露缺失。利率和波动率沿用有日期、有限滞后的最近观察，VIX 系列在日内发布时段结束后才接纳当日日线，利率代理的当日值统一按美联储 H.15 的纽约 16:15 发布界线处理。报告的 SPY 曲线来自同一信号序列，QQQ 当轮未采集。独立证明从 admission、原始行情 I/O 与 `data.prepare` 快照重建截点和所用日线。旧回执沿用其冻结源码对应的口径。来源发布时间未知或较旧时，模型仍需披露时效不确定性；日线日期是收盘研究参考。[NYSE 交易时段和日历](https://www.nyse.com/markets/hours-calendars) · [Nasdaq 交易日历](https://nasdaqtrader.com/Trader.aspx?id=Calendar) · [Cboe 波动率指数时段](https://www.cboe.com/tradable_products/vix/faqs) · [美联储 H.15 发布说明](https://www.federalreserve.gov/releases/h15/)

最终每个 buy/watch 都必须引用该股票自己的真实行情快照：`market_evidence` 恰好一项，仅有 `id`、`symbol`、`as_of`、`close`、`series_sha256` 五个字段。日期和收盘价来自该股票实际末根日线；完整标准化日线表的规范 JSON 计算 SHA-256，标识为 `md_<symbol>_<YYYYMMDD>_<摘要前24位>`。独立证明从冻结的 `data.prepare` 重建该项并核对模型请求、引用和最终输出；新口径的日期必须与冻结的已完成基准交易日一致。这里的 close 是所标日期的行情参考，不是获准入场价或已成交价。

模型 `source_ids` 必须恰好包含一次本股票的行情标识，不得借用其他股票快照。watch 允许不引用公司新闻，此时模型须明确说明新闻证据缺口和不确定性；不会强迫填入无关链接。buy 仍须另引用至少一篇、最多八篇原生搜索文档，行情不能代替买入新闻证据。发布计划的 `sources` 只放原生文章，`market_evidence` 单独保留行情事实；两类证据不互相冒充。空的候选集合仍须由模型给出顶层 WAIT/AVOID 解释。

模型还须将同一原始报道的跨站转载视作一个来源，并明确区分 AI 评论、第三方断言与公司公告。该提示约束不会改变既有交叉验证阈值；按域名计数的规则本身不能证明不同网站一定具有独立报道来源，实际来源质量仍须随研究证据复核。Node 的返回 schema 校验字段形状，另由 enabled 模式的语义检查核对行情归属、有效日期、正价格及计划边界；JSON schema 单独通过不等于完整业务验证。

任何模型/原生搜索不可用、401/配额错误、截断、错误模型、空输出、缺项、schema 不符、缺少真实调用或文档/阶段输出与调用不一致，均停止整个任务，不透传，不规则代答，不发布新账户或网站成功。成功只允许 `completed`；`completed_degraded` 不满足此配置。真实业务成功须通过 receipt/verify 独立重算全部阶段 I/O 和最终计划。所有自动测试里的模拟 HTTP 仅证明契约，不能算作真实模型使用。

## 2. CLI：提交一次，持续查询同一个身份

六个操作为 `doctor`、`describe`、`run`、`status`、`receipt`、`verify`。业务输入使用 `--input-file path.json` 或 `--input-file -`；stdin 必须是一个 UTF-8 JSON 对象，最大 64 KiB。重复键、未知字段、非有限数值、任意命令和路径覆写都会被拒绝。

请求文件只含以下四个字段；为一次逻辑任务分配一个持久、可恢复的幂等键：

```json
{
  "schema_version": "tiger.agent-request/v1",
  "profile": "us-public-intelligence-paper-v2",
  "paper_account": "public-usd",
  "idempotency_key": "operator-task-20261021-001"
}
```

```bash
node scripts/tiger-agent.mjs run \
  --python /absolute/path/to/python \
  --state-root /absolute/path/to/tiger-state \
  --mode paper --input-file request.json
```

如需单独运行已授权的 active 实验，将请求中的 `profile` 明确改为 `us-public-active-paper-v1`，使用新的逻辑幂等键和独立 state-root；复核历史严格调用使用 `us-public-model-required-paper-v1`，兼容旧调用使用 `us-public-paper-v1`，不作为新 v2 研究完成。`describe` 可用 `--input-file` 读取对应 profile 的对象返回其 policy。四个 profile 不应在同一个账户历史中混用；切换时应新建独立 state-root，不复制待成交/净值来冒充迁移。

成功提交返回 `run_id` 和 `status=accepted` 或 `running`。这只表示请求身份被接纳，不能报告分析或账户处理完成。将实际 `run_id` 写入查询文件：

```json
{"run_id":"run-0123456789abcdef0123456789abcdef"}
```

```bash
node scripts/tiger-agent.mjs status --python /absolute/path/to/python \
  --state-root /absolute/path/to/tiger-state --input-file run-id.json
node scripts/tiger-agent.mjs receipt --python /absolute/path/to/python \
  --state-root /absolute/path/to/tiger-state --input-file run-id.json
node scripts/tiger-agent.mjs verify --python /absolute/path/to/python \
  --state-root /absolute/path/to/tiger-state --input-file run-id.json
```

查询可以保持 readonly，但必须使用提交时同一个 state-root。`receipt` 独立验证证据和账户提交后才提供选股、持仓、挂单、现金、权益、日期及数据/模型限制；`verify` 提供验证结果而不重复业务动作。CLI 每次仅输出一个 JSON envelope；所有返回字段的契约在 `bundles/trading/agent-workflow.schema.json`。

需要持续等待时，原 `run` 命令可加 `--wait`，可选 `--wait-timeout-seconds 1..3600`。默认观察窗口 2460 秒；Python 业务 deadline 默认独立为 2400 秒。Node 单次提交/查询上限 60 秒，等待循环只轮询已有 `run_id`，不会重新提交。

仅对 `us-public-intelligence-paper-v2`，可信启动环境可设置 `TIGER_AGENT_RUNTIME_POLICY_FILE`，
指向 `{"schema":"tiger.agent-runtime-policy/v1","business_deadline_seconds":5400}` 形式的本地文件。
只接受这两个字段与 2400–7200 的整数秒数；[示例](../config/agent-clients/paper-runtime-5400.json.example)不自动加载。
doctor/describe 可只读检查有效值。新运行把脱敏、无凭据的内容冻结为 `runtime-input.json`，
其哈希同时进入 policy、admission 与 receipt；来源准备、锁等待和启动均从原接单时刻计入总截止。
验真和同键恢复只使用已冻结的原合同，不依赖当前配置文件或环境。
未配置文件时保留原 policy/admission 字段布局和 2400 秒规则，其他 profile 不接受该扩展。
时限不改变五个真实语义阶段、完整上下文、合法输出上限或数值风控；观察窗口结束时继续查原身份。

| 场景 | 正确行为 |
|---|---|
| 已拿到 run_id 后关闭终端/MCP、超时或取消 | 停止观察；accepted worker 继续独立执行。使用原 run_id 查询 |
| 提交响应丢失，未拿到 run_id | 用完全相同的四字段请求及原幂等键恢复身份；不得自动换新键 |
| 同一键已有 completed/completed_degraded | run 返回原身份及 `data.observed_status`，仍需 receipt 验证 |
| 同一键已有 failed/blocked/interrupted | 返回原失败，不重跑内部阶段 |
| 新请求遇到 ACCOUNT_BUSY | 新键尚未被消费；之后可重试同一个请求和键 |
| status 调用成功但 status=failed | 只证明查询成功，不证明业务成功 |
| receipt/verify 发现证据缺失或损坏 | 返回失败，不读取未验证摘要，不补写或重跑阶段 |

退出码：0 表示当前操作成功；2 输入无效；3 访问拒绝；5 幂等冲突/账户占用；6 数据拒绝；7 runner/证据/持久化错误；8 阶段失败或未完整完成；9 超时/中断；130 取消。退出 0、JSON 合法、存在文件或状态为 accepted 均不能单独证明完整业务交付。

## 3. 独立 Tiger MCP

入口 `scripts/tiger-agent-mcp.mjs` 使用相同 Python 契约。STDIO stdout 只承载逐行 JSON-RPC。初始化顺序为 `initialize` → `notifications/initialized` → `tools/list`；初始化、发现和临时探测均不创建 state-root、不启动 Python 或业务 worker。

| 原始工具名 | 行为 | readonly 可见 |
|---|---|---|
| `tiger_doctor` | 检查解释器、依赖和运行条件 | 是 |
| `tiger_workflow_describe` | 返回固定 profile 和阶段契约 | 是 |
| `tiger_workflow_run` | 提交完整公开美股 paper 流程 | 否，直接调用也在启动 Python 前拒绝 |
| `tiger_workflow_status` | 读取既有运行状态 | 是 |
| `tiger_workflow_receipt` | 独立校验证据后读取完成摘要 | 是 |
| `tiger_workflow_verify` | 独立校验证据和账户提交 | 是 |

工具结果同时包含相同 envelope 的 `structuredContent` 和 JSON 文本；`isError` 表示操作失败。未知工具为 JSON-RPC 参数错误。MCP 取消只停止对应提交/查询的观察，不是业务撤销 API。帧最大 128 KiB，业务输入仍为 64 KiB；最多四个并发请求，重复在途 ID、畸形 JSON、过量输出和断开的流均有有界退出路径。

初始化行为和工具结果遵循已核实的 [MCP 生命周期规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) 与 [工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。本服务不要求 task-based execution 扩展。

## 4. 生成 Codex 项目配置

生成器只写选中 Tiger 项目内的配置，默认位置 `.codex/config.toml`。它实际运行选定 Python 的 `tomllib` 校验 TOML，保留其他配置原文，只更新一对管理标记内的 Tiger server；遇到已有同名非托管条目、重复标记、非法 TOML、链接路径或并发修改则拒绝替换。

```bash
# 只检查是否已匹配；不会创建配置目录。需更新时退出 2。
node scripts/tiger-agent-config.mjs --client codex \
  --python /absolute/path/to/python --check

# 默认生成 readonly。
node scripts/tiger-agent-config.mjs --client codex \
  --python /absolute/path/to/python

# 经操作者选择后，显式启用 paper；再次运行可切回 readonly。
node scripts/tiger-agent-config.mjs --client codex \
  --python /absolute/path/to/python --mode paper
```

可选 `--project /absolute/path/to/workroom-tiger`、`--state-root /absolute/path/to/tiger-state` 和项目相对 `--config`。生成结果包含实际 Node/Python 路径、解释器版本、写入状态；不保存凭据、不编辑用户全局配置。原文件会原子替换，重复生成结果一致。`durability=unknown` 表示已发生替换，但目录同步或当前内容复核未确认；`cleanup=incomplete` 表示文件或写入锁清理未完全确认。两者都不能当成“没有写入”自动重试，应按 warning 核对现有文件和锁。

提交到仓库的是 `config/agent-clients/codex.toml.example` 可移植模板；带有本机绝对路径的实际配置留在本地并被 Git 忽略。

显式 paper 配置包含 `env_vars`，名称来自共享 `MODEL_ENV_NAMES`：必需模型配置及已支持的来源凭据、registry/archive 路径和可选的 `TIGER_AGENT_RUNTIME_POLICY_FILE`。只保存变量名；值由可信宿主启动环境提供，切回 readonly 时移除该业务名单。生成器不读取受保护凭据文件、不复制环境值，始终返回 `credentials_verified=false`；paper 回执中的 `environment_requirements` 只列所需名称和验证边界。配置语法及 Python 版本通过，不代表模型登录或调用已验证。Codex 环境转发字段依据 [官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。

Codex 支持项目 `.codex/config.toml`、STDIO 的 command/args/cwd、启动与工具超时配置；项目配置受客户端信任机制约束，详见已核实的 [Codex MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。生成配置成功不表示当前会话已加载。请在可信项目的新连接或显式刷新后的实际宿主中验证工具列表和 doctor；交付证据应分别记录“文件校验”“协议调用”“原生宿主挂载”。生成器始终返回 `host_attached=false`。

原生 `codex mcp get tiger-workflow --json` 是配置查询入口，单独的 not-found 不能判定项目文件无效。需要区分项目层发现和实际生效时，可通过已支持该接口的原生 app-server `config/read` 指定 `cwd`、检查项目层及 `disabledReason`；只输出 Tiger 字段，避免暴露其他配置。若项目层因尚未信任而禁用，应使用客户端正常的项目信任流程；生成器不会更改这项用户设置。只读配置检查不需要启动 thread/turn 或模型，接口见 [Codex App Server 文档](https://learn.chatgpt.com/docs/app-server)。

根 `AGENTS.md` 显式要求阅读 `AGENTS.repo.md`。后者补充 Tiger 用法，不创建覆盖根规则的 `AGENTS.override.md`；参见 [Codex AGENTS.md 发现规则](https://learn.chatgpt.com/docs/agent-configuration/agents-md)。

## 4.1. 生成 Qoder CLI 配置与桌面导入

```bash
# 只校验匹配情况；默认只读，不写用户全局设置。
node scripts/tiger-agent-config.mjs --client qoder \
  --python /absolute/path/to/python --check

# 生成或更新本机项目配置；显式 --mode paper 才启用提交能力。
node scripts/tiger-agent-config.mjs --client qoder \
  --python /absolute/path/to/python
```

默认目标是 `.qoder/settings.local.json`；可通过项目相对 `--config .qoder/settings.json` 明确选择共享项目文件，但带本机绝对路径的生成物不应提交。Qoder CLI 的项目级/本机级文件及目录信任规则见 [配置层级](https://docs.qoder.com/cli/settings)，STDIO 的 `type`、`command`、`args`、`cwd`、`timeout` 和 `mcpServers` 见 [官方 MCP reference](https://docs.qoder.com/cli/mcp-reference)。

生成器只修改一对 `// BEGIN/END TIGER AGENT MCP v1` 标记内的 Tiger 成员，保留其他服务器、设置与注释原文。它校验完整 JSONC、重复键和修改前后其他设置的语义，拒绝同名非托管条目、错误标记、无效 UTF-8、超限文件、链接路径和已检测到的并发改动。输出的 Node/Python/入口/state-root 均为可信启动配置，工具请求不能改写；不设置 `trust` 或 `alwaysAllow`。

独立 Qoder 桌面的 Add custom MCP 当前保存用户级 `~/.qoder/settings.json` 的 `mcpServers`，见 [桌面 Connectors](https://docs.qoder.com/qoder/connectors)。本项目生成器不会写该全局文件，也不承诺桌面自动发现 CLI 项目配置。通过桌面正常的 JSON 导入入口使用 [严格 JSON 示例](../config/agent-clients/qoder.json.example)，由操作者或获授权的宿主配置流程替换实际路径并保存；不要直接粘贴带管理注释的 JSONC 作为严格 JSON。MCP 连接成功后，在新任务里查询工具、调用 doctor 和对应 profile 的 describe。

Qoder 桌面的 Agent 可在 Settings → Agent 配置，导入 [外层操作者指令模板](agent-clients/tiger-operator.prompt.md) 并选择已配置的 Tiger Connector。只有保存成功、实际任务选择并完成工具发现后，才算该宿主已装配；生成草稿不等于保存的 Agent。官方步骤见 [Qoder Agent](https://docs.qoder.com/qoder/custom-agents)。本仓不宣称该 prompt 文件会被桌面自动加载。

没有足以保证任意 Qoder 宿主环境继承或插值写法的验证，本配置不填造 `${...}` 凭据值。paper 模式仍要求可信宿主环境到达实际 MCP 进程；环境条件不足时据实报告并保留失败身份，不改请求传 key、不读取宿主登录缓存。原始协议测试可以验证生成的服务器命令，不能替代 Qoder 宿主挂载或真实提供者调用。

## 5. 生成 DeepSeek Harness 插件列表

本仓 `governance/` 内置 DSH 组件属于产品治理壳；外部 DSH 客户端的 MCP 插件属于调用端，两者不共享执行入口。

```bash
node scripts/tiger-agent-config.mjs --client dsh \
  --python /absolute/path/to/python
```

输出项目 `.dsh/tiger-mcp.yaml`，内容是顶层插件列表 overlay，不是完整 DSH 应用配置。将该列表按所用 DSH 部署的配置装配方式加入插件集合；生成器不启动、安装或改写 DSH 全局配置。已安装的 `governance` YAML 依赖用于解析与保留配置；缺失时明确报错，不临时下载包。

每个服务器一条 `@deepseek-ai/dsh-mcp-client`，`serverName=tiger-workflow`、`transport=stdio`，具体字段为 command/args/cwd/toolCallTimeoutMs/failOnStartupError。调用端名称形如 `mcp__tiger-workflow__tiger_doctor`。字段和临时 STDIO probe 行为来自已核实的 [官方固定标签 dsh-v0.2.1-rc.2 MCP 插件文档](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/mcp/mcp-client/README.md)；这不是对任意客户端版本的兼容声明。

已有插件及注释按原字节保留；block list、flow list 和尾逗号均支持。生成器不会执行 `!!js`，其他插件的此类标记原样保留。已有同名 server/plugin 冲突时拒绝写入。默认 readonly；`--mode paper` 显式切换。模板见 `config/agent-clients/dsh.yaml.example`。实际宿主是否装配成功须由该宿主的工具发现和调用回执证明，不能用原始 STDIO 测试冒充。

## 6. 保留原 WorkLoom 只读目录

```bash
node scripts/workloom-agent.mjs list
node scripts/workloom-agent.mjs describe trading.repo.status
printf '%s' '{}' | node scripts/workloom-agent.mjs invoke trading.repo.status --input-file -
```

`trading.repo.status`、`trading.repo.docs`、`trading.bundle.summary`、`trading.repo.readiness` 读取本仓元数据；行业 bundle 来自 `governance/bundles/trading`。仓库 provider 不读 HOME 密钥文件、不联网、不写盘。通用目录其他服务端能力沿用自己的访问机制；这些能力不因此获得 Tiger paper 执行权。原通用 loader 的 local+execute 禁止规则保留。

## 7. 可复核的验证与排错

```bash
# 实际已安装 Python 路径只通过当前进程注入，不写入提交。
TIGER_TEST_PYTHON=/absolute/path/to/python node --test \
  bundles/trading/capabilities/workflow-client.test.mjs \
  bundles/trading/capabilities/workflow-model-required.test.mjs \
  bundles/trading/capabilities/repo.test.mjs \
  scripts/tiger-agent.test.mjs scripts/tiger-agent-mcp.test.mjs \
  scripts/tiger-agent-config.test.mjs \
  scripts/tiger-intelligence.test.mjs scripts/tiger-intelligence-workflow.test.mjs \
  scripts/agent-capabilities.test.mjs governance/scripts/agent-capabilities.test.mjs
```

这些 Node 测试包含确定性传输夹具，不能证明公开行情调用、所选 profile 的完整阶段执行或 paper 账户提交成功；阶段契约以该 profile 的实际 describe 为准。完整验收还需要真实 Python 集成、故障/并发/证据篡改测试，以及通过当前宿主 CLI 或工具取得的实际公开数据 paper 回执。任何 completion 声明须关联源代码身份、run_id、receipt hash、验证结果及限制。

| 错误或现象 | 处理 |
|---|---|
| doctor not_ready / Python 版本不符 | 指定已安装本仓依赖且满足 3.11+ 的实际 Python，再检查 doctor |
| READ_ONLY | 由操作者在部署配置中选择 paper；请求字段不能升级权限 |
| RESULT_INVALID / EVIDENCE_* | 保留原 run_id 和证据，调查格式/哈希/绑定；不得拼接成功摘要 |
| MODEL_REQUIRED_FAILED | 保留原失败身份和证据。新模型 I/O 错误可附固定安全码、HTTP 状态或 schema 路径/关键字；不保存响应正文、推理内容或任意错误字符串。旧证据没有诊断字段时，不能事后断言原始原因。定位原因并完成修复或服务恢复验证后，为新的尝试分配独立身份；原失败不改，受控入口不内部重跑或自动换键重试 |
| 配置锁存在 | 先确认写入进程是否仍活跃；不要绕过锁覆盖其他配置 |
| MCP 菜单没有 run | readonly 的正常结果；配置改动须在实际宿主重新连接后验证 |
| 公开源失败/访问挑战/数据不足 | 依据回执如实报告缺口；不注入演示行情，不绕过访问控制 |

当前 profile 不接受 skip、自由 argv、手改 score、tune、approve、live、provider 或 root 覆写。后续扩展必须修改已审查的固定 profile、Python 流程和验证契约，不能只在 Node 层放开参数。

## 8. 情报同步、监测、证据与独立评测

`node scripts/tiger-intelligence.mjs sync|monitor|status|evidence` 是独立采集入口。
它默认只读，可信启动配置可明确选择 acquire，并限制归档目录、来源、次数和时限；
请求只接受审定代码、登记 source IDs 与含时区的截止时点。同步完成只表示采集记录
已冻结，不能当作五个模型阶段、paper 账户或当日研究完整成功。来源正文、附件、
转载身份和时间未核实时仍保留缺口。

离线 `python scripts/intelligence-benchmark.py` 对固定 dataset/capture 计算召回、
主证据、时序、实体、转载、分层延迟与已报告成本，不联网、不调用模型。仓内 100 题
五类是明确标记的 synthetic_fixture，不能冒充人工金融 gold 或真实供应商比较。
请求示例、来源条件、预算、失败码与真实评测准备流程见
[INTELLIGENCE-ACQUISITION.md](INTELLIGENCE-ACQUISITION.md)。

## 9. 独立主题研究与持续来源监控

新增 [INTELLIGENCE-TOPICS.md](INTELLIGENCE-TOPICS.md) 定义不写账户的 topic 命名空间。CLI 为 `scripts/tiger-topic.mjs`，STDIO MCP 为 `scripts/tiger-topic-mcp.mjs`；两者调用同一 Python 状态、固定快照和独立报告验证。它们使用自己的 readonly/acquire/research 可信模式，不改变 paper 六操作及四字段合同。

用户仍只需对话。Agent 负责把问题和审定主体转为 topic、提交一次 research、保存原 job_id 并持续查询，最后调用 report 验真。只读宿主可以查询已有证据；需要采集或付费研究时，权限和模型环境必须由已授权的可信部署提供，不能放进工具参数。

可选独立服务器条目见 [topic-mcp.json.example](../config/agent-clients/topic-mcp.json.example)。原 `tiger-agent-config.mjs` 继续只管理 paper workflow 条目，不会自动添加 topic research 权限。具备本地执行能力的 Codex/Qoder Agent 可通过同权限 CLI 完成主题任务；实际宿主挂载、原始协议验证和真实研究回执仍分别报告。
