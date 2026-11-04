# 收盘后潜力股复核

已封存的美股任务可以另外生成一份潜力股复核。原任务仍由受控 Tiger 入口执行并完成独立验真。复核只读取已有运行，可以在原交易动作是 WAIT 时列出值得研究的个股。

从仓库根目录使用 Tiger 的 Python 环境运行。状态目录使用绝对路径。

    python -m trading_system.opportunity_review \
      --state-root /path/to/tiger-state \
      --run-id run-... \
      --issuer-review /path/to/issuer-review.json > opportunity-review.md

省略公司复核输入时，结果是待取证清单。工具从已精评的候选中先保留评分最高的 A/B/C 形态，再按 TSS 补充距关键位不超过一个 ATR、过去 20 个交易日相对 SPY 为正的非形态股，剩余位置由其他形态股递补。该顺序用于决定先查谁，完成公司研究后以复核者给出的排序为准。

公司输入采用 tiger.issuer-review.v1，字段包括 market_date、run_id、receipt_sha256 和 candidates。candidates 按复核顺位提供 1 到 6 项，每项包含 symbol、thesis、confirmation、invalidation、risk 和 sources。候选可以来自精评或扩围量价线索。每个来源提供 title、HTTPS url 和 published_on。收盘同日发布的材料须另填带时区的 published_at，不能晚于封存行情的收盘时刻。对来源归属、发布时间和公司事实的核对由复核者完成。程序校验输入与封存运行的绑定和时间边界，并将输入哈希记入报告。

程序先运行独立验真，再读取封存结果与原始扫描行情。五日和二十日涨跌以共同交易日收盘计算，相对量比较当日与前 20 日均量。无法匹配的数据会显示缺口，缺少行情的标的不能通过人工输入加入报告。复核输出写到标准输出，原始报告、纸面账户和模型调用记录保持只读。

对通过基础筛选但未进入精评的股票，报告补列一份量价线索。条件是近 20 日跑赢 SPY 且当日相对量至少 1.5 倍，按相对收益排序。线索需另行核对入场形态与公司资料。
