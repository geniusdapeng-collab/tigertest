# TigerTest 双主题情报选股架构

版本：`tiger.tech-focus.v1`

基线：`geniusdapeng-collab/workroom-tiger@d833064`

运行边界：美股公开数据、USD 本地 paper 账户、最终交易由人执行。

## 1. 要解决的不是一个评分问题

原系统已经具备完整的 MRS、SHS、ICS、TSS、L4 风控和证据回执，但选股链路仍有两个结构性矛盾：

1. **研究与扫描串联过宽。** `intelligence.frontier` 先冻结数值候选，随后
   `Acquisition` 同时研究六条科技子链、多个板块和每个 frontier 证券。候选越多，
   必答题越多；请求、原文、字节、重试和模型阶段一起膨胀。最后即使部分研究
   完成，也常因存在 required gap 而只能等待。
2. **“没有通过”没有变成可操作信息。** L4 返回空 picks 时，报告能说明 WAIT，
   但用户很难一眼看到：哪只票只差哪个条件、哪条主题证据缺失、下一轮在什么
   价格/广度/分数条件下应重新评估。

因此 TigerTest 不通过降低 MRS/SHS/ICS/TSS 阈值来“制造交易”。它改变候选空间、
研究预算和决策交付方式。

## 2. 产品需求与硬约束

### 功能需求

- 聚焦一个域：美国上市、高流动性科技股。
- 聚焦两个可执行母主题：
  - `semiconductor_cycle`：半导体周期，映射 `semis`；
  - `ai_infrastructure`：AI 基础设施，映射 `ai_compute`。
- 继续深挖六条科技子链：memory、logic、foundry、equipment、ai_model、ai_app。
- 每个证券都必须有财报/指引、重大不利事项、反证三类研究问题。
- 空仓日必须输出条件观察名单、研究缺口和下一轮开门条件。
- 所有价格、止损、股数、风险预算和闸门仍为确定性规则。

### 非功能需求

- 每日行情范围从原扩展池的数百只降到约 80 只。
- 深研 frontier 最多 16 只，单个主主题至少 4 个 frontier 名额。
- 研究问题先主题后个股；每个问题都有自己的 target、material type 和时限。
- 任何来源失败、证据缺失或冲突都必须保留为 gap；不能用“没有搜到坏消息”
  推断没有风险。
- 同一输入、同一冻结合同和同一行情快照必须产生同一 frontier、计划哈希和报告。
- 仅运行模拟盘；不连接券商、不执行真实订单。

## 3. 方案比较

### 方案 A：保留 500 只宽扫，只增加并发

优点：代码改动最少，仍能发现宽池中的长尾。

缺点：行情、source ledger、原文快照和逐证券研究都会继续膨胀；并发只会把
资源争用推高，无法改善每个目标的研究深度。请求失败时仍容易产生“有数字、
无证据”或“有请求、无完成”的半成品。

结论：不选。

### 方案 B：两主题、两阶段、证据驱动（选中）

第一阶段只处理两个母主题约 79 只高流动性美股。行情硬过滤后按原有五因子
排序，再用主题配额冻结最多 16 只 frontier。第二阶段只对这 16 只和六条子链
做深研究；公司证据、反证和宏观问题是逐目标的。L4 之后输出完整决策面。

优点：

- 数据范围约降低 80%，研究请求明显收敛；
- 主题研究从“顺手搜新闻”变成有目标的 coverage cells；
- 数值与语义边界不改，白皮书闸门全部保留；
- frontier、计划、证据包、报告和回执仍可离线重放。

缺点：

- 放弃了宽池长尾；这是有意的搜索偏差；
- 两主题之间的资金轮动仍需通过 XLK/SMH 与母链状态判断；
- 主题包需要长期维护，不能把映射当成永久事实。

结论：选中。它直接优化“数据量”和“研究深度”两个约束，同时不改变风险纪律。

### 方案 C：拆成独立情报微服务，再由选股服务消费

优点：数据与交易进程物理隔离，横向扩展方便。

缺点：当前项目是本机 paper 工作流，额外服务会引入网络边界、部署、认证、
版本协调和恢复成本；在证据量尚未稳定前，过早拆分不能解决选股空转。

结论：暂不选。保留为后续数据规模真正超过单机预算后的演进方向。

## 4. 目标架构

```text
两个母主题
  semiconductor_cycle ─┐
  ai_infrastructure ───┤
                       v
高流动性美股科技池（约 79 只）
                       |
              价格/ADV/历史/ATR 硬过滤
                       |
              原五因子市场初排
                       |
     每主题至少 4 个名额，总 frontier ≤ 16
                       |
        ┌──────────────┴──────────────┐
        v                             v
六条子链情报                    16 只证券深研
memory / logic / foundry        财报与指引
equipment / ai_model / ai_app   重大不利事项
        |                       反证/更正
        └──────────────┬──────────────┘
                       v
           冻结证据包 + 逐目标 coverage
                       |
         MRS -> SHS -> ICS -> TSS -> L4
                       |
             语义终审只能否决或缩小
                       |
        ACTIONABLE / CONDITIONAL / EVIDENCE_GAP
```

### 4.1 领域包

`trading_system/tech_focus.py` 是聚焦策略的单一事实源：

- 统一股票池：从 `CHAINS["semis"]` 与 `CHAINS["ai_compute"]` 派生；
- 流动性与历史门槛：价格 ≥ 10 USD、ADV20 ≥ 100M USD、历史 ≥ 260 日、
  ATR% ≤ 12%；
- frontier：最多 16 只，每个母主题至少 4 只；
- 最大纸面 picks：5；
- 板块证据只取 XLK/SMH；
- 所有参数通过 `FOCUS_POLICY.to_dict()` 冻结进 policy 和 frontier。

这些门槛高于白皮书的 `$5/$20M` 最低线，但属于“输入池质量选择”，不是修改
历史 profile 或风控阈值。

### 4.2 情报问题图

`tech_focus.research_cells()` 为每个冻结 frontier 生成有限问题：

- 1 个全球宏观；
- 六条子链各至少 1 个景气/风险问题；
- XLK、SMH 各 1 个板块问题；
- 每只证券 3 个问题：财报/指引、重大不利事项、反证/更正。

每道问题携带 `target_id`、`material_type`、`window_days`、`required` 和
稳定 `cell_id`。这样“研究主题”不是一段泛化 prompt，而是可计算覆盖率的任务。

### 4.3 预算与完成

焦点 profile 使用既有 source ledger、原文哈希、CAS 冻结和模型五阶段合同。
它不增加任意 URL、凭据、模型或阶段权限。预算控制点：

1. 股票池先缩到两主链；
2. frontier 再缩到 16；
3. 问题按 target 分配，而不是把全池文章一起塞给模型；
4. 深度问题优先于宽泛新闻；
5. 缺口保留为 partial，不伪造“完成”。

单轮不完成时，报告必须显示具体缺口；用户可对已验证的 pending security 发起
supplement，而不是新开一个更大的全市场搜索。

### 4.4 决策面

`tech_focus.build_decision_surface()` 在 L4 和模型终审之后生成：

- `ACTIONABLE`：通过数值闸门且模型未否决的纸面计划；
- `CONDITIONAL`：有 A/B/C 模板，但被市场、板块、预算或模型条件阻断；
- `SETUP_PENDING`：主题和质量尚可，但还没有可执行入场模板；
- `PRELIMINARY`：本轮连候选都没有时，仍给出通过市场排名的预备对象；
- `research_gaps`：未完成或冲突的目标级证据；
- `next_open_conditions`：下一轮可观察的开门条件。

条件观察不是买入。它把“空结果”变成可验证的下一轮研究队列。

## 5. 代码接口

### 新 profile

```json
{
  "schema_version": "tiger.agent-request/v1",
  "profile": "us-tech-intelligence-paper-v3",
  "paper_account": "public-usd",
  "idempotency_key": "operator-tech-focus-20261113-001"
}
```

### 普通 CLI

```bash
python3 main.py --universe tech --top 16 --picks 5
python3 main.py --demo --universe tech --top 16 --picks 5
```

### 核心接口

- `trading_system.tech_focus.TECH_FOCUS_UNIVERSE`
- `trading_system.tech_focus.select_frontier_rows`
- `trading_system.tech_focus.research_cells`
- `trading_system.tech_focus.build_theme_dossier`
- `trading_system.tech_focus.build_decision_surface`
- `trading_system.intelligence_workflow.build_frontier(..., focus_profile=...)`
- `trading_system.llm.intelligence_required.Acquisition(..., required_cells=...)`

## 6. 异常与退化路径

| 异常 | 行为 |
|---|---|
| SPY/TNX/VIX 缺失或陈旧 | 按白皮书诚实失败，不生成带污点交易日 |
| XLK/SMH 任一缺失 | 美股份聚焦 profile 失败关闭 |
| 行情硬过滤后无候选 | `PRELIMINARY` + 开门条件，不伪造 A/B/C |
| 有候选但模板缺失 | `SETUP_PENDING`，保留主题、价格、关键位和等待条件 |
| 主题证据不足 | `research_gaps` 明确 target/question；不把 null 改成中性分 |
| 模型否决数值批准 | picks 收缩，决策面标记模型否决和重开条件 |
| 来源/模型请求中断 | 原 run identity 恢复或补研；不得换 key 静默重跑 |
| 报告正常但账户记账失败 | 只交付研究，不宣称纸面账户已更新 |

## 7. 可观测性与验收指标

每个焦点任务至少披露：

- `universe_size` 与 `data_symbols`；
- frontier 总数、主题分布、拒绝原因；
- 研究 cell 数、请求/Fetch/字节/轮次使用量；
- `research_gaps` 数和每个 gap 的原因；
- MRS/SHS/ICS/TSS 分布；
- `ACTIONABLE`、`CONDITIONAL`、`SETUP_PENDING`、`PRELIMINARY` 数量；
- 运行耗时与各阶段耗时；
- 最终 receipt/verify 状态。

建议运营门槛：

- P95 业务耗时低于原宽池路径的 60%；
- 16 只 frontier 的逐目标必需研究覆盖率达到 100% 才可称完整；
- 不允许“有 picks、无公司证据”；
- 连续 20 个交易日记录 `CONDITIONAL` 命中率与模型否决原因，再决定是否扩展
  第三个主题。

## 8. 本方案明确不解决什么

- 不承诺收益，不把模拟成交当真实业绩。
- 不自动扩展第三个主题或全市场；扩展必须另做冻结版本。
- 不预测财报，不用新闻热度替代结构买点。
- 不提供真实券商接入、自动下单或资金迁移。
- 不把盘前未完成的研究强行标成 complete。
- 不解决美股实时 L1/L2、期权全链付费数据和盘口微结构；当前仍是日线级 paper
  研究。

## 9. 迁移与回退

- 历史 `us-public-intelligence-paper-v2` 保持原语义；新任务选择
  `us-tech-intelligence-paper-v3`。
- 旧 `--universe focused/extended/full` 仍可运行；回退时显式使用旧模式。
- 新 profile 的账户仍使用独立 state-root；不能从旧账户复制资产冒充迁移。
- 主题包、权重或映射变化必须升 `tiger.tech-focus` 版本，并重跑 frontier、
  acquisition、模型阶段、回放和报告测试。

## 10. 验证面

- `tests/test_tech_focus.py`：股票池、过滤器、主题配额、问题图、frontier、
  决策面和 profile。
- `tests/test_intelligence_workflow_v2.py`：前沿、封存和篡改拒绝。
- `tests/test_agent_runtime_policy.py`：旧/新 profile 的冻结合同。
- `scripts/tiger-agent*.test.mjs`：CLI/MCP schema、默认 profile、环境隔离。
- `main.py --demo --universe tech`：端到端空/非空决策面与报告渲染。
