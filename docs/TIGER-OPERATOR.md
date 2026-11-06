# 在 AI 对话框中操作 Tiger 后端

本指南供 Codex、Qoder 等平台中的外层 Agent 执行。用户说明目标，Agent 调用本仓 CLI 或 MCP，查询同一运行并解释已验证结果。完整后端入口不需要启动官网或治理壳页面。

## 开始前的固定约定

- 先读根 `AGENTS.md`、`AGENTS.repo.md` 和本指南；启动配置、权限和字段详见 [客户端接入指南](AGENT-CLIENTS.md)。
- MCP 使用 `scripts/tiger-agent-mcp.mjs`；具备本地执行能力时，可用同一后端的 `scripts/tiger-agent.mjs` 作 CLI 入口。原 `workloom-agent` 只读能力目录不能代替完整工作流。
- 解释器、访问模式和 state-root 来自操作者的可信部署配置。CLI 备用入口必须保持同一解释器、模式和 state-root，不能借切换入口升级权限。默认 readonly；只有已明确选择的 paper 部署可提交模拟研究。
- 对同一 profile、同一操作者的 paper 账户保持稳定 state-root，让持仓、待成交和净值连续。新逻辑任务只使用新的幂等键；切换 profile 或明确另建隔离模拟账户时才使用独立 root，并披露新账户的初始资金。不得复制或迁移旧账户冒充兼容。
- 新 AI 研究显式选择 `us-public-intelligence-paper-v2` 与 `public-usd`。旧 profile 用于其原有或历史语义，不用省略 profile、演示数据或透传结果代替新研究。
- 外层平台模型不等于内层研究模型。真实模型配置与 key 必须由可信启动环境提供，不能出现在聊天、请求 JSON、源码或配置示例中。生成配置不验证凭据或付费调用是否可用。

### 可选的完整研究运行时限

公开情报 paper 研究默认仍为 2400 秒。操作者可在可信启动环境设置
`TIGER_AGENT_RUNTIME_POLICY_FILE`，指向仅含下面两项的本地 JSON；它只适用于
`us-public-intelligence-paper-v2`，秒数须为 2400–7200 的整数。
[5400 秒示例](../config/agent-clients/paper-runtime-5400.json.example)用于完整五阶段研究的实测，
不是新的默认值。

```json
{"schema":"tiger.agent-runtime-policy/v1","business_deadline_seconds":5400}
```

启动 CLI/MCP 前由已授权的宿主导出该变量；业务请求仍保持原四字段，不能指定时限或配置路径。
doctor 的 `business_runtime` 和 v2 describe 的 policy 会显示实际秒数与内容哈希。
接单时先固定可信时钟，再读取和冻结配置；锁等待、来源准备、归档和 worker 启动都计入同一业务截止。
每次新运行保存 `runtime-input.json`，policy、admission 和最终 receipt 绑定同一哈希。
配置文件之后被修改、移走或未提供给只读查询，不会改变原运行的截止与验真。
未配置该文件的旧运行继续按原 2400 秒合同验证。

该配置不改变模型、五阶段顺序、完整原文、合法输出窗口或风险上限。
客户端 `--wait` 的等待窗口独立；观察超时后继续查询原 `run_id`，不要换键重跑。

## 场景一：只检查能否使用

用户可以说：“检查 Tiger 后端是否就绪，告诉我目前能做什么，先不要启动研究。”

Agent 依次调用：

1. `tiger_doctor`，参数 `{}`。
2. `tiger_workflow_describe`，参数 `{"profile":"us-public-intelligence-paper-v2"}`。

没有可用 MCP 时，由 Agent 在已选择的仓库根目录执行下面的等价 CLI。实际路径由已核实的部署配置取得，用户无需手工拼装命令：

```bash
node scripts/tiger-agent.mjs doctor --python /absolute/path/to/python \
  --state-root /absolute/path/to/existing-v2-state
printf '%s\n' '{"profile":"us-public-intelligence-paper-v2"}' | \
  node scripts/tiger-agent.mjs describe --python /absolute/path/to/python \
    --state-root /absolute/path/to/existing-v2-state --input-file -
```

这两步不拉取行情、不调用研究模型、不创建 paper 账户或业务 worker。报告实际解释器、doctor 状态、describe 返回的阶段与限制；文件布局检查、JSON 合法或工具出现在菜单中都不是业务验收。

## 场景二：完成一次模拟研究

用户可以说：“按已经配置的 paper 账户做今天的完整研究，完成后给我依据、风险和账户变化。”

Agent 先执行上面的检查，再确认使用既有可信 paper 部署及该 profile 的稳定 state-root。只提交一次四字段请求：

```json
{
  "schema_version": "tiger.agent-request/v1",
  "profile": "us-public-intelligence-paper-v2",
  "paper_account": "public-usd",
  "idempotency_key": "operator-task-20261105-001"
}
```

这里的键是示例；Agent 为新逻辑任务保存一个稳定键，恢复时原样复用。请求不能增加 mode、python、state-root、provider、密钥、skip、score、approve 或 live 字段。

调用 `tiger_workflow_run` 后，保存实际返回的 `run_id` 和原请求。`accepted`、`running` 只表示接纳或运行中。随后用 `{"run_id":"实际返回的run_id"}` 调用 status，达到终态后调用 receipt 和 verify；不要把占位字符串作为真实身份提交。

CLI 的等价流程是 Agent 写入不含秘密的请求文件，再使用同一部署参数执行：

```bash
node scripts/tiger-agent.mjs run --python /absolute/path/to/python \
  --state-root /absolute/path/to/existing-v2-state --mode paper \
  --input-file request.json
node scripts/tiger-agent.mjs status --python /absolute/path/to/python \
  --state-root /absolute/path/to/existing-v2-state --input-file run-id.json
node scripts/tiger-agent.mjs receipt --python /absolute/path/to/python \
  --state-root /absolute/path/to/existing-v2-state --input-file run-id.json
node scripts/tiger-agent.mjs verify --python /absolute/path/to/python \
  --state-root /absolute/path/to/existing-v2-state --input-file run-id.json
```

`run-id.json` 只含实际 `run_id`。查询可用 readonly，但 root 必须相同。CLI `run --wait` 只等待已有身份，不重复提交；MCP 单次调用提交后由 Agent 持续查询。

新 v2 任务只有同时满足以下条件，才报告“研究完成”：

- receipt 和 verify 的 `operation_ok=true`、`status=completed`、`verification.ok=true`。
- 两者绑定相同 `run_id` 和非空、相同的 `verification.receipt_sha256`；receipt 的 `summary` 非空。verify 不提供业务摘要。
- 按已验证 summary 如实报告数据时点、模型与来源缺口、关注/等待理由、入场和失效条件、风险，以及 paper 持仓、待成交、现金和权益。不得从未验证文件或宿主自行推测补出结果。

五个语义阶段要求真实、完整的 admitted DeepSeek 回复；缺 key、无效模型回复、证据不足导致的失败不能改称成功。无合格买入也可有真实完成的 WAIT/AVOID 结果。纸面信号和模拟成交不是券商订单，也不是实际收益。

## 场景三：断线、超时或失败后继续对话

用户可以说：“继续刚才那次任务，先找回原身份，不要重新跑。”

| 已知事实 | Agent 的动作 |
|---|---|
| 已知 run_id | 在同一 state-root 查询原 status，再取 receipt/verify |
| 提交响应丢失，尚无 run_id | 用完全相同的四字段请求和原幂等键恢复身份；不自动换键 |
| 客户端取消、断开或等待超时 | 说明停止的是观察；accepted worker 可能继续执行，先回读身份与状态 |
| 原任务 failed/blocked/interrupted | 保留原失败和证据；不自动换键重跑或补写阶段 |
| READ_ONLY 或 MODEL_REQUIRED_FAILED | 报告实际部署/模型条件缺口；请求 JSON 不能提权，不能改用演示或规则语义代答 |
| ACCOUNT_BUSY | 保留原请求；该新键未被消费时，稍后可按同一请求重试 |

调查并解决失败原因后，只有明确发起新的逻辑尝试才分配新键；同一 profile 的连续 paper 账户仍使用原 root。没有原 root 或原请求的必要信息时，说明缺什么，不猜测或新建账户掩盖历史。

## 平台接入与证据等级

Codex 项目配置由 `--client codex` 生成；Qoder CLI 项目本地配置由 `--client qoder` 生成。独立 Qoder 桌面通过 Connectors 的自定义 MCP 界面导入；项目本地文件不是已核实的桌面自动发现路径。设置方法见 [客户端指南](AGENT-CLIENTS.md)；可导入的 Agent 指令见 [操作员模板](agent-clients/tiger-operator.prompt.md)。

交付时分别说明：**配置文件生成/解析**、**原始 STDIO 协议调用**、**实际平台宿主工具发现或调用**。三者不能相互代替。原生工具发现不表示当前聊天已加载，更不证明付费研究已完成；Qoder 宿主是否加载须以所用宿主现场回执证明。

MCP 不可用但 Agent 有本地执行工具时，可按上述同权限 CLI 流程继续。两种执行工具均不可用时，明确报告尚未接通执行入口，不能只生成一份研究文案冒充后端结果。

## 场景四：主题研究与持续情报

当用户要研究产业、比较机构观点、确认人物原话或跟踪来源变化，而没有要求更新 paper 账户时，使用独立 [主题研究入口](INTELLIGENCE-TOPICS.md)：`scripts/tiger-topic.mjs` 或 `scripts/tiger-topic-mcp.mjs`。这类任务不按上文的 paper 四字段请求提交。

外层 Agent 先调用 health，按可信来源目录把问题、主体、期限和资料维度固定为 topic，再在已有 research 部署下提交 research。保存 topic_id/version、幂等键和 job_id；轮询 status 后读 report 独立验真，只从已核验的 readable_report 整理结论。partial 必须同时说明必需缺口，不能称全面研究已经完成。watch/tick 只采集、检测变化并标记待复核，不自动启动付费分析。

主题命名空间的模式为 readonly/acquire/research，paper 入口仍为 readonly/paper，二者不可混用。默认 readonly 不升权；source IDs 和主体必须审定，不接受任意 URL、key 或 provider。断线后保留原请求恢复同一 job；明确的阶段和模型响应检查点只在同 job 内复用，未知付费投递则先对账，不盲目重发。来源、权限或必需原文不可用时按 report 列出缺口，不用宿主文案替代实际后端研究。
