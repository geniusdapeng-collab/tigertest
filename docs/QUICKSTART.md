# 老虎全球资产管理 · 快速上手（QUICKSTART）

> 两条使用路径：**A. 交易内核单用**（Python，开箱即用）｜**B. 全栈**（内核 + WorkLoom 治理底座，适合团队/机构）。
>
> 下方 `main.py` 命令用于内核开发、离线演示和历史兼容。新的 AI 研究、官网更新、原身份 resume、候选 supplement、独立 receipt/verify 必须使用 [Agent 操作指南](AGENT-CLIENTS.md) 的受控 CLI/MCP 与 `us-public-intelligence-paper-v2`；`--demo`、透传和本页旧命令都不能作为新研究完成证明。

## A. 交易内核单用（5 分钟）

```bash
git clone https://github.com/geniusdapeng-collab/workroom-tiger.git
cd workroom-tiger
bash scripts/setup.sh        # 装依赖 + 冒烟测试 + 环境自检
```

三条命令验证：

```bash
python3 main.py --demo                                    # 离线演示（合成数据，确定性）
bash scripts/doctor.sh                                    # 你的网络下哪些数据源可用
python3 main.py --mode daily --universe extended \
       --top 30 --picks 8 --html                          # 真实数据全链路（约 30 分钟）
```

产物在 `reports/`（或 `--out` 指定目录）：双页签日报 HTML、journal 台账、小虎模拟盘台账、复盘纪要、治理事件流。

### 数据源说明（无需任何付费 API）

| 数据 | 来源（降级链顺序） |
|---|---|
| 美股行情 | yahoo → stooq → tencent → sina → eastmoney → agentgw → ifind → tiingo |
| 利率/波动率 | yahoo → stooq → **FRED（美联储官方）/ CBOE（官方）** → ... |
| 新闻情报 | agent-gw 全网 / Google News / Reddit / **东财资讯 / 新浪快讯** / SEC EDGAR / PatentsView / Federal Register |
| A股/港股 | tencent / sina / eastmoney（内置前复权） |

系统对你的网络环境自适应：`doctor.sh` 会给出源可达性画像；不可达源自动降级/熔断，**拿不到真数据的环节诚实透传，绝不编造**。

### LLM 配置（双模驱动，点亮全部能力）

无 LLM 也能跑（语义环节透传），但板块叙事、多空辩论、舆情/风险推断会缺失。支持两种方式：

**方式 a：自有 API（任何 OpenAI 兼容端点）**

```bash
export LLM_BACKEND=api
export LLM_BASE_URL=https://api.deepseek.com       # OpenAI 兼容根
export LLM_API_KEY=sk-...
export LLM_MODEL=deepseek-flash                    # 官方端点会强制归一为该模型
# 兼容客户端默认非思考 JSON；Kimi 旧通道仅在显式 LLM_BACKEND=kimi 时启用
```

**方式 b：本地 AI Coding Agent 主力模型（开发者本地零配置）**

开发者在本地用 AI Coding Agent 运行时，系统自动探测本地模型端点：

```bash
export LLM_BACKEND=local      # 或 auto（默认，自动探测）
# 探测顺序：LLM_LOCAL_URL（显式指定）→ Ollama localhost:11434 → LM Studio localhost:1234 → OPENAI_BASE_URL
# 例：显式指定：export LLM_LOCAL_URL=http://localhost:11434/v1 LLM_LOCAL_MODEL=qwen3:32b
```

默认 `LLM_BACKEND=auto`：DeepSeek API → local 依次探测；Kimi 不再作为隐式默认。全部不可用时按红线透传（报告如实标注），**系统绝不用规则伪造语义分**。

### 每日自动运行（cron）

```bash
bash scripts/install_cron.sh        # 周一至周五 06:00（北京时间）跑美股日报
# 官网发布环：daily 运行后执行
bash scripts/publish_site.sh
```

### 三市运行

```bash
python3 main.py --market us --mode daily --html     # 美股（默认）
python3 main.py --market cn --mode daily --html     # A股原币行情与研究（不做交易记账）
python3 main.py --market hk --mode daily --html     # 港股原币行情与研究（不做交易记账）
```

`--account` 始终表示 **USD 账户净值**。当前没有可核验的汇率换算契约，CNY/HKD
报价不能直接除美元风险预算或扣减美元现金。因此 A 股、港股仍生成行情、评分、观察名单与
JSON/Markdown/HTML 研究报告，但不输出可执行股数，也不更新信号日记、模拟盘、校准样本、
复盘或治理事件。报告中的 `currency_policy.execution_blocked` 和
`accounting.status=blocked_currency` 明确披露原因；研究运行正常完成。

模拟盘与信号日记的公开入口也拒绝显式非 USD 元数据，以及已知 `.SS`/`.SZ`/`.HK`
原币标的。发现旧账本存在这些记录时，原文件保持不变并停止记账，不自动重标币种或按猜测汇率
迁移。未标币种的旧美股账本按原有 USD 接口契约兼容；新增账本/记录明确记录 USD 单位。
轻仓毕业门槛与交易策略参数保持原有定义，达到门槛也不会绕过币种边界。

## B. 全栈（内核 + WorkLoom 治理底座）✅ 已落地

一条命令起全栈（需要 Node ≥24、pnpm 10、Docker）：

```bash
bash scripts/stack_setup.sh
```

脚本完成：依赖安装 → PostgreSQL 17 容器 → 迁移 → 演示种子 + **trading bundle 种子**（37 个岗位 preset、19 条三层围栏规则、10 个官方技能、7 个三市/夜班/WFA 触发器）→ 内核事件入库 → 启动指引。

> 数量口径以可执行产物为准，不在文档里手工维护第二份：preset/围栏/技能来自
> `governance/bundles/trading/{presets,fences,skills}` 与 `bundle.json#provides`，
> 触发器来自 `governance/scripts/seed-trading.ts`；三者任一变化时以该处实际数量为准。

落地核验（2026-08-30 实测）：

| 项 | 结果 |
|---|---|
| 底座测试套件 | 全绿（Node 24 + pnpm 10） |
| 内核事件入库 | `governance/scripts/ingest-tiger-events.ts`：30 条五元事件幂等入库（重复执行全量去重） |
| 哈希链 | `verify-chain.ts`：130 条事件逐条重算一致（双链：内核 JSONL 链 + 底座 DB 链，事件内 `decision.kernel_hash` 互验） |
| trading bundle | 33 agents / 18 fence_rules / 6 skills / 7 triggers 落库可查 |
| 服务 | server :8787（/health ✓）+ web :5173 |

日常衔接：内核每次运行后执行 `cd governance && pnpm tsx --env-file=.env scripts/ingest-tiger-events.ts` 增量入库（幂等，可入 cron）。

> Quest 夜班编排已落地（2026-08-30）：`governance/scripts/quest-trading-nightly.ts` 每晚 22:00 由 cron 触发，走围栏瀑布自治执行「自检→内核全链路→事件入库→官网发布」，逐步回执实证、断点幂等续跑。剩余边界：Quest 驱动**实盘券商下单**（实盘阶段，审批流阻塞式启用）。

## 合规声明

本系统仅运行于模拟盘，不构成投资建议，不承诺收益。实盘交易需另行接入持牌券商并满足各市场监管要求。
