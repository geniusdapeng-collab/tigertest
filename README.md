<div align="center">

# 老虎全球资产管理（Tiger Global Asset Management）

**由 AI 基金经理统筹的全自动化资产管理系统——覆盖美股 / A股 / 港股，24 小时运行。**

**No prediction. Process, discipline, audit.（别人预测市场，我们执行纪律。）**

[![License](https://img.shields.io/badge/license-Apache--2.0-9A7B2D)](LICENSE)
[![GitHub](https://img.shields.io/badge/repo-workroom--tiger-1B2A4E)](https://github.com/geniusdapeng-collab/workroom-tiger)

</div>

> **本系统当前仅运行于模拟盘（paper trading），不构成任何投资建议，不承诺收益。**

---

<!-- WORKLOOM-AUTONOMOUS-OPERATIONS:BEGIN -->
## 🧭 理论纲领 · AI 自主经营模式

> **客户购买的不是工具，而是一家正在运行的公司。**
> 本仓是 WorkLoom「AI 自主经营」体系的一部分；完整纲领《AI 自主经营纲领——论一个新经营范式的诞生》见 **[AI-AUTONOMOUS-OPERATIONS.md](AI-AUTONOMOUS-OPERATIONS.md)**。

- **成本结构翻转**：人类公司的管理动作，是为"人不可靠"支付的三种对冲税（激励 / 层级 / 监督）；AI 自治退掉这笔税，转而为 **边界定义 + 持续评测** 付费。
- **三个翻转**：管理对象从动机 → **边界**；经营动作从传递信息 → **分配注意力**；行业 knowhow 从"人在经验在" → **可计量资产**。
- **责任不可自动化**：AI 不能坐牢、不能赔钱。围栏 / 人审 / 汇报 / 事件账本构成的治理层，是自治的**对价**，不是限制器。
- **终态判据**：没有客户操作，这件事还会不会发生？"开箱即用"不够——终态是 **没开箱就在运营**：首次登录看到的是已经跑出来的业务进展与第一份汇报。
- **自治权是挣来的**：观察 → 请示后执行 → 汇报后执行 → 全权自治，按决策域用回测证据升降档；汇报透明是自治的价格标签。
- **度量即货币**：自治率、客户干预率、回测命中率三条曲线决定自治权扩张；模拟成功不计入自治率。
<!-- WORKLOOM-AUTONOMOUS-OPERATIONS:END -->

## 产品定位

老虎交易是一套**机构级纪律机器化的资产管理系统**：一位 AI 基金经理统领 37 人数字投研班组，以自研 **21 环节管线 × 六层决策栈**为交易内核，以 [WorkLoom IM](governance/README.md) 为治理外壳，把[《AI 短线美股交易（1–15 天波段版）白皮书》](reports/AI短线美股交易白皮书_20260730.pdf)的每一条交易纪律编译成不可绕过的机器闸门。

过去两年，AI 炒股项目死法高度一致——**让大模型既当裁判又当运动员**：它预测涨跌，但预测不可证伪；它给出理由，但理由事后无法审计；它亏了钱，但没人说得清亏在哪一步。老虎交易从第一天就拒绝这条路，答案是把决策拆成三层并物理隔离：

- **语义归 LLM**：读新闻、评板块叙事、组织多空辩论——这是 LLM 擅长的；
- **数值归规则**：评分、阈值、仓位、止损——全部是确定性代码，同一输入永远同一输出；
- **闸门必须确定**：开不开仓，永远由硬阈值决定，模型再自信也推不开这扇门。

治理外壳再把每个动作变成可追责事件：**谁、何时、对什么、做了什么、触发了哪条规则**——全部落进 SHA-256 哈希链，任何一笔交易三年后仍能逐条重算。这不是一个"会炒股的模型"，这是一家**流程完全透明、纪律完全闭环的数字资产管理公司**。

<p align="center">
  <img src="docs/images/diagrams/architecture.png" alt="老虎全球资产管理 · 系统架构图（体验层 / 治理外壳 / 交易内核 / 外部依赖与数据层）" width="92%"/>
</p>

- **交易内核**：仓库根目录即交易内核（[完整文档](docs/CAISHEN_README.md)）。决策栈、阈值、风控公式以 `trading_system/config.py` 为 single source of truth。
- **治理外壳**：`governance/` 为 WorkLoom IM 底座，只做治理不做决策——把每个交易动作变成可追责事件，把白皮书红线变成机器级保险的第二道锁。接入设计见 [docs/GOVERNANCE.md](docs/GOVERNANCE.md)。
- **行业角色包**：`governance/bundles/trading/`——AI 基金经理班组 presets、三层围栏包、投资 skills。

---

## 投资哲学：为什么我们刻意不预测市场

任何一个在市场中活过两轮牛熊的专业投资者都会承认三件事：**预测胜率会均值回归，杠杆会放大运气，而纪律是唯一可复利的资产。** 老虎交易把这三件事写成了系统公理：

1. **期望值思维替代涨跌判断。** 系统不回答「明天涨还是跌」，只回答「当前这笔交易的风险回报比是否值得用 0.8% 的净值去交换」。MRS 许可、SHS 主线、ICS 景气、TSS 买点，四道闸门过滤的不是"会涨的股票"，而是**值得承担风险的机会**——MRS*<4 的市场，空仓本身就是收益。
2. **纪律的价值在于它不可被破坏。** 人类基金经理的纪律毁于情绪与疲劳，模型的"纪律"毁于幻觉与谄媚。本系统的纪律存在于代码与围栏引擎中：亏损 1R 必走、单票不超 20%、数据不实立即停手——没有例外通道，没有"这次听我的"。
3. **可审计性是机构化的入场券。** 桥水们真正的壁垒不是策略，是**流程**：每一步有依据、有留痕、可复盘、可迭代。老虎交易把这套流程开源化：21 个环节逐一验收点名，缺一环即运行事故；每个决策都能回放到当时的输入、数据版本与规则版本。
4. **承认不知道，比假装知道更值钱。** 旧演示/禁用模型配置将模型缺失透传披露；真实模型必需配置在模型不可用时停止整个任务，禁止降级交付。行情源断供时诚实失败，参数不显著时保持默认——系统为「不知道」设计了正式出路，而不是用编造掩盖。**宁可不答，不可错答；宁可不交易，不用假数据交易。**

---

## AI 技术哲学：把前沿认知变成工程纪律

这套系统对 AI 的理解，不是"接一个大模型 API"，而是建立在对当前一代 AI 技术边界清醒认知之上的一整套工程方法论。以下每一条，都是我们在真实系统里交过学费后沉淀的判断。

### 大模型的能力边界，必须用架构来尊重

LLM 的本质是**高维语义压缩器**：它擅长理解、归纳、生成、辩论，但它的数值输出本质上是"看起来像数字的文本"——**幻觉不是 bug，是生成式模型的固有属性**。在消费级应用里，幻觉是体验问题；在交易系统里，幻觉是本金问题。所以我们的答案不是"换一个更准的模型"，而是架构分层：**语义归 LLM，数值归规则**。LLM 产出的一切语义结论（新闻含义、板块叙事、多空论点）都必须经过确定性规则的闸门才能转化为动作——模型可以影响系统，但永远不能越过系统。这与前沿研究对 high-stakes 场景的共识一致：**生成式 AI 负责候选与解释，符号系统负责验证与执行**。

### 确定性是最高级的 AI 工程

当前 Agent 系统最大的工程债是**不可复现**：同一个输入，两次运行两个结果，出了事故无法归因，优化无从下手。我们把**一致性（同输入同输出）列为十条铁律之一**，并在三个层面落地：决策层全部规则化（阈值、公式、状态机）；执行层盘中零 LLM（秒级动作 100% 确定性）；数据层五元事件 + 哈希链（每一步的输入快照可回放）。这意味着老虎交易的每一次决策都可以被**逐帧复盘**——回测、归因、参数迭代因此才有意义。没有确定性的 AI 系统，连"它在改进还是在退化"都无法回答。

### 单智能体的上限，用组织设计来突破

让一个通用 Agent 包办投研全流程，结果必然是样样通、样样松——上下文被稀释，责任无法定位，错误相互传染。前沿多智能体实践给出的方向是**专业化分工 + 对抗性制衡**，我们把它做成了真实的组织架构：37 名数字员工各有窄而深的职责（清洗、舆情、链景气、风控、合规……）；多方研究员与空方研究员**基于同一份证据对抗辩论**，主持官只汇总分歧不下结论；风险岗与收益岗相互独立，组合风险官对收益岗有一票叫停权。这复刻的是顶级资管机构百年演化出的组织智慧：**Alpha 来自分工，稳健来自制衡**——只不过执行者从人换成了 Agent。

### 护栏必须编译成代码，而不是写进提示词

"请千万不要越过红线"写进 system prompt，是最脆弱的护栏——一次幻觉、一次注入、一次上下文挤压就能绕过。我们把白皮书纪律编译为**围栏 DSL 引擎**：每一个动作（开仓、调价、发布、授权）在执行前 100% 经过 `auto / review / block` 三级判定，多条规则命中取最严（deny 优先），基线只可收紧不可放宽。**策略即代码（Policy-as-Code）**：红线不在模型的自觉里，在执行路径的物理结构上——售价低于成本，系统不是"提醒"，是拒绝。

### 模型的失败，需要一条设计好的出路

前沿系统与玩具系统的分水岭，不在于成功时多聪明，而在于**失败时多诚实**。旧演示/禁用模型配置允许不可用时透传披露（🔁透传 + 原因留痕）；`us-public-model-required-paper-v1` 则要求真实模型，任何模型不可用、空结果或透传都停止整个任务，不发布研究成功。所有配置均禁止降档重答、禁止规则伪造语义分；数据源断供时诚实失败，绝不回退合成数据。`main.py --demo` 的五阶段透传是历史离线演示，不是模型研究验收。

### 渐进式自治：AI 权力的授予必须比能力更谨慎

前沿 AI 安全的核心命题是**能力与控制的错配**——能力增长永远快于信任建立。我们的答案是渐进式自治（Progressive Autonomy）：数字 CEO 出厂默认关闭；启用需六步深度授权（风险揭示 / 逐条确认 / 边界滑杆 / 试用计划 / 身份核验 / 签署留痕）；先当 3 天影子只模拟不执行，再 7 天试用权限自动减半，到期不自动续期；五级权限体系把「能做什么」与「做到什么程度」解耦。**每一项 AI 权力都来自人类逐条签署过的授权书，且随时可收回**——这不是不信任 AI，这是让 AI 可以被长期信任的唯一方式。

### 前沿技术实践全景

| 前沿认知 | 本系统的工程落地 |
|---|---|
| 生成式模型幻觉不可消除，只能隔离 | 语义 / 数值架构分层，LLM 产出必经规则闸门 |
| Agent 系统必须可复现才可迭代 | 决策规则化 + 盘中零 LLM + 事件溯源回放，同输入同输出 |
| 多智能体靠分工与制衡，不靠单体全能 | 37 岗专业化班组 + 多空对抗辩论 + 风险/收益岗位独立 |
| Policy-as-Code 优于 Prompt 护栏 | 围栏 DSL 引擎：三级判定、deny 优先、只紧不松、物理阻断 |
| 诚实失败（honest failure）是可信前提 | 透传披露 / 熔断拒答 / 诚实失败，失败路径全部设计化 |
| 渐进式自治（Progressive Autonomy） | 数字 CEO 默认关闭 + 六步授权 + 影子→试用 + 五级权限 + 到期不续 |
| 模型路由的成本-时延-质量帕累托 | L1/L2/L3 场景定档 + 谷时批量（×0.2）+ 关键路径 noDowngrade |
| Agent 可观测性（Observability） | 五元事件 + SHA-256 哈希链 + 21 环节逐一点名验收 |
| 组织记忆是 Agent 的复利资产 | 每次决策依据、每次驳回理由沉淀为可检索记忆，跨周期复用 |
| 工具使用（Tool Use）需要感知分层 | computer-use 65 动作三层感知：DOM 级 / 语义树级 / 像素级兜底 |

---

## 核心能力

### 交易内核：21 环节管线，环环相扣缺一即事故

每日全市场决策不是一次模型调用，而是一条 **21 环节的可验收流水线**：搜索采集 → 规则清洗 → LLM 语义清洗 → 交叉验证 → 数据备料 → 技术监测 → 周期联动 → 舆情评估 → 风险评估 → 融合 → 板块叙事 → L1 市场许可 → L2 主线确认 → L2b 链景气 → L0 全市场扫描 → L3 买点评分 → L4 风控闸门 → 多空辩论 → 校准迭代 → 报告生成 → 日复盘。运行报告对每个环节逐一「点名」验收（✅ 通过 / 🔁透传 / ❌事故），**任何一个环节缺席即视为运行事故**，杜绝"静默降级"。

<p align="center">
  <img src="docs/images/diagrams/data-pipeline.png" alt="数据管道与 Agent 协作链路图（采集 → 清洗与语义 → 信号加工 → 六层决策栈 → 辩论与执行 → 复盘迭代闭环）" width="96%"/>
</p>

### 六层决策栈：顺序不可颠倒的漏斗

| 层 | 职责 | 闸门 | 不通过意味着什么 |
|---|---|---|---|
| L0 全市场扫描 | 数千只标的初筛，锁定候选池 | 流动性与数据完整性 | 不值得占用算力 |
| L1 MRS 市场许可 | 判断当前市场是否允许承担风险 | MRS*≥6 方可标准开仓；**MRS*<4.0 全面禁开仓** | 空仓等待，强做即违规 |
| L2 SHS 主线确认 | 确认资金主线所在板块 | SHS≥7.5 | 逆主线不开仓 |
| L2b ICS 链景气 | 确认产业链景气度与传导方向 | 链上关键节点状态达标 | 景气不明不重仓 |
| L3 TSS 买点评分 | 个股级买点精评 | TSS_final≥7.2 | 不是好买点，继续观察 |
| L4 风控闸门 | 仓位、止损、组合暴露终审 | 1R / 单票 / 组合上限 | 物理拒绝执行 |

生产模式下每日漏斗：**全市场数千只 → 初筛数百只 → 精评数十只 → 放行个位数**。绝大多数股票被拒绝——**拒绝本身就是纪律的产出**。

### 风控体系：把机构风控公式写成物理约束

- **1R = 净值 0.8%**：任何一笔交易的最大计划亏损锁死在净值的 0.8%，止损位在开仓瞬间确定，不容盘中协商；
- **单票 ≤20% + 轻仓通道**：标准仓位之外，信号强度不足时仓位自动按 ×0.30–0.40 折减；
- **双轨止损**：结构止损（跌破关键结构位即离场）+ 时间止损（持仓超时未达预期即离场），盈利 ≥2R 自动上移保护线锁定利润；
- **Kill Switch 急停**：触发后全系统只许平仓与留痕；治理侧「紧急制动」60 秒内全端生效，恢复后断点续跑；
- **组合视角**：回撤、暴露、相关性按日体检，组合风险官对组合层面异常有一票叫停权。

### 数据工程：先证真，再决策

- **SearchHub 六源采集 + 七环行情降级链**：主源故障逐级降级，每轮记录「实际拉取源、降级次数、失败原因」——你可以逐轮审计系统当时看到的世界；
- **绝不回退合成数据**：全部源不可用 → 立即诚实失败并告警。演示模式的合成数据在日报显著位置标注「离线合成」，与真实行情物理隔离；
- **零基线纪律**：每轮评分从零开始，杜绝「因为昨天看好所以今天继续看好」的惯性误判；
- **数据卫生五条**：来源可溯、口径一致、异常申报、合成隔离、留痕可验（[docs/DATA_HYGIENE.md](docs/DATA_HYGIENE.md)）。

### AI 分工与工程红线：幻觉永远碰不到交易按钮

- **5 个 LLM 环节全部登记在案**：clean.llm_semantic / tech.sentiment / tech.risk / sector.narrative / decision.debate。model-required 配置逐阶段校验实际 Flash 调用、完整输入与输出、来源和 schema；不能只靠注册表声称模型已执行；
- **LLM 不可逆**：没有任何"模型挂了就用规则编一个语义分"的回退分支。旧演示/禁用模型配置如实透传，model-required 配置失败即停；`main.py --demo` 的透传不能计为真实模型完成；
- **盘中零 LLM**：秒级交易决策 100% 纯规则确定性执行，LLM 负载全部落在盘前 / 盘后批量窗口（谷时算力 ×0.2，成本与稳定性的双赢）；
- **最终模型受风险上限约束**：旧路径的辩论只作为报告证据；model-required 的实际最终模型可选择或否决已经过风险闸门的买入，不能新增、扩大仓位或绕过数值限制。

### 迭代诚实：WFA + DSR，向回测过拟合宣战

量化系统最大的自欺是**回测过拟合**：在历史数据上调出漂亮曲线，实盘一塌糊涂。老虎交易的免疫系统：

- **WFA 滚动前推校正**：参数优化必须在前推样本上验证，而非全样本拟合；
- **DSR 缩减夏普比率**：用 Deflated Sharpe Ratio 校正多重检验偏差——试了 100 组参数总有一组"显著"，DSR 专门拆穿这种假显著；
- **首个 DSR 检查点设在运行满 60 天**：不显著？**保持默认参数，拒绝为调而调**；
- **提案审批制**：任何参数变更走「WFA 提案 → 人类审批 → 次日生效并披露」，每次变更都可回放对比。

### 复盘闭环：每笔亏损都必须教会系统一件事

诸葛复盘条线：每笔已结算交易**逐笔归因**（赚在哪、亏在哪、是流程问题还是概率问题）→ 参数体检 → 生成 WFA 提案 → 人类审批 → 次日生效并披露。命令行 `main.py --review-list / --review-approve / --review-reject` 全流程留痕，治理外壳同步生成审批卡。

### 治理外壳：监管级的可追责性

- **五元事件库**：每个动作（谁 / 何时 / 对什么 / 做了什么 / 规则影响）append-only 落库，SHA-256 哈希链逐条锁定——包括系统自己的操作也不可篡改，`pnpm db:verify-chain` 可当着任何人的面逐条重算；
- **围栏引擎**：白皮书阈值编译为 block 级基线，突破红线的动作**系统物理上无法执行**；基线只可收紧不可放宽；
- **五级权限**：L2 日常微调自主，L3 跨组合归集团层，L4 涉大钱 / 对外承诺 / 围栏放宽必须人类批准，L5 安全禁区物理熔断；
- **夜班值守**：盘后批量作业（复盘 / 归因 / 体检 / 参数校验）落在免打扰时段，清晨 08:30 交付三段式决策包（已完成 / 待审批 / 需介入）；北京时间 22:00–08:00 恰为美股盘中，夜班班组跨时区值守。

### AI 基金经理与数字 CEO

37 名数字员工组成完整投研班组：多方 / 空方研究员、链景气 / 链舆情分析官、数据质量官、辩论主持官、全球宏观哨兵、盘中交易员、组合风险官、校准官、合规官……数字 CEO 统领班组但**出厂默认关闭**：启用须在「董事长视图」完成六步深度授权（风险揭示 / 逐条确认 / 边界三滑杆 / 试用计划 / 身份核验 / 签署留痕），先当 3 天「影子」只模拟不执行，再 7 天试用（权限减半），到期不自动续期。**AI 的每一项权力都来自于你逐条签署过的授权书。**

### 通用模型路由：金融降级铁律

金融场景的成本与可靠性双重要求，由底座通用模型路由承载：

- **金融场景路由表**（`governance/bundles/trading/model-policy.yml`）：新闻标注 / 科技舆情 L2 盘前谷时批量；板块叙事评分 / 多空辩论 L3 旗舰档（`noDowngrade` + `fallback: passthrough-disclose`）；
- **金融降级铁律**：语义归 LLM，数值归规则；旧配置将模型缺失透传披露，model-required 配置失败即停，均**禁止降档重答或规则代写语义**；
- **成本可审**：每一次模型调用按场景定档、按倍率计量、按事件留痕，月度模型开支可逐场景审计。

### 白皮书十条铁律（全系统最高纪律，任何升级不得违反）

1. **Edge 不是预测是流程**：先许可(MRS)→再主线(SHS)→确认链景气(ICS)→最后买点(TSS)
2. **刻意不赚的钱**：不赌财报；MRS*<4 不硬做；数据地基不实立即诚实失败
3. **开仓硬逻辑**：标准做多 MRS*≥6 且 SHS≥7.5 且 TSS_final≥7.2；轻仓通道仓位 ×0.30–0.40；MRS*<4.0 禁开仓
4. **风控机械化**：1R=净值 0.8%；单票≤20%；结构/时间止损；≥2R 盈利保护；Kill Switch
5. **数据工程**：多源降级链绝不回退合成数据；零基线纪律每轮从零开始
6. **工程红线**：21 环节点名缺一即事故；LLM 不可逆（无规则回退分支）；异常透传留痕
7. **迭代诚实**：WFA+DSR 校正，不显著保持默认参数
8. **模拟盘公开验证**：全 AI 掌控、赚亏如实、禁止粉饰
9. **AI 分工**：语义归 LLM，数值归规则，闸门必须确定
10. **一致性**：同输入同输出，回测/复盘/迭代的前提

---

## 真实运行实录

以下不是效果图，是 `python3 main.py --demo`（离线合成数据，端到端 21 环节）在本仓库的**真实运行输出**：

- **21 环节点名验收**：规则环节全部 ✅；5 个 LLM 环节（clean.llm_semantic / tech.sentiment / tech.risk / sector.narrative / decision.debate）在无密钥环境下全部「🔁透传：LLM 不可用」——**如实披露，无一伪造**；
- **当日决策日报**（`reports/日报_20260901.html` 随仓归档）：市场环境综合评分 **7.98**（健康趋势），主线板块锁定科技 / 半导体，最热赛道晶圆代工链；全市场扫描后**仅放行 6 只**（TEAM / GTLB / ESTC / HPE / SNPS / DELL），每只附逐层评分卡与数据源、降级次数留痕；
- **小G模拟盘**：净值 **$99,992**（$100,000 起步），双页签日报（决策日报 + 个股深度 + 策略验证）逐笔可回放；
- **治理外壳**：老虎交易工作台 37 名员工在线、19 条生效围栏、数字 CEO 未授权默认关闭——权力边界一目了然。

---

## 系统截图（模拟运行态实拍）

以下截图均来自系统**模拟运行态**：治理外壳为种子演示数据 + 离线确定性模型（页面顶部琥珀色横幅「当前为全模拟运行态」为系统原生标识）；交易内核日报为 `main.py --demo`（离线合成演示数据，端到端 21 环节）真实运行产物。

### PC 端 · 治理外壳与交易内核

| 经营剧场 · 老虎交易工作台（默认首页 `/`） | 数字员工 · 人机混编通讯录（`/agents`，37 个岗位） |
|---|---|
| ![经营剧场](docs/images/shots/pc-home.png) | ![数字员工](docs/images/shots/pc-agents.png) |

| 统一待办（`/inbox`） | 审批中心（`/approvals`） |
|---|---|
| ![统一待办](docs/images/shots/pc-inbox.png) | ![审批中心](docs/images/shots/pc-approval.png) |

| 夜班中心频道（`/night`） | 董事长视图 · 数字 CEO（`/executive`） |
|---|---|
| ![夜班中心](docs/images/shots/pc-night.png) | ![董事长视图](docs/images/shots/pc-chairman.png) |

| 围栏规则（`/guardrails`） | 事件账本（`/events`） |
|---|---|
| ![围栏规则](docs/images/shots/pc-rules.png) | ![事件账本](docs/images/shots/pc-events.png) |

| 考试院（`/exams`） | 组织记忆（`/memory`） |
|---|---|
| ![考试院](docs/images/shots/pc-exams.png) | ![组织记忆](docs/images/shots/pc-memory.png) |

| 技能中心（`/skills`） | 经营报告 · 决策日报（`/reports`） |
|---|---|
| ![技能中心](docs/images/shots/pc-skills.png) | ![决策日报](docs/images/shots/pc-reports.png) |

### 数字人 · 织伴（Live2D 常驻值守）

| 织伴开场（S0–S4 全身像登场） | 织伴面板（聊聊 / 设置 / 记忆） |
|---|---|
| ![织伴开场](docs/images/shots/pc-mate-welcome.png) | ![织伴面板](docs/images/shots/pc-mate-chat.png) |

### 移动端

| 治理外壳 · 掌上经营主页 | 治理外壳 · 数字员工名册 | 交易内核 · 决策日报（移动视口） | 治理外壳 · 掌上日报（手机壳页） |
|---|---|---|---|
| ![掌上经营主页](docs/images/shots/mb-home.png) | ![移动端数字员工](docs/images/shots/mb-agents.png) | ![决策日报移动版](docs/images/shots/mb-daily-report.png) | ![掌上日报](docs/images/shots/mb-handoff.png) |

| C 端 · AI 服务前台（对话） | C 端 · 服务大厅 |
|---|---|
| ![AI服务前台](docs/images/shots/mc-chat.png) | ![服务大厅](docs/images/shots/mc-service.png) |

> **数字员工名册（`/agents`）**：AI 基金经理统领的 **37 位数字投研成员**（研究 / 辩论 / 执行 / 风控 / 复盘 / 数据六条线）与人类投资者同一本通讯录——每位都有档案（来源 Bundle、**围栏授权逐条对账**、绑定技能包、运行约束、30 天战绩与段位）。风控与复盘条线多为**只读岗位**（无写工具，物理上碰不到交易按钮），夜班岗位 22:00–08:00 自动上线。
> **数字人织伴（LoomMate）**：全页面常驻的 Live2D 值守员——语音 + 口型播报晨间决策包、风控熔断与夜班交接；三态（小角落 / 大形象 / 屏保）可切，屏保模式替你守着全场，记忆面板把「它记住了什么」摊开给你看。

---

## 使用方式

<p align="center">
  <img src="docs/images/diagrams/business-flow.png" alt="投资者视角 · 使用方式与业务闭环图（装机 → 观察 → 授权 → 试用 → 日常 → 迭代，以及你与系统的一天）" width="96%"/>
</p>

### 账号体系（基座自带 · 资管场景最严纪律）

老虎资管开箱自带完整账号体系（基座 `packages/base/accounts` 同步而来，无需配置）：

- **团队角色**：负责人（owner）/风控主管（manager）/研究员（staff）/合规审计（readonly）四角色；**凡涉及资金的策略动作永远走最高审批档**（owner 必审+二次验证），与「纪律大于预测」的投资哲学同构；
- **合规与审计**：readonly 角色 + 全量登录台账 + 操作留痕不可删——审计人员只看不动，但看得比谁都全；
- **外部会计师/托管行**：observer 观察者授权（指定报表只读+时效）；外包研究员用一次性任务通行证；
- **安全纪律**：邮箱+密码+二次验证可选强制、连续失败锁定、设备会话可远程下线、API 密钥 scoped 签发（量化对接）；
- **页面**：/login 登录 · /activate 自助开通 · /invite 接受邀请 · P28 统一待办 · P29 我的 · P30 成员管理 · P31 伙伴授权。

## 快速开始（交易内核）

只通过 Codex、Qoder 等平台对话操作后端时，从 [对话操作指南](docs/TIGER-OPERATOR.md) 开始。
Agent 会先检查环境，再按固定 CLI/MCP 提交与查询同一个任务，取得独立核验回执后汇报。
平台配置、Qoder CLI 与桌面导入的区别见 [客户端接入指南](docs/AGENT-CLIENTS.md)；
同一 profile 的纸面账户保持同一 state-root，日常新任务只更换逻辑幂等键。

```bash
bash scripts/setup.sh                # 一键安装+自检（或 pip install -r requirements.txt）
python3 -m pytest tests/ -q          # 交易内核测试（本机实测 327 passed，2026-09-19）
python3 main.py --demo               # 离线演示（合成数据，端到端 21 环节）
python3 main.py --universe focused --top 30 --picks 8 # 受控 v2 默认：120 只核心扫描
python3 main.py --universe full --top 30 --picks 8    # 旧通用入口的全市场实验
bash scripts/stack_setup.sh          # 全栈：+ WorkLoom 治理底座（Node≥24/Docker）

# S6 复盘闭环与审批流
python3 main.py --review-list                            # 查看 WFA 参数提案
python3 main.py --review-approve <id>                    # 批准 → 次日生效并披露
python3 main.py --review-reject <id> --reason "..."      # 驳回（原因必填）
```

普通非受控旧入口保留离线演示与透传语义，详见 [docs/QUICKSTART.md](docs/QUICKSTART.md)。新的 AI 研究和官网更新必须显式使用受控 CLI/MCP 和 `us-public-intelligence-paper-v2`，按 [Agent 操作指南](docs/AGENT-CLIENTS.md) 加载仓库外受保护环境文件；模板见 [`.env.example`](.env.example)。新配置先冻结独立数值候选，再按研究问题采集登记来源、原文与附件，逐目标验证实体、事件、时间和用途并冻结证据包。缺资料的目标保留 `null` 与缺口；搜索成功不会自动获得评分或行动资格。原 `us-public-model-required-paper-v1` 和更早 profile 保留历史回放。

受控 v2 默认用 `focused` 扫描池：120 只内嵌核心高流动性标的。它是稳定的广覆盖起点，
不是美股全量，也不等于最终研究范围；运行时仍按价格、20 日成交额、历史和波动率过滤，
再结合相对强度、均线、波动收缩和接近新高排序。产业链行情会单独并入用于板块与风险
上下文。需要对照广扫时可用 `extended`（480 只基础池，加链上补充标的后当前为 505 只），
需要完整证券目录时用 `full`（Nasdaq Trader 官方清单）。历史 profile 继续按其冻结源码和
合同读取，不能把今天的池子或结果回写到旧任务。

受控入口提供 `doctor / describe / run / resume / supplement / status / receipt / verify`。`resume` 只恢复同一 stopped run 的原身份、源码、策略、账户 before-state 与 deadline；`supplement` 从已核验 pending candidate 新建完整研究 admission，不复用旧证据快照或交易批准。`status` 的 `data.research_status` 分开显示流程、证据、决策和账户四轴，receipt/verify 仍必须独立通过。

文本语义仍固定 `deepseek-flash` + JSON mode，官方别名于 2026-10-26 观察为 `DeepSeek-V4.1-Flash-1017`，不宣称不可变快照。每次请求使用仓库内固定官方 tokenizer 对完整序列化输入计数，并申请全部合法剩余输出窗口；thinking 由场景策略决定：清洗/舆情非思考，风险/板块叙事 high，最终多空判断 max。工具请求/Fetch/字节/轮次/时限预算与模型窗口分别管理。可选 Brave、Tavily、Exa 仅用于发现资料，不替代文本模型；官方能力见 [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)、[Token 用量](https://api-docs.deepseek.com/zh-cn/quick_start/token_usage/) 和 [Search & Fetch](https://api-docs.deepseek.com/guides/search/)。五个模型阶段真实完整且独立校验通过才算完成；disabled、passthrough、规则语义替代或任何失败都不能交付为成功。该配置不启用 active 实验。网站的人读研究包含为什么关注、入场/失效条件、风险及来源；资金和数值为明确标识的模拟参考，富途/老虎真实操作由人执行，实际收益只来自真实成交回报。

独立来源同步/短时监测、原文证据查询和离线 100 题五类合同评测见 [情报采集指南](docs/INTELLIGENCE-ACQUISITION.md)。仓内评测是 synthetic_fixture，不代表真实供应商效果或生产 SLA。

### 治理外壳三端（模拟运行态）

```bash
cd governance
pnpm setup && pnpm preview:all
```

| 端 | 地址 | 说明 |
|---|---|---|
| PC 端 · 治理工作台 | http://localhost:3000 | 经营剧场（老虎交易工作台，默认工作区）/ 审批中心 / 夜班中心 / 董事长视图 |
| 移动端 · C 端 | http://localhost:3002 | 老虎交易 AI 服务前台 H5（查净值 / 数据服务 / 异常申报，演示直登） |

> 治理外壳默认进入 `tiger-trading` 老虎交易工作区；如需切换其他演示工作区，启动 web 时设置 `VITE_DEMO_WORKSPACE` / `VITE_DEMO_MEMBER` 覆盖。

### 从模拟运行态到真实数据

治理外壳种子完成后即进入**全模拟运行态**（演示数据 + 内置确定性模型，零外部依赖），页面顶部常显琥珀色横幅提示。点击「接入真实数据 →」进入**落地向导**（`/onboarding`）：① 环境自检（自动）→ ② 接入真实大模型（DeepSeek / Kimi / 智谱 / OpenAI 预设一键填，真实试调通过才落盘，保存即全链生效免重启）→ ③ 登记经营主体 → ④ 启用真实模式（横幅熄灭，全程五元事件留痕）。

---

## 面向 AI Coding Agent（治理外壳开发）

以下启动和页面验证要求用于 `governance/` 治理外壳开发。仅通过 CLI/MCP 操作 Tiger 后端时，按 [对话操作指南](docs/TIGER-OPERATOR.md) 执行，无需启动治理壳或官网。

治理外壳开发沿用 WorkLoom IM 底座约定：

1. 进仓先读 [`governance/README.md`](governance/README.md)；全量能力清单见 `governance/docs/capability-map.md`；
2. **首启必跑 `pnpm preview:all`**（governance 目录下），未完成视为环境初始化未完成；
3. **一键能力巡游 `pnpm agent:tour`**：环境 + 数据 + 服务 + 浏览器四层自检；
4. **操作电脑入口**位于 `governance/packages/base/computer-use/`，包含浏览器 DOM、GUI 语义树与截图感知；先运行 `pnpm computer:preflight && pnpm computer:smoke` 核对本机依赖、权限和执行结果。目录存在不能代替运行验收，HTTP / MCP 接入见 `governance/docs/computer-use-production.md`；
5. **验证纪律**：改完代码必跑 `pnpm suite`；发布前必跑 `pnpm release:gate`；改事件 / 号源后跑 `pnpm db:verify-chain`；UI 改动必须用浏览器能力实际打开页面截图核对。

---

## 文档地图

| 文档 | 说明 |
|---|---|
| [docs/CAISHEN_README.md](docs/CAISHEN_README.md) | 交易内核完整文档 |
| [reports/AI短线美股交易白皮书_20260730.pdf](reports/AI短线美股交易白皮书_20260730.pdf) | 交易理念白皮书（最高纪律） |
| [docs/GOVERNANCE.md](docs/GOVERNANCE.md) | WorkLoom 治理外壳接入设计 |
| [docs/PROJECT_INTRO.md](docs/PROJECT_INTRO.md) / [docs/PROJECT_INTRO_EN.md](docs/PROJECT_INTRO_EN.md) | 项目介绍（中文详细版 / English） |
| [README_EN.md](README_EN.md) | English README |
| [research/UPGRADE_PLAN_v3.md](research/UPGRADE_PLAN_v3.md) | 升级方案（已确认） |
| [research/01_benchmark.html](research/01_benchmark.html) | 调研一：行业标杆竞品深度调研（桥水 AIA 等 9 家） |
| [research/02_frontline.html](research/02_frontline.html) | 调研二：投资团队一线作业与 know-how 调研 |
| [docs/AGENT_CENSUS.md](docs/AGENT_CENSUS.md) | Agent 普查（20 个 Agent/模块分类） |
| [docs/DATA_HYGIENE.md](docs/DATA_HYGIENE.md) | 数据卫生纪律 |
| [docs/INTELLIGENCE-ACQUISITION.md](docs/INTELLIGENCE-ACQUISITION.md) | 受控来源采集、逐目标证据、冻结包、同步/监测及离线评测 |
| [docs/PUBLIC_VERIFICATION.md](docs/PUBLIC_VERIFICATION.md) | 模拟盘公开验证章程 |

## 质量与验证（本机实测 · 2026-09-19）

| 层 | 命令 | 结果 |
|---|---|---|
| 交易内核（Python） | `python3 -m pytest tests/ -q` | ✅ **327 passed** |
| 治理外壳（WorkLoom IM） | `cd governance && SUITE_SERVER_PORT=8899 pnpm suite` | ⚠️ **354/459 通过（105 失败）** |

治理外壳套件的失败高度集中在外键约束（`threads/night_runs/triggers_workspace_id_fkey`）与「基线规则装载 0 条」两类，根因是 `governance/scripts/suite.ts` 的 scope 仍硬编码基座酒店演示工作区 `ws-yunqi`，而本仓种子建的是交易工作区 `trading`（`governance/bundles/` 下也不再随仓携带 hotel 包）。**本次未修改测试逻辑**，如实登记为待修；修复方向是把 suite scope 参数化到 `product.manifest.json` 的演示工作区。

## 合规声明

本系统为技术研究与模拟验证项目：**仅模拟盘，不做真实下单**；不构成投资建议；免费行情源存在延时（已在报告中标注）。实盘交易需另行接入持牌券商并满足各市场监管要求（美股 SEC 15c3-5、A股程序化交易报备、港股 SFC）。

## 许可证

Apache-2.0。

## 桌面 Agent 接入（Codex / Qoder / DeepSeek Harness）

完整后端研究使用 `node scripts/tiger-agent.mjs`（固定八操作 CLI）或
`node scripts/tiger-agent-mcp.mjs`（独立 STDIO MCP）。通过
`node scripts/tiger-agent-config.mjs --client codex` 生成 Codex 本机项目配置，默认只读；
Qoder CLI 与 DSH 分别选择 `--client qoder` 和 `--client dsh`。
给外层 Agent 的流程见 [TIGER-OPERATOR.md](docs/TIGER-OPERATOR.md)，
客户端接入与真实模型条件见 [AGENT-CLIENTS.md](docs/AGENT-CLIENTS.md)。

原 `node scripts/workloom-agent.mjs list` 和 `node scripts/workloom-agent-mcp.mjs`
保留通用能力目录；目录中的仓库只读能力不会因此获得完整研究执行权限。

`list` 证明仓内接口声明可发现；可调用性与结果验证须按实际环境、提交和运行回执分别取证。治理壳的自动导览见 `governance/docs/capabilities.auto.md`，缺少独立运行证据的条目保持“未验证”。模拟运行结果不得标成真实交易或客户生产通过。
