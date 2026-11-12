# WorkLoom 开源组件清单 · workroom-tiger

<!-- 自动生成，请勿手改：node scripts/oss-inventory.mjs --write -->

> 生成器：`scripts/oss-inventory.mjs`（离线事实）＋ `scripts/oss-watch.sh`（上游最新版本）
> 仓库：workloom-ai/workroom-tiger ｜ 最近一次成功扫描：尚未记录已核实成功（运行 `pnpm oss:watch`） ｜ 最近尝试：未记录
> 每项上游版本按成功时间、查询状态和 TTL 单独判定；历史缓存与失败查询不作本次最新版本结论。
> 统计：登记组件 44 个 ｜ npm 直接依赖 156 个 ｜ Python 依赖 5 个 ｜ 容器镜像 1 个

## 0. 维护机制（四件事）

| 时机 | 动作 | 命令 |
|---|---|---|
| 依赖变更（改 package.json / lockfile / requirements） | CI 门禁：清单必须同步刷新，否则红灯 | `pnpm oss:check` |
| 每周（CNB crontab + 基座审计任务） | 扫描上游最新版本，有更新则进更新计划并开 PR | `pnpm oss:watch` |
| 安全事件（CVE / 供应链投毒） | 不等周期，立即全量扫描 | `pnpm oss:watch --all` |
| 发布前 | 复核清单新鲜度与更新计划 | `pnpm oss:plan` |

升级纪律：**扫描自动；升级走独立 PR（按 `docs/oss-update-plan.md` 组批 + 门禁 + 协议 §1/§9.5 合并），破坏性/大版本升级按 §3 人审放行**。

## 1. 登记组件（治理清单 · 人工登记 + 自动探测当前版本 + 自动扫描上游最新版本）

| # | 组件 | 开源地址 / 许可 | 当前使用版本 | 上游最新 | 状态 | 使用位置 | 注意事项 |
|---|---|---|---|---|---|---|---|
| 1 | @deepseek-ai/dsh | [github.com/deepseek-ai/harness](https://github.com/deepseek-ai/harness) | 0.1.2-rc.1 | —（未扫描） | — | packages/runtime/dsh-gate | Agent 运行时地基（DeepSeek Harness）锁版+内部 fork；永远单独一批升级，必过 E6 回归与 H-5 kill -9 重放 |
| 2 | computer-use | [github.com/anthropics/computer-use（已](https://github.com/anthropics/computer-use（已 vendor 进仓：packages/base/computer-use/toolkit，与沙箱技能同栈）) | repo-vendored-2026-08 | —（未扫描） | — | packages/base/computer-use | 三层感知（CDP/AXTree/截图）；生产工作站能力底座；publish-rpa 的 BrowserDriver 上游 |
| 3 | playwright | [github.com/microsoft/playwright](https://github.com/microsoft/playwright) | （随 computer-use 运行环境） | —（未扫描） | — | packages/base/computer-use | computer-use 浏览器驱动层；publish-rpa 适配器经 BrowserDriver 接口隔离不直 import |
| 4 | stagehand | [github.com/browserbase/stagehand](https://github.com/browserbase/stagehand) | 选型在案（剧本主路径） | —（未扫描） | — | 浏览器自动化分层执行面 | MIT，2026-07 复核活跃 |
| 5 | browser-use | [github.com/browser-use/browser-use](https://github.com/browser-use/browser-use) | 选型在案（长尾冷启动探索） | —（未扫描） | — | 浏览器自动化分层执行面 | MIT，78k+ stars |
| 6 | vite | [github.com/vitejs/vite](https://github.com/vitejs/vite) | 8.2.2 | —（未扫描） | — | apps/web, apps/webc | 前端工具链，与 @vitejs/plugin-react、@tailwindcss/vite 同批联动 |
| 7 | react | [github.com/facebook/react](https://github.com/facebook/react) | 19.2.8 | —（未扫描） | — | apps/web, apps/webc | React 19，与 react-dom 同批 |
| 8 | react-router | [github.com/remix-run/react-router](https://github.com/remix-run/react-router) | 8.3.0 | —（未扫描） | — | apps/web | — |
| 9 | @tanstack/react-query | [github.com/TanStack/query](https://github.com/TanStack/query) | 5.102.5 | —（未扫描） | — | apps/web | — |
| 10 | @trpc/server | [github.com/trpc/trpc](https://github.com/trpc/trpc) | 11.18.0 | —（未扫描） | — | apps/server, apps/web(@trpc/client) | server/client 必须同版同批 |
| 11 | hono | [github.com/honojs/hono](https://github.com/honojs/hono) | 4.13.5 | —（未扫描） | — | apps/server | MIT；与 @hono/node-server 同批 |
| 12 | tailwindcss | [github.com/tailwindlabs/tailwindcss](https://github.com/tailwindlabs/tailwindcss) | 4.3.3 | —（未扫描） | — | apps/web, apps/webc | v4 令牌制，升级后须过 Candy 设计系统纯色验证 |
| 13 | pg | [github.com/brianc/node-postgres](https://github.com/brianc/node-postgres) | 8.23.0 | —（未扫描） | — | root, packages/base | PG 驱动；现方案为原生 pg（原方案 drizzle 选型未启用，见 notes） |
| 14 | zod | [github.com/colinhacks/zod](https://github.com/colinhacks/zod) | 4.4.3 | —（未扫描） | — | apps/server, packages/base | — |
| 15 | jose | [github.com/panva/jose](https://github.com/panva/jose) | 6.2.10 | —（未扫描） | — | apps/server, packages/base | JWT/JWS |
| 16 | tsx | [github.com/privatenumber/tsx](https://github.com/privatenumber/tsx) | 4.23.12 | —（未扫描） | — | root, apps/server | — |
| 17 | vitest | [github.com/vitest-dev/vitest](https://github.com/vitest-dev/vitest) | 4.1.11 | —（未扫描） | — | root, packages/base | — |
| 18 | typescript | [github.com/microsoft/TypeScript](https://github.com/microsoft/TypeScript) | 7.0.2 | —（未扫描） | — | 全仓 | — |
| 19 | litellm | [github.com/BerriAI/litellm](https://github.com/BerriAI/litellm) | v1.89.3-stable（哈希 pin） | —（未扫描） | — | 模型网关（VPC） | ⚠ 2026-03 PyPI 供应链投毒史 + CVE-2026-42208（KEV）——强制哈希 pin + cosign 验签；安全告警即查不等周期 |
| 20 | mem0 | [github.com/mem0ai/mem0](https://github.com/mem0ai/mem0) | 自托管内核（选型在案） | —（未扫描） | — | 组织记忆引擎 | Apache-2.0 |
| 21 | presidio | [github.com/microsoft/presidio](https://github.com/microsoft/presidio) | 独立服务进程（选型在案） | —（未扫描） | — | PII 识别与脱敏 | MIT |
| 22 | langfuse | [github.com/langfuse/langfuse](https://github.com/langfuse/langfuse) | v3 自托管（选型在案） | —（未扫描） | — | 可观测性 | 2026-01 被 ClickHouse 收购；v3 footprint 重，边缘节点轻量导出契约对接 |
| 23 | deepeval | [github.com/confident-ai/deepeval](https://github.com/confident-ai/deepeval) | CI 质量门（选型在案） | —（未扫描） | — | LLM 回归评测 | Apache-2.0 |
| 24 | promptfoo | [github.com/promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) | 红队评测（选型在案） | —（未扫描） | — | LLM 红队/多模型对比 | MIT |
| 25 | skyvern | [github.com/Skyvern-AI/skyvern](https://github.com/Skyvern-AI/skyvern) | 选型在案（视觉抗改版） | —（未扫描） | — | 浏览器自动化分层执行面 | ⚠ AGPL-3.0，仅独立进程 |
| 26 | gui-agents | [github.com/simular-ai/gui-agents](https://github.com/simular-ai/gui-agents) | 选型在案（Agent-S3） | —（未扫描） | — | 桌面 GUI 操控 | Apache-2.0；与沙箱 computer-use 能力对照复核 |
| 27 | openadapt | [github.com/OpenAdaptAI/OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) | 选型在案（录制回放） | —（未扫描） | — | 流程固化 | MIT |
| 28 | wrenai | [github.com/Canner/WrenAI](https://github.com/Canner/WrenAI) | 独立进程（选型在案） | —（未扫描） | — | 消息图谱 NL 检索（G1） | Apache-2.0；仅用 governed text-to-SQL 引擎 |
| 29 | lago | [github.com/getlago/lago](https://github.com/getlago/lago) | 停车场（可选独立计费进程） | —（未扫描） | — | 计量计费 | ⚠ AGPL-3.0，仅独立部署 |
| 30 | copilotkit | [github.com/CopilotKit/CopilotKit](https://github.com/CopilotKit/CopilotKit) | 选型在案（AG-UI 组件库） | —（未扫描） | — | Agent 卡片/审批组件 | MIT；仅作组件库，消息流由 dsh 承接 |
| 31 | echarts | [github.com/apache/echarts](https://github.com/apache/echarts) | 选型在案 | —（未扫描） | — | 工作台图表 | Apache-2.0 |
| 32 | tauri | [github.com/tauri-apps/tauri](https://github.com/tauri-apps/tauri) | 2.x（桌面壳） | —（未扫描） | — | apps/desktop | Apache-2.0/MIT |
| 33 | pgvector | [github.com/pgvector/pgvector](https://github.com/pgvector/pgvector) | PG17+pgvector（docker workloom-im-pg） | —（未扫描） | — | 数据存储 | PostgreSQL License |
| 34 | litestream | [github.com/benbjohnson/litestream](https://github.com/benbjohnson/litestream) | 选型在案（社区版备份） | —（未扫描） | — | 边缘节点 SQLite 备份 | Apache-2.0 |
| 35 | browserskill | [github.com/Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill) | 观察项（2026-08-26 评估：Borrow 协议可借鉴） | —（未扫描） | — | 浏览器桥接对照组 | MIT；publish-rpa 真机发布的权限模型参照，暂不入运行时 |
| 36 | browseract | [github.com/browser-act/skills](https://github.com/browser-act/skills) | 选型入库（技能市场执行面技能 · 可选） | —（未扫描） | — | skills/registry/browser-act（执行面技能，L2 审批安装） | MIT；freemium 云依赖（stealth>5/代理/打码付费）——适配纪律：proxyMode=custom-only、凭据客户本机、出站 api.browseract.com 全声明过三段瀑布；集成评估与适配见 skills/official/component-integration |
| 37 | scrapling | [github.com/D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) · BSD-3-Clause | 历史签名技能包（本轮未升级或运行） | —（未扫描） | 观察项 | skills/registry/scrapling-collector | 保留技能市场 1.0.0 既有 HMAC 包与 registry manifest；目录登记不证明安装或执行。Tiger 根 Python requirements.txt 固定 scrapling==0.5.1，原生链路仅解析已获取 HTML 字节；普通 HTTP 采集由 Tiger transport 独立执行，动态浏览器/stealth/XHR/Spider/代理/MCP 未启用。旧 SKILL.md 中 DynamicFetcher、capture_xhr 等示例不作为本轮入口；不得从 egressDomains=[] 推断采集任务零出站。授权依赖来源继续按合同与真实凭据就绪状态披露。详见 skills/registry/scrapling-collector/NATIVE-INTEGRATION.md |
| 38 | kaykit-adventurers | [github.com/KayKit-Game-Assets/KayKit-Character-Pack-Adventures-1.0](https://github.com/KayKit-Game-Assets/KayKit-Character-Pack-Adventures-1.0) | 运行时素材（真人风数字员工角色/动画/道具） | —（未扫描） | — | apps/web/public/models/kaykit（5 角色 GLB + 道具，CC0 可商用免署名） | CC0 许可证；76 组骨骼动画；贴图内嵌单文件；装备节点显隐做外观差异化 |
| 39 | Kokoro-82M（织伴本机女声模型） | [github.com/hexgrad/kokoro](https://github.com/hexgrad/kokoro) · Apache-2.0 | 11751c087b4bbeed031e2b687b11dda698bd27ba0509472f960b57f835a999f7 | —（未扫描） | 素材/资产 | 本机可选织伴语音包（zf_001，已试听选定；不随基础客户端打包） | 固定 Kokoro-82M-v1.1-zh ONNX INT8 图与声线集 SHA-256；约 168 MB，配套 ZHG2P(version=1.1)，英文回调显式保留。产品所有者在 A/B/C 试听后选定 A / zf_001，旧小妮口音验收撤回。CPU 子进程推理，不支持任意情绪指令。导出器 MIT，模型 Apache-2.0；更新必须重跑听感、文本完整性、延迟与回退。 |
| 40 | Misaki（织伴中文文本转发音） | [github.com/hexgrad/misaki](https://github.com/hexgrad/misaki) · Apache-2.0 | 0.9.4 | 0.9.4（历史记录；未核实或已过期） | 独立服务 | 已有语音工位 venv；scripts/install-loommate-voice.py 安装可选 zh 依赖 | 可选本机语音环境；固定 0.9.4 中的中文 1.1 发音前端与 EspeakFallback 英文回调。发音版本和依赖计入缓存，不混用旧模型发音。 |
| 41 | mlx-audio（既有个人克隆工位） | [github.com/Blaizzy/mlx-audio](https://github.com/Blaizzy/mlx-audio) · MIT | 0.5.5 | 0.5.7（历史记录；未核实或已过期） | 独立服务 | 现有个人克隆/影视工位；女声安装器不升级该共享环境的 mlx-audio | 保留已安装且实测的 0.5.5，女声安装器只校验版本。织伴默认使用独立 CPU ONNX 后端；现有影视和个人克隆模型配置保持独立。升级需重跑原工位回归。 |
| 42 | ONNX Runtime（织伴 CPU 女声推理） | [github.com/microsoft/onnxruntime](https://github.com/microsoft/onnxruntime) · MIT | 1.30.0 | 1.30.0（历史记录；未核实或已过期） | 独立服务 | 可选本机织伴工位；scripts/loommate-voice-engine.py | 固定并实测 1.30.0，显式 CPUExecutionProvider；导入前禁用遥测。不改 Node 或行业依赖锁。模型与声音不联网推理；更新需真实合成、并发、进程恢复回归。 |
| 43 | phonemizer-fork（织伴英文文本转音素） | [github.com/bootphon/phonemizer](https://github.com/bootphon/phonemizer) · GPL-3.0-or-later | 3.3.2 | 3.3.2（历史记录；未核实或已过期） | 独立服务 | 可选本机独立语音工位；不打入客户端基础包 | 官方 PyPI 固定并实测 3.3.2；用于 Misaki 英文回调，防止混合文本漏读。更新需真实中英混读、离线安装及错误回归；保留组件各自许可。 |
| 44 | espeakng-loader（织伴本机英文发音库加载） | [github.com/thewh1teagle/espeakng-loader](https://github.com/thewh1teagle/espeakng-loader) · MIT（loader）；GPL-3.0-or-later（所带 eSpeak NG） | 0.2.4 | 0.2.4（历史记录；未核实或已过期） | 独立服务 | 可选本机独立语音工位；不打入客户端基础包 | 官方 PyPI 固定并实测 0.2.4；用于 Misaki 英文回调，防止混合文本漏读。更新需真实中英混读、离线安装及错误回归；保留组件各自许可。 |

## 2. 全量直接依赖（本仓事实，含上游最新）

### 2.1 npm 直接依赖（156 个，解析自 pnpm-lock.yaml）

| 包 | 当前版本 | 声明 | 类型 | 出现位置 | 上游最新 |
|---|---|---|---|---|---|
| `@agentclientprotocol/sdk` | 1.4.0（声明） | 1.4.0 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@axe-core/playwright` | 4.13.0 | 4.13.0 | 开发 | package.json | —（未扫描） |
| `@deepseek-ai/cordis` | 4.0.2（声明） | ^4.0.2 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/cordis-plugin-hmr` | 1.0.17（声明） | ^1.0.17 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/cordis-plugin-include` | 1.0.7（声明） | ^1.0.7 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/cordis-plugin-loader` | 1.0.3（声明） | ^1.0.3 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/cordis-plugin-timer` | 1.1.4（声明） | ^1.1.4 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh` | 0.2.0-rc.2 | 0.2.0-rc.2 | 生产 | packages/runtime/dsh-gate/package.json | —（未扫描） |
| `@deepseek-ai/dsh-acp` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-acp-app` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-agent` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-agent-instructions` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-agent-tool-presentation` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-app-boot` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-attachment-local` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-base` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-bash-local` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-client-ui-agent-preset` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-client-ui-cordis` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-cmdline` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-command-compact` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-command-goal` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-compaction-basic` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-compaction-tool-result-pruner` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-cordis-client-runner` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-credentials-local` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-deepseek-llm-api-extensions` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-experimental-agent-team` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-experimental-agent-team-profile` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-experimental-code-runtime-python` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-experimental-tool-agent-team` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-fs-local` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-fs-observation-policy` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-fs-sandbox` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-goal` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-goal-round-driver` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-headless` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-home-paths` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-hooks-claude-code` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-hooks-codex` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-host-frontend-static` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-host-webserver` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-jobs-local` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-launch-environment` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-llm` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-llm-deepseek` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-llm-mock-server` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-llm-pi-ai` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-llm-replay` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-loader-smoke` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-mcp-client` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-persona` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-plan-mode` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-plugin-package-inventory-deepseek` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-pwsh-local` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-pwsh-sandbox` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-sandbox-local` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-sandbox-policy` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-schedule` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-sdk-app` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-sdk-minimal` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-session` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-session-checkpoint-policy` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-session-log-deepseek` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-session-persistence-jsonl` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-session-projection` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-session-query` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-session-reference` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-settings` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-settings-file` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-shell-env` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-skill` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-skill-filesystem` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-subagent` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-subagent-fork-in-process` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-subagent-spawn-in-process` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-subprocess-local` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-system-prompt` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-terminal` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-terminal-bash` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-time-context` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tmux-context` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-token-meter` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-ask-user` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-bash` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-bash-persistent` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-cordis` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-fs` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-fs-search` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-goal` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-jobs` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-pwsh` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-pwsh-persistent` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-ralph` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-skill` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-str-replace-editor` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-subagent` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-subagent-control` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-todo` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-web` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tool-workflow` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-tools` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-user-approval` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-web-app` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-webhook` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-webhook-github` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/dsh-workflow-worker-thread` | 0.1.2-rc.1（声明） | ^0.1.2-rc.1 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@deepseek-ai/schemastery` | 3.18.2（声明） | ^3.18.2 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `@hono/node-server` | 2.1.1（声明） | 2.1.1 | 生产 | .workloom-runtime-deps/package.json、apps/server/package.json | —（未扫描） |
| `@playwright/test` | 1.63.0（声明） | 1.63.0 | 开发 | apps/web/package.json | —（未扫描） |
| `@react-three/drei` | 10.7.8（声明） | ^10.7.8 | 生产 | apps/web/package.json | —（未扫描） |
| `@react-three/fiber` | 9.7.0（声明） | ^9.7.0 | 生产 | apps/web/package.json | —（未扫描） |
| `@react-three/postprocessing` | 3.1.1（声明） | ^3.1.1 | 生产 | apps/web/package.json | —（未扫描） |
| `@tailwindcss/vite` | 4.3.3（声明） | 4.3.3 | 开发/生产 | .workloom-runtime-deps/package.json、apps/web/package.json、apps/webc/package.json | —（未扫描） |
| `@tanstack/react-query` | 5.103.1（声明） | 5.103.1 | 生产 | apps/web/package.json | —（未扫描） |
| `@trpc/client` | 11.19.0（声明） | 11.19.0 | 生产 | apps/web/package.json、apps/webb/package.json | —（未扫描） |
| `@trpc/server` | 11.19.0（声明） | 11.19.0 | 生产 | .workloom-runtime-deps/package.json、apps/server/package.json | —（未扫描） |
| `@types/js-yaml` | 4.0.9（声明） | ^4.0.9 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@types/node` | 24.0.0 / 24.13.3 | ^24.0.0 | 开发 | apps/server/package.json、package.json | —（未扫描） |
| `@types/pg` | 8.23.1（声明） | ^8.23.1 | 开发 | apps/server/package.json、packages/base/package.json、packages/db/package.json 等 4 处 | —（未扫描） |
| `@types/react` | 19.2.0（声明） | ^19.2.0 | 开发 | apps/web/package.json、apps/webb/package.json、apps/webc/package.json | —（未扫描） |
| `@types/react-dom` | 19.2.0（声明） | ^19.2.0 | 开发 | apps/web/package.json、apps/webb/package.json、apps/webc/package.json | —（未扫描） |
| `@types/three` | 0.186.0（声明） | ^0.186.0 | 开发 | apps/web/package.json | —（未扫描） |
| `@types/ws` | 8.18.1（声明） | 8.18.1 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `@vitejs/plugin-react` | 6.1.1（声明） | 6.1.1 | 开发/生产 | .workloom-runtime-deps/package.json、apps/web/package.json、apps/webb/package.json 等 4 处 | —（未扫描） |
| `@workloom/ui` | 0.1.17（声明） | 0.1.17 | 生产 | apps/web/package.json、apps/webb/package.json、apps/webc/package.json | —（未扫描） |
| `commander` | 15.0.0（声明） | ^15.0.0 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `concurrently` | 9.2.4 | ^9.1.0 | 开发 | package.json | —（未扫描） |
| `drizzle-orm` | 0.45.2（声明） | 0.45.2 / ^0.45.2 | 生产 | .workloom-runtime-deps/package.json、packages/db/package.json | —（未扫描） |
| `electron` | 44.1.1 | ^44.1.1 | 开发 | package.json | —（未扫描） |
| `electron-builder` | 26.15.3 | ^26.15.3 | 开发 | package.json | —（未扫描） |
| `execa` | 10.0.0（声明） | ^10.0.0 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `hono` | 4.13.8（声明） | 4.13.8 | 生产 | .workloom-runtime-deps/package.json、apps/server/package.json | —（未扫描） |
| `jose` | 6.2.12（声明） | 6.2.12 | 生产 | .workloom-runtime-deps/package.json、apps/server/package.json、packages/base/package.json | —（未扫描） |
| `js-yaml` | 4.2.0（声明） | ^4.2.0 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `jsdom` | 30.1.0 | ^30.0.1 / ^30.1.0 | 开发 | apps/web/package.json、apps/webc/package.json、package.json | —（未扫描） |
| `node-addon-require-builtin` | 0.1.4（声明） | ^0.1.4 | 生产 | vendor/dsh/package.json | —（未扫描） |
| `pg` | 8.23.0 | 8.23.0 | 生产 | .workloom-runtime-deps/package.json、package.json、packages/base/package.json 等 5 处 | —（未扫描） |
| `pixi-live2d-display` | 0.4.0（声明） | ^0.4.0 | 生产 | apps/web/package.json | —（未扫描） |
| `pixi.js` | 6.5.10（声明） | ^6.5.10 | 生产 | apps/web/package.json | —（未扫描） |
| `postprocessing` | 6.39.5（声明） | ^6.39.5 | 生产 | apps/web/package.json | —（未扫描） |
| `react` | 19.2.8（声明） | 19.2.8 | 生产 | apps/web/package.json、apps/webb/package.json、apps/webc/package.json | —（未扫描） |
| `react-dom` | 19.2.8（声明） | 19.2.8 | 生产 | apps/web/package.json、apps/webb/package.json、apps/webc/package.json | —（未扫描） |
| `react-router` | 8.4.0（声明） | 8.4.0 | 生产 | apps/web/package.json | —（未扫描） |
| `tailwindcss` | 4.3.3（声明） | 4.3.3 | 开发 | apps/web/package.json、apps/webc/package.json | —（未扫描） |
| `three` | 0.186.0（声明） | ^0.186.0 | 生产 | apps/web/package.json | —（未扫描） |
| `three-stdlib` | 2.36.1（声明） | ^2.36.1 | 生产 | apps/web/package.json | —（未扫描） |
| `tsx` | 4.23.12 / 4.23.13 | 4.23.12 / ^4.23.12 / ^4.23.13 | 开发/生产 | .workloom-runtime-deps/package.json、apps/server/package.json、package.json 等 4 处 | —（未扫描） |
| `typescript` | 5.9.0 / 7.0.2 | ^5.9.0 / ^7.0.2 | 开发 | apps/server/package.json、apps/web/package.json、apps/webb/package.json 等 10 处 | —（未扫描） |
| `typescript-governance → npm:typescript` | 5.9.3 | npm:typescript@5.9.3 | 开发 | package.json | —（未扫描） |
| `vite` | 8.3.0（声明） | 8.3.0 | 开发/生产 | .workloom-runtime-deps/package.json、apps/web/package.json、apps/webb/package.json 等 4 处 | —（未扫描） |
| `vitest` | 4.1.11 / 5.0.1 | ^4.1.11 / ^5.0.1 | 开发 | apps/webb/package.json、package.json、packages/base/package.json 等 6 处 | —（未扫描） |
| `ws` | 8.21.0（声明） | 8.21.0 | 开发 | vendor/dsh/package.json | —（未扫描） |
| `yaml` | 2.9.0 | 2.9.0 | 开发/生产 | .workloom-runtime-deps/package.json、package.json、packages/base/package.json | —（未扫描） |
| `yaml-governance → npm:yaml` | 2.9.0 | npm:yaml@2.9.0 | 开发 | package.json | —（未扫描） |
| `zod` | 4.6.5（声明） | 4.6.5 / ^4.6.5 | 生产 | .workloom-runtime-deps/package.json、apps/server/package.json、packages/base/package.json 等 6 处 | —（未扫描） |

### 2.2 Python 依赖（5 个）

| 包 | 当前版本 | 声明 | 出现位置 | 上游最新 |
|---|---|---|---|---|
| `av` | 18.1.0 | ==18.1.0 | scripts/whiteboard/engine/requirements.txt | —（未扫描） |
| `numpy` | 2.5.3 | ==2.5.3 | scripts/whiteboard/engine/requirements.txt | —（未扫描） |
| `opencv-python` | 5.0.0.93 | ==5.0.0.93 | scripts/whiteboard/engine/requirements.txt | —（未扫描） |
| `Pillow` | 12.3.0 | ==12.3.0 | scripts/whiteboard/engine/requirements.txt | —（未扫描） |
| `playwright` | >=1.40.0（下限声明） | >=1.40.0 | packages/base/computer-use/toolkit/requirements.txt | —（未扫描） |

### 2.3 容器镜像（1 个）

| 镜像 | 出现位置 |
|---|---|
| `pgvector/pgvector:pg17` | docker-compose.yml#postgres |

### 2.4 工具链

| 项 | 版本/要求 | 来源 |
|---|---|---|
| Node.js | `>=24.0.0` | package.json#engines |
| pnpm | `pnpm@10.14.0` | package.json#packageManager |
| 桌面载荷 npm | `11.17.0` | .workloom-runtime-deps/metadata.json |

## 3. 有可用更新

已核实记录未发现可用更新；历史缓存、失败及未扫描项不在此结论内。

