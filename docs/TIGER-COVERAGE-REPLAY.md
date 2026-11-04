# Tiger 候选覆盖离线对照

用封存且可独立验真的严格纸面案例，对照原候选上限和入场形态补查。每个指定窗口独立建立两份内存模拟账。两侧使用相同日期切片、同一股票池、相同形态、阈值、组合风控、次日开盘和摩擦成本。盘中可能成交的出场成本采用上一交易日已知的 ADV。差异为候选是否补查。

```bash
python -m trading_system.coverage_replay \
  --state-root /absolute/path/to/sealed-paper-state \
  --run-id run-0123456789abcdef0123456789abcdef \
  --period 2026-06-01:2026-06-26 \
  --period 2026-09-14:2026-10-09 > coverage-comparison.json
```

输入为已经完成并成功验真的运行，例中的运行 ID 应换成实际回执。每次可选 2 至 6 个互不重叠的区间，每个区间须含 5 至 65 个已观察到的 SPY 日线交易日，截止日早于原运行的纽约入场日期。比较程序只读封存数据。无法验真、日期越界或样本不足时退出 2。该入口独立于 Tiger 的 CLI/MCP 业务启动契约。

每个信号日对基准、板块和股票切片至当日收盘，缺同日股票或板块行情单独反映在覆盖计数里；日期滞后的宏观观察只允许使用截止日前有限间隔的历史数据。每个窗口分别输出通过初筛数、精评数、数值批准名单，以及两份模拟账的期末净值、已实现净收益、成本、回撤、成交周转、未平仓和待成交。历史期权、模型叙事、公司新闻或模型终审缺失时不补造；两侧同样按缺失处理，比较的是候选与数值风控层。股票池来自封存运行，可能有幸存者偏差，行情可能含后续供应商修订。需要独立的时点上市清单和可追溯行情才能外推策略表现。

## 点时名册与逐股复验

默认入口沿用旧档案内的固定池，输出 `roster.basis=frozen_current_pool`。它可以对比算法，结果仍受幸存者偏差影响。生产严格纸面回执在 `pipeline.prepare`、`data.prepare` 与最终结果记录当时审定池的同一哈希和 `ticker → us:<ticker>` 研究标识，注明 `reviewed_pool_observed_now`。这种研究标识在本次准入范围内可核对，历史更名或代码复用需要外部证券身份。

点时对照使用受信操作者提供的归一化名册，额外传入 `--roster roster.json --roster-sha256 <完整小写SHA-256>`。名册根包含 `schema: tiger.security-roster.v1`、`scope: us-reviewed-paper`、`sources`、`sessions` 和 `events`。

- `sources` 按来源引用编号，每项包含无凭据 HTTPS `url`、原始字节 `sha256` 和可知的 `published_at`。原件以 `<sha256>.bin` 放在名册旁边。原件和名册都会重算哈希，归一化字段由受信数据提供者按原件抽取。
- `sessions` 按美国交易日列出日内覆盖证明。每项含 `known_at` 与 `source_ref`。缺日期、证明晚于当日收盘或文件损坏时停止。日期从交易日历计算，即使 SPY 某日缺失也保留在分母。
- `events` 按稳定证券 ID 记录 `security_id`、`effective_date`、`known_at`、`source_ref` 与 `kind`。上市 `listing` 和更名 `rename` 还需 `symbol`，拆分 `split` 需正且非 1 的 `ratio`。停牌 `halt`、恢复 `resume` 和退市 `delist` 只需通用字段。所有时间戳带时区。更正同日同类型事件使用更新的真实可知时间。代码复用要用不同证券 ID。
- 回放截断至每只证券当前代码和价格时期的起始日。更名、拆分、退市或停牌后，失去可比行情的既有仓位保留最后可核验估值并披露悬而未决的标的。缺乏可靠原始价、换股或现金对价时，回放不会制造平仓。数据包不会自动扩大既有研究池数量，也不会调整风险阈值。采用按日市场状态，同日生命周期变化视为观察或执行证据不足。

NASDAQ 当前交易日的 Symbol Directory 适用于当日核对。NASDAQ Daily List 描述了可知时间、生效日期和历史生命周期，含 CUSIP 的服务受许可约束。完整历史验收需要经授权的跨市场日档、公告及价格版本。 [NASDAQ Symbol Directory](https://nasdaqtrader.com/trader.aspx?id=symbollookup) · [NASDAQ Daily List 规范](https://www.nasdaqtrader.com/content/technicalSupport/specifications/dataproducts/dlcompletespec.pdf) · [NYSE 停牌历史](https://www.nyse.com/trade/trading-halts)

加 `--verify-oracle` 会逐日对同一数据跑全量硬筛后 TSS，逐只比对形态、TSS 和遗漏。两组以每日相同空仓账户核对风控批准明细、顺序、预算和候选拒绝信息，与延续持仓的两组账分开计算。少于 30 个可评分的互异交易日或出现差异时，程序保留明细并退出 1。带名册与固定池的结果分别看，覆盖率只对其各自分母有效。

输出分趋势、震荡、弱势和基准缺失分别给出应有与可见股票日。两本纸面账按成交日期统计换手，按平仓日分组报告净收益和每笔期望，并列出从窗口峰值计算的回撤、最大单仓占净值比例、该状态最后一日的持仓与待成交。无已结算样本时，期望为 `null`。回放是数值验证，真实研究的模型、来源和账户链证据继续由受控 CLI 独立验真。
