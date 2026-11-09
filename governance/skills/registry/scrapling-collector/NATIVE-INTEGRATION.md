# Tiger 原生 Scrapling 解析与历史技能包边界

本说明适用于 Tiger 的情报获取链路，核对日期为 2026-11-06。根 Python 依赖固定为 `scrapling==0.5.1`；本轮使用的是已获取 HTML 字节的解析能力。技能市场目录中原有 `SKILL.md`、`dist.json`、`dist.package.json` 和 registry manifest 仍是同一份历史 1.0.0 技能资产，本轮没有重新签名、发布、安装或运行该技能包。

## 当前原生执行面

原生入口是根目录的 `trading_system/intelligence/scrapling_parser.py::apply_scrapling`，由 `parse_worker.py` 在可信 `parser_policy.html_engine` 明确选择 `scrapling` 时调用。它使用 `from scrapling import Selector` 解析 Tiger 已获取并保留的原始字节，设置 `adaptive=False`、`huge_tree=False`，不会自行调用 Scrapling Fetcher、浏览器、代理或 MCP。`Selector` 的公开导出及版本可查 [0.5.1 官方源码](https://github.com/D4Vinci/Scrapling/blob/v0.5.1/scrapling/__init__.py)；依赖版本与 Python 要求可查 [官方 PyPI 包页](https://pypi.org/project/scrapling/0.5.1/)。

CSS 选择器、预期词和资源上限来自可信配置。正文根节点、标题和配置中的预期词校验失败时，结果保留为 `partial`，并记录 `PARSER_SELECTOR_UNVERIFIED`；这些结构校验不能代替来源权威性、实体归属、时间和内容授权判断。解析结果记录实际安装版本、选择器、配置摘要和校验结果，原始字节仍由 Tiger 存档。

默认解析边界为 60 秒、1,000 页、32 MiB 输入、16 MiB 文本和 768 MiB 内存，准确默认值及合法配置范围以 `trading_system/intelligence/parse_limits.py::ParsePolicy` 为准。PDF 及显式 Scrapling HTML 解析在一次性子进程中执行；Linux 使用地址空间限制，macOS 由父进程采样 RSS 并终止超限子进程，另有墙钟与 CPU 边界。RSS 采样不是内核强制的瞬时内存上限。子进程只获得最小环境，不继承模型或来源凭据；这里没有声称存在网络沙箱。

超时、内存、页数、输入或文本上限失败会保留原文并返回明确的部分结果及错误回执，不把被截断内容当作完整证据。`tests/test_intelligence_parse_limits.py` 包含真实子进程和实际安装 Scrapling 的离线回归；这类解析测试不证明某个外部来源已经可用。

## 普通 HTTP 与 P1 边界

普通 HTTP 请求由 Tiger 自有来源注册、transport、预算和归档链路负责。使用本地 Selector 解析并不意味着整个采集任务零出站，也不意味着任何 URL 自动取得了采集许可。来源地址、用途、授权合同及凭据必须从可信配置装配，并以实际调用和回执判断就绪。

本轮没有接入 Scrapling 的动态浏览器、StealthyFetcher、XHR 捕获、Spider、代理轮换、验证码处理或 Scrapling MCP，也没有安装对应浏览器载荷。历史 `SKILL.md` 中的 `DynamicFetcher`、`capture_xhr`、`scrapling[fetchers]` 及 `scrapling install` 示例不能作为本轮原生链路的执行说明。不得为处理失败自动尝试动态、隐身或挑战绕行。

需要登录、许可或订阅的 P1 来源只有在可信合同、允许用途和实际所需凭据均就绪时，才可能通过普通 HTTP 适配接入；来源目录、示例配置和文档登记本身不能把状态变成 `connected`。缺授权、缺凭据、许可过期、用途不符或页面挑战都应保持可读缺口，不能由解析器接入或安装 Python 包来掩盖。

## 历史技能包与派生清单

`governance/scripts/skill-forge.mts` 读取 `SKILL.md` 和 `dist.json`，通过预检后使用 `SKILL_DIST_SIGNING_KEY` 生成真实 HMAC-SHA256 分发包，并更新 `skills/registry/manifest.json`。`packages/base/skill-ops/signature.ts` 定义签名载荷；普通内容 SHA-256 不能替代该签名。本轮环境未提供签名密钥，故保留历史包的版本、正文、元数据、签名及 registry 版本，不生成虚假市场升级回执，也不把它接入新主链路。将来升级市场技能应完整更新内容、版本、真实签名和 manifest，并单独验证安装与执行。

根 `oss-components.json` 的 Scrapling 项登记原生解析依赖，当前版本由 `requirements.txt` 自动探测；治理壳同名项仍表示历史技能资产。两份 `docs/OPEN_SOURCE_COMPONENTS.md` 分别由各自的 `scripts/oss-inventory.mjs` 离线生成。生成清单与运行 `--check` 不等于完成上游全量扫描；`oss-watch` 的成功时间及历史缓存保持原有真实状态。

安装根 Python 依赖应使用根目录 `requirements.txt`，它只要求 Scrapling 核心解析包，不要求 fetchers/AI extras 或浏览器下载。本轮原生解析的成功证据、P1 来源的授权状态和历史技能市场包的状态必须分别读取各自回执。
